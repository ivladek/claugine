"""Running background work as Tasks: start(kind, title, fn, …) records the task and runs fn(task) in a thread
(at once in tests, DEPLOY_ASYNC=False). fn reports with task.step(name, status, detail) and returns the result."""
import threading

from django.conf import settings
from django.db import connection
from django.utils import timezone

from .models import Task


def _run(task_id):
    task = Task.objects.get(pk=task_id)
    fn = _FUNCS.pop(task_id, None)
    task.state, task.started_at = Task.State.RUNNING, timezone.now()
    task.save(update_fields=["state", "started_at"])
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
    task = Task.objects.create(kind=kind, title=title[:200], started_by=user if getattr(user, "is_authenticated", False) else None,
                               target_type=target._meta.model_name if target is not None else "",
                               target_id=str(target.pk) if target is not None else "")
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
    return Task.objects.filter(kind=kind, target_type=target._meta.model_name, target_id=str(target.pk),
                               state__in=[Task.State.QUEUED, Task.State.RUNNING]).first()


def all_tasks(days):
    """Every task of the last `days` days, newest first: portal tasks and the tenant work records."""
    from datetime import timedelta
    from tenants.models import ConfigCheck, Deployment, NetworkRequest, TenantDeletion
    since = timezone.now() - timedelta(days=days)
    rows = []
    for t in Task.objects.filter(created_at__gte=since).select_related("started_by"):
        rows.append({"id": f"task-{t.pk}", "kind": t.kind, "title": t.title, "state": "running" if t.state == "queued" else t.state,
                     "label": t.get_state_display(), "by": t.started_by, "at": t.created_at, "end": t.finished_at,
                     "steps": t.steps, "error": t.error, "progress": t.progress, "result": t.result, "tenant": None,
                     "stale": t.is_stale})
    for d in Deployment.objects.filter(started_at__gte=since).select_related("tenant", "created_by"):
        rows.append({"id": f"dep-{d.pk}", "kind": "deployment", "title": f"{d.tenant} · {d.platform_key}",
                     "state": d.state, "label": d.get_state_display(), "by": d.created_by, "at": d.started_at,
                     "end": d.finished_at, "steps": d.steps, "error": d.error, "tenant": d.tenant, "stale": d.is_stale})
    for r in NetworkRequest.objects.exclude(run="").filter(decided_at__gte=since).select_related("tenant", "decided_by"):
        rows.append({"id": f"net-{r.pk}", "kind": "network", "title": f"{r.tenant} · {r.get_kind_display()} {r.get_action_display()} {r.what}",
                     "state": r.run, "label": r.get_run_display(), "by": r.decided_by, "at": r.decided_at,
                     "end": r.finished_at, "steps": r.steps, "error": r.error, "tenant": r.tenant, "stale": False})
    for x in TenantDeletion.objects.exclude(state="pending").filter(created_at__gte=since).select_related("tenant", "decided_by"):
        rows.append({"id": f"del-{x.pk}", "kind": "deletion", "title": str(x.tenant), "state": x.state,
                     "label": x.get_state_display(), "by": x.decided_by, "at": x.decided_at or x.created_at,
                     "end": x.finished_at, "steps": x.steps, "error": x.error, "tenant": x.tenant, "stale": False})
    for c in ConfigCheck.objects.filter(started_at__gte=since).select_related("tenant", "requested_by"):
        rows.append({"id": f"cfg-{c.pk}", "kind": "config check", "title": str(c.tenant), "state": c.state,
                     "label": c.get_state_display(), "by": c.requested_by, "at": c.started_at, "end": c.finished_at,
                     "steps": [{"name": k, "status": v.get("status", ""), "detail": v.get("error") or v.get("host", "")}
                               for k, v in (c.results or {}).items()], "error": c.error, "tenant": c.tenant, "stale": False})
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
    applies. A switchover of the portal waits until it is 0."""
    from accounts.models import Task
    from tenants.models import ConfigCheck, Deployment, IgwApply, NetworkRequest, TenantDeletion
    return (Task.objects.filter(state__in=["running", "queued"]).count()
            + Deployment.objects.filter(state="running").count()
            + NetworkRequest.objects.filter(run="running").count()
            + TenantDeletion.objects.filter(state="running").count()
            + ConfigCheck.objects.filter(state="running").count()
            + IgwApply.objects.filter(state__in=["running", "waiting"]).count())
