"""corrections-18: the portal's timers (Settings → Data → Timers).

    portal     every 10 minutes   the portal's components: the database, both nodes and the replication, the mail
                                  relay, the last backup
    platform   every 30 minutes   the platforms: their endpoints and service images, and the tenants (groups, quotas,
                                  users, VMs, igw VMs) read from them; every platform's datastores, hosts and VMs (the
                                  dashboard) and the platforms' own VMs (gateways, core services)
    ipmi       every 240 minutes  the hosts' health from their IPMI
    baseline   every 1440 minutes the platforms' front ends and hosts against Profiles → Baseline (inventory/baseline.py)
    hostmedia  every 1440 minutes the hosts' installer ISO kept on the newest point release of the base OS (accounts/hostmedia.py)
    vyos       every 480 minutes  every VyOS router of the provider compared with its template: route reflectors, the
                                  sites' egw and igw, the projects' egw, cgw and L2 gateways (never a project's igw)
    registrations  daily from 09:00   delete what was never approved (regcheck)
    reports    every 5 minutes    the e-mail reports that are due (Settings → Reports, accounts/reports.py)

The systemd timer of the portal (claugine-health, every 5 minutes) runs those that are due (`manage.py
check_platforms`); a timer's Refresh button runs it at once in the background (locked until it is over). Each timer
keeps its period, last run, state (ok, in progress, error) and a short result in PortalSettings.timers.
"""

from datetime import timedelta

from django.utils import timezone
from django.utils.translation import gettext_lazy as _l

TIMERS = [  # key, label, default minutes, what it does
    ("portal", _l("Portal"), 10, _l("Portal components: database, nodes and replication, mail relay, last backup")),
    (
        "platform",
        _l("Platforms"),
        30,
        _l("Platform endpoints and service images; projects read from the platforms"),
    ),  # and the dashboard's numbers
    ("ipmi", _l("IPMI"), 240, _l("Hosts' health from their IPMI")),
    # owner 2026-11-17: "vyos timer for all vyos routers - rrs, egw, l2gw, cgw, cloud egw/igw" (no project igw)
    (
        "vyos",
        _l("VyOS"),
        480,
        _l(
            "VyOS routers compared with their templates: route reflectors, site egw/igw, project egw, cgw and L2 gateways"
        ),
    ),
    # owner 2026-11-18 (T25): the platforms against the baseline of Profiles → Baseline (inventory/baseline.py)
    (
        "baseline",
        _l("Platform baseline"),
        1440,
        _l("Front ends and hosts of every platform against the base OS and OpenNebula version (Profiles → OS)"),
    ),
    # owner 2026-11-18 (T25): a newer point release of the base OS → the hosts' installer ISO (accounts/hostmedia.py)
    (
        "hostmedia",
        _l("Installation media"),
        1440,
        _l("The hosts' installer ISO: a newer point release of the base OS downloaded, checked and rebuilt"),
    ),
    # owner 2026-10-29 20:30: daily, not before 09:00
    (
        "registrations",
        _l("Registrations"),
        1440,
        _l(
            "Deletes registration requests not confirmed in time and declined join requests (the log keeps them; "
            "not before 09:00)"
        ),
    ),
    ("reports", _l("Reports"), 5, _l("Sends the e-mail reports that are due (Settings → Reports)")),  # u01
    # owner 2026-11-21 09:21 (T33): "for all deployed hosts ... report status of logging compliance"
    ("logging", _l("Hosts' logging"), 1440, _l("Deployed hosts' logging compared with Infrastructure → Services")),
]
NOT_BEFORE_HOUR = {"registrations": 9}
KEYS = [k for k, *_r in TIMERS]
STALE = timedelta(hours=3)  # a run "in progress" longer than this is taken as dead


def _settings():
    from tenants.models import PortalSettings

    return PortalSettings.get()


def state(key):
    default = next(m for k, _l2, m, _d in TIMERS if k == key)
    st = dict((_settings().timers or {}).get(key) or {})
    st.setdefault("minutes", default)
    return st


def _save(key, **values):
    """commit-172: read and written under the settings row's lock, so no update of a timer's state is lost."""
    from django.db import transaction

    from tenants.models import PortalSettings

    pk = PortalSettings.get().pk
    with transaction.atomic():
        s = PortalSettings.objects.select_for_update().get(pk=pk)
        t = dict(s.timers or {})
        cur = dict(t.get(key) or {})
        cur.update(values)
        t[key] = cur
        PortalSettings.objects.filter(pk=pk).update(timers=t)


def rows():
    """For the settings page: [{key, label, what, minutes, last_at, state, detail}]."""
    from django.utils.dateparse import parse_datetime

    out = []
    for key, label, _m, what in TIMERS:
        st = state(key)
        running = is_running(st)
        out.append(
            {
                "key": key,
                "label": label,
                "what": what,
                "minutes": st["minutes"],
                "last_at": parse_datetime(st["last_at"]) if st.get("last_at") else None,
                "state": "running" if running else (st.get("state") or ""),
                "detail": st.get("detail", ""),
            }
        )
    return out


def is_running(st):
    from django.utils.dateparse import parse_datetime

    if st.get("state") != "running":
        return False
    at = parse_datetime(st.get("started_at") or "")
    return at is not None and timezone.now() - at < STALE


def set_minutes(key, minutes):
    _save(key, minutes=max(1, min(int(minutes), 10080)))


def due(key, now=None):
    from django.utils.dateparse import parse_datetime

    st = state(key)
    if is_running(st):
        return False
    if st.get("again"):  # commit-172: asked while it ran - due at once
        return True
    last = parse_datetime(st.get("last_at") or "")
    now = now or timezone.now()
    hour = NOT_BEFORE_HOUR.get(key)
    if hour is not None and timezone.localtime(now).hour < hour:
        return False
    if hour is not None and last is not None and st["minutes"] >= 1440:  # once a day, from that hour
        return timezone.localtime(last).date() < timezone.localtime(now).date()
    return last is None or now - last >= timedelta(minutes=st["minutes"]) - timedelta(seconds=30)


def run(key):
    """Run one timer now (in this process). Returns (ok, detail). commit-172: asked again while it ran (start() sets
    "again"), it runs once more when it ends - so what changed meanwhile is covered."""
    for _round in range(3):
        _save(key, state="running", started_at=timezone.now().isoformat(), again=False)
        try:
            ok, detail = FUNCS[key]()
        except Exception as exc:  # noqa: BLE001 - kept as the timer's result
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        _save(key, state="ok" if ok else "error", detail=str(detail)[:500], last_at=timezone.now().isoformat())
        if not state(key).get("again"):
            break
    return ok, detail


def run_due():
    """Every timer that is due, one after another. Returns {key: (ok, detail)}."""
    out = {}
    for key in KEYS:
        if due(key):
            out[key] = run(key)
    return out


def start(key, user=None):
    """The Refresh button: run the timer in the background (a task); nothing when it is running already."""
    from . import tasks

    if is_running(state(key)):
        _save(key, again=True)  # commit-172: it runs once more when the run going on ends
        return None
    _save(key, state="running", started_at=timezone.now().isoformat())
    label = next(str(lbl) for k, lbl, _m, _d in TIMERS if k == key)
    return tasks.start(f"timer {key}", f"Timer: {label}", lambda task: {"ok": run(key)[0]}, user=user)


# --- the timers' work ------------------------------------------------------------------------------------------


def _portal():
    from django.db import connection

    from tenants.models import PortalSettings

    from . import appliance, ha_agent, thisnode

    problems = []
    with connection.cursor() as c:
        c.execute("SELECT 1")
    s = PortalSettings.get()
    if s.mail_mode in ("internal", "direct") and not appliance.relay_reachable():
        problems.append("mail relay not reachable")
    if thisnode.env().get("ADDR"):
        try:
            for label, _addr, st in ha_agent.nodes_status():
                if "error" in st:
                    problems.append(f"{label}: {st['error']}"[:120])
                    continue
                rep = (st.get("db") or {}).get("replica") or {}
                if rep.get("source") and not (rep.get("io") == "Yes" and rep.get("sql") == "Yes"):
                    problems.append(f"{label}: replication IO {rep.get('io')} / SQL {rep.get('sql')}")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"nodes: {exc}"[:120])
    if s.has_secret("backup_password"):
        limit = timedelta(hours=49)  # u07: once a day — flag only when about two days are missed
        if s.backup_last_at is None or timezone.now() - s.backup_last_at > limit:
            problems.append("no recent backup")
        elif not (s.backup_last_result or "").startswith("ok"):
            problems.append("the last backup failed")
    return not problems, "; ".join(problems) or "database, nodes, mail relay and backups ok"


def _platform():
    from adoption.collector import collect_all
    from inventory.health import check_due

    health = check_due(force=True)
    bad = [k for k, ok in health.items() if not ok]
    res = collect_all()
    errors = [k for k, v in res.items() if str(v).startswith("error")]
    # owner 2026-10-29 21:10: the dashboard's numbers come from here — every platform's datastores, hosts and VMs
    # (management and portal platforms too) and the platforms' own VMs (gateways, core services)
    from adoption.collector import collect_stats_only

    errors += [k for k, v in collect_stats_only().items() if v.startswith("error")]
    from inventory.core_services import refresh_vms
    from inventory.models import Site

    for site in Site.objects.all():
        errors += [k for k, _e in refresh_vms(site.code)]
    if res:
        try:
            from tenants.igwdetect import detect

            detect()
        except Exception as exc:  # noqa: BLE001 - the read itself is done
            errors.append(f"igw: {exc}"[:120])
    detail = (
        f"{len(health)} platforms checked"
        + (f", problems: {', '.join(bad)}" if bad else "")
        + f"; tenants read from {len(res)}"
        + (f", errors: {', '.join(errors)}" if errors else "")
    )
    return not bad and not errors, detail


def _ipmi():
    from inventory import hosthealth

    hosts = hosthealth.due(force=True)
    res = hosthealth.check(hosts) if hosts else {}
    bad = [n for n, ok in res.items() if not ok]
    return not bad, f"{len(res)} hosts" + (f", problems: {', '.join(bad[:10])}" if bad else " ok")


def _vyos():
    """owner 2026-11-17: every VyOS router of the provider — the sites' route reflectors and egw/igw, the projects' egw,
    cgw and L2 gateways; never a project's igw (the project's own after its deployment)."""
    from accounts import maintenance
    from tenants.configcheck import check_core, check_targets, core_nodes
    from tenants.configcheck import run as check_run
    from tenants.models import ConfigCheck, Tenant

    n = 0
    drift, down = [], []

    def count(name, r):
        nonlocal n
        n += 1
        if r.get("status") == "drift":
            drift.append(name)
        elif r.get("status") in ("unreachable", "error"):
            down.append(name)

    for node in core_nodes():
        count(node.name, check_core(node))
    for t in Tenant.objects.exclude(status__in=["archived", "saved", "deleting"]).exclude(one_group_id__isnull=True):
        if not check_targets(t):
            continue
        with maintenance.admitting():  # core-046: admitted only while no maintenance runs (accounts.maintenance)
            chk = ConfigCheck.objects.create(tenant=t)
        chk = check_run(chk)
        for key, r in (chk.results or {}).items():
            count(f"{t.name} {key}", r)

    def names(rows):
        return f" ({', '.join(rows[:8])}{', …' if len(rows) > 8 else ''})" if rows else ""

    return (
        not drift and not down,
        f"{n} routers: {len(drift)} differ from the template{names(drift)}, {len(down)} not reachable{names(down)}",
    )


def _registrations():
    from .regcheck import daily

    return daily()


def _reports():
    from . import reports

    return True, reports.run_due()


def _baseline():
    """commit-172: when it ends, the values are read again - changed meanwhile, the platforms are checked again."""
    from inventory import baseline

    for _round in range(3):
        base = baseline.values()
        ok, detail = baseline.check_all(base)
        if baseline.values() == base:
            break
    return ok, detail


def _logging():
    from inventory import hostlogging

    return hostlogging.check_all()


def _hostmedia():
    from . import hostmedia

    return hostmedia.check()


FUNCS = {
    "portal": _portal,
    "hostmedia": _hostmedia,
    "baseline": _baseline,
    "platform": _platform,
    "ipmi": _ipmi,
    "vyos": _vyos,
    "registrations": _registrations,
    "reports": _reports,
    "logging": _logging,
}
