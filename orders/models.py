from django.db import models
from django.conf import settings
import random, string


class Order(models.Model):

    class Network(models.TextChoices):
        MTN         = 'MTN',         'MTN Bundle'
        TELECEL     = 'TELECEL',     'Telecel'
        AT_NOEXPIRY = 'AT_NOEXPIRY', 'AT Non-Expiry'
        AT_EXPIRY   = 'AT_EXPIRY',   'AT Expiry'
        AFA         = 'AFA',         'AFA Bundle'

    class Status(models.TextChoices):
        PENDING         = 'pending',         'Pending'
        PENDING_PAYMENT = 'pending_payment', 'Pending Payment'
        PROCESSING      = 'processing',      'Processing'
        WAITING         = 'waiting',         'Waiting'
        KUNTANII_UNVERIFIED = 'kuntanii_unverified', 'Kuntanii Unverified'
        REFUNDED        = 'refunded',        'Refunded'
        DELIVERED       = 'delivered',       'Delivered'
        FAILED          = 'failed',          'Failed'

    class Source(models.TextChoices):
        CUSTOMER       = 'customer',       'Customer'
        AGENT_SINGLE   = 'agent_single',   'Agent Single'
        AGENT_BULK     = 'agent_bulk',     'Agent Bulk'
        AGENT_EXCEL    = 'agent_excel',    'Agent Excel'
        GENERATED_LINK = 'generated_link', 'Generated Link'
        API            = 'api',            'API'

    order_id       = models.CharField(max_length=20, unique=True, editable=False)
    via_api        = models.BooleanField(default=False)
    network        = models.CharField(max_length=20, choices=Network.choices)
    recipient      = models.CharField(max_length=20)
    gb_package     = models.ForeignKey(
                        'bundles.BundlePackage',
                        on_delete=models.SET_NULL,
                        null=True,
                        blank=True,
                     )
    status         = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    source         = models.CharField(max_length=20, choices=Source.choices, default=Source.CUSTOMER)
    placed_by      = models.ForeignKey(
                        settings.AUTH_USER_MODEL,
                        on_delete=models.SET_NULL,
                        null=True, blank=True,
                        related_name='orders_placed'
                     )
    price_paid     = models.DecimalField(max_digits=10, decimal_places=2)

    # Gati reference (MTN only)
    gati_order_id  = models.CharField(max_length=100, blank=True, null=True)
    kuntanii_reference = models.CharField(max_length=100, blank=True, null=True)
    dcs_order_reference = models.CharField(max_length=100, blank=True, null=True)
    kuntanii_unverified_reason = models.TextField(blank=True, null=True)
    kuntanii_unverified_at     = models.DateTimeField(blank=True, null=True)
    refund_requested   = models.BooleanField(default=False)
    refund_paid        = models.BooleanField(default=False)
    refund_paid_at     = models.DateTimeField(blank=True, null=True)
    refund_credited     = models.BooleanField(default=False)
    paystack_fee            = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    customer_refund_paid    = models.BooleanField(default=False)
    customer_refund_paid_at = models.DateTimeField(null=True, blank=True)
    agent_profit_reversed    = models.BooleanField(default=False)
    agent_profit_reversed_at = models.DateTimeField(null=True, blank=True)

    # Generated link reference
    generated_link = models.ForeignKey(
                        'GeneratedLink',
                        on_delete=models.SET_NULL,
                        null=True, blank=True,
                        related_name='orders'
                     )

    # Bulk / Excel batch grouping
    batch_id       = models.CharField(max_length=50, blank=True, null=True)

    # Blocked recipient — payment taken, Gati skipped, agent sees delivered
    is_blocked_order = models.BooleanField(default=False)

    # Queue system
    queue_number   = models.PositiveIntegerField(null=True, blank=True)

    # Payment fields
    payment_reference  = models.CharField(max_length=100, blank=True, null=True)
    payment_status     = models.CharField(max_length=20, default='pending')
    paid_at            = models.DateTimeField(blank=True, null=True)
    payment_expires_at = models.DateTimeField(blank=True, null=True)
    is_guest           = models.BooleanField(default=False)
    session_key        = models.CharField(max_length=40, blank=True, null=True)

    created_at     = models.DateTimeField(auto_now_add=True)
    updated_at     = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['payment_reference'],
                condition=~models.Q(payment_reference='') & models.Q(payment_reference__isnull=False),
                name='uniq_order_payment_reference',
            ),
        ]
        indexes = [
            models.Index(fields=['network', 'created_at']),
            models.Index(fields=['status', 'network']),
            models.Index(fields=['created_at']),
            models.Index(fields=['via_api', 'network']),
            models.Index(fields=['network', 'status', 'created_at']),
        ]

    def save(self, *args, **kwargs):
        if not self.order_id:
            self.order_id = self._generate_order_id()
        super().save(*args, **kwargs)

    def _generate_order_id(self):
        suffix = ''.join(random.choices(string.digits, k=8))
        return f"GMX{suffix}"

    def __str__(self):
        return f"{self.order_id} | {self.network} | {self.recipient} | {self.status}"

    @property
    def is_mtn(self):
        return self.network == self.Network.MTN

    @property
    def is_payment_expired(self):
        from django.utils import timezone
        if self.payment_expires_at and self.payment_status == 'pending':
            return timezone.now() > self.payment_expires_at
        return False


class GeneratedLink(models.Model):
    agent        = models.ForeignKey(
                       settings.AUTH_USER_MODEL,
                       on_delete=models.CASCADE,
                       related_name='generated_links'
                   )
    token        = models.CharField(max_length=64, unique=True)
    label        = models.CharField(max_length=100, blank=True, default='')
    is_active    = models.BooleanField(default=True)
    created_at   = models.DateTimeField(auto_now_add=True)
    daily_profit = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    class Meta:
        ordering = ['agent', 'created_at']

    def __str__(self):
        return f"{self.agent.full_name} — {self.label or self.token[:8]}"

    @property
    def total_orders(self):
        return self.orders.count()

    @property
    def total_profit(self):
        profit = 0
        orders = Order.objects.filter(
            generated_link=self,
            payment_status='paid',
        ).select_related('gb_package')

        for o in orders:
            if not o.gb_package:
                continue
            link_price = self.prices.filter(bundle=o.gb_package).first()
            if link_price:
                profit += float(link_price.retail_price) - float(o.gb_package.base_price)

        return round(profit, 2)

    @property
    def total_revenue(self):
        from django.db.models import Sum
        return self.orders.aggregate(total=Sum('price_paid'))['total'] or 0


class GeneratedLinkPrice(models.Model):
    link         = models.ForeignKey(GeneratedLink, on_delete=models.CASCADE, related_name='prices')
    bundle       = models.ForeignKey('bundles.BundlePackage', on_delete=models.CASCADE)
    retail_price = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        unique_together = ['link', 'bundle']


class BlockedRecipient(models.Model):
    """
    Numbers blocked from receiving bundles.
    Irrespective of source (API, bulk, single, excel, manual),
    any order for these numbers will:
    - Take payment + follow debt logic normally
    - Show 'delivered' to agent
    - NEVER dispatch to Gati
    - Send SMS to recipient about double delivery
    """
    recipient  = models.CharField(max_length=20, unique=True)
    reason     = models.TextField(default='Double delivery on 3rd May 2026')
    added_by   = models.ForeignKey(
                     settings.AUTH_USER_MODEL,
                     on_delete=models.SET_NULL,
                     null=True, blank=True
                 )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['recipient']

    def __str__(self):
        return self.recipient

    @property
    def total_recovered(self):
        from django.db.models import Sum
        return Order.objects.filter(
            recipient=self.recipient,
            is_blocked_order=True,
        ).aggregate(t=Sum('price_paid'))['t'] or 0

    @property
    def total_orders(self):
        return Order.objects.filter(
            recipient=self.recipient,
            is_blocked_order=True,
        ).count()