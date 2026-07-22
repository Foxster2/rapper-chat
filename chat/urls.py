from django.urls import path
from . import views

urlpatterns = [
    # Render main HTML interface page
    path('', views.index, name='index'),
    
    # API endpoints
    path('api/conversations/', views.conversations_list, name='conversations_list'),
    path('api/conversations/<int:conversation_id>/', views.conversation_detail, name='conversation_detail'),
    path('api/conversations/<int:conversation_id>/send/', views.send_message, name='send_message'),
]
