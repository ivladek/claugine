"""The node agent: what root does on an app node of the portal (see accounts/ha.py).

It runs as root (systemd `claugine-ha`, `manage.py ha_agent serve`) on this node's internal address, port
HA_AGENT_PORT (portal.env), over TLS (the portal's own certificate), and answers only requests that carry the shared
token (HA_TOKEN in portal.env); ufw lets only the other app node in. The appliance timer (root) also calls run_op()
directly for the automatic updates.

    GET  /status            role, site, version, database, running tasks, last operation
    GET  /op                the last operation run here (steps)
    GET  /installed, /backups  for the Settings pages (root reads what www-data cannot)
    GET  /hostmedia/<file>  the current installer ISO, for the other node (Enterprise; accounts/hostmedia.py)
    POST /role              {"role": …}
    POST /install-ref       {"ref": …, "migrate": bool}: install a release here
    POST /os-patch, /note
    POST /env-key           {"value": …}: FIELD_ENCRYPTION_KEY here (rotate-key on the other node, accounts/keyrotate.py)
    POST /op                {"name": …}: start an operation here, step by step

**Editions split, step 3 (2026-11-11):** everything that only means something with a second node — MySQL
replication, the database copy (/dump, /replicate, /copy, /promote, /wait-gtid, /install-from), the switchover, the
standby's updates, a new version under test, the pair's upgrade to the Enterprise releases — is `ee/ha_agent.py`,
reached through `_pair()`. A build without `ee/` (Standard) is one node: it updates itself (update-single) and can
upgrade itself to the Enterprise releases. ee/ha_agent.py calls this module's functions as `base.<name>`, so a test
that patches `accounts.ha_agent.<name>` governs both.

The portal itself comes from the control plane's bundles only; the nodes never pass code to each other.
"""

import fcntl
import hashlib
import json
import os
import re
import ssl
import subprocess
import threading
import time
from datetime import UTC, datetime

from django.conf import settings

from . import ha, thisnode

PREFIX = "/opt/claugine-portal"
STATE_DIR = "/var/lib/claugine-ha"
OP_FILE = f"{STATE_DIR}/op.json"
TLS_DIR = "/etc/claugine-portal/tls"
BASE_OPS = ("update-single", "update-os-single", "upgrade-enterprise", "rotate-key")
BASE_REF_OPS = ("update-single", "upgrade-enterprise")
REF_RE = re.compile(r"^(v[0-9][0-9A-Za-z.\-]{0,40}|[0-9a-f]{40})$")  # a release tag or a commit


class AgentError(Exception):
    pass


# --- MySQL -------------------------------------------------------------------------------------------------


def sql(query, timeout=120):
    """Run SQL as root over the local socket; rows as lists of strings."""
    r = subprocess.run(
        ["mysql", "--protocol=socket", "-uroot", "-N", "-B", "-e", query],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if r.returncode != 0:
        raise AgentError(f"mysql: {(r.stderr.strip().splitlines() or ['failed'])[-1][:300]}")
    return [line.split("\t") for line in r.stdout.splitlines()]


def sql1(query):
    rows = sql(query)
    return rows[0][0] if rows and rows[0] else ""


def db_name():
    return settings.DATABASES["default"]["NAME"]


def db_status():
    try:
        return {
            "read_only": sql1("SELECT @@super_read_only") == "1",
            "gtid": executed_gtid(),
            "gtid_mode": sql1("SELECT @@GLOBAL.gtid_mode"),
            "server_id": sql1("SELECT @@server_id"),
            "replica": _pair().replica_status() if _pair() else {},
        }
    except (AgentError, OSError, subprocess.TimeoutExpired) as exc:
        return {"error": str(exc)[:300]}


def set_read_only(on):
    """PERSIST: a standby stays read only after a MySQL restart, even before the agent runs again."""
    sql("SET PERSIST super_read_only=ON" if on else "SET PERSIST super_read_only=OFF; SET PERSIST read_only=OFF")


def enforce_role():
    """At the agent's start: a standby or a node in maintenance is read only (replication itself restarts with MySQL)."""
    from accounts import ha

    try:
        if ha.role() in ("standby", "maintenance") and not _ro():
            set_read_only(True)
    except (AgentError, OSError, subprocess.TimeoutExpired):
        pass


def executed_gtid():
    """This server's executed GTID set in one line (mysql -N prints the newlines between server UUIDs as \\n)."""
    return "".join(sql1("SELECT @@GLOBAL.gtid_executed").replace("\\n", "").split())


# --- version and distribution ---------------------------------------------------------------------------------


def version():
    try:
        with open(f"{PREFIX}/release", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


UPDATE = "/usr/local/sbin/claugine-portal-update"


def commit():
    """The release installed here (vNN.NN.NN, from /opt/claugine-portal/COMMIT, written by the updater and the
    installer: a bundle has no git history, so the release is its identity). "" when it was installed some other
    way."""
    try:
        with open(f"{PREFIX}/COMMIT", encoding="utf-8") as fh:
            c = fh.read().strip()
    except OSError:
        return ""
    return c if c.startswith("v") and REF_RE.match(c) else ""


def runs_enterprise():
    """This node runs the Enterprise code (ee/ is present). v01.00.00: every node installs bundles, so the code it
    runs - not where it came from - says whether the upgrade to Enterprise is still to be done."""
    from accounts import edition

    return not edition.standard_build()


def _restart_later():
    """The agent runs the new code after the operation: restarted a little later by systemd."""
    subprocess.run(
        ["systemd-run", "--on-active=20", "--unit=claugine-ha-restart", "systemctl", "restart", "claugine-ha"],
        capture_output=True,
        check=False,
    )


_INSTALLED = {"at": 0.0, "data": None}


def installed_status(fresh=True):
    """What is installed here, from the updater: {source: "bundle", installed, installed_tag}. The releases on offer
    come from the licence check-in, not from here. fresh=False: what was read in the last minute will do."""
    if not fresh and _INSTALLED["data"] and time.time() - _INSTALLED["at"] < 60:
        return _INSTALLED["data"]
    if not os.path.exists(UPDATE):
        raise AgentError(f"{UPDATE} is missing here: reinstall the node with deploy/claugine-install.sh")
    r = subprocess.run([UPDATE, "--json"], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise AgentError("updater: " + (r.stdout + r.stderr)[-300:])
    try:
        data = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise AgentError(f"updater: unreadable answer ({exc})") from exc
    _INSTALLED.update(at=time.time(), data=data)
    return data


def install_ref(ref, migrate=False):
    """Install here a release (vNN.NN.NN) from the control plane's bundles. migrate: run the database changes (the
    active node only; a standby takes them by replication)."""
    if not REF_RE.match(ref or ""):
        raise AgentError(f"not a release tag or commit: {ref}")
    if not os.path.exists(UPDATE):
        raise AgentError(f"{UPDATE} is missing here: reinstall the node with deploy/claugine-install.sh")
    args = [UPDATE, "--ref", ref, "--no-agent-restart"] + ([] if migrate else ["--no-migrate"])
    r = subprocess.run(args, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise AgentError("install failed: " + (r.stdout + r.stderr)[-400:])
    if commit() != ref:
        raise AgentError(f"installed {commit()[:12] or 'nothing'}, expected {ref}")
    _restart_later()
    return f"installed {ref} (release {version()})"


# --- talking to the other node --------------------------------------------------------------------------------


def _ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE  # self-signed: the other node's certificate is checked by its pin (_check_pin)
    return c


def _check_pin(addr, der):
    """enterprise-002: the token went to whoever answered at the other node's address. The other node's certificate
    is pinned (its SHA-256 in node.env, PEER_TLS_SHA256: written on both nodes when the pair is made, or recorded the
    first time this release reaches it) and checked before anything is sent; another certificate is refused."""
    n = thisnode.read()
    if addr != n.get("PEER_ADDR"):
        return  # this node's own agent, or a node that is not the pair's other one
    fp = hashlib.sha256(der or b"").hexdigest()
    pin = (n.get("PEER_TLS_SHA256") or "").lower()
    if pin:
        if fp != pin:
            raise AgentError(
                f"{addr}: its certificate is not the one recorded for the other node (SHA-256 {fp[:16]}…, recorded "
                f"{pin[:16]}…): nothing was sent. If the other node was reinstalled, set the pair up again "
                "(Settings → Portal → Replace node)"
            )
        return
    if os.geteuid() == 0:  # the agent and the timer (root) record it; the portal's own process only reads
        thisnode.write({"PEER_TLS_SHA256": fp})
        try:
            note("ha_peer_pinned", {"peer": addr, "sha256": fp})
        except Exception:  # noqa: BLE001
            pass


def peer_request(addr, method, path, body=None, raw=False, timeout=60, read_timeout=None, deadline=None):
    """`timeout` bounds the connection and the wait for the answer's headers; with raw, `read_timeout` then bounds each
    read of the body (commit-069: a dump the other node prepares for long, then streams). commit-060: with `deadline`
    (a time.monotonic() value) the connection, the headers and each read of the body get only the time left then.
    commit-075: the timeouts are set on the socket kept from the connection - for an HTTP/1.0 answer (the agent's own)
    http.client hands the socket to the response and conn.sock is None."""
    import http.client
    import time as _time

    def left():
        if deadline is None:
            return timeout
        rest = deadline - _time.monotonic()
        if rest <= 0:
            raise AgentError(f"{addr}{path}: no time left")
        return rest

    data = json.dumps(body or {}).encode() if method == "POST" else None
    ctx = _ctx()  # commit-060: made once, before the clock of the connection starts counting on it
    conn = http.client.HTTPSConnection(addr, settings.HA_AGENT_PORT, timeout=left(), context=ctx)
    watchdog = None
    late = None
    if deadline is not None:
        # commit-060: socket timeouts only limit silence; at the deadline the socket the connection has is shut down -
        # armed before connect(), so the TCP connect, the TLS handshake (on the same socket), the headers, an error's
        # body and the body all end at it
        import socket as _socket
        import threading as _threading

        late = _threading.Event()

        def _cut():
            late.set()
            s_ = conn.sock
            if s_ is None:
                s_ = getattr(conn, "_claugine_sock", None)
            try:
                if s_ is not None:
                    s_.shutdown(_socket.SHUT_RDWR)
            except OSError:
                pass

        watchdog = _threading.Timer(max(0.0, deadline - _time.monotonic()), _cut)
        watchdog.daemon = True
        watchdog.start()
    try:
        if deadline is None:
            conn.connect()
        else:
            # commit-060: the TCP connect gets the time left, then the TLS handshake what is left after it (one
            # connect() gave both the same whole timeout; the watchdog cannot reach a socket mid-handshake)
            import socket as _socket

            raw_sock = _socket.create_connection((addr, settings.HA_AGENT_PORT), timeout=left())
            conn._claugine_sock = raw_sock
            try:
                # the handshake as a step of its own, on the TLS socket the watchdog already holds, with the time
                # left just before it
                tls = ctx.wrap_socket(raw_sock, server_hostname=addr, do_handshake_on_connect=False)
                conn._claugine_sock = tls
                tls.settimeout(left())
                tls.do_handshake()
                conn.sock = tls
            except BaseException:
                conn._claugine_sock.close()
                raise
        sock = conn.sock
        conn._claugine_sock = sock  # kept: for an HTTP/1.0 answer conn.sock becomes None (commit-075)
        _check_pin(addr, sock.getpeercert(binary_form=True))  # before the token is sent (enterprise-002)
        headers = {"X-Claugine-Token": settings.HA_TOKEN, "Content-Type": "application/json"}
        if data is not None:
            headers["Content-Length"] = str(len(data))
        sock.settimeout(left())
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
    except AgentError:
        conn.close()
        if watchdog:
            watchdog.cancel()
        raise
    except (OSError, http.client.HTTPException) as exc:
        conn.close()
        if watchdog:
            watchdog.cancel()
        raise AgentError(f"{addr}{path}: {getattr(exc, 'reason', exc)}") from exc
    if resp.status >= 400:
        try:
            msg = json.loads(resp.read() or b"{}").get("error", "")
        except (ValueError, OSError, http.client.HTTPException):
            msg = ""
        conn.close()
        resp.close()
        if watchdog:
            watchdog.cancel()
        raise AgentError(f"{addr}{path}: HTTP {resp.status} {msg}".strip())
    if raw:
        # the caller reads the body: the watchdog stays on it (cancelled by close_response), and `late` says it cut
        resp.claugine_watchdog = watchdog
        resp.claugine_late = late
        if read_timeout:
            try:
                sock.settimeout(min(read_timeout, left()) if deadline is not None else read_timeout)
            except AgentError:
                close_response(resp)
                raise
        return resp
    try:
        return json.loads(resp.read() or b"{}")
    finally:
        conn.close()
        if watchdog:
            watchdog.cancel()


def close_response(resp):
    """Close a raw peer_request response and stop its watchdog (commit-060)."""
    w = getattr(resp, "claugine_watchdog", None)
    if w is not None:
        w.cancel()
    resp.close()


def cut_at_deadline(resp):
    """True when the response's watchdog shut it at the deadline (commit-060)."""
    late = getattr(resp, "claugine_late", None)
    return bool(late is not None and late.is_set())


def status():
    n = thisnode.read()
    out = {
        "role": n.get("ROLE", "active"),
        "site": n.get("SITE", ""),
        "addr": n.get("ADDR", ""),
        "peer": n.get("PEER_ADDR", ""),
        "mail_name": n.get("MAIL_NAME", ""),  # 2026-11-17: the DNS records of the direct sending (Settings → Mail)
        "version": version(),
        "commit": commit(),
        "db": db_status(),
        "op": read_op(),
        "time": time.time(),
        "reboot_required": os.path.exists("/var/run/reboot-required"),
        # v01.01.00: out of service after a failed switch over, or serving alone (Settings → Portal, mockup 36)
        "stranded": n.get("STRANDED", ""),
        "alone": n.get("ALONE", ""),
        "alone_by": n.get("ALONE_BY", ""),
        "both_active": n.get("BOTH_ACTIVE", ""),
        "resync_other": n.get("RESYNC_OTHER", ""),  # commit-021: the other node waits for a new copy
        "kept_by": n.get("KEPT_BY", ""),  # test-102: a super admin kept this node when both were active
        "kept_at": n.get("KEPT_AT", ""),
    }
    try:  # integration-003: which keys this node holds (hashes only), checked before a switch over
        from accounts import keyrotate

        out["keys"] = keyrotate.fingerprints()
    except Exception:  # noqa: BLE001
        out["keys"] = []
    try:
        from accounts.tasks import running_count

        out["tasks_running"] = running_count()
    except Exception as exc:  # noqa: BLE001
        out["tasks_running"] = None
        out["tasks_error"] = str(exc)[:200]
    return out


def note(action, details):
    from accounts.models import audit

    audit(
        None,
        action,
        None,
        # the steps in full (owner 2026-11-15: "remove steps from here - track them in logs")
        **{
            k: (v if isinstance(v, (int, float, bool)) else str(v)[: 3000 if k == "steps" else 300])
            for k, v in (details or {}).items()
        },
    )


# --- operations -----------------------------------------------------------------------------------------------


def _lock_path():
    return os.path.join(STATE_DIR, "op.lock")


def claim():
    """The right to run an operation on this node: an exclusive lock on state/op.lock, held until release(), shared
    by the agent's request threads and the appliance timer (integration-001: both read op.json and wrote it, so two
    operations could start together). The kernel drops it when the process ends, which is how an operation cut off
    by a restart is recognised (enterprise-005). Raises AgentError when another operation holds it."""
    os.makedirs(STATE_DIR, mode=0o750, exist_ok=True)
    fd = os.open(_lock_path(), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise AgentError("another operation is running") from None
    _reconcile()
    return fd


class role_lock:
    """commit-017: a role change, a promote and a fence are each done whole under one lock (state/role.lock), so an
    activation that has passed its fence check cannot finish after a fence: the fence waits for it and then sees an
    active node, or the activation waits and then sees the fence."""

    def __enter__(self):
        os.makedirs(STATE_DIR, mode=0o750, exist_ok=True)
        self.fd = os.open(os.path.join(STATE_DIR, "role.lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
        return False


def release(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _held():
    """Whether an operation holds the lock now (in this process or another)."""
    try:
        fd = os.open(_lock_path(), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def _reconcile():
    """An op.json still "running" whose lock nobody holds was cut off (a restart, a reboot, a killed process): it is
    recorded as interrupted, with the step it reached, so it no longer blocks the next one (enterprise-005). Called
    with the lock held, so a "running" record cannot be a live operation."""
    data = _read_file()
    if data.get("state") != "running":
        return data
    last = next((s for s in reversed(data.get("steps") or []) if s.get("status") == "running"), None)
    if last:
        last["status"] = "interrupted"
    data.update(
        state="failed",
        interrupted=True,
        error=("interrupted" + (f" at: {last['name']}" if last else "") + " (the node or its agent restarted)")[:500],
        finished=_now(),
    )
    _write_file(data)
    try:
        note("ha_op_interrupted", {"operation": data.get("name", ""), "error": data["error"]})
    except Exception:  # noqa: BLE001 - the portal may be read only here
        pass
    return data


def _read_file():
    try:
        with open(OP_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _write_file(data):
    os.makedirs(STATE_DIR, mode=0o750, exist_ok=True)
    tmp = f"{OP_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"  # one per writer (integration-001)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, OP_FILE)


def read_op():
    """The last operation. One that says "running" while nobody holds the lock was interrupted (enterprise-005)."""
    data = _read_file()
    if data.get("state") == "running" and not _held():
        try:
            fd = claim()  # reconciles under the lock
            release(fd)
        except AgentError:
            return data
        data = _read_file()
    return data


class Op:
    def __init__(self, name, by=""):
        self.data = {
            "name": name,
            "by": by,
            "state": "running",
            "steps": [],
            "started": _now(),
            "node": thisnode.read().get("SITE", ""),
        }
        self.save()

    def save(self):
        _write_file(self.data)

    def step(self, name, fn):
        entry = {"name": name, "status": "running", "at": _now(), "detail": ""}
        self.data["steps"].append(entry)
        self.save()
        try:
            entry["detail"] = str(fn() or "")[:500]
            entry["status"] = "ok"
        except Exception as exc:
            entry["status"], entry["detail"] = "failed", f"{exc}"[:500]
            raise
        finally:
            self.save()

    def finish(self, ok, error=""):
        self.data.update(state="done" if ok else "failed", error=error[:500], finished=_now())
        self.save()


def _now():
    return datetime.now(UTC).isoformat(timespec="seconds")


def set_role(r):
    if r not in ha.ROLES:
        raise AgentError(f"not a role: {r}")
    thisnode.write({"ROLE": r})
    return f"role {r}"


def os_patch(reboot=True):
    """Install all applicable OS updates on THIS node (apt dist-upgrade); reboot if the system needs it. Root."""
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    r = subprocess.run(["apt-get", "update", "-q"], capture_output=True, text=True, timeout=900, env=env)
    if r.returncode != 0:
        raise AgentError(f"apt-get update failed: {(r.stderr or r.stdout)[-300:].strip()}")
    # 2026-11-12 (v01.00.08 on the standby): a mysql-server upgrade starts a temporary server that runs an init file
    # ending in SHUTDOWN. The standby's persisted super_read_only made that file fail before SHUTDOWN, and the
    # package waited forever. Its persisted value is set aside for the upgrade (it stays on until mysqld restarts)
    # and put back afterwards, whatever happened.
    try:
        ro = sql1("SELECT @@super_read_only") == "1"
    except Exception:
        ro = False
    if ro:
        sql("RESET PERSIST IF EXISTS super_read_only")
    try:
        r = subprocess.run(
            [
                "apt-get",
                "-y",
                "-o",
                "Dpkg::Options::=--force-confold",
                "-o",
                "Dpkg::Options::=--force-confdef",
                "dist-upgrade",
            ],
            capture_output=True,
            text=True,
            timeout=3600,
            env=env,
        )
    finally:
        if ro:
            sql("SET PERSIST super_read_only=ON")
    if r.returncode != 0:
        raise AgentError(f"apt-get dist-upgrade failed: {(r.stderr or r.stdout)[-300:].strip()}")
    m = re.search(r"(\d+)\s+upgraded", r.stdout or "")
    n = m.group(1) if m else "?"
    need = os.path.exists("/var/run/reboot-required")
    if need and reboot:
        subprocess.run(
            ["systemd-run", "--on-active=5", "--unit=claugine-os-reboot", "systemctl", "reboot"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return {"upgraded": n, "rebooting": True, "result": f"{n} upgraded; rebooting"}
    return {
        "upgraded": n,
        "rebooting": False,
        "result": f"{n} upgraded" + ("; a reboot is still required" if need else ""),
    }


def run_op(name, by="", ref="", lock=None):
    """Run an operation here (in a thread of the agent, or the appliance timer). Steps are kept in op.json and noted
    in the portal's log. One node: update-single and upgrade-enterprise. The operations with another node (switchover,
    the standby's updates, a new version under test...) are the Enterprise edition's (ee/ha_agent.py, step 3 of the
    editions split, 2026-11-11): without ee/ they are not in OPS.

    lock: the claim() the caller already holds (the agent takes it before it answers a request); else it is taken
    here. It is released when the operation ends, whatever happens (integration-001)."""
    if name not in ops():
        if lock is not None:
            release(lock)
        raise AgentError(f"unknown operation {name}")
    lock = claim() if lock is None else lock
    try:
        return _run_op(name, by, ref)
    finally:
        release(lock)


def _run_op(name, by, ref):
    op = Op(name, by)
    peer = None
    gated = False
    try:
        if name == "update-single":  # 2026-11-10: a node with no other node (every Standard installation)
            if thisnode.read().get("PEER_ADDR"):
                raise AgentError("this node has another node: update both with update-portal")
            gated = _close_gate(name)
            op.data["ref"] = ref
            from accounts import appliance

            op.step("backup", lambda: _backup_or_fail(appliance, name))
            op.step(f"this node: install {ref} (with the database changes)", lambda: install_ref(ref, migrate=True))
        elif name == "update-os-single":  # owner 2026-11-15: "Update OS" on a node with no other node
            if thisnode.read().get("PEER_ADDR"):
                raise AgentError("this node has another node: update the OS of both with update-os")
            gated = _close_gate(name)
            op.step("this node: install OS updates (reboot if needed)", lambda: os_patch(reboot=True)["result"])
        elif name == "upgrade-enterprise":  # a Standard node whose licence is now Enterprise: it installs the
            # Enterprise release (the control plane serves each licence its own edition's bundle). One node only: a
            # pair is Enterprise from the start (v01.00.00).
            if runs_enterprise():
                raise AgentError("this node already runs the Enterprise edition")
            gated = _close_gate(name)
            op.data["ref"] = ref
            from accounts import appliance

            op.step("backup", lambda: _backup_or_fail(appliance, name))
            op.step(
                f"this node: install {ref}, the Enterprise release (with the database changes)",
                lambda: install_ref(ref, migrate=True),
            )
        elif name == "rotate-key":  # 2026-11-14: the key to the stored secrets, changed from the portal (keyrotate)
            if thisnode.read().get("ROLE", "active") != "active":
                raise AgentError("run it on the active node")
            gated = _close_gate(name)
            from accounts import keyrotate

            keyrotate.run(op, thisnode.read().get("PEER_ADDR", ""), peer_request)
            _restart_later()  # this agent still holds the old key
        else:
            peer = _pair_required().run(op, name, ref)
        op.finish(True)
    except Exception as exc:  # noqa: BLE001 - kept in the operation
        op.finish(False, str(exc))
        if _pair():
            _pair().after_failure(op, name)
    finally:
        if gated:  # core-046: new work is admitted again once the operation has ended, done or failed
            try:
                from accounts import maintenance

                maintenance.open_(name)
            except Exception:  # noqa: BLE001 - a gate left closed opens by itself (maintenance.STALE_HOURS)
                pass
        elif op.data.get("gate"):  # enterprise-023: a switch over's - here or on the node that serves now
            op.data["gate_opened"] = _open_gate(name)
    try:
        # a switch over left this node a standby (read only): the log entry goes to the node that serves now
        target = peer if (peer and thisnode.read().get("ROLE") == "standby") else None
        details = {
            "operation": name,
            "state": op.data["state"],
            "by": by,
            "error": op.data.get("error", ""),
            "steps": "; ".join(f"{s['name']}: {s['status']}" for s in op.data["steps"]),
            "ref": op.data.get("ref", ""),  # integration-005: the release, also when no install step names it
        }
        if target:
            peer_request(target, "POST", "/note", {"action": f"ha_{name.replace('-', '_')}", "details": details})
        elif thisnode.read().get("ROLE") in ("active", "maintenance") and not _ro():
            note(f"ha_{name.replace('-', '_')}", details)
    except Exception:  # noqa: BLE001 - the log entry is a courtesy
        pass
    return op.data


def _backup_or_fail(appliance, operation=None):
    r = appliance.backup(operation=operation)  # option A: the operation's own backup, inside the gate it closed
    if not str(r).startswith("ok"):
        raise AgentError(f"backup: {r}")
    return r


def _ro():
    try:
        return sql1("SELECT @@super_read_only") == "1"
    except AgentError:
        return True


def _open_gate(name):
    """Open the maintenance gate an operation closed: here when this node's database is writable, else on the other
    node (a switch over made this one the standby; the gate's row is the serving node's). A gate left closed opens by
    itself after maintenance.STALE_HOURS. Returns where it was opened, or ""."""
    from accounts import maintenance

    try:
        if not _ro():
            maintenance.open_(name)
            return "this node"
    except Exception:  # noqa: BLE001 - the other node is tried
        pass
    peer = thisnode.read().get("PEER_ADDR", "")
    if peer:
        try:
            peer_request(peer, "POST", "/gate-open", {"op": name}, timeout=15)
            return "the other node"
        except Exception:  # noqa: BLE001 - stale after STALE_HOURS
            pass
    return ""


def _close_gate(name):
    """core-046 (twenty-third full review): before an operation that restarts the portal on this node, close the
    maintenance gate (no new work is admitted from now on) and only then count the running work; the gate stays closed
    until the operation ends (_run_op opens it). True when closed."""
    from accounts import maintenance

    try:
        maintenance.close(name)
    except RuntimeError as exc:
        raise AgentError(str(exc)) from exc
    return True


def _check_no_tasks():
    from accounts.tasks import running_count

    n = running_count()
    if n:
        raise AgentError(f"{n} task(s) running: wait until they finish")


# --- the server ------------------------------------------------------------------------------------------------


def handle(method, path, body):
    """(status, dict) of one request (no streams)."""
    if method == "GET" and path == "/status":
        return 200, status()
    if method == "GET" and path == "/op":
        return 200, read_op()
    if method == "GET" and path == "/installed":
        return 200, installed_status(fresh=False)
    if method == "GET" and path == "/backups":
        return 200, backups_index()
    if method != "POST":
        return 404, {"error": "not found"}
    if path == "/role":
        with role_lock():  # commit-017: the fence check and the change together
            if body.get("role") == "active" and thisnode.read().get("FENCED"):  # fenced after a takeover that stopped
                return 409, {"error": "this node was fenced after a takeover that stopped: promote it again first"}
            if body.get("kept_at") and _pair():  # enterprise-020: two Keep decisions at once - the earlier one wins
                refused = _pair().kept_first(body)
                if refused:
                    return refused
            return 200, {"result": set_role(body.get("role", ""))}
    if path == "/install-ref":
        return 200, {"result": install_ref(body.get("ref", ""), migrate=bool(body.get("migrate")))}
    if path == "/os-patch":
        return 200, os_patch(reboot=bool(body.get("reboot", True)))
    if path == "/env-key":  # rotate-key on the other node: the same keys in portal.env here, the application restarted
        from accounts import keyrotate

        value = body.get("value", "")
        if not keyrotate.valid(value):
            return 400, {"error": "not a list of Fernet keys"}
        keyrotate.write(value)
        keyrotate.restart_apps()
        _restart_later()
        return 200, {"result": "key set, application restarted"}
    if _pair():
        r = _pair().handle(path, body)
        if r is not None:
            return r
    if path == "/note":
        note(body.get("action", "ha_note"), body.get("details") or {})
        return 200, {"result": "noted"}
    if path == "/gate-open":  # enterprise-023: the operation that closed the gate ended on the other node
        if _ro():
            return 409, {"error": "this node's database is read only"}
        from accounts import maintenance

        maintenance.open_(str(body.get("op") or ""))
        return 200, {"result": "maintenance gate open"}
    if path == "/op":
        name = body.get("name", "")
        if name not in ops():
            return 400, {"error": f"unknown operation {name}"}
        ref = body.get("ref", "")
        if name in ref_ops() and not REF_RE.match(ref or ""):
            return 400, {"error": f"{name} needs a release tag"}
        try:  # integration-001: the operation is claimed before the answer, and the claim goes with it
            lock = claim()
        except AgentError:
            return 409, {"error": "another operation is running"}
        threading.Thread(target=_op_thread, args=(name, body.get("by", ""), ref, lock), daemon=True).start()
        return 202, {"result": f"{name} started"}
    return 404, {"error": "not found"}


def _op_thread(name, by, ref="", lock=None):
    from django.db import connection

    try:
        run_op(name, by, ref=ref, lock=lock)
    finally:
        connection.close()


def check_token(given):
    want = settings.HA_TOKEN
    return bool(want) and hashlib.sha256((given or "").encode()).digest() == hashlib.sha256(want.encode()).digest()


def serve(addr, port):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, fmt, *args):  # no request lines (they would only show the paths)
            pass

        def _json(self, status_code, data):
            raw = json.dumps(data, default=str).encode()
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _auth(self):
            if not check_token(self.headers.get("X-Claugine-Token")):
                self._json(403, {"error": "forbidden"})
                return False
            return True

        def _send_dump(self):
            """enterprise-004: the dump is made and checked first, then sent with its length and SHA-256."""
            try:
                path, size, sha = _pair().make_dump()
            except Exception as exc:  # noqa: BLE001
                return self._json(500, {"error": str(exc)[:300]})
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Length", str(size))
                self.send_header("X-Claugine-SHA256", sha)
                self.send_header("Connection", "close")
                self.end_headers()
                with open(path, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        self.wfile.write(chunk)
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass

        def _send_media(self):
            """T25: the installer ISO named in this node's state, for the other node (ee/ha_agent.hostmedia_pull)."""
            from accounts import hostmedia

            name = self.path.rsplit("/", 1)[-1]
            cur = hostmedia.held(name)  # commit-159: current, previous or pinned - only one held whole
            if not cur:
                return self._json(404, {"error": "not an installer ISO held here"})
            path = os.path.join(hostmedia.folder(), name)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(os.path.getsize(path)))
            self.send_header("X-Claugine-SHA256", cur.get("sha256", ""))
            self.send_header("Connection", "close")
            self.end_headers()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    self.wfile.write(chunk)

        def do_GET(self):  # noqa: N802
            if not self._auth():
                return
            if self.path == "/dump" and _pair():  # the database, for the other node to clone (Enterprise)
                return self._send_dump()
            if self.path.startswith("/hostmedia/") and _pair():  # T25: the installer ISO, for the other node
                return self._send_media()
            self._reply("GET", {})

        def do_POST(self):  # noqa: N802
            if not self._auth():
                return
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except ValueError:
                return self._json(400, {"error": "bad JSON"})
            self._reply("POST", body)

        def _reply(self, method, body):
            from django.db import connection

            try:
                code, data = handle(method, self.path, body)
            except Exception as exc:  # noqa: BLE001 - the caller sees what failed
                code, data = 500, {"error": str(exc)[:500]}
            finally:
                connection.close()
            self._json(code, data)

    httpd = ThreadingHTTPServer((addr, port), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(f"{TLS_DIR}/portal.crt", f"{TLS_DIR}/portal.key")
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    httpd.serve_forever()


# --- what the portal (the web pages) uses ------------------------------------------------------------------------


def nodes_status():
    """Both nodes as their agents report them: [(label, addr, status or {"error"})]."""
    n = thisnode.env()
    out = []
    pairs = [("this node", n.get("ADDR", ""))] + ([("other node", n.get("PEER_ADDR", ""))] if _pair() else [])
    for label, addr in pairs:
        if not addr:
            continue
        try:
            out.append((label, addr, peer_request(addr, "GET", "/status", timeout=10)))
        except AgentError as exc:
            out.append((label, addr, {"error": str(exc)}))
    return out


def start_op(name, by, ref=""):
    """Ask this node's agent to run an operation (ref: the release of update-portal)."""
    addr = thisnode.env().get("ADDR", "") or "127.0.0.1"
    return peer_request(addr, "POST", "/op", {"name": name, "by": by, "ref": ref}, timeout=30)


def backups_index():
    """{local: [[name, size, iso-time], ...], s3: [name, ...]}, read here as root. The agent runs as root, so it can
    read the backup folders (owned by the backups owner, 0750) that the web process — www-data — cannot."""
    from accounts import appliance, fe_backup

    s = appliance._settings()

    def rows(items):
        return [[n, size, (at.isoformat() if hasattr(at, "isoformat") else (at or ""))] for n, size, at in items]

    return {
        "local": rows(appliance.last_local(s)),
        "s3": appliance.s3_backups(s),
        "fe_local": rows(fe_backup.last_local(s)),
        "fe_s3": fe_backup.s3_backups(s),
    }


def backups_for_page(s):
    """Settings → Backups: the available backups via this node's agent (root reads the folders), or a direct read
    when the agent is not reachable (single-host / development)."""
    try:
        addr = thisnode.env().get("ADDR", "") or "127.0.0.1"
        r = peer_request(addr, "GET", "/backups", timeout=20)
        return {
            "local": r.get("local") or [],
            "s3": r.get("s3") or [],
            "fe_local": r.get("fe_local") or [],
            "fe_s3": r.get("fe_s3") or [],
        }
    except Exception:  # noqa: BLE001 - display only: a read problem must never blank the Settings page
        from accounts import appliance, fe_backup

        try:
            return {
                "local": appliance.last_local(s),
                "s3": appliance.s3_backups(s),
                "fe_local": fe_backup.last_local(s),
                "fe_s3": fe_backup.s3_backups(s),
            }
        except Exception:  # noqa: BLE001
            return {"local": [], "s3": [], "fe_local": [], "fe_s3": []}


# --- the Enterprise edition's part ------------------------------------------------------------------------------


def _pair():
    """ee/ha_agent.py — the operations with another node — or None in a build without ee/ (Standard)."""
    from accounts import edition

    return edition.ee("ha_agent")


def _pair_required():
    p = _pair()
    if p is None:
        raise AgentError("this needs the Enterprise edition (a second node)")
    return p


def ops():
    """The operations this build runs: one node's, and with ee/ the pair's."""
    p = _pair()
    return BASE_OPS + (p.PAIR_OPS if p else ())


def ref_ops():
    """The operations that take a release (ref)."""
    p = _pair()
    return BASE_REF_OPS + (p.PAIR_REF_OPS if p else ())


def __getattr__(name):
    # OPS / REF_OPS as module attributes for the pages: computed on use, because ee/ha_agent.py imports this module
    # (a value fixed while it is still being imported would miss the pair's operations).
    if name == "OPS":
        return ops()
    if name == "REF_OPS":
        return ref_ops()
    raise AttributeError(name)
