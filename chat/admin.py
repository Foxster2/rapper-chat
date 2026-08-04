from django.contrib import admin

from .models import Subscriber


@admin.register(Subscriber)
class SubscriberAdmin(admin.ModelAdmin):
    list_display = ('clerk_user_id', 'plan', 'status', 'billing_interval', 'current_period_end', 'usage_period_start')
    list_filter = ('plan', 'status')
    search_fields = ('clerk_user_id', 'polar_customer_id', 'polar_subscription_id')
