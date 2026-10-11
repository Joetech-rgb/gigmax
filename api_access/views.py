from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django_ratelimit.decorators import ratelimit
from accounts.models import User
from bundles.models import BundlePackage
from orders.models import Order
from orders.utils import place_order, validate_ghana_number


@csrf_exempt
def dcs_webhook_view(request):
    """
    Receives order.fulfilled / order.failed events from DCS Elite.

    Security: verifies the X-DCS-Signature header (HMAC-SHA256 over the
    raw request body) before trusting anything in the payload. Any
    request that fails verification is rejected with 401 and never
    touches the database.
    """
    import json
    from orders.at import verify_dcs_webhook_signature

    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    signature = request.headers.get('X-DCS-Signature', '')
    raw_body = request.body

    if not verify_dcs_webhook_signature(raw_body, signature):
        return JsonResponse({'error': 'Invalid signature'}, status=401)

    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    event = payload.get('event')
    order_data = payload.get('order') or {}
    reference = order_data.get('reference')

    if not reference:
        return JsonResponse({'error': 'Missing order reference'}, status=400)

    status_map = {
        'order.fulfilled': 'delivered',
        'order.failed':    'failed',
    }
    our_status = status_map.get(event)

    if our_status:
        updated = Order.objects.filter(dcs_order_reference=reference).update(status=our_status)
        if not updated:
            # Reference not recognized — ack anyway so DCS doesn't keep retrying
            # a webhook we can never process, but log it for investigation.
            print(f'[DCS Webhook] No matching order for reference={reference} (event={event})')

    return JsonResponse({'ok': True})


def get_user_by_token(request):
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    try:
        return User.objects.get(api_token=token, has_api_access=True, is_active=True)
    except User.DoesNotExist:
        return None


@csrf_exempt
@api_view(['POST'])
@ratelimit(key="ip", rate="20/m", block=True)
def create_order_api(request):
    user = get_user_by_token(request)
    if not user:
        return Response({'error': 'Invalid or missing API token.'}, status=401)
    if user.is_paused:
        return Response({'error': 'Account paused. Contact Admin.'}, status=403)

    recipient = request.data.get('recipient')
    network   = request.data.get('network', '').upper()
    gb_size   = request.data.get('gb_size')

    if not all([recipient, network, gb_size]):
        return Response({'error': 'recipient, network and gb_size are required.'}, status=400)

    # ── MTN block ─────────────────────────────────────────────────────────
    if network == 'MTN':
        from admin_panel.models import SiteSettings
        if not SiteSettings.get().mtn_enabled:
            return Response(
                {'error': 'MTN orders are temporarily unavailable. Please try again later.'},
                status=503
            )
    # ─────────────────────────────────────────────────────────────────────

    if not validate_ghana_number(recipient):
        return Response({'error': 'Invalid recipient number.'}, status=400)

    try:
        package = BundlePackage.objects.get(network=network, gb_size=gb_size, is_active=True)
    except BundlePackage.DoesNotExist:
        valid_networks = sorted(set(BundlePackage.objects.filter(is_active=True).values_list('network', flat=True)))
        return Response({
            'error': 'Bundle package not found.',
            'requested': {'network': network, 'gb_size': gb_size},
            'valid_networks': valid_networks,
        }, status=404)

    # ── Duplicate check ───────────────────────────────────────────────────
    from django.utils import timezone
    from datetime import timedelta
    ten_minutes_ago = timezone.now() - timedelta(minutes=10)

    recent_duplicate = Order.objects.filter(
        placed_by       = user,
        recipient       = recipient,
        gb_package      = package,
        created_at__gte = ten_minutes_ago,
    ).exclude(status='failed').first()

    if recent_duplicate:
        return Response({
            'error'   : 'Duplicate order detected. An identical order was placed within the last 10 minutes.',
            'order_id': recent_duplicate.order_id,
            'status'  : recent_duplicate.status,
            'tip'     : 'If you intended a new order, wait 10 minutes or use a different package.',
        }, status=409)
    # ─────────────────────────────────────────────────────────────────────

    price = package.get_price_for_user(user)

    try:
        from orders.utils import _apply_payment
        _apply_payment(user, price)
    except ValueError:
        return Response({'error': 'Insufficient wallet balance.'}, status=402)

    order = place_order(
        package   = package,
        recipient = recipient,
        price     = price,
        source    = Order.Source.API,
        placed_by = user,
        mark_paid = True,
        via_api   = True,
    )
    return Response({
        'order_id' : order.order_id,
        'status'   : order.status,
        'network'  : order.network,
        'recipient': order.recipient,
        'gb_size'  : str(package.gb_size),
        'price'    : str(price),
    }, status=201)


@csrf_exempt
@api_view(['GET'])
def list_orders_api(request):
    user = get_user_by_token(request)
    if not user:
        return Response({'error': 'Unauthorized'}, status=401)

    page          = int(request.GET.get('page', 1))
    page_size     = min(int(request.GET.get('page_size', 50)), 200)
    offset        = (page - 1) * page_size
    status_filter = request.GET.get('status', '').strip()

    qs = Order.objects.filter(placed_by=user, source='api')
    if status_filter:
        qs = qs.filter(status=status_filter)

    orders = qs.order_by('-created_at').values(
        'order_id', 'network', 'recipient', 'status', 'price_paid', 'created_at'
    )[offset:offset + page_size]

    results = list(orders)
    return Response({
        'count'    : len(results),
        'page'     : page,
        'page_size': page_size,
        'results'  : results,
    })


@csrf_exempt
@api_view(['GET'])
def list_bundles_api(request):
    user = get_user_by_token(request)
    if not user:
        return Response({'error': 'Unauthorized'}, status=401)

    from django.contrib.auth import get_user_model
    fresh_user = get_user_model().objects.get(pk=user.pk)

    packages = BundlePackage.objects.filter(is_active=True).order_by('network', 'gb_size')

    results = []
    for pkg in packages:
        results.append({
            'id'         : pkg.id,
            'network'    : pkg.network,
            'gb_size'    : str(pkg.gb_size),
            'label'      : pkg.label or f'{pkg.gb_size}GB',
            'your_price' : str(pkg.get_price_for_user(fresh_user)),
        })

    return Response(results)


@csrf_exempt
@api_view(['GET'])
def order_status_api(request, order_id):
    user = get_user_by_token(request)
    if not user:
        return Response({'error': 'Unauthorized'}, status=401)
    try:
        order = Order.objects.get(order_id=order_id, placed_by=user)
        display_status = 'waiting' if order.status == 'kuntanii_unverified' else order.status
        return Response({
            'order_id': order.order_id,
            'status': order.status,
            'display_status': display_status,
        })
    except Order.DoesNotExist:
        return Response({'error': 'Order not found.'}, status=404)


@csrf_exempt
@api_view(['GET'])
def waiting_orders_api(request):
    user = get_user_by_token(request)
    if not user:
        return Response({'error': 'Unauthorized'}, status=401)

    from django.db.models import Sum, Count
    from django.core.paginator import Paginator

    qs = Order.objects.filter(placed_by=user, source='api', status__in=['waiting', 'kuntanii_unverified'])

    summary = qs.aggregate(
        total_count  = Count('id'),
        total_amount = Sum('price_paid'),
    )

    ordered_qs = qs.order_by('-created_at').values(
        'order_id', 'network', 'recipient', 'price_paid', 'created_at'
    )

    # Pagination: defaults to page 1, 50 per page. Callers pass
    # ?page=N&page_size=N (page_size capped at 200 to prevent abuse).
    try:
        page_size = min(int(request.GET.get('page_size', 50)), 200)
    except (TypeError, ValueError):
        page_size = 50
    try:
        page_number = int(request.GET.get('page', 1))
    except (TypeError, ValueError):
        page_number = 1

    paginator = Paginator(ordered_qs, page_size)
    page_obj  = paginator.get_page(page_number)

    return Response({
        'total_count'  : summary['total_count'] or 0,
        'total_amount' : str(summary['total_amount'] or 0),
        'page'         : page_obj.number,
        'total_pages'  : paginator.num_pages,
        'page_size'    : page_size,
        'has_next'     : page_obj.has_next(),
        'has_previous' : page_obj.has_previous(),
        'orders'       : list(page_obj.object_list),
    })


@csrf_exempt
@api_view(['GET'])
def orders_summary_api(request):
    user = get_user_by_token(request)
    if not user:
        return Response({'error': 'Unauthorized'}, status=401)

    from django.db.models import Q
    from decimal import Decimal
    from orders.utils import calculate_owed_amount
    from orders.models import GeneratedLink

    agent_links = GeneratedLink.objects.filter(agent=user)
    all_orders = Order.objects.filter(
        Q(placed_by=user) | Q(generated_link__in=agent_links)
    ).distinct()

    def serialize(qs, paid_field=False):
        rows = []
        total = Decimal('0')
        for o in qs.order_by('-created_at'):
            amount = calculate_owed_amount(o)
            total += amount
            row = {
                'order_id'   : o.order_id,
                'network'    : o.network,
                'recipient'  : o.recipient,
                'gb_size'    : str(o.gb_package.gb_size) if o.gb_package else None,
                'price_paid' : str(o.price_paid or 0),
                'amount_owed': str(amount),
                'status'     : o.status,
                'created_at' : o.created_at,
            }
            if paid_field:
                row['paid'] = (o.status == 'refunded')
            rows.append(row)
        return rows, total

    waiting_qs = all_orders.filter(status__in=['waiting', 'kuntanii_unverified'])
    refunded_qs = all_orders.filter(status='refunded')
    refund_requested_qs = all_orders.filter(refund_requested=True)

    waiting_orders, waiting_total = serialize(waiting_qs)
    refunded_orders, refunded_total = serialize(refunded_qs)
    refund_requested_orders, refund_requested_total = serialize(refund_requested_qs, paid_field=True)

    return Response({
        'waiting': {
            'count'  : waiting_qs.count(),
            'total'  : str(waiting_total),
            'orders' : waiting_orders,
        },
        'refunded': {
            'count'  : refunded_qs.count(),
            'total'  : str(refunded_total),
            'orders' : refunded_orders,
        },
        'refund_requested': {
            'count'  : refund_requested_qs.count(),
            'total'  : str(refund_requested_total),
            'orders' : refund_requested_orders,
        },
    })


@csrf_exempt
@api_view(['GET'])
def wallet_balance_api(request):
    user = get_user_by_token(request)
    if not user:
        return Response({'error': 'Unauthorized'}, status=401)

    from django.contrib.auth import get_user_model
    fresh_user = get_user_model().objects.get(pk=user.pk)

    return Response({
        'wallet_balance': str(fresh_user.wallet_balance),
        'debt_balance'  : str(fresh_user.debt_balance),
        'debt_access'   : fresh_user.debt_page_access,
    })


@csrf_exempt
@api_view(['POST', 'GET'])
def register_callback_api(request):
    user = get_user_by_token(request)
    if not user:
        return Response({'error': 'Unauthorized'}, status=401)

    if request.method == 'GET':
        return Response({
            'callback_url': user.callback_url or None,
            'message'     : 'POST a callback_url to register your webhook endpoint.',
        })

    callback_url = request.data.get('callback_url', '').strip()
    if not callback_url:
        user.callback_url = None
        user.save(update_fields=['callback_url'])
        return Response({'message': 'Callback URL cleared.'})

    if not callback_url.startswith(('http://', 'https://')):
        return Response({'error': 'callback_url must start with http:// or https://'}, status=400)

    user.callback_url = callback_url
    user.save(update_fields=['callback_url'])
    return Response({
        'message'     : 'Callback URL registered successfully.',
        'callback_url': user.callback_url,
        'note'        : 'We will POST to this URL when your orders are delivered. Verify using X-GigMax-Signature header.',
    })