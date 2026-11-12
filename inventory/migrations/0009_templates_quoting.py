from django.db import migrations


def update(apps, schema_editor):
    """Built-in templates without the first-boot defaults and with the new quoting."""
    from inventory.nf_templates import BUILTIN
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for role, name, os_, version, body, desc in BUILTIN:
        ConfigTemplate.objects.update_or_create(role=role, defaults={"name": name, "os": os_, "os_version": version,
                                                                     "body": body, "description": desc})


class Migration(migrations.Migration):
    dependencies = [("inventory", "0008_builtin_templates")]
    operations = [migrations.RunPython(update, migrations.RunPython.noop)]
