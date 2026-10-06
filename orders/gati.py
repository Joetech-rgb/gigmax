import re
import time
import requests
from django.conf import settings
from django.utils import timezone
from datetime import timedelta


def is_gati_enabled():
    """EMERGENCY LOCK — hardcoded off. Requires a code change + deploy to enable.
    Admin panel toggle is intentionally bypassed, same pattern as Kuntanii."""
    return False


def is_recipient_blocked(recipient):
    """
    Check if a recipient is on the blocked list.
    Returns True if blocked, False otherwise.
    Works for ALL sources — API, single, bulk, excel, manual.
    """
    try:
        from orders.models import BlockedRecipient
        return BlockedRecipient.objects.filter(recipient=recipient).exists()
    except Exception as e:
        print(f'[BlockedRecipient] Check error: {e}')
        return False


def send_blocked_sms(recipient):
    """
    Send SMS to blocked recipient via Africa's Talking.
    Credentials loaded from Django settings.
    """
    try:
        import africastalking
        africastalking.initialize(
            username=settings.AT_USERNAME,
            api_key=settings.AT_API_KEY,
        )
        sms     = africastalking.SMS
        message = (
            "Dear Customer, your bundle request has been received. "
            "Please note that on 3rd May 2026, you received a double bundle delivery. "
            "This order accounts for that. Thank you for your understanding. - SK Plug"
        )
        number = recipient.strip()
        if number.startswith('0'):
            number = '+233' + number[1:]

        response = sms.send(message, [number])
        print(f'[BlockedRecipient] SMS sent to {recipient}: {response}')
    except Exception as e:
        print(f'[BlockedRecipient] SMS failed for {recipient}: {e}')


def handle_blocked_order(recipient, order_id):
    """
    For a blocked recipient:
    - Mark order as delivered (agent sees this normally)
    - Set is_blocked_order=True (admin sees this in Blocked Numbers tab)
    - Skip Gati entirely
    - Send SMS to recipient
    Works for ALL sources irrespective of how the order was placed.
    """
    try:
        from orders.models import Order
        Order.objects.filter(order_id=order_id).update(
            status='delivered',
            is_blocked_order=True,
        )
        print(f'[BlockedRecipient] Order {order_id} for {recipient} → delivered (blocked), Gati skipped')
    except Exception as e:
        print(f'[BlockedRecipient] handle_blocked_order DB error: {e}')

    try:
        import threading
        threading.Thread(target=send_blocked_sms, args=(recipient,), daemon=True).start()
    except Exception as e:
        print(f'[BlockedRecipient] SMS thread error: {e}')


def send_to_gati(recipient, gb_size, order_id):
    """
    Sends MTN order to Getus only if:
    - Recipient is NOT blocked
    - Getus is enabled
    - Order is pending or processing (not delivered — prevent re-sending)

    Blocked check fires FIRST — before anything else — for ALL sources.
    """

    # ── BLOCKED RECIPIENT CHECK ──────────────────────────────────────
    if is_recipient_blocked(recipient):
        handle_blocked_order(recipient, order_id)
        return None
    # ── END BLOCKED CHECK ────────────────────────────────────────────

    if not is_gati_enabled():
        return None

    if not getattr(settings, 'GATI_API_URL', None) or not getattr(settings, 'GATI_API_KEY', None):
        return None

    try:
        from orders.models import Order
        order = Order.objects.get(order_id=order_id)
        if order.status not in ('pending', 'processing'):
            return None
        if order.gati_order_id:
            return None
    except Exception:
        return None

    try:
        gb_match   = re.search(r'(\d+(?:\.\d+)?)', str(gb_size))
        package_gb = int(float(gb_match.group(1))) if gb_match else int(gb_size)

        response = requests.post(
            f"{settings.GATI_API_URL}/order",
            headers={
                'Content-Type': 'application/json',
                'X-API-Key':    settings.GATI_API_KEY,
            },
            json={
                'network':    'MTN',
                'package_gb': package_gb,
                'recipient':  recipient,
            },
            timeout=10,
        )
        data = response.json()

        if data.get('status') == 'success':
            gatus_order_id = data.get('order_id')

            if gatus_order_id:
                from orders.models import Order
                for attempt in range(3):
                    try:
                        updated = Order.objects.filter(order_id=order_id).update(
                            gati_order_id=str(gatus_order_id),
                            status='processing'
                        )
                        if updated:
                            print(f'[Getus] Order {order_id} saved with Gati ID {gatus_order_id}')
                            break
                    except Exception as e:
                        print(f'[Getus] Retry {attempt + 1} failed saving Gati ID for {order_id}: {e}')
                        time.sleep(1)

            return gatus_order_id

        print(f'[Getus] Order failed: {data}')
        return None

    except Exception as e:
        print(f'[Getus] Error sending order {order_id}: {e}')
        return None


def check_gati_status(gati_order_id):
    """Check status of an order on Getus and sync back."""
    if not getattr(settings, 'GATI_API_URL', None):
        return None

    try:
        response = requests.get(
            f"{settings.GATI_API_URL}/order-status",
            headers={
                'X-API-Key': settings.GATI_API_KEY,
            },
            params={
                'order_id': gati_order_id,
            },
            timeout=10,
        )
        data = response.json()

        if data.get('status') != 'success':
            return None

        gatus_status = data.get('order_status')

        from orders.models import Order
        status_map = {
            'PENDING':     'processing',
            'IN_PROGRESS': 'processing',
            'SUCCESS':     'delivered',
            'FAILED':      'failed',
        }
        our_status = status_map.get(gatus_status)
        if our_status:
            Order.objects.filter(
                gati_order_id=str(gati_order_id)
            ).update(status=our_status)

        return gatus_status

    except Exception as e:
        print(f'[Getus] Error checking status {gati_order_id}: {e}')
        return None


def recover_lost_orders():
    """
    Recover any paid MTN orders never dispatched to Gati.
    Only runs when Gati is enabled.
    Blocked recipients are automatically skipped via send_to_gati check.
    """
    # ── GATI DISABLED CHECK ──────────────────────────────────────────
    if not is_gati_enabled():
        print('[Getus Recovery] Gati is disabled. Skipping recovery.')
        return 0
    # ── END CHECK ────────────────────────────────────────────────────

    from orders.models import Order
    from django.db import models as db_models

    lost = Order.objects.filter(
        network='MTN',
        status__in=['pending', 'processing'],
        payment_status='paid',
        is_blocked_order=False,
    ).filter(
        db_models.Q(gati_order_id__isnull=True) |
        db_models.Q(gati_order_id='') |
        db_models.Q(gati_order_id='None')
    )

    count = lost.count()
    if not count:
        return 0

    print(f'[Getus Recovery] Found {count} lost order(s) — dispatching now...')
    dispatched = 0
    for order in lost:
        try:
            result = send_to_gati(
                recipient=order.recipient,
                gb_size=float(order.gb_package.gb_size),
                order_id=order.order_id,
            )
            if result:
                dispatched += 1
                print(f'[Getus Recovery] Dispatched {order.order_id} → Gati ID {result}')
            else:
                print(f'[Getus Recovery] Failed to dispatch {order.order_id}')
        except Exception as e:
            print(f'[Getus Recovery] Error dispatching {order.order_id}: {e}')

    print(f'[Getus Recovery] Done — {dispatched}/{count} dispatched successfully.')
    return dispatched


def check_gati_health():
    """
    Detect if Gati has been down for over 1 hour and alert admin.
    Only runs when Gati is enabled.
    """
    # ── GATI DISABLED CHECK ──────────────────────────────────────────
    if not is_gati_enabled():
        return 0
    # ── END CHECK ────────────────────────────────────────────────────

    from orders.models import Order

    stuck_count = Order.objects.filter(
        network='MTN',
        status='processing',
        created_at__lt=timezone.now() - timedelta(hours=1),
    ).exclude(gati_order_id__isnull=True).exclude(gati_order_id='').count()

    if stuck_count > 50:
        print(f'[Getus Health] WARNING — {stuck_count} orders stuck for over 1 hour. Gati may be down!')
        try:
            from orders.notifications import notify_admin_gati_down
            notify_admin_gati_down(stuck_count)
        except Exception as e:
            print(f'[Getus Health] Could not send admin alert: {e}')

    return stuck_count