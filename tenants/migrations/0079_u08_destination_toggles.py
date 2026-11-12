"""u08 (owner 2026-10-31): each backup destination has its own on/off switch (4 destinations, 2 per block), instead of
one switch per block. The new local switches are initialized from the existing configuration so nothing silently stops."""

from django.db import migrations, models


def init_switches(apps, schema_editor):
    PortalSettings = apps.get_model("tenants", "PortalSettings")
    for s in PortalSettings.objects.all():
        s.backup_local = bool((s.backup_path or "").strip())                       # portal local was on if a folder was set
        s.fe_backup_local = bool(s.fe_backup and (s.fe_backup_path or "").strip())  # platform local was on if the block was on with a folder
        s.save(update_fields=["backup_local", "fe_backup_local"])


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0078_u07_backup_destinations"),
    ]

    operations = [
        migrations.AddField(
            model_name="portalsettings",
            name="backup_local",
            field=models.BooleanField(default=True, verbose_name="to a local folder"),
        ),
        migrations.AddField(
            model_name="portalsettings",
            name="fe_backup_local",
            field=models.BooleanField(default=False, verbose_name="to a local folder"),
        ),
        migrations.AlterField(
            model_name="portalsettings",
            name="backup_s3_prefix",
            field=models.CharField(blank=True, default="", max_length=200, verbose_name="S3 prefix"),
        ),
        migrations.AlterField(
            model_name="portalsettings",
            name="fe_backup_s3_prefix",
            field=models.CharField(blank=True, default="", max_length=200, verbose_name="S3 prefix"),
        ),
        migrations.RunPython(init_switches, migrations.RunPython.noop),
    ]
