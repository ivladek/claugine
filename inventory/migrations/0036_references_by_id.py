"""Every reference to a secret goes by its id (secret:<id>[#slot]), so a secret can be renamed. The old system's key
and the portal's own key are chosen in Settings (source_secret, service_secret) instead of found by name."""
import re

from django.db import migrations

NAME_RE = re.compile(r"^secret:(user|bgp|opennebula|key):([^#\s]+)(?:#(rrs|igw|egw))?$")


def forward(apps, schema_editor):
    Secret = apps.get_model("inventory", "Secret")
    Platform = apps.get_model("inventory", "Platform")
    Host = apps.get_model("inventory", "Host")
    Site = apps.get_model("inventory", "Site")
    PeerGroup = apps.get_model("inventory", "PeerGroup")
    PortalSettings = apps.get_model("tenants", "PortalSettings")
    ids = {(s.kind, s.name): s.pk for s in Secret.objects.all()}

    def by_id(ref):
        m = NAME_RE.match(ref or "")
        if not m or (m.group(1), m.group(2)) not in ids:
            return ref
        return f"secret:{ids[(m.group(1), m.group(2))]}" + (f"#{m.group(3)}" if m.group(3) else "")

    for p in Platform.objects.all():
        p.fe_secrets, p.sa = by_id(p.fe_secrets), by_id(p.sa)
        p.save(update_fields=["fe_secrets", "sa"])
    for h in Host.objects.exclude(ipmi_user=""):
        h.ipmi_user = by_id(h.ipmi_user)
        h.save(update_fields=["ipmi_user"])
    for s in Site.objects.all():
        j, i = dict(s.jump or {}), dict(s.ipmi or {})
        if j.get("account"):
            j["account"] = by_id(j["account"])
        if i.get("user"):
            i["user"] = by_id(i["user"])
        s.jump, s.ipmi = j, i
        s.save(update_fields=["jump", "ipmi"])
    for g in PeerGroup.objects.exclude(password=""):
        g.password = by_id(g.password)
        g.save(update_fields=["password"])
    for ps in PortalSettings.objects.all():
        ps.backup_account = by_id(ps.backup_account)
        if not ps.source_secret and ("user", "claugine.source.ssh") in ids:
            ps.source_secret = f"secret:{ids[('user', 'claugine.source.ssh')]}"
        if not ps.service_secret and ("user", "claugine.service") in ids:
            ps.service_secret = f"secret:{ids[('user', 'claugine.service')]}"
        ps.save(update_fields=["backup_account", "source_secret", "service_secret"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0035_service_images"), ("tenants", "0043_source_and_service_secret")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
