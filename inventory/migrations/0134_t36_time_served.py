"""T36 B5d (commit-577, owner 2026-11-26 15:05 "fix commit-577"): Host.time_served_at - a host is one of its site's time
servers only once confirmed over ssh to serve time (Deploy's OS baseline, or Check time servers on a mgmt or simple
platform's cluster for the hosts built before). Empty for every host: the existing FE hosts are checked first."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("inventory", "0133_t36_time")]

    operations = [
        migrations.AddField(
            model_name="host",
            name="time_served_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
    ]
