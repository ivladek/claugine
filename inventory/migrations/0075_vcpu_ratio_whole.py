"""Owner 2026-10-29 20:59: the vCores per pCore ratio is a whole number (every cluster had 10.00)."""
from django.db import migrations, models


def whole(apps, schema_editor):
    Cluster = apps.get_model("inventory", "Cluster")
    for c in Cluster.objects.all():
        r = int(round(float(c.vcpu_ratio or 10))) or 10
        if c.vcpu_ratio != r:
            Cluster.objects.filter(pk=c.pk).update(vcpu_ratio=r)


class Migration(migrations.Migration):
    dependencies = [("inventory", "0074_egw_shaper")]

    operations = [
        migrations.RunPython(whole, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="cluster", name="vcpu_ratio",
            field=models.PositiveSmallIntegerField(default=10, help_text="How many vCPUs one physical core (no hyperthreads) gives.",
                                                   verbose_name="vCores per pCore"),
        ),
    ]
