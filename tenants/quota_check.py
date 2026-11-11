"""Quotas set in the portal against the quotas the platform has (correction 08).

The platform's numbers come from the last collection (adoption.collector: the group's VM and datastore quotas). The
portal's are the tenant's current quota records, as the deployment sets them (tenants.deploy.Deployer._quota_template):
running vCPU, running RAM, GPU (PCI devices), VM disks (SYSTEM_DISK_SIZE) and backups (the backup datastore's size).
Status: ok — the same; diff — not the same (the differences listed); "" — not read (the platform is not collected,
or the group was not seen).
"""
from adoption.models import Observation, PlatformStats

from .models import split_of


def _num(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


def _list(x):
    return x if isinstance(x, list) else ([x] if isinstance(x, dict) else [])


def platform_values(obs, stats=None):
    """{cpu, ram_gb, gpu, vm_disk_gb, backups_gb} the group's quotas allow (None: unlimited), summed over clusters."""
    vm = [e for e in _list((obs.quotas.get("VM_QUOTA") or {}).get("VM")) if e.get("CLUSTER_IDS") not in (None, "")]
    out = {}
    for key, field, div in (("cpu", "RUNNING_CPU", 1), ("ram_gb", "RUNNING_MEMORY", 1024), ("gpu", "RUNNING_PCI_DEV", 1),
                            ("vm_disk_gb", "SYSTEM_DISK_SIZE", 1024)):
        vals = [_num(e.get(field)) for e in vm]
        vals = [v for v in vals if v is not None and v >= 0]
        out[key] = sum(vals) // div if vals else None
    backup_ids = {str(d["id"]) for d in (stats.datastores if stats else []) if d.get("type") == "backups"}
    sizes = [_num(e.get("SIZE")) for e in _list((obs.quotas.get("DATASTORE_QUOTA") or {}).get("DATASTORE"))
             if str(e.get("ID")) in backup_ids]
    sizes = [v for v in sizes if v is not None and v >= 0]
    out["backups_gb"] = sum(sizes) // 1024 if sizes else (None if backup_ids else "?")
    return out


def portal_values(tenant):
    recs = tenant.current_quota_records()
    return {"cpu": sum(r.cpu or 0 for r in recs), "ram_gb": sum(r.ram_gb or 0 for r in recs),
            "gpu": sum(r.gpu or 0 for r in recs), "vm_disk_gb": sum(split_of(r)[0] for r in recs),
            "backups_gb": sum(r.backups_gb or 0 for r in recs), "disk_gb": sum(r.disk_gb or 0 for r in recs)}


DISK_SLACK = 2  # GB
LABELS = {"cpu": "vCPU", "ram_gb": "RAM, GB", "gpu": "GPU", "vm_disk_gb": "VM disks, GB", "backups_gb": "backups, GB"}


def compare(tenant, obs=None, stats=None):
    """(status, [(what, portal, platform)]) — status ok | diff | "" (not read)."""
    pkey = (tenant.order or {}).get("platform") or ""
    if obs is None:
        obs = Observation.objects.filter(platform_key=pkey, group_name=tenant.one_group_name).first() if pkey else None
    if obs is None:
        return "", []
    if stats is None:
        stats = PlatformStats.objects.filter(platform_key=pkey).first()
    mine, theirs = portal_values(tenant), platform_values(obs, stats)
    rows = []
    for k, label in LABELS.items():
        p = theirs.get(k)
        if p == "?":
            continue  # the backup datastore is not known: not compared
        if p is None:  # unlimited on the platform
            rows.append((label, mine[k], "∞"))
        elif k == "vm_disk_gb":
            # the old system put all the storage there, rounded in MB: the same within DISK_SLACK GB of either number
            if min(abs(p - mine[k]), abs(p - mine["disk_gb"])) > DISK_SLACK:
                rows.append((label, mine[k], p))
        elif p != mine[k]:
            rows.append((label, mine[k], p))
    return ("diff" if rows else "ok"), rows


def compare_all(tenants):
    """{tenant pk: (status, rows)} with one query for the observations."""
    obs = {(o.platform_key, o.group_name): o for o in Observation.objects.all()}
    stats = {s.platform_key: s for s in PlatformStats.objects.all()}
    out = {}
    for t in tenants:
        pkey = (t.order or {}).get("platform") or ""
        o = obs.get((pkey, t.one_group_name))
        out[t.pk] = compare(t, o, stats.get(pkey)) if o is not None else ("", [])
    return out


def usage(tenant, records):
    """What the tenant uses of its quota on the platform (last collection), per quota record (correction 11).

    {record pk: {cpu, ram_gb, gpu, vm_disk_gb}} from the group's VM quota of the record's cluster, plus "site:<code>":
    {images_gb, files_gb, backups_gb} from its datastore quotas, and "at": the time of the collection. Empty when the
    platform was not read or the group not seen. The running numbers are the ones the quota limits (RUNNING_*)."""
    from inventory.models import Cluster, Platform
    out = {}
    group = tenant.one_group_name
    if not group or not records:
        return out
    sites = sorted({r.site for r in records})
    for site in sites:
        keys = list(Platform.objects.filter(site__code=site).values_list("key", flat=True))
        obs = Observation.objects.filter(group_name=group, platform_key__in=keys).exclude(quotas={}).order_by("-collected_at").first()
        if obs is None:
            continue
        stats = PlatformStats.objects.filter(platform_key=obs.platform_key).first()
        names = (stats.clusters or {}) if stats else {}
        by_name = {n: i for i, n in names.items()}
        vm = _list((obs.quotas.get("VM_QUOTA") or {}).get("VM"))
        per_cluster = {str(e.get("CLUSTER_IDS")): e for e in vm if e.get("CLUSTER_IDS") not in (None, "")}
        total = next((e for e in vm if e.get("CLUSTER_IDS") in (None, "")), None)
        site_recs = [r for r in records if r.site == site]
        codes = {}
        for c in Cluster.objects.filter(platform__key=obs.platform_key).exclude(quota_code="").exclude(state="retired").order_by("pk"):
            if c.name in by_name:
                codes.setdefault(c.quota_code, by_name[c.name])
        for r in site_recs:
            e = per_cluster.get(str(codes.get(r.cluster))) if codes.get(r.cluster) is not None else None
            if e is None and len(site_recs) == 1:
                e = total  # one record on the site: the group's totals are its usage
            if e is None:
                continue
            out[r.pk] = {"cpu": _num(e.get("RUNNING_CPU_USED")) or 0, "ram_gb": (_num(e.get("RUNNING_MEMORY_USED")) or 0) // 1024,
                         "gpu": _num(e.get("RUNNING_PCI_DEV_USED")) or 0,
                         "vm_disk_gb": (_num(e.get("SYSTEM_DISK_SIZE_USED")) or 0) // 1024}
        types = {str(d["id"]): d.get("type") for d in (stats.datastores if stats else [])}
        ds = {"images_gb": 0, "files_gb": 0, "backups_gb": 0}
        for e in _list((obs.quotas.get("DATASTORE_QUOTA") or {}).get("DATASTORE")):
            t = types.get(str(e.get("ID")))
            if t in ("images", "files", "backups"):
                ds[f"{t}_gb"] += (_num(e.get("SIZE_USED")) or 0) // 1024
        out[f"site:{site}"] = ds
        out["at"] = obs.collected_at
    return out
