

from bundles.models import BundlePackage
from django.http import JsonResponse

@agent_required
def bundles_json_view(request):
    network = request.GET.get('network', 'MTN')
    bundles = BundlePackage.objects.filter(network=network, is_active=True)
    data = [
        {
            'id': b.id,
            'gb_size': float(b.gb_size),
            'label': b.label or f'{b.gb_size}GB',
            'price': float(b.get_price_for_user(request.user)),
        }
        for b in bundles
    ]
    return JsonResponse({'bundles': data})

