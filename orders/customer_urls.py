from django.urls import path
from django.views.generic import TemplateView
from customer import views

urlpatterns = [
    path('', views.home_view, name='customer_home'),
    path('dashboard/', views.home_view, name='customer_dashboard'),
    path('buy/', views.guest_buy, name='guest_buy'),
    path('buy-data/', views.guest_buy, name='buy_data'),
    path('track/', views.track_order_view, name='track_order'),
    path('buy/link/<str:token>/', views.buy_with_link, name='generated_link_buy'),

    path('my-orders/', views.my_orders_view, name='my_orders'),
    path('afa-bundle/', TemplateView.as_view(template_name='customer/afa_bundle.html'), name='afa_bundle'),

    path('checkout/<int:package_id>/', views.checkout_page, name='checkout_page'),
    path('payment/initiate/<int:package_id>/', views.initiate_payment, name='initiate_payment'),
    path('payment/verify/', views.verify_payment, name='verify_payment'),
    path('payment/success/<str:order_id>/', views.payment_success, name='payment_success'),
    path('payment/failed/', views.payment_failed, name='payment_failed'),
    path('checkout/pay-wallet/<int:package_id>/', views.pay_with_wallet, name='pay_with_wallet'),
    path('order/<str:order_id>/', views.order_detail, name='order_detail'),
    path('order/<str:order_id>/request-refund/', views.request_refund_view, name='request_refund'),
]
