"""Tenant lifecycle (quota.md): status by hand, review dates, the daily round (saved → archived, notices).

Statuses change by hand only; the one exception is saved → archived on the review date (a month after saving).
The platform is not changed by a status change yet (blocking the account, powering VMs off, deleting them with a
backup kept): the record and its review date are what the portal keeps now.

Notices to the organization admins (the email to clients is off until Settings → Portal → "Email to clients"):
    production contract (contract_end)  90, 30 and 14 days before, then every day of the last week
    POC (date_review of a POC)          every week while the POC lasts, then every day of the last week
Each notice is logged (tenant_notice); with email to clients off it is logged as held.
"""
from datetime import timedelta

from django.utils import timezone

from accounts.models import audit

from .models import STATUS_NEXT, STATUS_PERIOD, Lifecycle, Tenant, TenantStage


def set_status(tenant, new, review=None, user=None, note=""):
    """A status change by hand (validated by the caller against STATUS_NEXT); saves and logs it."""
    old = tenant.status
    if new not in STATUS_NEXT.get(old, ()):
        raise ValueError(f"{old} → {new} is not allowed")
    today = timezone.localdate()
    if new in STATUS_PERIOD:
        tenant.date_review = review or today + timedelta(days=STATUS_PERIOD[new])
    elif new == Lifecycle.OK:
        tenant.date_review = (today + timedelta(days=Tenant.POC_DAYS)) if tenant.stage == TenantStage.POC else None
    else:
        tenant.date_review = None
    tenant.status, tenant.status_changed_at = new, timezone.now()
    tenant.save(update_fields=["status", "status_changed_at", "date_review", "updated_at"])
    audit(user, "tenant_status_changed", tenant, old=old, new=new,
          review=tenant.date_review.isoformat() if tenant.date_review else "", note=note[:300])
    return tenant


def default_review(tenant):
    """A new POC is reviewed a month after it was created."""
    if tenant.stage == TenantStage.POC and not tenant.date_review:
        start = timezone.localdate(tenant.created_at) if tenant.created_at else timezone.localdate()
        tenant.date_review = start + timedelta(days=Tenant.POC_DAYS)
    return tenant


def _contract_due(t, today):
    if not t.contract_end or t.stage != TenantStage.CLIENT:
        return None
    left = (t.contract_end - today).days
    if left in (90, 30, 14) or 0 <= left <= 7:
        return {"what": "contract", "days": left, "date": t.contract_end.isoformat()}
    return None


def _poc_due(t, today):
    if t.stage != TenantStage.POC or not t.date_review or t.status != Lifecycle.OK:
        return None
    left = (t.date_review - today).days
    created = timezone.localdate(t.created_at) if t.created_at else today
    if 0 <= left <= 7 or (left > 7 and (today - created).days % 7 == 0):
        return {"what": "poc", "days": left, "date": t.date_review.isoformat()}
    return None


def notices(today=None):
    """[(tenant, notice)] due today."""
    today = today or timezone.localdate()
    out = []
    for t in Tenant.objects.filter(approval="approved", status=Lifecycle.OK).select_related("organization"):
        for fn in (_contract_due, _poc_due):
            n = fn(t, today)
            if n:
                out.append((t, n))
    return out


def run_daily(today=None):
    """The daily round. Returns what it did."""
    from .models import PortalSettings
    today = today or timezone.localdate()
    archived = 0
    for t in Tenant.objects.filter(status=Lifecycle.SAVED, date_review__lte=today):
        t.status, t.status_changed_at, t.date_review = Lifecycle.ARCHIVED, timezone.now(), None
        t.save(update_fields=["status", "status_changed_at", "date_review", "updated_at"])
        audit(None, "tenant_status_changed", t, old="saved", new="archived", note="a month after saving (automatic)")
        archived += 1
    send = PortalSettings.get().notify_clients
    sent = held = 0
    for t, n in notices(today):
        admins = list(t.organization.admins) if t.organization_id else []
        if send and admins:
            from accounts.utils import notify, site_link
            from django.urls import reverse
            notify(admins, f"tenant_{n['what']}_ends", {"tenant": t, "days": n["days"], "date": n["date"],
                                                         "link": site_link(reverse("tenant_detail", args=[t.pk]))},
                   (f"{t.name}: " + ("the contract ends" if n["what"] == "contract" else "the POC ends") + f" {n['date']}"))
            sent += 1
        else:
            held += 1
        audit(None, "tenant_notice", t, what=n["what"], days=n["days"], date=n["date"],
              to=",".join(a.email for a in admins), sent="yes" if send and admins else "held")
    return {"archived": archived, "notices_sent": sent, "notices_held": held}


def cleanup_history(days):
    """Tenant history older than `days`: versions of quota and connectivity records replaced that long ago."""
    from .models import Connectivity, QuotaRecord
    limit = timezone.now() - timedelta(days=days)
    removed = 0
    for model, key in ((QuotaRecord, ("tenant_id", "site", "cluster")), (Connectivity, ("tenant_id", "site"))):
        latest = {}
        for r in model.objects.order_by(*key, "-version").only("pk", "effective_from", *key, "version"):
            k = tuple(getattr(r, f) for f in key)
            if k not in latest:
                latest[k] = r
                prev_start = r.effective_from
                continue
            # r was replaced when the next version took effect; drop it when that was before the limit
            if prev_start and prev_start < limit:
                r.delete()
                removed += 1
            prev_start = r.effective_from
    return removed
