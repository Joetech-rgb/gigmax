from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from accounts.decorators import agent_required, admin_required
from .gateway import initialize_transaction, verify_transaction, create_momo_recipient, initiate_transfer
from .models import Wallet, WalletTransaction, WithdrawalRequest
from .forms import WithdrawalForm
import uuid
import json
import hmac
import hashlib
import threading
import requests
from decimal import Decimal
from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from orders.notifications import notify_wallet_topup
from django.contrib.auth.decorators import login_required

SERVICE_CHARGE = Decimal('1.00')

NETWORK_CODES = {
    'MTN'        : 'MTN',
    'TELECEL'    : 'VOD',
    'AIRTELTIGO' : 'ATL',
    'AT'         : 'ATL',
}


def _dispatch_mtn_to_gati_async(order):
    def run():
        try:
            from orders.dispatch import dispatch_mtn_order
            if order.network == 'MTN' and not order.gati_order_id and not order.kuntanii_reference and order.gb_package:
                reference = dispatch_mtn_order(
                    recipient = order.recipient,
                    gb_size   = float(order.gb_package.gb_size),
                    order_id  = order.order_id,
                )
                if reference:
                    from orders.models import Order
                    Order.objects.filter(order_id=order.order_id).update(
                        via_api = True,
                    )
        except Exception as e:
            print(f'[MTN async dispatch] Error for {order.order_id}: {e}')

    t = threading.Thread(target=run, daemon=False)
    t.start()


def _confirm_order_payment(reference):
    from orders.models import Order

    updated = Order.objects.filter(
        payment_reference  = reference,
        payment_status__in = ['pending', '', 'failed', 'pending_payment'],
    ).update(
        payment_status = 'paid',
        paid_at        = timezone.now(),
        status         = Order.Status.PENDING,
    )

    if updated:
        for order in Order.objects.filter(payment_reference=reference, network='MTN'):
            _dispatch_mtn_to_gati_async(order)

    return updated


def _credit_link_profit(reference):
    """
    Credits link profit to agent's link_profit_balance and total_link_profit_earned.

    FORMULA: retail_price - get_price_for_user(agent)
    This respects each agent's actual cost price based on their tier or custom role.

    IDEMPOTENCY: PROFIT-{order_id} — one row per order, no matter how many times called.

    RACE SAFETY: IntegrityError on concurrent insert is caught explicitly.
    Balance update only runs when this call created the WalletTransaction row.
    """
    try:
        from orders.models import Order, GeneratedLinkPrice
        from orders.notifications import notify_link_profit
        from django.contrib.auth import get_user_model
        from django.db import IntegrityError

        User = get_user_model()

        link_orders = Order.objects.filter(
            payment_reference = reference,
            source            = 'generated_link',
        ).select_related('generated_link__agent', 'generated_link', 'gb_package')

        for lo in link_orders:
            if not (lo.generated_link and lo.generated_link.agent):
                continue
            if not lo.gb_package:
                continue

            agent = lo.generated_link.agent

            # Get the retail price the customer paid through the link
            lp = GeneratedLinkPrice.objects.filter(
                link   = lo.generated_link,
                bundle = lo.gb_package,
            ).first()
            if not lp:
                print(f'[_credit_link_profit] No GeneratedLinkPrice for order {lo.order_id} — skipping')
                continue

            # Agent's actual cost price based on their tier or custom role
            agent_cost = lo.gb_package.get_price_for_user(agent)
            profit     = float(lp.retail_price) - float(agent_cost)

            if profit <= 0:
                continue

            profit_decimal = Decimal(str(round(profit, 2)))
            profit_ref     = f'PROFIT-{lo.order_id}'

            wallet, _ = Wallet.objects.get_or_create(user=agent)

            try:
                _, created = WalletTransaction.objects.get_or_create(
                    reference = profit_ref,
                    defaults  = dict(
                        wallet           = wallet,
                        amount           = profit_decimal,
                        transaction_type = 'credit',
                        status           = 'completed',
                        description      = f'Link profit — {lo.generated_link.label or lo.order_id}',
                    )
                )
            except IntegrityError:
                # Concurrent insert from webhook/verify_view won the race.
                # Balance update must NOT run here.
                print(f'[_credit_link_profit] IntegrityError race on {lo.order_id} — skipping')
                continue

            if not created:
                print(f'[_credit_link_profit] SKIP {lo.order_id} — already credited')
                continue

            # Only reached when this call created the row
            User.objects.filter(pk=agent.pk).update(
                link_profit_balance      = F('link_profit_balance')      + profit_decimal,
                total_link_profit_earned = F('total_link_profit_earned') + profit_decimal,
            )

            try:
                notify_link_profit(agent, profit, lo.generated_link.label)
            except Exception:
                pass

    except Exception as e:
        print(f'[_credit_link_profit] error for {reference}: {e}')

        
def _create_order_from_metadata(data, reference):
    try:
        from orders.models import Order as _Order
        from orders.delivery_utils import guest_order, pay_and_deliver

        with transaction.atomic():
            if _Order.objects.filter(payment_reference=reference).exists():
                existing = _Order.objects.select_for_update().get(
                    payment_reference=reference
                )
                if existing.payment_status != 'paid':
                    pay_and_deliver(existing.order_id)
                return

            meta       = data.get('metadata') or {}
            phone      = meta.get('phone', '').strip()
            package_id = meta.get('package_id', '').strip()
            price      = meta.get('price', '').strip()
            link_token = meta.get('link_token', '').strip()

            if not phone or not package_id or not price:
                print(f'[WEBHOOK] Missing metadata for {reference}: '
                      f'phone={phone} pkg={package_id} price={price}')
                return

            order = guest_order(phone, package_id, price=float(price))
            _Order.objects.filter(id=order.id).update(payment_reference=reference)

            if link_token:
                try:
                    from orders.models import GeneratedLink
                    link_obj = GeneratedLink.objects.filter(
                        token=link_token, is_active=True
                    ).first()
                    if link_obj:
                        _Order.objects.filter(id=order.id).update(
                            generated_link=link_obj,
                            source='generated_link',
                        )
                except Exception as e:
                    print(f'[WEBHOOK] Link attach error: {e}')

        order.refresh_from_db()
        pay_and_deliver(order.order_id)
        print(f'[WEBHOOK] Created and delivered {order.order_id} for {reference}')

    except Exception as e:
        print(f'[WEBHOOK] _create_order_from_metadata error for {reference}: {e}')


# ---------------------------------------------------------------------------
# Wallet views
# ---------------------------------------------------------------------------

@agent_required
def wallet_view(request):
    from django.contrib.auth import get_user_model
    User = get_user_model()
    user = User.objects.get(pk=request.user.pk)
    wallet, _ = Wallet.objects.get_or_create(user=user)
    transactions = WalletTransaction.objects.filter(wallet=wallet).order_by('-created_at')[:30]
    withdrawals  = WithdrawalRequest.objects.filter(wallet=wallet).order_by('-created_at')[:5]

    return render(request, 'agent/wallet.html', {
        'transactions'            : transactions,
        'withdrawals'             : withdrawals,
        'link_profit_balance'     : user.link_profit_balance,
        'total_link_profit_earned': user.total_link_profit_earned,
        'wallet_only_balance'     : user.wallet_only_balance,
        'admin_whatsapp'          : getattr(settings, 'ADMIN_WHATSAPP_NUMBER', ''),
    })


@agent_required
def load_wallet_view(request):
    """
    FIX: Gross up the amount by the Paystack fee % so the agent pays more
    and receives exactly what they typed. e.g. agent enters GHS 20 →
    we charge GHS 20.40 → verify_view deducts 2% (GHS 0.40) → net GHS 20.00.
    """
    if request.method == 'POST':
        amount = request.POST.get('amount')
        try:
            amount = float(amount)
            if amount <= 0:
                raise ValueError
        except (ValueError, TypeError):
            messages.error(request, 'Enter a valid amount.')
            return redirect('/agent/wallet/')

        # Apply fee at initiation so agent receives exact amount they typed
        try:
            from admin_panel.models import FeeConfiguration
            fee_config  = FeeConfiguration.get_current()
            fee_percent = Decimal(str(fee_config.paystack_fee_percent))
        except Exception:
            fee_percent = Decimal('2.00')

        amount_decimal = Decimal(str(amount))
        gross_amount   = float(
            (amount_decimal * (1 + fee_percent / 100)).quantize(Decimal('0.01'))
        )

        reference = f'SKP-{uuid.uuid4().hex[:10].upper()}'
        email     = f'{request.user.phone_number}@gigmax.com'

        try:
            result = initialize_transaction(
                email        = email,
                amount_ghc   = gross_amount,
                reference    = reference,
                callback_url = request.build_absolute_uri('/wallet/paystack/verify/'),
            )
        except requests.Timeout:
            messages.error(request, 'Payment gateway timed out. Please try again.')
            return redirect('/agent/wallet/')
        except Exception as e:
            messages.error(request, f'Payment error: {str(e)}')
            return redirect('/agent/wallet/')

        if result.get('status'):
            return redirect(result['data']['authorization_url'])
        else:
            messages.error(request, f"Paystack error: {result.get('message', 'Payment initialization failed.')}")
            return redirect('/agent/wallet/')

    return redirect('/agent/wallet/')


@login_required(login_url='/accounts/login/')
def paystack_verify_view(request):
    reference = request.GET.get('reference')
    if not reference:
        messages.error(request, 'No payment reference found.')
        return redirect('/agent/wallet/')

    try:
        result = verify_transaction(reference)
    except requests.Timeout:
        messages.error(request, 'Verification timed out. Your payment may still have gone through — please check your wallet balance.')
        return redirect('/agent/wallet/')
    except Exception as e:
        messages.error(request, f'Verification error: {str(e)}')
        return redirect('/agent/wallet/')

    if result.get('data', {}).get('status') == 'success':
        amount_ghc = Decimal(result['data']['amount']) / 100

        from django.contrib.auth import get_user_model
        User = get_user_model()
        user = User.objects.get(pk=request.user.pk)

        pending_bulk   = request.session.get('pending_bulk',   None)
        pending_single = request.session.get('pending_single', None)

        from orders.models import Order
        has_order        = Order.objects.filter(payment_reference=reference).exists()
        has_pending      = bool(pending_bulk or pending_single)
        is_order_payment = has_order or has_pending

        # ── PURE WALLET TOP-UP ────────────────────────────────────────────
        if not is_order_payment:
            try:
                from admin_panel.models import FeeConfiguration
                fee_config  = FeeConfiguration.get_current()
                fee_percent = Decimal(str(fee_config.paystack_fee_percent))
            except Exception:
                fee_percent = Decimal('2.00')

            fee        = (amount_ghc * fee_percent / 100).quantize(Decimal('0.01'))
            net_amount = amount_ghc - fee

            with transaction.atomic():
                # ── Idempotency guard: create the WalletTransaction FIRST ──
                # This wins the race against the webhook. If the webhook fires
                # before us, get_or_create returns created=False and we skip
                # the balance update entirely — no double credit ever.
                wallet, _ = Wallet.objects.get_or_create(user=user)
                txn, created = WalletTransaction.objects.get_or_create(
                    reference = reference,
                    defaults  = dict(
                        wallet           = wallet,
                        amount           = net_amount,
                        transaction_type = 'credit',
                        status           = 'completed',
                        description      = f'Paystack top-up (after {fee_percent}% fee)',
                    )
                )

                if not created:
                    # Webhook already processed this — balance already credited
                    print(f'[verify_view] {reference} already processed — skipping balance update')
                    messages.success(request, f'GHS {net_amount:.2f} added to your wallet.')
                    return redirect('/agent/wallet/')

                # ── Transaction row created — now safely update balance ────
                locked_user = User.objects.select_for_update().get(pk=user.pk)

                debt = Decimal(str(locked_user.debt_balance or 0))
                if debt > 0:
                    if net_amount >= debt:
                        net_amount                 -= debt
                        locked_user.debt_balance    = Decimal('0')
                    else:
                        locked_user.debt_balance    = debt - net_amount
                        net_amount                  = Decimal('0')
                    locked_user.save(update_fields=['debt_balance'])
                    try:
                        from orders.notifications import notify_debt_deducted
                        notify_debt_deducted(
                            locked_user,
                            float(debt - locked_user.debt_balance),
                            float(locked_user.debt_balance),
                            float(locked_user.wallet_balance),
                        )
                    except Exception:
                        pass

                User.objects.filter(pk=user.pk).update(
                    wallet_balance=F('wallet_balance') + net_amount
                )

                # Update txn amount in case debt reduced net_amount
                if txn.amount != net_amount:
                    txn.amount = net_amount
                    txn.save(update_fields=['amount'])

                if fee > 0:
                    WalletTransaction.objects.get_or_create(
                        reference = f'FEE-{reference}',
                        defaults  = dict(
                            wallet           = wallet,
                            amount           = fee,
                            transaction_type = 'debit',
                            status           = 'completed',
                            description      = f'{fee_percent}% top-up service charge',
                        )
                    )

            try:
                user.refresh_from_db(fields=['wallet_balance'])
                notify_wallet_topup(user, float(net_amount), method='Paystack')
            except Exception:
                pass

            messages.success(
                request,
                f'GHS {net_amount:.2f} added to your wallet. '
                f'(GHS {fee:.2f} service charge deducted from GHS {amount_ghc:.2f})'
            )
            return redirect('/agent/wallet/')

        # ── ORDER PAYMENT FLOW ────────────────────────────────────────────
        _confirm_order_payment(reference)
        _credit_link_profit(reference)

        # ── PENDING BULK ORDER ────────────────────────────────────────────
        if pending_bulk:
            try:
                from orders.agent_views import _place_bulk_orders
                prices = [Decimal(str(p)) for p in pending_bulk['prices']]
                _place_bulk_orders(
                    request,
                    pending_bulk['recipients'],
                    pending_bulk['package_ids'],
                    prices,
                    pending_bulk['network'],
                    user=user,
                )
                request.session.pop('pending_bulk', None)
                messages.success(request, 'Bulk orders placed successfully.')
                return redirect('/agent/orders/')
            except Exception as e:
                print('pending_bulk error:', e)
                messages.warning(request, f'Payment received but bulk orders could not be placed. Contact support. ref: {reference}')
                return redirect('/agent/orders/')

        # ── PENDING SINGLE ORDER ──────────────────────────────────────────
        if pending_single:
            try:
                from bundles.models import BundlePackage
                from orders.utils import place_order
                from orders.models import Order
                from orders.notifications import notify_order_placed

                package = BundlePackage.objects.get(id=pending_single['package_id'])
                price   = package.get_price_for_user(user)
                order   = place_order(
                    package   = package,
                    recipient = pending_single['recipient'],
                    price     = price,
                    source    = Order.Source.AGENT_SINGLE,
                    placed_by = user,
                    mark_paid = True,
                )
                from orders.models import Order as OrderModel
                OrderModel.objects.filter(order_id=order.order_id).update(
                    payment_status    = 'paid',
                    paid_at           = timezone.now(),
                    payment_reference = reference,
                )

                _dispatch_mtn_to_gati_async(order)

                request.session.pop('pending_single', None)
                messages.success(request, f'Order {order.order_id} placed successfully.')
                try:
                    notify_order_placed(order)
                except Exception:
                    pass
                return redirect('/agent/orders/')
            except Exception as e:
                print('pending_single error:', e)
                messages.warning(request, f'Payment received but your order could not be placed. Contact support. ref: {reference}')
                return redirect('/agent/orders/')

        messages.success(request, 'Payment confirmed. Your order is being processed.')
        return redirect('/agent/orders/')

    else:
        error_msg = result.get('data', {}).get('gateway_response', 'Verification failed.')
        messages.error(request, f'Payment failed: {error_msg}')
        return redirect('/agent/wallet/')


# ---------------------------------------------------------------------------
# WITHDRAWAL — LINK PROFIT ONLY
# ---------------------------------------------------------------------------

@agent_required
def withdraw_link_profit_view(request):
    if request.method != 'POST':
        return redirect('/agent/wallet/')

    form = WithdrawalForm(request.POST)
    if not form.is_valid():
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(request, error)
        return redirect('/agent/wallet/')

    amount       = form.cleaned_data['amount']
    phone_number = form.cleaned_data['mobile_number']
    network      = form.cleaned_data['network']

    MIN_WITHDRAWAL = Decimal('10.00')
    if amount < MIN_WITHDRAWAL:
        messages.error(request, 'Minimum withdrawal is GHS 10.00.')
        return redirect('/agent/wallet/')

    total_deduction = amount + SERVICE_CHARGE

    from django.contrib.auth import get_user_model
    User = get_user_model()

    try:
        with transaction.atomic():
            user = User.objects.select_for_update().get(pk=request.user.pk)

            available_profit = Decimal(str(user.link_profit_balance))

            if available_profit < total_deduction:
                messages.error(
                    request,
                    f'Insufficient link profit. Available: GHS {available_profit:.2f}. '
                    f'You need GHS {total_deduction:.2f} including GHS 1.00 service charge.'
                )
                return redirect('/agent/wallet/')

            user.link_profit_balance = Decimal(str(user.link_profit_balance)) - total_deduction
            user.save(update_fields=['link_profit_balance'])

            reference = f'WLP-{uuid.uuid4().hex[:10].upper()}'
            wallet, _ = Wallet.objects.get_or_create(user=user)

            WithdrawalRequest.objects.create(
                wallet       = wallet,
                amount       = amount,
                phone_number = phone_number,
                network      = network,
                reference    = reference,
                status       = 'pending',
            )
            WalletTransaction.objects.create(
                wallet           = wallet,
                amount           = total_deduction,
                transaction_type = 'withdrawal',
                status           = 'pending',
                reference        = reference,
                description      = f'Link profit withdrawal to {network} {phone_number} (incl. GHS 1.00 charge)',
            )

        messages.success(
            request,
            f'Link profit withdrawal of GHS {amount:.2f} requested to {network} {phone_number}. '
            f'GHS 1.00 service charge applied.'
        )
    except Exception as e:
        messages.error(request, 'Something went wrong. Please try again.')

    return redirect('/agent/wallet/')


# ---------------------------------------------------------------------------
# WITHDRAWAL — MAIN WALLET ONLY
# ---------------------------------------------------------------------------

@agent_required
def withdrawal_request_view(request):
    if request.method == 'POST':
        form = WithdrawalForm(request.POST)
        if not form.is_valid():
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, error)
            return redirect('/agent/wallet/')

        amount       = form.cleaned_data['amount']
        phone_number = form.cleaned_data['mobile_number']
        network      = form.cleaned_data['network']

        MIN_WITHDRAWAL = Decimal('10.00')
        if amount < MIN_WITHDRAWAL:
            messages.error(request, 'Minimum withdrawal is GHS 10.00.')
            return redirect('/agent/wallet/')

        total_deduction = amount + SERVICE_CHARGE

        from django.contrib.auth import get_user_model
        User = get_user_model()

        try:
            with transaction.atomic():
                user = User.objects.select_for_update().get(pk=request.user.pk)

                wallet_only = Decimal(str(user.wallet_balance))

                if wallet_only < total_deduction:
                    messages.error(
                        request,
                        f'Insufficient wallet balance. Available: GHS {wallet_only:.2f}. '
                        f'You need GHS {total_deduction:.2f} including GHS 1.00 service charge. '
                        f'Note: your link profit (GHS {user.link_profit_balance:.2f}) is separate — '
                        f'use "Withdraw Link Profit" for that.'
                    )
                    return redirect('/agent/wallet/')

                user.wallet_balance = Decimal(str(user.wallet_balance)) - total_deduction
                user.save(update_fields=['wallet_balance'])

                reference = f'WD-{uuid.uuid4().hex[:10].upper()}'
                wallet, _ = Wallet.objects.get_or_create(user=user)

                WithdrawalRequest.objects.create(
                    wallet       = wallet,
                    amount       = amount,
                    phone_number = phone_number,
                    network      = network,
                    reference    = reference,
                    status       = 'pending',
                )
                WalletTransaction.objects.create(
                    wallet           = wallet,
                    amount           = total_deduction,
                    transaction_type = 'withdrawal',
                    status           = 'pending',
                    reference        = reference,
                    description      = f'Wallet withdrawal to {network} {phone_number} (incl. GHS 1.00 charge)',
                )

            messages.success(
                request,
                f'Withdrawal of GHS {amount:.2f} requested to {network} {phone_number}. '
                f'GHS 1.00 service charge applied.'
            )
        except Exception as e:
            messages.error(request, 'Something went wrong. Please try again.')

    return redirect('/agent/wallet/')


# ---------------------------------------------------------------------------
# APPROVE WITHDRAWAL
# ---------------------------------------------------------------------------

@admin_required
def approve_withdrawal_view(request, withdrawal_id):
    if request.method != 'POST':
        return redirect('/admin-panel/withdrawals/')

    withdrawal = get_object_or_404(WithdrawalRequest, id=withdrawal_id)

    if withdrawal.status != 'pending':
        messages.error(request, 'This withdrawal has already been processed.')
        return redirect('/admin-panel/withdrawals/')

    agent     = withdrawal.wallet.user
    bank_code = NETWORK_CODES.get(withdrawal.network.upper(), 'MTN')
    is_link_profit_withdrawal = withdrawal.reference.startswith('WLP-')

    name = getattr(agent, 'full_name', None) or withdrawal.phone_number

    try:
        recipient_code = create_momo_recipient(
            name      = name,
            phone     = withdrawal.phone_number,
            bank_code = bank_code,
        )

        transfer_response = initiate_transfer(
            recipient_code = recipient_code,
            amount_ghc     = withdrawal.amount,
            reference      = f'{withdrawal.reference}-R{uuid.uuid4().hex[:4].upper()}',
            reason         = f'Withdrawal - {withdrawal.phone_number}',
        )

        if transfer_response and transfer_response.get('transfer_code'):
            withdrawal.status       = 'completed'
            withdrawal.processed_by = request.user
            withdrawal.processed_at = timezone.now()
            withdrawal.save()
            WalletTransaction.objects.filter(reference=withdrawal.reference).update(
                status='completed'
            )
            messages.success(
                request,
                f'GHS {withdrawal.amount} sent to {withdrawal.phone_number} ({withdrawal.network}).'
            )
        else:
            reason = transfer_response.get('message', 'unknown reason') if transfer_response else 'no response'
            messages.error(
                request,
                f'Paystack rejected the transfer: {reason}. '
                f'Withdrawal left pending — check Paystack dashboard then retry.'
            )

    except requests.Timeout:
        withdrawal.status       = 'failed'
        withdrawal.processed_by = request.user
        withdrawal.processed_at = timezone.now()
        withdrawal.save()

        from django.contrib.auth import get_user_model
        User   = get_user_model()
        refund = withdrawal.amount + SERVICE_CHARGE

        if is_link_profit_withdrawal:
            User.objects.filter(pk=agent.pk).update(
                link_profit_balance = F('link_profit_balance') + refund,
            )
        else:
            User.objects.filter(pk=agent.pk).update(
                wallet_balance = F('wallet_balance') + refund,
            )

        WalletTransaction.objects.filter(reference=withdrawal.reference).update(status='failed')
        messages.error(
            request,
            f'Paystack timed out for {withdrawal.phone_number} (GHS {withdrawal.amount}). '
            f'Balance has been refunded. Check Paystack dashboard and retry manually if needed.'
        )

    except Exception as e:
        withdrawal.status       = 'failed'
        withdrawal.processed_by = request.user
        withdrawal.processed_at = timezone.now()
        withdrawal.save()

        from django.contrib.auth import get_user_model
        User   = get_user_model()
        refund = withdrawal.amount + SERVICE_CHARGE

        if is_link_profit_withdrawal:
            User.objects.filter(pk=agent.pk).update(
                link_profit_balance = F('link_profit_balance') + refund,
            )
        else:
            User.objects.filter(pk=agent.pk).update(
                wallet_balance = F('wallet_balance') + refund,
            )

        WalletTransaction.objects.filter(reference=withdrawal.reference).update(status='failed')
        messages.error(request, f'Payout failed: {str(e)}. Balance refunded.')

    return redirect('/admin-panel/withdrawals/')


# ---------------------------------------------------------------------------
# PAYSTACK WEBHOOK
# ---------------------------------------------------------------------------
@csrf_exempt
def paystack_webhook_view(request):
    signature = request.headers.get('x-paystack-signature', '')
    computed  = hmac.new(
        settings.PAYSTACK_SECRET_KEY.encode(),
        request.body,
        hashlib.sha512,
    ).hexdigest()

    if signature != computed:
        return HttpResponse(status=400)

    payload   = json.loads(request.body)
    event     = payload.get('event')
    data      = payload.get('data', {})
    reference = data.get('reference')

    if not reference:
        return HttpResponse(status=200)

    if event == 'charge.success':
        if reference.startswith('SKP-'):
            # Webhook is the FALLBACK for wallet top-ups.
            # verify_view is primary — it creates the WalletTransaction first.
            # If verify_view already ran, get_or_create returns created=False
            # and we skip the balance update entirely — no double credit.
            try:
                from django.contrib.auth import get_user_model
                amount_ghc = Decimal(str(data.get('amount', 0))) / 100
                User  = get_user_model()
                email = data.get('customer', {}).get('email', '')
                user  = None
                if '@gigmax.com' in email:
                    phone = email.replace('@gigmax.com', '')
                    user  = User.objects.filter(phone_number=phone).first()
                if not user:
                    user = User.objects.filter(email=email).first()

                if user and amount_ghc > 0:
                    try:
                        from admin_panel.models import FeeConfiguration
                        fee_config  = FeeConfiguration.get_current()
                        fee_percent = Decimal(str(fee_config.paystack_fee_percent))
                    except Exception:
                        fee_percent = Decimal('2.00')

                    fee        = (amount_ghc * fee_percent / 100).quantize(Decimal('0.01'))
                    net_amount = amount_ghc - fee

                    wallet_obj, _ = Wallet.objects.get_or_create(user=user)

                    with transaction.atomic():
                        txn, created = WalletTransaction.objects.get_or_create(
                            reference = reference,
                            defaults  = dict(
                                wallet           = wallet_obj,
                                amount           = net_amount,
                                transaction_type = 'credit',
                                status           = 'completed',
                                description      = f'Paystack top-up (webhook fallback, after {fee_percent}% fee)',
                            )
                        )

                        if not created:
                            # verify_view already handled this — do nothing
                            print(f'[WEBHOOK] SKP {reference} already processed by verify_view — skipping')
                        else:
                            # verify_view never ran — we are the fallback, credit the balance
                            print(f'[WEBHOOK] SKP {reference} — webhook acting as fallback')
                            locked_user = User.objects.select_for_update().get(pk=user.pk)
                            debt = Decimal(str(locked_user.debt_balance or 0))
                            if debt > 0:
                                if net_amount >= debt:
                                    net_amount               -= debt
                                    locked_user.debt_balance  = Decimal('0')
                                else:
                                    locked_user.debt_balance  = debt - net_amount
                                    net_amount                = Decimal('0')
                                locked_user.save(update_fields=['debt_balance'])

                            User.objects.filter(pk=locked_user.pk).update(
                                wallet_balance=F('wallet_balance') + net_amount
                            )

                            if txn.amount != net_amount:
                                txn.amount = net_amount
                                txn.save(update_fields=['amount'])

                            if fee > 0:
                                WalletTransaction.objects.get_or_create(
                                    reference=f'FEE-{reference}',
                                    defaults=dict(
                                        wallet           = wallet_obj,
                                        amount           = fee,
                                        transaction_type = 'debit',
                                        status           = 'completed',
                                        description      = f'{fee_percent}% top-up service charge',
                                    )
                                )

                            try:
                                locked_user.refresh_from_db(fields=['wallet_balance'])
                                notify_wallet_topup(locked_user, float(net_amount), method='Paystack')
                            except Exception:
                                pass

            except Exception as e:
                print(f'[WEBHOOK] SKP wallet top-up error for {reference}: {e}')

        else:
            try:
                _confirm_order_payment(reference)
            except Exception as e:
                print(f'[WEBHOOK] charge.success order error for {reference}: {e}')

            _create_order_from_metadata(data, reference)
            _credit_link_profit(reference)

    elif event == 'transfer.success':
        try:
            withdrawal        = WithdrawalRequest.objects.get(reference=reference)
            withdrawal.status = 'completed'
            withdrawal.save()
            WalletTransaction.objects.filter(reference=reference).update(status='completed')
        except WithdrawalRequest.DoesNotExist:
            pass

    elif event in ('transfer.failed', 'transfer.reversed'):
        try:
            withdrawal = WithdrawalRequest.objects.get(reference=reference)
            if withdrawal.status != 'failed':
                from django.contrib.auth import get_user_model
                User   = get_user_model()
                agent  = withdrawal.wallet.user
                refund = withdrawal.amount + SERVICE_CHARGE
                is_link_profit_withdrawal = withdrawal.reference.startswith('WLP-')

                if is_link_profit_withdrawal:
                    User.objects.filter(pk=agent.pk).update(
                        link_profit_balance = F('link_profit_balance') + refund,
                    )
                else:
                    User.objects.filter(pk=agent.pk).update(
                        wallet_balance = F('wallet_balance') + refund,
                    )

                withdrawal.status = 'failed'
                withdrawal.save()
                WalletTransaction.objects.filter(reference=withdrawal.reference).update(status='failed')
        except WithdrawalRequest.DoesNotExist:
            pass

    return HttpResponse(status=200)


# ---------------------------------------------------------------------------
# REJECT WITHDRAWAL
# ---------------------------------------------------------------------------

@admin_required
def reject_withdrawal_view(request, withdrawal_id):
    if request.method != 'POST':
        return redirect('/admin-panel/withdrawals/')

    withdrawal = get_object_or_404(WithdrawalRequest, id=withdrawal_id)

    if withdrawal.status != 'pending':
        messages.error(request, 'This withdrawal has already been processed.')
        return redirect('/admin-panel/withdrawals/')

    is_link_profit_withdrawal = withdrawal.reference.startswith('WLP-')

    try:
        with transaction.atomic():
            from django.contrib.auth import get_user_model
            User   = get_user_model()
            agent  = withdrawal.wallet.user
            refund = withdrawal.amount + SERVICE_CHARGE

            if is_link_profit_withdrawal:
                User.objects.filter(pk=agent.pk).update(
                    link_profit_balance = F('link_profit_balance') + refund,
                )
            else:
                User.objects.filter(pk=agent.pk).update(
                    wallet_balance = F('wallet_balance') + refund,
                )

            withdrawal.status       = 'rejected'
            withdrawal.processed_by = request.user
            withdrawal.processed_at = timezone.now()
            withdrawal.save()

            WalletTransaction.objects.filter(reference=withdrawal.reference).update(status='failed')

        messages.success(request, f'Withdrawal rejected. GHS {refund:.2f} refunded to {withdrawal.phone_number}.')
    except Exception as e:
        messages.error(request, f'Something went wrong: {str(e)}')

    return redirect('/admin-panel/withdrawals/')


# ---------------------------------------------------------------------------
# KUNTANII WEBHOOK
# ---------------------------------------------------------------------------
@csrf_exempt
def kuntanii_webhook_view(request):
    import json as _json
    import hmac as _hmac
    import hashlib as _hashlib
    from orders.models import Order

    print(f'[Kuntanii Webhook DEBUG] All headers received: {dict(request.headers)}')
    signature = request.headers.get('X-Kuntanii-Signature', '')
    secret    = getattr(settings, 'KUNTANII_WEBHOOK_SECRET', '')
    sig_valid = None
    if secret and signature:
        computed = _hmac.new(secret.encode(), request.body, _hashlib.sha256).hexdigest()
        sig_valid = _hmac.compare_digest(signature, computed)
        if not sig_valid:
            print(f'[Kuntanii Webhook] *** SECURITY WARNING *** SIGNATURE MISMATCH. '
                  f'Received: {signature[:12]}... Computed: {computed[:12]}... '
                  f'Processing anyway pending format confirmation — DO NOT IGNORE if this '
                  f'appears repeatedly, it may mean forged requests are being accepted.')
            try:
                from admin_panel.models import KuntaniiSettings
                ks = KuntaniiSettings.get()
                ks.last_signature_warning = timezone.now()
                ks.save(update_fields=['last_signature_warning'])
            except Exception as _e:
                print(f'[Kuntanii Webhook] Could not record signature warning: {_e}')
    elif secret and not signature:
        print(f'[Kuntanii Webhook] *** SECURITY WARNING *** No signature header present '
              f'on incoming webhook despite secret being configured. Processing anyway '
              f'pending format confirmation.')

    try:
        payload = _json.loads(request.body)
    except Exception:
        return HttpResponse(status=400)

    event = payload.get('event', '')
    data  = payload.get('data', {}) or {}
    external_order_id = data.get('externalOrderId')

    if not external_order_id:
        return HttpResponse(status=200)

    orders_list = data.get('orders', [])
    kt_status   = None
    if orders_list:
        kt_status = orders_list[0].get('status')

    status_map = {
        'pending':    'processing',
        'processing': 'processing',
        'processed':  'delivered',
        'cancelled':  'refunded',
    }
    our_status = status_map.get(kt_status)

    try:
        target_status = our_status or 'refunded'
        order_obj = Order.objects.filter(order_id=external_order_id).first()
        if order_obj:
            # Load-then-save (not .update()) so the pre_save signal fires and
            # agent wallet/debt crediting happens automatically on refund.
            order_obj.status = target_status
            order_obj.save()
            if our_status:
                print(f'[Kuntanii Webhook] {external_order_id} -> {our_status} (event={event}, kt_status={kt_status})')
            else:
                print(f'[Kuntanii Webhook] UNRECOGNIZED status "{kt_status}" for {external_order_id} '
                      f'(event={event}) - flagged as refunded for review. Raw: {payload}')
        else:
            print(f'[Kuntanii Webhook] Order {external_order_id} not found - skipped')
    except Exception as e:
        print(f'[Kuntanii Webhook] DB update error for {external_order_id}: {e}')

    return HttpResponse(status=200)


# ---------------------------------------------------------------------------
# REVERIFY — same as verify, different URL for retry flows
# ---------------------------------------------------------------------------

@login_required(login_url='/accounts/login/')
def paystack_reverify_view(request):
    return paystack_verify_view(request)