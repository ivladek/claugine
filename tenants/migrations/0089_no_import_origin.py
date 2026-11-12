"""Owner 2026-11-11: "no import from other systems in the future; from today only fresh installs". The records the
one-time import left become ordinary portal records; tenant-<name> groups are recognised by their name."""
from django.db import migrations, models


def forward(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    Tenant.objects.filter(origin="adopted").update(origin="portal")
    for name in ("QuotaRecord", "Connectivity", "PrefixAssignment"):
        apps.get_model("tenants", name).objects.filter(source="import").update(source="portal")


class Migration(migrations.Migration):
    dependencies = [("tenants", "0088_drop_old_system_import")]

    operations = [
        migrations.RunPython(forward, migrations.RunPython.noop),
        migrations.RemoveField(model_name="tenant", name="igw_adoption"),
        migrations.RemoveField(model_name="tenant", name="igw_adoption_note"),
        migrations.AlterField(
            model_name="connectivity",
            name="source",
            field=models.CharField(
                choices=[("portal", "Portal"), ("manual", "Entered by staff")],
                default="portal",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="prefixassignment",
            name="source",
            field=models.CharField(
                choices=[("portal", "Portal"), ("manual", "Entered by staff")],
                default="portal",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="quotarecord",
            name="source",
            field=models.CharField(
                choices=[("portal", "Portal"), ("manual", "Entered by staff")],
                default="portal",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="tenant",
            name="origin",
            field=models.CharField(
                choices=[
                    ("portal", "Registered in the portal"),
                    ("test", "Test project"),
                ],
                default="portal",
                max_length=16,
                verbose_name="origin",
            ),
        ),
        migrations.AlterField(
            model_name="tenant",
            name="group_name",
            field=models.CharField(
                blank=True,
                help_text="Fixed on approval: OpenNebula groups cannot be renamed.",
                max_length=32,
                null=True,
                unique=True,
                verbose_name="OpenNebula group",
            ),
        ),
        migrations.AlterField(
            model_name="tenant",
            name="igw_config_encrypted",
            field=models.TextField(
                blank=True,
                help_text="the igw's configuration (set commands), encrypted",
            ),
        ),
    
    ]
