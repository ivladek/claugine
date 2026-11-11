"""Django admin for the content app: the public articles and the service components.
"""
from django.contrib import admin

from .models import Article, ComponentEvent, ServiceComponent

admin.site.register(Article, list_display=("title", "kind", "is_published", "published_at"))
admin.site.register(ServiceComponent, list_display=("name", "state", "position"))
admin.site.register(ComponentEvent, list_display=("component", "state", "at"))
