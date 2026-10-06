from django.db import models


class BundlePackage(models.Model):

    class Network(models.TextChoices):
        MTN         = 'MTN',         'MTN Bundle'
        TELECEL     = 'TELECEL',     'Telecel'
        AT_NOEXPIRY = 'AT_NOEXPIRY', 'AT Non-Expiry'
        AT_EXPIRY   = 'AT_EXPIRY',   'AT Expiry'
        AFA         = 'AFA',         'AFA Bundle'

    network        = models.CharField(max_length=20, choices=Network.choices)
    gb_size        = models.DecimalField(max_digits=8, decimal_places=2)
    label          = models.CharField(max_length=50, blank=True)
    is_promo       = models.BooleanField(default=False)
    is_active      = models.BooleanField(default=True)
    order_position = models.PositiveIntegerField(default=0)

    # Pricing tiers
    base_price        = models.DecimalField(max_digits=10, decimal_places=2)
    extreme_price     = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    super_mega_price  = models.DecimalField(max_digits=10, decimal_places=2)
    mega_price        = models.DecimalField(max_digits=10, decimal_places=2)
    moderate_price    = models.DecimalField(max_digits=10, decimal_places=2)
    small_agent_price = models.DecimalField(max_digits=10, decimal_places=2)
    customer_price    = models.DecimalField(max_digits=10, decimal_places=2)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['network', 'order_position', 'gb_size']

    def __str__(self):
        return f"{self.network} — {self.gb_size}GB"

    def get_price_for_user(self, user):
        # 1. Custom role price takes priority
        if user.custom_role_id:
            custom_price = user.custom_role.get_price_for_bundle(self)
            if custom_price is not None:
                return custom_price

        # 2. Fall back to hardcoded role pricing
        role = user.role
        if role == 'extreme':
            return self.extreme_price
        elif role == 'super_mega':
            return self.super_mega_price
        elif role == 'mega':
            return self.mega_price
        elif role == 'moderate':
            return self.moderate_price
        elif role == 'small_agent':
            return self.small_agent_price
        else:
            return self.customer_price