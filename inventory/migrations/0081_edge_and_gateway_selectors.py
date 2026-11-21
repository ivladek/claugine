"""The edge and gateway clusters become selections, not flags (owner 2026-11-09).

"only one edge per site / only one gateway per platform", then: "move edge cluster selection to site settings and
gateway cluster selection to the platform - more controlled". A selector cannot express "two", so the limit holds
by construction; `Cluster.hosts_gateways` (migration 0080, never used in production) goes away with it.

Seeding keeps whatever is in force today: each platform's marked cluster becomes its `gateway_cluster`, and each
site's `kind="edge"` cluster becomes its `edge_cluster`. The `edge` kind stays as a label; nothing reads it to
decide placement any more.
"""
from django.db import migrations, models
import django.db.models.deletion


def seed(apps, schema_editor):
    Cluster = apps.get_model("inventory", "Cluster")
    Platform = apps.get_model("inventory", "Platform")
    Site = apps.get_model("inventory", "Site")
    for p in Platform.objects.all():
        c = (Cluster.objects.filter(platform=p, hosts_gateways=True).exclude(state="retired")
             .order_by("name").first())
        if c is not None:
            p.gateway_cluster = c
            p.save(update_fields=["gateway_cluster"])
    for s in Site.objects.all():
        c = (Cluster.objects.filter(platform__site=s, kind="edge").exclude(state="retired")
             .order_by("name").first())
        if c is not None:
            s.edge_cluster = c
            s.save(update_fields=["edge_cluster"])


def thaw(apps, schema_editor):
    Cluster = apps.get_model("inventory", "Cluster")
    Platform = apps.get_model("inventory", "Platform")
    for p in Platform.objects.exclude(gateway_cluster=None):
        Cluster.objects.filter(pk=p.gateway_cluster_id).update(hosts_gateways=True)


class Migration(migrations.Migration):

    dependencies = [("inventory", "0080_gateway_cluster")]

    operations = [
        migrations.AddField(
            model_name="platform", name="gateway_cluster",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="gateway_for", to="inventory.cluster",
                                    verbose_name="gateway cluster",
                                    help_text="Where the project gateways run - the egw, the cgw and the overlay "
                                              "side of an L2 gateway - wherever the project itself is placed. "
                                              "Shared clusters only; empty means the project's own cluster."),
        ),
        migrations.AddField(
            model_name="site", name="edge_cluster",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="edge_for", to="inventory.cluster", verbose_name="edge cluster",
                                    help_text="Where the route reflectors and the L2 gateway endpoints that "
                                              "connect to VLANs run. One per site; empty means the site's "
                                              "management platform carries them."),
        ),
        migrations.RunPython(seed, thaw),
        migrations.RemoveField(model_name="cluster", name="hosts_gateways"),
    ]
