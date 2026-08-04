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

    # Model-written chat title, requested once the opening reply has landed
    path('api/conversations/<int:conversation_id>/title/', views.conversation_title, name='conversation_title'),

    # Settings & billing (Polar.sh)
    path('settings/', views.settings_page, name='settings'),
    path('pricing/', views.pricing, name='pricing'),
    path('api/billing/checkout/<str:plan_key>/', views.start_checkout, name='start_checkout'),
    path('api/billing/change-plan/<str:plan_key>/', views.change_plan, name='change_plan'),
    path('api/billing/resume-with-plan/<str:plan_key>/', views.resume_with_plan, name='resume_with_plan'),
    path('api/billing/cancel/', views.cancel_subscription, name='cancel_subscription'),
    path('api/billing/resume/', views.resume_subscription, name='resume_subscription'),
    path('api/billing/webhook/', views.polar_webhook, name='polar_webhook'),
]
