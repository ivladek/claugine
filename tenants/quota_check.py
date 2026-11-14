"""Quotas set in the portal against the quotas the platform has (correction 08).

The platform's numbers come from the last collection (adoption.collector: the group's VM and datastore quotas). The
portal's are what the deployment sets from the tenant's current quota records (deploy.quota_for), so the check and the
deployment cannot drift apart. The rules (owner 2026-11-11):

    storage     a pool's storage = images + files + VM disks; backups is separate
    VM disks    the rest of a pool's storage, on that pool's cluster (SYSTEM_DISK_SIZE) - compared pool by pool,
                with its vCPU (RUNNING_CPU), RAM (RUNNING_MEMORY) and GPU (RUNNING_PCI_DEV)
    images      set per pool, on the image datastore of the pool's cluster; pools whose clusters share an image
                datastore share its quota (the sum); the platform's other image datastores are closed (0)
    files       set per pool; one file datastore per site, shared by all the pools: the sum
    backups     one figure per site (every pool of the site carries it): the backup datastore's quota

When the platform's clusters and datastores are not known (an old collection), the totals are compared instead.
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
    for key, field, div in (
        ("cpu", "RUNNING_CPU", 1),
        ("ram_gb", "RUNNING_MEMORY", 1024),
        ("gpu", "RUNNING_PCI_DEV", 1),
        ("vm_disk_gb", "SYSTEM_DISK_SIZE", 1024),
    ):
        vals = [_num(e.get(field)) for e in vm]
        vals = [v for v in vals if v is not None and v >= 0]
        out[key] = sum(vals) // div if vals else None
    backup_ids = {str(d["id"]) for d in (stats.datastores if stats else []) if d.get("type") == "backups"}
    sizes = [
        _num(e.get("SIZE"))
        for e in _list((obs.quotas.get("DATASTORE_QUOTA") or {}).get("DATASTORE"))
        if str(e.get("ID")) in backup_ids
    ]
    sizes = [v for v in sizes if v is not None and v >= 0]
    out["backups_gb"] = sum(sizes) // 1024 if sizes else (None if backup_ids else "?")
    return out


def portal_values(tenant, site=None):
    recs = [r for r in tenant.current_quota_records() if site is None or r.site == site]
    backups = {}
    for r in recs:  # one figure per site, carried by every pool of it
        backups[r.site] = max(backups.get(r.site, 0), r.backups_gb or 0)
    return {
        "cpu": sum(r.cpu or 0 for r in recs),
        "ram_gb": sum(r.ram_gb or 0 for r in recs),
        "gpu": sum(r.gpu or 0 for r in recs),
        "vm_disk_gb": sum(split_of(r)[0] for r in recs),
        "backups_gb": sum(backups.values()),
        "disk_gb": sum(r.disk_gb or 0 for r in recs),
    }


LABELS = {
    "cpu": "vCPU",
    "ram_gb": "RAM, GB",
    "gpu": "GPU",
    "vm_disk_gb": "VM disks, GB",
    "images_gb": "images, GB",
    "files_gb": "files, GB",
    "backups_gb": "backups, GB",
}
DS_TYPES = {"images": "0", "files": "2", "backups": "3"}


def expected(tenant, platform, stats):
    """What the deployment would set (deploy.quota_for) on the platform as the collection saw it; None when it cannot
    be worked out (the clusters or the datastores' clusters not collected, or no cluster for a pool)."""
    from .deploy import DeployError, quota_for

    if platform is None or stats is None or not stats.clusters or not stats.datastores:
        return None
    if any("clusters" not in d for d in stats.datastores):
        return None
    clusters = {name: int(cid) for cid, name in stats.clusters.items()}
    datastores = [
        (int(d["id"]), DS_TYPES.get(d.get("type"), ""), {clusters[n] for n in d.get("clusters") or [] if n in clusters})
        for d in stats.datastores
    ]
    try:
        return quota_for(tenant, platform, clusters, datastores)
    except DeployError:
        return None


def _gb(x, div):
    n = _num(x)
    return "∞" if n is None or n < 0 else n // div


def _rules(tenant, platform, obs, stats, want):
    """[(what, portal, platform)] against the deployment's template, pool by pool and datastore by datastore."""
    from inventory.models import Cluster

    names = stats.clusters or {}
    codes = {}
    for c in Cluster.objects.filter(platform=platform).exclude(quota_code="").exclude(state="retired"):
        codes.setdefault(c.name, c.quota_code)
    have_vm = {str(e.get("CLUSTER_IDS")): e for e in _list((obs.quotas.get("VM_QUOTA") or {}).get("VM"))}
    have_ds = {str(e.get("ID")): e for e in _list((obs.quotas.get("DATASTORE_QUOTA") or {}).get("DATASTORE"))}
    pools = [e for e in want["VM"] if e["RUNNING_CPU"] != -1]
    rows = []
    for e in pools:
        h = have_vm.get(str(e["CLUSTER_IDS"])) or {}
        name = names.get(str(e["CLUSTER_IDS"]), str(e["CLUSTER_IDS"]))
        pre = f"{codes.get(name, name)}: " if len(pools) > 1 else ""
        for field, label, div in (
            ("RUNNING_CPU", "vCPU", 1),
            ("RUNNING_MEMORY", "RAM, GB", 1024),
            ("RUNNING_PCI_DEV", "GPU", 1),
            ("SYSTEM_DISK_SIZE", "VM disks, GB", 1024),
        ):
            mine, theirs = e[field] // div, _gb(h.get(field), div)
            if mine != theirs:
                rows.append((pre + label, mine, theirs))
    kinds = {str(d["id"]): d.get("type") for d in stats.datastores}
    for kind, label in (("images", "images, GB"), ("files", "files, GB"), ("backups", "backups, GB")):
        entries = [e for e in want["DATASTORE"] if kinds.get(str(e["ID"])) == kind]
        if not entries:
            continue
        mine, theirs = 0, 0
        for e in entries:
            mine += max(e["SIZE"], 0) // 1024
            got = _gb((have_ds.get(str(e["ID"])) or {}).get("SIZE"), 1024)
            theirs = "∞" if theirs == "∞" or got == "∞" else theirs + got
        if mine != theirs:
            rows.append((label, mine, theirs))
    return rows


def compare(tenant, obs=None, stats=None):
    """(status, [(what, portal, platform)]) — status ok | diff | "" (not read)."""
    from inventory.models import Platform

    pkey = (tenant.order or {}).get("platform") or ""
    if obs is None:
        obs = Observation.objects.filter(platform_key=pkey, group_name=tenant.one_group_name).first() if pkey else None
    if obs is None:
        return "", []
    if stats is None:
        stats = PlatformStats.objects.filter(platform_key=pkey).first()
    platform = Platform.objects.select_related("site").filter(key=pkey).first()
    want = expected(tenant, platform, stats)
    if want is not None:
        rows = _rules(tenant, platform, obs, stats, want)
        return ("diff" if rows else "ok"), rows
    # an old collection: the totals
    mine, theirs = portal_values(tenant, platform.site.code if platform else None), platform_values(obs, stats)
    rows = []
    for k, label in LABELS.items():
        if k not in theirs:
            continue
        p = theirs.get(k)
        if p == "?":
            continue  # the backup datastore is not known: not compared
        if p is None:  # unlimited on the platform
            rows.append((label, mine[k], "∞"))
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
        obs = (
            Observation.objects.filter(group_name=group, platform_key__in=keys)
            .exclude(quotas={})
            .order_by("-collected_at")
            .first()
        )
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
        for c in (
            Cluster.objects.filter(platform__key=obs.platform_key)
            .exclude(quota_code="")
            .exclude(state="retired")
            .order_by("pk")
        ):
            if c.name in by_name:
                codes.setdefault(c.quota_code, by_name[c.name])
        for r in site_recs:
            e = per_cluster.get(str(codes.get(r.cluster))) if codes.get(r.cluster) is not None else None
            if e is None and len(site_recs) == 1:
                e = total  # one record on the site: the group's totals are its usage
            if e is None:
                continue
            out[r.pk] = {
                "cpu": _num(e.get("RUNNING_CPU_USED")) or 0,
                "ram_gb": (_num(e.get("RUNNING_MEMORY_USED")) or 0) // 1024,
                "gpu": _num(e.get("RUNNING_PCI_DEV_USED")) or 0,
                "vm_disk_gb": (_num(e.get("SYSTEM_DISK_SIZE_USED")) or 0) // 1024,
            }
        types = {str(d["id"]): d.get("type") for d in (stats.datastores if stats else [])}
        ds = {"images_gb": 0, "files_gb": 0, "backups_gb": 0}
        for e in _list((obs.quotas.get("DATASTORE_QUOTA") or {}).get("DATASTORE")):
            t = types.get(str(e.get("ID")))
            if t in ("images", "files", "backups"):
                ds[f"{t}_gb"] += (_num(e.get("SIZE_USED")) or 0) // 1024
        out[f"site:{site}"] = ds
        out["at"] = obs.collected_at
    return out


# --- the floor of a quota change (owner 2026-11-12: "add check when quota changes to disable values below real
# usage") ---------------------------------------------------------------------------------------------------------

LABELS = {
    "cpu": "vCPU",
    "ram_gb": "RAM, GB",
    "gpu": "GPU",
    "vm_disk_gb": "VM disks, GB",
    "images_gb": "images, GB",
    "files_gb": "files, GB",
    "backups_gb": "backups, GB",
}


def below_usage(tenant, site, cluster, values, removed=False):
    """What a quota change would set below what the project uses now on the platform (the last collection).

    `values`: the pool's new cpu, ram_gb, gpu, disk_gb (and images_gb, files_gb, backups_gb when known); `removed`:
    the pool goes. Images and files are shared by the site's pools, backups is one figure per site, so those are
    checked against the site's sum. Returns [(label, new, used)], empty when the change fits or nothing was read."""
    from .models import fit_split

    recs = tenant.current_quota_records()
    rec = next((r for r in recs if r.site == site and r.cluster == cluster), None)
    try:
        u = usage(tenant, recs)
    except Exception:  # noqa: BLE001 - no reading, no floor: the platform itself still refuses what does not fit
        return []
    out = []
    pool = u.get(rec.pk) if rec is not None else None
    if removed:
        values = dict.fromkeys(("cpu", "ram_gb", "gpu", "disk_gb", "images_gb", "files_gb"), 0)
    images, files = fit_split(
        values.get("disk_gb") or 0,
        values.get("images_gb", getattr(rec, "images_gb", None)),
        values.get("files_gb", getattr(rec, "files_gb", None)),
    )
    if pool:
        for f in ("cpu", "ram_gb", "gpu"):
            if (values.get(f) or 0) < (pool.get(f) or 0):
                out.append((LABELS[f], values.get(f) or 0, pool[f]))
        vm = max((values.get("disk_gb") or 0) - images - files, 0)
        if vm < (pool.get("vm_disk_gb") or 0):
            out.append((LABELS["vm_disk_gb"], vm, pool["vm_disk_gb"]))
    site_use = u.get(f"site:{site}")
    if site_use:
        others = [r for r in recs if r.site == site and r is not rec]
        for f, mine in (("images_gb", images), ("files_gb", files)):
            total = mine + sum(
                fit_split(r.disk_gb, r.images_gb, r.files_gb)[0 if f == "images_gb" else 1] for r in others
            )
            if total < (site_use.get(f) or 0):
                out.append((LABELS[f] + " (" + site + ")", total, site_use[f]))
        if "backups_gb" in values and not (removed and others):
            if (values.get("backups_gb") or 0) < (site_use.get("backups_gb") or 0):
                out.append(
                    (LABELS["backups_gb"] + " (" + site + ")", values.get("backups_gb") or 0, site_use["backups_gb"])
                )
    return out


def share_floor(tenant, rec):
    """(images, files): the least this pool must carry so that the site's sums cover what the project stores now,
    the site's other pools kept as they are (owner 2026-11-13: a pool whose images were set below usage could not be
    changed at all - the split kept the old images and every change was refused). (0, 0) when nothing was read."""
    from .models import fit_split

    recs = tenant.current_quota_records()
    try:
        u = usage(tenant, recs)
    except Exception:  # noqa: BLE001
        return 0, 0
    site_use = u.get(f"site:{rec.site}") or {}
    others = [r for r in recs if r.site == rec.site and r.pk != rec.pk]
    oi = sum(fit_split(r.disk_gb, r.images_gb, r.files_gb)[0] for r in others)
    of = sum(fit_split(r.disk_gb, r.images_gb, r.files_gb)[1] for r in others)
    return max((site_use.get("images_gb") or 0) - oi, 0), max((site_use.get("files_gb") or 0) - of, 0)


def floor(tenant, rec, gpu=None):
    """The smallest values the change form takes for this pool: {cpu, ram_gb, gpu, disk_gb} (0 when not read)."""
    import math

    from .models import fit_split

    try:
        u = usage(tenant, tenant.current_quota_records())
    except Exception:  # noqa: BLE001
        return {}
    pool = u.get(rec.pk) or {}
    site_use = u.get(f"site:{rec.site}") or {}
    others = [r for r in tenant.current_quota_records() if r.site == rec.site and r.pk != rec.pk]
    oi = sum(fit_split(r.disk_gb, r.images_gb, r.files_gb)[0] for r in others)
    of = sum(fit_split(r.disk_gb, r.images_gb, r.files_gb)[1] for r in others)
    disk = (pool.get("vm_disk_gb") or 0) + max((site_use.get("images_gb") or 0) - oi, 0)
    disk += max((site_use.get("files_gb") or 0) - of, 0)
    out = {"cpu": pool.get("cpu") or 0, "ram_gb": pool.get("ram_gb") or 0, "disk_gb": disk, "gpu": pool.get("gpu") or 0}
    if gpu:  # a GPU pool is asked in bundles: enough bundles for the GPUs, cores and memory in use
        needed = max(
            math.ceil(out["cpu"] / gpu["cores"]) if gpu.get("cores") else 0,
            math.ceil(out["ram_gb"] / gpu["ram_gb"]) if gpu.get("ram_gb") else 0,
        )
        # the pool's own bundle count keeps its vCPU and RAM as they are (ResourceChangeForm.values), so it is never
        # refused for them; fewer bundles must still carry the cores and memory in use (target-ai, 2026-11-14)
        out["gpu"] = max(out["gpu"], min(needed, rec.gpu) if rec.gpu else needed)
    return out


VM_LABELS = {LABELS[k] for k in ("cpu", "ram_gb", "gpu", "vm_disk_gb")}


def without_vms(found):
    """What is left of a floor check when the pool's VMs go with it: the site's images, files and backups."""
    return [x for x in found if x[0] not in VM_LABELS]


def floor_message(found):
    """One line for a refused change."""
    from django.utils.translation import gettext as _

    return _("Below what the project uses now: %(list)s.") % {
        "list": "; ".join(
            _("%(what)s %(new)s, in use %(used)s") % {"what": w, "new": n, "used": x} for w, n, x in found
        )
    }
