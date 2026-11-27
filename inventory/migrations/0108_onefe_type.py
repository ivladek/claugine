"""T34 B4 (owner 2026-11-21 13:15): the core service type onefe - a platform's OpenNebula front end, an instance of
the service profile onefe. The backup datastore's recipe logs in to its LUN with the instance's own initiator name
(the volume's client IQN in site -> Storage: the storage only presents the LUN to it) - changed only while the part
is still as 0106 seeded it."""

from django.db import migrations, models

SEEDED_LOGIN = (
    "{% for p in storage.portals %}iscsiadm -m discovery -t sendtargets -p {{ p }} >/dev/null; "
    "{% endfor %}iscsiadm -m node -T {{ storage.iqn }} --login || true"
)
INITIATOR = (
    "printf 'InitiatorName=%s\\n' {{ storage.initiator|sh }} > /etc/iscsi/initiatorname.iscsi; systemctl restart iscsid"
)


def initiator(apps, schema_editor):
    HostService = apps.get_model("inventory", "HostService")
    s = HostService.objects.filter(key="backup-ds").first()
    if s is None:
        return
    install = dict(s.install or {})
    cmds = list(install.get("commands") or [])
    if not cmds or cmds[0] != SEEDED_LOGIN:  # changed by a super admin: left as it is
        return
    install["commands"] = [INITIATOR] + cmds
    s.install = install
    s.save(update_fields=["install"])


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0107_services_from_profiles"),
    ]

    operations = [
        migrations.AlterField(
            model_name="coreservice",
            name="stype",
            field=models.CharField(
                choices=[
                    ("bgp-rr", "bgp-rr: route reflectors of a BGP network"),
                    ("cloud-egw", "cloud-egw: internet edge of the core services"),
                    ("cloud-igw", "cloud-igw: gateway of the core services"),
                    ("cloud-gw", "cloud-gw: the core services' egw and igw (two VMs)"),
                    (
                        "portal-gw",
                        "portal-gw: the egws of a portal platform (two VMs, VRRP on the public overlays)",
                    ),
                    ("ns", "ns: name servers"),
                    ("logs", "logs: log collector"),
                    ("admin", "admin: admin jump hosts"),
                    ("backup", "backup: backup storage of a datastore"),
                    ("onefe", "onefe: OpenNebula front end of a platform"),
                    ("core-vm", "core-vm: any other VMs or hosts"),
                ],
                default="core-vm",
                max_length=16,
                verbose_name="service type",
            ),
        ),
        # the database only goes forward (commit-309)
        migrations.RunPython(initiator),
    ]
