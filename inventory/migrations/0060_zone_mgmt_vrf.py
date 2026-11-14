"""The zone MGMT of the VyOS templates also names the VRF MGMT: in VyOS 2026.03 a zone whose interface is in a VRF
matches its traffic only when the VRF is a member too (vyos.dev T8761); with the interface alone, everything to the
router's management address (ping, ssh) hit the local zone's default drop (portal-ix RRs, 2026-10-27)."""
from django.db import migrations

IFACE = "set firewall zone MGMT member interface eth0\n"
VRF = "set firewall zone MGMT member vrf MGMT\n"


def forward(apps, schema_editor):
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for t in ConfigTemplate.objects.all():
        if IFACE in t.body and VRF not in t.body:
            t.body = t.body.replace(IFACE, IFACE + VRF)
            t.save(update_fields=["body"])


def backward(apps, schema_editor):
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for t in ConfigTemplate.objects.all():
        if VRF in t.body:
            t.body = t.body.replace(VRF, "")
            t.save(update_fields=["body"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0059_rr_no_mtu")]
    operations = [migrations.RunPython(forward, backward)]
