"""The route reflectors' templates set the MTU of their network's NIC (eth1) from the site network (e.g. 9050 on the
VXLAN underlay, as the running route reflectors and every KVM host's VTEP have it), not only from the platform's
network through the context. Templates edited by hand without that line get it; others stay."""
from django.db import migrations

OLD = 'set interfaces ethernet eth1 description "{{ ix.name }}"\n'
NEW = OLD + "{% if ix.mtu %}set interfaces ethernet eth1 mtu {{ ix.mtu }}\n{% endif %}"


def forward(apps, schema_editor):
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for t in ConfigTemplate.objects.filter(role__in=["rr-inet", "rr-corpnet", "rr-vxlan"]):
        if OLD in t.body and "eth1 mtu" not in t.body:
            t.body = t.body.replace(OLD, NEW, 1)
            t.save(update_fields=["body"])


def backward(apps, schema_editor):
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for t in ConfigTemplate.objects.filter(role__in=["rr-inet", "rr-corpnet", "rr-vxlan"]):
        t.body = t.body.replace(NEW, OLD)
        t.save(update_fields=["body"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0051_core_definitions")]
    operations = [migrations.RunPython(forward, backward)]
