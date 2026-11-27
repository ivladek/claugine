"""T36 B5b (rules v2 C2/C4, N4a approved 2026-11-24 20:56): the log collector's one graylog-server step (0129) split
into its three functions, each its own installation - mongodb (its repository, mongodb-org, its NFS mount, mongod),
graylog-datanode (the graylog repository, graylog-datanode, datanode.conf with opensearch_heap 8g, its sysctl and
overrides config files, its mount) and graylog-server (graylog-server and the plugin, server.conf, its JVM in
/etc/default/graylog-server as a config file: -Xms4g -Xmx4g as szk1onelog1, its mount, the firewall). A template
not as 0129 made it (edited since) is left as it is and named. Reversible: HostService(graylog-server).legacy["b5b"]."""

import yaml
from django.db import migrations

DEFAULT = """# claugine: graylog-server's JVM (T36 B5b, N4a: heap 4g, as szk1onelog1)
GRAYLOG_SERVER_JAVA_OPTS="-Xms4g -Xmx4g -server -XX:+UseG1GC -XX:-OmitStackTraceInFastThrow"
GRAYLOG_SERVER_JAVA_OPTS="$GRAYLOG_SERVER_JAVA_OPTS -Djdk.tls.acknowledgeCloseNotify=true"
GRAYLOG_SERVER_JAVA_OPTS="$GRAYLOG_SERVER_JAVA_OPTS -Dlog4j2.formatMsgNoLookups=true"
GRAYLOG_SERVER_ARGS=""
GRAYLOG_COMMAND_WRAPPER=""
"""
DEFAULT_ROLE = "graylog-server-default"
DATANODE_HEAP = "8g"


def _dump(steps):
    return yaml.safe_dump(steps, sort_keys=False, allow_unicode=True, width=4096, default_flow_style=False)


def _pick(rows, test):
    return [x for x in rows or [] if test(x)]


def split(step):
    """[mongodb, graylog-datanode, graylog-server] steps from 0129's graylog-server step, or None when it is not that
    step any more."""
    pk = list(step.get("packages") or [])
    repos = {str((r or {}).get("name")): r for r in step.get("repos") or [] if isinstance(r, dict)}
    files = {str((f or {}).get("path")): f for f in step.get("files") or [] if isinstance(f, dict)}
    mounts = {str((m or {}).get("where")): m for m in step.get("mounts") or [] if isinstance(m, dict)}
    need = (
        {"mongodb-org", "graylog-server", "graylog-datanode"} <= set(pk)
        and {"graylog", "mongodb"} <= set(repos)
        and {"/etc/graylog/server/server.conf", "/etc/graylog/datanode/datanode.conf"} <= set(files)
        and {"/var/lib/mongodb", "/var/lib/graylog-server", "/var/lib/graylog-datanode"} <= set(mounts)
    )
    if not need:
        return None
    nfs = [p for p in pk if p == "nfs-common"]
    datanode = dict(files["/etc/graylog/datanode/datanode.conf"])
    datanode["set"] = {**(datanode.get("set") or {}), "opensearch_heap": DATANODE_HEAP}
    rest = [p for p in pk if p not in ("nfs-common", "mongodb-org", "graylog-server", "graylog-datanode")]
    mongo = {
        "name": "mongodb",
        "repos": [repos["mongodb"]],
        "packages": nfs + ["mongodb-org"],
        "enable": ["mongod"],
        "mounts": [mounts["/var/lib/mongodb"]],
    }
    node = {
        "name": "graylog-datanode",
        "configs": list(step.get("configs") or []),
        "repos": [repos["graylog"]],
        "packages": nfs + ["graylog-datanode"],
        "files": [datanode],
        "enable": ["graylog-datanode"],
        "mounts": [mounts["/var/lib/graylog-datanode"]],
    }
    server = {
        "name": "graylog-server",
        "configs": ["/etc/default/graylog-server"],
        "repos": [repos["graylog"]],
        "packages": nfs + ["graylog-server"] + rest,
        "files": [files["/etc/graylog/server/server.conf"]]
        + [
            f
            for p, f in files.items()
            if p not in ("/etc/graylog/server/server.conf", "/etc/graylog/datanode/datanode.conf")
        ],
        "enable": ["graylog-server"],
        "mounts": [mounts["/var/lib/graylog-server"]]
        + [
            m
            for w, m in mounts.items()
            if w not in ("/var/lib/mongodb", "/var/lib/graylog-server", "/var/lib/graylog-datanode")
        ],
    }
    if step.get("commands"):
        server["commands"] = list(step["commands"])
    for k in ("users", "tools"):
        if step.get(k):
            server[k] = list(step[k])
    return [mongo, node, server]


def _as_0129_made(apps, server, t):
    """commit-562/566: the graylog-server template and config files exactly as 0129 gave them (its record "given": the
    template's id and body and the config files), or None."""
    HS = apps.get_model("inventory", "HostService")
    base = HS.objects.filter(key="base-os").first()
    given = ((((base.legacy or {}).get("b5") or {}).get("given") or {}).get("graylog-server") if base else None) or {}
    if (
        t is None
        or not given
        or given.get("template") != t.pk
        or given.get("body") != t.body
        or given.get("configs") != list(server.configs or [])
    ):
        return None
    return True


def _free(taken, base):
    """commit-559/563/564: the first free key of base, base-2 …, at most 40 characters."""
    n = 1
    while True:
        tail = "" if n == 1 else f"-{n}"
        key = base[: 40 - len(tail)] + tail
        if key not in taken:
            taken.add(key)
            return key
        n += 1


def forward(apps, schema_editor):
    HS = apps.get_model("inventory", "HostService")
    CT = apps.get_model("inventory", "ConfigTemplate")
    rows = {h.key: h for h in HS.objects.filter(key__in=["mongodb", "graylog-server", "graylog-datanode"])}
    server = rows.get("graylog-server")
    if len(rows) < 3 or "b5b" in (server.legacy or {}):
        return
    t = CT.objects.filter(pk=server.install_template_id, kind="steps").first() if server.install_template_id else None
    try:
        data = yaml.safe_load(t.body) if t is not None else None
    except yaml.YAMLError:
        data = None
    steps = split(data[0]) if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict) else None
    if (
        steps is None
        or rows["mongodb"].install_template_id
        or rows["graylog-datanode"].install_template_id
        or _as_0129_made(apps, server, t) is None  # commit-562: an edited template or config list is left as it is
    ):
        print(f"\n  inventory 0130: {t.role if t is not None else 'graylog-server'} is not as 0129 made it - not split")
        return
    legacy = {
        "template": t.pk,  # commit-565: the row this migration changed, restored by its reverse
        "body": t.body,
        "units": {
            k: {"install_template": h.install_template_id, "configs": list(h.configs or [])} for k, h in rows.items()
        },
        "made": [],
    }
    taken = set(CT.objects.values_list("role", flat=True))
    made = {}
    for st, unit in zip(steps, ("mongodb", "graylog-datanode", "graylog-server"), strict=True):
        if unit == "graylog-server":
            t.body = _dump([st])
            t.save(update_fields=["body"])
            continue
        role = _free(taken, f"{unit}-install")  # commit-559/564: a free key
        made[unit] = CT.objects.create(
            role=role,
            kind="steps",
            os="ubuntu",
            origin="0130",
            name=f"{rows[unit].name or unit}: installation",
            body=_dump([st]),
        )
        legacy["made"].append(made[unit].pk)
    default = CT.objects.create(  # commit-563: always its own, under a free key, bound by id
        role=_free(taken, DEFAULT_ROLE),
        kind="config",
        os="ubuntu",
        origin="0130",
        name="Graylog server: /etc/default/graylog-server",
        body=DEFAULT,
    )
    legacy["made"].append(default.pk)
    node = rows["graylog-datanode"]
    node.install_template_id, node.configs = made["graylog-datanode"].pk, list(server.configs or [])
    node.save(update_fields=["install_template", "configs"])
    mongo = rows["mongodb"]
    mongo.install_template_id = made["mongodb"].pk
    mongo.save(update_fields=["install_template"])
    server.configs = [  # owner 2026-11-26 12:09: the template by its id
        {
            "path": "/etc/default/graylog-server",
            "template": default.pk,
            "owner": "root:root",
            "mode": "0644",
            "then": "restart graylog-server",
        }
    ]
    server.legacy = {**(server.legacy or {}), "b5b": legacy}
    server.save(update_fields=["configs", "legacy"])
    print(
        f"\n  inventory 0130: graylog split into {', '.join(x.role for x in made.values())} and {t.role} "
        f"(opensearch_heap {DATANODE_HEAP})"
    )


def backward(apps, schema_editor):
    HS = apps.get_model("inventory", "HostService")
    CT = apps.get_model("inventory", "ConfigTemplate")
    server = HS.objects.filter(key="graylog-server").first()
    legacy = ((server.legacy or {}).get("b5b") if server is not None else None) or {}
    if not legacy:
        return
    if legacy.get("template"):  # commit-565: the row it changed, whatever the function names now
        CT.objects.filter(pk=legacy["template"]).update(body=legacy["body"])
    for key, v in (legacy.get("units") or {}).items():
        HS.objects.filter(key=key).update(install_template_id=v.get("install_template"), configs=v.get("configs") or [])
    CT.objects.filter(pk__in=legacy.get("made") or []).delete()
    server = HS.objects.get(pk=server.pk)
    server.legacy = {k: v for k, v in (server.legacy or {}).items() if k != "b5b"}
    server.save(update_fields=["legacy"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0129_t36_function_install")]

    operations = [migrations.RunPython(forward, backward)]
