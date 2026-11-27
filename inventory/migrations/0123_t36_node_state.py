"""T36 B3a (owner 2026-11-25, states QS): the state of each VM of a site service. A VM of a planned or retired service
takes its service's state; every other existing VM is non-compliant until a Redeploy makes it again from its service
(0122 marked the services so; rules v2 C8). Reversible: the field goes."""

from django.db import migrations, models

STATES = [("planned", "planned"), ("deployed", "deployed"), ("non-compliant", "non-compliant"), ("retired", "retired")]


def forward(apps, schema_editor):
    Node = apps.get_model("inventory", "CoreServiceNode")
    for st in ("planned", "retired"):
        Node.objects.filter(service__state=st).update(state=st)
    Node.objects.exclude(service__state__in=("planned", "retired")).update(state="non-compliant")


class Migration(migrations.Migration):
    dependencies = [("inventory", "0122_t36_data")]

    operations = [
        migrations.AddField(
            model_name="coreservicenode",
            name="state",
            field=models.CharField(choices=STATES, default="deployed", max_length=16, verbose_name="state"),
        ),
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
