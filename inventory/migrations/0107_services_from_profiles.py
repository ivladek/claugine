"""Owner 2026-11-21 13:10 (T34): "portal i the single source of truth" - no detection: a machine has the services
its profile lists (13:44: one KVM host profile, NVIDIA where its server profile has GPUs). A part carries the logging
services that come with it (includes; the mockup's Includes column): Base OS the system ones every machine had (empty
present_when), Graylog its MongoDB, the OpenNebula front end its MySQL and Prometheus (owner 13:15) …; present_when
goes. The KVM host profile lists what the hosts were found with (T33)."""

from django.db import migrations, models

EVERY = ["systemd", "network", "ssh", "cron", "rsyslog", "time", "updates", "dbus"]
INCLUDES = {
    "base-os": EVERY + ["vm-agents", "multipathd"],
    "graylog": ["mongodb", "rpcbind"],
    "opennebula": ["mysql", "prometheus", "rpcbind"],
    "jump": ["desktop", "auditd", "rpcbind"],
    "backup-ds": ["iscsid"],
}
HOST = EVERY + ["multipathd", "libvirt", "opennebula-node", "frr", "one-exporter", "lldpd", "tuned"]


def seed(apps, schema_editor):
    HostService = apps.get_model("inventory", "HostService")
    ServiceProfile = apps.get_model("inventory", "ServiceProfile")
    keys = set(HostService.objects.values_list("key", flat=True))
    for key, inc in INCLUDES.items():
        HostService.objects.filter(key=key).update(includes=[k for k in inc if k in keys])
    ServiceProfile.objects.filter(stype="host").update(services=[k for k in HOST if k in keys])


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0106_service_profiles"),
    ]

    operations = [
        migrations.AddField(
            model_name="hostservice",
            name="includes",
            field=models.JSONField(blank=True, default=list, help_text="keys of the services that come with it"),
        ),
        # commit-309: the database only goes forward (the way back is the backup taken before an update)
        migrations.RunPython(seed),
        migrations.RemoveField(model_name="hostservice", name="present_when"),
    ]
