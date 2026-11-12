from django.db import migrations

LISTEN = "set service ssh listen-address {{ mgmt.ip }}\n"
FRR_OLD = ("  neighbor fabric remote-as {{ ix.asn }}\n  neighbor fabric capability extended-nexthop\n"
           "  neighbor fabric update-source {{ ix.ip }}\n{% if passwords.int %}  neighbor fabric password {{ passwords.int }}\n"
           "{% endif %}!")
FRR_NEW = ("  neighbor fabric remote-as {{ ix.asn }}\n  neighbor fabric password {{ passwords.int }}\n"
           "  neighbor fabric capability extended-nexthop\n  neighbor fabric update-source {{ ix.ip }}\n!")


def update(apps, schema_editor):
    """VyOS 2026.03: ssh in the management VRF listens on all its addresses (no listen-address); the FRR of KVM hosts
    sets the fabric password right after the AS. Only these lines change: other edits of the templates are kept."""
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for t in ConfigTemplate.objects.all():
        body = t.body.replace(LISTEN, "")
        if t.role == "host-frr":
            body = body.replace(FRR_OLD, FRR_NEW)
            if "neighbor fabric password" not in body:
                body = body.replace("  neighbor fabric remote-as {{ ix.asn }}\n",
                                    "  neighbor fabric remote-as {{ ix.asn }}\n  neighbor fabric password {{ passwords.int }}\n")
        fields = {"body": body}
        if t.os == "vyos":
            fields["os_version"] = "2026.03"
        ConfigTemplate.objects.filter(pk=t.pk).update(**fields)


class Migration(migrations.Migration):
    dependencies = [("inventory", "0022_db_inventory")]
    operations = [migrations.RunPython(update, migrations.RunPython.noop)]
