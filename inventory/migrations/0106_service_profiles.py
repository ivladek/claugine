"""Owner 2026-11-21 12:29-13:44 (T34): a service is the whole recipe of what it puts on a machine (HostService.install),
a service profile is what a machine of a kind is made of (ServiceProfile: the services in order, own lines, size,
checks). Seeded from the at1 VMs read on 2026-11-21 (Claude, read only; owner 12:42 "existing VMs are read only by
Claude to build the services' templates and data"). The four empty base-OS templates go (owner 13:10, Q15)."""

from django.db import migrations, models

BIND_CONF = """include "/etc/bind/named.options";

view "internal" {
    match-clients     { localhost; ip-internal-all; };
    recursion         yes;
    allow-query       { localhost; ip-internal-all; };
    allow-recursion   { localhost; ip-internal-all; };
    allow-query-cache { localhost; ip-internal-all; };
    forwarders { {% for f in dns.forwarders %}{{ f }}; {% endfor %}};
    forward    first;
{% if dns.forward_zones %}
    validate-except { {% for z in dns.forward_zones %}"{{ z.name }}"; {% endfor %}};{% for z in dns.forward_zones %}
    zone "{{ z.name }}" {
        type       forward;
        forwarders { {% for s in z.servers %}{{ s }}; {% endfor %}};
        forward    only;
    };{% endfor %}
{% endif %}
    include           "/etc/bind/named.conf.zones";
};

view "public" {
    match-clients     { ip-public-all; };
    recursion         yes;
    allow-query       { ip-public-all; };
    allow-recursion   { ip-public-all; };
    allow-query-cache { ip-public-all; };
    forwarders { {% for f in dns.forwarders %}{{ f }}; {% endfor %}};
    forward    first;
    zone "." {
        type hint;
        file "/usr/share/dns/root.hints";
    };
};
"""

BIND_OPTIONS = """// made by claugine from Infrastructure -> DNS (owner 2026-11-21 13:38: one primary for all sites)
{% for s in dns.sites %}// site {{ s.code }}
acl ip-internal-{{ s.code }} { {% for n in s.internal %}{{ n }}; {% endfor %}};
acl ip-public-{{ s.code }}   { {% for n in s.public %}{{ n }}; {% endfor %}};
acl dns-{{ s.code }}         { {% for n in s.ns %}{{ n }}; {% endfor %}};
primaries ns-{{ s.code }}    { {% for n in s.ns %}{{ n }}; {% endfor %}};
{% endfor %}
acl ip-internal-all { {% for s in dns.sites %}ip-internal-{{ s.code }}; {% endfor %}{% for n in dns.internal_extra %}{{ n }}; {% endfor %}};
acl ip-public-all   { {% for s in dns.sites %}ip-public-{{ s.code }}; {% endfor %}};
acl dns-all         { {% for s in dns.sites %}dns-{{ s.code }}; {% endfor %}{% for n in dns.notify_extra %}{{ n }}; {% endfor %}};
primaries ns-all    { {% for s in dns.sites %}ns-{{ s.code }}; {% endfor %}};
primaries ns-primary { {{ dns.primary }}; };

options {
    directory          "/var/cache/bind";
    listen-on          { any; };
    listen-on-v6       { none; };
    dnssec-validation  auto;
    edns-udp-size      1232;
    max-udp-size       1232;
    minimal-responses  yes;
    empty-zones-enable yes;
    allow-update       { none; };
    allow-transfer     { dns-all; };
    notify             yes;
    also-notify        { ns-all; };
    masterfile-format  text;
};

logging {
  channel default_log  { file "/var/log/bind/default.log" versions 3 size 10m; severity info; print-time yes; print-category yes; print-severity yes; };
  channel queries_log  { file "/var/log/bind/queries.log" versions 3 size 10m; severity info; print-time yes; };
  channel security_log { file "/var/log/bind/security.log" versions 3 size 10m; severity dynamic; print-time yes; };
  category default  { default_log; };
  category queries  { queries_log; };
  category security { security_log; };
};
"""

BIND_ZONES = """{% for z in dns.zones %}zone "{{ z.name }}" {
{% if dns.role == "primary" %}    type primary;
    file "/var/cache/bind/zones/{{ z.name }}.zone";
{% else %}    type secondary;
    primaries { ns-primary; };
    file "/var/cache/bind/data/{{ z.name }}.zone";
{% endif %}};
{% endfor %}"""

FAIL2BAN = """[DEFAULT]
bantime  = 1h
findtime = 10m
maxretry = 5
ignoreip = 127.0.0.1/8 {% for n in site.internal %}{{ n }} {% endfor %}

[sshd]
enabled = true
port    = {{ node.ssh_port }}
backend = systemd
"""

PARTS = {
    "base-os": {
        "name": "Base OS",
        "group": "system",
        "notes": "The base of every base-OS VM (owner 13:15): Ubuntu from Settings / Platforms (26.04), user portal "
        "with the SA key, the common packages, ufw (the core service's firewall). From the at1 VMs, 2026-11-21.",
        "install": {
            "packages": ["haveged", "wget", "curl", "whois", "inetutils-traceroute", "gnupg", "apt-transport-https",
                         "jq", "ufw"],
            "users": [{"name": "portal", "uid": None, "groups": ["sudo"], "keys": ["sa"]}],
            "files": [{"path": "/etc/sudoers.d/90-portal", "owner": "root:root", "mode": "0440",
                       "text": "portal ALL=(ALL) NOPASSWD:ALL\n", "check": "visudo -cf /etc/sudoers.d/90-portal",
                       "then": "nothing"}],
            "enable": ["haveged"],
            "commands": ["timedatectl set-timezone {{ site.timezone }}",
                         "ufw default deny incoming", "ufw allow {{ node.ssh_port }}/tcp", "ufw --force enable"],
        },
    },
    "bind": {
        "install": {
            "packages": ["bind9", "bind9-utils", "bind9-dnsutils", "dns-root-data"],
            "files": [
                {"path": "/etc/bind/named.options", "owner": "root:bind", "mode": "0644", "text": BIND_OPTIONS,
                 "then": "nothing"},
                {"path": "/etc/bind/named.conf.zones", "owner": "root:bind", "mode": "0644", "text": BIND_ZONES,
                 "then": "nothing"},
                {"path": "/etc/bind/named.conf", "owner": "root:bind", "mode": "0644", "text": BIND_CONF,
                 "check": "named-checkconf", "then": "reload named"},
            ],
            "enable": ["named"],
            "commands": [
                "install -d -o bind -g bind /var/log/bind /var/cache/bind/data /var/cache/bind/zones",
                # commit-301: the primary's zone files from the portal's records (Infrastructure -> DNS), before named
                # starts; checked with the zones loaded
                "{% if dns.role == 'primary' %}{% for z in dns.zones %}{% if z.text %}printf '%s\\n' {{ z.text|sh }} "
                "| install -m 0644 -o root -g bind /dev/stdin /var/cache/bind/zones/{{ z.name }}.zone\n"
                "{% else %}echo 'zone {{ z.name }} has no records in the portal (Infrastructure -> DNS)' >&2; exit 1\n"
                "{% endif %}{% endfor %}named-checkconf -z{% else %}named-checkconf{% endif %}",
                "ufw allow 53",
            ],
        },
    },
    "graylog": {
        "notes": "Graylog with MongoDB, its database (owner 13:15: one service). The data on the site's NFS share "
        "(site -> Storage): server, data node and MongoDB.",
        "install": {
            "repos": [
                {"name": "graylog", "key": "https://downloads.graylog.org/repo/debian/pubkey.gpg",
                 "line": "deb [arch=amd64 signed-by=/etc/apt/keyrings/graylog.gpg] "
                         "https://downloads.graylog.org/repo/debian stable 7.1"},
                {"name": "mongodb", "key": "https://pgp.mongodb.com/server-8.0.asc",
                 "line": "deb [arch=amd64 signed-by=/etc/apt/keyrings/mongodb.gpg] "
                         "https://repo.mongodb.org/apt/ubuntu noble/mongodb-org/8.2 multiverse"},
            ],
            "packages": ["nfs-common", "mongodb-org", "graylog-server", "graylog-datanode",
                         "graylog-plugin-telegram-notification"],
            "mounts": [
                {"what": "{{ storage.nfs }}/mongodb", "where": "/var/lib/mongodb", "type": "nfs4",
                 "options": "rw,noatime,vers=4.2,hard,proto=tcp,nconnect=16,timeo=600,retrans=5,_netdev"},
                {"what": "{{ storage.nfs }}/graylog-server", "where": "/var/lib/graylog-server", "type": "nfs4",
                 "options": "rw,noatime,vers=4.2,hard,proto=tcp,nconnect=16,timeo=600,retrans=5,_netdev"},
                {"what": "{{ storage.nfs }}/graylog-datanode", "where": "/var/lib/graylog-datanode", "type": "nfs4",
                 "options": "rw,noatime,vers=4.2,hard,proto=tcp,nconnect=16,timeo=600,retrans=5,_netdev"},
            ],
            "files": [
                {"path": "/etc/sysctl.d/99-graylog-datanode.conf", "owner": "root:root", "mode": "0644",
                 "text": "vm.max_map_count = 262144\n", "check": "sysctl --system >/dev/null", "then": "nothing"},
                {"path": "/etc/graylog/server/server.conf", "owner": "root:root", "mode": "0640",
                 "set": {"password_secret": "{{ secret.graylog_password }}",
                         "root_password_sha2": "{{ secret.graylog_root_sha2 }}",
                         "http_bind_address": "0.0.0.0:9000", "message_journal_max_age": "72h",
                         "message_journal_max_size": "90g", "selfsigned_startup": "true"},
                 "then": "restart graylog-server"},
                {"path": "/etc/graylog/datanode/datanode.conf", "owner": "root:root", "mode": "0640",
                 "set": {"password_secret": "{{ secret.graylog_password }}",
                         "root_password_sha2": "{{ secret.graylog_root_sha2 }}",
                         "mongodb_uri": "mongodb://localhost:27017/graylog", "opensearch_heap": "24g",
                         "opensearch_configuration_overrides_file": "/etc/graylog/datanode/datanode-overrides.conf"},
                 "then": "restart graylog-datanode"},
                {"path": "/etc/graylog/datanode/datanode-overrides.conf", "owner": "root:root", "mode": "0644",
                 "text": "indices.recovery.max_bytes_per_sec = 300mb\n"
                         "cluster.routing.allocation.node_concurrent_recoveries = 2\n"
                         "cluster.routing.allocation.cluster_concurrent_rebalance = 2\n\n"
                         "indices.breaker.total.limit = 85%\nindices.fielddata.cache.size = 20%\n\n"
                         "thread_pool.write.queue_size = 1000\nthread_pool.search.queue_size = 1000\n",
                 "then": "restart graylog-datanode"},
            ],
            "enable": ["mongod", "graylog-datanode", "graylog-server"],
            "commands": ["ufw allow 9000/tcp", "ufw allow 15140:15143/tcp", "ufw allow 15140:15143/udp"],
        },
    },
    "opennebula": {
        "notes": "The OpenNebula front end with MySQL and Prometheus (owner 13:15: one service; Prometheus starts "
        "empty, owner Q5). Its files (oned.conf HA, FireEdge, OneFlow/OneGate, SAML) and the data come with the "
        "front end's redeploy (T34 B6): one node from the leader, all three from the platform backup.",
        "install": {
            "repos": [{"name": "opennebula", "key": "https://downloads.opennebula.io/repo/repo2.key",
                       "line": "deb [arch=amd64 signed-by=/etc/apt/keyrings/opennebula.gpg] "
                               "https://downloads.opennebula.io/repo/7.4/Ubuntu/{{ os.version }} stable opennebula"}],
            "packages": ["mysql-server", "nfs-common", "opennebula", "opennebula-fireedge", "opennebula-flow",
                         "opennebula-gate", "opennebula-migration", "opennebula-prometheus",
                         "opennebula-prometheus-kvm", "python3-pyone"],
            "enable": ["mysql"],
        },
    },
    "jump": {
        "name": "Jump host",
        "group": "jump",
        "notes": "One recipe for every jump host, admin1 the reference (owner Q3); no extras or home folders are "
        "carried (Q9). Ubuntu 26.04's own RDP (gnome-remote-desktop, no xrdp), ssh on the instance's port (55274), "
        "auditd, browsers; Tools: the packages a super admin adds (owner 13:38: xorriso and yq are obsolete).",
        "install": {
            "repos": [
                {"name": "google-chrome", "key": "https://dl.google.com/linux/linux_signing_key.pub",
                 "line": "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] "
                         "https://dl.google.com/linux/chrome-stable/deb/ stable main"},
                {"name": "opennebula", "key": "https://downloads.opennebula.io/repo/repo2.key",
                 "line": "deb [arch=amd64 signed-by=/etc/apt/keyrings/opennebula.gpg] "
                         "https://downloads.opennebula.io/repo/7.4/Ubuntu/{{ os.version }} stable opennebula"},
            ],
            "packages": ["gdm3", "gnome-session", "gnome-shell", "gnome-terminal", "gnome-text-editor",
                         "gnome-system-monitor", "gnome-control-center", "nautilus", "eog", "evince", "xwayland",
                         "fonts-dejavu", "fonts-liberation", "gnome-remote-desktop", "auditd", "firefox",
                         "google-chrome-stable", "libreoffice", "python3-pip", "pipx", "pwgen", "libxml2-utils",
                         "qemu-utils", "genisoimage", "openssl"],
            "tools": ["nfs-common", "opennebula-tools"],
            "files": [{"path": "/etc/ssh/sshd_config.d/10-claugine.conf", "owner": "root:root", "mode": "0644",
                       "text": "Port {{ node.ssh_port }}\n", "check": "sshd -t", "then": "restart ssh"}],
            "enable": ["gdm3", "auditd", "gnome-remote-desktop"],
            "commands": [
                "install -d -m 0700 -o gnome-remote-desktop /var/lib/gnome-remote-desktop/.local/share/gnome-remote-desktop",
                "test -s /var/lib/gnome-remote-desktop/rdp-tls.key || openssl req -new -newkey rsa:4096 -days 3650 "
                "-nodes -x509 -subj /CN={{ node.hostname }} -keyout /var/lib/gnome-remote-desktop/rdp-tls.key "
                "-out /var/lib/gnome-remote-desktop/rdp-tls.crt",
                "chown gnome-remote-desktop: /var/lib/gnome-remote-desktop/rdp-tls.*",
                "grdctl --system rdp set-tls-key /var/lib/gnome-remote-desktop/rdp-tls.key",
                "grdctl --system rdp set-tls-cert /var/lib/gnome-remote-desktop/rdp-tls.crt",
                "grdctl --system rdp set-port {{ node.rdp_port }}",
                "grdctl --system rdp set-credentials portal {{ secret.rdp|sh }}",  # commit-302
                "grdctl --system rdp enable",
                "systemctl set-default graphical.target",
                "ufw allow {{ node.rdp_port }}/tcp",
            ],
        },
    },
    "fail2ban": {
        "install": {
            "packages": ["fail2ban"],
            "files": [{"path": "/etc/fail2ban/jail.d/10-claugine.local", "owner": "root:root", "mode": "0644",
                       "text": FAIL2BAN, "check": "fail2ban-client -t", "then": "restart fail2ban"}],
            "enable": ["fail2ban"],
        },
    },
    "backup-ds": {
        "name": "Backup datastore",
        "group": "platform",
        "notes": "The VM is the cluster's OpenNebula backup datastore (owner 13:38): oneadmin and restic, the "
        "protocol OpenNebula's backup datastores use, over a volume of the site's Storage mounted by iSCSI (only the "
        "transport). The volume is found and mounted, never formatted: its volume group must be there.",
        "install": {
            "packages": ["open-iscsi", "multipath-tools", "lvm2", "xfsprogs", "restic"],
            "users": [{"name": "oneadmin", "uid": 9869, "groups": [], "keys": ["sa", "platform"]}],
            "enable": ["iscsid", "multipathd"],
            "commands": [
                "{% for p in storage.portals %}iscsiadm -m discovery -t sendtargets -p {{ p }} >/dev/null; "
                "{% endfor %}iscsiadm -m node -T {{ storage.iqn }} --login || true",
                "iscsiadm -m node -T {{ storage.iqn }} -o update -n node.startup -v automatic",
                "for i in $(seq 30); do vgs {{ storage.vg }} >/dev/null 2>&1 && break; sleep 2; done; "
                "vgs {{ storage.vg }} >/dev/null  # never formatted: the volume group must be found",
                "vgchange -ay {{ storage.vg }}",
            ],
            "mounts": [{"what": "/dev/mapper/{{ storage.vg }}-{{ storage.lv }}", "where": "/var/lib/one/datastores",
                        "type": "xfs", "options": "_netdev,nofail"}],
        },
    },
}

PROFILES = [
    {"key": "ns", "name": "Name server", "stype": "ns", "services": ["base-os", "bind"], "vcpu": 1, "ram_gb": 1,
     "disk_gb": 5, "checks": [{"kind": "unit", "value": "named"}, {"kind": "port", "value": "53"},
                              {"kind": "command", "value": "named-checkconf -z"}]},
    {"key": "logs", "name": "Log collector", "stype": "logs", "services": ["base-os", "graylog"], "vcpu": 6,
     "ram_gb": 48, "disk_gb": 20,
     "checks": [{"kind": "unit", "value": "mongod"}, {"kind": "unit", "value": "graylog-datanode"},
                {"kind": "unit", "value": "graylog-server"}, {"kind": "port", "value": "9000"}]},
    {"key": "onefe", "name": "OpenNebula front end", "stype": "onefe", "services": ["base-os", "opennebula"],
     "vcpu": 8, "ram_gb": 32, "disk_gb": 100,
     "checks": [{"kind": "unit", "value": "mysql"}, {"kind": "unit", "value": "opennebula"},
                {"kind": "port", "value": "2633"}]},
    {"key": "admin", "name": "Admin jump host", "stype": "admin", "services": ["base-os", "jump", "fail2ban"],
     "vcpu": 2, "ram_gb": 8, "disk_gb": 30,
     "checks": [{"kind": "unit", "value": "gdm3"}, {"kind": "unit", "value": "gnome-remote-desktop"},
                {"kind": "unit", "value": "fail2ban"}]},
    {"key": "backup", "name": "Backup storage", "stype": "backup", "services": ["base-os", "backup-ds"], "vcpu": 2,
     "ram_gb": 8, "disk_gb": 10,
     "checks": [{"kind": "command", "value": "mountpoint -q /var/lib/one/datastores"}]},
    {"key": "host", "name": "KVM host", "stype": "host",
     "services": ["ssh", "time", "libvirt", "opennebula-node", "frr", "one-exporter", "lldpd", "tuned"],
     "notes": "Every KVM host: the OS is identical; resources, NIC names and GPUs come from its server profile, "
     "detected when the host is added (owner 13:44). The host's deployment installs it (T25); NVIDIA where the "
     "server profile has GPUs."},
]

OLD_TEMPLATES = ("core-services-ns", "core-services-graylog", "core-services-admin", "core-services-backup")


def seed(apps, schema_editor):
    HostService = apps.get_model("inventory", "HostService")
    ServiceProfile = apps.get_model("inventory", "ServiceProfile")
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for key, p in PARTS.items():
        s = HostService.objects.filter(key=key).first()
        if s is None:
            s = HostService(key=key, name=p.get("name") or key, group=p.get("group") or "platform")
        s.install = p["install"]
        if p.get("notes") and not s.notes:
            s.notes = p["notes"]
        s.save()
    keys = set(HostService.objects.values_list("key", flat=True))
    for p in PROFILES:
        ServiceProfile.objects.update_or_create(
            key=p["key"],
            defaults={
                "name": p["name"],
                "stype": p["stype"],
                "services": [k for k in p["services"] if k in keys],
                "vcpu": p.get("vcpu"),
                "ram_gb": p.get("ram_gb"),
                "disk_gb": p.get("disk_gb"),
                "checks": p.get("checks") or [],
                "notes": p.get("notes") or "",
            },
        )
    ConfigTemplate.objects.filter(role__in=OLD_TEMPLATES).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0105_core_node_logging"),
    ]

    operations = [
        migrations.AddField(
            model_name="hostservice",
            name="install",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.CreateModel(
            name="ServiceProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.SlugField(max_length=40, unique=True)),
                ("name", models.CharField(max_length=100)),
                ("stype", models.CharField(help_text="a core service type, or host", max_length=16, unique=True,
                                           verbose_name="for")),
                ("services", models.JSONField(blank=True, default=list, help_text="keys of HostService, in order")),
                ("own_lines", models.TextField(blank=True, help_text="Shell lines run after the services'.",
                                               verbose_name="own lines")),
                ("vcpu", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("ram_gb", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("disk_gb", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("checks", models.JSONField(blank=True, default=list,
                                            help_text='[{"kind": unit|port|command, "value"}]: ready when all pass')),
                ("notes", models.TextField(blank=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.AlterField(
            model_name="configtemplate",
            name="role",
            field=models.CharField(
                choices=[
                    ("egw", "egw: internet edge of a project"),
                    ("igw", "igw: project gateway"),
                    ("cgw", "cgw: corporate network edge of a project"),
                    ("l2-a", "L2 channel, project side"),
                    ("l2-b", "L2 channel, external side"),
                    ("l2-br", "L2 channel, VLAN-to-network bridge (dedicated edge cluster)"),
                    ("rr-inet", "route reflector of the internet exchange"),
                    ("rr-corpnet", "route reflector of the corporate exchange"),
                    ("rr-vxlan", "route reflector of VXLAN/EVPN"),
                    ("cloud-igw", "cloud-igw: the site's igw (core service cloud-gw): the project igw with the "
                                  "management VRF"),
                    ("host-frr", "FRR of a KVM host (VXLAN/EVPN)"),
                    ("vyos-image-prepare", "VyOS image: prepare the installed image (run once on the ISO)"),
                    ("vyos-postconfig-bootup", "VyOS image: first boot from the OpenNebula context"),
                ],
                max_length=32,
                unique=True,
            ),
        ),
        migrations.AlterField(
            model_name="configtemplate",
            name="os",
            field=models.CharField(
                choices=[("vyos", "VyOS"), ("frr", "FRR")],
                default="vyos",
                help_text="What the configuration is for: VyOS routers or FRR on KVM hosts.",
                max_length=16,
                verbose_name="target",
            ),
        ),
        # commit-309: the database only goes forward (the way back is the backup taken before an update)
        migrations.RunPython(seed),
    ]
