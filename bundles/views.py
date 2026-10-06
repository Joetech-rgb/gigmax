from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.http import JsonResponse
from accounts.decorators import admin_required
from .models import BundlePackage

# Non-AFA networks only
NON_AFA_NETWORKS = ['MTN', 'TELECEL', 'AT_NOEXPIRY', 'AT_EXPIRY']


def bundle_list_view(request):
    # AFA excluded from public bundle list
    packages = BundlePackage.objects.filter(
        is_active=True
    ).exclude(network='AFA')
    return render(request, 'bundles/bundle_list.html', {'packages': packages})

def get_packages_api(request):
    network = request.GET.get('network', '')
    if not network:
        return JsonResponse({'packages': []})

    from accounts.models import User
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    try:
        user = User.objects.get(api_token=token, has_api_access=True, is_active=True)
    except User.DoesNotExist:
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    packages = BundlePackage.objects.filter(
        network=network, is_active=True
    ).order_by('gb_size')

    data = {
        'packages': [
            {
                'id'     : pkg.id,
                'gb_size': int(pkg.gb_size) if pkg.gb_size == int(pkg.gb_size) else float(pkg.gb_size),
                'label'  : pkg.label or f'{pkg.gb_size}GB',
                'price'  : float(pkg.get_price_for_user(user)),
            }
            for pkg in packages
        ]
    }
    return JsonResponse(data)

@admin_required
def bundles_admin_view(request):
    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'add':
            try:
                network = request.POST.get('network')
                # Prevent AFA from being added through this form
                if network == 'AFA':
                    messages.error(request, 'AFA bundles are managed separately.')
                    return redirect('/admin-panel/bundles/')
                BundlePackage.objects.create(
                    network           = network,
                    gb_size           = float(request.POST.get('gb_size')),
                    label             = request.POST.get('label', ''),
                    base_price        = float(request.POST.get('base_price')),
                    extreme_price     = float(request.POST.get('extreme_price')),
                    super_mega_price  = float(request.POST.get('super_mega_price')),
                    mega_price        = float(request.POST.get('mega_price')),
                    moderate_price    = float(request.POST.get('moderate_price')),
                    small_agent_price = float(request.POST.get('small_agent_price')),
                    customer_price    = float(request.POST.get('customer_price')),
                    is_promo          = request.POST.get('is_promo') == 'on',
                    is_active         = True,
                )
                messages.success(request, 'Package added successfully.')
            except Exception as e:
                messages.error(request, f'Error adding package: {e}')

        elif action == 'edit':
            pkg = get_object_or_404(BundlePackage, pk=request.POST.get('package_id'))
            try:
                network = request.POST.get('network')
                # Prevent changing to AFA through this form
                if network == 'AFA':
                    messages.error(request, 'AFA bundles are managed separately.')
                    return redirect('/admin-panel/bundles/')
                pkg.network           = network
                pkg.gb_size           = float(request.POST.get('gb_size'))
                pkg.label             = request.POST.get('label', '')
                pkg.base_price        = float(request.POST.get('base_price'))
                pkg.extreme_price     = float(request.POST.get('extreme_price'))
                pkg.super_mega_price  = float(request.POST.get('super_mega_price'))
                pkg.mega_price        = float(request.POST.get('mega_price'))
                pkg.moderate_price    = float(request.POST.get('moderate_price'))
                pkg.small_agent_price = float(request.POST.get('small_agent_price'))
                pkg.customer_price    = float(request.POST.get('customer_price'))
                pkg.is_promo          = request.POST.get('is_promo') == 'on'
                pkg.save()
                messages.success(request, f'{pkg.gb_size}GB package updated.')
            except Exception as e:
                messages.error(request, f'Error updating package: {e}')

        elif action == 'delete':
            pkg = get_object_or_404(BundlePackage, pk=request.POST.get('package_id'))
            name = f"{pkg.gb_size}GB {pkg.network}"
            pkg.delete()
            messages.success(request, f'{name} deleted.')

        return redirect('/admin-panel/bundles/')

    # Admin sees only NON-AFA networks — AFA has its own page
    grouped = {}
    for network in NON_AFA_NETWORKS:
        pkgs = BundlePackage.objects.filter(
            network=network, is_active=True
        ).order_by('gb_size')
        if pkgs.exists():
            grouped[network] = pkgs

    return render(request, 'admin_panel/bundle_packages.html', {
        'grouped': grouped,
        'networks': NON_AFA_NETWORKS,
    })