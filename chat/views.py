import time
from django.shortcuts import render, redirect
from django.http import (
    HttpResponse, JsonResponse, StreamingHttpResponse, QueryDict,
    HttpResponseBadRequest, HttpResponseForbidden, HttpResponseNotFound, HttpResponseNotAllowed,
)
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from django_cotton import render_component
from django_htmx.http import trigger_client_event
from polar_sdk.webhooks import WebhookVerificationError, WebhookUnknownTypeError

from . import billing
from .models import Conversation, Message
from .llm import build_chain, build_history, generate_title
from .utils import render_markdown, sse_frame

# How often the streaming generator checks whether the user asked it to stop.
# The browser freezes the reply on click, so this no longer gates how responsive
# stopping feels -- it gates how much extra text is generated (and billed) after
# the click, and how far the saved reply runs past what the user saw.
STOP_POLL_INTERVAL = 0.1  # seconds

# Shown when a reply produced no text at all -- the model returned an empty
# stream. The message row is still created (with empty content) so the
# conversation isn't left looking unanswered and won't re-stream on the next
# pane load. A stop always captures at least one token, since the stop check
# only runs after a token has been yielded, so it does not normally land here.
STOPPED_EMPTY_HTML = '<p class="italic text-base-content/50">Response stopped.</p>'


def require_clerk_auth(view_func):
    """Reject the request with 401 unless the Clerk middleware authenticated the user."""
    def _wrapped_view(request, *args, **kwargs):
        if not request.clerk_user_id:
            return JsonResponse({'error': 'Unauthorized. Please sign in.'}, status=401)
        return view_func(request, *args, **kwargs)
    return _wrapped_view


def _user_conversations(request):
    return Conversation.objects.filter(owner=request.clerk_user_id)


def _reject_if_over_quota(request):
    """None if the user may send another message; otherwise a response that sends
    them to the pricing page. Both send views are hx-post forms, so a plain 302
    would just get swapped into the target div by htmx instead of navigating the
    browser -- HX-Redirect is the header htmx honors to force a full redirect.
    The ?reason=limit query param lets the pricing page distinguish this from
    someone browsing plans proactively, so it doesn't claim a cap was hit when
    it wasn't."""
    if billing.has_quota(request.clerk_user_id):
        return None
    response = HttpResponse(status=204)
    response['HX-Redirect'] = '/pricing/?reason=limit'
    return response


def _rendered_messages(conversation):
    """Stored messages ready for templating — assistant turns pre-rendered to HTML."""
    items = []
    for m in conversation.messages.all():
        html = ''
        if m.role == 'assistant':
            if not m.content_html:
                # Backfills rows saved before content_html existed; new rows are
                # already populated by stream_reply and skip straight past this.
                m.content_html = render_markdown(m.content)
                m.save(update_fields=['content_html'])
            html = m.content_html
        items.append({
            'role': m.role,
            'content': m.content,
            'content_html': html,
        })
    return items


def _list_partial(request, active_id=None):
    """Render the sidebar conversation list (an htmx-swappable partial)."""
    return HttpResponse(render_component(request, 'conversation-list',
        conversations=_user_conversations(request),
        active_id=active_id,
    ))


def index(request):
    """Serve the single-page chat shell."""
    return render(request, 'chat/index.html', {
        'CLERK_PUBLISHABLE_KEY': settings.CLERK_PUBLISHABLE_KEY,
    })


@require_clerk_auth
def conversations_partial(request):
    """GET: the sidebar list, loaded by htmx once Clerk is ready."""
    if not request.htmx:
        return redirect('index')
    return _list_partial(request)


@csrf_exempt
@require_clerk_auth
def conversation_detail(request, conversation_id):
    """Rename (PUT) or delete (DELETE) a conversation; returns the refreshed list."""
    try:
        conversation = Conversation.objects.get(id=conversation_id, owner=request.clerk_user_id)
    except Conversation.DoesNotExist:
        return HttpResponseNotFound('Conversation not found')

    if request.method == 'DELETE':
        conversation.delete()
        return _list_partial(request)

    if request.method == 'PUT':
        # htmx sends the body form-encoded even for PUT, so parse it as a QueryDict.
        new_title = (QueryDict(request.body).get('title') or '').strip()
        if new_title:
            conversation.title = new_title[:100]
            conversation.save()
        return _list_partial(request, active_id=conversation.id)

    return HttpResponseNotAllowed(['PUT', 'DELETE'])


def _pane_response(request, conversation, refresh_list=False):
    """Render the chat pane (topbar + messages + input) for a conversation, telling
    the client which conversation is now active via a conversation-active client
    event. Optionally piggybacks an out-of-band refresh of the sidebar list (used
    when a new chat is created)."""
    last = conversation.messages.last()
    conversations = _user_conversations(request) if refresh_list else None
    active_id = conversation.id if refresh_list else None
    response = HttpResponse(render_component(request, 'chat-pane',
        conversation=conversation,
        messages=_rendered_messages(conversation),
        # If the latest turn is the user's, render an SSE-wired assistant bubble that
        # streams the pending reply as soon as the pane is inserted.
        pending=bool(last and last.role == 'user'),
        refresh_list=refresh_list,
        conversations=conversations,
        active_id=active_id,
    ))
    trigger_client_event(response, 'conversation-active', {'id': conversation.id})
    return response


@require_clerk_auth
def conversation_pane(request, conversation_id):
    """GET: the chat pane for an existing conversation (selected from the sidebar)."""
    if not request.htmx:
        return redirect('index')
    try:
        conversation = Conversation.objects.get(id=conversation_id, owner=request.clerk_user_id)
    except Conversation.DoesNotExist:
        return HttpResponseNotFound('Conversation not found')
    return _pane_response(request, conversation)


@csrf_exempt
@require_clerk_auth
def start_conversation(request):
    """POST: create a conversation from the first message, then return its pane
    (with the pending reply streaming) plus an OOB refresh of the sidebar."""
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    content = (request.POST.get('content') or '').strip()
    if not content:
        return HttpResponseBadRequest('Content cannot be empty')

    quota_response = _reject_if_over_quota(request)
    if quota_response is not None:
        return quota_response

    conversation = Conversation.objects.create(owner=request.clerk_user_id, title=content[:60])
    Message.objects.create(conversation=conversation, role='user', content=content)
    return _pane_response(request, conversation, refresh_list=True)


# ── Streaming (SSE) message flow ────────────────────────────────────────────
# A reply is produced in two steps because EventSource can only issue GET requests:
# (1) create_message saves the user turn and returns the message pair (user bubble +
# an SSE-wired assistant bubble), then (2) stream_reply streams the answer.

@csrf_exempt
@require_clerk_auth
def create_message(request, conversation_id):
    """POST: save the user's message and return the message-pair partial. The
    assistant bubble it contains connects to stream_reply to receive the answer."""
    try:
        conversation = Conversation.objects.get(id=conversation_id, owner=request.clerk_user_id)
    except Conversation.DoesNotExist:
        return HttpResponseNotFound('Conversation not found')
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])

    content = (request.POST.get('content') or '').strip()
    if not content:
        return HttpResponseBadRequest('Content cannot be empty')

    quota_response = _reject_if_over_quota(request)
    if quota_response is not None:
        return quota_response

    Message.objects.create(conversation=conversation, role='user', content=content)
    return HttpResponse(render_component(request, 'message-pair',
        conversation=conversation,
        content=content,
    ))


@require_clerk_auth
def stream_reply(request, conversation_id):
    """Stream the assistant's reply to the latest user message token-by-token via SSE.

    Emits `token` events as text arrives, persists the assembled reply once the
    stream finishes, then emits a final `done` event carrying the rendered HTML.
    """
    try:
        conversation = Conversation.objects.get(id=conversation_id, owner=request.clerk_user_id)
    except Conversation.DoesNotExist:
        return JsonResponse({'error': 'Conversation not found'}, status=404)

    # Guard against EventSource auto-reconnect regenerating a reply: only stream when
    # the most recent message is a user turn still awaiting an answer.
    last = conversation.messages.last()
    if last is None or last.role != 'user':
        return StreamingHttpResponse(sse_frame('done', ''), content_type='text/event-stream')

    # Build history eagerly (before streaming starts) so the DB query isn't deferred
    # into the generator.
    history = build_history(conversation)

    # Clear any flag left behind by an earlier stream so a stale request can't
    # cut this one short. Runs in the view body, which executes before the
    # generator is first iterated, so it can't race a genuine stop.
    Conversation.objects.filter(pk=conversation.pk).update(stop_requested=False)

    def event_stream():
        collected = []
        try:
            next_stop_check = time.monotonic() + STOP_POLL_INTERVAL
            for chunk in build_chain().stream({"history": history}):
                if not chunk:
                    continue
                collected.append(chunk)
                yield sse_frame('token', chunk)

                # Time-based rather than per-token: tokens arrive far faster than
                # a person can click, so polling every token would spend a query
                # per token for no extra responsiveness.
                now = time.monotonic()
                if now >= next_stop_check:
                    next_stop_check = now + STOP_POLL_INTERVAL
                    if Conversation.objects.filter(pk=conversation.pk, stop_requested=True).exists():
                        break
        except Exception as e:
            print(f"LangChain/OpenRouter streaming error: {e}")
            yield sse_frame('error', str(e))
            # Always close with `done` too: the bubble's sse-close="done" is what stops
            # EventSource from auto-reconnecting and re-hitting the API in a loop.
            # The payload replaces the bubble, so surface the failure there.
            yield sse_frame('done', '<p class="text-error">Sorry — the reply failed to generate. Please try again.</p>')
            return

        # A stopped reply takes this same path deliberately: it is persisted and
        # rendered exactly like a complete one, so it survives a reload and reads
        # back as an ordinary (if short) assistant turn.
        full = ''.join(collected)
        full_html = render_markdown(full) if full.strip() else STOPPED_EMPTY_HTML
        Message.objects.create(conversation=conversation, role='assistant', content=full, content_html=full_html)
        conversation.stop_requested = False
        conversation.save(update_fields=['stop_requested', 'updated_at'])
        yield sse_frame('done', full_html)

    response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'  # disable proxy/server buffering so tokens flush immediately
    return response


@csrf_exempt
@require_clerk_auth
def conversation_title(request, conversation_id):
    """POST: replace the placeholder title with one the model writes.

    Called by the client once the first reply has finished, rather than during
    it -- naming the chat is not something the user is waiting on, so it must
    not delay the reply. Only ever acts on the opening exchange; later calls are
    a no-op, which also means a rename can't be overwritten afterwards.
    """
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    try:
        conversation = Conversation.objects.get(id=conversation_id, owner=request.clerk_user_id)
    except Conversation.DoesNotExist:
        return HttpResponseNotFound('Conversation not found')

    messages = list(conversation.messages.all()[:3])
    if len(messages) != 2 or messages[0].role != 'user':
        return HttpResponse(status=204)

    title = generate_title(messages[0].content, messages[1].content)
    if not title:
        return HttpResponse(status=204)

    conversation.title = title
    conversation.save(update_fields=['title'])
    response = _list_partial(request, active_id=conversation.id)
    trigger_client_event(response, 'conversation-titled',
                         {'id': conversation.id, 'title': title})
    return response


@csrf_exempt
@require_clerk_auth
def stop_stream(request, conversation_id):
    """POST: ask the in-flight reply for this conversation to stop early.

    This only raises the flag; the streaming generator notices it, breaks out of
    the token loop, and finishes through its normal completion path. Leaving the
    generator as the sole writer of the assistant message is what keeps a stop
    from racing a reply that was about to finish on its own and producing two
    rows. Safe to call repeatedly, and a no-op if nothing is streaming.
    """
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    updated = Conversation.objects.filter(
        id=conversation_id, owner=request.clerk_user_id
    ).update(stop_requested=True)
    if not updated:
        return HttpResponseNotFound('Conversation not found')
    return HttpResponse(status=204)


# ── Settings & billing (Polar.sh) ────────────────────────────────────────────

@require_clerk_auth
def settings_page(request):
    """GET: landing page for the sidebar's Settings link. Just a "Manage
    subscription" entry point today, kept separate from /pricing/ so that page
    stays dedicated to plan comparison/checkout rather than doubling as the
    general settings screen."""
    return render(request, 'chat/settings.html', {})


@require_clerk_auth
def pricing(request):
    """GET: plan comparison + checkout buttons. Reached either by choice (from
    /settings/) or automatically (via HX-Redirect, see _reject_if_over_quota)
    once a free user exhausts their quota -- ?reason=limit tells these apart so
    the headline doesn't claim a cap was hit when the user got here on their own.

    Already-active subscribers see their current plan marked and get "Switch to"
    buttons (change_plan) on the rest instead of "Subscribe" (start_checkout), so
    upgrading/downgrading changes their existing subscription rather than
    starting a second, competing one."""
    subscriber = billing.get_or_create_subscriber(request.clerk_user_id)
    current = subscriber if subscriber.status == 'active' else None

    plans = billing.plan_display()
    for plan in plans:
        plan['monthly_is_current'] = bool(current and current.plan == plan['key'] and current.billing_interval == 'month')
        plan['annual_is_current'] = bool(current and current.plan == plan['key'] and current.billing_interval == 'year')

    return render(request, 'chat/pricing.html', {
        'plans': plans,
        'limit_reached': request.GET.get('reason') == 'limit',
        'current_subscriber': current,
    })


@require_clerk_auth
def start_checkout(request, plan_key):
    """POST: create a Polar hosted checkout for plan_key and send the browser to
    it. A plain (non-htmx) form post carrying a normal CSRF token, so Django's
    redirect is a normal top-level navigation to Polar's off-site checkout page."""
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    try:
        checkout_url = billing.create_checkout_session(
            request.clerk_user_id, plan_key,
            success_url=request.build_absolute_uri('/'),
        )
    except ValueError:
        return HttpResponseBadRequest('Unknown plan')
    return redirect(checkout_url)


@require_clerk_auth
def change_plan(request, plan_key):
    """POST: move the caller's existing active subscription onto plan_key in
    place (see billing.change_plan), then send them back to the pricing page to
    see the result."""
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    try:
        billing.change_plan(request.clerk_user_id, plan_key)
    except ValueError:
        return HttpResponseBadRequest('Unknown plan, or no active subscription to change')
    return redirect('pricing')


@csrf_exempt
def polar_webhook(request):
    """POST: Polar calls this on subscription lifecycle events. Not behind
    @require_clerk_auth -- Polar, not a signed-in browser, is the caller here;
    trust comes from the webhook signature instead."""
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    try:
        event = billing.verify_webhook(request.body, dict(request.headers))
    except WebhookVerificationError:
        return HttpResponseForbidden('Invalid signature')
    except WebhookUnknownTypeError:
        # A newer Polar event type this SDK version doesn't know about yet --
        # acknowledge rather than error, so Polar doesn't retry it forever.
        return HttpResponse(status=200)
    billing.handle_webhook_event(event)
    return HttpResponse(status=200)
