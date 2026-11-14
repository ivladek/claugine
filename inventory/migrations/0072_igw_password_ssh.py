"""Correction 14 (2026-10-29): a tenant igw made without an ssh key lets its user in over ssh with the password — the
image always sets disable-password-authentication, the template deletes it when the igw has no key. A body whose
time-zone line was edited by hand stays as it is (reported)."""
from django.db import migrations

OLD = "{% endfor %}set system time-zone {{ time_zone }}\n"
NEW = ("{% endfor %}{% if not igw_keys %}delete service ssh disable-password-authentication\n"
       "{% endif %}set system time-zone {{ time_zone }}\n")


def forward(apps, schema_editor):
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for t in ConfigTemplate.objects.filter(role="igw"):
        if "disable-password-authentication" in t.body:
            continue
        if OLD in t.body:
            t.body = t.body.replace(OLD, NEW, 1)
            t.save(update_fields=["body"])
        else:
            print("  inventory 0072: igw not changed (its time-zone line was edited by hand)")


class Migration(migrations.Migration):
    dependencies = [("inventory", "0071_host_health")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
