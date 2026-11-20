"""Owner 2026-11-19 (T25): the hosts' domain becomes Settings -> Customer -> Internal domain."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0104_host_deployment"),
    ]

    operations = [
        migrations.RenameField(
            model_name="portalsettings",
            old_name="hosts_domain",
            new_name="internal_domain",
        ),
        migrations.AlterField(
            model_name="portalsettings",
            name="internal_domain",
            field=models.CharField(blank=True, default="", max_length=100, verbose_name="internal domain"),
        ),
    ]
