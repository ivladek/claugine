"""Changing the configuration of a running tenant router (egw, igw, cgw) over ssh.

The configuration is always built from Infrastructure → Templates. To change a router the portal

1. reads its running configuration (`show configuration commands`) and its interfaces (`ip -o link`);
2. builds the configuration before the change and after it;
3. sets what the new configuration has and the router lacks, and deletes what the old configuration put
   there and the new one no longer has (nothing else is ever deleted: first-boot settings and manual
   additions stay), at the highest node whose lines all go;
4. commits and saves in one `vbash` session; a failed commit is discarded;
5. reads the configuration again and reports what is still missing.

Secrets never leave this module: reported lines have them masked.
"""
import re

from .configcheck import FIRST_BOOT, SHOW, _norm, _secrets, compare, mask
from .deploy import DeployError

LINK = "ip -o link show"
HEAD = "source /opt/vyatta/etc/functions/script-template\nconfigure\n"
TAIL = ('if ! commit; then echo "CLAUGINE-COMMIT-FAILED"; discard; exit 1; fi\n'
        "save >/dev/null\nexit\n")
TIMEOUT = 120


def login(tenant, role):
    """(host, port, user, private key, site) to log in to the tenant's router of this role."""
    from .configcheck import targets
    site = (tenant.order or {}).get("site")
    for r, host, port, user, key in targets(tenant):
        if r == role:
            if not user or not key:
                raise DeployError(f"{role}: no login (user or key)")
            return host, port, user, key, site
    raise DeployError(f"the tenant has no {role}")


def ssh(target, remote, stdin=None, timeout=60):
    from inventory.sshexec import SshError, run
    host, port, user, key, site = target
    try:
        return run(host, port, user, key, remote, site=site, stdin=stdin, timeout=timeout)
    except SshError as exc:
        raise DeployError(str(exc)) from exc


def interfaces(target):
    """{mac: interface name} of the router's ethernet interfaces."""
    out = {}
    for line in ssh(target, LINK).splitlines():
        m = re.match(r"^\d+:\s+([^:@\s]+)[:@].*link/ether\s+([0-9a-f:]{17})", line.strip())
        if m and m.group(1).startswith("eth"):
            out[m.group(2).lower()] = m.group(1)
    return out


def _tokens(line):
    return _norm(line).split()


def obsolete_for(role):
    """Nodes an older template put on routers of this role that the current one replaces (deleted when present)."""
    from inventory import nf_templates
    return {"egw": nf_templates.EGW_OBSOLETE}.get(role, ())


def plan(before, after, actual, keep=FIRST_BOOT, obsolete=()):
    """(deletes, sets): commands that turn the router from `before` into `after`, given its running lines. Lines
    matching `keep` are never deleted. `obsolete`: nodes ("qos policy limiter LIMIT-IN") deleted when the router has
    them and `after` does not (what an older template put there; correction 17)."""
    after_set = {_norm(x) for x in after.splitlines() if x.startswith("set ")}
    before_set = {_norm(x) for x in (before or "").splitlines() if x.startswith("set ")}
    running = [x for x in actual.splitlines() if x.startswith("set ")]
    running_norm = [_norm(x) for x in running]
    running_set = set(running_norm)
    sets = [x for x in after.splitlines() if x.startswith("set ") and _norm(x) not in running_set]
    gone = [(raw, n) for raw, n in zip(running, running_norm)
            if n in before_set and n not in after_set and not keep.match(n)]
    gone_set = {n for _r, n in gone}
    deletes, covered = [], set()
    for raw, n in gone:
        if n in covered:
            continue
        toks = n.split()
        cmd = None
        for k in range(3, len(toks)):  # the highest node all of whose running lines go
            prefix = " ".join(toks[:k]) + " "
            under = [x for x in running_norm if x.startswith(prefix)]
            if under and all(x in gone_set for x in under):
                cmd = "delete " + " ".join(toks[1:k])
                covered.update(under)
                break
        if cmd is None:
            cmd = "delete " + raw[4:]
            covered.add(n)
        if cmd not in deletes:
            deletes.append(cmd)
    for node in obsolete:
        prefix = "set " + node
        if any(x == prefix or x.startswith(prefix + " ") for x in running_norm) and not any(
                x == prefix or x.startswith(prefix + " ") for x in after_set):
            deletes = [d for d in deletes if not d.startswith("delete " + node)] + ["delete " + node]
    # a node deleted whole makes deleting its lines one by one needless
    deletes = [d for d in deletes if not any(d != o and d.startswith(o + " ") for o in deletes)]
    return deletes, sets


def script(deletes, sets):
    return HEAD + "".join(d + "\n" for d in deletes) + "".join(x + "\n" for x in sets) + TAIL


def apply(tenant, role, build, dry=False):
    """Change the router from its configuration before the change to the one after it.

    `build(ifaces, phase)` returns the configuration text for phase "before" or "after"; it gets
    {mac: interface} of the router so interfaces are named as the router names them.
    Returns a short report (counts; masked lines when something is left).
    """
    target = login(tenant, role)
    ifaces = interfaces(target)
    before, after = build(ifaces, "before"), build(ifaces, "after")
    actual = ssh(target, SHOW)
    deletes, sets = plan(before, after, actual, obsolete=obsolete_for(role))
    if not deletes and not sets:
        return f"{role}: nothing to change"
    if dry:
        secrets = _secrets()
        return f"{role}: would delete {len(deletes)}, set {len(sets)}: " + "; ".join(mask(deletes + sets, secrets))[:1500]
    out = ssh(target, "vbash -s", stdin=script(deletes, sets), timeout=TIMEOUT)
    secrets = _secrets()
    if "CLAUGINE-COMMIT-FAILED" in out:
        tail = mask([x for x in out.splitlines() if x.strip()][-3:], secrets)
        raise DeployError(f"{role}: commit failed, changes discarded: " + " | ".join(tail)[:400])
    missing, _extra = compare(after, ssh(target, SHOW))
    note = f"{role}: deleted {len(deletes)}, set {len(sets)}"
    if missing:
        raise DeployError(note + f"; still missing {len(missing)}: " + "; ".join(mask(missing[:3], secrets))[:400])
    return note + ", verified"


def nic_ifaces(one, vm_id, ifaces):
    """{network name: interface} of a VM's NICs, from OpenNebula (network, MAC) and the router (MAC -> eth)."""
    vm = one.xml("one.vm.info", vm_id, False)
    out = {}
    for n in vm.findall("TEMPLATE/NIC"):
        mac = (n.findtext("MAC") or "").lower()
        if mac in ifaces:
            out[n.findtext("NETWORK") or ""] = ifaces[mac]
    return out


__all__ = ["apply", "plan", "obsolete_for", "egw_shaper", "script", "interfaces", "nic_ifaces", "login"]


def egw_shaper(tenant, user=None, dry=False):
    """Correction 17: configure the tenant's egw from the current template — its bandwidth (the connectivity record)
    as the shaper, the older limiter and rate-control deleted. Returns the report."""
    from inventory import nfconfig
    from accounts.models import audit

    def build(ifaces, phase):
        return nfconfig.build("egw", tenant=tenant, reveal=True)
    out = apply(tenant, "egw", build, dry=dry)
    if not dry:
        audit(user, "egw_shaper_applied", tenant, result=out[:300])
    return out
