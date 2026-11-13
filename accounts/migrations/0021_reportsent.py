# owner 2026-11-13 (job 2026-11-09_resource-dashboard): the sent history of the e-mail reports

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0020_city_as_text"),
    ]

    operations = [
        migrations.CreateModel(
            name="ReportSent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("report", models.CharField(db_index=True, max_length=32)),
                ("name", models.CharField(blank=True, max_length=80)),
                ("at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("how", models.CharField(default="schedule", max_length=16)),
                ("to", models.TextField(blank=True)),
                ("sent", models.PositiveIntegerField(default=0)),
                ("ok", models.BooleanField(default=True)),
                ("detail", models.CharField(blank=True, max_length=500)),
                (
                    "by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-at"]},
        ),
    ]
