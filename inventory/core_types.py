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
    onefe      the OpenNebula front end of a platform whose front end runs in VMs, one per platform (T34)
    core-vm    anything else: the generic form (inventory/core_views.ServiceForm)

Existing VMs keep their names and host names; the calculated ones (target_*) are what a redeploy will give them.
"""

import ipaddress

from django import forms
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l

from .models import (
    Cluster,
    ConfigTemplate,
    CoreService,
    CoreServiceNode,
    Network,
    Platform,
    PlatformControl,
    PlatformDatastore,
    ServiceImage,
    StorageVolume,
)

T = CoreService.Type
# one per site; T36 B3b: ns - the primary site's 1st and 2nd, one on every other site (ns_limit)
SINGLE = (T.CLOUD_EGW, T.CLOUD_IGW, T.CLOUD_GW, T.PORTAL_GW, T.LOGS, T.ADMIN, T.CLAUGINE)
HOSTING = ("mgmt", "simple")  # rules v2 B5: the platforms Site → Services run on (portal-gw: its ext-managed one)
VYOS_TYPES = (T.BGP_RR, T.CLOUD_EGW, T.CLOUD_IGW, T.CLOUD_GW, T.PORTAL_GW)
SIZES = {
    T.BGP_RR: (2, 4, None),
    T.CLOUD_EGW: (2, 4, None),
    T.CLOUD_IGW: (4, 4, None),
    T.CLOUD_GW: (2, 4, None),
    T.PORTAL_GW: (2, 4, None),
    T.NS: (1, 2, 10),  # owner 2026-11-24 20:39 (T36 Q16): ns 1/2/10 - base-os's minimum RAM is 2 GB (commit-479)
    T.LOGS: (6, 48, 20),
    T.ADMIN: (2, 8, 30),
    T.BACKUP: (2, 8, 10),
    T.ONEFE: (8, 32, 100),  # owner 2026-11-21 13:15 (T34): the service profile onefe
    T.CLAUGINE: (4, 8, 50),  # rules v2 A6
}
TEMPLATES = {
    T.CLOUD_EGW: "egw",
    T.CLOUD_IGW: "igw",
}  # owner 2026-11-21 13:10 (T34 Q15): the base-OS types have no template - their VMs are made from a service profile
CHECKS = {
    T.CLOUD_EGW: {"bgp": "ipv4"},
    T.NS: {"units": ["named"], "ports": [53]},
    T.LOGS: {"units": ["graylog-server", "graylog-datanode", "mongod"], "ports": [9000]},
    T.ADMIN: {"units": ["gnome-remote-desktop"]},  # owner 13:38: Ubuntu 26.04's own RDP, no xrdp
    T.ONEFE: {"units": ["opennebula", "opennebula-fireedge", "mysql"], "ports": [2633, 2616]},
}
TYPED = (
    T.BGP_RR,
    T.CLOUD_GW,
    T.PORTAL_GW,
    T.NS,
    T.LOGS,
    T.ADMIN,
    T.BACKUP,
    T.ONEFE,
    T.CLAUGINE,
)  # cloud-egw / cloud-igw: kept, not made any more
GW_RANGE = "cloud-gw"  # the range of the egw's address on the exchange network (correction 11)
PORTAL_IX = "portal-ix"  # the exchange network of the portal platforms (correction 12)
PORTAL_KIND = "ext-managed"  # the platform kind of a portal platform


def portal_publics(platform, ix):
    """[{name, cidr, vip, ips: [egw1, egw2], prefix}] — one public overlay per delegated subnet of the portal platform on
    its exchange (correction 12): VIP the first address (the VMs' gateway), egw1 and egw2 the next two."""
    out = []
    subs = sorted(
        (d for d in platform.delegated_subnets("inet") if d.segment == ix.name),
        key=lambda d: ipaddress.ip_network(d.cidr),
    )
    for i, ds in enumerate(subs, 1):
        net = ipaddress.ip_network(ds.cidr)
        hs = list(net.hosts())
        out.append(
            {
                "name": f"portal-public{i:02d}",
                "cidr": str(net),
                "prefix": net.prefixlen,
                "vip": str(hs[0]),
                "ips": [str(hs[1]), str(hs[2])],
            }
        )
    return out


class DefinitionError(Exception):
    """What the definition cannot do without (a network, a datastore …)."""


# --- what the site has ----------------------------------------------------------------------------------------------


def net_of(site, kind):
    return Network.objects.filter(site=site, kind=kind).exclude(state=Network.State.RETIRED).order_by("name").first()


def portal_platform(site):
    """commit-435: the site's portal platform (ext-managed) a portal-gw is made on by default - an active one."""
    return Platform.objects.filter(site__code=site, kind=PORTAL_KIND).exclude(state="retired").order_by("key").first()


def mgmt_of(site):
    """(the management platform, its cluster) of a site: where the core services' VMs run (commit-435: an active one;
    T36 B5: the site's mgmt or simple platform - one per site by its layout)."""
    p = Platform.objects.filter(site__code=site, kind__in=HOSTING).exclude(state="retired").order_by("key").first()
    cl = Cluster.objects.filter(platform=p).exclude(state="retired").first() if p else None
    return p, cl


def image_of(platform, typ):
    """The platform's image of a type (vyos, base-os)."""
    c = PlatformControl.objects.filter(key=platform.key).first() if platform else None
    return (
        ServiceImage.objects.filter(control=c, purpose=typ, kind=ServiceImage.Kind.IMAGE)
        .order_by("position", "pk")
        .first()
        if c
        else None
    )


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
    """The route reflectors' addresses of a BGP network: its list, else its rrs range (owner 2026-11-17: the range is
    exactly the route reflectors, …241 + 3)."""
    if net.rrs:
        return [str(x) for x in net.rrs]
    r = net.ranges.filter(name="rrs").first()
    return [str(r.first + i) for i in range(r.length)] if r else []


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


def rr_role(net):
    """The VyOS template of a network's route reflectors: rr-vxlan (EVPN), rr-ix (any exchange with external routers:
    owner 2026-11-23 16:31 "rr-ix is the same for any network with extenal routers")."""
    if net.kind == Network.Kind.EVPN:
        return "rr-vxlan"
    return "rr-ix"


def definition(site, stype, p, existing=None, profile=None):
    """What a service of this type is, from what its form asks (p) and the site: a dict with name, platform, serves,
    image, template, ssh_port, ssh_secret, service_secret, secrets [(label, ref)], vcpu, ram_gb, disk_gb, nics
    [(Network, gateway)], nodes [{name, hostname, ips}], aliases (cloud-igw), checks, notes [text].
    Raises DefinitionError when something it needs is missing."""
    from .nfconfig import management

    ports = management(site)[1]
    platform, cluster = mgmt_of(site)
    chosen = p.get(HOST_PLATFORM)
    if chosen:  # owner 2026-11-23 16:01 (U11): the platform hosting the VMs, chosen in the form
        platform = Platform.objects.filter(pk=chosen, site__code=site).first()
        if platform is None:
            raise DefinitionError(_("The platform chosen to host the VMs is not one of %(s)s's.") % {"s": site})
        kept = existing is not None and existing.platform_id == platform.pk  # a service's own, as it was
        want = (PORTAL_KIND,) if stype == T.PORTAL_GW else HOSTING
        if platform.kind not in want and not kept:  # T36 rules v2 B5; portal-gw on its ext-managed (owner 17:40)
            raise DefinitionError(
                _("%(p)s is a %(k)s platform: the site's services run on its %(w)s platform.")
                % {"p": platform.key, "k": platform.kind, "w": " / ".join(want)}
            )
        cluster = Cluster.objects.filter(platform=platform).exclude(state="retired").first()
    elif (
        stype == T.PORTAL_GW
    ):  # commit-437: its default (an active portal platform) before the check, as the form shows
        platform = portal_platform(site) or platform
        cluster = Cluster.objects.filter(platform=platform).exclude(state="retired").first() if platform else None
    if platform is None:
        raise DefinitionError(_("%(s)s has no management platform.") % {"s": site})
    vcpu, ram, disk = SIZES[stype]
    from .core_services import site_checks

    d = {
        "stype": stype,
        "platform": platform,
        "cluster": cluster,
        "serves": None,
        "kind": "vm",
        "notes": [],
        "vcpu": p.get("vcpu") or vcpu,
        "ram_gb": p.get("ram_gb") or ram,
        "disk_gb": p.get("disk_gb") or disk or (existing.disk_gb if existing else None) or 2,
        "ssh_secret": platform.sa or "",
        "service_secret": "",
        "secrets": [],
        "aliases": [],
        "checks": site_checks(CHECKS.get(stype, {}), site),
    }
    vyos = stype in VYOS_TYPES
    d["image"] = image_of(platform, "vyos" if vyos else "base-os")
    if d["image"] is None:
        d["notes"].append(
            _("%(p)s has no %(t)s image (site → Platforms → service images).")
            % {"p": platform.key, "t": "vyos" if vyos else "base-os"}
        )
    if stype == T.ADMIN:  # commit-473 (owner N3): the admin VMs' public SSH port is Settings → Security's
        from tenants.models import PortalSettings

        d["ssh_port"] = PortalSettings.get().public_ssh_port
    else:
        d["ssh_port"] = ports["vyos"] if vyos else 22
    d["secrets"].append((_("ssh (super admin of %(p)s)") % {"p": platform.key}, platform.sa or ""))
    if not platform.sa:
        d["notes"].append(_("%(p)s has no super admin secret.") % {"p": platform.key})
    cs, vy = net_of(site, Network.Kind.CORE_SERVICES), net_of(site, Network.Kind.CORE_VYOS)

    def need(net, what):
        if net is None:
            raise DefinitionError(_("%(s)s has no %(w)s network (site → Networks).") % {"s": site, "w": what})
        return net

    if stype == T.BGP_RR:
        net = Network.objects.filter(
            pk=p.get("network") or 0, site=site, kind__in=[Network.Kind.IX, Network.Kind.EVPN]
        ).first()
        if net is None:
            raise DefinitionError(_("Choose the BGP network."))
        need(vy, "core-vyos")
        ips = rr_ips(net)
        if not ips:
            raise DefinitionError(_("%(n)s has no route reflector addresses (its rrs range).") % {"n": net.name})
        role = rr_role(net)
        d.update(
            name=f"{net.name}-rrs",
            template=role,
            nics=[(vy, ""), (net, "")],
            network=net,
            checks={"bgp": "evpn" if net.kind == Network.Kind.EVPN else "ipv4"},
            nodes=[vyos_node(f"{net.name}-rr{i + 1}-{site}", ["", ip]) for i, ip in enumerate(ips)],
        )
        d["service_secret"] = bgp_secret_of(net)
        d["secrets"].append((_("BGP (network %(n)s)") % {"n": net.name}, d["service_secret"]))
        if not d["service_secret"]:
            d["notes"].append(
                _("The peer groups of %(n)s have no BGP secret (site → Networks → BGP).") % {"n": net.name}
            )
    elif stype == T.CLOUD_EGW:
        need(vy, "core-vyos")
        need(cs, "core-services")
        ix = ix_for(site, cs)
        if ix is None:
            raise DefinitionError(
                _(
                    "No exchange routes the core-services subnet %(c)s (site → Networks → routes or "
                    "delegated subnets of an ix network)."
                )
                % {"c": cs.cidr}
            )
        first = hosts_of(cs)[0]
        d.update(
            name="cloud-egw",
            template=TEMPLATES.get(stype),
            nics=[(vy, ""), (ix, ""), (cs, "")],
            network=ix,
            nodes=[vyos_node(f"{cs.name.split('-')[0]}-egw-{site}", ["", "", str(first)])],
        )
        d["service_secret"] = bgp_secret_of(ix)
        d["secrets"].append((_("BGP (network %(n)s)") % {"n": ix.name}, d["service_secret"]))
    elif stype == T.CLOUD_IGW:
        need(vy, "core-vyos")
        need(cs, "core-services")
        cc = need(net_of(site, Network.Kind.CORE_CLOUD), "core-cloud")
        hs = hosts_of(cs)
        if len(hs) < 2:
            raise DefinitionError(_("%(c)s is too small for the igw (its second address).") % {"c": cs.cidr})
        d.update(
            name="cloud-igw",
            template=TEMPLATES.get(stype),
            nics=[(vy, ""), (cs, ""), (cc, "")],
            aliases=[str(x) for x in hs[2:]],
            nodes=[vyos_node(f"{cs.name.split('-')[0]}-igw-{site}", ["", str(hs[1]), str(hosts_of(cc)[0])])],
        )
    elif stype == T.CLOUD_GW:
        # correction 11: one service, two VMs. egw: eth0 management, eth1 the exchange network (its cloud-gw range),
        # eth2 core-services (first address). igw: eth0 management, eth1 core-services (second address, aliases: the
        # further public addresses the platform gives it), eth2 core-cloud (its last address, e.g. .254 of a /24: the core VMs' way out; the network's
        # gateway .1 is the core router).
        need(vy, "core-vyos")
        need(cs, "core-services")
        cc = need(net_of(site, Network.Kind.CORE_CLOUD), "core-cloud")
        ix = next(
            (
                n
                for n in Network.objects.filter(site=site, kind=Network.Kind.IX).order_by("name")
                if n.ranges.filter(name=GW_RANGE).exists()
            ),
            None,
        ) or ix_for(site, cs)
        if ix is None:
            raise DefinitionError(
                _("No exchange network has the range %(r)s (site → Networks → ranges).") % {"r": GW_RANGE}
            )
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
        d.update(
            name="cloud-gw",
            template=TEMPLATES[T.CLOUD_IGW],
            nics=[(vy, ""), (ix, ""), (cs, "")],
            network=ix,
            aliases=aliases,
            checks={},
            nodes=[
                dict(
                    vyos_node(f"{gw}-egw-{site}", ["", str(r.first), str(hs[0])]),
                    params={"role": "egw", "nics": [vy.pk, ix.pk, cs.pk], "checks": {"bgp": "ipv4"}},
                ),
                dict(
                    vyos_node(f"{gw}-igw-{site}", ["", str(hs[1]), str(hc[-1])]),
                    params={"role": "igw", "nics": [vy.pk, cs.pk, cc.pk], "aliases": aliases, "checks": {}},
                ),
            ],
        )
        d["service_secret"] = bgp_secret_of(ix)
        d["secrets"].append((_("BGP (network %(n)s)") % {"n": ix.name}, d["service_secret"]))
        if not d["service_secret"]:
            d["notes"].append(
                _("The peer groups of %(n)s have no BGP secret (site → Networks → BGP).") % {"n": ix.name}
            )
    elif stype == T.PORTAL_GW:
        # correction 12: the egws of the site's portal platform (ext-managed): two VMs; eth0 management, eth1 portal-ix
        # (a free address), eth2 … the public overlays (one per delegated subnet: VIP the first address over VRRP, egw1
        # the second, egw2 the third)
        plat = portal_platform(site)  # commit-435: an active one
        if chosen:  # U11: a portal platform chosen (the form offers only those)
            plat = d["platform"] if d["platform"].kind == PORTAL_KIND else None
        if plat is None:
            raise DefinitionError(_("%(s)s has no portal platform (type %(k)s).") % {"s": site, "k": PORTAL_KIND})
        if plat != d["platform"]:  # commit-439: the common part looked up (and noted) the image of the one it took
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
            nodes.append(
                dict(
                    vyos_node(f"portal-egw{k}-{site}", ["", ""] + [pb["ips"][k - 1] for pb in publics]),
                    params={
                        "role": "egw",
                        "nics": [vy.pk, ix.pk] + [f"public:{pb['name']}" for pb in publics],
                        "checks": {"bgp": "ipv4"},
                        "dynamic": [1],
                        "vrrp_priority": 200 if k == 1 else 100,
                        "publics": publics,
                    },
                )
            )
        d.update(
            name="portal-gw",
            template="egw",
            nics=[(vy, ""), (ix, "")],
            network=ix,
            aliases=[],
            checks={},
            nodes=nodes,
            publics=publics,
        )
        d["service_secret"] = bgp_secret_of(ix)
        d["secrets"].append((_("BGP (network %(n)s)") % {"n": ix.name}, d["service_secret"]))
        if not d["service_secret"]:
            d["notes"].append(
                _("The peer groups of %(n)s have no BGP secret (site → Networks → BGP).") % {"n": ix.name}
            )
    elif stype == T.NS:
        # owner 2026-11-21 18:44 (T34 Q19): the base-OS core services' NIC 1 is core-cloud (cloud-core), where they run
        cs = need(net_of(site, Network.Kind.CORE_CLOUD), "core-cloud")
        from .models import Site

        dns = list(Site.objects.get(code=site).dns_servers or [])
        if not dns:
            raise DefinitionError(_("%(s)s has no DNS servers (site settings).") % {"s": site})
        # T36 B3b (rules v2 C4): several ns services on a site (ns-primary, ns-secondary). commit-507: a profile with a
        # DNS slot takes its own server (1st, 2nd; a non-primary site's: its 1st); commit-506: a service of more VMs
        # made before the split keeps its servers (the ones no other ns service has) until it is split
        prof = profile if profile is not None else (existing.profile if existing is not None else None)
        slot = prof.dns_slot if prof is not None else None
        taken = ns_taken(site, existing)
        if slot is not None and not ns_legacy(existing):
            i = 1 if slot == 2 else 0
            if i >= len(dns):
                raise DefinitionError(
                    _("%(s)s has no DNS server number %(n)s (site settings).") % {"s": site, "n": i + 1}
                )
            if dns[i] in taken:
                raise DefinitionError(
                    _("DNS server %(ip)s of %(s)s has its name server service.") % {"ip": dns[i], "s": site}
                )
            free = [(i, dns[i])]
            d["ns_slot"] = True
        else:
            free = [(i, ip) for i, ip in enumerate(dns) if ip not in taken]
        if not free:
            raise DefinitionError(
                _("Every DNS server of %(s)s (site settings) has its name server service.") % {"s": site}
            )
        for ip in dns:
            if ipaddress.ip_address(ip) not in cs.net:
                raise DefinitionError(  # commit-319: never saved outside NIC 1's network
                    _("%(ip)s (site settings: DNS servers) is not in core-cloud %(c)s: correct it there first.")
                    % {"ip": ip, "c": cs.cidr}
                )
        d.update(
            name=existing.name if existing is not None else (profile.key if profile is not None else "ns"),
            template=TEMPLATES.get(stype),
            nics=[(cs, "")],
            nodes=[{"name": f"core-ns{i + 1}_{site}", "hostname": f"s{site}ns{i + 1}", "ips": [ip]} for i, ip in free],
        )
    elif stype == T.LOGS:
        cs = need(net_of(site, Network.Kind.CORE_CLOUD), "core-cloud")  # Q19
        vol = (
            StorageVolume.objects.filter(pk=p.get("volume") or 0, storage__site__code=site)
            .select_related("storage")
            .first()
        )
        if vol is None:
            raise DefinitionError(_("Choose the volume the logs are kept on."))
        dn = need(data_net(site, vol), vol.storage.kind)
        from .models import Site

        logs = Site.objects.get(code=site).log_collectors or {}
        host = (logs.get("core") or {}).get("host") or next((c.get("host") for c in logs.values() if c.get("host")), "")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            # commit-328: the collector the logs service takes, resolved by its own name (not the VyOS collector's)
            import socket

            name = str(host).strip()
            try:
                host = socket.gethostbyname(name) if name else ""
            except (OSError, UnicodeError, ValueError):  # commit-334/335: refused - the stored address is kept
                raise DefinitionError(
                    _("The log collector %(n)s (site settings) cannot be resolved: correct it there, or try again.")
                    % {"n": name[:80]}
                ) from None
            if host:
                d["notes"].append(
                    _("The log collector is a name in the site settings; its address is resolved: %(h)s.") % {"h": host}
                )
        if host and ipaddress.ip_address(host) not in cs.net:
            raise DefinitionError(  # commit-319
                _("%(ip)s (site settings: log collector) is not in core-cloud %(c)s: correct it there first.")
                % {"ip": host, "c": cs.cidr}
            )
        d.update(
            name="logs",
            template=TEMPLATES.get(stype),
            nics=[(cs, ""), (dn, "")],
            volume=vol,
            nodes=[{"name": f"core-logs1_{site}", "hostname": f"s{site}logs", "ips": [str(host), ""]}],
        )
    elif stype == T.ADMIN:
        cs = need(net_of(site, Network.Kind.CORE_CLOUD), "core-cloud")  # Q19
        vol = (
            StorageVolume.objects.filter(pk=p.get("data_volume") or 0, storage__site__code=site)
            .select_related("storage")
            .first()
        )
        if vol is None:
            raise DefinitionError(_("Choose the volume of the management data."))
        dn = need(data_net(site, vol), vol.storage.kind)
        n = admin_vms(existing, profile)  # owner 2026-11-23 (Q24): the count is the profile's
        try:
            first = ipaddress.ip_address(p.get("first_ip") or "")
        except ValueError:
            raise DefinitionError(_("Give the first instance's address.")) from None
        ips = [str(first + i) for i in range(n)]
        for ip in ips:
            if ipaddress.ip_address(ip) not in cs.net:  # commit-319
                raise DefinitionError(
                    _("%(ip)s (the instances' addresses) is not in core-cloud %(c)s: give a first address there.")
                    % {"ip": ip, "c": cs.cidr}
                )
            if not in_mgmt_hosts(site, ip):
                d["notes"].append(_("%(ip)s is not among the site's management hosts (site settings).") % {"ip": ip})
        home = StorageVolume.objects.filter(pk=p.get("home_volume") or 0, storage__site__code=site).first()
        d.update(
            name="admin",
            template=TEMPLATES.get(stype),
            nics=[(cs, ""), (dn, "")],
            volume=vol,
            home_volume=home,
            home_subdir=p.get("home_subdir") or "",
            nodes=[
                {"name": f"core-admin{i + 1}_{site}", "hostname": f"s{site}admin{i + 1}", "ips": [ip, ""]}
                for i, ip in enumerate(ips)
            ],
        )
    elif stype == T.BACKUP:
        ds = (
            PlatformDatastore.objects.filter(
                pk=p.get("datastore") or 0, platform__site__code=site, kind=PlatformDatastore.Kind.BACKUPS
            )
            .select_related("platform", "volume__storage")
            .first()
        )
        if ds is None:
            raise DefinitionError(_("Choose the backup datastore."))
        # owner 2026-11-21 19:03-19:04 (T34 Q20): every Ubuntu service VM has NIC 1 on cloud-core (no VM but the
        # cloud egw/igw is on core-services); a backup VM's NIC 2 is its volume's network (iSCSI)
        cs = need(net_of(site, Network.Kind.CORE_CLOUD), "core-cloud")
        if not ds.restic_ip:
            raise DefinitionError(_("%(d)s has no restic VM address (platform → datastores).") % {"d": ds.name})
        if ipaddress.ip_address(str(ds.restic_ip)) not in cs.net:
            raise DefinitionError(  # commit-319
                _(
                    "%(ip)s (the datastore's restic VM address) is not in core-cloud %(c)s: correct it first (platform → datastores)."
                )
                % {"ip": ds.restic_ip, "c": cs.cidr}
            )
        nics = [(cs, "")]
        if ds.backend != PlatformDatastore.Backend.S3:
            nics.append((need(data_net(site, ds.volume), "storage"), ""))
        pl = ds.platform.name
        d.update(
            name=f"{pl}-backup",
            template=TEMPLATES.get(stype),
            nics=nics,
            datastore=ds,
            serves=ds.platform,
            nodes=[
                {
                    "name": f"core-{pl}-backup1_{site}",
                    "hostname": f"s{site}{pl}backup",
                    "ips": [str(ds.restic_ip)] + ([""] if len(nics) > 1 else []),
                }
            ],
        )
        d["service_secret"] = ds.restic_secret
        d["secrets"].append((_("restic (datastore %(d)s)") % {"d": ds.name}, ds.restic_secret))
        if ds.backend == PlatformDatastore.Backend.S3:
            d["secrets"].append((_("S3 (datastore %(d)s)") % {"d": ds.name}, ds.s3_secret))
        if not ds.restic_secret:
            d["notes"].append(_("%(d)s has no restic password (platform → datastores).") % {"d": ds.name})
    elif stype == T.ONEFE:
        # owner 2026-11-21 13:15 (T34): a platform's front end is a core service; its three nodes are the platform's
        # (Platforms: fe nodes and their addresses), on core-cloud, the site's NFS network second (the datastores)
        plat = Platform.objects.filter(pk=p.get("platform") or 0, site__code=site).first()
        if plat is None:
            raise DefinitionError(_("Choose the platform whose front end it is."))
        if plat.kind in HOSTING and not (existing is not None and (existing.params or {}).get("platform") == plat.pk):
            raise DefinitionError(  # owner 2026-11-24 20:54 (N2): a mgmt / simple platform's front end is on its hosts
                _("%(p)s is a %(k)s platform: its front end runs on its own hosts (their host service).")
                % {"p": plat.key, "k": plat.kind}
            )
        fe = plat.fe or {}
        if fe.get("type") != "vm":
            raise DefinitionError(_("The front end of %(p)s is not made of VMs (Platforms).") % {"p": plat.key})
        names = [str(x) for x in (fe.get("nodes") or [])][:3]
        ips = [str(x or "") for x in (fe.get("node_ips") or [])][: len(names)]
        ips += [""] * (len(names) - len(ips))
        if not names or not all(ips):
            raise DefinitionError(
                _("%(p)s has no front-end nodes with their addresses (Platforms → Edit).") % {"p": plat.key}
            )
        cc = need(net_of(site, Network.Kind.CORE_CLOUD), "core-cloud")
        nfs = net_of(site, Network.Kind.NFS)
        for ip in ips:
            if ipaddress.ip_address(ip) not in cc.net:
                d["notes"].append(_("%(ip)s is not in core-cloud %(c)s.") % {"ip": ip, "c": cc.cidr})
        d.update(
            name=f"{plat.name}-fe",
            template=None,
            nics=[(cc, "")] + ([(nfs, "")] if nfs is not None else []),
            serves=plat,
            nodes=[
                {
                    "name": f"core-{plat.name}-one{i + 1}_{site}",
                    "hostname": n.split(".")[0],
                    "ips": [ip] + ([""] if nfs is not None else []),
                }
                for i, (n, ip) in enumerate(zip(names, ips, strict=True))
            ],
        )
        d["secrets"].append((_("OpenNebula (front end of %(p)s)") % {"p": plat.key}, plat.fe_secrets or ""))
        if not plat.fe_secrets:
            d["notes"].append(_("%(p)s has no front-end secret (Platforms).") % {"p": plat.key})
    elif stype == T.CLAUGINE:  # T36 rules v2 A6: one VM, one NIC on core-cloud
        cc = need(net_of(site, Network.Kind.CORE_CLOUD), "core-cloud")
        try:
            ip = ipaddress.ip_address(p.get("address") or "")
        except ValueError:
            raise DefinitionError(_("Give the VM's address.")) from None
        if ip not in cc.net:
            raise DefinitionError(_("%(ip)s is not in core-cloud %(c)s.") % {"ip": ip, "c": cc.cidr})
        why = claugine_why(site, platform, existing)
        if why:
            raise DefinitionError(why)
        d.update(
            name="claugine",
            template=None,
            nics=[(cc, "")],
            nodes=[{"name": f"core-claugine1_{site}", "hostname": f"s{site}claugine1", "ips": [str(ip)]}],
        )
    if stype not in VYOS_TYPES:  # T34 B4: the secrets its service profile's recipe takes from the instance
        own = p.get("secrets") or {}
        for name in profile_secrets(stype):
            d["secrets"].append((_("%(n)s (service profile)") % {"n": name}, own.get(name) or ""))
            if not own.get(name):
                d["notes"].append(_("No secret %(n)s: the service profile's recipe uses it.") % {"n": name})
    _vm_count(stype, d, existing, profile)  # owner 2026-11-23 (T34 Q24): as many VMs as the service profile says
    for nd in d["nodes"]:
        nd.setdefault("hostname", host_name(nd["name"]))
    if existing is not None:  # running VMs keep their names until they are redeployed
        for nd, old in zip(d["nodes"], existing.nodes.order_by("pk"), strict=False):
            nd["target_name"], nd["target_hostname"] = nd["name"], nd["hostname"]
            nd["name"], nd["hostname"] = old.name, old.hostname or nd["hostname"]
    return d


FIXED = {
    T.CLAUGINE: 1,
    T.LOGS: 1,
    T.BACKUP: 1,
    T.CLOUD_GW: 2,
    T.PORTAL_GW: 2,
    T.ONEFE: 3,
}  # commit-416: a front end is 3 nodes  # what the type's layout makes, whatever the profile


def is_primary(site):
    from .models import Site

    return Site.objects.filter(code=site, primary=True).exists()


def ns_limit(site):
    """T36 B3b (rules v2 C4): the ns services a site has - the primary site its 1st and 2nd, every other one."""
    return 2 if is_primary(site) else 1


def ns_taken(site, existing=None):
    """The DNS server addresses the site's other ns services have (their VMs' first address)."""
    out = set()
    for n in CoreServiceNode.objects.filter(service__site__code=site, service__stype=T.NS).exclude(
        service__pk=existing.pk if existing is not None else None
    ):
        if n.ips and n.ips[0]:
            out.add(str(n.ips[0]))
    return out


def ns_legacy(svc):
    """commit-506: a name server service of more than one VM, made before the split (0122 kept its VMs)."""
    return svc is not None and svc.pk is not None and svc.nodes.exclude(state="retired").count() > 1


def ns_why(site, profile):
    """commit-507: why a new name server service cannot be made from this profile on the site - "" when it can. The
    primary site takes slots 1 and 2, every other site slot 0; each once (a service made before the split holds the
    servers it has)."""
    slot = profile.dns_slot
    if slot is None:
        return str(_("the profile has no DNS slot (Profiles → Services)"))
    primary = is_primary(site)
    if primary and slot == 0:
        return str(_("it is a non-primary site's name server"))
    if not primary and slot != 0:
        return str(_("it is a name server of the primary site"))
    from .models import Site

    dns = list(Site.objects.get(code=site).dns_servers or [])
    i = 1 if slot == 2 else 0
    if i >= len(dns):
        return str(_("the site has no DNS server number %(n)s (site settings: DNS servers)") % {"n": i + 1})
    if dns[i] in ns_taken(site):
        return str(_("DNS server %(ip)s has its name server service") % {"ip": dns[i]})
    return ""


def ns_split_why(svc):
    """commit-506: why the name server service cannot be split into the primary site's ns-primary and ns-secondary -
    "" when it can: a service of two VMs on the primary site, the only one there, both slots' profiles there."""
    from .models import ServiceProfile

    if svc.stype != T.NS or not ns_legacy(svc):
        return str(_("only a name server service of two VMs is split"))
    if svc.nodes.exclude(state="retired").count() != 2:
        return str(_("it has more than two VMs"))
    if not is_primary(svc.site.code):
        return str(_("only on the primary site: every other site has one name server"))
    if CoreService.objects.filter(site=svc.site, stype=T.NS).exclude(pk=svc.pk).exists():
        return str(_("the site has another name server service"))
    for slot in (1, 2):
        if not ServiceProfile.objects.filter(stype=T.NS, dns_slot=slot).exists():
            return str(_("no name server profile has DNS slot %(n)s (Profiles → Services)") % {"n": slot})
    second = ServiceProfile.objects.filter(stype=T.NS, dns_slot=2).order_by("pk").first()
    if CoreService.objects.filter(site=svc.site, name=second.key).exists():
        return str(_("the site has a service named %(n)s") % {"n": second.key})
    _nodes, why = split_nodes(svc)
    return why or split_busy(svc)


def split_nodes(svc):
    """commit-515: ([the 1st DNS server's VM, the 2nd's], "") - each of the site's first two DNS servers matched by
    exactly one VM's first address; else ([], why)."""
    dns = list(svc.site.dns_servers or [])
    if len(dns) < 2:
        return [], str(_("the site has no 2nd DNS server (site settings: DNS servers)"))
    nodes = list(svc.nodes.exclude(state="retired").order_by("pk"))
    out = []
    for ip in dns[:2]:
        hits = [n for n in nodes if n.ips and str(n.ips[0]) == str(ip)]
        if len(hits) != 1:
            return [], str(
                _("DNS server %(ip)s is the address of %(n)s of its VMs, not one") % {"ip": ip, "n": len(hits)}
            )
        out.append(hits[0])
    if out[0].pk == out[1].pk:  # commit-515 again: two different VMs (a site's servers may repeat an address)
        return [], str(_("the site's 1st and 2nd DNS servers are one address"))
    return out, ""


def split_busy(svc):
    """commit-516: a VM of the service with a task queued or running (its redeploy, rollback, Remove snapd …; the
    service's Restore all) or a fresh claim (Apply logging, an operation starting) - "" when none."""
    from accounts.models import Task

    from .hostlogging import claim_fresh

    live = Task.objects.filter(state__in=[Task.State.QUEUED, Task.State.RUNNING])
    nodes = list(svc.nodes.all())
    if (
        live.filter(target_type="coreservicenode", target_id__in=[str(n.pk) for n in nodes]).exists()
        or live.filter(target_type="coreservice", target_id=str(svc.pk)).exists()
    ):
        return str(_("a task of it is queued or running"))
    if any(claim_fresh(n.logging_claim) for n in nodes):
        return str(_("an operation on one of its VMs is starting or running"))
    return ""


def ns_split(svc, user=None):
    """commit-506 (owner 18:10): a name server service of two VMs made before the split becomes the primary site's
    ns-primary (its 1st DNS server's VM) and ns-secondary (the 2nd's) - records only: the second VM's record moves
    with its state, made VM, addresses and logging; nothing on the platform changes. Returns the new service."""
    from django.db import transaction

    from accounts.models import audit

    from .models import ServiceProfile, Site

    with transaction.atomic():
        # commit-515/516: the site row (its DNS servers), then the service and its VMs - locked, read again, checked
        site = Site.objects.select_for_update().get(pk=svc.site_id)
        svc = CoreService.objects.select_for_update().get(pk=svc.pk)
        svc.site = site
        list(CoreServiceNode.objects.select_for_update().filter(service=svc).order_by("pk"))
        why = ns_split_why(svc)
        if why:
            raise DefinitionError(why)
        first = ServiceProfile.objects.filter(stype=T.NS, dns_slot=1).order_by("pk").first()
        second = ServiceProfile.objects.filter(stype=T.NS, dns_slot=2).order_by("pk").first()
        moved = split_nodes(svc)[0][1]  # commit-515: the 2nd DNS server's VM
        new = CoreService.objects.get(pk=svc.pk)
        new.pk, new.id, new.name, new.profile, new.recovery = None, None, second.key, second, {}
        new.description = svc.description
        new.save()
        CoreServiceNode.objects.filter(pk=moved.pk).update(service=new)
        svc.profile = first
        svc.save(update_fields=["profile"])
        svc.sync_state()
        new.sync_state()
    audit(user, "core_service_split", svc, site=svc.site.code, service=svc.name, new=new.name, vm=moved.name)
    return new


def second_node_allowed():
    """commit-509: a confirmed Enterprise licence (the control plane said so) on an Enterprise build, whose features
    allow a second node - never when the edition is not known or the licence is for one node."""
    from accounts import edition

    return edition.current() == edition.ENTERPRISE and not edition.standard_build() and edition.allows("second_node")


def claugine_why(site, platform=None, existing=None):
    """Why a claugine service cannot be on this site (platform) - "" when it can (rules v2 A6, owner 2026-11-24 20:13):
    the first on the primary site; a second only with an Enterprise licence, on another platform; never a third."""
    others = list(CoreService.objects.filter(stype=T.CLAUGINE).exclude(pk=existing.pk if existing else None))
    if not others:
        return "" if is_primary(site) else str(_("The first claugine service runs on the primary site."))
    if not second_node_allowed():
        return str(_("A second claugine node needs an Enterprise licence that allows it."))
    if len(others) > 1:
        return str(_("An installation has at most two claugine nodes."))
    if platform is not None and others[0].platform_id == platform.pk:
        return str(_("The second claugine node runs on another platform than the first."))
    return ""


def profile_for(stype, d, existing=None):
    """The service profile of the service being defined: its link, else the type's default (T34 S3b)."""
    if existing is not None and existing.profile_id and fits(existing.profile, stype, d):
        return existing.profile
    return default_profile(stype, d)


def admin_vms(existing=None, profile=None):
    """How many admin jump hosts: the profile's count (owner 2026-11-23: 3 for jumps), else 3."""
    prof = profile or profile_for(T.ADMIN, {}, existing)
    return (prof.vms if prof is not None else None) or 3


def _vm_count(stype, d, existing, profile=None):
    """owner 2026-11-23 (T34 Q24): "vm counts specied in each service profile". A type whose layout fixes its VMs
    (logs, backup, the gateways) must agree with its profile; the others take the first N of what their source gives
    (a route reflector network's rrs range, the site's DNS servers, the platform's front-end nodes) - fewer is an
    error."""
    if stype == T.NS and d.get("ns_slot"):  # commit-507: one VM, the profile's DNS server
        return
    prof = profile or profile_for(stype, d, existing)  # commit-419: the profile chosen on Add, when there is one
    want = prof.vms if prof is not None else None
    have = len(d["nodes"])
    if stype in FIXED and have != FIXED[stype]:  # commit-416: whatever the profile says (its count may be blank)
        raise DefinitionError(
            _("A %(t)s service has %(n)s VMs; only %(h)s can be defined here (%(why)s).")
            % {"t": stype, "n": FIXED[stype], "h": have, "why": SOURCES.get(stype, stype)}
        )
    if not want:
        return
    if stype in FIXED:
        if want != FIXED[stype]:
            raise DefinitionError(
                _("A %(t)s service has %(n)s VMs; the service profile %(p)s says %(w)s.")
                % {"t": stype, "n": FIXED[stype], "p": prof.key, "w": want}
            )
        return
    if have < want:
        raise DefinitionError(
            _("The service profile %(p)s wants %(w)s VMs; only %(n)s can be defined here (%(why)s).")
            % {"p": prof.key, "w": want, "n": have, "why": SOURCES.get(stype, stype)}
        )
    d["nodes"] = d["nodes"][:want]


SOURCES = {
    T.BGP_RR: _l("the network's route reflector addresses: its rrs range"),
    T.NS: _l("the site's DNS servers: site settings"),
    T.ONEFE: _l("the platform's front-end nodes: Platforms"),
    T.ADMIN: _l("the addresses from the first one"),
}


def _reader(svc):
    """A client of the platform hosting the service's VMs for reads only (adoption or execution mode)."""
    from tenants.deploy import One

    from .models import PlatformControl

    if svc.platform is None:
        raise DefinitionError(_("%(s)s has no platform hosting its VMs.") % {"s": svc.name})
    c = PlatformControl.objects.filter(key=svc.platform.key).first()
    if c is None:
        raise DefinitionError(
            _("%(p)s has no platform control (Infrastructure → Platforms).") % {"p": svc.platform.key}
        )
    return One(c)


def _no_running_surplus(svc, d):
    """commit-415: the nodes a saved definition would drop (beyond its count) must have no VM: none the portal made,
    none in the platform's last read (-old included), and none on the platform now - asked live, by name; refused when
    any is there or the platform cannot be asked, naming them."""
    from .core_services import site_vms

    surplus = list(svc.nodes.order_by("pk")[len(d["nodes"]) :])
    if not surplus:
        return
    vms = site_vms(svc.site.code)
    busy = [n.name for n in surplus if n.deployed_vm_id or n.name in vms or f"{n.name}-old" in vms]
    if not busy:
        from . import core_deploy as cd

        try:
            one = _reader(svc)  # commit-417: a read - adoption mode allows it
            busy = [
                n.name for n in surplus if cd._vm(one, n.name) is not None or cd._vm(one, f"{n.name}-old") is not None
            ]
        except Exception as exc:  # noqa: BLE001 - absence not established: nothing is removed
            raise DefinitionError(
                _("The service would have %(n)s VMs; whether %(v)s still run cannot be asked (%(e)s): not saved.")
                % {"n": len(d["nodes"]), "v": ", ".join(n.name for n in surplus), "e": exc}
            ) from exc
    if busy:
        raise DefinitionError(
            _("The service would have %(n)s VMs; these still run, remove them first: %(v)s.")
            % {"n": len(d["nodes"]), "v": ", ".join(busy)}
        )


def apply(svc, d, user=None):
    """Write a definition into the service and its nodes (records only)."""
    svc.stype, svc.kind = d["stype"], "vm"
    svc.platform, svc.serves, svc.image = d["platform"], d["serves"], d["image"]
    svc.template = ConfigTemplate.objects.filter(role=d["template"]).first() if d["template"] else None
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
            site=svc.site.code,
            name=pb["name"],
            defaults={
                "kind": Network.Kind.PUBLIC,
                "interface": "vxlan",
                "vlan": 0,
                "subnet": str(net.network_address),
                "prefix": net.prefixlen,
                "gateway": pb["vip"],
            },
        )
    olds = list(svc.nodes.order_by("pk"))
    from .models import ServiceProfile

    # commit-572: the profile read now, under the lock save() took (not an instance read before it)
    prof = (
        ServiceProfile.objects.filter(pk=svc.profile_id).first()
        if svc.profile_id
        else ServiceProfile.of_type(svc.stype)
    )
    for i, nd in enumerate(d["nodes"]):
        n = olds[i] if i < len(olds) else CoreServiceNode(service=svc, name=nd["name"], hostname=nd["hostname"])
        n.ips = nd["ips"]
        kept = (n.params or {}).get("role")
        n.params = dict(nd.get("params") or {})
        if prof is not None and "role" not in n.params:  # commit-560: its role among the profile's
            if kept:
                n.params["role"] = kept
            prof.give_role(n, i)
        if n.params.get("nics"):  # "public:<name>" → the site network's id
            n.params["nics"] = [
                publics[x.split(":", 1)[1]].pk if isinstance(x, str) and x.startswith("public:") else x
                for x in n.params["nics"]
            ]
        n.secret, n.ssh_port = svc.ssh_secret, svc.ssh_port if n.pk is None else n.ssh_port
        if n.pk is None:
            n.address = next((ip for ip in nd["ips"] if ip), nd["name"])
            n.state = CoreService.State.PLANNED  # T36 B3a: a VM added starts planned
        n.save()
    for n in olds[len(d["nodes"]) :]:  # beyond the definition (fewer instances)
        n.delete()
    svc.sync_state()  # T36 B3a: the service's state from its VMs'
    return svc


# --- the forms ------------------------------------------------------------------------------------------------------


def profile_secrets(stype):
    """The secrets a VM of the type takes from its instance (T34 B4): the {{ secret.<name> }} of its service
    profile's recipe, sorted; [] for a type without a profile."""
    from .base_deploy import templates_of
    from .models import ServiceProfile
    from .recipes import references

    prof = ServiceProfile.of_type(stype)
    if prof is None:
        return []
    return sorted(references(prof, templates_of(prof))[0].get("secret", ()))


SECRET_FIELD = "secret__"  # a form field per secret of the profile: secret__<name>


HOST_PLATFORM = "host_platform"  # owner 2026-11-23 (U11): the platform hosting a service's VMs, when chosen


def platform_fixed(svc):
    """Whether the platform hosting a service's VMs is fixed (owner 2026-11-23, Q7 "cannot"; commit-428): a VM of it was
    made by the portal (deployed_vm_id), or the last read of the site's platforms found one of its VMs or a kept -old
    VM (a service found on the platforms, not made by the portal), or that cannot be established - a platform of the
    site not read yet while the service has VMs defined. Only a service none of whose VMs is on a platform may move."""
    if svc is None or not svc.pk:
        return False
    nodes = list(svc.nodes.all())
    if not nodes:
        return False
    if any(n.deployed_vm_id for n in nodes):
        return True
    site = svc.site.code
    controls = list(PlatformControl.objects.filter(key__startswith=f"{site}."))
    if not controls or any(c.vms_at is None for c in controls):
        return True  # not read: their absence cannot be established
    names = {vm.get("name") for c in controls for vm in (c.vms or [])}
    return any(n.name in names or f"{n.name}-old" in names for n in nodes)


class TypeForm(forms.Form):
    """Only what the type's definition lists before "calculated automatically" (correction 07)."""

    stype = None
    vcpu = forms.IntegerField(label=_l("vCPU"), min_value=1, max_value=256)
    ram_gb = forms.IntegerField(label=_l("RAM, GB"), min_value=1, max_value=4096)
    SIZE_FIELDS = ("vcpu", "ram_gb")

    def __init__(self, *args, site=None, svc=None, profile=None, **kwargs):
        self.site, self.svc, self.profile = site, svc, profile
        super().__init__(*args, **kwargs)
        v, r, dk = SIZES[self.stype]
        if profile is not None:  # T34 S3c: a new service starts from its profile's size
            v, r, dk = profile.vcpu or v, profile.ram_gb or r, profile.disk_gb or dk
        self.fields["vcpu"].initial, self.fields["ram_gb"].initial = v, r
        self._host_platform_field()
        if "disk_gb" in self.fields:
            self.fields["disk_gb"].initial = dk
        if self.stype not in VYOS_TYPES:  # T34 B4: the instance's own secrets its profile's recipe uses
            from .credentials import ref_choices

            choices = ref_choices("password")
            for name in profile_secrets(self.stype):
                key = SECRET_FIELD + name
                cur = self.initial.get(key) or ""
                opts = list(choices) + ([(cur, f"{cur} (?)")] if cur and cur not in [c[0] for c in choices] else [])
                self.fields[key] = forms.ChoiceField(
                    label=_("Secret %(n)s") % {"n": name},
                    required=False,
                    choices=[("", "—")] + opts,
                    help_text=_("{{ secret.%(n)s }} of the service profile (a password secret).") % {"n": name},
                )

    def _host_platform_field(self):
        """U11 (owner 2026-11-23 16:01: "in edit dialog dont see platfrom selection"): the platform hosting the VMs;
        empty - the site's default (its management platform; the portal platform for portal-gw). Q7 "cannot": read only
        once a VM of the service is on a platform."""
        qs = Platform.objects.filter(site__code=self.site).exclude(state="retired").order_by("key")
        if self.stype == T.PORTAL_GW:
            qs = qs.filter(kind=PORTAL_KIND)
        else:  # T36 rules v2 B5: Site → Services run on the site's mgmt or simple platform
            qs = qs.filter(kind__in=HOSTING)
        default = self._default_platform()
        f = forms.ChoiceField(
            label=_("Platform hosting the VMs"),
            required=False,
            choices=[("", _("the site's: %(p)s") % {"p": default.key if default else "—"})]
            + [(str(x.pk), x.key) for x in qs],
            help_text=_("Chosen while none of its VMs is on a platform."),
        )
        svc = self.svc
        recorded = svc.platform if svc is not None and svc.platform_id else None
        if recorded is not None and str(recorded.pk) not in [v for v, _l in f.choices]:
            # commit-443: its own platform stays a valid value even when retired (or of another kind)
            f.choices = [
                *f.choices,
                (str(recorded.pk), f"{recorded.key} ({_('retired')})" if recorded.state == "retired" else recorded.key),
            ]
        if platform_fixed(svc):
            f.disabled = True
            f.help_text = _("A VM of it is on the platform: the platform cannot change.")
            if recorded is not None:  # commit-443: locked - its own platform is the one choice
                f.choices = [c for c in f.choices if c[0] == str(recorded.pk)]
        if svc is not None and HOST_PLATFORM not in (self.initial or {}) and svc.platform_id:
            self.initial[HOST_PLATFORM] = str(svc.platform_id)
        self.fields[HOST_PLATFORM] = f
        self.order_fields([HOST_PLATFORM])  # first, after the size

    def clean_host_platform(self):
        v = self.cleaned_data.get(HOST_PLATFORM) or ""
        svc = self.svc
        if svc is None or not svc.pk:
            return v
        recorded = str(svc.platform_id) if svc.platform_id else ""
        if platform_fixed(svc):  # Q7, commit-428: never moved by a post either - the recorded platform is kept
            return recorded
        default = self._default_platform()
        if (v or (str(default.pk) if default else "")) != recorded and svc.nodes.exists():
            # commit-428: a move is checked against the platforms as they are now - read again; when a read fails or
            # finds a VM of it (or a kept -old one), the recorded platform stays
            from .core_services import refresh_vms

            errors = refresh_vms(self.site)
            if errors:
                raise forms.ValidationError(
                    _("The site's platforms could not be read (%(e)s): the platform is kept.")
                    % {"e": ", ".join(k for k, _e in errors)}
                )
            if platform_fixed(svc):
                raise forms.ValidationError(_("A VM of it is on the platform: the platform cannot change."))
        return v

    def _default_platform(self):
        """commit-435: the platform the blank choice means - the one definition() takes (active platforms only)."""
        if self.stype == T.PORTAL_GW:
            return portal_platform(self.site)
        return mgmt_of(self.site)[0] if self.site else None

    def secret_fields(self):
        return [self[f] for f in self.fields if f.startswith(SECRET_FIELD)]

    def params(self):
        d = self.cleaned_data
        p = {k: d.get(k) for k in self.fields if k != "config_diff" and not k.startswith(SECRET_FIELD)}
        secrets = {k[len(SECRET_FIELD) :]: d.get(k) for k in self.fields if k.startswith(SECRET_FIELD) and d.get(k)}
        if secrets:
            p["secrets"] = secrets
        return p

    def size_fields(self):
        return [self[f] for f in self.SIZE_FIELDS if f in self.fields]


def _disk():
    return forms.IntegerField(label=_l("Disk, GB"), min_value=1, max_value=100000)


def _volumes(site):
    return [("", "—")] + [
        (str(v.pk), f"{v.storage.name} / {v.name} ({v.storage.get_kind_display()})")
        for v in StorageVolume.objects.filter(storage__site__code=site)
        .select_related("storage")
        .order_by("storage__name", "name")
    ]


class BgpRrForm(TypeForm):
    stype = T.BGP_RR
    network = forms.ChoiceField(
        label=_l("BGP network"), help_text=_l("An ix or evpn network of the site; one bgp-rr service per network.")
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        profile = self.profile
        taken = {
            str((s.params or {}).get("network"))
            for s in CoreService.objects.filter(site__code=self.site, stype=T.BGP_RR).exclude(
                pk=self.svc.pk if self.svc else None
            )
        }
        self.fields["network"].choices = [("", "—")] + [
            (str(n.pk), f"{n.name} ({n.kind}) · {n.cidr}")
            for n in Network.objects.filter(site=self.site, kind__in=[Network.Kind.IX, Network.Kind.EVPN]).order_by(
                "name"
            )
            if str(n.pk) not in taken
        ]
        if profile is not None:  # T34 S3c: only the networks whose route reflectors this profile makes
            nets = {str(n.pk): n for n in Network.objects.filter(site=self.site)}
            self.fields["network"].choices = [
                (k, v)
                for k, v in self.fields["network"].choices
                if not k or rr_role(nets[k]) == profile.install_template
            ]

    def params(self):
        p = super().params()
        p["network"] = int(p["network"]) if p.get("network") else None
        return p


class EgwForm(TypeForm):
    stype = T.CLOUD_EGW


class IgwForm(TypeForm):
    stype = T.CLOUD_IGW
    config_diff = forms.CharField(
        label=_l("VyOS configuration added"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 5, "class": "mono"}),
        help_text=_l("set/delete lines added to the standard igw configuration."),
    )


class PortalGwForm(TypeForm):
    """portal-gw: the size of both egws; the rest is calculated from the portal platform and its delegated subnets."""

    stype = T.PORTAL_GW


class GwForm(TypeForm):
    """cloud-gw: the size (both VMs), the igw's further addresses on core-services and the custom part of its
    configuration (e.g. destination NAT)."""

    stype = T.CLOUD_GW
    aliases = forms.CharField(
        label=_l("igw: further addresses on core-services"),
        required=False,
        widget=forms.TextInput(attrs={"class": "mono"}),
        help_text=_l(
            "Given to the igw by the platform as aliases of its core-services NIC, space "
            "separated, e.g. 198.18.93.3 198.18.93.4."
        ),
    )
    config_diff = forms.CharField(
        label=_l("Config addon (cloud-igw)"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 8, "class": "mono"}),
        help_text=_l(
            "set/delete lines added to the standard cloud-igw template (Infrastructure → "
            "Templates → cloud-igw): destination NAT, more addresses, routes … No "
            "secrets: they come from Secrets."
        ),
    )

    def clean_aliases(self):
        out = []
        for a in (self.cleaned_data.get("aliases") or "").replace(",", " ").split():
            try:
                out.append(str(ipaddress.ip_address(a)))
            except ValueError:
                raise forms.ValidationError(_("Not an IPv4 address: %(a)s") % {"a": a}) from None
        return out

    def clean_config_diff(self):
        import re

        text = self.cleaned_data.get("config_diff") or ""
        bad = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith(("set ", "delete ", "#"))]
        if bad:
            raise forms.ValidationError(_("Only set and delete lines: %(l)s") % {"l": bad[0][:80]})
        if any(re.search(r"(password|private-key|pre-shared-secret) ", ln) for ln in text.splitlines()):
            raise forms.ValidationError(_("No secrets here: they come from Secrets."))
        return text


class NsForm(TypeForm):
    stype = T.NS
    disk_gb = _disk()
    SIZE_FIELDS = ("vcpu", "ram_gb", "disk_gb")


class LogsForm(TypeForm):
    stype = T.LOGS
    disk_gb = _disk()
    volume = forms.ChoiceField(
        label=_l("Volume of the logs"), help_text=_l("Its storage type decides the data network of NIC 2.")
    )
    SIZE_FIELDS = ("vcpu", "ram_gb", "disk_gb")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["volume"].choices = _volumes(self.site)


class AdminForm(TypeForm):
    stype = T.ADMIN
    disk_gb = _disk()
    first_ip = forms.GenericIPAddressField(
        label=_l("First instance's address"),
        protocol="IPv4",
        help_text=_l(
            "On core-cloud; the next instances take the next addresses. Each must be "
            "one of the site's management hosts (site settings)."
        ),
    )
    data_volume = forms.ChoiceField(label=_l("Volume of the management data"))
    home_volume = forms.ChoiceField(label=_l("Volume of the admins' homes"), required=False, help_text=_l("Optional."))
    home_subdir = forms.RegexField(
        label=_l("Folder in it"), regex=r"^/?[A-Za-z0-9._/-]*$", required=False, help_text=_l("e.g. /admins/admin1")
    )
    SIZE_FIELDS = ("vcpu", "ram_gb", "disk_gb")  # owner 2026-11-23 (Q24): the number of VMs is the profile's

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["data_volume"].choices = self.fields["home_volume"].choices = _volumes(self.site)

    def clean(self):
        d = super().clean()
        first, n = d.get("first_ip"), admin_vms(self.svc)
        if first and n:
            bad = [
                str(ipaddress.ip_address(first) + i)
                for i in range(n)
                if not in_mgmt_hosts(self.site, str(ipaddress.ip_address(first) + i))
            ]
            if bad:
                self.add_error(
                    "first_ip",
                    _("Not among the site's management hosts (site settings): %(ips)s.") % {"ips": ", ".join(bad)},
                )
        return d


class BackupForm(TypeForm):
    stype = T.BACKUP
    disk_gb = _disk()
    datastore = forms.ChoiceField(
        label=_l("Backup datastore"),
        help_text=_l(
            "One backup service per datastore; its restic VM address, backend and secrets are the datastore's."
        ),
    )
    # T34 B4: the iSCSI LUN of the datastore's volume as the VM logs in to it (site → Storage keeps the portals and the
    # VM's initiator name; these are the instance's own): found and mounted, never formatted
    target_iqn = forms.RegexField(
        label=_l("iSCSI target IQN"), regex=r"^iqn\.[0-9]{4}-[0-9]{2}\.[A-Za-z0-9.:_-]+$", required=False
    )
    vg = forms.RegexField(
        label=_l("Volume group"), regex=r"^[A-Za-z0-9_.+-]+$", required=False, help_text=_l("e.g. vg_sat1onebkp1")
    )
    lv = forms.RegexField(
        label=_l("Logical volume"), regex=r"^[A-Za-z0-9_.+-]+$", required=False, help_text=_l("e.g. lv_sat1onebkp1")
    )
    SIZE_FIELDS = ("vcpu", "ram_gb", "disk_gb")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        taken = {
            str((s.params or {}).get("datastore"))
            for s in CoreService.objects.filter(site__code=self.site, stype=T.BACKUP).exclude(
                pk=self.svc.pk if self.svc else None
            )
        }
        self.fields["datastore"].choices = [("", "—")] + [
            (str(d.pk), f"{d.platform.key} / {d.name}" + (f" ({d.get_backend_display()})" if d.backend else ""))
            for d in PlatformDatastore.objects.filter(
                platform__site__code=self.site, kind=PlatformDatastore.Kind.BACKUPS
            )
            .select_related("platform")
            .order_by("platform__key", "name")
            if str(d.pk) not in taken
        ]

    def params(self):
        p = super().params()
        p["datastore"] = int(p["datastore"]) if p.get("datastore") else None
        return p


class ClaugineForm(TypeForm):
    """T36 rules v2 A6: the Claugine portal - one VM, one NIC on core-cloud."""

    stype = T.CLAUGINE
    disk_gb = _disk()
    address = forms.GenericIPAddressField(label=_l("Address"), protocol="IPv4", help_text=_l("On core-cloud."))
    SIZE_FIELDS = ("vcpu", "ram_gb", "disk_gb")


class OneFeForm(TypeForm):
    """T34 B4 (owner 13:15): the platform served; its three nodes and their addresses come from the platform."""

    stype = T.ONEFE
    disk_gb = _disk()
    platform = forms.ChoiceField(
        label=_l("Platform served"),
        help_text=_l("A platform whose front end runs in VMs (Platforms); one onefe service per platform."),
    )
    SIZE_FIELDS = ("vcpu", "ram_gb", "disk_gb")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        taken = {
            str((s.params or {}).get("platform"))
            for s in CoreService.objects.filter(site__code=self.site, stype=T.ONEFE).exclude(
                pk=self.svc.pk if self.svc else None
            )
        }
        self.fields["platform"].choices = [("", "—")] + [
            (str(p.pk), f"{p.key} ({p.kind})")
            for p in Platform.objects.filter(site__code=self.site).order_by("key")
            if (p.fe or {}).get("type") == "vm"
            and str(p.pk) not in taken
            and (p.kind not in HOSTING or str(p.pk) == str(self.initial.get("platform") or ""))  # N2
        ]

    def params(self):
        p = super().params()
        p["platform"] = int(p["platform"]) if p.get("platform") else None
        return p


FORMS = {
    T.BGP_RR: BgpRrForm,
    T.CLOUD_EGW: EgwForm,
    T.CLOUD_IGW: IgwForm,
    T.CLOUD_GW: GwForm,
    T.PORTAL_GW: PortalGwForm,
    T.NS: NsForm,
    T.LOGS: LogsForm,
    T.ADMIN: AdminForm,
    T.BACKUP: BackupForm,
    T.ONEFE: OneFeForm,
    T.CLAUGINE: ClaugineForm,
}


def initial(svc):
    p = dict(svc.params or {})
    for name, ref in (p.pop("secrets", None) or {}).items():  # T34 B4: one field per secret
        p[SECRET_FIELD + name] = ref
    p.update(vcpu=svc.vcpu, ram_gb=svc.ram_gb, disk_gb=svc.disk_gb, config_diff=svc.config_diff)
    if isinstance(p.get("aliases"), list):
        p["aliases"] = " ".join(p["aliases"])
    return {
        k: (
            str(v)
            if k in ("network", "datastore", "volume", "data_volume", "home_volume", "platform", HOST_PLATFORM) and v
            else v
        )
        for k, v in p.items()
    }


def default_profile(stype, d):
    """The service profile a service of the type is made from (T34 S3b): a VyOS service's by the VyOS template its
    definition calculates (rr-ix, rr-vxlan, igw, egw) - None when no profile names it (commit-411,
    420); any other the type's first."""
    from .models import ServiceProfile

    qs = ServiceProfile.objects.filter(stype=stype).order_by("pk")
    if stype in VYOS_TYPES:
        return ServiceProfile.of_type(stype, qs.filter(install_template=d.get("template") or ""))
    return ServiceProfile.of_type(stype, qs)


def fits(prof, stype, d):
    """The linked profile still fits: its type, and for a VyOS service the template its definition calculates
    (commit-420: a route reflector its network's, cloud-gw igw, portal-gw egw)."""
    if prof is None or prof.stype != stype:
        return False
    return stype not in VYOS_TYPES or prof.install_template == (d.get("template") or "")


def available(site):
    """The types a new service can have on the site: [(type, label, available)] (the one-per-site ones once)."""
    have = set(CoreService.objects.filter(site__code=site, stype__in=SINGLE).values_list("stype", flat=True))
    return [(t, label, t not in have) for t, label in T.choices if t in TYPED]  # core-vm: no form defined yet


def check_minimum(stype, d):
    """commit-479 (rules v2 C3): a service VM is never sized below its OS's minimum (Profiles → OS) - checked on the
    size it is saved with (the form's, else its profile's or type's, else its existing VMs')."""
    from .models import OsProfile

    if d.get("kind") == "host":
        return
    o = OsProfile.objects.filter(key=OsProfile.VYOS if stype in VYOS_TYPES else OsProfile.BASE_OS).first()
    below = o.below_minimum(d.get("vcpu"), d.get("ram_gb"), d.get("disk_gb")) if o is not None else []
    if below:
        names = {"vcpu": "vCPU", "ram_gb": _("RAM, GB"), "disk_gb": _("disk, GB")}
        raise DefinitionError(
            _("Below the minimum of %(os)s (Profiles → OS): %(what)s.")
            % {"os": o.key, "what": ", ".join(f"{names[f]} {m}" for f, m in below)}
        )


def save(site, stype, form, svc=None, user=None, profile=None):
    """Calculate the definition from the form and write it; a new service is named by its type. Returns the service.
    commit-514: a new name server is checked and written under its site row's lock. commit-572: every save takes the
    profiles' lock (ServiceProfile.install_lock) first - its VMs' roles are read from the profile as a profile edit
    leaves it."""
    from django.db import transaction

    from .models import ServiceProfile

    with transaction.atomic():
        ServiceProfile.install_lock()
        if profile is not None:  # commit-572: the profile as stored now (a role edit before the lock is seen)
            pick = profile
            profile = ServiceProfile.objects.filter(pk=pick.pk).first()
            if profile is None:
                raise DefinitionError(_("The service profile %(p)s was deleted: choose another.") % {"p": pick.key})
        return _save_locked(site, stype, form, svc, user, profile)


def _save_locked(site, stype, form, svc=None, user=None, profile=None):
    if svc is None and stype == T.NS:
        from django.db import transaction

        from .models import ServiceProfile, Site

        with transaction.atomic():
            Site.objects.select_for_update().filter(code=site).first()
            # commit-518: the profile as stored now, locked until the service is written (a profile edit waits)
            pick = profile if profile is not None else profile_for(stype, {}, None)
            if pick is not None:
                profile = ServiceProfile.objects.select_for_update().filter(pk=pick.pk).first()
                if profile is None:  # commit-519: deleted since it was chosen
                    raise DefinitionError(_("The service profile %(p)s was deleted: choose another.") % {"p": pick.key})
            return _save(site, stype, form, svc, user, profile)
    return _save(site, stype, form, svc, user, profile)


def _save(site, stype, form, svc=None, user=None, profile=None):
    from accounts.models import audit

    from .models import Site

    p = form.params()
    if stype == T.NS and profile is None:  # commit-513: one profile for the check, the definition and the link
        profile = profile_for(stype, {}, svc)
    if svc is None and stype == T.NS:  # commit-507: the profile's DNS slot fits the site and is free - asked first
        why = ns_why(site, profile) if profile is not None else str(_("no name server profile"))
        if why:
            raise DefinitionError(_("Not a name server here: %(w)s.") % {"w": why})
    d = definition(site, stype, p, existing=svc, profile=profile)
    check_minimum(stype, d)
    if svc is None:
        if stype in SINGLE and CoreService.objects.filter(site__code=site, stype=stype).exists():
            raise DefinitionError(_("%(s)s already has its %(t)s service.") % {"s": site, "t": stype})
        if stype == T.NS and CoreService.objects.filter(site__code=site, stype=stype).count() >= ns_limit(site):
            raise DefinitionError(  # T36 B3b
                _("%(s)s has its %(n)s name server service(s) (the primary site two, every other site one).")
                % {"s": site, "n": ns_limit(site)}
            )
        if CoreService.objects.filter(site__code=site, name=d["name"]).exists():
            raise DefinitionError(_("%(s)s already has a service named %(n)s.") % {"s": site, "n": d["name"]})
        # T36 B3a: anything added starts planned; each VM is deployed by its Redeploy
        svc = CoreService(site=Site.objects.get(code=site), name=d["name"], state=CoreService.State.PLANNED)
    svc.params = p
    if not svc.description:
        svc.description = dict(T.choices)[stype].split(": ", 1)[-1]
    if stype in (T.CLOUD_IGW, T.CLOUD_GW):
        svc.config_diff = form.cleaned_data.get("config_diff") or ""
    new = svc.pk is None
    if not new:  # commit-415: a VM beyond the definition (a lower profile count) is removed first, not forgotten
        _no_running_surplus(svc, d)
    if profile is not None:  # T34 S3c: chosen on the Add service page - it must fit what the form defines
        if not fits(profile, stype, d):
            raise DefinitionError(
                _("The service profile %(p)s does not make this service (%(t)s, template %(r)s).")
                % {"p": profile.key, "t": stype, "r": d.get("template") or "—"}
            )
        svc.profile = profile
    elif not (svc.profile_id and fits(svc.profile, stype, d)):  # T34 S3b, commit-411: the profile it is made from
        prof = default_profile(stype, d)
        if prof is None and stype in VYOS_TYPES:
            raise DefinitionError(
                _("No service profile of %(t)s names the template %(r)s (Profiles → Services).")
                % {"t": stype, "r": d.get("template") or "—"}
            )
        svc.profile = prof
    apply(svc, d, user)
    audit(user, "core_service_saved", svc, site=site, service=svc.name, type=stype, nodes=len(d["nodes"]), new=new)
    return svc
