from django.db import migrations


def update(apps, schema_editor):
    """egw: static routes of the extra public networks via the igw; cgw: one interface per corporate network."""
    from inventory.nf_templates import BUILTIN
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for role, name, os_, version, body, desc in BUILTIN:
        if role in ("egw", "cgw"):
            ConfigTemplate.objects.filter(role=role).update(body=body)


class Migration(migrations.Migration):
    dependencies = [("inventory", "0014_templates_service_user")]
    operations = [migrations.RunPython(update, migrations.RunPython.noop)]
