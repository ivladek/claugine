"""T36 B4b (owner 2026-11-25 19:25, QB4-1): a host run of kind "connect" (inventory/hostdeploy.py). Reversible."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0126_t36_host_service"),
    ]

    operations = [
        migrations.AlterField(
            model_name="hostdeployment",
            name="kind",
            field=models.CharField(
                choices=[("deploy", "deployment"), ("retire", "retirement"), ("connect", "connection")],
                default="deploy",
                max_length=8,
            ),
        ),
    ]
