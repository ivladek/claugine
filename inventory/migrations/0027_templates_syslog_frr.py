from django.db import migrations

OLD = "set vrf name MGMT table 100"
SYSLOG = """set vrf name MGMT table 100
{% if syslog %}set system syslog vrf MGMT
set system syslog remote {{ syslog.host }} facility all level info
set system syslog remote {{ syslog.host }} port {{ syslog.port }}
set system syslog remote {{ syslog.host }} protocol {{ syslog.proto }}
{% endif %}"""


def update(apps, schema_editor):
    """Provider VyOS routers send their logs to the site's VyOS log collector through VRF MGMT; FRR templates are
    for FRR 10.6. Only these lines change: other edits of the templates are kept."""
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for t in ConfigTemplate.objects.all():
        fields = {}
        if t.os == "vyos" and OLD in t.body and "system syslog remote" not in t.body:
            fields["body"] = t.body.replace(OLD, SYSLOG, 1)
        if t.os == "frr":
            fields["os_version"] = "10.6"
        if fields:
            ConfigTemplate.objects.filter(pk=t.pk).update(**fields)


class Migration(migrations.Migration):
    dependencies = [("inventory", "0026_site_log_collectors")]
    operations = [migrations.RunPython(update, migrations.RunPython.noop)]
