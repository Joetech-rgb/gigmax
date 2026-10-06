from django.urls import path
from . import views

urlpatterns = [
    path('apply/', views.afa_apply_view, name='afa_apply'),
    path('pay/', views.afa_pay_view, name='afa_pay'),
    path('success/<int:pk>/', views.afa_success_view, name='afa_success'),
    path('copy/<int:pk>/', views.afa_copy_record, name='afa_copy_record'),
    path('deliver/<int:pk>/', views.afa_mark_delivered, name='afa_mark_delivered'),
]
