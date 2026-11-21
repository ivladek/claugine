"""Portal and platform consistency (owner 2026-11-11): checked at every platform refresh, shown on the platform's
details (Site → Platforms), repaired with one button.

Two rules:

    quotas      every active project on the platform has the quota the portal records (tenants.quota_check);
                repair: set the platform's quota from the portal (deploy.apply_quota) — the portal is the reference
    unbound     nothing on the platform without a project: a group is an active project's or the platform's own
                (PLATFORM_GROUPS) — a project on any platform: at1 and zk1 are one federation and see each other's
                groups; an archived project has nothing left there ("archived means deleted: the record
                stays in the portal as history, everything on the platform goes"). Repair: delete what an archived
                project left, with the deletion's own steps (tenants.undeploy) but without its portal step. A group the
                portal does not know at all is shown and never deleted by the repair: it may be someone's work.

    addresses   public and corporate addresses (owner 2026-11-11): every address range on the platform inside one of
                its public or corporate pools is in the address registry, assigned to the project whose group owns
                the network (or to the platform: services, reserved); no project's assignment overlaps another; an
                active project's assignment is used on its platform. Shown, never repaired: an address in use is
                fixed by hand. Platforms whose addresses are managed outside the portal (ext-managed) are skipped.

The result is kept on the PlatformControl (consistency): {"at", "quota": [...], "leftovers": [...], "unknown": [...],
"addresses": [...], "actions": [...], "error", "repair": {...}}.
"""

import fcntl
import ipaddress
import os
import threading

from django.utils import timezone

PLATFORM_GROUPS = {"oneadmin", "users", "shared", "retired"}
NOT_OURS = {"ext-managed", "platform"}  # platforms whose addresses the portal does not manage
PROJECT_KINDS = ("inet_main", "inet_extra", "corpnet")  # a project's own subnets, used on its platform


def _nets(ip, size):
    try:
        first = ipaddress.ip_address(ip)
    except ValueError:
        return []
    return list(ipaddress.summarize_address_range(first, first + max(int(size or 0), 1) - 1))


def _overlaps(a, b):
    return a.version == b.version and (a.subnet_of(b) or b.subnet_of(a))


def addresses(control, vnpool_xml):
    """[{"what", "cidr", "network", "group", "project"}] — the public and corporate addresses of one platform against
    the address registry. what: unregistered | other_owner | archived | overlap | unused."""
    import xml.etree.ElementTree as ET

    from inventory.models import Platform

    from .models import Lifecycle, PrefixAssignment

    plat = Platform.objects.select_related("site").filter(key=control.key).first()
    if plat is None or plat.kind in NOT_OURS:
        return []
    pools = [
        ipaddress.ip_network(d.cidr, strict=False) for d in plat.delegated_subnets() if d.purpose in ("inet", "corpnet")
    ]
    if not pools:
        return []
    site = plat.site.code
    reg = [
        (ipaddress.ip_network(a.cidr, strict=False), a)
        for a in PrefixAssignment.active().filter(site=site).select_related("tenant")
    ]
    out, used = [], set()
    for v in ET.fromstring(vnpool_xml).findall("VNET"):
        vname, group = v.findtext("NAME") or "", v.findtext("GNAME") or ""
        for ar in v.findall("AR_POOL/AR"):
            for net in _nets(ar.findtext("IP"), ar.findtext("SIZE")):
                if not any(_overlaps(net, p) for p in pools):
                    continue
                cover = [(n, a) for n, a in reg if _overlaps(net, n)]
                row = {"cidr": str(net), "network": vname, "group": group, "project": ""}
                if not cover:
                    out.append({**row, "what": "unregistered"})
                    continue
                for _n, a in cover:
                    used.add(a.pk)
                    t = a.tenant
                    if t is None:
                        continue  # the platform's own (services, reserved)
                    row["project"] = t.one_group_name or t.name
                    if t.status == Lifecycle.ARCHIVED:
                        out.append({**row, "what": "archived"})
                    elif group and t.one_group_name and group != t.one_group_name:
                        out.append({**row, "what": "other_owner"})
    projects = [(n, a) for n, a in reg if a.tenant_id]
    for i, (n1, a1) in enumerate(projects):
        for n2, a2 in projects[i + 1 :]:
            if _overlaps(n1, n2) and a1.tenant_id != a2.tenant_id:
                out.append(
                    {
                        "what": "overlap",
                        "cidr": f"{a1.cidr} / {a2.cidr}",
                        "network": "",
                        "group": "",
                        "project": f"{a1.tenant.one_group_name or a1.tenant.name} / {a2.tenant.one_group_name or a2.tenant.name}",
                    }
                )
    for _n, a in projects:
        t = a.tenant
        if (
            a.pk in used
            or a.kind not in PROJECT_KINDS
            or t.status == Lifecycle.ARCHIVED
            or t.one_group_id is None
            or (t.order or {}).get("platform") != control.key
        ):
            continue
        out.append(
            {"what": "unused", "cidr": a.cidr, "network": "", "group": "", "project": t.one_group_name or t.name}
        )
    return out


def _active(control_key):
    from .models import Approval, Lifecycle, Tenant

    return [
        t
        for t in Tenant.objects.exclude(status=Lifecycle.ARCHIVED).filter(approval=Approval.APPROVED)
        if (t.order or {}).get("platform") == control_key
    ]


def _archived(control_key):
    from .models import Lifecycle, Tenant

    return [
        t for t in Tenant.objects.filter(status=Lifecycle.ARCHIVED) if (t.order or {}).get("platform") == control_key
    ]


def check(control, one=None):
    """Check one platform and keep the result on it. Read only. Returns the result."""
    import xml.etree.ElementTree as ET

    from adoption.collector import client_for

    from . import quota_check
    from .undeploy import POOLS, names_of

    res = {
        "at": timezone.now().isoformat(),
        "quota": [],
        "leftovers": [],
        "unknown": [],
        "addresses": [],
        "actions": [],
        "error": "",
        "repair": (control.consistency or {}).get("repair") or {},
    }
    try:
        one = one or client_for(control)
        active = _active(control.key)
        for t in active:
            if t.one_group_id is None:
                continue
            status, rows = quota_check.compare(t)
            if status == "diff":
                res["quota"].append(
                    {
                        "tenant": t.pk,
                        "group": t.group_name,
                        "title": t.title,
                        "rows": [[w, str(p), str(q)] for w, p, q in rows],
                    }
                )
        groups = {
            g.findtext("NAME"): int(g.findtext("ID"))
            for g in ET.fromstring(one._call("one.grouppool.info")).findall("GROUP")
        }
        users = ET.fromstring(one._call("one.userpool.info")).findall("USER")
        pools = {"vms": ET.fromstring(one._call("one.vmpool.info", -2, -1, -1, -1)).findall("VM")}
        for key, pool, tag, _d, _x in POOLS:
            try:
                pools[key] = ET.fromstring(one._call(pool, -2, -1, -1)).findall(tag)
            except Exception:  # noqa: BLE001 - a pool this OpenNebula does not have
                pools[key] = []
        # at1 and zk1 are one federation: every project's group is seen on both, so a group is known when a project
        # on any platform has it (found on production, 2026-11-11, before release)
        from .models import Approval, Lifecycle, Tenant

        everywhere = Tenant.objects.exclude(approval=Approval.REJECTED).exclude(group_name="")
        known = {t.group_name for t in everywhere} | {
            t.one_group_name for t in everywhere if t.status != Lifecycle.ARCHIVED
        }
        archived = {t.group_name: t for t in _archived(control.key)}
        for t in archived.values():
            gid = groups.get(t.group_name)
            uids = {u.findtext("ID") for u in users if gid is not None and u.findtext("GID") == str(gid)}
            named = names_of(t)
            counts = {}
            for key, items in pools.items():
                n = sum(
                    1
                    for el in items
                    if (key != "vms" or el.findtext("STATE") != "6")
                    and (
                        (gid is not None and el.findtext("GID") == str(gid))
                        or el.findtext("UID") in uids
                        or named.match(el.findtext("NAME") or "")
                    )
                )
                if n:
                    counts[key] = n
            if uids:
                counts["users"] = len(uids)
            if gid is not None:
                counts["group"] = 1
            if counts:
                res["leftovers"].append({"tenant": t.pk, "group": t.group_name, "title": t.title, "counts": counts})
        for name, gid in sorted(groups.items()):
            if name in PLATFORM_GROUPS or name in known:
                continue
            n = sum(1 for items in pools.values() for el in items if el.findtext("GID") == str(gid))
            res["unknown"].append({"group": name, "id": gid, "objects": n})
        res["addresses"] = addresses(control, one._call("one.vnpool.info", -2, -1, -1))
    except Exception as exc:  # noqa: BLE001 - shown on the platform
        res["error"] = f"{type(exc).__name__}: {exc}"[:500]
    for q in res["quota"]:
        res["actions"].append(f"set the quota of {q['group']} on the platform from the portal")
    for x in res["leftovers"]:
        res["actions"].append(
            f"delete what the archived project {x['group']} left: "
            + ", ".join(f"{v} {k}" for k, v in x["counts"].items())
        )
    return _store(control, result=res)


def _store(control, result=None, repair=None):
    """commit-045: the consistency is written under the control's row lock, read again there - a check's result keeps
    the repair's mark as the database has it (a repair may have started since this control was read: its "running"
    mark is what the maintenance gate counts), a repair's mark keeps the latest result."""
    from django.db import transaction

    from inventory.models import PlatformControl

    with transaction.atomic():
        c = PlatformControl.objects.select_for_update().get(pk=control.pk)
        cur = c.consistency or {}
        new = dict(result) if result is not None else dict(cur)
        new["repair"] = repair if repair is not None else (cur.get("repair") or {})
        c.consistency = new
        c.save(update_fields=["consistency"])
    control.consistency = new
    return new


def ok(control):
    c = control.consistency or {}
    return bool(c.get("at")) and not (
        c.get("quota") or c.get("leftovers") or c.get("unknown") or c.get("addresses") or c.get("error")
    )


def repair(control_pk, user_id=None):
    """Run the planned actions of the last check, then check again. Never raises; the result is kept on the control."""
    from accounts.models import User, audit
    from inventory.models import PlatformControl

    from . import deploy
    from .models import Tenant
    from .undeploy import Undeployer

    control = PlatformControl.objects.get(pk=control_pk)
    user = User.objects.filter(pk=user_id).first() if user_id else None
    c = control.consistency or {}
    done = []
    for q in c.get("quota") or []:
        t = Tenant.objects.filter(pk=q["tenant"]).first()
        if t is None:
            continue
        try:
            done.append(f"{t.group_name}: quota {deploy.apply_quota(t, user)}")
        except Exception as exc:  # noqa: BLE001 - reported
            done.append(f"{t.group_name}: quota failed: {exc}"[:300])
    for x in c.get("leftovers") or []:
        t = Tenant.objects.filter(pk=x["tenant"]).first()
        if t is None:
            continue
        u = Undeployer(t, user)
        if not u.live:
            done.append(f"{t.group_name}: not deleted: the platform is read-only")
            continue
        try:
            plan = u.inventory()
            u.connect()
            for name, fn in (
                ("vms", u.vms_step),
                ("resources", u.pools_step),
                ("verify", u.verify_step),
                ("acl", u.acl_step),
                ("users", u.users_step),
                ("group", u.group_step),
                ("check", u.check_step),
            ):
                u.step(name, lambda f=fn, p=plan: f(p))
            done.append(f"{t.group_name}: what it left is deleted")
        except Exception as exc:  # noqa: BLE001 - reported
            done.append(f"{t.group_name}: deletion failed: {exc}"[:300])
    audit(user, "platform_consistency_repaired", None, platform=control.key, actions="; ".join(done)[:1000])
    from adoption.collector import collect

    try:
        if control.collect_enabled:
            collect(control)
    except Exception:  # noqa: BLE001 - the check below reads the platform itself
        pass
    control.refresh_from_db()
    _store(control, repair={"state": "done", "at": timezone.now().isoformat(), "done": done})
    check(control)
    return done


#: core-050, commit-058 (twenty-fifth full review): a running repair holds an exclusive lock on a file named after its
#: attempt, in the portal's data folder (the one above MEDIA_ROOT: www-data's, which root's HA agent reads too). The
#: lock goes when the thread ends - however it ends - so any process on the node can tell that a repair whose mark
#: still says running has stopped (accounts.tasks.reap_repairs), even when its last write failed.


def _lock_path(attempt):
    from django.conf import settings

    return os.path.join(os.path.dirname(str(settings.MEDIA_ROOT).rstrip("/")), "run", f"repair-{attempt}.lock")


def _hold(attempt):
    """The attempt's lock, taken: returns its descriptor (closing it releases the lock)."""
    path = _lock_path(attempt)
    os.makedirs(os.path.dirname(path), mode=0o750, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except BaseException:  # commit-068: no descriptor or file left behind
        _release(attempt, fd)
        raise
    return fd


def _release(attempt, fd):
    try:
        os.unlink(_lock_path(attempt))  # first: a reaper that finds no file knows the attempt ended
    except OSError:
        pass
    os.close(fd)


def attempt_running(attempt):
    """True while the repair attempt's thread runs on this node; False once it ended (its lock free or gone)."""
    if not attempt or not attempt.isalnum():
        return False
    try:
        fd = os.open(_lock_path(attempt), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    except OSError:
        return True  # cannot tell: left to the process check and the stale rule
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


REPAIR_FAILED = "the repair stopped: {}; run Make consistent again"


def end_failed_repair(control_pk, runner, attempt, why):
    """Mark the repair failed - only the attempt that ran (a later repair keeps its mark). Returns True if marked."""
    from django.db import transaction

    from inventory.models import PlatformControl

    with transaction.atomic():
        c = PlatformControl.objects.select_for_update().filter(pk=control_pk).first()
        cur = ((c.consistency or {}) if c else {}).get("repair") or {}
        if c is None or cur.get("state") != "running" or (cur.get("runner"), cur.get("attempt")) != (runner, attempt):
            return False
        c.consistency = {
            **c.consistency,
            "repair": {"state": "failed", "at": timezone.now().isoformat(), "done": [REPAIR_FAILED.format(why)[:300]]},
        }
        c.save(update_fields=["consistency"])
        return True


def _run_repair(control_pk, user_id, runner, attempt):
    """repair() itself never raises once it runs; what fails around it (the database away for a moment while it
    loads the platform or writes the audit) ends the marker here, so the button and maintenance are not held."""
    import logging
    import time

    try:
        return repair(control_pk, user_id)
    except Exception as exc:  # noqa: BLE001 - recorded on the platform
        why = f"{type(exc).__name__}: {exc}"
        for wait in (0, 5, 30):
            time.sleep(wait)
            try:
                end_failed_repair(control_pk, runner, attempt, why)
                break
            except Exception:  # noqa: BLE001 - the reaper ends it (the attempt's lock is gone)
                logging.getLogger(__name__).exception("consistency repair: recording its failure")
        return [REPAIR_FAILED.format(why)[:300]]


def start_repair(control, user=None):
    import uuid

    from django.conf import settings
    from django.db import connection

    from accounts import maintenance
    from inventory.models import PlatformControl

    from .models import current_runner

    # core-046 (reopened, twenty-fourth full review): admitted only while no maintenance runs, with its process (counted
    # by accounts.tasks.running_count, reaped when that process ends); one repair at a time, under the control's lock
    runner, attempt = current_runner(), uuid.uuid4().hex
    fd = _hold(attempt)  # before the mark: a reaper that reads the mark finds the attempt running
    # commit-067: released once - here when the admission fails, below when another repair already runs (only after
    # the admission's transaction ended well)
    try:
        with maintenance.admitting():
            c = PlatformControl.objects.select_for_update().get(pk=control.pk)
            other = ((c.consistency or {}).get("repair") or {}).get("state") == "running"
            if not other:
                c.consistency = {
                    **(c.consistency or {}),
                    "repair": {
                        "state": "running",
                        "at": timezone.now().isoformat(),
                        "runner": runner,
                        "attempt": attempt,
                    },
                }
                c.save(update_fields=["consistency"])
    except BaseException:
        _release(attempt, fd)
        raise
    if other:
        _release(attempt, fd)
        return None
    control.consistency = c.consistency
    user_id = getattr(user, "pk", None)
    if not getattr(settings, "DEPLOY_ASYNC", True):
        try:
            return _run_repair(control.pk, user_id, runner, attempt)
        finally:
            _release(attempt, fd)

    def worker():
        try:
            _run_repair(control.pk, user_id, runner, attempt)
        finally:
            _release(attempt, fd)
            connection.close()

    try:
        threading.Thread(target=worker, name=f"consistency-{control.key}", daemon=True).start()
    except Exception as exc:
        _release(attempt, fd)
        end_failed_repair(control.pk, runner, attempt, f"{type(exc).__name__}: {exc}")
        raise
    return None
