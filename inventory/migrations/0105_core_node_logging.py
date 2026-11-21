"""Owner 2026-11-21 11:58 "service VMs now" (T33): Check logging / Apply logging and the Logging part for the core
service VMs, as for hosts - the services found, the last check, when applied, Apply's claim."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0104_service_config"),
    ]

    operations = [
        migrations.AddField(
            model_name="coreservicenode",
            name="services",
            field=models.JSONField(blank=True, default=list, help_text="keys of the services found on it"),
        ),
        migrations.AddField(
            model_name="coreservicenode",
            name="services_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="coreservicenode",
            name="logging",
            field=models.JSONField(blank=True, default=dict, help_text="the last logging check: status, files, at"),
        ),
        migrations.AddField(
            model_name="coreservicenode",
            name="logging_applied_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="coreservicenode",
            name="logging_claim",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
