"""T36 B5d (commit-577, owner 2026-11-26 15:44): Host.time_confirmed_by - "peer" when another host of the site got the
time server's answer, "self" when the site had no other host in service and the server asked itself (counted only
while it stays the site's only host). Empty for every host (none is confirmed before 0134's checks)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("inventory", "0135_t36_ip_rev")]

    operations = [
        migrations.AddField(
            model_name="host",
            name="time_confirmed_by",
            field=models.CharField(blank=True, editable=False, max_length=8),
        ),
    ]
