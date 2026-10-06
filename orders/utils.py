from decimal import Decimal
from django.utils import timezone
from .models import Order
from .notifications import notify_order_placed
import re
import threading

NETWORK_PREFIXES = {
    'MTN': ('024', '025', '053', '054', '055', '059'),
    'AT':  ('026', '027', '056', '057'),
    'TELECEL': ('020', '050'),
}


def number_matches_network(number, network):
    """
    Checks whether a recipient's phone number prefix actually belongs to
    the claimed network. Returns True if unknown/unmapped network (does
    not block), False only on a confirmed mismatch.
    """
    prefix = str(number).strip()[:3]
    network = (network or '').upper()

    if network.startswith('AT_'):
        return prefix in NETWORK_PREFIXES['AT']
    if network.startswith('MTN') or network in ('GATI', 'KUNTANII'):
        return prefix in NETWORK_PREFIXES['MTN']
    if network.startswith('TELECEL') or network.startswith('VODAFONE'):
        return prefix in NETWORK_PREFIXES['TELECEL']
    return True


def _dispatch_to_gati_async(order_id, recipient, gb_size):
    """Runs MTN dispatch (Kuntanii or Gati) in background thread — non-blocking.
    On failure: marks order as 'failed' so it doesn't sit stuck on pending.
    """
    def run():
        try:
            from orders.dispatch import dispatch_mtn_order
            dispatch_mtn_order(
                recipient=recipient,
                gb_size=gb_size,
                order_id=order_id,
            )
        except Exception as e:
            print(f'[MTN dispatch background] Error for {order_id}: {e}')
            # FIX: Don't leave order stuck on pending — mark it failed
            try:
                Order.objects.filter(
                    order_id=order_id,
                    status__in=['pending', 'processing'],  # only update if not already delivered
                ).update(
                    status=Order.Status.FAILED,
                    updated_at=timezone.now(),
                )
                print(f'[Gati background] Order {order_id} marked as failed after Gati error.')
            except Exception as inner_e:
                print(f'[Gati background] Could not mark {order_id} as failed: {inner_e}')

    t = threading.Thread(target=run, daemon=True)
    t.start()


def _dispatch_to_at_async(order_id, recipient, gb_size):
    """Runs AT (DCS Elite) dispatch in background thread — non-blocking.
    Mirrors _dispatch_to_gati_async(). Only marks order 'failed' on an
    unexpected exception — a graceful None return (no matching SKU,
    disabled, etc.) leaves the order 'pending' for manual processing,
    exactly like the MTN path.
    """
    def run():
        try:
            from orders.dispatch import dispatch_at_order
            dispatch_at_order(
                recipient=recipient,
                gb_size=gb_size,
                order_id=order_id,
            )
        except Exception as e:
            print(f'[AT dispatch background] Error for {order_id}: {e}')
            try:
                Order.objects.filter(
                    order_id=order_id,
                    status__in=['pending', 'processing'],
                ).update(
                    status=Order.Status.FAILED,
                    updated_at=timezone.now(),
                )
                print(f'[AT background] Order {order_id} marked as failed after DCS error.')
            except Exception as inner_e:
                print(f'[AT background] Could not mark {order_id} as failed: {inner_e}')
    t = threading.Thread(target=run, daemon=True)
    t.start()


def calculate_owed_amount(order):
    """
    Returns the correct amount the agent is owed for this order.

    - Orders NOT via a generated link (agent placed it themselves):
      price_paid IS the agent's cost — return it directly.
    - Orders via a generated link (a customer bought at retail through
      the agent's storefront): the agent is only owed their actual cost
      price, not the retail price the customer paid.
    """
    from decimal import Decimal

    if order.source == 'generated_link' and order.generated_link and order.gb_package:
        agent = order.generated_link.agent
        if agent:
            try:
                return Decimal(str(order.gb_package.get_price_for_user(agent)))
            except Exception:
                pass

    return Decimal(str(order.price_paid or 0))


def place_order(package, recipient, price, source, placed_by=None,
                generated_link=None, batch_id=None,
                payment_reference=None, mark_paid=False, via_api=False):
    """
    Central order placement function.
    MTN orders auto-route to Gati in background (non-blocking).
    Only dispatches to Gati if order is actually paid.
    """
    order = Order.objects.create(
        network           = package.network,
        recipient         = recipient,
        gb_package        = package,
        price_paid        = price,
        source            = source,
        placed_by         = placed_by,
        generated_link    = generated_link,
        batch_id          = batch_id,
        payment_reference = payment_reference or '',
        payment_status    = 'paid' if mark_paid else 'pending',
        paid_at           = timezone.now() if mark_paid else None,
        via_api           = via_api,
    )

    # Route MTN orders through the central dispatch router (Kuntanii-first,
    # falls back to Gati if Kuntanii is off, otherwise leaves it untouched
    # for manual processing). Only dispatch if the order is actually paid.
    # Previously this called _dispatch_to_gati_async() directly, which meant
    # bulk orders (and anything else going through place_order()) never
    # reached Kuntanii regardless of the toggle state — found and fixed
    # 2026-09-16.
    if order.is_mtn and mark_paid:
        from orders.dispatch import dispatch_mtn_order
        dispatch_mtn_order(
            recipient = recipient,
            gb_size   = float(package.gb_size),
            order_id  = order.order_id,
        )

    # Only dispatch AT_EXPIRY orders to DCS Elite if actually paid AND the
    # recipient's number actually matches the AirtelTigo prefix range.
    # A mismatch (e.g. an MTN number bought under an AT bundle) is left
    # pending for manual review instead of burning wallet balance on a
    # doomed DCS Elite dispatch.
    if order.network == 'AT_EXPIRY' and mark_paid:
        if number_matches_network(recipient, order.network):
            _dispatch_to_at_async(
                order_id  = order.order_id,
                recipient = recipient,
                gb_size   = float(package.gb_size),
            )
        else:
            print(f'[AT dispatch] Skipped {order.order_id} — recipient {recipient} '
                  f'does not match AirtelTigo prefix range. Left pending for manual review.')

    # Notify agent/customer
    try:
        notify_order_placed(order)
    except Exception:
        pass

    return order


def _apply_payment(user, total):
    """
    Payment logic:
    ─────────────
    Debt ON  → use wallet first; shortfall added to debt. Never Paystack.
    Debt OFF → wallet must cover everything; raise ValueError if short → Paystack.
    """
    total  = Decimal(str(total))
    wallet = Decimal(str(user.wallet_balance or 0))
    debt   = Decimal(str(user.debt_balance   or 0))

    if user.debt_page_access:
        wallet_used = min(wallet, total)
        debt_added  = total - wallet_used

        user.wallet_balance = wallet - wallet_used
        user.debt_balance   = debt + debt_added

        user.save(update_fields=['wallet_balance', 'debt_balance'])
        return wallet_used, debt_added

    else:
        if wallet < total:
            raise ValueError("Insufficient wallet balance")

        user.wallet_balance = wallet - total
        user.save(update_fields=['wallet_balance'])
        return total, Decimal('0')


def validate_ghana_number(number):
    """Validates Ghana mobile number format: 10 digits starting with 0."""
    pattern = r'^0[235679]\d{8}$'
    return bool(re.match(pattern, str(number).strip()))


def parse_bulk_lines(raw_input):
    """
    Parses bulk order input: 'number GB' per line.
    Rejects duplicates entirely instead of passing them through.
    """
    lines      = raw_input.strip().split('\n')
    valid      = []
    invalid    = []
    seen_pairs = {}
    seen_numbers = set()

    for line in lines:
        line = line.strip()
        if not line:
            continue

        parts = line.split()
        if len(parts) < 2:
            invalid.append({'line': line, 'reason': 'Wrong format — use: 0XXXXXXXXX GB'})
            continue

        number = parts[0].strip()
        gb     = parts[1].strip()

        if not validate_ghana_number(number):
            invalid.append({'line': line, 'reason': 'Invalid Ghana number'})
            continue

        pair_key = (number, gb)
        if pair_key in seen_pairs:
            invalid.append({'line': line, 'reason': 'Duplicate — exact same number and GB already in list'})
            continue

        seen_pairs[pair_key] = True
        is_same_number = number in seen_numbers
        seen_numbers.add(number)

        valid.append({
            'number'      : number,
            'gb'          : gb,
            'is_duplicate': is_same_number,
        })

    return valid, invalid

def _apply_refund_credit(user, amount, order_id, order_source=''):
    """
    Reverses a wallet/debt debit when an agent-funded order fails.
    Mirrors _apply_payment() in reverse:
    Debt outstanding -> reduce debt first, any remainder credited to wallet.
    No debt -> credit wallet directly.
    Locked + atomic to prevent race conditions with concurrent payments/topups.
    """
    from django.db import transaction
    from django.contrib.auth import get_user_model

    User = get_user_model()

    with transaction.atomic():
        locked_user   = User.objects.select_for_update().get(pk=user.pk)
        amount        = Decimal(str(amount))
        debt_before   = Decimal(str(locked_user.debt_balance or 0))
        wallet_before = Decimal(str(locked_user.wallet_balance or 0))
        debt          = debt_before

        debt_reduced    = Decimal('0')
        wallet_credited = Decimal('0')

        if debt > 0:
            if amount >= debt:
                remainder                  = amount - debt
                debt_reduced               = debt
                wallet_credited            = remainder
                locked_user.debt_balance   = Decimal('0')
                locked_user.wallet_balance = Decimal(str(locked_user.wallet_balance or 0)) + remainder
            else:
                debt_reduced               = amount
                locked_user.debt_balance   = debt - amount
        else:
            wallet_credited            = amount
            locked_user.wallet_balance = Decimal(str(locked_user.wallet_balance or 0)) + amount

        locked_user.save(update_fields=['wallet_balance', 'debt_balance'])

        debt_after   = locked_user.debt_balance
        wallet_after = locked_user.wallet_balance

        from wallet.models import Wallet, WalletTransaction

        if debt_reduced > 0 and wallet_credited > 0:
            desc = f'Auto-reversed: order {order_id} — GHS {debt_reduced} reduced from debt, GHS {wallet_credited} credited to wallet'
        elif debt_reduced > 0:
            desc = f'Auto-reversed: order {order_id} — GHS {debt_reduced} reduced from debt'
        else:
            desc = f'Auto-reversed: order {order_id} — GHS {wallet_credited} credited to wallet'

        wallet, _ = Wallet.objects.get_or_create(user=locked_user)
        WalletTransaction.objects.get_or_create(
            reference = f'AUTOREFUND-{order_id}',
            defaults  = dict(
                wallet           = wallet,
                amount           = amount,
                transaction_type = 'credit',
                status           = 'completed',
                description      = desc,
                debt_before      = debt_before,
                debt_after       = debt_after,
                wallet_before    = wallet_before,
                wallet_after     = wallet_after,
                order_source     = order_source,
            )
        )
        return locked_user.wallet_balance, locked_user.debt_balance


def get_agent_refund_detail(user, date, date_to=None):
    """
    Returns (items, total_amount, total_debt_reduced, total_wallet_credited, count)
    for all AUTOREFUND- WalletTransactions belonging to `user` on `date`.
    Shared by both the agent dashboard and the admin drill-down view so the
    two can never silently drift apart.
    """
    from decimal import Decimal
    from wallet.models import WalletTransaction

    _SOURCE_LABELS = {
        'api'          : 'API',
        'agent_single' : 'Single',
        'agent_bulk'   : 'Bulk',
        'agent_excel'  : 'Excel',
    }

    txns = WalletTransaction.objects.filter(
        wallet__user=user,
        reference__startswith='AUTOREFUND-',
        created_at__date__gte=date,
        created_at__date__lte=(date_to or date),
    ).order_by('-created_at')

    items                  = []
    total_amount            = Decimal('0')
    total_debt_reduced      = Decimal('0')
    total_wallet_credited   = Decimal('0')

    for txn in txns:
        debt_reduced    = None
        wallet_credited = None
        if txn.debt_before is not None and txn.debt_after is not None:
            debt_reduced = txn.debt_before - txn.debt_after
        if txn.wallet_before is not None and txn.wallet_after is not None:
            wallet_credited = txn.wallet_after - txn.wallet_before

        total_amount += txn.amount
        if debt_reduced:
            total_debt_reduced += debt_reduced
        if wallet_credited:
            total_wallet_credited += wallet_credited

        items.append({
            'time'           : txn.created_at,
            'amount'         : txn.amount,
            'source_label'   : _SOURCE_LABELS.get(txn.order_source, txn.order_source or '—'),
            'debt_before'    : txn.debt_before,
            'debt_after'     : txn.debt_after,
            'wallet_before'  : txn.wallet_before,
            'wallet_after'   : txn.wallet_after,
            'debt_reduced'   : debt_reduced,
            'wallet_credited': wallet_credited,
            'description'    : txn.description,
        })

    return items, total_amount, total_debt_reduced, total_wallet_credited, txns.count()


def get_paystack_fee(order):
    """
    Returns the Paystack fee for an order, in GHS. Fetches from Paystack
    once and caches on the order - never re-fetches once known.
    Returns None if unavailable (no payment_reference, or Paystack call
    fails) rather than raising, so the refund tab never breaks on one
    bad row.
    """
    if order.paystack_fee is not None:
        return order.paystack_fee

    if not order.payment_reference:
        return None

    try:
        from wallet.gateway import verify_transaction
        result = verify_transaction(order.payment_reference)
        fees_kobo = result.get('data', {}).get('fees')
        if fees_kobo is None:
            return None
        fee = Decimal(str(fees_kobo)) / 100
        Order.objects.filter(pk=order.pk).update(paystack_fee=fee)
        order.paystack_fee = fee
        return fee
    except Exception as e:
        print(f'[get_paystack_fee] Failed for {order.order_id}: {e}')
        return None


def get_refund_history_queryset(agent, request):
    """
    Returns a filtered, agent-scoped queryset of refunded orders.
    Shared by both the agent's own history page and the admin per-agent view.
    Supports filtering by date range, network, source, and GB size via GET params.
    """
    from django.db.models import Q
    from orders.models import GeneratedLink

    agent_links = GeneratedLink.objects.filter(agent=agent)
    qs = Order.objects.filter(
        Q(placed_by=agent) | Q(generated_link__in=agent_links),
        status='refunded',
    ).select_related('gb_package', 'generated_link').distinct().order_by('-updated_at')

    date_from = request.GET.get('date_from')
    date_to   = request.GET.get('date_to')
    network   = request.GET.get('network')
    source    = request.GET.get('source')
    gb_size   = request.GET.get('gb_size')

    if date_from:
        qs = qs.filter(updated_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(updated_at__date__lte=date_to)
    if network:
        qs = qs.filter(network=network)
    if source:
        qs = qs.filter(source=source)
    if gb_size:
        qs = qs.filter(gb_package__gb_size=gb_size)

    return qs
