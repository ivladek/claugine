"""Profiles → Services (owner 2026-11-21, T33; T34): the service profiles - what a VM of a site service (or every KVM
host) is made of: its installation and config files from Templates, its networks, checks and its logged services
(owner 2026-11-24 09:13: picked from Profiles → Logging, inventory/logging_views.py; a Base OS profile always has
common, a VyOS profile only vyos). A machine has what its profile gives; nothing is looked for on it (T34, owner
13:10). The deployment step of the logging: hostdeploy.s_logging.
"""

import json
import re

from django import forms
from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from accounts.models import Role, audit
from accounts.utils import staff_required

from . import base_deploy, logconf, logging_views, recipes
from .models import ServiceProfile
from .network_views import rows_widget

SEP = "¦"  # the rows' fields: a regular expression may hold | and spaces


class ServiceError(Exception):
    pass


@staff_required(Role.SUPPORT, Role.RESOURCE_MANAGER)
def services_view(request):
    from .models import Host

    if request.GET.get("tab") == "rules":  # the old Logging rules tab: Profiles → Logging (owner 2026-11-24 08:54)
        return redirect(reverse("infra_logging"))
    profiles = list(ServiceProfile.objects.prefetch_related("logged"))
    from . import baseline as _bl

    releases = {v: (label, r) for v, label, r in _bl.image_types()}
    from .models import HostService

    fn_ids = dict(HostService.objects.filter(group=HostService.Group.FUNCTION).values_list("key", "pk"))
    for p in profiles:  # T34 S2d (ui-14): the service profiles list and details
        p.fn_ids = fn_ids  # T36 B2d: a role's functions link to Profiles → Functions
        p.type_label = type_label(p.stype)
        p.os_label, p.os_release = releases.get(p.os, (p.os, ""))  # U7: Base OS or VyOS with the baseline's release
        tpls = base_deploy.templates_of(p)
        p.install_t = tpls.get(p.install_template) if p.install_template else None
        p.config_rows = [(c, tpls.get(str(c.get("template") or ""))) for c in p.configs or [] if isinstance(c, dict)]
        p.networks = base_deploy.nic_rows(p)
        # owner 2026-11-24 09:13: Configs = the service's config templates only; its logging = its logged services
        p.n_configs = len(p.config_rows)
        p.logged_list = sorted(p.logged.all(), key=lambda x: (x.name != "common", x.name))
        p.instances = _instances(p)
        # T36 B4a: a host service's own hosts (Host.service)
        p.host_list = (
            list(Host.objects.filter(service=p).select_related("site").order_by("site__code", "name"))
            if p.stype == ServiceProfile.HOST
            else []
        )
        p.hosts = len(p.host_list)
        p.n_instances = p.hosts if p.stype == ServiceProfile.HOST else len(p.instances)
        # T36 B3d: a site service profile 0122 left without roles (its services' VM counts differ from the rules v2
        # layout) - shown to be filled; the counts its services have now
        p.roles_missing = not p.roles and p.stype != ServiceProfile.HOST and p.usage == ServiceProfile.Usage.SITE
        # commit-511: a service with no VM is no layout to match
        p.instance_vms = layout_counts(p.instances) if p.roles_missing else []
        p.preview = preview(p)
    return render(
        request,
        "inventory/services.html",
        {"profiles": profiles, "can_edit": request.user.is_super_admin},
    )


# --- service profiles (Profiles → Services; owner 2026-11-21 13:01-13:44, T34) ----------------------------------------

PROFILE_FOR = [
    ("ns", "ns"),
    ("logs", "logs"),
    ("onefe", "onefe"),
    ("admin", "admin"),
    ("backup", "backup"),
    ("bgp-rr", "bgp-rr"),  # owner 2026-11-23 (T34 Q23 A): the VyOS services' profiles
    ("cloud-gw", "cloud-gw"),
    ("portal-gw", "portal-gw"),
    ("claugine", "claugine"),  # T36 rules v2 C4/C5: the portal and the project gateways (project automation)
    ("egw", "egw"),
    ("igw", "igw"),
    ("cgw", "cgw"),
    ("l2vlan", "l2vlan"),
    ("l2vnet", "l2vnet"),
    (ServiceProfile.HOST, _l("host: every KVM host")),
]
ROLE_RE = re.compile(
    r"^(?P<name>[a-z0-9][a-z0-9 -]{0,30}?)\s+(?P<count>\d{1,2})\s+(?P<os>base-os|vyos)\s*:\s*(?P<fns>.*)$"
)
VYOS_STYPES = ("bgp-rr", "cloud-gw", "portal-gw")  # made by their VyOS template (core_deploy), several per type
# T36 C5: the project gateways are VyOS services too (project automation; not made by core_deploy)
VYOS_PROJECT_STYPES = ("egw", "igw", "cgw", "l2vlan", "l2vnet")
# commit-420: the VyOS templates each type's definition calculates (core_types: rr_role, TEMPLATES, the egw)
VYOS_TEMPLATES = {"bgp-rr": ("rr-ix", "rr-vxlan"), "cloud-gw": ("igw",), "portal-gw": ("egw",)}  # rr-ix: Q9
CHECK_KINDS = ("unit", "port", "command")
# what a service VM's NIC is on; "storage": the network of its volume, nfs or iscsi (owner 2026-11-23 09:47, Q22 A)
MAX_NICS = 16
# service: the network chosen when the site service is added (a route reflector's IX or EVPN network)
NIC_NETWORKS = ("core-cloud", "core-services", "core-vyos", "nfs", "iscsi", "storage", "service")


def type_label(stype):
    """The profile's Type of service as the site's services name it (ui-14)."""
    from .models import CoreService

    if stype == ServiceProfile.HOST:
        return _("host: every KVM host")
    return str(dict(CoreService.Type.choices).get(stype, stype))


class ProfileForm(forms.Form):
    # owner 2026-11-23 16:01 (U5, U6, Q4): Name (the key; editable, unique, no spaces) and Description (the name)
    key = forms.SlugField(
        label=_l("Name"),
        max_length=40,
        help_text=_l("A short name without spaces; unique. Can be changed: services keep their profile."),
    )
    name = forms.CharField(label=_l("Description"), max_length=100, required=False)
    # U7 (Q5): Base OS or VyOS, with the releases of Profiles → Baseline; fixed once the profile is made
    os = forms.ChoiceField(label=_l("OS"), choices=ServiceProfile.Os.choices)
    stype = forms.ChoiceField(
        label=_l("Type of service"),
        choices=PROFILE_FOR,
        help_text=_l("The site services it makes. It cannot be changed later."),
    )
    vcpu = forms.IntegerField(label="vCPU", min_value=1, max_value=256, required=False)
    ram_gb = forms.IntegerField(label=_l("RAM, GB"), min_value=1, max_value=4096, required=False)
    disk_gb = forms.IntegerField(label=_l("Disk, GB"), min_value=1, max_value=65535, required=False)
    # T36 B2d (rules v2 C3-C5, mockup m03): where it is used, where its machines are placed, and its roles
    usage = forms.ChoiceField(label=_l("Used in"), choices=ServiceProfile.Usage.choices, initial="site", required=False)
    placement = forms.CharField(label=_l("Placed on"), max_length=200, required=False)
    dns_slot = forms.TypedChoiceField(  # commit-507: a name server profile's DNS server
        label=_l("DNS slot"),
        coerce=int,
        empty_value=None,
        required=False,
        choices=[("", "—")] + [(str(k), v) for k, v in ServiceProfile.DNS_SLOTS],
        help_text=_l("Name servers only: which of the site's DNS servers a service made from it takes."),
    )
    host_slot = forms.ChoiceField(  # T36 B4a: a host service's hosts
        label=_l("Host slot"),
        required=False,
        choices=[("", "—")] + list(ServiceProfile.HOST_SLOTS),
        help_text=_l("Host services only: which KVM hosts are made from it."),
    )
    roles = forms.CharField(
        label=_l("Roles"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "class": "mono"}),
        help_text=_l(
            "One per line: name count OS: functions in order - e.g. vm 3 base-os: mysql, one-fe (count 0: any number, "
            "as a shared platform's hosts). Each function is of that OS (Profiles → Functions) and comes with every "
            "function it requires."
        ),
    )
    vms = forms.IntegerField(  # owner 2026-11-23 (T34 Q24)
        label=_l("VMs"),
        min_value=1,
        max_value=20,
        required=False,
        help_text=_l("How many VMs a site service made from it has."),
    )
    # T34 S2d (owner 2026-11-22 16:59, ui-16): the installation and every config are chosen from Templates
    install_template = forms.ChoiceField(
        label=_l("Installation"),
        required=False,
        help_text=_l(
            "A template of the kind installation (Profiles → Templates); a VyOS service's: its VyOS configuration "
            "template."
        ),
    )
    configs = forms.CharField(
        label=_l("Configs"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "class": "mono"}),
        help_text=_l(
            "Each config file is written whole from its template (kind config). Check: a command run on the new file "
            "({file}: the file); Then: nothing, or reload or restart with its unit, once when any of its files "
            "changed. The rsyslog and logrotate files are generated from the log sources (Logging)."
        ),
    )
    nics = forms.CharField(
        label=_l("Networks"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 2, "class": "mono"}),
        help_text=_l(
            "The networks of its VMs' NICs, in order. storage: the network of the instance's volume (nfs or iscsi). "
            "Optional: when the instance has it. Redeploy checks a site service's NICs against them."
        ),
    )
    checks = forms.CharField(
        label=_l("Checks"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "class": "mono"}),
        help_text=_l("One per line: unit <name>, port <number> or command <shell line>. Ready when all pass."),
    )
    # owner 2026-11-24 09:13 / 09:21: the logged services from Profiles → Logging, picked like the countries in
    # Settings (two lists); a Base OS profile always has common, a VyOS profile only vyos (inventory/logging_views.py)
    logged = forms.MultipleChoiceField(label=_l("Logged services"), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from . import baseline
        from .models import ConfigTemplate, LoggedService

        self.fields["logged"].choices = [
            (str(x.pk), x.name)
            for x in LoggedService.objects.filter(kind=LoggedService.Kind.BASE_OS).exclude(name=LoggedService.COMMON)
        ]
        rel = {v: f"{label} — {r}" for v, label, r in baseline.image_types()}
        self.fields["os"].choices = [(v, rel.get(v, label)) for v, label in ServiceProfile.Os.choices]
        tpls = list(ConfigTemplate.objects.filter(kind__in=ConfigTemplate.SERVICE_KINDS).order_by("role"))
        self.fields["install_template"].choices = (
            [("", "—")]
            + [
                (t.role, f"{t.role} - {t.name} ({t.get_kind_display()})")
                for t in tpls
                if t.kind in ConfigTemplate.INSTALL_KINDS
            ]
            + [  # T34 S3b: a VyOS service profile names its VyOS template
                (t.role, f"{t.role} - {t.name} (VyOS)")
                for t in ConfigTemplate.objects.filter(kind=ConfigTemplate.Kind.NETWORK, os="vyos").order_by("role")
            ]
        )
        rows_widget(
            self.fields["configs"],
            [
                ("path", _("File"), "/etc/bind/named.conf.options", "16em"),
                ("template", _("Template"), "", "12em", [t.role for t in tpls if t.kind == "config"]),
                ("owner", _("Owner"), "root:root", "7em"),
                ("mode", _("Mode"), "0644", "4em"),
                ("check", _("Check"), "named-checkconf {file}"),
                ("then", _("Then"), "reload named", "9em"),
            ],
            _("Config"),
            sep=SEP,
        )
        for f in ("vcpu", "ram_gb", "disk_gb", "vms"):
            self.fields[f].widget.attrs["style"] = "width:6em"
        rows_widget(
            self.fields["nics"],
            [("network", _("Network"), "", "14em", NIC_NETWORKS), ("optional", _("Optional"), "", "6em", ("yes",))],
            _("NIC"),
            sep=SEP,
        )

    def clean(self):
        d = super().clean()
        os_, stype = d.get("os"), d.get("stype")
        vyos_type = stype in VYOS_STYPES or stype in VYOS_PROJECT_STYPES
        if os_ and stype and (os_ == ServiceProfile.Os.VYOS) != vyos_type:  # U7: the types of its OS
            self.add_error("stype", _("Not a type of this OS: %(t)s.") % {"t": stype})
        for f in (
            "usage",
            "placement",
            "roles",
        ):  # T36 B2d: a form without them (an older page) leaves them as they are
            if f not in self.data:
                d.pop(f, None)
        if os_ == ServiceProfile.Os.VYOS:  # U8: "dont add any ubuntu configs to VyOS based service profiles"
            for f in ("logged", "configs", "checks"):
                if d.get(f):
                    self.add_error(f, _("Not for a VyOS profile: its VyOS template is its whole configuration."))
        return d

    def clean_checks(self):
        out = []
        for line in (self.cleaned_data.get("checks") or "").splitlines():
            if not line.strip():
                continue
            kind, _sp, value = line.strip().partition(" ")
            value = value.strip()
            if kind not in CHECK_KINDS or not value:
                raise forms.ValidationError(_("%(l)s: unit, port or command, then its value.") % {"l": line.strip()})
            if kind == "port" and not (value.isdigit() and 0 < int(value) < 65536):
                raise forms.ValidationError(_("%(l)s: a port number.") % {"l": line.strip()})
            out.append({"kind": kind, "value": value})
        return out

    def clean_configs(self):
        out, paths = [], set()
        for line in (self.cleaned_data.get("configs") or "").splitlines():
            f = [x.strip() for x in line.split(SEP)]
            if not any(f):
                continue
            f += [""] * (6 - len(f))
            path, template, owner, mode, check, then = f[:6]
            if not recipes.plain_path(path) or not template:
                raise forms.ValidationError(
                    _("Give the file's full path and its template: %(l)s") % {"l": line.strip()}
                )
            if path in paths:
                raise forms.ValidationError(_("%(f)s is written twice.") % {"f": path})
            paths.add(path)
            then = " ".join((then or "nothing").split())
            if then != "nothing" and not re.fullmatch(r"(reload|restart) [A-Za-z0-9@._:-]+", then):
                raise forms.ValidationError(  # commit-396: the unit is always named
                    _("Then: nothing, or reload or restart with its unit (reload named): %(l)s") % {"l": line.strip()}
                )
            row = {"path": path, "template": template, "then": then}
            row.update({k: v for k, v in (("owner", owner), ("mode", mode), ("check", check)) if v})
            out.append(row)
        return out

    def clean_nics(self):
        out = []
        for line in (self.cleaned_data.get("nics") or "").splitlines():
            f = [x.strip() for x in line.split(SEP)] + [""]
            net, opt = f[0], f[1]
            if not net:
                continue
            if net not in NIC_NETWORKS:
                raise forms.ValidationError(_("Not a network of a service VM: %(l)s") % {"l": net})
            if opt not in ("", "yes"):
                raise forms.ValidationError(_("Optional: yes or empty: %(l)s") % {"l": line.strip()})
            out.append({"network": net, "optional": True} if opt else {"network": net})
        if len(out) > MAX_NICS:  # commit-401: a VM has a few NICs
            raise forms.ValidationError(_("At most %(n)s NICs.") % {"n": MAX_NICS})
        return out


def configs_text(p):
    return "\n".join(
        f" {SEP} ".join(
            [str(c.get(k) or "") for k in ("path", "template", "owner", "mode", "check")]
            + [str(c.get("then") or "nothing")]
        )
        for c in p.configs or []
        if isinstance(c, dict)
    )


def profile_initial(p):
    return {
        "key": p.key,
        "name": p.name,
        "os": p.os,
        "stype": p.stype,
        "logged": [str(x.pk) for x in p.logged.all() if x.name != "common" and not x.is_vyos],
        "vcpu": p.vcpu,
        "ram_gb": p.ram_gb,
        "disk_gb": p.disk_gb,
        "vms": p.vms,
        "install_template": p.install_template,
        "configs": configs_text(p),
        "nics": "\n".join(
            str(n.get("network") or "") + (f" {SEP} yes" if n.get("optional") else "")
            for n in p.nics or []
            if isinstance(n, dict)
        ),
        "checks": "\n".join(f"{c.get('kind')} {c.get('value')}" for c in p.checks or []),
        "notes": p.notes,
        "usage": p.usage,
        "placement": p.placement,
        "dns_slot": "" if p.dns_slot is None else str(p.dns_slot),
        "host_slot": p.host_slot,
        "roles": roles_text(p.roles),
    }


def roles_text(roles):
    """The roles as the edit form shows them: name count OS: functions."""
    return "\n".join(
        f"{r.get('name')} {r.get('count', 0)} {r.get('os')}: {', '.join(r.get('functions') or [])}"
        for r in roles or []
        if isinstance(r, dict)
    )


def parse_roles(text, os_, old=()):
    """The edit form's roles text → [{name, count, os, functions, nics}] (T36 rules v2 C3): each function of the role's
    OS, the role's OS the profile's, every function a role's function requires in that role too. A role keeps the NICs
    it had (by name)."""
    from .models import HostService

    fns = {f.key: f for f in HostService.objects.filter(group=HostService.Group.FUNCTION)}
    old = [r for r in old or [] if isinstance(r, dict)]
    nics = {r.get("name"): r.get("nics") or [] for r in old}
    out, seen = [], set()
    for n, line in enumerate((text or "").splitlines(), 1):
        line = " ".join(line.split())
        if not line:
            continue
        m = ROLE_RE.match(line)
        if not m:
            raise ServiceError(
                _("Roles, line %(n)s: name count OS: functions (e.g. vm 3 base-os: mysql, one-fe).") % {"n": n}
            )
        name, os_r = m["name"], m["os"]
        keys = [k.strip() for k in m["fns"].split(",") if k.strip()]
        if name in seen:
            raise ServiceError(_("Roles: %(r)s twice.") % {"r": name})
        seen.add(name)
        if os_r != os_:
            raise ServiceError(
                _("Roles: %(r)s runs %(o)s, the service's OS is %(s)s.") % {"r": name, "o": os_r, "s": os_}
            )
        unknown = [k for k in keys if k not in fns]
        if unknown:
            raise ServiceError(_("Roles: no function %(k)s (Profiles → Functions).") % {"k": ", ".join(unknown)})
        other = [k for k in keys if fns[k].os != os_r]
        if other:
            raise ServiceError(_("Roles: %(k)s is not a function of %(o)s.") % {"k": ", ".join(other), "o": os_r})
        missing = sorted({req for k in keys for req in fns[k].requires or [] if req not in keys})
        if missing:
            raise ServiceError(
                _("Roles: %(r)s needs %(m)s too (required by its functions).") % {"r": name, "m": ", ".join(missing)}
            )
        out.append({"name": name, "count": int(m["count"]), "os": os_r, "functions": keys, "nics": nics.get(name, [])})
    # commit-494/496: roles keep their NICs by name; a renamed role keeps its old NICs only when that is unambiguous -
    # exactly one old role and one new role are left without a match
    new_only = [r for r in out if r["name"] not in nics]
    old_only = [r for r in old if r.get("name") not in {x["name"] for x in out}]
    if len(new_only) == 1 and len(old_only) == 1:
        new_only[0]["nics"] = old_only[0].get("nics") or []
    elif new_only and any(r.get("nics") for r in old_only):  # commit-494: which new role is which old one is unclear
        raise ServiceError(
            _(
                "Roles: %(o)s gone and %(n)s new - their NICs would be lost. Rename one role at a time (it keeps its NICs)."
            )
            % {"o": ", ".join(r.get("name") for r in old_only), "n": ", ".join(r["name"] for r in new_only)}
        )
    return out


def preview(p):
    """The profile's first-boot script with example values (secrets masked), or why it cannot be made."""
    from .base_deploy import templates_of

    if p.stype == ServiceProfile.HOST or p.is_vyos:  # a VyOS VM is made from its VyOS template
        return None
    try:
        return recipes.render_script(p, templates_of(p), recipes.sample_values(p), title=f"example {p.stype} VM")
    except Exception as exc:  # noqa: BLE001 - commit-299: one profile's error never takes the page down
        return f"# {exc}"


def _check_minimum(p):
    """commit-479 (rules v2 C3): a service's size is never below its OS's minimum - the size it is given, else its
    type's default (core_types.SIZES); a host service has no size (the server profile's)."""
    from .core_types import SIZES
    from .models import OsProfile

    if p.stype == ServiceProfile.HOST:
        return
    o = OsProfile.objects.filter(key=p.os).first()
    if o is None:
        return
    dv = SIZES.get(p.stype) or (None, None, None)
    eff = [x if x is not None else y for x, y in zip((p.vcpu, p.ram_gb, p.disk_gb), dv, strict=True)]
    if eff[2] is None:  # commit-479: a new VM's disk falls back to 2 GB (core_types), so that is checked
        eff[2] = 2
    below = o.below_minimum(*eff)
    if below:
        names = {"vcpu": "vCPU", "ram_gb": _("RAM, GB"), "disk_gb": _("disk, GB")}
        raise ServiceError(
            _("Below the minimum of %(os)s (Profiles → OS): %(what)s.")
            % {"os": o.key, "what": ", ".join(f"{names[f]} {m}" for f, m in below)}
        )


def save_profile(user, pk, d, new=False):
    """Save a service profile, found by its id (owner 2026-11-23 U4: its Name can change, nothing else follows it)."""
    from .base_deploy import templates_of

    key = d["key"]
    with transaction.atomic():
        # commit-413: one profile per base-OS type is checked below - saves take the always-present host profile's lock
        # first, so two creates of the same type with different keys are made one after the other
        ServiceProfile.objects.select_for_update().filter(stype=ServiceProfile.HOST).order_by("pk").first()
        p = None if new else ServiceProfile.objects.select_for_update().filter(pk=pk).first()
        if not new and p is None:
            raise ServiceError(_("This profile does not exist."))
        if ServiceProfile.objects.filter(key=key).exclude(pk=p.pk if p else None).exists():
            raise ServiceError(_("Profile %(k)s already exists.") % {"k": key})
        if p is not None and p.stype != d["stype"]:  # commit-298: what a profile makes is fixed at its creation
            raise ServiceError(_("A profile's type cannot be changed: add a profile for %(t)s.") % {"t": d["stype"]})
        if p is not None and p.os != d["os"]:  # Q5: and its OS
            raise ServiceError(_("A profile's OS cannot be changed: add a profile."))
        # T36 (rules v2 C4): a type may have several services (kvm-mgmt … kvm-shared-gpu, ns-primary/secondary/site);
        # the type's own one (ServiceProfile.of_type) stays the one the older code paths read
        p = p or ServiceProfile()
        p.key, p.os = key, d["os"]
        p.name, p.stype = d["name"], d["stype"]
        p.vcpu, p.ram_gb, p.disk_gb, p.vms = d.get("vcpu"), d.get("ram_gb"), d.get("disk_gb"), d.get("vms")
        _check_minimum(p)
        p.checks, p.notes = d.get("checks") or [], d.get("notes") or ""
        if "dns_slot" in d:  # commit-507: name servers only
            p.dns_slot = d.get("dns_slot") if p.stype == "ns" else None
            other = (
                ServiceProfile.objects.filter(stype="ns", dns_slot=p.dns_slot).exclude(pk=p.pk).first()
                if p.dns_slot is not None
                else None
            )
            if other is not None:  # commit-514: a slot is one profile's
                raise ServiceError(_("DNS slot %(n)s is %(p)s's.") % {"n": p.dns_slot, "p": other.key})
        if "host_slot" in d:  # T36 B4a: host services only; a slot is one profile's
            from .models import Host

            was = p.host_slot
            p.host_slot = (d.get("host_slot") or "") if p.stype == ServiceProfile.HOST else ""
            if p.pk and p.host_slot != was and Host.objects.filter(service=p).exists():
                # commit-523: the hosts made from it keep what they are - its slot is fixed while it has hosts
                raise ServiceError(_("Hosts are made from this profile: its host slot cannot change."))
            other = (
                ServiceProfile.objects.filter(stype=ServiceProfile.HOST, host_slot=p.host_slot).exclude(pk=p.pk).first()
                if p.host_slot
                else None
            )
            if other is not None:
                raise ServiceError(_("Host slot %(n)s is %(p)s's.") % {"n": p.host_slot, "p": other.key})
        if "roles" in d:  # T36 B2d: the roles, where it is used and placed
            p.roles = parse_roles(d.get("roles"), p.os, p.roles)
        if p.roles and p.stype != ServiceProfile.HOST:  # commit-493: one count - the VMs are the roles' added up
            total = sum(int(r.get("count") or 0) for r in p.roles)
            zero = [r.get("name") for r in p.roles if int(r.get("count") or 0) < 1]
            if zero:  # count 0 ("any") is a host role's only: each role of a VM service has its own number
                raise ServiceError(
                    _("Roles: %(r)s - each role of a VM service has at least 1 VM.") % {"r": ", ".join(zero)}
                )
            if total > 20:  # commit-495: the VMs field's own bound
                raise ServiceError(_("Roles: they add up to %(t)s VMs; a service has 20 at most.") % {"t": total})
            if p.vms is None:
                p.vms = total
            elif p.vms != total:
                raise ServiceError(
                    _("VMs: %(v)s, but its roles add up to %(t)s - make them the same.") % {"v": p.vms, "t": total}
                )
        if d.get("usage"):
            p.usage = d["usage"]
        if "placement" in d:
            p.placement = (d.get("placement") or "").strip()
        # T34 S2d: the installation and configs from Templates; a profile that cannot make its script is not saved
        p.install_template = d.get("install_template") or ""
        p.configs, p.nics = d.get("configs") or [], d.get("nics") or []
        tpls = templates_of(p)
        inst = tpls.get(p.install_template) if p.install_template else None
        if p.is_vyos and p.stype not in VYOS_STYPES:  # T36 C5: a project gateway - its VyOS template comes with B3
            if p.configs or p.checks:
                raise ServiceError(_("A VyOS service profile has no config files: its VyOS template is its config."))
        elif p.stype in VYOS_STYPES:  # T34 S3b: its VyOS template; no configs, no first-boot script
            if inst is None or inst.kind != inst.Kind.NETWORK or inst.os != inst.Os.VYOS:
                raise ServiceError(
                    _("A VyOS service profile names a VyOS template: %(t)s.") % {"t": p.install_template}
                )
            if p.install_template not in VYOS_TEMPLATES[p.stype]:  # commit-420: the one its services are made with
                raise ServiceError(
                    _("A %(s)s service is made with %(l)s: its profile names one of them.")
                    % {"s": p.stype, "l": ", ".join(VYOS_TEMPLATES[p.stype])}
                )
            if p.configs or p.checks:  # U8: no Ubuntu part (config files, checks)
                raise ServiceError(_("A VyOS service profile has no config files: its VyOS template is its config."))
        else:
            if p.install_template and (inst is None or inst.kind not in inst.INSTALL_KINDS):
                raise ServiceError(_("%(t)s is not an installation template.") % {"t": p.install_template})
            try:
                recipes.render_script(p, tpls, recipes.sample_values(p))
            except recipes.RecipeError as exc:
                raise ServiceError(str(exc)) from exc
        if any(logging_views.is_logging_file(c.get("path")) for c in p.configs or [] if isinstance(c, dict)):
            # owner 2026-11-24 09:13: "remove from configs any syslog and logrotate templates" - Profiles → Logging
            raise ServiceError(
                _("rsyslog and logrotate files come from the logged services (Profiles → OS and Functions).")
            )
        # owner 09:13: Base OS - common and the ones picked; VyOS - vyos only; commit-444: checked before it is saved
        logged = logging_views.profile_logged(p, d.get("logged") or [])
        try:
            logging_views.check_sets(p, logged)
        except logconf.LogConfError as exc:
            raise ServiceError(_("Logged services: %(e)s") % {"e": exc}) from exc
        p.save()
        p.logged.set(logged)
    audit(user, "service_profile_saved", p, key=key, new=new, profile_id=p.pk, logged=[x.name for x in p.logged.all()])
    return p


def _form_errors(request, form):
    for f, errs in form.errors.items():
        messages.error(request, f"{form.fields[f].label if f in form.fields else ''}: {' '.join(errs)}")


@staff_required(super_only=True)
def profile_edit_view(request, pk=None):
    """Edit a service profile - a page, Save and Cancel on top (R8, ui-16); GET shows it, POST saves it. Found by its id
    (owner 2026-11-23 U4), its Name editable (U5)."""
    p = None
    if pk is not None:
        p = ServiceProfile.objects.filter(pk=pk).first()
        if p is None:
            messages.error(request, _("This profile does not exist."))
            return redirect(reverse("infra_services"))
    if request.method == "POST":
        form = ProfileForm(request.POST)
        if p is not None:  # disabled fields are not posted: the stored type and OS
            form.data = form.data.copy()
            form.data["stype"], form.data["os"] = p.stype, p.os
        if form.is_valid():
            try:
                saved = save_profile(request.user, p.pk if p else None, form.cleaned_data, new=p is None)
                messages.success(request, _("Profile saved."))
                return redirect(reverse("infra_services") + f"?sel=sp-{saved.pk}")
            except ServiceError as exc:
                messages.error(request, str(exc))
        else:
            _form_errors(request, form)
    else:
        os_ = request.GET.get("os") if request.GET.get("os") in ServiceProfile.Os.values else "base-os"
        form = ProfileForm(
            initial=profile_initial(p) if p else {"os": os_, "stype": "bgp-rr" if os_ == "vyos" else "ns"}
        )
    if p is not None:  # fixed once made (Q5): only its own OS shown
        form.fields["stype"].disabled = True
        form.fields["os"].disabled = True
        form.fields["os"].choices = [c for c in form.fields["os"].choices if c[0] == p.os]
    return render(
        request,
        "inventory/service_profile_edit.html",
        {
            "p": p,
            "form": form,
            "type_label": type_label(p.stype) if p else "",
            "instances": _instances(p) if p else [],
            "vyos_types_json": json.dumps(list(VYOS_STYPES) + list(VYOS_PROJECT_STYPES)),
            "logged_rows": _logged_rows(form),
        },
    )


def _logged_rows(form):
    """(available, chosen) of the two-list picker: [(id, name)], from the form's choices and its value."""
    chosen = [str(v) for v in (form["logged"].value() or [])]
    choices = list(form.fields["logged"].choices)
    return [c for c in choices if c[0] not in chosen], [c for c in choices if c[0] in chosen]


def _instances(p):
    """The site services made from the profile (R11: the last card): [{site, name, vms, state}]; a host profile: none
    (every KVM host is made from it)."""
    from .models import CoreService

    if p.stype == ServiceProfile.HOST:
        return []
    # commit-510: an unlinked service is the instance of one profile only - the one the portal would make it from (as
    # migration 0122 attributes it: unlinked_profile)
    rows = CoreService.objects.filter(Q(profile=p) | Q(profile=None, stype=p.stype)).select_related("site")
    out = [s for s in rows if s.profile_id == p.pk or getattr(unlinked_profile(s), "pk", None) == p.pk]
    return sorted(out, key=lambda s: (s.site.code, s.name))


GW_TEMPLATES = {"cloud-egw": "egw", "cloud-igw": "igw", "cloud-gw": "igw", "portal-gw": "egw"}


def layout_counts(instances):
    """commit-511: the VM counts of the services, those with no VM left out (no layout to match)."""
    return sorted({s.nodes.count() for s in instances} - {0})


def unlinked_profile(svc):
    """commit-510: the profile a site service saved without a link counts for - the rule of migration 0122
    (_unlinked_for) and of core_types.default_profile: its type's own profile (ServiceProfile.of_type); a VyOS one's
    among those of the template its definition calculates (a route reflector: EVPN rr-vxlan, any other exchange rr-ix;
    a gateway GW_TEMPLATES); a reflector whose network is gone, none."""
    from .models import Network

    qs = ServiceProfile.objects.filter(stype=svc.stype)
    if svc.stype == "bgp-rr":
        net = Network.objects.filter(pk=(svc.params or {}).get("network") or 0).first()
        if net is None or net.kind not in ("ix", "evpn"):
            return None
        qs = qs.filter(install_template="rr-vxlan" if net.kind == "evpn" else "rr-ix")
    elif svc.stype in GW_TEMPLATES:
        qs = qs.filter(install_template=GW_TEMPLATES[svc.stype])
    return ServiceProfile.of_type(svc.stype, qs)


def _made_from(p):
    """The site services made from the profile: linked to it (T34 S3b), or - saved without a link - of its base-OS
    type (that type has one profile)."""
    from django.db.models import Q

    q = Q(profile=p)
    if not p.is_vyos:
        q |= Q(profile=None, stype=p.stype)
    return q


@require_POST
@staff_required(super_only=True)
def profile_delete_view(request, pk):
    from .models import CoreService

    p = ServiceProfile.objects.filter(pk=pk).first()
    if p is None:
        messages.error(request, _("This profile does not exist."))
    elif p.stype == ServiceProfile.HOST or CoreService.objects.filter(_made_from(p)).exists():
        messages.error(request, _("Profile %(k)s makes machines that exist.") % {"k": p.key})
    else:
        key = p.key
        p.delete()
        audit(request.user, "service_profile_deleted", None, key=key, profile_id=pk)
        messages.success(request, _("Profile deleted."))
    return redirect(reverse("infra_services"))
