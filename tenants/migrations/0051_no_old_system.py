"""The old management system (ssot) is no longer read (2026-10-28: the portal is the only system managing the
platforms): its connection settings go. What they were is kept in the audit log (no secret: the key's reference only)."""
from django.db import migrations


def record(apps, schema_editor):
    PortalSettings = apps.get_model("tenants", "PortalSettings")
    AuditLog = apps.get_model("accounts", "AuditLog")
    s = PortalSettings.objects.filter(pk=1).first()
    if s is None or not (s.source_ssh_target or s.source_sqlite_path or s.source_secret):
        return
    AuditLog.objects.create(action="old_system_disconnected", target_type="portalsettings", target_id=str(s.pk),
                            details={"source_ssh_target": s.source_ssh_target, "source_sqlite_path": s.source_sqlite_path,
                                     "source_secret": s.source_secret, "by": "migration tenants 0051"})


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0050_platform_refresh"),
        ("accounts", "0010_archived_help_text"),
    ]

    operations = [
        migrations.RunPython(record, migrations.RunPython.noop),
        migrations.RemoveField(model_name="portalsettings", name="source_secret"),
        migrations.RemoveField(model_name="portalsettings", name="source_sqlite_path"),
        migrations.RemoveField(model_name="portalsettings", name="source_ssh_target"),
    ]
