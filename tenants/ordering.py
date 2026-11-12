"""Tenant orders: what a customer may ask for, and turning an approved order into records.

Order (Tenant.order) keys:
    cpu, ram_gb, disk_gb                      resources asked for
    connections [..]                          any of "inet", "corpnet" (corporate organizations only), "l2";
                                              at least one. Older orders: connection_type + l2.
    inet_mbps, inet_main_prefix,
    inet_extra_prefixes [lengths]             internet only
    corpnet_prefixes [lengths]                corporate network only
    note                                      free text from the customer
Chosen by the resource manager on approval:
    platform (inventory key), site, cluster (quota code), frontend, inet_pool
Public and corporate prefixes come only from the exchange (IX) networks of that platform;
each IX network serves exactly one shared front end, so front ends never share addresses.
Filled in when prefixes are allocated:
    allocated [{"kind", "cidr"}]
"""

import ipaddress

from django.db import transaction
from django.utils import timezone

PREFIX_LENGTHS = (31, 29, 28, 27, 26)
MBPS_MIN, MBPS_MAX, MBPS_DEFAULT = 10, 1000, 100
DEFAULT_RESOURCES = {"cpu": 4, "ram_gb": 16, "disk_gb": 100, "files_gb": 0, "backups_gb": 0}
FILES_FREE_GB = 1  # file storage up to this size is free
MAX_EXTRA_PREFIXES = 8


def prefix_lengths_for(purpose="inet", site=None):
    """u10: the project prefix lengths a site offers for a purpose — 31 then 29, 28 … down to the most
    permissive ``max`` among that site's IP subnets of the purpose (the smallest max_prefix number = the
    largest block). Falls back to the fixed PREFIX_LENGTHS when no such subnet is defined yet.

    ``site`` may be a Site, a site code, or None (across every site)."""
    from inventory.models import IPSubnet

    qs = IPSubnet.objects.filter(purpose=purpose)
    if site is not None:
        qs = qs.filter(site__code=site) if isinstance(site, str) else qs.filter(site=site)
    tops = list(qs.values_list("max_prefix", flat=True))
    if not tops:
        return tuple(PREFIX_LENGTHS)
    top = max(8, min(tops))  # smallest number = largest block = most permissive
    return tuple([31] + [n for n in range(29, top - 1, -1)])


class AllocationError(Exception):
    pass


def connections(order):
    """The connections of an order: a list of "inet", "corpnet", "l2"."""
    if "connections" in order:
        return list(order.get("connections") or [])
    out = [c for c in (order.get("connection_type"),) if c in ("inet", "corpnet")]
    return out + (["l2"] if order.get("l2") else [])


def describe(order):
    """Short human summary of an order's connectivity."""
    conns = connections(order)
    parts = []
    if "inet" in conns:
        text = f"Internet {order.get('inet_mbps')} Mbps, main /{order.get('inet_main_prefix')}"
        extra = order.get("inet_extra_prefixes") or []
        if extra:
            text += ", additional " + " ".join(f"/{x}" for x in extra)
        parts.append(text)
    if "corpnet" in conns:
        parts.append("Corporate network " + " ".join(f"/{x}" for x in order.get("corpnet_prefixes") or []))
    if "l2" in conns:
        parts.append("external L2 channel")
    return "; ".join(parts)


# --- address allocation ---------------------------------------------------------


def _net(text):
    try:
        return ipaddress.ip_network(str(text).strip(), strict=False)
    except ValueError:
        return None


def used_networks(site):
    """Everything known to be taken at a site: the registry and OpenNebula metadata."""
    from .models import PrefixAssignment

    used = [n for n in (_net(p.cidr) for p in PrefixAssignment.active().filter(site=site)) if n]
    try:
        from adoption.observations import latest_observations

        key = f"QUOTA_{site.upper()}"
        for o in (latest_observations() or {}).values():
            q = (o.template or {}).get(key)
            if not isinstance(q, dict):
                continue
            pairs = [(q.get("INET_MAIN_SUBNET"), q.get("INET_MAIN_PREFIX"))]
            for subs, lens in (
                ("INET_PUBLIC_SUBNETS", "INET_PUBLIC_PREFIXES"),
                ("CORPNET_SUBNETS", "CORPNET_PREFIXES"),
            ):
                pairs += list(zip(str(q.get(subs) or "").split(), str(q.get(lens) or "").split(), strict=False))
            for addr, length in pairs:
                if addr:
                    n = _net(f"{addr}/{length}" if length and "/" not in str(addr) else addr)
                    if n:
                        used.append(n)
    except Exception:
        pass
    # what OpenNebula really uses: the address ranges of all virtual networks of the site's platforms
    try:
        import ipaddress

        from adoption.models import PlatformStats
        from inventory.models import Platform

        keys = list(Platform.objects.filter(site__code=site).values_list("key", flat=True))
        for st in PlatformStats.objects.filter(platform_key__in=keys):
            for _name, ip, size in st.address_ranges or []:
                try:
                    first = ipaddress.ip_address(ip)
                except ValueError:
                    continue
                if size > 0:
                    used.extend(ipaddress.summarize_address_range(first, first + (int(size) - 1)))
    except Exception:
        pass
    return used


def pools(platform_key, purpose):
    """Delegated subnets of the IX networks a platform is connected to."""
    from inventory.models import Platform

    plat = Platform.objects.filter(key=platform_key).select_related("site").first()
    if plat is None:
        return []
    rows = list(plat.delegated_subnets(purpose))
    rows.sort(key=lambda d: (0 if d.segment.startswith("cloud") else 1, d.segment, d.cidr))
    return rows


def service_blocks(site=None):
    """[(site, network, segment name)]: site networks of the inventory (e.g. cloud-services) whose subnet lies inside
    a public pool of a shared platform. They are the service networks on the management clusters."""
    from inventory.models import DelegatedSubnet, Platform, Segment

    pools_by_site = {}
    for plat in Platform.objects.filter(kind="shared").select_related("site"):
        if site and plat.site.code != site:
            continue
        for d in plat.delegated_subnets(DelegatedSubnet.Purpose.INET):
            n = _net(d.cidr)
            if n:
                pools_by_site.setdefault(plat.site.code, set()).add(n)
    out = []
    for seg in Segment.objects.select_related("site").exclude(cidr=""):
        n = _net(seg.cidr)
        pools = pools_by_site.get(seg.site.code, set())
        if n and any(n.version == p.version and n.subnet_of(p) and n != p for p in pools):
            out.append((seg.site.code, n, seg.name))
    return out


def ensure_service_reservations(actor=None):
    """Keep the registry in line with the inventory's service networks inside public pools.

    Each becomes a "platform services" assignment; smaller reservations inside it are folded in, and service
    assignments the inventory no longer has are released. Returns the number of blocks added.
    """
    from .models import PrefixAssignment, Source

    wanted = {(site, str(n)): name for site, n, name in service_blocks()}
    now = timezone.now()
    for p in PrefixAssignment.active().filter(kind=PrefixAssignment.Kind.SERVICES):
        if (p.site, p.cidr) not in wanted:
            p.released_at = now
            p.save(update_fields=["released_at"])
    added = 0
    for (site, cidr), name in sorted(wanted.items()):
        active = PrefixAssignment.active().filter(site=site)
        if active.filter(cidr=cidr, kind=PrefixAssignment.Kind.SERVICES).exists():
            continue
        block = _net(cidr)
        inside = [
            p
            for p in active.exclude(kind=PrefixAssignment.Kind.SERVICES)
            if p.tenant_id is None
            and _net(p.cidr)
            and _net(p.cidr).version == block.version
            and _net(p.cidr).subnet_of(block)
        ]
        notes = "; ".join(f"{p.cidr} {p.note}".strip() for p in inside)
        for p in inside:
            p.released_at = now
            p.save(update_fields=["released_at"])
        PrefixAssignment.objects.create(
            cidr=cidr,
            site=site,
            kind=PrefixAssignment.Kind.SERVICES,
            source=Source.MANUAL,
            note=(f"{name}: service network on the management cluster" + (f" (includes {notes})" if notes else ""))[
                :200
            ],
            created_by=actor if getattr(actor, "pk", None) else None,
        )
        added += 1
    return added


def _first_free(pool, length, taken):
    for cand in pool.subnets(new_prefix=length):
        if not any(cand.overlaps(t) for t in taken):
            return cand
    return None


def _base_subnets(purpose):
    """The IX (BGP-announced) delegated subnets of a purpose a project's prefixes can be carved from, by platform.

    owner 2026-11-03 (u11): the orderer may pick a base subnet per prefix, or leave it on "auto" (the portal picks,
    cloud pools first — the old behaviour). Returns (choices, meta): ``choices`` = [(cidr, label)] for a dropdown,
    cloud segments first; ``meta`` = {cidr: {"platforms": [platform keys that reach it], "label": label}} so the
    order form shows only the subnets of the platform the chosen cluster sits on.
    """
    meta, order, seen = {}, [], set()
    for p in resource_pools():
        pkey = p["key"].split("/", 1)[0]
        if pkey in seen:
            continue
        seen.add(pkey)
        for d in pools(pkey, purpose):
            e = meta.get(d.cidr)
            if e is None:
                meta[d.cidr] = {
                    "platforms": [pkey],
                    "label": f"{d.segment} · {d.cidr}",
                    "cloud": d.segment.startswith("cloud"),
                }
                order.append(d.cidr)
            elif pkey not in e["platforms"]:
                e["platforms"].append(pkey)
    order.sort(key=lambda c: (0 if meta[c]["cloud"] else 1, meta[c]["label"]))
    choices = [(c, meta[c]["label"]) for c in order]
    for c in meta:
        meta[c].pop("cloud", None)
    return choices, meta


def inet_base_subnets():
    """The INET (public) IX base subnets, by platform. See _base_subnets."""
    from inventory.models import DelegatedSubnet

    return _base_subnets(DelegatedSubnet.Purpose.INET)


def corpnet_base_subnets():
    """The CORPNET IX base subnets, by platform. owner 2026-11-05: corporate prefixes pick a base like the public ones."""
    from inventory.models import DelegatedSubnet

    return _base_subnets(DelegatedSubnet.Purpose.CORPNET)


def _max_prefix_map(site):
    """{normalized cidr: max_prefix} for a site's IP subnets — the smallest prefix length (largest block) each allows.
    owner 2026-11-05 (doc-gaps §5a): a block carved from a subnet is never bigger than that subnet's max_prefix."""
    from inventory.models import IPSubnet

    out = {}
    for ip in IPSubnet.objects.filter(site__code=site):
        try:
            out[str(ipaddress.ip_network(ip.cidr, strict=False))] = ip.max_prefix
        except ValueError:
            pass
    return out


def plan_prefixes(order, platform_key, inet_pool=None):
    """[(kind, cidr)] for an order placed on a platform, without saving. Raises AllocationError.

    Each public prefix carries its own base subnet (``inet_main_pool`` and ``inet_extra_pools``, a list parallel to
    ``inet_extra_prefixes``); an empty one means auto (cloud pools first). ``inet_pool`` overrides the main prefix's
    base subnet — the resource manager's choice at approval wins over what was ordered for the main prefix.
    """
    from inventory.models import DelegatedSubnet

    from .models import PrefixAssignment

    conns = connections(order)
    groups = []  # (purpose, [(kind, length, base_cidr_or_"")])
    if "inet" in conns:
        extras = [int(x) for x in order.get("inet_extra_prefixes") or []]
        extra_pools = list(order.get("inet_extra_pools") or [])
        extra_pools += [""] * (len(extras) - len(extra_pools))
        main_pool = inet_pool or (order.get("inet_main_pool") or "")
        groups.append(
            (
                DelegatedSubnet.Purpose.INET,
                [(PrefixAssignment.Kind.INET_MAIN, int(order["inet_main_prefix"]), main_pool)]
                + [(PrefixAssignment.Kind.INET_EXTRA, ln, extra_pools[i] or "") for i, ln in enumerate(extras)],
            )
        )
    if "corpnet" in conns:
        cprefixes = [int(x) for x in order.get("corpnet_prefixes") or []]
        cpools = list(order.get("corpnet_pools") or [])
        cpools += [""] * (len(cprefixes) - len(cpools))
        groups.append(
            (
                DelegatedSubnet.Purpose.CORPNET,
                [(PrefixAssignment.Kind.CORPNET, ln, cpools[i] or "") for i, ln in enumerate(cprefixes)],
            )
        )
    if not groups:
        return []
    from inventory.models import Platform

    plat = Platform.objects.filter(key=platform_key).select_related("site").first()
    if plat is None:
        raise AllocationError(f"platform {platform_key} is not in the inventory")
    site = plat.site.code
    taken = used_networks(site)
    maxp = _max_prefix_map(site)
    out = []
    for purpose, wanted in groups:
        all_nets = [(d.cidr, n) for d in pools(platform_key, purpose) if (n := _net(d.cidr))]
        if purpose == DelegatedSubnet.Purpose.INET:
            taken += [n for _s, n, _name in service_blocks(site)]  # never handed to tenants
        if not all_nets:
            raise AllocationError(
                f"platform {platform_key} is connected to no {purpose} exchange network with delegated subnets"
            )
        # Largest blocks first keeps the pools from fragmenting.
        for kind, length, base in sorted(wanted, key=lambda w: w[1]):
            if base:  # prefer the chosen base subnet, the others as fallback
                nets = [n for c, n in all_nets if c == base] + [n for c, n in all_nets if c != base]
            else:
                nets = [n for _c, n in all_nets]
            got = None
            for pool in nets:
                if length < pool.prefixlen:
                    continue
                cap = maxp.get(str(pool))
                if cap is not None and length < cap:
                    continue  # §5a: never hand out a block bigger than this subnet's max_prefix
                got = _first_free(pool, length, taken)
                if got:
                    break
            if got is None:
                raise AllocationError(f"no free /{length} left in the {purpose} pools of {platform_key}")
            taken.append(got)
            out.append((kind, str(got)))
    order_index = {
        PrefixAssignment.Kind.INET_MAIN: 0,
        PrefixAssignment.Kind.INET_EXTRA: 1,
        PrefixAssignment.Kind.CORPNET: 2,
    }
    return sorted(out, key=lambda x: (order_index.get(x[0], 9), ipaddress.ip_network(x[1])))


def allocate(tenant, user, inet_pool=None):
    """Reserve the prefixes of a tenant's order in the address registry (idempotent)."""
    from .models import PrefixAssignment

    site = tenant.order.get("site")
    if PrefixAssignment.active().filter(tenant=tenant).exists():
        return list(PrefixAssignment.active().filter(tenant=tenant))
    plan = plan_prefixes(tenant.order, tenant.order.get("platform"), inet_pool)
    rows = [
        PrefixAssignment.objects.create(
            cidr=cidr, site=site, kind=kind, tenant=tenant, created_by=user, note="reserved by order approval"
        )
        for kind, cidr in plan
    ]
    tenant.order = {**tenant.order, "allocated": [{"kind": k, "cidr": c} for k, c in plan]}
    return rows


def release(tenant, user=None):
    from .models import PrefixAssignment

    return PrefixAssignment.active().filter(tenant=tenant).update(released_at=timezone.now())


def bundle_of(profile, cluster=None):
    """One GPU bundle of a host (correction 14): its share of the host's cores and RAM per GPU, less the cluster's
    reserve — vCPU rounded down, RAM rounded down to a multiple of 8 GB."""
    import math

    gpus = profile.gpu_count or 1
    cpu_pct = getattr(cluster, "reserved_cpu_pct", 0) or 0
    ram_pct = getattr(cluster, "reserved_ram_pct", 0) or 0
    cores = math.floor(profile.cores / gpus * (100 - cpu_pct) / 100)
    ram = math.floor(profile.ram_gb / gpus * (100 - ram_pct) / 100 / 8) * 8
    return {"cores": max(cores, 0), "ram_gb": max(ram, 0)}


def gpu_short(model):
    """'NVIDIA H200 NVL' → 'H200': the first word with a digit (else the last word)."""
    words = [w for w in str(model or "").replace("NVIDIA", "").split() if w]
    return next((w for w in words if any(ch.isdigit() for ch in w)), words[-1] if words else "GPU")


def resource_pools():
    """Orderable resource pools (shared clusters) for the order form.

    Each: key "<platform>/<quota code>", site, label and, for a GPU pool, the bundle one GPU card comes with and how
    many bundles are still free.

    The bundle is the cluster's setting (``Cluster.bundle()``), not a figure worked out here from the reserve
    percentages - those stopped applying to GPU clusters on 2026-11-09, because the head-room is inside the bundle.
    "Free" is the cluster's cards less the cards in its spare hosts less the cards already allocated: a spare is
    there to absorb a failure, so what it carries cannot be ordered.
    """
    from inventory.capacity import current_allocations
    from inventory.models import Cluster

    alloc = current_allocations()
    out = []
    for c in Cluster.objects.select_related("platform", "platform__site").prefetch_related("hosts__profile"):
        if not c.orderable:
            continue
        code = c.order_code
        profiles = {h.profile for h in c.hosts.all() if h.profile and h.state == "active"}
        gpu_profiles = [p for p in profiles if p.gpu_count]
        cpu_models = sorted({p.cpu_model for p in profiles if p.cpu_model})
        pool = {
            "key": f"{c.platform.key}/{code}",
            "site": c.platform.site.code,
            "site_city": c.platform.site.city,
            "quota_code": code,
            "cluster": c.name,
            "cpu_models": cpu_models,
            "gpu": None,
        }
        if gpu_profiles:
            slice_ = c.bundle()
            p0 = gpu_profiles[0]
            used = (alloc.get((c.platform.site.code, code)) or {}).get("gpu", 0)
            free = max(c.gpu_cards()["available"] - used, 0)
            pool["gpu"] = {
                "cores": slice_["cores"],
                "ram_gb": slice_["ram_gb"],
                "model": p0.gpu_model,
                "vram_gb": p0.gpu_vram_gb,
                "per_host": min(p.gpu_count for p in gpu_profiles),
                "free": free,
            }
            pool["label"] = (
                f"{c.name} · GPU {p0.gpu_model} {p0.gpu_vram_gb} GB · "
                f"1 bundle = 1 GPU + {slice_['cores']} vCPU + {slice_['ram_gb']} GB RAM"
            )
        else:
            pool["label"] = f"{c.name} · " + (", ".join(cpu_models) or "CPU")
        out.append(pool)
    return sorted(out, key=lambda p: (p["site"], p["gpu"] is not None, p["quota_code"]))


def images_share(disk_gb, vms_pct=None):
    from .models import VMS_SHARE_DEFAULT
    from .models import images_share as share

    return share(disk_gb, VMS_SHARE_DEFAULT if vms_pct is None else vms_pct)


def materialize(tenant, user):
    """Approved order -> first quota record and connectivity version (append-only tables)."""
    from .models import Connectivity, QuotaRecord, fit_split

    o = tenant.order or {}
    site, cluster = o.get("site"), o.get("cluster")
    if not site or not cluster or QuotaRecord.objects.filter(tenant=tenant).exists():
        return
    with transaction.atomic():
        QuotaRecord.objects.create(
            tenant=tenant,
            site=site,
            cluster=cluster,
            version=QuotaRecord.next_version(tenant, site, cluster),
            cpu=o.get("cpu") or 0,
            ram_gb=o.get("ram_gb") or 0,
            gpu=o.get("gpu") or 0,
            disk_gb=o.get("disk_gb") or 0,
            images_gb=fit_split(o.get("disk_gb") or 0, o.get("images_gb"), o.get("files_gb"))[0],
            files_gb=fit_split(o.get("disk_gb") or 0, o.get("images_gb"), o.get("files_gb"))[1],
            backups_gb=o.get("backups_gb", DEFAULT_RESOURCES["backups_gb"]),
            created_by=user,
        )
        conns = connections(o)
        inet, corp = "inet" in conns, "corpnet" in conns
        Connectivity.objects.create(
            tenant=tenant,
            site=site,
            version=Connectivity.next_version(tenant, site),
            connection_type=Connectivity.type_value(conns),
            igw_type=(o.get("igw_type") or Connectivity.Gateway.VYOS) if inet else "",
            inet_mbps=o.get("inet_mbps") if inet else None,
            inet_main_prefix=o.get("inet_main_prefix") if inet else None,
            inet_public_prefixes=" ".join(str(x) for x in o.get("inet_extra_prefixes") or []) if inet else "",
            corpnet_prefixes=" ".join(str(x) for x in o.get("corpnet_prefixes") or []) if corp else "",
            created_by=user,
        )


def allocate_one(tenant, purpose, length, user=None, note="", pool=None):
    """Assign one more prefix (inet_extra or corpnet) to an approved tenant from its platform's pools. owner 2026-11-03
    (u11): ``pool`` (a delegated subnet cidr) is tried first; without it the pools are used in order (cloud first)."""
    from inventory.models import DelegatedSubnet

    from .models import PrefixAssignment

    o = tenant.order or {}
    platform_key, site = o.get("platform"), o.get("site")
    if not platform_key or not site:
        raise AllocationError("the project has no placement")
    purpose_key = DelegatedSubnet.Purpose.INET if purpose == "inet" else DelegatedSubnet.Purpose.CORPNET
    nets = [n for n in (_net(d.cidr) for d in pools(platform_key, purpose_key)) if n]
    if pool:  # prefer the chosen delegated range, keeping the others as fallback
        nets = [n for n in nets if str(n) == pool] + [n for n in nets if str(n) != pool]
    if not nets:
        raise AllocationError(
            f"platform {platform_key} is connected to no {purpose} exchange network with delegated subnets"
        )
    taken = used_networks(site)
    maxp = _max_prefix_map(site)
    if purpose == "inet":
        taken += [n for _s, n, _name in service_blocks(site)]
    for pool in nets:
        if length < pool.prefixlen:
            continue
        cap = maxp.get(str(pool))
        if cap is not None and length < cap:
            continue  # §5a: never hand out a block bigger than this subnet's max_prefix
        got = _first_free(pool, length, taken)
        if got:
            kind = PrefixAssignment.Kind.INET_EXTRA if purpose == "inet" else PrefixAssignment.Kind.CORPNET
            return PrefixAssignment.objects.create(
                cidr=str(got), site=site, kind=kind, tenant=tenant, created_by=user, note=note or "network request"
            )
    raise AllocationError(f"no free /{length} left in the {purpose} pools of {platform_key}")
