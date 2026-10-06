import uuid
import requests
from django.utils import timezone
from datetime import timedelta
from django.db import connection
from bundles.models import BundlePackage
from orders.models import Order
import threading
from decimal import Decimal
from django.conf import settings
# FIX: increased from 3 minutes to 30 minutes — Paystack can take time
PAYMENT_EXPIRY_MINUTES = 30

# Single source of truth — defined in orders/utils.py
from orders.utils import _dispatch_to_gati_async


def expire_stale_payments():
    """
    Mark any pending_payment orders whose payment_expires_at has passed as failed.
    """
    now     = timezone.now()
    expired = Order.objects.filter(
        status                 = 'pending_payment',
        payment_status         = 'pending',
        payment_expires_at__lt = now,
    )
    count = expired.update(
        status         = 'failed',
        payment_status = 'failed',
    )
    if count:
        print(f"[expire_stale_payments] Expired {count} stale payment(s).")
    return count


def instant_deliver(phone, package_id, amount=None):
    package = BundlePackage.objects.get(id=package_id)
    price   = amount or package.customer_price
    order   = Order.objects.create(
        order_id   = f"INSTANT_{uuid.uuid4().hex[:8].upper()}",
        recipient  = phone,
        gb_package = package,
        price_paid = price,
        network    = package.network,
        status     = 'delivered',
        source     = 'instant_deliver',
    )
    payment_ref = f"PAY_{uuid.uuid4().hex[:12].upper()}"
    now = timezone.now()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE orders_order
            SET payment_reference = %s,
                payment_status = %s,
                paid_at = %s,
                is_guest = %s
            WHERE id = %s
            """,
            [payment_ref, 'paid', now, True, order.id]
        )

    try:
        from orders.notifications import notify_order_delivered
        order.refresh_from_db()
        notify_order_delivered(order)
    except Exception:
        pass

    print(f"Delivered {package.gb_size}GB to {phone}")
    return order


def guest_order(phone, package_id, price=None):
    package    = BundlePackage.objects.get(id=package_id)
    expires_at = timezone.now() + timedelta(minutes=PAYMENT_EXPIRY_MINUTES)

    order = Order.objects.create(
        order_id           = f"GUEST_{uuid.uuid4().hex[:8].upper()}",
        recipient          = phone,
        gb_package         = package,
        price_paid         = price if price is not None else package.customer_price,
        network            = package.network,
        status             = 'pending_payment',
        source             = 'guest_checkout',
        payment_expires_at = expires_at,
    )

    payment_ref = f"PAY_{uuid.uuid4().hex[:12].upper()}"
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE orders_order
            SET payment_reference = %s,
                payment_status = %s,
                is_guest = %s
            WHERE id = %s
            """,
            [payment_ref, 'pending', True, order.id]
        )

    print(f"Guest order: {order.order_id} | Ref: {payment_ref} | Expires: {expires_at}")
    return order


def pay_and_deliver(order_id):
    """
    Confirm payment and trigger delivery for an order.
    """
    now   = timezone.now()
    order = Order.objects.get(order_id=order_id)

    # ── IDEMPOTENCY GUARD ────────────────────────────────────────────────────
    if order.status in ('delivered', 'cancelled'):
        print(f"[pay_and_deliver] SKIP {order_id} — terminal status '{order.status}'")
        return order

    if order.payment_status == 'paid' and order.status not in ('pending', 'processing', 'pending_payment'):
        print(f"[pay_and_deliver] SKIP {order_id} — already paid and status={order.status}")
        return order
    # ── END GUARD ────────────────────────────────────────────────────────────

    if order.is_mtn:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE orders_order
                SET payment_status = %s,
                    paid_at = %s,
                    status = %s
                WHERE order_id = %s
                  AND status NOT IN ('delivered', 'cancelled')
                """,
                ['paid', now, 'pending', order_id]
            )

        order.refresh_from_db()

        if not order.gati_order_id:
            _dispatch_to_gati_async(
                order_id  = order.order_id,
                recipient = order.recipient,
                gb_size   = float(order.gb_package.gb_size),
            )

        _credit_link_profit_local(order)

    else:
        # Non-MTN (Telecel, AT) → payment confirmed, admin processes manually
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE orders_order
                SET payment_status = %s,
                    paid_at = %s,
                    status = %s
                WHERE order_id = %s
                  AND status NOT IN ('delivered', 'cancelled')
                """,
                ['paid', now, 'pending', order_id]
            )

        order.refresh_from_db()

        try:
            from orders.notifications import notify_payment_confirmed
            notify_payment_confirmed(order)
        except Exception:
            pass

        _credit_link_profit_local(order)

    print(f"Payment confirmed: {order.order_id} to {order.recipient} — status: {order.status}")
    return order

def recover_orphan_payments(hours_back=48, per_page=100):
    import requests as _requests
    from orders.models import Order

    recovered = 0
    page      = 1
    headers   = {'Authorization': f'Bearer {settings.PAYSTACK_SECRET_KEY}'}
    cutoff    = timezone.now() - timezone.timedelta(hours=hours_back)
    skip      = ('SKP-', 'WLP-', 'WD-', 'ADMWD-')

    while True:
        try:
            resp = _requests.get(
                'https://api.paystack.co/transaction',
                headers=headers,
                params={'status': 'success', 'perPage': per_page, 'page': page},
                timeout=30,
            )
        except Exception as e:
            print(f'[recover_orphan_payments] Paystack request failed: {e}')
            break

        if resp.status_code != 200:
            break

        payload = resp.json()
        txns    = payload.get('data', [])
        meta    = payload.get('meta', {})

        if not txns:
            break

        for tx in txns:
            # stop when we go past the lookback window
            paid_at_str = tx.get('paid_at') or tx.get('created_at', '')
            if paid_at_str:
                try:
                    from django.utils.dateparse import parse_datetime
                    paid_at = parse_datetime(paid_at_str)
                    if paid_at and timezone.is_naive(paid_at):
                        paid_at = timezone.make_aware(paid_at)
                    if paid_at and paid_at < cutoff:
                        return recovered
                except Exception:
                    pass

            ref = (tx.get('reference') or '').strip()
            if not ref:
                continue

            # skip wallet topups, withdrawals, admin payouts
            if any(ref.startswith(p) for p in skip):
                continue

            # order already exists — skip
            if Order.objects.filter(payment_reference=ref).exists():
                continue

            # orphan found — create and deliver
            print(f'[recover_orphan_payments] ORPHAN: ref={ref}')
            try:
                from wallet.views import _create_order_from_metadata
                _create_order_from_metadata(tx, ref)
                recovered += 1
                print(f'[recover_orphan_payments] Recovered: {ref}')
            except Exception as e:
                print(f'[recover_orphan_payments] Failed {ref}: {e}')

        current_page = meta.get('page', page)
        total_pages  = meta.get('pageCount', 1)
        if current_page >= total_pages:
            break
        page += 1

    return recovered

def _credit_link_profit_local(order):
    """
    Credits link profit for a single order object.
    Called from pay_and_deliver() inside delivery_utils.py.

    FORMULA: retail_price - get_price_for_user(agent)
    This respects each agent's actual cost price based on their tier or custom role.

    IDEMPOTENCY: PROFIT-{order_id} — matches _credit_link_profit in wallet/views.py
    so whichever path runs first wins, the second is a no-op.

    RACE SAFETY: IntegrityError on concurrent insert is caught explicitly.
    Balance update only runs when this call created the WalletTransaction row.
    """
    try:
        if not order.generated_link:
            return
        if not order.generated_link.agent:
            return
        if not order.gb_package:
            return

        link       = order.generated_link
        agent      = link.agent
        profit_ref = f'PROFIT-{order.order_id}'

        # Get the retail price the customer paid through the link
        link_price_obj = link.prices.filter(bundle=order.gb_package).first()
        if not link_price_obj:
            print(f'[_credit_link_profit_local] No link price for order {order.order_id} — skipping')
            return

        # Agent's actual cost price based on their tier or custom role
        agent_cost = order.gb_package.get_price_for_user(agent)
        profit     = float(link_price_obj.retail_price) - float(agent_cost)

        if profit <= 0:
            return

        profit_decimal = Decimal(str(round(profit, 2)))

        from django.contrib.auth import get_user_model
        from django.db.models import F
        from django.db import IntegrityError
        from wallet.models import Wallet, WalletTransaction

        User      = get_user_model()
        wallet, _ = Wallet.objects.get_or_create(user=agent)

        try:
            _, created = WalletTransaction.objects.get_or_create(
                reference = profit_ref,
                defaults  = dict(
                    wallet           = wallet,
                    amount           = profit_decimal,
                    transaction_type = 'credit',
                    status           = 'completed',
                    description      = f'Link profit — {link.label or order.order_id}',
                )
            )
        except IntegrityError:
            # Concurrent insert from _credit_link_profit (webhook/verify_view) won the race.
            # Balance update must NOT run here.
            print(f'[_credit_link_profit_local] IntegrityError race on {order.order_id} — skipping')
            return

        if not created:
            print(f'[_credit_link_profit_local] SKIP {order.order_id} — already credited')
            return

        # Only reached when this call created the row
        User.objects.filter(pk=agent.pk).update(
            link_profit_balance      = F('link_profit_balance')      + profit_decimal,
            total_link_profit_earned = F('total_link_profit_earned') + profit_decimal,
        )

        try:
            from orders.notifications import notify_link_profit
            notify_link_profit(agent, profit, link.label)
        except Exception:
            pass

    except Exception as e:
        print(f'[_credit_link_profit_local] Error for order {order.order_id}: {e}')