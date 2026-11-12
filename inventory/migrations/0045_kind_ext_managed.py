"""The platform and cluster kind "portal" is called "ext-managed": the platform is managed by Claugine, its payload by a
third-party system."""
from django.db import migrations


def forward(apps, schema_editor):
    for model in ("Platform", "Cluster"):
        apps.get_model("inventory", model).objects.filter(kind="portal").update(kind="ext-managed")


def backward(apps, schema_editor):
    for model in ("Platform", "Cluster"):
        apps.get_model("inventory", model).objects.filter(kind="ext-managed").update(kind="portal")


class Migration(migrations.Migration):
    dependencies = [("inventory", "0044_platform_public_endpoints")]
    operations = [migrations.RunPython(forward, backward)]
