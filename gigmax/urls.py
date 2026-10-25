from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from customer import views
from django.views.generic import TemplateView

urlpatterns = [
    path('', views.home_view, name='home'),
    path('buy/promo/<int:package_id>/', views.buy_promo, name='buy_promo'),
    path('django-admin/', admin.site.urls),
    path('accounts/', include('accounts.urls')),
    path('', include('orders.customer_urls')),
    path('agent/', include('orders.agent_urls')),
    path('admin-panel/', include('admin_panel.urls', namespace='admin_panel')),
    path('afa/', include('afa.urls')),
    path('wallet/', include('wallet.urls')),
    path('api/v1/', include('api_access.urls')),
    path('bundles/', include('bundles.urls')),
    path('terms/', TemplateView.as_view(template_name='rules.html'), name='terms'),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
