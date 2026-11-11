"""Site networks (Infrastructure → Sites → Networks), kept as the portal's records.

They were loaded once from the inventory files (``mirror_site``); since then they are edited only in the portal.
The network list, the delegated subnets of the exchanges, BGP settings and the configurations built from
Templates read them. DNS servers of a network are kept but not shown (site settings give them).

Types by name: ix — cloud-ix, portal-ix, corpnet-ix; evpn — cloud-vxlan, portal-vxlan; storage — data-nfs,
data-iscsi; core — all others. Exchange (ix) and EVPN networks have BGP settings; each peer group names a
range of the network (its addresses become the peer prefixes) and a password kept in Credentials.
"""
import ipaddress

from django.db import transaction

from .models import DelegatedSubnet, Network, NetworkImport, NetworkRange, PeerGroup, Segment, Site


class NetworkError(Exception):
    pass


# --- import from the inventory files ----------------------------------------------------------------

def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _hosts_set(items):
    out = set()
    for x in items or []:
        try:
            n = ipaddress.ip_network(str(x), strict=False)
        except ValueError:
            continue
        out.update(n if n.prefixlen >= 31 else list(n))
    return out


def mirror_site(site, nets, remove=True):
    """Make the portal's networks of `site` exactly the files' {name: data}. Returns notes on what did not fit.

    The inventory files are the source for now: every rebuild of the index mirrors them (created, changed,
    removed); editing a network in the portal writes its file. Peer group hosts are kept exactly as listed;
    the range they match (if any) is noted.
    """
    report = []
    seen = set()
    for name, d in sorted(nets.items()):
        d = d if isinstance(d, dict) else {}
        try:
            subnet, prefix = str(d.get("subnet") or ""), _int(d.get("prefix"))
            net = ipaddress.ip_network(f"{subnet}/{prefix}", strict=False)
        except ValueError:
            report.append(f"{name}: no valid subnet/prefix, skipped")
            continue
        seen.add(name)
        bgp = d.get("bgp") if isinstance(d.get("bgp"), dict) else {}
        kind = Network.kind_for(name)
        extra = {k: d[k] for k in ("bridge", "routes", "dns") if d.get(k)}
        vals = dict(kind=kind, interface=str(d.get("interface") or ""), vlan=_int(d.get("vlan")) or 0,
                    mtu=_int(d.get("mtu")) or 1500, subnet=str(net.network_address), prefix=prefix,
                    gateway=str(d.get("gw")) if d.get("gw") else None, extra=extra,
                    state=str(d.get("state") or Network.State.ACTIVE),
                    local_asn=_int(bgp.get("local_asn")),
                    remote_asn=_int(bgp.get("remote_asn")) if kind == Network.Kind.IX else None,
                    announced=[str(x) for x in bgp.get("subnets") or []], rrs=[str(x) for x in bgp.get("rrs") or []])
        if kind == Network.Kind.EVPN and bgp.get("remote_asn"):
            report.append(f"{name}: remote AS {bgp.get('remote_asn')} in the file is not used (EVPN has one AS)")
        # u10: an exchange's delegated subnets are managed in the portal (IP Subnets + Networks tabs); the import no
        # longer overwrites them on an existing network.
        if kind == Network.Kind.IX and Network.objects.filter(site=site, name=name).exists():
            vals.pop("announced", None)
        n, _created = Network.objects.update_or_create(site=site, name=name, defaults=vals)
        PeerGroup.objects.filter(network=n).delete()
        NetworkRange.objects.filter(network=n).delete()
        for rname, r in (d.get("ranges") or {}).items():
            r = r if isinstance(r, dict) else {}
            if r.get("start") and _int(r.get("len")):
                NetworkRange.objects.create(network=n, name=str(rname), start=str(r["start"]), length=_int(r["len"]))
            else:
                report.append(f"{name}: range {rname} has no start or length")
        for session in ("ibgp", "ebgp"):
            groups = ((bgp.get(session) or {}).get("peer_groups") or {}) if isinstance(bgp.get(session), dict) else {}
            for gname, g in groups.items():
                g = g if isinstance(g, dict) else {}
                hosts = [str(x) for x in g.get("hosts") or []]
                rng = _matching_range(n, _hosts_set(hosts))
                PeerGroup.objects.create(network=n, name=str(gname), session=session, range=rng, hosts=hosts,
                                         password=str(g.get("password") or ""))
        for problem in missing_bgp(n):
            report.append(f"{name}: BGP lacks {problem}")
    gone = Network.objects.filter(site=site).exclude(name__in=seen) if remove else Network.objects.none()
    for n in gone:
        report.append(f"{n.name}: no longer in the files, removed")
    gone.delete()
    return report


def import_site(site, nets):
    """Add or update the given networks of `site`, leaving the others (tests, partial loads)."""
    return mirror_site(site, nets, remove=False)


def _matching_range(n, hosts):
    if not hosts:
        return None
    for r in n.ranges.all():
        if {r.first + i for i in range(r.length)} == hosts:
            return r
    return None


def sync_from_tree(file_networks):
    """After the inventory index is rebuilt: mirror the networks of every site from the files."""
    with transaction.atomic():
        for site, nets in file_networks.items():
            report = mirror_site(site, nets)
            NetworkImport.objects.update_or_create(site=site, defaults={"report": report})
        Network.objects.exclude(site__in=list(file_networks)).delete()
        sync_index()


def sync_index(site=None):
    """Segment and DelegatedSubnet rows (used by placement, pools and capacity) from the portal's networks."""
    sites = {s.code: s for s in Site.objects.all()}
    qs = Network.objects.all() if site is None else Network.objects.filter(site=site)
    codes = set(sites) if site is None else {site}
    Segment.objects.filter(site__code__in=codes).delete()
    DelegatedSubnet.objects.filter(site__code__in=codes).delete()
    from .tree import subnet_purpose
    for n in qs:
        s = sites.get(n.site)
        if s is None or n.state == Network.State.RETIRED:
            continue
        Segment.objects.create(site=s, name=n.name, vlan=n.vlan, cidr=n.cidr)
        for sub in n.announced or []:
            try:
                ipaddress.ip_network(str(sub), strict=False)
            except ValueError:
                continue
            DelegatedSubnet.objects.create(site=s, segment=n.name, cidr=str(sub), purpose=subnet_purpose(n.name))


# --- what configurations need --------------------------------------------------------------------------

def get(site, name):
    return Network.objects.filter(site=site, name=name).first()


def segment(site, name):
    """The network in the shape the configuration builder uses (see nfconfig)."""
    n = get(site, name)
    if n is None:
        return None
    groups = {"ibgp": {}, "ebgp": {}}
    for g in n.peer_groups.select_related("range"):
        groups.setdefault(g.session, {})[g.name] = {"password": g.password, "hosts": g.cidrs()}
    rr = groups["ibgp"].get("rrs") or {}
    rr_hosts = [str(x) for x in rr.get("hosts") or []]
    try:  # the route reflectors as the fewest networks (firewall groups)
        rr_nets = [str(x) for x in ipaddress.collapse_addresses(ipaddress.ip_network(h, strict=False) for h in rr_hosts)]
    except (ValueError, TypeError):
        rr_nets = rr_hosts
    return {"name": f"{name}_{site}", "segment": name, "cidr": n.cidr, "prefix_len": n.prefix, "mtu": n.mtu,
            "asn": n.local_asn or "", "remote_asn": n.remote_asn or "", "rrs": list(n.rrs or []),
            "ibgp": groups["ibgp"], "ebgp": groups["ebgp"],
            "ranges": {r.name: {"start": r.start, "len": r.length} for r in n.ranges.all()},
            "dns": list((n.extra or {}).get("dns") or []), "gw": n.gateway or "",
            "rr_net": next(iter(rr_nets), ""), "rr_nets": rr_nets}


def services(site, platform):
    """(cidr, dns) of the platform's services network."""
    for name in (f"{platform}-services", "cloud-services"):
        n = get(site, name)
        if n is not None:
            return n.cidr, list((n.extra or {}).get("dns") or [])
    return "", []


# --- checks -------------------------------------------------------------------------------------------

def check_network(n, exclude_pk=None):
    errors = []
    try:
        net = ipaddress.ip_network(f"{n.subnet}/{n.prefix}", strict=True)
    except ValueError as exc:
        return [f"subnet: {exc}"]
    if not 1 <= int(n.vlan or 0) <= 4094:
        errors.append("VLAN: 1-4094")
    if not 576 <= int(n.mtu or 0) <= 9216:
        errors.append("MTU: 576-9216")
    if n.gateway and ipaddress.ip_address(n.gateway) not in net:
        errors.append(f"gateway {n.gateway} is not in {net}")
    if Network.objects.filter(site=n.site, name=n.name).exclude(pk=exclude_pk or n.pk).exists():
        errors.append(f"{n.name} already exists on {n.site}")
    for x in n.rrs or []:
        try:
            if ipaddress.ip_address(x) not in net:
                errors.append(f"route reflector {x} is not in {net}")
        except ValueError:
            errors.append(f"route reflector {x}: not an address")
    for x in n.announced or []:
        try:
            ipaddress.ip_network(x, strict=True)
        except ValueError:
            errors.append(f"announced subnet {x}: not a network")
    return errors


def check_range(n, name, start, length, exclude_pk=None):
    errors = []
    net = n.net
    try:
        first = ipaddress.ip_address(start)
    except ValueError:
        return [f"{start}: not an address"]
    if length < 1:
        return ["length: at least 1"]
    last = first + (length - 1) if int(first) + length - 1 <= 0xFFFFFFFF else None
    if first not in net or last is None or last not in net:
        errors.append(f"{start} + {length} is not inside {net}")
    others = n.ranges.exclude(pk=exclude_pk)
    if others.filter(name=name).exists():
        errors.append(f"range {name} already exists in {n.name}")
    if last is not None:
        for r in others:
            if r.first <= last and r.last >= first:
                errors.append(f"crosses range {r.name} ({r.start} + {r.length})")
    return errors


def missing_bgp(n):
    """What a network with BGP still lacks (local AS, remote AS, the hosts and password of each peer group)."""
    if not n.has_bgp:
        return []
    out = []
    if not n.local_asn:
        out.append("local AS")
    if n.kind == Network.Kind.IX and not n.remote_asn:
        out.append("remote AS")
    have = {g.name: g for g in n.peer_groups.all()}
    for sec in bgp_sections(n):
        g = have.get(sec["group"])
        if g is None or not g.cidrs():
            out.append(f"{sec['key']}: hosts")
        if g is None or not g.password_defined:
            out.append(f"{sec['key']}: password")
    return out


# --- BGP peer groups: rrs, igw, egw -----------------------------------------------------------------------

def bgp_sections(n):
    """The peer groups of a network as the portal shows them: rrs, igw and (ix) egw, with the group behind each."""
    from django.utils.translation import gettext as _
    if n.kind == Network.Kind.EVPN:
        return [
            {"key": "rrs", "group": "rrs", "session": "ibgp", "ip_only": True, "title": _("Route reflectors (rrs)")},
            {"key": "igw", "group": "hosts", "session": "ibgp", "ip_only": False, "title": _("Internal peers (igw): KVM hosts")},
        ]
    if n.kind == Network.Kind.IX:
        return [
            {"key": "rrs", "group": "rrs", "session": "ibgp", "ip_only": True, "title": _("Route reflectors (rrs)")},
            {"key": "igw", "group": n.peer_role, "session": "ibgp", "ip_only": False,
             "title": _("Internal peers (igw): project gateways")},
            {"key": "egw", "group": "ext", "session": "ebgp", "ip_only": False,
             "title": _("External peers (egw): the remote AS")},
        ]
    return []


def range_hosts(r, ip_only=True):
    """The addresses of a range (ip_only) or the fewest networks covering it."""
    if ip_only:
        return [str(r.first + i) for i in range(min(r.length, 1024))]
    return r.cidrs()


def expand(items, limit=1024):
    """Addresses of a list of addresses and networks (all addresses of each network)."""
    out = []
    for x in items or []:
        try:
            n = ipaddress.ip_network(str(x), strict=False)
        except ValueError:
            continue
        for a in n:
            if len(out) >= limit:
                return out
            if str(a) not in out:
                out.append(str(a))
    return out


DEFAULT_RANGES = {Network.Kind.IX: [("ext-gws", 0, 4), ("rrs", 240, 4)], Network.Kind.EVPN: [("rrs", 240, 4)]}


def ensure_default_ranges(n):
    """ix: ext-gws (…0/30) and rrs (…240/30); EVPN: rrs (…240/30). Skips a name that exists or a crossing range.
    Returns notes on what was skipped."""
    notes = []
    for name, offset, length in DEFAULT_RANGES.get(n.kind, []):
        if n.ranges.filter(name=name).exists():
            continue
        net = n.net
        if net.num_addresses < offset + length:
            notes.append(f"{n}: {name} does not fit in {net}")
            continue
        start = str(net.network_address + offset)
        errs = check_range(n, name, start, length)
        if errs:
            notes.append(f"{n}: default range {name} ({start} + {length}) not added: {'; '.join(errs)}")
            continue
        NetworkRange.objects.create(network=n, name=name, start=start, length=length)
    return notes


def has_password(ref):
    from .credentials import has
    return bool(ref) and has(ref)


def store_password(ref, value, n=None, group="", user=None):
    """Keep a peer group's password in the network's BGP secret (the slot the group uses)."""
    from accounts.models import audit
    from .credentials import SLOT_OF_GROUP, parse
    from .models import Secret
    sec, slot = parse(ref)
    if sec is None or sec.kind != Secret.Kind.BGP:
        site, net = getattr(n, "site", ""), getattr(n, "name", "")
        sec = Secret.objects.filter(kind=Secret.Kind.BGP, site=site, network=net).first() or Secret(
            kind=Secret.Kind.BGP, name=f"{site}.{net}", site=site, network=net, net_type=getattr(n, "kind", "ix"))
        slot = SLOT_OF_GROUP.get(group, "igw")
    sec.put(slot, value)
    sec.updated_by = user
    sec.save()
    if n is not None and group:  # the group refers to the secret by id
        from .models import PeerGroup
        PeerGroup.objects.filter(network=n, name=group).update(password=f"{sec.ref}#{slot}")
    audit(user, "network_password_set", sec, network=str(n) if n else "", group=group, length=len(value))
    return sec


def set_password(n, group, value, user=None):
    """The password of a peer group of `n` (its reference from the file, else the default one)."""
    g = n.peer_groups.filter(name=group).first()
    ref = (g.password if g and g.password else n.password_ref(group))
    return store_password(ref, value, n, group, user)
