"""Retiring a KVM host (owner 2026-11-20 10:15-10:17): the host leaves its platform the way an operator would take it
out - OpenNebula moves its VMs away first, the run stops at the first VM that does not move, and only an empty host is
deleted from OpenNebula and powered off. Super admins only; a run of its own (HostDeployment kind "retire"), its steps
shown on the host like a deployment's, resumed from the failed step.

    R0 checks       the host is in OpenNebula, in its cluster; another host of the cluster is enabled; the host's VMs
                    can be moved (running or powered off) and fit, together, in what the other hosts have free; the
                    iLO can be reached to power it off at the end
    R1 disable      the host disabled in OpenNebula: nothing new is placed on it ("onehost flush" starts so)
    R2 migrate      each VM in turn: a running one live-migrated, a powered-off one moved cold, to the enabled host of
                    the cluster with the most free memory that fits it; waited for until it runs (or is powered off)
                    on that host; the run stops at the first that does not ("stop if unsuccessfull"). OpenNebula's
                    flush leaves the placement to its scheduler, which does not place anything while OneDRS is manual -
                    so the run places each VM itself
    R3 empty        OpenNebula shows no VM on the host at all
    R4 delete       the host deleted from OpenNebula
    R5 power off    the server shut down gracefully through its iLO, forced after 10 minutes, checked off
    R6 records      the host retired in the portal

The host's local storage (commit-207; owner 2026-11-20 10:38-10:39): the OpenNebula datastores the portal places on
the host's local storage, the images in them and the VMs with a disk there (they cannot be moved from it) are listed
before the confirmation, and deleted - with no way to restore them - only when confirmed: the VMs right after R1
(R1a), the images and the datastores once the host is empty (R3a, R3b), the records with the host (R6). The run
deletes exactly what was confirmed: anything else found there stops it.
"""

import time

from django.db import transaction
from django.utils import timezone

from accounts.models import audit

from . import ilo
from .hostdeploy import DeployError, Run, _claim_fresh, _one, _one_host_id, _still_mine, mark_interrupted
from .models import HostDeployment

PHASES = [
    ("R0", "checks", "Checks"),
    ("R1", "disable", "Disabled in OpenNebula"),
    ("R1a", "wipe_vms", "VMs on the local storage deleted"),
    ("R2", "migrate", "VMs moved to the other hosts"),
    ("R3", "empty", "No VM left on the host"),
    ("R3a", "wipe_images", "Images on the local storage deleted"),
    ("R3b", "wipe_datastores", "Local datastores deleted"),
    ("R4", "one_delete", "Deleted from OpenNebula"),
    ("R5", "power_off", "Powered off"),
    ("R6", "records", "Records"),
]
STEP_KEYS = [k for _p, k, _l in PHASES]
MIGRATE_LIMIT = 1800  # seconds one VM may take to move
SHUTDOWN_LIMIT = 600  # seconds for a graceful shutdown before the power is cut
POLL = 10

RUNNING, POWEROFF, DONE = "3", "8", "6"  # VM STATE: ACTIVE, POWEROFF, DONE
LCM_RUNNING = "3"


def _vm(one, vid):
    """(state, lcm_state, host id, cpu x100, memory KB) of a VM."""
    v = one.xml("one.vm.info", vid)
    hist = v.findall("HISTORY_RECORDS/HISTORY")
    hid = hist[-1].findtext("HID") if hist else None
    cpu = float(v.findtext("TEMPLATE/CPU") or 0)
    mem = int(v.findtext("TEMPLATE/MEMORY") or 0)
    return v.findtext("STATE"), v.findtext("LCM_STATE"), hid, int(round(cpu * 100)), mem * 1024


def _one_of(cluster):
    """OpenNebula of the cluster's platform (the confirmation page reads it before any run)."""
    from tenants.deploy import One

    from .models import PlatformControl

    c = PlatformControl.objects.filter(key=cluster.platform.key).first()
    if c is None:
        raise DeployError(f"{cluster.platform.key}: no endpoint")
    return One(c)


def foreign(h, cluster):
    """commit-207: the host's storage used by a datastore of another platform - the run cannot see or delete it, so
    the host is not retired while it is there."""
    from .models import PlatformDatastore

    return [
        f"{d.platform.key}/{d.name}"
        for d in PlatformDatastore.objects.filter(volume__storage__host=h)
        .exclude(platform=cluster.platform)
        .select_related("platform")
    ]


def local_items(one, h, cluster):
    """commit-207: what lives on the host's local storage - {"datastores", "images", "vms"}, each [(id, name, extra)]:
    the OpenNebula datastores of the platform on a volume of the host's storage (by name), the images in them, and
    every VM (on any host) with a disk in them or its system datastore there; "key" names them all, to compare the
    confirmed list with the one found when the run deletes them."""
    from .models import PlatformDatastore

    names = set(
        PlatformDatastore.objects.filter(platform=cluster.platform, volume__storage__host=h).values_list(
            "name", flat=True
        )
    )
    ds = [
        (int(d.findtext("ID")), d.findtext("NAME"), d.findtext("TYPE"))
        for d in one.xml("one.datastorepool.info").findall("DATASTORE")
        if d.findtext("NAME") in names
    ]
    ids = {str(i) for i, _n, _t in ds}
    images, vms = [], []
    if ids:
        for i in one.xml("one.imagepool.info", -2, -1, -1).findall("IMAGE"):
            if i.findtext("DATASTORE_ID") in ids:
                images.append((int(i.findtext("ID")), i.findtext("NAME"), i.findtext("DATASTORE")))
        for v in one.xml("one.vmpool.info", -2, -1, -1, -1).findall("VM"):
            hist = v.findall("HISTORY_RECORDS/HISTORY")
            disks = {d.findtext("DATASTORE_ID") for d in v.findall("TEMPLATE/DISK")}
            sysds = hist[-1].findtext("DS_ID") if hist else None
            if disks & ids or sysds in ids:
                vms.append((int(v.findtext("ID")), v.findtext("NAME"), hist[-1].findtext("HOSTNAME") if hist else ""))
    out = {"datastores": ds, "images": images, "vms": vms}
    out["key"] = ";".join(f"{k}:{','.join(str(x[0]) for x in sorted(out[k]))}" for k in ("datastores", "images", "vms"))
    return out


def _host_vms(one, hid):
    return [int(x.text) for x in one.xml("one.host.info", hid).findall("VMS/ID")]


def _free(one, cluster_name, exclude):
    """{host id: (free cpu, free memory KB)} of the enabled hosts of the cluster other than the one retired."""
    out = {}
    for h in one.xml("one.hostpool.info").findall("HOST"):
        if h.findtext("CLUSTER") != cluster_name or h.findtext("ID") == str(exclude) or h.findtext("STATE") != "2":
            continue
        s = h.find("HOST_SHARE")
        cpu = int(s.findtext("MAX_CPU") or 0) - int(s.findtext("CPU_USAGE") or 0)
        mem = int(s.findtext("MAX_MEM") or 0) - int(s.findtext("MEM_USAGE") or 0)
        out[h.findtext("ID")] = (cpu, mem)
    return out


def s_checks(r):
    one = _one(r)
    hid = _one_host_id(one, r.h.name)
    if hid is None:
        raise DeployError(f"OpenNebula has no host {r.h.name}")
    h = one.xml("one.host.info", hid)
    if h.findtext("CLUSTER") != r.cluster.name:
        raise DeployError(f"OpenNebula has {r.h.name} in cluster {h.findtext('CLUSTER')}, not {r.cluster.name}")
    free = _free(one, r.cluster.name, hid)
    if not free:
        raise DeployError(f"no other enabled host in {r.cluster.name} to take the VMs")
    other = foreign(r.h, r.cluster)
    if other:
        raise DeployError(
            "the host's storage is used by datastores of other platforms: "
            + ", ".join(other[:10])
            + " - nothing changed"
        )
    local = local_items(one, r.h, r.cluster)
    if local["key"] != (r.dep.data.get("wipe") or local_items_none()):
        raise DeployError(
            "what is on the host's local storage is not what was confirmed: nothing changed. Open Retire again to see "
            "it and confirm"
        )
    gone = {v for v, _n, _x in local["vms"]}
    vms = [v for v in _host_vms(one, hid) if v not in gone]
    stuck, sized = [], []
    for vid in vms:
        st, lcm, _h, cpu, mem = _vm(one, vid)
        if not ((st == RUNNING and lcm == LCM_RUNNING) or st == POWEROFF):
            stuck.append(f"#{vid} (state {st}/{lcm})")
        sized.append((vid, cpu, mem))
    if stuck:
        raise DeployError("VMs that are neither running nor powered off cannot be moved: " + ", ".join(stuck[:10]))
    # core-053: each VM placed on one host, in the order and by the rule of the migration, the room counted down
    room = dict(free)
    for vid, cpu, mem in sized:
        to = _pick(room, cpu, mem)
        if to is None:
            raise DeployError(
                f"VM #{vid} ({cpu // 100} CPU / {mem // 1048576} GB) fits on no single enabled host of "
                f"{r.cluster.name} once the VMs before it are placed: nothing changed"
            )
        c, m = room[to]
        room[to] = (c - cpu, m - mem)
    ilo.power_state(r.c)  # the iLO answers: the host can be powered off at the end
    r.keep(one_id=hid, vms=vms, local={k: local[k] for k in ("datastores", "images", "vms")})
    extra = (
        f"; to delete: {len(local['vms'])} VM(s), {len(local['images'])} image(s), "
        f"{len(local['datastores'])} datastore(s)"
        if local["datastores"]
        else ""
    )
    return f"OpenNebula host #{hid}; {len(vms)} VM(s) to move to {len(free)} host(s){extra}"


def local_items_none():
    return "datastores:;images:;vms:"


def _local(r, key):
    return [x[0] for x in ((r.dep.data.get("local") or {}).get(key) or [])]


def _wait_gone(r, look, what):
    waited = 0
    while not look():
        if waited >= MIGRATE_LIMIT:
            raise DeployError(f"{what}: still there after {MIGRATE_LIMIT // 60} minutes")
        r.sleep(POLL)
        waited += POLL


def _still_local(one, vid, ds):
    """commit-211: the VM's state, from one read that also shows it gone already or still with a disk (or its system
    datastore) on the local ones; a VM moved off them is never deleted - the run stops for a new confirmation."""
    v = one.xml("one.vm.info", vid)
    if v.findtext("STATE") == DONE:
        return DONE
    hist = v.findall("HISTORY_RECORDS/HISTORY")
    uses = {d.findtext("DATASTORE_ID") for d in v.findall("TEMPLATE/DISK")} | {
        hist[-1].findtext("DS_ID") if hist else None
    }
    if not uses & ds:
        raise DeployError(
            f"VM #{vid} no longer uses the host's local storage: it is not deleted. Abandon this run and open Retire "
            "again to confirm"
        )
    return v.findtext("STATE")


def s_wipe_vms(r):
    """R1a: the confirmed VMs on the local storage deleted (terminate-hard) - they cannot be moved from it."""
    one = _one(r)
    vids = _local(r, "vms")
    ds = {str(i) for i in _local(r, "datastores")}
    for vid in vids:  # commit-211: all read first - a change seen now deletes none
        _still_local(one, vid, ds)
    for vid in vids:
        # commit-211: each again right before its deletion (a later one may move) - the same read decides it
        if _still_local(one, vid, ds) != DONE:
            r.log(f"wipe_vms: VM #{vid} deleted (terminate-hard)")
            one.call("one.vm.action", "terminate-hard", vid)
        _wait_gone(r, lambda vid=vid: _vm(one, vid)[0] == DONE, f"VM #{vid}")
    return f"{len(vids)} VM(s) deleted" if vids else "no VM on the local storage"


def s_disable(r):
    one = _one(r)
    one.call("one.host.status", r.dep.data["one_id"], 1)
    return "disabled"


def _pick(free, cpu, mem):
    """The host with the most free memory that fits the VM (None: none fits). The checks and the migration use it."""
    fits = [(m, hid) for hid, (c, m) in free.items() if c >= cpu and m >= mem]
    return max(fits)[1] if fits else None


def _target(one, r, cpu, mem):
    to = _pick(_free(one, r.cluster.name, r.dep.data["one_id"]), cpu, mem)  # the room as it is now
    if to is None:
        raise DeployError(f"no enabled host of {r.cluster.name} has room for {cpu // 100} CPU / {mem // 1048576} GB")
    return to


def s_migrate(r):
    one = _one(r)
    hid = str(r.dep.data["one_id"])
    moved = list(r.dep.data.get("moved") or [])
    for vid in _host_vms(one, int(hid)):
        st, lcm, cur, cpu, mem = _vm(one, vid)
        if cur != hid:
            continue
        live = st == RUNNING
        if not (live and lcm == LCM_RUNNING) and st != POWEROFF:
            raise DeployError(f"VM #{vid} is neither running nor powered off (state {st}/{lcm}): not moved")
        to = _target(one, r, cpu, mem)
        r.log(f"migrate: VM #{vid} to host #{to} ({'live' if live else 'cold'})")
        one.call("one.vm.migrate", vid, int(to), live, True, -1, 0)  # core-054: OpenNebula enforces the room

        def look(vid=vid):
            return _vm(one, vid)[:3]

        def arrived(x, to=to, st=st):
            s, lc, h = x
            return h == to and s == st and (st == POWEROFF or lc == LCM_RUNNING)

        def back(x, st=st):
            s, lc, h = x
            return h == hid and s == st and (st == POWEROFF or lc == LCM_RUNNING)

        waited = 0
        while True:
            r.sleep(POLL)
            waited += POLL
            got = look()
            if arrived(got):
                break
            if back(got) and waited > POLL * 3:  # the migration ended where it began: it failed
                raise DeployError(f"VM #{vid} did not move to host #{to}; it stays on this host (OpenNebula's log)")
            if waited >= MIGRATE_LIMIT:
                raise DeployError(f"VM #{vid}: not on host #{to} after {MIGRATE_LIMIT // 60} minutes (state {got})")
        moved.append({"vm": vid, "to": to})
        r.keep(moved=moved)
        r.log(f"migrate: VM #{vid} on host #{to}")
    return f"{len(moved)} VM(s) moved" if moved else "no VM to move"


def _vms_again(r, left, what):
    """core-055: VMs found on the host after the migration step: that step runs again on Resume, with the VMs then."""
    r.mark("migrate", "pending", "VMs found on the host again: Resume moves them")
    return DeployError(f"{what}: " + ", ".join(f"#{v}" for v in left[:10]) + " - Resume moves them")


def s_empty(r):
    one = _one(r)
    left = _host_vms(one, r.dep.data["one_id"])
    if left:
        raise _vms_again(r, left, "VMs still on the host")
    return "no VM on the host"


def _pool_ids(one, method, tag, *args):
    return {int(x.findtext("ID")) for x in one.xml(method, *args).findall(tag)}


def _stray(r, one):
    """Anything on the local datastores that was not confirmed stops the run before it is deleted."""
    local = local_items(one, r.h, r.cluster)
    sure = {k: set(_local(r, k)) for k in ("datastores", "images", "vms")}
    extra = [f"{k[:-1]} #{x[0]}" for k in ("images", "vms") for x in local[k] if x[0] not in sure[k]]
    if extra:
        raise DeployError(
            "on the host's local storage and not confirmed: " + ", ".join(extra[:10]) + " - nothing deleted; abandon "
            "this run and open Retire again"
        )


def s_wipe_images(r):
    """R3a: the confirmed images in the local datastores deleted."""
    one = _one(r)
    ids = _local(r, "images")
    if ids:
        _stray(r, one)
    for iid in ids:
        if iid in _pool_ids(one, "one.imagepool.info", "IMAGE", -2, -1, -1):
            r.log(f"wipe_images: image #{iid} deleted")
            one.call("one.image.delete", iid, False)
        _wait_gone(
            r, lambda iid=iid: iid not in _pool_ids(one, "one.imagepool.info", "IMAGE", -2, -1, -1), f"image #{iid}"
        )
    return f"{len(ids)} image(s) deleted" if ids else "no image on the local storage"


def s_wipe_datastores(r):
    """R3b: the local datastores deleted from OpenNebula (empty by now)."""
    one = _one(r)
    ids = _local(r, "datastores")
    if ids:
        _stray(r, one)
    for did in ids:
        if did in _pool_ids(one, "one.datastorepool.info", "DATASTORE"):
            r.log(f"wipe_datastores: datastore #{did} deleted")
            one.call("one.datastore.delete", did)
    return f"{len(ids)} datastore(s) deleted" if ids else "no local datastore"


def s_one_delete(r):
    one = _one(r)
    hid = r.dep.data["one_id"]
    if _one_host_id(one, r.h.name) is None:
        return "already deleted"
    left = _host_vms(one, hid)
    if left:  # the last look, right before the delete
        raise _vms_again(r, left, "the host has VMs again, not deleted")
    one.call("one.host.delete", hid)
    return f"OpenNebula host #{hid} deleted"


def _may_cut(r, when):
    """core-051, core-056, commit-207, core-059: read right before each shutdown request - the host is not in
    OpenNebula again and no datastore (local or of another platform) uses its storage; a failed read stops the run."""
    one = _one(r)
    left = [f"#{i} {n}" for i, n, _t in local_items(one, r.h, r.cluster)["datastores"]] + foreign(r.h, r.cluster)
    if left:
        raise DeployError(f"{when}: datastores still use the host's storage: " + ", ".join(left) + " - not powered off")
    again = _one_host_id(one, r.h.name)
    if again is not None:
        raise DeployError(
            f"{when}: OpenNebula has a host {r.h.name} again (#{again}): it may be in service, so it is not powered "
            "off. Take it out of OpenNebula first, or abandon this run"
        )


def s_power_off(r):
    off = ilo.power_state(r.c) == "Off"
    _may_cut(r, "shutdown")  # commit-216, core-062: before "already off" too - an off host may be back in OpenNebula
    if off:
        return "already off"
    r.c.post(f"{ilo.SYS}/Actions/ComputerSystem.Reset", {"ResetType": "GracefulShutdown"})
    try:
        ilo._wait(lambda: ilo.power_state(r.c), "Off", SHUTDOWN_LIMIT, poll=POLL, sleep=r.sleep, what="shutdown")
        return "shut down"
    except ilo.IloError:
        pass
    if ilo.power_state(r.c) == "Off":  # core-056: the iLO read first, then OpenNebula, then the power cut at once
        return "shut down"
    try:
        _may_cut(r, "the graceful shutdown did not end")
    except DeployError as exc:
        raise DeployError(f"{exc}; the power is not cut") from None
    r.log(f"power_off: still on after {SHUTDOWN_LIMIT // 60} minutes - the power is cut")
    r.c.post(f"{ilo.SYS}/Actions/ComputerSystem.Reset", {"ResetType": "ForceOff"})
    ilo._wait(lambda: ilo.power_state(r.c), "Off", SHUTDOWN_LIMIT, poll=POLL, sleep=r.sleep, what="power Off")
    return "powered off (forced after the graceful shutdown did not end)"


def s_records(r):
    from . import hostsfile
    from .models import PlatformDatastore

    with transaction.atomic():
        hostsfile.lock([r.p])  # commit-541: no /etc/hosts write of the platform renders while it leaves
        r.h.state = "retired"
        # owner 2026-11-20 15:46: the health shown is never one from before the run - a retired host is no longer
        # read (hosthealth.due leaves it out), so its last reading is cleared
        r.h.health, r.h.health_at = {}, None
        r.h.save(update_fields=["state", "health", "health_at"])
        names = [x[1] for x in (r.dep.data.get("local") or {}).get("datastores") or []]
        n = PlatformDatastore.objects.filter(platform=r.p, name__in=names, volume__storage__host=r.h).delete()[0]
        r.h.storages.update(state="retired")
        hostsfile.refresh(r.dep.started_by, r.p, f"{r.h.name} retired")  # T36 B4c: its line leaves the others' file
    return "the host is retired" + (f"; {n} local datastore record(s) removed" if n else "")


STEPS = {k: globals()[f"s_{k}"] for k in STEP_KEYS}


def run(dep, sleep=time.sleep, claim=None):
    """Every step not done yet, in order; stops at the first that fails (resumed later from it)."""
    if claim is not None:
        with transaction.atomic():
            cur = HostDeployment.objects.select_for_update().get(pk=dep.pk)
            if (cur.data or {}).get("claim") != claim or cur.state != HostDeployment.State.RUNNING:
                return False
        dep = cur
    r = Run(dep, sleep=sleep)
    first = True
    for key in STEP_KEYS:
        if (dep.steps.get(key) or {}).get("status") == "done":
            continue
        if claim is not None and not _still_mine(dep, claim):
            r.log(f"{key}: not started - this run was taken over")
            return False
        r.mark(key, "running")
        r.log(f"{key}: started")
        try:
            if first:  # core-059: before the first step of every run, a resumed one too
                first = False
                other = foreign(r.h, r.cluster)
                if other:
                    raise DeployError(
                        "the host's storage is used by datastores of other platforms: "
                        + ", ".join(other[:10])
                        + " - nothing more is done"
                    )
            detail = STEPS[key](r) or ""
        except Exception as exc:  # noqa: BLE001 - the run stops here and says why
            r.mark(key, "failed", exc)
            r.log(f"{key}: FAILED: {getattr(exc, 'detail', '') or exc}")
            HostDeployment.objects.filter(pk=dep.pk).update(
                state=HostDeployment.State.FAILED, finished_at=timezone.now()
            )
            audit(dep.started_by, "host_retire_failed", dep.host, host=dep.host.name, step=key, error=str(exc)[:300])
            return False
        r.mark(key, "done", detail)
        r.log(f"{key}: {detail}")
    HostDeployment.objects.filter(pk=dep.pk).update(state=HostDeployment.State.DONE, finished_at=timezone.now())
    audit(dep.started_by, "host_retired", dep.host, host=dep.host.name, cluster=dep.cluster.name)
    return True


DELETING = ("wipe_vms", "wipe_images", "wipe_datastores", "one_delete", "power_off", "records")


def _deleting(dep):
    """core-060: the run has begun deleting (a deletion step started) - it is kept for Resume."""
    return any(((dep.steps or {}).get(k) or {}).get("status") for k in DELETING)


def start(user, h, cluster, wipe=None):
    """A new retirement, or the failed (or interrupted) one resumed. One run of a host at a time, of either kind."""
    import secrets as _secrets

    from accounts import ha, tasks

    from . import hostlogging
    from .models import Host

    ha.forbid_writes(f"{h.name} retirement")
    claim = _secrets.token_hex(8)
    with transaction.atomic():
        row = Host.objects.select_for_update().get(pk=h.pk)
        if hostlogging.claim_fresh(row.logging_claim):  # commit-270: Apply logging holds the host
            raise DeployError("Apply logging of this host is running: try again when it ends")
        if tasks.running("host deploy", h):  # core-052: a worker still at work, whatever its run's row says
            raise DeployError("a run of this host (deployment or retirement) is running")
        live = HostDeployment.objects.select_for_update().filter(host=h, state=HostDeployment.State.RUNNING).first()
        if live is not None:
            if _claim_fresh(live):
                raise DeployError("a run of this host (deployment or retirement) is running")
            mark_interrupted(live)
        dep = HostDeployment.objects.filter(
            host=h, state=HostDeployment.State.FAILED, kind=HostDeployment.Kind.RETIRE, cluster=cluster
        ).first()
        # commit-213: a run that has begun deleting is never resumed by a different confirmation - only by its own
        # Resume, the host page showing what it was confirmed to delete. commit-217, core-063: refused after this
        # transaction commits, so an interrupted run marked failed above stays failed (Resume and Abandon shown)
        refused = dep is not None and wipe is not None and wipe != (dep.data or {}).get("wipe") and _deleting(dep)
        if not refused:
            if dep is not None and wipe is not None and wipe != (dep.data or {}).get("wipe"):
                # core-058: a new confirmation that differs is a new run - its checks read what is there now; core-060:
                # only while the old run has deleted nothing - once it has, it is the run that finishes (Resume), its
                # rechecks before each deletion and the power-off still in place
                dep = None
            if dep is None:
                HostDeployment.objects.filter(host=h, state=HostDeployment.State.FAILED).update(
                    state=HostDeployment.State.ABANDONED, finished_at=timezone.now()
                )
                dep = HostDeployment.objects.create(
                    host=h, cluster=cluster, kind=HostDeployment.Kind.RETIRE, started_by=user
                )
            dep.state, dep.finished_at = HostDeployment.State.RUNNING, None
            dep.data = {**(dep.data or {}), "claim": claim, "claim_at": timezone.now().isoformat()}
            if wipe is not None and not _deleting(dep):  # commit-207: what was confirmed (kept once deleting began)
                dep.data["wipe"] = wipe
            dep.save(update_fields=["state", "finished_at", "data"])
            resumed = bool(dep.steps)

            def go():
                tasks.start(
                    "host deploy",  # the same task name: one run of a host at a time, of either kind
                    f"Retire {h.name} from {cluster.name}",
                    lambda t: {"ok": run(dep, claim=claim)},
                    user=user,
                    target=h,
                )

            transaction.on_commit(go)
    if refused:
        raise DeployError(
            "a retirement of this host has begun deleting with its own confirmed list: use Resume on the host "
            "page (it shows that list), or Abandon it"
        )
    audit(user, "host_retire_started", h, host=h.name, cluster=cluster.name, resumed=resumed)
    return dep


def steps_view(dep):
    out = []
    for phase, key, label in PHASES:
        s = (dep.steps or {}).get(key) or {}
        out.append((phase, key, label, s.get("status", ""), s.get("at", ""), s.get("detail", "")))
    return out
