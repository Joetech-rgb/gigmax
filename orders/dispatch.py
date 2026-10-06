"""
Central MTN dispatch router.

Every place in the codebase that needs to send an MTN order to an external
processor (Kuntanii or Gati) should call dispatch_mtn_order() instead of
calling send_to_gati() or send_to_kuntanii() directly. This guarantees a
single order is never sent to more than one processor.

Priority: Kuntanii first if enabled, otherwise Gati if enabled, otherwise
the order stays untouched for manual processing.
"""


def dispatch_mtn_order(recipient, gb_size, order_id):
    from orders.models import Order

    try:
        order = Order.objects.get(order_id=order_id)
    except Exception:
        return None

    # Already dispatched to one processor — never dispatch to another.
    if order.gati_order_id:
        return None
    if order.kuntanii_reference:
        return None

    from orders.kuntanii import is_kuntanii_enabled, send_to_kuntanii, verify_beneficiary
    if is_kuntanii_enabled():
        verify_result = verify_beneficiary(recipient)
        if verify_result is None:
            # Kuntanii's verify API itself errored — proceed to dispatch anyway.
            # This is a rare Kuntanii-side reliability issue, not a signal the
            # number is bad. Logged distinctly so we can revisit this decision
            # if it turns out to happen more often than expected.
            print(f'[Kuntanii] verify_beneficiary returned None for {recipient} '
                  f'(order {order_id}) — proceeding to dispatch anyway.')
        elif not verify_result.get('eligible', True):
            from django.utils import timezone
            reason = verify_result.get('reason') or verify_result.get('message') or 'Not on Kuntanii beneficiary list'
            # Load-then-save (not .update()) so pre_save fires — this is what
            # triggers automatic agent wallet credit / debt reduction. A bare
            # .update() bypasses Django signals entirely and silently skips
            # that crediting logic (this was the root cause of several
            # uncredited refunds discovered in production on 2026-09-15).
            o = Order.objects.filter(order_id=order_id).first()
            if o:
                o.status = 'kuntanii_unverified'
                o.kuntanii_unverified_reason = reason
                o.kuntanii_unverified_at = timezone.now()
                o.save()
            print(f'[Kuntanii] {recipient} (order {order_id}) flagged as new/unverified — '
                  f'set to Waiting. reason={reason}')
            # Return None (not a reference) — the Waiting status is already
            # saved above. Callers check `if reference:` to decide whether to
            # mark via_api=True; a Waiting order was never actually dispatched
            # to a processor, so it must NOT be treated as a truthy success.
            return None
        return send_to_kuntanii(recipient, gb_size, order_id, verify_result=verify_result)

    from orders.gati import is_gati_enabled, send_to_gati
    if is_gati_enabled():
        return send_to_gati(recipient, gb_size, order_id)

    return None


def dispatch_at_order(recipient, gb_size, order_id):
    """
    Central AT (AirtelTigo) dispatch router — mirrors dispatch_mtn_order().

    Only AT_EXPIRY orders at sizes DCS Elite actually sells get dispatched;
    everything else (9GB, all AT_NOEXPIRY) is left alone for manual
    processing — send_to_at() already returns None gracefully for those,
    so no special-casing is needed here.
    """
    from orders.models import Order

    try:
        order = Order.objects.get(order_id=order_id)
    except Exception:
        return None

    # Already dispatched — never dispatch twice.
    if order.dcs_order_reference:
        return None

    # Only AT_EXPIRY is routed to DCS Elite (AT_NOEXPIRY stays manual,
    # per admin decision — DCS Elite sells no non-expiry AT bundles).
    if order.network != 'AT_EXPIRY':
        return None

    from orders.at import is_at_enabled, send_to_at
    if is_at_enabled():
        return send_to_at(recipient, gb_size, order_id)

    return None
