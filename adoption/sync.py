"""Compare the old system's data with the portal database, and apply it.

In adoption mode the old system is the truth for: the tenant list, resources
(quota records) and the internet subnet. Organization links, IIN/BIN, sales
reps, contracts and portal users are owned by staff and never overwritten.
"""
from django.db import transaction
from django.utils import timezone

from accounts.models import Kind, User, audit
from tenants.models import (
    Approval, Connectivity, Origin, PrefixAssignment, QuotaRecord, Source, Tenant, TenantKind, TenantStage,
)

REC_FIELDS = ("cpu", "ram_gb", "gpu", "disk_gb", "images_gb", "files_gb", "backups_gb")
CONN_FIELDS = ("connection_type", "inet_mbps", "inet_main_prefix", "corpnet_prefixes", "l2_vlans")
HINT_FIELDS = ("legal_name", "status", "contacts", "manager_email")


def _kind_from(source_tenant):
    """Corporate-network tenants are internal (corp); everything else is cloud."""
    if any(s.get("connection_type") == "corpnet" for s in source_tenant["sites"].values()):
        return TenantKind.CORP
    return TenantKind.CLOUD


def _stage_from(source_tenant):
    status = (source_tenant["hints"].get("status") or "").lower()
    if status == "client":
        return TenantStage.CLIENT
    if status == "corp" or _kind_from(source_tenant) == TenantKind.CORP:
        return TenantStage.CLIENT  # corporate tenants: type corp; a stage of their own no longer exists
    return TenantStage.POC


def _db_state(tenant):
    recs = {(r.site, r.cluster): r.values() for r in tenant.current_quota_records()}
    conns = {c.site: c.values() for c in tenant.current_connectivity()}
    subnets = {p.site: p.cidr for p in PrefixAssignment.active().filter(tenant=tenant, kind=PrefixAssignment.Kind.INET_MAIN)}
    hints = {"legal_name": tenant.hint_legal_name, "status": tenant.hint_status, "contacts": "",
             "manager_email": tenant.hint_manager_email}
    return recs, conns, subnets, hints


def _by_legacy_name():
    """Tenants whose OpenNebula group is tenant-<name>, keyed by <name> (the old system's tenant name)."""
    return {t.group_name[len("tenant-"):]: t for t in Tenant.objects.filter(group_name__startswith="tenant-")}


def compute_diff(source):
    """Return a JSON-able diff between `source` (normalized tenants) and the database."""
    db = _by_legacy_name()
    out = {"new": [], "changed": [], "unchanged": [], "missing": [], "conflicts": []}
    for name, s in source.items():
        t = db.get(name)
        if t is None:
            out["new"].append({"tenant": name, "records": len(s["records"]), "kind": _kind_from(s)})
            continue
        if t.origin != Origin.ADOPTED:
            out["conflicts"].append({"tenant": name, "text": f"exists in the portal as a {t.get_origin_display().lower()} tenant"})
            continue
        recs, conns, subnets, hints = _db_state(t)
        changes = []
        src_recs = {(r["site"], r["cluster"]): {f: r[f] for f in REC_FIELDS} for r in s["records"]}
        for key in sorted(set(recs) | set(src_recs)):
            where = f"{key[0]}/{key[1]}"
            if key not in recs:
                changes.append({"where": where, "what": "record added", "new": src_recs[key]})
            elif key not in src_recs:
                changes.append({"where": where, "what": "record removed", "old": recs[key]})
            else:
                for f in REC_FIELDS:
                    if recs[key].get(f) != src_recs[key].get(f):
                        changes.append({"where": where, "what": f, "old": recs[key].get(f), "new": src_recs[key].get(f)})
        for site, sc in s["sites"].items():
            dc = conns.get(site, {})
            for f in CONN_FIELDS:
                if (dc.get(f) or None) != (sc.get(f) or None):
                    changes.append({"where": site, "what": f, "old": dc.get(f), "new": sc.get(f)})
            if (subnets.get(site) or None) != (sc.get("inet_main_subnet") or None):
                changes.append({"where": site, "what": "inet subnet", "old": subnets.get(site), "new": sc.get("inet_main_subnet")})
        for f in HINT_FIELDS:
            if f == "contacts":  # never kept (apply blanks it): comparing it only reports the same change daily
                continue
            if (hints.get(f) or "") != (s["hints"].get(f) or ""):
                changes.append({"where": "hint", "what": f, "old": hints.get(f), "new": s["hints"].get(f)})
        (out["changed"] if changes else out["unchanged"]).append({"tenant": name, "changes": changes})
    for name, t in db.items():
        if t.origin == Origin.ADOPTED and name not in source:
            out["missing"].append({"tenant": name})
    out["counts"] = {k: len(v) for k, v in out.items() if isinstance(v, list)}
    return out


def _sales_entry(email, actor):
    """Directory entry for a sales manager named by email in the old system."""
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        user = User.objects.create_user(email, None, kind=Kind.STAFF, is_sales=True, login_enabled=False)
        audit(actor, "sales_rep_added", user, email=email, source="import")
    return user if user.is_sales else None


@transaction.atomic
def apply(source, actor, imp=None):
    """Write the source into the database. Returns a summary dict."""
    now = timezone.now()
    summary = {"created": 0, "updated": 0, "records": 0, "connectivity": 0, "subnets": 0, "missing": 0, "sales_reps": 0}
    # summary["missing"]: imported tenants the old system no longer has, deleted
    reps_before = User.objects.filter(is_sales=True).count()
    db = _by_legacy_name()
    for name, s in source.items():
        t = db.get(name)
        if t is not None and t.origin != Origin.ADOPTED:
            continue  # conflict: never touch portal or test tenants
        if t is None:
            t = Tenant.objects.create(name=name, group_name=f"tenant-{name}", title=name, kind=_kind_from(s), stage=_stage_from(s), origin=Origin.ADOPTED,
                                      approval=Approval.APPROVED, approved_at=now)
            summary["created"] += 1
            audit(actor, "tenant_adopted", t, name=name)
        touched = False
        recs, conns, subnets, hints = _db_state(t)

        src_keys = set()
        for r in s["records"]:
            key = (r["site"], r["cluster"])
            src_keys.add(key)
            vals = {f: r[f] for f in REC_FIELDS}
            if recs.get(key) != vals:
                QuotaRecord.objects.create(tenant=t, site=key[0], cluster=key[1],
                                           version=QuotaRecord.next_version(t, *key), source=Source.IMPORT,
                                           created_by=actor if getattr(actor, "pk", None) else None,
                                           effective_from=now, **vals)
                summary["records"] += 1
                touched = True
        for key in set(recs) - src_keys:
            QuotaRecord.objects.create(tenant=t, site=key[0], cluster=key[1], removed=True,
                                       version=QuotaRecord.next_version(t, *key), source=Source.IMPORT,
                                       effective_from=now)
            summary["records"] += 1
            touched = True

        for site, sc in s["sites"].items():
            vals = {f: sc.get(f) for f in CONN_FIELDS}
            vals = {k: (v if v is not None else ("" if k not in ("inet_mbps", "inet_main_prefix") else None)) for k, v in vals.items()}
            current = conns.get(site)
            if current is None or any((current.get(f) or None) != (vals.get(f) or None) for f in CONN_FIELDS):
                keep = {f: current.get(f) for f in ("igw_type", "inet_public_prefixes", "l2_vnets")} if current else {}
                Connectivity.objects.create(tenant=t, site=site, version=Connectivity.next_version(t, site),
                                            source=Source.IMPORT, effective_from=now,
                                            **{k: v for k, v in keep.items() if v is not None}, **vals)
                summary["connectivity"] += 1
                touched = True
            cidr = sc.get("inet_main_subnet")
            if (subnets.get(site) or None) != (cidr or None):
                PrefixAssignment.active().filter(tenant=t, site=site, kind=PrefixAssignment.Kind.INET_MAIN).update(released_at=now)
                if cidr:
                    PrefixAssignment.objects.create(cidr=cidr, site=site, kind=PrefixAssignment.Kind.INET_MAIN,
                                                    tenant=t, source=Source.IMPORT)
                summary["subnets"] += 1
                touched = True

        h = s["hints"]
        new_hints = {"hint_legal_name": h.get("legal_name", ""), "hint_status": h.get("status", ""),
                     "hint_contacts": "", "hint_manager_email": h.get("manager_email", "")}
        if any(getattr(t, k) != v for k, v in new_hints.items()) or not t.source_present:
            for k, v in new_hints.items():
                setattr(t, k, v)
            t.source_present = True
            t.save(update_fields=list(new_hints) + ["source_present"])
        email = h.get("manager_email")
        if email:
            rep = _sales_entry(email, actor)
            if rep and t.organization_id and not t.organization.sales_rep_id:
                t.organization.sales_rep = rep
                t.organization.save(update_fields=["sales_rep"])
                audit(actor, "sales_rep_assigned", t.organization, new=rep.email, source="import")
        if not (t.order or {}).get("platform"):  # where it lives, as an order (gateways, configurations)
            try:
                from tenants.takeover import adoption_order
                t.order = {**(t.order or {}), **adoption_order(t)}
                t.save(update_fields=["order"])
            except Exception:  # noqa: BLE001 - no records yet
                pass
        if touched:
            summary["updated"] += 1

    for name, t in db.items():  # the old system's tenant list is the truth: imported tenants it no longer has go
        if t.origin == Origin.ADOPTED and name not in source:
            prefixes = list(PrefixAssignment.objects.filter(tenant=t).values_list("cidr", flat=True))
            audit(actor, "tenant_deleted_missing_in_old_system", t, name=name, group=t.group_name,
                  organization=str(t.organization or ""), prefixes=", ".join(prefixes))
            PrefixAssignment.objects.filter(tenant=t).delete()
            t.delete()
            summary["missing"] += 1
    summary["sales_reps"] = User.objects.filter(is_sales=True).count() - reps_before
    if imp is not None:
        imp.state = imp.State.APPLIED
        imp.applied_at = now
        imp.save(update_fields=["state", "applied_at"])
    audit(actor, "source_applied", imp, **summary)
    return summary
