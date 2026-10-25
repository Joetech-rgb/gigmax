"""
One place that decides and applies Kuntanii-related refunds to agents.
Agent sources only. Money moves only through orders.utils._apply_refund_credit
(debt first, remainder to wallet). On credit the order becomes 'refunded'.
Duplicate protection: row lock + refund_credited flag + AUTOREFUND-<id> check.
Only orders created on/after AUTOREFUND_FROM are handled automatically.
"""
from datetime import datetime, timedelta, timezone as dt_tz
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from orders.models import Order

UNVERIFIED = 'kuntanii_unverified'
CLOCK_REASONS = ('status_unreadable', 'unrecognized_status', 'Unrecognized Kuntanii status')
AUTOREFUND_FROM = datetime.fromisoformat('2026-10-10T08:11:27').replace(tzinfo=dt_tz.utc)


def auto_refund_enabled():
    # Admin switch (Auto Refund). Fails open so a missing table never stops refunds.
    try:
        from admin_panel.models import AutoRefundSettings
        return bool(AutoRefundSettings.get().is_enabled)
    except Exception:
        return True


def _eligible_sources():
    from orders.signals import ELIGIBLE_REFUND_SOURCES
    return {getattr(s, 'value', s) for s in ELIGIBLE_REFUND_SOURCES}


def refund_after():
    return timedelta(hours=getattr(settings, 'KUNTANII_UNREADABLE_REFUND_HOURS', 2))


def refund_kuntanii_order(order_id, reason=None):
    """Credit the agent for one order, exactly once.
    Returns 'credited', 'already' or 'skipped'."""
    from orders.utils import calculate_owed_amount, _apply_refund_credit
    from wallet.models import WalletTransaction

    with transaction.atomic():
        o = Order.objects.select_for_update().filter(order_id=order_id).first()
        if o is None:
            return 'skipped'
        if o.refund_credited:
            return 'already'
        if (o.source not in _eligible_sources() or not o.placed_by_id
                or o.payment_status != 'paid'
                or o.status != UNVERIFIED or o.kuntanii_reference):
            return 'skipped'
        if WalletTransaction.objects.filter(reference='AUTOREFUND-%s' % o.order_id).exists():
            Order.objects.filter(pk=o.pk).update(refund_credited=True)
            return 'already'

        amount = calculate_owed_amount(o)
        if amount > 0:
            _apply_refund_credit(o.placed_by, amount, o.order_id, o.source)

        now = timezone.now()
        # Flag first, so the status signal sees refund_credited=True and never credits again.
        Order.objects.filter(pk=o.pk).update(
            refund_credited=True,
            kuntanii_unverified_reason=reason or o.kuntanii_unverified_reason or 'refunded_automatically',
            refund_paid=True,
            refund_paid_at=now,
            kuntanii_unverified_at=o.kuntanii_unverified_at or now,
            updated_at=now,
        )
        o.refresh_from_db()
        o.status = 'refunded'
        o.save()   # signal: skips the credit, sends the order.refunded webhook
        return 'credited'


def refund_if_new(order_id, reason=None):
    """Immediate-refund entry point. Ignores orders older than AUTOREFUND_FROM."""
    if not auto_refund_enabled():
        return 'held'
    o = Order.objects.filter(order_id=order_id).only('created_at').first()
    if o is None or o.created_at < AUTOREFUND_FROM:
        return 'skipped'
    return refund_kuntanii_order(order_id, reason)


def refundable_orders(now=None, include_old=False):
    now = now or timezone.now()
    cutoff = now - refund_after()
    no_ref = Q(kuntanii_reference__isnull=True) | Q(kuntanii_reference='')
    clock = Q()
    for prefix in CLOCK_REASONS:
        clock |= Q(kuntanii_unverified_reason__startswith=prefix)
    immediate = Q(status=UNVERIFIED) & no_ref
    delayed = (Q(status__in=[UNVERIFIED, 'processing']) & ~no_ref & clock
               & Q(kuntanii_unverified_at__lte=cutoff))
    qs = (Order.objects
          .filter(refund_credited=False, placed_by__isnull=False,
                  payment_status='paid', source__in=_eligible_sources())
          .filter(immediate))
    if not include_old:
        qs = qs.filter(created_at__gte=AUTOREFUND_FROM)
    return qs.order_by('placed_by_id', 'created_at')


def run_sweep(apply=False, agent_phone=None, limit=None, out=print, include_old=False):
    from orders.utils import calculate_owed_amount

    if apply and not auto_refund_enabled():
        out('Auto Refund is OFF in the admin panel. Nothing was credited.')
        return None

    qs = refundable_orders(include_old=include_old).select_related('placed_by')
    if agent_phone:
        qs = qs.filter(placed_by__phone_number=agent_phone)
    orders = list(qs[:limit] if limit else qs)

    plan = {}
    for o in orders:
        u = o.placed_by
        amt = calculate_owed_amount(o)
        p = plan.setdefault(u.pk, {'user': u, 'n': 0, 'total': Decimal('0'),
                                   'debt': Decimal(str(u.debt_balance or 0)),
                                   'debt_cut': Decimal('0'), 'wallet_add': Decimal('0')})
        cut = min(p['debt'], amt) if p['debt'] > 0 else Decimal('0')
        p['debt'] -= cut
        p['debt_cut'] += cut
        p['wallet_add'] += amt - cut
        p['n'] += 1
        p['total'] += amt

    out('%s: %d orders' % ('APPLYING' if apply else 'DRY RUN', len(orders)))
    tot = tcut = twal = Decimal('0')
    for p in sorted(plan.values(), key=lambda x: -x['total']):
        u = p['user']
        out('%s %s | orders %d | GHS %s | debt reduced %s | wallet credited %s' % (
            u.phone_number, u.full_name, p['n'], p['total'], p['debt_cut'], p['wallet_add']))
        tot += p['total']; tcut += p['debt_cut']; twal += p['wallet_add']
    out('TOTAL GHS %s | debt reduced %s | wallet credited %s' % (tot, tcut, twal))

    if not apply:
        out('Nothing changed. Re-run with apply=True to credit.')
        return None

    results = {'credited': 0, 'already': 0, 'skipped': 0, 'error': 0}
    for o in orders:
        try:
            r = refund_kuntanii_order(o.order_id, o.kuntanii_unverified_reason)
        except Exception as e:
            r = 'error'
            out('ERROR %s: %s' % (o.order_id, e))
        results[r] += 1
    out('Done: %s' % results)
    return results
