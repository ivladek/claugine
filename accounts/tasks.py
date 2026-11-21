"""Running background work as Tasks: start(kind, title, fn, …) records the task and runs fn(task) in a thread
(at once in tests, DEPLOY_ASYNC=False). fn reports with task.step(name, status, detail) and returns the result."""

import os
import socket
import threading

from django.conf import settings
from django.db import connection
from django.utils import timezone

from .models import Task


def _start_ticks(pid):
    """When the process started (clock ticks since boot): with the pid it names one process, even after the pid is
    reused."""
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as fh:
            return fh.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return ""


def me():
    return f"{socket.gethostname()}:{os.getpid()}:{_start_ticks(os.getpid())}"[:120]


def alive(runner):
    """True/False for a task run on this host by a process that is / is not there any more; None when it cannot be
    told (another host, or no runner recorded)."""
    try:
        host, pid, ticks = runner.rsplit(":", 2)
    except ValueError:
        return None
    if host != socket.gethostname() or not pid.isdigit():
        return None
    return bool(ticks) and _start_ticks(int(pid)) == ticks


def reap():
    """core-007: tasks whose process ended (the portal restarted, the VM rebooted) stay queued or running for good,
    and the updates, the OS updates and the key change wait for them forever. They are failed here, with the reason;
    one without a runner (made before this release), or on another host, only once it is stale."""
    n = 0
    for t in Task.objects.filter(state__in=[Task.State.QUEUED, Task.State.RUNNING]):
        live = alive(t.runner)
        if live is False or (live is None and t.is_stale):
            t.state, t.finished_at = Task.State.FAILED, timezone.now()
            t.error = (t.error + "\n" if t.error else "") + "interrupted: the process that ran it ended (a restart)"
            t.progress = ""
            t.save(update_fields=["state", "finished_at", "error", "progress"])
            n += 1
    return n + reap_work() + reap_archivals() + reap_repairs() + reap_deployments()


def reap_deployments():
    """commit-140 (T25): a host deployment whose task was interrupted becomes a failed run that Resume continues."""
    from inventory.hostdeploy import reap_interrupted

    return reap_interrupted()


REPAIR_INTERRUPTED = "interrupted: the process that ran it ended (a restart); run Make consistent again"
REPAIR_ENDED = "interrupted: the repair stopped without recording its end; run Make consistent again"


def repairs_running():
    """core-046 (reopened, twenty-fourth full review): the platforms whose consistency repair (tenants.consistency)
    runs - it deletes what archived projects left, so the maintenance gate counts it."""
    from inventory.models import PlatformControl

    return [
        c
        for c in PlatformControl.objects.only("pk", "consistency")
        if ((c.consistency or {}).get("repair") or {}).get("state") == "running"
    ]


def reap_repairs():
    """A repair whose process ended is marked interrupted (with the reason on the platform's page), so it no longer
    holds maintenance and Make consistent can run again; one with no runner or on another host only once stale."""
    from datetime import datetime, timedelta

    from django.db import transaction

    from inventory.models import PlatformControl
    from tenants.consistency import attempt_running

    n, now = 0, timezone.now()
    for c in repairs_running():
        rep = (c.consistency or {}).get("repair") or {}
        live = alive(rep.get("runner") or "")
        try:
            began = datetime.fromisoformat(rep.get("at") or "")
        except ValueError:
            began = None
        # core-050, commit-058: its process lives, but the repair's thread ended without its last write - its lock
        # is free (any process on the node can tell: the HA agent too)
        ended = live is True and bool(rep.get("attempt")) and not attempt_running(rep["attempt"])
        if (
            live is False
            or ended
            or (live is None and (began is None or began < now - timedelta(hours=WORK_STALE_HOURS)))
        ):
            with transaction.atomic():
                c = PlatformControl.objects.select_for_update().get(pk=c.pk)
                cur = (c.consistency or {}).get("repair") or {}
                if cur.get("state") != "running" or (cur.get("runner"), cur.get("attempt")) != (
                    rep.get("runner"),
                    rep.get("attempt"),
                ):
                    continue
                c.consistency = {
                    **c.consistency,
                    "repair": {
                        "state": "interrupted",
                        "at": now.isoformat(),
                        "done": [REPAIR_ENDED if ended else REPAIR_INTERRUPTED],
                    },
                }
                c.save(update_fields=["consistency"])
                n += 1
    return n


# core-007 (full review 2026-11-16): the other work the maintenance gate counts (running_count) runs in threads too,
# and was left "running" for good when its process ended. (model, state field, running value, failed value, the time
# it started, the field its reason goes in)
WORK = (
    ("Deployment", "state", "running", "failed", "started_at", "error"),
    ("NetworkRequest", "run", "running", "failed", "decided_at", "error"),
    ("TenantDeletion", "state", "running", "failed", "created_at", "error"),
    ("ConfigCheck", "state", "running", "failed", "started_at", "error"),
    ("IgwApply", "state", "running", "failed", "created_at", "result"),
)
WORK_STALE_HOURS = 6  # a record with no runner (made before this release) or run on another host: failed only then
INTERRUPTED = "interrupted: the process that ran it ended (a restart)"


ARCHIVAL_INTERRUPTED = "interrupted: the process that ran it ended (a restart); tried again by the daily round"


def reap_archivals():
    """core-044 (twenty-first full review): an archival (tenants.lifecycle) whose process ended - the portal updated or
    the node rebooted half way - goes back to saved with the reason on its page, so it no longer holds maintenance and
    the daily round archives it; one with no runner (claimed before this release) or on another host only once it is
    stale."""
    from datetime import timedelta

    from tenants.models import Lifecycle, Tenant

    n, now = 0, timezone.now()
    old = now - timedelta(hours=WORK_STALE_HOURS)
    for t in Tenant.objects.filter(status=Lifecycle.DELETING, archiving_since__isnull=False):
        live = alive(t.archive_runner)
        if live is False or (live is None and t.archiving_since < old):
            n += Tenant.objects.filter(pk=t.pk, status=Lifecycle.DELETING, archive_runner=t.archive_runner).update(
                status=Lifecycle.SAVED,
                status_changed_at=now,
                archiving_since=None,
                archive_runner="",
                archive_error=ARCHIVAL_INTERRUPTED,
            )
    return n


def reap_work():
    from datetime import timedelta

    from tenants import models as tm

    n, now = 0, timezone.now()
    old = now - timedelta(hours=WORK_STALE_HOURS)
    for name, field, running, failed, started, why in WORK:
        model = getattr(tm, name)
        for w in model.objects.filter(**{field: running}):
            live = alive(w.runner)
            began = getattr(w, started, None) or getattr(w, "created_at", None)
            if live is False or (live is None and began is not None and began < old):
                # core-049: only the run that was looked at - a retry that claimed the row meanwhile (a new runner,
                # host:pid:start) keeps running and stays counted
                n += model.objects.filter(pk=w.pk, **{field: running}, runner=w.runner).update(
                    **{
                        field: failed,
                        why: (getattr(w, why) + "\n" if getattr(w, why) else "") + INTERRUPTED,
                        "finished_at": now,
                    }
                )
    # an igw apply waiting for its confirmation past its deadline: the igw rolled back by itself (commit-confirm)
    for a in tm.IgwApply.objects.filter(state="waiting", deadline__lt=now - timedelta(hours=1)):
        a.state, a.finished_at = "rolled_back", now
        a.save(update_fields=["state", "finished_at"])
        n += 1
    return n


def _run(task_id):
    task = Task.objects.get(pk=task_id)
    fn = _FUNCS.pop(task_id, None)
    task.state, task.started_at, task.runner = Task.State.RUNNING, timezone.now(), me()
    task.save(update_fields=["state", "started_at", "runner"])
    try:
        result = fn(task) if fn else {}
        task.result = result if isinstance(result, dict) else {"result": str(result)}
        task.state = Task.State.DONE
    except Exception as exc:  # noqa: BLE001 - recorded on the task
        task.state, task.error = Task.State.FAILED, f"{type(exc).__name__}: {exc}"[:2000]
    task.finished_at = timezone.now()
    task.progress = ""
    task.save(update_fields=["state", "result", "error", "finished_at", "progress"])
    return task


_FUNCS = {}


def start(kind, title, fn, user=None, target=None):
    from . import maintenance

    with maintenance.admitting():  # core-046: admitted only while no maintenance runs (accounts.maintenance)
        task = Task.objects.create(
            kind=kind,
            title=title[:200],
            started_by=user if getattr(user, "is_authenticated", False) else None,
            target_type=target._meta.model_name if target is not None else "",
            target_id=str(target.pk) if target is not None else "",
            runner=me(),
        )
    _FUNCS[task.pk] = fn
    if not getattr(settings, "DEPLOY_ASYNC", True):
        return _run(task.pk)

    def worker():
        try:
            _run(task.pk)
        finally:
            connection.close()

    threading.Thread(target=worker, name=f"task-{task.pk}", daemon=True).start()
    return task


def running(kind, target):
    return Task.objects.filter(
        kind=kind,
        target_type=target._meta.model_name,
        target_id=str(target.pk),
        state__in=[Task.State.QUEUED, Task.State.RUNNING],
    ).first()


def all_tasks(days):
    """Every task of the last `days` days, newest first: portal tasks and the tenant work records."""
    from datetime import timedelta

    from tenants.models import ConfigCheck, Deployment, NetworkRequest, TenantDeletion

    since = timezone.now() - timedelta(days=days)
    rows = []
    for t in Task.objects.filter(created_at__gte=since).select_related("started_by"):
        rows.append(
            {
                "id": f"task-{t.pk}",
                "kind": t.kind,
                "title": t.title,
                "state": "running" if t.state == "queued" else t.state,
                "label": t.get_state_display(),
                "by": t.started_by,
                "at": t.created_at,
                "end": t.finished_at,
                "steps": t.steps,
                "error": t.error,
                "progress": t.progress,
                "result": t.result,
                "tenant": None,
                "stale": t.is_stale,
            }
        )
    for d in Deployment.objects.filter(started_at__gte=since).select_related("tenant", "created_by"):
        rows.append(
            {
                "id": f"dep-{d.pk}",
                "kind": "deployment",
                "title": f"{d.tenant} · {d.platform_key}",
                "state": d.state,
                "label": d.get_state_display(),
                "by": d.created_by,
                "at": d.started_at,
                "end": d.finished_at,
                "steps": d.steps,
                "error": d.error,
                "tenant": d.tenant,
                "stale": d.is_stale,
            }
        )
    for r in (
        NetworkRequest.objects.exclude(run="").filter(decided_at__gte=since).select_related("tenant", "decided_by")
    ):
        rows.append(
            {
                "id": f"net-{r.pk}",
                "kind": "network",
                "title": f"{r.tenant} · {r.get_kind_display()} {r.get_action_display()} {r.what}",
                "state": r.run,
                "label": r.get_run_display(),
                "by": r.decided_by,
                "at": r.decided_at,
                "end": r.finished_at,
                "steps": r.steps,
                "error": r.error,
                "tenant": r.tenant,
                "stale": False,
            }
        )
    for x in (
        TenantDeletion.objects.exclude(state="pending")
        .filter(created_at__gte=since)
        .select_related("tenant", "decided_by")
    ):
        rows.append(
            {
                "id": f"del-{x.pk}",
                "kind": "deletion",
                "title": str(x.tenant),
                "state": x.state,
                "label": x.get_state_display(),
                "by": x.decided_by,
                "at": x.decided_at or x.created_at,
                "end": x.finished_at,
                "steps": x.steps,
                "error": x.error,
                "tenant": x.tenant,
                "stale": False,
            }
        )
    for c in ConfigCheck.objects.filter(started_at__gte=since).select_related("tenant", "requested_by"):
        rows.append(
            {
                "id": f"cfg-{c.pk}",
                "kind": "config check",
                "title": str(c.tenant),
                "state": c.state,
                "label": c.get_state_display(),
                "by": c.requested_by,
                "at": c.started_at,
                "end": c.finished_at,
                "steps": [
                    {"name": k, "status": v.get("status", ""), "detail": v.get("error") or v.get("host", "")}
                    for k, v in (c.results or {}).items()
                ],
                "error": c.error,
                "tenant": c.tenant,
                "stale": False,
            }
        )
    rows.sort(key=lambda r: r["at"] or timezone.now(), reverse=True)
    return rows


def cleanup(task_days, log_days):
    """Remove finished tasks and log entries older than the history settings. Returns the counts."""
    from datetime import timedelta

    from tenants.models import ConfigCheck

    from .models import AuditLog

    now = timezone.now()
    t = Task.objects.filter(finished_at__lt=now - timedelta(days=task_days)).delete()[0]
    c = ConfigCheck.objects.filter(finished_at__lt=now - timedelta(days=task_days)).delete()[0]
    a = AuditLog.objects.filter(created_at__lt=now - timedelta(days=log_days)).delete()[0]
    return {"tasks": t, "config_checks": c, "log_entries": a}


def running_count():
    """Everything still running: portal tasks, deployments, network changes, deletions, configuration checks, igw
    applies, archivals. A switchover of the portal waits until it is 0."""
    from accounts import ha
    from accounts.models import Task
    from tenants.models import ConfigCheck, Deployment, IgwApply, Lifecycle, NetworkRequest, Tenant, TenantDeletion

    if ha.role() == "active":  # core-007: what an ended process left "running" does not count; only the active node
        reap()  # writes it (commit-020: a standby's database is read only - its status only counts)
    return (
        Task.objects.filter(state__in=["running", "queued"]).count()
        + Deployment.objects.filter(state="running").count()
        + NetworkRequest.objects.filter(run="running").count()
        + TenantDeletion.objects.filter(state="running").count()
        + ConfigCheck.objects.filter(state="running").count()
        + IgwApply.objects.filter(state__in=["running", "waiting"]).count()
        # core-044: an archival in progress (by hand in a thread, or the daily round's)
        + Tenant.objects.filter(status=Lifecycle.DELETING, archiving_since__isnull=False).count()
        # core-046 (reopened): a platform's consistency repair
        + len(repairs_running())
        # owner 2026-11-18 (option A): a backup run (portal or platforms) on this node - a switch over waits for it
        + _backups_running()
    )


def _backups_running():
    from accounts import appliance

    return appliance.backups_running()
