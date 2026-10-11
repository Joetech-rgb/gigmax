from django.contrib.auth import logout
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum, Count, Case, When, IntegerField
from django.http import JsonResponse
from django.utils import timezone
from decimal import Decimal
from accounts.decorators import agent_required, top_tier_required, afa_required
from bundles.models import BundlePackage
from .models import Order, GeneratedLink, GeneratedLinkPrice
from .utils import place_order, validate_ghana_number, parse_bulk_lines
from .notifications import notify_order_placed
import uuid, openpyxl
from django.conf import settings
from django.db.models import Sum, Count, Case, When, IntegerField, Q

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _network_order():
    return Case(
        When(network='MTN',         then=0),
        When(network='TELECEL',     then=1),
        When(network='AT_NOEXPIRY', then=2),
        When(network='AT_EXPIRY',   then=3),
        default=4, output_field=IntegerField()
    )


def _packages_ordered():
    return BundlePackage.objects.filter(is_active=True).annotate(
        net_order=_network_order()
    ).order_by('net_order', 'gb_size')


def _normalize_gb(val):
    try:
        cleaned = str(val).upper().replace('GB', '').strip()
        d = Decimal(cleaned)
        if d == d.to_integral_value():
            return str(int(d))
        else:
            return str(float(d))
    except Exception:
        return str(val)


def _local_today():
    return timezone.localdate()

def _apply_payment(user, total):
    """
    Payment logic — strictly follows the three-scenario rule:

      1. Wallet covers full amount → deduct wallet only.
      2. Wallet short + debt ON   → use wallet first, shortfall goes to debt.
      3. Wallet short + debt OFF  → raise ValueError so caller redirects to Paystack.

    No fee here — fee is only charged on Paystack transactions, not on
    wallet spending or debt accumulation.
    Returns (wallet_used, debt_added) as Decimals.
    """
    from django.db import transaction as db_transaction
    from django.contrib.auth import get_user_model

    total = Decimal(str(total))

    with db_transaction.atomic():
        User = get_user_model()
        locked_user = User.objects.select_for_update().get(pk=user.pk)

        wallet = Decimal(str(locked_user.wallet_balance or 0))
        debt   = Decimal(str(locked_user.debt_balance   or 0))

        if locked_user.debt_page_access:
            if wallet >= total:
                # Wallet covers fully — no debt needed
                wallet_used = total
                debt_added  = Decimal('0')
                description = 'Order payment'
            else:
                # Wallet short — use what's available, rest goes to debt
                wallet_used = wallet
                debt_added  = total - wallet_used
                description = 'Order payment (wallet portion)'

            locked_user.wallet_balance = wallet - wallet_used
            locked_user.debt_balance   = debt + debt_added
            locked_user.save(update_fields=['wallet_balance', 'debt_balance'])

            if wallet_used > 0:
                try:
                    from wallet.models import Wallet as WalletModel, WalletTransaction
                    wallet_obj, _ = WalletModel.objects.get_or_create(user=locked_user)
                    WalletTransaction.objects.create(
                        wallet           = wallet_obj,
                        amount           = wallet_used,
                        transaction_type = 'debit',
                        status           = 'completed',
                        description      = description,
                        reference        = f'ORD-{uuid.uuid4().hex[:10].upper()}',
                    )
                except Exception:
                    pass

            user.wallet_balance = locked_user.wallet_balance
            user.debt_balance   = locked_user.debt_balance

            return wallet_used, debt_added

        else:
            if wallet < total:
                raise ValueError("Insufficient wallet balance")

            locked_user.wallet_balance = wallet - total
            locked_user.save(update_fields=['wallet_balance'])

            try:
                from wallet.models import Wallet as WalletModel, WalletTransaction
                wallet_obj, _ = WalletModel.objects.get_or_create(user=locked_user)
                WalletTransaction.objects.create(
                    wallet           = wallet_obj,
                    amount           = total,
                    transaction_type = 'debit',
                    status           = 'completed',
                    description      = 'Order payment',
                    reference        = f'ORD-{uuid.uuid4().hex[:10].upper()}',
                )
            except Exception:
                pass

            user.wallet_balance = locked_user.wallet_balance
            user.debt_balance   = locked_user.debt_balance

            return total, Decimal('0')


def _reverse_payment(user, wallet_used, debt_added):
    """
    Undo a previously applied payment on order failure.
    Always safe to call — zero values are no-ops.
    """
    wallet_used = Decimal(str(wallet_used))
    debt_added  = Decimal(str(debt_added))

    user.wallet_balance = Decimal(str(user.wallet_balance)) + wallet_used
    user.debt_balance   = max(Decimal('0'), Decimal(str(user.debt_balance)) - debt_added)
    user.save(update_fields=['wallet_balance', 'debt_balance'])

    # Record reversal transaction
    if wallet_used > 0:
        try:
            from wallet.models import Wallet as WalletModel, WalletTransaction
            wallet_obj, _ = WalletModel.objects.get_or_create(user=user)
            WalletTransaction.objects.create(
                wallet           = wallet_obj,
                amount           = wallet_used,
                transaction_type = 'credit',
                status           = 'completed',
                description      = 'Order refund (payment reversed)',
                reference        = f'REF-{uuid.uuid4().hex[:10].upper()}',
            )
        except Exception:
            pass
def _place_bulk_orders(request, recipients, package_ids, prices, network, user=None):
    """
    Place all orders atomically.
    Same phone number → keep only the biggest GB, drop the rest silently.
    Dropped ones are not charged.
    """
    batch_id  = str(uuid.uuid4())[:8].upper()
    placed_by = user or request.user

    # Deduplicate: same phone → keep biggest GB only
    best = {}  # phone → (pkg_id, price, gb_size)
    for recipient, pkg_id, price in zip(recipients, package_ids, prices):
        phone = recipient.strip()
        try:
            gb = float(BundlePackage.objects.get(id=pkg_id).gb_size)
        except Exception:
            gb = 0
        if phone not in best or gb > best[phone][2]:
            best[phone] = (pkg_id, price, gb)

    unique_orders      = [(phone, data[0], data[1]) for phone, data in best.items()]
    duplicates_removed = len(recipients) - len(unique_orders)

    with transaction.atomic():
        for recipient, pkg_id, price in unique_orders:
            package = BundlePackage.objects.get(id=pkg_id)
            place_order(
                package   = package,
                recipient = recipient,
                price     = float(price),
                source    = Order.Source.AGENT_BULK,
                placed_by = placed_by,
                batch_id  = batch_id,
                mark_paid = True,
            )

    if duplicates_removed > 0:
        messages.warning(
            request,
            f'{duplicates_removed} duplicate number(s) were invalid and removed — only the biggest GB per number was kept and charged.'
        )

    messages.success(request, f'{len(unique_orders)} orders placed. Batch: {batch_id}')


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@agent_required
def refund_detail_json_view(request):
    from datetime import datetime
    from django.http import JsonResponse
    from orders.utils import get_agent_refund_detail

    user = request.user
    date_from_str = request.GET.get('date_from') or request.GET.get('date')
    date_to_str   = request.GET.get('date_to')

    try:
        date_from = datetime.strptime(date_from_str, '%Y-%m-%d').date() if date_from_str else datetime(2026, 8, 26).date()
        date_to   = datetime.strptime(date_to_str, '%Y-%m-%d').date() if date_to_str else _local_today()
    except ValueError:
        return JsonResponse({'error': 'Invalid date format, expected YYYY-MM-DD'}, status=400)

    items, total_amount, total_debt_reduced, total_wallet_credited, count = get_agent_refund_detail(user, date_from, date_to)

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
        'date_from'             : date_from.isoformat(),
        'date_to'               : date_to.isoformat(),
        'count'                 : count,
        'total_amount'          : str(total_amount),
        'total_debt_reduced'    : str(total_debt_reduced),
        'total_wallet_credited' : str(total_wallet_credited),
        'refunds'               : data,
    })


@agent_required
def dashboard_view(request):
    from admin_panel.models import Notice, DebtPayment
    from django.db.models import Q
 
    user  = request.user
    today = _local_today()
    own_orders  = Order.objects.filter(placed_by=user)
    agent_links = GeneratedLink.objects.filter(agent=user).prefetch_related('prices')

    link_orders_qs = Order.objects.filter(
        generated_link__in=agent_links,
        payment_status='paid',
    ).select_related('gb_package', 'generated_link')

    all_orders = Order.objects.filter(
        Q(placed_by=user) | Q(generated_link__in=agent_links)
    ).distinct()

    total_orders = all_orders.count()
    total_spent  = own_orders.aggregate(t=Sum('price_paid'))['t'] or 0
    total_gb     = own_orders.aggregate(t=Sum('gb_package__gb_size'))['t'] or 0
    daily_orders = all_orders.filter(created_at__date=today).count()
    from orders.utils import calculate_owed_amount
    from decimal import Decimal

    waiting_orders_qs = all_orders.filter(status__in=['waiting', 'kuntanii_unverified'])
    waiting_orders = waiting_orders_qs.count()
    waiting_orders_amount = sum((calculate_owed_amount(o) for o in waiting_orders_qs), Decimal('0'))

    refund_orders_qs = all_orders.filter(status='refunded')
    refund_orders = refund_orders_qs.count()
    refund_orders_amount = sum((calculate_owed_amount(o) for o in refund_orders_qs), Decimal('0'))
    daily_spent  = own_orders.filter(created_at__date=today).aggregate(t=Sum('price_paid'))['t'] or 0

    total_link_orders      = link_orders_qs.count()
    daily_link_profit      = Decimal('0')
    total_link_profit_calc = Decimal('0')

    for o in link_orders_qs:
        if not o.gb_package:
            continue
        link_price = o.generated_link.prices.filter(bundle=o.gb_package).first()
        if not link_price:
            continue
        agent_cost = o.gb_package.get_price_for_user(o.generated_link.agent)
        profit = Decimal(str(link_price.retail_price)) - Decimal(str(agent_cost))
        total_link_profit_calc += profit
        if o.created_at.date() == today:
            daily_link_profit += profit

    customer_orders_today = link_orders_qs.filter(created_at__date=today).count()

    network_breakdown = {}
    for network in ['MTN', 'TELECEL', 'AT_NOEXPIRY', 'AT_EXPIRY']:
        net_qs = own_orders.filter(created_at__date=today, network=network)
        network_breakdown[network] = {
            'count'  : net_qs.count(),
            'revenue': float(net_qs.aggregate(t=Sum('price_paid'))['t'] or 0),
        }

    link_network_breakdown = {}
    for network in ['MTN', 'TELECEL', 'AT_NOEXPIRY', 'AT_EXPIRY']:
        link_network_breakdown[network] = {
            'count': link_orders_qs.filter(created_at__date=today, network=network).count(),
        }

    notices = Notice.objects.filter(is_active=True).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now())
    ).order_by('-created_at')

    debt_payments = DebtPayment.objects.filter(agent=user).select_related('cleared_by').order_by('-created_at')[:10]

    # ── Statement (range-picker driven, defaults to Aug26-today) ──
    from datetime import date as _date
    from orders.utils import get_agent_refund_detail
    _statement_start = _date(2026, 8, 26)
    (
        todays_refund_list,
        todays_refund_total,
        todays_debt_reduced_total,
        todays_wallet_credited_total,
        todays_refund_count,
    ) = get_agent_refund_detail(user, _statement_start, today)  # full statement, matches date inputs / range picker

    # ── Today's Refunds summary card (genuinely today only, not the full statement) ──
    (
        today_card_list,
        today_card_refund_total,
        today_card_debt_reduced_total,
        today_card_wallet_credited_total,
        today_card_refund_count,
    ) = get_agent_refund_detail(user, today, today)

    return render(request, 'agent/dashboard.html', {
        'debt_payments'          : debt_payments,
        'todays_refund_list'          : todays_refund_list,
        'today_card_refund_count'      : today_card_refund_count,
        'today_card_refund_total'      : today_card_refund_total,
        'today_card_list'              : today_card_list,
        'today_card_debt_reduced_total'    : today_card_debt_reduced_total,
        'today_card_wallet_credited_total' : today_card_wallet_credited_total,
        'todays_debt_reduced_total'   : todays_debt_reduced_total,
        'todays_wallet_credited_total': todays_wallet_credited_total,
        'todays_refund_count'    : todays_refund_count,
        'todays_refund_total'    : todays_refund_total,
        'total_orders'           : total_orders,
        'total_spent'            : total_spent,
        'total_gb'               : total_gb,
        'daily_orders'           : daily_orders,
        'daily_spent'            : daily_spent,
        'total_link_orders'      : total_link_orders,
        'total_link_profit'      : user.total_link_profit_earned,
        'available_link_profit'  : user.link_profit_balance,
        'daily_link_profit'      : round(daily_link_profit, 2),
        'total_link_profit_calc' : round(total_link_profit_calc, 2),
        'wallet_only_balance'    : user.wallet_only_balance,
        'notices'                : notices,
        'network_breakdown'      : network_breakdown,
        'link_network_breakdown' : link_network_breakdown,
        'customer_orders_today'  : customer_orders_today,
        'waiting_orders'         : waiting_orders,
        'waiting_orders_amount'  : waiting_orders_amount,
        'refund_orders'          : refund_orders,
        'refund_orders_amount'   : refund_orders_amount,
    })

@agent_required
def export_waiting_refund_csv_view(request):
    import csv
    from django.http import HttpResponse
    from django.db.models import Q
    from decimal import Decimal
    from orders.utils import calculate_owed_amount

    user = request.user
    agent_links = GeneratedLink.objects.filter(agent=user)
    all_orders = Order.objects.filter(
        Q(placed_by=user) | Q(generated_link__in=agent_links)
    ).distinct()

    waiting_qs    = all_orders.filter(status='waiting').select_related('gb_package', 'generated_link').order_by('-created_at')
    unverified_qs = all_orders.filter(status='kuntanii_unverified').select_related('gb_package', 'generated_link').order_by('-created_at')
    refund_qs     = all_orders.filter(status='refunded').select_related('gb_package', 'generated_link').order_by('-created_at')

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="waiting_refund_orders.csv"'
    writer = csv.writer(response)
    writer.writerow(['Order Number', 'Recipient', 'Network', 'Status', 'Amount Owed (GHS)', 'Date'])

    waiting_count = 0
    waiting_total = Decimal('0')
    for o in waiting_qs:
        amount = calculate_owed_amount(o)
        waiting_count += 1
        waiting_total += amount
        writer.writerow([o.id, o.recipient, o.network, 'Waiting', f'{amount:.2f}', o.created_at.strftime('%d %b %Y %H:%M')])

    unverified_count = 0
    unverified_total = Decimal('0')
    for o in unverified_qs:
        amount = calculate_owed_amount(o)
        unverified_count += 1
        unverified_total += amount
        writer.writerow([o.id, o.recipient, o.network, 'Unverified', f'{amount:.2f}', o.created_at.strftime('%d %b %Y %H:%M')])

    refund_count = 0
    refund_total = Decimal('0')
    for o in refund_qs:
        amount = calculate_owed_amount(o)
        refund_count += 1
        refund_total += amount
        writer.writerow([o.id, o.recipient, o.network, 'Refunded', f'{amount:.2f}', o.created_at.strftime('%d %b %Y %H:%M')])

    writer.writerow([])
    writer.writerow(['Summary', '', '', '', '', ''])
    writer.writerow(['Waiting Orders', waiting_count, '', '', f'{waiting_total:.2f}', ''])
    writer.writerow(['Unverified Orders', unverified_count, '', '', f'{unverified_total:.2f}', ''])
    writer.writerow(['Refund Orders', refund_count, '', '', f'{refund_total:.2f}', ''])
    writer.writerow(['Grand Total', waiting_count + unverified_count + refund_count, '', '', f'{(waiting_total + unverified_total + refund_total):.2f}', ''])

    return response


@agent_required
def export_unverified_csv_view(request):
    """
    Standalone download of just the agent's Unverified (kuntanii_unverified)
    orders — the numbers that need review because Kuntanii returned a
    status we don't recognize. Kept separate from the combined
    waiting/refund export so agents can pull exactly this list any time.
    """
    import csv
    from django.http import HttpResponse
    from django.db.models import Q
    from decimal import Decimal
    from orders.utils import calculate_owed_amount

    user = request.user
    agent_links = GeneratedLink.objects.filter(agent=user)
    all_orders = Order.objects.filter(
        Q(placed_by=user) | Q(generated_link__in=agent_links)
    ).distinct()

    unverified_qs = all_orders.filter(status='kuntanii_unverified').select_related('gb_package', 'generated_link').order_by('-created_at')

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="unverified_contacts.csv"'
    writer = csv.writer(response)
    writer.writerow(['Order Number', 'Recipient', 'Network', 'Status', 'Reason', 'Amount Owed (GHS)', 'Flagged At'])

    count = 0
    total = Decimal('0')
    for o in unverified_qs:
        amount = calculate_owed_amount(o)
        count += 1
        total += amount
        flagged_at = o.kuntanii_unverified_at.strftime('%d %b %Y %H:%M') if o.kuntanii_unverified_at else ''
        writer.writerow([o.id, o.recipient, o.network, 'Unverified', o.kuntanii_unverified_reason or '', f'{amount:.2f}', flagged_at])

    writer.writerow([])
    writer.writerow(['Summary', '', '', '', '', '', ''])
    writer.writerow(['Unverified Orders', count, '', '', '', f'{total:.2f}', ''])

    return response


# ---------------------------------------------------------------------------
# Single order
# ---------------------------------------------------------------------------

@agent_required
def buy_single_view(request):
    from admin_panel.models import SiteSettings
    network  = request.GET.get('network', '')
    NETWORKS = ['MTN', 'TELECEL', 'AT_NOEXPIRY', 'AT_EXPIRY']

    if network in NETWORKS:
        packages = BundlePackage.objects.filter(
            network=network, is_active=True
        ).order_by('order_position', 'gb_size')
    else:
        packages = _packages_ordered()

    all_packages   = list(_packages_ordered())
    promo_packages = BundlePackage.objects.filter(is_promo=True, is_active=True)

    for pkg in all_packages:
        pkg.agent_price = pkg.get_price_for_user(request.user)

    return render(request, 'agent/buy_single.html', {
        'networks'      : NETWORKS,
        'network'       : network,
        'packages'      : packages,
        'all_packages'  : all_packages,
        'promo_packages': promo_packages,
        'mtn_enabled'   : SiteSettings.get().mtn_enabled,
    })

@agent_required
def confirm_single_view(request):
    """
    Show payment preview — user sees exactly what will happen before confirming.

    Preview uses _apply_payment logic directly so it can never diverge from
    what place_single_view will actually do.
    """
    if request.method != 'POST':
        return redirect('/agent/buy/single/')

    package_id = request.POST.get('package_id')
    recipient  = request.POST.get('recipient', '').strip()

    if not validate_ghana_number(recipient):
        messages.error(request, 'Invalid recipient number.')
        return redirect('/agent/buy/single/')

    package = get_object_or_404(BundlePackage, id=package_id)

    from django.contrib.auth import get_user_model
    user  = get_user_model().objects.get(pk=request.user.pk)
    price = package.get_price_for_user(user)

    wallet      = Decimal(str(user.wallet_balance or 0))
    debt_access = user.debt_page_access

    # Mirror _apply_payment exactly so the preview is never wrong.
    if debt_access:
        wallet_used = min(wallet, price)
        shortfall   = price - wallet_used
        pay_mode    = 'wallet' if shortfall == 0 else 'debt'
    else:
        wallet_used = price if wallet >= price else Decimal('0')
        shortfall   = Decimal('0') if wallet >= price else price - wallet
        pay_mode    = 'wallet' if wallet >= price else 'paystack'

    return render(request, 'agent/confirm_single.html', {
        'package'    : package,
        'recipient'  : recipient,
        'price'      : price,
        'wallet'     : wallet,
        'wallet_used': wallet_used,
        'shortfall'  : shortfall,
        'pay_mode'   : pay_mode,
    })

@agent_required
def place_single_view(request):
    if request.method != 'POST':
        return redirect('/agent/buy/single/')

    package_id = request.POST.get('package_id')
    recipient  = request.POST.get('recipient', '').strip()

    if not validate_ghana_number(recipient):
        messages.error(request, 'Invalid recipient number.')
        return redirect('/agent/buy/single/')

    package = get_object_or_404(BundlePackage, id=package_id)

    from django.contrib.auth import get_user_model
    user  = get_user_model().objects.get(pk=request.user.pk)
    price = package.get_price_for_user(user)

    wallet_used = Decimal('0')
    debt_added  = Decimal('0')

    try:
        wallet_used, debt_added = _apply_payment(user, price)

        order = place_order(
            package   = package,
            recipient = recipient,
            price     = price,
            source    = Order.Source.AGENT_SINGLE,
            placed_by = user,
            mark_paid = True,
        )
        messages.success(request, f'Order {order.order_id} placed successfully.')
        try:
            notify_order_placed(order)
        except Exception:
            pass
        return redirect('/agent/orders/')

    except ValueError:
        # Wallet insufficient, no debt access — redirect to Paystack.
        # Fee applied here only since agent is paying fresh via Paystack,
        # not spending pre-topped-up wallet balance.
        from wallet.gateway import initialize_transaction
        from admin_panel.models import FeeConfiguration

        try:
            fee_config  = FeeConfiguration.get_current()
            fee_percent = Decimal(str(fee_config.paystack_fee_percent))
        except Exception:
            fee_percent = Decimal('2.00')

        fee_multiplier  = 1 + fee_percent / 100
        paystack_amount = float(Decimal(str(price)) * fee_multiplier)

        ref    = f'ORD-{uuid.uuid4().hex[:8].upper()}'
        email  = user.email or f'{user.phone_number}@gigmax.com'
        result = initialize_transaction(
            email        = email,
            amount_ghc   = paystack_amount,
            reference    = ref,
            callback_url = request.build_absolute_uri('/wallet/paystack/verify/'),
        )
        if result and result.get('status'):
            request.session['pending_single'] = {
                'package_id': package_id,
                'recipient' : recipient,
            }
            messages.info(request, f'Redirecting to Paystack to pay GHS {paystack_amount:.2f}.')
            return redirect(result['data']['authorization_url'])

        messages.error(request, 'Insufficient balance and payment could not be initialized. Please top up your wallet.')
        return redirect('/agent/buy/single/')

    except Exception:
        _reverse_payment(user, wallet_used, debt_added)
        messages.error(request, 'Order could not be placed. Your balance has been refunded.')
        return redirect('/agent/buy/single/')

# ---------------------------------------------------------------------------
# Bulk order (paste)
# ---------------------------------------------------------------------------
@agent_required
def buy_bulk_view(request):
    from admin_panel.models import SiteSettings
    NETWORKS = ['MTN', 'TELECEL', 'AT_NOEXPIRY', 'AT_EXPIRY']
    return render(request, 'agent/buy_bulk.html', {
        'networks'   : NETWORKS,
        'mtn_enabled': SiteSettings.get().mtn_enabled,
    })


@agent_required
def validate_bulk_view(request):
    from admin_panel.models import SiteSettings
    if request.method != 'POST':
        return redirect('/agent/buy/bulk/')

    network   = request.POST.get('network', '').strip()
    raw_input = request.POST.get('bulk_input', '')

    if not network:
        messages.error(request, 'Please select a network.')
        return redirect('/agent/buy/bulk/')

    if network == 'MTN' and not SiteSettings.get().mtn_enabled:
        messages.error(request, 'MTN orders are temporarily unavailable.')
        return redirect('/agent/buy/bulk/')

    # ... rest unchanged
    db_packages      = BundlePackage.objects.filter(network=network, is_active=True)
    pkg_by_gb        = {}
    valid_gb_display = []

    for p in db_packages:
        key = _normalize_gb(p.gb_size)
        pkg_by_gb[key] = p
        valid_gb_display.append(key)

    lines = raw_input.strip().split('\n')

    # First pass — parse every line into clean rows, reject format errors
    parsed_rows = []  # list of {'phone', 'gb_key', 'line_label'}
    invalid     = []

    for i, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue

        parts  = line.split()
        label  = f'Line {i}: {line}'

        if len(parts) < 2:
            invalid.append({'line': label, 'error': 'Invalid format — use: 0244111222 5GB'})
            continue

        phone  = parts[0].strip()
        gb_raw = parts[1].upper().replace('GB', '').strip()

        if not phone.isdigit():
            invalid.append({'line': label, 'error': 'Phone must contain digits only'})
            continue
        if not phone.startswith('0'):
            invalid.append({'line': label, 'error': 'Phone must start with 0'})
            continue
        if len(phone) != 10:
            invalid.append({'line': label, 'error': f'Phone must be 10 digits (got {len(phone)})'})
            continue
        if not gb_raw.replace('.', '', 1).isdigit():
            invalid.append({'line': label, 'error': f'"{parts[1]}" is not a valid GB value'})
            continue

        gb_key = _normalize_gb(gb_raw)

        if gb_key not in pkg_by_gb:
            invalid.append({
                'line' : label,
                'error': f'{parts[1]} not available for {network}. Available: {", ".join(sorted(valid_gb_display, key=lambda x: float(x)))}GB'
            })
            continue

        parsed_rows.append({'phone': phone, 'gb_key': gb_key, 'label': label})

    # Second pass — for each phone keep only the row with the biggest GB
    # best: phone → {'gb_key': ..., 'label': ...}
    best = {}
    for row in parsed_rows:
        phone  = row['phone']
        gb_key = row['gb_key']
        if phone not in best:
            best[phone] = row
        else:
            existing_gb = float(best[phone]['gb_key'])
            current_gb  = float(gb_key)
            if current_gb > existing_gb:
                # Current is bigger — old one becomes invalid
                invalid.append({
                    'line' : best[phone]['label'],
                    'error': f'Duplicate number — replaced by larger {gb_key}GB entry'
                })
                best[phone] = row
            else:
                # Current is smaller or equal — current becomes invalid
                invalid.append({
                    'line' : row['label'],
                    'error': f'Duplicate number — {best[phone]["gb_key"]}GB already selected (bigger or equal)'
                })

    # Build final valid list
    valid        = []
    total_amount = 0
    total_gb_sum = 0

    for phone, row in best.items():
        gb_key = row['gb_key']
        pkg    = pkg_by_gb.get(gb_key)
        if not pkg:
            invalid.append({'line': row['label'], 'error': 'Package not found — please refresh'})
            continue
        price = pkg.get_price_for_user(request.user)
        valid.append({
            'number'      : phone,
            'gb'          : gb_key,
            'package_id'  : str(pkg.id),
            'price'       : price,
            'is_duplicate': False,
        })
        total_amount += float(price)
        total_gb_sum += float(gb_key)

    wallet          = float(request.user.wallet_balance)
    debt_access     = request.user.debt_page_access
    wallet_used     = min(wallet, total_amount)
    remainder       = max(0, total_amount - wallet)
    debt_needed     = remainder if debt_access else 0
    paystack_needed = remainder if not debt_access else 0

    return render(request, 'agent/bulk_validation.html', {
        'networks'       : ['MTN', 'TELECEL', 'AT_NOEXPIRY', 'AT_EXPIRY'],
        'network'        : network,
        'valid'          : valid,
        'invalid'        : invalid,
        'total_amount'   : round(total_amount, 2),
        'total_count'    : len(valid),
        'total_gb'       : round(total_gb_sum, 2),
        'wallet'         : wallet,
        'wallet_used'    : round(wallet_used, 2),
        'debt_needed'    : round(debt_needed, 2),
        'paystack_needed': round(paystack_needed, 2),
        'debt_access'    : debt_access,
    })


@agent_required
def submit_bulk_view(request):
    if request.method != 'POST':
        return redirect('/agent/buy/bulk/')

    from admin_panel.models import SiteSettings

    network     = request.POST.get('network')
    recipients  = [r for r in request.POST.getlist('recipients')  if r.strip()]
    package_ids = [p for p in request.POST.getlist('package_ids') if p.strip()]

    # ── MTN hard block ────────────────────────────────────────────────────
    if network == 'MTN' and not SiteSettings.get().mtn_enabled:
        messages.error(request, 'MTN orders are temporarily unavailable.')
        return redirect('/agent/buy/bulk/')
    # ─────────────────────────────────────────────────────────────────────

    if not recipients or not package_ids:
        messages.error(request, 'No valid orders to place.')
        return redirect('/agent/buy/bulk/')

    if len(recipients) != len(package_ids):
        messages.error(request, 'Order data mismatch. Please revalidate.')
        return redirect('/agent/buy/bulk/')

    try:
        prices = [
            BundlePackage.objects.get(id=pkg_id).get_price_for_user(request.user)
            for pkg_id in package_ids
        ]
    except BundlePackage.DoesNotExist:
        messages.error(request, 'One or more packages no longer exist. Please resubmit.')
        return redirect('/agent/buy/bulk/')

    total = sum(Decimal(str(p)) for p in prices)

    from django.contrib.auth import get_user_model
    user = get_user_model().objects.get(pk=request.user.pk)

    wallet_charged = Decimal('0')
    debt_charged   = Decimal('0')

    try:
        wallet_charged, debt_charged = _apply_payment(user, total)

    except ValueError:
        from wallet.gateway import initialize_transaction
        from admin_panel.models import FeeConfiguration

        try:
            fee_config  = FeeConfiguration.get_current()
            fee_percent = Decimal(str(fee_config.paystack_fee_percent))
        except Exception:
            fee_percent = Decimal('2.00')

        fee_multiplier  = 1 + fee_percent / 100
        paystack_amount = float(total * fee_multiplier)

        ref    = f'ORD-{uuid.uuid4().hex[:8].upper()}'
        email  = user.email or f'{user.phone_number}@gigmax.com'
        result = initialize_transaction(
            email        = email,
            amount_ghc   = paystack_amount,
            reference    = ref,
            callback_url = request.build_absolute_uri('/wallet/paystack/verify/'),
        )
        if result and result.get('status'):
            request.session['pending_bulk'] = {
                'network'    : network,
                'recipients' : recipients,
                'package_ids': package_ids,
                'prices'     : [str(p) for p in prices],
            }
            messages.info(request, f'Wallet short. Complete GHS {paystack_amount:.2f} payment to place orders.')
            return redirect(result['data']['authorization_url'])

        messages.error(request, 'Could not initiate payment. Please top up your wallet manually.')
        return redirect('/agent/buy/bulk/')

    try:
        _place_bulk_orders(request, recipients, package_ids, prices, network, user=user)
    except Exception:
        _reverse_payment(user, wallet_charged, debt_charged)
        messages.error(request, 'Orders could not be placed. Your balance has been fully refunded.')
        return redirect('/agent/buy/bulk/')

    return redirect('/agent/orders/')
# ---------------------------------------------------------------------------
# Excel order
# ---------------------------------------------------------------------------
@agent_required
def buy_excel_view(request):
    from admin_panel.models import SiteSettings
    NETWORKS = ['MTN', 'TELECEL', 'AT_NOEXPIRY', 'AT_EXPIRY']
    return render(request, 'agent/buy_excel.html', {
        'networks'   : NETWORKS,
        'mtn_enabled': SiteSettings.get().mtn_enabled,
    })

@agent_required
def upload_excel_view(request):
    from admin_panel.models import SiteSettings
    networks = ['MTN', 'TELECEL', 'AT_NOEXPIRY', 'AT_EXPIRY']

    if request.method != 'POST' or not request.FILES.get('excel_file'):
        return render(request, 'agent/buy_excel.html', {
            'networks'   : networks,
            'validated'  : False,
            'mtn_enabled': SiteSettings.get().mtn_enabled,
        })

    network = request.POST.get('network')

    if network == 'MTN' and not SiteSettings.get().mtn_enabled:
        messages.error(request, 'MTN orders are temporarily unavailable.')
        return render(request, 'agent/buy_excel.html', {
            'networks'   : networks,
            'validated'  : False,
            'mtn_enabled': SiteSettings.get().mtn_enabled,
        })

    # ... rest unchanged

    network    = request.POST.get('network')
    excel_file = request.FILES['excel_file']

    wb = openpyxl.load_workbook(excel_file)
    ws = wb.active

    valid   = []
    invalid = []

    all_packages = {}
    for p in BundlePackage.objects.filter(network=network, is_active=True):
        key = _normalize_gb(p.gb_size)
        all_packages[key] = p
    valid_gb_strs = sorted(all_packages.keys(), key=lambda x: float(x))

    seen_pairs = {}

    for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not any(row):
            continue

        raw_number = row[0]
        if raw_number is None:
            number = ''
        elif isinstance(raw_number, (int, float)):
            number = str(int(raw_number))
            if len(number) == 9:
                number = '0' + number
        else:
            number = str(raw_number).strip()
            if '.' in number:
                try:
                    number = str(int(float(number)))
                    if len(number) == 9:
                        number = '0' + number
                except ValueError:
                    pass

        raw_gb = row[1]
        if raw_gb is None or str(raw_gb).strip() == '':
            if number:
                invalid.append({'line': f'Row {row_idx}: {number}', 'error': 'GB size is missing'})
            continue

        try:
            gb = _normalize_gb(raw_gb)
        except (ValueError, TypeError):
            invalid.append({'line': f'Row {row_idx}: {number} {raw_gb}', 'error': 'Invalid GB value'})
            continue

        line = f"Row {row_idx}: {number} {gb}GB"

        if not number:
            invalid.append({'line': f'Row {row_idx}: (empty)', 'error': 'Phone number is missing'})
            continue
        if not number.isdigit():
            invalid.append({'line': line, 'error': 'Phone must contain digits only'})
            continue
        if not number.startswith('0'):
            invalid.append({'line': line, 'error': 'Phone must start with 0'})
            continue
        if len(number) != 10:
            invalid.append({'line': line, 'error': f'Phone must be 10 digits (got {len(number)})'})
            continue

        if gb not in all_packages:
            invalid.append({'line': line, 'error': f'{gb}GB not available for {network}. Available: {", ".join(valid_gb_strs)}GB'})
            continue

        pair_key = (number, gb)
        if pair_key in seen_pairs:
            invalid.append({'line': line, 'error': f'Duplicate — same number and GB already on Row {seen_pairs[pair_key]}'})
            continue

        same_number_exists = any(k[0] == number for k in seen_pairs)
        seen_pairs[pair_key] = row_idx

        pkg   = all_packages[gb]
        price = pkg.get_price_for_user(request.user)
        valid.append({
            'number'      : number,
            'gb'          : gb,
            'package_id'  : str(pkg.id),
            'price'       : price,
            'is_duplicate': same_number_exists,
        })

    total_amount    = sum(float(r['price']) for r in valid)
    total_gb        = sum(float(r['gb'])    for r in valid)
    wallet          = float(request.user.wallet_balance)
    debt_access     = request.user.debt_page_access
    wallet_used     = min(wallet, total_amount)
    remainder       = max(0, total_amount - wallet)
    debt_needed     = remainder if debt_access else 0
    paystack_needed = remainder if not debt_access else 0

    return render(request, 'agent/buy_excel.html', {
        'networks'       : networks,
        'network'        : network,
        'valid'          : valid,
        'invalid'        : invalid,
        'validated'      : True,
        'total_amount'   : round(total_amount, 2),
        'total_gb'       : round(total_gb, 2),
        'wallet'         : round(wallet, 2),
        'wallet_used'    : round(wallet_used, 2),
        'debt_needed'    : round(debt_needed, 2),
        'paystack_needed': round(paystack_needed, 2),
        'debt_access'    : debt_access,
    })


@agent_required
def submit_excel_view(request):
    """Delegates to submit_bulk_view — identical payment logic applies."""
    return submit_bulk_view(request)


# ---------------------------------------------------------------------------
# Orders
# --------------------------------
@agent_required
def my_orders_view(request):
    from django.core.paginator import Paginator

    search    = request.GET.get('search', '').strip()
    date_from = request.GET.get('date_from', '')
    date_to   = request.GET.get('date_to', '')
    status_filter = request.GET.get('status', '').strip()

    user        = request.user
    agent_links = GeneratedLink.objects.filter(agent=user)

    orders = Order.objects.filter(
        Q(placed_by=user) |
        Q(generated_link__in=agent_links)
    ).select_related('gb_package', 'generated_link').order_by('-created_at').distinct()

    if search:
        orders = orders.filter(
            Q(recipient__icontains=search) |
            Q(order_id__icontains=search)
        )
    if date_from:
        try:
            orders = orders.filter(created_at__date__gte=date_from)
        except Exception:
            pass
    if date_to:
        try:
            orders = orders.filter(created_at__date__lte=date_to)
        except Exception:
            pass

    # BUG FIX: the summary card (Delivered/Waiting/Failed/Refunded counts)
    # must reflect the full search+date range regardless of which status
    # the dropdown is set to. Snapshot the queryset here, BEFORE the status
    # filter is applied below, and use this for the summary counts.
    # Previously the summary was computed from `orders` AFTER the status
    # filter — so picking e.g. "Refunded" made Waiting/Delivered/Failed
    # always show 0, since they were being filtered out of an already
    # Refunded-only queryset.
    base_orders = orders

    if status_filter == 'waiting':
        orders = orders.filter(status__in=['waiting', 'kuntanii_unverified'])
    elif status_filter:
        orders = orders.filter(status=status_filter)

    from django.db.models import Sum
    from django.utils import timezone as _tz

    delivered_qs = base_orders.filter(status='delivered')
    waiting_qs   = base_orders.filter(status__in=['waiting', 'kuntanii_unverified'])
    failed_qs    = base_orders.filter(status='failed')
    refunded_qs  = base_orders.filter(status='refunded')

    status_summary = {
        'delivered': delivered_qs.count(),
        'delivered_amount': delivered_qs.aggregate(t=Sum('price_paid'))['t'] or 0,
        'waiting'  : waiting_qs.count(),
        'waiting_amount': waiting_qs.aggregate(t=Sum('price_paid'))['t'] or 0,
        'failed'   : failed_qs.count(),
        'failed_amount': failed_qs.aggregate(t=Sum('price_paid'))['t'] or 0,
        'refunded' : refunded_qs.count(),
        'refunded_amount': refunded_qs.aggregate(t=Sum('price_paid'))['t'] or 0,
        'as_of': _tz.now(),
    }

    paginator = Paginator(orders, 50)
    page      = request.GET.get('page', 1)
    page_obj  = paginator.get_page(page)

    # ── Calculate profit per order ────────────────────────────────────
    orders_with_profit = []
    for o in page_obj:
        profit = None
        if o.source == 'generated_link' and o.generated_link and o.gb_package and o.payment_status == 'paid':
            lp = o.generated_link.prices.filter(bundle=o.gb_package).first()
            if lp:
                agent_cost = o.gb_package.get_price_for_user(o.generated_link.agent)
                profit = round(float(lp.retail_price) - float(agent_cost), 2)
        orders_with_profit.append((o, profit))

    return render(request, 'agent/my_orders.html', {
        'orders'             : page_obj,
        'orders_with_profit' : orders_with_profit,
        'search'             : search,
        'date_from'          : date_from,
        'date_to'            : date_to,
        'status_filter'      : status_filter,
        'all_statuses'       : [c for c in Order.Status.choices if c[0] != 'kuntanii_unverified'],
        'status_summary'     : status_summary,
    })
@agent_required
def bulk_mark_delivered_view(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid'}, status=400)

    order_ids = request.POST.getlist('order_ids')
    user = request.user
    agent_links = GeneratedLink.objects.filter(agent=user)

    eligible = Order.objects.filter(
        order_id__in=order_ids,
        status__in=['waiting', 'kuntanii_unverified'],
    ).filter(
        Q(placed_by=user) | Q(generated_link__in=agent_links)
    )

    skipped = len(order_ids) - eligible.count()
    updated = 0
    for order in eligible:
        order.status = Order.Status.DELIVERED
        order.save()
        updated += 1

    return JsonResponse({'updated': updated, 'skipped_not_waiting': skipped})


# ---------------------------------------------------------------------------
# Wallet
# ---------------------------------------------------------------------------
@agent_required
def wallet_view(request):
    from wallet.models import Wallet, WalletTransaction
    
    user = request.user
    wallet, _ = Wallet.objects.get_or_create(user=user)
    transactions = WalletTransaction.objects.filter(wallet=wallet).order_by('-created_at')[:30]

    return render(request, 'agent/wallet.html', {
        'balance'       : user.wallet_balance,
        'transactions'  : transactions,
        'admin_whatsapp': getattr(settings, 'ADMIN_WHATSAPP_NUMBER', ''),
    })


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

@agent_required
def manage_links_view(request):
    links       = GeneratedLink.objects.filter(agent=request.user).prefetch_related('orders', 'prices')
    role_limits = {'small_agent': 2}
    max_links   = role_limits.get(request.user.role, 3)
    today       = _local_today()

    total_orders = sum(link.total_orders for link in links)
    total_profit = sum(link.total_profit for link in links)

    today_profit = 0
    for link in links:
        today_orders = Order.objects.filter(
            generated_link=link,
            payment_status='paid',
            created_at__date=today
        )
        for o in today_orders:
            link_price = link.prices.filter(bundle=o.gb_package).first()
            if link_price:
                agent_cost = o.gb_package.get_price_for_user(link.agent)
                today_profit += float(link_price.retail_price) - float(agent_cost)
    today_profit = round(today_profit, 2)

    return render(request, 'agent/manage_links.html', {
        'links'       : links,
        'max_links'   : max_links,
        'total_orders': total_orders,
        'total_profit': total_profit,
        'today_profit': today_profit,
    })
@agent_required
def create_link_view(request):
    user        = request.user
    role_limits = {'small_agent': 2}
    max_links   = role_limits.get(user.role, 3)
    count       = GeneratedLink.objects.filter(agent=user).count()

    if count >= max_links:
        messages.error(request, f'You can only create {max_links} link(s) on your current tier.')
        return redirect('/agent/links/')

    if request.method == 'POST':
        label    = request.POST.get('label', f'Link {count + 1}')
        token    = str(uuid.uuid4()).replace('-', '')[:32]
        packages = _packages_ordered()

        # ── Validate all prices before creating anything ──────────────────
        errors = []
        for pkg in packages:
            price = request.POST.get(f'price_{pkg.id}', '').strip()
            if not price:
                continue
            try:
                entered   = float(price)
                min_price = float(pkg.get_price_for_user(user))
                if entered < min_price:
                    errors.append(
                        f'{pkg.get_network_display()} {pkg.gb_size}GB — '
                        f'GHS {entered} is below your cost of GHS {min_price}.'
                    )
            except (ValueError, TypeError):
                errors.append(f'{pkg.gb_size}GB — invalid price entered.')

        if errors:
            for e in errors:
                messages.error(request, e)
            # Re-render form with errors
            for pkg in packages:
                pkg.agent_price = pkg.get_price_for_user(user)
            return render(request, 'agent/manage_links.html', {
                'creating' : True,
                'packages' : packages,
                'max_links': max_links,
            })

        # ── All prices valid — create the link ────────────────────────────
        link = GeneratedLink.objects.create(agent=user, token=token, label=label)
        for pkg in packages:
            price = request.POST.get(f'price_{pkg.id}', '').strip()
            if price:
                GeneratedLinkPrice.objects.create(link=link, bundle=pkg, retail_price=price)

        messages.success(request, f'Link "{label}" created.')
        return redirect('/agent/links/')

    packages = _packages_ordered()
    for pkg in packages:
        pkg.agent_price = pkg.get_price_for_user(user)

    return render(request, 'agent/manage_links.html', {
        'creating' : True,
        'packages' : packages,
        'max_links': max_links,
    })


@agent_required
def edit_link_view(request, pk):
    link     = get_object_or_404(GeneratedLink, pk=pk, agent=request.user)
    packages = _packages_ordered()

    if request.method == 'POST':

        # ── Validate all prices before saving anything ────────────────────
        errors = []
        for pkg in packages:
            price = request.POST.get(f'price_{pkg.id}', '').strip()
            if not price:
                continue
            try:
                entered   = float(price)
                min_price = float(pkg.get_price_for_user(request.user))
                if entered < min_price:
                    errors.append(
                        f'{pkg.get_network_display()} {pkg.gb_size}GB — '
                        f'GHS {entered} is below your cost of GHS {min_price}.'
                    )
            except (ValueError, TypeError):
                errors.append(f'{pkg.gb_size}GB — invalid price entered.')

        if errors:
            for e in errors:
                messages.error(request, e)
            # Re-render form with errors and prefill entered values
            for pkg in packages:
                pkg.agent_price = pkg.get_price_for_user(request.user)
            prices = {lp.bundle_id: lp.retail_price for lp in link.prices.all()}
            return render(request, 'agent/manage_links.html', {
                'editing' : True,
                'link'    : link,
                'packages': packages,
                'prices'  : prices,
            })

        # ── All prices valid — save ───────────────────────────────────────
        for pkg in packages:
            price = request.POST.get(f'price_{pkg.id}', '').strip()
            if price:
                GeneratedLinkPrice.objects.update_or_create(
                    link=link, bundle=pkg,
                    defaults={'retail_price': price}
                )
            else:
                # Blank = remove this package from the link
                GeneratedLinkPrice.objects.filter(link=link, bundle=pkg).delete()

        messages.success(request, 'Prices updated.')
        return redirect('/agent/links/')

    for pkg in packages:
        pkg.agent_price = pkg.get_price_for_user(request.user)
    prices = {lp.bundle_id: lp.retail_price for lp in link.prices.all()}

    return render(request, 'agent/manage_links.html', {
        'editing' : True,
        'link'    : link,
        'packages': packages,
        'prices'  : prices,
    })


@agent_required
def toggle_link_view(request, pk):
    link           = get_object_or_404(GeneratedLink, pk=pk, agent=request.user)
    link.is_active = not link.is_active
    link.save()
    return redirect('/agent/links/')


@agent_required
def delete_link_view(request, pk):
    link = get_object_or_404(GeneratedLink, pk=pk, agent=request.user)
    if request.method == 'POST':
        link.delete()
        messages.success(request, 'Link deleted successfully.')
    return redirect('/agent/links/')


# ---------------------------------------------------------------------------
# API docs
# ---------------------------------------------------------------------------

@agent_required
def api_docs_view(request):
    return render(request, 'agent/api_docs.html', {
        'has_api': getattr(request.user, 'has_api_access', False),
        'token'  : getattr(request.user, 'api_token', ''),
    })


@agent_required
def verify_password_for_token_view(request):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'Invalid request'}, status=400)

    password = request.POST.get('password', '')
    if request.user.check_password(password):
        return JsonResponse({'ok': True, 'token': request.user.api_token})
    return JsonResponse({'ok': False, 'error': 'Incorrect password'}, status=403)


@agent_required
def api_request_view(request):
    if request.method == 'POST':
        messages.success(request, 'API access request sent to Admin.')
    return redirect('/agent/api-docs/')


# ---------------------------------------------------------------------------
# AFA
# ---------------------------------------------------------------------------

@agent_required
def afa_view(request):
    from afa.models import AFAApplication
    applications = AFAApplication.objects.filter(agent=request.user)
    return render(request, 'agent/afa.html', {'applications': applications})


# ---------------------------------------------------------------------------
# Debt
# ---------------------------------------------------------------------------
@agent_required
def debt_view(request):
    user = request.user

    if not user.debt_page_access:
        already_requested = request.session.get('debt_requested', False)
        return render(request, 'agent/debt.html', {'already_requested': already_requested})

    from wallet.models import Wallet, WalletTransaction
    from collections import defaultdict

    wallet, _ = Wallet.objects.get_or_create(user=user)
    today = _local_today()

    # Only count actual debt shortfall transactions — not all wallet activity
    # Debt incurred = order payments where wallet was short (wallet portion only)
    # Debt deducted = top-up credits that cleared debt
    txns = WalletTransaction.objects.filter(wallet=wallet).order_by('created_at')

    daily = defaultdict(lambda: {'incurred': Decimal('0'), 'deducted': Decimal('0')})

    for tx in txns:
        date_key = tx.created_at.date()
        # Only count wallet portion payments as debt incurred
        if tx.transaction_type == 'debit' and 'wallet portion' in (tx.description or ''):
            daily[date_key]['incurred'] += Decimal(str(tx.amount))
        # Credits from top-ups that cleared debt
        elif tx.transaction_type == 'credit' and tx.reference.startswith('SKP-'):
            daily[date_key]['deducted'] += Decimal(str(tx.amount))

    debt_history = []
    for date_key in sorted(daily.keys(), reverse=True):
        row = daily[date_key]
        if row['incurred'] > 0 or row['deducted'] > 0:
            debt_history.append({
                'date'    : date_key,
                'incurred': row['incurred'],
                'deducted': row['deducted'],
                'net'     : row['incurred'] - row['deducted'],
            })

    today_debt = daily[today]['incurred'] - daily[today]['deducted'] if today in daily else Decimal('0')

    from admin_panel.models import DebtPayment
    debt_payments = DebtPayment.objects.filter(agent=user).select_related('cleared_by').order_by('-created_at')

    return render(request, 'agent/debt.html', {
        'debt'         : user.debt_balance,
        'today_debt'   : max(Decimal('0'), today_debt),
        'total_debt'   : user.debt_balance,
        'debt_history' : debt_history,
        'debt_payments': debt_payments,
    })

@agent_required
def debt_request_view(request):
    if request.method == 'POST':
        request.session['debt_requested'] = True
        messages.success(request, 'Request sent! Admin will review and activate your Debt account.')
        return redirect('/agent/debt/')
    return redirect('/agent/debt/')


# ---------------------------------------------------------------------------
# Complaint
# ---------------------------------------------------------------------------

@agent_required
def complaint_view(request):
    return render(request, 'agent/complaint.html', {
        'admin_whatsapp': request.user.__class__.__module__,
    })


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def logout_view(request):
    logout(request)
    return redirect('/')


# ---------------------------------------------------------------------------
# API access request
# ---------------------------------------------------------------------------

def request_api_access(request):
    from api_access.models import APIRequest

    if request.method == 'POST':
        website_url = request.POST.get('website_url', '')
        reason      = request.POST.get('reason', '')

        existing = APIRequest.objects.filter(agent=request.user, status='pending').first()
        if existing:
            messages.warning(request, 'You already have a pending API request.')
        else:
            APIRequest.objects.create(
                agent=request.user,
                website_url=website_url,
                reason=reason,
                status='pending'
            )
            messages.success(request, 'API access request sent to admin.')

        return redirect('/agent/api-docs/')

    return redirect('/agent/api-docs/')


# ---------------------------------------------------------------------------
# Bundle list (JSON)
# ---------------------------------------------------------------------------

@agent_required
def bundle_list_view(request):
    network  = request.GET.get('network', '')
    packages = BundlePackage.objects.filter(network=network, is_active=True).order_by('gb_size')
    bundles  = []
    for p in packages:
        gb    = _normalize_gb(p.gb_size)
        price = p.get_price_for_user(request.user)
        bundles.append({
            'id'     : p.id,
            'gb_size': gb,
            'price'  : float(price),
            'label'  : f'{gb}GB',
        })
    return JsonResponse({'bundles': bundles})


@agent_required
def bundles_json_view(request):
    network = request.GET.get('network', 'MTN')
    bundles = BundlePackage.objects.filter(network=network, is_active=True)
    data = [
        {
            'id'     : b.id,
            'gb_size': float(b.gb_size),
            'label'  : b.label or f'{b.gb_size}GB',
            'price'  : float(b.get_price_for_user(request.user)),
        }
        for b in bundles
    ]
    return JsonResponse({'bundles': data})




@login_required
def refund_history_view(request):
    from django.core.paginator import Paginator
    from orders.utils import get_refund_history_queryset, calculate_owed_amount
    from decimal import Decimal

    qs = get_refund_history_queryset(request.user, request)

    total_count  = qs.count()
    total_amount = sum((calculate_owed_amount(o) for o in qs), Decimal('0'))

    paginator = Paginator(qs, 50)
    page_obj  = paginator.get_page(request.GET.get('page'))

    return render(request, 'agent/refund_history.html', {
        'page_obj'     : page_obj,
        'total_count'  : total_count,
        'total_amount' : total_amount,
        'filters'      : request.GET,
    })



from django.contrib.auth.decorators import login_required as _refund_login_required


@_refund_login_required
def export_refunded_csv_view(request):
    """Agent's refunded orders as CSV. Optional ?date=YYYY-MM-DD."""
    import csv
    from datetime import datetime
    from django.http import HttpResponse
    from decimal import Decimal
    from orders.utils import calculate_owed_amount

    qs = (Order.objects.filter(placed_by=request.user, status='refunded')
          .select_related('gb_package').order_by('-updated_at'))
    d = request.GET.get('date')
    if d:
        try:
            from django.db.models import Q as _Q
            _day = datetime.strptime(d, '%Y-%m-%d').date()
            qs = qs.filter(_Q(refund_paid_at__date=_day) | _Q(refund_paid_at__isnull=True, updated_at__date=_day))
        except ValueError:
            pass

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="refunded_orders.csv"'
    w = csv.writer(response)
    w.writerow(['Order Number', 'Recipient', 'Network', 'Status', 'Amount (GHS)', 'Credited To Account', 'Refunded At'])
    count = 0
    total = Decimal('0')
    for o in qs:
        amount = calculate_owed_amount(o)
        count += 1
        total += amount
        w.writerow([o.order_id, o.recipient, o.network, 'Refunded', f'{amount:.2f}',
                    'Yes' if o.refund_credited else 'No', (o.refund_paid_at or o.updated_at).strftime('%d %b %Y %H:%M')])
    w.writerow([])
    w.writerow(['Refunded Orders', count, '', '', f'{total:.2f}', '', ''])
    return response
