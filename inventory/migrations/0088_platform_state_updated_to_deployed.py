"""Owner 2026-11-15: "all platfroms in deployed state - why i see updated ?". The portal no longer marks a deployed
platform "updated" when its clusters, datastores or networks change in Infrastructure (nothing ever set it back);
the platforms marked so are deployed again. Data only; rolling back leaves them deployed."""

from django.db import migrations


def updated_to_deployed(apps, schema_editor):
    apps.get_model("inventory", "Platform").objects.filter(state="updated").update(state="deployed")


class Migration(migrations.Migration):
    dependencies = [("inventory", "0087_platform_node_ips_from_collection")]
    operations = [migrations.RunPython(updated_to_deployed, migrations.RunPython.noop)]
