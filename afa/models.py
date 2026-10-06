from django.db import models
from django.conf import settings
from django.core.validators import RegexValidator

class AFAApplication(models.Model):
    STATUS_PENDING    = 'pending'
    STATUS_PROCESSING = 'processing'
    STATUS_DELIVERED  = 'delivered'
    STATUS_SUBMITTED  = 'submitted'
    STATUS_CHOICES = [
        (STATUS_PENDING,    'Pending'),
        (STATUS_SUBMITTED,  'Submitted'),
        (STATUS_PROCESSING, 'Processing'),
        (STATUS_DELIVERED,  'Delivered'),
    ]
    full_name         = models.CharField(max_length=200)
    phone_number      = models.CharField(max_length=20)
    ghana_card        = models.CharField(max_length=20, validators=[RegexValidator(regex=r'^GHA-\d{9}-\d$', message='Ghana Card must be in format GHA-#########-# (e.g. GHA-716197864-5)')])
    date_of_birth     = models.DateField()
    location          = models.CharField(max_length=200)
    region            = models.CharField(max_length=100)
    agent             = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='afa_applications')
    status            = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    payment_reference = models.CharField(max_length=100, blank=True, null=True, unique=True)  # ✅ added
    created_at        = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.full_name} - {self.phone_number}"


class AFAPrice(models.Model):
    CUSTOMER_BASE = 'customer_base'
    AGENT_BASE    = 'agent_base'
    ROLE_CHOICES  = [
        (CUSTOMER_BASE, 'Customer & Base Agent'),
        (AGENT_BASE,    'All Agent Base'),
    ]
    role       = models.CharField(max_length=20, choices=ROLE_CHOICES, unique=True)
    price      = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"AFA Price - {self.get_role_display()}: GHS {self.price}"