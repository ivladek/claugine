"""Owner 2026-11-03: each organization must have at least one org admin, and each project must have at least one
project admin (the user who gets the credentials); a user may hold both roles. Backfill existing data:

- every organization with active users but no active org admin gets one (its first active user);
- every live project (status not saved/archived/deleting) without an active project admin gets one — its sole
  project-admin member, else an org admin, else the first org user — set both as the tenant_admin and as a
  tenant-admin membership.
"""
from django.db import migrations

GONE = ("saved", "archived", "deleting")


def backfill(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    Tenant = apps.get_model("tenants", "Tenant")
    Membership = apps.get_model("tenants", "Membership")

    def org_users(org_id):
        return sorted(
            User.objects.filter(organization_id=org_id, kind="customer", is_active=True),
            key=lambda u: (not u.is_org_admin, (u.full_name or "").lower(), (u.email or "").lower(), u.pk))

    # 1) every organization with users keeps at least one org admin
    org_ids = Tenant.objects.exclude(organization__isnull=True).values_list("organization_id", flat=True)
    org_ids = set(org_ids) | set(User.objects.filter(kind="customer").values_list("organization_id", flat=True))
    for oid in org_ids:
        if oid is None:
            continue
        users = org_users(oid)
        if users and not any(u.is_org_admin for u in users):
            u = users[0]
            u.is_org_admin = True
            u.save(update_fields=["is_org_admin"])

    # 2) every live project gets a current, active project admin
    for t in Tenant.objects.exclude(status__in=GONE).select_related("tenant_admin"):
        if t.organization_id is None:
            continue
        cur = t.tenant_admin
        if cur is not None and cur.is_active:
            continue
        users = org_users(t.organization_id)
        if not users:
            continue
        by_pk = {u.pk: u for u in users}
        members = [by_pk[m.user_id] for m in
                   Membership.objects.filter(tenant_id=t.pk, role="tenant_admin") if m.user_id in by_pk]
        if len(members) == 1:
            pick = members[0]
        else:
            admins = [u for u in users if u.is_org_admin]
            pick = admins[0] if admins else (sorted(members, key=lambda u: u.pk)[0] if members else users[0])
        t.tenant_admin = pick
        t.save(update_fields=["tenant_admin"])
        Membership.objects.update_or_create(user_id=pick.pk, tenant_id=t.pk, defaults={"role": "tenant_admin"})


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0081_u09_backup_owner"),
        ("accounts", "0015_city_default_almaty"),
    ]

    operations = [migrations.RunPython(backfill, noop)]
