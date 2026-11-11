"""VyOS 2026.03 has no "system syslog vrf": the VRF is set per remote ("system syslog remote <host> vrf MGMT"). The
first boot rejected the old line, so the redeployed route reflectors sent their logs through the default VRF, which
has no route to the collector. The templates get the per-remote line instead, and lose the name servers (a router
resolves in its default VRF, where they are not reachable; the collector is given as an IP: nfconfig.syslog_target).
NTP stays: it runs in VRF MGMT ("service ntp vrf MGMT") with the site's NTP servers as IPs."""
from django.db import migrations

OLD_VRF = "set system syslog vrf MGMT\n"
PROTO = "set system syslog remote {{ syslog.host }} protocol {{ syslog.proto }}\n"
NEW_PROTO = PROTO + "set system syslog remote {{ syslog.host }} vrf MGMT\n"
DNS = "{% for d in dns %}set system name-server {{ d }}\n{% endfor %}"
DROPPED = (OLD_VRF, DNS)


def forward(apps, schema_editor):
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for t in ConfigTemplate.objects.all():
        if any(x in t.body for x in DROPPED):
            body = t.body
            for x in DROPPED:
                body = body.replace(x, "")
            if PROTO in body and NEW_PROTO not in body:
                body = body.replace(PROTO, NEW_PROTO, 1)
            t.body = body
            t.save(update_fields=["body"])


def backward(apps, schema_editor):  # the syslog line only: the name servers are not put back
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for t in ConfigTemplate.objects.all():
        if NEW_PROTO in t.body:
            t.body = t.body.replace(NEW_PROTO, PROTO).replace("{% if syslog %}", "{% if syslog %}" + OLD_VRF, 1)
            t.save(update_fields=["body"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0052_rr_mtu")]
    operations = [migrations.RunPython(forward, backward)]
