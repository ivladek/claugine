"""The route reflectors' templates no longer set the MTU of eth1 (correction 09): OpenNebula gives every NIC its MTU
when the VM is deployed (the network's GUEST_MTU in the context). Reverses 0052."""
from django.db import migrations

LINE = "{% if ix.mtu %}set interfaces ethernet eth1 mtu {{ ix.mtu }}\n{% endif %}"
AFTER = 'set interfaces ethernet eth1 description "{{ ix.name }}"\n'


def forward(apps, schema_editor):
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for t in ConfigTemplate.objects.filter(role__in=["rr-inet", "rr-corpnet", "rr-vxlan"]):
        if LINE in t.body:
            t.body = t.body.replace(LINE, "")
            t.save(update_fields=["body"])


def backward(apps, schema_editor):
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for t in ConfigTemplate.objects.filter(role__in=["rr-inet", "rr-corpnet", "rr-vxlan"]):
        if AFTER in t.body and "eth1 mtu" not in t.body:
            t.body = t.body.replace(AFTER, AFTER + LINE, 1)
            t.save(update_fields=["body"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0058_kind_labels")]
    operations = [migrations.RunPython(forward, backward)]
