"""The portal's new version under the public URL (owner 2026-10-31): <public URL>/test-new-version/.

HAProxy sends /test-new-version/ to the portal node "new" (a copy of the database, "Publish the new version") when there
is one, otherwise to the standby; Apache on the node serves it with portal/wsgi_test.py (CLAUGINE_TEST_MOUNT=1) next to
the portal itself. There:

- only super admins sign in (sessions in a signed cookie of its own, path /test-new-version/: nothing is written);
- on the standby (its database follows the active node, read only) nothing can be changed: only the sign-in, the
  second factor, the sign-out and the language are posted; the last sign-in and the audit entry are left out;
- a release with database changes that the standby does not have yet shows a page instead: it is tried with
  "Publish the new version" (the standby takes a copy of the database and its changes).

Platforms, routers and iLOs are never changed from here (accounts.ha.forbid_writes: only the active node).
"""
import os
import time

PREFIX = "/test-new-version"
ON = os.environ.get("CLAUGINE_TEST_MOUNT") == "1"
OPEN_PATHS = ("/login/", "/login/code/", "/logout/", "/i18n/")  # posted even while read only (no database writes)
_PLAN = {"at": 0.0, "pending": []}


def read_only():
    """The test mount on the standby (or anywhere but a node "new"): nothing is written to the database."""
    if not ON:
        return False
    from . import ha
    return ha.role() != "new"


def pending_migrations():
    """The database changes of this release that the database does not have yet (checked once a minute)."""
    now = time.monotonic()
    if now - _PLAN["at"] > 60:
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        try:
            ex = MigrationExecutor(connection)
            plan = ex.migration_plan(ex.loader.graph.leaf_nodes())
            _PLAN["pending"] = [f"{m.app_label}.{m.name}" for m, _back in plan]
        except Exception as exc:  # noqa: BLE001 - shown on the page
            _PLAN["pending"] = [f"? {exc}"[:200]]
        _PLAN["at"] = now
    return _PLAN["pending"]


def _page(request, kind, status, **ctx):
    from django.shortcuts import render
    from django.http import HttpResponse
    try:
        return render(request, "ha/test_mount.html", {"kind": kind, **ctx}, status=status)
    except Exception:  # noqa: BLE001
        return HttpResponse("The new portal version under test: " + kind, status=status, content_type="text/plain")


def _is_db_read_only(exc):
    from django.db import DatabaseError
    if not isinstance(exc, DatabaseError):
        return False
    code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else 0
    return code in (1290, 1836) or "read-only" in str(exc).lower() or "read only" in str(exc).lower()


class TestMountMiddleware:
    """Nothing at all outside the test mount."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not ON:
            return self.get_response(request)
        path = request.path_info
        if path.startswith("/static/"):
            return self.get_response(request)
        pending = pending_migrations()
        if pending:
            return _page(request, "migrations", 503, pending=pending)
        user = getattr(request, "user", None)
        signed_in = user is not None and user.is_authenticated
        if signed_in and not getattr(user, "is_super_admin", False):
            from django.contrib.auth import logout
            logout(request)
            return _page(request, "super_admins", 403)
        if not signed_in and not path.startswith(OPEN_PATHS):
            from django.shortcuts import redirect
            from django.urls import reverse
            from urllib.parse import quote
            return redirect(reverse("login") + ("" if path == "/" else "?next=" + quote(request.get_full_path())))
        if read_only() and request.method not in ("GET", "HEAD", "OPTIONS") and not path.startswith(OPEN_PATHS):
            return _page(request, "read_only", 403)
        return self.get_response(request)

    def process_exception(self, request, exception):
        if ON and _is_db_read_only(exception):
            return _page(request, "read_only", 403)
        return None


def context(request):
    """For base.html: the banner of the version under test."""
    if not ON:
        return {}
    from .ha_agent import commit, version
    return {"test_mount": True, "test_mount_read_only": read_only(), "test_mount_version": version(),
            "test_mount_commit": commit()[:12]}


def setup():
    """At start (portal/wsgi_test.py): on the standby the sign-in writes nothing (last sign-in)."""
    if not ON:
        return
    from django.contrib.auth.models import update_last_login
    from django.contrib.auth.signals import user_logged_in

    def maybe_update_last_login(sender, user, **kwargs):
        if not read_only():
            update_last_login(sender, user, **kwargs)

    user_logged_in.disconnect(update_last_login, dispatch_uid="update_last_login")
    user_logged_in.connect(maybe_update_last_login, dispatch_uid="update_last_login_test_mount", weak=False)
