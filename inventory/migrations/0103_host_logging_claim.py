"""commit-263: Apply logging's claim on a host; a deployment is refused while it is fresh."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0102_services_improvements"),
    ]

    operations = [
        migrations.AddField(
            model_name="host",
            name="logging_claim",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
