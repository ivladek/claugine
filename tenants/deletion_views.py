"""Deleting a tenant: always a request first; it starts only after approval (a resource manager, or the staff member who asked)."""

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.mailtemplates import T
from accounts.models import Role, audit
from accounts.utils import notify, site_link

from . import undeploy
from .models import Tenant, TenantDeletion, current_runner
from .views import back_to


def _back(t):
    return redirect(reverse("tenant_detail", args=[t.pk]) + "#delete")


def _confirmed(request, t):
    typed = (request.POST.get("confirm") or "").strip()
    if typed not in {x for x in (t.name, t.group_name) if x}:
        messages.error(request, _("Type the project name to confirm."))
        return False
    return True


def pending_deletion(t):
    return t.deletions.filter(state=TenantDeletion.State.PENDING).first()


def can_decide(t, d, user):
    """A resource manager approves; staff who asked for the deletion themselves may approve their own request."""
    return t.can_approve_as(user, Role.RESOURCE_MANAGER) or (
        d is not None and d.requested_by_id == user.pk and t.can_delete_now(user)
    )


def can_retry(t, d, user):
    """core-047 (twenty-third full review): a deletion that failed half way left the project DELETING - nobody can ask
    again (it counts as deleted); resource managers and super admins run that request again instead."""
    from .models import Approval, Lifecycle

    return (
        d is not None
        and d.state == TenantDeletion.State.FAILED
        and t.status == Lifecycle.DELETING
        and t.approval == Approval.APPROVED
        and t._approving_staff(user)
    )


def deletion_context(t, user):
    d = t.deletions.select_related("requested_by", "decided_by").first()
    if d is not None and d.state == TenantDeletion.State.PENDING and d.planned_at is None:
        undeploy.make_plan(d)  # a request from before the resource list
    can_decide_ = can_decide(t, d, user)
    return {
        "can_retry_deletion": can_retry(t, d, user),
        "deletion": d,
        "del_pending": d if d and d.state == TenantDeletion.State.PENDING else None,
        "can_request_deletion": t.can_request_deletion(user) and not pending_deletion(t),
        "can_delete_now": t.can_delete_now(user),
        "can_decide_deletion": can_decide_,
    }


def preview_for(t, user):
    """What a deletion would remove now (reads OpenNebula): for the people who can delete or approve."""
    if t.is_deleted or not (t.can_delete_now(user) or t.can_approve_as(user, Role.RESOURCE_MANAGER)):
        return None
    d = TenantDeletion(tenant=t)
    u = undeploy.Undeployer(t)
    if not u.live:
        d.plan_error = "OpenNebula is not touched: read-only platform or not deployed"
        return d
    try:
        d.plan = u.inventory()
    except Exception as exc:  # noqa: BLE001 - shown
        d.plan_error = f"{type(exc).__name__}: {exc}"
    return d


@require_POST
def deletion_request_view(request, pk):
    t = get_object_or_404(Tenant, pk=pk)
    if not t.can_request_deletion(request.user) or pending_deletion(t):
        raise PermissionDenied
    if not _confirmed(request, t):
        return _back(t)
    d = TenantDeletion.objects.create(tenant=t, requested_by=request.user, note=(request.POST.get("note") or "")[:2000])
    undeploy.make_plan(d)
    audit(request.user, "tenant_deletion_requested", t, note=d.note[:200], resources=d.plan_total)
    notify(
        t.approvers(Role.RESOURCE_MANAGER),
        T.TENANT_DELETION_REQUESTED,
        {"tenant": t, "d": d, "link": site_link(reverse("tenant_detail", args=[t.pk]) + "#delete")},
        _("Deletion of project %(t)s requested") % {"t": t.name or t.title},
    )
    messages.success(request, _("Deletion requested; a resource manager approves it."))
    return _back(t)


@require_POST
def deletion_decide_view(request, pk, did, decision):
    t = get_object_or_404(Tenant, pk=pk)
    d = get_object_or_404(TenantDeletion, pk=did, tenant=t, state=TenantDeletion.State.PENDING)
    user = request.user
    fields = {"decided_by": user, "decided_at": timezone.now()}
    if decision == "cancel":
        if not (t.can_request_deletion(user) or t.can_approve_as(user, Role.RESOURCE_MANAGER)):
            raise PermissionDenied
        fields["state"] = TenantDeletion.State.CANCELLED
    elif decision == "reject":
        if not t.can_approve_as(user, Role.RESOURCE_MANAGER):
            raise PermissionDenied
        fields["state"], fields["reason"] = TenantDeletion.State.REJECTED, (request.POST.get("reason") or "")[:2000]
    elif decision == "approve":
        if not can_decide(t, d, user):
            raise PermissionDenied
        if not _confirmed(request, t):
            return _back(t)
        # the approval claims the deletion for one run: undeploy.execute() runs only a deletion in this state
        fields["state"], fields["runner"] = TenantDeletion.State.RUNNING, current_runner()
    else:
        raise PermissionDenied
    # core-025 (eleventh full review): the request was read without a lock, so it is decided only while it is still
    # pending - one update filtered on the state. A cancellation, rejection or approval committed meanwhile wins:
    # this decision changes nothing and starts nothing, so a second approval never starts a second deletion.
    if fields["state"] == TenantDeletion.State.RUNNING:
        from accounts import maintenance

        with maintenance.admitting():  # core-046: admitted only while no maintenance runs (accounts.maintenance)
            took = TenantDeletion.objects.filter(pk=d.pk, state=TenantDeletion.State.PENDING).update(**fields)
    else:
        took = TenantDeletion.objects.filter(pk=d.pk, state=TenantDeletion.State.PENDING).update(**fields)
    if not took:
        d.refresh_from_db()
        messages.error(
            request,
            _("This deletion request was already %(s)s; nothing was changed.") % {"s": d.get_state_display()},
        )
        return back_to(request, reverse("tenant_detail", args=[t.pk]) + "#delete")
    d.refresh_from_db()
    audit(
        user,
        f"tenant_deletion_{decision}d" if decision != "cancel" else "tenant_deletion_cancelled",
        t,
        reason=d.reason[:200],
    )
    if decision == "approve":
        undeploy.start(d, user)
        messages.info(request, _("Deletion approved and started."))
    else:
        messages.success(request, _("Deletion request %(s)s.") % {"s": d.get_state_display()})
    return back_to(request, reverse("tenant_detail", args=[t.pk]) + "#delete")


@require_POST
def delete_now_view(request, pk):
    """Staff ask for a deletion too: it starts only after an approval (theirs is enough)."""
    t = get_object_or_404(Tenant, pk=pk)
    if not t.can_delete_now(request.user) or pending_deletion(t):
        raise PermissionDenied
    if not _confirmed(request, t):
        return _back(t)
    d = TenantDeletion.objects.create(tenant=t, requested_by=request.user, note=(request.POST.get("note") or "")[:2000])
    undeploy.make_plan(d)
    audit(request.user, "tenant_deletion_requested", t, note=d.note[:200], resources=d.plan_total)
    notify(
        [u for u in t.approvers(Role.RESOURCE_MANAGER) if u.pk != request.user.pk],
        T.TENANT_DELETION_REQUESTED,
        {"tenant": t, "d": d, "link": site_link(reverse("tenant_detail", args=[t.pk]) + "#delete")},
        _("Deletion of project %(t)s requested") % {"t": t.name or t.title},
    )
    messages.success(request, _("Deletion requested: it starts after approval; you can approve it yourself."))
    return _back(t)


@require_POST
def deletion_retry_view(request, pk, did):
    """core-047: run a failed deletion again - claimed once (failed -> running, one update), its resources listed on
    the platforms again (what is already gone is not listed), then run as an approved deletion is. Cancelled and
    rejected requests are never run."""
    from accounts import maintenance

    t = get_object_or_404(Tenant, pk=pk)
    d = get_object_or_404(TenantDeletion, pk=did, tenant=t)
    if not can_retry(t, d, request.user):
        raise PermissionDenied
    if not _confirmed(request, t):
        return _back(t)
    with maintenance.admitting():  # core-046: admitted only while no maintenance runs
        # commit-044: the plan is kept - listed again by the run (undeploy.execute) with its group and user ids
        took = TenantDeletion.objects.filter(pk=d.pk, state=TenantDeletion.State.FAILED).update(
            state=TenantDeletion.State.RUNNING,
            runner=current_runner(),
            error="",
            finished_at=None,
            plan_error="listed again for the retry",
        )
    if not took:  # a second click, or a retry started meanwhile: one run
        messages.error(request, _("This deletion is already running again; nothing was changed."))
        return _back(t)
    d.refresh_from_db()
    audit(request.user, "tenant_deletion_retried", t, deletion=d.pk)
    undeploy.start(d, request.user)
    messages.info(request, _("The deletion runs again."))
    return _back(t)
