"""owner 2026-11-09 (job gpu-bundles): the GPU bundle becomes a setting of the cluster.

Until now the bundle was worked out at read time from the cluster's reserve percentages
(``tenants.ordering.bundle_of``). It is now stored, so staff can set it, and the reserve percentages and
``vcpu_ratio`` stop applying to GPU clusters at all.

**This migration must not move any existing bundle.** A running project's vCPU and RAM were sized from the bundle
as it resolved on the day it was ordered, so each existing GPU cluster is given exactly the value the old formula
produces today - the old formula, with the reserve percentages, not the new 10% default. New clusters get the new
default offered in the form instead.
"""
import math

from django.db import migrations, models


def _old_bundle(profile, cluster):
    """What ``bundle_of`` returns today, inlined: historical models carry no methods."""
    gpus = profile.gpu_count or 1
    cores = math.floor(profile.cores / gpus * (100 - (cluster.reserved_cpu_pct or 0)) / 100)
    ram = math.floor(profile.ram_gb / gpus * (100 - (cluster.reserved_ram_pct or 0)) / 100 / 8) * 8
    return max(cores, 0), max(ram, 0)


def freeze(apps, schema_editor):
    Cluster = apps.get_model("inventory", "Cluster")
    for c in Cluster.objects.all():
        best = None
        for h in c.hosts.all():
            if h.state != "active" or not h.profile_id or not h.profile or not h.profile.gpu_count:
                continue
            one = _old_bundle(h.profile, c)
            if best is None or one < best:   # the smallest, as pools() has always taken
                best = one
        if best and (best[0] or best[1]):
            c.bundle_cores, c.bundle_ram_gb = best
            c.save(update_fields=["bundle_cores", "bundle_ram_gb"])


def thaw(apps, schema_editor):
    """Backwards: clear the stored bundle, and the old read-time formula applies again."""
    Cluster = apps.get_model("inventory", "Cluster")
    Cluster.objects.update(bundle_cores=0, bundle_ram_gb=0)


class Migration(migrations.Migration):
    dependencies = [("inventory", "0078_seed_ipsubnets")]

    operations = [
        migrations.AddField(
            model_name="cluster", name="bundle_cores",
            field=models.PositiveSmallIntegerField(default=0, verbose_name="vCores per GPU bundle"),
        ),
        migrations.AddField(
            model_name="cluster", name="bundle_ram_gb",
            field=models.PositiveSmallIntegerField(default=0, verbose_name="GB RAM per GPU bundle"),
        ),
        migrations.AlterField(
            model_name="cluster", name="spare_hosts",
            field=models.PositiveSmallIntegerField(
                default=1,
                help_text="X: hosts left out of the available resources; none is a dedicated spare.",
                verbose_name="spare hosts (N+X)"),
        ),
        migrations.RunPython(freeze, thaw),
    ]
