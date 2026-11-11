"""Replacing and archiving secrets, with checks on the systems that use them.

Replace: the contributors of a secret are pointed to another secret of the same type (in the portal's records), the old
one becomes "replaced", and a task checks every former contributor: the new secret works there and the old one does
not. When every check passes the old secret is archived; otherwise it stays "replaced" (still in use somewhere, or
not checkable automatically) until it is archived by hand, which checks again.

What can be checked automatically:
    OpenNebula front end      XML-RPC as oneadmin with the new password works, with the old one is refused
    jump host, super admins   ssh with the user's new key works, with the old key is refused (front end VIP)
    IPMI                      Redfish login with the new password works, with the old one is refused
    BGP peer groups           not checkable from the portal (the routers' sessions): confirm by hand
The portal does not change secrets on the systems: that is done there first, then the secret is replaced here.
"""
from django.utils import timezone

from accounts.models import audit

from . import credentials as cr
from .models import Secret


def _xmlrpc(platform_key, password):
    from adoption.collector import OneClient
    from .models import PlatformControl
    c = PlatformControl.objects.filter(key=platform_key).first()
    url = c.endpoint_url if c else ""
    if not url:
        raise RuntimeError("no endpoint")
    OneClient(url, f"oneadmin:{password}")._call("one.user.info", -1, False)


def _ssh(host, port, user, key, site):
    from .sshexec import run
    run(host, port, user, key, "true", site=site, timeout=30)


def _redfish(host, user, password, port=443):
    from .redfish import Client
    Client(host, user, password, port=port)._req("GET", "/redfish/v1/Systems")


def _probe(fn):
    try:
        fn()
        return True, ""
    except Exception as exc:  # noqa: BLE001 - the outcome of a check
        return False, str(exc)[:200]


def check_one(label, old, new):
    """(status, detail) for one former contributor: ok, old-works, new-fails, manual, error."""
    from .models import Host, Platform, Site
    parts = label.split(" ", 2)
    try:
        if old.kind == Secret.Kind.ONE and label.startswith("platform ") and label.endswith("front end"):
            key = parts[1].rstrip(":")
            ok_new, why = _probe(lambda: _xmlrpc(key, new.get("one_password")))
            ok_old, _w = _probe(lambda: _xmlrpc(key, old.get("one_password")))
        elif old.kind == Secret.Kind.USER and label.startswith("site ") and label.endswith("jump host"):
            s = Site.objects.get(code=parts[1].rstrip(":"))
            j = s.jump or {}
            ok_new, why = _probe(lambda: _ssh(j["host"], int(j.get("port") or 22), new.username, new.get("private_key"), None))
            ok_old, _w = _probe(lambda: _ssh(j["host"], int(j.get("port") or 22), old.username, old.get("private_key"), None))
        elif old.kind == Secret.Kind.USER and label.startswith("platform ") and label.endswith("super admins"):
            p = Platform.objects.select_related("site").get(key=parts[1].rstrip(":"))
            ok_new, why = _probe(lambda: _ssh(p.fe_vip, 22, new.username, new.get("private_key"), p.site.code))
            ok_old, _w = _probe(lambda: _ssh(p.fe_vip, 22, old.username, old.get("private_key"), p.site.code))
        elif old.kind == Secret.Kind.USER and label.startswith("host ") and label.endswith("IPMI"):
            site, name = parts[1].rstrip(":").split(".", 1)
            h = Host.objects.get(site__code=site, name=name)
            ok_new, why = _probe(lambda: _redfish(h.ipmi_host, new.username, new.get("password"), h.ipmi_port))
            ok_old, _w = _probe(lambda: _redfish(h.ipmi_host, old.username, old.get("password"), h.ipmi_port))
        else:
            return "manual", "not checkable from the portal: confirm on the system"
    except Exception as exc:  # noqa: BLE001
        return "error", str(exc)[:200]
    if not ok_new:
        return "new-fails", why
    if ok_old:
        return "old-works", "the old secret is still accepted"
    return "ok", ""


def run_checks(task, old):
    """The checks of a replaced secret's former contributors; archives it when every one passes."""
    new = old.replaced_by
    labels = list((old.extra or {}).get("replaced_contributors") or [])
    results = {}
    for label in labels:
        status, detail = check_one(label, old, new) if new is not None else ("error", "no new secret")
        results[label] = {"status": status, "detail": detail}
        task.step(label, status, detail)
    old.refresh_from_db()
    old.extra = {**(old.extra or {}), "checks": results, "checked_at": timezone.now().isoformat()}
    old.save(update_fields=["extra"])
    if labels and all(r["status"] == "ok" for r in results.values()):
        archive(old, None, note="every former contributor checked")
    return {"checked": len(results), "ok": sum(1 for r in results.values() if r["status"] == "ok"),
            "archived": old.state == Secret.State.ARCHIVED}


def replace(old, new, user=None):
    """Point every contributor of `old` to `new`, mark `old` replaced and start the checks. Returns the task."""
    from accounts import tasks
    if new.kind != old.kind or new.pk == old.pk or new.state != Secret.State.ACTIVE:
        raise ValueError("choose another active secret of the same type")
    labels = cr.repoint(old, new, user)
    old.state, old.replaced_by = Secret.State.REPLACED, new
    old.extra = {**(old.extra or {}), "replaced_contributors": labels, "replaced_at": timezone.now().isoformat()}
    old.updated_by = user
    old.save()
    audit(user, "secret_replaced", old, kind=old.kind, by=new.name, contributors=len(labels))
    return tasks.start("secret check", f"{old.get_kind_display()} {old.name} → {new.name}",
                       lambda task: run_checks(task, old), user=user, target=old)


def archive(sec, user=None, note=""):
    sec.state, sec.archived_at, sec.archived_by = Secret.State.ARCHIVED, timezone.now(), user
    sec.save(update_fields=["state", "archived_at", "archived_by", "updated_at"])
    audit(user, "secret_archived", sec, kind=sec.kind, name=sec.name, note=note)
    return sec


def archive_blockers(sec):
    """Why a secret cannot be archived now: contributors in the records, or checks that found it still in use."""
    used = cr.contributors().get(sec.pk) or []
    if used:
        return [f"used by {len(used)}: replace it first"]
    bad = [k for k, v in ((sec.extra or {}).get("checks") or {}).items() if v.get("status") in ("old-works",)]
    if bad and sec.state == Secret.State.REPLACED:
        return [f"still accepted by {len(bad)}: {', '.join(bad[:3])}"]
    return []
