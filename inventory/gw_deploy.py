"""The two VMs of cloud-gw made again from their templates (correction 11): the egw and the igw of the core services.

Like a route reflector's redeploy (inventory.core_deploy), with the gateways' own layout: the new VM's NICs are its
definition's (node.nic_list(), node.ips) — eth0 management (VRF MGMT; a new address unless one is defined), then the
egw's exchange address (the cloud-gw range) and its core-services address, or the igw's core-services address (with its
aliases) and its core-cloud address. The old VM (the VM of the node's name, or the one of the earlier standard
cloud-<role>_<site>) is powered off and gives up the NICs whose addresses the new one takes (with their aliases); its
other NICs (e.g. the igw's temporary cloud-ix NIC) stay with it. It is renamed <new name>-old and kept or removed.

    check   the new VM answers ssh on its management address; the egw also has BGP peers established
    rollback (failed check, or by hand while <name>-old exists): the new VM removed, the old one back under its name with
            the NICs it gave up, running

Its configuration: inventory.nfconfig.build_gateway (the tenants' egw/igw template with the site's context; the igw also
gets the management part and the service's custom part). Nothing is read from the old VM's configuration.
"""
import base64
import time

from django.utils import timezone

from . import core_deploy as cd
from .core_deploy import RedeployError
from .models import Cluster, ServiceImage


def _names(node):
    svc = node.service
    return [node.name, f"cloud-{node.role}_{svc.site.code}"]


def _find_old(one, node):
    for n in _names(node):
        v = cd._vm(one, n)
        if v is not None:
            return v
    return None


def _old_nics(one, vm_id):
    """[{nic_id, net, net_id, ip, alias, parent}] of a VM: its NICs and NIC aliases."""
    x = one.xml("one.vm.info", vm_id, False)
    out = []
    for tag, alias in (("TEMPLATE/NIC", False), ("TEMPLATE/NIC_ALIAS", True)):
        for n in x.findall(tag):
            out.append({"nic_id": int(n.findtext("NIC_ID")), "net": n.findtext("NETWORK") or "",
                        "net_id": int(n.findtext("NETWORK_ID") or -1), "ip": n.findtext("IP") or "", "alias": alias,
                        "name": n.findtext("NAME") or "", "parent": n.findtext("PARENT") or ""})
    return out


def plan(node):
    """What making this VM of cloud-gw again would do; reads only."""
    from . import nfconfig
    from .core_services import vm_status
    from .core_types import target_of
    svc = node.service
    site = svc.site.code
    role = node.role
    if role not in nfconfig.GATEWAY_ROLES:
        raise RedeployError(f"{node.name}: not an egw or igw of cloud-gw")
    one, control = cd._one(svc)
    old = _find_old(one, node)
    old_id = int(old.findtext("ID")) if old is not None else None
    old_nics = _old_nics(one, old_id) if old_id is not None else []
    new_nics, checks = [], []
    dynamic = set((node.params or {}).get("dynamic") or [])
    publics = {pb["name"]: pb for pb in (node.params or {}).get("publics") or []}
    portal = {"make": [], "add_ar": [], "vips": []}

    def c(check, ok, detail="", must=True):
        checks.append({"check": check, "ok": bool(ok), "detail": detail, "must": must})
    nets = node.nic_list()
    c("NIC 1 is the management network", nets and nets[0][0] is not None and nets[0][0].name == nfconfig.MGMT_NETWORK,
      ", ".join(f"NIC {i + 1} {n.name if n else '?'}" for i, (n, _g) in enumerate(nets)))
    for i, (net, _gw) in enumerate(nets):
        name = f"{net.name}_{site}" if net is not None else "?"
        v = one.find("one.vnpool.info", "VNET", name, -2, -1, -1) if net is not None else None
        ip = (node.ips or [])[i] if i < len(node.ips or []) else ""
        pb = publics.get(net.name) if net is not None else None
        if v is None and pb is not None:  # portal-gw: a public overlay made from the platform's VXLAN template
            tpl = f"portal-overlays_{site}"
            t = one.find("one.vntemplatepool.info", "VNTEMPLATE", tpl, -2, -1, -1)
            c(f"network {name}: made by the redeploy from {tpl}", t is not None, pb["cidr"] if t is not None else f"{tpl} not found")
            portal["make"].append({"name": name, "template_id": int(t.findtext("ID")) if t is not None else None, **pb})
            new_nics.append({"net": name, "net_id": None, "ip": ip})
            continue
        if v is None:
            c(f"network {name} on {svc.platform.key}", False, "not found")
            new_nics.append({"net": name, "net_id": None, "ip": ip})
            continue
        vid = int(v.findtext("ID"))
        if i == 0 and not ip:
            ip = cd._free_ip(one, vid, name)
            c("management address", bool(ip), f"free address {ip}", must=False)
        elif not ip and i in dynamic:  # portal-gw: a free address on the exchange (correction 12)
            ip = cd._free_ip(one, vid, name)
            c(f"address on {name}", bool(ip), f"free address {ip}", must=False)
        elif not ip:
            c(f"address on {name}", False, "not defined")
        if pb is not None and ip:
            full = one.xml("one.vn.info", vid, False)
            inside = any(_in_ar(ip, a) for a in full.findall("AR_POOL/AR"))
            if not inside:
                portal["add_ar"].append({"net_id": vid, "net": name, "ip": ip})
            c(f"{name} {ip}: in an address range", True, "yes" if inside else "added by the redeploy (1 address)", must=False)
            spoof = (full.findtext("TEMPLATE/FILTER_IP_SPOOFING") or "").upper()
            c(f"{name}: IP spoofing filter off (the VIP {pb['vip']} moves between the egws)", spoof != "YES",
              spoof or "not set", must=False)
            portal["vips"].append({"net_id": vid, "net": name, "vip": pb["vip"]})
        new_nics.append({"net": name, "net_id": vid, "ip": ip})
    aliases = [a for a in ((node.params or {}).get("aliases") or []) if a]
    cs_nic = next((n for n in new_nics if n["net"].startswith("cloud-services")), None)
    if aliases and cs_nic is None:
        c("aliases: a core-services NIC to carry them", False)
    want = {(n["net_id"], n["ip"]) for n in new_nics[1:] if n["ip"]} | {(cs_nic["net_id"], a) for a in aliases if cs_nic}
    kept = [n for n in old_nics if (n["net_id"], n["ip"]) in want]
    for n in new_nics[1:]:
        if old is not None and n["ip"] and not any(k["ip"] == n["ip"] and k["net_id"] == n["net_id"] for k in kept):
            c(f"{n['net']} {n['ip']}: the old VM has it (it gives it up)", False,
              "the old VM has no NIC with this address: the platform must have it free", must=False)
    left = [n for n in old_nics if n not in kept and not n["net"].startswith(nfconfig.MGMT_NETWORK)]
    c("NICs that stay with the old VM", True, ", ".join(f"{n['net']} {n['ip']}" for n in left) or "none", must=False)
    img = svc.image if svc.image_id and svc.image.control_id == control.pk else \
        ServiceImage.objects.filter(control=control, role=ServiceImage.VYOS).first()
    c("image customized through the context", img is not None and img.customization == "context" and img.one_id is not None,
      f"{img.name} #{img.one_id}" if img else "—")
    cl = Cluster.objects.filter(platform=svc.platform).exclude(state="retired").first()
    cid = None
    if cl is not None:
        x = one.find("one.clusterpool.info", "CLUSTER", cl.name)
        cid = int(x.findtext("ID")) if x is not None else None
    c("the cluster on the platform", cid is not None, cl.name if cl else "—")
    ports = nfconfig.management(site)[1]
    c("ssh port is the site's VyOS management port", svc.ssh_port == ports["vyos"], f"{svc.ssh_port} / {ports['vyos']}")
    from tenants.deploy import with_public
    from .credentials import account
    user, acc = account(svc.platform.sa) if svc.platform else (None, {})
    acc = with_public(acc or {})
    c("login user with an ssh key (the platform's super admin)", bool(user and acc.get("public")), user or "—")
    try:
        text = nfconfig.build_gateway(role, node, reveal=True, mgmt_ip=new_nics[0]["ip"] if new_nics else None)
        complete = nfconfig.is_complete(text)
    except nfconfig.BuildError as exc:
        text, complete = f"# {exc}", False
    new_name, new_host = target_of(node)
    return {"gateway": True, "role": role, "node": node.name, "new_name": new_name, "new_hostname": new_host,
            "platform": svc.platform.key, "old_id": old_id, "old_name": old.findtext("NAME") if old is not None else "",
            "old_state": vm_status(old.findtext("STATE") or "", old.findtext("LCM_STATE") or "") if old is not None else "",
            "old_nics": [{"net": n["net"], "ip": n["ip"]} for n in old_nics], "kept": kept, "new_nics": new_nics,
            "aliases": aliases, "mgmt_ip": new_nics[0]["ip"] if new_nics else "", "image": img.name if img else "",
            "image_id": img.one_id if img else None, "cluster": cl.name if cl else "", "cluster_id": cid,
            "vcpu": svc.vcpu or 2, "ram_mb": int(float(svc.ram_gb or 4) * 1024), "checks": checks,
            "definition_ok": all(x["ok"] for x in checks if x.get("must", True)), "complete": complete,
            "config": cd.masked(text), "ix": {"net": "", "ip": ""}, "mgmt_net": new_nics[0]["net"] if new_nics else "",
            "old_exists": any(cd._vm(one, f"{n}-old") is not None for n in (node.name, new_name)),
            "portal": portal if svc.stype == "portal-gw" else None}


def _in_ar(ip, ar):
    import ipaddress
    try:
        start = ipaddress.ip_address(ar.findtext("IP") or "")
        return start <= ipaddress.ip_address(ip) < start + int(ar.findtext("SIZE") or 0)
    except ValueError:
        return False


def portal_prepare(task, one, node, p):
    """portal-gw: the public overlays not on the platform yet are made from its VXLAN template (the first one exists and is
    kept); the egw's public addresses outside the networks' address ranges are added (one address each)."""
    from tenants.deploy import template
    site = node.service.site.code
    made = []
    for m in (p.get("portal") or {}).get("make") or []:
        if m["template_id"] is None:
            raise RedeployError(f"portal-overlays_{site} not found: {m['name']} cannot be made")
        import ipaddress
        net = ipaddress.ip_network(m["cidr"])
        hs = list(net.hosts())
        extra = {"NETWORK_ADDRESS": str(net.network_address), "NETWORK_MASK": str(net.netmask), "GATEWAY": m["vip"],
                 "AR": [{"TYPE": "IP4", "IP": m["vip"], "SIZE": 1}, {"TYPE": "IP4", "IP": str(hs[1]), "SIZE": 2},
                        {"TYPE": "IP4", "IP": str(hs[3]), "SIZE": len(hs) - 3}]}
        vid = int(one.call("one.vntemplate.instantiate", m["template_id"], f"{m['name']}_{site}", template(extra)))
        for n in p["new_nics"]:
            if n["net"] == f"{m['name']}_{site}":
                n["net_id"] = vid
        made.append(f"{m['name']}_{site} #{vid}")
    for a in (p.get("portal") or {}).get("add_ar") or []:
        one.call("one.vn.add_ar", a["net_id"], template({"AR": {"TYPE": "IP4", "IP": a["ip"], "SIZE": 1}}))
        made.append(f"{a['net']}: address range {a['ip']}")
    task.step("public networks", "ok", "; ".join(made) or "nothing to make")


def hold_vips(one, node):
    """portal-gw: each public overlay's VIP held on the network once no VM leases it (it lives on the egws' VRRP)."""
    from tenants.deploy import template
    out = []
    for pb in (node.params or {}).get("publics") or []:
        v = one.find("one.vnpool.info", "VNET", f"{pb['name']}_{node.service.site.code}", -2, -1, -1)
        if v is None:
            continue
        full = one.xml("one.vn.info", int(v.findtext("ID")), False)
        leased = {l.findtext("IP"): l.findtext("VM") for l in full.findall("AR_POOL/AR/LEASES/LEASE")}
        if pb["vip"] in leased:
            out.append(f"{pb['vip']} {'held' if leased[pb['vip']] in ('-1', None, '') else 'leased by VM #' + str(leased[pb['vip']])}")
            continue
        one.call("one.vn.hold", int(v.findtext("ID")), template({"LEASES": {"IP": pb["vip"]}}))
        out.append(f"{pb['vip']} held")
    return "; ".join(out)


def _ready(node, site, role, timeout):
    """Until the new VM answers ssh on its management address (and the egw has BGP peers established)."""
    from . import core_services as cs
    end, last = time.monotonic() + timeout, ""
    while True:
        cs.read_node(node, site)
        node.refresh_from_db()
        if not node.facts_error:
            if role != "egw":
                return "answers ssh"
            bgp = next((c for c in node.facts.get("checks", []) if c["check"].startswith("BGP")), None)
            if bgp and bgp["ok"]:
                return f"answers ssh; BGP {bgp['detail']}"
            last = f"BGP {bgp.get('detail') if bgp else '—'}"
        else:
            last = node.facts_error
        if time.monotonic() > end:
            raise RedeployError(f"{node.name} not ready after {timeout} s: {last}")
        time.sleep(cd._poll() * 2 or 0)


def _give_back(one, old_id, kept):
    """The old VM gets the NICs (and aliases) it gave up again; NICs first (aliases need their parent)."""
    from tenants.deploy import template
    names = {}
    for n in [k for k in kept if not k["alias"]]:
        one.call("one.vm.attachnic", old_id, template({"NIC": {"NETWORK_ID": n["net_id"], "IP": n["ip"]}}))
        cd._wait(one, old_id, "poweroff")
        x = one.xml("one.vm.info", old_id, False)
        got = next((m.findtext("NAME") for m in x.findall("TEMPLATE/NIC") if m.findtext("IP") == n["ip"]), "")
        names[n["name"]] = got
    for n in [k for k in kept if k["alias"]]:
        one.call("one.vm.attachnic", old_id, template({"NIC_ALIAS": {"NETWORK_ID": n["net_id"], "IP": n["ip"],
                                                                     "PARENT": names.get(n["parent"], n["parent"])}}))
        cd._wait(one, old_id, "poweroff")


def redeploy(task, node, user=None, force=False, keep_old=True):
    """Make the egw or igw of cloud-gw again (see the module); rolls back by itself if the new VM is not ready."""
    from accounts.models import audit
    from tenants.deploy import crypt_sha512, template, with_public
    from . import nfconfig
    from .credentials import account
    svc = node.service
    site = svc.site.code
    p = plan(node)
    role = p["role"]
    task.step("plan", "ok", f"{node.name} ({role}) on {p['platform']}: old #{p['old_id'] or '—'}; new "
                            + ", ".join(f"{n['net']} {n['ip']}" for n in p["new_nics"]))
    if p["old_exists"]:
        raise RedeployError(f"{p['new_name']}-old exists: finish or roll back the previous redeploy first")
    if not p["complete"]:
        raise RedeployError("the configuration has values missing (see the plan)")
    if not p["definition_ok"]:
        bad = [c["check"] for c in p["checks"] if c["must"] and not c["ok"]]
        if not force:
            raise RedeployError("the service's definition does not fit: " + "; ".join(bad))
        task.step("definition", "failed", "does not fit, redeployed anyway (forced): " + "; ".join(bad))
    one, control = cd._one(svc)
    user_name, acc = account(svc.platform.sa)
    acc = with_public(acc or {})
    if not user_name or not acc.get("public"):
        raise RedeployError(f"{svc.platform.key}: the super admin secret has no ssh key")
    old_id, kept = p["old_id"], p["kept"]
    if p.get("portal"):  # portal-gw: its public overlays and addresses first (correction 12)
        portal_prepare(task, one, node, p)
    old_address, old_port, old_name, old_hostname = node.address, node.ssh_port, node.name, node.hostname
    new_name, new_host = p["new_name"], p["new_hostname"]
    audit(user, "core_vm_redeploy_started", svc, site=site, vm=node.name, new_name=new_name, old_id=old_id, role=role,
          mgmt=p["mgmt_ip"], forced=bool(force and not p["definition_ok"]))
    old_autostart = ""
    if old_id is not None:  # 1 the old VM steps aside
        how = cd._powered_off(one, old_id)
        old_autostart = one.xml("one.vm.info", old_id, False).findtext("USER_TEMPLATE/AUTOSTART") or ""
        if old_autostart.lower() not in ("", "no"):
            cd.set_autostart(one, old_id, "no")
        for n in sorted(kept, key=lambda k: not k["alias"]):  # aliases before their NIC
            one.call("one.vm.detachnic", old_id, n["nic_id"])
            cd._wait(one, old_id, "poweroff")
        one.call("one.vm.rename", old_id, f"{new_name}-old")
        task.step("old VM aside", "ok", f"#{old_id} powered off{' (hard)' if how == 'hard' else ''}; gave up "
                  + (", ".join(f"{n['net']} {n['ip']}" for n in kept) or "nothing") + f"; renamed {new_name}-old")
        node.facts = {**(node.facts or {}), "redeploy": {"old_id": old_id, "old_name": p["old_name"], "kept": kept,
                                                          "autostart": old_autostart}}
        node.save(update_fields=["facts"])
    new_id = None
    try:
        text = nfconfig.build_gateway(role, node, reveal=True, mgmt_ip=p["mgmt_ip"])
        init = base64.b64encode(nfconfig.init_script(text, f"{site} {svc.name} {new_name}").encode()).decode()
        ports = nfconfig.management(site)[1]
        ctx = {"NETWORK": "yes", "SET_HOSTNAME": new_host, "USERNAME": user_name, "SSH_PUBLIC_KEY": acc["public"],
               "SSH_PORT": ports["vyos"], "ONE_TIME_INIT_SCRIPT_BASE64": init, "TIMEZONE": svc.site.tz}
        if acc.get("password"):
            ctx["CRYPTED_PASSWORD_BASE64"] = base64.b64encode(crypt_sha512(acc["password"]).encode()).decode()
        nics = [{"NETWORK_ID": n["net_id"], "IP": n["ip"], "NAME": "MGMT" if i == 0 else f"VMNIC{i}"}
                for i, n in enumerate(p["new_nics"])]
        parent = next((f"VMNIC{i}" for i, n in enumerate(p["new_nics"]) if i and n["net"].startswith("cloud-services")), None)
        # SCHED_REQUIREMENTS: the hypervisor only, no cluster (owner, 2026-10-28): placed where its networks and image are
        tpl = {"NAME": new_name, "AUTOSTART": cd.AUTOSTART, "SCHED_REQUIREMENTS": cd.SCHED, "MEMORY_RESIZE_MODE": cd.MEMORY_RESIZE_MODE,
               "OS": {"ARCH": "x86_64", "FIRMWARE": "UEFI", "MACHINE": "q35"},
               "CPU": p["vcpu"], "VCPU": p["vcpu"], "CPU_MODEL": {"MODEL": "host-passthrough"}, "MEMORY": p["ram_mb"],
               "VIDEO": {"TYPE": "virtio"}, "GRAPHICS": {"LISTEN": "0.0.0.0", "TYPE": "VNC"},
               "DISK": {"IMAGE_ID": p["image_id"]}, "NIC": nics, "CONTEXT": ctx}
        if p["aliases"] and parent:
            cs_id = next(n["net_id"] for n in p["new_nics"] if n["net"].startswith("cloud-services"))
            tpl["NIC_ALIAS"] = [{"NETWORK_ID": cs_id, "IP": a, "PARENT": parent} for a in p["aliases"]]
        new_id = int(one.call("one.vm.allocate", template(tpl), False))
        task.step("new VM", "ok", f"{new_name} #{new_id} (host name {new_host}): "
                  + ", ".join(f"{n['net']} {n['ip']}" for n in p["new_nics"])
                  + (f"; aliases {' '.join(p['aliases'])}" if p["aliases"] else ""))
        cd._wait(one, new_id, "running", timeout=600)
        node.address, node.ssh_port, node.name, node.hostname = p["mgmt_ip"], ports["vyos"], new_name, new_host
        node.save(update_fields=["address", "ssh_port", "name", "hostname"])
        cd._reread(task, control)
        detail = _ready(node, site, role, cd.MGMT_TIMEOUT + cd.BGP_TIMEOUT)
        task.step("check", "ok", f"{p['mgmt_ip']}:{ports['vyos']}: {detail}")
    except Exception as exc:  # noqa: BLE001 - roll back, then report
        task.step("check", "failed", str(exc)[:400])
        node.address, node.ssh_port, node.name, node.hostname = old_address, old_port, old_name, old_hostname
        node.save(update_fields=["address", "ssh_port", "name", "hostname"])
        _rollback(task, one, new_id, old_id, p["old_name"], kept, old_autostart)
        node.refresh_from_db()
        node.facts = {k: v for k, v in (node.facts or {}).items() if k != "redeploy"}
        node.save(update_fields=["facts"])
        audit(user, "core_vm_redeploy_rolled_back", svc, site=site, vm=node.name, error=str(exc)[:300])
        cd._reread(task, control)
        raise RedeployError(f"rolled back: {exc}") from exc
    try:
        removed = cd.drop_init_script(one, new_id)
        task.step("init script", "ok", "removed from the VM's context" if removed else "none in the context")
    except Exception as exc:  # noqa: BLE001 - reported
        task.step("init script", "failed", f"still in the context: {str(exc)[:300]}")
    node.deployed_vm_id = new_id
    node.save(update_fields=["deployed_vm_id"])
    audit(user, "core_vm_redeployed", svc, site=site, vm=node.name, new_id=new_id, role=role, mgmt=p["mgmt_ip"],
          old_kept=f"{new_name}-old #{old_id}" if keep_old and old_id else "")
    if not keep_old and old_id is not None:
        try:
            cd.finish(task, node, user)
        except Exception as exc:  # noqa: BLE001 - the new VM works
            task.step("old VM", "failed", f"{new_name}-old #{old_id} not removed: {str(exc)[:300]}")
    elif p.get("portal"):
        task.step("VIP", "ok", hold_vips(one, node) or "—")
    cd._reread(task, control)
    return {"new_id": new_id, "old_id": old_id, "mgmt_ip": p["mgmt_ip"]}


def _rollback(task, one, new_id, old_id, old_name, kept, autostart):
    if new_id is not None:
        one.call("one.vm.action", "terminate-hard", new_id)
        cd._wait(one, new_id, "gone")
        task.step("rollback: new VM", "ok", f"#{new_id} removed")
    if old_id is None:
        return
    one.call("one.vm.rename", old_id, old_name)
    _give_back(one, old_id, kept)
    one.call("one.vm.action", "resume", old_id)
    cd._wait(one, old_id, "running")
    if autostart and autostart.lower() != "no":
        cd.set_autostart(one, old_id, autostart)
    task.step("rollback: old VM", "ok", f"#{old_id} back as {old_name} with "
              + (", ".join(f"{n['net']} {n['ip']}" for n in kept) or "its NICs") + ", running")


def rollback(task, node, user=None):
    """By hand, while <name>-old exists: the new VM removed, the old one back as it was (what the redeploy recorded)."""
    from accounts.models import audit
    svc = node.service
    one, control = cd._one(svc)
    rec = (node.facts or {}).get("redeploy") or {}
    old = cd._vm(one, f"{node.name}-old")
    if old is None or not rec:
        cd._reread(task, control)
        raise RedeployError(f"{node.name}-old not found (or not recorded): nothing to roll back")
    new = cd._vm(one, node.name)
    _rollback(task, one, int(new.findtext("ID")) if new is not None else None, int(old.findtext("ID")),
              rec.get("old_name") or node.name, rec.get("kept") or [], rec.get("autostart") or "")
    node.name = rec.get("old_name") or node.name
    node.deployed_vm_id = None
    node.facts = {k: v for k, v in (node.facts or {}).items() if k != "redeploy"}
    node.save(update_fields=["name", "deployed_vm_id", "facts"])
    audit(user, "core_vm_redeploy_rolled_back", svc, site=svc.site.code, vm=node.name, by="hand")
    cd._reread(task, control)
    return {"old_id": int(old.findtext("ID"))}
