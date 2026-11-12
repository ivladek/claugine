"""Django admin for the accounts app: users, invitations and the log, read mostly.

The portal is run from its own pages, not from `/django-admin/`; this is a last resort for a super admin and is
registered deliberately narrowly.
"""

from django.contrib import admin

from .models import AuditLog, Invitation, User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = (
        "email",
        "full_name",
        "kind",
        "is_super_admin",
        "is_resource_manager",
        "is_support",
        "is_sales",
        "organization",
        "is_active",
        "login_enabled",
    )
    list_filter = ("kind", "is_super_admin", "is_sales", "is_active")
    search_fields = ("email", "full_name", "phone")
    exclude = ("password",)


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    list_display = ("email", "purpose", "organization", "created_at", "expires_at", "accepted_at", "revoked_at")
    exclude = ("token_hash",)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "action", "target_type", "target_id")
    list_filter = ("action",)

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
