"""The tenants' igw VMs as they are on the platforms (2026-10-29): the igw always holds the address right after the egw's
on the main subnet, whatever its name. Its image tells its type: a VyOS image → vyos (advanced), the Service Virtual
Router's → onevr (native), anything else → custom. Read only on the platforms; the portal keeps what it found
(Tenant.igw_vm) and fills the igw type where the tenant's connectivity has none (a new connectivity version, audited).
Run with every tenants refresh (collect_platforms) and by hand."""

import ipaddress

from django.utils import timezone


def addresses(tenant, site=None):
    """(egw, igw) addresses on the tenant's main subnet, or None."""
    from .models import PrefixAssignment

    site = site or (tenant.order or {}).get("site")
    p = PrefixAssignment.active().filter(tenant=tenant, kind="inet_main", site=site).first() if site else None
    if p is None:
        p = PrefixAssignment.active().filter(tenant=tenant, kind="inet_main").first()
    if p is None:
        return None
    net = ipaddress.ip_network(p.cidr)
    hosts = list(net) if net.prefixlen >= 31 else list(net.hosts())
    return (str(hosts[0]), str(hosts[1])) if len(hosts) >= 2 else None


def type_of(images):
    text = " ".join(images).lower()
    if "vyos" in text:
        return "vyos"
    if any(w in text for w in ("service virtual router", "vrouter", "virtual router", "one-router", "onevr", "svr")):
        return "onevr"
    return "custom"


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f == int(f) else f


def _platform_vms(control):
    """{ip: vm} of every VM on the platform (all groups)."""
    from inventory.core_services import vm_status

    from .deploy import One

    x = One(control).xml("one.vmpool.info", -2, -1, -1, -1)
    out = {}
    for v in x.findall("VM"):
        t = v.find("TEMPLATE")
        hist = v.findall("HISTORY_RECORDS/HISTORY")
        vm = {
            "id": int(v.findtext("ID") or 0),
            "name": v.findtext("NAME") or "",
            "group": v.findtext("GNAME") or "",
            "status": vm_status(v.findtext("STATE") or "", v.findtext("LCM_STATE") or ""),
            "host": hist[-1].findtext("HOSTNAME") if hist else "",
            "images": [d.findtext("IMAGE") or "" for d in t.findall("DISK")] if t is not None else [],
            # owner 2026-10-29 18:58: the size of every igw found (VyOS or not)
            "cpu": _num(t.findtext("VCPU") or t.findtext("CPU")) if t is not None else None,
            "ram_gb": round((_num(t.findtext("MEMORY")) or 0) / 1024)
            if t is not None
            else None,  # whole GB (owner 20:59)
            "disks_gb": [round((_num(d.findtext("SIZE")) or 0) / 1024) for d in t.findall("DISK")]
            if t is not None
            else [],
            # owner 2026-10-29 20:59: the igw's NICs in its details: [network, address]
            "nics": [
                [n.findtext("NETWORK") or "", n.findtext("IP") or n.findtext("IP6") or ""] for n in t.findall("NIC")
            ]
            if t is not None
            else [],
        }
        for n in (t.findall("NIC") + t.findall("NIC_ALIAS")) if t is not None else []:
            if n.findtext("IP"):
                out.setdefault(n.findtext("IP"), []).append(vm)
    return out


def detect(tenants=None, user=None, record=True):
    """Find the igw VM of every tenant with a main subnet. Returns [(tenant, found or None, note)]."""
    from accounts.models import audit
    from inventory.models import PlatformControl

    from .models import Connectivity, Lifecycle, Tenant

    if tenants is None:
        tenants = (
            Tenant.objects.exclude(status=Lifecycle.ARCHIVED)
            .filter(prefixes__kind="inet_main", prefixes__released_at__isnull=True)
            .distinct()
        )
    by_platform, out = {}, []
    for t in tenants:
        by_platform.setdefault((t.order or {}).get("platform") or "", []).append(t)
    for key, group in by_platform.items():
        c = PlatformControl.objects.filter(key=key).first()
        if c is None or not c.configured:
            out += [(t, None, f"{key or 'no platform'}: not readable") for t in group]
            continue
        try:
            vms = _platform_vms(c)
        except Exception as exc:  # noqa: BLE001 - per platform
            out += [(t, None, f"{key}: {type(exc).__name__}: {exc}"[:200]) for t in group]
            continue
        for t in group:
            ips = addresses(t)
            if not ips:
                out.append((t, None, "no main subnet"))
                continue
            egw_ip, igw_ip = ips
            cands = [v for v in vms.get(igw_ip, []) if v["status"] != "done"]
            mine = [v for v in cands if v["group"] == t.group_name] or cands
            vm = mine[0] if mine else None
            found = None
            if vm is not None:
                found = {
                    "id": vm["id"],
                    "name": vm["name"],
                    "ip": igw_ip,
                    "egw_ip": egw_ip,
                    "group": vm["group"],
                    "images": vm["images"],
                    "type": type_of(vm["images"]),
                    "status": vm["status"],
                    "host": vm["host"],
                    "cpu": vm.get("cpu"),
                    "ram_gb": vm.get("ram_gb"),
                    "disks_gb": vm.get("disks_gb") or [],
                    "nics": vm.get("nics") or [],
                    "platform": key,
                    "at": timezone.now().isoformat(),
                }
            note = ""
            if record:
                t.igw_vm = found or {
                    "ip": igw_ip,
                    "egw_ip": egw_ip,
                    "missing": True,
                    "platform": key,
                    "at": timezone.now().isoformat(),
                }
                fields = ["igw_vm"]
                if not (t.igw_host or "").strip():  # u10: keep the igw address from the platform when none is set
                    t.igw_host = igw_ip
                    fields.append("igw_host")
                t.save(update_fields=fields)
                site = (t.order or {}).get("site") or ""
                cur = next((x for x in Connectivity.current_for(t) if x.site == site), None)
                if found and cur is not None and not cur.igw_type and "inet" in cur.types:
                    vals = cur.values()
                    vals["igw_type"] = found["type"]
                    Connectivity.objects.create(
                        tenant=t, site=site, version=Connectivity.next_version(t, site), created_by=user, **vals
                    )
                    audit(
                        user,
                        "igw_type_detected",
                        t,
                        vm=found["name"],
                        vm_id=found["id"],
                        image=", ".join(found["images"]),
                        igw_type=found["type"],
                    )
                    note = "type recorded"
                elif found and cur is not None and cur.igw_type and cur.igw_type != found["type"]:
                    note = f"recorded {cur.igw_type}, the VM says {found['type']}"
            out.append((t, found, note))
    return out
