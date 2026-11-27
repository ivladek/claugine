"""Owner 2026-11-22 16:59 / 17:08 / 20:00 (T34 S2): a service profile is built from templates (Profiles → Templates,
scope "service"): one installation (kind steps) and a config template for every whole file. Made from what its
services install today: each service with an install becomes a step named after it (repos, packages, users, ``set``
lines, commands, mounts, units, as before, in the same order); each whole file ("text") becomes a config template
(<service>-<file name>, shared by the profiles with the same service) and a config of the profile, written at its
service's step; the profile's own lines become its last step. The networks of the VMs' NICs from today's core types
(owner Q20, 2026-11-21 19:03).

commit-380/381: a profile writing one file twice (however its path is spelled: /etc//x and /etc/x are one file), or
writing it whole and patching it (set lines), cannot keep its order as templates: the migration stops before it
changes anything and names them; a path with a .. part is listed too. Paths are stored written plainly. commit-382:
the services' install is kept (no longer read; it goes with its field). commit-386: what it changes on a profile is
recorded in ServiceProfile.legacy (the earlier values), and the reverse restores from that record alone; commit-388:
every template it makes has origin "0114", and the reverse deletes those, also of a profile deleted since.
commit-387: the record fields come with 0113, so this conversion is one step alone: refused, it changed nothing and
runs again as it is (on MySQL too). commit-389: a profile converted by an earlier, unreleased form of it keeps its
conversion; the templates its record names are marked as made here. commit-391: a converted profile without that
record (the first forms of 0113) stops it, before any change, with how to recover."""

import posixpath
import re

import yaml
from django.core.exceptions import ImproperlyConfigured
from django.db import migrations, transaction

NICS = {
    "ns": ["core-cloud"],
    "logs": ["core-cloud", "nfs"],
    "onefe": ["core-cloud"],
    "admin": ["core-cloud", "nfs"],
    "backup": ["core-cloud", "iscsi"],
}
INSTALL_KEYS = ("repos", "packages", "files", "enable", "users", "mounts", "commands", "tools")
MADE = "(made by migration 0114, T34 S2)"
ORIGIN = "0114"


class _Literal(str):
    pass


class _Dumper(yaml.SafeDumper):
    pass


_Dumper.add_representer(_Literal, lambda d, v: d.represent_scalar("tag:yaml.org,2002:str", v, style="|"))


def _lit(v):
    if isinstance(v, str) and "\n" in v:
        return _Literal(v)
    if isinstance(v, list):
        return [_lit(x) for x in v]
    if isinstance(v, dict):
        return {k: _lit(x) for k, x in v.items()}
    return v


def _key(*parts, taken):
    base = re.sub(r"[^a-z0-9]+", "-", "-".join(parts).lower()).strip("-")[:40].strip("-") or "template"
    key, n = base, 2
    while key in taken:
        tail = f"-{n}"
        key, n = base[: 40 - len(tail)] + tail, n + 1
    taken.add(key)
    return key


def _norm(path):
    """commit-381: the path written plainly (// and . parts gone, no / at the end); None with a .. part."""
    path = str(path or "")
    if ".." in path.split("/"):
        return None
    return posixpath.normpath(re.sub(r"/+", "/", path)).replace("//", "/")


def _clashes(profile, services):
    """commit-380/381: the files a profile writes more than once, or writes whole and patches, by their plain path;
    a file or mount whose path has a .. part."""
    whole, patched, odd = {}, set(), set()
    for skey in profile.services or []:
        s = services.get(skey)
        inst = (s.install or {}) if s else {}
        for f in inst.get("files") or []:
            n = _norm(f.get("path"))
            if n is None:
                odd.add(f"{f.get('path')} (a .. part)")
            elif "text" in f:
                whole[n] = whole.get(n, 0) + 1
            else:
                patched.add(n)
        odd.update(f"{m.get('where')} (a .. part)" for m in inst.get("mounts") or [] if _norm(m.get("where")) is None)
    return sorted([p for p, n in whole.items() if n > 1 or p in patched] + sorted(odd))


# the VyOS service profiles (0116, T34 S3b) are made from their VyOS template: never converted here (0114 runs before
# they exist; this keeps it so when its functions are run on a later database, as the tests do)
VYOS = ("bgp-rr", "cloud-gw", "portal-gw")


def forwards(apps, schema_editor):
    """commit-387: all or nothing - one transaction, also on MySQL (its DDL is not transactional, its data is)."""
    with transaction.atomic(using=schema_editor.connection.alias if schema_editor else "default"):
        _forwards(apps)


def _forwards(apps):
    HostService = apps.get_model("inventory", "HostService")
    ServiceProfile = apps.get_model("inventory", "ServiceProfile")
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    services = {s.key: s for s in HostService.objects.all()}
    bad, unrecorded = [], []
    for p in ServiceProfile.objects.exclude(stype__in=VYOS):
        if not (p.install_template or p.configs):
            bad += [f"{p.key}: {path}" for path in _clashes(p, services)]
        elif not (p.legacy or {}).get("templates"):
            unrecorded.append(p.key)
    if unrecorded:  # commit-391: converted by an earlier, unreleased form of 0113 - no record to roll back from
        raise ImproperlyConfigured(  # commit-393: the recovery of each earlier form
            "0114: the profiles " + ", ".join(unrecorded) + " were converted by an earlier, unreleased form of "
            "migration 0113 and have no record to roll back from. Converted by 568e5d6: its reverse undoes nothing - "
            "restore the database backup taken before that upgrade, then upgrade again. Converted by bfcdfd3: with "
            "that revision run 'manage.py migrate inventory 0112', then upgrade again"
        )
    if bad:  # checked before anything is changed
        raise ImproperlyConfigured(
            "0114: these files are written more than once, or written whole and patched, in one profile, or have a "
            ".. part - change the services' install so each is written once, plainly, then migrate again: "
            + "; ".join(bad)
        )
    taken = set(ConfigTemplate.objects.values_list("role", flat=True))
    made = {}  # (service, path) -> template: one template per service file, shared by its profiles
    for p in ServiceProfile.objects.exclude(stype__in=VYOS):
        if p.install_template or p.configs:  # already made from templates
            # commit-389: by an earlier, unreleased form of this conversion - the templates its record names are its
            ids = (p.legacy or {}).get("templates") or []
            if ids:
                ConfigTemplate.objects.filter(pk__in=ids, origin="").update(origin=ORIGIN)
            continue
        legacy = {
            "templates": [],
            "own_lines": p.own_lines,
            "nics": p.nics,
            "install_template": p.install_template,
            "configs": p.configs,
        }
        steps, configs = [], []
        for skey in p.services or []:
            s = services.get(skey)
            inst = dict((s.install or {}) if s else {})
            if not any(inst.get(k) for k in INSTALL_KEYS):
                continue
            step = {"name": skey}
            paths = []
            for f in inst.get("files") or []:
                if "text" not in f:
                    continue
                path = _norm(f["path"])
                t = made.get((skey, path))
                if t is None:
                    t = ConfigTemplate.objects.create(
                        role=_key(skey, path.rsplit("/", 1)[-1], taken=taken),
                        kind="config",
                        os="ubuntu",
                        os_version="26.04",
                        name=f"{s.name}: {path}"[:100],
                        description=f"{path} of the service {skey} {MADE}",
                        body=str(f["text"]),
                        origin=ORIGIN,
                    )
                    made[(skey, path)] = t
                if t.pk not in legacy["templates"]:
                    legacy["templates"].append(t.pk)
                cfg = {"path": path, "template": t.role, "owner": f.get("owner") or "root:root"}
                cfg["mode"] = f.get("mode") or "0644"
                if f.get("check"):
                    cfg["check"] = f["check"]
                cfg["then"] = f.get("then") or "nothing"
                configs.append(cfg)
                paths.append(path)
            if paths:
                step["configs"] = paths
            for k in INSTALL_KEYS:
                if k == "files":
                    sets = [dict(f, path=_norm(f["path"])) for f in inst.get("files") or [] if "text" not in f]
                    if sets:
                        step["files"] = _lit(sets)
                elif k == "mounts" and inst.get(k):
                    step[k] = _lit([dict(m, where=_norm(m.get("where"))) for m in inst[k]])
                elif k == "users" and inst.get(k):  # what is not set stays out
                    step[k] = [{a: b for a, b in u.items() if b not in (None, "", [])} for u in inst[k]]
                elif inst.get(k):
                    step[k] = _lit(inst[k])
            steps.append(step)
        if (p.own_lines or "").strip():
            steps.append({"name": "own lines", "commands": [_lit(p.own_lines)]})
        if steps:
            t = ConfigTemplate.objects.create(
                role=_key(p.key, "install", taken=taken),
                kind="steps",
                os="ubuntu",
                os_version="26.04",
                name=f"{p.name}: installation"[:100],
                description=f"The installation of the service profile {p.key}, step by step {MADE}",
                body=yaml.dump(steps, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=4096),
                origin=ORIGIN,
            )
            legacy["templates"].insert(0, t.pk)
            p.install_template = t.role
        p.configs = configs
        if not p.nics:
            p.nics = [{"network": n} for n in NICS.get(p.stype, [])]
        p.own_lines = ""
        p.legacy = legacy
        p.save(update_fields=["install_template", "configs", "nics", "own_lines", "legacy"])


def backwards(apps, schema_editor):
    with transaction.atomic(using=schema_editor.connection.alias if schema_editor else "default"):
        _backwards(apps)


def _backwards(apps):
    """commit-382/386/388: each profile 0114 changed as it was before, from its record (ServiceProfile.legacy) alone;
    the templates it made (origin "0114") deleted, whatever was edited in them since, also those of a profile deleted
    since."""
    ServiceProfile = apps.get_model("inventory", "ServiceProfile")
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for p in ServiceProfile.objects.exclude(legacy={}).exclude(stype__in=VYOS):
        rec = p.legacy or {}
        p.own_lines = rec.get("own_lines") or ""
        p.nics = rec.get("nics") or []
        p.install_template = rec.get("install_template") or ""
        p.configs = rec.get("configs") or []
        p.legacy = {}
        p.save(update_fields=["own_lines", "nics", "install_template", "configs", "legacy"])
    ConfigTemplate.objects.filter(origin=ORIGIN).delete()


class Migration(migrations.Migration):
    dependencies = [("inventory", "0113_profile_records")]

    operations = [migrations.RunPython(forwards, backwards, atomic=True)]  # commit-387: alone, so a refused run is retried as is
