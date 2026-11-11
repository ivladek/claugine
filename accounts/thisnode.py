"""This node's own identity: what `/etc/claugine-portal/node.env` says about the machine the portal runs on.

Split out of `accounts/ha.py` on 2026-11-09 (editions, Phase 4 step 1). `ha.py` was two modules wearing one name:
**this node's identity**, which every installation needs and which has nothing to do with high availability, and
**the pair**, which only the Enterprise edition has. Eighteen modules imported `ha` and nine of the eleven things
they called were identity — `read_node` 17 times, `node` 8, `forget` 6, `write_node` 4. Those callers now come here,
so the Enterprise code can later move into `ee/` without dragging the single-node portal with it.

Nothing in this module is edition-specific, and it must stay that way: a Standard installation reads its own
`node.env` exactly as an Enterprise one does.

    SITE=dc1                              this node's site
    SERVER_ID=1                           MySQL server id (unique per node)
    ADDR=10.81.101.25                     this node's internal address
    PEER_SITE=dc2                         the other app node (empty: one node only)
    PEER_ADDR=10.82.101.25
    LB_ADDRS=10.81.101.24,10.82.101.24    the HAProxy VMs allowed to reach the status page and the portal
    ROLE=active                           active | standby | maintenance | new  (read through accounts/ha.py)
    HTTP_PORT=8080                        the portal's Apache ports on this node (install.sh keeps them here;
    HTTPS_PORT=8443                       defaults 8080, 8443)

The file is **never replicated**, unlike the database: it is what distinguishes one node from the other. It is
written by root only — the HA agent and the installer — through `write()`. Without it the node is a single active
portal, which is also what a fresh Standard installation looks like.
"""
import os
import time

from django.conf import settings

_CACHE = {"at": 0.0, "mtime": "unread", "data": {}}


def path():
    """Where node.env is: NODE_ENV_FILE from portal.env, the environment, or the default."""
    return getattr(settings, "NODE_ENV_FILE", "") or os.environ.get("NODE_ENV_FILE", "/etc/claugine-portal/node.env")


def read(file=None):
    """node.env as a dict, straight from disk. A missing or unreadable file is an empty dict, never an error."""
    data = {}
    try:
        with open(file or path(), encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    data[k.strip()] = v.strip()
    except OSError:
        pass
    return data


def env():
    """This node's settings, cached: re-read when node.env changes, checked at most every 2 seconds."""
    now = time.monotonic()
    if now - _CACHE["at"] > 2:
        _CACHE["at"] = now
        try:
            mtime = os.path.getmtime(path())
        except OSError:
            mtime = None
        if mtime != _CACHE["mtime"]:
            _CACHE["mtime"], _CACHE["data"] = mtime, read()
    return _CACHE["data"]


def forget():
    """Drop the cache: after write(), and in tests that move node.env about."""
    _CACHE["at"], _CACHE["mtime"], _CACHE["data"] = 0.0, "unread", {}


def write(values, file=None):
    """Change node.env (root only: the HA agent and the installer). Keeps the lines it is not given.

    Written atomically — a half-written node.env would leave a node without an identity.
    """
    file = file or path()
    data = read(file)
    data.update({k: str(v) for k, v in values.items() if v is not None})
    tmp = f"{file}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("# This node of the Claugine portal (accounts/thisnode.py). Managed by the installer and the HA agent.\n")
        for k, v in data.items():
            fh.write(f"{k}={v}\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, file)
    forget()
    return data
