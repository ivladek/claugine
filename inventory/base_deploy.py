"""Redeploying a base-OS VM of a core service from its service profile (T34 B4; owner 2026-11-21 13:01: "redeploy -
simple action using service profile and instance specific data").

A VM of ns, logs, admin, backup (and onefe, B6) is made again from scratch: the platform's base-os image (Settings /
Platforms: Ubuntu 26.04 now), the instance's own data (the node's name, host name and addresses, the service's size
and NICs, its secrets, the site's storage) and the service profile's recipe (Profiles → Services) rendered into a
first-boot script. Nothing is taken from the old VM: it is only stepped aside and kept.

    offer     whether the button is offered: what is missing (profile, image, addresses, every value its recipe
              uses); reads only the portal
    plan      what would be done; reads the platform, nothing changes
    redeploy  1 the old VM (when there is one) is powered off, its NICs on the addresses the new VM takes are detached,
                it is renamed <name>-old and kept
              2 the new VM from the base-os image: the service's NICs with the node's addresses (an empty one is
                assigned by the platform), the recipe as START_SCRIPT_BASE64 - it starts the first boot as its own
                unit (claugine-firstboot), so the VM boots and answers ssh meanwhile
              3 wait until the first boot ends (/var/lib/claugine/firstboot.ok) and the profile's checks pass
              4 any failure in 2-3: rolled back by itself (the new VM removed, the old one back with its NICs)
              5 the script (secrets included) leaves the VM's context
    rollback  the same by hand while <name>-old exists (core_deploy.rollback sends it here)
Removing the old VM and the init script: core_deploy.finish / clean_context, as for VyOS.

A front end (onefe) takes its data from the zone's leader and joins the zone again (inventory/fe_deploy.py, B6); a name
server is offered once Infrastructure → DNS has a primary (B5: its values, the primary's zone files, commit-301).
Production redeploys wait for the owner's yes per VM.
"""

import base64
import time

from django.utils.translation import gettext as _

from .core_deploy import (
    AUTOSTART,
    SCHED_KEYS,
    RedeployError,
    _nics,
    _one,
    _powered_off,
    _reread,
    _schedule_text,
    _vm,
    _wait,
    put_schedule,
    set_autostart,
    take_schedule,
)
from .models import Cluster, CoreService, ItemState, ServiceProfile

T = CoreService.Type
TYPES = (T.NS, T.LOGS, T.ADMIN, T.BACKUP, T.ONEFE, T.CLAUGINE)  # made from a service profile (claugine: T36 B3b)
START_KEY = "START_SCRIPT_BASE64"
INTENT_KEY = "CLAUGINE_RESTORE"  # commit-351: a Restore all's tag on the VMs it asks for (USER_TEMPLATE)
STATE = "/var/lib/claugine"
LOG = "/var/log/claugine-firstboot.log"
STEP = f"{STATE}/firstboot.step"  # commit-312: the current step's name, written by the recipe's step function only
FIRSTBOOT_TIMEOUT = 2700  # seconds: packages of a desktop or Graylog take long
POLL = 15


def _poll():
    from django.conf import settings

    return 0 if not getattr(settings, "DEPLOY_ASYNC", True) else POLL


def is_base(node):
    """A VM of a profile-made core service (not VyOS, not a host)."""
    svc = node.service
    return svc.stype in TYPES and svc.kind == CoreService.Kind.VM and node.base_os


def profile_of(svc):
    """The profile the site service is made from (T34 S3b: its link; a service saved before it: the type's)."""
    if svc.profile_id:
        return svc.profile
    return ServiceProfile.of_type(svc.stype)


def templates_of(profile):
    """{id: ConfigTemplate} of the templates the profile uses (its units' installations and configs; T34 S2, T36 B5a -
    owner 2026-11-26 12:09: by id); a VyOS profile's VyOS template."""
    from .models import ConfigTemplate

    if profile.is_vyos:
        return {t.pk: t for t in ConfigTemplate.objects.filter(role__in=profile.template_keys())}
    return ConfigTemplate.objects.in_bulk(profile.template_ids())


def role_of(node, prof):
    """commit-560: the role of the profile a VM of the service is: its params' role, or the profile's only one (None:
    a service whose roles are to fill). A VM of a several-role service without one is an error (RedeployError)."""
    names = prof.role_names()
    want = (node.params or {}).get("role")
    if want in names:
        return want
    if len(names) <= 1:
        return names[0] if names else None
    raise RedeployError(
        _("%(n)s has no role of %(p)s (%(r)s): which role is it?")
        % {"n": node.name, "p": prof.key, "r": ", ".join(names)}
    )


def units_of(node, prof):
    """The units a VM installs: its own role's (commit-560)."""
    return prof.units(role_of(node, prof))


# --- the instance's values -----------------------------------------------------------------------------------------


def _nfs(volume):
    if volume is None or volume.storage.kind != "nfs" or not volume.storage.targets or not volume.device:
        return ""
    return f"{volume.storage.targets[0]}:{volume.device}"


def storage_of(svc):
    """The site's storage the recipe takes (site → Storage): logs and admin their NFS volume; backup its datastore's
    iSCSI LUN - the portals, the instance's initiator name (the volume's client) and, from the form, the target IQN,
    volume group and logical volume (found, never formatted)."""
    from .models import PlatformDatastore, StorageVolume

    p = svc.params or {}
    out = {}
    vol_id = p.get("volume") or p.get("data_volume")
    if vol_id:
        out["nfs"] = _nfs(StorageVolume.objects.filter(pk=vol_id).select_related("storage").first())
    if svc.stype == T.BACKUP:
        ds = PlatformDatastore.objects.filter(pk=p.get("datastore") or 0).select_related("volume__storage").first()
        vol = ds.volume if ds else None
        if vol is not None and vol.storage.kind == "iscsi":
            st = vol.storage
            out["portals"] = [f"{t}:{st.port}" if st.port and st.port != 3260 else str(t) for t in st.targets or []]
            out["initiator"] = str((vol.clients or [""])[0] or "")
        elif vol is not None:
            out["nfs"] = _nfs(vol)
        for k in ("iqn", "vg", "lv"):
            out[k] = str(p.get(f"target_{k}" if k == "iqn" else k) or "")
    return out


def _keys(svc):
    """{ref: public key} of the users' keys: sa (the platform's super admins), platform (the served platform's
    oneadmin, which reaches a backup datastore)."""
    from tenants.deploy import with_public

    from .credentials import _public_key, account, value

    out = {}
    if svc.platform and svc.platform.sa:
        out["sa"] = with_public(account(svc.platform.sa)[1] or {}).get("public") or ""
    if svc.serves and svc.serves.fe_secrets:
        priv = value(svc.serves.fe_secrets, "service_user.oneadmin.key") or ""
        out["platform"] = _public_key(priv) if "PRIVATE KEY" in priv else priv.strip()
    return out


def ssh_port_of(svc):
    """The ssh port a machine of the service is built with: an admin VM's is Settings → Security's public SSH port
    (owner N3; commit-473: also at a Redeploy of an existing VM), any other the service's."""
    if getattr(svc, "ssh_port_now", None):  # commit-483: the port this redeploy resolved once, for recipe and checks
        return svc.ssh_port_now
    if svc.stype == "admin":
        from tenants.models import PortalSettings

        return PortalSettings.get().public_ssh_port
    return svc.ssh_port


# commit-570: values a recipe may take empty - no admin VM recorded yet (fail2ban then ignores loopback only)
MAY_BE_EMPTY = {("site", "admins")}


def admin_addresses():
    """The addresses of every admin VM of every site, not retired (owner 2026-11-26 11:29 QB5-5 "only admin hosts in
    ignore list")."""
    from .models import CoreServiceNode, ItemState

    return sorted(
        {
            a
            for a in CoreServiceNode.objects.filter(service__stype="admin")
            .exclude(state=ItemState.RETIRED)
            .values_list("address", flat=True)
            if a
        }
    )


def values_of(node, reveal=False):
    """The instance's values for its profile's recipe (recipes.render_script). Secrets only with reveal (masked
    otherwise). dns comes with Infrastructure → DNS (B5)."""
    from tenants.models import PortalSettings

    from .credentials import value
    from .nfconfig import management
    from .recipes import MASK
    from .timesrv import servers

    svc = node.service
    site = svc.site
    hosts, ports = management(site.code)
    ps = PortalSettings.get()
    nodes = [n.pk for n in svc.nodes.order_by("pk")]
    secrets = {}
    for name, ref in ((svc.params or {}).get("secrets") or {}).items():
        v = value(ref) if ref else None
        secrets[name] = (v if reveal else MASK) if v else ""
    out = {
        "site": {
            "code": site.code,
            "timezone": site.tz,
            "domain": ps.internal_domain or "",
            "internal": list(hosts),  # who may manage its systems (site settings)
            "admins": admin_addresses(),  # T36 B5c (QB5-5): fail2ban never bans the admin VMs
            "ntp": servers(site),  # T36 B5d (QB5-2): the site's time servers (the FE hosts of its mgmt/simple platform)
        },
        "node": {
            "name": node.name,
            "hostname": node.hostname or node.name,
            "ips": list(node.ips or []),
            "index": nodes.index(node.pk) + 1 if node.pk in nodes else 1,
            "ssh_port": ssh_port_of(svc),
            # T36 B5c (owner N3, rules v2 C6): an admin VM's RDP is Settings → Security's public RDP port
            "rdp_port": ps.public_rdp_port if svc.stype == "admin" else (ports.get("rdp") or ""),
        },
        "service": {"name": svc.name, "stype": svc.stype},
        "platform": {"key": svc.platform.key if svc.platform else "", "served": svc.serves.key if svc.serves else ""},
        "os": {"name": "Ubuntu", "version": ps.baseline_os or ""},
        "storage": storage_of(svc),
        "secret": secrets,
        "keys": _keys(svc),
    }
    if svc.stype == T.NS:  # T34 B5: Infrastructure → DNS (the primary gets the zone files)
        from . import dns

        d = dns.values(node, reveal)
        if d is not None:
            out["dns"] = d
    return out


def missing(node, values=None):
    """What the recipe needs and the instance does not give: ["storage.iqn", "secret rdp", …] ([] = complete)."""
    from .recipes import references

    svc = node.service
    prof = profile_of(svc)
    if prof is None:
        return [_("no service profile for %(t)s (Profiles → Services)") % {"t": svc.stype}]
    templates = templates_of(prof)
    try:
        units = units_of(node, prof)
    except RedeployError as exc:
        return [str(exc)]
    gone = [str(k) for k in prof.template_ids(units) if k not in templates]
    if gone:
        return [_("the profile names templates that do not exist: #%(k)s") % {"k": ", #".join(gone)}]
    if not any(u.install_template_id or u.configs for u in units):  # T36 B5a: its units' installations
        return [_("nothing installs it: no installation in its OS or functions (Profiles → OS, Functions)")]
    used, keys = references(prof, templates, units=units)
    v = values if values is not None else values_of(node)
    out = []
    for ns, names in sorted(used.items()):
        if ns == "dns":  # T34 B5
            from .dns import OPTIONAL, domain

            if not domain():  # commit-356: the zones need it
                out.append(_("the internal domain (Settings → Customer)"))
                continue
            if "dns" not in v:
                out.append(_("the DNS values (Infrastructure → DNS: a primary name server)"))
                continue
            if v["dns"].get("waiting_for"):  # commit-354
                out.append(
                    _("the first redeploy of %(n)s as the primary has not ended: redeploy it again or roll it back")
                    % {"n": v["dns"]["waiting_for"]}
                )
                continue
            out += [f"dns.{n}" for n in sorted(names) if n not in OPTIONAL and v["dns"].get(n) in (None, "", [], {})]
            continue
        for name in sorted(names):
            got = (v.get(ns) or {}).get(name)
            if (ns, name) in MAY_BE_EMPTY and got is not None:  # commit-570: an empty list is a value
                continue
            if got in (None, "", [], {}):
                out.append(_("secret %(n)s (Edit)") % {"n": name} if ns == "secret" else f"{ns}.{name}")
    for ref in sorted(keys):
        if not (v.get("keys") or {}).get(ref):
            out.append(_("the %(r)s key (its secret)") % {"r": ref})
    return out


STORAGE_KINDS = ("nfs", "iscsi")  # a profile's "storage" NIC: the network of the instance's volume


def nic_rows(profile):
    """[(network, optional)] of a profile's NIC rows (owner 2026-11-23 09:47, Q22 A: "storage" is the network of the
    instance's volume - nfs or iscsi; an optional row is there when the instance has it)."""
    out = []
    for n in (profile.nics if profile else None) or []:
        if isinstance(n, dict) and n.get("network"):
            out.append((str(n["network"]), bool(n.get("optional"))))
    return out


def storage_kind(svc):
    """commit-399: the storage network ("nfs", "iscsi") of the instance's volume, as core_types builds its NIC - logs and
    admin their volume (params volume / data_volume), backup its datastore's volume (none on S3); None without one."""
    from .models import PlatformDatastore, StorageVolume

    p = svc.params or {}
    vol = None
    if svc.stype == T.BACKUP:
        ds = PlatformDatastore.objects.filter(pk=p.get("datastore") or 0).select_related("volume__storage").first()
        if ds is not None and ds.backend != PlatformDatastore.Backend.S3:
            vol = ds.volume
    elif p.get("volume") or p.get("data_volume"):
        vol = StorageVolume.objects.filter(pk=p.get("volume") or p.get("data_volume")).select_related("storage").first()
    kind = vol.storage.kind if vol is not None else ""
    return kind if kind in STORAGE_KINDS else None


def _fits(kind, network, storage):
    return (storage is not None and kind == storage) if network == "storage" else kind == network


def _matches(have, rows, storage):
    """commit-400, 401: True when the NICs fit the rows in order, each optional row either taken or passed over - the
    NIC positions reachable after each row, kept as a set (no recursion, so no depth limit)."""
    reach = {0}
    for network, optional in rows:
        nxt = {i + 1 for i in reach if i < len(have) and _fits(have[i], network, storage)}
        if optional:
            nxt |= reach
        reach = nxt
        if not reach:
            return False
    return len(have) in reach


def nic_mismatch(svc, nics=None):
    """commit-395, 398-400: the site service's NICs against its profile's rows, in order - an optional row may be
    passed over, "storage" is the network of the instance's volume. [] when they fit or the profile names none;
    otherwise where the first try (each NIC on the next row it fits) went wrong. A NIC's kind is its own or the one
    its name gives."""
    from .models import Network

    p = profile_of(svc)
    rows = nic_rows(p)
    if not rows:
        return []
    have = [(n.kind or Network.kind_for(n.name)) if n is not None else "" for n, _gw in nics or svc.nic_list()]
    storage = storage_kind(svc) if any(n == "storage" for n, _o in rows) else None
    if _matches(have, rows, storage):
        return []
    out, i = [], 0
    for network, optional in rows:
        if i < len(have) and _fits(have[i], network, storage):
            i += 1
        elif not optional:
            out.append(
                _("NIC %(i)s is on %(h)s, the profile %(p)s says %(w)s")
                % {
                    "i": i + 1,
                    "h": have[i] if i < len(have) else _("nothing"),
                    "p": p.key,
                    "w": f"storage ({storage or _('none')})" if network == "storage" else network,
                }
            )
            return out
    for j in range(i, len(have)):
        out.append(_("NIC %(i)s (%(h)s) is not in the profile %(p)s") % {"i": j + 1, "h": have[j] or "?", "p": p.key})
    # the first try fitting while the full match does not cannot happen; said plainly if it ever does
    return out or [_("the NICs do not fit the profile %(p)s") % {"p": p.key}]


def offer(node):
    """(offered, [reasons]) for the Redeploy button; reads only the portal."""
    from .core_types import image_of

    svc = node.service
    if not is_base(node):
        return False, [_("not a VM made from a service profile")]
    why = []
    if svc.stype == T.ONEFE:  # T34 B6: the platform served, its endpoint and the nodes' logins
        from .fe_deploy import offer_extra

        why += offer_extra(node)
    if svc.platform is None:
        why.append(_("no platform hosting the VMs"))
    else:
        img = image_of(svc.platform, "base-os")
        if img is None or img.one_id is None:
            why.append(_("%(p)s has no base-os image (site → Platforms → service images)") % {"p": svc.platform.key})
    nics = svc.nic_list()
    if not nics or any(n is None for n, _gw in nics):
        why.append(_("its NICs are not defined"))
    else:
        why += nic_mismatch(svc, nics)
    if not (node.ips or [""])[0]:
        why.append(_("no address on NIC 1"))
    if not svc.vcpu or not svc.ram_gb or not svc.disk_gb:
        why.append(_("its size (vCPU, RAM, disk) is not complete"))
    why += missing(node)
    return not why, why


# --- the script ----------------------------------------------------------------------------------------------------


def _firewall(svc):
    """The service's own ufw rules (Edit), after the recipe."""
    import shlex

    out = []
    for r in svc.firewall or []:
        port, proto, src = str(r.get("port") or ""), str(r.get("proto") or ""), str(r.get("source") or "")
        if not port:
            continue
        rule = (
            f"ufw allow from {shlex.quote(src)} to any port {shlex.quote(port)}"
            if src
            else f"ufw allow {shlex.quote(port)}"
        )
        out.append(rule + (f" proto {shlex.quote(proto)}" if proto else ""))
    return out


def script_of(node, reveal=False):
    """The recipe for this node: the profile with the instance's values, then the service's own firewall rules."""
    from .recipes import RecipeError, render_script

    svc = node.service
    prof = profile_of(svc)
    if prof is None:
        raise RedeployError(f"{svc.stype}: no service profile")
    try:
        text = render_script(
            prof,
            templates_of(prof),
            values_of(node, reveal),
            title=f"{svc.site.code} {svc.name} {node.name}",
            units=units_of(node, prof),
        )
    except RecipeError as exc:
        raise RedeployError(f"the recipe: {exc}") from exc
    fw = _firewall(svc)
    if fw:
        text = text.rstrip("\n").removesuffix('step "done"').rstrip("\n")
        text += '\nstep "firewall"\n' + "\n".join(fw) + '\nstep "done"\n'
    return text


def _heredoc(text):
    tag = "CLAUGINE_FIRSTBOOT"
    while tag in text:
        tag += "_"
    return tag


def first_boot(recipe):
    """The context's start script: once (a later boot finds firstboot.ok and does nothing), the recipe written to a
    private file and started as its own unit, so the contextualization ends and the VM boots on; the recipe's file
    (secrets) is removed when it ends."""
    tag = _heredoc(recipe)
    return (
        "#!/bin/bash\n"
        f"install -d -m 0700 {STATE}\n"
        f"[ -e {STATE}/firstboot.ok ] && exit 0\n"
        f"rm -f {STATE}/firstboot.failed\n"
        f"umask 077; cat > {STATE}/firstboot.sh <<'{tag}'\n{recipe.rstrip()}\n{tag}\n"
        f"cat > {STATE}/firstboot-run.sh <<'CLAUGINE_RUN'\n"
        "#!/bin/bash\n"
        f"touch {LOG}; chmod 0600 {LOG}\n"
        f"export CLAUGINE_STEP_FILE={STEP}\n"
        f"if bash {STATE}/firstboot.sh >> {LOG} 2>&1; then touch {STATE}/firstboot.ok; "
        f"else echo $? > {STATE}/firstboot.failed; fi\n"
        f"rm -f {STATE}/firstboot.sh\n"
        "CLAUGINE_RUN\n"
        f"systemd-run --unit claugine-firstboot --no-block bash {STATE}/firstboot-run.sh\n"
    )


# commit-312: only the exit code and the current step's name leave the VM - read from the step file the recipe's step
# function writes (service keys and paths, never values), never from the recipe's output, which stays in its log
STATUS = (
    f"step=$(head -n 1 {STEP} 2>/dev/null | tr -cd 'A-Za-z0-9 :/._@+-' | cut -c1-200); "
    f"if [ -e {STATE}/firstboot.ok ]; then echo OK; elif [ -e {STATE}/firstboot.failed ]; then "
    f'echo "FAILED exit $(tr -cd 0-9 < {STATE}/firstboot.failed) at $step"; else echo "RUNNING $step"; fi\n'
)


def check_script(profile):
    """The profile's checks: one line per failed check, nothing when all pass."""
    import shlex

    from .recipes import check_lines

    lines = []
    for c in check_lines(profile):
        lines.append(f"( {c} ) >/dev/null 2>&1 || echo FAIL {shlex.quote(c)}")
    return "\n".join(lines) + "\n"


# --- the platform --------------------------------------------------------------------------------------------------


def _vnet(one, net, site):
    name = f"{net.name}_{site}"
    v = one.find("one.vnpool.info", "VNET", name, -2, -1, -1)
    if v is None:
        raise RedeployError(f"network {name} not found on the platform")
    return int(v.findtext("ID")), name


def _leases(one, vnet_id):
    vn = one.xml("one.vn.info", vnet_id, False)
    return {lease.findtext("IP") for lease in vn.findall("AR_POOL/AR/LEASES/LEASE")}


def plan(node):
    """What redeploying the node would do; reads the platform only. {checks [{check, ok, detail, must}], ok, …}"""
    from .core_types import image_of

    svc = node.service
    site = svc.site.code
    if not is_base(node):
        raise RedeployError(f"{node.name}: not a VM made from a service profile")
    one, control = _one(svc)
    out = []

    def c(check, ok, detail="", must=True):
        out.append({"check": check, "ok": bool(ok), "detail": detail, "must": must})

    offered, why = offer(node)
    c("the instance gives every value of its profile", offered, "; ".join(why) or "complete")
    old = _vm(one, node.name)
    old_id = int(old.findtext("ID")) if old is not None else None
    old_nics = _nics(one.xml("one.vm.info", old_id, False)) if old_id is not None else []
    c("the running VM", True, f"#{old_id}" if old_id is not None else "none: the VM is made new", must=False)
    img = image_of(svc.platform, "base-os") if svc.platform else None
    c(
        "base-os image of the platform",
        img is not None and img.one_id is not None,
        f"{img.name} #{img.one_id}" if img else "—",
    )
    cl = Cluster.objects.filter(platform=svc.platform).exclude(state="retired").first()
    cl_el = one.find("one.clusterpool.info", "CLUSTER", cl.name) if cl else None
    c("the platform's cluster", cl_el is not None, cl.name if cl else "—")
    nics = []
    for i, (net, _gw) in enumerate(svc.nic_list()):
        if net is None:
            c(f"NIC {i + 1}", False, "network not defined")
            continue
        try:
            vid, vname = _vnet(one, net, site)
        except RedeployError as exc:
            c(f"NIC {i + 1}", False, str(exc))
            continue
        ip = (node.ips or [])[i] if i < len(node.ips or []) else ""
        held = next((n for n in old_nics if n["net_id"] == vid and n["ip"] == ip), None) if ip else None
        free = not ip or held is not None or ip not in _leases(one, vid)
        c(
            f"NIC {i + 1} {vname}",
            free,
            (f"{ip}: " + ("the old VM frees it" if held else "free" if free else "held by another VM"))
            if ip
            else "assigned by the platform",
        )
        nics.append({"net_id": vid, "net": vname, "ip": ip, "old_nic": held["id"] if held else None})
    others = [n for n in old_nics if not any(x["old_nic"] == n["id"] for x in nics)]
    c(
        "the old VM's other NICs stay with it",
        True,
        ", ".join(f"{n['net']} {n['ip']}" for n in others) or "none",
        must=False,
    )
    if svc.routes:
        c("routes", False, "the service's routes are not made on a base-OS VM yet", must=False)
    return {
        "node": node.name,
        "hostname": node.hostname or node.name,
        "platform": svc.platform.key if svc.platform else "",
        "old_id": old_id,
        "old_exists": _vm(one, f"{node.name}-old") is not None,
        "image_id": img.one_id if img else None,
        "image": img.name if img else "",
        "cluster": cl.name if cl else "",
        "nics": nics,
        "vcpu": svc.vcpu or 1,
        "ram_mb": int(float(svc.ram_gb or 1) * 1024),
        "disk_mb": int(svc.disk_gb or 10) * 1024,
        "checks": out,
        "ok": all(x["ok"] for x in out if x["must"]),
    }


def _status(node, timeout):
    """Until the first boot ends: "OK", or RedeployError with its exit code and last step (the log stays on the VM).
    commit-310: no answer over ssh (refused, timed out: the VM still booting, sshd moving to its port) is retried until
    the deadline; only the guest's own FAILED ends it."""
    from . import hostlogging
    from .sshexec import SshError

    end = time.monotonic() + timeout
    last = "no answer yet"
    while True:
        try:
            out = hostlogging.node_ssh(node)(STATUS, timeout=60)
            text = (out or "").strip().splitlines()[0] if (out or "").strip() else ""
            if text.startswith("OK"):
                return
            if text.startswith("FAILED"):
                raise RedeployError(f"the first boot failed: {text[7:].strip()[:300]} (its log: {LOG} on the VM)")
            last = text[8:].strip() or "running"
        except (hostlogging.LoggingError, SshError) as exc:
            last = str(exc)[:200]
        if time.monotonic() > end:
            raise RedeployError(f"the first boot has not ended after {timeout} s: {last}")
        time.sleep(_poll())


def _checks(node, profile):
    from . import hostlogging

    out = (hostlogging.node_ssh(node)(check_script(profile), timeout=120) or "").strip()
    bad = [ln[5:] for ln in out.splitlines() if ln.startswith("FAIL ")]
    if bad:
        raise RedeployError("the profile's checks failed: " + "; ".join(bad))
    return len(profile.checks or [])


FIRST_FOUND = "{n} is on the platform now (#{i}): nothing changed - refresh the page and Redeploy it"


def _expect_new(node, p):
    """commit-402: a Deploy was confirmed as the VM's first - stop before any change when the live plan finds one."""
    if p["old_id"] is not None:
        raise RedeployError(FIRST_FOUND.format(n=node.name, i=p["old_id"]))


def redeploy(task, node, user=None, keep_old=True, make_primary=False, first=False):
    """Replace the node's VM by a new one from its service profile (see the module); rolls back by itself. A name
    server holds the DNS claim meanwhile (commit-324); the primary gets the zones as published - published now under a
    new serial when they changed (commit-323). first (commit-402): confirmed as a first deployment (Deploy) - nothing is
    changed when the platform has the VM after all."""
    first_deploy = first  # the name first is the DNS's own below
    if first_deploy:  # read-only: the live plan, before any claim or change (every type, the front end's too)
        _expect_new(node, plan(node))
    if node.service.stype == T.ONEFE:  # T34 B6: the data from the zone's leader
        from . import fe_deploy

        return fe_deploy.redeploy(task, node, user, keep_old, first=first_deploy)
    if node.service.stype != T.NS:
        return _redeploy(task, node, user, keep_old, expect_new=first_deploy)
    from . import dns

    token = dns.claim(f"redeploy {node.name}")  # held until it ends: at most ~1 h, the claim lasts 2 h (commit-331)
    if token is None:
        raise RedeployError("a DNS task is running (Infrastructure → DNS): try again when it ends")
    from .models import DnsSettings

    primary = ok = False
    prev = None
    first_try = False  # commit-354: this redeploy is a first primary's
    try:
        if dns.drop_stale_primary():  # commit-355: a retired primary gives way, under the claim
            task.step("primary", "ok", "the stored primary is no longer a name server: cleared")
        had = DnsSettings.get().primary_id
        # owner 11:51: "add check box to make server as primary during deployment"
        v = dns.values(node, reveal=False, make_primary=make_primary)
        moves = bool(v and v.get("first") and had is not None and had != node.pk)  # the primary moves to this VM
        if v is not None and v.get("waiting_for"):  # commit-354: another VM is becoming the first primary
            raise RedeployError(
                f"the first redeploy of {v['waiting_for']} as the primary has not ended: redeploy it again or roll it "
                "back first"
            )
        is_primary = v is not None and v["role"] == "primary"
        # owner 2026-11-22 09:59: with no primary yet this VM becomes it when it passes; nothing served before it
        serving = is_primary and not v.get("first")

        def prepare():
            """commit-342: once the read-only checks passed, before the recipe is rendered - the candidate the new
            primary is given (commit-330: published only when it passes its checks); dropped if the redeploy stops."""
            nonlocal primary, prev
            if is_primary:
                cand = DnsSettings.get().candidate or {}
                if cand.get("first") and cand.get("node") == node.pk:  # commit-354: an earlier attempt's, kept
                    prev = cand  # on a failure of this one
                primary = True  # from here on, finally drops or publishes the candidate
                try:
                    serial = dns.reserve_for_primary(node, first=bool(v.get("first")))  # commit-354
                except dns.DnsError as exc:
                    primary = False
                    raise RedeployError(str(exc)) from exc
                task.step("zones", "ok", f"serial {serial} for the new primary")

        def before():
            """commit-341: right before the first VM change - a refused redeploy leaves the DNS as it was."""
            if serving:  # commit-338: cleared when it passes (published) or by an Apply
                dns.mark_uncertain(node, "redeploy", keep_candidate=True)

        try:
            first = first_try = bool(v and v.get("first") and is_primary)
            result = _redeploy(
                task,
                node,
                user,
                keep_old,
                prepare=prepare,
                before=before,
                allocated=(lambda vid: dns.candidate_vm(node, vid)) if first else None,
                tags={dns.FIRST_TAG: str(node.pk)} if first else None,
                unsure=(lambda: dns.mark_unsure(node)) if first else None,
                expect_new=first_deploy,
            )
            ok = True
            if moves:  # every server's configuration rewritten for the new primary (the old one becomes a secondary)
                # commit-362: the zones it was verified with stay published; promoted and marked "handoff" in one
                # write - the mark stays until an Apply reaches every server
                dns.published(node, True, handoff=True)
                primary = False
                try:
                    dns.apply(task, user, token)  # a success clears the mark
                    task.step("handoff", "ok", "every name server now follows the new primary")
                except dns.DnsError as exc:
                    task.step("handoff", "failed", f"{exc} - the handoff is not complete: Apply (Infrastructure → DNS)")
                    result = {**(result or {}), "handoff": "incomplete"}
            return result
        except RedeployError as exc:
            if primary and serving and str(exc).startswith("rolled back"):  # commit-338: it may have served
                dns.published(node, False)
                primary = False
                dns.republish(task, user, token)
            raise
    finally:
        if primary and not ok and prev:  # commit-354: the VM that began becoming the primary may still serve
            dns.keep_candidate(prev)
        elif primary and not ok and first_try:  # commit-354: a VM whose id never came back may serve - by its tag
            if not dns.end_first(node):
                task.step("primary", "failed", "a VM made as the primary-to-be may still serve: it stays pending")
        elif primary:
            dns.published(node, ok)
        dns.release(token)


def _redeploy(
    task,
    node,
    user=None,
    keep_old=True,
    prepare=None,
    before=None,
    after_boot=None,
    made=None,
    allocated=None,
    tags=None,
    unsure=None,
    expect_new=False,
):
    """made (T34 B6, commit-345/346): a front end's Restore all rolls the whole zone back itself - the new VM's id is
    put in made["new_id"] as soon as it exists, and a failure only puts the node's address back and raises (no
    rollback of this node alone)."""
    from accounts.models import audit
    from tenants.deploy import MEMORY_RESIZE_MODE, SCHED, crypt_sha512, template, with_public

    from . import core_services as cs
    from .core_deploy import drop_init_script, finish
    from .credentials import account

    svc = node.service
    site = svc.site.code
    p = plan(node)
    task.step(
        "plan",
        "ok" if p["ok"] else "failed",
        f"{node.name} on {p['platform']}: "
        + ", ".join(f"{n['net']} {n['ip'] or 'assigned'}" for n in p["nics"])
        + f"; image {p['image']} #{p['image_id']}",
    )
    if not p["ok"]:
        raise RedeployError("; ".join(f"{c['check']}: {c['detail']}" for c in p["checks"] if c["must"] and not c["ok"]))
    if expect_new:  # commit-402: the plan this task acts on
        _expect_new(node, p)
    if p["old_exists"]:
        raise RedeployError(f"{node.name}-old exists: remove the old VM or roll back the previous redeploy first")
    if p["old_id"] is not None and rollback_pending(node, {"id": p["old_id"]}):
        raise RedeployError(f"a rollback of {node.name} has not ended: Roll back again first")
    if prepare is not None:  # commit-342: the DNS candidate before the recipe that carries it is rendered
        prepare()
    prof = profile_of(svc)
    port = svc.ssh_port_now = ssh_port_of(svc)  # commit-483: read once - the recipe and the checks use the same port
    recipe = script_of(node, reveal=True)
    one, control = _one(svc)
    user_name, acc = account(svc.platform.sa)
    acc = with_public(acc or {})
    if not user_name or not acc.get("public"):
        raise RedeployError(f"{svc.platform.key}: the super admin secret has no ssh key")
    old_id = p["old_id"]
    taken = [n for n in p["nics"] if n["old_nic"] is not None]
    old_address, old_deployed, old_port = node.address, node.deployed_vm_id, node.ssh_port
    if before is not None:  # commit-341: every check above is read-only; the VMs change from here on
        before()
    audit(user, "core_vm_redeploy_started", svc, site=site, vm=node.name, old_id=old_id, profile=prof.key)
    new_id, schedule, old_autostart = None, None, ""
    try:
        if old_id is not None:  # 1 the old VM steps aside (kept)
            schedule = take_schedule(one, old_id)
            old_autostart = one.xml("one.vm.info", old_id, False).findtext("USER_TEMPLATE/AUTOSTART") or ""
            # commit-311: all a rollback by hand needs to put back, kept before anything else changes
            node.facts = {
                **(node.facts or {}),
                "redeploy": {
                    "old_id": old_id,
                    "taken": [[n["net_id"], n["ip"]] for n in taken],
                    "address": old_address,
                    "deployed_vm_id": old_deployed,
                    "autostart": old_autostart,
                    "schedule": schedule,
                    # T36 B3a: what a rollback by hand puts back (as stored, not as the row in memory says)
                    "state": type(node).objects.filter(pk=node.pk).values_list("state", flat=True).first(),
                    "ssh_port": old_port,  # commit-482: the ports the old VM answers on
                    "service_ssh_port": svc.ssh_port,
                },
            }
            node.save(update_fields=["facts"])
            # T34 B6: a front end's Restore all powers the three old VMs off first
            off = one.xml("one.vm.info", old_id, False).findtext("STATE") == "8"
            how = "already" if off else _powered_off(one, old_id)
            if old_autostart.lower() not in ("", "no"):
                set_autostart(one, old_id, "no")
            for n in taken:
                one.call("one.vm.detachnic", old_id, n["old_nic"])
                _wait(one, old_id, "poweroff")
            one.call("one.vm.rename", old_id, f"{node.name}-old")
            task.step(
                "old VM aside",
                "ok",
                f"#{old_id} {'was powered off' if how == 'already' else 'powered off'}"
                f"{' (hard)' if how == 'hard' else ''}, "
                + (f"NICs detached: {', '.join(n['net'] + ' ' + n['ip'] for n in taken)}; " if taken else "")
                + f"renamed {node.name}-old"
                + (f"; scheduled actions removed: {_schedule_text(schedule)}" if schedule else ""),
            )
        # 2 the new VM
        ctx = {
            "NETWORK": "yes",
            "SET_HOSTNAME": p["hostname"],
            "USERNAME": user_name,
            "SSH_PUBLIC_KEY": acc["public"],
            START_KEY: base64.b64encode(first_boot(recipe).encode()).decode(),
            "TIMEZONE": svc.site.tz,
        }
        if acc.get("password"):
            ctx["CRYPTED_PASSWORD_BASE64"] = base64.b64encode(crypt_sha512(acc["password"]).encode()).decode()
        tpl = {
            "NAME": node.name,
            "AUTOSTART": AUTOSTART,
            "SCHED_REQUIREMENTS": SCHED,
            "MEMORY_RESIZE_MODE": MEMORY_RESIZE_MODE,
            "OS": {"ARCH": "x86_64", "FIRMWARE": "UEFI", "MACHINE": "q35"},
            "CPU": p["vcpu"],
            "VCPU": p["vcpu"],
            "CPU_MODEL": {"MODEL": "host-passthrough"},
            "MEMORY": p["ram_mb"],
            "VIDEO": {"TYPE": "virtio"},
            "GRAPHICS": {"LISTEN": "0.0.0.0", "TYPE": "VNC"},
            "DISK": {"IMAGE_ID": p["image_id"], "SIZE": p["disk_mb"]},
            "NIC": [
                {"NETWORK_ID": n["net_id"], **({"IP": n["ip"]} if n["ip"] else {}), "NAME": f"NIC{i}"}
                for i, n in enumerate(p["nics"])
            ],
            "CONTEXT": ctx,
        }
        if made is not None and hasattr(made, "intent"):  # commit-351: tagged before it is asked for
            tpl[INTENT_KEY] = made.intent()
        tpl.update(tags or {})  # commit-354: e.g. a first primary's tag, found on the platform without its id
        try:
            new_id = int(one.call("one.vm.allocate", template(tpl), False))
        except Exception as exc:
            # commit-354: only OpenNebula's own refusal (an error code in its reply) says no VM was made
            from adoption.collector import OneError

            cause = exc.__cause__
            definitive = isinstance(cause, OneError) and cause.code is not None
            if unsure is not None and not definitive:
                unsure()
            if definitive and made is not None and hasattr(made, "refused"):  # commit-351: no VM was made
                made.refused()
            raise
        if allocated is not None:  # commit-354: a first primary's VM recorded the moment it exists
            allocated(new_id)
        if made is not None:
            made["new_id"] = new_id
        task.step("new VM", "ok", f"{node.name} #{new_id} (host name {p['hostname']}) from {p['image']}")
        _wait(one, new_id, "running", timeout=600)
        node.address = (node.ips or [old_address])[0] or old_address
        node.ssh_port = port  # commit-473/483: the new VM answers on the port its recipe set
        node.save(update_fields=["address", "ssh_port"])
        _reread(task, control)
        # 3 the first boot and the profile's checks
        _status(node, FIRSTBOOT_TIMEOUT)
        task.step("first boot", "ok", f"the recipe of {prof.key} ran")
        if after_boot is not None:  # T34 B6: a front end's data and configuration, before the profile's checks
            after_boot()
        n = _checks(node, prof)
        task.step("checks", "ok", f"{n} check(s) of {prof.key} pass")
        _record_service_port(svc, node)
    except Exception as exc:  # noqa: BLE001 - roll back, then report
        task.step("redeploy", "failed", str(exc)[:600])
        node.address, node.ssh_port = old_address, old_port
        node.save(update_fields=["address", "ssh_port"])
        if made is not None:  # commit-345: the caller rolls every node back together
            audit(user, "core_vm_redeploy_failed", svc, site=site, vm=node.name, error=str(exc)[:300])
            raise RedeployError(f"{node.name}: {exc}") from exc
        _rollback(
            task,
            one,
            node,
            new_id,
            old_id,
            [(n["net_id"], n["ip"]) for n in taken],
            old_autostart,
            schedule,
            restore={"address": old_address},
        )
        audit(user, "core_vm_redeploy_rolled_back", svc, site=site, vm=node.name, error=str(exc)[:300])
        _reread(task, control)
        raise RedeployError(f"rolled back: {exc}") from exc
    # 5 the script (secrets) leaves the context; the VM works either way
    init_left = True
    try:
        removed = drop_init_script(one, new_id, cs.START_KEY)  # commit-313: the key it set
        init_left = False
        task.step("init script", "ok", "removed from the VM's context" if removed else "none in the context")
    except Exception as exc:  # noqa: BLE001 - "Remove the init script" tries again
        task.step("init script", "failed", f"still in the context: {str(exc)[:300]}")
    _reread(task, control)
    cs.set_init_flag(control, new_id, init_left)
    node.deployed_vm_id = new_id
    node.save(update_fields=["deployed_vm_id"])
    node.set_state(ItemState.DEPLOYED)  # T36 B3a: made from its service, its checks passed
    audit(user, "core_vm_redeployed", svc, site=site, vm=node.name, new_id=new_id, old_kept=bool(old_id and keep_old))
    old_removed = False
    if old_id is not None and not keep_old:
        try:
            finish(task, node, user)
            old_removed = True
        except Exception as exc:  # noqa: BLE001 - "Remove the old VM" tries again
            task.step("old VM", "failed", f"{node.name}-old #{old_id} not removed: {str(exc)[:300]}")
    return {"new_id": new_id, "old_id": old_id, "old_removed": old_removed}


def _rollback(task, one, node, new_id, old_id, taken, autostart=AUTOSTART, schedule=None, restore=None):
    """The old VM back: the new one removed, the old one renamed back, its NICs on the taken addresses attached again,
    resumed. taken: [(vnet id, ip)]. restore: the node's fields to put back - saved in the same write that drops the
    redeploy record (commit-314), so a stop in between leaves the record for a retry."""
    from tenants.deploy import template

    if new_id is not None:
        one.call("one.vm.action", "terminate-hard", new_id)
        _wait(one, new_id, "gone")
        task.step("rollback: new VM", "ok", f"#{new_id} removed")
    if old_id is None:
        return
    vm = one.xml("one.vm.info", old_id, False)
    if vm.findtext("NAME") != node.name:
        one.call("one.vm.rename", old_id, node.name)
    have = {(n["net_id"], n["ip"]) for n in _nics(vm)}
    for net_id, ip in taken:
        if (int(net_id), ip) not in have:
            one.call("one.vm.attachnic", old_id, template({"NIC": {"NETWORK_ID": int(net_id), "IP": ip}}))
            _wait(one, old_id, "poweroff")
    if one.xml("one.vm.info", old_id, False).findtext("STATE") == "8":
        one.call("one.vm.action", "resume", old_id)
    _wait(one, old_id, "running")
    if autostart and autostart.lower() != "no":
        set_autostart(one, old_id, autostart)
    # commit-316: only the saved actions the VM lacks are added (one call each: a retry finds those already back)
    vm = one.xml("one.vm.info", old_id, False)
    add = _missing_actions(vm, schedule)
    if add:  # commit-317: read again only when actions were added; otherwise the first answer is checked
        put_schedule(one, old_id, add)
        vm = one.xml("one.vm.info", old_id, False)
    # commit-315: the record goes only when the old VM is running under its name with every NIC it gave up
    have = {(n["net_id"], n["ip"]) for n in _nics(vm)}
    missing = [f"{net_id} {ip}" for net_id, ip in taken if (int(net_id), ip) not in have]
    lacking = _missing_actions(vm, schedule)
    if lacking:
        missing.append(f"scheduled actions: {_schedule_text(lacking)}")
    if vm.findtext("NAME") != node.name or vm.findtext("STATE") != "3" or missing:
        raise RedeployError(
            f"#{old_id} is not back in service yet ({vm.findtext('NAME')}, state {vm.findtext('STATE')}"
            + (f", missing: {', '.join(missing)}" if missing else "")
            + "): Roll back again"
        )
    _restore(node, restore)
    task.step("rollback: old VM", "ok", f"#{old_id} back as {node.name}, running")


def _actions(vm):
    """The VM's scheduled actions by their own fields (as take_schedule keeps them)."""
    out, seen = [], set()
    for s in vm.findall(".//SCHED_ACTION"):
        sid = s.findtext("ID")
        if sid is not None and sid in seen:
            continue
        seen.add(sid)
        out.append({k: s.findtext(k) for k in SCHED_KEYS if (s.findtext(k) or "") != ""})
    return out


def _missing_actions(vm, schedule):
    """The saved actions the VM does not have (each counted once per copy: two equal actions need two)."""
    have = _actions(vm)
    out = []
    for a in schedule or ():
        a = {k: str(v) for k, v in a.items() if v not in (None, "")}
        if a in have:
            have.remove(a)
        else:
            out.append(a)
    return out


def _record_service_port(svc, node):
    """commit-473/484-486: the service takes the port its new VM answers on. One transaction: the service row locked
    and read now (an edit made during the redeploy is the value replaced, not the one read at its start), the new port
    written and both kept in the node's record - so a rollback undoes exactly this write, or nothing was written."""
    from django.db import transaction

    from .models import CoreService, CoreServiceNode

    with transaction.atomic():
        cur = CoreService.objects.select_for_update().values_list("ssh_port", flat=True).get(pk=svc.pk)
        if cur == node.ssh_port:
            return
        n = CoreServiceNode.objects.select_for_update().get(pk=node.pk)
        rec = dict((n.facts or {}).get("redeploy") or {})
        CoreService.objects.filter(pk=svc.pk).update(ssh_port=node.ssh_port)
        if rec:
            rec.update(service_ssh_port=cur, service_ssh_port_new=node.ssh_port)
            n.facts = {**(n.facts or {}), "redeploy": rec}
            n.save(update_fields=["facts"])
    svc.ssh_port = node.ssh_port
    if rec:
        node.facts = n.facts


def _restore(node, fields=None):
    """The node's fields as before and the redeploy record dropped, in one write (commit-314)."""
    fields = dict(fields or {})
    svc_port = fields.pop("service_ssh_port", None)
    if svc_port:  # commit-482/484: the service's port back only while it still holds the value the redeploy wrote
        from .models import CoreService

        old, new = svc_port
        if CoreService.objects.filter(pk=node.service_id, ssh_port=new).update(ssh_port=old):
            node.service.ssh_port = old
    if "redeploy" in (node.facts or {}):
        fields["facts"] = {k: v for k, v in node.facts.items() if k != "redeploy"}
    for k, v in fields.items():
        setattr(node, k, v)
    if fields:
        node.save(update_fields=list(fields))
    if "state" in fields:  # T36 B3a: the service's state follows
        node.set_state(fields["state"])


def rollback_pending(node, vm):
    """commit-314: a rollback that stopped after the old VM got its name back - the node keeps its redeploy record and
    the VM under its name is the recorded old one (vm: the platform's VM element, or the page's {id})."""
    rec = (node.facts or {}).get("redeploy") or {}
    if rec.get("old_id") is None or vm is None:
        return False
    vm_id = vm.get("id") if isinstance(vm, dict) else int(vm.findtext("ID"))
    return vm_id == rec["old_id"]


def _restored(rec, old_id):
    """The node's fields before the redeploy (commit-311): its address and the VM id the portal had made."""
    same = rec.get("old_id") == old_id
    out = {"deployed_vm_id": rec.get("deployed_vm_id") if same else None}
    # T36 B3a: the old VM back - the state it had (one the portal did not make: non-compliant)
    out["state"] = (rec.get("state") or ItemState.NON_COMPLIANT) if same else ItemState.NON_COMPLIANT
    if rec.get("address"):
        out["address"] = rec["address"]
    if rec.get("ssh_port"):  # commit-482: the old VM's port, and the service's as it was
        out["ssh_port"] = rec["ssh_port"]
    if rec.get("service_ssh_port") and rec.get("service_ssh_port_new"):  # commit-484: only what the redeploy changed
        out["service_ssh_port"] = (rec["service_ssh_port"], rec["service_ssh_port_new"])
    return out


def rollback(task, node, user=None):
    """By hand while <name>-old exists: the new VM removed, the old one back with the NICs it gave up. A name server's
    holds the DNS claim; the primary's then republishes the zones (commit-338)."""
    if node.service.stype == T.ONEFE:  # T34 B6: the old node is told to catch up from the leader
        from . import fe_deploy

        return fe_deploy.rollback(task, node, user)
    if node.service.stype != T.NS:
        return _rollback_by_hand(task, node, user)
    from . import dns

    token = dns.claim(f"rollback {node.name}")
    if token is None:
        raise RedeployError("a DNS task is running (Infrastructure → DNS): try again when it ends")
    try:
        v = dns.values(node, reveal=False)
        marked = []

        def before():
            """commit-338: marked before the VM changes, kept if cut off; commit-341: only once there is something
            to roll back - a refused rollback leaves the DNS as it was."""
            if v is not None and v["role"] == "primary" and not v.get("first"):
                dns.mark_uncertain(node, "rollback")
                marked.append(True)

        result = _rollback_by_hand(task, node, user, before=before)
        if marked:
            dns.republish(task, user, token)
        if v is not None and v["role"] == "primary" and v.get("first"):
            # commit-354: the candidate ends only when the VM brought back was not made for it (it never served)
            back = (result or {}).get("old_id")
            if back is not None and dns.may_serve(node, back):
                task.step("primary", "ok", f"#{back} was made as the primary-to-be: it stays the one to become it")
            elif not dns.end_first(node):  # another VM made for it may still be alive (its tag on the platform)
                task.step("primary", "ok", "a VM made as the primary-to-be is still on the platform: it stays pending")
        return result
    finally:
        dns.release(token)


def _rollback_by_hand(task, node, user=None, before=None):
    from accounts.models import audit

    svc = node.service
    one, control = _one(svc)
    old = _vm(one, f"{node.name}-old")
    rec = (node.facts or {}).get("redeploy") or {}
    if old is None:
        back = _vm(one, node.name)
        if rollback_pending(node, back):
            # commit-314/315: an earlier rollback renamed the old VM back and stopped; the platform steps are done
            # again where they are missing (NICs, resume, autostart, schedule), checked, then the portal is updated
            if before is not None:  # commit-341
                before()
            _rollback(
                task,
                one,
                node,
                None,
                rec["old_id"],
                rec.get("taken") or [],
                autostart=rec.get("autostart") or "",
                schedule=rec.get("schedule"),
                restore=_restored(rec, rec["old_id"]),
            )
            task.step("rollback: portal", "ok", f"#{rec['old_id']} is back as {node.name}: the portal updated")
            audit(user, "core_vm_redeploy_rolled_back", svc, site=svc.site.code, vm=node.name, by="hand")
            _reread(task, control)
            return {"old_id": rec["old_id"]}
        _reread(task, control)
        raise RedeployError(f"{node.name}-old not found: nothing to roll back")
    old_id = int(old.findtext("ID"))
    new = _vm(one, node.name)
    new_id = int(new.findtext("ID")) if new is not None else None
    taken = rec.get("taken")
    if taken is None:  # not recorded: the new VM's addresses the old VM has no NIC on
        old_nets = {n["net_id"] for n in _nics(one.xml("one.vm.info", old_id, False))}
        taken = (
            [
                [n["net_id"], n["ip"]]
                for n in _nics(one.xml("one.vm.info", new_id, False))
                if n["net_id"] not in old_nets
            ]
            if new_id is not None
            else []
        )
    # commit-311: the old VM's autostart, scheduled actions, address and "made by the portal" as they were before
    autostart = rec.get("autostart", AUTOSTART) if rec else AUTOSTART
    if before is not None:  # commit-341: everything above only reads; the VMs change from here on
        before()
    _rollback(
        task,
        one,
        node,
        new_id,
        old_id,
        taken,
        autostart=autostart or "",
        schedule=rec.get("schedule"),
        restore=_restored(rec, old_id),
    )
    audit(user, "core_vm_redeploy_rolled_back", svc, site=svc.site.code, vm=node.name, by="hand")
    _reread(task, control)
    return {"old_id": old_id}
