"""Core service types (correction 06): each type has its own edit form asking only what cannot be calculated; the rest
of the definition (placement, VM and host names, image, template, NICs and addresses, secrets, instances) follows from
the site's settings, networks, storage and platforms.

    bgp-rr     route reflectors of one BGP network (ix or evpn), one service per network
    cloud-egw  internet edge of the core services, one per site
    cloud-igw  gateway of the core services, one per site (before cloud-gw)
    cloud-gw   the core services' egw and igw, two VMs, one service per site (correction 11; replaces cloud-egw/-igw)
    ns         name servers, one per site
    logs       log collector, one per site
    admin      admin jump hosts, one per site
    backup     backup storage (restic) of one backup datastore, one per datastore
    core-vm    anything else: the generic form (inventory/core_views.ServiceForm)

Existing VMs keep their names and host names; the calculated ones (target_*) are what a redeploy will give them.
"""
import ipaddress

from django import forms
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l

from .models import (Cluster, ConfigTemplate, CoreService, CoreServiceNode, Network, Platform, PlatformControl,
                     PlatformDatastore, ServiceImage, StorageVolume)

T = CoreService.Type
SINGLE = (T.CLOUD_EGW, T.CLOUD_IGW, T.CLOUD_GW, T.PORTAL_GW, T.NS, T.LOGS, T.ADMIN)  # one per site
VYOS_TYPES = (T.BGP_RR, T.CLOUD_EGW, T.CLOUD_IGW, T.CLOUD_GW, T.PORTAL_GW)
SIZES = {T.BGP_RR: (2, 4, None), T.CLOUD_EGW: (2, 4, None), T.CLOUD_IGW: (4, 4, None), T.CLOUD_GW: (2, 4, None),
         T.PORTAL_GW: (2, 4, None), T.NS: (1, 1, 5),
         T.LOGS: (6, 48, 20), T.ADMIN: (2, 8, 30), T.BACKUP: (2, 8, 10)}
TEMPLATES = {T.CLOUD_EGW: "egw", T.CLOUD_IGW: "igw", T.NS: "core-services-ns", T.LOGS: "core-services-graylog",
             T.ADMIN: "core-services-admin", T.BACKUP: "core-services-backup"}
CHECKS = {T.CLOUD_EGW: {"bgp": "ipv4"}, T.NS: {"units": ["named"], "ports": [53]},
          T.LOGS: {"units": ["graylog-server", "graylog-datanode", "mongod"], "ports": [9000]},
          T.ADMIN: {"units": ["xrdp"]}}
TYPED = (T.BGP_RR, T.CLOUD_GW, T.PORTAL_GW, T.NS, T.LOGS, T.ADMIN, T.BACKUP)  # cloud-egw / cloud-igw: kept, not made any more
GW_RANGE = "cloud-gw"  # the range of the egw's address on the exchange network (correction 11)
PORTAL_IX = "portal-ix"  # the exchange network of the portal platforms (correction 12)
PORTAL_KIND = "ext-managed"  # the platform kind of a portal platform


def portal_publics(platform, ix):
    """[{name, cidr, vip, ips: [egw1, egw2], prefix}] — one public overlay per delegated subnet of the portal platform on
    its exchange (correction 12): VIP the first address (the VMs' gateway), egw1 and egw2 the next two."""
    out = []
    subs = sorted((d for d in platform.delegated_subnets("inet") if d.segment == ix.name),
                  key=lambda d: ipaddress.ip_network(d.cidr))
    for i, ds in enumerate(subs, 1):
        net = ipaddress.ip_network(ds.cidr)
        hs = list(net.hosts())
        out.append({"name": f"portal-public{i:02d}", "cidr": str(net), "prefix": net.prefixlen, "vip": str(hs[0]),
                    "ips": [str(hs[1]), str(hs[2])]})
    return out


class DefinitionError(Exception):
    """What the definition cannot do without (a network, a datastore …)."""


# --- what the site has ----------------------------------------------------------------------------------------------

def net_of(site, kind):
    return Network.objects.filter(site=site, kind=kind).exclude(state=Network.State.RETIRED).order_by("name").first()


def mgmt_of(site):
    """(the management platform, its cluster) of a site: where the core services' VMs run."""
    p = Platform.objects.filter(site__code=site, kind="mgmt").first()
    cl = Cluster.objects.filter(platform=p).exclude(state="retired").first() if p else None
    return p, cl


def image_of(platform, typ):
    """The platform's image of a type (vyos, base-os)."""
    c = PlatformControl.objects.filter(key=platform.key).first() if platform else None
    return ServiceImage.objects.filter(control=c, purpose=typ, kind=ServiceImage.Kind.IMAGE).order_by("position", "pk").first() \
        if c else None


def data_net(site, volume):
    """The storage network a volume is reached on: its storage's type (nfs, iscsi)."""
    if volume is None:
        return None
    kind = {"nfs": Network.Kind.NFS, "iscsi": Network.Kind.ISCSI}.get(volume.storage.kind)
    return net_of(site, kind) if kind else None


def bgp_secret_of(net):
    refs = {(g.password or "").split("#")[0] for g in net.peer_groups.all() if g.password} if net is not None else set()
    return refs.pop() if len(refs) == 1 else ""


def rr_ips(net):
    """The route reflectors' addresses of a BGP network: its list, else its rrs range without the first address."""
    if net.rrs:
        return [str(x) for x in net.rrs]
    r = net.ranges.filter(name="rrs").first()
    return [str(r.first + i) for i in range(1, r.length)] if r else []


def ix_for(site, cs):
    """The exchange whose routes or delegated subnets carry the core-services subnet (the egw's uplink)."""
    if cs is None:
        return None
    want = cs.net
    for n in Network.objects.filter(site=site, kind=Network.Kind.IX).order_by("name"):
        for r in (n.extra or {}).get("routes") or []:
            try:
                if isinstance(r, dict) and want.subnet_of(ipaddress.ip_network(r.get("subnet", ""), strict=False)):
                    return n
            except ValueError:
                continue
        for a in n.announced or []:
            try:
                if want.subnet_of(ipaddress.ip_network(a, strict=False)):
                    return n
            except ValueError:
                continue
    return None


def hosts_of(net):
    return list(net.net.hosts()) if net is not None else []


def host_name(vm):
    """A host name from a VM name: a host name has no underscore."""
    return vm.replace("_", "-")


def vyos_node(name, ips):
    """A VyOS VM's node: its host name is exactly its VM name (correction 09), so the VM name has no underscore
    (VyOS host names: letters, digits, "-" and ".")."""
    return {"name": name, "hostname": name, "ips": ips}


def target_of(node):
    """(VM name, host name) the node gets when it is redeployed: from its service's definition (saved values);
    the current ones when the definition cannot be made."""
    svc = node.service
    try:
        d = definition(svc.site.code, svc.stype, svc.params or {})
        nodes = d["nodes"]
        i = [n.pk for n in svc.nodes.order_by("pk")].index(node.pk)
        if i < len(nodes):
            return nodes[i]["name"], nodes[i]["hostname"]
    except Exception:  # noqa: BLE001 - a definition that cannot be made: the current name, made a host name
        pass
    if svc.stype in VYOS_TYPES or (svc.template and svc.template.role.startswith("rr-")):
        n = host_name(node.name)
        return n, n
    return node.name, node.hostname or host_name(node.name)


def in_mgmt_hosts(site, ip):
    from .nfconfig import management
    prefixes = management(site)[0]
    a = ipaddress.ip_address(ip)
    return any(a in ipaddress.ip_network(p, strict=False) for p in prefixes)


# --- the definition of a type ---------------------------------------------------------------------------------------

def definition(site, stype, p, existing=None):
    """What a service of this type is, from what its form asks (p) and the site: a dict with name, platform, serves,
    image, template, ssh_port, ssh_secret, service_secret, secrets [(label, ref)], vcpu, ram_gb, disk_gb, nics
    [(Network, gateway)], nodes [{name, hostname, ips}], aliases (cloud-igw), checks, notes [text].
    Raises DefinitionError when something it needs is missing."""
    from .nfconfig import management
    ports = management(site)[1]
    platform, cluster = mgmt_of(site)
    if platform is None:
        raise DefinitionError(_("%(s)s has no management platform.") % {"s": site})
    vcpu, ram, disk = SIZES[stype]
    from .core_services import site_checks
    d = {"stype": stype, "platform": platform, "cluster": cluster, "serves": None, "kind": "vm", "notes": [],
         "vcpu": p.get("vcpu") or vcpu, "ram_gb": p.get("ram_gb") or ram,
         "disk_gb": p.get("disk_gb") or disk or (existing.disk_gb if existing else None) or 2,
         "ssh_secret": platform.sa or "", "service_secret": "", "secrets": [], "aliases": [],
         "checks": site_checks(CHECKS.get(stype, {}), site)}
    vyos = stype in VYOS_TYPES
    d["image"] = image_of(platform, "vyos" if vyos else "base-os")
    if d["image"] is None:
        d["notes"].append(_("%(p)s has no %(t)s image (site → Platforms → service images).") % {
            "p": platform.key, "t": "vyos" if vyos else "base-os"})
    d["ssh_port"] = ports["vyos"] if vyos else (ports["ssh"] if stype == T.ADMIN else 22)
    d["secrets"].append((_("ssh (super admin of %(p)s)") % {"p": platform.key}, platform.sa or ""))
    if not platform.sa:
        d["notes"].append(_("%(p)s has no super admin secret.") % {"p": platform.key})
    cs, vy = net_of(site, Network.Kind.CORE_SERVICES), net_of(site, Network.Kind.CORE_VYOS)

    def need(net, what):
        if net is None:
            raise DefinitionError(_("%(s)s has no %(w)s network (site → Networks).") % {"s": site, "w": what})
        return net
    if stype == T.BGP_RR:
        net = Network.objects.filter(pk=p.get("network") or 0, site=site, kind__in=[Network.Kind.IX, Network.Kind.EVPN]).first()
        if net is None:
            raise DefinitionError(_("Choose the BGP network."))
        need(vy, "core-vyos")
        ips = rr_ips(net)
        if not ips:
            raise DefinitionError(_("%(n)s has no route reflector addresses (its rrs range).") % {"n": net.name})
        role = "rr-vxlan" if net.kind == Network.Kind.EVPN else ("rr-corpnet" if net.name.startswith("corpnet") else "rr-inet")
        d.update(name=f"{net.name}-rrs", template=role, nics=[(vy, ""), (net, "")], network=net,
                 checks={"bgp": "evpn" if net.kind == Network.Kind.EVPN else "ipv4"},
                 nodes=[vyos_node(f"{net.name}-rr{i + 1}-{site}", ["", ip]) for i, ip in enumerate(ips)])
        d["service_secret"] = bgp_secret_of(net)
        d["secrets"].append((_("BGP (network %(n)s)") % {"n": net.name}, d["service_secret"]))
        if not d["service_secret"]:
            d["notes"].append(_("The peer groups of %(n)s have no BGP secret (site → Networks → BGP).") % {"n": net.name})
    elif stype == T.CLOUD_EGW:
        need(vy, "core-vyos")
        need(cs, "core-services")
        ix = ix_for(site, cs)
        if ix is None:
            raise DefinitionError(_("No exchange routes the core-services subnet %(c)s (site → Networks → routes or "
                                    "delegated subnets of an ix network).") % {"c": cs.cidr})
        first = hosts_of(cs)[0]
        d.update(name="cloud-egw", template=TEMPLATES[stype], nics=[(vy, ""), (ix, ""), (cs, "")], network=ix,
                 nodes=[vyos_node(f"{cs.name.split('-')[0]}-egw-{site}", ["", "", str(first)])])
        d["service_secret"] = bgp_secret_of(ix)
        d["secrets"].append((_("BGP (network %(n)s)") % {"n": ix.name}, d["service_secret"]))
    elif stype == T.CLOUD_IGW:
        need(vy, "core-vyos")
        need(cs, "core-services")
        cc = need(net_of(site, Network.Kind.CORE_CLOUD), "core-cloud")
        hs = hosts_of(cs)
        if len(hs) < 2:
            raise DefinitionError(_("%(c)s is too small for the igw (its second address).") % {"c": cs.cidr})
        d.update(name="cloud-igw", template=TEMPLATES[stype], nics=[(vy, ""), (cs, ""), (cc, "")],
                 aliases=[str(x) for x in hs[2:]],
                 nodes=[vyos_node(f"{cs.name.split('-')[0]}-igw-{site}", ["", str(hs[1]), str(hosts_of(cc)[0])])])
    elif stype == T.CLOUD_GW:
        # correction 11: one service, two VMs. egw: eth0 management, eth1 the exchange network (its cloud-gw range),
        # eth2 core-services (first address). igw: eth0 management, eth1 core-services (second address, aliases: the
        # further public addresses the platform gives it), eth2 core-cloud (its last address, e.g. .254 of a /24: the core VMs' way out; the network's
        # gateway .1 is the core router).
        need(vy, "core-vyos")
        need(cs, "core-services")
        cc = need(net_of(site, Network.Kind.CORE_CLOUD), "core-cloud")
        ix = next((n for n in Network.objects.filter(site=site, kind=Network.Kind.IX).order_by("name")
                   if n.ranges.filter(name=GW_RANGE).exists()), None) or ix_for(site, cs)
        if ix is None:
            raise DefinitionError(_("No exchange network has the range %(r)s (site → Networks → ranges).") % {"r": GW_RANGE})
        r = ix.ranges.filter(name=GW_RANGE).first()
        if r is None:
            raise DefinitionError(_("%(n)s has no range %(r)s for the egw's address.") % {"n": ix.name, "r": GW_RANGE})
        hs, hc = hosts_of(cs), hosts_of(cc)
        if len(hs) < 2 or not hc:
            raise DefinitionError(_("%(c)s is too small for the egw and the igw.") % {"c": cs.cidr})
        aliases = [str(a).strip() for a in (p.get("aliases") or []) if str(a).strip()]
        for a in aliases:
            if ipaddress.ip_address(a) not in cs.net:
                d["notes"].append(_("%(ip)s (alias) is not in core-services %(c)s.") % {"ip": a, "c": cs.cidr})
        gw = "cloud"
        d.update(name="cloud-gw", template=TEMPLATES[T.CLOUD_IGW], nics=[(vy, ""), (ix, ""), (cs, "")], network=ix,
                 aliases=aliases, checks={},
                 nodes=[dict(vyos_node(f"{gw}-egw-{site}", ["", str(r.first), str(hs[0])]),
                             params={"role": "egw", "nics": [vy.pk, ix.pk, cs.pk], "checks": {"bgp": "ipv4"}}),
                        dict(vyos_node(f"{gw}-igw-{site}", ["", str(hs[1]), str(hc[-1])]),
                             params={"role": "igw", "nics": [vy.pk, cs.pk, cc.pk], "aliases": aliases, "checks": {}})])
        d["service_secret"] = bgp_secret_of(ix)
        d["secrets"].append((_("BGP (network %(n)s)") % {"n": ix.name}, d["service_secret"]))
        if not d["service_secret"]:
            d["notes"].append(_("The peer groups of %(n)s have no BGP secret (site → Networks → BGP).") % {"n": ix.name})
    elif stype == T.PORTAL_GW:
        # correction 12: the egws of the site's portal platform (ext-managed): two VMs; eth0 management, eth1 portal-ix
        # (a free address), eth2 … the public overlays (one per delegated subnet: VIP the first address over VRRP, egw1
        # the second, egw2 the third)
        plat = Platform.objects.filter(site__code=site, kind=PORTAL_KIND).order_by("key").first()
        if plat is None:
            raise DefinitionError(_("%(s)s has no portal platform (type %(k)s).") % {"s": site, "k": PORTAL_KIND})
        d["platform"] = plat
        d["cluster"] = Cluster.objects.filter(platform=plat).exclude(state="retired").first()
        d["image"] = image_of(plat, "vyos")
        if d["image"] is None:
            d["notes"].append(_("%(p)s has no vyos image (site → Platforms → service images).") % {"p": plat.key})
        d["ssh_secret"] = plat.sa or ""
        d["secrets"] = [(_("ssh (super admin of %(p)s)") % {"p": plat.key}, plat.sa or "")]
        need(vy, "core-vyos")
        ix = Network.objects.filter(site=site, name=PORTAL_IX).first()
        if ix is None:
            raise DefinitionError(_("%(s)s has no network %(n)s.") % {"s": site, "n": PORTAL_IX})
        publics = portal_publics(plat, ix)
        if not publics:
            raise DefinitionError(_("%(p)s has no delegated subnet on %(n)s.") % {"p": plat.key, "n": ix.name})
        nodes = []
        for k in (1, 2):
            nodes.append(dict(vyos_node(f"portal-egw{k}-{site}", ["", ""] + [pb["ips"][k - 1] for pb in publics]),
                              params={"role": "egw", "nics": [vy.pk, ix.pk] + [f"public:{pb['name']}" for pb in publics],
                                      "checks": {"bgp": "ipv4"}, "dynamic": [1], "vrrp_priority": 200 if k == 1 else 100,
                                      "publics": publics}))
        d.update(name="portal-gw", template="egw", nics=[(vy, ""), (ix, "")], network=ix, aliases=[], checks={},
                 nodes=nodes, publics=publics)
        d["service_secret"] = bgp_secret_of(ix)
        d["secrets"].append((_("BGP (network %(n)s)") % {"n": ix.name}, d["service_secret"]))
        if not d["service_secret"]:
            d["notes"].append(_("The peer groups of %(n)s have no BGP secret (site → Networks → BGP).") % {"n": ix.name})
    elif stype == T.NS:
        need(cs, "core-services")
        from .models import Site
        dns = list(Site.objects.get(code=site).dns_servers or [])
        if not dns:
            raise DefinitionError(_("%(s)s has no DNS servers (site settings).") % {"s": site})
        for ip in dns:
            if ipaddress.ip_address(ip) not in cs.net:
                d["notes"].append(_("%(ip)s (site DNS servers) is not in core-services %(c)s.") % {"ip": ip, "c": cs.cidr})
        d.update(name="ns", template=TEMPLATES[stype], nics=[(cs, "")],
                 nodes=[{"name": f"core-ns{i + 1}_{site}", "hostname": f"s{site}ns{i + 1}", "ips": [ip]} for i, ip in enumerate(dns)])
    elif stype == T.LOGS:
        need(cs, "core-services")
        vol = StorageVolume.objects.filter(pk=p.get("volume") or 0, storage__site__code=site).select_related("storage").first()
        if vol is None:
            raise DefinitionError(_("Choose the volume the logs are kept on."))
        dn = need(data_net(site, vol), vol.storage.kind)
        from .models import Site
        logs = Site.objects.get(code=site).log_collectors or {}
        host = (logs.get("core") or {}).get("host") or next((c.get("host") for c in logs.values() if c.get("host")), "")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            from .nfconfig import syslog_target
            host = ((syslog_target(Site.objects.get(code=site)) or {}).get("host") or "")
            d["notes"].append(_("The log collector is a name in the site settings; its address is resolved: %(h)s.") % {"h": host})
        if host and not str(host).startswith("«") and ipaddress.ip_address(host) not in cs.net:
            d["notes"].append(_("%(ip)s (site log collector) is not in core-services %(c)s.") % {"ip": host, "c": cs.cidr})
        d.update(name="logs", template=TEMPLATES[stype], nics=[(cs, ""), (dn, "")], volume=vol,
                 nodes=[{"name": f"core-logs1_{site}", "hostname": f"s{site}logs", "ips": [str(host), ""]}])
    elif stype == T.ADMIN:
        need(cs, "core-services")
        vol = StorageVolume.objects.filter(pk=p.get("data_volume") or 0, storage__site__code=site).select_related("storage").first()
        if vol is None:
            raise DefinitionError(_("Choose the volume of the management data."))
        dn = need(data_net(site, vol), vol.storage.kind)
        n = int(p.get("instances") or 3)
        try:
            first = ipaddress.ip_address(p.get("first_ip") or "")
        except ValueError:
            raise DefinitionError(_("Give the first instance's address."))
        ips = [str(first + i) for i in range(n)]
        for ip in ips:
            if ipaddress.ip_address(ip) not in cs.net:
                d["notes"].append(_("%(ip)s is not in core-services %(c)s.") % {"ip": ip, "c": cs.cidr})
            if not in_mgmt_hosts(site, ip):
                d["notes"].append(_("%(ip)s is not among the site's management hosts (site settings).") % {"ip": ip})
        home = StorageVolume.objects.filter(pk=p.get("home_volume") or 0, storage__site__code=site).first()
        d.update(name="admin", template=TEMPLATES[stype], nics=[(cs, ""), (dn, "")], volume=vol, home_volume=home,
                 home_subdir=p.get("home_subdir") or "",
                 nodes=[{"name": f"core-admin{i + 1}_{site}", "hostname": f"s{site}admin{i + 1}", "ips": [ip, ""]}
                        for i, ip in enumerate(ips)])
    elif stype == T.BACKUP:
        ds = PlatformDatastore.objects.filter(pk=p.get("datastore") or 0, platform__site__code=site,
                                              kind=PlatformDatastore.Kind.BACKUPS).select_related("platform", "volume__storage").first()
        if ds is None:
            raise DefinitionError(_("Choose the backup datastore."))
        need(cs, "core-services")
        if not ds.restic_ip:
            raise DefinitionError(_("%(d)s has no restic VM address (platform → datastores).") % {"d": ds.name})
        nics = [(cs, "")]
        if ds.backend != PlatformDatastore.Backend.S3:
            nics.append((need(data_net(site, ds.volume), "storage"), ""))
        pl = ds.platform.name
        d.update(name=f"{pl}-backup", template=TEMPLATES[stype], nics=nics, datastore=ds, serves=ds.platform,
                 nodes=[{"name": f"core-{pl}-backup1_{site}", "hostname": f"s{site}{pl}backup",
                         "ips": [str(ds.restic_ip)] + ([""] if len(nics) > 1 else [])}])
        d["service_secret"] = ds.restic_secret
        d["secrets"].append((_("restic (datastore %(d)s)") % {"d": ds.name}, ds.restic_secret))
        if ds.backend == PlatformDatastore.Backend.S3:
            d["secrets"].append((_("S3 (datastore %(d)s)") % {"d": ds.name}, ds.s3_secret))
        if not ds.restic_secret:
            d["notes"].append(_("%(d)s has no restic password (platform → datastores).") % {"d": ds.name})
    for nd in d["nodes"]:
        nd.setdefault("hostname", host_name(nd["name"]))
    if existing is not None:  # running VMs keep their names until they are redeployed
        for nd, old in zip(d["nodes"], existing.nodes.order_by("pk")):
            nd["target_name"], nd["target_hostname"] = nd["name"], nd["hostname"]
            nd["name"], nd["hostname"] = old.name, old.hostname or nd["hostname"]
    return d


def apply(svc, d, user=None):
    """Write a definition into the service and its nodes (records only)."""
    svc.stype, svc.kind = d["stype"], "vm"
    svc.platform, svc.serves, svc.image = d["platform"], d["serves"], d["image"]
    svc.template = ConfigTemplate.objects.filter(role=d["template"]).first()
    svc.vcpu, svc.ram_gb, svc.disk_gb = d["vcpu"], d["ram_gb"], d["disk_gb"]
    svc.ssh_port, svc.ssh_secret, svc.service_secret = d["ssh_port"], d["ssh_secret"], d["service_secret"]
    nics = d["nics"]
    svc.nic1_network, svc.nic1_gateway = nics[0][0], (nics[0][1] or None)
    svc.more_nics = [{"network": n.pk, "gateway": gw} for n, gw in nics[1:]]
    if not svc.checks:
        svc.checks = d["checks"]
    svc.updated_by = user
    svc.save()
    publics = {}
    for pb in d.get("publics") or []:  # portal-gw: the public overlays as site networks (kind public)
        net = ipaddress.ip_network(pb["cidr"])
        publics[pb["name"]], _new = Network.objects.get_or_create(
            site=svc.site.code, name=pb["name"],
            defaults={"kind": Network.Kind.PUBLIC, "interface": "vxlan", "vlan": 0, "subnet": str(net.network_address),
                      "prefix": net.prefixlen, "gateway": pb["vip"]})
    olds = list(svc.nodes.order_by("pk"))
    for i, nd in enumerate(d["nodes"]):
        n = olds[i] if i < len(olds) else CoreServiceNode(service=svc, name=nd["name"], hostname=nd["hostname"])
        n.ips = nd["ips"]
        n.params = dict(nd.get("params") or {})
        if n.params.get("nics"):  # "public:<name>" → the site network's id
            n.params["nics"] = [publics[x.split(":", 1)[1]].pk if isinstance(x, str) and x.startswith("public:") else x
                                for x in n.params["nics"]]
        n.secret, n.ssh_port = svc.ssh_secret, svc.ssh_port if n.pk is None else n.ssh_port
        if n.pk is None:
            n.address = next((ip for ip in nd["ips"] if ip), nd["name"])
        n.save()
    for n in olds[len(d["nodes"]):]:  # beyond the definition (fewer instances)
        n.delete()
    return svc


# --- the forms ------------------------------------------------------------------------------------------------------

class TypeForm(forms.Form):
    """Only what the type's definition lists before "calculated automatically" (correction 07)."""
    stype = None
    vcpu = forms.IntegerField(label=_l("vCPU"), min_value=1, max_value=256)
    ram_gb = forms.IntegerField(label=_l("RAM, GB"), min_value=1, max_value=4096)
    SIZE_FIELDS = ("vcpu", "ram_gb")

    def __init__(self, *args, site=None, svc=None, **kwargs):
        self.site, self.svc = site, svc
        super().__init__(*args, **kwargs)
        v, r, dk = SIZES[self.stype]
        self.fields["vcpu"].initial, self.fields["ram_gb"].initial = v, r
        if "disk_gb" in self.fields:
            self.fields["disk_gb"].initial = dk

    def params(self):
        d = self.cleaned_data
        return {k: d.get(k) for k in self.fields if k != "config_diff"}

    def size_fields(self):
        return [self[f] for f in self.SIZE_FIELDS if f in self.fields]


def _disk():
    return forms.IntegerField(label=_l("Disk, GB"), min_value=1, max_value=100000)


def _volumes(site):
    return [("", "—")] + [(str(v.pk), f"{v.storage.name} / {v.name} ({v.storage.get_kind_display()})")
                          for v in StorageVolume.objects.filter(storage__site__code=site).select_related("storage")
                          .order_by("storage__name", "name")]


class BgpRrForm(TypeForm):
    stype = T.BGP_RR
    network = forms.ChoiceField(label=_l("BGP network"), help_text=_l("An ix or evpn network of the site; one bgp-rr service per network."))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        taken = {str((s.params or {}).get("network")) for s in CoreService.objects.filter(site__code=self.site, stype=T.BGP_RR)
                 .exclude(pk=self.svc.pk if self.svc else None)}
        self.fields["network"].choices = [("", "—")] + [
            (str(n.pk), f"{n.name} ({n.kind}) · {n.cidr}") for n in Network.objects.filter(
                site=self.site, kind__in=[Network.Kind.IX, Network.Kind.EVPN]).order_by("name") if str(n.pk) not in taken]

    def params(self):
        p = super().params()
        p["network"] = int(p["network"]) if p.get("network") else None
        return p


class EgwForm(TypeForm):
    stype = T.CLOUD_EGW


class IgwForm(TypeForm):
    stype = T.CLOUD_IGW
    config_diff = forms.CharField(label=_l("VyOS configuration added"), required=False,
                                  widget=forms.Textarea(attrs={"rows": 5, "class": "mono"}),
                                  help_text=_l("set/delete lines added to the standard igw configuration."))


class PortalGwForm(TypeForm):
    """portal-gw: the size of both egws; the rest is calculated from the portal platform and its delegated subnets."""
    stype = T.PORTAL_GW


class GwForm(TypeForm):
    """cloud-gw: the size (both VMs), the igw's further addresses on core-services and the custom part of its
    configuration (e.g. destination NAT)."""
    stype = T.CLOUD_GW
    aliases = forms.CharField(label=_l("igw: further addresses on core-services"), required=False,
                              widget=forms.TextInput(attrs={"class": "mono"}),
                              help_text=_l("Given to the igw by the platform as aliases of its core-services NIC, space "
                                           "separated, e.g. 198.18.93.3 198.18.93.4."))
    config_diff = forms.CharField(label=_l("Config addon (cloud-igw)"), required=False,
                                  widget=forms.Textarea(attrs={"rows": 8, "class": "mono"}),
                                  help_text=_l("set/delete lines added to the standard cloud-igw template (Infrastructure → "
                                               "Templates → cloud-igw): destination NAT, more addresses, routes … No "
                                               "secrets: they come from Secrets."))

    def clean_aliases(self):
        out = []
        for a in (self.cleaned_data.get("aliases") or "").replace(",", " ").split():
            try:
                out.append(str(ipaddress.ip_address(a)))
            except ValueError:
                raise forms.ValidationError(_("Not an IPv4 address: %(a)s") % {"a": a})
        return out

    def clean_config_diff(self):
        import re
        text = self.cleaned_data.get("config_diff") or ""
        bad = [l for l in text.splitlines() if l.strip() and not l.strip().startswith(("set ", "delete ", "#"))]
        if bad:
            raise forms.ValidationError(_("Only set and delete lines: %(l)s") % {"l": bad[0][:80]})
        if any(re.search(r"(password|private-key|pre-shared-secret) ", l) for l in text.splitlines()):
            raise forms.ValidationError(_("No secrets here: they come from Secrets."))
        return text


class NsForm(TypeForm):
    stype = T.NS
    disk_gb = _disk()
    SIZE_FIELDS = ("vcpu", "ram_gb", "disk_gb")


class LogsForm(TypeForm):
    stype = T.LOGS
    disk_gb = _disk()
    volume = forms.ChoiceField(label=_l("Volume of the logs"), help_text=_l("Its storage type decides the data network of NIC 2."))
    SIZE_FIELDS = ("vcpu", "ram_gb", "disk_gb")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["volume"].choices = _volumes(self.site)


class AdminForm(TypeForm):
    stype = T.ADMIN
    disk_gb = _disk()
    instances = forms.IntegerField(label=_l("Instances"), min_value=1, max_value=20, initial=3)
    first_ip = forms.GenericIPAddressField(label=_l("First instance's address"), protocol="IPv4",
                                           help_text=_l("On core-services; the next instances take the next addresses. Each must be "
                                                        "one of the site's management hosts (site settings)."))
    data_volume = forms.ChoiceField(label=_l("Volume of the management data"))
    home_volume = forms.ChoiceField(label=_l("Volume of the admins' homes"), required=False, help_text=_l("Optional."))
    home_subdir = forms.RegexField(label=_l("Folder in it"), regex=r"^/?[A-Za-z0-9._/-]*$", required=False,
                                   help_text=_l("e.g. /admins/admin1"))
    SIZE_FIELDS = ("vcpu", "ram_gb", "disk_gb", "instances")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["data_volume"].choices = self.fields["home_volume"].choices = _volumes(self.site)

    def clean(self):
        d = super().clean()
        first, n = d.get("first_ip"), d.get("instances")
        if first and n:
            bad = [str(ipaddress.ip_address(first) + i) for i in range(n)
                   if not in_mgmt_hosts(self.site, str(ipaddress.ip_address(first) + i))]
            if bad:
                self.add_error("first_ip", _("Not among the site's management hosts (site settings): %(ips)s.") % {"ips": ", ".join(bad)})
        return d


class BackupForm(TypeForm):
    stype = T.BACKUP
    disk_gb = _disk()
    datastore = forms.ChoiceField(label=_l("Backup datastore"), help_text=_l("One backup service per datastore; its restic VM "
                                                                             "address, backend and secrets are the datastore's."))
    SIZE_FIELDS = ("vcpu", "ram_gb", "disk_gb")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        taken = {str((s.params or {}).get("datastore")) for s in CoreService.objects.filter(site__code=self.site, stype=T.BACKUP)
                 .exclude(pk=self.svc.pk if self.svc else None)}
        self.fields["datastore"].choices = [("", "—")] + [
            (str(d.pk), f"{d.platform.key} / {d.name}" + (f" ({d.get_backend_display()})" if d.backend else ""))
            for d in PlatformDatastore.objects.filter(platform__site__code=self.site, kind=PlatformDatastore.Kind.BACKUPS)
            .select_related("platform").order_by("platform__key", "name") if str(d.pk) not in taken]

    def params(self):
        p = super().params()
        p["datastore"] = int(p["datastore"]) if p.get("datastore") else None
        return p


FORMS = {T.BGP_RR: BgpRrForm, T.CLOUD_EGW: EgwForm, T.CLOUD_IGW: IgwForm, T.CLOUD_GW: GwForm, T.PORTAL_GW: PortalGwForm, T.NS: NsForm, T.LOGS: LogsForm,
         T.ADMIN: AdminForm, T.BACKUP: BackupForm}


def initial(svc):
    p = dict(svc.params or {})
    p.update(vcpu=svc.vcpu, ram_gb=svc.ram_gb, disk_gb=svc.disk_gb, config_diff=svc.config_diff)
    if isinstance(p.get("aliases"), list):
        p["aliases"] = " ".join(p["aliases"])
    return {k: (str(v) if k in ("network", "datastore", "volume", "data_volume", "home_volume") and v else v) for k, v in p.items()}


def available(site):
    """The types a new service can have on the site: [(type, label, available)] (the one-per-site ones once)."""
    have = set(CoreService.objects.filter(site__code=site, stype__in=SINGLE).values_list("stype", flat=True))
    return [(t, label, t not in have) for t, label in T.choices if t in TYPED]  # core-vm: no form defined yet


def save(site, stype, form, svc=None, user=None):
    """Calculate the definition from the form and write it; a new service is named by its type. Returns the service."""
    from accounts.models import audit
    from .models import Site
    p = form.params()
    d = definition(site, stype, p, existing=svc)
    if svc is None:
        if stype in SINGLE and CoreService.objects.filter(site__code=site, stype=stype).exists():
            raise DefinitionError(_("%(s)s already has its %(t)s service.") % {"s": site, "t": stype})
        if CoreService.objects.filter(site__code=site, name=d["name"]).exists():
            raise DefinitionError(_("%(s)s already has a service named %(n)s.") % {"s": site, "n": d["name"]})
        svc = CoreService(site=Site.objects.get(code=site), name=d["name"])
    svc.params = p
    if not svc.description:
        svc.description = dict(T.choices)[stype].split(": ", 1)[-1]
    if stype in (T.CLOUD_IGW, T.CLOUD_GW):
        svc.config_diff = form.cleaned_data.get("config_diff") or ""
    new = svc.pk is None
    apply(svc, d, user)
    audit(user, "core_service_saved", svc, site=site, service=svc.name, type=stype, nodes=len(d["nodes"]), new=new)
    return svc
