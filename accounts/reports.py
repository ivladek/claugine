"""Settings → Reports (owner 2026-10-31, u01): the portal's e-mail reports, when they go and to whom.

Each report has a row in the table: on/off, daily or weekly (a weekday), the time (the portal's time zone) and
"redirect". A redirected report goes only to the address set in "Send reports to" (one person reads them instead of
the recipients: "for <name>" in the subject); without that address a redirected report is not sent at all.

    resources       resource managers        usage of the shared clusters; new, changed and deleted organizations and
                                             projects in the period
    sales           each sales rep           the organizations they manage, with their projects
    kam             each key account manager the organizations they manage, with their projects
    support         each support engineer    the internal (corporate) organizations they serve, with their projects
    statistics      super admins             the portal in numbers: organizations, projects, reviews, requests, the
                                             platforms and the portal's own state

Review dates are highlighted: come (red) or close (a week for a POC, 3 months for a client: Tenant.review_alert).
The timer "reports" (every 5 minutes, the active node only) sends what is due (run_due); "Send now" sends one at once.
The state is kept in PortalSettings.reports: {redirect_to, items: {key: {on, period, weekday, time, redirect}},
sent: {key: ISO time}}.
"""
from datetime import datetime, time, timedelta

from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l

# key, label, recipients (label), per person, default period
REPORTS = [
    # u10 (owner 2026-11-02): the registrations-only report was dropped
    ("resources", _l("Usage and changes"), _l("resource managers"), False, "daily"),
    ("sales", _l("Organizations of a sales rep"), _l("each sales rep"), True, "weekly"),
    ("kam", _l("Organizations of a key account manager"), _l("each key account manager"), True, "weekly"),
    ("support", _l("Internal organizations"), _l("each support engineer"), True, "weekly"),
    ("statistics", _l("Portal statistics"), _l("super admins"), False, "weekly"),
    ("backups", _l("Backups"), _l("super admins & support engineers"), False, "weekly"),  # u06
]
KEYS = [k for k, *_r in REPORTS]
# u05: the recipient role, shown in the subject of every report (not only in test samples)
ROLE_NAMES = {"resources": _l("Resource manager"), "sales": _l("Sales rep"),
              "kam": _l("KAM"), "support": _l("Support engineer"), "statistics": _l("Super admin"), "backups": _l("Backups")}
WEEKDAYS = [(0, _l("Monday")), (1, _l("Tuesday")), (2, _l("Wednesday")), (3, _l("Thursday")), (4, _l("Friday")),
            (5, _l("Saturday")), (6, _l("Sunday"))]
DEFAULT = {"on": True, "time": "09:00", "weekday": 0, "redirect": False}
FIRST_GRACE = timedelta(minutes=30)  # a report never sent goes only this soon after its time


def _settings():
    from tenants.models import PortalSettings
    return PortalSettings.get()


def config(s=None):
    """{redirect_to, items: {key: {on, period, weekday, time, redirect}}, sent: {key: iso}} with the defaults."""
    raw = (s or _settings()).reports or {}
    items = {}
    for key, _label, _who, _per, period in REPORTS:
        it = {**DEFAULT, "period": period, **((raw.get("items") or {}).get(key) or {})}
        items[key] = it
    return {"redirect_to": raw.get("redirect_to", ""), "items": items, "sent": dict(raw.get("sent") or {})}


def _save(**changes):
    from tenants.models import PortalSettings
    s = _settings()
    raw = dict(s.reports or {})
    raw.update(changes)
    PortalSettings.objects.filter(pk=s.pk).update(reports=raw)


def mark_sent(key, at=None):
    sent = dict(config()["sent"])
    sent[key] = (at or timezone.now()).isoformat()
    _save(sent=sent)


def rows():
    """For the settings page."""
    c = config()
    out = []
    for key, label, who, per, _p in REPORTS:
        it = c["items"][key]
        last = c["sent"].get(key)
        out.append({"key": key, "label": label, "who": who, **it,
                    "last": timezone.localtime(datetime.fromisoformat(last)) if last else None})
    return out


def clean(post):
    """The settings form: (config, errors)."""
    import re
    errors = {}
    to = (post.get("redirect_to") or "").strip()
    if to and not re.match(r"^[^@\s]+@[^@\s]+\.[a-z]{2,63}$", to, re.I):
        errors["redirect_to"] = _("Not an e-mail address.")
    items = {}
    for key in KEYS:
        t = (post.get(f"time_{key}") or "09:00").strip()
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", t):
            errors[f"time_{key}"] = _("HH:MM")
        period = post.get(f"period_{key}") if post.get(f"period_{key}") in ("daily", "weekly") else "daily"
        try:
            wd = min(6, max(0, int(post.get(f"weekday_{key}") or 0)))
        except ValueError:
            wd = 0
        items[key] = {"on": post.get(f"on_{key}") == "on", "period": period, "weekday": wd, "time": t,
                      "redirect": post.get(f"redirect_{key}") == "on"}
    return {"redirect_to": to, "items": items}, errors


def save(conf):
    _save(redirect_to=conf["redirect_to"], items=conf["items"])


# --- when ----------------------------------------------------------------------------------------------------------

def last_slot(item, now=None):
    """The latest time this report was due, at or before now (aware)."""
    now = timezone.localtime(now or timezone.now())
    hh, mm = (int(x) for x in item["time"].split(":"))
    slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if item["period"] == "weekly":
        slot -= timedelta(days=(slot.weekday() - int(item["weekday"])) % 7)
    if slot > now:
        slot -= timedelta(days=7 if item["period"] == "weekly" else 1)
    return slot


def period_of(item):
    return timedelta(days=7 if item["period"] == "weekly" else 1)


def due(key, conf=None, now=None):
    conf = conf or config()
    it = conf["items"][key]
    if not it["on"]:
        return False
    slot = last_slot(it, now)
    sent = conf["sent"].get(key)
    if sent and datetime.fromisoformat(sent) >= slot:
        return False
    late = (now or timezone.now()) - slot
    if not sent:  # never sent: only at its time (not a slot passed before it was set up), u02
        return late < FIRST_GRACE
    # a slot missed while the portal was down: within the period
    return late < period_of(it)


def run_due(log=print, now=None):
    """The timer: send every report that is due. Returns a short summary."""
    conf = config()
    done = []
    for key in KEYS:
        if due(key, conf, now):
            try:
                n, detail = send(key, conf)
                done.append(f"{key}: {detail}")
            except Exception as exc:  # noqa: BLE001 - the next round tries again
                log(f"report {key}: {exc}")
                done.append(f"{key}: {exc}"[:120])
    return "; ".join(done) or "nothing due"


# --- sending -------------------------------------------------------------------------------------------------------

def _staff(**flags):
    # owner 2026-11-03: reports reach every role holder with an e-mail, including users who have not been invited yet
    # (login not enabled); only blocked (is_active False) and archived users are left out.
    from .models import Kind, User
    return list(User.objects.filter(kind=Kind.STAFF, is_active=True, archived_at__isnull=True, **flags)
                .exclude(email="").order_by("email"))


def recipients(key):
    """[(user, context)] the report goes to (per person: their own part)."""
    if key == "statistics":
        return [(u, {}) for u in _staff(is_super_admin=True)]
    if key == "resources":
        return [(u, {}) for u in _staff(is_resource_manager=True)]
    if key == "sales":
        return [(u, {"orgs": org_rows(sales_rep=u)}) for u in _staff(is_sales=True)]
    if key == "kam":
        return [(u, {"orgs": org_rows(kam=u)}) for u in _staff(is_kam=True)]
    if key == "support":
        return [(u, {"orgs": org_rows(support=u)}) for u in _staff(is_support=True)]
    if key == "backups":  # u06: super admins and support engineers
        seen = {}
        for u in _staff(is_super_admin=True) + _staff(is_support=True):
            seen[u.pk] = u
        return [(u, {}) for u in seen.values()]
    return []


def send(key, conf=None, now=None):
    """Send one report now (to its recipients, or to the redirect address). Returns (messages sent, detail)."""
    from .utils import notify
    conf = conf or config()
    it = conf["items"][key]
    label = dict((k, lab) for k, lab, *_r in REPORTS)[key]
    now = now or timezone.now()
    since = now - period_of(it)
    redirect_to = conf["redirect_to"] if it["redirect"] else ""
    if it["redirect"] and not redirect_to:
        mark_sent(key, now)
        return 0, "redirected, but no address in Send reports to: not sent"
    common = context(key, since, now)
    sent, people = 0, 0
    targets = recipients(key)
    per_person = dict((k, per) for k, _l1, _w, per, _p in REPORTS)[key]
    groups = [(u, extra) for u, extra in targets] if per_person else ([(None, {})] if targets else [])
    for user, extra in groups:
        if per_person and not extra.get("orgs"):
            continue  # nothing of theirs
        people += 1
        to = [redirect_to] if redirect_to else ([user] if user else [u for u, _e in targets])
        whom = user.full_name or user.email if user else ", ".join(u.email for u, _e in targets)
        subject = f"{ROLE_NAMES.get(key, '')} · {label} · {timezone.localtime(now).strftime('%d.%m.%Y')}"
        if redirect_to:
            subject += " · " + _("for %(w)s") % {"w": whom}
        sent += notify(to, f"report_{key}", {**common, **extra, "person": user, "redirected": bool(redirect_to),
                                             "for_whom": whom, "label": label}, subject)
    mark_sent(key, now)
    detail = f"{sent} sent" + (f" to {redirect_to} (redirected)" if redirect_to else "") + f", {people} recipient(s)"
    return sent, detail


# --- what is in them -----------------------------------------------------------------------------------------------

def project_row(t):
    return {"id": t.name or t.title, "name": t.name or t.title, "kind": t.get_kind_display(),
            "stage": t.get_stage_display(), "status": t.get_status_display(), "review": t.date_review,
            "alert": t.review_alert, "created": timezone.localtime(t.created_at).date() if t.created_at else None}


def org_rows(sales_rep=None, kam=None, support=None):
    """The organizations of one person, with their live projects."""
    from django.db.models import Q
    from tenants.models import Approval, Lifecycle, OrgKind, Organization
    qs = Organization.objects.filter(archived_at__isnull=True)
    if sales_rep is not None:
        qs = qs.filter(sales_rep=sales_rep)
    if kam is not None:
        qs = qs.filter(kam=kam)
    if support is not None:  # internal organizations: theirs, or without an engineer (any engineer serves them)
        qs = qs.filter(kind=OrgKind.CORP).filter(Q(support_engineer=support) | Q(support_engineer__isnull=True))
    out = []
    for o in qs.order_by("name"):
        ts = [project_row(t) for t in o.tenants.filter(approval=Approval.APPROVED).exclude(status=Lifecycle.ARCHIVED)
              .order_by("date_review", "name")]
        out.append({"name": o.name, "tax_id": o.tax_id or "", "kind": o.get_kind_display(), "projects": ts,
                    "alerts": sum(1 for t in ts if t["alert"])})
    return out


def _changes(since):
    """New, changed and deleted organizations and projects since a time."""
    from .models import AuditLog
    from tenants.models import Organization, Tenant
    acts = {"organization_registration_done": _l("organizations approved"), "organization_edited": _l("organizations changed"),
            "tenant_requested": _l("projects requested"), "tenant_approved": _l("projects approved"),
            "tenant_deployed": _l("projects deployed"), "tenant_rejected": _l("projects rejected"),
            "tenant_status_changed": _l("project status changes"), "tenant_stage_changed": _l("project stage changes"),
            "tenant_review_changed": _l("review date changes"), "tenant_deletion_requested": _l("deletions requested"),
            "tenant_deleted": _l("projects deleted")}
    logs = list(AuditLog.objects.filter(action__in=acts, created_at__gte=since).values("action", "target_type", "target_id"))
    names = {}
    tids = {int(r["target_id"]) for r in logs if r["target_type"] == "tenant" and r["target_id"].isdigit()}
    oids = {int(r["target_id"]) for r in logs if r["target_type"] == "organization" and r["target_id"].isdigit()}
    names.update({("tenant", str(t.pk)): t.name or t.title for t in Tenant.objects.filter(pk__in=tids)})
    names.update({("organization", str(o.pk)): o.name for o in Organization.objects.filter(pk__in=oids)})
    out = []
    for action, label in acts.items():
        rs = [r for r in logs if r["action"] == action]
        if rs:
            who = sorted({names.get((r["target_type"], r["target_id"]), "?") for r in rs})
            out.append({"label": label, "n": len(rs), "names": ", ".join(who[:12]) + (" …" if len(who) > 12 else "")})
    return out


def _usage(all_clusters=False):
    from inventory.capacity import clusters
    out = []
    for r in clusters(kinds=None if all_clusters else ["shared"]):
        lg = r["logical"]
        out.append({"site": r["site"], "cluster": r["cluster"].name,
                    "kind": getattr(r["cluster"], "get_kind_display", lambda: r.get("kind", ""))() if hasattr(r["cluster"], "get_kind_display") else r.get("kind", ""),
                    "projects": r["alloc"].get("tenants", 0),
                    "vcpu": lg["used_vcpu"], "vcpu_cap": lg["vcpu"], "vcpu_pct": lg["cpu_pct"],
                    "ram": lg["used_ram"], "ram_cap": lg["ram_gb"], "ram_pct": lg["ram_pct"],
                    "gpu": lg["used_gpus"], "gpu_cap": lg["gpus"], "disk": r["alloc"].get("disk_gb", 0)})
    return out


def provisioned_rows():
    """Every live project with the resources provisioned to it (summed over its pools). One row per project."""
    from tenants.models import Approval, Lifecycle, Tenant
    out = []
    for t in (Tenant.objects.filter(approval=Approval.APPROVED).exclude(status=Lifecycle.ARCHIVED)
              .select_related("organization").order_by("organization__name", "name")):
        recs = t.current_quota_records()
        out.append({
            "org": t.organization.name if t.organization else "—", "kind": t.get_kind_display(),
            "name": t.name or t.title, "stage": t.get_stage_display(), "status": t.get_status_display(),
            "sites": ", ".join(sorted({r.site for r in recs})) or "—", "pools": len(recs),
            "cpu": sum(r.cpu for r in recs), "ram": sum(r.ram_gb for r in recs), "gpu": sum(r.gpu for r in recs),
            "disk": sum(r.disk_gb for r in recs), "backups": sum((r.backups_gb or 0) for r in recs),
        })
    return out


def _prov_totals(rows):
    keys = ("cpu", "ram", "gpu", "disk", "backups")
    t = {k: sum(r[k] for r in rows) for k in keys}
    t["projects"] = len(rows)
    return t


def project_stats(since):
    """Projects in the period: total (live now), new (provisioned), updated (changed), deleted."""
    from .models import AuditLog
    from tenants.models import Approval, Lifecycle, Tenant

    def tenants_with(actions):
        return {r["target_id"] for r in AuditLog.objects.filter(
            action__in=actions, target_type="tenant", created_at__gte=since).values("target_id")}

    new = tenants_with(["tenant_deployed"])
    deleted = tenants_with(["tenant_deleted"])
    updated = tenants_with(["resources_changed", "quota_applied", "tenant_status_changed",
                            "tenant_stage_changed", "tenant_review_changed"]) - new - deleted
    return {"total": Tenant.objects.filter(approval=Approval.APPROVED).exclude(status=Lifecycle.ARCHIVED).count(),
            "new": len(new), "updated": len(updated), "deleted": len(deleted)}


def _tenants_with(actions, since):
    from .models import AuditLog
    return {int(r["target_id"]) for r in AuditLog.objects.filter(action__in=actions, target_type="tenant",
            created_at__gte=since).values("target_id") if (r["target_id"] or "").isdigit()}


def cluster_matrix(since):
    """Projects per shared cluster (resource-manager report, v2): total / new / deleted / renewed in the period,
    with a Total column. "Renewed" = the review date was moved (tenant_review_changed)."""
    from inventory.capacity import clusters
    from tenants.models import Approval, Lifecycle, QuotaRecord, Tenant
    cols = [(r["site"], r["cluster"].name) for r in clusters(kinds=["shared"])]
    labels = [f"{s} · {c}" for s, c in cols]
    live = set(Tenant.objects.filter(approval=Approval.APPROVED).exclude(status=Lifecycle.ARCHIVED).values_list("pk", flat=True))
    new = _tenants_with(["tenant_deployed"], since)
    deleted = _tenants_with(["tenant_deleted"], since)
    renewed = _tenants_with(["tenant_review_changed"], since) - new - deleted
    all_pks = live | new | deleted | renewed
    rec = {}
    for q in QuotaRecord.objects.filter(tenant_id__in=all_pks).values("tenant_id", "site", "cluster"):
        rec.setdefault(q["tenant_id"], set()).add((q["site"], q["cluster"]))

    def row(label, pks):
        pks = list(pks)
        return {"label": label, "total": len(pks),
                "cells": [sum(1 for pk in pks if sc in rec.get(pk, ())) for sc in cols]}
    return {"labels": labels, "rows": [row(_("Total projects"), live), row(_("New"), new),
                                       row(_("Deleted"), deleted), row(_("Renewed"), renewed)]}


def org_stats(since):
    """Organizations (resource-manager report, v2): total / new / deleted in the period."""
    from tenants.models import Organization
    orgs = Organization.objects.filter(archived_at__isnull=True)
    return {"total": orgs.count(), "new": orgs.filter(created_at__gte=since).count(),
            "deleted": len(_orgs_with(["organization_archived"], since))}


def _orgs_with(actions, since):
    from .models import AuditLog
    return {int(r["target_id"]) for r in AuditLog.objects.filter(action__in=actions, target_type="organization",
            created_at__gte=since).values("target_id") if (r["target_id"] or "").isdigit()}


def platform_health():
    """Super-admin report (v2): the portal's HA and release, and the managed platforms."""
    from . import ha, ha_agent
    out = {"version": "", "role": "", "has_peer": None, "platforms": None}
    try:
        out["version"] = ha_agent.version()
    except Exception:  # noqa: BLE001
        pass
    try:
        out["role"], out["has_peer"] = ha.role(), ha.has_peer()
    except Exception:  # noqa: BLE001
        pass
    try:
        from inventory.models import Platform
        out["platforms"] = Platform.objects.count()
    except Exception:  # noqa: BLE001
        pass
    return out


def backups_stats():
    """Super-admin / backups report (u07): the last portal and platform front-end backups (one a day each)."""
    try:
        from accounts import appliance
        s = appliance._settings()
        return {"portal_at": s.backup_last_at, "portal_result": (s.backup_last_result or "")[:160],
                "fe_at": s.fe_backup_last_at, "fe_result": (s.fe_backup_last_result or "")[:160]}
    except Exception:  # noqa: BLE001
        return {}


def _statistics(since):
    from collections import Counter
    from .models import AuditLog, Task
    from tenants.models import Approval, Deployment, Lifecycle, NetworkRequest, Organization, ResourceChange, Tenant
    live = Tenant.objects.filter(approval=Approval.APPROVED).exclude(status=Lifecycle.ARCHIVED)
    by = Counter((t.kind, t.stage) for t in live)
    alerts = [t for t in live.exclude(date_review__isnull=True).order_by("date_review") if t.review_alert]
    orgs = Organization.objects.filter(archived_at__isnull=True)
    from . import ha_agent
    try:
        from accounts import appliance
        st = appliance._settings()
        backup = (st.backup_last_at, (st.backup_last_result or "")[:80])
    except Exception:  # noqa: BLE001
        backup = (None, "")
    return {
        "orgs": orgs.count(), "orgs_cloud": orgs.filter(kind="cloud").count(), "orgs_corp": orgs.filter(kind="corp").count(),
        "orgs_new": orgs.filter(created_at__gte=since).count(),
        "orgs_waiting": orgs.filter(registration="pending").count(),
        "projects": live.count(), "by": sorted(((k, s, n) for (k, s), n in by.items())),
        "statuses": Counter(t.get_status_display() for t in live).most_common(),
        "projects_new": Tenant.objects.filter(created_at__gte=since).count(),
        "requests_waiting": Tenant.objects.filter(approval=Approval.PENDING).count(),
        "changes_waiting": ResourceChange.objects.filter(state="pending").count(),
        "network_waiting": NetworkRequest.objects.filter(state="pending").count() if hasattr(NetworkRequest, "state") else 0,
        "reviews": [project_row(t) | {"org": t.organization.name if t.organization else ""} for t in alerts[:40]],
        "deploy_failed": Deployment.objects.filter(state="failed", started_at__gte=since).count(),
        "tasks_failed": Task.objects.filter(state="failed", created_at__gte=since).count() if hasattr(Task, "created_at") else 0,
        "logins": AuditLog.objects.filter(action="login", created_at__gte=since).count(),
        "backup": backup, "version": ha_agent.version(),
    }


def context(key, since, now):
    from .utils import site_link
    from django.urls import reverse
    ctx = {"since": timezone.localtime(since), "now": timezone.localtime(now), "link": site_link(reverse("tenant_list"))}
    if key == "resources":  # u05: after the cluster usage, only the projects-by-cluster matrix and the org counts
        ctx.update(usage=_usage(), matrix=cluster_matrix(since), org_stats=org_stats(since))
    if key == "support":  # u05: the support report starts like the resource-manager one, then the per-org tables
        ctx.update(usage=_usage(), matrix=cluster_matrix(since), org_stats=org_stats(since))
    if key == "statistics":  # u05: all clusters, the portal stats, platform health and backups
        ctx.update(stats=_statistics(since), usage=_usage(all_clusters=True), pstats=project_stats(since),
                   health=platform_health(), backups=backups_stats())
    if key == "backups":  # u06: the weekly backups report
        ctx.update(backups=backups_stats(), bconf=_backups_conf())
    return ctx


def _backups_conf():
    """u06/u07: the two backup blocks, each with its local and S3 destinations (independent schedules), for the report.
    The S3 start/period fall back to the local schedule when left empty."""
    from tenants.models import PortalSettings
    from accounts import edition
    from accounts.edition import effective as _eff
    s = PortalSettings.get()
    s3_allowed = edition.allows("s3_backups", s)

    def block(local_on, path, start, keep, s3, bucket):
        return {"local_on": local_on, "on": local_on or s3, "local": path, "start": start, "keep": keep,
                "s3": s3, "bucket": bucket}

    return {
        # the values **in force**: on a Standard installation the stored ones are ignored, and a report that
        # announced S3 and a 10-day retention that are not happening would be worse than no report
        "portal": block(_eff(s, "backup_local"), _eff(s, "backup_path"), _eff(s, "backup_start"),
                        _eff(s, "backup_keep_days"), _eff(s, "backup_s3"), s.backup_s3_bucket),
        "platform": dict(block(s.fe_backup_local, s.fe_backup_path, s.fe_backup_time, s.fe_backup_keep_days,
                               bool(s.fe_backup_s3 and s3_allowed),
                               s.backup_s3_bucket if s.fe_backup_s3_use_portal else s.fe_backup_s3_bucket),
                         platforms=s.fe_backup_platforms or []),
    }


def setup_redirect_default():
    """Nothing: the address is a setting of each installation."""
    return None


def time_of(item):
    hh, mm = (int(x) for x in item["time"].split(":"))
    return time(hh, mm)
