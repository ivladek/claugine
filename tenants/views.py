"""Every page of a project: the order, the approval, the project page and its tabs, and the staff lists around them.

The largest module in the project and deliberately flat — one view per address, each gated through
`tenants/permissions.py` so views and templates cannot disagree about who may do what. The work itself lives in
the modules these views call: `ordering.py`, `provision.py`, `deploy.py`, `netops.py`, `lifecycle.py`.
"""

from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.forms import CustomerInviteForm
from accounts.models import AuditLog, Invitation, Role, User, audit
from accounts.utils import customer_required, notify, site_link, staff_required

from . import ordering
from .forms import (
    MergeForm,
    OrderForm,
    OrganizationForm,
    RejectForm,
    RMApprovalForm,
    StageForm,
    TenantMoveForm,
    TenantStaffForm,
)
from .models import (
    Approval,
    JoinRequest,
    Lifecycle,
    Membership,
    Organization,
    OrgKind,
    PortalSettings,
    PrefixAssignment,
    Tenant,
    TenantKind,
    TenantStage,
)

STAFF_VIEW_ROLES = (Role.RESOURCE_MANAGER, Role.SUPPORT, Role.SALES_MANAGER, Role.KAM)


def _visible_tenants(user):
    qs = Tenant.objects.select_related("organization", "organization__sales_rep")
    if user.is_super_admin or user.has_role(Role.RESOURCE_MANAGER) or user.has_role(Role.SUPPORT):
        return qs
    if user.is_sales or user.is_kam:
        q = Q()
        if user.is_sales:
            q |= Q(organization__sales_rep=user)
        if user.is_kam:
            q |= Q(organization__kam=user)
        return qs.filter(q)
    return qs.none()


# --- staff: tenants ------------------------------------------------------------

TENANT_TABS = ("active", "requests")
APPROVAL_TABS = ("pending", "approved", "closed")


@staff_required(*STAFF_VIEW_ROLES)
def tenant_list_view(request):
    """Cloud → Tenants: approved tenants (default) and requests that are not tenants yet; archived ones on demand.
    What happened to a tenant is in Logs (its History button)."""
    from .models import Lifecycle

    tab = request.GET.get("tab", "active")
    tab = tab if tab in TENANT_TABS else "active"
    show_archived = request.GET.get("archived") == "1"
    visible = _visible_tenants(request.user)
    if not show_archived:
        visible = visible.exclude(status=Lifecycle.ARCHIVED)
    counts = {
        "active": visible.filter(approval=Approval.APPROVED).count(),
        "requests": visible.exclude(approval=Approval.APPROVED).count(),
    }
    tenants = (
        visible.filter(approval=Approval.APPROVED) if tab == "active" else visible.exclude(approval=Approval.APPROVED)
    )
    f = {k: request.GET.get(k, "").strip() for k in ("q", "approval", "kind", "stage", "status", "gaps")}
    if f["q"]:
        tenants = tenants.filter(
            Q(name__icontains=f["q"])
            | Q(title__icontains=f["q"])
            | Q(group_name__icontains=f["q"])
            | Q(organization__name__icontains=f["q"])
            | Q(organization__tax_id__icontains=f["q"])
        )
    for key in ("approval", "kind", "stage", "status"):
        if f[key]:
            tenants = tenants.filter(**{key: f[key]})
    if f["gaps"]:
        tenants = tenants.filter(
            Q(organization__isnull=True)
            | Q(organization__tax_id__isnull=True)
            | Q(organization__sales_rep__isnull=True, kind=TenantKind.CLOUD)
        )
    tenants = list(tenants.select_related("organization").order_by("organization__name", "name"))
    from adoption.models import PlatformStats

    return render(
        request,
        "tenants/list.html",
        {
            "tab": tab,
            "counts": counts,
            "show_archived": show_archived,
            "refreshed_at": PlatformStats.objects.order_by("-collected_at")
            .values_list("collected_at", flat=True)
            .first(),
            "tenants": tenants,
            "f": f,
            "approvals": [c for c in Approval.choices if c[0] != Approval.APPROVED],
            "kinds": TenantKind.choices,
            "stages": TenantStage.choices,
            "statuses": Lifecycle.choices,
            "can_add": request.user.is_super_admin or request.user.has_role(Role.RESOURCE_MANAGER),
        },
    )


def back_to(request, default):
    """Redirect to the page the action came from (the approval list), else to `default`."""
    from django.utils.http import url_has_allowed_host_and_scheme

    nxt = request.POST.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(
        nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(nxt)
    return redirect(default)


def _safe_next(request, default):
    """u11: the return URL from ?next= / next= (projects list at the row), else `default`."""
    from django.utils.http import url_has_allowed_host_and_scheme

    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(
        nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return nxt
    return default


def waiting_items(user):
    """Everything waiting for approval that this person may see: new tenants, resource changes, networks, deletions."""
    from .network_views import can_decide

    items = []
    for t in _visible_tenants(user).filter(approval=Approval.PENDING).select_related("created_by"):
        steps = []
        for role in t.required_approvals():
            done = t.approved_by_role(role)
            steps.append(
                {
                    "role": t.step_label(role),
                    "done": done,
                    "people": [] if done else t.approvers(role),
                    "mine": not done and tenant_asks(t, user, role),
                    "key": role,
                    "can": not done and tenant_can_approve(t, user, role),
                }
            )
        mine_roles = [st["key"] for st in steps if st["can"]]
        items.append(
            {
                "kind": "tenant",
                "label": _("New project"),
                "t": t,
                "at": t.created_at,
                "by": t.created_by,
                "what": ordering.describe(t.order or {}),
                "res": t.order or {},
                "steps": steps,
                "mine": any(st["mine"] for st in steps),
                "mine_roles": mine_roles,
                "can_reject": bool(mine_roles) or t.can_approve_as(user, Role.RESOURCE_MANAGER),
            }
        )
    for c in _pending_changes(user).select_related("tenant", "tenant__organization", "requested_by"):
        items.append(
            {
                "kind": "resources",
                "label": _("Resource change"),
                "t": c.tenant,
                "at": c.created_at,
                "by": c.requested_by,
                "obj": c,
                "mine": c.tenant.asks(user, Role.RESOURCE_MANAGER),
                "can": c.tenant.can_approve_as(user, Role.RESOURCE_MANAGER),
                "what": f"{c.cpu} vCPU · {c.ram_gb} GB RAM"
                + (f" · {c.gpu} GPU" if c.gpu else "")
                + f" · {c.disk_gb} GB ({c.vms_gb} / {c.images_gb})",
            }
        )
    for r in _pending_networks(user).select_related("tenant", "tenant__organization", "requested_by"):
        items.append(
            {
                "kind": "network",
                "label": f"{r.get_kind_display()}: {r.get_action_display()}",
                "t": r.tenant,
                "at": r.created_at,
                "by": r.requested_by,
                "obj": r,
                "mine": can_decide(r.tenant, user),
                "what": r.what,
                "net_kind": r.kind,
            }
        )
    # correction 13: approved tenants waiting for Provision, L2 channels waiting for their VLAN
    from .provision import can_provision, provisioned

    for t in (
        _visible_tenants(user)
        .filter(approval=Approval.APPROVED)
        .filter(Q(one_group_id__isnull=True) | Q(deployments__state="failed"))
        .exclude(status=Lifecycle.ARCHIVED)
        .distinct()
        .select_related("organization")
    ):
        if not provisioned(t) and not t.is_deleted:
            items.append(
                {
                    "kind": "provision",
                    "label": _("Provision"),
                    "t": t,
                    "at": t.approved_at or t.created_at,
                    "by": None,
                    "obj": None,
                    "mine": can_provision(t, user),
                    "what": "",
                }
            )
    from .models import L2Channel
    from .network_views import can_set_vlan

    for ch in L2Channel.objects.filter(
        state=L2Channel.State.PLANNED, vlan__isnull=True, tenant__in=_visible_tenants(user)
    ).select_related("tenant", "tenant__organization"):
        items.append(
            {
                "kind": "vlan",
                "label": _("L2: VLAN"),
                "t": ch.tenant,
                "at": ch.created_at,
                "by": None,
                "obj": ch,
                "mine": can_set_vlan(ch.tenant, user),
                "what": ch.name,
            }
        )
    for d in _pending_deletions(user).select_related("tenant", "tenant__organization", "requested_by"):
        items.append(
            {
                "kind": "deletion",
                "label": _("Deletion"),
                "t": d.tenant,
                "at": d.created_at,
                "by": d.requested_by,
                "obj": d,
                "mine": d.tenant.asks(user, Role.RESOURCE_MANAGER),
                "can": d.tenant.can_approve_as(user, Role.RESOURCE_MANAGER),
                "what": d.note,
            }
        )
    from . import moves as mv  # corrections-18: moves to another organization
    from .models import TenantMove

    for m in TenantMove.objects.filter(
        state=TenantMove.State.PENDING, tenant__in=_visible_tenants(user)
    ).select_related("tenant", "tenant__organization", "from_org", "to_org", "requested_by"):
        miss = mv.missing(m)
        items.append(
            {
                "kind": "move",
                "label": _("Move"),
                "t": m.tenant,
                "at": m.created_at,
                "by": m.requested_by,
                "obj": m,
                "what": f"{m.from_org.name if m.from_org else '—'} → {m.to_org.name}",
                "mine": any(mv.asks(user, m, s) for s in miss),
                "can": any(mv.can(user, m, s) for s in miss),
                "move_steps": [s for s in mv.steps(m) if not s["done"] and mv.can(user, m, s["key"])],
                "move_wait": [str(s["label"]) for s in mv.steps(m) if not s["done"]],
            }
        )
    for i in items:
        i.setdefault("can", i["mine"])  # may act (the super admin: always); "mine": waiting for this person
    items.sort(key=lambda x: x["at"])
    return items


@staff_required(*STAFF_VIEW_ROLES)
def approvals_view(request):
    """Administration → Approval requests: everything waiting (tenants, resources, networks, deletions), decisions."""
    user = request.user
    tab = request.GET.get("tab", "pending")
    if tab not in APPROVAL_TABS:
        tab = "pending"
    qs = _visible_tenants(user).select_related("created_by")
    since = timezone.now() - timedelta(days=30)
    items = waiting_items(user)
    from .orgreg import pending_rows

    orgs = pending_rows(user)  # correction 16: organizations registered on the organizations' portal
    counts = {
        "pending": len(items) + len(orgs),
        "mine": sum(1 for i in items if i["mine"]) + sum(1 for o in orgs if o["mine"]),
        "approved": qs.filter(approval=Approval.APPROVED, approved_at__gte=since).count(),
        "closed": qs.filter(approval__in=[Approval.REJECTED, Approval.CANCELLED], created_at__gte=since).count(),
    }
    mine = bool(request.GET.get("mine"))
    if tab == "pending":
        return render(
            request,
            "tenants/approvals.html",
            {
                "items": [i for i in items if i["mine"]] if mine else items,
                "tab": tab,
                "counts": counts,
                "mine": mine,
                "orgs": [o for o in orgs if o["mine"]] if mine else orgs,
                "reject_form": RejectForm(prefix="rej"),
            },
        )
    if tab == "approved":
        qs = qs.filter(approval=Approval.APPROVED, approved_at__gte=since).order_by("-approved_at")
    else:
        qs = qs.filter(approval__in=[Approval.REJECTED, Approval.CANCELLED], created_at__gte=since).order_by(
            "-created_at"
        )
    rows = [{"t": t, "order": ordering.describe(t.order or {}), "res": t.order or {}} for t in qs]
    return render(request, "tenants/approvals.html", {"rows": rows, "tab": tab, "counts": counts, "mine": False})


def _pending_changes(user):
    from .models import ResourceChange

    return ResourceChange.objects.filter(
        state=ResourceChange.State.PENDING, tenant__in=_visible_tenants(user)
    ).order_by("created_at")


def _pending_networks(user):
    from .models import NetworkRequest

    return NetworkRequest.objects.filter(
        state=NetworkRequest.State.PENDING, tenant__in=_visible_tenants(user)
    ).order_by("created_at")


def _pending_deletions(user):
    from .models import TenantDeletion

    return TenantDeletion.objects.filter(
        state=TenantDeletion.State.PENDING, tenant__in=_visible_tenants(user)
    ).order_by("created_at")


def _move_context(tenant, user):
    """corrections-18: a move to another organization waiting for approval, with the steps this person can give."""
    from . import moves

    m = moves.pending_for_tenant(tenant)
    if m is None:
        return None
    return {
        "m": m,
        "steps": moves.steps(m),
        "mine": [s for s in moves.steps(m) if not s["done"] and moves.can(user, m, s["key"])],
    }


def tenant_can_approve(tenant, user, role):
    if role == Role.SALES_MANAGER:
        return tenant.can_approve_as(user, role) and PortalSettings.get().sales_approval_required
    return tenant.can_approve_as(user, role)


def tenant_asks(tenant, user, role):
    """Correction 16: the approval waits for this person (a super admin: only in the roles ticked for them)."""
    return tenant_can_approve(tenant, user, role) and tenant.asks(user, role)


def pending_for(user):
    """How many pending requests this person can act on now (for the menu badge)."""
    if not (user.is_authenticated and user.is_staff_member):
        return 0
    n = 0
    for t in _visible_tenants(user).filter(approval=Approval.PENDING):
        if any(tenant_asks(t, user, r) for r in t.missing_approvals()):
            n += 1
    n += sum(1 for c in _pending_changes(user).select_related("tenant") if c.tenant.asks(user, Role.RESOURCE_MANAGER))
    from .network_views import can_decide

    n += sum(1 for r in _pending_networks(user).select_related("tenant") if can_decide(r.tenant, user))
    n += sum(1 for d in _pending_deletions(user).select_related("tenant") if d.tenant.asks(user, Role.RESOURCE_MANAGER))
    from .orgreg import pending_count

    n += pending_count(user)
    from . import moves as mv
    from .models import TenantMove

    n += sum(
        1
        for m in TenantMove.objects.filter(state=TenantMove.State.PENDING, tenant__in=_visible_tenants(user))
        if any(mv.asks(user, m, s) for s in mv.missing(m))
    )
    return n


def _tenant_for(request, pk):
    tenant = get_object_or_404(Tenant.objects.select_related("organization"), pk=pk)
    if not tenant.visible_to(request.user):
        raise PermissionDenied
    return tenant


@login_required
def tenant_detail_view(request, pk):
    tenant = _tenant_for(request, pk)
    user = request.user
    staff = user.is_staff_member
    is_rm = user.has_role(Role.RESOURCE_MANAGER)
    is_support = user.has_role(Role.SUPPORT)
    pending = tenant.approval == Approval.PENDING
    missing = tenant.missing_approvals() if pending else []
    my_role = tenant.membership_role(user)
    sales_on = PortalSettings.get().sales_approval_required
    is_rep = user.is_sales and tenant.sales_rep and tenant.sales_rep.pk == user.pk
    ctx = {
        "tenant": tenant,
        "staff": staff,
        "my_role": my_role,
        "missing": missing,
        "missing_labels": tenant.missing_labels() if pending else [],
        "rm_by_super": tenant.rm_step_by_super_admin(),
        "records": _with_usage(tenant, _records_with_split(tenant.current_quota_records())),
        "connectivity": tenant.current_connectivity(),
        "prefixes": PrefixAssignment.active().filter(tenant=tenant),
        "members": Membership.objects.filter(tenant=tenant).select_related("user"),
        "org_admins": tenant.organization.admins if tenant.organization_id else [],
        "pending_move": _move_context(tenant, user),
        "can_rm_approve": Role.RESOURCE_MANAGER in missing and tenant.can_approve_as(user, Role.RESOURCE_MANAGER),
        "can_support_approve": Role.SUPPORT in missing and tenant.can_approve_as(user, Role.SUPPORT),
        "can_sales_approve": is_rep and Role.SALES_MANAGER in missing,
        "can_reject": pending
        and (
            tenant.can_approve_as(user, Role.RESOURCE_MANAGER)
            or (Role.SUPPORT in missing and tenant.can_approve_as(user, Role.SUPPORT))
            or (is_rep and sales_on)
        ),
        "can_edit": staff and (is_rm or user.is_super_admin),
        "can_edit_resources": staff and (is_rm or user.is_super_admin) and tenant.approval == Approval.APPROVED,
        "can_cancel": tenant.can_cancel(user),
        "can_delete": tenant.can_delete(user),
        "can_stage": tenant.can_change_stage(user) and tenant.approval == Approval.APPROVED and not tenant.is_archived,
        "can_status": tenant.can_change_status(user),
        "status_form": __import__("tenants.forms", fromlist=["StatusForm"]).StatusForm(
            tenant=tenant, prefix="ss", keep_option=True
        ),
        "can_review": tenant.can_change_review(user) and tenant.approval == Approval.APPROVED,
        "review_form": __import__("tenants.forms", fromlist=["ReviewForm"]).ReviewForm(instance=tenant, prefix="rv"),
        "can_history": staff or my_role in (Membership.Role.ORG_ADMIN, Membership.Role.TENANT_ADMIN),
        "stage_form": StageForm(instance=tenant, prefix="st"),
        "move_form": TenantMoveForm(prefix="mv", tenant=tenant) if staff and is_rm and not tenant.is_archived else None,
        "order_summary": ordering.describe(tenant.order or {}),
        "can_invite": my_role in (Membership.Role.ORG_ADMIN, Membership.Role.TENANT_ADMIN),
        "deployment": tenant.deployments.first(),
        "changes": tenant.resource_changes.select_related("requested_by", "decided_by")[:10],
        "can_request_change": tenant.approval == Approval.APPROVED
        and not tenant.is_deleted
        and (my_role in (Membership.Role.ORG_ADMIN, Membership.Role.TENANT_ADMIN) or user.is_super_admin),
        "can_decide_change": tenant.can_approve_as(user, Role.RESOURCE_MANAGER),
        "can_split": tenant.approval == Approval.APPROVED
        and not tenant.is_deleted
        and (
            my_role in (Membership.Role.ORG_ADMIN, Membership.Role.TENANT_ADMIN)
            or user.is_super_admin
            or user.has_role(Role.RESOURCE_MANAGER)
        ),
        "gateways": __import__("tenants.gwstatus", fromlist=["rows"]).rows(tenant)
        if tenant.group_name and tenant.one_group_id is not None and not tenant.is_deleted
        else [],  # once provisioned
        "can_redeploy_gw": can_redeploy_gateways(user) and not tenant.is_deleted,
        # correction 12: the igw's type, networks and configurations — the tenant's admins and the super admin
        "can_edit_igw": (my_role in (Membership.Role.ORG_ADMIN, Membership.Role.TENANT_ADMIN) or user.is_super_admin)
        and tenant.approval == Approval.APPROVED
        and not tenant.is_deleted,
        "can_see_access": my_role in (Membership.Role.ORG_ADMIN, Membership.Role.TENANT_ADMIN) or user.is_super_admin,
        "igw_manageable": _igw_manageable(tenant),
        "igw_main_ip": _igw_main_ip(tenant),
        **_revealed(request, tenant),
        "rm_form": RMApprovalForm(instance=tenant, prefix="rm"),
        "edit_form": TenantStaffForm(instance=tenant, prefix="e"),
        "corp_staff": corp_staff_candidates()
        if staff and tenant.kind == TenantKind.CORP and (is_rm or user.is_super_admin)
        else None,
        "reject_form": RejectForm(prefix="rej"),
        "deploy_blocked": (
            __import__("tenants.deploy", fromlist=["blocked_reason"]).blocked_reason(tenant)
            if staff and tenant.approval == Approval.APPROVED
            else ""
        ),
    }
    if ctx["can_invite"]:
        ctx["invite_form"] = CustomerInviteForm(prefix="inv", tenants=[tenant], allow_org_admin=False)
    # correction 15: one Edit dialog for everything of the details; the tenant admin; resend the credentials
    from .admins import choices as admin_choices

    ctx["can_admin"] = staff and (is_rm or user.is_super_admin or is_rep) and not tenant.is_archived
    ctx["admin_choices"] = admin_choices(tenant.organization) if ctx["can_admin"] else []
    ctx["can_edit_all"] = staff and any(
        ctx.get(k) for k in ("can_edit", "can_stage", "can_status", "can_review", "move_form", "can_admin")
    )
    from .provision import can_resend

    ctx["can_resend"] = can_resend(tenant, user)
    ctx["can_order_pools"] = _can_order_pools(request, tenant)
    if ctx["can_order_pools"]:
        from .forms import PoolOrderForm

        ctx["pool_form"] = PoolOrderForm(prefix="po", tenant=tenant)
        import json as _json

        ctx["pool_info"] = _json.dumps(
            {
                k: {"gpu": bool(p.get("gpu")), "new_site": p["site"] not in ctx["pool_form"].sites}
                for k, p in ctx["pool_form"].pools.items()
            }
        )
    changes = list(ctx["changes"])
    ctx["pending_change"] = next((c for c in changes if c.state == "pending"), None)
    from .network_views import connectivity_context

    ctx.update(connectivity_context(tenant, user))
    from .sshkey_views import can_manage, igw_commands

    ctx.update(
        ssh_keys=list(tenant.ssh_keys.all()),
        can_manage_keys=can_manage(tenant, user),
        igw_key_commands=igw_commands(tenant) if tenant.one_group_id else "",
    )
    if ctx.get("can_edit_igw"):  # correction 14: the Internet dialogs (igw details, configurations)
        from .igw_views import dialog_context

        ctx.update(dialog_context(request, tenant))
    ctx["igw_open"] = request.GET.get("igw", "")
    from .provision import ProvisionForm, can_provision, failed_provisioning

    ctx["provisioned_done"] = tenant.deployments.filter(state="done").exists()
    ctx["provision_failed"] = failed_provisioning(tenant) if tenant.approval == Approval.APPROVED else None
    if ctx["provision_failed"] is not None:
        ctx["provision_failed_step"] = next(
            (s.get("name") for s in ctx["provision_failed"].steps or [] if s.get("status") == "failed"), ""
        )
    if can_provision(tenant, user):  # correction 13: nothing is made on the platform before Provision
        from . import deploy as _dp

        ctx["can_provision"] = True
        ctx["provision_blocked"] = _dp.blocked_reason(tenant)
        ctx["provision_form"] = ProvisionForm(prefix="pv", tenant=tenant, user=user)
        ctx["provision_open"] = request.GET.get("provision") == "1"
    ctx["config_check"] = tenant.config_checks.first()
    ctx["can_check_config"] = (
        bool(tenant.one_group_id)
        and not tenant.is_deleted
        and (can_manage(tenant, user) or (staff and (is_support or is_rm or user.is_super_admin)))
    )
    from .deletion_views import deletion_context, preview_for

    ctx.update(deletion_context(tenant, user))
    if request.GET.get("preview_delete"):
        ctx["del_preview"] = preview_for(tenant, user)
    if tenant.is_deleted:
        for k in ("can_request_change", "can_edit_resources", "can_invite", "net_can_request", "can_split"):
            ctx[k] = False
    if staff and tenant.approval == Approval.APPROVED:
        from inventory.nfconfig import tenant_roles

        ctx["nf_roles"] = tenant_roles(tenant)
        from .quota_check import compare

        ctx["quota_status"], ctx["quota_rows"] = compare(tenant)
    # u09: resource change history on the page, and the review timeline (one line)
    ctx["hist"] = __import__("tenants.tracking", fromlist=["history"]).history(tenants=[tenant])
    created = timezone.localdate(tenant.created_at) if tenant.created_at else None
    if created:
        today = timezone.localdate()
        tl = {"created": tenant.created_at, "running": max((today - created).days, 0), "review": tenant.date_review}
        if tenant.date_review:
            total = (tenant.date_review - created).days
            left = (tenant.date_review - today).days
            tl["left"], tl["overdue"] = left, (-left if left < 0 else 0)
            tl["pct"] = 0 if total <= 0 else max(0, min(100, round((today - created).days * 100 / total)))
            tl["state"] = "due" if left < 0 else ("soon" if left <= 14 else "ok")
        ctx["tl"] = tl
    # u11: every form opened from the project page returns to the projects list at this row (like Edit)
    ctx["back_to_list"] = f"{reverse('tenant_list' if staff else 'my_org')}?sel=t-{tenant.pk}"
    # u10 fix: the project Users table lists each user once (org admins + members), roles combined — like org detail
    pp, order = {}, []
    for a in ctx.get("org_admins") or []:
        if a.pk not in pp:
            pp[a.pk] = {"user": a, "roles": [], "staff_member": None}
            order.append(a.pk)
        pp[a.pk]["roles"].append(_("organization admin"))
    for m in ctx.get("members") or []:
        u = m.user
        if u.pk not in pp:
            pp[u.pk] = {"user": u, "roles": [], "staff_member": None}
            order.append(u.pk)
        pp[u.pk]["roles"].append(m.get_role_display())
        if u.kind == "staff":
            pp[u.pk]["staff_member"] = m
    ctx["project_people"] = [pp[pk] for pk in order]
    return render(request, "tenants/detail.html", ctx)


def _igw_manageable(tenant):
    from . import igwconfig

    try:
        return igwconfig.available(tenant)
    except Exception:  # noqa: BLE001 - shown as not available
        return False


def _igw_main_ip(tenant):
    from . import igwconfig

    return igwconfig._main_ip(tenant)


def _revealed(request, tenant):
    """The passwords shown once after "Show passwords" (OpenNebula admin, igw console)."""
    if request.session.pop(f"reveal_{tenant.pk}", False):
        return {
            "admin_password": tenant.get_admin_password(),
            "igw_password": tenant.get_igw_password(),
            "revealed": True,
        }
    return {"admin_password": None, "igw_password": None, "revealed": False}


def _with_usage(tenant, records):
    """Each record gets .use (vCPU, RAM, GPU, VM disks in use on its cluster) and .site_use (images, files, backups in
    use on its site) from the last platform collection (correction 11); None when not read."""
    from .quota_check import usage

    try:
        u = usage(tenant, records)
    except Exception:  # noqa: BLE001 - the quotas show without the usage
        u = {}
    seen = set()
    for r in records:
        r.use = u.get(r.pk)
        r.first_of_site = r.site not in seen  # backups and the site's datastore figures belong to this row
        r.site_use = u.get(f"site:{r.site}") if r.first_of_site else None
        seen.add(r.site)
        # owner 2026-11-09: storage shows one total in use and its bar tracks vms + images + files. Images and
        # files are read per site, not per pool, so they count on the row that carries the site's figures - the
        # same row that prints them - and the total always matches the numbers beside it.
        r.disk_used = None
        if r.use is not None:
            r.disk_used = r.use.get("vm_disk_gb") or 0
            if r.site_use:
                r.disk_used += (r.site_use.get("images_gb") or 0) + (r.site_use.get("files_gb") or 0)
    tenant.usage_at = u.get("at")
    return records


def _records_with_split(records):
    out = []
    gpus = {}
    if any(r.gpu for r in records):  # correction 14: the bundles column shows the GPU's short name — H200(1)
        gpus = {
            (p["site"], p["quota_code"]): ordering.gpu_short(p["gpu"]["model"])
            for p in ordering.resource_pools()
            if p["gpu"]
        }
    for r in records:
        r.gpu_short = gpus.get((r.site, r.cluster), "GPU")
        r.vms_gb, r.img_gb, r.fil_gb = r.split
        total = r.disk_gb or 0
        r.vms_pct = round(100 * r.vms_gb / total) if total else 0
        r.img_pct = round(100 * r.img_gb / total) if total else 0
        r.fil_pct = max(100 - r.vms_pct - r.img_pct, 0) if total else 0
        out.append(r)
    return out


def _after_approval(request, tenant, finalized):
    if finalized:
        ordering.materialize(tenant, request.user)
    if not finalized:
        notify_next_approvers(tenant)
        messages.success(
            request,
            _("Approval recorded. Still waiting for: %(who)s.")
            % {"who": ", ".join(str(x) for x in tenant.missing_labels())},
        )
        return
    audit(request.user, "tenant_approved", tenant)
    # corrections-18: provisioned at once; the access details go out when it is done (no "approved" e-mail before)
    from .provision import auto

    dep, why = auto(tenant, request.user)
    if dep is not None and dep.state == "failed":
        messages.error(request, _("Project approved; provisioning failed: %(e)s") % {"e": dep.error})
    elif dep is not None:
        messages.success(
            request,
            _("Project approved; provisioning started (Tasks). The access details are e-mailed when it is done."),
        )
    else:
        messages.warning(
            request,
            _("Project approved; provisioning could not start: %(w)s. Provision it from the project when possible.")
            % {"w": why},
        )


def _report_deployment(request, tenant, dep):
    if dep.state == "running":
        messages.info(request, _("Deployment on %(p)s started; the progress is in Tasks.") % {"p": dep.platform_key})
    elif dep.state == "done":
        messages.success(request, _("Project deployed on %(p)s.") % {"p": dep.platform_key})
    else:
        messages.error(request, _("Deployment failed: %(e)s") % {"e": dep.error})


@require_POST
@staff_required(super_only=True)
def deploy_view(request, pk):
    """Preview (dry run) or run the deployment of an approved tenant."""
    from . import deploy as dp

    tenant = _tenant_for(request, pk)
    dry = request.POST.get("dry") == "1"
    if tenant.legacy_names:
        messages.error(
            request, _("A project with a tenant-<name> group is not deployed from an order: use its gateway actions.")
        )
        return redirect("tenant_detail", pk=pk)
    reason = dp.blocked_reason(tenant) if tenant.approval == Approval.APPROVED else _("the project is not approved")
    if not dry and reason:
        messages.error(request, _("Not deployed: %(why)s.") % {"why": reason})
        return redirect("tenant_detail", pk=pk)
    running = tenant.deployments.filter(state="running").first()
    if not dry and running and not running.is_stale:
        messages.warning(request, _("A deployment is already running."))
        return redirect(f"{reverse('tenant_detail', args=[pk])}#gateways")
    dep = dp.deploy(tenant, request.user, dry=True) if dry else dp.start(tenant, request.user)
    if dry:
        (messages.info if not dep.error else messages.error)(
            request, _("Preview ready.") if not dep.error else _("Preview stopped: %(e)s") % {"e": dep.error}
        )
    else:
        _report_deployment(request, tenant, dep)
    return redirect(f"{reverse('tenant_detail', args=[pk])}#gateways")


def can_redeploy_gateways(user):
    """Correction 11: the super admin and support engineers redeploy a tenant's gateways; everybody else sees them."""
    return user.is_authenticated and (user.is_super_admin or user.has_role(Role.SUPPORT))


@require_POST
@login_required
def gateway_check_view(request, pk):
    """Tenant → Gateways → Check: each gateway answers ssh in VRF MGMT and its BGP peers are up (read only)."""
    from . import gwstatus

    tenant = _tenant_for(request, pk)
    if not can_redeploy_gateways(request.user):
        raise PermissionDenied
    st = gwstatus.check(tenant, request.user, request.POST.get("key") or None)
    bad = [k for k, v in st.items() if not v.get("ok")]
    (messages.warning if bad else messages.success)(
        request,
        _("Gateways checked: %(r)s.")
        % {"r": ", ".join(f"{k} {'ok' if v.get('ok') else v.get('detail')}" for k, v in st.items()) or "—"},
    )
    return redirect(f"{reverse('tenant_detail', args=[pk])}#gateways")


@require_POST
@login_required
def gateway_redeploy_view(request, pk, role):
    """Tenant → Gateways → Redeploy: the egw or cgw made again (tenants.takeover.Takeover.replace), or an L2 channel's
    gateways (role l2src<vlan> / l2dst<vlan> / l2br<vlan>: both sides of that channel, owner 2026-11-11)."""
    from . import deploy as dp
    from . import takeover

    tenant = _tenant_for(request, pk)
    import re

    m = re.fullmatch(r"l2(?:src|dst|br)(\d{4})", role)
    if not can_redeploy_gateways(request.user) or not (role in ("egw", "cgw") or m):
        raise PermissionDenied
    what = f"l2{int(m.group(1))}" if m else role
    reason = dp.blocked_reason(tenant)
    running = tenant.deployments.filter(state="running").first()
    if reason:
        messages.error(request, _("Not started: %(why)s.") % {"why": reason})
    elif running and not running.is_stale:
        messages.warning(request, _("A run is already going on for this project."))
    else:
        try:
            takeover.start(tenant, what, request.user)
            messages.info(
                request,
                _("The %(r)s is being made again; the steps are in Tasks.")
                % {"r": _("L2 channel VLAN %(v)s") % {"v": int(m.group(1))} if m else role},
            )
        except Exception as exc:  # noqa: BLE001 - shown
            messages.error(request, str(exc))
    return redirect(f"{reverse('tenant_detail', args=[pk])}#gateways")


@login_required
def tenant_password_view(request, pk):
    """Tenant admins change the OpenNebula admin's password (policy, not one of the last 5); OpenNebula follows."""
    from .forms import TenantPasswordForm

    tenant = _tenant_for(request, pk)
    if (
        tenant.membership_role(request.user) not in (Membership.Role.ORG_ADMIN, Membership.Role.TENANT_ADMIN)
        and not request.user.is_super_admin
    ):
        raise PermissionDenied
    form = TenantPasswordForm(request.POST or None, tenant=tenant, user=request.user)
    if request.method == "POST" and form.is_valid():
        new = form.cleaned_data["password"]
        if tenant.one_admin_id is not None and tenant.one_group_id is not None:
            from . import deploy as dp

            try:
                dp.set_one_password(tenant, new)
            except dp.DeployError as exc:
                messages.error(request, _("OpenNebula did not accept the change: %(e)s") % {"e": exc})
                return redirect("tenant_password", pk=pk)
        tenant.set_admin_password(new)
        tenant.save(update_fields=["admin_password_encrypted"])
        audit(request.user, "tenant_password_changed", tenant, opennebula=tenant.one_admin_id is not None)
        messages.success(request, _("Password changed."))
        return redirect(f"{reverse('tenant_detail', args=[pk])}#access")
    return render(request, "tenants/password.html", {"tenant": tenant, "form": form})


@require_POST
@login_required
def reveal_password_view(request, pk):
    """Tenant admins (and the org admin) see the OpenNebula admin's password; every view is audited."""
    tenant = _tenant_for(request, pk)
    if (
        tenant.membership_role(request.user) not in (Membership.Role.ORG_ADMIN, Membership.Role.TENANT_ADMIN)
        and not request.user.is_super_admin
    ):
        raise PermissionDenied
    audit(request.user, "tenant_password_viewed", tenant)
    request.session[f"reveal_{tenant.pk}"] = True
    return redirect(f"{reverse('tenant_detail', args=[pk])}#access")


@require_POST
@login_required
def secret_copy_view(request, pk):
    """Owner 2026-10-29 19:52: a hidden password is copied without being shown (each copy logged): {"admin", "igw"}."""
    from django.http import JsonResponse

    tenant = _tenant_for(request, pk)
    if (
        tenant.membership_role(request.user) not in (Membership.Role.ORG_ADMIN, Membership.Role.TENANT_ADMIN)
        and not request.user.is_super_admin
    ):
        raise PermissionDenied
    audit(request.user, "tenant_password_copied", tenant)
    return JsonResponse({"admin": tenant.get_admin_password() or "", "igw": tenant.get_igw_password() or ""})


@login_required
def deployment_status_view(request, pk):
    """JSON of the latest deployment, for the progress on the tenant page."""
    from django.http import JsonResponse

    tenant = _tenant_for(request, pk)
    dep = tenant.deployments.first()
    if dep is None:
        return JsonResponse({"state": None})
    return JsonResponse(
        {
            "id": dep.pk,
            "state": "interrupted" if dep.is_stale else dep.state,
            "steps": [{"name": s["name"], "status": s["status"], "detail": s.get("detail", "")} for s in dep.steps],
            "error": dep.error,
        }
    )


def _tenant_admin(request, tenant):
    return tenant.membership_role(request.user) in (Membership.Role.ORG_ADMIN, Membership.Role.TENANT_ADMIN)


def _pool_of(tenant):
    from .ordering import resource_pools

    o = tenant.order or {}
    return next((p for p in resource_pools() if p["key"] == f"{o.get('platform')}/{o.get('cluster')}"), None)


@login_required
def resource_request_view(request, pk):
    """Tenant admins ask for other resources; a resource manager (or the super admin) approves the change."""
    from .forms import ResourceChangeForm
    from .models import ResourceChange

    tenant = _tenant_for(request, pk)
    from .permissions import can_manage_project  # u12 §1: the org's manager/KAM/RM have full resource management

    if (
        not (can_manage_project(request.user, tenant) or _tenant_admin(request, tenant) or request.user.is_super_admin)
        or tenant.approval != Approval.APPROVED
        or tenant.is_deleted
    ):
        raise PermissionDenied
    nxt = _safe_next(request, f"{reverse('tenant_detail', args=[pk])}#changes")
    # owner 2026-11-11: with several resource pools every row's Edit opened the first one. The row names its pool
    # (site + quota code); without it, the ordered pool, else the first.
    recs = tenant.current_quota_records()
    site = (request.POST.get("site") or request.GET.get("site") or "").strip()
    cluster = (request.POST.get("cluster") or request.GET.get("cluster") or "").strip()
    if site or cluster:
        rec = next((r for r in recs if r.site == site and r.cluster == cluster), None)
        if rec is None:
            messages.error(request, _("The project has no such resource pool."))
            return redirect(nxt)
    else:
        o = tenant.order or {}
        rec = next((r for r in recs if r.cluster == o.get("cluster")), None) or next(iter(recs), None)
    if rec is None:
        messages.error(request, _("The project has no resources yet."))
        return redirect(nxt)
    from .ordering import resource_pools

    pool = (
        next((p for p in resource_pools() if p["site"] == rec.site and p["quota_code"] == rec.cluster), None)
        or _pool_of(tenant)
        or {}
    )
    if tenant.resource_changes.filter(state=ResourceChange.State.PENDING).exists():
        messages.warning(request, _("A resource change is already waiting for approval."))
        return redirect(nxt)
    _vms, images, files = rec.split
    initial = {"cpu": rec.cpu, "ram_gb": rec.ram_gb, "gpu": rec.gpu or 1, "disk_gb": rec.disk_gb}
    form = ResourceChangeForm(request.POST or None, pool=pool, initial=initial)
    if request.method == "POST" and form.is_valid():
        v = form.values(rec)
        same = all(getattr(rec, k) == v[k] for k in ("cpu", "ram_gb", "gpu", "disk_gb", "backups_gb"))
        if same:
            messages.info(request, _("Nothing changed."))
            return redirect(nxt)
        ch = ResourceChange.objects.create(
            tenant=tenant,
            site=rec.site,
            cluster=rec.cluster,
            requested_by=request.user,
            note=form.cleaned_data.get("note") or "",
            **v,
        )
        audit(
            request.user,
            "resources_requested",
            tenant,
            change=ch.pk,
            resources=f"{v['cpu']} vCPU, {v['ram_gb']} GB RAM, {v['gpu']} GPU, {v['disk_gb']} GB storage "
            f"(images {v['images_gb']}, files {v['files_gb']}), backups {v['backups_gb']}",
        )
        messages.success(request, _("Resource change sent for approval."))
        return redirect(nxt)
    return render(
        request,
        "tenants/resource_request.html",
        {"tenant": tenant, "form": form, "rec": rec, "pool": pool, "images": images, "files": files, "next": nxt},
    )


@login_required
def backups_request_view(request, pk):
    """The backups quota of one site: asked for once, applied to every resource pool the project has there.

    Owner 2026-11-09: "backups is site wide value / need to add button to change backups quota (request to
    change)". It rides the ordinary ResourceChange approval flow, marked pool.kind = "backups"; the approval
    writes the new figure to every record of the site (resource_decide_view).
    """
    from .forms import BackupsChangeForm
    from .models import ResourceChange

    tenant = _tenant_for(request, pk)
    from .permissions import can_manage_project

    if (
        not (can_manage_project(request.user, tenant) or _tenant_admin(request, tenant) or request.user.is_super_admin)
        or tenant.approval != Approval.APPROVED
        or tenant.is_deleted
    ):
        raise PermissionDenied
    nxt = _safe_next(request, f"{reverse('tenant_detail', args=[pk])}#resources")
    site = (request.POST.get("site") or request.GET.get("site") or "").strip()
    recs = [r for r in tenant.current_quota_records() if r.site == site]
    if not recs:
        messages.error(request, _("The project has no resources on this site."))
        return redirect(nxt)
    if tenant.resource_changes.filter(state=ResourceChange.State.PENDING).exists():
        messages.warning(request, _("A resource change is already waiting for approval."))
        return redirect(nxt)
    rec = recs[0]
    form = BackupsChangeForm(request.POST or None, initial={"backups_gb": rec.backups_gb or 0})
    if request.method == "POST" and form.is_valid():
        want = form.cleaned_data["backups_gb"]
        if all((r.backups_gb or 0) == want for r in recs):
            messages.info(request, _("Nothing changed."))
            return redirect(nxt)
        ch = ResourceChange.objects.create(
            tenant=tenant,
            site=site,
            cluster=rec.cluster,
            requested_by=request.user,
            note=form.cleaned_data.get("note") or "",
            pool={"kind": "backups", "cluster_name": rec.cluster},
            cpu=rec.cpu,
            ram_gb=rec.ram_gb,
            gpu=rec.gpu,
            disk_gb=rec.disk_gb,
            images_gb=rec.images_gb,
            files_gb=rec.files_gb,
            backups_gb=want,
        )
        audit(
            request.user,
            "backups_requested",
            tenant,
            change=ch.pk,
            site=site,
            resources=f"backups {rec.backups_gb or 0} GB -> {want} GB on every pool of {site}",
        )
        messages.success(request, _("The backups change is sent for approval."))
        return redirect(nxt)
    return render(
        request,
        "tenants/backups_request.html",
        {"tenant": tenant, "form": form, "site": site, "recs": recs, "next": nxt},
    )


def _can_order_pools(request, tenant):
    """u12: who may add/remove/update a project's resources — the org's manager/KAM/RM, its org admin, or a project
    admin of this project (plus super admin)."""
    from .permissions import can_manage_project

    return tenant.approval == Approval.APPROVED and not tenant.is_deleted and can_manage_project(request.user, tenant)


@require_POST
@login_required
def pool_order_view(request, pk):
    """Correction 15: order a resource pool on another cluster (or site); a resource manager approves it."""
    from .forms import PoolOrderForm
    from .models import ResourceChange

    tenant = _tenant_for(request, pk)
    if not _can_order_pools(request, tenant):
        raise PermissionDenied
    back = _safe_next(request, f"{reverse('tenant_detail', args=[pk])}#resources")
    if tenant.resource_changes.filter(state=ResourceChange.State.PENDING).exists():
        messages.warning(request, _("A resource change is already waiting for approval."))
        return redirect(back)
    form = PoolOrderForm(request.POST, prefix="po", tenant=tenant)
    if not form.is_valid():
        for name, errs in form.errors.items():
            for e in errs:
                messages.error(request, f"{form.fields[name].label}: {e}" if name in form.fields else e)
        return redirect(back)
    v = form.change_values()
    ch = ResourceChange.objects.create(tenant=tenant, requested_by=request.user, **v)
    audit(
        request.user,
        "resource_pool_requested",
        tenant,
        change=ch.pk,
        kind=v["pool"]["kind"],
        site=v["site"],
        cluster=v["pool"]["cluster_name"],
        resources=f"{v['cpu']} vCPU, {v['ram_gb']} GB RAM, {v['gpu']} GPU, {v['disk_gb']} GB",
    )
    messages.success(request, _("The resource pool is ordered; a resource manager approves it."))
    return redirect(back)


@require_POST
@login_required
def pool_remove_view(request, pk):
    """Correction 15: remove a resource pool (not the last one: removing it means deleting the tenant)."""
    from .models import ResourceChange

    tenant = _tenant_for(request, pk)
    if not _can_order_pools(request, tenant):
        raise PermissionDenied
    back = f"{reverse('tenant_detail', args=[pk])}#resources"
    recs = tenant.current_quota_records()
    rec = next(
        (r for r in recs if r.site == request.POST.get("site") and r.cluster == request.POST.get("cluster")), None
    )
    if rec is None:
        raise PermissionDenied
    if len(recs) < 2:
        messages.error(request, _("This is the project's last resource pool: delete the project instead."))
        return redirect(back)
    if tenant.resource_changes.filter(state=ResourceChange.State.PENDING).exists():
        messages.warning(request, _("A resource change is already waiting for approval."))
        return redirect(back)
    from accounts.templatetags.ui import cluster_name as cluster_label

    ch = ResourceChange.objects.create(
        tenant=tenant,
        site=rec.site,
        cluster=rec.cluster,
        requested_by=request.user,
        backups_gb=rec.backups_gb or 0,
        note=(request.POST.get("note") or "")[:2000],
        pool={"kind": "remove", "cluster_name": cluster_label(rec.site, rec.cluster)},
    )
    audit(request.user, "resource_pool_removal_requested", tenant, change=ch.pk, site=rec.site, cluster=rec.cluster)
    messages.success(request, _("Removing the resource pool waits for a resource manager's approval."))
    return redirect(back)


def _approved_values(request, ch):
    """The values the approver gives (they may differ from the request; the request is kept in ch.requested).
    False (with a message) when they are not numbers."""
    from .models import fit_split

    given = {}
    # owner 2026-11-09: backups belongs to the site, not to one pool, so an approver edits it only on a backups
    # change - and never touches the pool's own numbers there.
    kind = (ch.pool or {}).get("kind", "")
    if kind == "backups":
        fields = (("backups_gb", 0),)
    elif kind == "new_site":
        # a new site brings its own backups figure with it - it is the site's first pool, so there is nothing
        # for it to disagree with, and the approver has to be able to set it (owner 2026-11-09).
        fields = (("cpu", 0), ("ram_gb", 0), ("gpu", 0), ("disk_gb", 0), ("backups_gb", 0))
    else:
        fields = (("cpu", 0), ("ram_gb", 0), ("gpu", 0), ("disk_gb", 0))
    for f, lo in fields:
        raw = request.POST.get(f)
        if raw in (None, ""):
            continue
        try:
            v = int(raw)
        except ValueError:
            messages.error(request, _("Resources: whole numbers only."))
            return False
        if v < lo:
            messages.error(request, _("Resources: whole numbers only."))
            return False
        given[f] = v
    if not given:
        return True
    asked = {f: getattr(ch, f) for f in ch.VALUES}
    for f, v in given.items():
        setattr(ch, f, v)
    if kind != "backups":
        ch.images_gb, ch.files_gb = fit_split(ch.disk_gb, asked["images_gb"], asked["files_gb"])
    if any(getattr(ch, f) != asked[f] for f in ch.VALUES):
        ch.requested = asked
    return True


@require_POST
@login_required
def resource_decide_view(request, pk, cid, decision):
    """Approve, reject (resource manager / super admin) or cancel (the tenant's admins) a resource change."""
    from .models import QuotaRecord, ResourceChange

    tenant = _tenant_for(request, pk)
    ch = get_object_or_404(ResourceChange, pk=cid, tenant=tenant, state=ResourceChange.State.PENDING)
    user = request.user
    if decision == "cancel":
        if not (_tenant_admin(request, tenant) or user.is_super_admin):
            raise PermissionDenied
        ch.state = ResourceChange.State.CANCELLED
    elif decision in ("approve", "reject"):
        if not tenant.can_approve_as(user, Role.RESOURCE_MANAGER):
            raise PermissionDenied
        ch.state = ResourceChange.State.APPROVED if decision == "approve" else ResourceChange.State.REJECTED
        ch.reason = (request.POST.get("reason") or "").strip()[:2000]
        if decision == "approve" and not _approved_values(request, ch):
            return back_to(request, f"{reverse('tenant_detail', args=[pk])}#resources")
    else:
        raise PermissionDenied
    ch.decided_by, ch.decided_at = user, timezone.now()
    with transaction.atomic():
        if ch.state == ResourceChange.State.APPROVED:
            kind = (ch.pool or {}).get("kind", "")
            if kind == "backups":
                # owner 2026-11-09: backups is one figure for the whole site, so the approved value lands on
                # every pool the project has there - each keeps its own cpu/ram/storage, only backups moves.
                now = timezone.now()
                for r in [x for x in tenant.current_quota_records() if x.site == ch.site]:
                    QuotaRecord.objects.create(
                        tenant=tenant,
                        site=r.site,
                        cluster=r.cluster,
                        version=QuotaRecord.next_version(tenant, r.site, r.cluster),
                        cpu=r.cpu,
                        ram_gb=r.ram_gb,
                        gpu=r.gpu,
                        disk_gb=r.disk_gb,
                        images_gb=r.images_gb,
                        files_gb=r.files_gb,
                        backups_gb=ch.backups_gb,
                        effective_from=now,
                        created_by=user,
                    )
            else:
                QuotaRecord.objects.create(
                    tenant=tenant,
                    site=ch.site,
                    cluster=ch.cluster,
                    version=QuotaRecord.next_version(tenant, ch.site, ch.cluster),
                    cpu=ch.cpu,
                    ram_gb=ch.ram_gb,
                    gpu=ch.gpu,
                    disk_gb=ch.disk_gb,
                    images_gb=ch.images_gb,
                    files_gb=ch.files_gb,
                    backups_gb=ch.backups_gb,
                    effective_from=timezone.now(),
                    created_by=user,
                    removed=kind == "remove",
                )
            if kind == "new_site":  # correction 15: the connectivity of the new site, as ordered
                from .models import Connectivity

                c = (ch.pool or {}).get("connectivity") or {}
                if not Connectivity.objects.filter(tenant=tenant, site=ch.site).exists():
                    # owner 2026-11-09: the gateway type and the base subnets are chosen on the order now, so the
                    # new site takes what was asked for; the project's own type is the fallback for older changes.
                    Connectivity.objects.create(
                        tenant=tenant,
                        site=ch.site,
                        version=Connectivity.next_version(tenant, ch.site),
                        created_by=user,
                        connection_type=" ".join(c.get("types") or []),
                        inet_mbps=c.get("inet_mbps"),
                        inet_main_prefix=c.get("inet_main_prefix"),
                        corpnet_prefixes=c.get("corpnet_prefixes") or "",
                        igw_type=(
                            (c.get("igw_type") or (tenant.order or {}).get("igw_type", ""))
                            if "inet" in (c.get("types") or [])
                            else ""
                        ),
                    )
        ch.save()
    audit(
        user,
        f"resources_{ch.state}",
        tenant,
        change=ch.pk,
        reason=ch.reason,
        changed="; ".join(f"{f} {a} → {b}" for f, a, b in ch.changed_on_approval),
    )
    if ch.state == ResourceChange.State.APPROVED:
        from . import deploy as dp

        try:
            ch.applied = dp.apply_quota(tenant, user)[:300]
        except dp.DeployError as exc:
            ch.applied = f"OpenNebula: {exc}"[:300]
            audit(user, "quota_apply_failed", tenant, error=str(exc)[:300])
        ch.save(update_fields=["applied"])
        messages.success(request, _("Resource change approved: %(r)s") % {"r": ch.applied})
        if ch.changed_on_approval:
            messages.info(
                request,
                _("Approved with other values than asked: %(c)s")
                % {"c": "; ".join(f"{f} {a} → {b}" for f, a, b in ch.changed_on_approval)},
            )
    else:
        messages.success(request, _("Resource change %(s)s.") % {"s": ch.get_state_display()})
    return back_to(request, f"{reverse('tenant_detail', args=[pk])}#resources")


APPROVAL_FIELDS = {Role.RESOURCE_MANAGER: "rm", Role.SUPPORT: "support", Role.SALES_MANAGER: "sales"}


def _cover_other_roles(tenant, user, done):
    """One person in several roles approves once: the other steps they could approve are done with it."""
    now = timezone.now()
    covered = []
    for role in tenant.missing_approvals():
        if role == done or role == Role.RESOURCE_MANAGER:
            continue  # the resource manager step needs its form (name, placement, resources)
        if tenant.asks(user, role):  # correction 16: only the roles this person holds (not a super admin's rights)
            f = APPROVAL_FIELDS[role]
            setattr(tenant, f"{f}_approved_by", user)
            setattr(tenant, f"{f}_approved_at", now)
            covered.append(f)
    return covered


def notify_next_approvers(tenant):
    from accounts.views import notify_tenant_pending

    notify_tenant_pending(tenant, tenant.created_by)


@require_POST
@staff_required(Role.RESOURCE_MANAGER)
def rm_approve_view(request, pk):
    tenant = _tenant_for(request, pk)
    if Role.RESOURCE_MANAGER not in tenant.missing_approvals() or tenant.approval != Approval.PENDING:
        return redirect("tenant_detail", pk=pk)
    if not tenant.can_approve_as(request.user, Role.RESOURCE_MANAGER):
        raise PermissionDenied
    form = RMApprovalForm(request.POST, instance=tenant, prefix="rm")
    if not form.is_valid():
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
        return redirect("tenant_detail", pk=pk)
    with transaction.atomic():
        tenant = form.save(commit=False)
        tenant.order = {**(tenant.order or {}), **form.order_update()}
        if getattr(form, "group_name", None):
            tenant.group_name = form.group_name
        try:
            ordering.allocate(tenant, request.user, form.cleaned_data.get("inet_pool") or None)
        except ordering.AllocationError as exc:
            messages.error(request, str(exc))
            return redirect("tenant_detail", pk=pk)
        rep = form.cleaned_data.get("sales_rep")
        if rep and tenant.organization_id and not tenant.organization.sales_rep_id and not tenant.organization.is_corp:
            tenant.organization.sales_rep = rep
            tenant.organization.save(update_fields=["sales_rep"])
            audit(request.user, "sales_rep_assigned", tenant.organization, new=rep.email)
        from .admins import (
            proposed,
            set_tenant_admin,  # correction 15: can be changed at approval
        )

        pick = form.cleaned_data.get("tenant_admin") or tenant.tenant_admin or proposed(tenant)[0]
        if pick is not None:
            set_tenant_admin(tenant, pick, request.user, why="approval")
        else:
            messages.warning(request, _("The organization has no users: add the project admin before provisioning."))
        tenant.rm_approved_by, tenant.rm_approved_at = request.user, timezone.now()
        covered = _cover_other_roles(tenant, request.user, Role.RESOURCE_MANAGER)
        finalized = tenant.finalize_if_complete()
        tenant.save()
    audit(request.user, "tenant_rm_approved", tenant, name=tenant.name, kind=tenant.kind, also=",".join(covered))
    _after_approval(request, tenant, finalized)
    return redirect("tenant_detail", pk=pk)


@require_POST
@staff_required(Role.SUPPORT)
def support_approve_view(request, pk):
    tenant = _tenant_for(request, pk)
    if not tenant.can_approve_as(request.user, Role.SUPPORT):
        raise PermissionDenied
    if Role.SUPPORT in tenant.missing_approvals() and tenant.approval == Approval.PENDING:
        with transaction.atomic():
            tenant.support_approved_by, tenant.support_approved_at = request.user, timezone.now()
            covered = _cover_other_roles(tenant, request.user, Role.SUPPORT)
            finalized = tenant.finalize_if_complete()
            tenant.save()
        audit(request.user, "tenant_support_approved", tenant, also=",".join(covered))
        _after_approval(request, tenant, finalized)
    return back_to(request, reverse("tenant_detail", args=[pk]))


@require_POST
@staff_required(Role.SALES_MANAGER)
def sales_approve_view(request, pk):
    tenant = _tenant_for(request, pk)
    if tenant.sales_rep is None or tenant.sales_rep.pk != request.user.pk:
        raise PermissionDenied
    if Role.SALES_MANAGER in tenant.missing_approvals() and tenant.approval == Approval.PENDING:
        with transaction.atomic():
            tenant.sales_approved_by, tenant.sales_approved_at = request.user, timezone.now()
            covered = _cover_other_roles(tenant, request.user, Role.SALES_MANAGER)
            finalized = tenant.finalize_if_complete()
            tenant.save()
        audit(request.user, "tenant_sales_approved", tenant, also=",".join(covered))
        _after_approval(request, tenant, finalized)
    return back_to(request, reverse("tenant_detail", args=[pk]))


@require_POST
@staff_required(*STAFF_VIEW_ROLES)
def reject_view(request, pk):
    tenant = _tenant_for(request, pk)
    user = request.user
    missing = tenant.missing_approvals()
    if not (
        tenant.can_approve_as(user, Role.RESOURCE_MANAGER)
        or (Role.SUPPORT in missing and tenant.can_approve_as(user, Role.SUPPORT))
        or (PortalSettings.get().sales_approval_required and tenant.can_approve_as(user, Role.SALES_MANAGER))
    ):
        raise PermissionDenied
    form = RejectForm(request.POST, prefix="rej")
    if tenant.approval == Approval.PENDING and form.is_valid():
        tenant.approval = Approval.REJECTED
        tenant.rejection_reason = form.cleaned_data["reason"]
        tenant.save(update_fields=["approval", "rejection_reason"])
        ordering.release(tenant)
        _free_group(tenant)
        audit(request.user, "tenant_rejected", tenant, reason=tenant.rejection_reason)
        recipients = list(tenant.organization.admins) if tenant.organization_id else []
        notify(recipients, "tenant_rejected", {"tenant": tenant}, _("Your project request was not approved"))
        messages.success(request, _("Project rejected."))
    return back_to(request, reverse("tenant_detail", args=[pk]))


def _free_group(tenant):
    """A request that never became a tenant gives its group name back."""
    if tenant.legacy_names or tenant.one_group_id:
        return
    if tenant.group_name:
        tenant.group_name = None
        tenant.save(update_fields=["group_name"])


@require_POST
@login_required
def cancel_view(request, pk):
    """The ordering organization (or a resource manager) withdraws a request that is not approved yet."""
    tenant = _tenant_for(request, pk)
    if not tenant.can_cancel(request.user):
        raise PermissionDenied
    with transaction.atomic():
        tenant.approval = Approval.CANCELLED
        tenant.save(update_fields=["approval"])
        ordering.release(tenant)
        _free_group(tenant)
    audit(request.user, "tenant_cancelled", tenant, title=tenant.title)
    messages.success(request, _("Request cancelled."))
    return redirect("tenant_detail", pk=pk)


@require_POST
@login_required
def delete_request_view(request, pk):
    """Rejected and cancelled requests never became tenants: they are archived (kept, out of the lists)."""
    tenant = _tenant_for(request, pk)
    if not tenant.can_delete(request.user):
        raise PermissionDenied
    label = tenant.title or tenant.name or f"#{tenant.pk}"
    with transaction.atomic():
        tenant.status = Lifecycle.ARCHIVED
        tenant.save(update_fields=["status", "updated_at"])
        audit(
            request.user,
            "tenant_request_archived",
            tenant,
            title=tenant.title,
            name=tenant.name or "",
            approval=tenant.approval,
        )
    messages.success(request, _("Request %(t)s archived.") % {"t": label})
    return redirect("tenant_list" if request.user.is_staff_member else "my_org")


@staff_required(Role.RESOURCE_MANAGER)
def tenant_resources_view(request, pk):
    """Change the resources of an approved tenant: every change is a new version of its quota records."""
    from django.forms import formset_factory

    from .forms import ResourceRowForm, tenant_placements
    from .models import QuotaRecord

    tenant = _tenant_for(request, pk)
    if tenant.approval != Approval.APPROVED:
        raise PermissionDenied
    nxt = _safe_next(request, reverse("tenant_detail", args=[pk]))
    current = {(r.site, r.cluster): r for r in tenant.current_quota_records()}
    initial = [
        {"key": f"{k[0]}/{k[1]}", **{f: getattr(r, f) for f in ResourceRowForm.FIELDS}}
        for k, r in sorted(current.items())
    ]
    placements = [p for p in tenant_placements(tenant) if tuple(p[0].split("/", 1)) not in current]
    Rows = formset_factory(ResourceRowForm, extra=1)
    formset = Rows(request.POST or None, initial=initial, prefix="r", form_kwargs={"placements": placements})
    if request.method == "POST" and formset.is_valid():
        now, changes = timezone.now(), []
        with transaction.atomic():
            for form in formset:
                d = form.cleaned_data
                if not d:
                    continue
                key = d.get("key") or d.get("placement")
                if not key:
                    continue
                site, cluster = key.split("/", 1)
                old = current.get((site, cluster))
                vals = {f: d.get(f) for f in ResourceRowForm.FIELDS}
                vals["gpu"] = vals["gpu"] or 0
                from .models import fit_split

                vals["images_gb"], vals["files_gb"] = fit_split(
                    vals["disk_gb"] or 0, getattr(old, "images_gb", None), getattr(old, "files_gb", None)
                )
                if d.get("remove"):
                    if old:
                        QuotaRecord.objects.create(
                            tenant=tenant,
                            site=site,
                            cluster=cluster,
                            removed=True,
                            version=QuotaRecord.next_version(tenant, site, cluster),
                            effective_from=now,
                            created_by=request.user,
                        )
                        changes.append(f"{key} removed")
                    continue
                if old and all(getattr(old, f) == v for f, v in vals.items()):
                    continue
                QuotaRecord.objects.create(
                    tenant=tenant,
                    site=site,
                    cluster=cluster,
                    version=QuotaRecord.next_version(tenant, site, cluster),
                    effective_from=now,
                    created_by=request.user,
                    **vals,
                )
                changes.append(f"{key} {'changed' if old else 'added'}")
        if changes:
            audit(request.user, "resources_changed", tenant, changes="; ".join(changes))
            messages.success(request, _("Resources saved: %(c)s.") % {"c": "; ".join(changes)})
        else:
            messages.info(request, _("Nothing changed."))
        return redirect(nxt)
    return render(request, "tenants/resources.html", {"tenant": tenant, "formset": formset, "next": nxt})


@require_POST
@login_required
def storage_split_view(request, pk):
    """Tenant admins (and staff) split the storage quota between VM disks, images and files: no approval needed."""
    from .models import QuotaRecord

    tenant = _tenant_for(request, pk)
    user = request.user
    staff_ok = user.is_super_admin or user.has_role(Role.RESOURCE_MANAGER)
    if not (_tenant_admin(request, tenant) or staff_ok) or tenant.approval != Approval.APPROVED or tenant.is_deleted:
        raise PermissionDenied
    site, cluster = request.POST.get("site", ""), request.POST.get("cluster", "")
    rec = next((r for r in tenant.current_quota_records() if r.site == site and r.cluster == cluster), None)
    if rec is None:
        raise PermissionDenied
    try:
        images, files = int(request.POST.get("images_gb") or 0), int(request.POST.get("files_gb") or 0)
    except ValueError:
        images = files = -1
    if images < 0 or files < 0 or images + files > rec.disk_gb:
        messages.error(
            request,
            _("Images and files together can take at most the %(n)s GB of the storage quota.") % {"n": rec.disk_gb},
        )
        return redirect(f"{reverse('tenant_detail', args=[pk])}#resources")
    if (images, files) == rec.split[1:]:
        messages.info(request, _("Nothing changed."))
        return redirect(f"{reverse('tenant_detail', args=[pk])}#resources")
    QuotaRecord.objects.create(
        tenant=tenant,
        site=site,
        cluster=cluster,
        version=QuotaRecord.next_version(tenant, site, cluster),
        effective_from=timezone.now(),
        created_by=user,
        **{**rec.values(), "images_gb": images, "files_gb": files},
    )
    audit(
        user,
        "storage_split_changed",
        tenant,
        place=f"{site}/{cluster}",
        total=rec.disk_gb,
        vms=rec.disk_gb - images - files,
        images=images,
        files=files,
    )
    note = ""
    if tenant.one_group_id:
        from . import deploy as dp

        try:
            note = dp.apply_quota(tenant, user)[:300]
        except dp.DeployError as exc:
            note = f"OpenNebula: {exc}"[:300]
            audit(user, "quota_apply_failed", tenant, error=str(exc)[:300])
    messages.success(
        request,
        _("Storage split: VM disks %(v)s GB, images %(i)s GB, files %(f)s GB.")
        % {"v": rec.disk_gb - images - files, "i": images, "f": files}
        + (f" {note}" if note else ""),
    )
    return redirect(f"{reverse('tenant_detail', args=[pk])}#resources")


@require_POST
@staff_required(Role.RESOURCE_MANAGER)
def tenant_move_view(request, pk):
    """corrections-18: ask to move the tenant; it moves when the resource manager and the target sales rep approve."""
    from . import moves

    tenant = _tenant_for(request, pk)
    form = TenantMoveForm(request.POST, prefix="mv", tenant=tenant)
    if form.is_valid():
        move, err = moves.request(request.user, tenant, form.cleaned_data["organization"])
        if err:
            messages.error(request, err)
        elif move.state == "done":
            messages.success(request, _("Project moved to %(o)s.") % {"o": move.to_org.name})
        else:
            messages.info(
                request,
                _("The move to %(o)s waits for: %(w)s.")
                % {"o": move.to_org.name, "w": ", ".join(str(s["label"]) for s in moves.steps(move) if not s["done"])},
            )
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
    return redirect("tenant_detail", pk=pk)


@require_POST
@staff_required(*STAFF_VIEW_ROLES)
def tenant_move_decide_view(request, pk, mid, action):
    from . import moves
    from .models import TenantMove

    tenant = get_object_or_404(Tenant, pk=pk)  # the target's sales rep may not see the tenant yet
    move = get_object_or_404(TenantMove, pk=mid, tenant=tenant)
    if not any(moves.can(request.user, move, s) for s in moves.missing(move)):
        raise PermissionDenied
    if action == "reject":
        err = moves.reject(request.user, move, (request.POST.get("reason") or "").strip())
        ok = _("The move is rejected.")
    elif action in ("rm", "sales"):
        err = moves.approve(request.user, move, action)
        move.refresh_from_db()
        ok = (
            _("Project moved to %(o)s.") if move.state == "done" else _("Approved; the move waits for the others.")
        ) % {"o": move.to_org.name}
    else:
        raise PermissionDenied
    if err:
        messages.error(request, err)
    else:
        messages.success(request, ok)
    return back_to(request, reverse("approvals"))


@require_POST
@staff_required(Role.RESOURCE_MANAGER)
def org_merge_view(request, pk):
    from adoption.grouping import merge

    org = get_object_or_404(Organization, pk=pk)
    form = MergeForm(request.POST, prefix="mg", source=org)
    if form.is_valid():
        target = form.cleaned_data["target"]
        n_t, n_u = merge(org, target, request.user)
        messages.success(
            request,
            _("%(t)s project(s) and %(u)s user(s) moved to %(o)s. %(s)s is empty now and can be deleted.")
            % {"t": n_t, "u": n_u, "o": target.name, "s": org.name},
        )
        return redirect("org_detail", pk=target.pk)
    return redirect("org_detail", pk=pk)


@require_POST
@staff_required(*STAFF_VIEW_ROLES)
def stage_view(request, pk):
    tenant = _tenant_for(request, pk)
    if not tenant.can_change_stage(request.user):
        raise PermissionDenied
    old = tenant.stage
    form = StageForm(request.POST, instance=tenant, prefix="st")
    if form.is_valid() and form.cleaned_data["stage"] != old:
        t = form.save(commit=False)
        from datetime import timedelta

        if t.stage == TenantStage.POC and t.status == Lifecycle.OK and not t.date_review:
            t.date_review = timezone.localdate() + timedelta(days=Tenant.POC_DAYS)
        elif t.stage == TenantStage.CLIENT and t.status == Lifecycle.OK:
            t.date_review = None  # a client's review dates come with paused / suspended
        t.save()
        audit(
            request.user,
            "tenant_stage_changed",
            tenant,
            old=old,
            new=tenant.stage,
            review=t.date_review.isoformat() if t.date_review else "",
        )
        messages.success(request, _("Stage changed to %(s)s.") % {"s": tenant.get_stage_display()})
    return redirect("tenant_detail", pk=pk)


@require_POST
@login_required
def status_view(request, pk):
    """Change a tenant's status by hand (ok → paused → suspended → saved; back to ok while paused or suspended)."""
    from .forms import StatusForm

    tenant = _tenant_for(request, pk)
    if not tenant.can_change_status(request.user):
        raise PermissionDenied
    form = StatusForm(request.POST, tenant=tenant, prefix="ss")
    if not form.is_valid():
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
        return redirect("tenant_detail", pk=pk)
    from . import lifecycle

    old = tenant.status
    lifecycle.set_status(
        tenant,
        form.cleaned_data["status"],
        form.cleaned_data.get("date_review"),
        request.user,
        note=form.cleaned_data.get("note") or "",
    )
    messages.success(
        request, _("Status: %(a)s → %(b)s.") % {"a": Lifecycle(old).label, "b": tenant.get_status_display()}
    )
    return redirect("tenant_detail", pk=pk)


@require_POST
@login_required
def review_view(request, pk):
    """The review date: the end of a POC (the sales rep may extend it up to 3 months) or of the current status."""
    from .forms import ReviewForm

    tenant = _tenant_for(request, pk)
    if not tenant.can_change_review(request.user):
        raise PermissionDenied
    old = tenant.date_review
    form = ReviewForm(request.POST, instance=tenant, prefix="rv", user=request.user)
    if form.is_valid():
        form.save()
        audit(
            request.user,
            "tenant_review_changed",
            tenant,
            old=old.isoformat() if old else "",
            new=tenant.date_review.isoformat(),
        )
        messages.success(request, _("Review date: %(d)s.") % {"d": tenant.date_review.strftime("%d.%m.%Y")})
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
    return redirect("tenant_detail", pk=pk)


def _edit_page_context(request, tenant):
    """u05: the project edit page (a URL, not a floating dialog). The sub-forms and who may change what."""
    from .admins import choices as admin_choices
    from .forms import ReviewForm, StatusForm

    user = request.user
    staff = user.is_staff_member
    is_rm = user.has_role(Role.RESOURCE_MANAGER)
    is_rep = bool(user.is_sales and tenant.sales_rep and tenant.sales_rep.pk == user.pk)
    can_edit = staff and (is_rm or user.is_super_admin)
    can_admin = staff and (is_rm or user.is_super_admin or is_rep) and not tenant.is_archived
    ctx = {
        "tenant": tenant,
        "staff": staff,
        "can_edit": can_edit,
        "can_stage": tenant.can_change_stage(user) and tenant.approval == Approval.APPROVED and not tenant.is_archived,
        "can_status": tenant.can_change_status(user),
        "can_review": tenant.can_change_review(user) and tenant.approval == Approval.APPROVED,
        "can_admin": can_admin,
        "admin_choices": admin_choices(tenant.organization) if can_admin else [],
        "edit_form": TenantStaffForm(instance=tenant, prefix="e"),
        "stage_form": StageForm(instance=tenant, prefix="st"),
        "status_form": StatusForm(tenant=tenant, prefix="ss", keep_option=True),
        "review_form": ReviewForm(instance=tenant, prefix="rv"),
        "move_form": TenantMoveForm(prefix="mv", tenant=tenant) if staff and is_rm and not tenant.is_archived else None,
        "pending_move": _move_context(tenant, user),
    }
    from .admins import admin_candidates as _ac
    from .models import OrgKind, TenantKind

    ctx["can_kind"] = bool(
        can_edit and tenant.organization and tenant.organization.kind == OrgKind.CORP
    )  # u10: type editable for corporate orgs
    ctx["kind_choices"] = TenantKind.choices
    _meta = {}  # u10: team-table autofill (pk -> contact)
    for _u in ctx["edit_form"].fields["support_engineer"].queryset:
        _meta[str(_u.pk)] = {"e": _u.email, "p": _u.phone, "c": _u.get_city_display()}
    for _u in _ac(tenant.organization):
        _meta[str(_u.pk)] = {"e": _u.email, "p": _u.phone, "c": _u.get_city_display()}
    ctx["people_meta"] = _meta
    ctx["can_edit_all"] = staff and any(
        ctx.get(k) for k in ("can_edit", "can_stage", "can_status", "can_review", "move_form", "can_admin", "can_kind")
    )
    return ctx


@staff_required(*STAFF_VIEW_ROLES)
def tenant_edit_all_view(request, pk):
    """u05: the project edit page. GET shows the form; POST applies each part that changed, by its own action."""
    tenant = _tenant_for(request, pk)
    if request.method != "POST":
        ctx = _edit_page_context(request, tenant)
        if not ctx["can_edit_all"]:
            raise PermissionDenied
        ctx["next"] = request.GET.get("next", "")
        return render(request, "tenants/tenant_edit.html", ctx)
    P = request.POST
    user = request.user
    done = []

    def run(name, view):
        try:
            view(request, pk)
            done.append(name)
        except PermissionDenied:
            messages.error(request, _("%(p)s: not allowed for you.") % {"p": name})

    if any(k.startswith("e-") for k in P) and TenantStaffForm(P, instance=tenant, prefix="e").has_changed():
        run(_("Details"), tenant_edit_view)
        tenant.refresh_from_db()
    if P.get("ta-user") and str(tenant.tenant_admin_id or "") != P["ta-user"]:
        from .admins import admin_candidates, set_tenant_admin

        u = admin_candidates(tenant.organization).filter(pk=P["ta-user"]).first()
        if u is None or not (
            user.is_super_admin
            or user.has_role(Role.RESOURCE_MANAGER)
            or (tenant.sales_rep and tenant.sales_rep.pk == user.pk)
        ):
            messages.error(request, _("Project admin: choose a user of the organization."))
        else:
            set_tenant_admin(tenant, u, user, why="edited")
            done.append(_("Project admin"))
    if P.get("st-stage") and P["st-stage"] != tenant.stage:
        run(_("Stage"), stage_view)
        tenant.refresh_from_db()
    if P.get("ss-status") and P["ss-status"] != tenant.status:
        run(_("Status"), status_view)
        tenant.refresh_from_db()
    elif P.get("rv-date_review") and P["rv-date_review"] != (
        tenant.date_review.isoformat() if tenant.date_review else ""
    ):
        run(_("Review date"), review_view)
        tenant.refresh_from_db()
    from .models import OrgKind, TenantKind

    if (
        P.get("kd-kind")
        and P["kd-kind"] != tenant.kind
        and tenant.organization
        and tenant.organization.kind == OrgKind.CORP
    ):
        if (user.is_super_admin or user.has_role(Role.RESOURCE_MANAGER)) and P["kd-kind"] in dict(TenantKind.choices):
            tenant.kind = P["kd-kind"]
            tenant.save(update_fields=["kind"])
            done.append(_("Type"))
        else:
            messages.error(request, _("Type: not allowed for you."))
    if P.get("mv-organization"):  # corrections-18: a move request
        run(_("Organization"), tenant_move_view)
    if not done and not getattr(messages.get_messages(request), "_queued_messages", None):
        messages.info(request, _("Nothing changed."))
    nxt = request.POST.get("next")
    return redirect(nxt if nxt and nxt.startswith("/") else reverse("tenant_detail", args=[pk]))


@require_POST
@staff_required(Role.RESOURCE_MANAGER)
def tenant_edit_view(request, pk):
    tenant = _tenant_for(request, pk)
    form = TenantStaffForm(request.POST, instance=tenant, prefix="e")
    if form.is_valid():
        changed = form.changed_data
        form.save()
        audit(request.user, "tenant_updated", tenant, **{f: str(form.cleaned_data.get(f) or "") for f in changed})
        messages.success(request, _("Saved."))
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
    return redirect("tenant_detail", pk=pk)


CORP_STAFF_ROLES = (Membership.Role.TENANT_ADMIN, Membership.Role.TENANT_USER)


def corp_staff_candidates():
    return User.objects.filter(Q(is_support=True) | Q(is_super_admin=True), kind="staff", is_active=True).order_by(
        "full_name", "email"
    )


@require_POST
@staff_required(Role.RESOURCE_MANAGER)
def tenant_staff_member_view(request, pk):
    """Add (or remove) a support engineer or super admin as a user of a corporate tenant."""
    tenant = _tenant_for(request, pk)
    if tenant.kind != TenantKind.CORP:
        raise PermissionDenied
    person = corp_staff_candidates().filter(pk=request.POST.get("user") or 0).first()
    if person is None:
        messages.error(request, _("Choose a support engineer or super admin."))
        return redirect("tenant_detail", pk=pk)
    if request.POST.get("remove"):
        Membership.objects.filter(tenant=tenant, user=person).delete()
        audit(request.user, "member_removed", tenant, email=person.email)
        messages.success(request, _("%(email)s removed from the project.") % {"email": person.email})
        return redirect("tenant_detail", pk=pk)
    role = request.POST.get("role")
    if role not in CORP_STAFF_ROLES:
        role = Membership.Role.TENANT_USER
    if role == Membership.Role.TENANT_ADMIN and not person.may_tenant_admin:  # owner 2026-11-03: eligibility gate
        messages.error(
            request,
            _("%(email)s is not allowed to be a project admin — grant it in Platform users first.")
            % {"email": person.email},
        )
        return redirect("tenant_detail", pk=pk)
    m, created = Membership.objects.update_or_create(tenant=tenant, user=person, defaults={"role": role})
    audit(request.user, "member_added" if created else "member_role_changed", tenant, email=person.email, role=role)
    messages.success(request, _("%(email)s is a user of this project.") % {"email": person.email})
    return redirect("tenant_detail", pk=pk)


# --- staff: organizations ------------------------------------------------------


@staff_required(*STAFF_VIEW_ROLES)
def org_list_view(request):
    user = request.user
    from .models import Lifecycle

    # correction 11: the count is of tenants that are not archived; the archived ones only on demand
    orgs = Organization.objects.select_related("sales_rep", "kam", "resource_manager", "support_engineer").annotate(
        n_tenants=Count("tenants", filter=~Q(tenants__status=Lifecycle.ARCHIVED), distinct=True),
        n_archived=Count("tenants", filter=Q(tenants__status=Lifecycle.ARCHIVED), distinct=True),
        n_users=Count("users", filter=Q(users__is_active=True), distinct=True),
    )
    if not (user.is_super_admin or user.has_role(Role.RESOURCE_MANAGER) or user.has_role(Role.SUPPORT)):
        orgs = orgs.filter(Q(sales_rep=user) | Q(kam=user))
    q = request.GET.get("q", "").strip()
    if q:
        orgs = orgs.filter(Q(name__icontains=q) | Q(tax_id__icontains=q))
    kind = request.GET.get("kind", "")
    if kind in (OrgKind.CLOUD, OrgKind.CORP):
        orgs = orgs.filter(kind=kind)
    show_archived = request.GET.get("archived") == "1"
    if not show_archived:
        orgs = orgs.filter(archived_at__isnull=True)
    # options for the Manager / KAM header filters, from the organizations this user can see (u09)
    from accounts.models import User

    mgr_ids = set(orgs.order_by().values_list("sales_rep_id", flat=True)) | set(
        orgs.order_by().values_list("resource_manager_id", flat=True)
    )
    kam_ids = set(orgs.order_by().values_list("kam_id", flat=True))
    managers = list(User.objects.filter(pk__in={i for i in mgr_ids if i}).order_by("full_name", "email"))
    kams = list(User.objects.filter(pk__in={i for i in kam_ids if i}).order_by("full_name", "email"))
    manager = request.GET.get("manager", "")
    if manager.isdigit():
        orgs = orgs.filter(Q(sales_rep_id=manager) | Q(resource_manager_id=manager))
    kam = request.GET.get("kam", "")
    if kam.isdigit():
        orgs = orgs.filter(kam_id=kam)
    orgs = with_pending(list(orgs), user)
    return render(
        request,
        "tenants/org_list.html",
        {
            "orgs": orgs,
            "q": q,
            "kind": kind,
            "kinds": OrgKind.choices,
            "managers": managers,
            "kams": kams,
            "manager": manager,
            "kam": kam,
            "show_archived": show_archived,
            "can_create": __import__("tenants.permissions", fromlist=["can_add_org"]).can_add_org(user),
            "can_invite": _can_invite_org(user),
        },
    )


def with_pending(orgs, user):
    """Each organization gets n_pending: the approval requests waiting for its tenants."""
    counts = {}
    for i in waiting_items(user):
        oid = i["t"].organization_id
        counts[oid] = counts.get(oid, 0) + 1
    for o in orgs:
        o.n_pending = counts.get(o.pk, 0)
    return orgs


def _can_invite_org(user):
    from .permissions import can_add_org

    return can_add_org(user)


@staff_required(*STAFF_VIEW_ROLES)
def org_invite_view(request):
    """Invite a person to register a new customer organization of a chosen type (cloud or corporate)."""
    from accounts.forms import OrganizationInviteForm

    user = request.user
    if not _can_invite_org(user):
        raise PermissionDenied
    form = OrganizationInviteForm(request.POST or None, inviter=user)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        Invitation.objects.open().filter(email=email).update(revoked_at=timezone.now())
        inv, token = Invitation.issue(
            purpose=Invitation.Purpose.ORGANIZATION,
            email=email,
            grants=form.grants(),
            invited_by=user,
            ttl_hours=settings.REGISTRATION_TTL_HOURS * 3,
        )
        link = site_link(reverse("accept_invitation", args=[token]))
        notify(
            [email],
            "organization_invitation",
            {
                "inviter": user,
                "invitation": inv,
                "link": link,
                "corp": inv.grants.get("org_kind") == OrgKind.CORP,
                "hours": settings.REGISTRATION_TTL_HOURS * 3,
            },
            _("Register your organization on the Example Cloud portal"),
            allow_client=True,
        )  # the registration link is the client's own: always sent
        audit(user, "organization_invited", inv, email=email, kind=inv.grants.get("org_kind"))
        messages.success(request, _("Invitation sent to %(email)s.") % {"email": email})
        return redirect("org_invite")
    invitations = Invitation.objects.filter(purpose=Invitation.Purpose.ORGANIZATION).select_related("invited_by")
    if not (user.is_super_admin or user.has_role(Role.RESOURCE_MANAGER) or user.has_role(Role.SUPPORT)):
        invitations = invitations.filter(invited_by=user)
    return render(request, "tenants/org_invite.html", {"form": form, "invitations": invitations[:50]})


@staff_required(*STAFF_VIEW_ROLES)
def org_edit_view(request, pk=None):
    from .permissions import can_add_org, can_edit_org

    org = get_object_or_404(Organization, pk=pk) if pk else None
    if org is None:
        if not can_add_org(request.user):
            raise PermissionDenied
    elif not can_edit_org(request.user, org):
        raise PermissionDenied
    form = OrganizationForm(request.POST or None, instance=org, actor=request.user)
    if request.method == "POST" and form.is_valid():
        old_rep = org.sales_rep if org else None
        created = org is None
        with transaction.atomic():
            org = form.save()
            if created:  # correction 14: its user at once
                form.save_user(org, request.user)
            else:  # u11: a platform/org user can be set as the org admin (corporate orgs)
                form.save_org_admin(org, request.user)
        audit(request.user, "organization_saved", org, fields=",".join(form.changed_data))
        if org.sales_rep and org.sales_rep != old_rep:
            notify(
                [org.sales_rep],
                "org_assigned",
                {"org": org, "link": site_link(reverse("org_detail", args=[org.pk]))},
                _("Organization assigned to you: %(name)s") % {"name": org.name},
            )
        messages.success(request, _("Saved."))
        nxt = request.POST.get("next")
        return redirect(nxt if nxt and nxt.startswith("/") else reverse("org_detail", args=[org.pk]))
    people = {}  # u10: pk -> contact details, for the team table's auto-filled cells
    for _name in ("sales_rep", "kam"):
        _fld = form.fields.get(_name)
        if _fld is not None:
            for u in _fld.queryset:
                people[str(u.pk)] = {"e": u.email, "p": u.phone, "c": u.get_city_display()}
    for u in getattr(form, "_org_admin_cands", []):  # u11: org-admin picker candidates
        people[str(u.pk)] = {"e": u.email, "p": u.phone, "c": u.get_city_display()}
    return render(
        request,
        "tenants/org_edit.html",
        {"form": form, "org": org, "next": request.GET.get("next", ""), "people_meta": people},
    )


def archive_org_users(actor, org):
    """u02: an archived organization's users are archived too: no sign-in, no roles, no open invitation (kept for the
    record; bringing the organization back does not bring them back)."""
    from accounts.models import Invitation, Kind
    from accounts.people import clear_roles

    now, n = timezone.now(), 0
    for person in org.users.filter(kind=Kind.CUSTOMER, archived_at__isnull=True):
        with transaction.atomic():
            person.archived_at, person.is_active, person.login_enabled = now, False, False
            person.save(update_fields=["archived_at", "is_active", "login_enabled"])
            Invitation.objects.open().filter(user=person).update(revoked_at=now)
            Invitation.objects.open().filter(email__iexact=person.email).update(revoked_at=now)
            clear_roles(person)
        audit(actor, "user_archived", person, email=person.email, why="organization archived")
        n += 1
    return n


@require_POST
@staff_required(Role.RESOURCE_MANAGER)
def org_delete_view(request, pk, action="archive"):
    """Organizations are archived, never deleted (their history stays); an archived one can be brought back."""
    org = get_object_or_404(Organization, pk=pk)
    if action == "unarchive":
        if org.archived_at:
            org.archived_at, org.archived_by = None, None
            org.save(update_fields=["archived_at", "archived_by"])
            audit(request.user, "organization_unarchived", org, name=org.name)
            messages.success(request, _("Organization %(name)s is active again.") % {"name": org.name})
        return redirect("org_detail", pk=pk)
    blockers = org.archive_blockers()
    if blockers:
        messages.error(request, _("Cannot archive: %(why)s.") % {"why": ", ".join(str(b) for b in blockers)})
        return redirect("org_detail", pk=pk)
    org.archived_at, org.archived_by = timezone.now(), request.user
    org.save(update_fields=["archived_at", "archived_by"])
    n = archive_org_users(request.user, org)  # u02: its users go with it
    audit(request.user, "organization_archived", org, name=org.name, tax_id=org.tax_id or "", users_archived=n)
    messages.success(request, _("Organization %(name)s archived, with its %(n)s users.") % {"name": org.name, "n": n})
    return redirect("org_detail", pk=pk)


@staff_required(*STAFF_VIEW_ROLES)
def org_detail_view(request, pk):
    org = get_object_or_404(Organization.objects.select_related("sales_rep"), pk=pk)
    if not org.visible_to(request.user):
        raise PermissionDenied
    from .models import Lifecycle

    show_archived = request.GET.get("archived") == "1"
    tenants = org.tenants.all() if show_archived else org.tenants.exclude(status=Lifecycle.ARCHIVED)
    hist = __import__("tenants.tracking", fromlist=["history"]).history(org)  # correction 13: on the page
    return render(
        request,
        "tenants/org_detail.html",
        {
            "org": org,
            "tenants": tenants,
            "show_archived": show_archived,
            "n_archived": org.tenants.filter(status=Lifecycle.ARCHIVED).count(),
            "people": _people_with_forms(org, request.user),
            "can_add_users": _can_add_org_users(request.user, org)
            and not org.archived_at
            and PortalSettings.get().users_add,
            "can_set_roles": __import__(
                "tenants.permissions", fromlist=["can_assign_project_admin"]
            ).can_assign_project_admin(request.user, org)
            and not org.archived_at,
            "user_form": __import__("tenants.forms", fromlist=["OrgUserForm"]).OrgUserForm(prefix="ou", org=org),
            "rows": hist,
            "history_projects": sorted({(r["tenant"].name or r["tenant"].title) for r in hist}),
            "join_requests": org.join_requests.select_related("user", "decided_by"),
            "can_edit": __import__("tenants.permissions", fromlist=["can_edit_org"]).can_edit_org(request.user, org),
            "can_new_project": __import__("tenants.permissions", fromlist=["can_create_project"]).can_create_project(
                request.user, org
            )
            and not org.archived_at,
            "can_archive": request.user.has_role(Role.RESOURCE_MANAGER)
            and not org.archived_at
            and not org.archive_blockers(),
            "can_unarchive": request.user.has_role(Role.RESOURCE_MANAGER) and bool(org.archived_at),
            **_registration_context(org, request.user),
        },
    )


def _registration_context(org, user, form=None):
    """Correction 16: a registered organization waiting for approval."""
    from . import orgreg
    from .forms import OrgRegistrationForm

    if not org.is_pending:
        return {}
    step = orgreg.next_step(org)
    can = bool(step) and orgreg.can(user, org, step)
    from accounts.regcheck import SUSPICIOUS, labels

    chk = org.reg_check or {}
    return {
        "reg": {
            "steps": orgreg.steps(org),
            "step": step,
            "can": can,
            "people": orgreg.asked(org, step) if step else [],
            "check": {
                "score": chk.get("score", 0),
                "ip": chk.get("ip", ""),
                "reasons": labels(chk.get("flags", [])),
                "suspicious": chk.get("score", 0) >= SUSPICIOUS,
            }
            if chk
            else None,
            "admin": org.users.filter(is_org_admin=True).first(),
            "form": (form or OrgRegistrationForm(instance=org, prefix="reg", actor=user))
            if can and step == "rm"
            else None,
        }
    }


@require_POST
@staff_required(*STAFF_VIEW_ROLES)
def org_registration_view(request, pk, action):
    """Correction 16: approve (the resource manager's step with the platform users, the sales rep's) or refuse."""
    from . import orgreg
    from .forms import OrgRegistrationForm

    org = get_object_or_404(Organization, pk=pk)
    if not org.visible_to(request.user) and not request.user.has_role(Role.RESOURCE_MANAGER):
        raise PermissionDenied
    step = orgreg.next_step(org)
    if action == "reject":
        reason = (request.POST.get("reason") or "").strip()
        if not reason:
            messages.error(request, _("Give the reason."))
            return redirect("org_detail", pk=pk)
        name = org.name
        err = orgreg.reject(request.user, org, reason)
        if err:
            messages.error(request, err)
            return redirect("org_detail", pk=pk)
        messages.success(
            request, _("The registration of %(n)s is refused and removed; the registrant is told.") % {"n": name}
        )
        return redirect("org_list")
    data = None
    if step == "rm":
        form = OrgRegistrationForm(request.POST, instance=org, prefix="reg", actor=request.user)
        if not form.is_valid():
            for name, errs in form.errors.items():
                for e in errs:
                    messages.error(request, f"{form.fields[name].label}: {e}" if name in form.fields else e)
            return redirect("org_detail", pk=pk)
        data = form.cleaned_data
        org = Organization.objects.get(pk=pk)  # the form changed the instance in memory
    err = orgreg.approve(request.user, org, step, data)
    if err:
        messages.error(request, err)
    else:
        org.refresh_from_db()
        messages.success(
            request,
            _("%(n)s is approved: its admin can sign in now.") % {"n": org.name}
            if not org.is_pending
            else _("Approved. Now waiting for: %(w)s.")
            % {"w": ", ".join(str(s["label"]) for s in orgreg.steps(org) if not s["done"])},
        )
    return redirect("org_detail", pk=pk)


def _people_with_forms(org, user):
    from accounts.people import _open_invites, state

    from .forms import OrgUserForm

    users = list(org.users.order_by("-is_org_admin", "full_name"))
    ids, emails = _open_invites(users)
    # owner 2026-11-03: hide users who were invited but have not accepted yet (manage pending invites on Users)
    from accounts.people import customer_roles

    from .permissions import can_assign_project_admin

    can = _can_add_org_users(user, org) and not org.archived_at
    set_roles = can_assign_project_admin(user, org) and not org.archived_at
    people = []
    for p in users:
        st = state(p, ids, emails)
        if st == "invited":
            continue
        p.row_state = st  # owner 2026-11-05 (§4): the org page shows Lock/Unlock from this
        if can:
            p.edit_form = OrgUserForm(prefix=f"ou{p.pk}", user=p, org=org)
        if set_roles:  # §4 part 2b: a per-user role dialog for staff
            r = customer_roles(p)
            p.roles_now = r
            p.admin_of_ids = [t.pk for t in r["admin_of"]]
        people.append(p)
    return people


def _can_add_org_users(user, org):
    """u12: invite/lock/unlock an org's users — its manager, KAM or resource manager, or its own org admin."""
    from .permissions import can_manage_org_users

    return can_manage_org_users(user, org)


@require_POST
@staff_required(*STAFF_VIEW_ROLES)
def org_user_view(request, pk, uid=None):
    """Add a user to an organization, or change one (name, phone, organization admin)."""
    from .forms import OrgUserForm

    org = get_object_or_404(Organization, pk=pk)
    if not org.visible_to(request.user) or not _can_add_org_users(request.user, org):
        raise PermissionDenied
    person = get_object_or_404(User, pk=uid, organization=org) if uid else None
    if person is None and not PortalSettings.get().users_add:  # u03: only invited
        raise PermissionDenied
    form = OrgUserForm(request.POST, prefix="ou" if person is None else f"ou{person.pk}", user=person, org=org)
    if form.is_valid():
        u = form.save(request.user)
        messages.success(
            request, (_("%(n)s is added.") if person is None else _("%(n)s is saved.")) % {"n": u.full_name}
        )
    else:
        for name, errs in form.errors.items():
            for e in errs:
                messages.error(request, f"{form.fields[name].label}: {e}" if name in form.fields else e)
    return redirect(reverse("org_detail", args=[pk]) + "#users")


@staff_required(*STAFF_VIEW_ROLES)
def org_resources_view(request, pk):
    """Organization → Resource tracking: every change of its tenants' resources, newest first."""
    from .tracking import history

    org = get_object_or_404(Organization, pk=pk)
    if not org.visible_to(request.user):
        raise PermissionDenied
    return render(request, "tenants/org_resources.html", {"org": org, "rows": history(org)})


@login_required
def tenant_tracking_view(request, pk):
    """Tenant → Resource tracking: every change of its resources, newest first (correction 11: as the organization's)."""
    from .tracking import history

    tenant = _tenant_for(request, pk)
    user = request.user
    if not (
        user.is_staff_member
        or tenant.membership_role(user) in (Membership.Role.ORG_ADMIN, Membership.Role.TENANT_ADMIN)
        or (tenant.organization_id and user.organization_id == tenant.organization_id and user.is_org_admin)
    ):
        raise PermissionDenied
    return render(
        request,
        "tenants/org_resources.html",
        {"tenant": tenant, "org": tenant.organization, "rows": history(tenants=[tenant])},
    )


# --- customers: own organization -------------------------------------------------


def _org_tenants(user):
    from .models import Lifecycle

    org = user.organization
    qs = (
        org.tenants.all()
        if (user.is_org_admin or user.is_org_readonly)
        else Tenant.objects.filter(memberships__user=user)
    )
    return qs.exclude(status=Lifecycle.ARCHIVED)


def _with_resources(tenants):
    out = []
    for t in tenants:
        recs = t.current_quota_records()
        t.res = {k: sum(getattr(r, k) or 0 for r in recs) for k in ("cpu", "ram_gb", "gpu", "disk_gb")}
        t.dep = t.deployments.exclude(state="plan").first()
        out.append(t)
    return out


@customer_required
def my_org_view(request):
    """Organization → Tenants: the tenants, ordering, the default ssh key."""
    user = request.user
    if not user.organization_id:
        return redirect("dashboard")
    org = user.organization
    ctx = {"org": org, "tenants": _with_resources(_org_tenants(user).order_by("name")), "is_admin": user.is_org_admin}
    if user.is_org_admin:
        from .forms import SshKeyForm

        key_form = SshKeyForm(request.POST or None, prefix="key", initial={"default_ssh_key": org.default_ssh_key})
        if request.method == "POST" and key_form.is_valid():
            org.default_ssh_key = key_form.cleaned_data["default_ssh_key"]
            org.save(update_fields=["default_ssh_key"])
            audit(user, "org_ssh_key_changed", org, set=bool(org.default_ssh_key))
            messages.success(request, _("Default ssh key saved."))
            return redirect("my_org")
        ctx["key_form"] = key_form
    return render(request, "tenants/my_org.html", ctx)


@customer_required
def org_users_view(request):
    """Organization → Users: the organization's people, their state and roles; join requests."""
    from accounts import people

    user = request.user
    if not user.organization_id or not user.is_org_admin:
        raise PermissionDenied
    org = user.organization
    show_archived = request.GET.get("archived") == "1"
    return render(
        request,
        "tenants/org_users.html",
        {
            "org": org,
            "is_admin": True,
            "rows": people.customer_rows(org, show_archived),
            "show_archived": show_archived,
            "archived_toggle": "?" if show_archived else "?archived=1",
            "customer_roles": people.CUSTOMER_ROLES,
            "customer_columns": people.CUSTOMER_ROLE_COLUMNS,
            "tenants": list(
                org.tenants.filter(approval__in=[Approval.PENDING, Approval.APPROVED]).exclude(
                    status__in=["saved", "archived", "deleting"]
                )
            ),
            "join_requests": org.join_requests.filter(state=JoinRequest.State.PENDING).select_related("user"),
        },
    )


def org_dashboard(request):
    """Organization admin's dashboard: tenants, resources in force, VMs, people, recent events."""
    user = request.user
    org = user.organization
    tenants = _with_resources(_org_tenants(user).order_by("name"))
    from adoption.models import PlatformStats

    groups = {}
    for st in PlatformStats.objects.all():
        for g, v in ((st.vms or {}).get("by_group") or {}).items():
            agg = groups.setdefault(g, {"total": 0, "running": 0})
            agg["total"] += v.get("total", 0)
            agg["running"] += v.get("running", 0)
    vms = {"total": 0, "running": 0}
    for t in tenants:
        t.vms = groups.get(t.one_group_name or "", {"total": 0, "running": 0})
        vms["total"] += t.vms["total"]
        vms["running"] += t.vms["running"]
    res = {
        k: sum(t.res[k] for t in tenants if t.approval == Approval.APPROVED)
        for k in ("cpu", "ram_gb", "gpu", "disk_gb")
    }
    ctx = {
        "org": org,
        "tenants": tenants,
        "res": res,
        "vms": vms,
        "is_admin": user.is_org_admin,
        "approved": sum(1 for t in tenants if t.approval == Approval.APPROVED),
        "pending": sum(1 for t in tenants if t.approval == Approval.PENDING),
        "deployed": sum(1 for t in tenants if t.dep and t.dep.state == "done"),
    }
    if user.is_org_admin:
        ctx.update(
            people=org.users.filter(is_active=True).count(),
            join_requests=org.join_requests.filter(state=JoinRequest.State.PENDING).count(),
            invitations=Invitation.objects.filter(organization=org, accepted_at__isnull=True).count(),
            entries=org_log_entries(org, limit=10),
        )
    return render(request, "tenants/org_dashboard.html", ctx)


def org_log_entries(org, tenant_pk=None, limit=500):
    tenant_ids = [str(t) for t in org.tenants.values_list("pk", flat=True)]
    user_ids = [str(u) for u in org.users.values_list("pk", flat=True)]
    if tenant_pk:
        q = Q(target_type="tenant", target_id=str(tenant_pk))
    else:
        q = (
            Q(target_type="organization", target_id=str(org.pk))
            | Q(target_type="tenant", target_id__in=tenant_ids)
            | Q(target_type="user", target_id__in=user_ids)
        )
    return AuditLog.objects.filter(q).select_related("actor")[:limit]


@customer_required
def org_logs_view(request):
    """Organization → Logs: the same page as the portal's Logs, for the organization, its tenants and its users."""
    from accounts.views import logs_page

    user = request.user
    if not user.organization_id or not user.is_org_admin:
        raise PermissionDenied
    org = user.organization
    tenants = list(org.tenants.order_by("name"))
    tenant_ids = [str(t.pk) for t in tenants]
    user_ids = [str(u) for u in org.users.values_list("pk", flat=True)]
    qs = AuditLog.objects.filter(
        Q(target_type="organization", target_id=str(org.pk))
        | Q(target_type="tenant", target_id__in=tenant_ids)
        | Q(target_type="user", target_id__in=user_ids)
    )
    objects = [
        (_("Organization"), [(f"organization:{org.pk}", org.name)]),
        (_("Projects"), [(f"tenant:{t.pk}", t.name or t.title) for t in tenants]),
    ]
    people = User.objects.filter(pk__in=qs.exclude(actor=None).values("actor").distinct()).order_by("email")
    return logs_page(request, qs, objects, people, title_scope=org.name)


@customer_required
def org_tasks_view(request):
    """Organization → Tasks: the work on its tenants (deployments, network changes, deletions, checks)."""
    from accounts.tasks import all_tasks
    from accounts.views import tasks_page

    user = request.user
    if not user.organization_id or not user.is_org_admin:
        raise PermissionDenied
    org = user.organization
    days = PortalSettings.get().task_days or 10
    mine = {t.pk for t in org.tenants.all()}
    rows = [r for r in all_tasks(days) if r["tenant"] is not None and r["tenant"].pk in mine]
    return tasks_page(request, rows, days, customer=True, tenants=list(org.tenants.order_by("name")))


def _send_customer_invite(request, org, form):
    email = form.cleaned_data["email"]
    Invitation.objects.open().filter(email=email).update(revoked_at=timezone.now())
    inv, token = Invitation.issue(
        purpose=Invitation.Purpose.CUSTOMER,
        email=email,
        organization=org,
        grants=form.grants(),
        invited_by=request.user,
    )
    link = site_link(reverse("accept_invitation", args=[token]))
    notify(
        [email],
        "member_invitation",
        {"invitation": inv, "org": org, "inviter": request.user, "link": link, "hours": settings.INVITATION_TTL_HOURS},
        _("%(name)s invites you to the Example Cloud portal") % {"name": org.name},
        allow_client=True,
    )  # the invitation link is the client's sign-in: always sent
    audit(request.user, "customer_invited", org, email=email, grants=inv.summary)
    messages.success(request, _("Invitation sent to %(email)s.") % {"email": email})


@require_POST
@customer_required
def tenant_invite_view(request, pk):
    """A tenant admin invites a tenant user to their tenant."""
    tenant = _tenant_for(request, pk)
    role = tenant.membership_role(request.user)
    if role not in (Membership.Role.ORG_ADMIN, Membership.Role.TENANT_ADMIN):
        raise PermissionDenied
    form = CustomerInviteForm(request.POST, prefix="inv", tenants=[tenant], allow_org_admin=False)
    if form.is_valid():
        if (
            role == Membership.Role.TENANT_ADMIN
            and form.grants()["tenants"].get(str(tenant.pk)) != Membership.Role.TENANT_USER
        ):
            messages.error(request, _("Project admins can invite project users only."))
        else:
            _send_customer_invite(request, tenant.organization, form)
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
    return redirect("tenant_detail", pk=pk)


def _order(request, org, user, staff=False):
    """The order form of a tenant for `org` (its admin, or staff ordering on its behalf). Returns a response."""
    corporate_allowed = org.is_corp
    form = OrderForm(request.POST or None, corporate_allowed=corporate_allowed, organization=org, user=user)
    if request.method == "POST" and form.is_valid():
        order = form.order()
        cd = form.cleaned_data
        with transaction.atomic():  # correction 13: resources only; the admin, the igw and the keys at provisioning
            tenant = Tenant.objects.create(
                organization=org,
                title=form.short_name(),
                kind=form.tenant_kind(),
                stage=cd.get("stage") or TenantStage.POC,
                date_review=cd.get("date_review"),
                name=form.short_name(),
                order=order,
                created_by=user,
                igw_ssh_from="",
            )
            from .lifecycle import default_review

            default_review(tenant)
            tenant.save(update_fields=["date_review"])
            from .admins import set_tenant_admin  # correction 15: chosen with the order

            set_tenant_admin(tenant, cd["tenant_admin"], user, why="ordered")
        audit(
            user,
            "tenant_requested",
            tenant,
            title=tenant.title,
            kind=tenant.kind,
            order=ordering.describe(tenant.order),
            on_behalf=org.name if staff else "",
        )
        from accounts.views import notify_tenant_pending

        notify_tenant_pending(tenant, user)
        messages.success(request, _("Project ordered. It will be available after approval."))
        return redirect("tenant_detail", pk=tenant.pk)
    from .admins import admin_candidates

    people = {
        str(u.pk): {"e": u.email, "p": u.phone, "c": u.get_city_display()} for u in admin_candidates(org)
    }  # u10 team autofill
    return render(
        request,
        "tenants/order.html",
        {
            "form": form,
            "corporate_allowed": corporate_allowed,
            "org": org,
            "staff": staff,
            "orgs": Organization.objects.order_by("name") if staff else None,
            "pools": {k: {"site": p["site"], "gpu": p["gpu"]} for k, p in form.pools.items()},
            "prefix_lengths": form._inet_lengths,
            "people_meta": people,
            "base_subnets": form._base_meta,
            "base_choices": form._base_choices,
            "corpnet_prefix_lengths": form._corpnet_lengths,
            "corpnet_base_subnets": form._corp_base_meta,
            "corpnet_base_choices": form._corp_base_choices,
        },
    )


@customer_required
def new_tenant_view(request):
    """An organization admin orders a tenant. It starts as a POC and waits for approval."""
    user = request.user
    if not user.is_org_admin or not user.organization_id:
        raise PermissionDenied
    return _order(request, user.organization, user)


@staff_required(*STAFF_VIEW_ROLES)
def staff_new_tenant_view(request):
    """Staff order a tenant on behalf of an organization (chosen first); it goes through the usual approvals. u12: a
    manager may order for their own orgs; a KAM may not create projects."""
    from .permissions import can_create_project, is_rm

    org = Organization.objects.filter(pk=request.GET.get("org") or request.POST.get("org") or 0).first()
    if org is None:
        orgs = Organization.objects.order_by("name")
        if not is_rm(request.user):  # a manager chooses among their own orgs; others get none
            orgs = orgs.filter(sales_rep=request.user) if request.user.is_sales else orgs.none()
        return render(request, "tenants/order.html", {"staff": True, "org": None, "orgs": orgs})
    if not can_create_project(request.user, org):
        raise PermissionDenied
    return _order(request, org, request.user, staff=True)


@customer_required
def person_view(request, pk):
    """(Old page) the roles are edited on Organization → Users."""
    admin = request.user
    if not admin.is_org_admin:
        raise PermissionDenied
    if request.method == "GET":
        return redirect(f"{reverse('org_users')}?sel=u-{pk}")
    person = get_object_or_404(User, pk=pk, organization=admin.organization)
    tenants = list(admin.organization.tenants.filter(approval__in=[Approval.PENDING, Approval.APPROVED]))
    current = {m.tenant_id: m.role for m in Membership.objects.filter(user=person)}
    if request.method == "POST" and person.pk != admin.pk:
        with transaction.atomic():
            person.is_org_admin = request.POST.get("org_admin") == "on"
            person.save(update_fields=["is_org_admin"])
            for t in tenants:
                role = request.POST.get(f"tenant_{t.pk}", "")
                if role in (Membership.Role.TENANT_ADMIN, Membership.Role.TENANT_USER):
                    Membership.objects.update_or_create(user=person, tenant=t, defaults={"role": role})
                else:
                    Membership.objects.filter(user=person, tenant=t).delete()
        audit(admin, "member_roles_changed", person, org_admin=person.is_org_admin)
        messages.success(request, _("Roles saved."))
        return redirect("org_users")
    return render(
        request,
        "tenants/person.html",
        {
            "person": person,
            "rows": [(t, current.get(t.pk, "")) for t in tenants],
            "roles": Membership.TENANT_ROLES,
            "is_self": person.pk == admin.pk,
        },
    )


@require_POST
@customer_required
def remove_person_view(request, pk):
    admin = request.user
    person = get_object_or_404(User, pk=pk, organization=admin.organization)
    if not admin.is_org_admin or person.pk == admin.pk:
        raise PermissionDenied
    person.is_active = False
    person.save(update_fields=["is_active"])
    Membership.objects.filter(user=person).delete()
    audit(admin, "member_removed", admin.organization, email=person.email)
    messages.success(request, _("%(email)s no longer has access.") % {"email": person.email})
    return redirect("org_users")


@require_POST
@customer_required
def decide_join_view(request, pk, decision):
    if decision not in ("accept", "reject"):
        raise PermissionDenied
    admin = request.user
    jr = get_object_or_404(JoinRequest, pk=pk, state=JoinRequest.State.PENDING)
    if not admin.is_org_admin or admin.organization_id != jr.organization_id:
        raise PermissionDenied
    applicant = jr.user
    with transaction.atomic():
        jr.decided_by, jr.decided_at = admin, timezone.now()
        if decision == "accept":
            jr.state = JoinRequest.State.ACCEPTED
            applicant.organization = jr.organization
            applicant.save(update_fields=["organization"])
        else:
            jr.state = JoinRequest.State.REJECTED
            applicant.is_active = False
            applicant.save(update_fields=["is_active"])
        jr.save()
    audit(admin, f"join_{jr.state}", jr.organization, email=applicant.email)
    notify(
        [applicant],
        "join_decided",
        {"join": jr, "org": jr.organization},
        _("Your request to join %(name)s") % {"name": jr.organization.name},
    )
    if decision == "accept":
        messages.success(request, _("Accepted. Give the user a role in your projects."))
        return redirect("person", pk=applicant.pk)
    from accounts.regcheck import purge  # owner 2026-10-29 20:30: a declined request is deleted (the log keeps it)

    purge()
    messages.success(request, _("Request rejected."))
    return redirect("org_users")
