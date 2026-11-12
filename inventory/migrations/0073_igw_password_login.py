"""Correction 14 (owner 2026-10-29 12:22): 0072 did not change the stored igw template on dc1/dc2 (its ssh lines were
edited by hand), so a tenant igw made without an ssh key kept the image's key-only ssh. The line goes before the
time-zone line; the configuration builder adds it as well when a template lacks it."""
from django.db import migrations

LINE = "{% if not igw_keys %}delete service ssh disable-password-authentication\n{% endif %}"
TZ = "set system time-zone {{ time_zone }}\n"


def forward(apps, schema_editor):
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for t in ConfigTemplate.objects.filter(role="igw"):
        if "delete service ssh disable-password-authentication" in t.body:
            continue
        if TZ in t.body:
            t.body = t.body.replace(TZ, LINE + TZ, 1)
            t.save(update_fields=["body"])
            print("  inventory 0073: igw template — password login without a key")
        else:
            print("  inventory 0073: igw not changed (no time-zone line); the builder adds the line")


class Migration(migrations.Migration):
    dependencies = [("inventory", "0072_igw_password_ssh")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
