from django.contrib import admin

# Register your models here.
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User, AgentSignupRequest


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display  = ['full_name', 'phone_number', 'role', 'is_approved', 'is_paused', 'date_joined']
    list_filter   = ['role', 'is_approved', 'is_paused', 'has_api_access']
    search_fields = ['full_name', 'phone_number', 'email']
    ordering      = ['-date_joined']

    fieldsets = (
        (None,           {'fields': ('phone_number', 'password')}),
        ('Personal',     {'fields': ('full_name', 'email')}),
        ('Role & Access',{'fields': ('role', 'is_approved', 'is_paused', 'has_api_access', 'api_token')}),
        ('Financials',   {'fields': ('wallet_balance', 'debt_balance')}),
        ('Permissions',  {'fields': ('is_active', 'is_staff', 'is_superuser')}),
    )

    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields' : ('phone_number', 'full_name', 'role', 'password1', 'password2'),
        }),
    )


@admin.register(AgentSignupRequest)
class AgentSignupRequestAdmin(admin.ModelAdmin):
    list_display  = ['full_name', 'phone_number', 'status', 'requested_at']
    list_filter   = ['status']
    search_fields = ['full_name', 'phone_number']