"""Owner 2026-11-15: a GPU cluster's vCores per pCore is 1 (no CPU overcommit on GPU hosts: a bundle is a fixed
share of its host's cores). The ratio is now a setting of every cluster, shown in the same Edit dialog; this sets
it to 1 on the clusters that have GPU hosts. Data only; rolling back leaves the values, which the older code ignored
for GPU clusters anyway."""

from django.db import migrations


def gpu_ratio_one(apps, schema_editor):
    Cluster = apps.get_model("inventory", "Cluster")
    for c in Cluster.objects.all():
        if c.hosts.filter(profile__gpu_count__gt=0).exclude(state="retired").exists() and c.vcpu_ratio != 1:
            c.vcpu_ratio = 1
            c.save(update_fields=["vcpu_ratio"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0085_platformcontrol_vip")]
    operations = [migrations.RunPython(gpu_ratio_one, migrations.RunPython.noop)]
