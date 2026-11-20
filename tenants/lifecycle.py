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

ARCHIVE_STALE = timedelta(hours=6)  # an archival claimed this long ago and not finished was cut off: run again


def claim_archive(tenant, due=None, stale=None):
    """core-038: an archival claims its project in one update - saved (and, for the daily round, still due on `due`),
    or an archival cut off before `stale` - to "deleting" ("the portal is removing it from the platform (then
    archived)"). A review date extended first leaves nothing to claim; one changed after the claim is refused
    (set_review). Returns whether this caller holds it."""
    now = timezone.now()
    q = Tenant.objects.filter(pk=tenant.pk)
    if stale is not None:
        q = q.filter(status=Lifecycle.DELETING, archiving_since__lt=stale)
    else:
        q = q.filter(status=Lifecycle.SAVED)
        if due is not None:
            q = q.filter(date_review__lte=due)
    # core-044: with the process that runs it - counted by the maintenance gate while that process lives
    from django.db import transaction

    from accounts import maintenance

    from .models import current_runner

    with transaction.atomic():
        maintenance.admit()  # core-046: admitted only while no maintenance runs (accounts.maintenance)
        return bool(
            q.update(
                status=Lifecycle.DELETING,
                status_changed_at=now,
                archiving_since=now,
                archive_runner=current_runner(),
                updated_at=now,
            )
        )


def finish_archive(tenant_pk, user=None, note=""):
    """The cleanup of a claimed project (undeploy.archive: the platform, then the portal's records); archived when it
    is done. On a failure the project is saved again, with its review date, and the reason kept on its page; the daily
    round tries it again. Returns "" when archived, else the reason."""
    from .undeploy import archive as cleanup

    t = Tenant.objects.get(pk=tenant_pk)
    try:
        cleanup(t, user)
    except Exception as exc:  # noqa: BLE001 - whatever stopped it, the project goes back to saved
        error = str(exc)[:2000] or type(exc).__name__
        Tenant.objects.filter(pk=t.pk, status=Lifecycle.DELETING).update(
            status=Lifecycle.SAVED,
            status_changed_at=timezone.now(),
            archiving_since=None,
            archive_runner="",
            archive_error=error,
        )
        audit(user, "tenant_archive_failed", t, error=error[:300], note="tried again by the daily round")
        return error
    now = timezone.now()
    Tenant.objects.filter(pk=t.pk).update(
        status=Lifecycle.ARCHIVED,
        status_changed_at=now,
        date_review=None,
        archiving_since=None,
        archive_runner="",
        archive_error="",
        updated_at=now,
    )
    audit(user, "tenant_status_changed", t, old="saved", new="archived", note=note[:300])
    return ""


def archive(tenant, user=None, due=None, note=""):
    """Claim and archive now (the daily round). Returns "" when archived, "not due" when there was nothing to claim,
    else the reason it failed."""
    if not claim_archive(tenant, due=due):
        return "not due"
    return finish_archive(tenant.pk, user, note)


def start_archive(tenant, user=None, note=""):
    """core-039: saved → archived by hand - the same archival, claimed now and run in the background (the platform
    can take minutes), as a deletion is. Returns whether it was claimed."""
    import threading

    from django.conf import settings
    from django.db import connection

    if not claim_archive(tenant):
        return False
    audit(user, "tenant_archive_started", tenant, note=note[:300])
    if not getattr(settings, "DEPLOY_ASYNC", True):
        finish_archive(tenant.pk, user, note)
        return True

    def worker():
        try:
            finish_archive(tenant.pk, user, note)
        finally:
            connection.close()

    threading.Thread(target=worker, name=f"archive-{tenant.pk}", daemon=True).start()
    return True


def set_review(tenant, date):
    """core-038: the review date is written only while the project is as the page showed it - not while an archival
    holds it. Returns whether it was written."""
    now = timezone.now()
    done = (
        Tenant.objects.filter(pk=tenant.pk, status=tenant.status)
        .exclude(status__in=(Lifecycle.DELETING, Lifecycle.ARCHIVED))
        .update(date_review=date, updated_at=now)
    )
    if done:
        tenant.date_review = date
    return bool(done)


def set_status(tenant, new, review=None, user=None, note=""):
    """A status change by hand (validated by the caller against STATUS_NEXT); saves and logs it. Archived (core-039)
    is the archival: everything the project still has goes first (start_archive), in the background."""
    old = tenant.status
    if new not in STATUS_NEXT.get(old, ()):
        raise ValueError(f"{old} → {new} is not allowed")
    if new == Lifecycle.ARCHIVED:
        if not start_archive(tenant, user, note=note or "by hand"):
            raise ValueError("the project changed meanwhile")
        tenant.refresh_from_db()
        return tenant
    today = timezone.localdate()
    if new in STATUS_PERIOD:
        tenant.date_review = review or today + timedelta(days=STATUS_PERIOD[new])
    elif new == Lifecycle.OK:
        tenant.date_review = (today + timedelta(days=Tenant.POC_DAYS)) if tenant.stage == TenantStage.POC else None
    else:
        tenant.date_review = None
    tenant.status, tenant.status_changed_at = new, timezone.now()
    tenant.save(update_fields=["status", "status_changed_at", "date_review", "updated_at"])
    audit(
        user,
        "tenant_status_changed",
        tenant,
        old=old,
        new=new,
        review=tenant.date_review.isoformat() if tenant.date_review else "",
        note=note[:300],
    )
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
    # owner 2026-11-11: an archived project keeps nothing anywhere but its record in the portal. core-038: each is
    # claimed (still saved and due) before anything is removed; an archival cut off is run again
    from accounts.tasks import reap_archivals

    reap_archivals()  # core-044: an archival whose process ended is saved again, and so archived below
    for t in Tenant.objects.filter(status=Lifecycle.SAVED, date_review__lte=today):
        if archive(t, due=today, note="a month after saving (automatic)") == "":
            archived += 1
    stale = timezone.now() - ARCHIVE_STALE
    for t in Tenant.objects.filter(status=Lifecycle.DELETING, archiving_since__lt=stale):
        if claim_archive(t, stale=stale) and finish_archive(t.pk, note="an archival cut off, run again") == "":
            archived += 1
    send = PortalSettings.get().client_mail  # owner 2026-11-17: Settings → Mail, e-mail to organization users
    sent = held = 0
    for t, n in notices(today):
        admins = list(t.organization.admins) if t.organization_id else []
        if send and admins:
            from django.urls import reverse

            from accounts.mailtemplates import T
            from accounts.utils import notify, site_link

            notify(
                admins,
                T.TENANT_CONTRACT_ENDS if n["what"] == "contract" else T.TENANT_POC_ENDS,
                {
                    "tenant": t,
                    "days": n["days"],
                    "date": n["date"],
                    "link": site_link(reverse("tenant_detail", args=[t.pk])),
                },
                (
                    f"{t.name}: "
                    + ("the contract ends" if n["what"] == "contract" else "the POC ends")
                    + f" {n['date']}"
                ),
            )
            sent += 1
        else:
            held += 1
        audit(
            None,
            "tenant_notice",
            t,
            what=n["what"],
            days=n["days"],
            date=n["date"],
            to=",".join(a.email for a in admins),
            sent="yes" if send and admins else "held",
        )
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
