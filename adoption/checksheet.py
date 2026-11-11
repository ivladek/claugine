"""Fill gaps in adopted tenants from a tenant check sheet (CSV kept by the sales team).

Columns: tenant, status, manager sales, KAM email, kam, BIN/IIN, client email, client phone, legal entity.
- BIN/IIN (leading zeros lost by spreadsheets are restored when the check digit then matches) groups tenants:
  every tenant with the same IIN/BIN ends up in one organization, which gets that IIN/BIN.
- status: client / poc -> stage; corp -> corporate tenant in a corporate organization.
- Cloud organizations: manager sales -> sales rep, KAM email -> key account manager (staff with the KAM role).
- Corporate organizations have only a resource manager and a support engineer: manager sales -> their
  resource manager; no sales rep, no KAM. Staff people are created as directory entries when needed.
- Only empty assignments are filled. Client contacts in the sheet are ignored.
- legal entity is used only if it survived the export (no "?" from a lost encoding) and the organization has no name.
Existing data is never overwritten except organization membership driven by IIN/BIN.
"""
import csv
import io
import re

from django.db import transaction

from accounts.kz import guess_tax_id_type, normalize_phone, tax_id_checksum_ok
from accounts.models import Kind, User, audit
from tenants.models import Organization, OrgKind, Tenant, TenantKind, TenantStage

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _tax(value):
    v = re.sub(r"\D", "", value or "")
    if len(v) < 12 and v:
        v = v.zfill(12)
    return v if len(v) == 12 and tax_id_checksum_ok(v) else None


def _email(value):
    v = (value or "").strip().lower()
    return v if EMAIL_RE.match(v) else ""


def _phone(value):
    v = (value or "").strip()
    if not v or v.startswith("@"):
        return ""
    try:
        return normalize_phone(v)
    except Exception:  # noqa: BLE001 - keep what was written (e.g. foreign numbers)
        return v[:32]


def parse(data: bytes):
    text = data.decode("utf-8-sig", errors="replace") if data[:3] == b"\xef\xbb\xbf" else data.decode("cp1251", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    return [{k.strip().lower(): (v or "").strip() for k, v in r.items() if k} for r in rows if (r.get("tenant") or "").strip()]


def _staff(email, flag, actor):
    """The staff person with this email, given the role `flag` (created as a directory entry if new)."""
    if not email:
        return None
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        user = User.objects.create_user(email, None, kind=Kind.STAFF, login_enabled=False, email_verified=True,
                                        **{flag: True})
        audit(actor, "person_imported", user, email=email, role=flag, source="check sheet")
    elif user.kind != Kind.STAFF:
        return None
    elif not getattr(user, flag):
        setattr(user, flag, True)
        user.save(update_fields=[flag])
        audit(actor, "staff_roles_changed", user, roles=",".join(user.staff_role_labels), source="check sheet")
    return user


def _rep(email, actor):
    return _staff(email, "is_sales", actor)


@transaction.atomic
def apply(rows, actor=None):
    report = {"tenants": 0, "unknown": [], "bad_tax_id": [], "orgs_created": 0, "moved": 0, "tax_ids_set": 0,
              "stage": 0, "corp": 0, "reps": 0, "contacts": 0}
    tenants = {t.group_name[len("tenant-"):]: t for t in Tenant.objects.filter(group_name__startswith="tenant-")
               .select_related("organization")}
    by_tax = {}
    for r in rows:
        name = r["tenant"].lower()
        t = tenants.get(name)
        if t is None:
            report["unknown"].append(name)
            continue
        report["tenants"] += 1
        raw = r.get("bin/iin", "")
        tax = _tax(raw)
        if raw and not tax:
            report["bad_tax_id"].append(f"{name}: {raw}")
        if tax:
            by_tax.setdefault(tax, []).append((t, r))

    # 1. one organization per IIN/BIN
    for tax, items in sorted(by_tax.items()):
        org = Organization.objects.filter(tax_id=tax).first()
        if org is None:
            # reuse the current organization if it has no IIN/BIN and holds only tenants of this IIN/BIN
            names = {t.pk for t, _r in items}
            current = [Organization.objects.filter(pk=t.organization_id).first() for t, _r in items]
            for o in current:
                if o and not o.tax_id and set(o.tenants.values_list("pk", flat=True)) <= names:
                    org = o
                    break
            if org is None:
                for o in current:
                    if o and not o.tax_id:
                        org = o
                        break
            if org is None:
                legal = next((r.get("legal entity") for _t, r in items if r.get("legal entity") and "?" not in r.get("legal entity")), "")
                org = Organization.objects.create(name=legal or items[0][0].hint_legal_name or items[0][0].name)
                report["orgs_created"] += 1
                audit(actor, "organization_saved", org, created=True, source="check sheet")
            org.tax_id, org.tax_id_type = tax, guess_tax_id_type(tax)
            org.save(update_fields=["tax_id", "tax_id_type"])
            report["tax_ids_set"] += 1
            audit(actor, "organization_saved", org, tax_id=tax, source="check sheet")
        for t, _r in items:
            if t.organization_id != org.pk:
                old = t.organization
                t.organization = org
                t.save(update_fields=["organization"])
                report["moved"] += 1
                audit(actor, "tenant_moved", t, frm=old.name if old else "", to=org.name, source="check sheet")

    # 2. per-tenant fields
    for r in rows:
        t = tenants.get(r["tenant"].lower())
        if t is None:
            continue
        t.refresh_from_db()
        status = r.get("status", "").lower()
        changed = []
        if status in ("client", "poc", "pilot"):
            stage = TenantStage.CLIENT if status == "client" else TenantStage.POC
            if t.stage != stage:
                t.stage = stage
                changed.append("stage")
        elif status == "corp":
            if t.kind != TenantKind.CORP:
                t.kind = TenantKind.CORP
                changed.append("kind")
            # the stage (client / poc) of a corporate tenant is not in the sheet: kept as it is
        if changed:
            t.save(update_fields=changed)
            report["stage" if "stage" in changed else "corp"] += 1
            audit(actor, "tenant_updated", t, fields=",".join(changed), source="check sheet")
        org = t.organization
        if org is None:
            continue
        upd = []
        if status == "corp" and org.kind != OrgKind.CORP:
            org.kind = OrgKind.CORP
            upd.append("kind")
        manager = _email(r.get("manager sales"))
        if t.kind == TenantKind.CORP and manager and not t.resource_manager_id:
            t.resource_manager = _staff(manager, "is_resource_manager", actor)
            if t.resource_manager_id:
                t.save(update_fields=["resource_manager"])
                audit(actor, "tenant_updated", t, fields="resource_manager", source="check sheet")
        if org.kind == OrgKind.CORP:
            from tenants.models import PortalSettings
            dse = PortalSettings.get().default_support_engineer
            if dse and not org.support_engineer_id:
                org.support_engineer = dse
                upd.append("support_engineer")
            # Corporate organizations have a resource manager and a support engineer only:
            # the manager column names their resource manager.
            if manager and not org.resource_manager_id:
                org.resource_manager = _staff(manager, "is_resource_manager", actor)
                if org.resource_manager_id:
                    upd.append("resource_manager")
            for f in ("sales_rep", "kam"):
                if getattr(org, f + "_id"):
                    setattr(org, f, None)
                    upd.append(f)
        else:
            if manager and not org.sales_rep_id:
                org.sales_rep = _rep(manager, actor)
                if org.sales_rep_id:
                    upd.append("sales_rep")
                    report["reps"] += 1
            kam = _email(r.get("kam email"))
            if kam and not org.kam_id:
                org.kam = _staff(kam, "is_kam", actor)
                if org.kam_id:
                    upd.append("kam")
        if upd:
            org.save(update_fields=sorted(set(upd)))
            report["contacts"] += 1
            audit(actor, "organization_saved", org, fields=",".join(sorted(set(upd))), source="check sheet")
    return report
