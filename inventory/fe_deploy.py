"""The front end of a platform made again (T34 B6; owner 2026-11-21 13:15 "onefe - opennebula front end with mysql",
Q4 "now from local folder"; design.md, mockup 05).

A front-end VM is made like every base-OS VM (base_deploy: the old VM stepped aside and kept as -old, the new one from
the base-os image and the profile's recipe, rolled back by itself on a failure); after its first boot, before the
profile's checks, it gets its data:

    redeploy     one node, from the zone's leader: the leader's configuration (the folders of Settings → Backups →
                 Platform front ends, and fe_backup.EXTRA: firewall rules, MySQL settings, the oned unit's override,
                 the datastores' NFS mounts), its own SERVER_ID in oned.conf, the leader's database (onedb backup →
                 onedb restore); the leader resets the node's log (onezone server-reset); oned and the units enabled
                 on the leader start; ready when the node is a follower that has caught up with the leader's commit.
                 Only with the two other nodes serving (a leader and a follower). The leader itself: its oned is
                 stopped first and a new leader is waited for (the leadership moves), then it is made like a follower.
    restore_all  every node, from the platform's latest backup in the local folder (accounts/fe_backup.py): the three
                 old VMs powered off first (the old zone stops), each node from its own folder of the backup, every
                 node the backup's database; ready when the zone has a leader and every other node follows it. Any
                 failure: every new VM removed, every old VM back. Everything after the backup's time is lost;
                 Prometheus starts empty (owner Q5).
    rollback     by hand (Roll back): base_deploy's, then the old node is told to catch up from the leader.

The portal reaches the nodes' XML-RPC by their addresses (http://<NIC 1>:2633/RPC2) with the served platform's
endpoint credentials, and the nodes over ssh with their login (as Check logging does). Nothing is copied from the old
VM. Secrets stay on the portal and the nodes: the files go through a private temporary folder, removed at the end.
"""

import os
import re
import shlex
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from django.utils.translation import gettext as _

from .core_deploy import RedeployError
from .models import CoreService, PlatformControl

T = CoreService.Type
PORT = 2633
WORK = "/var/lib/claugine/fe"  # on the node: the files carried (root only), removed when done
LEADER_TIMEOUT = 180  # seconds for a new leader (ELECTION_TIMEOUT_MS is 15 s on at1)
SYNC_TIMEOUT = 1800  # seconds for a follower to catch up
UNITS = ("opennebula", "opennebula-fireedge", "opennebula-flow", "opennebula-gate", "opennebula-prometheus")
UNIT_RE = re.compile(r"^opennebula[a-z0-9-]*\.service$")
RESTORE_KIND = "core FE restore"  # the task of a Restore all (target: the service)
REDEPLOY_KIND = "core VM redeploy"  # the task of a node's redeploy, roll back, removal (target: the node)
START_WAIT = 60  # seconds for a started unit to be active
FENCE_TIMEOUT = 300  # seconds for a new VM to be confirmed gone (commit-345)


def _fenced(one, vm_id, timeout=None):
    """commit-345: until the platform says the VM is done (STATE 6), or answers this VM's own one.vm.info with
    OpenNebula's structured NO_EXISTS code (never a text match: an XML-RPC fault or a transport error carries no code).
    Any other answer - an RPC failure, a time-out, another state - is not proof: retried until the deadline, then
    RedeployError."""
    from adoption.collector import NO_EXISTS, OneError
    from tenants.deploy import DeployError

    timeout = FENCE_TIMEOUT if timeout is None else timeout
    end = time.monotonic() + timeout
    last = "no answer"
    while True:
        try:
            st = one.xml("one.vm.info", vm_id, False).findtext("STATE")
            if st == "6":
                return
            last = f"state {st}"
        except DeployError as exc:
            cause = exc.__cause__
            if isinstance(cause, OneError) and cause.method == "one.vm.info" and cause.code == NO_EXISTS:
                return
            last = str(exc)[:200]
        except ET.ParseError as exc:
            last = f"unreadable answer: {exc}"
        if time.monotonic() > end:
            raise RedeployError(f"#{vm_id} not confirmed gone in {timeout} s ({last})")
        time.sleep(_poll())


def pending(svc):
    """commit-350: the service's recovery record as stored now ({} = none)."""
    return CoreService.objects.filter(pk=svc.pk).values_list("recovery", flat=True).first() or {}


def guard(node):
    """commit-350: no node-level action on a front end while a Restore all is not rolled back completely."""
    svc = node.service
    if svc.stype == T.ONEFE and pending(svc):
        raise RedeployError(
            f"a Restore all of {svc.name} is not rolled back completely: Retry the rollback on its details first"
        )


def exclusive(task, svc, retry=False):
    """commit-344: one operation at a time on a front end - a node's redeploy or roll back and a Restore all each need
    the zone as they found it, so any other task of the same service (on any of its nodes) refuses this one. The views
    take the same claim (core_views._start, fe_restore_view); this is the check inside the task itself."""
    from django.db.models import Q

    from accounts.models import Task

    nodes = [str(i) for i in svc.nodes.values_list("pk", flat=True)]
    other = (
        Task.objects.filter(state__in=[Task.State.QUEUED, Task.State.RUNNING])
        .exclude(pk=getattr(task, "pk", None))
        .filter(
            Q(kind=REDEPLOY_KIND, target_type="coreservicenode", target_id__in=nodes)
            | Q(kind=RESTORE_KIND, target_type="coreservice", target_id=str(svc.pk))
        )
        .first()
    )
    if other is not None:
        raise RedeployError(f"another task of {svc.name} runs ({other.title}): one at a time on a front end")
    if not retry and pending(svc):  # commit-350
        raise RedeployError(
            f"a Restore all of {svc.name} is not rolled back completely: Retry the rollback on its details first"
        )


def _poll():
    from .base_deploy import _poll as p

    return p()


# --- the zone ------------------------------------------------------------------------------------------------------


def ip_of(node):
    return (node.ips or [node.address or ""])[0] or node.address or ""


def session_of(svc):
    """The served platform's XML-RPC credentials ("user:password")."""
    from .credentials import SecretsError

    c = PlatformControl.objects.filter(key=svc.serves.key).first() if svc.serves else None
    if c is None:
        raise RedeployError(f"{svc.serves.key if svc.serves else '?'}: no endpoint (Infrastructure → Platforms)")
    try:
        _url, auth = c.credentials()
    except SecretsError as exc:
        raise RedeployError(str(exc)) from exc
    if not auth or auth.endswith(":"):
        raise RedeployError(f"{c.key}: the endpoint has no credentials")
    return auth


def _client(addr, session):
    from adoption.collector import OneClient

    return OneClient(f"http://{addr}:{PORT}/RPC2", session)


def raft(addr, session):
    """One front end's own Raft state, by its own XML-RPC: {state, server_id, term, commit, index}, None when it does
    not answer."""
    from adoption.collector import RAFT_STATES, OneError

    try:
        x = ET.fromstring(_client(addr, session)._call("one.zone.raftstatus"))
    except (OneError, ET.ParseError):
        return None

    def n(tag):
        try:
            return int(x.findtext(tag) or -1)
        except ValueError:
            return -1

    return {
        "state": RAFT_STATES.get(x.findtext("STATE") or "", "unknown"),
        "server_id": n("SERVER_ID"),
        "term": n("TERM"),
        "commit": n("COMMIT"),
        "index": n("LOG_INDEX"),
    }


def zone(svc):
    """The zone of the platform served, read from its nodes: {session, nodes, states {node pk: raft or None}, leader
    (node or None), zone (id or None), servers [{id, name, addr}]} - the zone and its servers from the leader."""
    from adoption.collector import OneError

    session = session_of(svc)
    nodes = list(svc.nodes.order_by("pk"))
    states = {n.pk: raft(ip_of(n), session) for n in nodes}
    leader = next((n for n in nodes if (states[n.pk] or {}).get("state") == "leader"), None)
    out = {"session": session, "nodes": nodes, "states": states, "leader": leader, "zone": None, "servers": []}
    if leader is not None:
        cl = _client(ip_of(leader), session)
        try:
            cfg = ET.fromstring(cl._call("one.system.config"))
            out["zone"] = int(cfg.findtext("FEDERATION/ZONE_ID") or 0)
            z = ET.fromstring(cl._call("one.zone.info", out["zone"]))
        except (OneError, ET.ParseError) as exc:
            raise RedeployError(f"the zone from the leader {leader.name}: {exc}") from exc
        for s in z.findall("SERVER_POOL/SERVER"):
            out["servers"].append(
                {
                    "id": int(s.findtext("ID") or -1),
                    "name": s.findtext("NAME") or "",
                    "addr": urlsplit(s.findtext("ENDPOINT") or "").hostname or "",
                }
            )
    return out


def server_id(z, node):
    """The node's server id in the zone: by its address, else its host name; else what it says itself."""
    host = (node.hostname or node.name).split(".")[0]
    for s in z["servers"]:
        if s["addr"] and s["addr"] == ip_of(node):
            return s["id"]
    for s in z["servers"]:
        if s["name"] and s["name"].split(".")[0] == host:
            return s["id"]
    st = z["states"].get(node.pk)
    return st["server_id"] if st and st["server_id"] >= 0 else None


def _wait_leader(nodes, session, timeout):
    end = time.monotonic() + timeout
    while True:
        for n in nodes:
            if (raft(ip_of(n), session) or {}).get("state") == "leader":
                return n
        if time.monotonic() > end:
            return None
        time.sleep(_poll())


# --- what is offered (reads only the portal) ----------------------------------------------------------------------


def offer_extra(node):
    """What a front end's redeploy needs beyond any base-OS VM's ([] = complete)."""
    svc = node.service
    why = []
    if svc.serves is None:
        why.append(_("no platform served"))
        return why
    if svc.platform_id and svc.platform_id == svc.serves_id:
        why.append(_("its VMs run on the platform it serves: they must run on another one"))
    if not PlatformControl.objects.filter(key=svc.serves.key).exists():
        why.append(_("%(p)s has no endpoint (Infrastructure → Platforms)") % {"p": svc.serves.key})
    for n in svc.nodes.order_by("pk"):
        if not ip_of(n):
            why.append(_("%(n)s has no address") % {"n": n.name})
        elif not _login(n)[0]:
            why.append(_("%(n)s has no login with a key (its secret)") % {"n": n.name})
    return why


def restore_offer(svc):
    """(offered, [reasons], backup name or "") for Restore all from a backup; reads the portal and its local folder."""
    from . import base_deploy as bd

    why = []
    nodes = list(svc.nodes.order_by("pk"))
    if svc.stype != T.ONEFE or not nodes:
        return False, [_("not a front end with VMs")], ""
    for n in nodes:
        ok, w = bd.offer(n)
        why += [f"{n.name}: {x}" for x in w]
    if pending(svc):  # commit-350
        why.append(_("a Restore all is not rolled back completely: Retry the rollback"))
    name = latest_backup(svc)
    if not name:
        why.append(
            _("no backup of %(p)s in the local folder (Settings → Backups → Platform front ends)")
            % {"p": svc.serves.key if svc.serves else "?"}
        )
    return not why, why, name


# --- ssh ----------------------------------------------------------------------------------------------------------


def _login(node):
    from .credentials import account

    user, acc = account(node.secret)
    key = (acc or {}).get("key") or ""
    return (user, key) if user and "PRIVATE KEY" in key else ("", "")


def _ssh(node):
    from . import hostlogging

    return hostlogging.node_ssh(node)


def _fetch(node, remote, out, timeout=3600):
    from .sshexec import SshError, fetch

    user, key = _login(node)
    try:
        return fetch(node.address, node.ssh_port, user, key, remote, out, timeout=timeout)
    except SshError as exc:
        raise RedeployError(f"{node.name}: {exc}") from exc


def _push(node, path, name):
    from .sshexec import SshError, push

    user, key = _login(node)
    remote = f"sudo -n sh -c {shlex.quote(f'umask 077; install -d -m 0700 {WORK} && cat > {WORK}/{name}')}"
    try:
        push(node.address, node.ssh_port, user, key, remote, path)
    except SshError as exc:
        raise RedeployError(f"{node.name}: {exc}") from exc


def _run(node, script, timeout=600):
    from . import hostlogging
    from .sshexec import SshError

    try:
        return _ssh(node)(script, timeout=timeout) or ""
    except (hostlogging.LoggingError, SshError) as exc:
        raise RedeployError(f"{node.name}: {exc}") from exc


def _size(path):
    n = os.path.getsize(path)
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n} B"


# --- the data on a new node ---------------------------------------------------------------------------------------

# oned.conf's DB section to the MySQL statements that make its database and user (the values never leave the node)
DB_SQL = r"""import re, sys
t = open("/etc/one/oned.conf").read()
m = re.search(r"^DB\s*=\s*\[", t, re.M)
if not m:
    sys.exit("oned.conf has no DB section")
kv, pos = {}, m.end()
item = re.compile(r'\s*(\w+)\s*=\s*(?:"([^"]*)"|([^,\s\]]*))\s*([,\]])', re.S)
while True:
    x = item.match(t, pos)
    if not x:
        sys.exit("oned.conf: the DB section does not read")
    kv[x.group(1).upper()] = x.group(2) if x.group(2) is not None else x.group(3)
    pos = x.end()
    if x.group(4) == "]":
        break
if kv.get("BACKEND", "").lower() != "mysql":
    sys.exit("oned.conf: the database is not MySQL")
if kv.get("SERVER", "localhost") not in ("localhost", "127.0.0.1", ""):
    sys.exit("oned.conf: the database is not on the node")
q = lambda s: s.replace("\\", "\\\\").replace("'", "\\'")
u, p, d = kv.get("USER", "oneadmin"), kv.get("PASSWD", ""), kv.get("DB_NAME", "opennebula")
if not re.match(r"^[A-Za-z0-9_]+$", d):
    sys.exit("oned.conf: an unexpected database name")
print(f"CREATE DATABASE IF NOT EXISTS `{d}`;")
print(f"CREATE USER IF NOT EXISTS '{q(u)}'@'localhost' IDENTIFIED BY '{q(p)}';")
print(f"ALTER USER '{q(u)}'@'localhost' IDENTIFIED BY '{q(p)}';")
print(f"GRANT ALL PRIVILEGES ON `{d}`.* TO '{q(u)}'@'localhost';")
print("SET GLOBAL TRANSACTION ISOLATION LEVEL READ COMMITTED;")
"""


def install_script(server, units, fallback=()):
    """On the new node (root): oned stopped, the configuration unpacked (fstab: only its NFS lines are added, the
    mounts made), its SERVER_ID, the firewall rules carried (the recipe's own kept; `fallback` rules when none were
    carried), the database and its user from oned.conf, the database restored, the units enabled. Ends with
    "RULES carried" or "RULES fallback"."""
    stop = " ".join(UNITS)
    enable = " ".join(shlex.quote(u) for u in units)
    fb = "\n".join(f"  {r} >/dev/null" for r in fallback) or "  :"
    return f"""set -euo pipefail
W={WORK}
systemctl stop {stop} 2>/dev/null || true
ufw show added 2>/dev/null | grep '^ufw ' > $W/own.rules || true
tar -xzpf $W/config.tar.gz -C / --exclude=etc/fstab
tar -xzOf $W/config.tar.gz etc/fstab 2>/dev/null | awk '$1 !~ /^#/ && $3 ~ /^nfs/' > $W/nfs.lines || true
while read -r what where rest; do
  [ -n "$where" ] || continue
  awk -v w="$where" '$1 !~ /^#/ && $2 == w {{f=1}} END {{exit !f}}' /etc/fstab || echo "$what $where $rest" >> /etc/fstab
  install -d -o oneadmin -g oneadmin "$where"
done < $W/nfs.lines
systemctl daemon-reload
if [ -s $W/nfs.lines ]; then mount -a -t nfs,nfs4 </dev/null; fi
sed -i -E '/^FEDERATION *=/,/\\]/ s/^([[:space:]]*SERVER_ID[[:space:]]*=[[:space:]]*)-?[0-9]+/\\1{int(server)}/' /etc/one/oned.conf
awk '/^FEDERATION *=/{{f=1}} f && /SERVER_ID/ {{print; exit}}' /etc/one/oned.conf | grep -qE '=[[:space:]]*{int(server)}([^0-9]|$)'
if tar -tzf $W/config.tar.gz etc/ufw/user.rules >/dev/null 2>&1; then
  ufw reload >/dev/null </dev/null
  while read -r rule; do eval "$rule" >/dev/null </dev/null || true; done < $W/own.rules
  echo "RULES carried"
else
{fb}
  echo "RULES fallback"
fi
systemctl restart mysql
python3 - <<'CLAUGINE_DB' > $W/db.sql
{DB_SQL}CLAUGINE_DB
mysql < $W/db.sql
rm -f $W/db.sql
F=/var/lib/one/.claugine-onedb.sql
gunzip -c $W/onedb.sql.gz > $F
chown oneadmin: $F; chmod 0600 $F
sudo -u oneadmin -H onedb restore -f $F >/dev/null </dev/null
rm -f $F
systemctl enable {enable} >/dev/null 2>&1
"""


def start_script(units, work=WORK, wait=START_WAIT):
    """commit-349: the units started, then each must be active (within START_WAIT s) - the first that is not ends the
    script with its name; only then are the carried files removed."""
    names = " ".join(shlex.quote(u) for u in units)
    tries = max(1, wait // 2)
    return f"""set -euo pipefail
systemctl start {names}
for u in {names}; do
  for i in $(seq {tries}); do systemctl is-active --quiet "$u" && break; sleep 2; done
  systemctl is-active --quiet "$u" || {{ echo "$u is not active"; exit 1; }}
done
rm -rf {work}
"""


def _units_of(leader):
    out = _run(
        leader,
        "systemctl list-unit-files 'opennebula*' --state=enabled --no-legend 2>/dev/null | awk '{print $1}'\n",
        timeout=60,
    )
    units = [u.strip() for u in out.splitlines() if UNIT_RE.match(u.strip())]
    return [u.removesuffix(".service") for u in units] or ["opennebula"]


def _from_leader(leader, folders, tmp):
    """config.tar.gz and onedb.sql.gz from the leader into tmp."""
    from accounts.fe_backup import _readable_tar, tar_paths

    cfg, db = os.path.join(tmp, "config.tar.gz"), os.path.join(tmp, "onedb.sql.gz")
    _fetch(leader, f"sudo -n tar -czf - --ignore-failed-read {tar_paths(folders)} 2>/dev/null; [ $? -le 1 ]", cfg)
    if not _readable_tar(cfg):
        raise RedeployError(f"{leader.name}: the configuration did not come whole")
    t = f"/tmp/claugine-onedb-{os.urandom(6).hex()}.sql"
    inner = f"onedb backup {t} --force >/dev/null && gzip -c {t}; rc=$?; rm -f {t}; exit $rc"
    _fetch(leader, f"sudo -n -u oneadmin sh -c {shlex.quote(inner)}", db)
    return cfg, db


def _install(task, node, cfg, db, server, units, fallback=()):
    """The configuration and the database onto the new node (oned not started)."""
    _push(node, cfg, "config.tar.gz")
    _push(node, db, "onedb.sql.gz")
    out = _run(node, install_script(server, units, fallback), timeout=3600)
    rules = "carried" if "RULES carried" in out else "fallback"
    task.step(
        f"{node.name}: data",
        "ok",
        f"server {server}; configuration {_size(cfg)}, database {_size(db)} restored; firewall rules "
        + ("carried" if rules == "carried" else "not in the source: the zone's ports opened between the nodes"),
    )
    return rules


def _leader_commit(nodes, node, session):
    """(leader node, its commit) of a node that says it is the leader with a valid commit, or (None, -1)."""
    for n in nodes:
        if n.pk == node.pk:
            continue
        st = raft(ip_of(n), session)
        if st is not None and st["state"] == "leader" and st["commit"] >= 0:
            return n, st["commit"]
    return None, -1


def _wait_sync(node, nodes, session, timeout):
    """Until the node is a follower with a valid commit that reaches the leader's. commit-348: the reference is a commit
    read from a node that says it is the leader; a failed read or no leader is no reference (waited for), and a new
    leader gives a new reference."""
    end = time.monotonic() + timeout
    ref = None  # (leader pk, its commit when first read)
    last = "no answer"
    while True:
        lead, commit = _leader_commit(nodes, node, session)
        if lead is None:
            last = "no leader answers with its commit"
        else:
            if ref is None or ref[0] != lead.pk:
                ref = (lead.pk, commit)
            st = raft(ip_of(node), session)
            if st is None:
                last = f"{node.name} does not answer"
            else:
                last = f"{st['state']}, commit {st['commit']} of {ref[1]} ({lead.name})"
                if st["state"] == "follower" and st["commit"] >= 0 and st["commit"] >= ref[1]:
                    return last
        if time.monotonic() > end:
            raise RedeployError(f"{node.name} has not caught up with the leader in {timeout} s: {last}")
        time.sleep(_poll())


def _reset(leader, zone_id, server):
    _run(leader, f"sudo -u oneadmin -H onezone server-reset {int(zone_id)} {int(server)} >/dev/null\n", timeout=120)


# --- one node, from the leader ------------------------------------------------------------------------------------


def plan(svc, node):
    """Who gives the data and what must move first; reads only. RedeployError when the zone does not allow it."""
    z = zone(svc)
    me = server_id(z, node)
    if z["leader"] is None:
        raise RedeployError(_("the zone has no leader: Restore all from a backup"))
    if me is None:
        raise RedeployError(f"{node.name} is not a server of zone {z['zone']}")
    if len(z["servers"]) < 3:
        raise RedeployError(
            f"zone {z['zone']} has {len(z['servers'])} server(s): a node is made again only while two others serve "
            "(Restore all from a backup otherwise)"
        )
    peers = [n for n in z["nodes"] if n.pk != node.pk]
    serving = [n for n in peers if (z["states"][n.pk] or {}).get("state") in ("leader", "follower")]
    if len(serving) < 2:
        states = ", ".join(f"{n.name} {(z['states'][n.pk] or {}).get('state', 'no answer')}" for n in peers)
        raise RedeployError(f"the two other nodes must serve (a leader and a follower): {states}")
    return {"zone": z, "me": me, "moves": z["leader"].pk == node.pk, "peers": serving}


def redeploy(task, node, user=None, keep_old=True, first=False):
    """One front-end node made again; its data from the leader (see the module). first (commit-402): confirmed as its
    first deployment (Deploy) - nothing changes when the platform has the VM after all."""
    from accounts.fe_backup import dirs
    from tenants.models import PortalSettings

    from . import base_deploy as bd

    svc = node.service
    exclusive(task, svc)  # commit-344
    p = plan(svc, node)
    z, me, session = p["zone"], p["me"], p["zone"]["session"]
    leader = z["leader"]
    # commit-347: everything read-only before oned is stopped anywhere - the platform's checks of the new VM (addresses,
    # image, no -old), the units the zone runs (from a serving peer), the settings' folders, the temporary folder
    bp = bd.plan(node)
    if not bp["ok"]:
        raise RedeployError(
            "; ".join(f"{c['check']}: {c['detail']}" for c in bp["checks"] if c["must"] and not c["ok"])
        )
    if first:  # commit-402: before the leadership moves or any VM changes
        bd._expect_new(node, bp)
        if p["moves"]:  # a node with no VM cannot lead the zone: it has one after all
            raise RedeployError(
                f"{node.name} leads the zone, so it is on the platform: nothing changed - refresh the page and "
                "Redeploy it"
            )
    if bp["old_exists"]:
        raise RedeployError(f"{node.name}-old exists: remove the old VM or roll back the previous redeploy first")
    units = _units_of(p["peers"][0])
    folders = dirs(PortalSettings.get())
    task.step(
        "zone",
        "ok",
        f"zone {z['zone']}: {node.name} is server {me}; leader {leader.name}"
        + ("; the leadership moves first" if p["moves"] else ""),
    )
    tmp = tempfile.mkdtemp(prefix="claugine-fe-")
    moved = False
    touched = []  # commit-402: set right before the first VM change (_redeploy's before)
    try:
        if p["moves"]:
            moved = True  # from the stop on, a failure starts oned again on the node (commit-347)
            _run(node, "systemctl stop opennebula\n", timeout=300)
            leader = _wait_leader(p["peers"], session, LEADER_TIMEOUT)
            if leader is None:
                raise RedeployError(f"no new leader within {LEADER_TIMEOUT} s")
            task.step("leader", "ok", f"the leadership moved to {leader.name}")
        lead = leader

        def after_boot():
            cfg, db = _from_leader(lead, folders, tmp)
            task.step("from the leader", "ok", f"{lead.name}: configuration {_size(cfg)}, database {_size(db)}")
            _install(task, node, cfg, db, me, units)
            _reset(lead, z["zone"], me)
            _run(node, start_script(units), timeout=300)
            task.step(f"{node.name}: sync", "ok", _wait_sync(node, z["nodes"], session, SYNC_TIMEOUT))

        return bd._redeploy(
            task, node, user, keep_old, after_boot=after_boot, expect_new=first, before=lambda: touched.append(1)
        )
    except Exception:
        if moved:  # the old VM was never replaced, or is back (rolled back): its oned again
            started = _attempt(lambda: _run(node, "systemctl start opennebula\n", timeout=300))
            task.step(
                "leader",
                "ok" if started else "failed",
                f"oned started again on {node.name}"
                if started
                else f"oned could not be started again on {node.name}: start it by hand",
            )
        if (moved or touched) and leader is not None and leader.pk != node.pk:  # commit-402: only what was changed
            _attempt(lambda: _reset(leader, z["zone"], me))  # the old node catches up from the leader
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _attempt(fn):
    """True when fn ran; after a failure the rollback's own result counts, so this only says whether it worked."""
    try:
        fn()
        return True
    except Exception:  # noqa: BLE001
        return False


def rollback(task, node, user=None):
    """By hand: base_deploy's rollback, then the old node is reset on the leader so it catches up."""
    from . import base_deploy as bd

    exclusive(task, node.service)  # commit-344
    result = bd._rollback_by_hand(task, node, user)
    try:
        z = zone(node.service)
        me = server_id(z, node)
        if z["leader"] is not None and me is not None and z["leader"].pk != node.pk:
            _reset(z["leader"], z["zone"], me)
            task.step("zone", "ok", f"{node.name} (server {me}) catches up from {z['leader'].name}")
    except Exception as exc:  # noqa: BLE001 - the VM is back; the zone is only told
        task.step("zone", "failed", f"not reset on the leader: {str(exc)[:300]}")
    return result


# --- every node, from the latest backup ---------------------------------------------------------------------------


def _folder():
    from accounts import fe_backup
    from tenants.models import PortalSettings

    s = PortalSettings.get()
    return (os.path.join(s.fe_backup_path, fe_backup.FOLDER) if fe_backup.FOLDER else s.fe_backup_path) or "", s


def latest_backup(svc):
    """The newest backup of the platform served in the local folder (platform_<name>_<vip>_<stamp>), or ""."""
    from accounts import fe_backup

    plat = svc.serves
    if plat is None:
        return ""
    _f, s = _folder()
    prefix = f"{fe_backup.PREFIX}{plat.name or plat.key}_{plat.fe_vip}_"
    names = [n for n, _size, _t in fe_backup.last_local(s, n=1000) if n.startswith(prefix)]
    return names[0] if names else ""


FOLDER_RE = re.compile(r"^(\d+)-(.+)-([0-9a-fA-F.:]+)_(leader|follower)$")


def _backup_nodes(content):
    """{address: (server id, folder, leader?)} of an unpacked backup's node folders."""
    out = {}
    for d in os.listdir(content):
        m = FOLDER_RE.match(d)
        if m and os.path.isfile(os.path.join(content, d, "config.tar.gz")):
            out[m.group(3)] = (int(m.group(1)), os.path.join(content, d), m.group(4) == "leader")
    return out


MARKER = "CLAUGINE_INSTALLATION"  # commit-352 (owner 12:28): in oneadmin's user template of a hosting installation


def same_endpoint(control):
    """commit-364: every platform record that reaches the same OpenNebula endpoint as control (two platforms can share
    a VIP), locked in one order (by pk) - marker creation is serialized per installation, not per record."""
    url = control.endpoint_url
    pks = [c.pk for c in PlatformControl.objects.all() if c.pk == control.pk or (url and c.endpoint_url == url)]
    return list(PlatformControl.objects.select_for_update().filter(pk__in=pks).order_by("pk"))


def installation_marker(one, create=False, control=None):
    """The installation's marker (MARKER in oneadmin's user template, user 0). create: written when it has none - one
    attribute merged into that template (nothing else changes), then read back - commit-364: under a lock on every
    platform record reaching the same endpoint (same_endpoint), so two restores of front ends on one installation never
    write two markers: the second finds the first one's. RedeployError when it cannot be had."""

    def read():
        return (one.xml("one.user.info", 0, False).findtext(f"TEMPLATE/{MARKER}") or "").strip()

    if not create:
        got = read()
        if not got:
            raise RedeployError(f"the installation has no {MARKER} (its first Restore all writes it)")
        return got
    import uuid

    from django.db import transaction

    with transaction.atomic():
        if control is not None:  # held from the read to the read-back
            same_endpoint(control)
        got = read()
        if got:
            return got
        one.call("one.user.update", 0, f'{MARKER} = "{uuid.uuid4().hex}"', 1)  # 1: merge into the template
        got = read()
    if not got:
        raise RedeployError(f"{MARKER} could not be written to oneadmin's template")
    return got
    import uuid

    one.call("one.user.update", 0, f'{MARKER} = "{uuid.uuid4().hex}"', 1)  # 1: merge into the template
    got = read()
    if not got:
        raise RedeployError(f"{MARKER} could not be written to oneadmin's template")
    return got


class Recovery:
    """commit-350: a Restore all's recovery record on the service (CoreService.recovery), written before the first VM
    change and kept until the zone is restored or rolled back: {kind, phase (restoring, rolling-back, incomplete),
    backup, task, at, problems, nodes {pk: {before [address, deployed VM id], old (VM id or None), off (powered off by
    the restore), new (the new VM's id, as soon as it exists)}}}. A Retry the rollback runs _undo from it."""

    def __init__(self, svc):
        self.svc = svc
        self.rec = pending(svc)

    def identity(self, one, control, create=False):
        """commit-352 (owner 11:50, 12:28): what the installation answered with - its endpoint URL, its zone's id and
        name, and the portal's own marker in oneadmin's user template (MARKER=<random id>), written once by the first
        Restore all on that installation (create) and only read afterwards."""
        cfg = one.xml("one.system.config")
        zid = int(cfg.findtext("FEDERATION/ZONE_ID") or 0)
        url = control.endpoint_url if control.platform is not None else control.xmlrpc_url
        marker = installation_marker(one, create=create, control=control)
        return {
            "url": url or "",
            "zone_id": zid,
            "zone": one.xml("one.zone.info", zid).findtext("NAME") or "",
            "installation": marker,
        }

    def begin(self, task, backup, nodes, one=None, control=None):
        import uuid

        from django.utils import timezone

        self.rec = {
            "kind": "fe-restore",
            "id": uuid.uuid4().hex[:12],
            "platform": self.svc.platform.key if self.svc.platform else "",  # commit-352: where its VMs are
            "identity": self.identity(one, control, create=True) if one is not None else {},
            "phase": "restoring",
            "backup": backup,
            "task": getattr(task, "pk", None),
            "at": timezone.now().isoformat(timespec="seconds"),
            "problems": [],
            "nodes": {
                str(n.pk): {
                    "name": n.name,
                    "before": [n.address, n.deployed_vm_id, n.state],  # T36 B3a: its state too
                    "old": None,
                    "off": False,
                    "intent": None,
                    "refused": False,  # commit-351: OpenNebula refused the allocation - nothing was made
                    "confirmed_absent": None,  # commit-351: an operator stated no VM was made (who, when)
                    "stime": "",  # commit-352: the old VM's start time
                    "new": None,
                }
                for n in nodes
            },
        }
        self.save()

    def save(self):
        CoreService.objects.filter(pk=self.svc.pk).update(recovery=self.rec)
        self.svc.recovery = self.rec

    def set(self, pk, **kw):
        self.rec["nodes"][str(pk)].update(kw)
        self.save()

    def phase(self, phase, problems=()):
        self.rec["phase"] = phase
        self.rec["problems"] = [str(p)[:300] for p in problems]
        self.save()

    def clear(self):
        self.rec = {}
        self.save()

    def one(self):
        """commit-352: the platform the record's VMs are on - never the service's present one; RedeployError when
        it is gone or not in execution mode (nothing is done elsewhere)."""
        from tenants.deploy import One

        key = self.rec.get("platform") or ""
        c = PlatformControl.objects.filter(key=key).first() if key else None
        if c is None or c.mode != PlatformControl.Mode.EXECUTION:
            raise RedeployError(f"the record's platform {key or '?'} is not reachable in execution mode")
        one = One(c)
        # commit-352 (owner 11:50): the same installation as when the restore began, before anything is read as
        # absent or any VM is acted on
        want = self.rec.get("identity") or {}
        if not want:
            raise RedeployError("the record has no identity of its installation: nothing is done")
        try:
            got = self.identity(one, c)
        except Exception as exc:  # noqa: BLE001 - not confirmed: nothing is done
            raise RedeployError(f"{key}: its identity could not be read ({str(exc)[:150]}): nothing is done") from exc
        if got != want:
            raise RedeployError(
                f"{key} is not the installation the restore began on ({want} then, {got} now): nothing is done"
            )
        return one

    def unresolved(self):
        """commit-351: nodes whose new VM was asked for, never got an id, was not refused by OpenNebula and has not
        been confirmed absent by an operator - [(pk, name, its tag)]."""
        return [
            (int(pk), r.get("name") or pk, r["intent"])
            for pk, r in (self.rec.get("nodes") or {}).items()
            if r.get("intent") and r.get("new") is None and not r.get("refused") and not r.get("confirmed_absent")
        ]

    def intents(self):
        return {r["intent"]: r.get("name") or pk for pk, r in (self.rec.get("nodes") or {}).items() if r.get("intent")}

    def maps(self):
        """(nodes, made, old, off, before) as _undo takes them."""
        recs = {int(k): v for k, v in (self.rec.get("nodes") or {}).items()}
        nodes = list(self.svc.nodes.filter(pk__in=list(recs)).order_by("pk"))
        made = {pk: r["new"] for pk, r in recs.items() if r.get("new") is not None}
        old = {pk: r["old"] for pk, r in recs.items() if r.get("old") is not None}
        off = {pk for pk, r in recs.items() if r.get("off")}
        before = {pk: (tuple(r.get("before") or ["", None]) + (None,))[:3] for pk, r in recs.items()}
        return nodes, made, old, off, before


class _Made(dict):
    """base_deploy._redeploy's made: the new VM's tag stored before it is asked for (commit-351), its id the moment
    the platform answers."""

    def __init__(self, rec, pk):
        super().__init__()
        self._rec, self._pk = rec, pk

    def intent(self):
        tag = f"{self._rec.rec.get('id')}-{self._pk}"
        self._rec.set(self._pk, intent=tag)
        return tag

    def refused(self):
        self._rec.set(self._pk, refused=True)

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if key == "new_id":
            self._rec.set(self._pk, new=value)


def restore_all(task, svc, user=None, name=None):
    """Every node of the front end made again from the backup `name` (the latest when None); see the module.
    commit-345/346: no node is rolled back alone - a failure rolls the whole zone back (_undo)."""
    from accounts import backup_archive
    from accounts.models import audit

    from . import base_deploy as bd
    from .core_deploy import _one, _powered_off, _vm

    exclusive(task, svc)  # commit-344
    ok, why, latest = restore_offer(svc)
    if not ok:
        raise RedeployError("; ".join(why))
    name = name or latest
    folder, s = _folder()
    password = s.get_secret("fe_backup_password") or s.get_secret("backup_password")
    if not password:
        raise RedeployError("no archive password (Settings → Backups)")
    nodes = list(svc.nodes.order_by("pk"))
    one, _control = _one(svc)
    for n in nodes:
        if _vm(one, f"{n.name}-old") is not None:
            raise RedeployError(f"{n.name}-old exists: remove the old VM or roll it back first")
    session = session_of(svc)
    tmp = tempfile.mkdtemp(prefix="claugine-fe-restore-")
    rec = Recovery(svc)
    started = False  # the record exists: from here on a failure rolls back from it
    try:
        content = os.path.join(tmp, "content")
        backup_archive.extract(os.path.join(folder, name, name + backup_archive.ARCHIVE), password, content)
        have = _backup_nodes(content)
        lead = [v for v in have.values() if v[2] and os.path.isfile(os.path.join(v[1], "onedb-zone.sql.gz"))]
        if not lead:
            raise RedeployError(f"{name}: no database in the backup")
        db = os.path.join(lead[0][1], "onedb-zone.sql.gz")
        missing = [n.name for n in nodes if ip_of(n) not in have]
        if missing:
            raise RedeployError(f"{name} has no folder of {', '.join(missing)} (by its address)")
        task.step("backup", "ok", f"{name}: {len(have)} node(s), database {_size(db)}")
        # commit-350: the record before the first VM change
        rec.begin(task, name, nodes, one, _control)  # commit-352: with the installation's identity
        started = True
        # 1 the old zone stops: every old VM powered off (kept), before any new node starts
        old, off = {}, set()
        for n in nodes:
            vm = _vm(one, n.name)
            if vm is None:
                continue
            old[n.pk] = int(vm.findtext("ID"))
            rec.set(n.pk, old=old[n.pk], stime=vm.findtext("STIME") or "")
            if vm.findtext("STATE") != "8":
                off.add(n.pk)  # recorded first: a stop half-way is resumed by the rollback
                rec.set(n.pk, off=True)
                _powered_off(one, old[n.pk])
        task.step(
            "old VMs",
            "ok",
            ", ".join(f"{n.name} #{old[n.pk]}" for n in nodes if n.pk in off) + " powered off" if off else "none ran",
        )
        audit(user, "core_fe_restore_started", svc, site=svc.site.code, backup=name)
        nodes.sort(key=lambda n: not have[ip_of(n)][2])  # the backup's leader first
        units = list(UNITS)
        fallback = _fallback(nodes, svc)
        for n in nodes:
            sid, nfolder, _lead = have[ip_of(n)]
            cfg = os.path.join(nfolder, "config.tar.gz")

            def after_boot(n=n, sid=sid, cfg=cfg):
                _install(task, n, cfg, db, sid, units, fallback=fallback)
                _run(n, start_script(units), timeout=300)

            # commit-346/350: the new VM's id is in the record as soon as it exists, old VM or not
            bd._redeploy(task, n, user, keep_old=True, after_boot=after_boot, made=_Made(rec, n.pk))
        # 2 the zone: a leader, every other node following it
        leader = _wait_leader(nodes, session, LEADER_TIMEOUT)
        if leader is None:
            raise RedeployError(f"no leader within {LEADER_TIMEOUT} s")
        for n in nodes:
            if n.pk != leader.pk:
                _wait_sync(n, nodes, session, SYNC_TIMEOUT)
        others = ", ".join(n.name for n in nodes if n.pk != leader.pk)
        task.step("zone", "ok", f"leader {leader.name}; {others} follow")
        rec.clear()
        audit(user, "core_fe_restored", svc, site=svc.site.code, backup=name)
        return {"backup": name, "leader": leader.name, "nodes": [n.name for n in nodes]}
    except Exception as exc:
        task.step("restore", "failed", str(exc)[:500])
        if not started:
            raise RedeployError(f"nothing changed: {exc}") from exc
        rec.phase("rolling-back")
        problems = _undo(task, svc, rec, session)
        if problems:
            rec.phase("incomplete", problems)
        else:
            rec.clear()
        audit(
            user,
            "core_fe_restore_rolled_back",
            svc,
            site=svc.site.code,
            backup=name,
            error=str(exc)[:300],
            complete=not problems,
        )
        if problems:
            raise RedeployError(f"not completely rolled back ({'; '.join(problems)[:600]}): {exc}") from exc
        raise RedeployError(f"rolled back: {exc}") from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def confirm_absent(svc, node_pk, user=None, tag=""):
    """commit-351 (owner 11:50): an operator states that no VM was made for this node's unanswered allocation (they
    checked the platform); audited. Returns False when there is nothing to confirm."""
    from django.db import transaction
    from django.utils import timezone

    from accounts.models import audit

    with transaction.atomic():
        CoreService.objects.select_for_update().get(pk=svc.pk)
        rec = Recovery(svc)
        # commit-359: only the allocation the operator checked - its tag as the dialog showed it
        if (node_pk, tag) not in {(pk, t) for pk, _n, t in rec.unresolved()}:
            return False
        rec.set(node_pk, confirmed_absent={"by": getattr(user, "email", "") or "", "at": timezone.now().isoformat()})
    audit(user, "core_fe_restore_alloc_confirmed_absent", svc, site=svc.site.code, node=node_pk, tag=tag)
    return True


def retry_rollback(task, svc, user=None):
    """commit-350: Retry the rollback - the zone-wide rollback of a Restore all that did not end (incomplete, or cut
    off), from the service's recovery record: every new VM confirmed gone first, then the old VMs, then the zone."""
    from accounts.models import audit

    exclusive(task, svc, retry=True)
    rec = Recovery(svc)
    if not rec.rec:
        raise RedeployError(f"{svc.name}: no Restore all to roll back")
    task.step("record", "ok", f"{rec.rec.get('backup', '')}: phase {rec.rec.get('phase')} since {rec.rec.get('at')}")
    rec.phase("rolling-back")
    problems = _undo(task, svc, rec, session_of(svc))
    audit(user, "core_fe_restore_rollback_retried", svc, site=svc.site.code, complete=not problems)
    if problems:
        rec.phase("incomplete", problems)
        raise RedeployError(f"not completely rolled back: {'; '.join(problems)[:600]}")
    rec.clear()
    return {"rolled_back": True}


def _fallback(nodes, svc):
    """The firewall rules a node gets when the backup carries none (made before v01.01.53): the zone's ports between
    the nodes, and the front end's ports from the site's management networks."""
    from .nfconfig import management

    out = []
    for n in nodes:
        out.append(f"ufw allow proto tcp from {shlex.quote(ip_of(n))} to any port 2633,2634")
    hosts, _ports = management(svc.site.code)
    for h in hosts:
        out.append(f"ufw allow proto tcp from {shlex.quote(str(h))} to any port 2474,2616,2633,2634,5030")
    return out


def _fence_new(one, vid, rid):
    """commit-352: a recorded new VM removed only when the platform shows it is this restore's own (its tag starts
    with the record's id) - an id that answers with another VM, on whatever installation the key now points to, is
    not touched and keeps the record pending; done or NO_EXISTS: gone."""
    from adoption.collector import NO_EXISTS, OneError
    from tenants.deploy import DeployError

    try:
        vm = one.xml("one.vm.info", vid, False)
    except DeployError as exc:
        cause = exc.__cause__
        if isinstance(cause, OneError) and cause.method == "one.vm.info" and cause.code == NO_EXISTS:
            return
        raise RedeployError(f"#{vid} cannot be read: {str(exc)[:200]}") from exc
    if vm.findtext("STATE") == "6":
        return
    tag = vm.findtext("USER_TEMPLATE/CLAUGINE_RESTORE") or ""
    if not tag.startswith(f"{rid}-"):
        raise RedeployError(
            f"#{vid} ({vm.findtext('NAME')}) is not this restore's VM (tag {tag or 'none'}): not touched"
        )
    try:
        one.call("one.vm.action", "terminate-hard", vid)
    except Exception:  # noqa: BLE001 - a retry or an earlier removal: the check below decides
        pass
    _fenced(one, vid)  # commit-345: confirmed done or not found, never an inconclusive answer


def _undo(task, svc, rec, session):
    """commit-345/346: the whole zone back, never old and restored nodes together.
    1 every new VM removed and checked gone - if one is not, every old VM stays off (said in the task);
    2 then the old VMs: a VM its redeploy stepped aside is put back (name, NICs, resumed - base_deploy._rollback), one
      only powered off here is resumed; a node without an old VM gets its fields back;
    3 the old zone checked (a leader).
    Returns what did not work ([] = rolled back)."""
    from . import base_deploy as bd
    from .core_deploy import _wait

    nodes, made, old, off, before = rec.maps()  # commit-350: from the stored record (a retry too)
    problems = []
    try:
        one = rec.one()  # commit-352: the record's own platform
    except RedeployError as exc:
        problems.append(str(exc))
        task.step("rollback: new VMs", "failed", f"{exc} - the old VMs are kept off")
        return problems
    # 1 every new VM gone: the recorded ids, and (commit-351) every VM carrying a tag the record asked for - a VM
    # whose id never came back is found by its tag; a pool that cannot be read keeps the record pending
    targets = {vid: f"#{vid}" for vid in made.values()}
    tags = rec.intents()
    seen = set()
    if tags:
        try:
            for vm in one.xml("one.vmpool.info", -2, -1, -1, -1).findall("VM"):
                tag = vm.findtext("USER_TEMPLATE/CLAUGINE_RESTORE") or ""
                if tag in tags:
                    seen.add(tag)
                    vid = int(vm.findtext("ID"))
                    targets.setdefault(vid, f"{tags[tag]} #{vid}")
                    pk = int(tag.rsplit("-", 1)[1])
                    if str(pk) in rec.rec.get("nodes", {}) and rec.rec["nodes"][str(pk)].get("new") is None:
                        rec.set(pk, new=vid)  # commit-360: stored before it is removed - a later retry checks it by id
        except Exception as exc:  # noqa: BLE001 - not knowing is not proof that none exists
            problems.append(f"the platform's VMs could not be read for the restore's tags: {str(exc)[:200]}")
        else:
            # commit-351 (owner 11:50): an allocation with no id, not refused and no tagged VM is not settled by time
            # - it stays pending until its VM shows up (found by its tag) or an operator confirms none was made
            for pk, name, _tag in rec.unresolved():
                if rec.rec["nodes"][str(pk)]["intent"] not in seen:
                    problems.append(
                        f"OpenNebula did not answer the creation of {name}'s new VM: check the platform, then "
                        "Confirm no VM was made"
                    )
    rid = rec.rec.get("id") or ""
    for vid, label in sorted(targets.items()):
        try:
            _fence_new(one, vid, rid)  # commit-352: only this restore's own VM
        except Exception as exc:  # noqa: BLE001 - each tried; any left keeps the old VMs off
            problems.append(f"new VM {label} not removed: {str(exc)[:200]}")
    gone = [
        str(r.get("name") or pk)
        for pk, r in (rec.rec.get("nodes") or {}).items()
        if int(pk) not in {n.pk for n in nodes}
    ]
    if gone:  # commit-352: a node the record names is no longer in the portal
        problems.append(f"the record's nodes {', '.join(gone)} are not in the portal")
    if problems:
        task.step("rollback: new VMs", "failed", "; ".join(problems)[:400] + " - the old VMs are kept off")
        return problems
    task.step("rollback: new VMs", "ok", ", ".join(targets.values()) + " removed" if targets else "none was made")
    names = {int(pk): r.get("name") or "" for pk, r in (rec.rec.get("nodes") or {}).items()}
    for n in nodes:  # 2
        try:
            n.refresh_from_db()
            rd = (n.facts or {}).get("redeploy") or {}
            addr, dep, was = before[n.pk]
            was = was or "non-compliant"  # T36 B3a: a record written before B3a: the old VM is not the portal's
            if n.pk in old:  # commit-352: the old VM is the one recorded - its name and start time - before anything
                vm = one.xml("one.vm.info", old[n.pk], False)
                got = vm.findtext("NAME") or ""
                want = names.get(n.pk) or n.name
                if got not in (want, f"{want}-old"):
                    raise RedeployError(f"#{old[n.pk]} is {got or '?'}, not {want}: not touched")
                stime = str((rec.rec["nodes"].get(str(n.pk)) or {}).get("stime") or "")
                if (vm.findtext("STIME") or "") != stime:
                    raise RedeployError(f"#{old[n.pk]} started at {vm.findtext('STIME')}, not {stime}: not touched")
            if rd.get("old_id") is not None and rd["old_id"] == old.get(n.pk):  # stepped aside by its redeploy
                bd._rollback(
                    task,
                    one,
                    n,
                    None,
                    rd["old_id"],
                    rd.get("taken") or [],
                    autostart=rd.get("autostart") or "",
                    schedule=rd.get("schedule"),
                    restore={"address": addr, "deployed_vm_id": dep, "state": was},
                )
            else:
                if n.pk in off:  # only powered off here
                    vm = one.xml("one.vm.info", old[n.pk], False)
                    if vm.findtext("STATE") == "8":
                        one.call("one.vm.action", "resume", old[n.pk])
                    _wait(one, old[n.pk], "running")
                n.address, n.deployed_vm_id = addr, dep
                n.save(update_fields=["address", "deployed_vm_id"])
                n.set_state(was)
        except Exception as exc:  # noqa: BLE001 - the next one is tried
            problems.append(f"{n.name}: {str(exc)[:200]}")
            task.step(f"{n.name}: rollback", "failed", f"{str(exc)[:300]} - Roll back on its details")
    if problems:
        return problems
    if off or any(n.pk in old for n in nodes):  # 3
        leader = _wait_leader(nodes, session, LEADER_TIMEOUT)
        if leader is None:
            problems.append("the old zone has no leader yet: check the front end")
            task.step("rollback: zone", "failed", problems[-1])
        else:
            task.step("rollback: zone", "ok", f"the old VMs are back; leader {leader.name}")
    return problems
