"""Owner 2026-11-15: "prod must store fe nodes ip". A platform's node IPs, new in v01.00.36, are taken once from the
last collection (adoption.PlatformStats.frontends) where they are still empty, matched by the node's name. Data
only; rolling back leaves the IPs, which the older code ignores."""

from django.db import migrations


def fill_node_ips(apps, schema_editor):
    from inventory.nodeips import fill

    Platform = apps.get_model("inventory", "Platform")
    PlatformStats = apps.get_model("adoption", "PlatformStats")
    stats = {s.platform_key: s.frontends for s in PlatformStats.objects.all()}
    for p in Platform.objects.all():
        primary = p.fe_primary if p.fe_mode == "secondary" and p.fe_primary else p.key
        fe, changed = fill(p.fe, stats.get(p.key) or stats.get(primary) or [])
        if changed:
            p.fe = fe
            p.save(update_fields=["fe"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0086_gpu_clusters_ratio_one"), ("adoption", "0008_platformstats_frontends")]
    operations = [migrations.RunPython(fill_node_ips, migrations.RunPython.noop)]
