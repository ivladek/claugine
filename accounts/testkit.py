"""Helpers the test modules share: a node.env on disk, a staff user, a verified OTP session.

Public on purpose (2026-11-09, Phase 4): these are used by tests of **both** editions, so they cannot live in an
Enterprise test module — `ee/tests_ha.py` imports them from here, and so do `accounts/tests_updates.py` and
`accounts/tests_backups_s3.py`. They were in `tests_ha.py` until the split made that an Enterprise-only file.

Not imported by the portal itself; it is test scaffolding that happens to ship with the source.
"""

import os
import tempfile

from django.test import override_settings

from accounts import thisnode


class NodeFile:
    """A node.env on disk, pointed at by NODE_ENV_FILE for the duration of the `with`."""

    def __init__(self, **values):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "node.env")
        thisnode.write(values, file=self.path)

    def __enter__(self):
        self.ov = override_settings(NODE_ENV_FILE=self.path)
        self.ov.enable()
        thisnode.forget()
        return self

    def __exit__(self, *a):
        self.ov.disable()
        thisnode.forget()

    def set(self, **values):
        thisnode.write(values, file=self.path)
        thisnode.forget()


def staff(email="sa@corp.kz", **kw):
    """A staff user with two-factor already set up (every staff sign-in needs it)."""
    from accounts.models import Kind, User

    u = User.objects.create_user(email, "Str0ng-PASS-123", kind=Kind.STAFF, email_verified=True, **kw)
    enrol(u)
    return u


def enrol(user):
    """Two-factor set up for `user`: a confirmed authenticator. Returns the device (accounts.utils.totp_code)."""
    from django_otp.plugins.otp_totp.models import TOTPDevice

    d = TOTPDevice.objects.create(user=user, name="authenticator", confirmed=True, tolerance=1)
    user.totp_enabled = True
    user.save(update_fields=["totp_enabled"])
    return d


def otp(client):
    """Mark this client's session as having passed the second factor."""
    s = client.session
    s["otp_verified"] = True
    s.save()


class FakeNode:
    """The other node as the agent sees it: its role, version, replication and the calls made to it.

    A test double, not Enterprise logic — `accounts/tests_updates.py` needs it as much as `ee/tests_ha.py`
    does, and an import of `ee` from public code is what the publish guard refuses (2026-11-09).
    """

    def __init__(self, role="standby", version="v1"):
        self.role, self.version, self.calls, self.applied = role, version, [], True

    def request(self, addr, method, path, body=None, raw=False, timeout=60):
        self.calls.append((method, path, body))
        if path == "/status":
            return {
                "role": self.role,
                "site": "dc2",
                "version": self.version,
                "db": {"replica": {"io": "Yes", "sql": "Yes"} if self.role == "standby" else {}},
            }
        if path == "/role":
            self.role = body["role"]
            return {"result": f"role {self.role}"}
        if path == "/wait-gtid":
            return {"ok": self.applied}
        return {"result": f"{path} ok"}
