from django.db import migrations


def seed(apps, schema_editor):
    from inventory.nf_templates import BUILTIN
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for role, name, os_, version, body, desc in BUILTIN:
        ConfigTemplate.objects.get_or_create(role=role, defaults={"name": name, "os": os_, "os_version": version,
                                                                  "body": body, "description": desc})


class Migration(migrations.Migration):
    dependencies = [("inventory", "0007_host_ipmi_host_host_ipmi_type_host_ipmi_user_and_more")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
