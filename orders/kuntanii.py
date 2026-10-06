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
        print(f'[Kuntanii] {recipient} not eligible ({reason}) — marking order {order_id} as kuntanii_unverified (auto-refund will fire via signal).')
        from django.utils import timezone
        # Load-then-save (not .update()) so pre_save fires and the agent's
        # wallet/debt is auto-credited, same as the waiting-order path.
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


def check_kuntanii_status(order_id):
    """
    Checks live status of an order via Kuntanii using our own order_id
    (used as externalOrderId). Maps their status to ours.

    Safety net: any status value we don't recognize is flagged as
    'kuntanii_unverified' (not auto-refunded) so it can be reviewed
    manually, rather than silently ignored or treated as a confirmed
    refund.
    """
    if not getattr(settings, 'KUNTANII_API_URL', None):
        return None

    try:
        response = requests.get(
            f"{settings.KUNTANII_API_URL}/api/integrations/orders/{order_id}",
            headers=_headers(),
            timeout=10,
        )
        data = response.json()

        if not data.get('success'):
            return None

        orders_list = data.get('orders', [])
        kt_status = orders_list[0].get('status') if orders_list else None

        from orders.models import Order
        status_map = {
            'pending':    'processing',
            'processing': 'processing',
            'processed':  'delivered',
        }
        our_status = status_map.get(kt_status)
        target_status = our_status or 'kuntanii_unverified'

        order_obj = Order.objects.filter(order_id=order_id).first()
        if order_obj:
            # Load-then-save (not .update()) so the pre_save signal fires and
            # agent wallet/debt crediting happens automatically on refund.
            order_obj.status = target_status
            if not our_status:
                from django.utils import timezone
                order_obj.kuntanii_unverified_reason = f'Unrecognized Kuntanii status: "{kt_status}"'
                order_obj.kuntanii_unverified_at = timezone.now()
            order_obj.save()
            if not our_status:
                print(f'[Kuntanii] UNRECOGNIZED status "{kt_status}" for order {order_id} — '
                      f'flagged as kuntanii_unverified for review. Raw response: {data}')

        return kt_status

    except Exception as e:
        print(f'[Kuntanii] Error checking status for {order_id}: {e}')
        return None


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
