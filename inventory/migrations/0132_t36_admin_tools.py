"""T36 B5c (rules v2 C2/C6/C7; owner 2026-11-24 N3, 2026-11-26 11:29 QB5-5): the admin VM's functions each their own.

- admin: GNOME and Ubuntu's own RDP (gnome-remote-desktop) on Settings → Security's public RDP port (node.rdp_port),
  Chrome, and Firefox from Mozilla's APT repository, pinned above Ubuntu's snap wrapper (snapd is removed on base-os);
  the RDP port is no longer open to all - fail2ban's ssh-to-rdp opens it per address.
- tools: its own installation, the C7 list (opennebula-tools, nfs-common, curl, wget, jq, pwgen, whois,
  inetutils-traceroute, tree, libxml2-utils, dnsutils, pipx; auditd; libreoffice, eog, evince, nautilus,
  gnome-terminal, gnome-text-editor - no yq, xorriso, genisoimage), editable as its template.
- fail2ban: the jump's jails (szk1admin1) - sshd 3/1h → 50d, ssh-to-rdp (a successful ssh login opens the RDP port to
  that address for 10 h; only where the VM has an RDP port), recidive 2/100d → 500d; banaction nftables-allports;
  ignoreip the admin VMs only (site.admins, QB5-5).

The three change together or not at all (commit-569: the RDP port is closed to all only with fail2ban opening it per
address): only when admin and fail2ban are exactly as 0129 gave them (its record, commit-566) and the jail file as
shipped; otherwise all are left and named. recidive never counts ssh-to-rdp's grants (commit-568); ssh-to-rdp ignores
loopback only (commit-571). New templates under free keys, bound
by id. Reversible: HostService(tools).legacy["b5c"] (the rows it changed, by id, and their bodies)."""

from django.db import migrations

ADMIN = """- name: admin firefox
  configs:
  - /etc/apt/preferences.d/mozilla-firefox
  repos:
  - name: mozilla
    key: https://packages.mozilla.org/apt/repo-signing-key.gpg
    line: deb [signed-by=/etc/apt/keyrings/mozilla.gpg] https://packages.mozilla.org/apt mozilla main
- name: admin
  configs:
  - /etc/ssh/sshd_config.d/10-claugine.conf
  repos:
  - name: google-chrome
    key: https://dl.google.com/linux/linux_signing_key.pub
    line: deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome-stable/deb/ stable main
  packages:
  - gdm3
  - gnome-session
  - gnome-shell
  - gnome-system-monitor
  - gnome-control-center
  - xwayland
  - fonts-dejavu
  - fonts-liberation
  - gnome-remote-desktop
  - openssl
  - firefox
  - google-chrome-stable
  enable:
  - gdm3
  - gnome-remote-desktop
  commands:
  - install -d -m 0700 -o gnome-remote-desktop /var/lib/gnome-remote-desktop/.local/share/gnome-remote-desktop
  - test -s /var/lib/gnome-remote-desktop/rdp-tls.key || openssl req -new -newkey rsa:4096 -days 3650 -nodes -x509 -subj /CN={{ node.hostname }} -keyout /var/lib/gnome-remote-desktop/rdp-tls.key -out /var/lib/gnome-remote-desktop/rdp-tls.crt
  - 'chown gnome-remote-desktop: /var/lib/gnome-remote-desktop/rdp-tls.*'
  - grdctl --system rdp set-tls-key /var/lib/gnome-remote-desktop/rdp-tls.key
  - grdctl --system rdp set-tls-cert /var/lib/gnome-remote-desktop/rdp-tls.crt
  - grdctl --system rdp set-port {{ node.rdp_port }}
  - grdctl --system rdp set-credentials portal {{ secret.rdp|sh }}
  - grdctl --system rdp enable
  - systemctl set-default graphical.target
"""
PIN = """# claugine: Firefox from Mozilla's repository, never Ubuntu's snap wrapper (snapd is removed)
Package: *
Pin: origin packages.mozilla.org
Pin-Priority: 1000
"""
TOOLS = """- name: tools
  repos:
  - name: opennebula
    key: https://downloads.opennebula.io/repo/repo2.key
    line: deb [arch=amd64 signed-by=/etc/apt/keyrings/opennebula.gpg] https://downloads.opennebula.io/repo/7.4/Ubuntu/{{ os.version }} stable opennebula
  packages:
  - opennebula-tools
  - nfs-common
  - curl
  - wget
  - jq
  - pwgen
  - whois
  - inetutils-traceroute
  - tree
  - libxml2-utils
  - dnsutils
  - pipx
  - auditd
  - libreoffice
  - eog
  - evince
  - nautilus
  - gnome-terminal
  - gnome-text-editor
  enable:
  - auditd
"""
FAIL2BAN = """- name: fail2ban
  configs:
  - /etc/fail2ban/filter.d/ssh-to-rdp.conf
  - /etc/fail2ban/filter.d/recidive-claugine.conf
  - /etc/fail2ban/action.d/rdp-allow.conf
  - /etc/fail2ban/jail.d/10-claugine.local
  packages:
  - fail2ban
  enable:
  - fail2ban
"""
JAIL_SHIPPED = """[DEFAULT]
bantime  = 1h
findtime = 10m
maxretry = 5
ignoreip = 127.0.0.1/8 {% for n in site.internal %}{{ n }} {% endfor %}

[sshd]
enabled = true
port    = {{ node.ssh_port }}
backend = systemd
"""
JAIL = """# claugine: the jump's jails (rules v2 C6)
[DEFAULT]
banaction = nftables-allports
ignoreip = 127.0.0.1/8 ::1{% for a in site.admins %} {{ a }}{% endfor %}

[sshd]
enabled = true
port = {{ node.ssh_port }}
backend = systemd
maxretry = 3
findtime = 1h
bantime = 50d
{% if node.rdp_port %}
[ssh-to-rdp]
enabled = true
# commit-571: a login from an admin VM opens RDP too - only loopback is ignored here
ignoreip = 127.0.0.1/8 ::1
filter = ssh-to-rdp
backend = systemd
journalmatch = _SYSTEMD_UNIT=ssh.service
maxretry = 1
findtime = 1m
action = rdp-allow
bantime = 10h
{% endif %}
[recidive]
enabled = true
# commit-568: an RDP grant is logged as a ban of ssh-to-rdp - never counted here
filter = recidive-claugine
logpath = /var/log/fail2ban.log
maxretry = 2
findtime = 100d
bantime = 500d
"""
FILTER = """# claugine: a successful ssh login (ssh-to-rdp)
[Definition]
failregex = Accepted .* for .* from <HOST> port .* ssh2
"""
RECIDIVE = """# claugine: recidive, never counting ssh-to-rdp's grants (they are logged as bans) - commit-568
[INCLUDES]
before = recidive.conf

[DEFAULT]
# recidive.conf reads it from [DEFAULT]: its regex skips the bans of these jails
_jailname = (?:recidive|ssh-to-rdp)
"""
ACTION = """# claugine: the RDP port opened to the address that logged in with ssh, closed after bantime (ssh-to-rdp)
[Definition]
actionban = ufw allow from <ip> to any port {{ node.rdp_port }} proto tcp
actionunban = ufw --force delete allow from <ip> to any port {{ node.rdp_port }} proto tcp
"""


def _given(rec, unit, t, h):
    """commit-566: the unit's template and config files exactly as 0129 gave them (its record "given")."""
    g = ((rec.get("given") or {}).get(unit)) or {}
    return (
        bool(g)
        and t is not None
        and g.get("template") == t.pk
        and g.get("body") == t.body
        and g.get("configs") == list(h.configs or [])
    )


def _free(taken, base):
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
    rows = {h.key: h for h in HS.objects.filter(key__in=["base-os", "admin", "fail2ban", "tools"])}
    if len(rows) < 4 or "b5c" in (rows["tools"].legacy or {}):
        return
    rec = (rows["base-os"].legacy or {}).get("b5") or {}
    taken = set(CT.objects.values_list("role", flat=True))
    legacy = {"bodies": {}, "units": {}, "made": []}
    notes = []

    def keep(h):
        legacy["units"].setdefault(h.key, {"install_template": h.install_template_id, "configs": list(h.configs or [])})

    def rewrite(t, body):
        legacy["bodies"].setdefault(str(t.pk), t.body)
        t.body = body
        t.save(update_fields=["body"])

    def new(role, kind, name, body):
        t = CT.objects.create(role=_free(taken, role), kind=kind, os="ubuntu", origin="0132", name=name, body=body)
        legacy["made"].append(t.pk)
        return t

    admin, f2b, tools = rows["admin"], rows["fail2ban"], rows["tools"]
    ta = CT.objects.filter(pk=admin.install_template_id).first()
    tf = CT.objects.filter(pk=f2b.install_template_id).first()
    jail_c = next((c for c in f2b.configs or [] if c.get("path") == "/etc/fail2ban/jail.d/10-claugine.local"), None)
    tj = CT.objects.filter(pk=(jail_c or {}).get("template")).first()
    admin_ok = _given(rec, "admin", ta, admin) and not tools.install_template_id
    f2b_ok = (
        _given(rec, "fail2ban", tf, f2b) and tj is not None and tj.body == JAIL_SHIPPED and len(f2b.configs or []) == 1
    )
    if not (admin_ok and f2b_ok):  # commit-569: RDP's access changes only with both - else nothing changes
        notes.append(
            ("admin's installation is not as 0129 made it (or tools has one)" if not admin_ok else "")
            + ("; " if not admin_ok and not f2b_ok else "")
            + ("fail2ban's installation or jail file is not as shipped" if not f2b_ok else "")
            + " - admin, tools and fail2ban left as they are (the RDP port stays open to all)"
        )
    else:
        keep(admin)
        keep(tools)
        keep(f2b)
        rewrite(ta, ADMIN)
        pin = new("admin-mozilla-firefox", "config", "Admin: /etc/apt/preferences.d/mozilla-firefox", PIN)
        admin.configs = [
            {
                "path": "/etc/apt/preferences.d/mozilla-firefox",
                "template": pin.pk,
                "owner": "root:root",
                "mode": "0644",
                "then": "nothing",
            }
        ] + [c for c in admin.configs or [] if c.get("path") != "/etc/apt/preferences.d/mozilla-firefox"]
        admin.save(update_fields=["configs"])
        tools.install_template_id = new("tools-install", "steps", "Tools: installation", TOOLS).pk
        tools.save(update_fields=["install_template"])
        rewrite(tf, FAIL2BAN)
        rewrite(tj, JAIL)
        flt = new("fail2ban-ssh-to-rdp", "config", "fail2ban: /etc/fail2ban/filter.d/ssh-to-rdp.conf", FILTER)
        rec_f = new("fail2ban-recidive", "config", "fail2ban: /etc/fail2ban/filter.d/recidive-claugine.conf", RECIDIVE)
        act = new("fail2ban-rdp-allow", "config", "fail2ban: /etc/fail2ban/action.d/rdp-allow.conf", ACTION)
        f2b.configs = [
            {
                "path": "/etc/fail2ban/filter.d/ssh-to-rdp.conf",
                "template": flt.pk,
                "owner": "root:root",
                "mode": "0644",
                "then": "restart fail2ban",
            },
            {
                "path": "/etc/fail2ban/filter.d/recidive-claugine.conf",
                "template": rec_f.pk,
                "owner": "root:root",
                "mode": "0644",
                "then": "restart fail2ban",
            },
            {
                "path": "/etc/fail2ban/action.d/rdp-allow.conf",
                "template": act.pk,
                "owner": "root:root",
                "mode": "0644",
                "then": "restart fail2ban",
            },
            jail_c,
        ]
        f2b.save(update_fields=["configs"])
    tools = HS.objects.get(pk=tools.pk)
    tools.legacy = {**(tools.legacy or {}), "b5c": legacy}
    tools.save(update_fields=["legacy"])
    print(
        f"\n  inventory 0132: admin / tools / fail2ban - {len(legacy['made'])} templates made, "
        f"{len(legacy['bodies'])} rewritten" + "".join(f"\n  inventory 0132: {n}" for n in notes)
    )


def backward(apps, schema_editor):
    HS = apps.get_model("inventory", "HostService")
    CT = apps.get_model("inventory", "ConfigTemplate")
    tools = HS.objects.filter(key="tools").first()
    legacy = ((tools.legacy or {}).get("b5c") if tools is not None else None) or {}
    if not legacy:
        return
    for pk, body in (legacy.get("bodies") or {}).items():
        CT.objects.filter(pk=int(pk)).update(body=body)
    for key, v in (legacy.get("units") or {}).items():
        HS.objects.filter(key=key).update(install_template_id=v.get("install_template"), configs=v.get("configs") or [])
    CT.objects.filter(pk__in=legacy.get("made") or []).delete()
    tools = HS.objects.get(pk=tools.pk)
    tools.legacy = {k: v for k, v in (tools.legacy or {}).items() if k != "b5c"}
    tools.save(update_fields=["legacy"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0131_t36_reference_ids")]

    operations = [migrations.RunPython(forward, backward)]
