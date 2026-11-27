"""T36 B5d (commit-580, owner 2026-11-26 15:22): Host.ip_rev - raised by every address edit in the same write; a time
check confirms a time server only for the address and revision it read before checking (an edit away and back again
in between is not confirmed by the old result)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("inventory", "0134_t36_time_served")]

    operations = [
        migrations.AddField(
            model_name="host",
            name="ip_rev",
            field=models.PositiveIntegerField(default=0, editable=False),
        ),
    ]
