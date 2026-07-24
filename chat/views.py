import json
import markdown  # server-side markdown -> HTML for the final rendered reply
from django.shortcuts import render
from django.http import (
    JsonResponse, StreamingHttpResponse, QueryDict,
    HttpResponseBadRequest, HttpResponseNotFound, HttpResponseNotAllowed,
)
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.output_parsers import StrOutputParser

from .models import Conversation, Message

SYSTEM_PROMPT = "You are a helpful, thoughtful AI assistant. Output response in clean, semantic markdown format."


def build_chain():
    """Build the LCEL chain (prompt | ChatOpenAI@OpenRouter | str) used by the
    streaming endpoint. Supports both .invoke() and .stream()."""
    llm = ChatOpenAI(
        openai_api_base="https://openrouter.ai/api/v1",
        openai_api_key=settings.OPENROUTER_API_KEY,
        model_name=settings.OPENROUTER_MODEL,
        default_headers={
            "HTTP-Referer": "http://localhost:8000",
            "X-Title": "AI Chat Wrapper v1",
        },
    )
    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        MessagesPlaceholder(variable_name="history"),
    ])
    return prompt | llm | StrOutputParser()


def build_history(conversation):
    """Convert stored messages into LangChain Human/AI messages, in order."""
    history = []
    for msg in conversation.messages.all():
        if msg.role == 'user':
            history.append(HumanMessage(content=msg.content))
        else:
            history.append(AIMessage(content=msg.content))
    return history


def sse_frame(event, data):
    """Format a single Server-Sent Events frame. Multi-line data is split across
    multiple `data:` lines per the SSE spec (the client rejoins them with newlines)."""
    body = ''.join(f'data: {line}\n' for line in data.split('\n'))
    return f'event: {event}\n{body}\n'


def render_markdown(text):
    """Render the assistant's markdown reply to HTML for final display."""
    return markdown.markdown(text, extensions=['fenced_code', 'tables'])


def require_clerk_auth(view_func):
    """Reject the request with 401 unless the Clerk middleware authenticated the user."""
    def _wrapped_view(request, *args, **kwargs):
        if not request.clerk_user_id:
            return JsonResponse({'error': 'Unauthorized. Please sign in.'}, status=401)
        return view_func(request, *args, **kwargs)
    return _wrapped_view


def _user_conversations(request):
    return Conversation.objects.filter(owner=request.clerk_user_id)


def _rendered_messages(conversation):
    """Stored messages ready for templating — assistant turns pre-rendered to HTML."""
    items = []
    for m in conversation.messages.all():
        items.append({
            'role': m.role,
            'content': m.content,
            'content_html': render_markdown(m.content) if m.role == 'assistant' else '',
        })
    return items


def _list_partial(request, active_id=None):
    """Render the sidebar conversation list (an htmx-swappable partial)."""
    return render(request, 'chat/partials/_conversation_list.html', {
        'conversations': _user_conversations(request),
        'active_id': active_id,
    })


def index(request):
    """Serve the single-page chat shell."""
    return render(request, 'chat/index.html', {
        'CLERK_PUBLISHABLE_KEY': settings.CLERK_PUBLISHABLE_KEY,
    })


@require_clerk_auth
def conversations_partial(request):
    """GET: the sidebar list, loaded by htmx once Clerk is ready."""
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
    the client which conversation is now active via HX-Trigger. Optionally piggybacks
    an out-of-band refresh of the sidebar list (used when a new chat is created)."""
    last = conversation.messages.last()
    context = {
        'conversation': conversation,
        'messages': _rendered_messages(conversation),
        # If the latest turn is the user's, render an SSE-wired assistant bubble that
        # streams the pending reply as soon as the pane is inserted.
        'pending': bool(last and last.role == 'user'),
    }
    if refresh_list:
        context['refresh_list'] = True
        context['conversations'] = _user_conversations(request)
        context['active_id'] = conversation.id
    response = render(request, 'chat/partials/_chat_pane.html', context)
    response['HX-Trigger'] = json.dumps({'conversation-active': {'id': conversation.id}})
    return response


@require_clerk_auth
def conversation_pane(request, conversation_id):
    """GET: the chat pane for an existing conversation (selected from the sidebar)."""
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

    Message.objects.create(conversation=conversation, role='user', content=content)
    return render(request, 'chat/partials/_message_pair.html', {
        'conversation': conversation,
        'content': content,
    })


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

    def event_stream():
        collected = []
        try:
            for chunk in build_chain().stream({"history": history}):
                if not chunk:
                    continue
                collected.append(chunk)
                yield sse_frame('token', chunk)
        except Exception as e:
            print(f"LangChain/OpenRouter streaming error: {e}")
            yield sse_frame('error', str(e))
            return

        # Persist the full reply, bump updated_at, and send the rendered HTML.
        full = ''.join(collected)
        Message.objects.create(conversation=conversation, role='assistant', content=full)
        conversation.save()
        yield sse_frame('done', render_markdown(full))

    response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'  # disable proxy/server buffering so tokens flush immediately
    return response
