# 2026-11-14: the front-end nodes of each platform's zone and their state (Analysis → Summary)

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("adoption", "0007_drop_old_system_import"),
    ]

    operations = [
        migrations.AddField(
            model_name="platformstats",
            name="frontends",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="[{id, name, addr, state}] the zone's front-end nodes and their Raft state (2026-11-14)",
            ),
        ),
    ]
