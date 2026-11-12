"""u10 (owner 2026-11-02): seed the new IP Subnets once from today's delegated subnets, so BGP/provisioning keep
working while the source of truth moves into the IP Subnets tab. Idempotent; reversible (removes them)."""
import ipaddress

from django.db import migrations


def seed(apps, schema_editor):
    DelegatedSubnet = apps.get_model("inventory", "DelegatedSubnet")
    IPSubnet = apps.get_model("inventory", "IPSubnet")
    Network = apps.get_model("inventory", "Network")
    for d in DelegatedSubnet.objects.select_related("site").all():
        try:
            net = ipaddress.ip_network(d.cidr, strict=False)
        except ValueError:
            continue
        subnet, prefix = str(net.network_address), net.prefixlen
        if IPSubnet.objects.filter(site=d.site, subnet=subnet, prefix=prefix).exists():
            continue
        ixnet = Network.objects.filter(site=d.site.code, name=d.segment, kind="ix").first()
        purpose = "corpnet" if net.is_private else "inet"
        IPSubnet.objects.create(site=d.site, subnet=subnet, prefix=prefix, max_prefix=prefix,
                                routing="bgp", purpose=purpose, network=ixnet)


def unseed(apps, schema_editor):
    apps.get_model("inventory", "IPSubnet").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("inventory", "0077_ipsubnet")]
    operations = [migrations.RunPython(seed, unseed)]
