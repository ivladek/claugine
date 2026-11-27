"""Owner 2026-11-21 10:49 "BIND B1 auditd and Graylog too" (T33, proposal-standard-logrotate.md, mockup 12): every
service on standard logrotate. A service's rotation may have a size (rotate.max_mb: then logrotate runs hourly on the
machine and files are kept by age) and a service may have config changes - settings in its own files the portal makes
sure of (HostService.config).
- BIND: rotated by logrotate only, daily and at 100 MB, kept 5 days; named's own "versions N size X" removed from its
  logging channels (named-checkconf, then rndc reconfig).
- auditd: max_log_file_action = ignore (auditd stops rotating), logrotate copy then truncate.
- Graylog server and data node: log4j2's RollingFile made a plain File, logrotate copy then truncate (checked as XML,
  then the services restarted).
Only what the owner has not changed is set (a service whose rotation or config was edited keeps it); the way back
changes no data (as 0102, commit-268)."""

from django.db import migrations, models

BIND_CONFIG = {
    "changes": [{"file": "/etc/bind/named.options", "find": r"\s+versions\s+\d+\s+size\s+\w+;", "replace": ";"}],
    "check": "named-checkconf",
    "then": "reload",
    "command": "rndc reconfig",
}
AUDITD_CONFIG = {
    "changes": [
        {
            "file": "/etc/audit/auditd.conf",
            "find": r"^max_log_file_action\s*=.*$",
            "replace": "max_log_file_action = ignore",
        }
    ],
    "check": "",
    "then": "reload",
    "command": "service auditd reload",
}
LOG4J = [
    (r'<RollingFile (name="rolling-file" fileName="[^"]+")[^>]*>', r'<File \1 append="true">'),
    (r"(?s)\s*<Policies>.*?<DefaultRolloverStrategy[^>]*/>", ""),
    (r"</RollingFile>", "</File>"),
]
GRAYLOG_CONFIG = {
    "changes": [
        {"file": f"/etc/graylog/{part}/log4j2.xml", "find": find, "replace": repl}
        for part in ("server", "datanode")
        for find, repl in LOG4J
    ],
    "check": "python3 -c 'import sys, xml.dom.minidom as m; m.parse(sys.argv[1])' {file}",
    "then": "restart",
    # commit-278: any failed restart fails the command (the restart stays pending on the machine)
    "command": "rc=0; for u in graylog-server graylog-datanode; do if systemctl cat $u >/dev/null 2>&1; "
    "then systemctl restart $u || rc=1; fi; done; exit $rc",
}
OWN_TO_LOGROTATE = {
    "auditd": (["/var/log/audit/audit.log"], AUDITD_CONFIG, "auditd rotates its own log."),
    "graylog": (
        ["/var/log/graylog-server/*.log", "/var/log/graylog-datanode/*.log"],
        GRAYLOG_CONFIG,
        "The log collector itself: it sends to itself. Graylog rotates its own logs.",
    ),
}


def _covers(rotate, paths):
    """commit-284: the service's rotation (after this migration) is logrotate's and names these logs - only then are
    the service's own rotation switched off."""
    r = rotate or {}
    return (r.get("method") or "copytruncate") != "own" and all(p in (r.get("paths") or []) for p in paths)


BIND_LOGS = ["/var/log/bind/*.log"]


def forward(apps, schema_editor):
    HostService = apps.get_model("inventory", "HostService")
    bind = HostService.objects.filter(key="bind").first()
    if bind is not None:
        rotate = dict(bind.rotate or {})
        if rotate.get("paths") and (rotate.get("method") or "copytruncate") != "own" and not rotate.get("max_mb"):
            rotate["max_mb"] = 100
            bind.rotate = rotate
        if not bind.config and _covers(bind.rotate, BIND_LOGS):
            bind.config = BIND_CONFIG
        bind.save(update_fields=["rotate", "config"])
    for key, (paths, config, old_note) in OWN_TO_LOGROTATE.items():
        s = HostService.objects.filter(key=key).first()
        if s is None:
            continue
        rotate = dict(s.rotate or {})
        if rotate.get("method") == "own" and not rotate.get("paths"):
            s.rotate = {"paths": paths, "method": "copytruncate"}
            if s.notes.strip() == old_note:
                s.notes = old_note.replace(
                    "Graylog rotates its own logs.", "Rotated by logrotate (its own rotation off)."
                ).replace("auditd rotates its own log.", "Rotated by logrotate (its own rotation off).")
        if not s.config and _covers(s.rotate, paths):  # commit-284
            s.config = config
        s.save(update_fields=["rotate", "config", "notes"])


def back(apps, schema_editor):
    """Nothing changed back (as 0102, commit-268): the data stays the services' own."""


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0103_host_logging_claim"),
    ]

    operations = [
        migrations.AddField(
            model_name="hostservice",
            name="config",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(forward, back),
    ]
