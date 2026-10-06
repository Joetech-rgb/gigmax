from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone
import uuid

class UserManager(BaseUserManager):
    def create_user(self, phone_number, password=None, **extra_fields):
        if not phone_number:
            raise ValueError('Phone number is required')
        user = self.model(phone_number=phone_number, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, phone_number, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('role', 'admin')
        extra_fields.setdefault('is_approved', True)
        return self.create_user(phone_number, password, **extra_fields)

class User(AbstractBaseUser, PermissionsMixin):
    class Role(models.TextChoices):
        CUSTOMER    = 'customer',    'Customer'
        SMALL_AGENT = 'small_agent', 'Small Agent'
        MODERATE    = 'moderate',    'Moderate Agent'
        MEGA        = 'mega',        'Mega Agent'
        SUPER_MEGA  = 'super_mega',  'Super-Mega Agent'
        EXTREME     = 'extreme',     'Extreme Agent'
        ADMIN       = 'admin',       'Admin'

    id               = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    full_name        = models.CharField(max_length=200)
    phone_number     = models.CharField(max_length=20, unique=True)
    email            = models.EmailField(blank=True, null=True)
    role             = models.CharField(max_length=20, choices=Role.choices, default=Role.CUSTOMER)

    # Status Flags
    is_approved      = models.BooleanField(default=False)
    is_active        = models.BooleanField(default=True)
    is_staff         = models.BooleanField(default=False)
    is_paused        = models.BooleanField(default=False)
    date_joined      = models.DateTimeField(default=timezone.now)

    # Branding and AFA Access
    afa_access       = models.BooleanField(default=False)
    note             = models.TextField(blank=True, null=True, default="Verified Reseller/Dealer")

    # Financials
    wallet_balance   = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    debt_balance     = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    debt_limit       = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    debt_page_access = models.BooleanField(default=False)

    # Link Profit Tracking
    link_profit_balance = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0.00,
        help_text='Available link profit balance. Goes up when profit is earned, down when withdrawn.'
    )
    total_link_profit_earned = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0.00,
        help_text='Lifetime total link profit earned. Never decremented — for display/stats only.'
    )

    # Technical
    has_api_access   = models.BooleanField(default=False)
    api_token        = models.CharField(max_length=100, blank=True, null=True, unique=True)
    callback_url     = models.URLField(blank=True, null=True, help_text='Webhook URL for order status updates')

    # Custom Role — overrides hardcoded role for pricing/permissions when set
    custom_role      = models.ForeignKey(
        'AgentRole',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='agents',
        help_text='When set, overrides the hardcoded role for pricing, max links, API and debt access.',
    )

    objects = UserManager()
    USERNAME_FIELD  = 'phone_number'
    REQUIRED_FIELDS = ['full_name']

    class Meta:
        verbose_name = 'User'
        verbose_name_plural = 'Users'
        ordering = ['-date_joined']

    def __str__(self):
        return f"{self.full_name} ({self.get_role_display()})"

    # --- Role & Permission Helpers ---

    @property
    def is_agent(self):
        return self.role in [
            self.Role.SMALL_AGENT,
            self.Role.MODERATE,
            self.Role.MEGA,
            self.Role.SUPER_MEGA,
            self.Role.EXTREME,
        ]



    @property
    def max_links(self):
        # Custom role overrides max_links
        if self.custom_role_id:
            return self.custom_role.max_links
        if self.role == self.Role.SMALL_AGENT:
            return 2
        elif self.is_agent:
            return 3
        return 0

    @property
    def can_use_debt(self):
        return self.debt_page_access and self.debt_balance < self.debt_limit

    @property
    def is_top_tier(self):
        return self.role in [
            self.Role.MODERATE,
            self.Role.MEGA,
            self.Role.SUPER_MEGA,
            self.Role.EXTREME,
        ]

    @property
    def has_afa_access_level(self):
        return self.is_agent or self.role == self.Role.ADMIN

    @property
    def wallet_only_balance(self):
        """
        The topped-up portion of the agent's wallet.
        link_profit_balance is tracked as a fully separate pool (credited
        independently in _credit_link_profit, never merged into
        wallet_balance), so wallet_balance itself already represents only
        the topped-up/admin-credited money. No subtraction needed.
        """
        return max(self.wallet_balance, 0)

    @property
    def effective_role_name(self):
        """Display name — shows custom role name if set, otherwise hardcoded role."""
        if self.custom_role_id:
            return self.custom_role.name
        return self.get_role_display()

    @property
    def effective_has_api_access(self):
        """API access — custom role overrides field value when set."""
        if self.custom_role_id:
            return self.custom_role.has_api_access
        return self.has_api_access

    @property
    def effective_debt_access(self):
        """Debt access — custom role overrides field value when set."""
        if self.custom_role_id:
            return self.custom_role.has_debt_access
        return self.debt_page_access

    @property
    def tier_style(self):
        if self.custom_role_id:
            return 'tier-custom'
        styles = {
            'small_agent': 'tier-standard',
            'moderate'   : 'tier-bronze',
            'mega'       : 'tier-silver',
            'super_mega' : 'tier-gold',
            'extreme'    : 'tier-extreme-heat',
        }
        return styles.get(self.role, 'tier-default')

    def is_active_user(self):
        from orders.models import Order
        week_ago = timezone.now() - timezone.timedelta(days=7)
        return Order.objects.filter(placed_by=self, created_at__gte=week_ago).exists()


class AgentSignupRequest(models.Model):
    class Status(models.TextChoices):
        PENDING  = 'pending',  'Pending'
        APPROVED = 'approved', 'Approved'
        DECLINED = 'declined', 'Declined'

    full_name    = models.CharField(max_length=200)
    phone_number = models.CharField(max_length=20)
    email        = models.EmailField(blank=True, null=True)
    password_raw = models.CharField(max_length=128)
    status       = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    requested_at = models.DateTimeField(auto_now_add=True)
    reviewed_at  = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-requested_at']

    def __str__(self):
        return f"{self.full_name} - {self.status}"


# ── Custom Agent Roles ────────────────────────────────────────────────────────

class AgentRole(models.Model):
    """
    Admin-created custom agent roles.
    Each role has its own price per bundle package via AgentRolePrice.
    When a User has custom_role set, it overrides the hardcoded role
    for pricing, max_links, api_access, and debt_access.
    """
    name            = models.CharField(max_length=100, unique=True, help_text='Display name e.g. Super Pro Agent')
    slug            = models.SlugField(max_length=50, unique=True, help_text='Internal key e.g. super_pro')
    description     = models.TextField(blank=True)
    max_links       = models.PositiveIntegerField(default=3, help_text='Max generated links this role can create')
    has_api_access  = models.BooleanField(default=False, help_text='Can this role use the API?')
    has_debt_access = models.BooleanField(default=False, help_text='Can this role use debt ordering?')
    is_active       = models.BooleanField(default=True)
    created_at      = models.DateTimeField(auto_now_add=True)
    created_by      = models.ForeignKey(
        'accounts.User',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='created_roles',
    )

    class Meta:
        ordering = ['name']
        verbose_name = 'Agent Role'
        verbose_name_plural = 'Agent Roles'

    def __str__(self):
        return self.name

    @property
    def agent_count(self):
        return User.objects.filter(custom_role=self).count()

    def get_price_for_bundle(self, bundle):
        """Returns custom price for this role + bundle, or None if not set."""
        price_obj = self.prices.filter(bundle=bundle).first()
        return price_obj.price if price_obj else None


class AgentRolePrice(models.Model):
    """
    Price for a specific AgentRole + BundlePackage combination.
    Mirrors the hardcoded price fields on BundlePackage but stored in DB
    so admin can set them without code changes.
    """
    role   = models.ForeignKey(AgentRole,               on_delete=models.CASCADE, related_name='prices')
    bundle = models.ForeignKey('bundles.BundlePackage', on_delete=models.CASCADE, related_name='custom_role_prices')
    price  = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        unique_together = ('role', 'bundle')
        ordering = ['bundle__network', 'bundle__gb_size']

    def __str__(self):
        return f"{self.role.name} — {self.bundle} — GHS {self.price}"