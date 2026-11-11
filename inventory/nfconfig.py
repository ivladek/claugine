"""Configurations of network functions built from Infrastructure → Templates.

    build(role, tenant=t)                      egw, igw, cgw of a tenant
    build("l2-a", tenant=t, channel=l2)        one side of an L2 channel
    build("rr-inet", site="dc1", index=0)      route reflectors and host FRR of a site

Secrets (BGP passwords, password hashes) are Secret objects: masked ``«secret:…»`` unless ``reveal=True``.
Values that are only known after deployment (an egw address on the exchange network) show as ``«…»``.
"""
import ipaddress
import re
import zlib

import yaml
from django.template import Context, Engine

from .models import ConfigTemplate, Platform
from . import nf_templates

# a site's management ports (Infrastructure → Sites → management): VyOS VMs, core services, RDP of the jump hosts;
# the installation's own values are kept per site, these are only the standard ones when a site has none
MGMT_PORTS = {"vyos": 22, "ssh": 22, "rdp": 3389}
# the L2 tunnel between the src and dst gateways: VXLAN inside WireGuard over cloud-s2s
WG_PORT, WG_OVERHEAD, VXLAN_OVERHEAD = 51820, 80, 50
WG_INNER = {"a": "169.254.0.1", "b": "169.254.0.2"}  # /30 inside each channel's own tunnel (a = dst, b = src)


def tunnel_keys():
    """Fresh WireGuard keys for both ends of an L2 tunnel and a random pre-shared key ("password"). They go into the
    two gateways' first-boot configuration only: the portal does not keep them."""
    import base64
    import os
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    raw = serialization.Encoding.Raw
    out = {"psk": base64.b64encode(os.urandom(32)).decode()}
    for side in ("a", "b"):
        k = X25519PrivateKey.generate()
        out[f"{side}_private"] = base64.b64encode(k.private_bytes(raw, serialization.PrivateFormat.Raw,
                                                                  serialization.NoEncryption())).decode()
        out[f"{side}_public"] = base64.b64encode(k.public_key().public_bytes(raw, serialization.PublicFormat.Raw)).decode()
    return out
MGMT_NETWORK = "cloud-vyos"  # the site network of the management interfaces (eth0, VRF MGMT) of provider routers
SERVICE_USER = "claugine"
from .models import LEGACY_TZ as TIMEZONES  # noqa: E402 - former city names; use Site.tz
MARTIANS = ["0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
            "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
            "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4", "255.255.255.255/32"]
SEGMENTS = {"rr-inet": "cloud-ix", "rr-corpnet": "corpnet-ix", "rr-vxlan": "cloud-vxlan", "host-frr": "cloud-vxlan"}
ENGINE = Engine(autoescape=False)


class BuildError(Exception):
    pass


class Secret:
    """A secret in a configuration: masked by default."""

    def __init__(self, ref, reveal=False, value=None, label=None):
        self.ref, self.reveal, self._value, self.label = ref, reveal, value, label

    def value(self):
        if self._value is None and self.ref:
            from .credentials import value
            self._value = value(self.ref) or ""
        return self._value or ""

    def __str__(self):
        if self.reveal:
            v = self.value()
            return v if v else f"«missing:{self.ref}»"
        if not self.label and self.ref:
            from .credentials import describe
            self.label = describe(self.ref)
        return f"«secret:{self.label or ''}»"

    def __bool__(self):
        return bool(self.ref) or bool(self._value) or bool(self.label)


class Unknown(str):
    """A value decided at deployment."""

    def __new__(cls, what):
        return super().__new__(cls, f"«{what}»")


def _files():
    """Nothing: the portal's records are the source (kept so the helpers keep one shape)."""
    return {}


def _y(files, path):
    try:
        d = yaml.safe_load(files.get(path) or "") or {}
    except yaml.YAMLError:
        d = {}
    return d if isinstance(d, dict) else {}


def _segment(files, site, seg):
    """A site network from Infrastructure → Sites → Networks (the portal keeps them)."""
    from .networks import segment
    d = segment(site, seg)
    if d is None:
        raise BuildError(f"network {seg} of {site} not found (Infrastructure → Sites → {site} → Networks)")
    return d


def _site(files, site):
    from .models import Site
    s = Site.objects.filter(code=site).first()
    from .models import DEFAULT_TZ
    return s, (s.tz if s else DEFAULT_TZ)


def _services(files, site, platform):
    from .networks import services
    return services(site, platform)


def _admin(files, site, platform, reveal):
    """The platform's super admin (Platform.sa): name, password hash (secret) and public key."""
    from .credentials import _public_key, account, find
    p = Platform.objects.filter(key=f"{site}.{platform}").first()
    ref = p.sa if p else ""
    if not ref:
        return []
    name, acc = account(ref)
    if not name:
        return []
    sec = find(ref)
    key = (sec.public_key if sec is not None else "") or ""
    if not key and acc.get("key"):
        k = acc["key"]
        key = _public_key(k) if "PRIVATE KEY" in k else k
    key_type, _sp, key_b64 = key.partition(" ")
    pw = acc.get("password") or ""
    if reveal and pw:
        from tenants.deploy import crypt_sha512
        pw_hash = crypt_sha512(pw)
    else:
        from .credentials import describe
        pw_hash = Secret(None, label=f"sha512 of the password of {describe(ref)}") if pw else ""
    return [{"name": name, "password_hash": pw_hash, "key_type": key_type, "key": key_b64.split(" ")[0]}]


def _jumps():
    """The management hosts of every site (the jump hosts are among them): they reach the routers over ssh."""
    from .models import Site
    out = []
    for site in Site.objects.all():
        for x in (site.management or {}).get("hosts") or []:
            x = x if "/" in x else f"{x}/32"
            if x not in out:
                out.append(x)
    return out


def _bgp_password(seg, group, reveal):
    ref = ((seg["ibgp"].get(group) or seg["ebgp"].get(group) or {}).get("password")) or ""
    return Secret(ref, reveal) if ref else ""


def portal_addresses():
    """Where the portal connects from: settings.PORTAL_MGMT_IPS, else the source address of its default route."""
    from django.conf import settings
    ips = list(getattr(settings, "PORTAL_MGMT_IPS", []) or [])
    if not ips:
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
                sk.connect(("10.255.255.254", 9))
                ip = sk.getsockname()[0]
            if not ip.startswith("127."):
                ips = [ip]
        except OSError:
            pass
    return [x if "/" in x else f"{x}/32" for x in ips]


def service_user():
    """The portal's service user on tenant routers (igw): name and public key, for reading their configuration."""
    from .credentials import service_key
    c = service_key()
    t, _sp, rest = (c.public_key or "").partition(" ")
    return {"name": SERVICE_USER, "type": t, "key": rest.split(" ")[0]} if c.public_key else None


def segment_ssh_from(cidr):
    """SSH sources for a router on a segment: the whole segment network except its first /29."""
    try:
        net = ipaddress.ip_network(str(cidr), strict=False)
    except ValueError:
        return []
    if net.version != 4 or net.prefixlen >= 29:
        return [str(net)]
    first = ipaddress.ip_network(f"{net.network_address}/29")
    return [str(n) for n in sorted(net.address_exclude(first))]



def _igw_port_of(group):
    """The ssh port of an igw that has none recorded: fixed per group, within the range of Settings → Data."""
    from tenants.models import Tenant
    a, b = Tenant.igw_ports()
    return a + zlib.crc32(group.encode()) % (b - a + 1)

def management(site):
    """(hosts as prefixes, ports {vyos, ssh, rdp}) of a site: who may manage its systems, and on which ports."""
    from .models import Site
    s = Site.objects.filter(code=site).first()
    m = (s.management if s else None) or {}
    hosts = [x if "/" in x else f"{x}/32" for x in (m.get("hosts") or [])]
    ports = {**MGMT_PORTS, **{k: int(v) for k, v in (m.get("ports") or {}).items() if str(v).isdigit()}}
    return hosts, ports


def mgmt_context(site, ip=None):
    """eth0 of a provider router: its address on the site's cloud-vyos network, in VRF MGMT."""
    from .networks import get
    n = get(site, MGMT_NETWORK)
    if n is None:
        return {"iface": "eth0", "ip": ip or Unknown(f"address on {MGMT_NETWORK}_{site}"), "prefix_len": "",
                "gw": "", "net_name": f"{MGMT_NETWORK}_{site}"}
    return {"iface": "eth0", "ip": ip or Unknown(f"address on {MGMT_NETWORK}_{site}"), "prefix_len": n.prefix,
            "gw": n.gateway or "", "net_name": f"{MGMT_NETWORK}_{site}", "cidr": n.cidr}


def syslog_target(s):
    """The site's VyOS log collector with its address as an IP: a router resolves names in its default VRF, which
    has no route to the name servers (they are in VRF MGMT), so it gets no name servers and no names. The portal
    resolves the name each time a configuration is built; not resolvable: a value missing (the build is incomplete)."""
    import ipaddress
    import socket
    c = dict(((s.log_collectors or {}).get("vyos") if s else None) or {})
    host = (c.get("host") or "").strip()
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            c["name"], c["host"] = host, socket.gethostbyname(host)
        except OSError:
            c["name"], c["host"] = host, Unknown(f"IP of the log collector {host}")
    return c


def _common(files, site, platform, reveal, mgmt_ip=None):
    s, tz = _site(files, site)
    services, services_dns = _services(files, site, platform)
    hosts, ports = management(site)
    return {"site": site, "platform": platform, "time_zone": tz, "ntp": (s.ntp_servers if s else []) or [],
            "dns": ((s.dns_servers if s else []) or services_dns), "ssh_port": ports["vyos"], "ssh_password": False,
            "ssh_from": [x for x in [services] + _jumps() + portal_addresses() if x],
            "mgmt_hosts": hosts, "mgmt": mgmt_context(site, mgmt_ip),
            "syslog": syslog_target(s),
            "users": _admin(files, site, platform, reveal),
            "martians": MARTIANS}


# --- tenants --------------------------------------------------------------------------------

def _tenant_names(tenant):
    from tenants.naming import GROUP_RE
    m = GROUP_RE.match(tenant.group_name or "")
    return (m.group(1), m.group(2)) if m else ("org", tenant.name or "tenant")


def _names(tenant, key):
    """{prefix: OpenNebula network name} of the tenant's networks recorded by deployments and network changes."""
    dep = tenant.deployments.exclude(state="plan").first()
    rows = ((dep.resources or {}) if dep else {}).get(key) or []
    return {r.get("prefix"): r.get("name") for r in rows if r.get("prefix") and r.get("name")}


def pub_names(tenant, site):
    return _names(tenant, "public_networks")


def corp_names(tenant, site):
    return _names(tenant, "corp_networks")


def tenant_context(role, tenant, reveal=False, channel=None, res=None):
    from tenants.models import PrefixAssignment
    o = tenant.order or {}
    platform_key = o.get("platform") or ""
    site, _sp, platform = platform_key.partition(".")
    if not site:
        raise BuildError("the project has no platform yet")
    files = _files()
    org, ten = _tenant_names(tenant)
    from tenants.deploy import object_base
    group = object_base(tenant)
    dep0 = tenant.deployments.exclude(state="plan").first()
    res0 = {**((dep0.resources or {}) if dep0 else {}), **(res or {})}
    ctx = _common(files, site, platform, reveal, mgmt_ip=res0.get("mgmt_ip") or res0.get(f"{role}_mgmt_ip"))
    from tenants.deploy import object_base
    group = object_base(tenant)
    dep = tenant.deployments.exclude(state="plan").first()
    res = {**((dep.resources or {}) if dep else {}), **(res or {})}
    skip = set(res.get("without") or [])  # a network change: the configuration without these prefixes
    prefixes = [p for p in PrefixAssignment.active().filter(tenant=tenant, site=site) if p.cidr not in skip]
    main = next((p for p in prefixes if p.kind == "inet_main"), None)
    if role in ("egw", "igw"):
        if main is None:
            raise BuildError("no main public prefix is assigned")
        net = ipaddress.ip_network(main.cidr)
        hosts = list(net) if net.prefixlen >= 31 else list(net.hosts())
        egw_ip, igw_ip = str(hosts[0]), str(hosts[1])
        main_name = f"{group}-main_{site}"
        pubs = [p for p in prefixes if p.kind == "inet_extra"]
        if role == "egw":
            seg = _segment(files, site, _ix_of(site, platform, main.cidr))
            ctx.update(host_name=f"r{site}egw-{org}-{ten}", client_networks=[p.cidr for p in [main] + pubs],
                       routed_networks=[p.cidr for p in pubs], igw_ip=igw_ip,
                       gw={"ip": egw_ip, "prefix_len": net.prefixlen, "net_name": main_name},
                       mbps=_mbps(tenant, site),
                       ix={"ip": res.get("egw_ix_ip") or Unknown("egw address on " + seg["name"]),
                           "prefix_len": seg["prefix_len"], "cidr": seg["cidr"], "name": seg["name"], "rr_net": seg["rr_net"],
                           "rrs": seg["rrs"], "asn": seg["asn"], "bgp_password": _bgp_password(seg, "egw", reveal)})
        else:
            lan = []
            ifaces = res.get("igw_ifaces") or {}  # network name -> interface on the igw (from its NICs)
            names = pub_names(tenant, site)
            for i, p in enumerate(pubs, start=1):
                pn = ipaddress.ip_network(p.cidr)
                gw = str(next(pn.hosts())) if pn.prefixlen < 31 else str(pn[0])
                name = names.get(p.cidr) or f"{group}-pub{i}_{site}"
                lan.append({"iface": ifaces.get(name) or f"eth{i}", "cidr": p.cidr, "gw": gw, "prefix_len": pn.prefixlen,
                            "zone": "PUB", "net_name": name})
            ctx.update(host_name=f"r{site}igw-{org}-{ten}", lan_networks=lan, int_networks=[n for n in lan if n["zone"] == "INT"],
                       pub_networks=[n for n in lan if n["zone"] == "PUB"],
                       ext={"ip": igw_ip, "prefix_len": net.prefixlen, "net_name": main_name, "gateway": egw_ip},
                       ssh_port=tenant.igw_ssh_port or _igw_port_of(group), igw_ssh_from=tenant.igw_ssh_from_list(),
                       users=[{"name": tenant.igw_username or f"{ten}-igw-admin", "password_hash": "",
                               "key_type": (tenant.igw_ssh_key or "").split(" ")[0],
                               "key": ((tenant.igw_ssh_key or "").split(" ") + [""])[1]}])
            ctx["ssh_listen"] = [igw_ip] + [n["gw"] for n in lan]
            ctx["igw_user"] = tenant.igw_username or f"{ten}-igw-admin"
            ctx["service_user"] = service_user()
            ctx["igw_keys"] = [{"name": k.name, "key": k.parts[1], "type": k.key_type} for k in tenant.ssh_keys.filter(on_igw=True)]
    elif role == "cgw":
        corp = [p for p in prefixes if p.kind == "corpnet"]
        if not corp:
            raise BuildError("no corporate prefix is assigned")
        first = ipaddress.ip_network(corp[0].cidr)
        hosts = list(first) if first.prefixlen >= 31 else list(first.hosts())
        seg = _segment(files, site, _ix_of(site, platform, corp[0].cidr, purpose="corpnet"))
        ifaces = res.get("cgw_ifaces") or {}
        names = corp_names(tenant, site)
        corp_networks = []
        for i, p in enumerate(corp, start=1):
            pn = ipaddress.ip_network(p.cidr)
            hs = list(pn) if pn.prefixlen >= 31 else list(pn.hosts())
            name = names.get(p.cidr) or (f"{group}-corp_{site}" if i == 1 else f"{group}-corp{i}_{site}")
            corp_networks.append({"iface": ifaces.get(name) or f"eth{i + 1}", "cidr": p.cidr, "gw": str(hs[0]),
                                  "prefix_len": pn.prefixlen, "net_name": name})
        ctx["corp_networks"] = corp_networks
        ctx.update(host_name=f"r{site}cgw-{org}-{ten}", client_networks=[p.cidr for p in corp],
                   gw={"ip": str(hosts[0]), "prefix_len": first.prefixlen, "net_name": f"{group}-corp_{site}"},
                   ix={"ip": res.get("cgw_ix_ip") or Unknown("cgw address on " + seg["name"]), "prefix_len": seg["prefix_len"],
                       "cidr": seg["cidr"], "name": seg["name"], "rr_net": seg["rr_net"], "rrs": seg["rrs"], "asn": seg["asn"],
                       "bgp_password": _bgp_password(seg, "cgw", reveal)})
    elif role in ("l2-a", "l2-b"):
        if channel is None:
            raise BuildError("choose an L2 channel")
        seg = _segment(files, site, "cloud-s2s")
        a, b = channel.addresses()
        local, remote = (a, b) if role == "l2-a" else (b, a)
        vni = channel.vni or Unknown("VNI")
        s2s_mtu = seg["mtu"] or 1500
        wg_mtu = s2s_mtu - WG_OVERHEAD
        keys = getattr(channel, "tunnel_keys", None) or {}  # made when the gateways are made; never kept
        me, peer = ("a", "b") if role == "l2-a" else ("b", "a")
        ctx.update(host_name=f"r{site}s2s-{org}-{ten}-{channel.pk}{'a' if role == 'l2-a' else 'b'}", vni=vni, mtu=1500,
                   vxlan_mtu=min(1550, wg_mtu - VXLAN_OVERHEAD),
                   s2s={"ip": local or Unknown("address on " + seg["name"]), "remote": remote or Unknown("other side"),
                        "prefix_len": seg["prefix_len"], "name": seg["name"], "mtu": seg["mtu"]},
                   wg={"local": WG_INNER[me], "remote": WG_INNER[peer], "mtu": wg_mtu, "port": WG_PORT,
                       "peer": "dst" if role == "l2-b" else "src",
                       "private_key": keys.get(f"{me}_private") or Unknown("random key, made with the gateways"),
                       "peer_public_key": keys.get(f"{peer}_public") or Unknown("random key, made with the gateways"),
                       "preshared_key": keys.get("psk") or Unknown("random password, made with the gateways")},
                   side_label="TENANT" if role == "l2-a" else "EXT",
                   attached=channel.tenant_network if role == "l2-a" else (channel.vlan_name or Unknown("VLAN set at approval")))
    elif role == "l2-br":
        if channel is None:
            raise BuildError("choose an L2 channel")
        ctx.update(host_name=f"r{site}l2br-{org}-{ten}-{channel.pk}", vni=channel.vni or Unknown("VNI"), mtu=1500,
                   vlan=channel.vlan_name or Unknown("VLAN set at approval"), attached=channel.tenant_network)
    return ctx


def _mbps(tenant, site):
    c = next((c for c in tenant.current_connectivity() if c.site == site), None)
    return (c.inet_mbps if c else None) or (tenant.order or {}).get("inet_mbps") or 100


def _ix_of(site, platform, cidr, purpose="inet"):
    from .models import DelegatedSubnet, Platform
    p = Platform.objects.filter(key=f"{site}.{platform}").first()
    net = ipaddress.ip_network(cidr)
    for d in DelegatedSubnet.objects.filter(site__code=site, segment__in=(p.segments if p else []) or []):
        pool = ipaddress.ip_network(d.cidr)
        if net.version == pool.version and net.subnet_of(pool):
            return d.segment
    return "corpnet-ix" if purpose == "corpnet" else "cloud-ix"


# --- infrastructure (site services) --------------------------------------------------------

def site_context(role, site, index=0, reveal=False, platform="cloud", segment=None, mgmt_ip=None, host_name=None):
    """A site service's configuration (route reflectors, host FRR); `segment`: another network than the role's usual
    one (e.g. portal-ix for the ext-managed platform's internet exchange), `mgmt_ip`: its address on cloud-vyos."""
    files = _files()
    seg = _segment(files, site, segment or SEGMENTS[role])
    ctx = _common(files, site, platform, reveal, mgmt_ip=mgmt_ip)
    rrs = seg["rrs"]
    me = rrs[index] if index < len(rrs) else Unknown("RR address")
    pw = {g: _bgp_password(seg, g, reveal) for g in ("ext", "rrs", "egw", "cgw", "hosts")}
    int_group = {"rr-inet": "egw", "rr-corpnet": "cgw"}.get(role, "hosts")
    hosts = lambda g: [str(x) for x in ((seg["ibgp"].get(g) or seg["ebgp"].get(g) or {}).get("hosts") or [])]  # noqa: E731
    label, pg, title = {"rr-inet": ("CLOUD", "cloud", "Cloud"), "rr-corpnet": ("CORPNET", "corpnet", "CorpNet")}.get(
        role, ("CLOUD", "cloud", "Cloud"))
    if role == "rr-inet" and seg["segment"].startswith("portal-"):  # the ext-managed platform's exchange: its own names
        label, pg, title = "PORTAL", "portal", "Portal"
    ctx.update(label=label, pg=pg, title=title, remote_asn=seg["remote_asn"],
               ext_hosts=hosts("ext"), int_hosts=hosts(int_group), rr_hosts=hosts("rrs"),
               other_rrs=[r for r in rrs if r != me],
               passwords={"ext": pw["ext"], "int": pw[int_group], "rrs": pw["rrs"]},
               ix={"ip": me, "prefix_len": seg["prefix_len"], "cidr": seg["cidr"], "name": seg["name"], "rr_net": seg["rr_net"],
                   "rrs": rrs, "asn": seg["asn"], "mtu": seg["mtu"]},
               host_name=host_name or (f"r{site}-{seg['segment'].replace('-', '')}-rr{index + 1}" if role != "host-frr"
                                       else "${HOSTNAME}"))
    if role == "host-frr":
        ctx["ix"]["ip"] = Unknown("host address on " + seg["name"])
    return ctx


def build(role, tenant=None, site=None, index=0, reveal=False, channel=None, res=None, segment=None, mgmt_ip=None,
          platform="cloud", host_name=None):
    tpl = ConfigTemplate.objects.filter(role=role).first()
    if tpl is None:
        raise BuildError(f"no template for {role}")
    ctx = (tenant_context(role, tenant, reveal, channel, res) if tenant is not None
           else site_context(role, site, index, reveal, platform=platform, segment=segment, mgmt_ip=mgmt_ip,
                             host_name=host_name))
    text = _render(tpl.body, ctx)
    if role == "igw" and tenant is not None:
        text = igw_password_login(text, ctx)
    return text


PASSWORD_LOGIN = "delete service ssh disable-password-authentication"


def igw_password_login(text, ctx):
    """A tenant igw without an ssh key lets its user in over ssh with the password (owner 2026-10-29 12:22): the VyOS
    image always sets disable-password-authentication, so the configuration deletes it — whatever the stored template
    says. With a key the image's key-only ssh stays."""
    if ctx.get("igw_keys") or PASSWORD_LOGIN in text:
        return text
    lines = text.splitlines()
    i = next((n for n, line in enumerate(lines) if line.startswith("set system time-zone")), len(lines))
    lines.insert(i, PASSWORD_LOGIN)
    return "\n".join(lines) + "\n"


def _render(body, ctx):
    text = ENGINE.from_string(body).render(Context(ctx, autoescape=False))  # a configuration, not HTML: "&" stays "&"
    return "\n".join(line.rstrip() for line in text.splitlines() if line.strip()) + "\n"


# --- the site's own gateways (core services cloud-egw, cloud-igw): the standard template plus their differences -----

GATEWAY_ROLES = ("egw", "igw")
# A site igw is the tenant igw template plus the management part; ssh is in VRF MGMT only (2026-10-27: "SSH connections
# allowed only on mgmt vrf, service network only for service"), so the tenant igw's ssh on EXT is left out.
SITE_IGW_NO_SSH = re.compile(r"SSHFROM|port-group SSHPORT description \"GW SSH port\"|name EXT-to-GW rule 10 |"
                             r"name EXT-to-GW description |service ssh listen-address")
# The management part every provider router has (eth0 in VRF MGMT); the tenants' igw template has none of it, so a
# site igw gets it from here.
MGMT_PART = nf_templates.CLOUD_IGW_MGMT  # in the cloud-igw template (correction 12)


def _vm_ips(node):
    """{network (without the site): [addresses]} of a node: its definition (address per NIC) first, then what the
    platform's last read says."""
    from .core_services import short, site_vms
    site = node.service.site.code
    out = {}
    for (net, _gw), ip in zip(node.nic_list(), node.ips or []):
        if net is not None and ip:
            out.setdefault(net.name, []).append(ip)
    vm = (node.facts or {}).get("vm") or ((site_vms(site).get(node.name) or (None, None))[1]) or {}
    for net, ip in vm.get("nics") or []:
        if ip and short(net, site) not in out:
            out.setdefault(short(net, site), []).append(ip)
    return out


def gateway_context(role, node, reveal=False, mgmt_ip=None):
    """The context of the tenants' egw/igw template for a site gateway (core services cloud-egw, cloud-igw).

    egw: eth0 management, eth1 cloud-ix (BGP to the route reflectors), eth2 cloud-services (its public subnet as the
    client side). igw: eth0 management, eth1 cloud-services (EXT, default route to the egw), eth2 cloud-core (INT,
    masqueraded)."""
    from .networks import get
    svc = node.service
    site = svc.site.code
    files = _files()
    ctx = _common(files, site, svc.platform.name if svc.stype == "portal-gw" and svc.platform else "cloud", reveal,
                  mgmt_ip=mgmt_ip)
    ips = _vm_ips(node)
    if svc.stype == "portal-gw":  # correction 12: eth1 portal-ix, eth2 … the public overlays (VRRP: portal_vrrp)
        if role != "egw":
            raise BuildError(f"{role} is not a portal-gw role")
        publics = (node.params or {}).get("publics") or []
        if not publics:
            raise BuildError("portal-gw: no public overlay (the portal platform's delegated subnets)")
        seg = _segment(files, site, "portal-ix")
        first = publics[0]
        ctx.update(host_name=node.hostname or f"r{site}{role}", client_networks=[p["cidr"] for p in publics],
                   routed_networks=[], igw_ip="", mbps=None,
                   gw={"ip": (ips.get(first["name"]) or [Unknown(f"egw address on {first['name']}")])[0],
                       "prefix_len": first["prefix"], "net_name": f"{first['name']}_{site}"},
                   ix={"ip": (ips.get("portal-ix") or [Unknown("egw address on portal-ix")])[0],
                       "prefix_len": seg["prefix_len"], "cidr": seg["cidr"], "name": seg["name"], "rr_net": seg["rr_net"],
                       "rrs": seg["rrs"], "asn": seg["asn"], "bgp_password": _bgp_password(seg, "egw", reveal)})
        ctx["ssh_listen"] = []
        return ctx
    srv = get(site, "cloud-services")
    if srv is None:
        raise BuildError(f"network cloud-services of {site} not found (Infrastructure → Sites → {site} → Networks)")
    host = node.hostname or f"r{site}{role}"
    if role == "egw":
        seg = _segment(files, site, "cloud-ix")
        ctx.update(host_name=host, client_networks=[srv.cidr], routed_networks=[], igw_ip="",
                   gw={"ip": (ips.get("cloud-services") or [Unknown("egw address on cloud-services")])[0],
                       "prefix_len": srv.prefix, "net_name": f"cloud-services_{site}"},
                   mbps=None,
                   ix={"ip": (ips.get("cloud-ix") or [Unknown("egw address on cloud-ix")])[0],
                       "prefix_len": seg["prefix_len"], "cidr": seg["cidr"], "name": seg["name"], "rr_net": seg["rr_net"],
                       "rrs": seg["rrs"], "asn": seg["asn"], "bgp_password": _bgp_password(seg, "egw", reveal)})
    elif role == "igw":
        core = get(site, "cloud-core")
        if core is None:
            raise BuildError(f"network cloud-core of {site} not found")
        lan = [{"iface": "eth2", "cidr": core.cidr, "gw": (ips.get("cloud-core") or [Unknown("igw address on cloud-core")])[0],
                "prefix_len": core.prefix, "zone": "INT", "net_name": f"cloud-core_{site}"}]
        ctx.update(host_name=host, lan_networks=lan, int_networks=lan, pub_networks=[],
                   ext={"iface": "eth1", "ip": (ips.get("cloud-services") or [Unknown("igw address on cloud-services")])[0],
                        "prefix_len": srv.prefix, "net_name": f"cloud-services_{site}", "gateway": srv.gateway or ""},
                   igw_ssh_from=[], igw_user="", igw_keys=[], service_user=None)
    else:
        raise BuildError(f"{role} is not a gateway role")
    ctx["ssh_listen"] = []  # ssh listens in VRF MGMT only
    return ctx


SINGLE = {"description", "default-action", "action", "protocol", "port", "system-as", "router-id", "remote-as",
          "update-source", "holdtime", "keepalive", "host-name", "time-zone", "mtu", "address", "name", "distance",
          "inbound-interface", "outbound-interface", "vrf", "table", "level", "state"}


def _tokens(line):
    """The words of a set/delete line, quoted values ('…' or "…") as one word."""
    import shlex
    try:
        return shlex.split(line)
    except ValueError:
        return line.replace("'", "").replace('"', "").split()


def _path(line):
    """The path of a set line without its value: "set a b c v" → "a b c"; a value-less line is its own path."""
    w = _tokens(line)[1:]
    return "\x1f".join(w[:-1]) if len(w) > 1 else "\x1f".join(w)


def _norm(line):
    """A line compared by its words (quotes and spacing do not matter)."""
    return "\x1f".join(_tokens(line))


def _leaf(line):
    w = _tokens(line)
    return w[-2] if len(w) >= 3 else ""


def _quoted(words):
    return " ".join(f"'{w}'" if (not w or re.search(r"[\s'\"!&;<>|]", w)) and "'" not in w else w for w in words)


def apply_diff(base, diff):
    """The standard configuration with a service's differences: "delete <path>" drops the base lines under the path,
    "set …" lines are added; a set of a single-valued leaf (description, port, action …) replaces the base's value."""
    lines = [l for l in base.splitlines() if l.strip()]
    deletes, sets = [], []
    for l in (diff or "").splitlines():
        l = l.strip()
        if l.startswith("delete "):
            deletes.append(_norm(l)[len("delete\x1f"):])
        elif l.startswith("set "):
            sets.append(l)
    single = {}
    for l in sets:
        w = _tokens(l)
        if _leaf(l) in SINGLE and not (_leaf(l) == "address" and w[1] == "interfaces"):
            single[_path(l)] = l
    out = []
    for l in lines:
        n = _norm(l)
        body = n[len("set\x1f"):] if n.startswith("set\x1f") else None
        if body is not None and any(body == d or body.startswith(d + "\x1f") for d in deletes):
            continue
        if body is not None and _path(l) in single and n != _norm(single[_path(l)]):
            continue
        out.append(l)
    have = {_norm(l) for l in out}
    out += [l for l in sets if _norm(l) not in have]
    return "\n".join(out) + "\n"


def build_gateway(role, node, reveal=False, mgmt_ip=None):
    """A site gateway's configuration: the tenants' egw/igw template (plus the management part for the igw) with the
    site's context, then — for the igw only — the service's differences from it (CoreService.config_diff): nothing else is
    carried over from a running VM (2026-10-27)."""
    base = gateway_base(role, node, reveal, mgmt_ip)
    if node.service.stype == "portal-gw":
        return base + portal_vrrp(node)
    return apply_diff(base, node.service.config_diff) if role == "igw" else base


def portal_vrrp(node):
    """portal-gw (correction 12): each public overlay on eth2, eth3 … — the VIP (its first address, the VMs' gateway)
    shared by the two egws over VRRP (egw1 priority 200, egw2 100); the overlays after the first also join zone CLIENT;
    VRRP between the egws passes the CLIENT zone."""
    p = node.params or {}
    prio = int(p.get("vrrp_priority") or 100)
    out = []
    for i, pb in enumerate(p.get("publics") or []):
        iface, grp = f"eth{2 + i}", f"PUB{i + 1:02d}"
        if i:
            out += [f'set interfaces ethernet {iface} description "CLIENT: {pb["name"]}"',
                    f"set firewall zone CLIENT member interface {iface}"]
        out += [f"set high-availability vrrp group {grp} interface {iface}",
                f"set high-availability vrrp group {grp} vrid {i + 1}",
                f"set high-availability vrrp group {grp} address {pb['vip']}/{pb['prefix']}",
                f"set high-availability vrrp group {grp} priority {prio}"]
    out += ["set firewall ipv4 name CLIENT-to-GW rule 20 action accept",
            "set firewall ipv4 name CLIENT-to-GW rule 20 description \"VRRP between the egws\"",
            "set firewall ipv4 name CLIENT-to-GW rule 20 protocol vrrp",
            "set firewall ipv4 name GW-to-CLIENT rule 20 action accept",
            "set firewall ipv4 name GW-to-CLIENT rule 20 protocol vrrp"]
    return "\n".join(out) + "\n"


def gateway_base(role, node, reveal=False, mgmt_ip=None):
    """The standard configuration of a site gateway, without the service's differences."""
    site_igw = ConfigTemplate.objects.filter(role="cloud-igw").first() if role == "igw" else None
    tpl = site_igw or ConfigTemplate.objects.filter(role=role).first()
    if tpl is None:
        raise BuildError(f"no template for {role}")
    ctx = gateway_context(role, node, reveal, mgmt_ip)
    base = _render(tpl.body, ctx)
    if role == "igw":
        if site_igw is None:  # before the cloud-igw template (correction 12): the tenant igw + the management part
            base += _render(MGMT_PART, ctx)
        seen, lines = set(), []
        for line in base.splitlines():  # the tenant igw and the management part both name the local zone GW
            if line.startswith("set ") and line in seen:
                continue
            if SITE_IGW_NO_SSH.search(line):  # ssh only in VRF MGMT: none of the tenant igw's ssh on EXT
                continue
            seen.add(line)
            lines.append(line)
        base = "\n".join(lines) + "\n"
    return base


EXCLUDED_FROM_DIFF = re.compile(r"^set (system host-name|system login |system time-zone|interfaces loopback|"
                                r"interfaces ethernet eth\d+ mtu )|"
                                r"(password|encrypted-password|public-keys \S+ key|private-key|pre-shared-secret) ")
KEEP_STANDARD = re.compile(r"(MGMT|\bvrf\b|service ntp|system name-server|system syslog|service ssh|ethernet eth0 )")


def diff_from_running(base, running, iface_map=None):
    """The differences of a running configuration from the standard one, as set/delete lines (what config_diff holds).
    iface_map renames the running interfaces first (e.g. the old layout without a management NIC: eth0 → eth1 …).
    Never takes host names, users, the time zone or secrets (the context and the portal's secrets give those); never
    removes the standard's management part (VRF MGMT, NTP, name servers, syslog, ssh)."""
    iface_map = iface_map or {}
    def ren(l):
        return re.sub(r"\beth(\d+)\b", lambda m: iface_map.get(m.group(0), m.group(0)), l)
    run = [ren(l.strip()) for l in running.splitlines() if l.strip().startswith("set ")]
    base_lines = [l.strip() for l in base.splitlines() if l.strip().startswith("set ")]
    run_n, base_n = {_norm(l) for l in run}, {_norm(l) for l in base_lines}
    sets = [l for l in run if _norm(l) not in base_n and not EXCLUDED_FROM_DIFF.search(l)]
    set_paths = {_path(l) for l in sets}
    deletes = []
    for l in base_lines:
        if _norm(l) in run_n or EXCLUDED_FROM_DIFF.search(l) or KEEP_STANDARD.search(l):
            continue
        if _leaf(l) in SINGLE and _path(l) in set_paths:
            continue  # replaced by the running value (a set of the same leaf)
        deletes.append("delete " + _quoted(_tokens(l)[1:]))
    return "\n".join(deletes + sets) + ("\n" if deletes or sets else "")


INIT_HEAD = """#!/bin/bash
# {title}
# one-time init script (ONE_TIME_INIT_SCRIPT_BASE64), run by vyos-postconfig-bootup.script on the first boot
# after the context variables; built by Claugine from Infrastructure -> Templates
source /etc/default/vyatta
conf=/opt/vyatta/sbin/vyatta-cfg-cmd-wrapper
failed=0
${{conf}} begin
"""
INIT_TAIL = """if ${conf} commit; then echo "commit: ok"; else echo "commit: failed"; exit 1; fi
if ${conf} save; then echo "save: ok"; else echo "save: failed"; exit 1; fi
${conf} end
echo "init script: ${failed} command(s) failed"
"""


def init_script(text, title):
    """A VyOS configuration (set/delete lines) as the one-time init script of the first boot.

    A failed command is reported by its line number only (lines may hold secrets); the commit decides."""
    out = [INIT_HEAD.format(title=title)]
    n = 0
    for line in text.splitlines():
        if line.startswith(("set ", "delete ")):
            n += 1
            out.append(f"${{conf}} {line} >/dev/null 2>&1 || {{ echo \"line {n} failed\"; failed=$((failed + 1)); }}\n")
    out.append(INIT_TAIL)
    return "".join(out)


def is_complete(text):
    """No value left to be decided («…») and no secret missing."""
    return "«" not in text


def tenant_roles(tenant):
    """The network functions a tenant has: egw and igw with internet, cgw with the corporate network."""
    from tenants.ordering import connections
    conns = connections(tenant.order or {})
    return [r for r, need in (("egw", "inet"), ("igw", "inet"), ("cgw", "corpnet")) if need in conns]


VARIABLES = {
    "common": "host_name, time_zone, ntp[], dns[] (not used: no name servers on the routers), syslog (host: the "
              "collector's IP, name, port, proto), ssh_port (the site's VyOS management port), mgmt_hosts[] (the site's "
              "management hosts), mgmt (iface eth0, ip, prefix_len, gw, net_name: the cloud-vyos network, VRF MGMT), "
              "martians[] (host name, time zone, user and key are set by the first boot from the context)",
    "egw": "client_networks[], routed_networks[] (extra public networks, via igw_ip), igw_ip, gw (ip, prefix_len, net_name), "
           "ix (ip, prefix_len, cidr, name, rr_net, rrs[], asn, bgp_password), mbps",
    "cgw": "client_networks[], corp_networks[] (iface, cidr, gw, prefix_len, net_name), gw (ip, prefix_len, net_name), "
           "ix (ip, prefix_len, cidr, name, rr_net, rrs[], asn, bgp_password)",
    "igw": "ext (ip, prefix_len, net_name, gateway), lan_networks[] / int_networks[] / pub_networks[] "
           "(iface, cidr, gw, prefix_len, zone, net_name), igw_ssh_from[], igw_user, igw_keys[] (name, key, type), "
           "service_user (name, key, type: the portal reads the configuration with it)",
    "l2-a": "vni, mtu, vxlan_mtu, s2s (ip, remote, prefix_len, name, mtu), wg (local, remote, mtu, port, peer, "
            "private_key, peer_public_key, preshared_key: made at random with the gateways, never kept), side_label, attached",
    "l2-b": "vni, mtu, vxlan_mtu, s2s (ip, remote, prefix_len, name, mtu), wg (local, remote, mtu, port, peer, "
            "private_key, peer_public_key, preshared_key: made at random with the gateways, never kept), side_label, attached",
    "l2-br": "vni, mtu, vlan, attached",
    "rr-inet": "label, pg, title, ix (ip, prefix_len, cidr, name, rr_net, rrs[], asn, mtu), remote_asn, ext_hosts[], int_hosts[], "
               "rr_hosts[], other_rrs[], passwords (ext, int, rrs)",
    "rr-corpnet": "as rr-inet",
    "rr-vxlan": "ix (...), int_hosts[], rr_hosts[], other_rrs[], passwords (int, rrs)",
    "host-frr": "ix (ip, asn, rrs[]), passwords (int)",
}
