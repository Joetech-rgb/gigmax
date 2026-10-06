from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.utils import timezone
from bundles.models import BundlePackage
from .models import Order, GeneratedLink, GeneratedLinkPrice
from .utils import place_order, validate_ghana_number
from django.db.models import Case, When, IntegerField

def home_view(request):
    return render(request, 'customer/home.html')


def buy_data_view(request):
    network  = request.GET.get('network', '')
    packages = []
    NETWORKS = ['MTN', 'TELECEL', 'AT_NOEXPIRY', 'AT_EXPIRY', 'AFA']

    if network in NETWORKS:
        packages = BundlePackage.objects.filter(
            network=network, is_active=True
        ).order_by('order_position', 'gb_size')

    promo_packages = BundlePackage.objects.filter(is_promo=True, is_active=True)

    return render(request, 'customer/buy_data.html', {
        'networks'       : NETWORKS,
        'network'        : network,
        'packages'       : packages,
        'promo_packages' : promo_packages,
    })


def confirm_order_view(request):
    """Step 3 — Confirm or Cancel before placing order."""
    if request.method == 'POST':
        package_id = request.POST.get('package_id')
        recipient  = request.POST.get('recipient')

        if not validate_ghana_number(recipient):
            messages.error(request, 'Invalid recipient number.')
            return redirect('/buy/')

        package = get_object_or_404(BundlePackage, id=package_id, is_active=True)

        return render(request, 'customer/confirm_order.html', {
            'package'  : package,
            'recipient': recipient,
            'price'    : package.customer_price,
        })

    return redirect('/buy/')

def place_order_view(request):
    if request.method == 'POST':
        package_id = request.POST.get('package_id')
        recipient  = request.POST.get('recipient')
        package    = get_object_or_404(BundlePackage, id=package_id)

        order = place_order(
            package   = package,
            recipient = recipient,
            price     = package.customer_price,
            source    = Order.Source.CUSTOMER,
            placed_by = request.user if request.user.is_authenticated else None,
        )

        # Save to session for non-logged-in customers
        if not request.user.is_authenticated:
            order_ids = request.session.get('order_ids', [])
            order_ids.append(order.order_id)
            request.session['order_ids'] = order_ids

        messages.success(request, f'Order {order.order_id} placed! Your bundle will be delivered shortly.')
        return redirect('/orders/')

    return redirect('/buy/')

def my_orders_view(request):
    from django.db.models import Q
    orders    = Order.objects.none()
    search    = request.GET.get('search', '')
    date_from = request.GET.get('date_from', '')
    date_to   = request.GET.get('date_to', '')

    if request.user.is_authenticated:
        orders = Order.objects.filter(placed_by=request.user).order_by('-created_at')[:100]
    else:
        # Show orders by session if not logged in
        session_orders = request.session.get('order_ids', [])
        orders = Order.objects.filter(order_id__in=session_orders)

    if search:
        orders = orders.filter(recipient__icontains=search)
    if date_from:
        orders = orders.filter(created_at__date__gte=date_from)
    if date_to:
        orders = orders.filter(created_at__date__lte=date_to)

    return render(request, 'customer/my_orders.html', {
        'orders'   : orders,
        'search'   : search,
        'date_from': date_from,
        'date_to'  : date_to,
    })

def link_order_view(request, token):
    link = get_object_or_404(GeneratedLink, token=token, is_active=True)

    all_packages = GeneratedLinkPrice.objects.filter(
        link=link
    ).select_related('bundle').order_by(
        Case(
            When(bundle__network='MTN',         then=0),
            When(bundle__network='TELECEL',     then=1),
            When(bundle__network='AT_NOEXPIRY', then=2),
            When(bundle__network='AT_EXPIRY',   then=3),
            default=4, output_field=IntegerField()
        ),
        'bundle__gb_size'
    )

    # Build network list from available packages
    seen = []
    networks = []
    for lp in all_packages:
        key = lp.bundle.network
        if key not in seen:
            seen.append(key)
            networks.append({
                'key'  : key,
                'label': lp.bundle.get_network_display(),
            })

    # Agent branding
    agent_name = link.agent.full_name or 'Data Shop'
    initials   = ''.join([w[0].upper() for w in agent_name.split()[:2]])
    shop_name  = link.label or f"{agent_name}'s Data Shop"

    selected_network = request.GET.get('network', '')

    if request.method == 'POST':
        bundle_id = request.POST.get('package_id')
        recipient = request.POST.get('phone', '').strip()

        if not validate_ghana_number(recipient):
            messages.error(request, 'Invalid recipient number.')
            return redirect(f'/order/{token}/')

        link_price = get_object_or_404(GeneratedLinkPrice, link=link, bundle_id=bundle_id)

        order = place_order(
            package        = link_price.bundle,
            recipient      = recipient,
            price          = link_price.retail_price,
            source         = Order.Source.GENERATED_LINK,
            placed_by      = None,
            generated_link = link,
        )

        return render(request, 'customer/link_order.html', {
            'link'            : link,
            'success'         : True,
            'order'           : order,
            'shop_name'       : shop_name,
            'initials'        : initials,
            'accent'          : '#1a4fd6',
            'accent_dark'     : '#0f3499',
            'networks'        : networks,
            'all_packages'    : all_packages,
            'selected_network': selected_network,
        })

    return render(request, 'customer/link_order.html', {
        'link'            : link,
        'all_packages'    : all_packages,
        'networks'        : networks,
        'selected_network': selected_network,
        'shop_name'       : shop_name,
        'initials'        : initials,
        'accent'          : '#1a4fd6',
        'accent_dark'     : '#0f3499',
    })

def pay_with_wallet_view(request, package_id):
    from django.contrib.auth.decorators import login_required
    if not request.user.is_authenticated:
        return redirect('/accounts/login/')

    if request.method == 'POST':
        package   = get_object_or_404(BundlePackage, id=package_id, is_active=True)
        phone     = request.POST.get('phone')
        price     = package.customer_price

        if not validate_ghana_number(phone):
            messages.error(request, 'Invalid recipient number.')
            return redirect(f'/checkout/{package_id}/?phone={phone}')

        if request.user.wallet_balance < price:
            messages.error(request, 'Insufficient wallet balance.')
            return redirect(f'/checkout/{package_id}/?phone={phone}')

        # Deduct wallet
        request.user.wallet_balance = float(request.user.wallet_balance) - float(price)
        request.user.save(update_fields=['wallet_balance'])

        # Place order
        order = place_order(
            package   = package,
            recipient = phone,
            price     = price,
            source    = Order.Source.CUSTOMER,
            placed_by = request.user,
        )
        order.payment_status = 'paid'
        order.paid_at = timezone.now()
        order.save(update_fields=['payment_status', 'paid_at'])

        messages.success(request, f'Order {order.order_id} placed! GHS {price} deducted from wallet.')
        return redirect('/orders/')

    return redirect('/')