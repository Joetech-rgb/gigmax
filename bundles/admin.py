from django.contrib import admin

# Register your models here.
from django.contrib import admin
from .models import BundlePackage


@admin.register(BundlePackage)
class BundlePackageAdmin(admin.ModelAdmin):
    list_display = [
        'network', 'gb_size', 'is_promo', 'is_active',
        'base_price', 'customer_price', 'small_agent_price',
        'moderate_price', 'mega_price', 'super_mega_price',
        'order_position'
    ]
    list_filter   = ['network', 'is_promo', 'is_active']
    list_editable = ['order_position', 'is_active']
    ordering      = ['network', 'order_position']