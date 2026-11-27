"""T36 B5f (owner 2026-11-26 11:29, QB5-4 "ok"): the function claugine gets its own installation, which only prepares
the VM for the portal's installer (deploy/claugine-install.sh); the portal itself is installed by B6 (init, stage 5,
with the licence).

What the installer needs from the VM, beyond base-os (Ubuntu, user portal with passwordless sudo, ufw on with ssh):
curl, tar, sha256sum, python3 (its own check), ca-certificates (HTTPS to cp.example.com), iproute2 (its own
address), openssh-client (the second node of an Enterprise pair), 7zip (a restore); its work folder ~portal/claugine;
and portal's passwordless sudo checked (the installer stops without it). No ports: install.sh and install-lb.sh open
their own (80/443, the status port, the other node's) when ufw is on.

Only when the function has no installation yet (else left and named). New template under a free key (commit-592: free
under the database's case-insensitive collation), bound by id. Reversible: HostService(claugine).legacy["b5f"] (the
template made; commit-593: kept, and named, while another function uses it)."""

from django.db import migrations

INSTALL = """- name: claugine
  packages:
  - ca-certificates
  - curl
  - tar
  - coreutils
  - python3
  - iproute2
  - openssh-client
  - 7zip
  commands:
  - 'install -d -m 0750 -o portal -g portal "$(getent passwd portal | cut -d: -f6)/claugine"'
  - runuser -u portal -- sudo -n true
"""


def _free(CT, base):
    """commit-592: the first free key of base, base-2 …, at most 40 characters. ConfigTemplate.role is unique under
    MySQL's case- and accent-insensitive collation: a key is free only when no key equals it ignoring case and the
    database itself finds no row equal to it (its own collation)."""
    taken = {r.casefold() for r in CT.objects.values_list("role", flat=True)}
    n = 1
    while True:
        tail = "" if n == 1 else f"-{n}"
        key = base[: 40 - len(tail)] + tail
        if key.casefold() not in taken and not CT.objects.filter(role=key).exists():
            return key
        n += 1


def _users(HS, pk):
    """commit-593: the functions (other than claugine) whose installation or a config file is template pk."""
    return [
        h.key
        for h in HS.objects.exclude(key="claugine").order_by("key")
        if h.install_template_id == pk or any(str((c or {}).get("template")) == str(pk) for c in h.configs or [])
    ]


def forward(apps, schema_editor):
    HS = apps.get_model("inventory", "HostService")
    CT = apps.get_model("inventory", "ConfigTemplate")
    h = HS.objects.filter(key="claugine").first()
    if h is None or "b5f" in (h.legacy or {}):
        return
    if h.install_template_id:
        print("\n  inventory 0137: claugine has an installation already - left as it is")
        return
    t = CT.objects.create(
        role=_free(CT, "claugine-install"),
        kind="steps",
        os="ubuntu",
        origin="0137",
        name="Claugine portal: preparing the VM",
        body=INSTALL,
    )
    h.install_template_id = t.pk
    h.legacy = {**(h.legacy or {}), "b5f": {"made": [t.pk]}}
    h.save(update_fields=["install_template", "legacy"])
    print("\n  inventory 0137: claugine - its installation prepares the VM for the portal's installer")


def backward(apps, schema_editor):
    HS = apps.get_model("inventory", "HostService")
    CT = apps.get_model("inventory", "ConfigTemplate")
    h = HS.objects.filter(key="claugine").first()
    legacy = ((h.legacy or {}).get("b5f") if h is not None else None) or {}
    if not legacy:
        return
    made = legacy.get("made") or []
    if h.install_template_id in made:
        h.install_template_id = None
    h.legacy = {k: v for k, v in (h.legacy or {}).items() if k != "b5f"}
    h.save(update_fields=["install_template", "legacy"])
    for t in CT.objects.filter(pk__in=made):
        users = _users(HS, t.pk)
        if users:  # commit-593: kept while another function uses it (PROTECT would stop the reversal)
            print(f"\n  inventory 0137: template {t.role} kept - used by {', '.join(users)}")
        else:
            t.delete()


class Migration(migrations.Migration):
    dependencies = [("inventory", "0136_t36_time_confirmed_by")]

    operations = [migrations.RunPython(forward, backward)]
