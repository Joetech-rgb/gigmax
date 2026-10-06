from django.urls import path
from . import webhook

urlpatterns = [
    path('webhook/', webhook.paystack_webhook, name='paystack_webhook'),
    path('expire-pending/', webhook.expire_pending_payments, name='expire_pending_payments'),
]
