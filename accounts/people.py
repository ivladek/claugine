"""People of the portal: platform users (staff) and tenant users (customers), their state and roles.

State
    active    the invitation was accepted: they can sign in
    blocked   the invitation was accepted, sign-in is locked
    invited   an invitation was sent and is not accepted yet
    inactive  not invited yet (a directory entry)
    archived  had an account; removed from the lists, kept for the record without roles; can be invited back

Someone who never accepted an invitation (invited, inactive) is deleted, not archived. Platform users get their
roles with the invitation (no role before it, none after an invitation is revoked). Tenant users are organization
admins (everything of the organization), tenant admins (only the tenants given) or read only (see every tenant of
the organization, change nothing). Platform users must use two-factor authentication; tenant users choose.
"""
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l

from .models import Invitation, Kind, User, audit

STAFF_ROLES = [
    ("is_super_admin", _l("Super admin")),
    ("is_resource_manager", _l("Resource manager")),
    ("is_support", _l("Support engineer")),
    ("is_sales", _l("Sales manager")),
    ("is_kam", _l("Key account manager")),
]
CUSTOMER_ROLES = [("org_admin", _l("Org admin")), ("tenant_admin", _l("Project admin")), ("read_only", _l("Read only"))]
# owner 2026-11-03: the Users table shows short column headers (full name in a tooltip). (field, short, full).
# The platform columns add org-admin / project-admin eligibility after the staff roles.
PLATFORM_ROLE_COLUMNS = [
    ("is_super_admin", _l("SA"), _l("Super admin")),
    ("is_resource_manager", _l("RES"), _l("Resource manager")),
    ("is_support", _l("SUPPORT"), _l("Support engineer")),
    ("is_sales", _l("SALES"), _l("Sales manager")),
    ("is_kam", _l("KAM"), _l("Key account manager")),
    ("may_org_admin", _l("ORG"), _l("Org admin")),
    ("may_tenant_admin", _l("PROJECT"), _l("Project admin")),
]
PLATFORM_ELIGIBILITY = ["may_org_admin", "may_tenant_admin"]  # not the "every role keeps a holder" kind
CUSTOMER_ROLE_COLUMNS = [
    ("org_admin", _l("ORG"), _l("Org admin")),
    ("tenant_admin", _l("PROJECT"), _l("Project admin")),
    ("read_only", _l("READ"), _l("Read only")),
]
STATE_LABELS = {"active": _l("active"), "blocked": _l("blocked"), "invited": _l("invited"), "inactive": _l("inactive"),
                "archived": _l("archived")}


def state(u, invited_ids=frozenset(), invited_emails=frozenset()):
    if u.archived_at:
        return "archived"
    accepted = u.login_enabled and u.has_usable_password()
    if accepted:
        return "active" if u.is_active else "blocked"
    if u.pk in invited_ids or u.email.lower() in invited_emails:
        return "invited"
    return "inactive"


def never_accepted(u):
    """Never got in: no accepted invitation (no usable password with sign-in on) and never signed in — or a tenant
    user who registered but was never approved (no organization: the join request was never accepted)."""
    if u.kind == Kind.CUSTOMER and not u.organization_id and not u.memberships.exists():
        return True
    return u.last_login is None and not (u.login_enabled and u.has_usable_password())


def _open_invites(users):
    open_ = Invitation.objects.open().filter(accepted_at__isnull=True)
    ids = set(open_.filter(user__in=users).values_list("user_id", flat=True))
    emails = {e.lower() for e in open_.filter(email__in=[u.email for u in users]).values_list("email", flat=True)}
    return ids, emails


def _roles_label(roles):
    """u10: the user's held roles as one line, e.g. 'Support · Resource manager' (empty → '—' in the template)."""
    return " · ".join(str(label) for _f, label, on in roles if on)


def staff_rows(show_archived=False):
    users = list(User.objects.filter(kind=Kind.STAFF).order_by("-is_super_admin", "full_name", "email"))
    ids, emails = _open_invites(users)
    rows = []
    for u in users:
        st = state(u, ids, emails)
        if st == "archived" and not show_archived:
            continue
        # owner 2026-11-03: inactive (non-invited) users can hold roles too (used for report delivery); only an
        # archived user has none (its flags are cleared on archive)
        roles = [(f, full, getattr(u, f) and st != "archived") for f, _short, full in PLATFORM_ROLE_COLUMNS]
        rows.append({"u": u, "rowid": f"u-{u.pk}", "state": st, "label": STATE_LABELS[st], "deletable": never_accepted(u),
                     "roles": roles, "roles_label": _roles_label(roles),
                     "directory": st == "inactive" and u.is_sales})
    return rows


def customer_roles(u):
    from tenants.models import Membership
    ms = list(u.memberships.select_related("tenant"))
    admin_of = [m.tenant for m in ms if m.role == Membership.Role.TENANT_ADMIN]
    return {"org_admin": u.is_org_admin, "tenant_admin": bool(admin_of), "read_only": u.is_org_readonly,
            "admin_of": admin_of, "memberships": ms}


def staff_org_role_rows(org=None, show_archived=False):
    """owner 2026-11-03: platform (staff) users who hold an org role — org admin, or project admin of a corporate
    project — appear in the org-users lists too, shown as org users. They are read-only here (their roles are managed
    from Platform users and the project's team), so the role cells are not editable in this list."""
    from tenants.models import Membership
    grouped = {}  # (user_id, org_pk) -> {"u", "org", "admin_of": [tenants]}
    # owner 2026-11-03: only created (approved, live) projects — a membership on a not-yet-created or archived project
    # must not show the platform user as its project admin.
    ms = (Membership.objects.filter(role=Membership.Role.TENANT_ADMIN, user__kind=Kind.STAFF, tenant__approval="approved")
          .exclude(tenant__status__in=["archived", "saved", "deleting"])
          .select_related("user", "tenant", "tenant__organization"))
    if org is not None:
        ms = ms.filter(tenant__organization=org)
    for m in ms:
        o = m.tenant.organization
        if o is None:
            continue
        grouped.setdefault((m.user_id, o.pk), {"u": m.user, "org": o, "admin_of": []})["admin_of"].append(m.tenant)
    oa = User.objects.filter(kind=Kind.STAFF, is_org_admin=True, organization__isnull=False).select_related("organization")
    if org is not None:
        oa = oa.filter(organization=org)
    for u in oa:
        grouped.setdefault((u.pk, u.organization_id), {"u": u, "org": u.organization, "admin_of": []})
    users = [d["u"] for d in grouped.values()]
    ids, emails = _open_invites(users) if users else (frozenset(), frozenset())
    rows = []
    for d in grouped.values():
        u, o = d["u"], d["org"]
        st = state(u, ids, emails)
        if st == "archived" and not show_archived:
            continue
        info = {"org_admin": bool(u.is_org_admin and u.organization_id == o.pk), "tenant_admin": bool(d["admin_of"]),
                "read_only": False, "admin_of": d["admin_of"], "memberships": []}
        roles = [(k, full, info[k]) for k, _short, full in CUSTOMER_ROLE_COLUMNS]
        rows.append({"u": u, "rowid": f"u-{u.pk}-o{o.pk}", "state": st, "label": STATE_LABELS[st], "org": o,
                     "deletable": False, "roles": roles, "roles_label": _roles_label(roles), "info": info,
                     "platform": True})
    return rows


def customer_rows(org=None, show_archived=False):
    qs = User.objects.filter(kind=Kind.CUSTOMER).select_related("organization")
    if org is not None:
        qs = qs.filter(organization=org)
    users = list(qs.prefetch_related("memberships__tenant").order_by("organization__name", "full_name", "email"))
    ids, emails = _open_invites(users)
    rows = []
    for u in users:
        st = state(u, ids, emails)
        if st == "archived" and not show_archived:
            continue
        r = customer_roles(u)
        roles = [(k, full, r[k]) for k, _short, full in CUSTOMER_ROLE_COLUMNS]
        rows.append({"u": u, "rowid": f"u-{u.pk}", "state": st, "label": STATE_LABELS[st], "org": u.organization,
                     "deletable": never_accepted(u), "roles": roles, "roles_label": _roles_label(roles), "info": r})
    # platform users who hold an org role show here too, read-only
    rows += staff_org_role_rows(org, show_archived)
    rows.sort(key=lambda r: ((r["org"].name if r.get("org") else ""), (r["u"].full_name or r["u"].email) if r.get("u") else ""))
    # invitations of people who have no account yet
    known = {u.email.lower() for u in users}
    inv = Invitation.objects.open().filter(purpose=Invitation.Purpose.CUSTOMER)
    if org is not None:
        inv = inv.filter(organization=org)
    for i in inv.select_related("organization"):
        if i.email.lower() in known:
            continue
        g = i.grants or {}
        roles = [("org_admin", CUSTOMER_ROLES[0][1], bool(g.get("org_admin"))),
                 ("tenant_admin", CUSTOMER_ROLES[1][1],
                  any(v == "tenant_admin" for v in (g.get("tenants") or {}).values())),
                 ("read_only", CUSTOMER_ROLES[2][1], bool(g.get("read_only")))]
        rows.append({"u": None, "rowid": f"i-{i.pk}", "invitation": i, "email": i.email, "state": "invited",
                     "label": STATE_LABELS["invited"], "org": i.organization, "roles": roles,
                     "roles_label": _roles_label(roles)})
    return rows


# --- actions ---------------------------------------------------------------------------------------------

def set_staff_roles(actor, person, fields):
    """Give a platform user exactly these roles; every role must keep at least one holder (an inactive, not-yet-invited
    holder counts — owner 2026-11-03). Returns errors.

    Correction 16: every super admin's roles can be edited, their own too. The super admin role itself is never
    taken from oneself; taken from another super admin it is only asked for (superadmins.request) — the other roles
    are saved at once. person.sa_removal: that request."""
    from . import roles as R
    from . import superadmins
    fields = set(fields) & {f for f, _l2 in STAFF_ROLES}
    person.sa_removal = None
    remove_sa = person.is_super_admin and "is_super_admin" not in fields
    add_sa = not person.is_super_admin and "is_super_admin" in fields  # owner u12: granting SA also needs approval
    if remove_sa and person.pk == actor.pk:
        return [_("You cannot take the super admin role from yourself.")]
    if (remove_sa or add_sa) and not actor.is_super_admin:
        return [_("Only a super admin can change the super admin role.")]
    if remove_sa:
        fields.add("is_super_admin")  # stays until the super admins confirm
    if add_sa:
        fields.discard("is_super_admin")  # not granted until the super admins confirm
    old = {f: getattr(person, f) for f, _l2 in STAFF_ROLES}
    for f, _l2 in STAFF_ROLES:
        setattr(person, f, f in fields)
    people = [person if u.pk == person.pk else u for u in R.staff()]
    # owner 2026-11-03: an inactive (not-yet-invited) holder is enough to keep a role, so it may be removed from an
    # active user as long as any user still holds it.
    before = set(R.missing(require_activated=False))
    left = [f for f in R.missing(people, require_activated=False) if f not in before or old.get(f)]
    errors = [_("%(r)s: at least one user must hold it.") % {"r": dict(STAFF_ROLES)[f]} for f in left]
    if errors:
        for f, v in old.items():
            setattr(person, f, v)
        return errors
    person.is_staff = person.is_superuser = person.is_super_admin
    changed = [f for f, v in old.items() if getattr(person, f) != v]
    if changed:
        person.save(update_fields=[f for f, _l2 in STAFF_ROLES] + ["is_staff", "is_superuser"])
        audit(actor, "staff_roles_changed", person, roles=",".join(person.staff_role_labels) or "-")
    if remove_sa:
        person.sa_removal, err = superadmins.request(actor, person)
        if err:
            return [err]
    if add_sa:
        person.sa_removal, err = superadmins.request_add(actor, person)
        if err:
            return [err]
    return []


def other_active_org_admins(person):
    """The organization's other active org admins (owner 2026-11-03: an organization keeps at least one)."""
    if person.organization_id is None:
        return User.objects.none()
    return User.objects.filter(organization_id=person.organization_id, kind=Kind.CUSTOMER,
                               is_org_admin=True, is_active=True).exclude(pk=person.pk)


def set_customer_roles(actor, person, org_admin, read_only, tenant_admin_of):
    """Org admin / read only / project admin of the given projects. A user may hold both the org admin and the project
    admin role at once (owner 2026-11-03). Returns "" on success, or an error when it would leave the organization
    without an admin."""
    from tenants.models import Membership
    org_admin = bool(org_admin)
    if person.is_org_admin and not org_admin and not other_active_org_admins(person).exists():
        return _("%(e)s is the organization's only administrator. Give the org admin role to someone else first.") % {
            "e": person.email}
    # owner 2026-11-03: read-only is incompatible with any other org role (org admin or project admin)
    read_only = bool(read_only) and not org_admin and not tenant_admin_of
    with transaction.atomic():
        person.is_org_admin, person.is_org_readonly = org_admin, read_only
        person.save(update_fields=["is_org_admin", "is_org_readonly"])
        keep = {t.pk for t in tenant_admin_of}
        for t in tenant_admin_of:
            Membership.objects.update_or_create(user=person, tenant=t, defaults={"role": Membership.Role.TENANT_ADMIN})
        Membership.objects.filter(user=person, role=Membership.Role.TENANT_ADMIN).exclude(tenant_id__in=keep).delete()
    audit(actor, "member_roles_changed", person, org_admin=person.is_org_admin, read_only=person.is_org_readonly,
          tenant_admin=",".join(str(t) for t in tenant_admin_of))
    return ""


def reassign_admin_projects(person, actor=None):
    """Hand this person's active projects (where they are the project admin) to another org user, so every project
    keeps a project admin (owner 2026-11-03). Used when the person is archived or blocked."""
    from tenants.admins import proposed, set_tenant_admin
    from tenants.models import Tenant
    moved = []
    for t in Tenant.objects.filter(tenant_admin=person).exclude(status="archived").select_related("organization"):
        if t.is_deleted:
            continue
        u, why = proposed(t, exclude=person)
        if u is not None and u.pk != person.pk:
            set_tenant_admin(t, u, actor, why=why or "previous admin archived")
            moved.append(t.name)
    return moved


def block(actor, person):
    from . import roles as R
    if person.pk == actor.pk:
        return _("You cannot block your own account.")
    if person.is_active and person.kind == Kind.STAFF:  # correction 16: every role keeps an active user
        before = set(R.missing())
        person.is_active = False
        left = [f for f in R.missing([person if u.pk == person.pk else u for u in R.staff()]) if f not in before]
        person.is_active = True
        if left:
            return _("%(e)s is the only active user with: %(r)s. Give the role to someone else first.") % {
                "e": person.email, "r": ", ".join(str(dict(STAFF_ROLES)[f]) for f in left)}
    if person.is_active and person.kind == Kind.CUSTOMER and person.is_org_admin and not other_active_org_admins(person).exists():
        return _("%(e)s is the organization's only administrator. Give the org admin role to someone else first.") % {
            "e": person.email}
    person.is_active = not person.is_active
    person.save(update_fields=["is_active"])
    if not person.is_active and person.kind == Kind.CUSTOMER:  # a blocked user cannot stay a project's admin
        reassign_admin_projects(person, actor)
    audit(actor, "user_unblocked" if person.is_active else "user_blocked", person)
    return ""


def clear_roles(person):
    """No roles: an archived person, or a platform user whose invitation is gone (a sales rep directory entry keeps
    its place in the directory)."""
    if person.kind == Kind.STAFF:
        directory = person.is_sales and not person.login_enabled and not person.archived_at
        for f, _l2 in STAFF_ROLES:
            setattr(person, f, False)
        person.is_sales = directory
        person.is_staff = person.is_superuser = False
        person.save(update_fields=[f for f, _l2 in STAFF_ROLES] + ["is_staff", "is_superuser"])
    else:
        person.is_org_admin = person.is_org_readonly = False
        person.save(update_fields=["is_org_admin", "is_org_readonly"])
        person.memberships.all().delete()


def delete(actor, person):
    """Someone who never accepted an invitation is deleted (with the invitations sent), not archived."""
    from django.db.models import ProtectedError
    if person.pk == actor.pk:
        return _("You cannot delete your own account.")
    if not never_accepted(person):
        return _("%(email)s has had an account: archive instead.") % {"email": person.email}
    email = person.email
    try:
        with transaction.atomic():
            Invitation.objects.filter(user=person).delete()
            Invitation.objects.filter(email__iexact=email, accepted_at__isnull=True).delete()
            person.delete()
    except ProtectedError:
        return _("%(email)s is still referred to (an organization or a project): archive instead.") % {"email": email}
    audit(actor, "user_deleted", None, email=email)
    return ""


def archive(actor, person):
    """Archive someone who had an account: no sign-in, no roles, no open invitation; kept for the record."""
    from . import roles as R
    if person.pk == actor.pk:
        return _("You cannot archive your own account.")
    # owner 2026-11-03: users are never deleted, only archived — the record and the audit/resource history stay
    if person.kind == Kind.STAFF:
        others = [u for u in R.staff() if u.pk != person.pk]
        left = R.missing(others)
        if left:
            return _("Give %(r)s to someone else first.") % {"r": ", ".join(str(dict(STAFF_ROLES)[f]) for f in left)}
    if person.kind == Kind.CUSTOMER and person.is_org_admin and not other_active_org_admins(person).exists():
        return _("%(e)s is the organization's only administrator. Give the org admin role to someone else first.") % {
            "e": person.email}
    with transaction.atomic():
        if person.kind == Kind.CUSTOMER:  # every project keeps a project admin: hand theirs on before they lose roles
            reassign_admin_projects(person, actor)
        person.archived_at, person.is_active, person.login_enabled = timezone.now(), False, False
        person.save(update_fields=["archived_at", "is_active", "login_enabled"])
        Invitation.objects.open().filter(user=person).update(revoked_at=timezone.now())
        Invitation.objects.open().filter(email__iexact=person.email).update(revoked_at=timezone.now())
        clear_roles(person)
    audit(actor, "user_archived", person, email=person.email)
    return ""


def report(request, err, ok):
    if err:
        messages.error(request, err)
    else:
        messages.success(request, ok)
