"""owner u12: who may do what, by role — one place so views and templates agree.

Platform (staff) roles: super admin, resource manager (RM), support, manager (= an org's sales rep), KAM (= an org's
key account manager). Customer roles: org admin, project admin (a tenant_admin membership), read only.

u12 rights:
  Manager (sales rep of the org): full rights to their orgs and all their projects; add a new org (manager fixed to
    themselves); invite/lock/unlock the org's users.
  KAM (of the org): read-only org except invite/lock/unlock its users; may NOT create projects; full resource
    management inside the org's projects.
  Org admin: full projects (through requests); invite/lock/unlock/archive/unarchive org users; assign project admin.
  Project admin: read-only org; add/remove/update resources in the projects assigned to them.
"""
from accounts.models import Role


def _staff(u):
    return bool(getattr(u, "is_authenticated", False) and u.is_staff_member)


def is_rm(u):
    return bool(getattr(u, "is_authenticated", False) and (u.is_super_admin or u.has_role(Role.RESOURCE_MANAGER)))


def manages_org(u, org):
    """Staff with FULL rights to this org: super admin, resource manager, or the org's own manager (sales rep)."""
    if not (org and getattr(u, "is_authenticated", False)):
        return False
    if is_rm(u):
        return True
    return _staff(u) and u.is_sales and org.sales_rep_id == u.pk


def kam_of(u, org):
    """The org's key account manager."""
    return bool(org and getattr(u, "is_authenticated", False) and _staff(u) and u.is_kam and org.kam_id == u.pk)


def _org_admin_of(u, org):
    return bool(org and getattr(u, "is_authenticated", False) and u.is_customer and u.is_org_admin
                and u.organization_id == org.pk)


# --- organizations -------------------------------------------------------------------------------------------
def can_add_org(u):
    """Create a new organization: super admin, RM, support or a manager (sales). Not a KAM."""
    return bool(_staff(u) and (is_rm(u) or u.has_role(Role.SUPPORT) or u.is_sales))


def can_edit_org(u, org):
    """Edit the org's own details. Manager (and RM/super) full; KAM and customers: no (read-only)."""
    return manages_org(u, org)


def can_assign_reps(u, org):
    """Reassign the org's manager / KAM / resource manager — only RM and super admin (u12 decision)."""
    return is_rm(u)


# --- the org's users -----------------------------------------------------------------------------------------
def can_manage_org_users(u, org):
    """Invite, lock or unlock the org's users: its manager, KAM or RM; or the org's own org admin."""
    return bool(manages_org(u, org) or kam_of(u, org) or _org_admin_of(u, org))


def can_archive_org_users(u, org):
    """Archive / unarchive the org's users: RM/super, or the org's own org admin (not manager or KAM)."""
    return bool(is_rm(u) or _org_admin_of(u, org))


def can_assign_project_admin(u, org):
    """Assign the project-admin role in the org: its manager/RM, or the org's own org admin."""
    return bool(manages_org(u, org) or _org_admin_of(u, org))


# --- projects ------------------------------------------------------------------------------------------------
def can_create_project(u, org):
    """Order a new project for the org: its manager/RM, or the org's own org admin (through a request). Not a KAM,
    not a project admin."""
    return bool(manages_org(u, org) or _org_admin_of(u, org))


def can_manage_project(u, tenant):
    """Add / remove / update a project's resources: the org's manager/KAM/RM; the org's org admin; or a project admin
    of this very project."""
    org = tenant.organization if tenant.organization_id else None
    if manages_org(u, org) or kam_of(u, org):
        return True
    if org and getattr(u, "is_authenticated", False) and u.is_customer and u.organization_id == org.pk:
        if u.is_org_admin:
            return True
        from .models import Membership
        return tenant.membership_role(u) == Membership.Role.TENANT_ADMIN
    return False
