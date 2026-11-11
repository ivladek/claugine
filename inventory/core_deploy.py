"""Redeploying a VM of a core service from its template (VyOS route reflectors for now). Nothing is taken from the old
VM (2026-10-27): its name, addresses, password and configuration all come from the service's definition, the site and
the secrets; the old VM is only stepped aside (and kept or removed).

The new VM takes the old one's place: the name from the service's definition (its host name exactly that name,
correction 09), the same platform, the same address on the service's network, but built the current way — eth0 on the site's management network (cloud-vyos, VRF MGMT), the service's network on eth1, the
configuration from Infrastructure → Templates applied on the first boot.

    plan      what would be done, nothing changes (the configuration with the passwords hidden)
    redeploy  1 the old VM is powered off, its NIC on the service's network detached (the address is free) and the
                VM renamed <new name>-old — it is kept, so it can come back
              2 the new VM is made from the platform's VyOS image: management NIC (a free cloud-vyos address) first,
                the service's network (the old address) second, the template as its one-time init script
              3 wait until it runs, answers ssh on the management address and its BGP peers are established
                (at least the number given, when one is given); the node's address becomes the management address
              4 when anything in 2–3 fails: roll back by itself (the new VM is removed, the old one gets its NIC back
                and is resumed)
    rollback  the same by hand, while <name>-old exists (the old VM comes back under the node's current name)
    finish    the old VM is removed (after the new one has proven itself)

Only for the active portal node and a platform in execution mode (One.call refuses otherwise).
"""
import re
import time

from django.utils import timezone

from .models import Cluster, CoreServiceNode, PlatformControl, ServiceImage

ROLES = ("rr-inet", "rr-corpnet", "rr-vxlan")  # templates a VM can be redeployed from
MGMT_TIMEOUT = 420  # seconds until the new VM answers ssh on its management address
BGP_TIMEOUT = 240  # seconds until its BGP peers are established


class RedeployError(Exception):
    pass


def _poll():
    from django.conf import settings
    return 0 if not getattr(settings, "DEPLOY_ASYNC", True) else 5


def role_of(svc):
    role = svc.template.role if svc.template else ""
    if role not in ROLES:
        raise RedeployError(f"{svc.name}: redeploying is for services made from a route reflector template "
                            f"({', '.join(ROLES)}); this one has {role or 'no template'}")
    return role


def index_of(node):
    """rr1 → 0, rr2 → 1 …; else its place among the service's nodes."""
    m = re.search(r"rr(\d+)", node.name)
    if m:
        return int(m.group(1)) - 1
    return [n.pk for n in node.service.nodes.order_by("name")].index(node.pk)


def _one(svc):
    from tenants.deploy import One
    if svc.platform is None:
        raise RedeployError(f"{svc.name}: no platform (the service's VMs run on one)")
    c = PlatformControl.objects.filter(key=svc.platform.key).first()
    if c is None or c.mode != PlatformControl.Mode.EXECUTION:
        raise RedeployError(f"{svc.platform.key} is not in execution mode (Infrastructure → Platforms)")
    return One(c), c


def _vm(one, name):
    v = one.find("one.vmpool.info", "VM", name, -2, -1, -1, -1)
    return v


def _nics(vm_xml):
    return [{"id": int(n.findtext("NIC_ID")), "net_id": int(n.findtext("NETWORK_ID") or -1),
             "net": n.findtext("NETWORK") or "", "ip": n.findtext("IP") or ""} for n in vm_xml.findall("TEMPLATE/NIC")]


def drop_init_script(one, vm_id):
    """Empty the one-time init script in the VM's context (it holds the first configuration with its secrets; VyOS ran
    it at the first boot and never again). Only that value changes (merge). False when there was none."""
    from tenants.deploy import template
    from .core_services import INIT_KEY, has_init_script
    if not has_init_script(one, vm_id):
        return False
    one.call("one.vm.updateconf", int(vm_id), template({"CONTEXT": {INIT_KEY: ""}}), 1)
    if has_init_script(one, vm_id):
        raise RedeployError(f"#{vm_id}: the init script is still in the context after the update")
    return True


def _reread(task, control):
    """The platform's VMs again, so the page shows what is there now (the old VM, the status)."""
    from . import core_services as cs
    try:
        cs.read_platform_vms(control)
    except Exception as exc:  # noqa: BLE001 - the work is done; only the page's view is older
        task.step("platform read", "failed", f"{control.key}: {str(exc)[:200]} (Refresh on the page reads it again)")


def _free_ip(one, vnet_id, name):
    import ipaddress
    vn = one.xml("one.vn.info", vnet_id, False)
    used = {lease.findtext("IP") for lease in vn.findall("AR_POOL/AR/LEASES/LEASE")}
    for ar in vn.findall("AR_POOL/AR"):
        if ar.findtext("TYPE") not in ("IP4", "IP4_6"):
            continue
        start, size = ipaddress.ip_address(ar.findtext("IP")), int(ar.findtext("SIZE") or 0)
        for i in range(size):
            ip = str(start + i)
            if ip not in used:
                return ip
    raise RedeployError(f"no free address in {name}")


def _seg(svc):
    """The name of the network the service works on (its first NIC off the management network)."""
    n = svc.service_network() or svc.nic1_network
    return n.name if n else ""


def _platform_family(svc):
    return "portal" if _seg(svc).startswith("portal") else "cloud"


def _service_nic_index(svc):
    """The position (0-based) of the service network among the defined NICs."""
    nics = svc.nic_list()
    seg = _seg(svc)
    return next((i for i, (n, _gw) in enumerate(nics) if n is not None and n.name == seg), 0)


def _config(node, mgmt_ip, reveal, host_name=None):
    """host_name: the one it gets (correction 09: exactly its VM name after the redeploy)."""
    from . import nfconfig
    from .core_types import target_of
    svc = node.service
    try:
        text = nfconfig.build(role_of(svc), site=svc.site.code, index=index_of(node), reveal=reveal,
                              segment=_seg(svc) or None, mgmt_ip=mgmt_ip,
                              platform=_platform_family(svc), host_name=host_name or target_of(node)[1])
    except nfconfig.BuildError as exc:
        raise RedeployError(f"configuration: {exc}") from exc
    return text


def config_of(node, reveal=False, mgmt_ip=None):
    """A node's configuration: route reflectors from their template; the site's gateways (cloud-egw, cloud-igw) from
    the tenants' egw/igw template with the site's context plus the service's differences."""
    from . import nfconfig
    svc = node.service
    role = node.role  # a VM of cloud-gw has its own (egw, igw)
    if role in ROLES:
        return _config(node, mgmt_ip, reveal)
    if role in nfconfig.GATEWAY_ROLES:
        try:
            return nfconfig.build_gateway(role, node, reveal=reveal, mgmt_ip=mgmt_ip)
        except nfconfig.BuildError as exc:
            raise RedeployError(f"configuration: {exc}") from exc
    raise RedeployError(f"{svc.name}: no VyOS template (route reflector, egw or igw)")


def masked(text):
    """The configuration as it may be shown: passwords and keys hidden."""
    return re.sub(r"((?:password|key|secret|encrypted-password|plaintext-password)\s+)'[^']*'", r"\1'••••'", text)


def plan(node):
    """What redeploying the node would do; reads only."""
    from . import nfconfig
    svc = node.service
    if svc.stype in ("cloud-gw", "portal-gw"):  # their egw and igw: inventory.gw_deploy (corrections 11, 12)
        from . import gw_deploy
        return gw_deploy.plan(node)
    one, control = _one(svc)
    old = _vm(one, node.name)
    if old is None:
        raise RedeployError(f"VM {node.name} not found on {svc.platform.key}")
    old_id = int(old.findtext("ID"))
    nics = _nics(one.xml("one.vm.info", old_id, False))
    seg = _seg(svc)
    ix = next((n for n in nics if n["net"].startswith(seg) or n["net"].replace("one-vxlan", "cloud-vxlan").startswith(seg)), None)
    if ix is None:
        raise RedeployError(f"{node.name}: no NIC on {seg}_{svc.site.code}")
    mgmt_name = f"{nfconfig.MGMT_NETWORK}_{svc.site.code}"
    mv = one.find("one.vnpool.info", "VNET", mgmt_name, -2, -1, -1)
    if mv is None:
        raise RedeployError(f"management network {mgmt_name} not found on {svc.platform.key}")
    mgmt_id = int(mv.findtext("ID"))
    defined_mgmt = (node.ips or [""])[0] if svc.mgmt_nic() else ""
    mgmt_ip = defined_mgmt or _free_ip(one, mgmt_id, mgmt_name)
    img = svc.image if svc.image_id and svc.image.control_id == control.pk else \
        ServiceImage.objects.filter(control=control, role=ServiceImage.VYOS).first()
    if img is None or img.one_id is None:
        raise RedeployError(f"{svc.platform.key}: no image id for {img.name if img else 'the VyOS image'} "
                            "(Infrastructure → Platforms → service images; the health check reads it)")
    if img.customization != ServiceImage.Customization.CONTEXT:
        raise RedeployError(f"{img.name} is customized over ssh: only images customized through the context are "
                            "redeployed here")
    cl = Cluster.objects.filter(platform=svc.platform).exclude(state="retired").first()
    cluster_id = None
    if cl is not None:
        c = one.find("one.clusterpool.info", "CLUSTER", cl.name)
        cluster_id = int(c.findtext("ID")) if c is not None else None
    if cluster_id is None:
        raise RedeployError(f"{svc.platform.key}: its cluster was not found on the platform")
    text = _config(node, mgmt_ip, reveal=True)  # only to know it is complete; shown with the secrets hidden
    vn = one.xml("one.vn.info", ix["net_id"], False) if ix.get("net_id") is not None else None
    vn_mtu = (vn.findtext("TEMPLATE/GUEST_MTU") or vn.findtext("TEMPLATE/MTU")) if vn is not None else None
    checks = check_definition(node, nics, old, ix, img, mgmt_ip, defined_mgmt, vn_mtu)
    from .core_services import vm_status
    from .core_types import target_of
    new_name, new_host = target_of(node)
    i = _service_nic_index(svc)
    service_ip = (node.ips or [])[i] if i < len(node.ips or []) else ""  # the definition's, not the old VM's
    return {"node": node.name, "service_ip": service_ip, "new_name": new_name, "new_hostname": new_host, "platform": svc.platform.key, "old_id": old_id,
            "old_state": vm_status(old.findtext("STATE") or "", old.findtext("LCM_STATE") or ""),
            "checks": checks, "definition_ok": all(c["ok"] for c in checks if c.get("must", True)),
            "old_nics": nics, "ix": ix, "mgmt_net": mgmt_name, "mgmt_net_id": mgmt_id, "mgmt_ip": mgmt_ip,
            "image_id": img.one_id, "image": img.name, "cluster_id": cluster_id, "cluster": cl.name,
            "vcpu": svc.vcpu or 2, "ram_mb": int(float(svc.ram_gb or 4) * 1024),
            "complete": nfconfig.is_complete(text), "config": masked(text),
            "old_exists": _vm(one, f"{node.name}-old") is not None or _vm(one, f"{new_name}-old") is not None}


def check_definition(node, old_nics, old, ix, img, mgmt_ip, defined_mgmt, vn_mtu=None):
    """Whether the service's definition describes this VM as it is and as it should be made (reads nothing more).
    [{check, ok, detail, must}] — a failed "must" check stops the redeploy."""
    from tenants.deploy import with_public
    from . import nfconfig
    from .credentials import account
    svc = node.service
    t = old.find("TEMPLATE")
    out = []

    def c(check, ok, detail="", must=True):
        out.append({"check": check, "ok": bool(ok), "detail": detail, "must": must})
    nics = svc.nic_list()
    c("NIC 1 is the management network", svc.mgmt_nic(),
      ", ".join(f"NIC {i + 1} {n.name if n else '?'}" for i, (n, _g) in enumerate(nics)) or "no NICs defined")
    c("the service network is defined", bool(svc.service_network()), _seg(svc) or "—")
    i = _service_nic_index(svc)
    want_ip = (node.ips or [])[i] if i < len(node.ips or []) else ""
    # the new VM takes the definition's address; the old VM frees it (its NIC there is detached), so they must agree
    c("the defined address on the service network is the one the old VM frees", want_ip == ix["ip"],
      f"defined {want_ip or '—'}, running {ix['ip']}")
    mgmt_name = f"{nfconfig.MGMT_NETWORK}_{svc.site.code}"
    old_mgmt = next((n for n in old_nics if n["net"] == mgmt_name), None)  # a VM redeployed before has one
    others = [n for n in old_nics if n is not ix and n is not old_mgmt]
    c("the running VM has no other NICs (nothing else to carry over)", not others,
      ", ".join(f"{n['net']} {n['ip']}" for n in others) or "none", must=False)
    net = svc.service_network()
    c("MTU of the service network: the site's network and the platform's the same", net is not None and vn_mtu is not None
      and str(net.mtu) == str(vn_mtu), f"site network {net.mtu if net else '—'}, platform network {vn_mtu or '—'}")
    c("management address", bool(mgmt_ip), ((defined_mgmt and f"defined {mgmt_ip}") or f"free address {mgmt_ip}")
      + (f"; the old VM keeps {old_mgmt['ip']} until it is removed" if old_mgmt else ""), must=False)
    from .core_types import target_of
    c("host name = VM name", True, " / ".join(dict.fromkeys(target_of(node))))
    run_vcpu = int(float(t.findtext("VCPU") or t.findtext("CPU") or 0))
    run_ram = round(int(t.findtext("MEMORY") or 0) / 1024)
    c("size as running", (svc.vcpu, svc.ram_gb) == (run_vcpu, run_ram),
      f"defined {svc.vcpu or '?'} vCPU / {svc.ram_gb or '?'} GB, running {run_vcpu} / {run_ram}", must=False)
    c("image customized through the context", img is not None and img.customization == "context",
      f"{img.name} #{img.one_id}" if img else "—")
    c("the image is the service's", svc.image_id == (img.pk if img else None), svc.image.name if svc.image else "— (the platform's VyOS image)",
      must=False)
    ports = nfconfig.management(svc.site.code)[1]
    c("ssh port is the site's VyOS management port", svc.ssh_port == ports["vyos"], f"{svc.ssh_port} / {ports['vyos']}")
    user, acc = account(svc.platform.sa) if svc.platform else (None, {})
    acc = with_public(acc or {})
    c("login user with an ssh key (the platform's super admin)", bool(user and acc.get("public")), user or "—")
    # 2026-10-27: the login user gets the super admin's password (the portal knows it; console login), not the hash
    # taken from the running VM (a password nobody had)
    c("the login user gets the super admin's password", bool(acc.get("password")),
      user or "—" if acc.get("password") else "— (no password in the super admin secret: no console login)",
      must=False)
    want_secret = svc.bgp_secret()
    c("the service secret is the network's BGP secret", not want_secret or svc.service_secret == want_secret,
      f"service {svc.service_secret or '—'}, network {want_secret or '—'}", must=False)
    role = svc.template.role if svc.template else ""
    want_role = {"cloud-ix": "rr-inet", "portal-ix": "rr-inet", "corpnet-ix": "rr-corpnet", "cloud-vxlan": "rr-vxlan",
                 "portal-vxlan": "rr-vxlan"}.get(_seg(svc), "")
    c("template fits the network", role == want_role, f"{role or '—'} for {_seg(svc)}")
    c("platform hosting the VMs", svc.platform is not None, svc.platform.key if svc.platform else "—")
    c("platform served", True, svc.serves.key if svc.serves else "core", must=False)
    return out


def _wait(one, vm_id, want, timeout=300):
    """Until the VM is in `want` ("running", "poweroff", "gone")."""
    from tenants.deploy import DeployError
    end = time.monotonic() + timeout
    while True:
        try:
            vm = one.xml("one.vm.info", vm_id, False)
            st, lcm = vm.findtext("STATE"), vm.findtext("LCM_STATE")
        except DeployError:
            if want == "gone":
                return
            raise
        if (want == "running" and st == "3" and lcm == "3") or (want == "poweroff" and st == "8") or \
                (want == "gone" and st == "6"):
            return
        if want == "running" and (st == "7" or lcm in ("36", "37", "38", "39", "40", "41", "42", "43", "44")):
            raise RedeployError(f"VM #{vm_id} failed ({st}/{lcm})")
        if time.monotonic() > end:
            raise RedeployError(f"VM #{vm_id}: still {st}/{lcm} after {timeout} s, waiting for {want}")
        time.sleep(_poll())


# every core service VM starts again by itself after its platform's hosts go down, gracefully or not (2026-10-28): the
# platform's autostart hook reads USER_TEMPLATE/AUTOSTART — "always": resumed whatever its last state
AUTOSTART = "always"
from tenants.deploy import MEMORY_RESIZE_MODE, SCHED  # noqa: E402 - the hypervisor only, never a cluster; ballooning (owner, 2026-10-28)


def set_autostart(one, vm_id, value=AUTOSTART):
    """AUTOSTART in a VM's user template (merge: nothing else changes). The host hook (7.4: autostart/host) resumes
    yes/true/always VMs when their host is back; "always" whatever their last state. "no": an old VM stepped aside must
    not come back with its host."""
    one.call("one.vm.update", vm_id, f'AUTOSTART = "{value}"', 1)


# a scheduled action's own fields (the rest — ID, DONE, MESSAGE, PARENT_ID, TYPE, WARNING — the platform keeps)
SCHED_KEYS = ("ACTION", "ARGS", "DAYS", "END_TYPE", "END_VALUE", "REPEAT", "TIME")


def take_schedule(one, vm_id):
    """Remove the VM's scheduled actions and return them (put back on a rollback). 2026-10-28: the VMs made before the
    portal have an hourly "resume" — an old VM powered off and stepped aside came back running within the hour."""
    taken, seen = [], set()
    for s in one.xml("one.vm.info", vm_id, False).findall(".//SCHED_ACTION"):
        sid = s.findtext("ID")
        if sid is None or sid in seen:
            continue
        seen.add(sid)
        taken.append({k: s.findtext(k) for k in SCHED_KEYS if (s.findtext(k) or "") != ""})
        one.call("one.vm.scheddelete", vm_id, int(sid))
    return taken


def put_schedule(one, vm_id, actions):
    from tenants.deploy import template
    for a in actions or ():
        one.call("one.vm.schedadd", vm_id, template({"SCHED_ACTION": a}))


def _schedule_text(actions):
    return ", ".join(f"{a.get('ACTION')}" + (f" (repeat {a['REPEAT']})" if a.get("REPEAT") not in (None, "-1") else "")
                     for a in actions)


SOFT_OFF_TIMEOUT = 120  # seconds the guest gets to power off by itself (ACPI) before it is powered off hard


def _powered_off(one, vm_id):
    """Power the VM off and make sure it stays off (2026-10-27: old route reflectors ignored the ACPI power-off — the
    platform showed them off for a moment, then its monitoring found them running again). Soft first, hard when the
    guest has not stopped within SOFT_OFF_TIMEOUT or comes back. Returns "soft" or "hard"."""
    one.call("one.vm.action", "poweroff", vm_id)
    try:
        _wait(one, vm_id, "poweroff", timeout=SOFT_OFF_TIMEOUT)
        time.sleep(_poll() * 2 or 0)  # off, and still off a moment later (the monitoring had time to look)
        if one.xml("one.vm.info", vm_id, False).findtext("STATE") == "8":
            return "soft"
    except RedeployError:
        pass
    one.call("one.vm.action", "poweroff-hard", vm_id)
    _wait(one, vm_id, "poweroff")
    return "hard"


def _check(node, site, expected, timeout):
    """Until the new VM answers ssh and its BGP peers are established (at least `expected`)."""
    from . import core_services as cs
    end = time.monotonic() + timeout
    last = ""
    while True:
        cs.read_node(node, site)
        node.refresh_from_db()
        if not node.facts_error:
            bgp = next((c for c in node.facts.get("checks", []) if c["check"].startswith("BGP")), None)
            if bgp:
                m = re.match(r"(\d+)/(\d+)", bgp.get("detail") or "")
                up = int(m.group(1)) if m else 0
                if bgp["ok"] and up >= (expected or 1):
                    return bgp["detail"]
                last = f"BGP {bgp.get('detail')}"
            else:
                last = "no BGP check"
        else:
            last = node.facts_error
        if time.monotonic() > end:
            raise RedeployError(f"{node.name} not ready after {timeout} s: {last}")
        time.sleep(_poll() * 2 or 0)


def redeploy(task, node, user=None, expected=None, force=False, keep_old=True):
    """Replace the node's VM by a new one built from the template (see the module); rolls back by itself on failure."""
    if node.service.stype in ("cloud-gw", "portal-gw"):
        from . import gw_deploy
        return gw_deploy.redeploy(task, node, user, force, keep_old)
    from accounts.models import audit
    from tenants.deploy import crypt_sha512, template, with_public
    from .credentials import account
    from .nfconfig import init_script, is_complete, management
    from . import nfconfig
    import base64
    svc = node.service
    site = svc.site.code
    p = plan(node)
    task.step("plan", "ok", f"{node.name} #{p['old_id']} on {p['platform']}: {p['ix']['net']} {p['ix']['ip']}; "
                            f"new management {p['mgmt_ip']}; image {p['image']} #{p['image_id']}")
    if p["old_exists"]:
        raise RedeployError(f"{node.name}-old exists: finish or roll back the previous redeploy first")
    if not p["complete"]:
        raise RedeployError("the configuration has values missing (see the plan)")
    if not p["definition_ok"]:
        bad = [c["check"] for c in p["checks"] if c["must"] and not c["ok"]]
        if not force:
            raise RedeployError("the service's definition does not fit: " + "; ".join(bad))
        task.step("definition", "failed", "does not fit, redeployed anyway (forced): " + "; ".join(bad))
    one, _control = _one(svc)
    user_name, acc = account(svc.platform.sa)
    acc = with_public(acc or {})
    if not user_name or not acc.get("public"):
        raise RedeployError(f"{svc.platform.key}: the super admin secret has no ssh key")
    old_id, ix = p["old_id"], p["ix"]
    old_address, old_port = node.address, node.ssh_port
    old_name, old_hostname = node.name, node.hostname
    new_name, new_host = p["new_name"], p["new_hostname"]  # correction 09: the host name is exactly the VM name
    audit(user, "core_vm_redeploy_started", svc, site=site, vm=node.name, new_name=new_name, old_id=old_id, ix=ix["ip"],
          mgmt=p["mgmt_ip"], forced=bool(force and not p["definition_ok"]))
    # 1 the old VM steps aside (kept): first its scheduled actions go (an hourly "resume" would bring it back)
    schedule = take_schedule(one, old_id)
    how = _powered_off(one, old_id)
    old_autostart = one.xml("one.vm.info", old_id, False).findtext("USER_TEMPLATE/AUTOSTART") or ""
    if old_autostart.lower() not in ("", "no"):  # it must stay off: a host restart would resume it (2026-10-28)
        set_autostart(one, old_id, "no")
    one.call("one.vm.detachnic", old_id, ix["id"])
    _wait(one, old_id, "poweroff")
    one.call("one.vm.rename", old_id, f"{new_name}-old")
    task.step("old VM aside", "ok", f"#{old_id} powered off{' (hard: it ignored the soft power-off)' if how == 'hard' else ''}, "
                              f"its {ix['net']} NIC detached, renamed {new_name}-old"
                              + (f"; scheduled actions removed: {_schedule_text(schedule)}" if schedule else ""))
    new_id = None
    try:
        # 2 the new VM
        text = _config(node, p["mgmt_ip"], reveal=True, host_name=new_host)
        if not is_complete(text):
            raise RedeployError("the configuration has values missing")
        init = base64.b64encode(init_script(text, f"{site} {svc.name} {new_name}").encode()).decode()
        ports = management(site)[1]
        ctx = {"NETWORK": "yes", "SET_HOSTNAME": new_host,
               "USERNAME": user_name, "SSH_PUBLIC_KEY": acc["public"], "SSH_PORT": ports["vyos"],
               "ONE_TIME_INIT_SCRIPT_BASE64": init,
               "TIMEZONE": svc.site.tz}
        if acc.get("password"):  # the super admin's: known to the portal, the same on every redeployed router
            ctx["CRYPTED_PASSWORD_BASE64"] = base64.b64encode(crypt_sha512(acc["password"]).encode()).decode()
        # nothing is taken from the old VM (2026-10-27): without a password in the secret, console login has none
        # SCHED_REQUIREMENTS: the hypervisor only, no cluster (owner, 2026-10-28): placed where its networks and image are
        tpl = {"NAME": new_name, "AUTOSTART": AUTOSTART, "SCHED_REQUIREMENTS": SCHED, "MEMORY_RESIZE_MODE": MEMORY_RESIZE_MODE,
               "OS": {"ARCH": "x86_64", "FIRMWARE": "UEFI", "MACHINE": "q35"},
               "CPU": p["vcpu"], "VCPU": p["vcpu"], "CPU_MODEL": {"MODEL": "host-passthrough"}, "MEMORY": p["ram_mb"],
               "VIDEO": {"TYPE": "virtio"}, "GRAPHICS": {"LISTEN": "0.0.0.0", "TYPE": "VNC"},
               "DISK": {"IMAGE_ID": p["image_id"]},
               "NIC": [{"NETWORK_ID": p["mgmt_net_id"], "IP": p["mgmt_ip"], "NAME": "MGMT"},
                       {"NETWORK_ID": ix["net_id"], "IP": p["service_ip"] or ix["ip"], "NAME": "VMNIC1"}],
               "CONTEXT": ctx}
        new_id = int(one.call("one.vm.allocate", template(tpl), False))
        task.step("new VM", "ok", f"{new_name} #{new_id} (host name {new_host}): management {p['mgmt_ip']}, "
                                  f"{ix['net']} {p['service_ip'] or ix['ip']}")
        _wait(one, new_id, "running", timeout=600)
        # 3 it answers on the management address and its BGP peers come up
        node.address, node.ssh_port = p["mgmt_ip"], ports["vyos"]
        node.name, node.hostname = new_name, new_host  # the platform's view finds the new VM by its name
        node.save(update_fields=["address", "ssh_port", "name", "hostname"])
        _reread(task, _control)  # the checks compare with the platform's view of the VM: the new one, not the old
        detail = _check(node, site, expected, MGMT_TIMEOUT + BGP_TIMEOUT)
        task.step("check", "ok", f"answers on {p['mgmt_ip']}:{ports['vyos']}; BGP {detail}")
    except Exception as exc:  # noqa: BLE001 - roll back, then report
        task.step("check", "failed", str(exc)[:400])
        node.address, node.ssh_port = old_address, old_port
        node.name, node.hostname = old_name, old_hostname  # the old VM comes back under its own name
        node.save(update_fields=["address", "ssh_port", "name", "hostname"])
        _rollback(task, one, node, new_id, old_id, ix, autostart=old_autostart, schedule=schedule)
        audit(user, "core_vm_redeploy_rolled_back", svc, site=site, vm=node.name, error=str(exc)[:300])
        _reread(task, _control)
        raise RedeployError(f"rolled back: {exc}") from exc
    # 4 the init script (the configuration with its secrets) leaves the platform's context; the VM works either way
    init_left = True
    try:
        removed = drop_init_script(one, new_id)
        init_left = False
        task.step("init script", "ok", "removed from the VM's context on the platform" if removed else "none in the context")
    except Exception as exc:  # noqa: BLE001 - reported; "Remove the init script" on the page tries again
        task.step("init script", "failed", f"still in the context: {str(exc)[:300]}")
    _reread(task, _control)
    from . import core_services as cs
    cs.set_init_flag(_control, new_id, init_left)
    node.deployed_vm_id = new_id  # made by the portal: compliant
    node.save(update_fields=["deployed_vm_id"])
    audit(user, "core_vm_redeployed", svc, site=site, vm=node.name, new_id=new_id,
          old_kept=f"{node.name}-old #{old_id}" if keep_old else "", mgmt=p["mgmt_ip"])
    old_removed = False
    if not keep_old:  # 5 asked not to keep it: the new VM has passed its checks, the old one goes
        try:
            finish(task, node, user)
            old_removed = True
        except Exception as exc:  # noqa: BLE001 - the new VM works; "Remove the old VM" on the page tries again
            task.step("old VM", "failed", f"{node.name}-old #{old_id} not removed: {str(exc)[:300]}")
    return {"new_id": new_id, "old_id": old_id, "mgmt_ip": p["mgmt_ip"], "bgp": detail, "old_removed": old_removed}


def _rollback(task, one, node, new_id, old_id, ix, autostart=AUTOSTART, schedule=None):
    """The old VM back in service. Its scheduled actions come back when the redeploy itself rolls back; a rollback by
    hand later does not know them — the old VM gets AUTOSTART "always" instead (what replaced the hourly "resume")."""
    if new_id is not None:
        one.call("one.vm.action", "terminate-hard", new_id)
        _wait(one, new_id, "gone")
        task.step("rollback: new VM", "ok", f"#{new_id} removed")
    from tenants.deploy import template
    one.call("one.vm.rename", old_id, node.name)
    one.call("one.vm.attachnic", old_id, template({"NIC": {"NETWORK_ID": ix["net_id"], "IP": ix["ip"]}}))
    _wait(one, old_id, "poweroff")
    one.call("one.vm.action", "resume", old_id)
    _wait(one, old_id, "running")
    if autostart and autostart.lower() != "no":  # back in service: it starts again with its host, as before
        set_autostart(one, old_id, autostart)
    put_schedule(one, old_id, schedule)
    task.step("rollback: old VM", "ok", f"#{old_id} back as {node.name} with {ix['net']} {ix['ip']}, running"
                                        + (f"; scheduled actions back: {_schedule_text(schedule)}" if schedule else ""))


def rollback(task, node, user=None):
    """Bring the old VM back while <name>-old exists: remove the new VM, give the old one its NIC, resume it."""
    if node.service.stype in ("cloud-gw", "portal-gw"):
        from . import gw_deploy
        return gw_deploy.rollback(task, node, user)
    from accounts.models import audit
    svc = node.service
    one, control = _one(svc)
    old = _vm(one, f"{node.name}-old")
    if old is None:
        _reread(task, control)
        raise RedeployError(f"{node.name}-old not found: nothing to roll back")
    new = _vm(one, node.name)
    old_id = int(old.findtext("ID"))
    seg = _seg(svc)
    ix = None
    if new is not None:
        ix = next((n for n in _nics(one.xml("one.vm.info", int(new.findtext("ID")), False)) if n["net"].startswith(seg)), None)
    if ix is None:
        raise RedeployError("the new VM's address on the service's network is not known: roll back by hand")
    _rollback(task, one, node, int(new.findtext("ID")), old_id, ix)
    vm = (node.facts or {}).get("vm") or {}
    node.address = ix["ip"]
    node.deployed_vm_id = None  # the old VM is back: not made by the portal
    node.save(update_fields=["address", "deployed_vm_id"])
    audit(user, "core_vm_redeploy_rolled_back", svc, site=svc.site.code, vm=node.name, by="hand")
    _reread(task, control)
    return {"old_id": old_id, "vm": vm.get("name", node.name)}


def finish(task, node, user=None):
    """Remove the old VM (<name>-old) once the new one has proven itself."""
    from accounts.models import audit
    svc = node.service
    one, control = _one(svc)
    old = _vm(one, f"{node.name}-old")
    if old is None:
        _reread(task, control)
        raise RedeployError(f"{node.name}-old not found (already removed)")
    old_id = int(old.findtext("ID"))
    if old.findtext("STATE") != "8":
        # running again (it ignored the power-off): removed only when it has no NIC left — then it is the stepped-aside
        # VM, cut off from every network, and nothing of it is used any more
        if _nics(one.xml("one.vm.info", old_id, False)):
            raise RedeployError(f"{node.name}-old #{old_id} is not powered off and still has NICs: not removed")
        one.call("one.vm.action", "poweroff-hard", old_id)
        _wait(one, old_id, "poweroff")
        task.step("old VM off", "ok", f"{node.name}-old #{old_id} was running without NICs: powered off hard")
    one.call("one.vm.action", "terminate-hard", old_id)
    _wait(one, old_id, "gone")
    task.step("old VM", "ok", f"{node.name}-old #{old_id} removed")
    audit(user, "core_vm_old_removed", svc, site=svc.site.code, vm=f"{node.name}-old", id=old_id)
    if svc.stype == "portal-gw":  # the old egw's public address (the VIP) is held for the VRRP of the new ones
        from .gw_deploy import hold_vips
        task.step("VIP", "ok", hold_vips(one, node) or "—")
    node.facts_at = node.facts_at or timezone.now()
    if "redeploy" in (node.facts or {}):
        node.facts = {k: v for k, v in node.facts.items() if k != "redeploy"}
        node.save(update_fields=["facts"])
    _reread(task, control)
    return {"removed": old_id}


def clean_context(task, node, user=None):
    """Remove the one-time init script from the context of the node's VM (made before the portal did it by itself)."""
    from accounts.models import audit
    from . import core_services as cs
    svc = node.service
    one, control = _one(svc)
    vm = _vm(one, node.name)
    if vm is None:
        raise RedeployError(f"VM {node.name} not found on {svc.platform.key}")
    vm_id = int(vm.findtext("ID"))
    removed = drop_init_script(one, vm_id)
    task.step("init script", "ok", f"{node.name} #{vm_id}: " + ("removed from the context" if removed else "none in the context"))
    if removed:
        audit(user, "core_vm_init_script_removed", svc, site=svc.site.code, vm=node.name, id=vm_id)
    _reread(task, control)
    cs.set_init_flag(control, vm_id, False)
    return {"vm": vm_id, "removed": removed}


def pending(site):
    """Nodes whose old VM is kept (from the last read of the platforms): {node pk: old vm name}."""
    from . import core_services as cs
    vms = cs.site_vms(site)
    return {n.pk: f"{n.name}-old" for n in CoreServiceNode.objects.filter(service__site__code=site)
            if f"{n.name}-old" in vms}
