"""Owner 2026-11-19 17:55 "i approve proposed for colored rows" (job 2026-11-18_host-deployment, the BIOS of
a test host against a reference host): PcieGlobalAspm, NumaGroupSizeOpt and SubNumaClustering come out of the server
profiles - WorkloadProfile Virtualization-MaxPerformance governs them (the BIOS refuses ASPM under it, and it sets the
NUMA settings itself). commit-194: only from a profile whose BIOS settings are still exactly the ones 0094 gave; a
profile the owner edited keeps everything."""

from django.db import migrations

SEED = {  # 0094's BIOS, as it gave it
    "WorkloadProfile": "Virtualization-MaxPerformance",
    "AutoPowerOn": "RestoreLastState",
    "PostF1Prompt": "Disabled",
    "PowerOnDelay": "NoDelay",
    "ExtendedMemTest": "Disabled",
    "MemFastTraining": "Enabled",
    "SubNumaClustering": "Disabled",
    "NumaGroupSizeOpt": "Flat",
    "EmbeddedUefiShell": "Disabled",
    "PcieGlobalAspm": "Disabled",
    "EmbeddedIpxe": "Disabled",
}
REMOVED = ("PcieGlobalAspm", "NumaGroupSizeOpt", "SubNumaClustering")


def forward(apps, schema_editor):
    ServerProfile = apps.get_model("inventory", "ServerProfile")
    for p in ServerProfile.objects.all():
        if dict(p.bios or {}) == SEED:
            p.bios = {k: v for k, v in SEED.items() if k not in REMOVED}
            p.save(update_fields=["bios"])


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0096_network_core_lm"),
    ]

    operations = [
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
