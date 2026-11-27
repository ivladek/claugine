"""T36 (owner 2026-11-24/25, rules v2 and QS) - the data of the new model.

States: every "active" item is "deployed"; the existing hosts and site services are then "non-compliant" until their
first Check passes (owner 21:11 "most of services on platfrom deployed - but i think became to non-com pliant state").
OpenNebula per platform from the Baseline row. Profiles → OS: base-os and vyos. Functions (rules v2 C2) from the host
services (renamed: opennebula → one-fe, graylog → graylog-server, bind → ns, jump → admin; the OS part time →
timesync), the others become parts or stay OS parts, and the new functions are added. Each logged service gets its
function. Services (rules v2 C4/C5): the profiles renamed (ns → ns-primary, logs → log-collector, host → kvm-shared,
rr-ix → nf-ix, rr-vxlan → nf-evpn) with their roles and sizes, and the new ones added. Reversible: what was changed is
kept in each profile's legacy["t36"]; made rows are deleted on the way back."""

from django.db import migrations

RENAME_FN = {"opennebula": "one-fe", "graylog": "graylog-server", "bind": "ns", "jump": "admin", "time": "timesync"}
FUNCTIONS = {  # key: (name, os, includes, requires, version, vyos_template)
    "kvm": ("KVM host", "base-os", ["libvirt", "opennebula-node", "one-exporter", "lldpd", "tuned"], [], "", ""),
    "frr": (None, "base-os", None, [], "@frr", ""),
    "gpu": ("GPU passthrough", "base-os", ["nvidia"], ["kvm"], "", ""),
    "time": ("Time server (chrony)", "base-os", [], [], "", ""),
    "mysql": (None, "base-os", None, [], "8.4", ""),
    "one-fe": ("OpenNebula front end", "base-os", ["prometheus", "rpcbind"], ["mysql"], "", ""),
    "mongodb": (None, "base-os", None, [], "8.2", ""),
    "graylog-server": ("Graylog server", "base-os", ["rpcbind"], ["mongodb"], "7.1", ""),
    "graylog-datanode": ("Graylog data node (OpenSearch)", "base-os", [], ["mongodb"], "7.1", ""),
    "ns": ("Name server (BIND)", "base-os", None, [], "", ""),
    "admin": ("Admin desktop (GNOME, Ubuntu RDP, Chrome, Firefox)", "base-os", ["desktop", "rpcbind"], [], "", ""),
    "fail2ban": (None, "base-os", None, [], "", ""),
    "tools": ("Admin tools", "base-os", ["auditd"], [], "", ""),
    "backup-ds": (None, "base-os", None, [], "", ""),
    "claugine": ("Claugine portal", "base-os", [], [], "", ""),
    "nf-ix": ("Route reflectors of an ix network", "vyos", [], [], "", "rr-ix"),
    "nf-evpn": ("Route reflectors of the EVPN fabric", "vyos", [], [], "", "rr-vxlan"),
    "nf-egw": ("Internet edge gateway", "vyos", [], [], "", "egw"),
    "nf-igw": ("Project internet gateway", "vyos", [], [], "", "igw"),
    "nf-cgw": ("Corporate gateway", "vyos", [], [], "", "cgw"),
    "nf-xgw": ("Cloud gateway of the core services", "vyos", [], [], "", "cloud-igw"),
    "nf-l2-vlan": ("L2 tunnel endpoint on a VLAN", "vyos", [], [], "", "l2-b"),
    "nf-l2-vnet": ("L2 tunnel endpoint on a VNet", "vyos", [], [], "", "l2-a"),
}
PARTS = (
    "prometheus",
    "one-exporter",
    "libvirt",
    "lldpd",
    "tuned",
    "nvidia",
    "iscsid",
    "auditd",
    "xrdp",
    "desktop",
    "opennebula-node",
    "rpcbind",
)
TOOLS = [
    "opennebula-tools",
    "nfs-common",
    "curl",
    "wget",
    "jq",
    "pwgen",
    "whois",
    "inetutils-traceroute",
    "tree",
    "libxml2-utils",
    "dnsutils",
    "pipx",
    "libreoffice",
    "eog",
    "evince",
    "nautilus",
    "gnome-terminal",
    "gnome-text-editor",
]
LOG_FN = {
    "mysql": "mysql",
    "opennebula": "one-fe",
    "prometheus": "one-fe",
    "one-exporter": "kvm",
    "libvirt": "kvm",
    "frr": "frr",
    "nvidia": "gpu",
    "graylog": "graylog-server",
    "mongodb": "mongodb",
    "bind": "ns",
    "auditd": "tools",
    "fail2ban": "fail2ban",
}
CLOUD = [{"network": "core-cloud"}]
VYOS_NICS = [{"network": "core-vyos"}, {"network": "service"}]


def role(name, count, os_, fns, nics=None):
    return {"name": name, "count": count, "os": os_, "functions": fns, "nics": list(nics or [])}


# key: (rename from, stype, os, usage, size (vcpu, ram, disk) or None, vms, roles, placement, copy from)
SERVICES = {
    "kvm-mgmt": (
        None,
        "host",
        "base-os",
        "site",
        None,
        None,
        [role("host", 3, "base-os", ["kvm", "mysql", "one-fe", "time"])],
        "mgmt platform: its 3 hosts, automatic",
        "host",
    ),
    "kvm-simple-fe": (
        None,
        "host",
        "base-os",
        "site",
        None,
        None,
        [role("host", 3, "base-os", ["kvm", "frr", "mysql", "one-fe", "time"])],
        "simple platform: hosts 1-3, automatic",
        "host",
    ),
    "kvm-simple": (
        None,
        "host",
        "base-os",
        "site",
        None,
        None,
        [role("host", 3, "base-os", ["kvm", "frr"])],
        "simple platform: hosts 4-6, automatic",
        "host",
    ),
    "kvm-shared": (
        "host",
        "host",
        "base-os",
        "site",
        None,
        None,
        [role("host", 0, "base-os", ["kvm", "frr"])],
        "shared platform: its clusters",
        None,
    ),
    "kvm-shared-gpu": (
        None,
        "host",
        "base-os",
        "site",
        None,
        None,
        [role("host", 0, "base-os", ["kvm", "frr", "gpu"])],
        "shared platform: its GPU clusters",
        "host",
    ),
    "onefe": (
        "onefe",
        "onefe",
        "base-os",
        "site",
        (2, 8, 50),
        3,
        [role("vm", 3, "base-os", ["mysql", "one-fe"], CLOUD)],
        "mgmt platform (VMs): the platform it serves",
        None,
    ),
    "ns-primary": (
        "ns",
        "ns",
        "base-os",
        "site",
        (1, 2, 10),
        1,
        [role("vm", 1, "base-os", ["ns"], CLOUD)],
        "primary site: the 1st name server",
        None,
    ),
    "ns-secondary": (
        None,
        "ns",
        "base-os",
        "site",
        (1, 2, 10),
        1,
        [role("vm", 1, "base-os", ["ns"], CLOUD)],
        "primary site: the 2nd name server",
        "ns-primary",
    ),
    "ns-site": (
        None,
        "ns",
        "base-os",
        "site",
        (1, 2, 10),
        1,
        [role("vm", 1, "base-os", ["ns"], CLOUD)],
        "any other site",
        "ns-primary",
    ),
    "log-collector": (
        "logs",
        "logs",
        "base-os",
        "site",
        (4, 24, 20),
        1,
        [role("vm", 1, "base-os", ["mongodb", "graylog-server", "graylog-datanode"], CLOUD + [{"network": "nfs"}])],
        "mgmt/simple: the site's log settings",
        None,
    ),
    "admin": (
        "admin",
        "admin",
        "base-os",
        "site",
        (2, 8, 20),
        3,
        [role("vm", 3, "base-os", ["admin", "fail2ban", "tools"], CLOUD + [{"network": "nfs"}])],
        "mgmt/simple",
        None,
    ),
    "nf-ix": (
        "rr-ix",
        "bgp-rr",
        "vyos",
        "site",
        (2, 4, 2),
        3,
        [role("vm", 3, "vyos", ["nf-ix"], VYOS_NICS)],
        "mgmt/simple: an ix network",
        None,
    ),
    "nf-evpn": (
        "rr-vxlan",
        "bgp-rr",
        "vyos",
        "site",
        (2, 4, 2),
        3,
        [role("vm", 3, "vyos", ["nf-evpn"], VYOS_NICS)],
        "mgmt/simple: an evpn network",
        None,
    ),
    "cloud-gw": (
        "cloud-gw",
        "cloud-gw",
        "vyos",
        "site",
        (2, 4, 2),
        2,
        [role("egw", 1, "vyos", ["nf-egw"]), role("igw", 1, "vyos", ["nf-xgw"])],
        "mgmt/simple: core-cloud + core-services",
        None,
    ),
    "portal-gw": (
        "portal-gw",
        "portal-gw",
        "vyos",
        "site",
        (2, 4, 2),
        2,
        [role("egw", 2, "vyos", ["nf-egw"])],
        "mgmt/simple: an ext-managed platform, its public overlays",
        None,
    ),
    "backup": (
        "backup",
        "backup",
        "base-os",
        "site",
        (2, 8, 10),
        1,
        [role("vm", 1, "base-os", ["backup-ds"], CLOUD + [{"network": "iscsi"}])],
        "mgmt/simple: a platform (at least one each)",
        None,
    ),
    "claugine": (
        None,
        "claugine",
        "base-os",
        "site",
        (4, 8, 50),
        1,
        [role("vm", 1, "base-os", ["claugine"], CLOUD)],
        "primary platform (+ a 2nd node with Enterprise)",
        None,
    ),
    "egw": (None, "egw", "vyos", "project", (2, 4, 2), 1, [role("vm", 1, "vyos", ["nf-egw"])], "gateway cluster", None),
    "igw": (None, "igw", "vyos", "project", (1, 1, 2), 1, [role("vm", 1, "vyos", ["nf-igw"])], "gateway cluster", None),
    "cgw": (None, "cgw", "vyos", "project", (2, 4, 2), 1, [role("vm", 1, "vyos", ["nf-cgw"])], "gateway cluster", None),
    "l2vlan": (
        None,
        "l2vlan",
        "vyos",
        "project",
        (2, 2, 2),
        2,
        [role("endpoint 1", 1, "vyos", ["nf-l2-vlan"]), role("endpoint 2", 1, "vyos", ["nf-l2-vnet"])],
        "edge cluster (VLAN) / gateway cluster (VNet)",
        None,
    ),
    "l2vnet": (
        None,
        "l2vnet",
        "vyos",
        "project",
        (2, 2, 2),
        2,
        [role("endpoint 1", 1, "vyos", ["nf-l2-vnet"]), role("endpoint 2", 1, "vyos", ["nf-l2-vnet"])],
        "gateway cluster",
        None,
    ),
}
NAMES = {
    "kvm-mgmt": "KVM host, mgmt platform (with the front end)",
    "kvm-simple-fe": "KVM host, simple platform 1-3 (with the front end)",
    "kvm-simple": "KVM host, simple platform 4-6",
    "kvm-shared-gpu": "KVM host with GPUs, shared platform",
    "ns-secondary": "Name server, secondary",
    "ns-site": "Name server of a site",
    "claugine": "Claugine portal",
    "egw": "Project internet edge gateway",
    "igw": "Project internet gateway",
    "cgw": "Project corporate gateway",
    "l2vlan": "Project L2 tunnel (VLAN)",
    "l2vnet": "Project L2 tunnel (VNet)",
}
HOST_EXTRA = {"kvm-mgmt": ["opennebula", "time"], "kvm-simple-fe": ["opennebula", "time"], "kvm-shared-gpu": ["nvidia"]}
COPY = ("install_template", "configs", "nics", "checks", "own_lines")


def _ren(keys):
    return [RENAME_FN.get(k, k) for k in keys or []]


STEP_REN = {k: v for k, v in RENAME_FN.items() if k != "time"}  # an installation step per function (time: a part)


def _steps(apps, ren):
    """The installation templates' steps are named by their function: renamed as the functions are."""
    import re

    alt = "|".join(re.escape(k) for k in ren)
    rx = re.compile(rf"^(- name: )({alt})[ \t]*$", re.M)
    for t in apps.get_model("inventory", "ConfigTemplate").objects.filter(kind="steps"):
        body = rx.sub(lambda m: m.group(1) + ren[m.group(2)], t.body or "")
        if body != t.body:
            t.body = body
            t.save(update_fields=["body"])


HS_FIELDS = ("key", "name", "group", "includes", "os", "requires", "version", "vyos_template", "install")


def _free_key(model, key, limit=40):
    """commit-462: a key nobody holds, for a profile that held the name a T36 service takes."""
    for n in range(1, 1000):
        k = (f"{key}-own" if n == 1 else f"{key}-own{n}")[:limit]
        if not model.objects.filter(key=k).exists():
            return k
    raise RuntimeError(f"0122: no free key for {key}")


def _restore_keys(rows):
    """[(row, key)]: each row gets its key back - through a temporary key first, so no two rows meet on one key."""
    if not rows:
        return
    # commit-467: compared as MySQL's collation does (case-insensitive): the temporary keys are lower case
    model, targets = type(rows[0][0]), {k.lower() for _o, k in rows}
    for o, _k in rows:  # a temporary key nobody holds and no row is going back to
        n = 0
        while True:
            tmp = f"t36-tmp-{o.pk}" + (f"-{n}" if n else "")
            if tmp not in targets and not model.objects.filter(key__iexact=tmp).exists():
                break
            n += 1
        o.key = tmp
        o.save(update_fields=["key"])
    for o, k in rows:
        o.key = k
        o.save(update_fields=["key"])


# commit-499: the template a VyOS service's definition calculates (core_types: TEMPLATES, cloud-gw's igw, portal-gw's
# egw); a route reflector's comes from its network
GW_TEMPLATES = {"cloud-egw": "egw", "cloud-igw": "igw", "cloud-gw": "igw", "portal-gw": "egw"}


def _unlinked_for(apps, svc, p):
    """commit-498/499: an unlinked service counts only for the profile the portal would make it from
    (core_types.default_profile): of its type - for a VyOS one, of the template its definition calculates (a route
    reflector its network's, core_types.rr_role: EVPN rr-vxlan, any other exchange rr-ix; a gateway GW_TEMPLATES) -
    the first by pk that 0122 did not add (ServiceProfile.of_type). Several profiles can share a template (0117 keeps
    those with their own settings); a reflector whose network is gone, none."""
    SP = apps.get_model("inventory", "ServiceProfile")
    qs = SP.objects.filter(stype=svc.stype)
    if svc.stype == "bgp-rr":
        net = apps.get_model("inventory", "Network").objects.filter(pk=(svc.params or {}).get("network") or 0).first()
        if net is None or net.kind not in ("ix", "evpn"):
            return False
        qs = qs.filter(install_template="rr-vxlan" if net.kind == "evpn" else "rr-ix")
    elif svc.stype in GW_TEMPLATES:
        qs = qs.filter(install_template=GW_TEMPLATES[svc.stype])
    rows = list(qs.order_by("pk"))
    own = next((x for x in rows if not ((x.legacy or {}).get("t36") or {}).get("made")), rows[0] if rows else None)
    return own is not None and own.pk == p.pk


def _blank_count(apps, p, stype, roles):
    """commit-493/497: a VM profile with no VM count. Its services' layouts decide, never the seeded role: when they
    all have the same number of VMs, that is its count (and its one role's); when they differ, the count stays blank
    and the profile keeps no roles until they can be told apart (B3); with no service, its roles' count."""
    CS, N = apps.get_model("inventory", "CoreService"), apps.get_model("inventory", "CoreServiceNode")
    svcs = list(CS.objects.filter(profile_id=p.pk)) + [
        x for x in CS.objects.filter(profile__isnull=True, stype=stype) if _unlinked_for(apps, x, p)
    ]
    counts = {N.objects.filter(service=x).count() for x in svcs}
    counts.discard(0)
    total = sum(int(r.get("count") or 0) for r in roles)
    if not counts:
        p.vms = total or None
        return roles, f"VMs set to {p.vms} from its roles (it had no count, no service)"
    if len(counts) == 1:
        n = counts.pop()
        p.vms = n
        if len(roles) == 1:
            return [dict(roles[0], count=n)], f"VMs set to {n}, as its services have (it had no count)"
        if total == n:
            return roles, f"VMs set to {n}, as its services have (it had no count)"
        return [], f"VMs set to {n}, as its services have; no roles until they match it"
    return [], f"no VM count, no roles: its services have {sorted(counts)} VMs (Site → Services, B3)"


def forward(apps, schema_editor):
    g = apps.get_model
    PS = g("tenants", "PortalSettings")
    ps = PS.objects.order_by("pk").first()
    os_ver = getattr(ps, "baseline_os", "") or "26.04"
    vyos_ver = getattr(ps, "baseline_vyos", "") or "Stream 2026.03"
    one_ver = getattr(ps, "baseline_one", "") or "7.4"
    frr_ver = getattr(ps, "baseline_frr", "") or "10.6"
    # states
    for m in ("Host", "Cluster", "Storage", "CoreService", "Network"):
        g("inventory", m).objects.filter(state="active").update(state="deployed")
    g("inventory", "Host").objects.filter(state="deployed").update(state="non-compliant")
    g("inventory", "CoreService").objects.filter(state="deployed").update(state="non-compliant")
    # commit-468: each site's layout - its first simple or mgmt platform; platforms without one: dedicated
    for site in g("inventory", "Site").objects.filter(layout=""):
        first = g("inventory", "Platform").objects.filter(site=site, kind__in=("simple", "mgmt")).order_by("pk").first()
        if first is not None or g("inventory", "Platform").objects.filter(site=site).exists():
            site.layout = "simple" if first is not None and first.kind == "simple" else "dedicated"
            site.save(update_fields=["layout"])
    # OpenNebula per platform
    g("inventory", "Platform").objects.filter(one_version="").update(
        one_version=one_ver,
        one_ee=bool(getattr(ps, "one_repo_private", False)),
        one_login=getattr(ps, "one_repo_login", "") if getattr(ps, "one_repo_private", False) else "",
    )
    # functions
    HS = g("inventory", "HostService")
    LS = g("inventory", "LoggedService")
    for hs in HS.objects.all():  # commit-463: each row as it was, for the reverse
        legacy = dict(hs.legacy or {})
        legacy["t36"] = {f: getattr(hs, f) for f in HS_FIELDS}
        hs.legacy = legacy
        hs.save(update_fields=["legacy"])
    for old, new in RENAME_FN.items():
        if HS.objects.filter(key=old).exists() and not HS.objects.filter(key=new).exists():
            HS.objects.filter(key=old).update(key=new)
    for hs in HS.objects.all():
        changed = False
        inc = _ren(hs.includes)
        if hs.key == "one-fe":
            inc = [k for k in inc if k != "mysql"]
        if hs.key == "graylog-server":
            inc = [k for k in inc if k != "mongodb"]
        if hs.key == "admin":
            inc = [k for k in inc if k not in ("auditd", "xrdp")]
        if inc != hs.includes:
            hs.includes, changed = inc, True
        if hs.key in PARTS:
            hs.group, changed = "part", True
        if changed:
            hs.save()
    for key, (name, os_, inc, req, ver, tpl) in FUNCTIONS.items():
        hs = HS.objects.filter(key=key).first() or HS(key=key, name=name or key, legacy={"t36": {"made": True}})
        hs.group, hs.os, hs.requires = "function", os_, list(req)
        if name:  # rules v2 C2: what the function is (a renamed row too - its old name is in legacy["t36"])
            hs.name = name
        if inc is not None and not hs.pk:
            hs.includes = list(inc)
        elif inc is not None and key in ("kvm", "gpu", "tools", "graylog-datanode", "time", "claugine"):
            hs.includes = list(inc)
        hs.version = frr_ver if ver == "@frr" else ver
        hs.vyos_template = tpl
        if key == "tools":
            hs.install = {"tools": TOOLS}
        hs.save()
    _steps(apps, STEP_REN)
    for ls in LS.objects.all():
        fk = LOG_FN.get(ls.name)
        if fk:
            ls.function = HS.objects.filter(key=fk).first()
            ls.save(update_fields=["function"])
    # commit-464: Host.services / CoreServiceNode.services hold logged-service names (not renamed): left as they are
    # OS
    OS = g("inventory", "OsProfile")
    OS.objects.get_or_create(
        key="base-os",
        defaults={
            "distro": "Ubuntu",
            "version": os_ver,
            "min_vcpu": 1,
            "min_ram_gb": 2,
            "min_disk_gb": 5,
            "logging": LS.objects.filter(name="common").first(),
        },
    )
    OS.objects.get_or_create(
        key="vyos",
        defaults={
            "distro": "VyOS",
            "version": vyos_ver,
            "min_vcpu": 1,
            "min_ram_gb": 1,
            "min_disk_gb": 2,
            "logging": LS.objects.filter(name="vyos").first(),
        },
    )
    # services
    SP = g("inventory", "ServiceProfile")
    made = []
    for key, (old, stype, os_, usage, size, vms, roles, placement, copy) in SERVICES.items():
        # commit-462: only the old profile of the same type is taken over; another holding the new key moves aside
        p = SP.objects.filter(key=old, stype=stype).first() if old else None
        other = SP.objects.filter(key=key).exclude(pk=p.pk if p else None).first()
        if other is not None:
            legacy = dict(other.legacy or {})
            legacy["t36"] = {"moved": other.key}
            other.key, other.legacy = _free_key(SP, key), legacy
            other.save(update_fields=["key", "legacy"])
            print(f"  inventory 0122: profile {legacy['t36']['moved']} renamed {other.key} (its name is a T36 service)")
        if p is not None:
            legacy = dict(p.legacy or {})
            legacy["t36"] = {
                "key": p.key,
                "services": p.services,
                "vcpu": p.vcpu,
                "ram_gb": p.ram_gb,
                "disk_gb": p.disk_gb,
                "vms": p.vms,
                "nics": p.nics,
            }
            p.legacy = legacy
            p.services = _ren(p.services)
        else:
            src = SP.objects.filter(key=copy).first() if copy else None
            p = SP(key=key, stype=stype, os=os_, name=NAMES.get(key, ""))
            if src is not None:
                for f in COPY:
                    setattr(p, f, getattr(src, f))
                base = list(src.services)
                p.services = base + [k for k in _ren(HOST_EXTRA.get(key, [])) if k not in base]
            else:
                p.services = (["base-os"] if os_ == "base-os" else []) + [f for r in roles for f in r["functions"]]
            p.legacy = {"t36": {"made": True}}
            made.append(key)
        if key not in made and p.vms and len(roles) == 1 and stype != "host":
            # commit-493: one count - a renamed profile keeps its VMs (above) and its one role says the same
            roles = [dict(roles[0], count=p.vms)]
        elif key not in made and not p.vms and stype != "host" and roles:
            roles, why = _blank_count(apps, p, stype, roles)
            print(f"  inventory 0122: profile {key}: {why}")
        p.key, p.usage, p.roles, p.placement = key, usage, roles, placement
        if size:
            p.vcpu, p.ram_gb, p.disk_gb = size
        # a renamed profile keeps its VM count and NICs: the running layouts stay as they are (the ns split into
        # ns-primary + ns-secondary comes with Site → Services, B3); only the new rows take rules v2's
        if key in made:
            if size:
                p.vms = vms
            if roles and roles[0].get("nics") and stype not in ("host",):
                p.nics = roles[0]["nics"]
        p.save()
        if key in made:  # what it logs: its OS's rules + those of its functions (and their parts)
            fns = {f for r in roles for f in r["functions"]}
            logged = list(LS.objects.filter(name="vyos" if os_ == "vyos" else "common"))
            if os_ != "vyos":
                logged += list(LS.objects.filter(function__key__in=fns).exclude(name__in=("common", "vyos")))
            p.logged.set(logged)
    print(f"  inventory 0122: services made {', '.join(made) or 'none'}")


def back(apps, schema_editor):
    g = apps.get_model
    SP = g("inventory", "ServiceProfile")
    SP.objects.filter(legacy__t36__made=True).delete()
    keys = []
    for p in SP.objects.all():
        t = (p.legacy or {}).get("t36")
        if not t:
            continue
        legacy = dict(p.legacy)
        legacy.pop("t36", None)
        p.legacy = legacy
        if "moved" in t:  # commit-462: its own name back
            keys.append((p, t["moved"]))
        else:
            for f in ("services", "vcpu", "ram_gb", "disk_gb", "vms", "nics"):
                setattr(p, f, t[f])
            p.roles, p.usage, p.placement = [], "site", ""
            keys.append((p, t["key"]))
        p.save()
    _restore_keys(keys)
    g("inventory", "OsProfile").objects.all().delete()
    HS = g("inventory", "HostService")
    g("inventory", "LoggedService").objects.update(function=None)
    HS.objects.filter(legacy__t36__made=True).delete()
    keys = []
    for hs in HS.objects.all():  # commit-463: each row exactly as it was
        t = (hs.legacy or {}).get("t36")
        if not t:
            continue
        for f in HS_FIELDS:
            if f != "key":
                setattr(hs, f, t[f])
        legacy = dict(hs.legacy)
        legacy.pop("t36", None)
        hs.legacy = legacy
        hs.save()
        keys.append((hs, t["key"]))
    _restore_keys(keys)
    _steps(apps, {v: k for k, v in STEP_REN.items()})
    for m in ("Host", "Cluster", "Storage", "CoreService", "Network"):
        g("inventory", m).objects.filter(state__in=("deployed", "non-compliant")).update(state="active")


class Migration(migrations.Migration):
    dependencies = [("inventory", "0121_t36_model"), ("tenants", "0110_t36_settings")]

    operations = [migrations.RunPython(forward, back)]
