"""T36 B5d (owner 2026-11-26 11:29, QB5-2): the installation's external NTP servers, which every site's time servers
(the FE hosts of its mgmt or simple platform) sync with (Settings → Customer). Empty until set."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tenants", "0110_t36_settings")]

    operations = [
        migrations.AddField(
            model_name="portalsettings",
            name="ntp_external",
            field=models.JSONField(blank=True, default=list, verbose_name="external NTP servers"),
        ),
    ]
