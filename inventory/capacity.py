"""Capacity of clusters and datastores: what the hardware gives vs what tenants hold.

Clusters
    physical  every core (hyperthreads are not counted), all RAM and GPUs of the active hosts; the spare hosts
              (N+X: the X largest hosts) and the reserved percentages are shown as marked parts of it. Tenants'
              vCPU are shown as the physical cores they take: vCPU / (vCores per pCore).
    logical   what can be given to tenants: hosts minus the spare ones, minus the reserved percentage, CPU times the
              vCores per pCore ratio. Tenants' quotas are counted against it.
    GPU       clusters with GPUs are ordered in slots: one slot = one GPU with its host's CPU and RAM divided by the
              GPUs of the host. Slots are GPUs; spare hosts take their GPUs out of the logical view.

Datastores
    logical   the volume's size minus the reserved percentage, against the tenants' quota parts (VM disks, images,
              files, backups) of the clusters that use the datastore (Infrastructure: the datastore's clusters; a
              difference from OpenNebula's cluster membership is shown, since it puts quotas on the wrong datastore).
    physical  what OpenNebula reports (total / used) from the last collection.
"""
from collections import defaultdict
from decimal import Decimal

from tenants.models import Lifecycle, QuotaRecord

from .models import Cluster, PlatformDatastore


def current_records():
    """{(site, quota code): [current QuotaRecord of each live tenant]}."""
    from django.utils import timezone
    now = timezone.now()
    out, seen = defaultdict(list), set()
    for r in (QuotaRecord.objects.exclude(tenant__status=Lifecycle.ARCHIVED).filter(effective_from__lte=now)
              .order_by("tenant_id", "site", "cluster", "-version")):
        key = (r.tenant_id, r.site, r.cluster)
        if key in seen:
            continue
        seen.add(key)
        if not r.removed:
            out[(r.site, r.cluster)].append(r)
    return out


def current_allocations():
    """{(site, quota code): {cpu, ram_gb, gpu, disk_gb, tenants}} over current records of live tenants."""
    totals = {}
    for key, recs in current_records().items():
        totals[key] = {"cpu": sum(r.cpu or 0 for r in recs), "ram_gb": sum(r.ram_gb or 0 for r in recs),
                       "gpu": sum(r.gpu or 0 for r in recs), "disk_gb": sum(r.disk_gb or 0 for r in recs),
                       "tenants": len(recs)}
    return totals


def _pct(used, total):
    """Percent used; more than everything (100+) when there is nothing to use but something is used."""
    if not total:
        return 999 if used else None
    return round(100 * used / total)


def cluster_capacity(c, alloc):
    """The numbers of one cluster (see the module doc)."""
    hosts = [h for h in c.hosts.all() if h.state == "active" and h.profile]
    per = [{"name": h.name, "cores": h.profile.cores, "ram": h.profile.ram_gb, "gpus": h.profile.gpu_count,
            "mhz": h.profile.cpu_mhz} for h in hosts]
    cores = sum(x["cores"] for x in per)
    ram = sum(x["ram"] for x in per)
    gpus = sum(x["gpus"] for x in per)
    ghz = sum(x["cores"] * x["mhz"] for x in per) / 1000
    mhz_avg = (sum(x["cores"] * x["mhz"] for x in per) / cores) if cores else 0
    spare = sorted(per, key=lambda x: (x["cores"], x["ram"]), reverse=True)[:min(c.spare_hosts or 0, len(per))]
    s_cores, s_ram, s_gpus = sum(x["cores"] for x in spare), sum(x["ram"] for x in spare), sum(x["gpus"] for x in spare)
    # owner 2026-11-09 (gpu-bundles): a GPU cluster has no vcpu_ratio and no reserve percentages - the head-room
    # is already inside the bundle, so applying a ratio or a reserve on top would count it twice and advertise
    # capacity nobody can order. Its CPU and RAM therefore read at ratio 1 and reserve 0; everything the page
    # shows for a CPU cluster it still shows for a GPU one, plus the bundles below.
    is_gpu = sum(x["gpus"] for x in per) > 0
    r_cores = 0 if is_gpu else (cores - s_cores) * (c.reserved_cpu_pct or 0) / 100
    r_ram = 0 if is_gpu else (ram - s_ram) * (c.reserved_ram_pct or 0) / 100
    ratio = Decimal(1) if is_gpu else Decimal(c.vcpu_ratio or 1)
    a = alloc.get((c.platform.site.code, c.order_code)) or {"cpu": 0, "ram_gb": 0, "gpu": 0, "disk_gb": 0, "tenants": 0}
    avail_cores = max(0.0, cores - s_cores - r_cores)
    avail_ram = max(0.0, ram - s_ram - r_ram)
    vcpu_cap = int(Decimal(str(avail_cores)) * ratio)
    used_pcores = float(Decimal(a["cpu"]) / ratio) if ratio else 0
    gpu = gpus > 0
    out = {
        "cluster": c, "site": c.platform.site.code, "platform": c.platform.key, "kind": c.kind, "gpu": gpu,
        "hosts": len(per), "spare": [x["name"] for x in spare], "alloc": a, "ratio": ratio,
        "physical": {
            "cores": cores, "ram_gb": ram, "gpus": gpus, "ghz": round(ghz, 1),
            "spare_cores": s_cores, "spare_ram": s_ram, "spare_gpus": s_gpus,
            "reserved_cores": round(r_cores, 1), "reserved_ram": round(r_ram),
            "used_cores": round(used_pcores, 1), "used_ghz": round(used_pcores * mhz_avg / 1000, 1),
            "used_ram": a["ram_gb"], "used_gpus": a["gpu"],
        },
        "logical": {
            "vcpu": vcpu_cap, "ram_gb": round(avail_ram), "gpus": gpus - s_gpus, "ghz": round(avail_cores * mhz_avg / 1000, 1),
            "used_vcpu": a["cpu"], "used_ram": a["ram_gb"], "used_gpus": a["gpu"],
            "used_ghz": round(used_pcores * mhz_avg / 1000, 1),
        },
    }
    p, lg = out["physical"], out["logical"]
    # bar segments in % of the physical total: used, spare, reserved (the rest is free)
    p["cpu_bar"] = _segments(cores, p["used_cores"], s_cores, r_cores)
    p["ram_bar"] = _segments(ram, a["ram_gb"], s_ram, r_ram)
    p["gpu_bar"] = _segments(gpus, a["gpu"], s_gpus, 0)
    lg["cpu_pct"] = _pct(a["cpu"], vcpu_cap)
    lg["ram_pct"] = _pct(a["ram_gb"], avail_ram)
    lg["gpu_pct"] = _pct(a["gpu"], gpus - s_gpus)
    lg["over"] = {"cpu": a["cpu"] > vcpu_cap, "ram": a["ram_gb"] > avail_ram, "gpu": a["gpu"] > gpus - s_gpus}
    out["oversubscription"] = None if is_gpu else (round(a["cpu"] / avail_cores, 2) if avail_cores else None)
    if is_gpu:  # the only unit a GPU cluster is ordered in
        b = c.bundle()
        free_cards = max(gpus - s_gpus - a["gpu"], 0)
        out["bundle"] = {
            "cores": b["cores"], "ram_gb": b["ram_gb"],
            "total": gpus, "spare": s_gpus, "used": a["gpu"], "free": free_cards,
            "bar": _segments(gpus, a["gpu"], s_gpus, 0),
            "pct": _pct(a["gpu"], max(gpus - s_gpus, 0)),
        }
    return out


def _segments(total, used, spare, reserved):
    if not total:
        return {"used": 100 if used else 0, "spare": 0, "reserved": 0, "free": 0, "over": bool(used)}
    u = min(100.0, 100 * used / total)
    sp = 100 * spare / total
    rs = 100 * reserved / total
    return {"used": round(u, 1), "spare": round(sp, 1), "reserved": round(rs, 1),
            "free": round(max(0.0, 100 - u - sp - rs), 1), "over": used > total - spare - reserved}


def clusters(kinds=None, site=None):
    """Capacity of the clusters (all kinds unless `kinds`), retired ones left out."""
    alloc = current_allocations()
    qs = Cluster.objects.exclude(state="retired").select_related("platform", "platform__site").prefetch_related("hosts__profile")
    if kinds:
        qs = qs.filter(kind__in=kinds)
    if site:
        qs = qs.filter(platform__site__code=site)
    rows = [cluster_capacity(c, alloc) for c in qs]
    rows.sort(key=lambda r: (r["site"], r["platform"], r["cluster"].name))
    return rows


def datastores(kinds=None, shared_only=False):
    """Usage of the platform datastores: logical (size − reserve vs quotas) and physical (OpenNebula's numbers)."""
    from adoption.models import PlatformStats
    recs = current_records()
    stats = {s.platform_key: {d["name"]: d for d in (s.datastores or [])} for s in PlatformStats.objects.all()}
    qs = PlatformDatastore.objects.select_related("platform", "platform__site", "volume").prefetch_related("clusters")
    if shared_only:
        qs = qs.filter(platform__kind="shared")
    if kinds:
        qs = qs.filter(kind__in=kinds)
    out = []
    for d in qs:
        size = d.volume.size_gb if d.volume else 0
        cap = size * (100 - (d.reserved_pct or 0)) / 100
        used = 0.0
        for c in d.clusters.all():
            same = max(1, c.datastores.filter(kind=d.kind).count())
            for r in recs.get((c.platform.site.code, c.order_code), []):
                vms, images, files = r.split if hasattr(r, "split") else (r.disk_gb or 0, 0, 0)
                part = {"vms": vms, "images": images, "files": files, "backups": r.backups_gb or 0}.get(d.kind, 0)
                used += (part or 0) / same
        live = (stats.get(d.platform.key) or {}).get(d.name) or {}
        mine = sorted(c.name for c in d.clusters.all())
        drift = ({"portal": mine, "one": live["clusters"]} if "clusters" in live and mine != sorted(live["clusters"])
                 else None)
        out.append({
            "ds": d, "platform": d.platform.key, "site": d.platform.site.code, "kind": d.kind, "size_gb": size,
            "reserved_pct": d.reserved_pct, "reserved_gb": round(size - cap), "cap_gb": round(cap), "used_gb": round(used),
            "pct": _pct(used, cap), "over": used > cap, "drift": drift,
            "live": {"total_gb": round(live.get("total_mb", 0) / 1024), "used_gb": round(live.get("used_mb", 0) / 1024),
                     "pct": _pct(live.get("used_mb", 0), live.get("total_mb", 0))} if live else None,
        })
    out.sort(key=lambda r: (r["site"], r["platform"], r["kind"], r["ds"].name))
    return out
