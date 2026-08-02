from django.urls import path
from . import views

urlpatterns = [
    # Render main HTML shell
    path('', views.index, name='index'),

    # htmx partials
    path('api/conversations/partial/', views.conversations_partial, name='conversations_partial'),
    path('api/conversations/start/', views.start_conversation, name='start_conversation'),
    path('api/conversations/<int:conversation_id>/', views.conversation_detail, name='conversation_detail'),
    path('api/conversations/<int:conversation_id>/pane/', views.conversation_pane, name='conversation_pane'),
    path('api/conversations/<int:conversation_id>/messages/', views.create_message, name='create_message'),

    # SSE stream of the assistant reply, and the out-of-band request to end it early
    path('api/conversations/<int:conversation_id>/stream/', views.stream_reply, name='stream_reply'),
    path('api/conversations/<int:conversation_id>/stop/', views.stop_stream, name='stop_stream'),
]
