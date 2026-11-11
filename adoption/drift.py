"""Differences between the portal database and what OpenNebula holds.

Compared: the tenant metadata in the group template, in the format of
quota.md (TENANT, QUOTA_<SITE>, QUOTA_<SITE>_<QR>). The database is the
reference; every difference is reported, nothing is changed.
"""
import ipaddress
from collections import defaultdict

from tenants.models import PrefixAssignment, Tenant

from .models import Observation

DEFAULTS = {"files_gb": 1, "images_gb": 10, "backups_gb": 0}


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _net(v):
    try:
        return str(ipaddress.ip_network(str(v).strip(), strict=False).network_address)
    except ValueError:
        return str(v).strip()


def _norm_list(v):
    return " ".join(str(v or "").split())


def latest_observations():
    """{group name: observation}; the most recent if several platforms report the group."""
    obs = {}
    for o in Observation.objects.order_by("collected_at"):
        obs[o.group_name] = o
    return obs


def drift_for(tenant, observations=None):
    """List of {"where", "field", "portal", "opennebula"}; None if no data was collected."""
    if not tenant.group_name:  # pending requests have no group yet
        return []
    observations = observations if observations is not None else latest_observations()
    if not observations:
        return None
    o = observations.get(tenant.one_group_name)
    if o is None:
        return [{"where": "group", "field": "exists", "portal": tenant.one_group_name, "opennebula": "missing"}]
    tpl = o.template or {}
    issues = []

    def add(where, field, portal, one):
        issues.append({"where": where, "field": field, "portal": portal, "opennebula": one})

    t_meta = tpl.get("TENANT") if isinstance(tpl.get("TENANT"), dict) else {}
    if t_meta.get("STATUS") and t_meta.get("STATUS") != tenant.status:
        add("TENANT", "STATUS", tenant.status, t_meta.get("STATUS"))

    records = tenant.current_quota_records()
    per_site = defaultdict(list)
    for r in records:
        per_site[r.site].append(r)
        key = f"QUOTA_{r.site.upper()}_{r.cluster.upper()}"
        q = tpl.get(key) if isinstance(tpl.get(key), dict) else None
        if q is None:
            add(key, "record", "present", "missing")
            continue
        for field, attr in (("CPU", "cpu"), ("RAM_GB", "ram_gb"), ("GPU", "gpu"), ("DISK_GB", "disk_gb")):
            one_v = _int(q.get(field)) if q.get(field) not in (None, "") else 0
            ok = {getattr(r, attr) or 0}
            if field == "DISK_GB":  # the old system's DISK_GB leaves the files out; the portal's includes them
                ok.add((r.disk_gb or 0) - (r.files_gb if r.files_gb is not None else 0))
            if one_v not in ok:
                add(key, field, getattr(r, attr) or 0, one_v)
    portal_keys = {f"QUOTA_{r.site.upper()}_{r.cluster.upper()}" for r in records}
    for key in tpl:
        if key.startswith("QUOTA_") and key.count("_") >= 2 and key not in portal_keys:
            add(key, "record", "missing", "present")

    conns = {c.site: c for c in tenant.current_connectivity()}
    prefixes = defaultdict(list)
    for p in PrefixAssignment.active().filter(tenant=tenant):
        prefixes[(p.site, p.kind)].append(p.cidr)
    for site in sorted(set(per_site) | set(conns)):
        key = f"QUOTA_{site.upper()}"
        q = tpl.get(key) if isinstance(tpl.get(key), dict) else None
        if q is None:
            add(key, "site", "present", "missing")
            continue
        recs = per_site.get(site, [])
        for field, attr in (("IMAGES_GB", "images_gb"), ("FILES_GB", "files_gb"), ("BACKUPS_GB", "backups_gb")):
            portal_v = sum((getattr(r, attr) if getattr(r, attr) is not None else DEFAULTS[attr]) for r in recs)
            one_v = _int(q.get(field)) if q.get(field) not in (None, "") else DEFAULTS[attr] * len(recs)
            if portal_v != one_v:
                add(key, field, portal_v, one_v)
        c = conns.get(site)
        if c is None:
            continue
        checks = [
            ("CONNECTION_TYPE", " ".join(sorted(c.types)), " ".join(sorted(str(q.get("CONNECTION_TYPE") or "").split()))),
            ("INET_MBPS", c.inet_mbps, _int(q.get("INET_MBPS"))),
            ("INET_MAIN_PREFIX", c.inet_main_prefix, _int(q.get("INET_MAIN_PREFIX"))),
            ("L2_VLANS", _norm_list(c.l2_vlans), _norm_list(q.get("L2_VLANS"))),
            ("CORPNET_PREFIXES", _norm_list(c.corpnet_prefixes), _norm_list(q.get("CORPNET_PREFIXES"))),
        ]
        if c.igw_type:
            checks.append(("IGW_TYPE", c.igw_type, str(q.get("IGW_TYPE") or "")))
        for field, portal_v, one_v in checks:
            if (portal_v or None) != (one_v or None):
                add(key, field, portal_v, one_v)
        main = prefixes.get((site, PrefixAssignment.Kind.INET_MAIN), [])
        one_main = q.get("INET_MAIN_SUBNET")
        if (main or one_main) and (_net(main[0]) if main else None) != (_net(one_main) if one_main else None):
            add(key, "INET_MAIN_SUBNET", main[0] if main else None, one_main)
        corp = sorted(_net(x) for x in prefixes.get((site, PrefixAssignment.Kind.CORPNET), []))
        one_corp = sorted(_net(x) for x in str(q.get("CORPNET_SUBNETS") or "").split())
        if corp != one_corp:
            add(key, "CORPNET_SUBNETS", " ".join(corp) or None, " ".join(one_corp) or None)
    return issues


def drift_all():
    """{tenant id: issue count}, or None if nothing was collected yet."""
    observations = latest_observations()
    if not observations:
        return None
    out = {}
    for t in Tenant.objects.filter(group_name__isnull=False).exclude(status="archived"):
        issues = drift_for(t, observations)
        out[t.pk] = len(issues or [])
    return out


def unadopted_groups():
    """Tenant groups in OpenNebula with no tenant in the portal."""
    from tenants.naming import GROUP_RE
    known = {t.one_group_name for t in Tenant.objects.select_related("organization")} - {""}
    return [o for o in latest_observations().values()
            if (o.group_name.startswith("tenant-") or GROUP_RE.match(o.group_name)) and o.group_name not in known]
