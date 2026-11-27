"""Owner 2026-11-23 09:47 (T34 S2d, Q22 A; commit-398): a service profile's NIC rows say what varies per instance -
"storage" is the network of the instance's volume (nfs or iscsi), an optional row is there when the instance has it.
The rows 0114 seeded are rewritten (only where they are still as seeded); the reverse puts them back the same way."""

from django.db import migrations

SEEDED = {
    "onefe": [{"network": "core-cloud"}],
    "logs": [{"network": "core-cloud"}, {"network": "nfs"}],
    "admin": [{"network": "core-cloud"}, {"network": "nfs"}],
    "backup": [{"network": "core-cloud"}, {"network": "iscsi"}],
}
ROWS = {
    "onefe": [{"network": "core-cloud"}, {"network": "nfs", "optional": True}],
    "logs": [{"network": "core-cloud"}, {"network": "storage"}],
    "admin": [{"network": "core-cloud"}, {"network": "storage"}],
    "backup": [{"network": "core-cloud"}, {"network": "storage", "optional": True}],
}


def _swap(apps, old, new):
    ServiceProfile = apps.get_model("inventory", "ServiceProfile")
    for p in ServiceProfile.objects.filter(stype__in=list(old)):
        if (p.nics or []) == old[p.stype]:  # edited rows are the administrator's: left as they are
            p.nics = new[p.stype]
            p.save(update_fields=["nics"])


def forwards(apps, schema_editor):
    _swap(apps, SEEDED, ROWS)


def backwards(apps, schema_editor):
    _swap(apps, ROWS, SEEDED)


class Migration(migrations.Migration):
    dependencies = [("inventory", "0114_profiles_from_templates")]

    operations = [migrations.RunPython(forwards, backwards)]
