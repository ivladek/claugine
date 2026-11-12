"""The node agent: what root does on an app node of the portal (see accounts/ha.py).

It runs as root (systemd `claugine-ha`, `manage.py ha_agent serve`) on this node's internal address, port
HA_AGENT_PORT (portal.env), over TLS (the portal's own certificate), and answers only requests that carry the shared
token (HA_TOKEN in portal.env); ufw lets only the other app node in. The appliance timer (root) also calls run_op()
directly for the automatic updates.

    GET  /status            role, site, version, database, running tasks, last operation
    GET  /op                the last operation run here (steps)
    GET  /installed, /backups  for the Settings pages (root reads what www-data cannot)
    POST /role              {"role": …}
    POST /install-ref       {"ref": …, "migrate": bool}: install a release here
    POST /os-patch, /note
    POST /op                {"name": …}: start an operation here, step by step

**Editions split, step 3 (2026-11-11):** everything that only means something with a second node — MySQL
replication, the database copy (/dump, /replicate, /copy, /promote, /wait-gtid, /install-from), the switchover, the
standby's updates, a new version under test, the pair's upgrade to the Enterprise releases — is `ee/ha_agent.py`,
reached through `_pair()`. A build without `ee/` (Standard) is one node: it updates itself (update-single) and can
upgrade itself to the Enterprise releases. ee/ha_agent.py calls this module's functions as `base.<name>`, so a test
that patches `accounts.ha_agent.<name>` governs both.

The portal itself comes from the control plane's bundles only; the nodes never pass code to each other.
"""

import hashlib
import json
import os
import re
import ssl
import subprocess
import threading
import time
import urllib.request
from datetime import UTC, datetime

from django.conf import settings

from . import ha, thisnode

PREFIX = "/opt/claugine-portal"
STATE_DIR = "/var/lib/claugine-ha"
OP_FILE = f"{STATE_DIR}/op.json"
TLS_DIR = "/etc/claugine-portal/tls"
BASE_OPS = ("update-single", "upgrade-enterprise")
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
    c.verify_mode = ssl.CERT_NONE  # the nodes' self-signed certificates; the token authenticates
    return c


def peer_request(addr, method, path, body=None, raw=False, timeout=60):
    data = json.dumps(body or {}).encode() if method == "POST" else None
    req = urllib.request.Request(
        f"https://{addr}:{settings.HA_AGENT_PORT}{path}",
        data=data,
        method=method,
        headers={"X-Claugine-Token": settings.HA_TOKEN, "Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=_ctx())
    except urllib.error.HTTPError as exc:
        try:
            msg = json.loads(exc.read() or b"{}").get("error", "")
        except ValueError:
            msg = ""
        raise AgentError(f"{addr}{path}: HTTP {exc.code} {msg}".strip()) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise AgentError(f"{addr}{path}: {getattr(exc, 'reason', exc)}") from exc
    if raw:
        return resp
    return json.loads(resp.read() or b"{}")


def status():
    n = thisnode.read()
    out = {
        "role": n.get("ROLE", "active"),
        "site": n.get("SITE", ""),
        "addr": n.get("ADDR", ""),
        "peer": n.get("PEER_ADDR", ""),
        "version": version(),
        "commit": commit(),
        "db": db_status(),
        "op": read_op(),
        "time": time.time(),
        "reboot_required": os.path.exists("/var/run/reboot-required"),
    }
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
        **{k: (v if isinstance(v, (int, float, bool)) else str(v)[:300]) for k, v in (details or {}).items()},
    )


# --- operations -----------------------------------------------------------------------------------------------


def read_op():
    try:
        with open(OP_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


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
        os.makedirs(STATE_DIR, mode=0o750, exist_ok=True)
        tmp = OP_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=1)
        os.replace(tmp, OP_FILE)

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


def run_op(name, by="", ref=""):
    """Run an operation here (in a thread of the agent, or the appliance timer). Steps are kept in op.json and noted
    in the portal's log. One node: update-single and upgrade-enterprise. The operations with another node (switchover,
    the standby's updates, a new version under test...) are the Enterprise edition's (ee/ha_agent.py, step 3 of the
    editions split, 2026-11-11): without ee/ they are not in OPS."""
    if read_op().get("state") == "running":
        raise AgentError("another operation is running")
    if name not in ops():
        raise AgentError(f"unknown operation {name}")
    op = Op(name, by)
    peer = None
    try:
        if name == "update-single":  # 2026-11-10: a node with no other node (every Standard installation)
            if thisnode.read().get("PEER_ADDR"):
                raise AgentError("this node has another node: update the standby first (update-standby)")
            _check_no_tasks()
            op.data["ref"] = ref
            from accounts import appliance

            op.step("backup", lambda: _backup_or_fail(appliance))
            op.step(f"this node: install {ref} (with the database changes)", lambda: install_ref(ref, migrate=True))
        elif name == "upgrade-enterprise":  # a Standard node whose licence is now Enterprise: it installs the
            # Enterprise release (the control plane serves each licence its own edition's bundle). One node only: a
            # pair is Enterprise from the start (v01.00.00).
            if runs_enterprise():
                raise AgentError("this node already runs the Enterprise edition")
            _check_no_tasks()
            op.data["ref"] = ref
            from accounts import appliance

            op.step("backup", lambda: _backup_or_fail(appliance))
            op.step(
                f"this node: install {ref}, the Enterprise release (with the database changes)",
                lambda: install_ref(ref, migrate=True),
            )
        else:
            peer = _pair_required().run(op, name, ref)
        op.finish(True)
    except Exception as exc:  # noqa: BLE001 - kept in the operation
        op.finish(False, str(exc))
        if _pair():
            _pair().after_failure(op, name)
    try:
        target = peer if (op.data["state"] == "done" and name in ("switchover", "new-to-active")) else None
        details = {
            "operation": name,
            "state": op.data["state"],
            "by": by,
            "error": op.data.get("error", ""),
            "steps": "; ".join(f"{s['name']}: {s['status']}" for s in op.data["steps"]),
        }
        if target:
            peer_request(target, "POST", "/note", {"action": f"ha_{name.replace('-', '_')}", "details": details})
        elif thisnode.read().get("ROLE") in ("active", "maintenance") and not _ro():
            note(f"ha_{name.replace('-', '_')}", details)
    except Exception:  # noqa: BLE001 - the log entry is a courtesy
        pass
    return op.data


def _backup_or_fail(appliance):
    r = appliance.backup()
    if not str(r).startswith("ok"):
        raise AgentError(f"backup: {r}")
    return r


def _ro():
    try:
        return sql1("SELECT @@super_read_only") == "1"
    except AgentError:
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
        return 200, {"result": set_role(body.get("role", ""))}
    if path == "/install-ref":
        return 200, {"result": install_ref(body.get("ref", ""), migrate=bool(body.get("migrate")))}
    if path == "/os-patch":
        return 200, os_patch(reboot=bool(body.get("reboot", True)))
    if _pair():
        r = _pair().handle(path, body)
        if r is not None:
            return r
    if path == "/note":
        note(body.get("action", "ha_note"), body.get("details") or {})
        return 200, {"result": "noted"}
    if path == "/op":
        name = body.get("name", "")
        if read_op().get("state") == "running":
            return 409, {"error": "another operation is running"}
        if name not in ops():
            return 400, {"error": f"unknown operation {name}"}
        ref = body.get("ref", "")
        if name in ref_ops() and not REF_RE.match(ref or ""):
            return 400, {"error": f"{name} needs a release tag"}
        threading.Thread(target=_op_thread, args=(name, body.get("by", ""), ref), daemon=True).start()
        return 202, {"result": f"{name} started"}
    return 404, {"error": "not found"}


def _op_thread(name, by, ref=""):
    from django.db import connection

    try:
        run_op(name, by, ref=ref)
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

        def _stream(self, proc, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in iter(lambda: proc.stdout.read(1 << 20), b""):
                self.wfile.write(chunk)
            proc.wait()

        def do_GET(self):  # noqa: N802
            if not self._auth():
                return
            if self.path == "/dump" and _pair():  # the database, for the other node to clone (Enterprise)
                return self._stream(_pair().dump_stream(), "application/gzip")
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
    """Ask this node's agent to run an operation (ref: the release of update-standby / update-active)."""
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
