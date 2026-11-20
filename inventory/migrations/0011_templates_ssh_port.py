from django.db import migrations


def update(apps, schema_editor):
    """ssh: only the template's port (the image starts with 22); igw: no provider NTP/DNS."""
    from inventory.nf_templates import BUILTIN
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for role, name, os_, version, body, desc in BUILTIN:
        ConfigTemplate.objects.update_or_create(role=role, defaults={"name": name, "os": os_, "os_version": version,
                                                                     "body": body, "description": desc})


class Migration(migrations.Migration):
    dependencies = [("inventory", "0010_platformcontrol_health_and_more")]
    operations = [migrations.RunPython(update, migrations.RunPython.noop)]
