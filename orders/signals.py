import hmac
import hashlib
import json
import threading
import requests
from django.utils import timezone
from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
from .models import Order

# Shared by both the transition signal (pre_save) and the born-into-waiting
# signal (post_save) below, so the two can never silently drift apart.
ELIGIBLE_REFUND_SOURCES = {
    Order.Source.AGENT_SINGLE, Order.Source.AGENT_BULK,
    Order.Source.AGENT_EXCEL, Order.Source.API,
}


def _send_webhook(url, payload, secret):
    def run():
        try:
            body      = json.dumps(payload)
            signature = hmac.new(
                secret.encode(),
                body.encode(),
                hashlib.sha256,
            ).hexdigest()
            requests.post(
                url,
                data    = body,
                headers = {
                    'Content-Type'       : 'application/json',
                    'X-GigMax-Signature' : signature,
                },
                timeout = 10,
            )
            print(f'[WEBHOOK] Sent to {url}')
        except Exception as e:
            print(f'[WEBHOOK] Failed: {e}')
    threading.Thread(target=run, daemon=False).start()


@receiver(post_save, sender=Order)
def on_order_created_into_waiting(sender, instance, created, **kwargs):
    """
    Catches orders that are born directly at status=waiting (dispatch failed
    at the moment of placement, never passed through processing/delivered).
    The pre_save signal above can't see these, since there's no prior row to
    compare against. This mirrors that signal's crediting logic exactly, but
    fires on creation instead of on a status transition.

    Uses a direct .update() (not instance.save()) to avoid re-triggering
    post_save recursively.
    """
    # Auto-refund disabled: orders born at 'waiting' are NOT credited or
    # converted to refunded. Refunds happen only when an admin sets REFUNDED.
    return
    if not created:
        return
    if instance.status != Order.Status.WAITING:
        return
    if instance.refund_credited:
        return
    if instance.source not in ELIGIBLE_REFUND_SOURCES:
        return
    if not instance.placed_by_id:
        return

    from .utils import calculate_owed_amount, _apply_refund_credit
    try:
        amount = calculate_owed_amount(instance)
        if amount > 0:
            _apply_refund_credit(instance.placed_by, amount, instance.order_id, instance.source)
        Order.objects.filter(pk=instance.pk).update(
            status=Order.Status.REFUNDED,
            refund_credited=True,
            refund_requested=True,
            refund_paid=True,
            refund_paid_at=timezone.now(),
        )
    except Exception as e:
        print(f'[AUTO REFUND - born waiting] Failed for {instance.order_id}: {e}')


@receiver(pre_save, sender=Order)
def on_order_status_change(sender, instance, **kwargs):
    if not instance.pk:
        return
    try:
        old = Order.objects.get(pk=instance.pk)
    except Order.DoesNotExist:
        return

    # -- AUTO REFUND: agent-funded order fails, reverse wallet/debt --------
    # Fires on ANY transition into WAITING or REFUNDED, from any prior
    # status, as long as it hasn't been credited yet. Broadened from the
    # old PROCESSING/DELIVERED-only check because real orders can land on
    # WAITING/REFUNDED straight from PENDING too (e.g. webhook cancellations).
    #
    # RACE GUARD: two near-simultaneous .save() calls on the same order
    # (e.g. a webhook retry landing alongside a manual status change) could
    # both read the same stale `old.status` before either commits, and both
    # proceed to credit the wallet. select_for_update() inside an atomic
    # block forces the second save to wait for the first to fully commit
    # (including refund_credited=True) before it re-reads `old` — closing
    # that window. This re-fetch happens *inside* the lock, so the second
    # caller sees the already-updated refund_credited flag and skips.
    # NOTE: KUNTANII_UNVERIFIED deliberately does NOT trigger a credit. Orders
    # parked there are held for manual review. They are credited only when an
    # admin moves them to REFUNDED (old.status=unverified -> REFUNDED).
    # Only an explicit move to REFUNDED (admin action) credits the agent.
    # WAITING and KUNTANII_UNVERIFIED are held with no refund.
    if (instance.status == Order.Status.REFUNDED
            and old.status != Order.Status.REFUNDED
            and not old.refund_credited
            and instance.source in ELIGIBLE_REFUND_SOURCES
            and instance.placed_by_id):
        from django.db import transaction
        from .utils import calculate_owed_amount, _apply_refund_credit
        try:
            with transaction.atomic():
                locked_old = Order.objects.select_for_update().get(pk=instance.pk)
                if locked_old.refund_credited or locked_old.status == Order.Status.REFUNDED:
                    # Another concurrent save already credited this order
                    # while we were waiting for the lock — skip entirely.
                    instance.status           = locked_old.status
                    instance.refund_credited  = locked_old.refund_credited
                    instance.refund_requested = locked_old.refund_requested
                    instance.refund_paid      = locked_old.refund_paid
                    instance.refund_paid_at   = locked_old.refund_paid_at
                else:
                    amount = calculate_owed_amount(instance)
                    if amount > 0:
                        _apply_refund_credit(instance.placed_by, amount, instance.order_id, instance.source)
                    instance.status           = Order.Status.REFUNDED
                    instance.refund_credited  = True
                    instance.refund_requested = True
                    instance.refund_paid      = True
                    instance.refund_paid_at   = timezone.now()
        except Exception as e:
            import traceback
            print(f'[AUTO REFUND] Failed for {instance.order_id}: {e}')
            traceback.print_exc()
    # ------------------------------------------------------------------------

    if old.payment_status != 'paid' and instance.payment_status == 'paid':
        if instance.gb_package_id:
            from bundles.models import BundlePackage
            from decimal import Decimal
            try:
                pkg    = BundlePackage.objects.get(pk=instance.gb_package_id)
                margin = Decimal(str(instance.price_paid)) - Decimal(str(pkg.base_price))
                if margin > 0:
                    from admin_panel.models import AdminEarning
                    AdminEarning.objects.create(
                        order  = instance,
                        amount = margin,
                        note   = f'Margin on {instance.order_id}',
                    )
            except BundlePackage.DoesNotExist:
                pass

    NOTIFY_STATUSES = {'delivered', 'waiting', 'refunded', 'failed', 'processing'}
    if old.status != instance.status and instance.status in NOTIFY_STATUSES:
        agent = instance.placed_by
        if not agent:
            return
        if instance.source != Order.Source.API:
            return
        callback_url = getattr(agent, 'callback_url', None)
        if not callback_url:
            return
        secret  = agent.api_token or ''
        shown_status = 'unverified' if instance.status == 'kuntanii_unverified' else instance.status
        payload = {
            'event'    : f'order.{shown_status}',
            'order_id' : instance.order_id,
            'status'   : shown_status,
            'message'  : 'Order rejected. Your account has been credited.' if (shown_status == 'refunded' and instance.refund_credited) else None,
            'recipient': instance.recipient,
            'network'  : instance.network,
            'gb_size'  : str(instance.gb_package.gb_size) if instance.gb_package else None,
            'price'    : str(instance.price_paid),
            'timestamp': timezone.now().isoformat(),
        }
        _send_webhook(callback_url, payload, secret)


# --- Immediate auto-refund when an order is rejected (number not on Kuntanii list) ---
from django.db.models.signals import post_save as _ar_post_save
from django.dispatch import receiver as _ar_receiver
from django.db import transaction as _ar_tx


@_ar_receiver(_ar_post_save, sender=Order)
def auto_refund_on_unverified(sender, instance, created, **kwargs):
    if instance.status != 'kuntanii_unverified' or instance.refund_credited:
        return
    if instance.kuntanii_reference:
        return   # order exists at Kuntanii: handled by the 2-hour sweep instead
    oid = instance.order_id

    def _go():
        try:
            from orders.kuntanii_refunds import refund_if_new
            refund_if_new(oid)
        except Exception as e:
            print('[AutoRefund] failed for %s: %s' % (oid, e))

    _ar_tx.on_commit(_go)
