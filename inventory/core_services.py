"""Core services of a site: what the site itself runs — name servers, jump hosts, the platforms' front ends, route
reflectors, gateways, backups, the portal's own VMs.

Two ways a service is deployed:
  on hosts      on hosts that are already there (their OS): the front end of the management platform, NTP on its hosts
  from scratch  its own VMs are made for it on a platform (usually the management one); every VM of the service has
                the same size and networks (NIC 1 network, optional gateway and routes, optional NIC 2 network)

Each host or VM (node) has its address, ssh port and the user secret the portal logs in with. Reading a service is
read only: the platform's view of its VMs (one.vmpool.info, group oneadmin) and, over ssh, the host name, OS, CPU,
memory, disk and addresses.

`seed(site)` makes the services of the standard Claugine layout from the VMs of the site's platforms and the hosts of
its management cluster, by the VM names (cloud-ns1_<site>, cloud-one2_<site>, cloud-ix-rr3_<site>, …); a service that
exists is never changed by it.
"""
import ipaddress
import math
import re

from django.utils import timezone

from .models import Cluster, ConfigTemplate, CoreService, CoreServiceNode, Network, Platform, PlatformControl

# The standard layout. match: the VM name without the site suffix; host: on the hosts of the management cluster.
CATALOGUE = [
    {"name": "mgmt-fe", "serves": "mgmt", "title": "OpenNebula front end of the management platform", "kind": "host", "hosts": "mgmt",
     "description": "On each host of the management cluster (always three hosts).",
     "checks": {"units": ["opennebula", "opennebula-fireedge", "mysql"], "ports": [2633, 2616], "vip": "200"}},
    {"name": "ntp", "title": "NTP", "kind": "host", "hosts": "mgmt",
     "description": "On the KVM/front-end hosts of the management cluster.", "checks": {"ntp": True}},
    {"name": "ns", "title": "Name servers", "match": r"cloud-ns\d+", "port": 22,
     "checks": {"units": ["named"], "ports": [53]}},
    {"name": "admin", "title": "Admin jump hosts", "match": r"cloud-admin\d+", "port": "ssh",
     "checks": {"units": ["xrdp"], "ports": ["ssh", "rdp"]}},
    {"name": "graylog", "title": "Log collector (Graylog)", "match": r"cloud-onelog\d+", "port": 22,
     "checks": {"units": ["graylog-server", "graylog-datanode", "mongod"], "ports": [9000, 15142]}},
    {"name": "internal", "title": "Internal services", "match": r"cloud-claugine\d+", "port": "ssh",
     "description": "The internal web site used to deploy hosts.",
     "checks": {"units": ["apache2"], "ports": [80]}},
    {"name": "shared-fe", "serves": "shared", "title": "OpenNebula front end of the shared platform", "match": r"cloud-one\d+", "port": 22,
     "checks": {"units": ["opennebula", "opennebula-fireedge", "mysql"], "ports": [2633, 2616], "vip": "30",
                "leader_units": ["opennebula-gate", "opennebula-flow"], "leader_ports": [5030, 2474]}},
    {"name": "ext-managed-fe", "serves": "ext-managed", "title": "OpenNebula front end of the ext-managed platform", "match": r"portal-one\d+",
     "port": 22, "checks": {"units": ["opennebula", "opennebula-fireedge", "mysql"], "ports": [2633, 2616], "vip": "60"}},
    {"name": "cloud-ix-rrs", "serves": "shared", "title": "Route reflectors — internet exchange (shared platform)", "match": r"cloud-ix-rr\d+",
     "port": "vyos", "template": "rr-inet", "checks": {"bgp": "ipv4"}},
    {"name": "corpnet-ix-rrs", "serves": "shared", "title": "Route reflectors — corporate exchange (shared platform)",
     "match": r"corpnet-ix-rr\d+", "port": "vyos", "template": "rr-corpnet", "checks": {"bgp": "ipv4"}},
    {"name": "cloud-vxlan-rrs", "serves": "shared", "title": "Route reflectors — VXLAN/EVPN (shared platform)", "match": r"cloud-vxlan-rr\d+",
     "port": "vyos", "template": "rr-vxlan", "checks": {"bgp": "evpn"}},
    {"name": "portal-ix-rrs", "serves": "ext-managed", "title": "Route reflectors — internet exchange (ext-managed platform)",
     "match": r"portal-ix-rr\d+", "port": "vyos", "template": "rr-inet", "checks": {"bgp": "ipv4"}},
    {"name": "portal-vxlan-rrs", "serves": "ext-managed", "title": "Route reflectors — VXLAN/EVPN (ext-managed platform)",
     "match": r"portal-vxlan-rr\d+", "port": "vyos", "template": "rr-vxlan", "checks": {"bgp": "evpn"}},
    {"name": "cloud-igw", "nets": ["cloud-services", "cloud-core"], "title": "IGW of the cloud-services subnet", "match": r"cloud-igw", "port": "vyos", "template": "igw",
     "description": "Target: three NICs — management, cloud-services, cloud-core (it has more today)."},
    {"name": "cloud-egw", "title": "EGW of the cloud-services subnet", "match": r"cloud-egw", "port": "vyos", "template": "egw",
     "checks": {"bgp": "ipv4"}},
    {"name": "portal-egw", "serves": "ext-managed", "title": "EGW of the ext-managed platform (portal-ix, portal-public)",
     "match": r"portal-egw\d*", "port": "vyos", "template": "egw", "checks": {"bgp": "ipv4"},
     "description": "On the ext-managed platform itself (not on the management platform)."},
    {"name": "claugine-lb", "title": "Claugine load balancer (HAProxy)", "match": r"claugine-lb", "port": 22,
     "checks": {"units": ["haproxy"], "ports": [80, 443]}},
    {"name": "claugine-portal", "title": "Claugine portal", "match": r"claugine-app", "port": 22,
     "checks": {"units": ["apache2", "mysql", "claugine-ha"], "ports": [443, 3306]}},
    {"name": "claugine-web", "title": "Claugine web site", "match": r"claugine-web", "port": 22,
     "description": "Managed by the claugine-web project.", "checks": {"ports": [80]}},
    {"name": "mgmt-backup", "serves": "mgmt", "title": "Backup storage — management platform", "match": r"cloud-mgmtbkp\d+", "port": 22},
    {"name": "shared-backup", "serves": "shared", "title": "Backup storage — shared platform", "match": r"cloud-onebkp\d+", "port": 22},
    {"name": "ext-managed-backup", "serves": "ext-managed", "title": "Backup storage — ext-managed platform", "match": r"cloud-portalbkp\d+",
     "port": 22},
]
CHECKS = {spec["name"]: spec.get("checks", {}) for spec in CATALOGUE}
NET_ALIASES = {"one-vxlan": "cloud-vxlan"}  # the platform's name of a site network → the portal's
READ_SCRIPT = (
    "echo '##host'; hostname; echo '##os'; (. /etc/os-release 2>/dev/null && echo \"$PRETTY_NAME\"); "
    "echo '##cpu'; nproc; echo '##mem'; awk '/MemTotal/{print int($2/1024)}' /proc/meminfo; "
    "echo '##disk'; df -BG --output=size / 2>/dev/null | tail -1; "
    "echo '##addr'; ip -4 -o addr show 2>/dev/null | awk '$2 != \"lo\" {print $2\" \"$4}'"
)


def checks_of(svc, node=None):
    """What a read verifies on the service's hosts/VMs: a VM's own (cloud-gw: the egw's BGP), the service's, else those
    of the standard layout."""
    if node is not None and "checks" in (node.params or {}):
        return node.params["checks"] or {}
    return svc.checks or site_checks(CHECKS.get(svc.name, {}), svc.site.code if svc.site_id else "")


def check_script(checks):
    out = []
    units = list(checks.get("units", [])) + list(checks.get("leader_units", []))
    if units:
        out.append("echo '##units'; systemctl is-active " + " ".join(units) + " 2>/dev/null")
    if checks.get("ports") or checks.get("leader_ports"):
        out.append("echo '##ports'; ss -ltnH 2>/dev/null | awk '{print $4}'")
    if checks.get("ntp"):
        out.append("echo '##ntp'; timedatectl show -p NTPSynchronized --value 2>/dev/null")
    if checks.get("bgp") == "ipv4":
        out.append("echo '##bgp'; vtysh -c 'show bgp summary json' 2>/dev/null | tr -d '\\n'; echo")
    elif checks.get("bgp") == "evpn":
        out.append("echo '##bgp'; vtysh -c 'show bgp l2vpn evpn summary json' 2>/dev/null | tr -d '\\n'; echo")
    return "; ".join(out)


def _peers(data):
    """The peers dict of a 'show bgp … summary json' answer (wherever FRR puts it)."""
    if isinstance(data, dict):
        if isinstance(data.get("peers"), dict):
            return data["peers"]
        for v in data.values():
            found = _peers(v)
            if found is not None:
                return found
    return None


def floating_address(checks, address, facts):
    """The platform's floating address for this node: checks["vip"] is an address or a host number in the subnet of
    the node's own address (as the node reports its prefix). None when not set or not computable."""
    vip = str(checks.get("vip") or "").strip()
    if not vip:
        return None
    if "." in vip:
        return vip
    for a in facts.get("addresses", []):
        cidr = a.split()[-1]
        if cidr.split("/")[0] == address and "/" in cidr:
            net = ipaddress.ip_interface(cidr).network
            try:
                return str(net.network_address + int(vip)) if int(vip) < net.num_addresses else None
            except ValueError:
                return None
    return None


def is_leader(checks, address, facts):
    """True/False: the node holds the floating address (it is the leader); None: no floating address to tell by."""
    vip = floating_address(checks, address, facts)
    if not vip or not facts.get("addresses"):
        return None
    return vip in {a.split()[-1].split("/")[0] for a in facts["addresses"]}


def evaluate(checks, out, facts, address=None):
    """[{check, ok, detail}] from what the node answered and what it reported. Leader-only units and ports are checked
    on the node that holds the floating address; on the others they are not required."""
    import json
    parts, cur = {}, None
    for line in (out or "").splitlines():
        if line.startswith("##"):
            cur = line[2:].strip()
            parts[cur] = []
        elif cur:
            parts[cur].append(line.strip())
    res = []
    leader = is_leader(checks, address, facts)
    if checks.get("vip"):
        vip = floating_address(checks, address, facts)
        res.append({"check": "leader (holds the floating address)" if leader else "follower", "ok": True,
                    "detail": vip or "?", "role": True})
    states = parts.get("units", [])
    for i, unit in enumerate(list(checks.get("units", [])) + list(checks.get("leader_units", []))):
        st = states[i] if i < len(states) else "?"
        if i >= len(checks.get("units", [])):
            if leader is False:
                continue  # leader only
            res.append({"check": f"{unit} running (leader)", "ok": st == "active", "detail": st})
        else:
            res.append({"check": f"{unit} running", "ok": st == "active", "detail": st})
    listening = {x.rsplit(":", 1)[-1] for x in parts.get("ports", []) if ":" in x}
    for port in checks.get("ports", []):
        res.append({"check": f"port {port} listening", "ok": str(port) in listening, "detail": ""})
    if leader is not False:
        for port in checks.get("leader_ports", []):
            res.append({"check": f"port {port} listening (leader)", "ok": str(port) in listening, "detail": ""})
    if checks.get("ntp"):
        v = (parts.get("ntp") or [""])[0]
        res.append({"check": "time synchronized (NTP)", "ok": v == "yes", "detail": v or "?"})
    if checks.get("bgp"):
        raw = " ".join(parts.get("bgp", []))
        try:
            peers = _peers(json.loads(raw)) or {}
        except ValueError:
            peers = None
        if peers is None:
            res.append({"check": f"BGP ({checks['bgp']}) peers established", "ok": False, "detail": "no answer from vtysh"})
        else:
            down = sorted(ip for ip, p in peers.items() if (p.get("state") or "").lower() != "established")
            res.append({"check": f"BGP ({checks['bgp']}) peers established", "ok": bool(peers) and not down,
                        "detail": f"{len(peers) - len(down)}/{len(peers)}" + (f"; down: {', '.join(down[:8])}" if down else "")})
    vm = facts.get("vm")
    if vm and facts.get("addresses"):  # the platform's addresses of the VM are on it
        have = {a.split()[-1].split("/")[0] for a in facts["addresses"]}
        want = [ip for _net, ip in vm.get("nics", []) if ip]
        missing = [ip for ip in want if ip not in have]
        res.append({"check": "the platform's addresses on the VM", "ok": not missing,
                    "detail": ("missing " + ", ".join(missing)) if missing else f"{len(want)}"})
    return res


def short(vm_name, site):
    """A VM's name without the site suffix: cloud-ns1_dc1 → cloud-ns1."""
    return re.sub(rf"_{re.escape(site)}$", "", vm_name or "")


def site_network(site, name):
    """The site network a platform network name stands for (cloud-ix_dc1 → cloud-ix)."""
    base = NET_ALIASES.get(short(name, site), short(name, site))
    return Network.objects.filter(site=site, name=base).first()


# --- the platforms' own VMs ---------------------------------------------------------------------------------------

def read_platform_vms(control):
    """The VMs of group oneadmin on a platform, as the platform sees them (read only); kept on the control."""
    from tenants.deploy import One
    one = One(control)
    x = one.xml("one.vmpool.info", -2, -1, -1, -1)
    try:
        clusters = {c.findtext("ID"): c.findtext("NAME") for c in one.xml("one.clusterpool.info").findall("CLUSTER")}
    except Exception:  # noqa: BLE001 - the cluster names are a nicety
        clusters = {}
    rows = []
    for v in x.findall("VM"):
        if v.findtext("GNAME") != "oneadmin":
            continue
        t = v.find("TEMPLATE")
        hist = v.findall("HISTORY_RECORDS/HISTORY")
        last = hist[-1] if hist else None
        rows.append({
            "host": last.findtext("HOSTNAME") if last is not None else "",
            "cluster": clusters.get(last.findtext("CID") or "", last.findtext("CID") or "") if last is not None else "",
            "status": vm_status(v.findtext("STATE") or "", v.findtext("LCM_STATE") or ""),
            "name": v.findtext("NAME") or "", "id": int(v.findtext("ID") or 0),
            "vcpu": int(float(t.findtext("VCPU") or t.findtext("CPU") or 0)),
            "ram_gb": round(int(t.findtext("MEMORY") or 0) / 1024),  # whole GB (owner 2026-10-29 20:59)
            "disks_gb": [round(int(d.findtext("SIZE") or 0) / 1024) for d in t.findall("DISK")],
            "nics": [[n.findtext("NETWORK") or "", n.findtext("IP") or ""] for n in t.findall("NIC")],
            "state": v.findtext("STATE") or "", "lcm_state": v.findtext("LCM_STATE") or "",
            # what a rebuild needs to know (names only: context values can hold secrets)
            "images": [d.findtext("IMAGE") or "" for d in t.findall("DISK")],
            "template_id": t.findtext("TEMPLATE_ID") or "",
            "context_keys": sorted({c.tag for c in (t.find("CONTEXT") if t.find("CONTEXT") is not None else [])}),
            "nic_details": [{"network": n.findtext("NETWORK") or "", "ip": n.findtext("IP") or "",
                             "gateway": n.findtext("GATEWAY") or "", "model": n.findtext("MODEL") or ""}
                            for n in t.findall("NIC")],
        })
    before = {r.get("id"): r.get("init_script") for r in control.vms or []}  # the pool has no context: kept from vm.info
    for r in rows:
        if before.get(r["id"]) is not None:
            r["init_script"] = before[r["id"]]
    control.vms, control.vms_at = sorted(rows, key=lambda r: r["name"]), timezone.now()
    control.save(update_fields=["vms", "vms_at"])
    return control.vms


INIT_KEY = "ONE_TIME_INIT_SCRIPT_BASE64"  # a VyOS VM's first configuration, secrets included


def has_init_script(one, vm_id):
    """Whether the VM's context still holds its one-time init script (with a value)."""
    ctx = one.xml("one.vm.info", int(vm_id), False).find("TEMPLATE/CONTEXT")
    return bool(ctx is not None and (ctx.findtext(INIT_KEY) or "").strip())


def set_init_flag(control, vm_id, value):
    """Keep on the last read whether the VM's context holds the init script."""
    rows = control.vms or []
    for r in rows:
        if r.get("id") == int(vm_id):
            r["init_script"] = value
    control.vms = rows
    control.save(update_fields=["vms"])


def read_init_flags(control, names):
    """For the named VMs (VyOS-based core services): whether the init script is still in their context (vm.info each)."""
    from tenants.deploy import One
    one = One(control)
    n = 0
    for r in control.vms or []:
        if r["name"] in names or r["name"].removesuffix("-old") in names:
            r["init_script"] = has_init_script(one, r["id"])
            n += r["init_script"]
    control.save(update_fields=["vms"])
    return n


def refresh_vms(site):
    """Read the VMs of every platform of the site again (the platform's view only; quick). [(key, error)]"""
    errors = []
    for c in PlatformControl.objects.filter(key__startswith=f"{site}."):
        try:
            read_platform_vms(c)
        except Exception as exc:  # noqa: BLE001 - one platform's error does not stop the rest
            errors.append((c.key, str(exc)[:200]))
    return errors


VM_STATES = {"0": "init", "1": "pending", "2": "hold", "4": "stopped", "5": "suspended", "6": "done", "8": "poweroff",
             "9": "undeployed", "10": "cloning", "11": "cloning failure"}


def vm_status(state, lcm):
    """A VM's status in one word, as OpenNebula shows it (running, poweroff, …)."""
    if state == "3":
        return {"3": "running", "0": "lcm init", "1": "prolog", "2": "boot", "14": "failure", "36": "boot failure",
                "16": "unknown"}.get(lcm, "active")
    return VM_STATES.get(state, state or "?")


def site_vms(site):
    """{vm name: (platform key, vm)} of every platform of the site, from the last read."""
    out = {}
    for c in PlatformControl.objects.filter(key__startswith=f"{site}."):
        for vm in c.vms or []:
            out[vm["name"]] = (c.key, vm)
    return out


def unassigned(site):
    """The management platform's own VMs that belong to no core service: [(platform key, vm)]."""
    names = set(CoreServiceNode.objects.filter(service__site__code=site).values_list("name", flat=True))
    mgmt = {p.key for p in Platform.objects.filter(site__code=site, kind="mgmt")}
    return [(k, vm) for name, (k, vm) in sorted(site_vms(site).items())
            if k in mgmt and name not in names and not name.startswith(("_", "tenant_"))]  # tenants' objects


def login_address(vm, site):
    """The address the portal logs in to a VM at: on the core network if it has one (cloud-core, one-core), else its
    first address that is not on a storage network."""
    ips = [(short(net, site), ip) for net, ip in vm["nics"] if ip]
    for want in ("cloud-core", "one-core"):
        for net, ip in ips:
            if net == want:
                return ip
    return next((ip for net, ip in ips if not net.startswith("data-")), vm["name"])


# --- the standard layout ------------------------------------------------------------------------------------------

def _default_secret(site):
    from .sshexec import default_account
    return default_account(site)


def image_of(platform, vm, vyos=False):
    """The service image a VM is made from: the platform's VyOS image for a VyOS service, else the image row named
    like the VM's first disk. None when the platform has no such row."""
    from .models import ServiceImage
    if platform is None:
        return None
    rows = ServiceImage.objects.filter(control__key=platform.key, kind="image")
    if vyos:
        return rows.filter(role=ServiceImage.VYOS).first()
    disk = (vm.get("images") or [""])[0]
    return rows.filter(name=disk).first() if disk else None


VYOS_PORT = "vyos"  # the catalogue names a site's port by its purpose (Site → management ports), not by number


def site_port(value, site):
    """A port of the catalogue: a number, or a purpose of the site's management ports ("vyos", "ssh", "rdp")."""
    if isinstance(value, int):
        return value
    from .nfconfig import management
    return management(site)[1].get(value) or {"rdp": 3389}.get(value, 22)


def site_checks(checks, site):
    """The checks with the ports of the site in place of their purposes."""
    if not checks:
        return {}
    out = dict(checks)
    for key in ("ports", "leader_ports"):
        if key in out:
            out[key] = [site_port(p, site) for p in out[key]]
    return out


def definition_nets(spec, vm, site):
    """The NICs a service's VMs are defined with: the networks of its first VM in NIC order (each once; the IGW only
    cloud-services and cloud-core, its target), led by the management network (cloud-vyos) for a VyOS service."""
    from .nfconfig import MGMT_NETWORK
    names = []
    for net, _ip in vm.get("nics") or []:
        n = NET_ALIASES.get(short(net, site), short(net, site))
        if n not in names:
            names.append(n)
    if spec.get("nets"):
        names = [n for n in spec["nets"]]
    if spec.get("port") == VYOS_PORT and MGMT_NETWORK not in names:
        names = [MGMT_NETWORK] + names
    nets = [site_network(site, n) for n in names]
    return [n for n in nets if n is not None]


def definition_ips(nets, vm, site):
    """A VM's address on each defined NIC, from what the platform says it has (empty: none yet, assigned when made)."""
    have = {}
    for net, ip in vm.get("nics") or []:
        n = NET_ALIASES.get(short(net, site), short(net, site))
        if ip and n not in have:
            have[n] = ip
    ips = [have.get(n.name, "") for n in nets]
    while ips and not ips[-1]:
        ips.pop()
    return ips


def seed_type(spec):
    """The type a service of the standard layout is (None: not a core service any more)."""
    if (spec.get("template") or "").startswith("rr-"):
        return CoreService.Type.BGP_RR
    if spec["name"].endswith("-backup"):
        return CoreService.Type.BACKUP
    return {"ns": CoreService.Type.NS, "graylog": CoreService.Type.LOGS, "admin": CoreService.Type.ADMIN}.get(spec["name"])


def seed(site, user=None):
    """Make the services of CATALOGUE that the site has (by its VMs and its management cluster) and do not exist yet.
    Returns the names made."""
    from accounts.models import audit
    from .models import Site
    st = Site.objects.get(code=site)
    vms = site_vms(site)
    secret = _default_secret(site)
    made = []
    for spec in CATALOGUE:
        stype = seed_type(spec)
        if stype is None:  # correction 06: only these are core services; other VMs are ignored
            continue
        if CoreService.objects.filter(site=st, name=spec["name"]).exists():
            continue
        nodes = []
        platform = None
        if spec.get("hosts") == "mgmt":
            cl = Cluster.objects.filter(platform__site=st, kind="mgmt").exclude(state="retired").first()
            hosts = sorted((h for h in cl.hosts.all() if h.state != "retired"), key=lambda h: h.name) if cl else []
            nodes = [dict(name=h.name, address=h.ip or h.name, ssh_port=22, host=h) for h in hosts]
            platform = cl.platform if cl else None
        else:
            pat = re.compile(rf"^{spec['match']}$")
            found = sorted(((k, vm) for name, (k, vm) in vms.items() if pat.match(short(name, site))),
                           key=lambda kv: kv[1]["name"])
            if found:
                platform = Platform.objects.filter(key=found[0][0]).first()
            for _k, vm in found:
                nodes.append(dict(name=vm["name"], address=login_address(vm, site),
                                  ssh_port=site_port(spec.get("port", 22), site)))
        if not nodes:
            continue
        desc = spec["title"] + ("\n" + spec["description"] if spec.get("description") else "")
        svc = CoreService(site=st, name=spec["name"], kind=spec.get("kind", "vm"), description=desc,
                          platform=platform if spec.get("kind", "vm") == "vm" else None,
                          checks=site_checks(spec.get("checks", {}), site), ssh_port=site_port(spec.get("port", 22), site), ssh_secret=secret or "", updated_by=user,
                          serves=Platform.objects.filter(site=st, kind=spec["serves"]).first() if spec.get("serves") else None)
        if spec.get("template"):
            svc.template = ConfigTemplate.objects.filter(role=spec["template"]).first()
        if svc.kind == "vm":
            first = vms[nodes[0]["name"]][1]
            svc.vcpu, svc.ram_gb = first["vcpu"] or None, math.ceil(first["ram_gb"]) if first["ram_gb"] else None
            svc.image = image_of(platform, first, vyos=spec.get("port") == VYOS_PORT)
            svc.disk_gb = (first["disks_gb"] or [None])[0]
            nets = definition_nets(spec, first, site)
            if nets:
                svc.nic1_network = nets[0]
                svc.more_nics = [{"network": n.pk, "gateway": ""} for n in nets[1:]]
            for n in nodes:
                n["ips"] = definition_ips(nets, vms[n["name"]][1], site)
        if svc.template is not None:  # route reflectors, gateways: the BGP secret of their network
            svc.service_secret = svc.bgp_secret()
        svc.stype = stype
        params = {"vcpu": svc.vcpu, "ram_gb": svc.ram_gb, "disk_gb": svc.disk_gb}
        if stype == CoreService.Type.BGP_RR:
            net = svc.service_network()
            params["network"] = net.pk if net is not None else None
        if stype == CoreService.Type.BACKUP and svc.serves is not None:
            ds = list(svc.serves.datastores.filter(kind="backups"))
            params["datastore"] = ds[0].pk if len(ds) == 1 else None
        svc.params = params
        svc.save()
        for n in nodes:
            CoreServiceNode.objects.create(service=svc, secret=secret, **n)
        made.append(svc.name)
        audit(user, "core_service_saved", svc, site=site, service=svc.name, nodes=len(nodes), source="standard layout")
    return made


# --- reading --------------------------------------------------------------------------------------------------------

def parse(out):
    parts, cur = {}, None
    for line in (out or "").splitlines():
        if line.startswith("##"):
            cur = line[2:].strip()
            parts[cur] = []
        elif cur:
            parts[cur].append(line.strip())
    first = lambda k: (parts.get(k) or [""])[0]  # noqa: E731
    facts = {"hostname": first("host"), "os": first("os")}
    try:
        facts["vcpu"] = int(first("cpu"))
    except ValueError:
        pass
    try:
        facts["ram_gb"] = round(int(first("mem")) / 1024)
    except ValueError:
        pass
    m = re.match(r"(\d+)G", first("disk"))
    if m:
        facts["disk_gb"] = int(m.group(1))
    facts["addresses"] = [x for x in parts.get("addr", []) if x]
    return facts


def read_node(node, site):
    """Log in to a node over ssh (read only) and keep what it reports."""
    from .credentials import account
    from .sshexec import SshError, run
    vm = site_vms(site).get(node.name)
    facts = {"vm": vm[1]} if vm else {}
    user, acc = account(node.secret)
    try:
        if not user:
            raise SshError(f"no login: the secret {node.secret or '—'} has no user with a key or password")
        checks = checks_of(node.service, node)
        script = READ_SCRIPT + ("; " + check_script(checks) if check_script(checks) else "")
        out = run(node.address, node.ssh_port, user, acc.get("key"), script, site=site, timeout=40,
                  password=acc.get("password"))
        facts.update(parse(out))
        facts["checks"] = evaluate(checks, out, facts, node.address)
        node.facts_error = ""
    except SshError as exc:
        node.facts_error = str(exc)[:300]
    if "redeploy" in (node.facts or {}):  # what a gateway's old VM gave up: kept until finished or rolled back
        facts["redeploy"] = node.facts["redeploy"]
    node.facts, node.facts_at = facts, timezone.now()
    node.save(update_fields=["facts", "facts_at", "facts_error"])
    return not node.facts_error


def read(task, site, services=None):
    """Read the platforms of the site (their own VMs) and then every node of the services (all when None)."""
    vyos = set(CoreServiceNode.objects.filter(service__site__code=site, service__template__role__regex=r"^(rr-|egw$|igw$)")
               .values_list("name", flat=True))
    for c in PlatformControl.objects.filter(key__startswith=f"{site}."):
        try:
            n = len(read_platform_vms(c))
            task.step(f"{c.key}: VMs", "ok", f"{n} VMs of group oneadmin")
            if vyos and any(r["name"].removesuffix("-old") in vyos for r in c.vms or []):
                k = read_init_flags(c, vyos)
                task.step(f"{c.key}: init scripts", "ok" if not k else "failed",
                          f"{k} VyOS VM(s) still keep the one-time init script in their context" if k else
                          "no VyOS VM keeps the one-time init script in its context")
        except Exception as exc:  # noqa: BLE001 - one platform's error does not stop the rest
            task.step(f"{c.key}: VMs", "failed", str(exc)[:300])
    qs = CoreService.objects.filter(site__code=site).prefetch_related("nodes")
    if services:
        qs = qs.filter(pk__in=[s.pk for s in services])
    ok = bad = 0
    for svc in qs:
        res = [read_node(n, site) for n in svc.nodes.all()]
        ok, bad = ok + sum(res), bad + len(res) - sum(res)
        errors = [n.facts_error for n in svc.nodes.all() if n.facts_error]
        failed = [f"{n.name}: {c['check']}" + (f" ({c['detail']})" if c.get("detail") else "")
                  for n in svc.nodes.all() for c in (n.facts or {}).get("checks", []) if not c["ok"]]
        lead = leaders(svc)
        if lead is not None and len(lead) != 1:
            failed.insert(0, f"{len(lead)} nodes hold the floating address" + (f" ({', '.join(lead)})" if lead else ""))
        task.step(svc.name, "ok" if not errors and not failed else "failed",
                  f"{sum(res)}/{len(res)} read" + (f"; {errors[0]}" if errors else "") + (f"; failed: {'; '.join(failed[:4])}" if failed else ""))
        svc.read_at = timezone.now()
        svc.save(update_fields=["read_at"])
    return {"read": ok, "failed": bad}


def leaders(svc):
    """Names of the nodes that hold the service's floating address (from their last read); None when the service has
    none or no node was read."""
    checks = checks_of(svc)
    if not checks.get("vip"):
        return None
    nodes = [n for n in svc.nodes.all() if (n.facts or {}).get("addresses")]
    if not nodes:
        return None
    return [n.name for n in nodes if is_leader(checks, n.address, n.facts)]


def mismatches(svc):
    """What a node reports that differs from the service's size: [(node, text)]."""
    out = []
    for n in svc.nodes.all():
        vm = (n.facts or {}).get("vm") or {}
        if svc.kind != "vm" or not vm:
            continue
        if svc.vcpu and vm.get("vcpu") and vm["vcpu"] != svc.vcpu:
            out.append((n.name, f"vCPU {vm['vcpu']} (service: {svc.vcpu})"))
        if svc.ram_gb and vm.get("ram_gb") and float(vm["ram_gb"]) != float(svc.ram_gb):
            out.append((n.name, f"RAM {vm['ram_gb']} GB (service: {svc.ram_gb})"))
        if svc.disk_gb and vm.get("disks_gb") and vm["disks_gb"][0] != svc.disk_gb:
            out.append((n.name, f"disk {vm['disks_gb'][0]} GB (service: {svc.disk_gb})"))
    return out
