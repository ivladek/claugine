"""The front-end nodes' IPs (owner 2026-11-15: "prod must store fe nodes ip").

A platform keeps its three nodes' names (fe["nodes"]) and IPs (fe["node_ips"]); the IPs are entered in the platform's
Edit dialog. One that is still empty is taken from the last collection - OpenNebula's zone server pool, where each
node is listed with the address of its XML-RPC endpoint (adoption.collector frontends) - matched by the node's short
name. A stored IP is never overwritten: a corrected one stays as it was set.
"""


def _short(name):
    return str(name or "").strip().lower().split(".")[0]


def fill(fe, frontends):
    """(fe, changed): fe with its empty node IPs taken from the collected frontends [{name, addr, ...}]."""
    fe = dict(fe or {})
    names = list(fe.get("nodes") or [])[:3]
    ips = [str(x or "") for x in (fe.get("node_ips") or [])][: len(names)]
    ips += [""] * (len(names) - len(ips))
    changed = False
    for i, name in enumerate(names):
        if ips[i]:
            continue
        m = next((f for f in frontends or [] if f.get("addr") and _short(f.get("name")) == _short(name)), None)
        if m is not None:
            ips[i], changed = str(m["addr"]), True
    if changed:
        fe["node_ips"] = ips
    return fe, changed
