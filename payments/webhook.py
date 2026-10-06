import json
import hmac
import hashlib
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from django.conf import settings
from django.utils import timezone
from django.db import connection

# DEPRECATED: Use /wallet/paystack/webhook/ as your Paystack webhook URL.
# This endpoint is kept only for backwards-compatibility and returns 200
# immediately without processing any events.
@csrf_exempt
def paystack_webhook(request):
    # All webhook processing has moved to wallet/views.py:paystack_webhook_view
    return JsonResponse({'status': 'deprecated_use_wallet_webhook'}, status=200)
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    signature = request.headers.get('x-paystack-signature')
    if not signature:
        return JsonResponse({'error': 'No signature'}, status=400)

    secret = settings.PAYSTACK_SECRET_KEY
    computed = hmac.new(secret.encode(), request.body, hashlib.sha512).hexdigest()

    if not hmac.compare_digest(signature, computed):
        return JsonResponse({'error': 'Invalid signature'}, status=401)

    try:
        payload = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    event = payload.get('event')
    reference = payload.get('data', {}).get('reference')

    # --- Successful payment ---
    if event == 'charge.success' and reference:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT order_id FROM orders_order WHERE payment_reference = %s",
                    [reference]
                )
                result = cursor.fetchone()

            if result:
                order_id = result[0]
                now = timezone.now().isoformat()
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE orders_order SET payment_status = %s, paid_at = %s, status = %s WHERE order_id = %s",
                        ['paid', now, 'processing', order_id]
                    )
                return JsonResponse({'status': 'success', 'order_id': order_id})
            else:
                return JsonResponse({'error': 'Order not found'}, status=404)
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)

    # --- Failed / abandoned payment ---
    if event in ('charge.failed', 'transfer.failed') and reference:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE orders_order SET payment_status = %s, status = %s WHERE payment_reference = %s",
                    ['failed', 'failed', reference]
                )
            return JsonResponse({'status': 'marked_failed'})
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)

    return JsonResponse({'status': 'ignored'})

@csrf_exempt
def expire_pending_payments(request):
    from orders.models import Order
    from wallet.views import _confirm_order_payment
    from wallet.gateway import verify_transaction

    now = timezone.now()

    # Find expired pending orders
    expired_orders = Order.objects.filter(
        payment_status='pending',
        payment_expires_at__isnull=False,
        payment_expires_at__lt=now,
    ).exclude(payment_reference='')

    count = 0
    rescued = 0

    for order in expired_orders:
        try:
            # Check Paystack before killing it
            result = verify_transaction(order.payment_reference)
            if result.get('data', {}).get('status') == 'success':
                # Payment actually went through — confirm it
                _confirm_order_payment(order.payment_reference)
                rescued += 1
                continue
        except Exception:
            pass

        # Truly failed — expire it
        Order.objects.filter(
            order_id=order.order_id,
            payment_status='pending',
        ).update(payment_status='failed', status='failed')
        count += 1

    return JsonResponse({
        'expired': count,
        'rescued': rescued,
        'checked_at': now.isoformat()
    })