"""T36 B4c commit-540/541 (owner 2026-11-26 07:51): HostsFileLock - one row per platform, locked by every /etc/hosts write and every edit of what the block holds. Reversible."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0127_t36_connect"),
    ]

    operations = [
        migrations.CreateModel(
            name="HostsFileLock",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "platform",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="hosts_file_lock",
                        to="inventory.platform",
                    ),
                ),
            ],
        ),
    ]
