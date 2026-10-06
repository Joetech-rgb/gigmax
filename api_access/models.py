from django.db import models
from django.conf import settings
from django.utils import timezone
import secrets


def generate_api_key():
    return secrets.token_hex(32)


class APIKey(models.Model):
    agent = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='api_key_obj')
    key = models.CharField(max_length=100, unique=True, default=generate_api_key)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"{self.agent.phone_number} - {self.key[:16]}..."

    def regenerate(self):
        self.key = secrets.token_hex(32)
        self.save()
        return self.key


class APIRequest(models.Model):
    class Status(models.TextChoices):
        PENDING  = 'pending',  'Pending'
        APPROVED = 'approved', 'Approved'
        DENIED   = 'denied',   'Denied'

    agent       = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='api_requests')
    website_url = models.URLField(blank=True, null=True)
    reason      = models.TextField(blank=True)
    status      = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='reviewed_api_requests')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.agent.full_name} — {self.status}"

    def approve(self, reviewer):
        self.status      = self.Status.APPROVED
        self.reviewed_by = reviewer
        self.reviewed_at = timezone.now()
        self.api_key     = secrets.token_hex(32)
        self.save()
        self.agent.has_api_access = True
        self.agent.api_token      = self.api_key
        self.agent.save(update_fields=['has_api_access', 'api_token'])
        return self.api_key

    def deny(self, reviewer):
        self.status      = self.Status.DENIED
        self.reviewed_by = reviewer
        self.reviewed_at = timezone.now()
        self.save()
        self.agent.has_api_access = False
        self.agent.save(update_fields=['has_api_access'])