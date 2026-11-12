"""VyOS: no "-- MARK --" lines in the logs (owner, 2026-10-28 19:02): every stored VyOS template with the management
part gets `set system syslog marker disable` right after its syslog lines. Templates whose management part was edited
by hand (the known lines not found) stay as they are (reported). Backward: restore the database backup taken before."""
from django.db import migrations

OLD = "set system syslog remote {{ syslog.host }} vrf MGMT\n{% endif %}"
LINE = "set system syslog marker disable\n"


def forward(apps, schema_editor):
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for t in ConfigTemplate.objects.all():
        if LINE in t.body:
            continue
        if OLD in t.body:
            t.body = t.body.replace(OLD, OLD + LINE)
            t.save(update_fields=["body"])
        elif "system syslog remote" in t.body:
            print(f"  inventory 0067: {t.role} not changed (its syslog lines were edited by hand)")


class Migration(migrations.Migration):
    dependencies = [("inventory", "0066_cloud_gw")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
