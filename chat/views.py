from django.shortcuts import render

import json
import os
import markdown  # server-side markdown -> HTML for the final rendered reply
from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse, StreamingHttpResponse
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.output_parsers import StrOutputParser

from .models import Conversation, Message
from rapper import settings

SYSTEM_PROMPT = "You are a helpful, thoughtful AI assistant. Output response in clean, semantic markdown format."


def build_chain():
    """Build the LCEL chain (prompt | ChatOpenAI@OpenRouter | str) shared by the
    blocking and streaming endpoints. Supports both .invoke() and .stream()."""
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
    def _wrapped_view(request, *args, **kwargs):
        if not request.clerk_user_id:
            return JsonResponse({'error': 'Unauthorized. Please sign in.'}, status=401)
        return view_func(request, *args, **kwargs)
    return _wrapped_view

# Create your views here.

def index(request):
    """Serve the single-page chat dashboard template."""
    context = {
        'CLERK_PUBLISHABLE_KEY': settings.CLERK_PUBLISHABLE_KEY
    }
    return render(request, 'chat/index.html', context)

@csrf_exempt
@require_clerk_auth
def conversations_list(request):
    """API endpoint to list or create conversations for the authenticated user."""
    if request.method == 'GET':
        conversations = Conversation.objects.filter(owner=request.clerk_user_id)
        data = [
            {
                'id': c.id,
                'title': c.title,
                'created_at': c.created_at.isoformat(),
                'updated_at': c.updated_at.isoformat()
            }
            for c in conversations
        ]
        return JsonResponse(data, safe=False)

    elif request.method == 'POST':
        try:
            body = json.loads(request.body)
            title = body.get('title', 'New Chat').strip()
        except (json.JSONDecodeError, AttributeError):
            title = 'New Chat'
        if not title:
            title = 'New Chat'

        conversation = Conversation.objects.create(
            owner=request.clerk_user_id,
            title=title
        )
        return JsonResponse({
            'id': conversation.id,
            'title': conversation.title,
            'created_at': conversation.created_at.isoformat(),
            'updated_at': conversation.updated_at.isoformat()
        }, status=201)

@csrf_exempt
@require_clerk_auth
def conversation_detail(request, conversation_id):
    """API endpoint to fetch all messages in a specific conversation."""
    try:
        conversation = Conversation.objects.get(id=conversation_id, owner=request.clerk_user_id)
    except Conversation.DoesNotExist:
        return JsonResponse({'error': 'Conversation not found'}, status=404)

    if request.method == 'GET':
        messages = conversation.messages.all()
        data = [
            {
                'id': m.id,
                'role': m.role,                                                                                                                                      
                'content': m.content,                                                                                                                                
                'created_at': m.created_at.isoformat() 
            }
            for m in messages
        ]
        return JsonResponse(data, safe=False)

    elif request.method == 'DELETE':
        conversation.delete()
        return JsonResponse({'success': True}, status=200)

    elif request.method == 'PUT':
        try:
            body = json.loads(request.body)
            new_title = body.get('title', '').strip()
            if new_title:
                conversation.title = new_title[:100]  # limit length
                conversation.save()
                return JsonResponse({'success': True, 'title': conversation.title}, status=200)
            return JsonResponse({'error': 'Title cannot be empty'}, status=400)
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid request body'}, status=400)

@csrf_exempt
@require_clerk_auth
def send_message(request, conversation_id):                                                                                                                          
    """API endpoint to post a user message, run LangChain orchestration, and return the response."""                                                                 
    try:                                                                                                                                                             
        conversation = Conversation.objects.get(id=conversation_id, owner=request.clerk_user_id)                                                                     
    except Conversation.DoesNotExist:                                                                                                                                
        return JsonResponse({'error': 'Conversation not found'}, status=404)                                                                                         
                                                                                                                                                                     
    if request.method == 'POST':                                                                                                                                     
        try:                                                                                                                                                         
            body = json.loads(request.body)                                                                                                                          
            content = body.get('content', '').strip()                                                                                                                
        except (json.JSONDecodeError, AttributeError):                                                                                                               
            return JsonResponse({'error': 'Invalid request body'}, status=400)                                                                                       
                                                                                                                                                                     
        if not content:                                                                                                                                              
            return JsonResponse({'error': 'Content cannot be empty'}, status=400)                                                                                    
                                                                                                                                                                     
        # 1. Save user's message to the DB
        Message.objects.create(
            conversation=conversation,
            role='user',
            content=content
        )

        # 2. Run the shared LCEL chain over the full history (blocking, no streaming)
        try:
            chain = build_chain()
            assistant_content = chain.invoke({"history": build_history(conversation)})
        except Exception as e:
            error_str = str(e)
            print(f"LangChain/OpenRouter error: {error_str}")
            return JsonResponse({'error': f'AI model error: {error_str}'}, status=502)
                                                                                                                                                                      
        # Save assistant's reply to the DB                                                                                                                        
        assistant_message = Message.objects.create(                                                                                                                  
            conversation=conversation,                                                                                                                               
            role='assistant',                                                                                                                                        
            content=assistant_content                                                                                                                                
        )                                                                                                                                                            
                                                                                                                                                                         
        # Update the conversation's updated_at timestamp to bring it to the top of the list                                                                       
        conversation.save()                                                                                                                                          
                                                                                                                                                                     
        return JsonResponse({
            'id': assistant_message.id,
            'role': assistant_message.role,
            'content': assistant_message.content,
            'created_at': assistant_message.created_at.isoformat()
        })


# ── Streaming (SSE) message flow ────────────────────────────────────────────
# The reply is produced in two steps because EventSource can only issue GET
# requests: (1) create_message saves the user turn, then (2) stream_reply streams
# the assistant answer as Server-Sent Events and persists it on completion.

@csrf_exempt
@require_clerk_auth
def create_message(request, conversation_id):
    """Save the user's message. The assistant reply is streamed separately by stream_reply."""
    try:
        conversation = Conversation.objects.get(id=conversation_id, owner=request.clerk_user_id)
    except Conversation.DoesNotExist:
        return JsonResponse({'error': 'Conversation not found'}, status=404)

    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    try:
        body = json.loads(request.body)
        content = body.get('content', '').strip()
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({'error': 'Invalid request body'}, status=400)

    if not content:
        return JsonResponse({'error': 'Content cannot be empty'}, status=400)

    message = Message.objects.create(conversation=conversation, role='user', content=content)
    return JsonResponse({
        'id': message.id,
        'role': message.role,
        'content': message.content,
        'created_at': message.created_at.isoformat(),
    }, status=201)


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