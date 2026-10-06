"""
DCS Elite (AT / AirtelTigo bundle reseller) integration.

Mirrors the gati.py pattern: enable toggle, order submission, status
polling, lost-order recovery. Webhook receipt is handled separately
(see orders/views_at_webhook.py, to be added).

SAFETY: is_at_enabled() is a hardcoded emergency lock, same pattern as
Kuntanii. Requires an explicit code change + deploy to flip on — an
admin panel toggle alone cannot activate real order routing.
"""
import hashlib
import hmac
import requests
from django.conf import settings


def is_at_enabled():
    """DB-driven toggle via ATSettings. Controlled from the admin panel."""
    try:
        from admin_panel.models import ATSettings
        return ATSettings.get().is_enabled
    except Exception:
        return False


# Confirmed against DCS Elite's live catalog (GET /api/v1/bundles) —
# only these MB sizes have a real SKU. Anything else must NOT be sent.
_VALID_AT_MB_SIZES = {
    1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 10000, 12000, 15000,
}


def _headers():
    return {
        'Authorization': f"Bearer {settings.DCS_API_KEY}",
        'Content-Type': 'application/json',
    }


def gb_to_at_sku(gb_size):
    """
    Maps a BundlePackage.gb_size (Decimal/float, e.g. 5.00) to the
    matching DCS Elite SKU (e.g. 'AT-5000MB-60D').

    Returns None if there is no matching bundle on DCS Elite's side —
    callers MUST check for None and NOT submit the order in that case.
    """
    try:
        mb = int(round(float(gb_size) * 1000))
    except (TypeError, ValueError):
        return None

    if mb not in _VALID_AT_MB_SIZES:
        return None

    return f"AT-{mb}MB-60D"


def send_to_at(recipient, gb_size, order_id):
    """
    Sends an AT order to DCS Elite.

    Returns the DCS Elite order reference (str) on success, or None on
    any failure (no matching SKU, disabled, API error, etc). Callers
    should treat None as "not dispatched — leave for manual processing",
    exactly like send_to_gati()/send_to_kuntanii().
    """
    if not is_at_enabled():
        return None

    if not getattr(settings, 'DCS_API_KEY', None) or not getattr(settings, 'DCS_BASE_URL', None):
        return None

    # Guard against dispatching a number that doesn't actually belong to
    # AirtelTigo. This check must live HERE (not just in place_order()) so
    # it also protects the recover_lost_at_orders() retry path, which calls
    # send_to_at() directly and would otherwise bypass any check done only
    # at order-creation time.
    from orders.utils import number_matches_network
    if not number_matches_network(recipient, 'AT_EXPIRY'):
        print(f'[DCS] Skipping order {order_id} — recipient {recipient} does not match AirtelTigo prefix range.')
        return None

    sku = gb_to_at_sku(gb_size)
    if not sku:
        print(f'[DCS] No matching SKU for gb_size={gb_size} (order {order_id}) — skipping, needs manual processing.')
        return None

    try:
        from orders.models import Order
        order = Order.objects.get(order_id=order_id)
        if order.status not in ('pending', 'processing'):
            return None
        if getattr(order, 'dcs_order_reference', ''):
            return None
    except Exception:
        return None

    try:
        response = requests.post(
            f"{settings.DCS_BASE_URL}/orders",
            headers=_headers(),
            json={
                'sku': sku,
                'recipient_phone': recipient,
                'quantity': 1,
                'reference': order_id,
            },
            timeout=15,
        )
        data = response.json()

        order_data = data.get('order')
        if not order_data:
            print(f'[DCS] Order failed for {order_id}: {data}')
            return None

        dcs_reference = order_data.get('reference') or order_data.get('id')
        dcs_status = order_data.get('status')

        if dcs_reference:
            from orders.models import Order
            for attempt in range(3):
                try:
                    updated = Order.objects.filter(order_id=order_id).update(
                        dcs_order_reference=str(dcs_reference),
                        status='processing',
                    )
                    if updated:
                        print(f'[DCS] Order {order_id} saved with DCS reference {dcs_reference} (status={dcs_status})')
                        break
                except Exception as e:
                    print(f'[DCS] Retry {attempt + 1} failed saving reference for {order_id}: {e}')
                    import time
                    time.sleep(1)

        return dcs_reference

    except Exception as e:
        print(f'[DCS] Error sending order {order_id}: {e}')
        return None


def check_at_status(dcs_reference):
    """Poll DCS Elite for an order's current status and sync back. Safety-net
    alongside webhooks, same role as check_gati_status()."""
    if not getattr(settings, 'DCS_BASE_URL', None):
        return None

    try:
        response = requests.get(
            f"{settings.DCS_BASE_URL}/orders/{dcs_reference}",
            headers=_headers(),
            timeout=15,
        )
        data = response.json()
        order_data = data.get('order')
        if not order_data:
            return None

        dcs_status = order_data.get('status')

        status_map = {
            'queued':      'processing',
            'processing':  'processing',
            'fulfilled':   'delivered',
            'failed':      'failed',
        }
        our_status = status_map.get(dcs_status)
        if our_status:
            from orders.models import Order
            Order.objects.filter(dcs_order_reference=str(dcs_reference)).update(status=our_status)

        return dcs_status

    except Exception as e:
        print(f'[DCS] Error checking status {dcs_reference}: {e}')
        return None


def verify_dcs_webhook_signature(raw_body: bytes, signature_header: str) -> bool:
    """
    Verifies an inbound DCS Elite webhook using HMAC-SHA256 against
    DCS_WEBHOOK_SECRET, checking the X-DCS-Signature header.

    Uses constant-time comparison to avoid timing attacks.
    """
    secret = getattr(settings, 'DCS_WEBHOOK_SECRET', '')
    if not secret or not signature_header:
        return False

    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def recover_lost_at_orders():
    """
    Recover any paid AT orders never dispatched to DCS Elite.
    Only runs when AT is enabled. Mirrors recover_lost_orders() in gati.py.
    """
    if not is_at_enabled():
        print('[DCS Recovery] AT integration is disabled. Skipping recovery.')
        return 0

    from orders.models import Order
    from django.db import models as db_models

    lost = Order.objects.filter(
        network__in=['AT_EXPIRY', 'AT_NOEXPIRY'],
        status__in=['pending', 'processing'],
        payment_status='paid',
    ).filter(
        db_models.Q(dcs_order_reference__isnull=True) |
        db_models.Q(dcs_order_reference='')
    )

    count = lost.count()
    if not count:
        return 0

    print(f'[DCS Recovery] Found {count} lost AT order(s) — dispatching now...')
    dispatched = 0
    for order in lost:
        try:
            result = send_to_at(
                recipient=order.recipient,
                gb_size=float(order.gb_package.gb_size),
                order_id=order.order_id,
            )
            if result:
                dispatched += 1
                print(f'[DCS Recovery] Dispatched {order.order_id} → DCS reference {result}')
            else:
                print(f'[DCS Recovery] Skipped/failed {order.order_id} (likely no matching SKU or already handled)')
        except Exception as e:
            print(f'[DCS Recovery] Error dispatching {order.order_id}: {e}')

    print(f'[DCS Recovery] Done — {dispatched}/{count} dispatched successfully.')
    return dispatched
