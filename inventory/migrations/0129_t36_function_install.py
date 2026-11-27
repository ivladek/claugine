"""T36 B5a (owner 2026-11-26 11:29, QB5-1 "drop it"): a base-os VM is installed from its units - base-os, then its
service's functions in role order -, each from its own installation template and config files (HostService
install_template - a template by id - and configs, each {"path", "template": its id, …}; owner 2026-11-26 12:09: every
reference is the object's id); a service profile has neither any more (its install_template stays a VyOS profile's
VyOS template only; its configs column is emptied - kept for the earlier migrations' reversals).

The steps of each base-os profile's installation template (named after their functions since 0122) become the
templates <unit>-install, one per function (and base-os); each config file goes to the unit whose step writes it (a
config no step names: the profile's last unit). The machines' scripts stay the same. commit-558: a step named after a
function that is not one of its profile's (its roles' functions, or its services list), or steps in another order than
its functions, stops the migration before anything is changed, saying what to change; a step of no function stays
with the step before it. commit-559: every new template key is chosen first - free, at most 40 characters
(<unit>-install, -install-2 …). commit-567: where two profiles carry different text for one function's step or
config, it stops (one function, one installation). commit-575: the checks run first, before the columns are added -
on MySQL a schema change is not undone by a stop, and the corrected run must find none. The profiles' old installation templates are
deleted (admin-install and ns-install are reused as the admin and ns functions' names). Reversible:
HostService(base-os).legacy["b5"] keeps what was there, the old templates with their own ids."""

import re

from django.core.exceptions import ImproperlyConfigured
from django.db import migrations, models

STEP = re.compile(r"(?m)^(?=- name: )")
NAME = re.compile(r"^- name: *['\"]?([^'\"\s]+)")
BASE_OS = "base-os"
KEY_MAX = 40


def _chunks(body):
    """[(name, text)] - each top-level step of a steps template, its text as written."""
    out = []
    for c in STEP.split(body or ""):
        if not c.strip():
            continue
        m = NAME.match(c)
        out.append((m.group(1) if m else "", c))
    if out and out[0][0] == "" and len(out) > 1:  # text before the first step (a comment): with the first step
        out[1] = (out[1][0], out[0][1] + out[1][1])
        out = out[1:]
    return out


def _step_configs(text):
    import yaml

    try:
        data = yaml.safe_load(text) or []
    except yaml.YAMLError:
        return []
    paths = []
    for st in data if isinstance(data, list) else []:
        for p in (st or {}).get("configs") or [] if isinstance(st, dict) else []:
            paths.append(str(p))
    return paths


def _units_of(p, steps, rows):
    """The profile's unit keys as ServiceProfile.units() reads them (functions by key until 0131): its roles'
    functions; a profile whose roles are to fill - its installation's steps that are functions (its services list may
    hold names 0122 renamed in the steps only)."""
    fns = [k for r in p.roles or [] if isinstance(r, dict) for k in r.get("functions") or []]
    if not p.roles:
        fns = [n for n in steps if n in rows and n != BASE_OS]
    out = [BASE_OS]
    for k in fns:
        if k not in out:
            out.append(k)
    return out


def _key(unit, taken):
    """commit-559: the first free key of <unit>-install, <unit>-install-2 …, at most 40 characters."""
    n = 1
    while True:
        tail = "-install" + (f"-{n}" if n > 1 else "")
        key = unit[: KEY_MAX - len(tail)] + tail
        if key not in taken:
            taken.add(key)
            return key
        n += 1


def check(apps, schema_editor):
    """commit-575: the checks alone, before any schema change (reads only what 0128 has)."""
    forward(apps, schema_editor, check_only=True)


def forward(apps, schema_editor, check_only=False):
    SP = apps.get_model("inventory", "ServiceProfile")
    HS = apps.get_model("inventory", "HostService")
    CT = apps.get_model("inventory", "ConfigTemplate")
    rows = {h.key: h for h in HS.objects.all()}
    base = rows.get(BASE_OS)
    if base is None:
        print("\n  inventory 0129: no base-os row - nothing moved")
        return
    if "b5" in (base.legacy or {}):  # done already: its record of what was there is never overwritten
        return
    ids = dict(CT.objects.values_list("role", "pk"))
    legacy = {"templates": [], "profiles": {}, "made": [], "units": {}}
    texts, configs, notes, old_roles, stop, services, gains = {}, {}, [], set(), [], {}, []
    for p in SP.objects.filter(os="base-os").exclude(stype="host").order_by("pk"):
        legacy["profiles"][p.key] = {
            "install_template": p.install_template,
            "configs": list(p.configs or []),
            "services": list(p.services or []),
        }
        t = CT.objects.filter(role=p.install_template).first() if p.install_template else None
        if p.install_template and t is None:
            notes.append(f"{p.key}: its installation {p.install_template} does not exist")
        units = _units_of(p, [n for n, _x in _chunks(t.body)] if t is not None and t.kind == "steps" else [], rows)
        if not p.roles and units[1:] and units != list(p.services or []):
            services[p.pk] = units  # roles to fill: what ServiceProfile.units() reads - written after the checks
        role_fns = [set(r.get("functions") or []) for r in p.roles or [] if isinstance(r, dict)]
        role_lists = [list(r.get("functions") or []) for r in p.roles or [] if isinstance(r, dict)]
        chunks = []
        if t is not None and t.kind == "steps":
            old_roles.add(t.role)
            for name, text in _chunks(t.body):
                if name in units:
                    chunks.append((name, text))
                elif name in rows:  # commit-558: a function this profile does not have - it would never be installed
                    stop.append(
                        f"{p.key}: its installation {t.role} has a step {name}, a function its roles do not have "
                        f"({', '.join(units)}) - add {name} to its roles or rename the step"
                    )
                elif chunks:  # a step of no function: stays where it was, with the step before it
                    notes.append(f"{p.key}: step {name or '?'} is no function - kept with {chunks[-1][0]}")
                    chunks[-1] = (chunks[-1][0], chunks[-1][1] + text)
                else:
                    notes.append(f"{p.key}: step {name or '?'} is no function - kept with base-os")
                    chunks.append((BASE_OS, text))
            seen = []
            for name, _text in chunks:
                if not seen or seen[-1] != name:
                    seen.append(name)
            if len(seen) != len(set(seen)) or seen != [u for u in units if u in seen]:  # commit-558: the order
                stop.append(
                    f"{p.key}: the steps of {t.role} run {', '.join(seen)}, its functions in role order are "
                    f"{', '.join(units)} - order its roles' functions as the steps (or the steps as them)"
                )
            for fns in role_lists:  # commit-558: each role installs its functions in its own order - the steps' order
                ran = [n for n in seen if n in fns]
                if ran != [f for f in fns if f in ran]:
                    stop.append(
                        f"{p.key}: the steps of {t.role} run {', '.join(ran)}, a role lists them "
                        f"{', '.join(f for f in fns if f in ran)} - order that role's functions as the steps"
                    )
            if len(role_fns) > 1:  # commit-558: every VM ran every step - each now runs its own role's functions
                lost = [n for n in seen if n != BASE_OS and not all(n in fns for fns in role_fns)]
                if lost:
                    stop.append(
                        f"{p.key}: its VMs of every role ran the steps {', '.join(lost)}, which not every role has - "
                        f"give those functions to every role (or make the profile one role)"
                    )
        elif t is not None:  # commit-567: a script - its function's (roles, else the services list), never base-os
            old_roles.add(t.role)
            fns = [k for r in p.roles or [] if isinstance(r, dict) for k in r.get("functions") or []] or [
                k for k in p.services or [] if k != BASE_OS
            ]
            unit = next((k for k in fns if k in rows and k != BASE_OS), None)
            if unit is None:
                stop.append(
                    f"{p.key}: its installation {t.role} is a script and the profile has no function to give it to - "
                    f"give its roles their functions (Profiles → Services), or make the installation steps"
                )
            elif len(role_fns) > 1 and not all(unit in f for f in role_fns):
                stop.append(
                    f"{p.key}: its installation {t.role} is a script every VM ran, {unit} is not in every role - "
                    f"give {unit} to every role (or make the profile one role)"
                )
            else:
                notes.append(f"{p.key}: its installation is a script - given to {unit}")
                chunks.append((unit, None, t))
        mine = {}
        for c in chunks:
            mine.setdefault(c[0], []).append(c)
        for unit, cs in mine.items():
            want = (
                ("script", cs[0][2].body) if len(cs) == 1 and len(cs[0]) == 3 else ("steps", "".join(c[1] for c in cs))
            )
            if unit not in texts:
                texts[unit] = (want, p.key)
            elif texts[unit][0] != want:  # commit-567: one function, one installation - never another's
                stop.append(
                    f"{p.key}: its {unit} installation differs from {texts[unit][1]}'s - make them the same, or give "
                    f"one of them another function"
                )
        # commit-567: what this profile's VMs ran - each unit it had a step (or the script) for, its config files; a
        # profile without any installation was never deployable (Redeploy refused it): nothing it ran can change
        if t is not None or p.configs:
            gains.append(
                (
                    p.key,
                    # commit-567: what it will install from - its roles, else its services list as kept or rewritten
                    role_lists or [[k for k in (services.get(p.pk) or list(p.services or [])) if k != BASE_OS]],
                    set(mine),
                    {str(c.get("path")) for c in p.configs or [] if isinstance(c, dict)},
                )
            )
        named = {}
        for unit, cs in mine.items():
            for c in cs:
                if c[1] is not None:
                    for path in _step_configs(c[1]):
                        named.setdefault(path, unit)
        last = list(mine)[-1] if mine else BASE_OS
        for c in p.configs or []:
            if not isinstance(c, dict):
                continue
            unit = named.get(c.get("path"), last)
            row = {**c, "template": ids.get(str(c.get("template") or ""))}  # owner 12:09: the template by id
            if row["template"] is None:
                notes.append(f"{p.key}: its config {c.get('path')} names no template that exists ({c.get('template')})")
            have = configs.setdefault(unit, [])
            same = next((x for x in have if x.get("path") == row.get("path")), None)
            if same is None:
                have.append(row)
            elif same != row:  # commit-567: nor another profile's config file
                stop.append(
                    f"{p.key}: its config {c.get('path')} differs from the one {unit} has from another profile - make "
                    f"them the same, or give one of them another function"
                )
    for key, roles, had, paths in gains:  # commit-567: no profile's VMs gain what their installation did not have
        for fns in roles:
            for u in fns:
                if u == BASE_OS:  # base-os is every base-os VM's (QB5-1)
                    continue
                if u in texts and u not in had:
                    stop.append(
                        f"{key}: its VMs would now run {u}'s installation (from {texts[u][1]}), which their "
                        f"installation did not have - give {key} its own {u} step, or take {u} out of its roles"
                    )
                new = [c.get("path") for c in configs.get(u, []) if c.get("path") not in paths]
                if new:
                    stop.append(
                        f"{key}: its VMs would now get {', '.join(new)} (config files of {u}), which their "
                        f"installation did not write - add them to {key}'s configs, or take {u} out of its roles"
                    )
    if stop:  # commit-558/567: nothing changed
        raise ImproperlyConfigured(
            "inventory 0129 cannot move the installations to the functions:\n  "
            + "\n  ".join(stop)
            + "\nChange it in Profiles → Services / Templates (the version before this update), then update again."
        )
    if check_only:
        return
    for pk, units in services.items():  # after the checks: a stop changes nothing
        SP.objects.filter(pk=pk).update(services=units)
    taken = set(ids) - old_roles  # commit-559: every new key chosen before anything is deleted
    keys = {unit: _key(unit, taken) for unit in texts}
    for t in CT.objects.filter(role__in=old_roles).order_by("pk"):
        legacy["templates"].append(
            {
                f: getattr(t, f)
                for f in ("id", "role", "kind", "name", "os", "os_version", "origin", "description", "body")
            }
        )
    CT.objects.filter(role__in=old_roles).delete()
    for unit in set(texts) | set(configs):
        h = rows[unit]
        legacy["units"][unit] = {"install_template": h.install_template_id, "configs": list(h.configs or [])}
        if unit in texts:
            (kind, body), _first = texts[unit]
            if keys[unit] != f"{unit}-install":
                notes.append(f"{unit}: its installation is {keys[unit]} ({unit}-install is taken or too long)")
            made = CT.objects.create(
                role=keys[unit],
                kind=kind,
                name=f"{h.name or unit}: installation"[:100],
                os="ubuntu",
                origin="0129",
                body=body,
            )
            legacy["made"].append(made.pk)
            h.install_template_id = made.pk
        h.configs = configs.get(unit, [])
        h.save(update_fields=["install_template", "configs"])
        # commit-566: exactly what it gave the unit, for the migrations after it to tell an edit apart
        legacy.setdefault("given", {})[unit] = {
            "template": h.install_template_id if unit in texts else None,
            "body": texts[unit][0][1] if unit in texts else None,
            "configs": list(h.configs),
        }
    SP.objects.filter(os="base-os").exclude(stype="host").update(install_template="", configs=[])
    base = HS.objects.get(pk=base.pk)
    base.legacy = {**(base.legacy or {}), "b5": legacy}
    base.save(update_fields=["legacy"])
    print(
        f"\n  inventory 0129: installations of {', '.join(sorted(legacy['units'])) or 'nothing'} from "
        f"{', '.join(sorted(old_roles)) or 'nothing'}" + "".join(f"\n  inventory 0129: {n}" for n in notes)
    )


def backward(apps, schema_editor):
    SP = apps.get_model("inventory", "ServiceProfile")
    HS = apps.get_model("inventory", "HostService")
    CT = apps.get_model("inventory", "ConfigTemplate")
    base = HS.objects.filter(key=BASE_OS).first()
    legacy = ((base.legacy or {}).get("b5") if base is not None else None) or {}
    if not legacy:
        return
    for key, v in (legacy.get("units") or {}).items():
        HS.objects.filter(key=key).update(install_template_id=v.get("install_template"), configs=v.get("configs") or [])
    CT.objects.filter(pk__in=legacy.get("made") or []).delete()
    for t in legacy.get("templates") or []:  # with their own ids: 0114's reverse finds its templates by id
        if CT.objects.filter(role=t["role"]).exists():
            continue
        if CT.objects.filter(pk=t.get("id")).exists():
            t = {k: v for k, v in t.items() if k != "id"}
        CT.objects.create(**t)
    for key, v in (legacy.get("profiles") or {}).items():
        SP.objects.filter(key=key).update(
            install_template=v.get("install_template") or "",
            configs=v.get("configs") or [],
            **({"services": v["services"]} if "services" in v else {}),
        )
    base = HS.objects.get(pk=base.pk)
    base.legacy = {k: v for k, v in (base.legacy or {}).items() if k != "b5"}
    base.save(update_fields=["legacy"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0128_t36_hosts_file_lock")]

    operations = [
        migrations.RunPython(check, migrations.RunPython.noop),  # commit-575: a stop before any column is added
        migrations.AddField(
            model_name="hostservice",
            name="install_template",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.PROTECT,
                related_name="installs",
                to="inventory.configtemplate",
                verbose_name="installation",
            ),
        ),
        migrations.AddField(
            model_name="hostservice",
            name="configs",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.RunPython(forward, backward),
    ]
