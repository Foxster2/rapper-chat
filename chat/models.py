from django.db import models
from django.utils import timezone

# Create your models here.
class Conversation(models.Model):
    owner = models.CharField(max_length=255, db_index=True)
    title = models.CharField(max_length=255, default="New Chat")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # Set by the stop endpoint and polled by the in-flight streaming generator,
    # which lives in a different request (and possibly a different Gunicorn
    # worker), so the signal has to travel through shared storage rather than
    # process memory. Cleared when a stream starts and when one finishes.
    stop_requested = models.BooleanField(default=False)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f"{self.title} ({self.owner})"

class Message(models.Model):
    ROLE_CHOICES = (
        ('user', 'User'),
        ('assistant', 'Assistant'),
    )
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name='messages'
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    content = models.TextField()
    content_html = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.role}: {self.content[:30]}..."


class Subscriber(models.Model):
    PLAN_CHOICES = (
        ('free', 'Free'),
        ('basic', 'Basic'),
        ('pro', 'Pro'),
    )
    # 'free' is our own default before a user has ever reached Polar; the rest
    # mirror polar_sdk.models.subscription.SubscriptionStatus verbatim, since
    # webhook payloads are stored here as-is. Only 'active' grants a paid quota --
    # every other status (ours or Polar's) falls back to the free tier.
    STATUS_CHOICES = (
        ('free', 'Free'),
        ('incomplete', 'Incomplete'),
        ('incomplete_expired', 'Incomplete expired'),
        ('trialing', 'Trialing'),
        ('active', 'Active'),
        ('past_due', 'Past due'),
        ('canceled', 'Canceled'),
        ('unpaid', 'Unpaid'),
        ('paused', 'Paused'),
    )
    clerk_user_id = models.CharField(max_length=255, unique=True, db_index=True)
    # Anchor for the rolling 30-day usage window, shared by all three tiers --
    # free/basic/pro each just apply a different cap to the same window. Advanced
    # lazily on read (see chat.billing.has_quota) rather than by a scheduled job.
    usage_period_start = models.DateTimeField(default=timezone.now)
    polar_customer_id = models.CharField(max_length=255, blank=True, default='')
    polar_subscription_id = models.CharField(max_length=255, blank=True, default='')
    plan = models.CharField(max_length=10, choices=PLAN_CHOICES, default='free')
    billing_interval = models.CharField(max_length=10, blank=True, default='')  # 'month' | 'year'
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='free')
    current_period_end = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.clerk_user_id} ({self.plan}/{self.status})"