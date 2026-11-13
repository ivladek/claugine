"""Boot disks are sized in GB (10^9 bytes, as disk vendors do); the hardware reading gave GiB before. Volumes stay
in GiB."""
from django.db import migrations

RATIO = 1073741824 / 1000000000


def forward(apps, schema_editor):
    ServerProfile = apps.get_model("inventory", "ServerProfile")
    Host = apps.get_model("inventory", "Host")
    for p in ServerProfile.objects.all():
        b = p.boot or {}
        if b.get("size_gb"):
            b["size_gb"] = round(int(b["size_gb"]) * RATIO)
            p.boot = b
            p.save(update_fields=["boot"])
    for h in Host.objects.all():
        b = (h.facts or {}).get("boot") or {}
        if b.get("size_gb"):
            b["size_gb"] = round(int(b["size_gb"]) * RATIO)
            h.facts["boot"] = b
            h.save(update_fields=["facts"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0030_old_system_source_in_db")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
