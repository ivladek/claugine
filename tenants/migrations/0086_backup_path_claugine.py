# owner 2026-11-08: folders are named "claugine", not "claugine-portal". Only the default for a fresh
# installation changes; existing rows keep whatever folder they were given.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0085_update_stream"),
    ]

    operations = [
        migrations.AlterField(
            model_name="portalsettings",
            name="backup_path",
            field=models.CharField(
                blank=True,
                default="/var/backups/claugine",
                max_length=300,
                verbose_name="local folder",
            ),
        ),
    ]
