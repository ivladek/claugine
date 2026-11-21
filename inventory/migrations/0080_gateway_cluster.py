"""owner 2026-11-09: which cluster carries a project's own provider routers (egw, cgw, project-side l2gw).

Distinct from `kind="edge"`, which is the platform's common network functions. Nothing is marked by this
migration: until staff tick a cluster, placement falls back to where it went before - the project's own cluster.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("inventory", "0079_gpu_bundles")]

    operations = [
        migrations.AddField(
            model_name="cluster", name="hosts_gateways",
            field=models.BooleanField(
                default=False,
                help_text="The project gateways run here - the egw, the cgw and the overlay side of an L2 gateway - wherever the project itself is placed.",
                verbose_name="gateway cluster"),
        ),
    ]
