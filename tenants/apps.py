"""The tenants app: clients' projects — orders, approvals, provisioning, networks, gateways, lifecycle.
"""
from django.apps import AppConfig


class TenantsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "tenants"
