"""corrections-18: moving a tenant to another organization.

Staff ask (the tenant's Edit → Move to another organization: a list of the organizations, IIN/BIN first, by name);
the resource manager and the target organization's sales rep (when it has one) approve. A person in both roles
approves once; the one asking approves in the roles they hold. A super admin may approve any step; they are asked only
in the roles ticked for them. When every approval is there the tenant moves; its tenant admin is kept when they can
be one in the new organization, otherwise the new organization's user proposed for it takes over.
"""
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from accounts.models import Role, audit

from .models import TenantMove


def sales_rep(move):
    return move.to_org.sales_rep if move.to_org_id else None


def steps(move):
    out = [{"key": "rm", "label": _("Resource manager"), "done": bool(move.rm_at), "by": move.rm_by}]
    if sales_rep(move) is not None:
        out.append({"key": "sales", "label": _("Sales rep"), "done": bool(move.sales_at), "by": move.sales_by})
    return out


def missing(move):
    return [s["key"] for s in steps(move) if not s["done"]]


def can(user, move, step):
    if not (user.is_authenticated and user.is_staff_member) or move.state != TenantMove.State.PENDING:
        return False
    if user.is_super_admin:
        return True
    if step == "rm":
        return move.tenant.can_approve_as(user, Role.RESOURCE_MANAGER)
    rep = sales_rep(move)
    return step == "sales" and rep is not None and rep.pk == user.pk


def asks(user, move, step):
    """The step waits for this person (a super admin: only in the roles ticked)."""
    if not can(user, move, step):
        return False
    if step == "rm":
        return move.tenant.asks(user, Role.RESOURCE_MANAGER)
    rep = sales_rep(move)
    if rep is not None and rep.pk == user.pk:
        return True
    return user.is_super_admin and user.holds(Role.SALES_MANAGER) and not (rep and rep.is_active and rep.login_enabled)


def approvers(move, step):
    from accounts.models import User
    if step == "rm":
        return move.tenant.approvers(Role.RESOURCE_MANAGER)
    rep = sales_rep(move)
    if rep is not None and rep.is_active and rep.login_enabled:
        return [rep]
    supers = User.objects.filter(kind="staff", is_super_admin=True, is_active=True, login_enabled=True)
    return list(supers.filter(is_sales=True)) or list(supers)


def pending_for_tenant(tenant):
    return tenant.moves.filter(state=TenantMove.State.PENDING).select_related("to_org", "from_org").first()


def _tell(move):
    from accounts.utils import notify, site_link
    people = {}
    for step in missing(move):
        for u in approvers(move, step):
            people[u.pk] = u
    if people:
        notify(list(people.values()), "tenant_move_requested",
               {"move": move, "tenant": move.tenant, "link": site_link(reverse("tenant_detail", args=[move.tenant_id]))},
               _("Project %(t)s: move to %(o)s waits for your approval") % {"t": move.tenant.name or move.tenant.title,
                                                                          "o": move.to_org.name})


def _cover(move, user):
    """The approvals this person gives by asking or approving: every step they are asked for (or may do)."""
    now = timezone.now()
    for step in missing(move):
        if asks(user, move, step) or (step == "rm" and user.holds(Role.RESOURCE_MANAGER) and can(user, move, step)):
            setattr(move, f"{step}_by", user)
            setattr(move, f"{step}_at", now)


def request(actor, tenant, to_org):
    """Ask to move the tenant. Returns (move, error)."""
    if to_org is None or to_org.pk == tenant.organization_id:
        return None, _("Choose another organization.")
    if pending_for_tenant(tenant) is not None:
        return None, _("A move of this project is already waiting for approval.")
    move = TenantMove.objects.create(tenant=tenant, from_org=tenant.organization, to_org=to_org, requested_by=actor)
    _cover(move, actor)
    move.save()
    audit(actor, "tenant_move_requested", tenant, frm=move.from_org.name if move.from_org else "", to=to_org.name,
          approved=", ".join(s["key"] for s in steps(move) if s["done"]))
    if not complete_if_ready(move, actor):
        _tell(move)
    return move, ""


def approve(actor, move, step):
    if step not in missing(move) or not can(actor, move, step):
        return _("This step is not waiting for you.")
    now = timezone.now()
    setattr(move, f"{step}_by", actor)
    setattr(move, f"{step}_at", now)
    _cover(move, actor)
    move.save()
    audit(actor, "tenant_move_approved", move.tenant, step=step, to=move.to_org.name)
    if not complete_if_ready(move, actor):
        _tell(move)
    return ""


def reject(actor, move, reason):
    if move.state != TenantMove.State.PENDING or not any(can(actor, move, s) for s in missing(move)):
        return _("This step is not waiting for you.")
    move.state, move.reason, move.finished_at = TenantMove.State.REJECTED, reason[:2000], timezone.now()
    move.save(update_fields=["state", "reason", "finished_at"])
    audit(actor, "tenant_move_rejected", move.tenant, to=move.to_org.name, reason=reason[:300])
    return ""


def complete_if_ready(move, actor=None):
    """Move the tenant when nothing is missing."""
    from adoption.grouping import move_tenant
    from .admins import admin_candidates, proposed, set_tenant_admin
    if move.state != TenantMove.State.PENDING or missing(move):
        return False
    t = move.tenant
    with transaction.atomic():
        move_tenant(t, move.to_org, actor)
        if t.tenant_admin_id and not admin_candidates(move.to_org).filter(pk=t.tenant_admin_id).exists():
            old = t.tenant_admin
            t.tenant_admin = None
            t.save(update_fields=["tenant_admin"])
            audit(actor, "tenant_admin_cleared", t, user=old.email, why="moved to another organization")
            u, why = proposed(t)
            if u is not None:
                set_tenant_admin(t, u, actor, why=why)
        move.state, move.finished_at = TenantMove.State.DONE, timezone.now()
        move.save(update_fields=["state", "finished_at"])
    return True
