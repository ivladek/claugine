"""Correction 17 (2026-10-29): the egw template's bandwidth control becomes a shaper (fq-codel) on the egress of eth1
and eth2, in place of the ingress limiter and the rate-control (unpredictable drops). The stored template is changed
when it has the old block as the portal wrote it; otherwise it is reported and left for Infrastructure → Templates."""
from django.db import migrations

OLD = """{% if mbps %}set qos interface eth1 egress LIMIT-OUT
set qos interface eth1 ingress LIMIT-IN
set qos policy limiter LIMIT-IN default bandwidth {{ mbps }}mbit
set qos policy rate-control LIMIT-OUT bandwidth {{ mbps }}mbit
{% endif %}"""
NEW = """{% if mbps %}set qos interface eth1 egress LIMIT-OUT
set qos interface eth2 egress LIMIT-OUT
set qos policy shaper LIMIT-OUT bandwidth {{ mbps }}mbit
set qos policy shaper LIMIT-OUT default bandwidth 100%
set qos policy shaper LIMIT-OUT default queue-type fq-codel
{% endif %}"""


def forward(apps, schema_editor):
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for t in ConfigTemplate.objects.filter(role="egw"):
        if NEW in t.body:
            continue
        if OLD in t.body:
            t.body = t.body.replace(OLD, NEW, 1)
            t.save(update_fields=["body"])
            print("  inventory 0074: egw template — shaper on eth1 and eth2 egress")
        else:
            print("  inventory 0074: egw template NOT changed (no old qos block): change it in Infrastructure → Templates")


def backward(apps, schema_editor):
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for t in ConfigTemplate.objects.filter(role="egw"):
        if NEW in t.body:
            t.body = t.body.replace(NEW, OLD, 1)
            t.save(update_fields=["body"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0073_igw_password_login")]
    operations = [migrations.RunPython(forward, backward)]
