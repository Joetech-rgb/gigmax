from django.urls import path
from . import views

app_name = 'bundles'

urlpatterns = [
    path('api/packages/', views.get_packages_api, name='packages_api'),
    path('', views.bundle_list_view, name='list'),
]