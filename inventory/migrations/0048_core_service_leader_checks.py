"""Leader-only checks: OpenNebula gate and flow run only on the leader, the front end that holds the floating
address. Services whose checks are still the old standard ones get the new standard ones; edited ones stay as they are."""
from django.db import migrations

OLD = {
    "mgmt-fe": {"units": ["opennebula", "opennebula-fireedge", "mysql"], "ports": [2633, 2616]},
    "shared-fe": {"units": ["opennebula", "opennebula-fireedge", "opennebula-gate", "opennebula-flow", "mysql"],
                  "ports": [2633, 2616, 5030, 2474]},
    "ext-managed-fe": {"units": ["opennebula", "opennebula-fireedge", "mysql"], "ports": [2633, 2616]},
}
NEW = {
    "mgmt-fe": {"units": ["opennebula", "opennebula-fireedge", "mysql"], "ports": [2633, 2616], "vip": "200"},
    "shared-fe": {"units": ["opennebula", "opennebula-fireedge", "mysql"], "ports": [2633, 2616], "vip": "30",
                  "leader_units": ["opennebula-gate", "opennebula-flow"], "leader_ports": [5030, 2474]},
    "ext-managed-fe": {"units": ["opennebula", "opennebula-fireedge", "mysql"], "ports": [2633, 2616], "vip": "60"},
}


def forward(apps, schema_editor):
    CoreService = apps.get_model("inventory", "CoreService")
    for svc in CoreService.objects.filter(name__in=list(OLD)):
        if svc.checks == OLD[svc.name]:
            svc.checks = NEW[svc.name]
            svc.save(update_fields=["checks"])


def backward(apps, schema_editor):
    CoreService = apps.get_model("inventory", "CoreService")
    for svc in CoreService.objects.filter(name__in=list(NEW)):
        if svc.checks == NEW[svc.name]:
            svc.checks = OLD[svc.name]
            svc.save(update_fields=["checks"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0047_core_service_checks")]
    operations = [migrations.RunPython(forward, backward)]
