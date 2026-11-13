"""Settings → Reports: the portal's e-mail reports, what is in them, when they go and to whom.

Owner 2026-11-13 (job 2026-11-09_resource-dashboard): four reports, one per role, replace the six of u01-u07:

    sa          super admins          totals of platforms, clusters, hosts, organizations, projects, platform users
                                      and org users (+new, updated, −archived in the period); every cluster's usage;
                                      the IP subnets; backups of the portal and the platforms; the portal's releases,
                                      update stream, auto-update and switches
    support     support engineers     totals of the shared clusters, organizations and projects; the shared clusters'
                                      usage; the IP subnets; projects per cluster with their allocations; projects near
                                      their review date. Each engineer gets **their own** organizations (owner: "SE -
                                      only his/her own"): the ones they serve, and the internal ones without an engineer
    sales       managers and KAMs     totals of organizations, projects and org users; the shared clusters' usage;
                                      projects near their review date - each person their own organizations
    resources   resource managers     totals of the shared platforms and clusters, organizations, projects and org
                                      users; the shared clusters' usage; the IP subnets; backups; the portal

A report has a name and roles (both editable), a day (daily, or a weekday), a time, on/off and "redirect", and keeps
the time it last went and a sent history (ReportSent). A person with an unrestricted role (super admin, resource
manager) gets the whole picture in any report. A redirected report goes only to the address in "Send reports to"
("for <name>" in the subject); without that address it is not sent.

"Near the review date": Tenant.review_alert (the date has come, or within a week for a POC and 3 months for a
client - u01). The timer "reports" (every 5 minutes, the active node only) sends what is due (run_due); Send now sends
one at once.

State in PortalSettings.reports: {redirect_to, list: [{id, name, kind, roles, day, time, on, redirect}], sent: {id:
ISO time}}. The six reports' settings ("items") are left in the JSON, unused; on the first read the four take their
redirect from them.
"""

import ipaddress
import re
from datetime import datetime, timedelta

from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l

KINDS = [
    ("sa", _l("Super admin")),
    ("support", _l("Support engineer")),
    ("sales", _l("Manager / KAM")),
    ("resources", _l("Resource manager")),
]
KIND_KEYS = [k for k, _n in KINDS]
# role key, the User flag, its name
ROLES = [
    ("super_admin", "is_super_admin", _l("Super admin")),
    ("resource_manager", "is_resource_manager", _l("Resource manager")),
    ("support", "is_support", _l("Support engineer")),
    ("sales", "is_sales", _l("Sales manager")),
    ("kam", "is_kam", _l("KAM")),
]
ROLE_FLAG = {k: f for k, f, _n in ROLES}
WEEKDAYS = [
    (0, _l("Monday")),
    (1, _l("Tuesday")),
    (2, _l("Wednesday")),
    (3, _l("Thursday")),
    (4, _l("Friday")),
    (5, _l("Saturday")),
    (6, _l("Sunday")),
]
DAYS = [("daily", _l("Daily"))] + [(str(n), lab) for n, lab in WEEKDAYS]
DEFAULTS = [
    {"id": "sa", "name": "Super admin weekly", "kind": "sa", "roles": ["super_admin"], "time": "08:00"},
    {"id": "support", "name": "Support weekly", "kind": "support", "roles": ["support"], "time": "08:30"},
    {"id": "sales", "name": "Sales weekly", "kind": "sales", "roles": ["sales", "kam"], "time": "09:00"},
    {
        "id": "resources",
        "name": "Resources weekly",
        "kind": "resources",
        "roles": ["resource_manager"],
        "time": "08:00",
    },
]
FIRST_GRACE = timedelta(minutes=30)  # a report never sent goes only this soon after its time
HISTORY_DAYS = 90


def _settings():
    from tenants.models import PortalSettings

    return PortalSettings.get()


def config(s=None):
    """{redirect_to, list: [report], sent: {id: iso}} with the defaults."""
    raw = (s or _settings()).reports or {}
    reps = raw.get("list")
    if not reps:  # the first read after the six reports: the four, redirected if the old ones were
        old = raw.get("items") or {}
        redirect = any((v or {}).get("redirect") for v in old.values())
        reps = [{**d, "day": "0", "on": True, "redirect": redirect} for d in DEFAULTS]
    out = []
    for r in reps:
        r = {"day": "0", "time": "09:00", "on": True, "redirect": False, "roles": [], **r}
        if r.get("kind") in KIND_KEYS and r.get("id"):
            out.append(r)
    return {"redirect_to": raw.get("redirect_to", ""), "list": out, "sent": dict(raw.get("sent") or {})}


def by_id(rid, conf=None):
    return next((r for r in (conf or config())["list"] if r["id"] == rid), None)


def _save(**changes):
    from tenants.models import PortalSettings

    s = _settings()
    raw = dict(s.reports or {})
    raw.update(changes)
    PortalSettings.objects.filter(pk=s.pk).update(reports=raw)


def mark_sent(rid, at=None):
    sent = dict(config()["sent"])
    sent[rid] = (at or timezone.now()).isoformat()
    _save(sent=sent)


def rows():
    """For the settings page: each report with its last time and its recent history."""
    from .models import ReportSent

    c = config()
    kind_names = dict(KINDS)
    out = []
    for r in c["list"]:
        last = c["sent"].get(r["id"])
        out.append(
            {
                **r,
                "kind_name": kind_names[r["kind"]],
                "last": timezone.localtime(datetime.fromisoformat(last)) if last else None,
                "history": list(ReportSent.objects.filter(report=r["id"]).order_by("-at")[:20]),
            }
        )
    return out


def clean(post):
    """The settings form: (config without "sent", errors)."""
    errors = {}
    to = (post.get("redirect_to") or "").strip()
    if to and not re.match(r"^[^@\s]+@[^@\s]+\.[a-z]{2,63}$", to, re.I):
        errors["redirect_to"] = _("Not an e-mail address.")
    out = []
    for r in config()["list"]:
        rid = r["id"]
        t = (post.get(f"time_{rid}") or r["time"]).strip()
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", t):
            errors[f"time_{rid}"] = _("HH:MM")
        day = post.get(f"day_{rid}") or r["day"]
        if day not in dict(DAYS):
            day = r["day"]
        name = (post.get(f"name_{rid}") or "").strip()[:80] or r["name"]
        roles = [x for x in post.getlist(f"roles_{rid}") if x in ROLE_FLAG]
        if not roles:
            errors[f"roles_{rid}"] = _("Choose at least one role.")
        out.append(
            {
                "id": rid,
                "kind": r["kind"],
                "name": name,
                "roles": roles or r["roles"],
                "day": day,
                "time": t,
                "on": post.get(f"on_{rid}") == "on",
                "redirect": post.get(f"redirect_{rid}") == "on",
            }
        )
    return {"redirect_to": to, "list": out}, errors


def save(conf):
    _save(redirect_to=conf["redirect_to"], list=conf["list"])


# --- when ----------------------------------------------------------------------------------------------------------


def last_slot(rep, now=None):
    """The latest time this report was due, at or before now (aware)."""
    now = timezone.localtime(now or timezone.now())
    hh, mm = (int(x) for x in rep["time"].split(":"))
    slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    weekly = rep["day"] != "daily"
    if weekly:
        slot -= timedelta(days=(slot.weekday() - int(rep["day"])) % 7)
    if slot > now:
        slot -= timedelta(days=7 if weekly else 1)
    return slot


def period_of(rep):
    return timedelta(days=1 if rep["day"] == "daily" else 7)


def due(rid, conf=None, now=None):
    conf = conf or config()
    rep = by_id(rid, conf)
    if not rep or not rep["on"]:
        return False
    slot = last_slot(rep, now)
    sent = conf["sent"].get(rid)
    if sent and datetime.fromisoformat(sent) >= slot:
        return False
    late = (now or timezone.now()) - slot
    if not sent:  # never sent: only at its time (not a slot passed before it was set up), u02
        return late < FIRST_GRACE
    return late < period_of(rep)  # a slot missed while the portal was down: within the period


def run_due(log=print, now=None):
    """The timer: send every report that is due. Returns a short summary."""
    conf = config()
    done = []
    for rep in conf["list"]:
        if due(rep["id"], conf, now):
            try:
                _n, detail = send(rep["id"], conf, now=now)
                done.append(f"{rep['id']}: {detail}")
            except Exception as exc:  # noqa: BLE001 - the next round tries again
                log(f"report {rep['id']}: {exc}")
                done.append(f"{rep['id']}: {exc}"[:120])
    return "; ".join(done) or "nothing due"


# --- to whom -------------------------------------------------------------------------------------------------------


def _staff(**flags):
    # owner 2026-11-03: reports reach every role holder with an e-mail, including users who have not been invited yet
    # (login not enabled); only blocked (is_active False) and archived users are left out.
    from .models import Kind, User

    return list(
        User.objects.filter(kind=Kind.STAFF, is_active=True, archived_at__isnull=True, **flags)
        .exclude(email="")
        .order_by("email")
    )


def recipients(rep):
    """The staff holding any of the report's roles."""
    seen = {}
    for role in rep["roles"]:
        if role in ROLE_FLAG:
            for u in _staff(**{ROLE_FLAG[role]: True}):
                seen[u.pk] = u
    return sorted(seen.values(), key=lambda u: u.email)


def own_orgs(user, kind):
    """The organizations a person's report covers: None for all (the super admin and the resource manager, and the
    reports that are about the whole portal), else a queryset."""
    from django.db.models import Q

    from tenants.models import Organization, OrgKind

    if kind in ("sa", "resources") or user is None or user.is_super_admin or user.is_resource_manager:
        return None
    qs = Organization.objects.filter(archived_at__isnull=True)
    if kind == "support":  # theirs, and the internal ones without an engineer (any engineer serves them)
        return qs.filter(Q(support_engineer=user) | Q(kind=OrgKind.CORP, support_engineer__isnull=True))
    return qs.filter(Q(sales_rep=user) | Q(kam=user))


def send(rid, conf=None, now=None, by=None, how="schedule"):
    """Send one report now (to its recipients, or to the redirect address). Returns (messages sent, detail)."""
    from .models import ReportSent
    from .utils import notify

    conf = conf or config()
    rep = by_id(rid, conf)
    if rep is None:
        return 0, "no such report"
    now = now or timezone.now()
    since = now - period_of(rep)
    redirect_to = conf["redirect_to"] if rep["redirect"] else ""

    def record(sent, detail, ok=True, to=""):
        mark_sent(rid, now)
        ReportSent.objects.create(
            report=rid, name=rep["name"], how=how, by=by, to=to[:1000], sent=sent, ok=ok, detail=detail[:500]
        )
        ReportSent.objects.filter(at__lt=timezone.now() - timedelta(days=HISTORY_DAYS)).delete()

    if rep["redirect"] and not redirect_to:
        record(0, "redirected, but no address in Send reports to: not sent", ok=False)
        return 0, "redirected, but no address in Send reports to: not sent"
    people = recipients(rep)
    whole = [u for u in people if own_orgs(u, rep["kind"]) is None]
    groups = ([(None, whole)] if whole else []) + [(u, [u]) for u in people if u not in whole]
    sent, delivered, skipped = 0, [], 0
    day = timezone.localtime(now).strftime("%d.%m.%Y")
    for user, to_users in groups:
        orgs = own_orgs(user, rep["kind"]) if user else None
        if orgs is not None and not orgs.exists():
            skipped += 1  # nothing of theirs
            continue
        ctx = context(rep["kind"], since, now, orgs)
        whom = (user.full_name or user.email) if user else ", ".join(u.email for u in to_users)
        to = [redirect_to] if redirect_to else to_users
        subject = f"{rep['name']} · {day}"
        if redirect_to:
            subject += " · " + _("for %(w)s") % {"w": whom}
        n = notify(
            to,
            "report",
            {
                **ctx,
                "rep": rep,
                "label": rep["name"],
                "person": user,
                "redirected": bool(redirect_to),
                "for_whom": whom,
            },
            subject,
        )
        sent += n
        if n:
            delivered.append(redirect_to or ", ".join(u.email for u in to_users))
    detail = f"{sent} sent" + (f" to {redirect_to} (redirected)" if redirect_to else "")
    detail += f", {len(people)} recipient(s)" + (f", {skipped} with nothing of theirs" if skipped else "")
    record(sent, detail, to="; ".join(sorted(set(delivered))))
    return sent, detail


# --- what is in them -----------------------------------------------------------------------------------------------

# log entries that change nothing (views, checks, steps): not "updated"
QUIET = {
    "login",
    "config_checked",
    "tenant_gateways_checked",
    "secret_revealed",
    "tenant_password_viewed",
    "tenant_password_copied",
    "igw_config_viewed",
    "igw_config_read",
    "igw_config_downloaded",
    "igw_password_history_shown",
    "deploy_step",
    "tenant_notice",
    "timer_refreshed",
    "tenant_object_deleted",
}


def _logged(ttype, since, ids=None):
    """{target pk} with a log entry (that changes something) in the period."""
    from .models import AuditLog

    out = set()
    for t in (
        AuditLog.objects.filter(target_type=ttype, created_at__gte=since)
        .exclude(action__in=QUIET)
        .values_list("target_id", flat=True)
        .distinct()
    ):
        if (t or "").isdigit() and (ids is None or int(t) in ids):
            out.add(int(t))
    return out


def _first_logged(ttype, since):
    """{target pk} whose first log entry is in the period: new objects that have no creation time of their own."""
    from django.db.models import Min

    from .models import AuditLog

    return {
        int(r["target_id"])
        for r in AuditLog.objects.filter(target_type=ttype)
        .values("target_id")
        .annotate(first=Min("created_at"))
        .filter(first__gte=since)
        if (r["target_id"] or "").isdigit()
    }


def _count(actions, since):
    from .models import AuditLog

    return AuditLog.objects.filter(action__in=actions, created_at__gte=since).count()


def tile(key, label, total, new, updated, gone):
    return {"key": key, "label": label, "total": total, "new": new, "updated": updated, "gone": gone}


def tiles(which, since, orgs=None):
    """The totals of the period: [{label, total, new, updated, gone}] for the objects named in `which`."""
    from accounts.models import Kind, User
    from inventory.models import Cluster, Host, Platform
    from tenants.models import Approval, Lifecycle, Organization, Tenant

    org_ids = set(orgs.values_list("pk", flat=True)) if orgs is not None else None
    out = []
    for key in which:
        if key in ("platforms", "shared_platforms"):
            qs = Platform.objects.all()
            if key == "shared_platforms":
                qs = qs.filter(kind="shared")
            ids = set(qs.values_list("pk", flat=True))
            new = _first_logged("platform", since) & ids
            out.append(
                tile(
                    key,
                    _("Shared platforms") if key == "shared_platforms" else _("Platforms"),
                    len(ids),
                    len(new),
                    len(_logged("platform", since, ids) - new),
                    _count(["platform_deleted"], since),
                )
            )
        elif key in ("clusters", "shared_clusters"):
            qs = Cluster.objects.exclude(state="retired")
            if key == "shared_clusters":
                qs = qs.filter(kind="shared")
            ids = set(qs.values_list("pk", flat=True))
            new = _first_logged("cluster", since) & ids
            out.append(
                tile(
                    key,
                    _("Shared clusters") if key == "shared_clusters" else _("Clusters"),
                    len(ids),
                    len(new),
                    len(_logged("cluster", since, ids) - new),
                    _count(["cluster_deleted"], since),
                )
            )
        elif key == "hosts":
            ids = set(Host.objects.exclude(state="retired").values_list("pk", flat=True))
            new = _first_logged("host", since) & ids
            out.append(
                tile(
                    key,
                    _("Hosts"),
                    len(ids),
                    len(new),
                    len(_logged("host", since, ids) - new),
                    _count(["host_retired", "host_deleted"], since),
                )
            )
        elif key == "organizations":
            qs = Organization.objects.all() if org_ids is None else Organization.objects.filter(pk__in=org_ids)
            live = qs.filter(archived_at__isnull=True)
            new = set(live.filter(created_at__gte=since).values_list("pk", flat=True))
            ids = set(live.values_list("pk", flat=True))
            out.append(
                tile(
                    key,
                    _("Organizations"),
                    len(ids),
                    len(new),
                    len(_logged("organization", since, ids) - new),
                    qs.filter(archived_at__gte=since).count(),
                )
            )
        elif key == "projects":
            qs = Tenant.objects.all() if org_ids is None else Tenant.objects.filter(organization_id__in=org_ids)
            all_ids = set(qs.values_list("pk", flat=True))
            live = set(
                qs.filter(approval=Approval.APPROVED).exclude(status=Lifecycle.ARCHIVED).values_list("pk", flat=True)
            )
            from .models import AuditLog

            def with_action(actions, all_ids=all_ids):
                return {
                    int(t)
                    for t in AuditLog.objects.filter(
                        action__in=actions, target_type="tenant", created_at__gte=since
                    ).values_list("target_id", flat=True)
                    if (t or "").isdigit() and int(t) in all_ids
                }

            new = with_action(["tenant_deployed"])
            gone = with_action(["tenant_deleted"]) | {
                int(t)
                for t in AuditLog.objects.filter(
                    action="tenant_status_changed", target_type="tenant", created_at__gte=since, details__new="archived"
                ).values_list("target_id", flat=True)
                if (t or "").isdigit() and int(t) in all_ids
            }
            out.append(
                tile(
                    key,
                    _("Projects"),
                    len(live),
                    len(new),
                    len(_logged("tenant", since, all_ids) - new - gone),
                    len(gone),
                )
            )
        elif key in ("platform_users", "org_users"):
            if key == "platform_users":
                qs = User.objects.filter(kind=Kind.STAFF)
            else:
                qs = User.objects.filter(kind=Kind.CUSTOMER)
                if org_ids is not None:
                    qs = qs.filter(organization_id__in=org_ids)
            live = qs.filter(archived_at__isnull=True, is_active=True)
            ids = set(live.values_list("pk", flat=True))
            new = set(live.filter(date_joined__gte=since).values_list("pk", flat=True))
            out.append(
                tile(
                    key,
                    _("Platform users") if key == "platform_users" else _("Org users"),
                    len(ids),
                    len(new),
                    len(_logged("user", since, ids) - new),
                    qs.filter(archived_at__gte=since).count(),
                )
            )
    return out


def usage(all_clusters=False):
    """The clusters' allocation against their capacity (shared ones unless all_clusters)."""
    from inventory.capacity import clusters

    out = []
    for r in clusters(kinds=None if all_clusters else ["shared"]):
        lg = r["logical"]
        out.append(
            {
                "site": r["site"],
                "cluster": r["cluster"].name,
                "kind": r["cluster"].kind,
                "projects": r["alloc"].get("tenants", 0),
                "vcpu": lg["used_vcpu"],
                "vcpu_cap": lg["vcpu"],
                "vcpu_pct": lg["cpu_pct"],
                "ram": lg["used_ram"],
                "ram_cap": lg["ram_gb"],
                "ram_pct": lg["ram_pct"],
                "gpu": lg["used_gpus"],
                "gpu_cap": lg["gpus"],
                "gpu_pct": round(lg["used_gpus"] * 100 / lg["gpus"]) if lg.get("gpus") else None,
                "disk": r["alloc"].get("disk_gb", 0),
                "top": max(x or 0 for x in (lg["cpu_pct"], lg["ram_pct"])),
            }
        )
    return out


def subnets():
    """Every IP subnet with the addresses delegated from it (the active prefixes inside it)."""
    from inventory.models import IPSubnet
    from tenants.models import PrefixAssignment

    active = []
    for p in PrefixAssignment.active().values("site", "cidr"):
        try:
            active.append((p["site"], ipaddress.ip_network(p["cidr"], strict=False)))
        except ValueError:
            continue
    out = []
    for s in IPSubnet.objects.select_related("site").order_by("site__code", "subnet"):
        try:
            net = ipaddress.ip_network(f"{s.subnet}/{s.prefix}", strict=False)
        except ValueError:
            continue
        used = sum(n.num_addresses for site, n in active if n.version == net.version and n.subnet_of(net))
        total = net.num_addresses
        out.append(
            {
                "site": s.site.code,
                "subnet": str(net),
                "purpose": s.get_purpose_display(),
                "used": used,
                "total": total,
                "pct": round(used * 100 / total) if total else 0,
            }
        )
    return out


_KIB = re.compile(r"\((\d+) KiB")


def backups(since):
    """Portal and platform backups in the period: runs, failures (with their detail), size."""
    from .models import AuditLog

    out = {}
    for key, action, label in (
        ("portal", "portal_backup", _("Portal")),
        ("platform", "platform_backup", _("Platforms")),
    ):
        runs = list(AuditLog.objects.filter(action=action, created_at__gte=since).order_by("created_at"))
        ok = [r for r in runs if (r.details or {}).get("ok")]
        failed = [
            {"at": timezone.localtime(r.created_at), "result": (r.details or {}).get("result", "")}
            for r in runs
            if not (r.details or {}).get("ok")
        ]
        sizes = [sum(int(x) for x in _KIB.findall((r.details or {}).get("result", ""))) for r in ok]
        out[key] = {
            "label": label,
            "runs": len(runs),
            "ok": len(ok),
            "failed": failed,
            "last_kib": sizes[-1] if sizes else 0,
            "total_kib": sum(sizes),
            "last_bytes": (sizes[-1] if sizes else 0) * 1024,
            "total_bytes": sum(sizes) * 1024,
        }
    return out


_INSTALL = re.compile(r"install (v\d+\.\d+\.\d+)")


def portal(since):
    """The portal's releases in the period, the current one, the update stream, auto-update and the switches."""
    from . import ha_agent
    from .models import AuditLog

    s = _settings()
    releases, failures = [], []
    for r in AuditLog.objects.filter(action__startswith="ha_", created_at__gte=since).order_by("created_at"):
        d = r.details or {}
        if r.action.endswith("_requested") or "state" not in d:
            continue
        if d.get("state") == "failed":
            failures.append(
                {
                    "at": timezone.localtime(r.created_at),
                    "operation": d.get("operation", r.action),
                    "error": d.get("error", ""),
                }
            )
            continue
        if r.action in ("ha_update_active", "ha_update_single", "ha_upgrade_enterprise"):
            m = _INSTALL.search(d.get("steps", ""))
            if m and (not releases or releases[-1]["version"] != m.group(1)):
                releases.append({"version": m.group(1), "at": timezone.localtime(r.created_at), "by": d.get("by", "")})
    try:
        current = ha_agent.version()
    except Exception:  # noqa: BLE001
        current = ""
    st = s.auto_update_state or {}
    return {
        "current": current,
        "releases": releases,
        "failures": failures,
        "stream": s.get_update_stream_display(),
        "auto_update": s.auto_update,
        "auto_result": st.get("result", ""),
        "failed_tag": st.get("failed_tag", ""),
        "switches": AuditLog.objects.filter(
            action__in=["ha_switchover", "ha_new_to_active"], created_at__gte=since, details__state="done"
        ).count(),
    }


def live_tenants(orgs=None):
    from tenants.models import Approval, Lifecycle, Tenant

    qs = Tenant.objects.filter(approval=Approval.APPROVED).exclude(status=Lifecycle.ARCHIVED)
    if orgs is not None:
        qs = qs.filter(organization__in=orgs)
    return qs.select_related("organization")


def per_cluster(orgs=None):
    """Projects per cluster with what each is allocated there: [{site, cluster, projects: [...], totals}]."""
    from tenants.models import QuotaRecord

    groups = {}
    for t in live_tenants(orgs):
        for q in QuotaRecord.current_for(t):
            g = groups.setdefault((q.site, q.cluster), {"site": q.site, "cluster": q.cluster, "projects": []})
            g["projects"].append(
                {
                    "name": t.name or t.title,
                    "org": t.organization.name if t.organization else "",
                    "cpu": q.cpu,
                    "ram": q.ram_gb,
                    "gpu": q.gpu,
                    "disk": q.disk_gb,
                }
            )
    out = []
    for key in sorted(groups):
        g = groups[key]
        g["projects"].sort(key=lambda p: (p["org"], p["name"]))
        g["totals"] = {f: sum(p[f] for p in g["projects"]) for f in ("cpu", "ram", "gpu", "disk")}
        out.append(g)
    return out


def near_review(orgs=None):
    """Projects whose review date has come or is close (Tenant.review_alert), soonest first."""
    out = []
    for t in live_tenants(orgs).exclude(date_review__isnull=True).order_by("date_review", "name"):
        if t.review_alert:
            o = t.organization
            out.append(
                {
                    "name": t.name or t.title,
                    "org": o.name if o else "",
                    "stage": t.get_stage_display(),
                    "review": t.date_review,
                    "alert": t.review_alert,
                    "manager": (o.sales_rep.full_name or o.sales_rep.email) if o and o.sales_rep else "",
                    "kam": (o.kam.full_name or o.kam.email) if o and o.kam else "",
                }
            )
    return out


SECTIONS = {
    "sa": {
        "tiles": ["platforms", "clusters", "hosts", "organizations", "projects", "platform_users", "org_users"],
        "all_clusters": True,
        "subnets": True,
        "backups": True,
        "portal": True,
    },
    "support": {
        "tiles": ["shared_clusters", "organizations", "projects"],
        "subnets": True,
        "per_cluster": True,
        "near": True,
    },
    "sales": {"tiles": ["organizations", "projects", "org_users"], "near": True},
    "resources": {
        "tiles": ["shared_platforms", "shared_clusters", "organizations", "projects", "org_users"],
        "subnets": True,
        "backups": True,
        "portal": True,
    },
}


def context(kind, since, now, orgs=None):
    """Everything a report of this kind shows; `orgs` limits it to a person's organizations (None: all)."""
    from django.urls import reverse

    from .utils import site_link

    sec = SECTIONS[kind]
    ctx = {
        "kind": kind,
        "since": timezone.localtime(since),
        "now": timezone.localtime(now),
        "link": site_link(reverse("tenant_list")),
        "scoped": orgs is not None,
        "tiles": tiles(sec["tiles"], since, orgs),
        "usage": usage(all_clusters=sec.get("all_clusters", False)),
        "all_clusters": sec.get("all_clusters", False),
    }
    if sec.get("subnets"):
        ctx["subnets"] = subnets()
    if sec.get("backups"):
        ctx["backups"] = backups(since)
    if sec.get("portal"):
        ctx["portal"] = portal(since)
    if sec.get("per_cluster"):
        ctx["per_cluster"] = per_cluster(orgs)
    if sec.get("near"):
        ctx["near"] = near_review(orgs)
    return ctx
