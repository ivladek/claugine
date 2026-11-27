"""T36 B3b (owner 2026-11-25 17:40): the primary site (zk1 where it exists, else the first site; never moved) and the
site service type claugine (rules v2 A6). Reversible: the field goes, the choices change back."""

from django.db import migrations, models

PRIMARY = "zk1"  # the production installation's primary site (owner: "zk1 on prod by the migration")


def forward(apps, schema_editor):
    Site = apps.get_model("inventory", "Site")
    if Site.objects.filter(primary=True).exists():
        return
    s = Site.objects.filter(code=PRIMARY).first() or Site.objects.order_by("pk").first()
    if s is not None:
        Site.objects.filter(pk=s.pk).update(primary=True)


class Migration(migrations.Migration):
    dependencies = [("inventory", "0123_t36_node_state")]

    operations = [
        migrations.AddField(
            model_name="site",
            name="primary",
            field=models.BooleanField(default=False, editable=False, verbose_name="primary site"),
        ),
        migrations.RunPython(forward, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="coreservice",
            name="stype",
            field=models.CharField(
                choices=[
                    ("bgp-rr", "bgp-rr: route reflectors of a BGP network"),
                    ("cloud-egw", "cloud-egw: internet edge of the core services"),
                    ("cloud-igw", "cloud-igw: gateway of the core services"),
                    ("cloud-gw", "cloud-gw: the core services' egw and igw (two VMs)"),
                    ("portal-gw", "portal-gw: the egws of a portal platform (two VMs, VRRP on the public overlays)"),
                    ("ns", "ns: name servers"),
                    ("logs", "logs: log collector"),
                    ("admin", "admin: admin jump hosts"),
                    ("backup", "backup: backup storage of a datastore"),
                    ("onefe", "onefe: OpenNebula front end of a platform"),
                    ("claugine", "claugine: the Claugine portal"),
                    ("core-vm", "core-vm: any other VMs or hosts"),
                ],
                default="core-vm",
                max_length=16,
                verbose_name="service type",
            ),
        ),
    ]
