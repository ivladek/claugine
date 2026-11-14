"""The key to the stored secrets, changed from the portal (job 2026-11-14_from-claugine-web-console-procedures,
decision 0010: no routine work on the console).

FIELD_ENCRYPTION_KEY in portal.env is one Fernet key or several, comma-separated (inventory/crypto.py): the first
encrypts, all decrypt. Settings → Portal → "Change the key" runs the node operation ``rotate-key`` (accounts/ha_agent.py,
as root, on this node, the active one of a pair), step by step:

    1. a backup (with the old key, which it carries);
    2. a new key first, the old ones after it, in portal.env here and on the other node (its agent: POST /env-key);
       the application restarted on both - every secret still opens;
    3. every stored secret encrypted again with the new key (manage.py rotate_keys, in a process that reads the new
       portal.env);
    4. the old keys removed here and on the other node, the application restarted on both;
    5. a backup with the new key; the agent restarts itself a little later (it held the old key).

A failure in 3 leaves step 2's keys in place: nothing is lost and the operation can run again. The backups made
before keep the key they were made with.
"""

import os
import re
import subprocess

ENV_FILE = os.environ.get("PORTAL_ENV_FILE", "/etc/claugine-portal/portal.env")
NAME = "FIELD_ENCRYPTION_KEY"
APPS = ("claugine-app", "claugine-app-test")
KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{43}=$")  # a Fernet key: 32 bytes, URL-safe base64


class KeyError_(RuntimeError):
    pass


def valid(value):
    keys = [k.strip() for k in (value or "").split(",")]
    return bool(keys) and all(KEY_RE.match(k) for k in keys)


def read(path=ENV_FILE):
    """The keys in portal.env, as written there (the process's own environment may hold older ones)."""
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if line.startswith(NAME + "="):
                return line.split("=", 1)[1].strip()
    return ""


def write(value, path=ENV_FILE):
    """Set FIELD_ENCRYPTION_KEY in portal.env, keeping every other line, the owner and the mode; atomic."""
    if not valid(value):
        raise KeyError_("not a list of Fernet keys")
    st = os.stat(path)
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    out, done = [], False
    for line in lines:
        if line.strip().startswith(NAME + "="):
            out.append(f"{NAME}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{NAME}={value}")
    tmp = path + ".new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, st.st_mode & 0o777)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, path)


def restart_apps():
    r = subprocess.run(["systemctl", "restart", *APPS], capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise KeyError_(f"restart: {(r.stderr or r.stdout).strip()[-200:]}")
    return "restarted"


def manage(*args, timeout=1800):
    """manage.py in a new process that reads portal.env as it is now: the agent's own environment still holds the
    keys it started with, and portal/settings.py never overrides what the environment has."""
    env = {k: v for k, v in os.environ.items() if k != NAME}
    r = subprocess.run(
        ["claugine-manage-root", *args], capture_output=True, text=True, timeout=timeout, env=env, check=False
    )
    if r.returncode != 0:
        raise KeyError_(f"{' '.join(args)}: {(r.stderr or r.stdout).strip()[-300:]}")
    return (r.stdout or "").strip().splitlines()[-1:] or ["ok"]


def new_key():
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def set_here_and_there(value, peer, request):
    """portal.env here and on the other node (when there is one), then the application restarted on both."""
    write(value)
    restart_apps()
    if peer:
        request(peer, "POST", "/env-key", {"value": value}, timeout=240)
    return "both nodes" if peer else "this node"


def run(op, peer, request):
    """The steps of rotate-key in ``op`` (accounts.ha_agent.Op). ``peer``: the other node's address or ""."""
    from accounts import appliance

    def backup():
        r = appliance.backup()
        if not str(r).startswith("ok"):
            raise KeyError_(f"backup: {r}")
        return r

    old = read()
    if not valid(old):
        raise KeyError_("portal.env has no valid FIELD_ENCRYPTION_KEY")
    new = new_key()
    op.step("backup", backup)
    op.step("the new key first, the old one kept", lambda: set_here_and_there(f"{new},{old}", peer, request))
    op.step("every stored secret encrypted again with the new key", lambda: manage("rotate_keys")[0])
    op.step("the old key removed", lambda: set_here_and_there(new, peer, request))
    op.step("backup with the new key", lambda: manage("appliance", "--backup-now")[0])
    return new
