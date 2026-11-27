from django.db import migrations


def update(apps, schema_editor):
    """igw: every ssh key of the tenant assigned to the igw."""
    from inventory.nf_templates import BUILTIN
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for role, name, os_, version, body, desc in BUILTIN:
        if role == "igw":
            ConfigTemplate.objects.filter(role=role).update(body=body)


class Migration(migrations.Migration):
    dependencies = [("inventory", "0011_templates_ssh_port")]
    operations = [migrations.RunPython(update, migrations.RunPython.noop)]
