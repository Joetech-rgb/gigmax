from django.db import models
from django.conf import settings
from django.utils import timezone


class FeeConfiguration(models.Model):
    paystack_fee_percent = models.DecimalField(max_digits=5, decimal_places=2, default=1.5)
    admin_share_percent = models.DecimalField(max_digits=5, decimal_places=2, default=50)
    agent_share_percent = models.DecimalField(max_digits=5, decimal_places=2, default=50)
    agent_overrides = models.JSONField(default=dict, blank=True)
    operational_cost_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)
    effective_from = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        app_label = 'admin_panel'

    @classmethod
    def get_current(cls):
        config, created = cls.objects.get_or_create(id=1)
        return config

    def get_agent_share(self, agent_id):
        agent_id_str = str(agent_id)
        if agent_id_str in self.agent_overrides:
            return self.agent_overrides[agent_id_str].get('share', self.agent_share_percent)
        return self.agent_share_percent

    def calculate_breakdown(self, amount, agent_id=None):
        paystack_fee = amount * (self.paystack_fee_percent / 100)
        if agent_id:
            agent_share = self.get_agent_share(agent_id)
            admin_share = 100 - agent_share
        else:
            agent_share = self.agent_share_percent
            admin_share = self.admin_share_percent
        admin_gets = paystack_fee * (admin_share / 100)
        agent_gets = paystack_fee * (agent_share / 100)
        operational = amount * (self.operational_cost_percent / 100)
        return {
            'paystack_fee': paystack_fee,
            'admin_earns': admin_gets,
            'agent_earns': agent_gets,
            'operational_cost': operational,
            'net_profit': admin_gets - operational
        }

    def __str__(self):
        return f"Fees: Paystack {self.paystack_fee_percent}% | Admin {self.admin_share_percent}% | Agent {self.agent_share_percent}%"


class MTNDailyRevenue(models.Model):
    date                = models.DateField(unique=True)
    api_base_revenue    = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    manual_base_revenue = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_debt          = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    notes               = models.TextField(blank=True)
    created_at          = models.DateTimeField(auto_now_add=True)
    updated_at          = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date']

    @property
    def total_base_revenue(self):
        return self.api_base_revenue + self.manual_base_revenue

    @property
    def daily_profit(self):
        return self.total_base_revenue - self.total_debt

    def __str__(self):
        return f"MTN Revenue {self.date} | GHS {self.total_base_revenue}"


class DebtPayment(models.Model):
    agent          = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='debt_payments')
    amount         = models.DecimalField(max_digits=12, decimal_places=2)
    note           = models.TextField(blank=True)
    cleared_by     = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='debt_clearances')
    created_at     = models.DateTimeField(auto_now_add=True)
    # Snapshots so every clearance row is self-contained proof — no need to
    # recompute from full history order to know what was owed at this exact
    # moment. Null on rows created before this field existed.
    balance_before = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    balance_after  = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.agent.full_name} — GHS {self.amount} cleared on {self.created_at.date()}"


class BulkCopySession(models.Model):
    date        = models.DateField(default=timezone.now)
    network     = models.CharField(max_length=20)
    copy_number = models.PositiveIntegerField(default=1)
    order_ids   = models.JSONField(default=list)
    created_by  = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='bulk_sessions')
    created_at  = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']
        unique_together = [('network', 'copy_number')]

    def __str__(self):
        return f"Bulk Copy {self.copy_number} — {self.network} — {self.date}"


class GatiSettings(models.Model):
    is_enabled = models.BooleanField(default=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Gati Settings'

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(id=1)
        return obj

    def __str__(self):
        return f"Gati {'Enabled' if self.is_enabled else 'Disabled'}"


class ATSettings(models.Model):
    is_enabled = models.BooleanField(default=False)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'AT Settings'

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(id=1)
        return obj

    def __str__(self):
        return f"AT {'Enabled' if self.is_enabled else 'Disabled'}"


class PaymentGatewaySettings(models.Model):
    """
    Controls which payment gateway is active for top-ups and payouts.
    Same pattern as KuntaniiSettings/GatiSettings — a single row, admin
    toggles which gateway handles NEW transactions. Existing in-flight
    transactions under the previous gateway are unaffected, since each
    gateway keeps its own permanent webhook endpoint regardless of which
    one is currently "active".
    """
    GATEWAY_CHOICES = [
        ('paystack', 'Paystack'),
        ('moolre', 'Moolre'),
    ]
    active_gateway = models.CharField(max_length=20, choices=GATEWAY_CHOICES, default='paystack')
    updated_by     = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    updated_at     = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Payment Gateway Settings'

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(id=1)
        return obj

    def __str__(self):
        return f"Active gateway: {self.get_active_gateway_display()}"


class KuntaniiSettings(models.Model):
    is_enabled = models.BooleanField(default=False)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_signature_warning = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Kuntanii Settings'

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(id=1)
        return obj

    def __str__(self):
        return f"Kuntanii {'Enabled' if self.is_enabled else 'Disabled'}"


class Promo(models.Model):
    network    = models.CharField(max_length=20)
    gb         = models.DecimalField(max_digits=6, decimal_places=2, help_text="GB size on promo")
    price      = models.DecimalField(max_digits=10, decimal_places=2, help_text="Promo price in GHS")
    label      = models.CharField(max_length=100, blank=True, help_text="e.g. midnight, weekend")
    is_active  = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if self.is_active:
            Promo.objects.exclude(pk=self.pk).update(is_active=False)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Promo: {self.network} {self.gb}GB @ GHS {self.price} ({'active' if self.is_active else 'inactive'})"


class AdminEarnings(models.Model):
    date            = models.DateField(unique=True)
    order_revenue   = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    service_charges = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_earnings  = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    withdrawn       = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    updated_at      = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date']

    @classmethod
    def get_total_available(cls):
        from django.db.models import Sum
        totals = cls.objects.aggregate(
            earned=Sum('total_earnings'),
            withdrawn=Sum('withdrawn')
        )
        earned    = totals['earned'] or 0
        withdrawn = totals['withdrawn'] or 0
        return float(earned) - float(withdrawn)

    def __str__(self):
        return f"Admin Earnings {self.date} — GHS {self.total_earnings}"


class AdminWithdrawal(models.Model):
    STATUS_CHOICES = [
        ('pending',   'Pending'),
        ('completed', 'Completed'),
        ('failed',    'Failed'),
    ]
    amount                 = models.DecimalField(max_digits=12, decimal_places=2)
    phone_number           = models.CharField(max_length=20)
    network                = models.CharField(max_length=20)
    reference              = models.CharField(max_length=100, unique=True)
    recipient_code         = models.CharField(max_length=100, blank=True)
    status                 = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    paystack_transfer_code = models.CharField(max_length=100, blank=True)
    note                   = models.TextField(blank=True)
    created_at             = models.DateTimeField(auto_now_add=True)
    processed_at           = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Admin Withdrawal GHS {self.amount} to {self.network} {self.phone_number} — {self.status}"


class WalletTopup(models.Model):
    user       = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='wallet_topups')
    amount     = models.DecimalField(max_digits=12, decimal_places=2)
    note       = models.TextField(blank=True)
    added_by   = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='topups_made')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Topup GHS {self.amount} to {self.user.full_name} on {self.created_at.date()}"


class WalletTopupCorrection(models.Model):
    original_topup = models.ForeignKey('WalletTopup', on_delete=models.CASCADE, related_name='corrections')
    corrected_by   = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    wrong_amount   = models.DecimalField(max_digits=10, decimal_places=2)
    correct_amount = models.DecimalField(max_digits=10, decimal_places=2)
    difference     = models.DecimalField(max_digits=10, decimal_places=2)
    note           = models.TextField(blank=True)
    created_at     = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Correction on TopUp #{self.original_topup_id} | {self.wrong_amount} → {self.correct_amount}"


class Notice(models.Model):
    PRIORITY_CHOICES = [
        ('info',    'ℹ️  Info'),
        ('success', '✅ Success'),
        ('warning', '⚠️  Warning'),
        ('urgent',  '🚨 Urgent'),
    ]
    title      = models.CharField(max_length=150)
    message    = models.TextField()
    priority   = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default='info')
    is_active  = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        'accounts.User',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='notices_created',
    )
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title

    @property
    def is_visible(self):
        if not self.is_active:
            return False
        if self.expires_at and timezone.now() > self.expires_at:
            return False
        return True


class AdminEarning(models.Model):
    """One row per paid order — stores the margin admin keeps."""
    order      = models.OneToOneField(
                     'orders.Order',
                     on_delete=models.CASCADE,
                     related_name='admin_earning',
                 )
    amount     = models.DecimalField(max_digits=10, decimal_places=2)
    note       = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.order.order_id} → GHS {self.amount}"


class SiteSettings(models.Model):
    is_open     = models.BooleanField(default=True)
    mtn_enabled = models.BooleanField(default=True)
    closed_by   = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Site Settings'

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(id=1)
        return obj

    def __str__(self):
        return f"Site {'Open' if self.is_open else 'Closed'} | MTN {'Enabled' if self.mtn_enabled else 'Paused'}"

class OrderStatusLog(models.Model):
    order_id = models.CharField(max_length=64, db_index=True)
    old_status = models.CharField(max_length=32)
    new_status = models.CharField(max_length=32)
    changed_by = models.CharField(max_length=150, blank=True, null=True)
    changed_at = models.DateTimeField(auto_now_add=True)
    source = models.CharField(max_length=100, default='update_order_status_view')

    def __str__(self):
        return f"{self.order_id}: {self.old_status} -> {self.new_status} @ {self.changed_at}"


class AutoRefundSettings(models.Model):
    # Single row. When OFF, orders that would be refunded automatically stay in the review queue.
    is_enabled = models.BooleanField(default=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Auto Refund Settings'

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(id=1)
        return obj

    def __str__(self):
        return 'Auto Refund ' + ('ON' if self.is_enabled else 'OFF')


class SettingChangeLog(models.Model):
    setting = models.CharField(max_length=60)
    old_value = models.CharField(max_length=60, blank=True)
    new_value = models.CharField(max_length=60, blank=True)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-changed_at']

    def __str__(self):
        return '%s: %s -> %s' % (self.setting, self.old_value, self.new_value)
