"""T36 B5d (owner 2026-11-26 11:29, QB5-2): every system of a site syncs with its time servers - the hosts whose host
service has the function time (the FE hosts of its mgmt or simple platform; they run chrony from the installation's
external NTP servers, Settings → Customer).

- base-os (the VMs): a step "base-os time" at the end of its installation - systemd-timesyncd (it replaces chrony, both
  provide time-daemon) with /etc/systemd/timesyncd.conf.d/claugine.conf from a new config template (the site's time
  servers, site.ntp). Only when base-os's installation is of the kind steps, has no such file yet and - commit-579 -
  its text with the step appended reads as its steps plus that one (a flow-style list or an end-of-document marker
  would not); otherwise left and named.
- the sites: their typed NTP list (Site.ntp_servers) is no longer read; a site whose list differs from its time
  servers is named (its systems change to those at their next deployment or configuration).

Reversible: HostService(base-os).legacy["b5d"] (the template's body and the configs before, the template made)."""

import yaml
from django.db import migrations

PATH = "/etc/systemd/timesyncd.conf.d/claugine.conf"
STEP = """- name: base-os time
  configs:
  - /etc/systemd/timesyncd.conf.d/claugine.conf
  packages:
  - systemd-timesyncd
  enable:
  - systemd-timesyncd
"""
TIMESYNCD = """# claugine (T36 B5d): the site's time servers - the FE hosts of its mgmt or simple platform
[Time]
NTP={{ site.ntp|join:" " }}
"""


def _free(taken, base, CT):
    """commit-559/563/564: the first free key of base, base-2 …, at most 40 characters. commit-592 (owner 2026-11-27
    07:35 "fix it for v01.01.63"): ConfigTemplate.role is unique under MySQL's case- and accent-insensitive collation -
    a key is free only when no key equals it ignoring case (taken holds casefolded keys) and the database itself finds
    no row equal to it (its own collation)."""
    n = 1
    while True:
        tail = "" if n == 1 else f"-{n}"
        key = base[: 40 - len(tail)] + tail
        if key.casefold() not in taken and not CT.objects.filter(role=key).exists():
            taken.add(key.casefold())
            return key
        n += 1


def _time_servers(apps):
    """{site code: [addresses]} - each site's hosts whose service has the time function, in service, by name."""
    HS = apps.get_model("inventory", "HostService")
    Host = apps.get_model("inventory", "Host")
    t = HS.objects.filter(key="time").first()
    out = {}
    if t is None:
        return out
    for h in (
        Host.objects.filter(service__isnull=False, state__in=("deployed", "non-compliant"))
        .exclude(ip="")
        .order_by("name")
    ):
        ids = [i for r in (h.service.roles or []) for i in ((r or {}).get("functions") or [])]
        if t.pk in ids:
            out.setdefault(h.site.code, []).append(h.ip)
    return {k: v[:4] for k, v in out.items()}


def _appended(body):
    """commit-579: the body with STEP appended, only when that text reads as the body's own steps plus STEP; else None."""
    try:
        old = yaml.safe_load(body)
        text = body.rstrip("\n") + "\n" + STEP
        new = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(old, list) or new != old + yaml.safe_load(STEP):
        return None
    return text


def forward(apps, schema_editor):
    HS = apps.get_model("inventory", "HostService")
    CT = apps.get_model("inventory", "ConfigTemplate")
    Site = apps.get_model("inventory", "Site")
    notes = []
    base = HS.objects.filter(key="base-os").first()
    if base is not None and "b5d" not in (base.legacy or {}):
        t = CT.objects.filter(pk=base.install_template_id).first() if base.install_template_id else None
        if t is None or t.kind != "steps":
            notes.append("base-os: its installation is not of the kind steps - its time step is left to add by hand")
        elif any((c or {}).get("path") == PATH for c in base.configs or []) or PATH in t.body:
            notes.append(f"base-os: {PATH} is there already - left as it is")
        elif _appended(t.body) is None:
            notes.append(
                "base-os: its steps cannot take a step appended as text - its time step is left to add by hand"
            )
        else:
            taken = {r.casefold() for r in CT.objects.values_list("role", flat=True)}
            ct = CT.objects.create(
                role=_free(taken, "base-os-timesyncd", CT),
                kind="config",
                os="ubuntu",
                origin="0133",
                name=f"base-os: {PATH}",
                body=TIMESYNCD,
            )
            legacy = {"template": t.pk, "body": t.body, "configs": list(base.configs or []), "made": [ct.pk]}
            t.body = _appended(t.body)
            t.save(update_fields=["body"])
            base.configs = list(base.configs or []) + [
                {
                    "path": PATH,
                    "template": ct.pk,
                    "owner": "root:root",
                    "mode": "0644",
                    "then": "restart systemd-timesyncd",
                }
            ]
            base.legacy = {**(base.legacy or {}), "b5d": legacy}
            base.save(update_fields=["configs", "legacy"])
            notes.append("base-os: step base-os time added (systemd-timesyncd with the site's time servers)")
    have = _time_servers(apps)
    for s in Site.objects.order_by("code"):
        typed = [x for x in s.ntp_servers or [] if x]
        now = have.get(s.code, [])
        if typed != now:
            notes.append(
                f"site {s.code}: NTP servers {' '.join(typed) or '-'} are no longer read; its time servers: "
                + (" ".join(now) or "none yet (its mgmt or simple platform's FE hosts)")
                + (" - each once Check time servers sees it serving time (commit-577)" if now else "")
            )
    if notes:
        print("".join(f"\n  inventory 0133: {n}" for n in notes))


def backward(apps, schema_editor):
    HS = apps.get_model("inventory", "HostService")
    CT = apps.get_model("inventory", "ConfigTemplate")
    base = HS.objects.filter(key="base-os").first()
    legacy = ((base.legacy or {}).get("b5d") if base is not None else None) or {}
    if not legacy:
        return
    CT.objects.filter(pk=legacy.get("template")).update(body=legacy.get("body") or "")
    base.configs = legacy.get("configs") or []
    base.legacy = {k: v for k, v in (base.legacy or {}).items() if k != "b5d"}
    base.save(update_fields=["configs", "legacy"])
    CT.objects.filter(pk__in=legacy.get("made") or []).delete()


class Migration(migrations.Migration):
    dependencies = [("inventory", "0132_t36_admin_tools")]

    operations = [migrations.RunPython(forward, backward)]
