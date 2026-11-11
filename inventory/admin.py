"""Django admin for the inventory app: the infrastructure records, read mostly.
"""
from django.contrib import admin

from .models import Cluster, DelegatedSubnet, Host, Platform, PlatformControl, Revision, ServerProfile, Site

admin.site.register(Revision, list_display=("pk", "source", "note", "is_current", "created_at"))
admin.site.register(PlatformControl, list_display=("key", "mode", "xmlrpc_url", "last_collected_at"),
                    exclude=("password_encrypted",))
for m in (Site, ServerProfile, Host, Platform, Cluster, DelegatedSubnet):
    admin.site.register(m)
