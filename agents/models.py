from django.db import models
from django.conf import settings
from django.utils import timezone

class AgentProfile(models.Model):
    AGENT_TIERS = [
        ('small', 'Small Agent'),
        ('moderate', 'Moderate Agent'),
        ('mega', 'Mega Agent'),
        ('super_mega', 'Super-Mega Agent'),
        ('extreme', 'Extreme Agent'),
    ]
    
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='agent_profile')
    full_name = models.CharField(max_length=100)
    phone = models.CharField(max_length=15)
    email = models.EmailField()
    agent_tier = models.CharField(max_length=20, choices=AGENT_TIERS, default='small')
    has_afa_access = models.BooleanField(default=False)
    
    is_approved = models.BooleanField(default=False)
    approved_at = models.DateTimeField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    is_paused = models.BooleanField(default=False)
    
    total_debt = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    debt_limit = models.DecimalField(max_digits=12, decimal_places=2, default=1000)
    is_debtor = models.BooleanField(default=False)
    
    api_access = models.BooleanField(default=False)
    api_key = models.CharField(max_length=100, blank=True, unique=True, null=True)
    website_url = models.CharField(max_length=255, blank=True)
    
    wallet_balance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    max_links = models.IntegerField(default=1)
    total_profit = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    total_orders = models.IntegerField(default=0)
    total_gbs = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_spent = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.full_name} - {self.get_agent_tier_display()}"
    
    def can_place_order(self):
        if not self.is_active or self.is_paused:
            return False, "Account is paused"
        if not self.is_approved:
            return False, "Account not approved"
        if self.is_debtor and self.total_debt >= self.debt_limit:
            return False, f"Debt limit exceeded: GHS{self.total_debt}"
        return True, "OK"
    
    def add_debt(self, amount):
        self.total_debt += amount
        self.save()
        return self.total_debt
    
    def pay_debt(self, amount):
        self.total_debt = max(0, self.total_debt - amount)
        self.save()
        return self.total_debt
    
    def add_profit(self, amount):
        self.total_profit += amount
        self.save()
        return self.total_profit

class DebtRecord(models.Model):
    agent = models.ForeignKey(AgentProfile, on_delete=models.CASCADE, related_name='debts')
    order = models.ForeignKey('orders.Order', on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(blank=True, null=True)
    
    def __str__(self):
        return f"{self.agent.full_name} - GHS{self.amount}"

class PaymentRecord(models.Model):
    agent = models.ForeignKey(AgentProfile, on_delete=models.CASCADE, related_name='payments')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_method = models.CharField(max_length=50)
    reference = models.CharField(max_length=100, unique=True)
    notes = models.TextField(blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.agent.full_name} - GHS{self.amount}"

class AgentSignupRequest(models.Model):
    full_name = models.CharField(max_length=100)
    phone = models.CharField(max_length=15)
    email = models.EmailField()
    password = models.CharField(max_length=128)
    status = models.CharField(max_length=20, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.full_name} - {self.status}"
