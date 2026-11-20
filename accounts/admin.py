"""Django admin for the accounts app: users, invitations and the log, read mostly.

The portal is run from its own pages, not from `/django-admin/`; this is a last resort for a super admin and is
registered deliberately narrowly.
"""

from django.contrib import admin

from .models import AuditLog, Invitation, User


def portal_sign_in(request, extra_context=None):
    """core-001: the admin has no sign-in of its own. Django's form signed a super admin in with the password alone,
    skipping the second factor, and that session opened the portal too; the portal's sign-in (password, then the
    second factor or a passkey) is the only way in, and the admin opens for a staff session made there."""
    from django.contrib.auth.views import redirect_to_login
    from django.shortcuts import redirect

    if request.user.is_authenticated:
        return redirect("dashboard")  # signed in, but not allowed in the admin
    return redirect_to_login(request.GET.get("next") or request.get_full_path(), login_url="login")


admin.site.login = portal_sign_in


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
    # core-016 (fourth full review): the roles and Django's own privilege flags are changed in the portal only, where
    # the super admin role needs the super admins' agreement (owner u12) and every role is checked and logged
    readonly_fields = (
        "is_super_admin",
        "is_resource_manager",
        "is_support",
        "is_sales",
        "is_kam",
        "kind",
        "is_staff",
        "is_superuser",
        "groups",
        "user_permissions",
        # core-016 again (fifth full review): who may sign in, and archiving, are the portal's too (a super admin
        # archived or blocked here would lose the role without the super admins' agreement)
        "is_active",
        "login_enabled",
        "archived_at",
    )

    def has_delete_permission(self, request, obj=None):
        return False  # users are archived in the portal, never deleted here (owner 2026-11-03)

    # core-016 again (ninth full review): a person is not changed here at all - their e-mail and two-factor state let
    # one super admin take over another's account (a password reset to a new address, no second factor) and give its
    # agreement; every change to a person is the portal's, where roles need the super admins' agreement
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    """core-016 again (sixth full review): shown, never made or changed here - an invitation rewritten here (its
    address, purpose, grants) kept its link and could make a super admin without the super admins' agreement."""

    list_display = ("email", "purpose", "organization", "created_at", "expires_at", "accepted_at", "revoked_at")
    exclude = ("token_hash",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "action", "target_type", "target_id")
    list_filter = ("action",)

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
