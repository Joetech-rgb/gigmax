import uuid
import requests
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from orders.models import Order
from bundles.models import BundlePackage
from orders.delivery_utils import guest_order, pay_and_deliver, expire_stale_payments
from django.db import connection


# ---------------------------------------------------------------------------
# Helper — redirect logged-in staff away from customer pages
# ---------------------------------------------------------------------------

def _redirect_if_logged_in(request):
    if request.user.is_authenticated and hasattr(request.user, 'role'):
        if request.user.role == 'admin':
            return redirect('/admin-panel/dashboard/')
        elif request.user.role in ['small_agent', 'moderate', 'mega', 'extreme', 'super_mega']:
            return redirect('/agent/dashboard/')
    return None


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------

def checkout_page(request, package_id):
    package = get_object_or_404(BundlePackage, id=package_id)
    phone   = request.GET.get('phone', '')
    return render(request, 'customer/checkout.html', {
        'package': package,
        'phone':   phone,
    })

@require_http_methods(["POST"])
def initiate_payment(request, package_id):
    """Initialize Paystack — NO order created yet."""
    package = get_object_or_404(BundlePackage, id=package_id)
    phone   = request.POST.get('phone')
    email   = request.POST.get('email', f"{phone}@customer.com")

    if not phone:
        return JsonResponse({'error': 'Phone number required'}, status=400)

    # GUARD: if this phone already has a pending/processing order for this package
    # block the payment and tell the customer
    existing_order = Order.objects.filter(
        recipient=phone,
        gb_package=package,
        status__in=['pending', 'processing'],
    ).first()

    if existing_order:
        print(f'[initiate_payment] Blocked duplicate — {phone} already has '
              f'order {existing_order.order_id} in status {existing_order.status}')
        return JsonResponse({
            'error': (
                f'You already have an active order ({existing_order.order_id}) '
                f'for this bundle. It is currently {existing_order.status}. '
                f'Please wait for it to complete before placing another.'
            )
        }, status=400)

    link       = None
    link_token = None
    price      = package.customer_price
    try:
        link_token = request.session.get('active_link_token')
        if link_token:
            from orders.models import GeneratedLink, GeneratedLinkPrice
            link = GeneratedLink.objects.filter(token=link_token, is_active=True).first()
            if link:
                link_price = GeneratedLinkPrice.objects.filter(
                    link=link, bundle=package
                ).first()
                if link_price:
                    price = link_price.retail_price
    except Exception as e:
        print(f"[initiate_payment link price] {e}")

    # -- Apply 2% service fee -----------------------------------------
    try:
        from admin_panel.models import FeeConfiguration
        from decimal import Decimal
        fee_config  = FeeConfiguration.get_current()
        fee_percent = Decimal(str(fee_config.paystack_fee_percent))
    except Exception:
        from decimal import Decimal
        fee_percent = Decimal('2.00')

    price_decimal  = Decimal(str(price))
    fee            = (price_decimal * fee_percent / 100).quantize(Decimal('0.01'))
    price_with_fee = price_decimal + fee

    reference = f"PAY_{uuid.uuid4().hex[:12].upper()}"

    request.session[f'pay_intent_{reference}'] = {
        'charged'    : str(price_with_fee),
        'phone'      : phone,
        'package_id' : str(package_id),
        'price'      : str(price),
        'link_token' : link_token or '',
        'email'      : email,
    }

    try:
        from wallet import gateway
        result = gateway.initialize_transaction(
            email=email,
            amount_ghc=price_with_fee,
            reference=reference,
            metadata=request.session.get(f'pay_intent_{reference}'),
            callback_url=request.build_absolute_uri('/payment/verify/'),
        )
        if result.get('status'):
            return redirect(result['data']['authorization_url'])

        return JsonResponse({'error': 'Payment initialization failed'}, status=400)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
# ---------------------------------------------------------------------------
# Shared helper — build intent from Paystack metadata when session is gone
# ---------------------------------------------------------------------------

def _intent_from_metadata(data):
    """
    Reconstruct order intent from Paystack transaction data when session is missing.
    Returns intent dict or None if not enough info.
    """
    meta       = data.get('metadata') or {}
    phone      = meta.get('phone', '').strip()
    link_token = meta.get('link_token', '').strip()
    package_id = meta.get('package_id', '').strip()
    price      = meta.get('price', '').strip()

    # Best case: metadata has everything
    if phone and package_id and price:
        return {
            'phone'      : phone,
            'package_id' : package_id,
            'price'      : price,
            'link_token' : link_token,
        }

    # NEW: metadata has phone + package but no price — look price up from DB
    if phone and package_id and not price:
        try:
            from bundles.models import BundlePackage
            from orders.models import GeneratedLink, GeneratedLinkPrice
            pkg = BundlePackage.objects.filter(id=package_id, is_active=True).first()
            if pkg:
                resolved_price = str(pkg.customer_price)
                if link_token:
                    link = GeneratedLink.objects.filter(
                        token=link_token, is_active=True
                    ).first()
                    if link:
                        lp = GeneratedLinkPrice.objects.filter(
                            link=link, bundle=pkg
                        ).first()
                        if lp:
                            resolved_price = str(lp.retail_price)
                print(f'[_intent_from_metadata] Recovered price {resolved_price} '
                      f'for pkg {package_id} from DB')
                return {
                    'phone'      : phone,
                    'package_id' : package_id,
                    'price'      : resolved_price,
                    'link_token' : link_token,
                }
        except Exception as e:
            print(f'[_intent_from_metadata] DB price lookup error: {e}')

    # Fallback: try to find package from link + amount paid
    if phone and link_token:
        try:
            from orders.models import GeneratedLink, GeneratedLinkPrice
            link = GeneratedLink.objects.filter(token=link_token, is_active=True).first()
            if link:
                amount_ghc = float(data.get('amount', 0)) / 100
                lp = GeneratedLinkPrice.objects.filter(
                    link=link, retail_price=amount_ghc
                ).select_related('bundle').first()
                if not lp:
                    for candidate in GeneratedLinkPrice.objects.filter(
                        link=link
                    ).select_related('bundle'):
                        if abs(float(candidate.retail_price) - amount_ghc) <= 0.50:
                            lp = candidate
                            break
                if lp:
                    return {
                        'phone'      : phone,
                        'package_id' : str(lp.bundle.id),
                        'price'      : str(lp.retail_price),
                        'link_token' : link_token,
                    }
        except Exception as e:
            print(f'[_intent_from_metadata] {e}')

    return None

# ---------------------------------------------------------------------------
# Payment verification — order created HERE after Paystack confirms
# ---------------------------------------------------------------------------

def verify_payment(request):
    reference = request.GET.get('reference')
    if not reference:
        return redirect('/buy/')

    try:
        from wallet import gateway
        result = gateway.verify_transaction(reference)

        if result:
            data = result.get('data', {})

            if data.get('status') == 'success':

                # IDEMPOTENCY: webhook or previous verify already handled this
                existing = Order.objects.filter(payment_reference=reference).first()
                if existing and existing.payment_status == 'paid':
                    return render(request, 'customer/payment_success.html', {'order': existing})

                # If order exists but not yet confirmed — confirm it
                if existing:
                    order = pay_and_deliver(existing.order_id)
                    request.session.pop(f'pay_intent_{reference}', None)
                    return render(request, 'customer/payment_success.html', {'order': order})

                # Get intent from session first, fall back to Paystack metadata
                intent = request.session.get(f'pay_intent_{reference}')
                if not intent:
                    print(f'[verify_payment] Session miss for {reference} — trying metadata fallback')
                    intent = _intent_from_metadata(data)

                if not intent:
                    print(f'[verify_payment] ORPHAN: ref={reference} — no session, no usable metadata')
                    return render(request, 'customer/payment_failed.html', {'reference': reference})

                phone      = intent['phone']
                package_id = intent['package_id']
                price      = intent['price']
                link_token = intent.get('link_token', '')

                try:
                    from decimal import Decimal as _D
                    _charged = intent.get('charged')
                    if _charged and _D(str(data.get('amount', 0))) / 100 < _D(str(_charged)):
                        print(f'[verify_payment] UNDERPAID ref={reference} paid={data.get("amount")} expected={_charged}')
                        return render(request, 'customer/payment_failed.html', {
                            'reference': reference,
                            'error': 'The amount paid does not match this order. Contact support with your payment reference.',
                        })
                except ArithmeticError:
                    pass

                from decimal import Decimal as _D2
                from wallet.views import _expected_base_price as _ebp
                _exp = _ebp(package_id, link_token)
                try:
                    _paid = _D2(str(data.get('amount', 0))) / 100
                except ArithmeticError:
                    _paid = _D2('0')
                if _exp is None or _paid < _exp:
                    print(f'[verify_payment] REFUSED ref={reference}: paid {_paid}, database price {_exp}')
                    return render(request, 'customer/payment_failed.html', {
                        'reference': reference,
                        'error': 'The amount paid does not match this order. Contact support with your payment reference.',
                    })
                price = str(_exp)

                from django.db import transaction
                with transaction.atomic():
                    existing_now = Order.objects.filter(
                        payment_reference=reference
                    ).first()
                    if existing_now:
                        print(f'[verify_payment] Order already created by webhook: '
                              f'{existing_now.order_id} for {reference}')
                        request.session.pop(f'pay_intent_{reference}', None)
                        if existing_now.payment_status == 'paid':
                            return render(request, 'customer/payment_success.html',
                                          {'order': existing_now})
                        order = pay_and_deliver(existing_now.order_id)
                        return render(request, 'customer/payment_success.html', {'order': order})

                    existing_active = Order.objects.filter(
                        recipient=phone,
                        gb_package__id=package_id,
                        status__in=['pending', 'processing'],
                    ).exclude(payment_reference=reference).first()

                    if existing_active:
                        print('[verify_payment] %s already has active order %s - paid reference %s still gets its own order' % (phone, existing_active.order_id, reference))

                    from django.db import IntegrityError as _IE
                    try:
                        with transaction.atomic():
                            order = guest_order(phone, package_id, price=price)
                            Order.objects.filter(id=order.id).update(payment_reference=reference)
                    except _IE:
                        _won = Order.objects.filter(payment_reference=reference).first()
                        if _won is None:
                            raise
                        request.session.pop(f'pay_intent_{reference}', None)
                        if _won.payment_status == 'paid':
                            return render(request, 'customer/payment_success.html', {'order': _won})
                        _won = pay_and_deliver(_won.order_id)
                        return render(request, 'customer/payment_success.html', {'order': _won})

                # ── FIX: attach generated_link BEFORE pay_and_deliver ──────
                # Previously this was done AFTER pay_and_deliver, which meant
                # _credit_link_profit_local ran with generated_link=NULL and
                # skipped profit, and the order never appeared in the agent's
                # order list because generated_link was still NULL at query time.
                if link_token:
                    try:
                        from orders.models import GeneratedLink
                        link_obj = GeneratedLink.objects.filter(
                            token=link_token, is_active=True
                        ).first()
                        if link_obj:
                            Order.objects.filter(id=order.id).update(
                                generated_link=link_obj,
                                source='generated_link',
                            )
                    except Exception as e:
                        print(f"[verify_payment link attach] {e}")

                # refresh_from_db so pay_and_deliver sees generated_link set
                order.refresh_from_db()
                order = pay_and_deliver(order.order_id)
                request.session.pop(f'pay_intent_{reference}', None)
                return render(request, 'customer/payment_success.html', {'order': order})

        return render(request, 'customer/payment_failed.html', {'reference': reference})

    except Exception as e:
        return render(request, 'customer/payment_failed.html', {'error': str(e)})
    
    
# ---------------------------------------------------------------------------
# Payment result pages
# ---------------------------------------------------------------------------

def payment_success(request, order_id):
    order = get_object_or_404(Order, order_id=order_id)
    return render(request, 'customer/payment_success.html', {'order': order})


def payment_failed(request):
    return render(request, 'customer/payment_failed.html')


# ---------------------------------------------------------------------------
# Guest buy (main public page)
# ---------------------------------------------------------------------------
def guest_buy(request):
    from admin_panel.models import SiteSettings

    redir = _redirect_if_logged_in(request)
    if redir:
        return redir

    packages       = BundlePackage.objects.filter(is_active=True).order_by('gb_size')
    promo_packages = BundlePackage.objects.filter(is_promo=True, is_active=True)

    if request.method == 'POST':
        package_id = request.POST.get('package_id')
        phone      = request.POST.get('phone')
        if package_id and phone:
            return redirect(f'/checkout/{package_id}/?phone={phone}')

    return render(request, 'customer/guest_buy.html', {
        'packages'      : packages,
        'promo_packages': promo_packages,
        'mtn_enabled'   : SiteSettings.get().mtn_enabled,
    })

def home_view(request):
    redir = _redirect_if_logged_in(request)
    if redir:
        return redir

    promo_packages = BundlePackage.objects.filter(is_promo=True, is_active=True)
    return render(request, 'customer/home.html', {'promo_packages': promo_packages})


def buy_promo(request, package_id):
    phone = request.GET.get('phone', '')
    if not phone:
        return redirect('/')
    return redirect(f'/checkout/{package_id}/?phone={phone}')


# ---------------------------------------------------------------------------
# Agent storefront — buy via generated link
# ---------------------------------------------------------------------------

_ACCENT_COLORS = [
    {'primary': '#1a4fd6', 'dark': '#1240b0'},
    {'primary': '#1a7fd6', 'dark': '#1265aa'},
    {'primary': '#1565c0', 'dark': '#0d47a1'},
    {'primary': '#0077b6', 'dark': '#005f8f'},
    {'primary': '#2563eb', 'dark': '#1d4ed8'},
    {'primary': '#1e40af', 'dark': '#1e3a8a'},
    {'primary': '#0369a1', 'dark': '#075985'},
]

_NETWORK_LABELS = {
    'MTN':         'MTN',
    'TELECEL':     'Telecel',
    'AT_NOEXPIRY': 'AirtelTigo (No Expiry)',
    'AT_EXPIRY':   'AirtelTigo (Expiry)',
}

def buy_with_link(request, token):
    from orders.models import GeneratedLink, GeneratedLinkPrice
    from admin_panel.models import SiteSettings
    from django.contrib import messages

    link = GeneratedLink.objects.filter(token=token, is_active=True).first()
    if not link:
        messages.error(request, "This purchase link is invalid or has expired.")
        return redirect('/buy/')

    request.session['active_link_token'] = token
    request.session['active_link_agent'] = str(link.agent.id)

    shop_name = (link.label or '').strip() or link.agent.full_name
    words     = shop_name.split()
    initials  = ''.join(w[0].upper() for w in words[:2]) if words else '??'

    color       = _ACCENT_COLORS[int(str(link.agent.id).replace('-', ''), 16) % len(_ACCENT_COLORS)]
    accent      = color['primary']
    accent_dark = color['dark']

    all_link_prices = (
        GeneratedLinkPrice.objects
        .filter(link=link, bundle__is_active=True)
        .select_related('bundle')
        .order_by('bundle__network', 'bundle__gb_size')
    )

    if not all_link_prices.exists():
        messages.error(request, "This link has no packages configured yet.")
        return redirect('/buy/')

    # Build network list
    seen_nets = []
    net_keys  = []
    for lp in all_link_prices:
        net = lp.bundle.network
        if net not in net_keys:
            net_keys.append(net)
            seen_nets.append({'key': net, 'label': _NETWORK_LABELS.get(net, net)})

    base_ctx = {
        'link'       : link,
        'shop_name'  : shop_name,
        'initials'   : initials,
        'accent'     : accent,
        'accent_dark': accent_dark,
        'networks'   : seen_nets,
        'all_packages': all_link_prices,
        'success'    : False,
        'hide_login' : True,
        'mtn_enabled': SiteSettings.get().mtn_enabled,
    }

    # POST: customer submitted — initialize Paystack WITHOUT creating an order
    if request.method == 'POST':
        phone      = request.POST.get('phone', '').strip()
        package_id = request.POST.get('package_id', '').strip()
        email      = f"{phone}@customer.com"

        if not phone or not package_id:
            messages.error(request, "Please fill in all fields.")
        else:
            try:
                package    = get_object_or_404(BundlePackage, id=package_id, is_active=True)
                link_price = GeneratedLinkPrice.objects.filter(link=link, bundle=package).first()
                price      = link_price.retail_price if link_price else package.customer_price

                # -- Apply service fee ------------------------------------
                try:
                    from admin_panel.models import FeeConfiguration
                    from decimal import Decimal
                    fee_config  = FeeConfiguration.get_current()
                    fee_percent = Decimal(str(fee_config.paystack_fee_percent))
                except Exception:
                    from decimal import Decimal
                    fee_percent = Decimal('2.00')

                price_decimal  = Decimal(str(price))
                fee            = (price_decimal * fee_percent / 100).quantize(Decimal('0.01'))
                price_with_fee = price_decimal + fee

                # Generate reference — NO guest_order() call here
                reference = f"PAY_{uuid.uuid4().hex[:12].upper()}"

                # Store intent in session — original price, NOT price_with_fee
                request.session[f'pay_intent_{reference}'] = {
                    'charged'    : str(price_with_fee),
                    'phone'      : phone,
                    'package_id' : str(package_id),
                    'price'      : str(price),
                    'link_token' : token,
                    'email'      : email,
                }

                try:
                    from wallet import gateway
                    result = gateway.initialize_transaction(
                        email=email,
                        amount_ghc=price_with_fee,
                        reference=reference,
                        metadata=request.session.get(f'pay_intent_{reference}'),
                        callback_url=request.build_absolute_uri('/payment/verify/'),
                    )
                    if result.get('status'):
                        return redirect(result['data']['authorization_url'])
                    messages.error(request, "Payment initialization failed. Please try again.")
                except Exception as e:
                    print(f"[buy_with_link initialize] {e}")
                    messages.error(request, "Payment initialization failed. Please try again.")

            except Exception as e:
                print(f"[buy_with_link POST] {e}")
                messages.error(request, f"Something went wrong: {str(e)}")

    selected_network = request.GET.get('network', net_keys[0] if net_keys else '')
    packages         = all_link_prices.filter(bundle__network=selected_network)

    return render(request, 'customer/link_order.html', {
        **base_ctx,
        'selected_network': selected_network,
        'packages'        : packages,
    })
# ---------------------------------------------------------------------------
# Wallet payment
# ---------------------------------------------------------------------------

def pay_with_wallet(request, package_id):
    if not request.user.is_authenticated:
        return redirect('/accounts/login/')

    if request.method == 'POST':
        from django.contrib import messages
        from django.utils import timezone
        from admin_panel.models import FeeConfiguration
        from decimal import Decimal

        package = get_object_or_404(BundlePackage, id=package_id, is_active=True)
        phone   = request.POST.get('phone')

        try:
            fee_config  = FeeConfiguration.get_current()
            fee_percent = Decimal(str(fee_config.paystack_fee_percent))
        except Exception:
            fee_percent = Decimal('2.00')

        price          = Decimal(str(package.customer_price))
        fee            = (price * fee_percent / 100).quantize(Decimal('0.01'))
        price_with_fee = price + fee

        if not phone:
            messages.error(request, 'Enter the recipient phone number.')
            return redirect(f'/checkout/{package_id}/')

        import uuid as _uuid
        from django.db import transaction as _tx
        from django.contrib.auth import get_user_model as _gum
        from wallet.models import Wallet as _WM, WalletTransaction as _WT
        _U = _gum()
        _ref = f'CWALLET-{_uuid.uuid4().hex[:10].upper()}'

        _short = False
        try:
            with _tx.atomic():
                _u = _U.objects.select_for_update().get(pk=request.user.pk)
                _bal = Decimal(str(_u.wallet_balance or 0))
                if _bal < price_with_fee:
                    _short = True
                else:
                    _u.wallet_balance = _bal - price_with_fee
                    _u.save(update_fields=['wallet_balance'])
                    _w, _ = _WM.objects.get_or_create(user=_u)
                    _WT.objects.create(
                        wallet=_w, amount=price_with_fee, transaction_type='debit',
                        status='completed', reference=_ref,
                        description=f'Customer wallet payment (incl. {fee_percent}% fee)',
                        wallet_before=_bal, wallet_after=_bal - price_with_fee,
                    )
        except Exception as e:
            print(f'[wallet pay] debit failed for user {request.user.pk}: {e}')
            messages.error(request, 'Payment could not be processed. You have not been charged.')
            return redirect(f'/checkout/{package_id}/?phone={phone}')
        if _short:
            messages.error(request, 'Insufficient wallet balance.')
            return redirect(f'/checkout/{package_id}/?phone={phone}')

        def _reverse():
            with _tx.atomic():
                _r = _U.objects.select_for_update().get(pk=request.user.pk)
                _b = Decimal(str(_r.wallet_balance or 0))
                _r.wallet_balance = _b + price_with_fee
                _r.save(update_fields=['wallet_balance'])
                _w2, _ = _WM.objects.get_or_create(user=_r)
                _WT.objects.create(
                    wallet=_w2, amount=price_with_fee, transaction_type='credit',
                    status='completed', reference='REV-' + _ref,
                    description=f'Reversal: order could not be created ({_ref})',
                    wallet_before=_b, wallet_after=_b + price_with_fee,
                )

        try:
            order = guest_order(phone, package_id)
        except Exception as e:
            print(f'[wallet pay] order creation failed after debit {_ref}: {e}')
            try:
                _reverse()
                messages.error(request, 'We could not create your order, so your wallet was refunded.')
            except Exception as e2:
                print(f'[wallet pay] REVERSAL FAILED for {_ref}, user {request.user.pk}: {e2}')
                messages.error(request, f'Something went wrong. Contact support with reference {_ref}.')
            return redirect(f'/checkout/{package_id}/?phone={phone}')

        try:
            order = pay_and_deliver(order.order_id)
        except Exception as e:
            print(f'[wallet pay] delivery step failed for {order.order_id} ({_ref}): {e}')
            messages.error(request, f'Your payment was taken but delivery did not finish. Contact support with order {order.order_id}.')
            return redirect('/')
        return render(request, 'customer/payment_success.html', {'order': order})

    return redirect('/')

# ---------------------------------------------------------------------------
# Order detail
# ---------------------------------------------------------------------------

_STATUS_DISPLAY = {
    'pending_payment': ('Awaiting payment', 'badge-muted', "We haven't received payment for this order yet."),
    'pending': ('Pending', 'badge-info', 'Your order has been received and is waiting to be sent.'),
    'processing': ('Processing', 'badge-warning', 'Your order is being delivered.'),
    'delivered': ('Delivered', 'badge-success', 'The data has been delivered to this number.'),
    'waiting': ('Waiting', 'badge-warning', "We couldn't deliver this order yet. You can request a refund below."),
    'kuntanii_unverified': ('Waiting', 'badge-warning', "We couldn't deliver this order yet. You can request a refund below."),
    'refunded': ('Refunded', 'badge-muted', 'This order has been refunded.'),
    'failed': ('Failed', 'badge-danger', 'Delivery failed for this order.'),
}


def _with_status_display(orders):
    rows = list(orders)
    for o in rows:
        shown = _STATUS_DISPLAY.get(o.status)
        if shown is None:
            getter = getattr(o, 'get_status_display', None)
            shown = (getter() if getter else str(o.status), 'badge-muted', '')
        o.status_label, o.status_class, o.status_note = shown
    return rows


def my_orders_view(request):
    from orders.models import Order
    search    = request.GET.get('search', '').strip()
    order_ref = request.GET.get('order', '').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to   = request.GET.get('date_to', '').strip()

    orders = Order.objects.none()
    if order_ref:
        orders = Order.objects.filter(order_id=order_ref.upper()).order_by('-created_at')
    elif search:
        orders = Order.objects.filter(recipient=search).order_by('-created_at')
        if date_from:
            orders = orders.filter(created_at__date__gte=date_from)
        if date_to:
            orders = orders.filter(created_at__date__lte=date_to)

    return render(request, 'customer/my_orders.html', {
        'orders':    _with_status_display(orders),
        'search':    search,
        'order_ref': order_ref,
        'searched':  bool(search or order_ref),
        'date_from': date_from,
        'date_to':   date_to,
    })


def order_detail(request, order_id):
    from orders.models import GeneratedLink
    link = GeneratedLink.objects.filter(token=order_id, is_active=True).first()
    if link:
        return redirect(f'/buy/link/{order_id}/')
    order = get_object_or_404(Order, order_id=order_id)
    return render(request, 'customer/payment_success.html', {'order': order})


def track_order_view(request):
    from urllib.parse import urlencode
    from django.contrib import messages

    query = request.GET.get('query', '').strip()
    if not query:
        messages.error(request, 'Please enter your phone number or Order ID.')
        return redirect('/buy/')

    # Exact Order ID first.
    order = Order.objects.filter(order_id=query.upper()).first()
    if order:
        return redirect('/my-orders/?' + urlencode({'order': order.order_id}))

    # Otherwise treat it as a phone number. Accept +233 / 233 / spaces.
    search = query
    if all(ch.isdigit() or ch in ' +-()' for ch in query):
        digits = ''.join(ch for ch in query if ch.isdigit())
        if digits.startswith('233') and len(digits) == 12:
            digits = '0' + digits[3:]
        elif len(digits) == 9:
            digits = '0' + digits
        search = digits
    return redirect('/my-orders/?' + urlencode({'search': search}))


def request_refund_view(request, order_id):
    from django.contrib import messages

    if request.method != 'POST':
        return redirect(f'/order/{order_id}/')

    order = get_object_or_404(Order, order_id=order_id)

    if order.status not in ('waiting', 'kuntanii_unverified'):
        messages.error(request, 'Refund requests are only available for orders on the waiting list.')
        return redirect(f'/order/{order_id}/')

    if getattr(order, 'refund_requested', False):
        messages.info(request, 'A refund has already been requested for this order.')
        return redirect(f'/order/{order_id}/')

    try:
        from orders.kuntanii import report_not_received
        gb_size = float(order.gb_package.gb_size) if order.gb_package else None
        result = report_not_received(
            order_id=order.order_id,
            phone_number=order.recipient,
            data_gb=gb_size,
            description='Customer requested a refund via the waiting list button.',
        )
        Order.objects.filter(order_id=order.order_id).update(refund_requested=True)
        if result and result.get('success'):
            messages.success(request, 'Your refund request has been submitted. We will update your order status once it is processed.')
        else:
            messages.warning(request, 'Your refund request has been recorded. Our team will review it manually.')
    except Exception as e:
        print(f'[request_refund_view] Error for {order_id}: {e}')
        messages.warning(request, 'Your refund request has been recorded. Our team will review it manually.')

    return redirect(f'/order/{order_id}/')
