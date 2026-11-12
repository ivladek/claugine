"""Correction 10 (2026-10-28): the site's log collectors and ssh ports become lists with required lines.
Log collectors: "internal" becomes "core" (Core services); every entry keeps its purpose label (the optional ones, e.g.
"public" → Public services). SSH ports: the RDP port goes; vyos (VyOS VMs) and ssh (Core services) stay, other lines
are kept in management["ports_extra"]. One green-field DNS server."""
from django.db import migrations

LABELS = {"ipmi": "IPMI", "vyos": "VyOS VMs", "core": "Core services", "public": "Public services"}


def forward(apps, schema_editor):
    Site = apps.get_model("inventory", "Site")
    for s in Site.objects.all():
        logs = {}
        for k, c in (s.log_collectors or {}).items():
            k = "core" if k == "internal" else k
            logs[k] = {**c, "label": c.get("label") or LABELS.get(k, k)}
        m = dict(s.management or {})
        ports = {k: v for k, v in (m.get("ports") or {}).items() if k != "rdp"}
        m["ports"] = ports
        m.setdefault("ports_extra", [])
        s.log_collectors, s.management, s.bootstrap_dns = logs, m, list(s.bootstrap_dns or [])[:1]
        s.save(update_fields=["log_collectors", "management", "bootstrap_dns"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0064_site_time_zone")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
