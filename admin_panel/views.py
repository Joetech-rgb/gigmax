from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from orders.notifications import notify_reseller_approved, notify_reseller_declined, notify_api_turned_on, notify_api_turned_off
from django.db.models import Sum, Count
from django.utils import timezone
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from collections import defaultdict
from accounts.decorators import admin_required
from accounts.models import User, AgentSignupRequest
from orders.models import Order, GeneratedLink
from bundles.models import BundlePackage
from afa.models import AFAApplication
from admin_panel.models import SiteSettings

def _apply_order_filters(request, queryset):
    search = request.GET.get('search', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    status_filter = request.GET.get('status_filter', '')
    if search:
        queryset = queryset.filter(recipient__icontains=search) | queryset.filter(order_id__icontains=search)
    if date_from:
        queryset = queryset.filter(created_at__date__gte=date_from)
    if date_to:
        queryset = queryset.filter(created_at__date__lte=date_to)
    if status_filter:
        queryset = queryset.filter(status=status_filter)
    return queryset


@admin_required
def dashboard_view(request):
    from datetime import timedelta
    from django.db.models.functions import TruncDate
    from django.db.models import Sum, Count
    from collections import defaultdict
    from .models import SiteSettings
    from afa.models import AFAApplication, AFAPrice

    today            = timezone.now().date()
    today_start      = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end        = today_start + timedelta(days=1)
    week_ago         = today - timedelta(days=7)
    fifteen_days_ago = today - timedelta(days=15)

    # â”€â”€ Simple counts â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    total_orders     = Order.objects.count()
    total_revenue    = Order.objects.aggregate(t=Sum('price_paid'))['t'] or 0
    daily_orders     = Order.objects.filter(created_at__gte=today_start, created_at__lt=today_end).count()
    daily_revenue    = Order.objects.filter(created_at__gte=today_start, created_at__lt=today_end).aggregate(t=Sum('price_paid'))['t'] or 0
    total_users      = User.objects.count()
    active_users     = User.objects.filter(last_login__date__gte=week_ago).count()
    pending_requests = AgentSignupRequest.objects.filter(status='pending').count()
    waiting_orders   = Order.objects.filter(status='waiting').count()
    kuntanii_unverified_count = Order.objects.filter(status='kuntanii_unverified').count()
    waiting_orders_amount = Order.objects.filter(status='waiting').aggregate(
        total=Sum('price_paid')
    )['total'] or 0
    refunded_orders  = Order.objects.filter(status='refunded').count()

    top_sellers_today = (
        User.objects
        .filter(orders_placed__created_at__gte=today_start, orders_placed__created_at__lt=today_end)
        .annotate(order_count=Count('orders_placed'))
        .order_by('-order_count')[:5]
    )

    # â”€â”€ Today's MTN stats â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    mtn_today_qs = Order.objects.filter(
        network='MTN',
        created_at__gte=today_start,
        created_at__lt=today_end,
        status__in=['processing', 'delivered'],
        gb_package__isnull=False,
    )
    mtn_api_revenue    = mtn_today_qs.filter(via_api=True).aggregate(t=Sum('gb_package__base_price'))['t'] or 0
    mtn_manual_revenue = mtn_today_qs.filter(via_api=False).aggregate(t=Sum('gb_package__base_price'))['t'] or 0
    mtn_total_today    = float(mtn_api_revenue) + float(mtn_manual_revenue)
    mtn_today_profit   = float(mtn_today_qs.aggregate(t=Sum('price_paid'))['t'] or 0) - \
                         float(mtn_today_qs.aggregate(t=Sum('gb_package__base_price'))['t'] or 0)
    mtn_daily_profit   = round(mtn_today_profit, 2)

    mtn_agent_debt = float(
        User.objects.filter(role__in=['moderate', 'mega', 'super_mega'])
        .aggregate(t=Sum('debt_balance'))['t'] or 0
    )

    # â”€â”€ MTN 15-day history â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    rows = list(
        Order.objects.filter(
            network='MTN',
            status__in=['processing', 'delivered'],
            created_at__date__gte=fifteen_days_ago,
            gb_package__isnull=False,
        )
        .annotate(day=TruncDate('created_at'))
        .values('day', 'source', 'placed_by_id', 'placed_by__full_name', 'via_api')
        .annotate(
            total_base=Sum('gb_package__base_price'),
            total_paid=Sum('price_paid'),
            order_count=Count('id'),
        )
    )

    agent_debts = {
        str(u.id): float(u.debt_balance)
        for u in User.objects.filter(role__in=['moderate', 'mega', 'super_mega'])
    }

    daily = defaultdict(lambda: {
        'api_revenue': 0, 'manual_revenue': 0,
        'order_count': 0, 'profit': 0,
        'agent_orders': defaultdict(lambda: {'name': '', 'base': 0, 'count': 0, 'debt': 0, 'profit': 0})
    })

    for r in rows:
        d      = r['day']
        base   = float(r['total_base'] or 0)
        paid   = float(r['total_paid'] or 0)
        profit = paid - base
        count  = r['order_count']
        daily[d]['order_count'] += count
        daily[d]['profit']      += profit
        if r['via_api']:
            daily[d]['api_revenue'] += base
        else:
            daily[d]['manual_revenue'] += base
        if r['placed_by_id']:
            uid = str(r['placed_by_id'])
            daily[d]['agent_orders'][uid]['name']   = r['placed_by__full_name'] or ''
            daily[d]['agent_orders'][uid]['base']   += base
            daily[d]['agent_orders'][uid]['count']  += count
            daily[d]['agent_orders'][uid]['debt']   = agent_debts.get(uid, 0)
            daily[d]['agent_orders'][uid]['profit'] += profit

    from orders.models import GeneratedLinkPrice as GLP
    from django.db.models import Q as DQ

    mtn_history = []
    for date in sorted(daily.keys(), reverse=True)[:15]:
        d     = daily[date]
        total = d['api_revenue'] + d['manual_revenue']

        # -- Customer (guest) orders for this day --
        guest_qs = Order.objects.filter(
            network='MTN',
            created_at__date=date,
            payment_status='paid',
            gb_package__isnull=False,
            generated_link__isnull=True,
        ).filter(DQ(placed_by__isnull=True) | DQ(placed_by__role='customer'))
        guest_base   = float(guest_qs.aggregate(t=Sum('gb_package__base_price'))['t'] or 0)
        guest_paid   = float(guest_qs.aggregate(t=Sum('price_paid'))['t'] or 0)
        guest_count  = guest_qs.count()
        guest_profit = round(guest_paid - guest_base, 2)

        # -- Generated link orders for this day --
        link_qs = Order.objects.filter(
            network='MTN',
            created_at__date=date,
            payment_status='paid',
            gb_package__isnull=False,
            generated_link__isnull=False,
        ).select_related('gb_package', 'generated_link__agent')
        link_count  = link_qs.count()
        link_profit = 0.0
        link_base   = 0.0
        for o in link_qs:
            lp = GLP.objects.filter(link=o.generated_link, bundle=o.gb_package).first()
            if lp and o.generated_link and o.generated_link.agent:
                agent_cost = float(o.gb_package.get_price_for_user(o.generated_link.agent))
                link_profit += float(lp.retail_price) - agent_cost
            link_base += float(o.gb_package.base_price)
        link_profit = round(link_profit, 2)
        link_base   = round(link_base, 2)

        mtn_history.append({
            'date':            date,
            'api_revenue':     round(d['api_revenue'], 2),
            'manual_revenue':  round(d['manual_revenue'], 2),
            'total_revenue':   round(total, 2),
            'profit':          round(d['profit'], 2),
            'order_count':     d['order_count'],
            'agent_breakdown': sorted(d['agent_orders'].values(), key=lambda x: x['base'], reverse=True),
            'guest_count':     guest_count,
            'guest_base':      round(guest_base, 2),
            'guest_profit':    guest_profit,
            'link_count':      link_count,
            'link_base':       link_base,
            'link_profit':     link_profit,
        })

    # â”€â”€ Recent MTN transactions â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    recent_transactions_qs = Order.objects.filter(
        network='MTN',
        status__in=['processing', 'delivered'],
        gb_package__isnull=False,
    ).select_related('gb_package', 'placed_by').order_by('-created_at')[:15]

    recent_transactions = [
        {
            'order_id':  o.order_id,
            'date':      o.created_at,
            'recipient': o.recipient,
            'gb_size':   o.gb_package.gb_size,
            'paid':      float(o.price_paid),
            'base':      float(o.gb_package.base_price),
            'profit':    round(float(o.price_paid) - float(o.gb_package.base_price), 2),
            'source':    o.source,
            'placed_by': o.placed_by.full_name if o.placed_by else 'Guest',
        }
        for o in recent_transactions_qs
    ]

    # â”€â”€ Gati stuck check â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    from datetime import timedelta as td
    gati_stuck_count = Order.objects.filter(
        network='MTN', status='processing',
        created_at__lt=timezone.now() - td(hours=1),
    ).exclude(gati_order_id__isnull=True).exclude(gati_order_id='').count()

    # â”€â”€ Other Networks: TELECEL, AT_EXPIRY, AT_NOEXPIRY â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    other_networks = ['TELECEL', 'AT_EXPIRY', 'AT_NOEXPIRY']

    other_networks_today = []
    for network in other_networks:
        qs      = Order.objects.filter(
            network=network,
            created_at__gte=today_start,
            created_at__lt=today_end,
            payment_status='paid',
            gb_package__isnull=False,
        )
        revenue = float(qs.aggregate(t=Sum('price_paid'))['t'] or 0)
        cost    = float(qs.aggregate(t=Sum('gb_package__base_price'))['t'] or 0)
        count   = qs.count()
        other_networks_today.append({
            'network': network,
            'revenue': round(revenue, 2),
            'cost':    round(cost, 2),
            'profit':  round(revenue - cost, 2),
            'count':   count,
        })

    other_network_history = []
    for network in other_networks:
        net_rows = list(
            Order.objects.filter(
                network=network,
                payment_status='paid',
                created_at__date__gte=fifteen_days_ago,
                gb_package__isnull=False,
            )
            .annotate(day=TruncDate('created_at'))
            .values('day')
            .annotate(
                total_paid=Sum('price_paid'),
                total_base=Sum('gb_package__base_price'),
                order_count=Count('id'),
            )
            .order_by('-day')[:15]
        )
        history = [
            {
                'date':    r['day'],
                'revenue': round(float(r['total_paid'] or 0), 2),
                'cost':    round(float(r['total_base'] or 0), 2),
                'profit':  round(float(r['total_paid'] or 0) - float(r['total_base'] or 0), 2),
                'count':   r['order_count'],
            }
            for r in net_rows
        ]
        other_network_history.append({
            'network':       network,
            'history':       history,
            'total_profit':  round(sum(h['profit'] for h in history), 2),
            'total_revenue': round(sum(h['revenue'] for h in history), 2),
        })

    # â”€â”€ AFA Profit â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    try:
        afa_prices     = {p.role: float(p.price) for p in AFAPrice.objects.all()}
        customer_price = afa_prices.get('customer_base', 0)
        agent_price    = afa_prices.get('agent_base', 0)
    except Exception:
        customer_price = 0
        agent_price    = 0

    def calc_afa_revenue(qs):
        agent_count    = qs.filter(agent__isnull=False).count()
        customer_count = qs.filter(agent__isnull=True).count()
        return round(agent_count * agent_price + customer_count * customer_price, 2)

    afa_today_qs      = AFAApplication.objects.filter(created_at__gte=today_start, created_at__lt=today_end)
    afa_today_count   = afa_today_qs.count()
    afa_today_revenue = calc_afa_revenue(afa_today_qs)
    afa_today_profit  = afa_today_revenue

    afa_days = (
        AFAApplication.objects
        .filter(created_at__date__gte=fifteen_days_ago)
        .annotate(day=TruncDate('created_at'))
        .values_list('day', flat=True)
        .distinct()
        .order_by('-day')[:15]
    )
    afa_history = []
    for day in afa_days:
        day_qs  = AFAApplication.objects.filter(created_at__date=day)
        revenue = calc_afa_revenue(day_qs)
        afa_history.append({
            'date':    day,
            'count':   day_qs.count(),
            'revenue': revenue,
            'profit':  revenue,
        })

    # â”€â”€ Combined profit today â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    other_profit_today = sum(n['profit'] for n in other_networks_today)
    total_profit_today = round(mtn_daily_profit + other_profit_today + afa_today_profit, 2)

    return render(request, 'admin_panel/dashboard.html', {
        'total_orders':           total_orders,
        'total_revenue':          total_revenue,
        'daily_orders':           daily_orders,
        'daily_revenue':          daily_revenue,
        'total_users':            total_users,
        'active_users':           active_users,
        'pending_requests':       pending_requests,
        'waiting_orders':         waiting_orders,
        'kuntanii_unverified_count': kuntanii_unverified_count,
        'waiting_orders_amount':  waiting_orders_amount,
        'refunded_orders':        refunded_orders,
        'top_sellers_today':      top_sellers_today,
        'mtn_api_revenue':        round(float(mtn_api_revenue), 2),
        'mtn_manual_revenue':     round(float(mtn_manual_revenue), 2),
        'mtn_total_today':        round(mtn_total_today, 2),
        'mtn_agent_debt':         round(mtn_agent_debt, 2),
        'mtn_daily_profit':       mtn_daily_profit,
        'mtn_history':            mtn_history,
        'recent_transactions':    recent_transactions,
        'today':                  today,
        'site_open':              SiteSettings.get().is_open,
        'mtn_enabled':            SiteSettings.get().mtn_enabled,
        'gati_stuck_count':       gati_stuck_count,
        'other_networks_today':   other_networks_today,
        'other_network_history':  other_network_history,
        'afa_today_count':        afa_today_count,
        'afa_today_revenue':      afa_today_revenue,
        'afa_today_profit':       afa_today_profit,
        'afa_history':            afa_history,
        'afa_customer_price':     customer_price,
        'afa_agent_price':        agent_price,
        'total_profit_today':     total_profit_today,
        'other_profit_today':     round(other_profit_today, 2),
    })

@admin_required
def mtn_api_orders_view(request):
    import threading
    from orders.gati import check_gati_status, send_to_gati
    from .models import GatiSettings

    gati_enabled = GatiSettings.get().is_enabled

    processing_ids = list(
        Order.objects.filter(network='MTN', via_api=True, status='processing')
        .exclude(gati_order_id__isnull=True).exclude(gati_order_id='')
        .values_list('gati_order_id', flat=True)
    )

    missing_gati = list(
        Order.objects.filter(
            network='MTN', via_api=True,
            payment_status='paid', status='pending',
            gati_order_id__isnull=True,
            kuntanii_reference__isnull=True,
        )
    ) + list(
        Order.objects.filter(
            network='MTN', via_api=True,
            payment_status='paid', status='pending',
            gati_order_id='',
            kuntanii_reference__isnull=True,
        )
    )

    def _sync():
        from orders.dispatch import dispatch_mtn_order
        for gid in processing_ids:
            try:
                check_gati_status(gid)
            except Exception:
                pass
        for o in missing_gati:
            try:
                if o.gb_package:
                    reference = dispatch_mtn_order(
                        recipient=o.recipient,
                        gb_size=float(o.gb_package.gb_size),
                        order_id=o.order_id,
                    )
                    if reference:
                        Order.objects.filter(order_id=o.order_id).update(
                            via_api=True,
                        )
            except Exception as e:
                print(f'[Retry Gati] Error for {o.order_id}: {e}')

    if gati_enabled and (processing_ids or missing_gati):
        threading.Thread(target=_sync, daemon=True).start()

    orders = Order.objects.filter(network='MTN', via_api=True).values(
        'order_id', 'recipient', 'status', 'price_paid', 'created_at',
        'gb_package__gb_size', 'placed_by__full_name'
    ).order_by('-created_at')
    orders = _apply_order_filters(request, orders)

    from django.core.paginator import Paginator
    paginator = Paginator(orders, 100)
    page = request.GET.get('page', 1)
    orders = paginator.get_page(page)

    from orders.models import BlockedRecipient
    blocked_numbers = set(BlockedRecipient.objects.values_list('recipient', flat=True))

    return render(request, 'admin_panel/mtn_api_orders.html', {
        'orders'         : orders,
        'gati_enabled'   : gati_enabled,
        'blocked_numbers': blocked_numbers,
    })


@admin_required
def all_api_orders_view(request):
    import threading
    from orders.gati import check_gati_status

    processing_ids = list(
        Order.objects.filter(source__iexact='api', status='processing')
        .exclude(gati_order_id__isnull=True).exclude(gati_order_id='')
        .values_list('gati_order_id', flat=True)
    )
    def _sync():
        for gid in processing_ids:
            try:
                check_gati_status(gid)
            except Exception:
                pass
    if processing_ids:
        threading.Thread(target=_sync, daemon=True).start()

    orders = Order.objects.filter(source__iexact='api').values(
        'order_id', 'recipient', 'status', 'price_paid', 'created_at',
        'gb_package__gb_size', 'placed_by__full_name'
    ).order_by('-created_at')
    orders = _apply_order_filters(request, orders)

    from django.core.paginator import Paginator
    paginator = Paginator(orders, 100)
    page = request.GET.get('page', 1)
    orders = paginator.get_page(page)

    return render(request, 'admin_panel/mtn_api_orders.html', {'orders': orders})

@admin_required
def mtn_manual_orders_view(request):
    import threading
    from django.db import models as db_models
    from orders.gati import check_gati_status
    from django.core.paginator import Paginator
    from .models import GatiSettings

    from orders.kuntanii import is_kuntanii_enabled
    gati_enabled     = GatiSettings.get().is_enabled
    kuntanii_enabled = is_kuntanii_enabled()
    any_processor_enabled = gati_enabled or kuntanii_enabled

    processing_ids = list(
        Order.objects.filter(network='MTN', via_api=False, status='processing')
        .exclude(gati_order_id__isnull=True).exclude(gati_order_id='')
        .values_list('gati_order_id', flat=True)
    )

    def _sync():
        for gid in processing_ids:
            try:
                check_gati_status(gid)
            except Exception:
                pass

    if processing_ids:
        threading.Thread(target=_sync, daemon=True).start()

    # ── Kuntanii stuck-order reconciliation ──
    # No webhook guarantee yet, so sweep orders that have been sitting in
    # 'processing' with a kuntanii_reference for 15+ minutes and re-check
    # their live status directly with Kuntanii.
    from django.utils import timezone as _tz
    from datetime import timedelta as _timedelta
    stuck_threshold = _tz.now() - _timedelta(minutes=15)
    kuntanii_stuck_ids = list(
        Order.objects.filter(
            network='MTN', status='processing',
            kuntanii_reference__isnull=False,
            updated_at__lt=stuck_threshold,
        ).exclude(kuntanii_reference='')
        .values_list('order_id', flat=True)
    )

    def _kuntanii_sync():
        from orders.kuntanii import check_kuntanii_status
        for oid in kuntanii_stuck_ids:
            try:
                check_kuntanii_status(oid)
            except Exception:
                pass

    if kuntanii_stuck_ids:
        threading.Thread(target=_kuntanii_sync, daemon=True).start()

    if any_processor_enabled:
        queryset = Order.objects.filter(
            network='MTN',
            via_api=False,
        ).select_related('gb_package', 'placed_by').order_by('-created_at')
    else:
        queryset = Order.objects.filter(
            network='MTN',
        ).filter(
            db_models.Q(via_api=False) |
            db_models.Q(via_api=True, status__in=['pending', 'processing'], payment_status='paid')
        ).select_related('gb_package', 'placed_by').order_by('-created_at')

    queryset = _apply_order_filters(request, queryset)

    if any_processor_enabled:
        pending_count = Order.objects.filter(
            network='MTN',
            via_api=False,
            status='pending',
            payment_status='paid',
        ).count()
    else:
        pending_count = Order.objects.filter(
            network='MTN',
            via_api=False,
            status='pending',
            payment_status='paid',
        ).count()
        pending_count += Order.objects.filter(
            network='MTN',
            via_api=True,
            status='pending',
            payment_status='paid',
            gati_order_id__isnull=True,
            kuntanii_reference__isnull=True,
        ).count()

    paginator   = Paginator(queryset, 100)
    page_number = request.GET.get('page', 1)
    orders      = paginator.get_page(page_number)

    from orders.models import BlockedRecipient
    blocked_numbers = set(BlockedRecipient.objects.values_list('recipient', flat=True))

    return render(request, 'admin_panel/mtn_manual_orders.html', {
        'orders'         : orders,
        'paginator'      : paginator,
        'pending_count'  : pending_count,
        'gati_enabled'   : gati_enabled,
        'blocked_numbers': blocked_numbers,
    })

@admin_required
def agent_requests_view(request):
    pending_requests = AgentSignupRequest.objects.filter(status='pending')
    return render(request, 'admin_panel/agent_requests.html', {'requests': pending_requests})


@admin_required
def other_networks_view(request):
    from django.db.models import Sum, Count
    from django.db.models.functions import TruncDate
    from datetime import timedelta

    orders = Order.objects.exclude(network='MTN').order_by('-created_at')
    orders = _apply_order_filters(request, orders)

    network_summary = list(
        Order.objects.exclude(network='MTN')
        .filter(payment_status='paid', gb_package__isnull=False)
        .values('network')
        .annotate(
            total_revenue=Sum('price_paid'),
            total_cost=Sum('gb_package__base_price'),
            order_count=Count('id'),
        )
        .order_by('-total_revenue')
    )
    for s in network_summary:
        s['profit']        = round(float(s['total_revenue'] or 0) - float(s['total_cost'] or 0), 2)
        s['total_revenue'] = round(float(s['total_revenue'] or 0), 2)
        s['total_cost']    = round(float(s['total_cost'] or 0), 2)

    fifteen_days_ago = timezone.now().date() - timedelta(days=15)
    daily_rows = list(
        Order.objects.exclude(network='MTN')
        .filter(payment_status='paid', gb_package__isnull=False, created_at__date__gte=fifteen_days_ago)
        .annotate(day=TruncDate('created_at'))
        .values('day', 'network')
        .annotate(
            total_paid=Sum('price_paid'),
            total_base=Sum('gb_package__base_price'),
            count=Count('id'),
        )
        .order_by('-day')
    )
    for r in daily_rows:
        r['profit']  = round(float(r['total_paid'] or 0) - float(r['total_base'] or 0), 2)
        r['revenue'] = round(float(r['total_paid'] or 0), 2)

    return render(request, 'admin_panel/other_networks.html', {
        'orders':          orders,
        'network_summary': network_summary,
        'daily_rows':      daily_rows,
    })


@admin_required
def debt_page_view(request):
    from .models import DebtPayment
    from django.db.models import Q

    add_search  = request.GET.get('add_search', '')
    add_results = []
    if add_search:
        add_results = User.objects.filter(
            Q(full_name__icontains=add_search) | Q(phone_number__icontains=add_search),
            role__in=['small_agent','moderate','mega','super_mega','extreme']
        )[:10]

    if request.method == 'POST':
        agent_id  = request.POST.get('agent_id')
        amount    = request.POST.get('amount')
        note      = request.POST.get('note', '')
        agent     = get_object_or_404(User, pk=agent_id)

        try:
            amount = float(amount)
            if amount <= 0:
                raise ValueError
            if amount > float(agent.debt_balance):
                amount = float(agent.debt_balance)

            balance_before = float(agent.debt_balance)
            balance_after  = max(0.0, balance_before - amount)

            DebtPayment.objects.create(
                agent          = agent,
                amount         = amount,
                note           = note,
                cleared_by     = request.user,
                balance_before = balance_before,
                balance_after  = balance_after,
            )

            agent.debt_balance = balance_after
            agent.save()

            try:
                from orders.notifications import notify_debt_deducted
                notify_debt_deducted(agent, amount, float(agent.debt_balance), float(agent.wallet_balance))
            except Exception:
                pass

            messages.success(request, f'GHS {amount:.2f} cleared from {agent.full_name}. Remaining: GHS {agent.debt_balance:.2f}')
        except (ValueError, TypeError):
            messages.error(request, 'Invalid amount entered.')

        return redirect('/admin-panel/debt/')

    agents = User.objects.filter(
        Q(debt_page_access=True) | Q(debt_balance__gt=0)
    ).order_by('-debt_balance')

    for agent in agents:
        agent.payment_history = DebtPayment.objects.filter(
            agent=agent
        ).select_related('cleared_by').order_by('-created_at')

    total_debt = agents.aggregate(t=Sum('debt_balance'))['t'] or 0
    return render(request, 'admin_panel/debt_page.html', {
        'agents'     : agents,
        'add_search' : add_search,
        'add_results': add_results,
        'total_debt' : round(float(total_debt), 2),
    })


@admin_required
def afa_view(request):
    from afa.models import AFAPrice
    if request.method == 'POST':
        for role in ['customer_base', 'agent_base']:
            price = request.POST.get(f'price_{role}')
            if price:
                try:
                    AFAPrice.objects.update_or_create(
                        role=role,
                        defaults={'price': float(price)}
                    )
                except Exception:
                    pass
        messages.success(request, 'AFA prices updated.')
        return redirect('/admin-panel/afa/')
    applications = AFAApplication.objects.all().order_by('-created_at')
    prices = {p.role: p.price for p in AFAPrice.objects.all()}
    return render(request, 'admin_panel/afa_bundle.html', {
        'applications': applications,
        'prices': prices,
    })


@admin_required
def bundles_view(request):
    from bundles.views import bundles_admin_view
    return bundles_admin_view(request)


@admin_required
def api_usage_view(request):
    from django.db.models import Count
    from .models import GatiSettings

    api_users = User.objects.filter(
        role__in=['moderate', 'mega', 'super_mega']
    ).annotate(
        total_orders=Count('orders_placed')
    ).order_by('-has_api_access', '-total_orders')

    gati_users  = [u for u in api_users if 'gati' in u.full_name.lower()]
    other_users = [u for u in api_users if 'gati' not in u.full_name.lower()]
    api_users_sorted = gati_users + other_users

    from .models import KuntaniiSettings, ATSettings, PaymentGatewaySettings
    return render(request, 'admin_panel/api_usage.html', {
        'api_users':    api_users_sorted,
        'gati_enabled': GatiSettings.get().is_enabled,
        'kuntanii_enabled': KuntaniiSettings.get().is_enabled,
        'at_enabled': ATSettings.get().is_enabled,
        'payment_gateway_settings': PaymentGatewaySettings.get(),
    })


@admin_required
def generated_links_view(request):
    links = GeneratedLink.objects.all().select_related('agent')
    total_available = User.objects.filter(generated_links__isnull=False).distinct().aggregate(t=Sum('link_profit_balance'))['t'] or 0
    total_earned = User.objects.filter(generated_links__isnull=False).distinct().aggregate(t=Sum('total_link_profit_earned'))['t'] or 0
    return render(request, 'admin_panel/generated_links.html', {
        'links':           links,
        'total_available': round(float(total_available), 2),
        'total_earned':    round(float(total_earned), 2),
    })

@admin_required
def users_view(request):
    from accounts.models import AgentRole
    search = request.GET.get('search', '')
    role_filter = request.GET.get('role', '')
    users = User.objects.all().order_by('-date_joined').select_related('custom_role')
    if search:
        users = (
            User.objects.filter(full_name__icontains=search) |
            User.objects.filter(phone_number__icontains=search) |
            User.objects.filter(email__icontains=search)
        ).order_by('-date_joined').select_related('custom_role')
    if role_filter:
        users = users.filter(role=role_filter)
    total_wallet_balance = User.objects.filter(wallet_balance__gt=0).aggregate(t=Sum('wallet_balance'))['t'] or 0
    return render(request, 'admin_panel/users.html', {
        'users':                users,
        'roles':                User.Role.choices,
        'custom_roles':         AgentRole.objects.filter(is_active=True),
        'total_wallet_balance': round(float(total_wallet_balance), 2),
    })

@admin_required
def approve_agent_view(request, pk):
    signup = get_object_or_404(AgentSignupRequest, pk=pk, status='pending')

    user = User(
        phone_number = signup.phone_number,
        full_name    = signup.full_name,
        email        = signup.email,
        role         = 'small_agent',
        is_approved  = True,
    )
    user.set_password(signup.password_raw)
    user.save()

    from agents.models import AgentProfile
    AgentProfile.objects.create(
        user        = user,
        full_name   = signup.full_name,
        phone       = signup.phone_number,
        email       = signup.email,
        agent_tier  = 'small',
        is_approved = True,
    )

    signup.status = 'approved'
    try:
        notify_reseller_approved(user)
    except Exception:
        pass
    signup.save()
    messages.success(request, f'{user.full_name} approved as Agent')
    return redirect('/admin-panel/agent-requests/')


@admin_required
def decline_agent_view(request, pk):
    signup = get_object_or_404(AgentSignupRequest, pk=pk, status='pending')
    signup.status = 'declined'
    signup.save()
    try:
        notify_reseller_declined(signup)
    except Exception:
        pass
    messages.success(request, 'Request declined')
    return redirect('/admin-panel/agent-requests/')

@admin_required
def update_order_status_view(request, order_id):
    if request.method == 'POST':
        new_status = request.POST.get('status')

        order_check = Order.objects.filter(order_id=order_id).values(
            'status', 'kuntanii_reference'
        ).first()
        if not order_check:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'ok': False, 'error': 'Order not found'}, status=404)
            messages.error(request, f'Order {order_id} not found')
            return redirect(request.META.get('HTTP_REFERER', '/admin-panel/'))

        current_status = order_check['status']

        if order_check['kuntanii_reference'] and request.POST.get('confirm') != '1':
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({
                    'ok': False,
                    'needs_confirm': True,
                    'error': 'This order was dispatched to Kuntanii and may still be in progress.'
                }, status=409)
            messages.warning(request, f'Order {order_id} was dispatched to Kuntanii \u2014 resend with confirm=1 to override.')
            return redirect(request.META.get('HTTP_REFERER', '/admin-panel/'))

        if current_status == 'refunded' and new_status != 'refunded' and request.POST.get('confirm') != '1':
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({
                    'ok': False,
                    'needs_confirm': True,
                    'error': f'Order {order_id} is refunded. Moving it to "{new_status}" requires confirmation.'
                }, status=409)
            messages.warning(request, f'Order {order_id} is refunded \u2014 resend with confirm=1 to override.')
            return redirect(request.META.get('HTTP_REFERER', '/admin-panel/'))

        order_obj = Order.objects.filter(order_id=order_id).first()
        if not order_obj:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'ok': False, 'error': 'Order not found'}, status=404)
            messages.error(request, f'Order {order_id} not found')
            return redirect(request.META.get('HTTP_REFERER', '/admin-panel/'))

        # Load-then-save (not a raw .update()) so pre_save signal fires —
        # this is what triggers automatic agent wallet credit/debt reduction
        # when an order transitions into Waiting. A bare .update() bypasses
        # Django signals entirely and silently skips that crediting logic.
        order_obj.status = new_status
        order_obj.save()
        updated = 1

        from .models import OrderStatusLog
        OrderStatusLog.objects.create(
            order_id=order_id,
            old_status=current_status,
            new_status=new_status,
            changed_by=getattr(request.user, 'username', None) or request.META.get('REMOTE_ADDR'),
        )

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'ok': True, 'status': new_status})
        messages.success(request, f'Order {order_id} updated')
    return redirect(request.META.get('HTTP_REFERER', '/admin-panel/'))


@admin_required
def bulk_copy_view(request):
    from .models import BulkCopySession
    from django.db import models as db_models, transaction
    from orders.models import BlockedRecipient
    from orders.gati import handle_blocked_order, send_blocked_sms

    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid request'}, status=400)

    order_ids = request.POST.getlist('order_ids')
    network   = request.POST.get('network', 'MTN')
    # NOTE: gati_enabled no longer read from GatiSettings DB toggle — that
    # caused a bug where API-placed orders were silently excluded from bulk
    # copy whenever the toggle was on. Gati/Kuntanii are both hardcoded off
    # (see orders/gati.py, orders/kuntanii.py); the via_api-based branch
    # below has been removed entirely so this can never recur regardless of
    # any future toggle state.

    if order_ids:
        orders = Order.objects.filter(
            order_id__in=order_ids,
            status='pending',
            payment_status='paid',
            network=network,
        ).select_related('gb_package')
    else:
        orders = Order.objects.filter(
            network=network,
            status__in=['pending', 'processing'],
            payment_status='paid',
        ).filter(
            db_models.Q(gati_order_id__isnull=True) | db_models.Q(gati_order_id='')
        ).filter(
            db_models.Q(kuntanii_reference__isnull=True) | db_models.Q(kuntanii_reference='')
        ).select_related('gb_package')

    if not orders.exists():
        return JsonResponse({'error': 'No pending paid orders found.'}, status=400)

    blocked_numbers = set(BlockedRecipient.objects.values_list('recipient', flat=True))

    copied_ids  = []
    blocked_ids = []
    lines       = []

    for o in orders:
        if o.recipient in blocked_numbers:
            Order.objects.filter(order_id=o.order_id).update(
                status='delivered',
                is_blocked_order=True,
            )
            blocked_ids.append(o.order_id)
            try:
                send_blocked_sms(o.recipient)
            except Exception:
                pass
            print(f'[BlockedRecipient] Manual copy skipped {o.recipient} — order {o.order_id} marked delivered')
            continue
        copied_ids.append(o.order_id)
        gb = int(o.gb_package.gb_size) if o.gb_package else 0
        lines.append(f'{o.recipient} {gb}')

    if not copied_ids and not blocked_ids:
        return JsonResponse({'error': 'No pending paid orders found.'}, status=400)

    with transaction.atomic():
        last = BulkCopySession.objects.filter(
            network=network
        ).order_by('-created_at').select_for_update().first()

        if last is None:
            copy_number = 1
        elif last.copy_number >= 20:
            copy_number = 1
        else:
            copy_number = last.copy_number + 1

        existing = BulkCopySession.objects.filter(
            network=network,
            copy_number=copy_number
        ).first()

        if existing:
            existing.date       = timezone.now().date()
            existing.order_ids  = copied_ids
            existing.created_by = request.user
            existing.created_at = timezone.now()
            existing.save()
        else:
            BulkCopySession.objects.create(
                date        = timezone.now().date(),
                network     = network,
                copy_number = copy_number,
                order_ids   = copied_ids,
                created_by  = request.user,
            )

        if copied_ids:
            Order.objects.filter(
                order_id__in=copied_ids,
                status='pending',
            ).update(status='processing')

    network_labels = {
        'TELECEL'    : 'TELECEL',
        'AT_EXPIRY'  : 'AT',
        'AT_NOEXPIRY': 'AT NON-EXPIRY',
    }
    network_label = network_labels.get(network, '')
    copy_text     = "\n".join(lines)
    if network_label:
        copy_text += "\n" + network_label

    return JsonResponse({
        'copy_text'    : copy_text,
        'count'        : len(copied_ids),
        'copy_number'  : copy_number,
        'order_ids'    : copied_ids,
        'updated'      : len(copied_ids),
        'blocked_count': len(blocked_ids),
    })

@admin_required
def bulk_status_view(request):
    if request.method == 'POST':
        order_ids = request.POST.getlist('order_ids')
        status = request.POST.get('status', 'delivered')

        if status == 'delivered':
            # Only orders actually sent to a processor (status='processing')
            # can be marked delivered via this endpoint. Waiting orders are
            # excluded on purpose — they were never dispatched, so marking
            # them delivered would be false. They must go through Processing
            # first before they can become Delivered.
            eligible = Order.objects.filter(
                order_id__in=order_ids, status='processing'
            ).filter(
                kuntanii_reference__isnull=True, gati_order_id__isnull=True
            ).exclude(kuntanii_reference='').exclude(gati_order_id='')
            skipped  = len(order_ids) - eligible.count()
            orders_to_update = eligible
        else:
            orders_to_update = Order.objects.filter(order_id__in=order_ids)
            skipped = 0

        updated = 0
        for order in orders_to_update:
            if order.status != status:
                order.status = status
                order.save()
                updated += 1

        return JsonResponse({'updated': updated, 'skipped_not_sent': skipped})
    return JsonResponse({'error': 'Invalid'}, status=400)

@admin_required
def change_role_view(request, pk):
    from accounts.models import AgentRole
    if request.method == 'POST':
        user         = get_object_or_404(User, pk=pk)
        new_role     = request.POST.get('role')
        custom_role_id = request.POST.get('custom_role_id') or None

        # custom_role_id means a custom role was selected from dropdown
        if custom_role_id:
            custom_role = get_object_or_404(AgentRole, pk=custom_role_id)
            user.custom_role = custom_role
            # keep their base hardcoded role as small_agent if currently customer
            if user.role == 'customer':
                user.role = 'small_agent'
            user.save()
            messages.success(request, f'Custom role "{custom_role.name}" assigned to {user.full_name}')
        else:
            valid_roles = [r[0] for r in User.Role.choices]
            if new_role in valid_roles:
                user.role        = new_role
                user.custom_role = None  # clear custom role when hardcoded role is picked
                user.save()
                messages.success(request, f'Role updated to {user.get_role_display()} for {user.full_name}')
            else:
                messages.error(request, 'Invalid role selected.')
    return redirect('/admin-panel/users/')

@admin_required
def toggle_pause_view(request, pk):
    user = get_object_or_404(User, pk=pk)
    user.is_paused = not user.is_paused
    user.save()
    status = 'paused' if user.is_paused else 'active'
    messages.success(request, f'{user.full_name} is now {status}')
    return redirect('/admin-panel/users/')


@admin_required
def toggle_link_view(request, pk):
    link = get_object_or_404(GeneratedLink, pk=pk)
    link.is_active = not link.is_active
    link.save()
    return redirect('/admin-panel/generated-links/')


@admin_required
def delete_link_view(request, pk):
    link = get_object_or_404(GeneratedLink, pk=pk)
    link.delete()
    return redirect('/admin-panel/generated-links/')


@admin_required
def api_requests_list(request):
    from api_access.models import APIRequest
    pending_requests  = APIRequest.objects.filter(status='pending')
    approved_requests = APIRequest.objects.filter(status='approved')
    denied_requests   = APIRequest.objects.filter(status='denied')
    if request.method == 'POST':
        request_id = request.POST.get('request_id')
        action     = request.POST.get('action')
        api_req    = APIRequest.objects.get(id=request_id)
        if action == 'approve':
            api_req.approve(request.user)
            messages.success(request, f'API access approved for {api_req.agent.full_name}')
        elif action == 'deny':
            api_req.deny(request.user)
            messages.warning(request, f'API access denied for {api_req.agent.full_name}')
    return render(request, 'admin_panel/api_requests.html', {
        'pending_requests':  pending_requests,
        'approved_requests': approved_requests,
        'denied_requests':   denied_requests,
    })


@admin_required
def fee_settings_view(request):
    from .models import FeeConfiguration
    config = FeeConfiguration.get_current()
    if request.method == 'POST':
        config.paystack_fee_percent      = float(request.POST.get('paystack_fee', 1.5))
        config.admin_share_percent       = float(request.POST.get('admin_share', 50))
        config.agent_share_percent       = float(request.POST.get('agent_share', 50))
        config.operational_cost_percent  = float(request.POST.get('operational_cost', 0))
        config.save()
        messages.success(request, 'Fee settings updated successfully!')
        return redirect('/admin-panel/fee-settings/')
    example = config.calculate_breakdown(100)
    return render(request, 'admin_panel/fee_settings.html', {
        'config':  config,
        'example': example,
    })


@admin_required
def add_to_debt_view(request):
    if request.method == "POST":
        user_id = request.POST.get("user_id")
        user = get_object_or_404(User, pk=user_id)
        user.debt_page_access = True
        user.save(update_fields=["debt_page_access"])
        messages.success(request, f"{user.full_name} added to debt page.")
    return redirect("/admin-panel/debt/")


@admin_required
def admin_wallet_topup_view(request):
    from django.db.models import Q
    from .models import WalletTopup
    search     = request.GET.get('search', '')
    found_user = None

    if search:
        found_user = User.objects.filter(
            Q(full_name__icontains=search) |
            Q(phone_number__icontains=search) |
            Q(email__icontains=search)
        ).first()

    if request.method == 'POST':
        user_id = request.POST.get('user_id')
        amount  = request.POST.get('amount')
        note    = request.POST.get('note', 'Admin top-up')

        target_user = get_object_or_404(User, pk=user_id)
        try:
            amount = float(amount)
            if amount <= 0:
                raise ValueError
            target_user.wallet_balance = float(target_user.wallet_balance) + amount
            target_user.save(update_fields=['wallet_balance'])
            WalletTopup.objects.create(
                user=target_user,
                amount=amount,
                note=note,
                added_by=request.user,
            )
            try:
                from orders.notifications import notify_wallet_topup
                notify_wallet_topup(target_user, amount, method='Manual')
            except Exception:
                pass
            messages.success(request, f'GHS {amount:.2f} added to {target_user.full_name}\'s wallet. New balance: GHS {target_user.wallet_balance:.2f}')
        except (ValueError, TypeError):
            messages.error(request, 'Invalid amount.')

        return redirect('/admin-panel/wallet-topup/')

    topup_history = WalletTopup.objects.select_related('user', 'added_by').all()[:50]
    return render(request, 'admin_panel/wallet_topup.html', {
        'search':        search,
        'found_user':    found_user,
        'topup_history': topup_history,
    })


@admin_required
def toggle_api_access_view(request, pk):
    user = get_object_or_404(User, pk=pk)
    user.has_api_access = not user.has_api_access
    user.save(update_fields=['has_api_access'])
    status = 'enabled' if user.has_api_access else 'disabled'
    try:
        if user.has_api_access:
            notify_api_turned_on(user)
        else:
            notify_api_turned_off(user)
    except Exception:
        pass
    messages.success(request, f'API access {status} for {user.full_name}')
    return redirect('/admin-panel/api-usage/')


@admin_required
def toggle_gati_view(request):
    from .models import GatiSettings
    from orders.models import Order
    gati        = GatiSettings.get()
    was_enabled = gati.is_enabled
    gati.is_enabled = not gati.is_enabled
    gati.updated_by = request.user
    gati.save()
    status = 'enabled' if gati.is_enabled else 'disabled'

    if gati.is_enabled and not was_enabled:
        pending = Order.objects.filter(
            network='MTN',
            status='pending',
            payment_status='paid',
            gati_order_id__isnull=True,
        ).order_by('created_at')
        count = pending.count()
        if count:
            import threading
            from orders.gati import send_to_gati

            def _dispatch_all():
                for order in pending:
                    try:
                        send_to_gati(
                            recipient=order.recipient,
                            gb_size=float(order.gb_package.gb_size),
                            order_id=order.order_id,
                        )
                    except Exception as e:
                        print(f'[toggle_gati] Error dispatching {order.order_id}: {e}')

            threading.Thread(target=_dispatch_all, daemon=True).start()
            messages.success(request, f'Gati enabled. {count} pending MTN order(s) dispatched to Gati in background.')
        else:
            messages.success(request, 'Gati auto-processing enabled. No queued orders.')
    else:
        messages.success(request, 'Gati auto-processing disabled. MTN orders will now appear in Manual Orders.')

    return redirect('/admin-panel/api-usage/')


@admin_required
def toggle_kuntanii_view(request):
    from orders.kuntanii import is_kuntanii_enabled as _kuntanii_hardcode_check
    from .models import KuntaniiSettings
    from orders.models import Order

    # Hardcoded emergency lock takes priority over the DB flag. If the code-level
    # lock is active, this button must not flip the DB flag or dispatch anything.
    if _kuntanii_hardcode_check.__doc__ and 'EMERGENCY LOCK' in _kuntanii_hardcode_check.__doc__:
        messages.error(request, 'Kuntanii is locked at the code level and cannot be toggled from the admin panel right now. Contact the developer to enable it.')
        return redirect('/admin-panel/api-usage/')

    kuntanii    = KuntaniiSettings.get()
    was_enabled = kuntanii.is_enabled
    kuntanii.is_enabled = not kuntanii.is_enabled
    kuntanii.updated_by = request.user
    kuntanii.save()

    if kuntanii.is_enabled and not was_enabled:
        pending = Order.objects.filter(
            network='MTN',
            status='pending',
            payment_status='paid',
            kuntanii_reference__isnull=True,
        ).order_by('created_at')
        count = pending.count()
        if count:
            import threading
            from orders.kuntanii import send_to_kuntanii

            def _dispatch_all():
                for order in pending:
                    try:
                        send_to_kuntanii(
                            recipient=order.recipient,
                            gb_size=float(order.gb_package.gb_size),
                            order_id=order.order_id,
                        )
                    except Exception as e:
                        print(f'[toggle_kuntanii] Error dispatching {order.order_id}: {e}')

            threading.Thread(target=_dispatch_all, daemon=True).start()
            messages.success(request, f'Kuntanii enabled. {count} pending MTN order(s) dispatched to Kuntanii in background.')
        else:
            messages.success(request, 'Kuntanii auto-processing enabled. No queued orders.')
    else:
        messages.success(request, 'Kuntanii auto-processing disabled. MTN orders will now appear in Manual Orders.')

    return redirect('/admin-panel/api-usage/')


def toggle_at_view(request):
    from orders.at import is_at_enabled as _at_hardcode_check
    from .models import ATSettings
    from orders.models import Order

    # Hardcoded emergency lock takes priority over the DB flag. If the code-level
    # lock is active, this button must not flip the DB flag or dispatch anything.
    if _at_hardcode_check.__doc__ and 'EMERGENCY LOCK' in _at_hardcode_check.__doc__:
        messages.error(request, 'AT is locked at the code level and cannot be toggled from the admin panel right now. Contact the developer to enable it.')
        return redirect('/admin-panel/api-usage/')

    at_settings = ATSettings.get()
    was_enabled = at_settings.is_enabled
    at_settings.is_enabled = not at_settings.is_enabled
    at_settings.updated_by = request.user
    at_settings.save()

    if at_settings.is_enabled and not was_enabled:
        pending = Order.objects.filter(
            network__in=['AT_EXPIRY', 'AT_NOEXPIRY'],
            status='pending',
            payment_status='paid',
            dcs_order_reference__isnull=True,
        ).order_by('created_at')
        count = pending.count()
        if count:
            import threading
            from orders.at import send_to_at

            def _dispatch_all():
                for order in pending:
                    try:
                        send_to_at(
                            recipient=order.recipient,
                            gb_size=float(order.gb_package.gb_size),
                            order_id=order.order_id,
                        )
                    except Exception as e:
                        print(f'[toggle_at] Error dispatching {order.order_id}: {e}')

            threading.Thread(target=_dispatch_all, daemon=True).start()
            messages.success(request, f'AT enabled. {count} pending order(s) dispatched to AT in background.')
        else:
            messages.success(request, 'AT auto-processing enabled. No queued orders.')
    else:
        messages.success(request, 'AT auto-processing disabled.')

    return redirect('/admin-panel/api-usage/')


@admin_required
def delete_user_view(request, pk):
    if request.method == 'POST':
        user = get_object_or_404(User, pk=pk)
        name = user.full_name
        user.delete()
        messages.success(request, f'{name} has been deleted.')
    return redirect('/admin-panel/users/')


@admin_required
def order_queue_view(request):
    from collections import defaultdict

    today = timezone.now().date()
    date  = request.GET.get('date', str(today))

    orders = Order.objects.filter(
        network='MTN',
        created_at__date=date,
        status__in=['pending', 'processing']
    ).select_related('placed_by', 'gb_package').order_by('created_at')

    agent_queues = {}
    for o in orders:
        uid = str(o.placed_by.id) if o.placed_by else 'guest'
        if uid not in agent_queues:
            agent_queues[uid] = {
                'name':        o.placed_by.full_name if o.placed_by else 'Guest',
                'agent':       o.placed_by,
                'orders':      [],
                'first_order': o.created_at,
            }
        agent_queues[uid]['orders'].append(o)

    queue_list = sorted(agent_queues.values(), key=lambda x: x['first_order'])

    for i, q in enumerate(queue_list, 1):
        q['queue_number'] = i
        q['total_orders'] = len(q['orders'])

    return render(request, 'admin_panel/order_queue.html', {
        'queue_list': queue_list,
        'date':       date,
        'today':      str(today),
    })


@admin_required
def approve_debt_access_view(request, pk):
    from django.contrib.auth import get_user_model
    User = get_user_model()
    user = get_object_or_404(User, pk=pk)
    user.debt_page_access = not getattr(user, 'debt_page_access', False)
    user.save(update_fields=['debt_page_access'])
    status = 'granted' if user.debt_page_access else 'revoked'
    messages.success(request, f'Debt page access {status} for {user.full_name}')
    return redirect('/admin-panel/debt/')


@admin_required
def excel_export_view(request):
    import openpyxl
    from django.http import HttpResponse

    network = request.GET.get('network', 'MTN')
    date    = request.GET.get('date', str(timezone.now().date()))

    orders = Order.objects.filter(
        network=network,
        created_at__date=date
    ).select_related('gb_package', 'placed_by').order_by('created_at')

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"{network} Orders"
    ws.append(['No', 'Number', 'GB', 'Network', 'Status', 'Type'])

    for i, o in enumerate(orders, 1):
        gb_value = ''
        if o.gb_package:
            try:
                raw = str(o.gb_package.gb_size).upper().replace('GB', '').strip()
                gb_value = float(raw)
                if gb_value == int(gb_value):
                    gb_value = int(gb_value)
            except (ValueError, TypeError):
                gb_value = o.gb_package.gb_size

        source_label = 'API (offline)' if o.source == 'api' and not o.via_api else \
                       'API' if o.via_api else 'Manual'

        ws.append([i, o.recipient, gb_value, network, o.status, source_label])

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="{network}_orders_{date}.xlsx"'
    wb.save(response)
    return response


@admin_required
def promo_view(request):
    from .models import Promo
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create':
            Promo.objects.create(
                network    = request.POST.get('network'),
                gb         = request.POST.get('gb'),
                price      = request.POST.get('price'),
                label      = request.POST.get('label', ''),
                is_active  = True,
                created_by = request.user,
            )
            messages.success(request, 'Promo created. Previous promo deactivated.')
        elif action == 'deactivate':
            promo_id = request.POST.get('promo_id')
            Promo.objects.filter(pk=promo_id).update(is_active=False)
            messages.success(request, 'Promo deactivated.')
        return redirect('/admin-panel/promos/')

    promos = Promo.objects.all()[:20]
    return render(request, 'admin_panel/promos.html', {'promos': promos})


@admin_required
def admin_earnings_view(request):
    from .models import AdminEarning, AdminWithdrawal
    from wallet.models import WithdrawalRequest
    from django.db.models import Sum
    from decimal import Decimal

    total_margin = AdminEarning.objects.aggregate(
        t=Sum('amount')
    )['t'] or Decimal('0')

    completed_withdrawals = WithdrawalRequest.objects.filter(
        status__in=['completed', 'approved']
    ).count()
    total_service_charges = Decimal(str(completed_withdrawals)) * Decimal('1.00')

    total_withdrawn = AdminWithdrawal.objects.filter(
        status='completed'
    ).aggregate(t=Sum('amount'))['t'] or Decimal('0')

    available   = total_margin + total_service_charges - total_withdrawn
    withdrawals = AdminWithdrawal.objects.all()[:20]

    if request.method == 'POST':
        amount  = request.POST.get('amount')
        phone   = request.POST.get('phone_number', '').strip()
        network = request.POST.get('network', '').strip()

        try:
            amount = float(amount)
            if amount <= 0:
                raise ValueError
            if amount > float(available):
                messages.error(request, f'Insufficient earnings. Available: GHS {available:.2f}')
                return redirect('/admin-panel/earnings/')

            import uuid
            from wallet.gateway import create_momo_recipient as create_transfer_recipient, initiate_transfer

            reference    = f'ADMWD-{uuid.uuid4().hex[:10].upper()}'
            recip_result = create_transfer_recipient(
                name='Admin Withdrawal',
                account_number=phone,
                bank_code=network
            )

            if not recip_result.get('status'):
                messages.error(request, f'Recipient error: {recip_result.get("message", "Failed")}')
                return redirect('/admin-panel/earnings/')

            recipient_code  = recip_result['data']['recipient_code']
            transfer_result = initiate_transfer(amount, recipient_code, reason='SK Plug Admin Withdrawal')

            withdrawal = AdminWithdrawal.objects.create(
                amount=amount,
                phone_number=phone,
                network=network,
                reference=reference,
                recipient_code=recipient_code,
                status='pending',
                paystack_transfer_code=transfer_result.get('data', {}).get('transfer_code', ''),
            )

            if transfer_result.get('data', {}).get('status') in ['success', 'pending']:
                withdrawal.status       = 'completed'
                withdrawal.processed_at = timezone.now()
                withdrawal.save()
                messages.success(request, f'GHS {amount:.2f} successfully sent to {network} {phone}.')
            else:
                messages.error(request, f'Transfer failed: {transfer_result.get("message", "Unknown error")}')

        except (ValueError, TypeError):
            messages.error(request, 'Invalid amount.')

        return redirect('/admin-panel/earnings/')

    return render(request, 'admin_panel/earnings.html', {
        'total_margin':          round(float(total_margin), 2),
        'total_service_charges': round(float(total_service_charges), 2),
        'total_withdrawn':       round(float(total_withdrawn), 2),
        'available':             round(float(available), 2),
        'withdrawals':           withdrawals,
    })


def export_users_csv(request):
    import csv
    from django.http import HttpResponse
    from accounts.models import User
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="registered_users.csv"'
    writer = csv.writer(response)
    writer.writerow(['Full Name', 'Phone', 'Email', 'Role', 'Date Joined'])
    for u in User.objects.all().order_by('-date_joined'):
        writer.writerow([u.full_name, u.phone_number, u.email or '', u.get_role_display(), u.date_joined.strftime('%d %b %Y')])
    return response


def orders_analysis_view(request):
    from datetime import date
    from django.db.models import Count, Sum, Q

    today    = timezone.now().date()
    date_str = request.GET.get('date', str(today))
    try:
        selected_date = date.fromisoformat(date_str)
    except Exception:
        selected_date = today

    orders   = Order.objects.filter(created_at__date=selected_date)
    afa_apps = AFAApplication.objects.filter(created_at__date=selected_date)

    def build_row(agent=None, label=None, role_label=None):
        if agent:
            agent_orders = orders.filter(placed_by=agent)
            agent_afa    = afa_apps.filter(agent=agent)
            name         = agent.full_name
            role         = agent.get_role_display()
        else:
            agent_orders = orders.filter(Q(placed_by__role='customer') | Q(placed_by__isnull=True))
            agent_afa    = afa_apps.filter(agent__isnull=True)
            name         = label or 'All Customers'
            role         = role_label or 'Customer'

        mtn_api    = agent_orders.filter(network='MTN', via_api=True)
        mtn_manual = agent_orders.filter(network='MTN', via_api=False)
        other_net  = agent_orders.exclude(network='MTN').exclude(network='AFA')
        afa_orders = agent_orders.filter(network='AFA')

        total_orders = agent_orders.count() + agent_afa.count()
        if total_orders == 0:
            return None

        delivered  = agent_orders.filter(status='delivered').count()  + agent_afa.filter(status='delivered').count()
        pending    = agent_orders.filter(status='pending').count()    + agent_afa.filter(status='pending').count()
        processing = agent_orders.filter(status='processing').count() + agent_afa.filter(status='processing').count()
        total_gb   = agent_orders.aggregate(gb=Sum('gb_package__gb_size'))['gb'] or 0
        total_amt  = agent_orders.aggregate(amt=Sum('price_paid'))['amt'] or 0

        return {
            'name'         : name,
            'role'         : role,
            'total_orders' : total_orders,
            'delivered'    : delivered,
            'pending'      : pending,
            'processing'   : processing,
            'total_gb'     : round(float(total_gb), 2),
            'total_amount' : round(float(total_amt), 2),
            'mtn_api'      : mtn_api.count(),
            'mtn_manual'   : mtn_manual.count(),
            'other_network': other_net.count(),
            'afa'          : afa_orders.count() + agent_afa.count(),
        }

    agent_roles = ['small_agent', 'moderate', 'mega', 'super_mega', 'extreme']
    agents      = User.objects.filter(role__in=agent_roles)
    agent_data  = [row for agent in agents for row in [build_row(agent=agent)] if row]

    customer_row = build_row(label='All Customers', role_label='Customer')
    if customer_row:
        agent_data.append(customer_row)

    agent_data.sort(key=lambda x: x['total_orders'], reverse=True)
    total_orders = orders.count() + afa_apps.count()

    return render(request, 'admin_panel/orders_analysis.html', {
        'agent_data'  : agent_data,
        'date'        : selected_date,
        'total_orders': total_orders,
    })


@admin_required
def admin_withdrawals_view(request):
    from wallet.models import WithdrawalRequest
    from django.db.models import Sum

    today = timezone.now().date()

    pending_withdrawals = WithdrawalRequest.objects.filter(
        status='pending'
    ).select_related('wallet__user', 'processed_by').order_by('created_at')

    recent_withdrawals = WithdrawalRequest.objects.exclude(
        status='pending'
    ).select_related('wallet__user', 'processed_by').order_by('-processed_at')[:30]

    pending_count   = pending_withdrawals.count()
    pending_total   = pending_withdrawals.aggregate(t=Sum('amount'))['t'] or 0
    completed_today = WithdrawalRequest.objects.filter(
        status='completed',
        processed_at__date=today
    ).count()

    return render(request, 'admin_panel/withdrawals.html', {
        'pending_withdrawals': pending_withdrawals,
        'recent_withdrawals':  recent_withdrawals,
        'pending_count':       pending_count,
        'pending_total':       pending_total,
        'completed_today':     completed_today,
    })

@admin_required
def complete_withdrawal_view(request, pk):
    """
    Delegates to wallet.views.approve_withdrawal_view â€” correct logic:
    - Only marks 'approved' when Paystack actually returns a transfer_code
    - Refunds correct wallet (link_profit_balance vs wallet_balance) on failure
    - Shows clear error to admin when Paystack fails instead of silently completing
    """
    from wallet.views import approve_withdrawal_view
    return approve_withdrawal_view(request, withdrawal_id=pk)

@admin_required
def reject_withdrawal_view(request, pk):
    """
    Delegates to wallet.views.reject_withdrawal_view â€” correct logic:
    - WLP- references refund to link_profit_balance
    - WD- references refund to wallet_balance
    """
    from wallet.views import reject_withdrawal_view as _reject
    return _reject(request, withdrawal_id=pk)


@admin_required
def correct_wallet_topup_view(request, pk):
    from .models import WalletTopup, WalletTopupCorrection
    from decimal import Decimal

    topup = get_object_or_404(WalletTopup, pk=pk)

    age = timezone.now() - topup.created_at
    if age.total_seconds() > 86400:
        messages.error(request, 'This top-up is older than 24 hours and can no longer be corrected.')
        return redirect('/admin-panel/wallet-topup/')

    if topup.corrections.exists():
        messages.error(request, 'This top-up has already been corrected.')
        return redirect('/admin-panel/wallet-topup/')

    if request.method == 'POST':
        correct_amount_raw = request.POST.get('correct_amount', '')
        note               = request.POST.get('note', '').strip()

        try:
            correct_amount = Decimal(correct_amount_raw)
            if correct_amount < 0:
                raise ValueError('Amount must be positive.')
        except Exception:
            messages.error(request, 'Invalid amount entered.')
            return redirect(f'/admin-panel/wallet-topup/correct/{pk}/')

        wrong_amount = Decimal(str(topup.amount))
        difference   = correct_amount - wrong_amount

        agent                = topup.user
        agent.wallet_balance = Decimal(str(agent.wallet_balance)) + difference
        if agent.wallet_balance < 0:
            agent.wallet_balance = Decimal('0.00')
        agent.save(update_fields=['wallet_balance'])

        WalletTopupCorrection.objects.create(
            original_topup = topup,
            corrected_by   = request.user,
            wrong_amount   = wrong_amount,
            correct_amount = correct_amount,
            difference     = difference,
            note           = note,
        )

        try:
            from orders.notifications import notify_wallet_topup
            if difference > 0:
                notify_wallet_topup(agent, float(difference), method='Correction (addition)')
        except Exception:
            pass

        action = f"added GHS {difference:.2f} more" if difference > 0 else f"deducted GHS {abs(difference):.2f}"
        messages.success(
            request,
            f'Correction applied: {action} for {agent.full_name}. New balance: GHS {agent.wallet_balance:.2f}'
        )
        return redirect('/admin-panel/wallet-topup/')

    return render(request, 'admin_panel/correct_topup.html', {'topup': topup})


@admin_required
def customer_orders_view(request):
    from django.db.models import Q
    orders = Order.objects.filter(
        Q(placed_by__role='customer') |
        Q(placed_by__isnull=True) |
        Q(source='guest_checkout') |
        Q(source='generated_link')
    ).select_related('placed_by', 'gb_package').order_by('-created_at')
    orders = _apply_order_filters(request, orders)
    source_filter = request.GET.get('source', '')
    if source_filter:
        orders = orders.filter(source=source_filter)
    return render(request, 'admin_panel/customer_orders.html', {'orders': orders})


@admin_required
def bulk_sessions_view(request):
    from .models import BulkCopySession

    sessions = BulkCopySession.objects.filter(network='MTN').order_by('-copy_number')
    data = []
    for s in sessions:
        order_ids  = s.order_ids
        total      = len(order_ids)
        delivered  = Order.objects.filter(order_id__in=order_ids, status='delivered').count()
        processing = Order.objects.filter(order_id__in=order_ids, status='processing').count()
        waiting    = Order.objects.filter(order_id__in=order_ids, status='waiting').count()
        data.append({
            'id':               s.pk,
            'copy_number':      s.copy_number,
            'date':             s.date.strftime('%d %b %Y'),
            'time':             timezone.localtime(s.created_at).strftime('%H:%M'),
            'count':            total,
            'all_delivered':    delivered == total and total > 0,
            'processing_count': processing,
            'waiting_count':    waiting,
        })

    return JsonResponse({'sessions': data})


@admin_required
def bulk_session_deliver_view(request):
    from .models import BulkCopySession

    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid request'}, status=400)

    session_id = request.POST.get('session_id')
    session    = get_object_or_404(BulkCopySession, pk=session_id, network='MTN')

    # Only orders actually still in Processing get marked Delivered here.
    # Waiting orders are excluded on purpose — a manual admin decision to
    # move an order to Waiting must never be silently overridden by this
    # bulk session action.
    updated = Order.objects.filter(
        order_id__in=session.order_ids,
        status='processing',
    ).update(status='delivered')

    return JsonResponse({
        'updated':     updated,
        'copy_number': session.copy_number,
    })


@admin_required
def toggle_site_view(request):
    from .models import SiteSettings

    if request.method != 'POST':
        return redirect('/admin-panel/dashboard/')

    confirm_password = request.POST.get('confirm_password', '').strip()

    if not confirm_password:
        messages.error(request, 'Password is required to toggle the site.')
        return redirect('/admin-panel/dashboard/')

    if not request.user.check_password(confirm_password):
        messages.error(request, 'Incorrect password. Site status was NOT changed.')
        return redirect('/admin-panel/dashboard/')

    site = SiteSettings.get()
    site.is_open   = not site.is_open
    site.closed_by = request.user
    site.save()
    status = 'opened' if site.is_open else 'closed'
    messages.success(request, f'Site is now {status}.')
    return redirect('/admin-panel/dashboard/')


@admin_required
def notices_view(request):
    from .models import Notice

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'create':
            title      = request.POST.get('title', '').strip()
            message    = request.POST.get('message', '').strip()
            priority   = request.POST.get('priority', 'info')
            expires_at = request.POST.get('expires_at', '').strip()

            if not title or not message:
                messages.error(request, 'Title and message are required.')
                return redirect('/admin-panel/notices/')

            Notice.objects.create(
                title      = title,
                message    = message,
                priority   = priority,
                expires_at = expires_at or None,
                created_by = request.user,
                is_active  = True,
            )
            messages.success(request, 'Notice published. Agents will see it on their dashboard.')

        elif action == 'toggle':
            pk     = request.POST.get('notice_id')
            notice = Notice.objects.filter(pk=pk).first()
            if notice:
                notice.is_active = not notice.is_active
                notice.save(update_fields=['is_active'])
                status = 'activated' if notice.is_active else 'paused'
                messages.success(request, f'Notice "{notice.title}" {status}.')

        elif action == 'delete':
            pk = request.POST.get('notice_id')
            Notice.objects.filter(pk=pk).delete()
            messages.success(request, 'Notice deleted.')

        return redirect('/admin-panel/notices/')

    notices = Notice.objects.all()[:50]
    return render(request, 'admin_panel/notices.html', {'notices': notices})


@csrf_exempt
def gati_sync_view(request):
    import hmac
    from django.conf import settings
    from django.db.models import Q as _Q
    from orders.gati import check_gati_status, recover_lost_orders, check_gati_health
    from admin_panel.models import GatiSettings

    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    secret   = request.headers.get('X-Sync-Secret', '')
    expected = getattr(settings, 'GATI_SYNC_SECRET', '')
    if not expected or not hmac.compare_digest(secret, expected):
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    gati_enabled = GatiSettings.get().is_enabled

    # ── 4 ALWAYS RUNS — even when Gati is off ───────────────────────────────
    # Confirms payments that Paystack took but never got linked to an order.
    # Has nothing to do with Gati — just fixes payment status.
    rescued_payments = 0
    try:
        from wallet.gateway import verify_transaction
        from wallet.views import (
            _confirm_order_payment,
            _credit_link_profit,
            _create_order_from_metadata,
        )
        from orders.models import BlockedRecipient
        from datetime import timedelta

        blocked_numbers = set(
            BlockedRecipient.objects.values_list('recipient', flat=True)
        )

        cutoff = timezone.now() - timedelta(hours=24)

        suspect = Order.objects.filter(
            payment_status__in=['pending', 'failed'],
            created_at__gte=cutoff,
        ).filter(
            _Q(payment_reference__isnull=False) &
            ~_Q(payment_reference='')
        ).order_by('-created_at')[:50]

        for o in suspect:
            if o.recipient in blocked_numbers:
                continue
            try:
                result  = verify_transaction(o.payment_reference)
                tx_data = result.get('data', {})
                if tx_data.get('status') == 'success':
                    confirmed = _confirm_order_payment(o.payment_reference)
                    if not confirmed:
                        _create_order_from_metadata(tx_data, o.payment_reference)
                    rescued_payments += 1
                    print(
                        f'[gati_sync] Rescued missed payment: '
                        f'{o.order_id} | {o.payment_reference}'
                    )
            except Exception as e:
                print(
                    f'[gati_sync] Payment rescue error for '
                    f'{o.order_id} | {o.payment_reference}: {e}'
                )

    except Exception as e:
        print(f'[gati_sync] Payment rescue block error: {e}')

    # ── ORPHAN PAYMENT RECOVERY — ALWAYS RUNS ───────────────────────────────
    # Catches payments where session died before verify_payment() ran.
    # These have NO order in the DB at all — money sits in Paystack forever.
    # Goes directly to Paystack as source of truth, not the DB.
    orphans_recovered = 0
    try:
        from orders.delivery_utils import recover_orphan_payments
        orphans_recovered = recover_orphan_payments()
        if orphans_recovered:
            print(f'[gati_sync] Orphan payments recovered: {orphans_recovered}')
    except Exception as e:
        print(f'[gati_sync] recover_orphan_payments error: {e}')

    # ── GATI ONLY BLOCKS — skip entirely when Gati is off ───────────────────
    synced         = 0
    updated        = 0
    recovered      = 0
    rescued_nogati = 0

    if not gati_enabled:
        print('[gati_sync] Gati is disabled. Skipping Gati blocks.')
        print(
            f'[gati_sync] rescued_payments={rescued_payments} '
            f'orphans_recovered={orphans_recovered}'
        )
        return JsonResponse({
            'synced':             synced,
            'updated':            updated,
            'recovered':          recovered,
            'rescued_payments':   rescued_payments,
            'orphans_recovered':  orphans_recovered,
            'rescued_nogati':     rescued_nogati,
            'gati_enabled':       False,
        })

    # ── 1. RECOVER LOST ORDERS ───────────────────────────────────────────────
    try:
        recovered = recover_lost_orders()
    except Exception as e:
        print(f'[gati_sync] recover_lost_orders error: {e}')
        recovered = 0

    # ── 2. HEALTH CHECK ──────────────────────────────────────────────────────
    try:
        check_gati_health()
    except Exception as e:
        print(f'[gati_sync] check_gati_health error: {e}')

    # ── 3. SYNC ORDERS ALREADY IN GATI ──────────────────────────────────────
    processing = Order.objects.filter(
        network='MTN',
        status__in=['pending', 'processing'],
    ).exclude(
        gati_order_id__isnull=True
    ).exclude(
        gati_order_id=''
    ).order_by('created_at')[:50]

    for order in processing:
        try:
            result = check_gati_status(order.gati_order_id)
            synced += 1
            if result in ('SUCCESS', 'FAILED'):
                updated += 1
        except Exception as e:
            print(f'[gati_sync] Error on {order.gati_order_id}: {e}')

    # ── 5. RESCUE MTN ORDERS PAID BUT NEVER SENT TO A PROCESSOR ─────────────
    # via_api=True ensures we never touch manual orders waiting for bulk copy.
    try:
        from orders.dispatch import dispatch_mtn_order
        from orders.models import BlockedRecipient as BR
        from datetime import timedelta as td

        _blocked = set(BR.objects.values_list('recipient', flat=True))
        _cutoff  = timezone.now() - td(hours=48)

        _nogati = Order.objects.filter(
            network='MTN',
            payment_status='paid',
            status__in=['pending', 'processing'],
            via_api=True,
            created_at__gte=_cutoff,
            gb_package__isnull=False,
        ).filter(
            _Q(gati_order_id__isnull=True) | _Q(gati_order_id='')
        ).filter(
            _Q(kuntanii_reference__isnull=True) | _Q(kuntanii_reference='')
        ).order_by('created_at')[:20]

        for _o in _nogati:
            if _o.recipient in _blocked:
                continue
            try:
                _ref = dispatch_mtn_order(
                    recipient=_o.recipient,
                    gb_size=float(_o.gb_package.gb_size),
                    order_id=_o.order_id,
                )
                if _ref:
                    Order.objects.filter(order_id=_o.order_id).update(
                        via_api=True,
                    )
                    rescued_nogati += 1
                    print(f'[mtn_sync] Rescued order: {_o.order_id} -> {_ref}')
                else:
                    print(f'[mtn_sync] Rescue failed: {_o.order_id}')
            except Exception as _e:
                print(f'[mtn_sync] Error {_o.order_id}: {_e}')

    except Exception as _e:
        print(f'[mtn_sync] Rescue block error: {_e}')


    print(
        f'[gati_sync] synced={synced} updated={updated} recovered={recovered} '
        f'rescued_payments={rescued_payments} orphans_recovered={orphans_recovered} '
        f'rescued_nogati={rescued_nogati}'
    )

    return JsonResponse({
        'synced':             synced,
        'updated':            updated,
        'recovered':          recovered,
        'rescued_payments':   rescued_payments,
        'orphans_recovered':  orphans_recovered,
        'rescued_nogati':     rescued_nogati,
        'gati_enabled':       True,
    })
    
@admin_required
def blocked_recipients_view(request):
    from orders.models import BlockedRecipient, Order
    from django.db.models import Sum, Count

    RECOVERY_TARGET = 2381.00

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add':
            recipient = request.POST.get('recipient', '').strip()
            reason    = request.POST.get('reason', 'Double delivery on 3rd May 2026').strip()
            if recipient:
                BlockedRecipient.objects.get_or_create(
                    recipient=recipient,
                    defaults={'reason': reason, 'added_by': request.user}
                )
                messages.success(request, f'{recipient} added to blocked list.')
        elif action == 'remove':
            recipient_id = request.POST.get('recipient_id')
            obj = BlockedRecipient.objects.filter(pk=recipient_id).first()
            if obj:
                messages.success(request, f'{obj.recipient} removed. They will now receive bundles normally.')
                obj.delete()
        return redirect('/admin-panel/blocked-recipients/')

    search_blocked = request.GET.get('search_blocked', '').strip()
    if search_blocked:
        blocked = list(BlockedRecipient.objects.filter(recipient__icontains=search_blocked))
    else:
        blocked = list(BlockedRecipient.objects.all())

    stats_qs = (
        Order.objects
        .filter(is_blocked_order=True)
        .values('recipient')
        .annotate(
            total=Sum('price_paid'),
            count=Count('id')
        )
    )
    stats_map = {s['recipient']: s for s in stats_qs}

    blocked_data = []
    for b in blocked:
        s         = stats_map.get(b.recipient, {})
        recovered = float(s.get('total') or 0)
        blocked_data.append({
            'obj'      : b,
            'recovered': round(recovered, 2),
            'count'    : s.get('count', 0),
        })

    total_recovered = float(
        Order.objects.filter(is_blocked_order=True)
        .aggregate(t=Sum('price_paid'))['t'] or 0
    )

    recovery_percent = min(100, round((total_recovered / RECOVERY_TARGET) * 100, 1)) if RECOVERY_TARGET else 0

    return render(request, 'admin_panel/blocked_recipients.html', {
        'blocked_data'    : blocked_data,
        'total_recovered' : round(total_recovered, 2),
        'recovery_target' : RECOVERY_TARGET,
        'recovery_percent': recovery_percent,
        'remaining'       : round(max(0, RECOVERY_TARGET - total_recovered), 2),
        'total_blocked'   : len(blocked_data),
        'search_blocked'  : search_blocked,
    })

@admin_required
def agents_waiting_refund_overview_view(request):
    from decimal import Decimal
    from django.db.models import Q as _Q2
    from orders.utils import calculate_owed_amount

    AGENT_ROLES = ['small_agent', 'moderate', 'mega', 'super_mega', 'extreme']
    agents = User.objects.filter(role__in=AGENT_ROLES).order_by('full_name')

    rows = []
    grand_waiting_count  = 0
    grand_waiting_amount = Decimal('0')
    grand_waiting_retail = Decimal('0')
    grand_refund_count   = 0
    grand_refund_amount  = Decimal('0')
    grand_refund_retail  = Decimal('0')

    for agent in agents:
        agent_links = GeneratedLink.objects.filter(agent=agent)
        agent_orders = Order.objects.filter(
            _Q2(placed_by=agent) | _Q2(generated_link__in=agent_links)
        ).distinct()

        waiting_qs = agent_orders.filter(status='waiting')
        refund_qs  = agent_orders.filter(status='refunded')

        waiting_count  = waiting_qs.count()
        waiting_amount = sum((calculate_owed_amount(o) for o in waiting_qs), Decimal('0'))
        waiting_retail = sum((Decimal(str(o.price_paid or 0)) for o in waiting_qs), Decimal('0'))
        refund_count   = refund_qs.count()
        refund_amount  = sum((calculate_owed_amount(o) for o in refund_qs), Decimal('0'))
        refund_retail  = sum((Decimal(str(o.price_paid or 0)) for o in refund_qs), Decimal('0'))

        if waiting_count == 0 and refund_count == 0:
            continue

        today_refund_qs = refund_qs.filter(created_at__date=timezone.now().date())
        today_refund_count  = today_refund_qs.count()
        today_refund_amount = sum((calculate_owed_amount(o) for o in today_refund_qs), Decimal('0'))

        rows.append({
            'agent'               : agent,
            'waiting_count'       : waiting_count,
            'waiting_amount'      : waiting_amount,
            'waiting_retail'      : waiting_retail,
            'refund_count'        : refund_count,
            'refund_amount'       : refund_amount,
            'refund_retail'       : refund_retail,
            'today_refund_count'  : today_refund_count,
            'today_refund_amount' : today_refund_amount,
        })

        grand_waiting_count  += waiting_count
        grand_waiting_amount += waiting_amount
        grand_waiting_retail += waiting_retail
        grand_refund_count   += refund_count
        grand_refund_amount  += refund_amount
        grand_refund_retail  += refund_retail

    from django.utils import timezone as _tz
    as_of = _tz.now()

    return render(request, 'admin_panel/agents_waiting_refund.html', {
        'as_of': as_of,
        'rows'                 : rows,
        'grand_waiting_count'  : grand_waiting_count,
        'grand_waiting_amount' : grand_waiting_amount,
        'grand_waiting_retail' : grand_waiting_retail,
        'grand_refund_count'   : grand_refund_count,
        'grand_refund_amount'  : grand_refund_amount,
        'grand_refund_retail'  : grand_refund_retail,
        'grand_total_amount'   : grand_waiting_amount + grand_refund_amount,
        'total_agents'         : len(rows),
    })


@admin_required
def agent_todays_refund_detail_view(request, pk):
    from datetime import datetime
    from django.utils import timezone as _tz
    from orders.utils import get_agent_refund_detail

    agent = get_object_or_404(User, pk=pk)

    date_from_str = request.GET.get('date_from') or request.GET.get('date')
    date_to_str   = request.GET.get('date_to')

    try:
        date_from = datetime.strptime(date_from_str, '%Y-%m-%d').date() if date_from_str else datetime(2026, 8, 26).date()
        date_to   = datetime.strptime(date_to_str, '%Y-%m-%d').date() if date_to_str else _tz.localdate()
    except ValueError:
        return JsonResponse({'error': 'Invalid date format, expected YYYY-MM-DD'}, status=400)

    items, total_amount, total_debt_reduced, total_wallet_credited, count = get_agent_refund_detail(agent, date_from, date_to)

    data = [{
        'time'           : item['time'].strftime('%H:%M'),
        'amount'         : str(item['amount']),
        'source_label'   : item['source_label'],
        'debt_before'    : str(item['debt_before']) if item['debt_before'] is not None else None,
        'debt_after'     : str(item['debt_after']) if item['debt_after'] is not None else None,
        'wallet_before'  : str(item['wallet_before']) if item['wallet_before'] is not None else None,
        'wallet_after'   : str(item['wallet_after']) if item['wallet_after'] is not None else None,
        'debt_reduced'   : str(item['debt_reduced']) if item['debt_reduced'] else None,
        'wallet_credited': str(item['wallet_credited']) if item['wallet_credited'] else None,
        'description'    : item['description'],
    } for item in items]

    return JsonResponse({
        'agent_name'            : agent.full_name,
        'date_from'             : date_from.isoformat(),
        'date_to'               : date_to.isoformat(),
        'count'                 : count,
        'total_amount'          : str(total_amount),
        'total_debt_reduced'    : str(total_debt_reduced),
        'total_wallet_credited' : str(total_wallet_credited),
        'refunds'   : data,
    })


@admin_required
def convert_agent_kuntanii_unverified_to_refund_view(request, pk):
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid'}, status=400)

    from django.db.models import Q as _Q4

    agent = get_object_or_404(User, pk=pk)
    agent_links = GeneratedLink.objects.filter(agent=agent)

    # Load-then-save each order (not a bulk .update()) so pre_save fires per
    # order — this is what triggers automatic agent wallet credit / debt
    # reduction. A bulk .update() bypasses Django signals entirely and
    # silently skips that crediting logic (same root cause found and fixed
    # in orders/dispatch.py and orders/kuntanii.py on 2026-09-15).
    orders_qs = Order.objects.filter(
        _Q4(placed_by=agent) | _Q4(generated_link__in=agent_links),
        status='kuntanii_unverified',
    ).distinct()

    updated = 0
    for o in orders_qs:
        o.status = 'refunded'
        o.save()
        updated += 1

    return JsonResponse({'updated': updated, 'agent': agent.full_name})


@admin_required
def kuntanii_unverified_view(request):
    from decimal import Decimal
    from orders.utils import calculate_owed_amount

    orders_qs = Order.objects.filter(
        status='kuntanii_unverified',
    ).select_related(
        'gb_package', 'placed_by', 'generated_link', 'generated_link__agent'
    ).order_by('-kuntanii_unverified_at')

    agent_groups = {}
    total_count = 0
    grand_agent_cost = Decimal('0')

    for o in orders_qs:
        agent = o.placed_by or (o.generated_link.agent if o.generated_link else None)
        agent_key = agent.pk if agent else 'unknown'
        agent_name = agent.full_name if agent else 'Unknown / Direct Customer'

        if agent_key not in agent_groups:
            agent_groups[agent_key] = {
                'agent_name': agent_name,
                'agent_pk': agent.pk if agent else None,
                'orders': [],
                'agent_cost_total': Decimal('0'),
            }

        agent_cost = calculate_owed_amount(o)

        agent_groups[agent_key]['orders'].append({
            'phone': o.recipient,
            'gb_size': o.gb_package.gb_size if o.gb_package else None,
            'placed_at': o.created_at,
            'reason': o.kuntanii_unverified_reason,
            'flagged_at': o.kuntanii_unverified_at,
            'source': o.get_source_display() if hasattr(o, 'get_source_display') else o.source,
            'order_id': o.order_id,
            'retail_price': Decimal(str(o.price_paid or 0)),
            'agent_cost': agent_cost,
        })
        agent_groups[agent_key]['agent_cost_total'] += agent_cost
        grand_agent_cost += agent_cost
        total_count += 1

    groups = sorted(agent_groups.values(), key=lambda g: -len(g['orders']))

    return render(request, 'admin_panel/kuntanii_unverified.html', {
        'groups': groups,
        'total_count': total_count,
        'total_agents': len(groups),
        'grand_agent_cost': grand_agent_cost,
    })


@admin_required
def convert_agent_waiting_to_refund_view(request, pk):
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid'}, status=400)

    if request.POST.get('confirm') != '1':
        return JsonResponse({
            'error': 'This marks orders as refunded and logs them as paid. '
                     'Resend with confirm=1 once the agent has actually been paid.'
        }, status=400)

    from django.db.models import Q as _Q3
    from django.utils import timezone as _tz_refund
    from orders.utils import calculate_owed_amount, _apply_refund_credit

    agent = get_object_or_404(User, pk=pk)
    agent_links = GeneratedLink.objects.filter(agent=agent)

    orders = Order.objects.filter(
        _Q3(placed_by=agent) | _Q3(generated_link__in=agent_links),
        status='waiting',
    ).distinct()

    updated = 0
    errors = []
    for o in orders:
        try:
            if not o.refund_credited:
                amount = calculate_owed_amount(o)
                if amount > 0:
                    _apply_refund_credit(agent, amount, o.order_id, o.source)
            Order.objects.filter(pk=o.pk).update(
                status='refunded',
                refund_credited=True,
                refund_requested=True,
                refund_paid=True,
                refund_paid_at=_tz_refund.now(),
            )
            updated += 1
        except Exception as e:
            errors.append(f'{o.order_id}: {e}')
            print(f'[convert_agent_waiting_to_refund] Failed for {o.order_id}: {e}')

    resp = {'updated': updated, 'agent': agent.full_name}
    if errors:
        resp['errors'] = errors
    return JsonResponse(resp)


@admin_required
def bulk_waiting_to_refunded_view(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid'}, status=400)

    if request.POST.get('confirm') != '1':
        return JsonResponse({
            'error': 'This marks orders as refunded and logs them as paid. '
                     'Resend with confirm=1 once agents have actually been paid.'
        }, status=400)

    from django.utils import timezone as _tz_refund_bulk
    from orders.utils import calculate_owed_amount, _apply_refund_credit

    orders = Order.objects.filter(status='waiting').select_related('placed_by', 'generated_link__agent')

    updated = 0
    skipped_no_agent = 0
    errors = []
    for o in orders:
        agent = o.placed_by or (o.generated_link.agent if o.generated_link else None)
        if not agent:
            skipped_no_agent += 1
            continue
        try:
            if not o.refund_credited:
                amount = calculate_owed_amount(o)
                if amount > 0:
                    _apply_refund_credit(agent, amount, o.order_id, o.source)
            Order.objects.filter(pk=o.pk).update(
                status='refunded',
                refund_credited=True,
                refund_requested=True,
                refund_paid=True,
                refund_paid_at=_tz_refund_bulk.now(),
            )
            updated += 1
        except Exception as e:
            errors.append(f'{o.order_id}: {e}')
            print(f'[bulk_waiting_to_refunded] Failed for {o.order_id}: {e}')

    resp = {'updated': updated, 'skipped_no_agent': skipped_no_agent}
    if errors:
        resp['errors'] = errors
    return JsonResponse(resp)


@admin_required
def refund_requests_view(request):
    from decimal import Decimal
    from orders.utils import calculate_owed_amount

    orders_qs = Order.objects.filter(refund_requested=True).select_related(
        'gb_package', 'generated_link', 'placed_by'
    ).order_by('-created_at')

    rows = []
    pending_count = 0
    pending_total = Decimal('0')
    paid_count = 0
    paid_total = Decimal('0')

    for o in orders_qs:
        retail = Decimal(str(o.price_paid or 0))
        agent_cost = calculate_owed_amount(o)
        is_paid = o.refund_paid
        rows.append({
            'order': o,
            'retail_price': retail,
            'agent_cost': agent_cost,
            'is_paid': is_paid,
            'paid_at': o.refund_paid_at,
        })
        if is_paid:
            paid_count += 1
            paid_total += agent_cost
        else:
            pending_count += 1
            pending_total += agent_cost

    return render(request, 'admin_panel/refund_requests.html', {
        'rows': rows,
        'pending_count': pending_count,
        'pending_total': pending_total,
        'paid_count': paid_count,
        'paid_total': paid_total,
        'total_requests': len(rows),
    })


@admin_required
def mark_refund_requests_paid_view(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid'}, status=400)

    order_ids = request.POST.getlist('order_ids')
    if not order_ids:
        return JsonResponse({'error': 'No orders selected'}, status=400)

    from django.utils import timezone

    eligible_ids = list(
        Order.objects.filter(
            order_id__in=order_ids,
            refund_requested=True,
            refund_paid=False,
        ).values_list('order_id', flat=True)
    )

    already_paid_count = len(order_ids) - len(eligible_ids)

    updated = Order.objects.filter(
        order_id__in=eligible_ids,
    ).update(
        status='refunded',
        refund_paid=True,
        refund_paid_at=timezone.now(),
    )

    return JsonResponse({'updated': updated, 'already_paid_skipped': already_paid_count})


@admin_required
def bulk_delete_view(request):
    if request.method == 'POST':
        order_ids = request.POST.getlist('order_ids')
        deleted   = Order.objects.filter(
            order_id__in=order_ids,
            status__in=['pending', 'processing', 'failed'],
        ).delete()
        count = deleted[0] if deleted else 0
        return JsonResponse({'deleted': count})
    return JsonResponse({'error': 'Invalid'}, status=400)

@admin_required
def all_pending_order_ids_view(request):
    from django.db import models as db_models
    from .models import GatiSettings

    gati_enabled = GatiSettings.get().is_enabled

    if gati_enabled:
        # Gati handles API orders — only give admin manual orders
        qs = Order.objects.filter(
            network='MTN',
            via_api=False,
            status__in=['pending', 'processing'],
            payment_status='paid',
        )
    else:
        # Gati is off — admin must copy everything (API + manual)
        qs = Order.objects.filter(
            network='MTN',
            status__in=['pending', 'processing'],
            payment_status='paid',
        ).filter(
            db_models.Q(gati_order_id__isnull=True) | db_models.Q(gati_order_id='')
        )

    order_ids = list(qs.values_list('order_id', flat=True).order_by('-created_at'))
    return JsonResponse({'order_ids': order_ids, 'count': len(order_ids)})


@admin_required
def agent_roles_view(request):
    from accounts.models import AgentRole, AgentRolePrice

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'create':
            name           = request.POST.get('name', '').strip()
            slug           = request.POST.get('slug', '').strip()
            description    = request.POST.get('description', '').strip()
            max_links      = int(request.POST.get('max_links', 3))
            has_api_access  = request.POST.get('has_api_access') == 'on'
            has_debt_access = request.POST.get('has_debt_access') == 'on'

            if not name or not slug:
                messages.error(request, 'Name and slug are required.')
                return redirect('/admin-panel/agent-roles/')

            role, created = AgentRole.objects.get_or_create(
                slug=slug,
                defaults=dict(
                    name=name,
                    description=description,
                    max_links=max_links,
                    has_api_access=has_api_access,
                    has_debt_access=has_debt_access,
                    created_by=request.user,
                )
            )
            if created:
                messages.success(request, f'Role "{name}" created.')
            else:
                messages.error(request, f'Slug "{slug}" already exists.')

        elif action == 'edit':
            role_id  = request.POST.get('role_id')
            role_obj = get_object_or_404(AgentRole, pk=role_id)
            role_obj.name            = request.POST.get('name', '').strip()
            role_obj.slug            = request.POST.get('slug', '').strip()
            role_obj.description     = request.POST.get('description', '').strip()
            role_obj.max_links       = int(request.POST.get('max_links', 3))
            role_obj.has_api_access  = request.POST.get('has_api_access') == 'on'
            role_obj.has_debt_access = request.POST.get('has_debt_access') == 'on'
            role_obj.save()
            messages.success(request, 'Role ' + role_obj.name + ' updated.')

        elif action == 'set_prices':
            role_id  = request.POST.get('role_id')
            role_obj = get_object_or_404(AgentRole, pk=role_id)
            for bundle in BundlePackage.objects.filter(is_active=True):
                price_val = request.POST.get('price_' + str(bundle.id), '').strip()
                if price_val:
                    try:
                        from accounts.models import AgentRolePrice
                        AgentRolePrice.objects.update_or_create(
                            role=role_obj, bundle=bundle,
                            defaults={'price': float(price_val)}
                        )
                    except (ValueError, TypeError):
                        pass
                else:
                    from accounts.models import AgentRolePrice
                    AgentRolePrice.objects.filter(role=role_obj, bundle=bundle).delete()
            messages.success(request, 'Prices saved for ' + role_obj.name + '.')

        elif action == 'delete':
            role_id = request.POST.get('role_id')
            AgentRole.objects.filter(pk=role_id).delete()
            messages.success(request, 'Role deleted.')

        elif action == 'set_price':
            role_id   = request.POST.get('role_id')
            bundle_id = request.POST.get('bundle_id')
            price     = request.POST.get('price', '').strip()
            role      = get_object_or_404(AgentRole, pk=role_id)
            bundle    = get_object_or_404(BundlePackage, pk=bundle_id)
            try:
                AgentRolePrice.objects.update_or_create(
                    role=role, bundle=bundle,
                    defaults={'price': float(price)}
                )
                messages.success(request, f'Price set for {role.name} â€” {bundle}.')
            except (ValueError, TypeError):
                messages.error(request, 'Invalid price.')

        elif action == 'assign_role':
            user_id = request.POST.get('user_id')
            role_id = request.POST.get('role_id') or None
            user    = get_object_or_404(User, pk=user_id)
            user.custom_role_id = role_id
            user.save(update_fields=['custom_role'])
            label = AgentRole.objects.get(pk=role_id).name if role_id else 'none'
            messages.success(request, f'Custom role set to "{label}" for {user.full_name}.')

        return redirect('/admin-panel/agent-roles/')

    roles   = AgentRole.objects.prefetch_related('prices__bundle').all()
    bundles = BundlePackage.objects.filter(is_active=True).order_by('network', 'gb_size')
    agents  = User.objects.filter(
        role__in=['small_agent', 'moderate', 'mega', 'super_mega', 'extreme']
    ).select_related('custom_role').order_by('full_name')

    builtin_tiers = [
        ('small_agent', 'Small Agent'),
        ('moderate',    'Moderate'),
        ('mega',        'Mega'),
        ('super_mega',  'Super Mega'),
        ('extreme',     'Extreme'),
    ]
    return render(request, 'admin_panel/agent_roles.html', {
        'roles':         roles,
        'bundles':       bundles,
        'agents':        agents,
        'builtin_tiers': builtin_tiers,
    })


@admin_required
def link_profit_live_view(request):
    from datetime import timedelta
    from orders.models import GeneratedLinkPrice as GLP

    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end   = today_start + timedelta(hours=24)

    paid_link_orders = Order.objects.filter(
        generated_link__isnull=False,
        payment_status='paid',
        created_at__gte=today_start,
        created_at__lt=today_end,
        gb_package__isnull=False,
    ).select_related('gb_package', 'generated_link')

    total_profit = 0.0
    for o in paid_link_orders:
        lp = GLP.objects.filter(
            link=o.generated_link,
            bundle=o.gb_package
        ).first()
        if lp and o.generated_link and o.generated_link.agent:
            agent_cost = float(o.gb_package.get_price_for_user(o.generated_link.agent))
            total_profit += float(lp.retail_price) - agent_cost

    return JsonResponse({
        'profit': round(total_profit, 2),
        'count':  paid_link_orders.count(),
    })



@admin_required
def toggle_mtn_view(request):
    if request.method != 'POST':
        return redirect('/admin-panel/dashboard/')
    site = SiteSettings.get()
    site.mtn_enabled = not site.mtn_enabled
    site.save()
    status = 'enabled' if site.mtn_enabled else 'paused'
    messages.success(request, f'MTN orders are now {status}.')
    return redirect('/admin-panel/dashboard/')

@csrf_exempt
def at_sync_view(request):
    import hmac
    from django.conf import settings
    from orders.at import recover_lost_at_orders, check_at_status
    from orders.models import Order

    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    secret   = request.headers.get('X-Sync-Secret', '')
    expected = getattr(settings, 'AT_SYNC_SECRET', '')
    if not expected or not hmac.compare_digest(secret, expected):
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    dispatched = recover_lost_at_orders()

    # Poll status for orders already dispatched but still processing
    polled = 0
    in_flight = Order.objects.filter(
        network__in=['AT_EXPIRY', 'AT_NOEXPIRY'],
        status='processing',
        dcs_order_reference__isnull=False,
    ).exclude(dcs_order_reference='')
    for order in in_flight:
        try:
            check_at_status(order.dcs_order_reference)
            polled += 1
        except Exception as e:
            print(f'[AT Sync] Error polling {order.order_id}: {e}')

    return JsonResponse({'dispatched': dispatched, 'polled': polled})


NETWORK_DISPLAY = {
    'MTN': 'MTN', 'TELECEL': 'Telecel',
    'AT_NOEXPIRY': 'AirtelTigo', 'AT_EXPIRY': 'AirtelTigo', 'AFA': 'AFA',
}


@admin_required
def customer_refunds_view(request):
    from decimal import Decimal
    from orders.utils import get_paystack_fee
    from orders.models import GeneratedLinkPrice
    from wallet.models import WalletTransaction

    orders_qs = Order.objects.filter(
        status='waiting',
        source__in=['guest_checkout', 'generated_link'],
    ).select_related(
        'gb_package', 'placed_by', 'generated_link', 'generated_link__agent'
    ).order_by('-created_at')

    rows = []
    for o in orders_qs:
        agent = o.placed_by or (o.generated_link.agent if o.generated_link else None)
        fee = get_paystack_fee(o)
        paid = Decimal(str(o.price_paid or 0))
        refund_amount = (paid - fee) if fee is not None else None

        gets_price = None
        agent_profit = None
        can_claw_back = False

        if o.source == 'generated_link' and o.generated_link and o.generated_link.agent and o.gb_package:
            link_agent = o.generated_link.agent
            lp = GeneratedLinkPrice.objects.filter(
                link=o.generated_link,
                bundle=o.gb_package,
            ).first()
            if lp:
                try:
                    gets_price = Decimal(str(o.gb_package.get_price_for_user(link_agent)))
                    agent_profit = Decimal(str(lp.retail_price)) - gets_price
                except Exception as e:
                    print(f'[customer_refunds_view] profit calc failed for {o.order_id}: {e}')

            if agent_profit is not None and agent_profit > 0:
                profit_credited = WalletTransaction.objects.filter(
                    reference=f'PROFIT-{o.order_id}'
                ).exists()
                can_claw_back = profit_credited and not o.agent_profit_reversed

        rows.append({
            'order'          : o,
            'agent_name'     : agent.full_name if agent else 'Direct Customer',
            'gb_size'        : o.gb_package.gb_size if o.gb_package else None,
            'amount_paid'    : paid,
            'paystack_fee'   : fee,
            'refund_amount'  : refund_amount,
            'network_display': NETWORK_DISPLAY.get(o.network, o.network),
            'is_paid'        : o.customer_refund_paid,
            'gets_price'     : gets_price,
            'agent_profit'   : agent_profit,
            'can_claw_back'  : can_claw_back,
            'profit_reversed': o.agent_profit_reversed,
        })

    paid_count = sum(1 for r in rows if r['is_paid'])
    unpaid_count = len(rows) - paid_count

    return render(request, 'admin_panel/customer_refunds.html', {
        'rows': rows,
        'total_count': len(rows),
        'paid_count': paid_count,
        'unpaid_count': unpaid_count,
    })


@admin_required
def claw_back_agent_profit_view(request, order_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid'}, status=400)

    from decimal import Decimal
    from django.db import transaction, IntegrityError
    from django.db.models import F
    from orders.models import GeneratedLinkPrice
    from wallet.models import Wallet, WalletTransaction

    order = get_object_or_404(Order, order_id=order_id)

    if order.agent_profit_reversed:
        return JsonResponse({'error': 'Profit already clawed back for this order.'}, status=400)

    if order.source != 'generated_link' or not order.generated_link or not order.generated_link.agent:
        return JsonResponse({'error': 'This order has no agent link profit to claw back.'}, status=400)

    if not order.gb_package:
        return JsonResponse({'error': 'Order has no bundle package on record.'}, status=400)

    agent = order.generated_link.agent

    profit_credit = WalletTransaction.objects.filter(reference=f'PROFIT-{order.order_id}').first()
    if not profit_credit:
        return JsonResponse({'error': 'No profit credit found for this order — nothing to claw back.'}, status=400)

    lp = GeneratedLinkPrice.objects.filter(link=order.generated_link, bundle=order.gb_package).first()
    if not lp:
        return JsonResponse({'error': 'No link price found for this order.'}, status=400)

    try:
        gets_price = Decimal(str(order.gb_package.get_price_for_user(agent)))
        profit_amount = Decimal(str(lp.retail_price)) - gets_price
    except Exception as e:
        return JsonResponse({'error': f'Could not compute profit amount: {e}'}, status=400)

    if profit_amount <= 0:
        return JsonResponse({'error': 'Computed profit is zero or negative — nothing to claw back.'}, status=400)

    reversal_ref = f'PROFIT-REVERSED-{order.order_id}'

    try:
        with transaction.atomic():
            wallet, _ = Wallet.objects.get_or_create(user=agent)
            try:
                _, created = WalletTransaction.objects.get_or_create(
                    reference=reversal_ref,
                    defaults=dict(
                        wallet=wallet,
                        amount=profit_amount,
                        transaction_type='debit',
                        status='completed',
                        description=f'Link profit clawed back — order {order.order_id} refunded to customer',
                    )
                )
            except IntegrityError:
                return JsonResponse({'error': 'Clawback already in progress for this order.'}, status=400)

            if not created:
                return JsonResponse({'error': 'Profit already clawed back for this order.'}, status=400)

            from accounts.models import User as AgentUser
            AgentUser.objects.filter(pk=agent.pk).update(
                link_profit_balance=F('link_profit_balance') - profit_amount,
                total_link_profit_earned=F('total_link_profit_earned') - profit_amount,
            )

            order.agent_profit_reversed = True
            order.agent_profit_reversed_at = timezone.now()
            order.save(update_fields=['agent_profit_reversed', 'agent_profit_reversed_at'])
    except Exception as e:
        print(f'[claw_back_agent_profit] Failed for {order.order_id}: {e}')
        return JsonResponse({'error': f'Clawback failed: {e}'}, status=500)

    return JsonResponse({'ok': True, 'order_id': order.order_id, 'amount': str(profit_amount)})


@admin_required
def mark_customer_refund_paid_view(request, order_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid'}, status=400)

    order = get_object_or_404(Order, order_id=order_id)
    order.customer_refund_paid = True
    order.customer_refund_paid_at = timezone.now()
    # Once the customer has been refunded, this order is fully resolved from
    # a "waiting" standpoint — flip status to refunded so it drops off the
    # dashboard's Waiting count. This is independent of whether the agent's
    # link profit has been clawed back (that remains a separate, optional
    # admin action and does not block this transition).
    if order.status == 'waiting':
        # Save the customer fields first, then change status with .update() so the
        # pre_save signal does NOT credit the agent as a side effect.
        order.save(update_fields=['customer_refund_paid', 'customer_refund_paid_at'])
        Order.objects.filter(pk=order.pk).update(status='refunded')
        order.status = 'refunded'
    else:
        order.save(update_fields=['customer_refund_paid', 'customer_refund_paid_at'])

    return JsonResponse({'ok': True, 'order_id': order.order_id, 'status': order.status})


def agent_refund_history_view(request, pk):
    from django.core.paginator import Paginator
    from orders.utils import get_refund_history_queryset, calculate_owed_amount
    from decimal import Decimal

    agent = get_object_or_404(User, pk=pk)
    qs = get_refund_history_queryset(agent, request)

    total_count  = qs.count()
    total_amount = sum((calculate_owed_amount(o) for o in qs), Decimal('0'))

    paginator = Paginator(qs, 50)
    page_obj  = paginator.get_page(request.GET.get('page'))

    return render(request, 'admin_panel/agent_refund_history.html', {
        'agent'        : agent,
        'page_obj'     : page_obj,
        'total_count'  : total_count,
        'total_amount' : total_amount,
        'filters'      : request.GET,
    })


@admin_required
def toggle_payment_gateway_view(request):
    from .models import PaymentGatewaySettings

    if request.method != 'POST':
        return redirect('/admin-panel/api-usage/')

    new_gateway = request.POST.get('gateway')
    valid_choices = dict(PaymentGatewaySettings.GATEWAY_CHOICES)
    if new_gateway not in valid_choices:
        messages.error(request, 'Invalid gateway selected.')
        return redirect('/admin-panel/api-usage/')

    settings_obj = PaymentGatewaySettings.get()
    old_gateway = settings_obj.active_gateway
    settings_obj.active_gateway = new_gateway
    settings_obj.updated_by = request.user
    settings_obj.save()

    if old_gateway != new_gateway:
        messages.success(request, f'Payment gateway switched from {valid_choices[old_gateway]} to {valid_choices[new_gateway]}. New transactions will use {valid_choices[new_gateway]}.')
    else:
        messages.info(request, f'Payment gateway is already set to {valid_choices[new_gateway]}.')

    return redirect('/admin-panel/api-usage/')
