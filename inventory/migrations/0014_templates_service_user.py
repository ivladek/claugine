from django.db import migrations


def update(apps, schema_editor):
    """igw: the portal's service user (reads the configuration); egw/cgw/RR: the portal in SSHFROM (a variable)."""
    from inventory.nf_templates import BUILTIN
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for role, name, os_, version, body, desc in BUILTIN:
        if role == "igw":
            ConfigTemplate.objects.filter(role=role).update(body=body)


class Migration(migrations.Migration):
    dependencies = [("inventory", "0013_credential_created_at_credential_retired_at_and_more")]
    operations = [migrations.RunPython(update, migrations.RunPython.noop)]
