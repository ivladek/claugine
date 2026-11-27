"""Infrastructure → DNS (owner 2026-11-21 13:38-13:50, T34 B5): the zones of all sites and their name servers.

    servers      the VMs of the sites' core services ns: one primary for all sites (DNS servers → Edit), every other
                 a secondary; they take the zones by transfer
    zones        automatic - the direct zone from Settings → Customer → Internal domain (renamed with it) and a reverse
                 zone for every core-cloud and core-kvm network - and added ones (DnsZone, source "")
    records      automatic - every core service VM (deployed or planned), host and front end by its host name, with a
                 PTR in the reverse zone of its address - made from the portal's records, never stored; added ones
                 (DnsRecord) in any zone
    zone_text    a zone file (SOA from the settings, NS: every server)
    apply        the primary's zone files written and checked (named-checkzone, named-checkconf -z), reloaded; every
                 server's zone list (/etc/bind/named.conf.zones, the bind service's own file) written; the secondaries
                 transfer. Only servers made from their profile (their named.conf includes that list): the others are
                 refused, nothing is written there
    check        read only: each zone's file on the primary against what was applied, and the serial every server
                 answers
    values       the dns values of a name server's recipe (inventory/recipes.py, the bind service)

The zone data is kept only here (owner 13:38): the name servers are made from it.
"""

import base64
import hashlib
import ipaddress
import re
import shlex

from django.utils import timezone
from django.utils.translation import gettext as _

from .models import CoreService, CoreServiceNode, DnsSettings, DnsZone, Host, Network, Platform

DEFAULT_SOA = {"ttl": 300, "refresh": 3600, "retry": 600, "expire": 1209600, "minimum": 300}
REVERSE_KINDS = (Network.Kind.CORE_CLOUD, Network.Kind.CORE_KVM)
ZONES_FILE = "/etc/bind/named.conf.zones"
ZONE_DIR = "/var/cache/bind/zones"
LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
NAME = re.compile(r"^(@|\*|(\*\.)?[A-Za-z0-9_]([A-Za-z0-9_-]{0,62})(\.[A-Za-z0-9_]([A-Za-z0-9_-]{0,62}))*)$")
ZONE_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62})(\.[a-z0-9]([a-z0-9-]{0,62}))+$")


class DnsError(Exception):
    pass


# --- what the portal has -------------------------------------------------------------------------------------------


def domain():
    from tenants.models import PortalSettings

    return (PortalSettings.get().internal_domain or "").strip().strip(".").lower()


def soa(st=None):
    st = DnsSettings.get() if st is None else st
    return {**DEFAULT_SOA, **{k: int(v) for k, v in (st.soa or {}).items() if str(v).isdigit()}}


def _ip(x):
    try:
        return str(ipaddress.IPv4Address(str(x).strip()))
    except ValueError:
        return ""


def reverse_zones_of(net):
    """The reverse zones of a network: one per /24 it covers (a /16 or wider: one per /16, at most 16)."""
    n = ipaddress.ip_network(net.cidr, strict=False)
    if n.version != 4:
        return []
    if n.prefixlen <= 16:
        subs = list(n.subnets(new_prefix=16))[:16] if n.prefixlen < 16 else [n]
        return [".".join(reversed(str(s.network_address).split(".")[:2])) + ".in-addr.arpa" for s in subs]
    subs = list(n.subnets(new_prefix=24)) if n.prefixlen < 24 else [n.supernet(new_prefix=24)]
    return [".".join(reversed(str(s.network_address).split(".")[:3])) + ".in-addr.arpa" for s in subs[:64]]


def reverse_net(zone):
    """The network a reverse zone name stands for (10.61.101.0/24 for 101.61.10.in-addr.arpa), or None."""
    if not zone.endswith(".in-addr.arpa"):
        return None
    parts = zone[: -len(".in-addr.arpa")].split(".")
    if not 1 <= len(parts) <= 3 or not all(p.isdigit() and int(p) < 256 for p in parts):
        return None
    octets = list(reversed(parts)) + ["0"] * (4 - len(parts))
    return ipaddress.ip_network(f"{'.'.join(octets)}/{8 * len(parts)}")


def ptr_label(ip, zone):
    net = reverse_net(zone)
    a = ipaddress.ip_address(ip)
    if net is None or a not in net:
        return None
    keep = 4 - net.prefixlen // 8
    return ".".join(reversed(str(a).split(".")[-keep:]))


def sync_zones():
    """The automatic zones as the portal's records say (created, renamed with the internal domain); one whose source
    is gone becomes an added zone when it has added records, else it goes. Returns the zones."""
    wanted = {}
    d = domain()
    if d:
        wanted[d] = (DnsZone.Kind.DIRECT, "domain")
    for net in Network.objects.filter(kind__in=REVERSE_KINDS).exclude(state=Network.State.RETIRED).order_by("pk"):
        for z in reverse_zones_of(net):
            wanted.setdefault(z, (DnsZone.Kind.REVERSE, f"network:{net.pk}"))
    old_domain = DnsZone.objects.filter(source="domain").first()
    if old_domain is not None and d and old_domain.name != d and not DnsZone.objects.filter(name=d).exists():
        old_domain.name = d  # renamed in Settings → renamed here, its added records kept
        old_domain.save(update_fields=["name"])
    for z in DnsZone.objects.exclude(source=""):
        if z.name not in wanted:
            if z.records.exists():
                z.source, z.notes = "", _("was automatic; kept for its added records")
                z.save(update_fields=["source", "notes"])
            else:
                z.delete()
    for name, (kind, source) in wanted.items():
        z, _new = DnsZone.objects.get_or_create(name=name, defaults={"kind": kind, "source": source})
        if (z.kind, z.source) != (kind, source):
            z.kind, z.source = kind, source
            z.save(update_fields=["kind", "source"])
    return list(DnsZone.objects.all())


def servers(st=None, d=None):
    """[{node, site, ip, fqdn, role}] - the VMs of every site's core service ns (retired services left out)."""
    s = DnsSettings.get() if st is None else st
    d = domain() if d is None else d
    out = []
    qs = (
        CoreServiceNode.objects.filter(service__stype=CoreService.Type.NS)
        .exclude(service__state=CoreService.State.RETIRED)
        .select_related("service__site")
        .order_by("service__site__code", "name")
    )
    for n in qs:
        ip = next((_ip(x) for x in (n.ips or []) if _ip(x)), "") or _ip(n.address)
        host = (n.hostname or n.name).split(".")[0].lower()
        out.append(
            {
                "node": n,
                "site": n.service.site.code,
                "ip": ip,
                "host": host,
                "fqdn": f"{host}.{d}." if d else "",
                "role": "primary" if s.primary_id == n.pk else "secondary",
            }
        )
    return out


def _label(x):
    x = str(x or "").split(".")[0].strip().lower()
    return x if LABEL.match(x) else ""


def automatic():
    """{zone name: [{name, rtype, value, source}]} - the records made from the portal's records; and [notes]."""
    d = domain()
    # commit-326: every reverse zone, most specific first; a PTR goes to the most specific one holding its address -
    # when that one is an added zone, its records are its own (no automatic PTR there)
    rev_all = sorted(
        ((z.name, reverse_net(z.name), z.automatic) for z in DnsZone.objects.filter(kind=DnsZone.Kind.REVERSE)),
        key=lambda x: -(x[1].prefixlen if x[1] is not None else -1),
    )
    rev = {name: net for name, net, auto in rev_all if auto}
    a, notes = {}, []

    def add(label, ip, source):
        label, ip = _label(label), _ip(ip)
        if not label or not ip:
            return
        if label in a:
            if a[label]["value"] != ip:
                notes.append(
                    _("%(n)s: %(a)s (%(s1)s) kept, %(b)s (%(s2)s) left out")
                    % {"n": label, "a": a[label]["value"], "s1": a[label]["source"], "b": ip, "s2": source}
                )
            return
        a[label] = {"name": label, "rtype": "A", "value": ip, "source": source}

    nodes = CoreServiceNode.objects.exclude(service__state=CoreService.State.RETIRED).select_related(
        "service__site", "service"
    )
    for n in nodes.order_by("service__site__code", "service__name", "name"):
        ip = next((_ip(x) for x in (n.ips or []) if _ip(x)), "") or _ip(n.address)
        state = _("deployed") if n.deployed_vm_id else (n.service.get_state_display())
        add(n.hostname or n.name, ip, f"{_('core service')} {n.service.name} · {n.service.site.code} · {state}")
    for h in Host.objects.exclude(state="retired").select_related("site").order_by("site__code", "name"):
        add(h.name, h.ip, f"{_('host')} · {h.site.code} · {h.state}")
    for p in Platform.objects.select_related("site").order_by("key"):
        fe = p.fe or {}
        if fe.get("hostname") and _ip(p.fe_vip):
            add(fe["hostname"], p.fe_vip, f"{_('FE VIP')} {p.key}")
        for name, ip in zip(fe.get("nodes") or [], fe.get("node_ips") or [], strict=False):
            add(name, ip, f"{_('front end')} {p.key}")
    out = {z: [] for z in rev}
    if d:
        out[d] = sorted(a.values(), key=lambda r: r["name"])
        for r in out[d]:
            ip = ipaddress.ip_address(r["value"])
            for z, net, auto in rev_all:
                if net is not None and ip in net:
                    if not auto:
                        break
                    out[z].append(
                        {
                            "name": ptr_label(r["value"], z),
                            "rtype": "PTR",
                            "value": f"{r['name']}.{d}.",
                            "source": r["source"],
                        }
                    )
                    break
    for z in out:  # commit-325: names as text; PTR labels in number order
        out[z].sort(
            key=lambda r: (
                r["rtype"],
                [int(x) for x in r["name"].split(".")]
                if r["rtype"] == "PTR" and all(x.isdigit() for x in r["name"].split("."))
                else [],
                r["name"],
            )
        )
    return out, notes


def rows(zone, auto=None):
    """Every record of a zone: the automatic ones, then the added ones ({..., "added": DnsRecord})."""
    auto = automatic()[0] if auto is None else auto
    out = [dict(r, added=None) for r in auto.get(zone.name, [])] if zone.automatic else []
    for r in zone.records.all():
        out.append({"name": r.name, "rtype": r.rtype, "value": r.value, "ttl": r.ttl, "source": _("added"), "added": r})
    return out


# --- zone files ----------------------------------------------------------------------------------------------------


def _value(rtype, value):
    v = " ".join(str(value).split())  # one line
    if rtype == "TXT" and not v.startswith('"'):
        v = '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return v


def first_pending(st, srv):
    """commit-354/358: the VM whose redeploy as the primary-to-be has not ended (the candidate marked first), or None:
    the server's entry plus {pk, eligible}; a node retired or deleted since is not eligible - it still blocks every
    other name server (its VM may still answer as a primary) until it is released by hand (release_first)."""
    cand = st.candidate or {}
    if not cand.get("first"):
        return None
    x = next((x for x in srv if x["node"].pk == cand.get("node")), None)
    if x is not None:
        return {**x, "pk": x["node"].pk, "eligible": True, "rev": cand.get("rev") or ""}
    n = CoreServiceNode.objects.filter(pk=cand.get("node")).first()
    host = (n.hostname or n.name).split(".")[0].lower() if n else f"node #{cand.get('node')} (deleted)"
    return {"pk": cand.get("node"), "host": host, "node": n, "ip": "", "eligible": False, "rev": cand.get("rev") or ""}


def _rev():
    """commit-363: a candidate's revision - renewed at every change of the attempt (a reservation, a VM recorded, an
    allocation marked unsure, a retry's merge), so a Release confirms exactly what the operator saw."""
    import secrets

    return secrets.token_hex(6)


def release_first(user=None, host=None, rev=None):
    """commit-358 (owner 12:28): by hand (DNS servers → Release), under the DNS claim - the operator states that no VM
    made for the pending primary-to-be can answer as a primary any more (its service retired, its node deleted, an
    allocation whose outcome is not known checked on the platform); another name server may then become it."""
    from accounts.models import audit

    token = claim("release the primary-to-be")
    if token is None:
        raise DnsError(_("A DNS task is running: try again when it ends."))
    try:
        st = DnsSettings.get()
        cand = st.candidate or {}
        if not cand.get("first"):
            return False
        if rev is not None or host is not None:  # commit-363: what the operator saw, checked under the claim
            p = first_pending(st, servers(st)) or {}
            if (cand.get("rev") or "") != (rev or "") or p.get("host") != host:
                raise DnsError(_("The primary-to-be changed since the page was shown: look again, then Release."))
        DnsSettings.objects.filter(pk=st.pk).update(candidate={})
        audit(
            user,
            "dns_first_primary_released",
            None,
            node=cand.get("node"),
            vms=",".join(map(str, cand.get("vms") or [])),
        )
        return True
    finally:
        release(token)


def candidate_vm(node, vm_id):
    """commit-354: a VM made for a first primary is recorded in its candidate (vms) the moment it exists - any of
    them may serve the candidate's zones, so a rollback that brings one back keeps the candidate."""
    from django.db import transaction

    with transaction.atomic():
        st = DnsSettings.objects.select_for_update().get(pk=DnsSettings.get().pk)
        cand = dict(st.candidate or {})
        if cand.get("first") and cand.get("node") == node.pk:
            cand["vms"] = sorted(set(cand.get("vms") or []) | {int(vm_id)})
            cand["rev"] = _rev()  # commit-363
            DnsSettings.objects.filter(pk=st.pk).update(candidate=cand)


FIRST_TAG = "CLAUGINE_DNS_FIRST"  # commit-354: put in the allocation of a first primary's VM (USER_TEMPLATE)


def first_tag_of(node, vm_id):
    """The VM's FIRST_TAG as the platform shows it ("" when it has none). Raises when the VM cannot be read."""
    from .core_deploy import _one

    one, _control = _one(node.service)
    return one.xml("one.vm.info", int(vm_id), False).findtext(f"USER_TEMPLATE/{FIRST_TAG}") or ""


def first_alive(node):
    """commit-354: the node's VMs on its platform that carry its FIRST_TAG and are not done - any may serve the
    candidate's zones (an allocation whose id never came back included); None when the platform cannot be read."""
    from .core_deploy import _one

    try:
        one, _control = _one(node.service)
        pool = one.xml("one.vmpool.info", -2, -1, -1, -1)
    except Exception:  # noqa: BLE001 - unknown: the caller keeps the candidate
        return None
    return [
        int(vm.findtext("ID"))
        for vm in pool.findall("VM")
        if (vm.findtext(f"USER_TEMPLATE/{FIRST_TAG}") or "") == str(node.pk) and vm.findtext("STATE") != "6"
    ]


def mark_unsure(node):
    """commit-354: a first primary's allocation whose outcome is not known (no id, no refusal from OpenNebula) - the
    candidate is kept whatever a pool read shows later; only the node's successful redeploy ends it (or, once it
    exists, an operator's release)."""
    from django.db import transaction

    with transaction.atomic():
        st = DnsSettings.objects.select_for_update().get(pk=DnsSettings.get().pk)
        cand = dict(st.candidate or {})
        if cand.get("first") and cand.get("node") == node.pk:
            cand["unsure"] = True
            cand["rev"] = _rev()  # commit-363
            DnsSettings.objects.filter(pk=st.pk).update(candidate=cand)


def end_first(node):
    """commit-354: a first primary's candidate ends only when no VM made for it can still serve: none tagged and alive
    on the platform (the platform read). Otherwise the alive ones are recorded and it stays. True when it ended."""
    if (DnsSettings.get().candidate or {}).get("unsure"):  # an allocation's outcome not known: kept
        return False
    alive = first_alive(node)
    if alive is None or alive:
        for vid in alive or []:
            candidate_vm(node, vid)
        return False
    published(node, False)
    return True


def may_serve(node, vm_id):
    """commit-354: whether this VM of the node may serve its pending first primary's zones - its id was recorded, or
    the platform shows the tag its allocation carried (an id never saved); a VM that cannot be read may (kept)."""
    cand = DnsSettings.get().candidate or {}
    if not (cand.get("first") and cand.get("node") == node.pk):
        return False
    if int(vm_id) in (cand.get("vms") or []):
        return True
    try:
        return first_tag_of(node, vm_id) == str(node.pk)
    except Exception:  # noqa: BLE001 - not knowing is not proof that it never served
        return True


def keep_candidate(prev):
    """commit-354: a failed retry of a first primary puts back the candidate it found - the VM that began becoming
    the primary may still serve those zones; only its success or a rollback by hand ends it. What the retry learnt is
    kept with it: the VMs it made (vms) and an allocation whose outcome is not known (unsure)."""
    from django.db import transaction

    with transaction.atomic():
        st = DnsSettings.objects.select_for_update().get(pk=DnsSettings.get().pk)
        cur = st.candidate or {}
        out = dict(prev)
        if cur.get("first") and cur.get("node") == prev.get("node"):
            out["vms"] = sorted(set(prev.get("vms") or []) | set(cur.get("vms") or []))
            out["unsure"] = bool(prev.get("unsure") or cur.get("unsure"))
            out["rev"] = _rev()  # commit-363: the retry changed what stands
        DnsSettings.objects.filter(pk=st.pk).update(candidate=out)


def drop_stale_primary():
    """commit-355: a stored primary that is no longer a name server (its service retired, its node gone) is cleared,
    so the next name server made from its profile becomes the primary. Under the DNS claim. True when cleared."""
    st = DnsSettings.get()
    if st.primary_id is None:
        return False
    if any(x["node"].pk == st.primary_id for x in servers(st)):
        return False
    DnsSettings.objects.filter(pk=st.pk, primary_id=st.primary_id).update(primary=None)
    return True


def stale_primary(st=None, srv=None):
    """commit-355: the stored primary when it is no longer a name server, else None (for the page)."""
    st = DnsSettings.get() if st is None else st
    srv = servers(st) if srv is None else srv
    if st.primary_id is None or any(x["node"].pk == st.primary_id for x in srv):
        return None
    return st.primary


def as_primary(srv, node, force=False):
    """owner 2026-11-22 09:59: with no primary yet, the servers as they will be once this VM (the first made) is it.
    force (owner 11:51, "make server as primary during deployment"): this VM the primary, every other a secondary."""
    if not force and any(x["role"] == "primary" for x in srv):
        return srv
    return [{**x, "role": "primary" if x["node"].pk == node.pk else "secondary"} for x in srv]


def zone_text(zone, serial, auto=None, srv=None, snap=None):
    """The zone file: SOA (the primary's name, hostmaster@ the domain), NS (every server), the records. snap: one
    snapshot for every file of an Apply (commit-321)."""
    s = snap["soa"] if snap else soa()
    d = snap["domain"] if snap else domain()
    srv = snap["srv"] if snap else (servers() if srv is None else srv)
    auto = snap["auto"] if snap else auto
    primary = next((x for x in srv if x["role"] == "primary"), None)
    if primary is None or not primary["fqdn"]:
        raise DnsError(_("No primary name server (DNS servers → Edit), or no internal domain (Settings → Customer)."))
    lines = [
        "; made by claugine from Infrastructure -> DNS - a change here is overwritten by the next Apply",
        f"$ORIGIN {zone.name}.",
        f"$TTL {s['ttl']}",
        f"@ IN SOA {primary['fqdn']} hostmaster.{d}. ( {serial} {s['refresh']} {s['retry']} {s['expire']} "
        f"{s['minimum']} )",
    ]
    lines += [f"@ IN NS {x['fqdn']}" for x in srv if x["fqdn"]]
    for r in rows(zone, auto):
        if r["rtype"] == "NS" and r["name"] == "@":
            continue
        ttl = f" {r['ttl']}" if r.get("ttl") else ""
        lines.append(f"{r['name']}{ttl} IN {r['rtype']} {_value(r['rtype'], r['value'])}")
    return "\n".join(lines) + "\n"


def digest(text):
    """The zone's text without its serial, hashed: what decides whether it must be applied again."""
    return hashlib.sha256(re.sub(r"\( \d+ ", "( ", text, count=1).encode()).hexdigest()


def next_serial(cur, reserved=0):
    """A serial newer than both the current one and every reserved one (commit-330: never reused)."""
    base = int(timezone.localdate().strftime("%Y%m%d")) * 100
    return max(base, int(cur or 0) + 1, int(reserved or 0) + 1)


def snapshot(st=None):
    """commit-321: the settings, servers, SOA, domain and automatic records read once, for every file of an Apply."""
    sync_zones()
    st = DnsSettings.get() if st is None else st
    d = domain()
    return {"st": st, "domain": d, "soa": soa(st), "srv": servers(st, d), "auto": automatic()[0]}


def revision(snap):
    """commit-327: what the servers' configuration depends on - a later Check must have seen the same."""
    st = snap["st"]
    data = [
        st.primary_id,
        st.forwarders,
        st.forward_zones,
        st.sites,
        st.internal_extra,
        st.notify_extra,
        snap["soa"],
        snap["domain"],
        [(x["host"], x["ip"], x["role"]) for x in snap["srv"]],
    ]
    return hashlib.sha256(repr(data).encode()).hexdigest()[:16]


def state(zone, auto=None, srv=None):
    """ "applied" when the zone as the portal has it now is what was applied; "to apply" otherwise."""
    st = DnsSettings.get()
    if st.uncertain:  # commit-338: a primary's redeploy or rollback - not known what it serves until an Apply
        return "to apply"
    try:
        text = zone_text(zone, st.serial, auto, srv)
    except DnsError:
        return "no primary"
    return "applied" if ((st.applied or {}).get(zone.name) or [None])[0] == digest(text) else "to apply"


# --- the values of a name server's recipe ---------------------------------------------------------------------------


def values(node, reveal=True, st=None, srv=None, zones=None, make_primary=False):
    """The dns values of the bind service for this VM; None when it has no address. Owner 2026-11-22 09:59: "first
    deployed VM is primary, other - secondaries" - with no primary yet, this VM is the one to become it ("first": True;
    published() makes it the primary when its redeploy passes). st, srv: one snapshot of the
    settings and servers for every server of an Apply (commit-321); zones: the zones being applied ([{name, text}]).
    The primary's zone files are the published ones (commit-323: what the secondaries have, never the current records
    under a serial they already hold); a preview shows the current records."""
    st = DnsSettings.get() if st is None else st
    srv = servers() if srv is None else srv
    # "first": this VM is becoming the primary (it served none before) - with no primary yet (owner 09:59), or when
    # its deployment is asked to make it the primary (owner 11:51); commit-354: a redeploy becoming the primary that
    # has not ended (its candidate, marked first) keeps that VM the one to become it, every other waits
    current = next((x for x in srv if x["role"] == "primary"), None)
    pending = first_pending(st, srv)
    if pending is not None and not pending["eligible"]:  # commit-358: no longer a name server - every other waits
        return {
            "role": "secondary",
            "first": False,
            "waiting_for": pending["host"],
            "primary": "",
            "forwarders": [],
            "forward_zones": [],
            "sites": [],
            "internal_extra": [],
            "notify_extra": [],
            "zones": [],
        }
    if pending is not None:
        srv = as_primary(srv, pending["node"], force=True)
        first = pending["node"].pk == node.pk
    elif current is None or (make_primary and current["node"].pk != node.pk):
        srv = as_primary(srv, node, force=True)
        first = True
    else:
        first = False
    primary = next((x for x in srv if x["role"] == "primary"), None)
    if primary is None or not primary["ip"]:
        return None
    by_site = {}
    for x in srv:
        by_site.setdefault(x["site"], []).append(x["ip"])
    sites = []
    for code in sorted(set(by_site) | set(st.sites or {})):
        v = (st.sites or {}).get(code) or {}
        sites.append(
            {
                "code": code,
                "internal": list(v.get("internal") or []),
                "public": list(v.get("public") or []),
                "ns": by_site.get(code, []),
            }
        )
    role = "primary" if node.pk == primary["node"].pk else "secondary"
    if zones is not None:
        zones = [{"name": z["name"], "text": z["text"] if role == "primary" else ""} for z in zones]
    elif role == "primary" and reveal:  # commit-330: the candidate reserved for this redeploy, else the published
        cand = st.candidate or {}
        texts = cand.get("texts") if cand.get("node") == node.pk else st.published
        zones = [{"name": n, "text": text} for n, text in sorted((texts or {}).items())]
    elif role == "primary":
        auto = automatic()[0]
        try:  # commit-356: a preview never fails (no internal domain: missing() says so)
            zones = [{"name": z.name, "text": zone_text(z, st.serial, auto, srv)} for z in DnsZone.objects.all()]
        except DnsError:
            zones = []
    else:
        zones = [{"name": n, "text": ""} for n in sorted(st.published or {})] or [
            {"name": z.name, "text": ""} for z in DnsZone.objects.all()
        ]
    return {
        "role": role,
        "first": first,
        # commit-354: the first primary's redeploy not ended - every other name server waits for it
        "waiting_for": pending["host"] if pending and pending["pk"] != node.pk else "",
        "primary": primary["ip"],
        "forwarders": list(st.forwarders or []),
        "forward_zones": list(st.forward_zones or []),
        "sites": sites,
        "internal_extra": list(st.internal_extra or []),
        "notify_extra": list(st.notify_extra or []),
        "zones": zones,
    }


OPTIONAL = {"forward_zones", "internal_extra", "notify_extra"}  # may be empty


# --- Apply and Check ------------------------------------------------------------------------------------------------

BIND_FILES = ("/etc/bind/named.options", ZONES_FILE, "/etc/bind/named.conf")  # commit-321: written together
CLAIM_SECONDS = 7200  # commit-331: only a claim nobody has refreshed for 2 hours is abandoned


def _fresh(c):
    from datetime import datetime

    try:
        at = datetime.fromisoformat((c or {}).get("at") or "")
    except ValueError:
        return False
    return bool((c or {}).get("token")) and (timezone.now() - at).total_seconds() < CLAIM_SECONDS


def claim(by):
    """commit-324/331: the DNS claim (one DNS task at a time: Apply, Check, a name server's redeploy), taken under the
    settings row's lock; None when a live one is held. The holder refreshes it (touch) while it works."""
    import secrets

    from django.db import transaction

    with transaction.atomic():
        st = DnsSettings.objects.select_for_update().get(pk=DnsSettings.get().pk)
        if _fresh(st.claim):
            return None
        token = secrets.token_hex(8)
        DnsSettings.objects.filter(pk=st.pk).update(claim={"token": token, "at": timezone.now().isoformat(), "by": by})
    return token


def hold(token):
    """commit-331: the claim refreshed - DnsError when its holder lost it (expired and taken by another task)."""
    if not token:
        return
    from django.db import transaction

    with transaction.atomic():
        st = DnsSettings.objects.select_for_update().get(pk=DnsSettings.get().pk)
        if (st.claim or {}).get("token") != token:
            raise DnsError(_("The DNS claim of this task was lost (another DNS task holds it): stopped."))
        DnsSettings.objects.filter(pk=st.pk).update(claim={**st.claim, "at": timezone.now().isoformat()})


def touch(token):
    """The claim refreshed by its holder (each server of an Apply, each poll of a redeploy)."""
    if token:
        from django.db import transaction

        with transaction.atomic():
            st = DnsSettings.objects.select_for_update().get(pk=DnsSettings.get().pk)
            if (st.claim or {}).get("token") == token:
                DnsSettings.objects.filter(pk=st.pk).update(claim={**st.claim, "at": timezone.now().isoformat()})


def release(token):
    from django.db import transaction

    with transaction.atomic():
        st = DnsSettings.objects.select_for_update().get(pk=DnsSettings.get().pk)
        if (st.claim or {}).get("token") == token:
            DnsSettings.objects.filter(pk=st.pk).update(claim={})


def _texts(serial, snap):
    """{zone: its file text} of every zone now, with this serial, from one snapshot."""
    return {z.name: zone_text(z, serial, snap=snap) for z in DnsZone.objects.all()}


def reserve_for_primary(node, first=False):
    """commit-323/330: before a primary is made from its profile - unless the published zones are the same as now, a
    new serial is reserved (never reused) and the zones as they are now are kept as this redeploy's candidate; they
    become the published ones only when the new primary passes its checks (published). Returns the serial."""
    from django.db import transaction

    with transaction.atomic():
        st = DnsSettings.objects.select_for_update().get(pk=DnsSettings.get().pk)
        cand = st.candidate or {}
        if cand.get("first") and cand.get("node") != node.pk:  # commit-354: another VM is becoming the primary
            raise DnsError(_("Another name server's first redeploy as the primary has not ended: finish it first."))
        vms = list(cand.get("vms") or []) if cand.get("first") else []  # the VMs of earlier attempts, kept
        unsure = bool(cand.get("first") and cand.get("unsure"))  # commit-354: kept across retries
        snap = snapshot(st)
        snap["srv"] = as_primary(snap["srv"], node, force=first)  # becoming the primary: its own SOA and NS
        now = _texts(st.serial, snap)
        same = st.published and {n: digest(x) for n, x in st.published.items()} == {
            n: digest(x) for n, x in now.items()
        }
        # commit-343: the published serial only when none above it was reserved (an Apply may have served that one)
        if same and (st.reserved or 0) <= st.serial:
            # commit-342: recorded all the same - what the new primary is given is exactly what becomes published
            st.candidate = {"node": node.pk, "serial": st.serial, "texts": dict(st.published), "first": first}
            if first:
                st.candidate.update(vms=vms, unsure=unsure, rev=_rev())  # commit-363
            st.save(update_fields=["candidate"])
            return st.serial
        serial = next_serial(st.serial, st.reserved)
        st.reserved = serial
        st.candidate = {"node": node.pk, "serial": serial, "texts": _texts(serial, snap), "first": first}
        if first:
            st.candidate.update(vms=vms, unsure=unsure, rev=_rev())  # commit-363
        st.save(update_fields=["reserved", "candidate"])
    return serial


def published(node, ok, handoff=False):
    """After a primary's redeploy: its candidate recorded as published when it passed, dropped otherwise. With no
    primary yet, the VM that passed becomes it (owner 2026-11-22 09:59: the first deployed VM is the primary)."""
    from django.db import transaction

    with transaction.atomic():
        st = DnsSettings.objects.select_for_update().get(pk=DnsSettings.get().pk)
        cand = st.candidate or {}
        if cand.get("node") != node.pk:  # commit-354: another VM's candidate is left as it is
            return
        if ok:
            st.published, st.serial = cand["texts"], max(st.serial, cand["serial"])
            st.uncertain = {}  # commit-338: the new primary passed with these zones (a handoff is kept: its own field)
            if st.primary_id is None or cand.get("first"):  # the first one, or one made the primary (owner 11:51)
                if handoff and st.primary_id not in (None, node.pk):  # commit-362: marked in the same write
                    st.handoff = {"node": node.pk, "from": st.primary_id, "at": timezone.now().isoformat()}
                st.primary_id = node.pk
        st.candidate = {}
        st.save(update_fields=["published", "serial", "candidate", "uncertain", "primary", "handoff"])


def _bind_files(node, snap, zones):
    """The three bind files of the bind service for this server, from one snapshot (commit-321)."""
    st, srv = snap["st"], snap["srv"]
    from .base_deploy import RedeployError, profile_of, templates_of, units_of, values_of
    from .recipes import config_file, render

    prof = profile_of(node.service)  # T34 S2, T36 B5a: the bind files are configs of the ns function
    templates = templates_of(prof) if prof is not None else {}
    try:  # commit-574: this server's own role's units - as its Redeploy renders them
        units = units_of(node, prof) if prof is not None else []
    except RedeployError as exc:
        raise DnsError(str(exc)) from exc
    v = values_of(node, reveal=True)
    v["dns"] = values(node, True, st, srv, zones)
    out = {}
    for path in BIND_FILES:
        found = config_file(prof, templates, path, units=units) if prof is not None else None
        if found is None:
            raise DnsError(_("The ns function has no config %(p)s (Profiles → Functions).") % {"p": path})
        c, body = found
        out[path] = (render(body, v), c.get("mode") or "0644", c.get("owner") or "root:bind")
    return out


GUARD = (
    f"grep -qs {shlex.quote(ZONES_FILE)} /etc/bind/named.conf || "
    "{ echo 'CLAUGINE-UNCHANGED: its named.conf does not include the zone list of Infrastructure -> DNS "
    "(not made from its profile)' >&2; exit 9; }\n"
)


STATE_DIR = "/var/lib/claugine"


def _check_reload(primary):
    check = "named-checkconf -z >/dev/null" if primary else "named-checkconf >/dev/null"
    reload_ = "rndc reconfig >/dev/null && rndc reload >/dev/null" if primary else "rndc reconfig >/dev/null"
    return check, reload_


def recover(primary):
    """commit-322: an earlier attempt cut off while it swapped (its journal "swapping" still there) is undone first,
    from its manifest; if that cannot be done, nothing else is."""
    check, reload_ = _check_reload(primary)
    return (
        f'for d in {STATE_DIR}/dns-*/; do [ -e "$d/swapping" ] || continue; ok=1; '
        'while read -r i how path; do if [ "$how" = had ]; then cp -p "$d/$i.bak" "$path" || ok=0; '
        'else rm -f "$path" || ok=0; fi; done < "$d/manifest"; '
        f'if [ $ok = 1 ] && {check} && {reload_}; then rm -f "$d/swapping" || {{ echo "CLAUGINE-RESTORE-FAILED: recovered $d but its journal could not be cleared" >&2; exit 11; }}; rm -rf "$d"; '
        'echo "recovered an attempt left half way: $d" >&2; '
        'else echo "CLAUGINE-RESTORE-FAILED: an earlier attempt ($d) could not be undone" >&2; exit 11; fi; done'
    )


def rollout(files, attempt, primary):
    """The script that puts files on a name server (commit-322, commit-329):
      1 an earlier attempt left half way is undone first (recover)
      2 every file staged in this attempt's own folder; zone files checked (named-checkzone) - nothing changed yet
      3 every existing file backed up and the copy verified, each one listed in the manifest (had / new); a backup
        that fails stops here, nothing changed
      4 the journal "swapping" written, the files swapped in, checked (named-checkconf, with the zones on the
        primary) and reloaded; then the journal goes
      5 any failure in 4: every file put back from the manifest (a new one removed), each step must succeed, then
        checked and reloaded - RESTORED; otherwise RESTORE-FAILED and the journal stays for the next Apply
    The last line on stderr says how it ended: CLAUGINE-UNCHANGED, CLAUGINE-RESTORED or CLAUGINE-RESTORE-FAILED;
    stdout CLAUGINE-APPLIED."""
    a = f"{STATE_DIR}/dns-{attempt}"
    q = shlex.quote
    check, reload_ = _check_reload(primary)
    # commit-333: an attempt folder lives only while it is needed - removed after a success or a restoration; one left
    # half way (its journal) stays for recovery; folders of earlier versions without a journal go
    sweep = f'for d in {STATE_DIR}/dns-*/; do [ -d "$d" ] && [ ! -e "$d/swapping" ] && rm -rf "$d"; done; true'
    # commit-322: an interrupted attempt is undone before anything else - before the profile check, which a damaged
    # named.conf would fail
    lines = ["set -u", recover(primary), GUARD.rstrip("\n"), sweep, f"install -d -m 0700 {a}"]
    lines.append(f"install -d -o root -g bind -m 0755 {ZONE_DIR}")
    items = list(files.items())
    unchanged = f"rm -rf {a}; echo"
    for i, (path, (text, _mode, _owner)) in enumerate(items):
        b = base64.b64encode(text.encode()).decode()
        lines.append(
            f"echo {b} | base64 -d > {a}/{i}.new || {{ {unchanged} 'CLAUGINE-UNCHANGED: staging' >&2; exit 9; }}"
        )
        if path.startswith(ZONE_DIR + "/"):
            zone = path[len(ZONE_DIR) + 1 : -len(".zone")]
            lines.append(
                f"named-checkzone -q {q(zone)} {a}/{i}.new || "
                f"{{ {unchanged} {q('CLAUGINE-UNCHANGED: named-checkzone ' + zone)} >&2; exit 9; }}"
            )
    lines.append(f": > {a}/manifest")
    for i, (path, _f) in enumerate(items):
        lines.append(
            f"if [ -e {q(path)} ]; then cp -p {q(path)} {a}/{i}.bak && cmp -s {q(path)} {a}/{i}.bak "
            f"&& echo {i} had {q(path)} >> {a}/manifest || {{ {unchanged} {q('CLAUGINE-UNCHANGED: backup ' + path)} >&2; "
            f"exit 9; }}; else echo {i} new {q(path)} >> {a}/manifest || {{ {unchanged} 'CLAUGINE-UNCHANGED: manifest' "
            ">&2; exit 9; }; fi"
        )
    restore = [
        f"if grep -qx {q(f'{i} had {path}')} {a}/manifest; then cp -p {a}/{i}.bak {q(path)} || ok=0; "
        f"else rm -f {q(path)} || ok=0; fi"
        for i, (path, _f) in enumerate(items)
    ]
    lines.append(
        "fail() { ok=1; " + "; ".join(restore) + f"; if [ $ok = 1 ] && {check} && {reload_}; then "
        # commit-339: the journal goes first, as its own checked step; only then the folder (backups included)
        f'rm -f {a}/swapping || {{ echo "CLAUGINE-RESTORE-FAILED: $1 - put back, but the journal could not be cleared" >&2; exit 11; }}; '
        f"rm -rf {a}; "
        'echo "CLAUGINE-RESTORED: $1" >&2; exit 10; else echo "CLAUGINE-RESTORE-FAILED: $1" >&2; exit 11; fi; }'
    )
    lines.append(f"touch {a}/swapping || {{ {unchanged} 'CLAUGINE-UNCHANGED: journal' >&2; exit 9; }}")
    for i, (path, (_t, mode, owner)) in enumerate(items):
        u, _sep, g = owner.partition(":")
        lines.append(f"install -m {mode} -o {u} -g {g or 'root'} {a}/{i}.new {q(path)} || fail {q('install ' + path)}")
    lines.append(f"{check} || fail named-checkconf")
    lines.append(f"{reload_} || fail rndc")
    # commit-339: the journal cleared first, checked - a journal left behind would make the next Apply undo this one
    lines.append(
        f"rm -f {a}/swapping || {{ echo 'CLAUGINE-JOURNAL-KEPT: applied, but {a}/swapping could not be removed - "
        "remove it by hand before the next Apply' >&2; exit 12; }"
    )
    lines.append(f"rm -rf {a}")
    lines.append("echo CLAUGINE-APPLIED")
    return "\n".join(lines) + "\n"


def _outcome(exc):
    s = str(exc)
    for mark, word in (
        ("CLAUGINE-UNCHANGED", _("not changed")),
        ("CLAUGINE-RESTORED", _("put back as it was")),
        ("CLAUGINE-RESTORE-FAILED", _("NOT put back - check it by hand")),
        ("CLAUGINE-JOURNAL-KEPT", _("applied, but its journal was not cleared - check it by hand")),
    ):
        if mark in s:
            return word, s.split(mark + ":", 1)[-1].strip()[:300]
    return _("state unknown (no answer from it) - check it"), s[:300]


def apply(task, user=None, token=None):
    """The zones and the bind files on every server, from one snapshot (commit-321): the primary first (its zone files
    and its bind files), then each secondary's bind files; then check. A server not made from its profile is refused
    (nothing is written there). The caller holds the DNS claim (token), refreshed at each server (commit-331)."""
    import secrets

    from accounts.models import audit

    from . import hostlogging

    if (DnsSettings.get().candidate or {}).get("first"):  # commit-361: before any file or serial
        raise DnsError(
            _(
                "A name server is becoming the primary and its redeploy has not ended: Apply waits for it (redeploy it "
                "again or roll it back)."
            )
        )
    snap = snapshot()
    st, srv = snap["st"], snap["srv"]
    primary = next((x for x in srv if x["role"] == "primary"), None)
    if primary is None:
        raise DnsError(_("No primary name server (DNS servers → Edit)."))
    serial = reserve_serial()  # commit-337
    texts = _texts(serial, snap)
    zones = [{"name": n, "text": x} for n, x in sorted(texts.items())]
    attempt = f"{timezone.now():%Y%m%d%H%M%S}-{secrets.token_hex(3)}"
    files = {f"{ZONE_DIR}/{n}.zone": (x, "0644", "root:bind") for n, x in sorted(texts.items())}
    files.update(_bind_files(primary["node"], snap, zones))
    sec_files = {x["host"]: _bind_files(x["node"], snap, zones) for x in srv if x is not primary}
    touch(token)
    try:
        hostlogging.node_ssh(primary["node"])(rollout(files, attempt, True), timeout=300)
    except Exception as exc:  # noqa: BLE001 - nothing more is done
        how, why = _outcome(exc)
        task.step(f"primary {primary['host']}", "failed", f"{how}: {why}")
        audit(user, "dns_apply_failed", None, server=primary["host"], outcome=how)
        raise DnsError(
            _("The primary %(h)s: %(how)s (%(why)s).") % {"h": primary["host"], "how": how, "why": why}
        ) from exc
    task.step(f"primary {primary['host']}", "ok", f"{len(texts)} zones and the bind files, serial {serial}")
    from django.db import transaction

    with transaction.atomic():
        cur = DnsSettings.objects.select_for_update().get(pk=st.pk)
        cur.serial, cur.applied_at, cur.published = serial, timezone.now(), texts
        cur.applied = {n: [digest(x), hashlib.sha256(x.encode()).hexdigest()] for n, x in texts.items()}
        cur.uncertain = {}  # commit-338: the primary serves what was just applied (a handoff stays: its own field)
        cur.save(update_fields=["serial", "applied_at", "applied", "published", "uncertain"])
    failed = []
    for x in srv:
        if x is primary:
            continue
        touch(token)
        try:
            hostlogging.node_ssh(x["node"])(rollout(sec_files[x["host"]], attempt, False), timeout=120)
            task.step(f"secondary {x['host']}", "ok", _("bind files written"))
        except Exception as exc:  # noqa: BLE001 - the others go on
            how, why = _outcome(exc)
            failed.append(x["host"])
            task.step(f"secondary {x['host']}", "failed", f"{how}: {why}")
    audit(user, "dns_applied", None, serial=serial, zones=len(texts), failed=",".join(failed))
    if not failed:  # commit-362: every server follows the primary - a handoff is over
        DnsSettings.objects.filter(pk=st.pk).exclude(handoff={}).update(handoff={})
    touch(token)
    result = check(task, token)  # commit-331: it refreshes the claim at each server too
    if failed:
        raise DnsError(_("Applied on the primary; not on %(s)s.") % {"s": ", ".join(failed)})
    return result


def check_script(zones, primary):
    """The read-only script of Check on one server: the serial each zone answers, the primary's zone files' hashes,
    and the attempts left half way (commit-322). It ends well whatever it finds (commit-336: no journal is no error)."""
    q = " ".join(shlex.quote(z) for z in zones)
    script = (
        f'for z in {q}; do printf \'%s \' "$z"; dig +short +norecurse +time=2 +tries=1 SOA "$z" @127.0.0.1 '
        "| awk '{print $3}' | head -n1; echo; done\n"
    )
    if primary:
        script += (
            f"echo '--files'; for z in {q}; do printf '%s ' \"$z\"; sha256sum {ZONE_DIR}/\"$z\".zone 2>/dev/null "
            "| cut -d' ' -f1; echo; done\n"
        )
    script += f'echo \'--halfway\'; for j in {STATE_DIR}/dns-*/swapping; do [ -e "$j" ] && echo "$j"; done; true\n'
    return script


def reserve_serial():
    """commit-337: the next serial, reserved under the row lock before any server is contacted - kept whatever
    happens, so a serial a server may have served is never given to other records."""
    from django.db import transaction

    with transaction.atomic():
        st = DnsSettings.objects.select_for_update().get(pk=DnsSettings.get().pk)
        serial = next_serial(st.serial, st.reserved)
        DnsSettings.objects.filter(pk=st.pk).update(reserved=serial)
    return serial


def mark_uncertain(node, why, keep_candidate=False):
    """commit-338: before a primary's VM changes - what it serves is not known until an Apply succeeds (the mark
    outlives a task cut off); the published zones are dropped, and so are the last Apply and Check (commit-340: they
    describe a VM that is no longer there); the serial and its reservation are kept. keep_candidate: a redeploy's
    candidate, already rendered into the new primary's recipe (commit-342)."""
    DnsSettings.objects.filter(pk=DnsSettings.get().pk).update(
        uncertain={"node": node.pk, "why": why, "at": timezone.now().isoformat()},
        published={},
        applied={},
        last_check={},
        **({} if keep_candidate else {"candidate": {}}),
    )


def republish(task, user, token):
    """commit-338: after a primary is rolled back (by hand or by itself once it may have served): what it serves is
    not known - the published zones are dropped and the zones applied again under a new serial; when the restored
    primary is not made from its profile, the task says Apply is needed."""
    DnsSettings.objects.filter(pk=DnsSettings.get().pk).update(published={}, candidate={}, applied={}, last_check={})
    try:
        apply(task, user, token)  # a success clears the mark
        return True
    except DnsError as exc:
        task.step("zones", "failed", f"{exc} - Apply them (Infrastructure → DNS) once the primary can take them")
        return False


def check(task=None, token=None):
    """Read only: each zone's file on the primary against what was applied, and the serial each server answers.
    token: the DNS claim its caller holds, refreshed before each server (commit-331: a Check after an Apply, or on its
    own, never outlasts the claim)."""
    from . import hostlogging

    st = DnsSettings.get()
    d = domain()
    srv = servers(st, d)
    zones = [z.name for z in DnsZone.objects.all()]
    # commit-327: the configuration it was checked against; the page needs the same now to call a server in sync
    rev = revision({"st": st, "soa": soa(st), "domain": d, "srv": srv})
    out = {"at": timezone.now().isoformat(), "revision": rev, "servers": {}, "files": {}}
    for x in srv:
        hold(token)  # commit-331: stops when the claim was lost
        script = check_script(zones, x["role"] == "primary")
        try:
            text = hostlogging.node_ssh(x["node"])(script, timeout=120) or ""
        except Exception as exc:  # noqa: BLE001 - shown
            out["servers"][x["host"]] = {"error": str(exc)[:200]}
            if task:
                task.step(f"check {x['host']}", "failed", str(exc)[:200])
            continue
        serials, files, halfway = {}, {}, []
        cur = serials
        for line in text.splitlines():
            if line.strip() in ("--files", "--halfway"):
                cur = files if line.strip() == "--files" else None
                continue
            parts = line.split()
            if cur is None:
                if parts:
                    halfway.append(parts[0])
            elif parts:
                cur[parts[0]] = parts[1] if len(parts) > 1 else ""
        out["servers"][x["host"]] = {"serials": serials, "role": x["role"], "ip": x["ip"], "halfway": halfway}
        if x["role"] == "primary":  # each zone file as applied, changed on the server, or missing
            out["files"] = {
                z: (
                    "as applied"
                    if files.get(z) and files.get(z) == ((st.applied or {}).get(z) or [None, None])[1]
                    else "missing"
                    if not files.get(z)
                    else "changed on the server"
                )
                for z in zones
            }
        if task:
            behind = [z for z in zones if serials.get(z) != str(st.serial)]
            task.step(
                f"check {x['host']}",
                "ok" if not behind else "failed",
                f"serial {st.serial} on {len(zones) - len(behind)}/{len(zones)} zones",
            )
    from django.db import transaction

    with transaction.atomic():  # commit-331: saved only by the claim's holder, checked in the same write
        cur = DnsSettings.objects.select_for_update().get(pk=st.pk)
        if token and (cur.claim or {}).get("token") != token:
            raise DnsError(_("The DNS claim of this task was lost (another DNS task holds it): stopped."))
        DnsSettings.objects.filter(pk=st.pk).update(last_check=out)
    return out


# --- the one import of the present zones (owner 12:42: by Claude, once, while building) -----------------------------


def import_zone(name, text, user=None):
    """Add what a zone file has beyond the automatic records: the zone (added, when the portal does not make it) and
    its records. ``text`` is the canonical form (named-compilezone -s full: "fqdn. TTL IN TYPE rdata" per line); the
    SOA and the zone's own NS records are the portal's. Returns (zone, added, skipped)."""
    from accounts.models import audit

    from .models import DnsRecord

    name = name.strip().strip(".").lower()
    if not ZONE_NAME.match(name):
        raise DnsError(f"{name}: not a zone name")
    sync_zones()
    kind = DnsZone.Kind.REVERSE if name.endswith(".in-addr.arpa") else DnsZone.Kind.DIRECT
    zone, _new = DnsZone.objects.get_or_create(name=name, defaults={"kind": kind, "source": ""})
    auto = automatic()[0]
    have = {(r["name"], r["rtype"], _value(r["rtype"], r["value"]).rstrip(".").lower()) for r in rows(zone, auto)}
    default_ttl = soa()["ttl"]
    added = skipped = 0
    for line in text.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5 or parts[0].startswith(";") or parts[2].upper() != "IN":
            continue
        fqdn, ttl, _in, rtype, rdata = parts
        rtype = rtype.upper()
        fqdn = fqdn.rstrip(".").lower()
        if fqdn == name:
            rel = "@"
        elif fqdn.endswith("." + name):
            rel = fqdn[: -len(name) - 1]
        else:
            skipped += 1
            continue
        if rtype == "SOA" or (rtype == "NS" and rel == "@") or rtype not in DnsRecord.TYPES:
            skipped += 1
            continue
        rdata = " ".join(rdata.split())
        if (rel, rtype, _value(rtype, rdata).rstrip(".").lower()) in have:
            skipped += 1
            continue
        DnsRecord.objects.create(
            zone=zone, name=rel, rtype=rtype, value=rdata, ttl=None if int(ttl) == default_ttl else int(ttl)
        )
        have.add((rel, rtype, _value(rtype, rdata).rstrip(".").lower()))
        added += 1
    audit(user, "dns_zone_imported", zone, zone=name, added=added, skipped=skipped)
    return zone, added, skipped
