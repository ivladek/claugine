"""owner 2026-11-03: a platform user may be an organization admin or a project admin only when flagged eligible.

Adds the two eligibility fields and backfills them for the platform users who already hold those roles, so existing
assignments (e.g. the Freedom Cloud staff admins) are kept.
"""
from django.db import migrations, models


def backfill(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    Membership = apps.get_model("tenants", "Membership")
    # org-admin eligibility: platform users who are already an organization admin
    User.objects.filter(kind="staff", is_org_admin=True).update(may_org_admin=True)
    # project-admin eligibility: platform users who already administer a project
    staff_ids = list(Membership.objects.filter(role="tenant_admin", user__kind="staff")
                     .values_list("user_id", flat=True).distinct())
    if staff_ids:
        User.objects.filter(pk__in=staff_ids).update(may_tenant_admin=True)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0015_city_default_almaty"),
        ("tenants", "0082_project_admins_backfill"),
    ]

    operations = [
        migrations.AddField("user", "may_org_admin", models.BooleanField(default=False,
                            verbose_name="may be an organization admin")),
        migrations.AddField("user", "may_tenant_admin", models.BooleanField(default=False,
                            verbose_name="may be a project admin")),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
