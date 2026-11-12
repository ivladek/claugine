"""0031 multiplied the profiles' boot disk sizes by 2^30/10^9, but they were already in GB (the vendors' sizes, as
the hardware reading now gives them: bytes / 10^9). Back to the stored numbers. Host facts are read again."""
from django.db import migrations

RATIO = 1073741824 / 1000000000


def forward(apps, schema_editor):
    ServerProfile = apps.get_model("inventory", "ServerProfile")
    for p in ServerProfile.objects.all():
        b = p.boot or {}
        if b.get("size_gb"):
            b["size_gb"] = round(int(b["size_gb"]) / RATIO)
            p.boot = b
            p.save(update_fields=["boot"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0038_l2_edge")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
