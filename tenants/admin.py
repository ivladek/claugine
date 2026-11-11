"""Django admin for the tenants app: the projects and their records, read mostly.
"""
from django.contrib import admin

from .models import (
    Connectivity, JoinRequest, Membership, Organization, PortalSettings, PrefixAssignment,
    QuotaRecord, Registration, Tenant,
)

admin.site.register(Organization, list_display=("name", "tax_id", "sales_rep"), search_fields=("name", "tax_id"))
admin.site.register(Tenant, list_display=("name", "title", "organization", "kind", "origin", "approval", "status"),
                    search_fields=("name", "title"))
admin.site.register(QuotaRecord, list_display=("tenant", "site", "cluster", "version", "cpu", "ram_gb", "gpu", "source"))
admin.site.register(Connectivity, list_display=("tenant", "site", "version", "connection_type", "source"))
admin.site.register(PrefixAssignment, list_display=("cidr", "site", "kind", "tenant", "released_at"))
for m in (Membership, JoinRequest, Registration, PortalSettings):
    admin.site.register(m)
