"""People of adopted tenants.

- Each tenant has an associated OpenNebula user: the group admin of its group (older tenants: admin-<name>).
  The collector records the users of each tenant group; this links the admin user to the tenant and,
  when OpenNebula knows its email, adds that person to the portal as the tenant admin.
- Tenant users come from the sales team's check sheet (client email). The first person of a tenant becomes
  its tenant admin, the first person of an organization its organization admin.
People added this way are directory entries: they cannot sign in until they are invited and activate
their account. A person belongs to one organization; conflicts are reported, never forced.
Support engineers and super admins can also be users of corporate tenants.
"""
import re

from django.db import transaction

from accounts.kz import to_international
from accounts.models import Kind, User, audit
from tenants.models import Membership, Tenant, TenantKind

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _person(email, org, report, actor, source):
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        return None
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        user = User.objects.create_user(email, None, kind=Kind.CUSTOMER, organization=org, login_enabled=False,
                                        email_verified=True)
        audit(actor, "person_imported", user, email=email, organization=org.name, source=source)
        report["people_created"] += 1
        return user
    if user.kind != Kind.CUSTOMER:
        report["conflicts"].append(f"{email}: is a staff account")
        return None
    if user.organization_id is None:
        user.organization = org
        user.save(update_fields=["organization"])
        return user
    if user.organization_id != org.pk:
        report["conflicts"].append(f"{email}: already in {user.organization.name}, not added to {org.name}")
        return None
    return user


def _member(user, tenant, role, report):
    m, created = Membership.objects.get_or_create(user=user, tenant=tenant, defaults={"role": role})
    if created:
        report["memberships"] += 1
    elif role == Membership.Role.TENANT_ADMIN and m.role != role:
        m.role = role
        m.save(update_fields=["role"])
        report["memberships"] += 1


@transaction.atomic
def users_from_checksheet(rows, actor=None):
    report = {"people_created": 0, "memberships": 0, "phones": 0, "org_admins": 0, "conflicts": [], "skipped": []}
    tenants = {t.group_name[len("tenant-"):]: t for t in Tenant.objects.filter(group_name__startswith="tenant-")
               .select_related("organization")}
    for r in rows:
        t = tenants.get(r["tenant"].lower())
        email = (r.get("client email") or "").strip().lower()
        if t is None or not email:
            continue
        if t.organization is None:
            report["skipped"].append(f"{t.name}: no organization")
            continue
        staff = User.objects.filter(email__iexact=email, kind=Kind.STAFF).first()
        if staff is not None:
            # Support engineers and super admins can be users of corporate tenants; never of cloud ones.
            if t.kind == TenantKind.CORP and (staff.is_support or staff.is_super_admin):
                has_admin = Membership.objects.filter(tenant=t, role=Membership.Role.TENANT_ADMIN).exclude(user=staff).exists()
                _member(staff, t, Membership.Role.TENANT_USER if has_admin else Membership.Role.TENANT_ADMIN, report)
            else:
                report["conflicts"].append(f"{email}: is a staff account, not added to {t.name}")
            continue
        user = _person(email, t.organization, report, actor, "check sheet")
        if user is None and not EMAIL_RE.match(email):
            report["skipped"].append(f"{t.name}: {email!r} is not an email")
        if user is not None:
            # The first person of a tenant becomes its tenant admin, the first of an organization its org admin.
            has_admin = Membership.objects.filter(tenant=t, role=Membership.Role.TENANT_ADMIN).exclude(user=user).exists()
            _member(user, t, Membership.Role.TENANT_USER if has_admin else Membership.Role.TENANT_ADMIN, report)
            if not User.objects.filter(organization=t.organization, is_org_admin=True).exists():
                user.is_org_admin = True
                user.save(update_fields=["is_org_admin"])
                report["org_admins"] += 1
            phone = to_international(r.get("client phone"))
            if phone and not user.phone:
                user.phone = phone
                user.save(update_fields=["phone"])
                report["phones"] += 1
            elif r.get("client phone") and not phone:
                report["skipped"].append(f"{t.name}: phone {r.get('client phone')!r} not recognised")
    return report


@transaction.atomic
def import_one_admins(actor=None):
    """Link each tenant to its OpenNebula admin user from the last collection."""
    from .models import Observation
    report = {"linked": 0, "missing": [], "people_created": 0, "memberships": 0, "conflicts": []}
    from inventory.models import Platform
    known = set(Platform.objects.values_list("key", flat=True))
    # Only platforms of the inventory: extra endpoints such as stage may have groups with the same names.
    observations, on_platform = {}, {}
    for o in Observation.objects.filter(platform_key__in=known).order_by("collected_at"):
        observations[o.group_name] = o
        on_platform[(o.platform_key, o.group_name)] = o
    report["groups_linked"] = 0
    for t in Tenant.objects.exclude(group_name__isnull=True).select_related("organization"):
        o = on_platform.get(((t.order or {}).get("platform") or "", t.group_name)) or observations.get(t.group_name)
        if o is None:
            continue
        # the group's ID, as a tenant the portal made has it (2026-10-27: adopted tenants get the same information)
        if o.group_id is not None and t.one_group_id != o.group_id and not t.is_deleted:
            t.one_group_id = o.group_id
            t.save(update_fields=["one_group_id"])
            audit(actor, "one_group_linked", t, group=o.group_name, id=o.group_id, platform=o.platform_key)
            report["groups_linked"] += 1
        users = [u for u in (o.users or []) if isinstance(u, dict)]
        legacy = t.group_name[len("tenant-"):] if t.group_name.startswith("tenant-") else t.name
        # The group admin of the OpenNebula group; older data: the user named admin-<name>.
        admin = next((u for u in users if u.get("admin")), None) or \
            next((u for u in users if u.get("name") == f"admin-{legacy}"), None) or \
            next((u for u in users if str(u.get("name", "")).startswith("admin-")), None)
        if admin is None:
            report["missing"].append(t.group_name)
            continue
        if (t.one_admin_user, t.one_admin_id) != (admin["name"], admin["id"]):
            t.one_admin_user, t.one_admin_id = admin["name"], admin["id"]
            t.save(update_fields=["one_admin_user", "one_admin_id"])
            audit(actor, "one_admin_linked", t, user=admin["name"], id=admin["id"])
            report["linked"] += 1
        if admin.get("email") and t.organization_id:
            user = _person(admin["email"], t.organization, report, actor, "opennebula")
            if user is not None:
                _member(user, t, Membership.Role.TENANT_ADMIN, report)
    return report
