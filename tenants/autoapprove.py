"""The resource manager step without a resource manager (owner 2026-11-12): "if checkbox cleared then no any approval
request to that role".

With the resource manager's approval switched off in Settings → Platform, a new order is approved at once with what
was ordered: the name, the site and pool, the resources, the default internet pool, the project admin chosen on the
order. It is the resource manager's own form (tenants.forms.RMApprovalForm) filled with its defaults and applied by
the same code (tenants.views.apply_rm_approval). When the form does not validate or the addresses cannot be
allocated, the step comes back - for a super admin - with the reason on the project, so the order is never lost.
"""

from accounts.models import audit

from .models import Approval, PortalSettings


def run(tenant):
    """After an order. Returns "approved", "waiting" (other approvals still wanted), "not used" or the failure."""
    if PortalSettings.get().rm_approval_required or tenant.approval != Approval.PENDING or tenant.rm_approved_at:
        return "not used"
    from . import ordering
    from .forms import RMApprovalForm
    from .views import apply_rm_approval

    blank = RMApprovalForm(instance=tenant, prefix="rm")
    data = {}
    for name in blank.fields:
        v = blank[name].value()
        if v is None or v is False:
            continue
        data[blank.add_prefix(name)] = getattr(v, "pk", v)
    form = RMApprovalForm(data, instance=tenant, prefix="rm")
    why = ""
    if not form.is_valid():
        why = "; ".join(f"{k}: {' '.join(v)}" for k, v in form.errors.items())
    else:
        try:
            tenant, finalized, _covered, _no_admin = apply_rm_approval(tenant, form, None)
        except ordering.AllocationError as exc:
            why = str(exc)
    if why:
        tenant.refresh_from_db()
        tenant.order = {**(tenant.order or {}), "auto_rm_failed": why[:500]}
        tenant.save(update_fields=["order"])
        audit(None, "tenant_auto_approval_failed", tenant, why=why[:500])
        return why
    audit(None, "tenant_rm_approved", tenant, name=tenant.name, kind=tenant.kind, automatic=True)
    if not finalized:
        return "waiting"
    ordering.materialize(tenant, None)
    audit(None, "tenant_approved", tenant, automatic=True)
    from .provision import auto

    auto(tenant, None)
    return "approved"
