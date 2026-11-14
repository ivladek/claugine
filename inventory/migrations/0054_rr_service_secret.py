"""The route reflectors' and gateways' service secret is the BGP secret of their network (e.g. "BGP: dc1.portal-vxlan"):
its slots (rrs, igw, egw, …) are the passwords of the network's peer groups, which the template uses. Set where empty."""
from django.db import migrations

MGMT = "cloud-vyos"


def forward(apps, schema_editor):
    CoreService = apps.get_model("inventory", "CoreService")
    Network = apps.get_model("inventory", "Network")
    PeerGroup = apps.get_model("inventory", "PeerGroup")
    for s in CoreService.objects.filter(service_secret="").exclude(template=None):
        if not (s.template.role.startswith("rr-") or s.template.role in ("egw", "igw")):
            continue
        ids = [s.nic1_network_id] + [n.get("network") for n in (s.more_nics or [])]
        net = next((n for n in Network.objects.filter(pk__in=[int(i) for i in ids if i]) if n.name != MGMT
                    and PeerGroup.objects.filter(network=n).exists()), None)
        if net is None:
            continue
        refs = {(g.password or "").split("#")[0] for g in PeerGroup.objects.filter(network=net) if g.password}
        if len(refs) == 1:
            s.service_secret = refs.pop()
            s.save(update_fields=["service_secret"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0053_syslog_vrf")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
