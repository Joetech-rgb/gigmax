from django.urls import path
from . import views

urlpatterns = [
    path('',                                views.wallet_view,               name='wallet'),
    path('load/',                           views.load_wallet_view,          name='load_wallet'),
    path('withdraw/',                       views.withdrawal_request_view,   name='withdrawal_request'),
    path('withdraw/link-profit/',           views.withdraw_link_profit_view, name='withdraw_link_profit'),
    path('paystack/verify/',                views.paystack_verify_view,      name='paystack_verify'),
    path('paystack/reverify/',               views.paystack_reverify_view,    name='paystack_reverify'),
    path('paystack/webhook/',               views.paystack_webhook_view,     name='paystack_webhook'),
    path('kuntanii/webhook/',               views.kuntanii_webhook_view,     name='kuntanii_webhook'),
    path('approve/<int:withdrawal_id>/',    views.approve_withdrawal_view,   name='approve_withdrawal'),
    path('reject/<int:withdrawal_id>/',     views.reject_withdrawal_view,    name='reject_withdrawal'),
]