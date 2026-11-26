"""Owner 2026-11-18 (T25): disk layouts, one per server profile, and the provider's part of a profile (BIOS settings,
BIOS and iLO versions, iLO services).

The first layout, "standard", is the one the hosts have (read 2026-11-18 on three provider hosts:
480 GB NVMe, volume group vg_data): / 50, /var/lib 200, /var/log 100 and /home the rest, all xfs; every
profile gets it. (The profiles' records said / 50, /var 100, /var/log 100, /var/tmp 200, /home the rest: 450 GiB that
a 480 GB device - 447 GiB - cannot hold; the owner decides, job 2026-11-18_host-deployment Q2.) Every profile gets the BIOS settings
decided on 12:58-13:01 and the iLO services SSH on, IPMI over LAN off; its BIOS and iLO versions are the ones most of
its hosts reported at their last reading (the IPMI health), empty when none did."""

from collections import Counter

import django.db.models.deletion
from django.db import migrations, models


STANDARD = [
    {"path": "/", "size_gib": 50, "fs": "xfs"},
    {"path": "/var/lib", "size_gib": 200, "fs": "xfs"},
    {"path": "/var/log", "size_gib": 100, "fs": "xfs"},
    {"path": "/home", "size_gib": None, "fs": "xfs"},
]
BIOS = {  # inventory/profiles.py BIOS_DEFAULTS as of this migration
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


def forward(apps, schema_editor):
    DiskLayout = apps.get_model("inventory", "DiskLayout")
    ServerProfile = apps.get_model("inventory", "ServerProfile")
    Host = apps.get_model("inventory", "Host")
    lay, _c = DiskLayout.objects.get_or_create(
        code="standard", defaults={"title": "The hosts' layout as of 2026-11-18", "volumes": STANDARD}
    )
    for p in ServerProfile.objects.all():
        health = [h.health or {} for h in Host.objects.filter(profile=p).exclude(state="retired")]
        bios = Counter(x.get("bios") for x in health if x.get("bios")).most_common(1)
        ilo = Counter(x.get("ipmi_fw") for x in health if x.get("ipmi_fw")).most_common(1)
        p.layout = p.layout or lay
        p.bios = p.bios or dict(BIOS)
        p.ilo_services = p.ilo_services or {"ssh": True, "ipmi_lan": False}
        p.bios_version = p.bios_version or (bios[0][0] if bios else "")
        p.ilo_version = p.ilo_version or (ilo[0][0] if ilo else "")
        p.save(update_fields=["layout", "bios", "ilo_services", "bios_version", "ilo_version"])


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0093_platform_baseline'),
    ]

    operations = [
        migrations.CreateModel(
            name='DiskLayout',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=32, unique=True)),
                ('title', models.CharField(blank=True, max_length=140)),
                ('volumes', models.JSONField(blank=True, default=list)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['code'],
            },
        ),
        migrations.AddField(
            model_name='serverprofile',
            name='bios',
            field=models.JSONField(blank=True, default=dict, help_text="BIOS attributes set over the server's factory defaults at a deployment"),
        ),
        migrations.AddField(
            model_name='serverprofile',
            name='bios_version',
            field=models.CharField(blank=True, max_length=80, verbose_name='BIOS version'),
        ),
        migrations.AddField(
            model_name='serverprofile',
            name='ilo_services',
            field=models.JSONField(blank=True, default=dict, help_text='{ssh, ipmi_lan}: on or off; SSDP always off'),
        ),
        migrations.AddField(
            model_name='serverprofile',
            name='ilo_version',
            field=models.CharField(blank=True, max_length=80, verbose_name='iLO version'),
        ),
        migrations.AddField(
            model_name='serverprofile',
            name='layout',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='profiles', to='inventory.disklayout', verbose_name='disk layout'),
        ),
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
