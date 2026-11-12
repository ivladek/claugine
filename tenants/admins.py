"""Correction 15: the tenant admin — the person on the client's side who gets the tenant's credentials. Always a user of
the tenant's organization; chosen when the tenant is ordered, can be changed at approval and later."""

from accounts.models import Kind, User, audit


def org_users(org):
    """The active users of an organization, its admins first."""
    if org is None:
        return User.objects.none()
    return User.objects.filter(organization=org, kind=Kind.CUSTOMER, is_active=True).order_by(
        "-is_org_admin", "full_name", "email"
    )


def admin_candidates(org, capability="may_tenant_admin"):
    """Who can be an org/project admin: the organization's users; for a corporate organization also the platform
    users of a corporate e-mail domain. owner 2026-11-03: a platform user is offered only when flagged eligible —
    capability is "may_tenant_admin" for a project admin (default) or "may_org_admin" for the org admin."""
    from django.db.models import Q

    if org is None:
        return User.objects.none()
    q = Q(organization=org, kind=Kind.CUSTOMER)
    if org.kind == "corp":
        from .forms import corp_domain_ok

        staff = [
            u.pk
            for u in User.objects.filter(
                kind=Kind.STAFF, is_active=True, archived_at__isnull=True, **{capability: True}
            )
            if corp_domain_ok(u)
        ]
        q |= Q(pk__in=staff)
    return User.objects.filter(q, is_active=True).order_by("kind", "-is_org_admin", "full_name", "email")


def label(u):
    return f"{u.full_name or '—'} <{u.email}>"


def choices(org):
    return [(str(u.pk), label(u)) for u in admin_candidates(org)]


def set_tenant_admin(tenant, user, by=None, why=""):
    """Make `user` (a user of the tenant's organization) the tenant admin: kept on the tenant and as its tenant admin
    membership. Returns True when it changed."""
    from .models import Membership

    if user is None or not admin_candidates(tenant.organization).filter(pk=user.pk).exists():
        raise ValueError(
            "the project admin must be a user of the project's organization (corporate: or a platform user)"
        )
    old = tenant.tenant_admin
    Membership.objects.update_or_create(user=user, tenant=tenant, defaults={"role": Membership.Role.TENANT_ADMIN})
    if old is not None and old.pk == user.pk:
        return False
    tenant.tenant_admin = user
    tenant.save(update_fields=["tenant_admin"])
    audit(by, "tenant_admin_set", tenant, user=user.email, old=old.email if old else "", why=why)
    return True


def proposed(tenant, exclude=None):
    """(user or None, why) — who should be the tenant admin when none is set: its only tenant admin member, else the
    organization's first admin, else its only user. A user may be the org admin and a project admin at once
    (owner 2026-11-03). ``exclude`` leaves a user out (reassigning a project away from someone being archived)."""
    from .models import Membership

    ex = {exclude.pk} if exclude is not None else set()
    members = [
        m.user
        for m in Membership.objects.filter(
            tenant=tenant, role=Membership.Role.TENANT_ADMIN, user__is_active=True
        ).select_related("user")
        if m.user.organization_id == tenant.organization_id and m.user.pk not in ex
    ]
    if len(members) == 1:
        return members[0], "its tenant admin member"
    users = [u for u in org_users(tenant.organization) if u.pk not in ex]
    admins = [u for u in users if u.is_org_admin]
    if admins:
        return sorted(admins, key=lambda u: u.pk)[0], "the organization admin"
    if members:
        return sorted(members, key=lambda u: u.pk)[0], "a tenant admin member"
    if users:  # owner 2026-10-29: an organization with users always gives its tenants an admin
        return sorted(users, key=lambda u: u.pk)[0], "an organization user"
    return None, "the organization has no eligible users"


def fill_missing(org, by=None):
    """Give every project of the organization a current, active project admin (owner 2026-11-03: each project must
    have at least one). Covers projects with no admin and projects whose admin is no longer active. Returns names."""
    from .models import Tenant

    done = []
    for t in Tenant.objects.filter(organization=org).exclude(status="archived").select_related("tenant_admin"):
        if t.is_deleted:
            continue
        if t.tenant_admin_id and t.tenant_admin and t.tenant_admin.is_active:
            continue
        u, why = proposed(t)
        if u is not None:
            set_tenant_admin(t, u, by, why=why)
            done.append(t.name)
    return done
