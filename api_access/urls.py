from django.urls import path
from . import views

urlpatterns = [
    path('order/',                  views.create_order_api,      name='api_create_order'),
    path('orders/',                 views.list_orders_api,       name='api_list_orders'),
    path('bundles/',                views.list_bundles_api,      name='api_list_bundles'),
    path('status/<str:order_id>/',  views.order_status_api,      name='api_order_status'),
    path('callback/',               views.register_callback_api, name='api_register_callback'),
    path('balance/',                views.wallet_balance_api,    name='api_wallet_balance'),
    path('waiting/',                views.waiting_orders_api,    name='api_waiting_orders'),
    path('orders-summary/',         views.orders_summary_api,    name='api_orders_summary'),

    # No-slash variants
    path('order',                   views.create_order_api,      name='api_create_order_noslash'),
    path('orders',                  views.list_orders_api,       name='api_list_orders_noslash'),
    path('bundles',                 views.list_bundles_api,      name='api_list_bundles_noslash'),
    path('status/<str:order_id>',   views.order_status_api,      name='api_order_status_noslash'),
    path('callback',                views.register_callback_api, name='api_register_callback_noslash'),
    path('balance',                 views.wallet_balance_api,    name='api_wallet_balance_noslash'),
    path('waiting',                 views.waiting_orders_api,    name='api_waiting_orders_noslash'),
    path('orders-summary',          views.orders_summary_api,    name='api_orders_summary_noslash'),

    # DCS Elite (AT bundle reseller) inbound webhook
    path('webhooks/dcselite/',      views.dcs_webhook_view,      name='dcs_webhook'),
    path('webhooks/dcselite',       views.dcs_webhook_view,      name='dcs_webhook_noslash'),
]