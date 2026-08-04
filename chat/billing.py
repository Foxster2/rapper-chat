"""Free-trial/subscription quota checks and Polar.sh checkout + webhook glue."""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from polar_sdk import Polar
from polar_sdk.models.polarerror import PolarError
from polar_sdk.webhooks import validate_event

from .models import Message, Subscriber

USAGE_WINDOW = timedelta(days=30)

# The free trial isn't special-cased infrastructure -- it's just the 'free' entry
# in the same table the paid tiers use. A plan with no entry here is unlimited
# (not used today, but keeps the door open).
PLAN_LIMITS = {
    'free': 20,
    'basic': 300,
    'pro': 3000,
}

# plan_key (as used in URLs and checkout requests) -> (plan, billing_interval, Polar product id)
PLAN_PRODUCTS = {
    'basic_monthly': ('basic', 'month', settings.POLAR_PRODUCT_BASIC_MONTHLY),
    'basic_annual': ('basic', 'year', settings.POLAR_PRODUCT_BASIC_ANNUAL),
    'pro_monthly': ('pro', 'month', settings.POLAR_PRODUCT_PRO_MONTHLY),
    'pro_annual': ('pro', 'year', settings.POLAR_PRODUCT_PRO_ANNUAL),
}
_PRODUCT_ID_TO_PLAN = {
    product_id: (plan, interval)
    for plan, interval, product_id in PLAN_PRODUCTS.values()
    if product_id
}

# List price in USD/month; the annual checkout bills 12x this with a 10% discount.
ANNUAL_DISCOUNT = 0.1
_MONTHLY_PRICE = {'basic': 5, 'pro': 50}


def plan_display():
    """Pricing-page data: one entry per paid plan with its monthly cap and both
    the monthly and (10%-off) annual list price, pre-computed so the template
    doesn't need to do arithmetic."""
    plans = []
    for plan, monthly_price in _MONTHLY_PRICE.items():
        annual_price = round(monthly_price * 12 * (1 - ANNUAL_DISCOUNT))
        plans.append({
            'key': plan,
            'name': plan.capitalize(),
            'message_limit': PLAN_LIMITS[plan],
            'monthly_price': monthly_price,
            'annual_price': annual_price,
            'monthly_plan_key': f'{plan}_monthly',
            'annual_plan_key': f'{plan}_annual',
        })
    return plans


def get_or_create_subscriber(clerk_user_id):
    subscriber, _ = Subscriber.objects.get_or_create(clerk_user_id=clerk_user_id)
    return subscriber


def has_quota(clerk_user_id):
    """Whether this user may send another message right now, and roll their usage
    window forward if it has expired. Lapsed/canceled/past-due subscribers are
    treated as 'free' -- one rule covers every non-active status."""
    subscriber = get_or_create_subscriber(clerk_user_id)
    effective_plan = subscriber.plan if subscriber.status == 'active' else 'free'
    limit = PLAN_LIMITS.get(effective_plan)
    if limit is None:
        return True

    now = timezone.now()
    if now - subscriber.usage_period_start >= USAGE_WINDOW:
        subscriber.usage_period_start = now
        subscriber.save(update_fields=['usage_period_start', 'updated_at'])

    used = Message.objects.filter(
        conversation__owner=clerk_user_id,
        role='user',
        created_at__gte=subscriber.usage_period_start,
    ).count()
    return used < limit


def create_checkout_session(clerk_user_id, plan_key, success_url):
    """Create a Polar hosted checkout for plan_key and return its URL. success_url
    is where Polar sends the browser back to after payment -- built from the
    current request so it works across every deploy host, same as this project's
    ALLOWED_HOSTS/CSRF_TRUSTED_ORIGINS being env-driven rather than hardcoded."""
    entry = PLAN_PRODUCTS.get(plan_key)
    if not entry or not entry[2]:
        raise ValueError(f"Unknown or unconfigured plan: {plan_key}")
    _, _, product_id = entry

    try:
        with Polar(access_token=settings.POLAR_ACCESS_TOKEN, server=settings.POLAR_SERVER) as polar:
            checkout = polar.checkouts.create(request={
                'products': [product_id],
                'external_customer_id': clerk_user_id,
                'success_url': success_url,
            })
    except PolarError as e:
        raise ValueError(f"Polar checkout failed: {e}") from e
    return checkout.url


def verify_webhook(body, headers):
    """Validate an incoming Polar webhook request and return its parsed event.
    Raises WebhookVerificationError on a bad signature."""
    return validate_event(body=body, headers=headers, secret=settings.POLAR_WEBHOOK_SECRET)


def _apply_subscription(subscriber, subscription):
    """Copy a polar_sdk Subscription's state onto a Subscriber row. Shared by the
    webhook handler and change_plan(), since a direct subscriptions.update() call
    returns the same Subscription shape a subscription.* webhook's data does."""
    subscriber.status = subscription.status.value
    subscriber.polar_customer_id = subscription.customer_id or subscriber.polar_customer_id
    subscriber.polar_subscription_id = subscription.id or subscriber.polar_subscription_id
    subscriber.current_period_end = subscription.current_period_end
    subscriber.cancel_at_period_end = subscription.cancel_at_period_end

    plan_interval = _PRODUCT_ID_TO_PLAN.get(subscription.product_id)
    if plan_interval:
        subscriber.plan, subscriber.billing_interval = plan_interval

    # pending_update is set while a next_period plan change (resume_with_plan)
    # hasn't taken effect yet, and clears itself once it has -- recomputing it
    # unconditionally here means this needs no separate "clear" step anywhere.
    pending = subscription.pending_update
    pending_interval = _PRODUCT_ID_TO_PLAN.get(pending.product_id) if pending else None
    subscriber.pending_plan, subscriber.pending_billing_interval = pending_interval or ('', '')

    subscriber.save()


def handle_webhook_event(event):
    """Sync a Subscriber row from a verified Polar subscription webhook event.
    Unrelated event types (e.g. order.*, customer.*) are ignored. The discriminated
    payload's event-type field is named TYPE (aliased from 'type' on the wire) --
    see polar_sdk.models.webhooksubscriptionactivepayload.WebhookSubscriptionActivePayload.
    """
    if not event.TYPE.startswith('subscription.'):
        return

    subscription = event.data
    customer = subscription.customer
    clerk_user_id = customer.external_id if customer else None
    polar_customer_id = (customer.id if customer else None) or subscription.customer_id

    subscriber = None
    if clerk_user_id:
        subscriber = get_or_create_subscriber(clerk_user_id)
    elif polar_customer_id:
        subscriber = Subscriber.objects.filter(polar_customer_id=polar_customer_id).first()
    if subscriber is None:
        return

    _apply_subscription(subscriber, subscription)


def change_plan(clerk_user_id, plan_key):
    """Move an already-active subscriber onto a different plan/interval by
    updating their EXISTING Polar subscription in place, rather than starting a
    second, competing subscription through checkout. proration_behavior='invoice'
    charges (upgrade) or credits (downgrade) the price difference immediately;
    the renewal date itself is untouched by this behavior."""
    entry = PLAN_PRODUCTS.get(plan_key)
    if not entry or not entry[2]:
        raise ValueError(f"Unknown or unconfigured plan: {plan_key}")
    _, _, product_id = entry

    subscriber = get_or_create_subscriber(clerk_user_id)
    if subscriber.status != 'active' or not subscriber.polar_subscription_id:
        raise ValueError("No active subscription to change")
    if subscriber.cancel_at_period_end:
        raise ValueError("Subscription is pending cancellation -- use resume_with_plan instead")

    try:
        with Polar(access_token=settings.POLAR_ACCESS_TOKEN, server=settings.POLAR_SERVER) as polar:
            subscription = polar.subscriptions.update(
                id=subscriber.polar_subscription_id,
                subscription_update={
                    'product_id': product_id,
                    'proration_behavior': 'invoice',
                },
            )
    except PolarError as e:
        raise ValueError(f"Polar plan change failed: {e}") from e
    _apply_subscription(subscriber, subscription)
    return subscriber


def resume_with_plan(clerk_user_id, plan_key):
    """Undo a pending cancellation while switching to a different plan/interval
    at the same time. Unlike change_plan(), the switch takes effect at the next
    renewal (proration_behavior='next_period') rather than charging immediately
    -- someone who just clicked cancel shouldn't be billed again the moment they
    change their mind about which plan to stay on. Two separate update calls:
    Polar's update payload is a discriminated union (cancel vs. product-change
    are distinct operations), so they can't be combined into one request."""
    entry = PLAN_PRODUCTS.get(plan_key)
    if not entry or not entry[2]:
        raise ValueError(f"Unknown or unconfigured plan: {plan_key}")
    _, _, product_id = entry

    subscriber = get_or_create_subscriber(clerk_user_id)
    if subscriber.status != 'active' or not subscriber.polar_subscription_id:
        raise ValueError("No active subscription to resume")

    try:
        with Polar(access_token=settings.POLAR_ACCESS_TOKEN, server=settings.POLAR_SERVER) as polar:
            polar.subscriptions.update(
                id=subscriber.polar_subscription_id,
                subscription_update={'cancel_at_period_end': False},
            )
            subscription = polar.subscriptions.update(
                id=subscriber.polar_subscription_id,
                subscription_update={
                    'product_id': product_id,
                    'proration_behavior': 'next_period',
                },
            )
    except PolarError as e:
        raise ValueError(f"Polar resume failed: {e}") from e
    _apply_subscription(subscriber, subscription)
    return subscriber


def cancel_subscription(clerk_user_id):
    """Schedule a subscriber's plan to cancel at the end of their current
    billing period -- they keep their plan's quota until then; it just doesn't
    renew. Reversible via resume_subscription() any time before the period ends."""
    subscriber = get_or_create_subscriber(clerk_user_id)
    if subscriber.status != 'active' or not subscriber.polar_subscription_id:
        raise ValueError("No active subscription to cancel")

    try:
        with Polar(access_token=settings.POLAR_ACCESS_TOKEN, server=settings.POLAR_SERVER) as polar:
            subscription = polar.subscriptions.update(
                id=subscriber.polar_subscription_id,
                subscription_update={'cancel_at_period_end': True},
            )
    except PolarError as e:
        raise ValueError(f"Polar cancellation failed: {e}") from e
    _apply_subscription(subscriber, subscription)
    return subscriber


def resume_subscription(clerk_user_id):
    """Undo a pending cancel_at_period_end before it takes effect. This is NOT
    the SDK's {'resume': True} action -- that undoes a *paused* subscription
    (a separate billing-pause feature we don't use), and 403s with
    'NotPausedSubscription' against one that's merely cancel-at-period-end.
    Un-cancelling is just calling the same update with the flag flipped back,
    which is also why we're already subscribed to the subscription.uncanceled
    webhook event (see the Polar dashboard webhook config)."""
    subscriber = get_or_create_subscriber(clerk_user_id)
    if subscriber.status != 'active' or not subscriber.polar_subscription_id:
        raise ValueError("No active subscription to resume")

    try:
        with Polar(access_token=settings.POLAR_ACCESS_TOKEN, server=settings.POLAR_SERVER) as polar:
            subscription = polar.subscriptions.update(
                id=subscriber.polar_subscription_id,
                subscription_update={'cancel_at_period_end': False},
            )
    except PolarError as e:
        raise ValueError(f"Polar resume failed: {e}") from e
    _apply_subscription(subscriber, subscription)
    return subscriber
