"""A deployed host's logging against its profile's logged services (Profiles → Logging, owner 2026-11-24; T33 from
2026-11-21 09:21 - 09:35): the check (read
only: the services found, the files they give compared with the host's own, rsyslog, the services' units) and Apply
logging (the deployment's install: staged, checked, the previous set kept and put back on failure). Hosts deployed by
the portal get their logging in the deployment (hostdeploy.s_logging); every deployed host is checked once a day
(accounts/timers.py "logging") and with Check logging. Owner 11:58 "service VMs now": the base-OS VMs of the core
services (CoreServiceNode.base_os) the same way - the functions take a host or such a VM ("a machine").
Owner 10:49: a service's config changes (settings in its own files) are made with its logging and checked with it.

The result is kept on the host (Host.logging): {"status": compliant|differs|not checked, "at", "services", "files":
[{"path", "state": same|differs|added|moved aside|removed|config made|config change not made|not on the machine,
"diff"}], "rsyslog", "units_down", "error"}; "at" is when
the host was read. commit-266: when logging was last applied is its own field (Host.logging_applied_at), never written
by a check, and a check's result is kept only when no newer one was stored while it ran.
"""

import base64
import difflib
import re
import shlex

from django.utils import timezone

from accounts.models import audit

from . import logconf

STATES = ("same", "differs", "added", "moved aside", "removed")
LEFTOVER = "/var/log/ufnvidiaw.log"  # an earlier logging script's typo, written by nothing (read 2026-11-21)
DIFF_MAX = 12000


class LoggingError(Exception):
    pass


def is_node(m):
    from .models import CoreServiceNode

    return isinstance(m, CoreServiceNode)


def platform_of(h):
    c = h.clusters.select_related("platform").first()
    if c is None:
        raise LoggingError(f"{h.name} is in no cluster: no platform to reach it with")
    return c.platform


def host_ssh(h):
    """ssh to the host as root (sudo) with its platform's SA account, as the deployment does."""
    from . import sshexec
    from .hostdeploy import logins

    if not h.ip:
        raise LoggingError(f"{h.name} has no address")
    user, key, *_r = logins(platform_of(h))

    def run(script, timeout=300):
        return sshexec.run(
            h.ip, 22, user, key, "sudo -n bash -s", site=h.site.code, stdin=script, timeout=timeout, stdout_reason=True
        )

    return run


def node_ssh(n):
    """ssh to a core service VM as root (sudo) with its own login (its secret), as Read logs in."""
    from . import sshexec
    from .credentials import account

    if not n.address:
        raise LoggingError(f"{n.name} has no address")
    if not n.base_os:
        raise LoggingError(f"{n.name} is not a base-OS machine: its logging comes from its own template")
    user, acc = account(n.secret)
    if not user:
        raise LoggingError(f"{n.name}: the secret {n.secret or '-'} has no user with a key or password")

    def run(script, timeout=300):
        return sshexec.run(
            n.address,
            n.ssh_port,
            user,
            acc.get("key"),
            "sudo -n bash -s",
            site=n.service.site.code,
            stdin=script,
            timeout=timeout,
            password=acc.get("password"),
            stdout_reason=True,
        )

    return run


def ssh_of(m):
    return node_ssh(m) if is_node(m) else host_ssh(m)


def services():
    from .hostdeploy import SERVICE_GROUPS
    from .models import HostService

    return sorted(HostService.objects.all(), key=lambda s: (SERVICE_GROUPS.index(s.group), s.name.lower()))


def plan(h, ssh):
    """(the names of the machine's logged services, those services, {path: text} the machine should have, the
    collector). ``ssh`` is not used: they come from the machine's profiles (owner 2026-11-24, Profiles → Logging)."""
    from tenants.models import PortalSettings

    col = logconf.collector(h.site)
    chosen = logconf.logged_of(h)
    keys = [s.name for s in chosen]
    return keys, chosen, logconf.files(chosen, col, logconf.rules_of(PortalSettings.get())), col


def units_of(h):
    """The systemd units of the machine's services (its profiles' services, HostService), checked as running."""
    allsv = services()
    keys = logconf.services_of(h, allsv)
    return [u for s in allsv if s.key in keys for u in (s.units or [])]


def config_files(chosen):
    """[(service key, file, changes)] of the found services' config changes."""
    return [
        (c["key"], f["file"], f["changes"]) for c in (logconf.config_of(s) for s in chosen) if c for f in c["files"]
    ]


# owner 2026-11-24 09:34 "need to all logs on each systsm will rotated": the files under /var/log that no rotation
# covers are listed (the journal, rotated copies and files that are not logs left out)
NOT_LOGS = ("/var/log/journal/*", "/var/log/lastlog", "/var/log/faillog", "/var/log/installer/*", "/var/log/private/*")
COPY = re.compile(r"(\.[0-9]+|-[0-9]{8,10}|\.old)(\.gz|\.xz|\.bz2|\.zst)?$|\.(gz|xz|bz2|zst)$")


def unrotated(listed, patterns):
    """The files listed (find /var/log) that no rotation pattern covers."""
    out = []
    for f in listed:  # commit-446: matched as logrotate matches - one path part at a time
        # commit-450: a directory "/*" by its prefix, a file exactly
        if COPY.search(f) or any(f.startswith(g[:-1]) if g.endswith("/*") else f == g for g in NOT_LOGS):
            continue
        if not any(logconf.path_match(f, g) for g in patterns):
            out.append(f)
    return sorted(out)


def check_script(paths, units, read=()):
    """Read only: each path's text (base64) or - (the logging files and the files of config changes, read), the other
    files of /etc/rsyslog.d and /etc/logrotate.d, rsyslog's state, the state of each unit that exists, whether the
    leftover file is there and whether the portal's hourly logrotate timer is there when the machine should not
    have it."""
    lines = ["set -u"]
    for p in sorted(set(paths) | set(read)):
        q = shlex.quote(p)
        lines.append(f'if [ -f {q} ]; then echo "F {p} $(base64 -w0 < {q})"; else echo "F {p} -"; fi')
    keep = " ".join(shlex.quote(p) for p in sorted(paths))
    lines += [
        f"keep=' {keep} '",
        "for f in /etc/rsyslog.d/* /etc/logrotate.d/*; do",
        '  [ -f "$f" ] || continue',
        '  case "$keep" in *" $f "*) ;; *) echo "O $f";; esac',
        "done",
        'echo "R $(systemctl is-active rsyslog 2>/dev/null)"',
    ]
    for u in sorted(set(units)):
        q = shlex.quote(u)
        lines.append(f'if systemctl cat {q} >/dev/null 2>&1; then echo "U {u} $(systemctl is-active {q})"; fi')
    lines.append(f'[ -e {LEFTOVER} ] && echo "X {LEFTOVER}"')
    # commit-283: logrotate.timer's state and schedule; commit-278: the reloads/restarts still pending
    lines.append(
        'echo "L $(systemctl is-active logrotate.timer 2>/dev/null) '
        "$(systemctl show -P TimersCalendar logrotate.timer 2>/dev/null | tr -d ' ')\""
    )
    lines.append(f'for f in {logconf.PENDING}/*; do [ -e "$f" ] && echo "P ${{f##*/}}"; done')
    if logconf.TIMER not in paths:
        lines.append(f'[ -e {logconf.TIMER} ] && echo "T {logconf.TIMER}"')
    lines.append("find /var/log -xdev -type f 2>/dev/null | head -2000 | sed 's/^/V /'")
    lines.append("true")
    return "\n".join(lines) + "\n"


OK_STATES = ("same", "config made", "not on the machine")


def compare(content, out, keys=(), configs=(), patterns=None, markers=None):
    """The machine's state against the files it should have and its services' config changes. ``patterns``: the
    rotation patterns of its files - the logs under /var/log none of them covers are listed (not a difference)."""
    files, others, units_down, rsyslog, leftover, timer, listed = [], [], [], "", False, False, []
    timer_state, pending = None, []
    have = {}
    for line in str(out or "").splitlines():
        kind, _s, rest = line.partition(" ")
        if kind == "F":
            path, _s, b64 = rest.partition(" ")
            have[path] = None if b64.strip() == "-" else base64.b64decode(b64.strip() or "").decode("utf-8", "replace")
        elif kind == "O":
            others.append(rest.strip())
        elif kind == "R":
            rsyslog = rest.strip()
        elif kind == "U":
            unit, _s, state = rest.partition(" ")
            if state.strip() != "active":
                units_down.append(f"{unit} ({state.strip() or 'unknown'})")
        elif kind == "X":
            leftover = True
        elif kind == "T":
            timer = True
        elif kind == "L":
            state, _s, sched = rest.partition(" ")
            timer_state = (state.strip(), sched.strip())
        elif kind == "P":
            pending.append(rest.strip())
        elif kind == "V":
            listed.append(rest.strip())
    for path in sorted(content):
        cur = have.get(path)
        if cur is None:
            files.append({"path": path, "state": "added"})
        elif cur == content[path]:
            files.append({"path": path, "state": "same"})
        else:
            diff = "".join(
                difflib.unified_diff(
                    cur.splitlines(True), content[path].splitlines(True), "on the host", "from the services", n=1
                )
            )
            files.append({"path": path, "state": "differs", "diff": diff[:DIFF_MAX]})
    files += [{"path": p, "state": "moved aside"} for p in sorted(others)]
    if leftover:
        files.append({"path": LEFTOVER, "state": "removed"})
    if timer:  # no found service rotates at a size: logrotate daily again
        files.append({"path": logconf.TIMER, "state": "removed"})
    if timer_state is not None:  # commit-283: the timer running, hourly exactly where the drop-in should be
        state, sched = timer_state
        if state != "active":
            files.append({"path": "logrotate.timer", "state": f"timer {state or 'unknown'}"})
        elif logconf.calendars(sched) != [logconf.HOURLY if logconf.TIMER in content else logconf.DAILY]:
            want = "hourly" if logconf.TIMER in content else "daily"  # commit-290: exactly, not "any but hourly"
            files.append({"path": "logrotate.timer", "state": f"timer not {want}"})
    for key, path, changes in configs:  # owner 10:49: the settings in the services' own files
        cur = have.get(path)
        if cur is None:
            files.append({"path": path, "state": "not on the machine", "service": key})
            continue
        if logconf.apply_changes(cur, changes) == cur:
            files.append({"path": path, "state": "config made", "service": key})
        else:  # commit-282: no text of the file kept or shown - only which changes are still to be made, and where
            todo = [
                {"find": c["find"], "places": len(re.findall(c["find"], cur, flags=re.M))}
                for c in changes
                if logconf.apply_changes(cur, [c]) != cur
            ]
            files.append({"path": path, "state": "config change not made", "service": key, "changes": todo})
    for key in sorted(set(pending)):  # commit-278; commit-445: a marker named by the service's id shows its name
        files.append(
            {"path": f"{logconf.PENDING}/{key}", "state": "reload pending", "service": (markers or {}).get(key, key)}
        )
    ok = all(f["state"] in OK_STATES for f in files) and rsyslog == "active"
    result = {
        "status": "compliant" if ok else "differs",
        "services": list(keys),
        "files": files,
        "rsyslog": rsyslog or "unknown",
        "units_down": units_down,
    }
    if patterns is not None:
        result["unrotated"] = unrotated(listed, patterns)[:200]
    return result


def _when(at):
    from datetime import UTC, datetime

    from django.utils.dateparse import parse_datetime

    try:
        return parse_datetime(str(at or "")) or datetime.min.replace(tzinfo=UTC)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)


def _newer(stored, at):
    return stored is not None and at is not None and stored > at


def keep_services(h, keys, at):
    """commit-267/293: the services found by a read that started at `at` - kept only when the stored ones are not from
    a later read (under the row lock). Returns whether they were kept."""
    from django.db import transaction

    model = type(h)
    with transaction.atomic():
        stored = model.objects.select_for_update().filter(pk=h.pk).values_list("services_at", flat=True).first()
        if _newer(stored, at):
            return False
        model.objects.filter(pk=h.pk).update(services=keys, services_at=at)
    h.services, h.services_at = keys, at
    return True


def _store(h, result, **fields):
    """commit-266: kept only when the stored result is not newer (a check that started before an Apply and ended after
    it never puts the old state back). commit-267/293: the services found by the same read (fields) are written in the
    same locked update, and only when the stored services are not from a later read (a refused Apply keeps the
    services it found without a result). Returns whether the result was kept."""
    from django.db import transaction

    model = type(h)  # a Host or a CoreServiceNode
    with transaction.atomic():
        row = model.objects.select_for_update().filter(pk=h.pk).values("logging", "services_at").first() or {}
        cur = row.get("logging") or {}
        if _when(cur.get("at")) > _when(result.get("at")):
            h.logging = cur
            return False
        if _newer(row.get("services_at"), fields.get("services_at")):
            fields = {}  # the services of a later read stay
        model.objects.filter(pk=h.pk).update(logging=result, **fields)
    h.logging = result
    for k, v in fields.items():
        setattr(h, k, v)
    return True


def check(h, ssh=None):
    """Read the host's logging and keep the result on it; never changes the host."""
    now = timezone.now()  # when the host is read: the result's "at" (commit-266)
    found = {}
    try:
        ssh = ssh or ssh_of(h)
        keys, chosen, content, _col = plan(h, ssh)
        units = units_of(h)
        configs = config_files(chosen)
        out = ssh(check_script(content, units, [c[1] for c in configs]), timeout=120)
        result = compare(
            content,
            out,
            keys,
            configs,
            [p for _o, p in logconf.rotation_paths(chosen)],
            {logconf.marker_of(s): s.name for s in chosen},
        )
        found = {"services": keys, "services_at": now}  # commit-267: stored with the result, under its guard
    except Exception as exc:  # noqa: BLE001 - the host's own failure is kept as its result
        result = {"status": "not checked", "error": str(exc)[:300]}
    result["at"] = now.isoformat(timespec="microseconds")
    _store(h, result, **found)
    return result


def mark_applied(h):
    """commit-266: when logging was applied, in its own field."""
    now = timezone.now()
    type(h).objects.filter(pk=h.pk).update(logging_applied_at=now)
    h.logging_applied_at = now
    return now


CLAIM_SECONDS = (
    900  # commit-263: an Apply's claim on its host: plan, install (300 s), test message and check, with room
)


def deploy_running(h):
    """A deployment or retirement of the host: its task, or a run claimed so recently that its task is not listed yet
    (hostdeploy.start saves the claim first and starts the task after the commit)."""
    from accounts import tasks

    from .hostdeploy import _claim_fresh
    from .models import HostDeployment

    if is_node(h):  # a redeploy, roll back or removal of the VM (inventory/core_views.py)
        from accounts.models import Task

        return Task.objects.filter(
            kind="core VM redeploy",
            target_type="coreservicenode",
            target_id=str(h.pk),
            state__in=[Task.State.QUEUED, Task.State.RUNNING],
        ).exists()
    if tasks.running("host deploy", h):
        return True
    return any(_claim_fresh(d) for d in HostDeployment.objects.filter(host=h, state=HostDeployment.State.RUNNING))


def claim_fresh(claim):
    """commit-263: an Apply logging claim on a host ({"token", "at"}) that is still held."""
    at = _when((claim or {}).get("at"))
    return bool((claim or {}).get("token")) and (timezone.now() - at).total_seconds() < CLAIM_SECONDS


def _claim(h):
    """commit-263: the host's row locked (as hostdeploy.start locks it), refused while a deployment runs or another
    Apply holds the host, then the Apply's claim put on it. Returns the token."""
    import secrets

    from django.db import transaction

    model = type(h)
    token = secrets.token_hex(8)
    with transaction.atomic():
        row = model.objects.select_for_update().get(pk=h.pk)
        if deploy_running(h):
            raise LoggingError(
                f"a redeploy of {h.name} is running"
                if is_node(h)
                else f"a deployment of {h.name} is running: its own logging step writes these files"
            )
        if claim_fresh(row.logging_claim):
            if (row.logging_claim or {}).get("by"):  # commit-281: a redeploy, roll back or removal is being started
                raise LoggingError(f"a {row.logging_claim['by']} of {h.name} is starting")
            raise LoggingError(f"Apply logging of {h.name} is already running")
        model.objects.filter(pk=h.pk).update(logging_claim={"token": token, "at": timezone.now().isoformat()})
    return token


def _release(h, token):
    from django.db import transaction

    model = type(h)
    with transaction.atomic():
        row = model.objects.select_for_update().get(pk=h.pk)
        if (row.logging_claim or {}).get("token") == token:
            model.objects.filter(pk=h.pk).update(logging_claim={})


def restarts_of(chosen):
    """commit-285: the services Apply would restart when their config changes, by key (a name may hold a comma)."""
    return sorted(
        s.key for s in chosen if (s.config or {}).get("changes") and (s.config or {}).get("then") == "restart"
    )


def restart_names(chosen):
    return [s.name for s in sorted(chosen, key=lambda s: s.key) if s.key in restarts_of([s])]


def apply(user, h, ssh=None, restarts=None):
    """Apply logging: the files of the host's services installed as the deployment does, then a test message, then the
    host checked again. Raises LoggingError when the install did not work (the host's previous set is back then).
    commit-263: the host is claimed first (its row locked, as a deployment's start locks it): refused while a
    deployment runs or another Apply holds it, and a deployment is refused while this claim is held - to the end of
    the check. The host's own lock still refuses a second install. commit-264: the install is recorded as soon as it
    succeeded; the test message's failure is reported on its own (result["test_message"]) - the new files stay then."""
    token = _claim(h)
    try:
        return _apply(user, h, ssh, restarts)
    finally:
        _release(h, token)


def make_config(chosen, ssh):
    """Owner 10:49: the found services' config changes made on the machine (after its logging files). Returns
    (files, then, error): what each file and each reload/restart did; error is "" when nothing failed."""
    script = logconf.config_script(chosen)
    if script is None:
        return [], [], ""
    error, out = "", ""
    try:
        out = ssh(script, timeout=300)
    except Exception as exc:  # noqa: BLE001 - not run (busy, no python3, no login): reported, nothing changed
        error = f"config changes not made: {str(exc)[:300]}"
    files, then = logconf.parse_config(out)
    bad = [f"{f['file']}: {f['detail']}" for f in files if f["state"] == "failed"]
    bad += [f"{t['service']}: {t['detail']}" for t in then if t["state"] == "failed"]
    if bad:
        error = "; ".join(bad)[:400]
    elif not error and not files:
        error = "config changes not made: no answer from the machine's script"
    return files, then, error


def _apply(user, h, ssh, restarts=None):
    ssh = ssh or ssh_of(h)
    seen = timezone.now()  # commit-293: when the machine is read, before the read
    try:
        keys, chosen, content, col = plan(h, ssh)
    except Exception as exc:  # noqa: BLE001 - nothing changed yet
        raise LoggingError(str(exc)[:400]) from exc
    fresh = restarts_of(chosen)
    if restarts is not None and sorted(restarts) != fresh:  # commit-285: confirmed for other restarts than these
        # commit-289: the services just found kept, so the dialog shown again names the restarts Apply would make;
        # commit-293: not over the services of a later read
        keep_services(h, keys, seen)
        raise LoggingError(
            "the machine's services changed since the page was shown: Apply would restart "
            + ("; ".join(restart_names(chosen)) or "nothing")
            + " - nothing was changed; confirm again"
        )
    try:
        ssh(logconf.install_script(content), timeout=300)
    except Exception as exc:  # noqa: BLE001 - reported on the page and in the audit log
        audit(user, "host_logging_failed", h, error=str(exc)[:300])
        check(h, ssh)
        raise LoggingError(str(exc)[:400]) from exc
    mark_applied(h)
    audit(user, "host_logging_applied", h, services=keys, collector=f"{col['host']}:{col['port']}")
    cfiles, cthen, cerror = make_config(chosen, ssh)
    if cfiles or cthen:
        audit(
            user,
            "host_config_changes",
            h,
            files={f["file"]: f["state"] for f in cfiles},
            then=[f"{t['service']} {t['state']}" for t in cthen],
            error=cerror,
        )
    test = "sent"
    try:
        ssh(f"logger -p user.warning -t claugine {shlex.quote('claugine logging check ' + h.name)}\n", timeout=60)
    except Exception as exc:  # noqa: BLE001 - the files are installed; only the test message failed
        test = f"not sent: {str(exc)[:200]}"
        audit(user, "host_logging_test_failed", h, error=str(exc)[:300])
    result = check(h, ssh)
    result["test_message"] = test
    result["config"], result["config_then"], result["config_error"] = cfiles, cthen, cerror
    return result


def deployed_hosts():
    from .models import Host

    return (
        Host.objects.filter(state__in=_in_service(), clusters__isnull=False)
        .exclude(ip="")
        .select_related("site")
        .distinct()
    )


def service_vms():
    """Owner 11:58: the base-OS VMs (and hosts not deployed by the portal) of the active core services."""
    from .models import CoreServiceNode

    nodes = (
        CoreServiceNode.objects.filter(service__state__in=_in_service(), host__isnull=True)
        .exclude(address="")
        .select_related("service__site", "service__template")
    )
    return [n for n in nodes if n.base_os]


def check_all():
    """The timer: every deployed host and every base-OS core service VM. A machine that differs is a report, not a
    failure of the timer; one that could not be read is. Returns (ok, detail). commit-265: machines named by site and
    name (the same name may be on two sites)."""
    res = {h.key: check(h).get("status") for h in deployed_hosts()}
    res.update({n.key: check(n).get("status") for n in service_vms()})
    unread = sorted(n for n, s in res.items() if s == "not checked")
    differs = sorted(n for n, s in res.items() if s == "differs")
    return not unread, (
        f"{len(res)} machines, {len(res) - len(unread) - len(differs)} compliant"
        + (f", differ: {', '.join(differs[:10])}" if differs else "")
        + (f", not read: {', '.join(unread[:10])}" if unread else "")
    )


def _in_service():
    from .models import IN_SERVICE

    return IN_SERVICE
