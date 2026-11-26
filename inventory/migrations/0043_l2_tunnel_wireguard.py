from django.db import migrations


def wireguard(apps, schema_editor):
    """The L2 src/dst tunnel goes into WireGuard with random keys made with the gateways (never kept). Templates
    still as shipped get the new body; edited ones are left as they are (Infrastructure → Templates)."""
    from inventory.nf_templates import BUILTIN, L2, L2_PLAIN
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    desc = {role: d for role, _n, _o, _v, _b, d in BUILTIN}
    for role in ("l2-a", "l2-b"):
        for t in ConfigTemplate.objects.filter(role=role):
            fields = ["description"]
            t.description = desc[role]
            if t.body.strip() == L2_PLAIN.strip():
                t.body = L2
                fields.append("body")
            t.save(update_fields=fields)


class Migration(migrations.Migration):
    dependencies = [("inventory", "0042_platform_public_url")]
    operations = [migrations.RunPython(wireguard, migrations.RunPython.noop)]
