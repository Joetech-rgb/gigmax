from django.db import models
from django.conf import settings

class Wallet(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='wallet')
    balance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.user.phone_number} - GHS{self.balance}"
    
    def add_funds(self, amount):
        self.balance += amount
        self.save()
        self.user.wallet_balance = self.balance
        self.user.save(update_fields=['wallet_balance'])
        return self.balance
    
    def deduct_funds(self, amount):
        if self.balance >= amount:
            self.balance -= amount
            self.save()
            self.user.wallet_balance = self.balance
            self.user.save(update_fields=['wallet_balance'])
            return True
        return False

class WalletTransaction(models.Model):
    TRANSACTION_TYPES = [
        ('credit', 'Credit'),
        ('debit', 'Debit'),
        ('withdrawal', 'Withdrawal'),
    ]
    
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]
    
    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name='transactions')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    reference = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    debt_before = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    debt_after = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    wallet_before = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    wallet_after = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    order_source = models.CharField(max_length=30, blank=True, default='')
    
    def __str__(self):
        return f"{self.wallet.user.phone_number} - {self.transaction_type} - GHS{self.amount}"

class WithdrawalRequest(models.Model):
    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name='withdrawals')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    phone_number = models.CharField(max_length=15)
    network = models.CharField(max_length=20)
    status = models.CharField(max_length=20, default='pending')
    reference = models.CharField(max_length=100, unique=True)
    processed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(blank=True, null=True)
    
    def __str__(self):
        return f"{self.wallet.user.phone_number} - GHS{self.amount} - {self.status}"


