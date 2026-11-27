from django.db import migrations, models


class Migration(migrations.Migration):
    """T34 B6, commit-350: a front end's Restore all keeps its recovery record on the service."""

    dependencies = [
        ("inventory", "0109_dns"),
    ]

    operations = [
        migrations.AddField(
            model_name="coreservice",
            name="recovery",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
