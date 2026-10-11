import requests
from django.conf import settings


def is_kuntanii_enabled():
    """Reads the live DB toggle (KuntaniiSettings.is_enabled), controlled from
    the admin panel. Defaults to False until an admin explicitly enables it."""
    from admin_panel.models import KuntaniiSettings
    return KuntaniiSettings.get().is_enabled


def _headers():
    return {
        'Authorization': f"Bearer {settings.KUNTANII_API_KEY}",
        'Content-Type': 'application/json',
    }


def verify_beneficiary(recipient):
    """
    Check if a recipient is eligible with Kuntanii before creating an order.
    Returns a dict: {'eligible': bool, 'reason': str, 'message': str} or None on error.
    """
    if not getattr(settings, 'KUNTANII_API_URL', None):
        return None

    try:
        response = requests.post(
            f"{settings.KUNTANII_API_URL}/api/integrations/beneficiaries/verify",
            headers=_headers(),
            json={'phoneNumbers': [recipient]},
            timeout=10,
        )
        data = response.json()
        if not data.get('success'):
            print(f'[Kuntanii] Verify failed for {recipient}: {data}')
            return None

        results = data.get('results', [])
        if not results:
            return None

        return results[0]

    except Exception as e:
        print(f'[Kuntanii] Verify error for {recipient}: {e}')
        return None


def send_to_kuntanii(recipient, gb_size, order_id, verify_result=None):
    """
    Verifies the recipient first. If not eligible (new/unverified number),
    sets the order to 'waiting' directly — no Kuntanii order is created,
    nothing is charged.

    If eligible, submits the order to Kuntanii and stores kuntanii_reference.
    """
    from orders.models import Order

    try:
        order = Order.objects.get(order_id=order_id)
        if order.status not in ('pending', 'processing'):
            return None
        if order.kuntanii_reference:
            return None
    except Exception:
        return None

    if verify_result is None:
        verify_result = verify_beneficiary(recipient)
    if verify_result is None:
        print(f'[Kuntanii] Could not verify {recipient} — leaving order {order_id} untouched for retry.')
        return None

    if not verify_result.get('eligible'):
        reason = verify_result.get('reason') or verify_result.get('message') or 'Not on Kuntanii beneficiary list'
        print(f'[Kuntanii] {recipient} not eligible ({reason}) — marking order {order_id} as kuntanii_unverified (no auto-refund; held for manual review).')
        from django.utils import timezone
        # Load-then-save. kuntanii_unverified is NOT auto-refunded; the order
        # is held for manual review until an admin converts it to refunded.
        o = Order.objects.filter(order_id=order_id).first()
        if o:
            o.status = 'kuntanii_unverified'
            o.kuntanii_unverified_reason = reason
            o.kuntanii_unverified_at = timezone.now()
            o.save()
        return None

    # Eligible — proceed to create the order
    try:
        response = requests.post(
            f"{settings.KUNTANII_API_URL}/api/integrations/orders",
            headers={**_headers(), 'X-Kuntanii-Idempotency-Key': order_id},
            json={
                'externalOrderId': order_id,
                'items': [{'phoneNumber': recipient, 'dataGb': float(gb_size)}],
            },
            timeout=15,
        )
        data = response.json()

        if not data.get('success'):
            print(f'[Kuntanii] Order creation failed for {order_id}: {data}')
            return None

        kuntanii_ref = data.get('orderReference')
        if kuntanii_ref:
            Order.objects.filter(order_id=order_id).update(
                kuntanii_reference=kuntanii_ref,
                status='processing',
            )
            print(f'[Kuntanii] Order {order_id} created — reference {kuntanii_ref}')

        return kuntanii_ref

    except Exception as e:
        print(f'[Kuntanii] Error creating order {order_id}: {e}')
        return None


UNREADABLE_PREFIX = 'status_unreadable'
UNRECOGNIZED_PREFIX = 'unrecognized_status'


def _stamp_unreadable(order_id, prefix, detail, move_to_unverified):
    # Starts the refund clock the first time a given problem is seen.
    # Never restarts a clock that is already running.
    from django.utils import timezone
    from orders.models import Order

    o = Order.objects.filter(order_id=order_id).first()
    if o is None or o.refund_credited or o.status not in ('processing', 'kuntanii_unverified'):
        return
    if (o.kuntanii_unverified_reason or '').startswith(prefix) and o.kuntanii_unverified_at:
        return
    reason = ('%s: %s' % (prefix, detail))[:250]
    if move_to_unverified and o.status != 'kuntanii_unverified':
        o.status = 'kuntanii_unverified'
        o.kuntanii_unverified_reason = reason
        o.kuntanii_unverified_at = timezone.now()
        o.save()  # only a move to REFUNDED moves money
    else:
        Order.objects.filter(pk=o.pk).update(
            kuntanii_unverified_reason=reason,
            kuntanii_unverified_at=timezone.now(),
            updated_at=timezone.now(),
        )


def _clear_clock(order_id):
    from orders.models import Order
    for prefix in (UNREADABLE_PREFIX, UNRECOGNIZED_PREFIX, 'Unrecognized Kuntanii status'):
        Order.objects.filter(
            order_id=order_id, refund_credited=False,
            kuntanii_unverified_reason__startswith=prefix,
        ).update(kuntanii_unverified_reason=None, kuntanii_unverified_at=None)


def check_kuntanii_status(order_id):
    # Reads one order's live status from Kuntanii.
    #   processed / still processing -> our status is updated, any refund clock is cleared
    #   timeout, API error, odd reply -> nothing is assumed, the refund clock starts
    #   unrecognised status           -> kuntanii_unverified, the refund clock starts
    # Refunds themselves are issued only by orders.kuntanii_refunds.
    if not getattr(settings, 'KUNTANII_API_URL', None):
        return None

    from orders.models import Order

    if not (Order.objects.filter(order_id=order_id, refund_credited=False)
            .exclude(kuntanii_reference__isnull=True)
            .exclude(kuntanii_reference='')
            .exists()):
        return None

    try:
        response = requests.get(
            f"{settings.KUNTANII_API_URL}/api/integrations/orders/{order_id}",
            headers=_headers(),
            timeout=10,
        )
        data = response.json()
    except Exception as e:
        print(f'[Kuntanii] Could not read status for {order_id}: {e}')
        _stamp_unreadable(order_id, UNREADABLE_PREFIX, type(e).__name__, False)
        return None

    if not isinstance(data, dict) or not data.get('success'):
        print(f'[Kuntanii] Unreadable reply for {order_id}: http {response.status_code}')
        _stamp_unreadable(order_id, UNREADABLE_PREFIX, 'http %s' % response.status_code, False)
        return None

    orders_list = data.get('orders') or []
    kt_status = orders_list[0].get('status') if orders_list else None

    status_map = {
        'pending':    'processing',
        'processing': 'processing',
        'processed':  'delivered',
    }
    our_status = status_map.get(kt_status)

    order_obj = Order.objects.filter(order_id=order_id).first()
    if order_obj is None or order_obj.status == 'refunded':
        return kt_status

    if our_status:
        _clear_clock(order_id)
        order_obj.refresh_from_db()
        if order_obj.status != our_status:
            order_obj.status = our_status
            order_obj.save()
        return kt_status

    print(f'[Kuntanii] UNRECOGNIZED status "{kt_status}" for order {order_id}. Raw response: {data}')
    _stamp_unreadable(order_id, UNRECOGNIZED_PREFIX, kt_status, True)
    return kt_status


def report_not_received(order_id, phone_number, data_gb=None, description=''):
    """
    Opens a dispute with Kuntanii for a recipient who says they never
    received their bundle. Used by the customer 'Request Refund' button.
    """
    if not getattr(settings, 'KUNTANII_API_URL', None):
        return None

    payload = {'phoneNumber': phone_number, 'description': description}
    if data_gb is not None:
        payload['dataGb'] = float(data_gb)

    try:
        response = requests.post(
            f"{settings.KUNTANII_API_URL}/api/integrations/orders/{order_id}/not-received",
            headers=_headers(),
            json=payload,
            timeout=10,
        )
        data = response.json()
        if data.get('success'):
            print(f'[Kuntanii] Dispute opened for {order_id}: {data.get("reportReference")}')
        return data

    except Exception as e:
        print(f'[Kuntanii] Error reporting not-received for {order_id}: {e}')
        return None
