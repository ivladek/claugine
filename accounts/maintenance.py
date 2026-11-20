"""core-046 / enterprise-022 (twenty-third full review, owner "fix before" 2026-11-17 19:14): one maintenance gate.

An operation that restarts the portal (a single node's update, OS update or edition upgrade, a key rotation, Add /
Replace node) closes the gate before it counts the running work, and keeps it closed until it ends. Every place that
starts work the count includes - a task, a deployment, a network change, a deletion, a configuration check, an igw
apply, an archival - records it inside `admitting()`, which refuses while the gate is closed. Closing and admitting
take the same row lock, so work is either recorded before the gate closed (and then counted) or refused after it.

A gate left closed by an operation that ended without opening it (a killed process) opens by itself after
STALE_HOURS.
"""

from contextlib import contextmanager
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

STALE_HOURS = 6


class MaintenanceRunning(Exception):
    """New work is refused: an operation that restarts the portal is running."""

    def __init__(self, op=""):
        self.op = op
        super().__init__(_("Maintenance is running (%(op)s): try again when it ends.") % {"op": op or "-"})


def _row(lock=False):
    from .models import MaintenanceGate

    MaintenanceGate.objects.get_or_create(pk=1)
    qs = MaintenanceGate.objects.filter(pk=1)
    return (qs.select_for_update() if lock else qs).get()


def _closed(g):
    return bool(g.since) and g.since > timezone.now() - timedelta(hours=STALE_HOURS)


def state():
    """(closed, op) - for the status pages."""
    g = _row()
    return _closed(g), (g.op if _closed(g) else "")


@contextmanager
def admitting():
    """Record new work inside this block: refused (MaintenanceRunning) while the gate is closed."""
    with transaction.atomic():
        g = _row(lock=True)
        if _closed(g):
            raise MaintenanceRunning(g.op)
        yield


def admit():
    """For a starter already inside its own transaction: take the gate's lock there (it is held until that
    transaction ends) and refuse while closed. Outside a transaction the lock would end at once (commit-043): use
    admitting() then."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("maintenance.admit() needs the starter's transaction: use maintenance.admitting()")
    g = _row(lock=True)
    if _closed(g):
        raise MaintenanceRunning(g.op)


def close(op):
    """Close the gate for `op`, then count the work that runs; with work running, the gate is not closed and
    MaintenanceRunning-like RuntimeError is raised. Returns nothing; the caller opens it when the operation ends."""
    from .tasks import running_count

    # integration-007, commit-056: the count runs under the gate's row lock, inside the transaction that closes it -
    # a starter waits on that lock, so nothing is admitted between the count and the commit; the closure is committed
    # only with nothing running, and a count that fails (the database away) or finds work rolls it back with no
    # second write that could fail too
    # commit-066: the count comes first and the gate is saved only when nothing runs; "busy" is raised after the
    # transaction ends, so what the count's clean-up marked (interrupted work, reap()) stays marked
    with transaction.atomic():
        g = _row(lock=True)
        # an operation's second half (update-portal-finish after update-portal) takes the gate its first half closed
        if _closed(g) and g.op != op and not op.startswith(g.op + "-"):
            raise RuntimeError(f"another maintenance is running ({g.op})")
        n = running_count()
        if not n:
            g.since, g.op = timezone.now(), op[:64]
            g.save(update_fields=["since", "op"])
    if n:
        raise RuntimeError(f"{n} task(s) running: wait until they finish")


def open_(op=""):
    """Open the gate (when `op` is given, only if it is the one that closed it)."""
    with transaction.atomic():
        g = _row(lock=True)
        # commit-046: only the operation that holds the gate opens it - an update's first half, ending after its second
        # half took the gate (update-portal-finish), leaves it to the second half
        if g.since and (not op or g.op == op[:64]):
            g.since, g.op = None, ""
            g.save(update_fields=["since", "op"])


class Middleware:
    """A request refused by the gate goes back with the message (a JSON 503 for the API), not an error page."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exc):
        if not isinstance(exc, MaintenanceRunning):
            return None
        from django.contrib import messages
        from django.http import HttpResponseRedirect, JsonResponse
        from django.utils.http import url_has_allowed_host_and_scheme

        if request.path.startswith("/api/") or "application/json" in request.headers.get("Accept", ""):
            return JsonResponse({"error": str(exc)}, status=503)
        messages.error(request, str(exc))
        back = request.headers.get("Referer") or "/"
        if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}):
            back = "/"
        return HttpResponseRedirect(back)
