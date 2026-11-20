"""The tenant igw template too: `set system syslog marker disable` after its time zone (owner, 2026-10-28 19:33: "update
all vyos templates"). A body whose time-zone line was edited by hand stays as it is (reported)."""
from django.db import migrations

OLD = "{% endfor %}set system time-zone {{ time_zone }}\n"
LINE = "set system syslog marker disable\n"


def forward(apps, schema_editor):
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for t in ConfigTemplate.objects.filter(role="igw"):
        if LINE in t.body:
            continue
        if OLD in t.body:
            t.body = t.body.replace(OLD, OLD + LINE, 1)
            t.save(update_fields=["body"])
        else:
            print("  inventory 0068: igw not changed (its time-zone line was edited by hand)")


class Migration(migrations.Migration):
    dependencies = [("inventory", "0067_syslog_marker")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
