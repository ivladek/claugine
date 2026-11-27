"""commit-266: when logging was last applied is its own field; the time kept so far in Host.logging moves to it."""

from django.db import migrations, models
from django.utils.dateparse import parse_datetime


def forward(apps, schema_editor):
    Host = apps.get_model("inventory", "Host")
    for h in Host.objects.exclude(logging={}):
        lg = dict(h.logging or {})
        at = lg.pop("applied_at", None)
        if at is None:
            continue
        Host.objects.filter(pk=h.pk).update(logging=lg, logging_applied_at=parse_datetime(at))


def back(apps, schema_editor):
    Host = apps.get_model("inventory", "Host")
    for h in Host.objects.exclude(logging_applied_at=None):
        lg = dict(h.logging or {})
        lg["applied_at"] = h.logging_applied_at.isoformat(timespec="seconds")
        Host.objects.filter(pk=h.pk).update(logging=lg)


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0100_host_logging"),
    ]

    operations = [
        migrations.AddField(
            model_name="host",
            name="logging_applied_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(forward, back),
    ]
