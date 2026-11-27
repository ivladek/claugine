"""Owner 2026-11-24 (T33, Profiles → Logging, the vyos row; checked 09:30 on a provider egw, VyOS 2026.03): every VyOS
template with the management part sends its logs with octet-counted framing over TCP (as every Linux machine does)
and rotates /var/log/messages by size - the vyos row's Size / 10 MB a file, 9 old files (nfconfig.syslog_target).
Templates whose syslog lines were edited by hand (the known lines not found) stay as they are (reported); the tenants'
own routers (igw) have no such lines and are not touched."""

from django.db import migrations

OLD = "set system syslog remote {{ syslog.host }} vrf MGMT\n{% endif %}"
NEW = (
    "set system syslog remote {{ syslog.host }} vrf MGMT\n"
    '{% if syslog.proto == "tcp" %}set system syslog remote {{ syslog.host }} format octet-counted\n{% endif %}'
    "set system logs logrotate messages max-size {{ syslog.max_size }}\n"
    "set system logs logrotate messages rotate {{ syslog.rotate }}\n"
    "{% endif %}"
)


def forward(apps, schema_editor):
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for t in ConfigTemplate.objects.filter(os="vyos"):
        if "logrotate messages" in t.body:
            continue
        if OLD in t.body:
            t.body = t.body.replace(OLD, NEW)
            t.save(update_fields=["body"])
        elif "system syslog remote" in t.body:
            print(f"  inventory 0120: {t.role} not changed (its syslog lines were edited by hand)")


def back(apps, schema_editor):
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for t in ConfigTemplate.objects.filter(os="vyos"):
        if NEW in t.body:
            t.body = t.body.replace(NEW, OLD)
            t.save(update_fields=["body"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0119_logged_services")]
    operations = [migrations.RunPython(forward, back)]
