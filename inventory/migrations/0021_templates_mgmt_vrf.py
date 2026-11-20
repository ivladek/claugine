from django.db import migrations


def update(apps, schema_editor):
    """Provider routers (all but the igw): eth0 in VRF MGMT on cloud-vyos, ssh only there from the management hosts;
    the other interfaces move up by one."""
    from inventory.nf_templates import BUILTIN
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for role, name, os_, version, body, desc in BUILTIN:
        if role in ("egw", "cgw", "l2-a", "l2-b", "rr-inet", "rr-corpnet", "rr-vxlan"):
            ConfigTemplate.objects.filter(role=role).update(body=body, description=desc)


class Migration(migrations.Migration):
    dependencies = [("inventory", "0020_site_management")]
    operations = [migrations.RunPython(update, migrations.RunPython.noop)]
