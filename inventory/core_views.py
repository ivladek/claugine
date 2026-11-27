"""Infrastructure → site → Core services: the list, the form, reading (a Task) and the standard layout."""

import ipaddress

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from accounts.models import Role, audit
from accounts.utils import staff_required

from . import core_services as cs
from . import core_types as ct
from .models import ConfigTemplate, CoreService, CoreServiceNode, Network, Platform, Site
from .network_views import rows_widget

NAME = r"^[a-z0-9][a-z0-9-]{0,59}$"


MAX_NICS = 4  # NIC 1 plus up to three further NICs (the VMs table has one address column per NIC)


class ServiceForm(forms.Form):
    """The definition of a core service: name, description, the platform hosting its VMs and the one it serves; one row
    of VM resources (NIC 1 included) and rows for further NICs; routes; its VMs with an address per NIC (empty: assigned
    when the VM is made); the ssh and the service secret; ufw rules (not for a VyOS image); template, differences, checks."""

    name = forms.RegexField(
        label=_l("Name"), regex=NAME, max_length=60, help_text=_l("A short name without spaces, e.g. cloud-vxlan-rrs.")
    )
    kind = forms.ChoiceField(label=_l("Type"), choices=[("vm", "vm"), ("host", "host")])
    state = forms.ChoiceField(label=_l("State"), choices=CoreService.State.choices, initial="planned")
    description = forms.CharField(label=_l("Description"), required=False, widget=forms.Textarea(attrs={"rows": 2}))
    platform = forms.ChoiceField(label=_l("Platform hosting the VMs"), required=False)
    serves = forms.ChoiceField(
        label=_l("Platform served"),
        required=False,
        help_text=_l("The platform the service contributes to; core: a common service of the site."),
    )
    vcpu = forms.IntegerField(label=_l("vCPU"), min_value=1, max_value=256, required=False)
    ram_gb = forms.IntegerField(label=_l("RAM, GB"), min_value=1, max_value=4096, required=False)
    disk_gb = forms.IntegerField(label=_l("Disk, GB"), min_value=1, max_value=100000, required=False)
    image = forms.ChoiceField(label=_l("Image"), required=False)
    ssh_port = forms.IntegerField(label=_l("ssh port"), min_value=1, max_value=65535, initial=22)
    nic1_network = forms.ChoiceField(label=_l("NIC 1 subnet"), required=False)
    nic1_gateway = forms.GenericIPAddressField(label=_l("NIC 1 gateway"), protocol="IPv4", required=False)
    more_nics = forms.CharField(
        label=_l("Further NICs"), required=False, widget=forms.Textarea(attrs={"rows": 2, "class": "mono"})
    )
    routes = forms.CharField(
        label=_l("Routes"), required=False, widget=forms.Textarea(attrs={"rows": 2, "class": "mono"})
    )
    nodes = forms.CharField(
        label=_l("VMs"),
        widget=forms.Textarea(attrs={"rows": 3, "class": "mono"}),
        help_text=_l(
            "The VM's name on the platform, its host name and its address on each NIC (empty: "
            "assigned when the VM is made). On hosts: the host's name, host name and address."
        ),
    )
    ssh_secret = forms.ChoiceField(
        label=_l("Secret for ssh"),
        required=False,
        help_text=_l("The user secret the portal logs in to every host and VM of it with."),
    )
    service_secret = forms.ChoiceField(
        label=_l("Secret for the service"),
        required=False,
        help_text=_l("The secret the service itself uses, e.g. a BGP password."),
    )
    firewall = forms.CharField(
        label=_l("Firewall (ufw)"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 2, "class": "mono"}),
        help_text=_l("Not for a VyOS image (its firewall is in its template)."),
    )
    template = forms.ChoiceField(
        label=_l("Template to apply"),
        required=False,
        help_text=_l("The template its configuration is made from (Infrastructure → Templates)."),
    )
    config_diff = forms.CharField(
        label=_l("Differences from the template"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 4, "class": "mono"}),
        help_text=_l(
            "set/delete lines applied after the template: what this service has beyond "
            "the standard configuration. No secrets: those come from Secrets."
        ),
    )
    check_units = forms.CharField(
        label=_l("Services running"),
        required=False,
        max_length=300,
        help_text=_l("systemd units that must be active on every host/VM, e.g. named mysql"),
    )
    check_ports = forms.CharField(
        label=_l("Ports listening"),
        required=False,
        max_length=200,
        help_text=_l("TCP ports that must listen, e.g. 53 2633"),
    )
    check_ntp = forms.BooleanField(label=_l("Time synchronized (NTP)"), required=False)
    check_bgp = forms.ChoiceField(
        label=_l("BGP peers established"),
        required=False,
        choices=[("", "—"), ("ipv4", "IPv4 unicast"), ("evpn", "L2VPN EVPN")],
    )
    check_vip = forms.CharField(
        label=_l("Floating address"),
        required=False,
        max_length=45,
        help_text=_l(
            "The address the leader holds: an address, or a host number in the subnet "
            "of each node's address (e.g. 30). Exactly one node must hold it."
        ),
    )
    check_leader_units = forms.CharField(
        label=_l("Services running on the leader only"),
        required=False,
        max_length=300,
        help_text=_l("e.g. opennebula-gate opennebula-flow"),
    )
    check_leader_ports = forms.CharField(
        label=_l("Ports listening on the leader only"), required=False, max_length=200, help_text=_l("e.g. 5030 2474")
    )

    RESOURCES = ("vcpu", "ram_gb", "disk_gb", "image", "ssh_port", "nic1_network", "nic1_gateway")
    CHECKS = (
        "check_units",
        "check_ports",
        "check_ntp",
        "check_bgp",
        "check_vip",
        "check_leader_units",
        "check_leader_ports",
    )

    def __init__(self, *args, site=None, keep=None, **kwargs):
        super().__init__(*args, **kwargs)
        from .credentials import ref_choices
        from .models import Secret, ServiceImage

        self.site = site
        # commit-377: a site service is made from a network template; the service profiles' config and installation
        # templates are theirs (the choices also validate the post)
        choices = [(str(t.pk), t.name) for t in ConfigTemplate.objects.filter(kind=ConfigTemplate.Kind.NETWORK)]
        # commit-379: the service's own template, when it is not a network one, stays shown and kept while unchanged
        # (an unrelated edit must not clear it); another choice is a network template or none
        if keep is not None and keep.kind != ConfigTemplate.Kind.NETWORK:
            choices.insert(0, (str(keep.pk), _("%(n)s - not a network template") % {"n": keep.name}))
        self.fields["template"].choices = [("", "—")] + choices
        self.fields["ssh_secret"].choices = [("", "—")] + ref_choices("account")
        self.fields["service_secret"].choices = [("", "—")] + [
            (x.ref, f"{x.name} ({x.get_kind_display()})")
            for x in Secret.objects.filter(state=Secret.State.ACTIVE).order_by("name")
        ]
        plats = [(str(p.pk), p.key) for p in Platform.objects.filter(site__code=site).order_by("key")]
        self.fields["platform"].choices = [("", "—")] + plats
        self.fields["serves"].choices = [("", _("core (common)"))] + plats
        imgs = list(
            ServiceImage.objects.filter(control__key__startswith=f"{site}.", kind="image")
            .select_related("control")
            .order_by("control__key", "position")
        )
        self.fields["image"].choices = [("", "—")] + [(str(i.pk), f"{i.control.key} · {i.name}") for i in imgs]
        self.vyos_images = " ".join(str(i.pk) for i in imgs if i.customization == ServiceImage.Customization.CONTEXT)
        self.nets = list(Network.objects.filter(site=site).order_by("name"))
        self.fields["nic1_network"].choices = [("", "—")] + [(str(n.pk), f"{n.name} · {n.cidr}") for n in self.nets]
        rows_widget(
            self.fields["more_nics"],
            [
                ("network", _("Subnet"), "", "", [n.name for n in self.nets]),
                ("gateway", _("Gateway"), _("empty: the subnet's")),
            ],
            _("NIC"),
        )
        rows_widget(
            self.fields["nodes"],
            [("name", _("VM name"), "cloud-ns1_dc1"), ("hostname", _("Host name"), "ns1")]
            + [(f"ip{i}", f"NIC {i} IP", "auto" if i == 1 else "", "9em") for i in range(1, MAX_NICS + 1)],
            _("VM"),
            sep="|",
        )
        rows_widget(
            self.fields["routes"],
            [
                ("nic", "NIC", "1", "4em", [str(i) for i in range(1, MAX_NICS + 1)]),
                ("subnet", _("Subnet"), "10.0.0.0/8"),
                ("via", _("Next hop"), "10.0.0.1"),
                ("metric", _("Metric"), "20", "6em"),
            ],
            _("Route"),
        )
        rows_widget(
            self.fields["firewall"],
            [
                ("port", _("Port"), "22", "7em"),
                ("proto", _("Protocol"), "", "7em", ["tcp", "udp", "any"]),
                ("source", _("From"), "10.0.0.0/8"),
            ],
            _("Rule"),
        )

    @property
    def resource_fields(self):
        return [self[n] for n in self.RESOURCES]

    @property
    def check_fields(self):
        return [self[n] for n in self.CHECKS]

    def clean(self):
        d = super().clean()
        by_name = {n.name: n for n in self.nets}
        nics = []
        for line in (d.get("more_nics") or "").splitlines():
            parts = line.split()
            if not parts:
                continue
            n = by_name.get(parts[0])
            gw = parts[1] if len(parts) > 1 else ""
            try:
                if n is None:
                    raise ValueError(parts[0])
                if gw:
                    ipaddress.IPv4Address(gw)
            except ValueError:
                self.add_error("more_nics", _("Not a NIC: %(l)s") % {"l": line})
                continue
            nics.append({"network": n.pk, "gateway": gw})
        if len(nics) + 1 > MAX_NICS:
            self.add_error("more_nics", _("At most %(n)s NICs.") % {"n": MAX_NICS})
        d["nic_list"] = nics
        count = 1 + len(nics) if d.get("nic1_network") else 0
        rows = []
        for line in (d.get("nodes") or "").splitlines():
            parts = [x.strip() for x in line.split("|")] if "|" in line else line.split()
            if not any(parts):
                continue
            if "|" not in line and len(parts) == 2:  # name and address
                parts = [parts[0], "", parts[1]]
            name, hostname = parts[0], (parts[1] if len(parts) > 1 and parts[1] != "-" else "")
            ips = [x if x.lower() != "auto" else "" for x in parts[2 : 2 + MAX_NICS]]
            while ips and not ips[-1]:
                ips.pop()
            bad = [x for x in ips if x and not _is_ip(x)]
            if not name or " " in name + hostname or bad:
                self.add_error("nodes", _("Not a VM row: %(l)s") % {"l": line})
                continue
            if d.get("kind") == "host" and not ips:
                self.add_error("nodes", _("A host needs its address: %(l)s") % {"l": line})
                continue
            if count and len(ips) > count:
                self.add_error("nodes", _("%(n)s has more addresses than the service has NICs.") % {"n": name})
                continue
            rows.append((name, hostname, ips))
        if not rows and not self.errors.get("nodes"):
            self.add_error("nodes", _("At least one host or VM."))
        d["node_rows"] = rows
        routes = []
        for line in (d.get("routes") or "").splitlines():
            parts = line.split()
            if not parts:
                continue
            if not parts[0].isdigit():
                parts = ["1"] + parts
            try:
                ipaddress.ip_network(parts[1], strict=False)
                if len(parts) > 2:
                    ipaddress.ip_address(parts[2])
                metric = int(parts[3]) if len(parts) > 3 else None
                if metric is not None and not 0 <= metric <= 4294967295:
                    raise ValueError(metric)
                if not 1 <= int(parts[0]) <= MAX_NICS:
                    raise ValueError(parts[0])
            except (ValueError, IndexError):
                self.add_error("routes", _("Not a route: %(l)s") % {"l": line})
                continue
            r = {"nic": int(parts[0]), "subnet": parts[1]}
            if len(parts) > 2:
                r["via"] = parts[2]
            if metric is not None:
                r["metric"] = metric
            routes.append(r)
        if count and any(r["nic"] > count for r in routes):
            self.add_error("routes", _("A route on a NIC the service does not have."))
        d["route_list"] = routes
        fw = []
        for line in (d.get("firewall") or "").splitlines():
            parts = line.split()
            if not parts:
                continue
            port, proto, source = (
                parts[0],
                (parts[1] if len(parts) > 1 else "tcp"),
                (parts[2] if len(parts) > 2 else ""),
            )
            try:
                if not all(p.isdigit() and 1 <= int(p) <= 65535 for p in port.replace(":", ",").split(",")):
                    raise ValueError(port)
                if proto not in ("tcp", "udp", "any"):
                    raise ValueError(proto)
                if source:
                    ipaddress.ip_network(source, strict=False)
            except ValueError:
                self.add_error("firewall", _("Not a rule: %(l)s") % {"l": line})
                continue
            fw.append({"port": port, "proto": proto, **({"source": source} if source else {})})
        d["fw_list"] = fw
        found = {}
        for field in ("check_ports", "check_leader_ports"):
            ports = (d.get(field) or "").split()
            if any(not p.isdigit() or not 1 <= int(p) <= 65535 for p in ports):
                self.add_error(field, _("Ports are numbers from 1 to 65535."))
            found[field] = [int(p) for p in ports if p.isdigit()]
        for field in ("check_units", "check_leader_units"):
            units = (d.get(field) or "").split()
            if any(not __import__("re").match(r"^[A-Za-z0-9@._-]+$", u) for u in units):
                self.add_error(field, _("Unit names only (letters, digits, @ . _ -)."))
            found[field] = units
        vip = (d.get("check_vip") or "").strip()
        if vip:
            try:
                ok = 0 < int(vip) < 2**16 if vip.isdigit() else bool(ipaddress.IPv4Address(vip))
            except ValueError:
                ok = False
            if not ok:
                self.add_error("check_vip", _("An IPv4 address or a host number."))
        if (found["check_leader_units"] or found["check_leader_ports"]) and not vip:
            self.add_error("check_vip", _("Required to tell the leader."))
        d["check_dict"] = {
            k: v
            for k, v in (
                ("units", found["check_units"]),
                ("ports", found["check_ports"]),
                ("ntp", bool(d.get("check_ntp"))),
                ("bgp", d.get("check_bgp") or ""),
                ("vip", vip),
                ("leader_units", found["check_leader_units"]),
                ("leader_ports", found["check_leader_ports"]),
            )
            if v
        }
        bad = [
            ln
            for ln in (d.get("config_diff") or "").splitlines()
            if ln.strip() and not ln.strip().startswith(("set ", "delete ", "#"))
        ]
        if bad:
            self.add_error("config_diff", _("Only set and delete lines: %(l)s") % {"l": bad[0][:80]})
        if any(
            __import__("re").search(r"(password|private-key|pre-shared-secret) ", ln)
            for ln in (d.get("config_diff") or "").splitlines()
        ):
            self.add_error("config_diff", _("No secrets here: they come from Secrets."))
        if d.get("kind") == "vm":
            if not d.get("nic1_network"):
                self.add_error("nic1_network", _("Required for a service made from scratch."))
            if not d.get("platform"):
                self.add_error("platform", _("Required for a service made from scratch."))
            elif d.get("image"):
                from .models import ServiceImage

                img = ServiceImage.objects.filter(pk=d["image"]).select_related("control").first()
                p = Platform.objects.filter(pk=d["platform"]).first()
                if img and p and img.control.key != p.key:
                    self.add_error("image", _("The image is not on the platform hosting the VMs."))
        return d


def _is_ip(x):
    try:
        ipaddress.ip_address(x)
        return True
    except ValueError:
        return False


def initial(svc):
    nodes = list(svc.nodes.all())
    nets = {n.pk: n.name for n in Network.objects.filter(site=svc.site.code)}
    return {
        "name": svc.name,
        "kind": svc.kind,
        "state": svc.state,
        "description": svc.description,
        "platform": str(svc.platform_id or ""),
        "serves": str(svc.serves_id or ""),
        "template": str(svc.template_id or ""),
        "config_diff": svc.config_diff,
        "nodes": "\n".join(
            " | ".join([n.name, n.hostname] + [x or "" for x in (n.ips or ([n.address] if svc.kind == "host" else []))])
            for n in nodes
        ),
        "ssh_port": svc.ssh_port,
        "ssh_secret": svc.ssh_secret,
        "service_secret": svc.service_secret,
        "image": str(svc.image_id or ""),
        "vcpu": svc.vcpu,
        "ram_gb": svc.ram_gb,
        "disk_gb": svc.disk_gb,
        "nic1_network": str(svc.nic1_network_id or ""),
        "nic1_gateway": svc.nic1_gateway or "",
        "more_nics": "\n".join(
            f"{nets.get(int(x['network']), '?')} {x.get('gateway') or ''}".strip() for x in svc.more_nics or []
        ),
        "routes": "\n".join(
            " ".join(str(x) for x in (r.get("nic", 1), r["subnet"], r.get("via", ""), r.get("metric", "")) if x != "")
            for r in svc.routes or []
        ),
        "firewall": "\n".join(
            " ".join(x for x in (r.get("port", ""), r.get("proto", "tcp"), r.get("source", "")) if x)
            for r in svc.firewall or []
        ),
        **_check_initial(cs.checks_of(svc)),
    }


def _check_initial(c):
    return {
        "check_units": " ".join(c.get("units", [])),
        "check_ports": " ".join(str(p) for p in c.get("ports", [])),
        "check_ntp": bool(c.get("ntp")),
        "check_bgp": c.get("bgp", ""),
        "check_vip": str(c.get("vip", "")),
        "check_leader_units": " ".join(c.get("leader_units", [])),
        "check_leader_ports": " ".join(str(p) for p in c.get("leader_ports", [])),
    }


def _deployed(key, vm):
    """A VM of the last platform read, for the page."""
    return {
        "platform": key,
        "id": vm.get("id"),
        "cluster": vm.get("cluster", ""),
        "host": vm.get("host", ""),
        "status": vm.get("status") or cs.vm_status(vm.get("state", ""), vm.get("lcm_state", "")),
        "nic1": next((ip for _net, ip in vm.get("nics") or [] if ip), ""),
        "nics": vm.get("nics") or [],
        "vcpu": vm.get("vcpu"),
        "ram_gb": vm.get("ram_gb"),
        "disks_gb": vm.get("disks_gb") or [],
        "images": [i for i in vm.get("images") or [] if i],
        "init_script": vm.get("init_script"),
    }


def _busy(site):
    """Nodes of the site with a redeploy, roll back or removal queued or running."""
    from accounts.models import Task

    ids = Task.objects.filter(
        kind="core VM redeploy", target_type="coreservicenode", state__in=[Task.State.QUEUED, Task.State.RUNNING]
    ).values_list("target_id", flat=True)
    out = {int(i) for i in ids if str(i).isdigit()}
    # T34 B6: a front end's Restore all holds every node of its service
    svcs = Task.objects.filter(
        kind=RESTORE_KIND, target_type="coreservice", state__in=[Task.State.QUEUED, Task.State.RUNNING]
    ).values_list("target_id", flat=True)
    out |= set(
        CoreServiceNode.objects.filter(service__pk__in=[int(i) for i in svcs if str(i).isdigit()]).values_list(
            "pk", flat=True
        )
    )
    return out


RESTORE_KIND = "core FE restore"  # T34 B6 (fe_deploy.RESTORE_KIND)


def context(site, can_edit):
    """The Core services tab."""
    from .models import PlatformControl
    from .snapd import removable as snapd_removable

    busy = _busy(site)
    services = list(
        CoreService.objects.filter(site__code=site)
        .select_related("template", "platform", "serves", "image__control", "nic1_network", "profile")
        .prefetch_related("nodes__host")
    )
    vms = cs.site_vms(site)
    from .credentials import find
    from .hostlogging import restart_names, restarts_of
    from .models import HostService, LoggedService

    by_key = {h.key: h for h in HostService.objects.all()}
    logged = {x.name: x for x in LoggedService.objects.all()}  # owner 2026-11-24: the logged services found
    from . import base_deploy as bd
    from .models import ServiceProfile

    profiles = {}
    for p in ServiceProfile.objects.order_by("pk"):  # T36: the type's own one (as ServiceProfile.of_type)
        if p.stype not in profiles or ((profiles[p.stype].legacy or {}).get("t36") or {}).get("made"):
            if p.stype not in profiles or not ((p.legacy or {}).get("t36") or {}).get("made"):
                profiles[p.stype] = p
    for s in services:
        s.node_list = list(s.nodes.all())
        for n in s.node_list:  # owner 11:58 "service VMs now": the services found and what Apply would restart
            found = [logged[k] for k in n.services or [] if k in logged]
            n.restarts, n.restart_names = restarts_of(found), restart_names(found)  # commit-285 (keys sent)
        s.nics = s.nic_list()
        s.problems = cs.mismatches(s)
        s.read_ok = sum(1 for n in s.node_list if n.facts_at and not n.facts_error)
        s.read_bad = sum(1 for n in s.node_list if n.facts_error)
        s.checks_run = [c for n in s.node_list for c in (n.facts or {}).get("checks", []) if not c.get("role")]
        s.checks_failed = [c for c in s.checks_run if not c["ok"]]
        s.leaders = cs.leaders(s)
        if s.leaders is not None:
            one = {"check": "exactly one leader", "ok": len(s.leaders) == 1, "detail": ", ".join(s.leaders) or "none"}
            s.checks_run.append(one)
            if not one["ok"]:
                s.checks_failed.append(one)
        s.check_def = cs.checks_of(s)
        role = s.template.role if s.template else ""
        s.vyos_tpl = role.startswith("rr-") or role in ("egw", "igw")
        s.redeployable = role.startswith("rr-") or s.stype in (
            CoreService.Type.CLOUD_GW,
            CoreService.Type.PORTAL_GW,
        )  # corrections 11, 12
        s.vyos_image = bool(s.image and s.image.customization == "context")
        s.diff_lines = len(
            [ln for ln in (s.config_diff or "").splitlines() if ln.strip().startswith(("set ", "delete "))]
        )
        net = (
            s.service_network() if s.vyos_tpl else None
        )  # BGP passwords of route reflectors and gateways: the network's
        s.peer_groups = list(net.peer_groups.all()) if net is not None and net.has_bgp else []
        s.peer_net = net
        s.ssh_secret_obj, s.service_secret_obj = (
            find(s.ssh_secret) if s.ssh_secret else None,
            find(s.service_secret) if s.service_secret else None,
        )
        for n in s.node_list:  # the definition (addresses per NIC) and what the platform runs
            n.def_ips = [
                (i + 1, (s.nics[i][0].name if i < len(s.nics) and s.nics[i][0] else "?"), ip)
                for i, ip in enumerate((n.ips or []) + [""] * max(0, len(s.nics) - len(n.ips or [])))
            ]
            n.failed_checks = [c for c in (n.facts or {}).get("checks", []) if not c["ok"] and not c.get("role")]
            key, vm = vms.get(n.name, (None, None))
            n.deployed = vm and _deployed(key, vm)
            okey, ovm = vms.get(f"{n.name}-old", (None, None))
            n.old_vm = ovm and _deployed(okey, ovm)
            n.busy = n.pk in busy
            # the table shows the VM's NIC 1 address as the platform has it; a host's (or a VM not read yet) its own
            n.nic1_ip = (n.deployed["nic1"] if n.deployed else "") or (
                (n.ips or [""])[0] if s.kind == "vm" else n.address
            )
        # T34 B4: a base-OS service is an instance of its service profile; Redeploy is offered when it gives every value
        if not s.profile_id and s.stype in bd.TYPES:  # T34 S3b: the link; a service saved before it: the type's
            s.profile = profiles.get(s.stype)
        s.base_profile = s.stype in bd.TYPES and s.profile is not None  # its facts: own secrets, latest backup
        s.profile_parts = (
            [by_key[k].name if k in by_key else k for k in (s.profile.services or [])] if s.profile else []
        )
        s.own_secrets = sorted(((s.params or {}).get("secrets") or {}).items())
        s.serves_text = serves_text(s)  # T34 S3a (ui-18): what the service serves, a column of the list
        if s.stype == CoreService.Type.NS:  # owner 11:51: the Redeploy dialog's "make it the primary"
            from .models import DnsSettings

            s.dns_primary_id = DnsSettings.get().primary_id
        for n in s.node_list:
            n.is_base = bd.is_base(n)
            n.rollback_retry = n.is_base and not n.old_vm and bool(n.deployed) and bd.rollback_pending(n, n.deployed)
            n.redeploy_ok, n.redeploy_why = bd.offer(n) if n.is_base and can_edit else (False, [])
        s.restore_ok, s.restore_why, s.latest_backup = False, [], ""
        if s.stype == CoreService.Type.ONEFE:  # T34 B6: Restore all from a backup (the latest in the local folder)
            from . import fe_deploy

            s.restore_ok, s.restore_why, s.latest_backup = fe_deploy.restore_offer(s)
            s.recovery_rec = s.recovery or {}  # commit-350: a Restore all not rolled back completely
            s.recovery_unresolved = fe_deploy.Recovery(s).unresolved() if s.recovery_rec else []  # commit-351
            s.restore_running = any(n.busy for n in s.node_list)
            s.any_busy = any(n.busy or n.old_vm for n in s.node_list)
            if s.restore_running or s.recovery_rec:  # commit-344/350: no node action meanwhile
                for n in s.node_list:
                    n.busy = True
        # T36 B3a: the VMs' stored states (Read marks, a Redeploy makes deployed); why from the last read
        s.noncompliant = sum(1 for n in s.node_list if n.state == CoreService.State.NON_COMPLIANT)
        s.split_ok = s.stype == CoreService.Type.NS and can_edit and not ct.ns_split_why(s)  # commit-506
        for n in s.node_list:
            n.why = cs.compliance(n) if n.state == CoreService.State.NON_COMPLIANT else []
            n.snapd = snapd_removable(n)  # T36 B3c, commit-517: Remove snapd offered when snapd is surely there
        s.typed = s.stype in ct.TYPED
        s.calc, s.calc_error = None, ""
        if s.typed:
            try:
                s.calc = ct.definition(site, s.stype, s.params or {}, existing=s)
            except ct.DefinitionError as exc:
                s.calc_error = str(exc)
        if can_edit:
            if s.typed:
                s.tform = ct.FORMS[s.stype](site=site, svc=s, initial=ct.initial(s), auto_id=f"id_cs_{s.pk}_%s")
            else:
                s.form = ServiceForm(site=site, keep=s.template, initial=initial(s), auto_id=f"id_cs_{s.pk}_%s")
    controls = PlatformControl.objects.filter(key__startswith=f"{site}.").exclude(vms_at=None)
    return {
        "core_services": services,
        "core_vms_at": max((c.vms_at for c in controls), default=None),
        "core_form": ServiceForm(
            site=site, initial={"kind": "vm", "state": "planned", "ssh_port": 22}, auto_id="id_cs_new_%s"
        )
        if can_edit
        else None,
        "core_read_task": _running(site),
        "core_busy": bool(busy),
    }


def serves_text(s):
    """What a site service serves, in a few words (T34 S3a, ui-18): the BGP network of a route reflector, the
    platform of a front end or a portal gateway, the datastore of a backup; "" when it serves the site as a whole."""
    T = CoreService.Type
    p = s.params or {}
    if s.stype == T.BGP_RR:
        net = Network.objects.filter(pk=p.get("network")).first() if p.get("network") else None
        return f"{net.kind.upper()} {net.name}" if net else ""
    if s.stype == T.BACKUP:
        from .models import PlatformDatastore

        ds = PlatformDatastore.objects.filter(pk=p.get("datastore")).select_related("platform").first()
        return _("datastore %(p)s / %(d)s") % {"p": ds.platform.key, "d": ds.name} if ds else ""
    if s.stype == T.CLOUD_GW:
        return _("the core services")
    if s.serves_id:
        return _("platform %(p)s") % {"p": s.serves.key}
    return ""


REFRESH_WHILE_RUNNING = 10  # seconds (owner 2026-10-29 18:38: the statuses refresh only while a task runs)


def site_task_running(site):
    """A task of this site queued or running: a core services read, a redeploy / roll back / removal of one of its
    core service VMs, site settings on its hosts."""
    from accounts.models import Task

    from .models import CoreServiceNode

    st = Site.objects.filter(code=site).first()
    if st is None:
        return False
    live = Task.objects.filter(state__in=[Task.State.QUEUED, Task.State.RUNNING])
    nodes = [str(i) for i in CoreServiceNode.objects.filter(service__site=st).values_list("pk", flat=True)]
    return (
        live.filter(target_type="site", target_id=str(st.pk)).exists()
        or live.filter(target_type="coreservicenode", target_id__in=nodes).exists()
        or live.filter(kind="site settings").exists()
    )


def _running(site):
    from accounts.models import Task

    st = Site.objects.filter(code=site).first()
    return Task.objects.filter(
        kind="core services read",
        target_type="site",
        target_id=str(st.pk) if st else "",
        state__in=[Task.State.QUEUED, Task.State.RUNNING],
    ).first()


def _back(site, sel=None):
    return redirect(f"{reverse('infra_site', args=[site])}?tab=core" + (f"&sel={sel}" if sel else ""))


def _save(svc, d, user, new):
    """commit-572: under the profiles' lock (ServiceProfile.install_lock), as a profile edit."""
    from django.db import transaction

    from .models import ServiceProfile

    with transaction.atomic():
        ServiceProfile.install_lock()
        return _save_locked(svc, d, user, new)


def _save_locked(svc, d, user, new):
    from .models import Host, ServiceImage

    vm = d["kind"] == "vm"
    svc.name, svc.kind, svc.state, svc.description = d["name"], d["kind"], d["state"], d["description"]
    svc.template = ConfigTemplate.objects.filter(pk=d["template"]).first() if d.get("template") else None
    svc.config_diff = "\n".join(ln.rstrip() for ln in (d.get("config_diff") or "").splitlines() if ln.strip())
    svc.ssh_port, svc.ssh_secret, svc.service_secret = (
        d["ssh_port"],
        d.get("ssh_secret") or "",
        d.get("service_secret") or "",
    )
    plats = Platform.objects.filter(site=svc.site)
    svc.platform = plats.filter(pk=d["platform"]).first() if vm and d.get("platform") else None
    svc.serves = plats.filter(pk=d["serves"]).first() if d.get("serves") else None
    svc.image = (
        ServiceImage.objects.filter(pk=d["image"], control__key__startswith=f"{svc.site.code}.").first()
        if vm and d.get("image")
        else None
    )
    svc.vcpu, svc.ram_gb, svc.disk_gb = (d["vcpu"], d["ram_gb"], d["disk_gb"]) if vm else (None, None, None)
    nets = Network.objects.filter(site=svc.site.code)
    svc.nic1_network = nets.filter(pk=d["nic1_network"]).first() if vm and d.get("nic1_network") else None
    svc.nic1_gateway = (d.get("nic1_gateway") or None) if vm else None
    svc.more_nics = d["nic_list"] if vm else []
    svc.routes = d["route_list"] if vm else []
    svc.firewall = (
        d["fw_list"] if not (svc.image and svc.image.customization == ServiceImage.Customization.CONTEXT) else []
    )
    svc.checks = d["check_dict"]
    svc.updated_by = user
    svc.save()
    keep = {}
    from .base_deploy import profile_of

    prof = profile_of(svc)
    for i, (name, hostname, ips) in enumerate(d["node_rows"]):
        n = svc.nodes.filter(name=name).first() or CoreServiceNode(service=svc, name=name, state=svc.state)
        if prof is not None and svc.kind != "host":  # commit-560: its role among the profile's
            prof.give_role(n, i)
        n.hostname, n.ips, n.secret = hostname, ips, svc.ssh_secret
        if svc.kind == "host":
            if n.address != ips[0]:
                n.facts, n.facts_at, n.facts_error = {}, None, ""
            n.address = ips[0]
            n.host = (
                Host.objects.filter(site=svc.site, name=name).first()
                or Host.objects.filter(site=svc.site, ip=ips[0]).first()
            )
        else:
            n.host = None
            if not n.address:  # how the portal logs in: kept as it is for a running VM (a redeploy moves it)
                n.address = next((x for x in ips if x), name)
        if n.pk is None or n.ssh_port == svc.ssh_port or svc.kind == "host":
            n.ssh_port = svc.ssh_port
        n.save()
        keep[n.pk] = name
    gone = list(svc.nodes.exclude(pk__in=keep).values_list("name", flat=True))
    svc.nodes.exclude(pk__in=keep).delete()
    audit(
        user,
        "core_service_saved",
        svc,
        site=svc.site.code,
        service=svc.name,
        new=new,
        kind=svc.kind,
        nodes=", ".join(keep.values()),
        removed=", ".join(gone) or "-",
    )


def fe_busy(svc):
    """commit-352: why a front end may not be edited or deleted now - a Restore all or its rollback starting (a fresh
    claim), running (a task of the service or its nodes) or pending (its recovery record); "" when it may. Called
    under the service row's lock."""
    from . import fe_deploy, hostlogging

    if svc.pk is None or svc.stype != CoreService.Type.ONEFE:
        return ""
    if fe_deploy.pending(svc):
        return _("a Restore all is not rolled back completely: Retry the rollback first")
    rows = list(CoreServiceNode.objects.filter(service=svc).order_by("pk"))
    if rows and _running_node(rows[0]):
        return _("an operation on its VMs runs; see Tasks")
    if any(hostlogging.claim_fresh(r.logging_claim) for r in rows):
        return _("an operation on its VMs is starting")
    return ""


@require_POST
@staff_required(super_only=True)
def save_view(request, code, pk=None):
    from django.db import transaction

    st = get_object_or_404(Site, code=code)
    svc = get_object_or_404(CoreService, pk=pk, site=st) if pk else CoreService(site=st)
    if pk and svc.stype == CoreService.Type.ONEFE:  # commit-352: locked while its VMs are being restored
        with transaction.atomic():
            svc = CoreService.objects.select_for_update().get(pk=svc.pk)
            why = fe_busy(svc)
            if not why:
                return _save_typed(request, code, svc)
        messages.error(request, _("Not saved: %(w)s.") % {"w": why})
        return _back(code, f"cs-{pk}")
    stype = svc.stype if pk else (request.POST.get("stype") or CoreService.Type.CORE_VM)
    if stype in ct.TYPED:  # its own form; the rest is calculated
        return _save_typed(request, code, svc if pk else None, stype)
    form = ServiceForm(request.POST, site=code, keep=svc.template if pk else None)
    if form.is_valid():
        d = form.cleaned_data
        clash = CoreService.objects.filter(site=st, name=d["name"]).exclude(pk=svc.pk).exists()
        if clash:
            messages.error(request, _("A core service %(n)s exists on this site.") % {"n": d["name"]})
        else:
            _save(svc, d, request.user, new=pk is None)
            messages.success(request, _("Core service saved."))
            return _back(code, f"cs-{svc.pk}")
    else:
        for field, errs in form.errors.items():
            for e in errs:
                messages.error(request, f"{form.fields[field].label if field in form.fields else ''}: {e}".lstrip(": "))
    return _back(code, f"cs-{pk}" if pk else None)


def add_choices(code):
    """T34 S3c (ui-19): the service profiles a site service can be added from, each [(profile, why not or "")]: a
    one-per-site type the site has, a route reflector profile with no free network its template serves."""
    from .models import ServiceProfile

    have = set(CoreService.objects.filter(site__code=code, stype__in=ct.SINGLE).values_list("stype", flat=True))
    ns_count = CoreService.objects.filter(site__code=code, stype=CoreService.Type.NS).count()
    out = []
    # commit-491 (T36 C5): only what is used in Site → Services - project automation's never offered (GET and POST)
    for p in ServiceProfile.objects.filter(stype__in=ct.TYPED, usage=ServiceProfile.Usage.SITE).order_by(
        "stype", "key"
    ):
        why = ""
        from .services import VYOS_TEMPLATES

        if p.stype in VYOS_TEMPLATES and p.install_template not in VYOS_TEMPLATES[p.stype]:  # commit-421
            why = _("it names %(r)s; a %(t)s service is made with %(l)s: edit the profile") % {
                "r": p.install_template or "—",
                "t": p.stype,
                "l": ", ".join(VYOS_TEMPLATES[p.stype]),
            }
        elif p.stype in have:
            why = _("the site has its %(t)s service") % {"t": p.stype}
        elif p.stype == CoreService.Type.NS and ns_count >= ct.ns_limit(code):  # T36 B3b (rules v2 C4)
            why = _("the site has its %(n)s name server service(s): the primary site two, every other one") % {
                "n": ct.ns_limit(code)
            }
        elif p.stype == CoreService.Type.NS:  # commit-507: its DNS slot
            why = ct.ns_why(code, p)
        elif p.stype == CoreService.Type.CLAUGINE:  # T36 rules v2 A6
            why = ct.claugine_why(code, ct.mgmt_of(code)[0])
        elif p.stype == CoreService.Type.BGP_RR:
            free = ct.FORMS[p.stype](site=code, profile=p).fields["network"].choices
            if len(free) <= 1:
                why = _("no network of the site without route reflectors for %(r)s") % {"r": p.install_template}
        out.append((p, why))
    return out


@staff_required(super_only=True)
def add_view(request, code):
    """T34 S3c (ui-19, owner 2026-11-22 16:59): Add service - a page (Add and Cancel on top): the service profile
    first, then what its type needs (a route reflector: its IX or EVPN network). Nothing is deployed: each VM is
    deployed from its details."""
    from django.shortcuts import render

    st = get_object_or_404(Site, code=code)
    choices = add_choices(code)
    key = request.POST.get("profile") or request.GET.get("profile") or ""  # U4: the profile's id
    prof = next((p for p, why in choices if str(p.pk) == key and not why), None)
    form = None
    if prof is not None:
        cls = ct.FORMS[prof.stype]
        if request.method == "POST":
            form = cls(request.POST, site=code, profile=prof)
            if form.is_valid():
                try:
                    svc = ct.save(code, prof.stype, form, None, request.user, profile=prof)
                    messages.success(
                        request, _("Service %(n)s added: deploy each of its VMs from its details.") % {"n": svc.name}
                    )
                    return _back(code, f"cs-{svc.pk}")
                except ct.DefinitionError as exc:
                    messages.error(request, str(exc))
        else:
            form = cls(site=code, profile=prof)
    elif key:
        name = next((p.key for p, _why in choices if str(p.pk) == key), key)
        messages.error(request, _("Profile %(k)s cannot be added here.") % {"k": name})
    return render(
        request,
        "inventory/site/core_add.html",
        {
            "site": st,
            "choices": choices,
            "prof": prof,
            "form": form,
            "type_label": prof and dict(CoreService.Type.choices).get(prof.stype),
        },
    )


def _save_typed(request, code, svc, stype=None):
    """A typed core service's form saved (svc None: a new one)."""
    pk = svc.pk if svc is not None else None
    stype = stype or svc.stype
    form = ct.FORMS[stype](request.POST, site=code, svc=svc)
    if form.is_valid():
        try:
            svc = ct.save(code, stype, form, svc, request.user)
            messages.success(request, _("Core service %(n)s saved.") % {"n": svc.name})
            return _back(code, f"cs-{svc.pk}")
        except ct.DefinitionError as exc:
            messages.error(request, str(exc))
    else:
        for field, errs in form.errors.items():
            for e in errs:
                messages.error(request, f"{form.fields[field].label if field in form.fields else ''}: {e}".lstrip(": "))
    return _back(code, f"cs-{pk}" if pk else None)


@require_POST
@staff_required(super_only=True)
def delete_view(request, code, pk):
    from django.db import transaction

    svc = get_object_or_404(CoreService, pk=pk, site__code=code)
    name = svc.name
    with transaction.atomic():  # commit-352: a front end is not deleted while its VMs are being restored
        svc = CoreService.objects.select_for_update().get(pk=svc.pk)
        why = fe_busy(svc)
        if why:
            messages.error(request, _("Not deleted: %(w)s.") % {"w": why})
            return _back(code, f"cs-{pk}")
        return _delete(request, code, svc, name)


def _delete(request, code, svc, name):
    audit(
        request.user,
        "core_service_deleted",
        None,
        site=code,
        service=name,
        nodes=", ".join(svc.nodes.values_list("name", flat=True)),
    )
    svc.delete()
    messages.success(request, _("Core service %(n)s deleted.") % {"n": name})
    return _back(code)


@require_POST
@staff_required(Role.SUPPORT)
def read_view(request, code, pk=None):
    """Read the site's platforms (their own VMs) and the services' hosts and VMs over ssh, read only (a Task)."""
    st = get_object_or_404(Site, code=code)
    from accounts import tasks

    if _running(code):
        messages.info(request, _("Reading is already running."))
        return _back(code, f"cs-{pk}" if pk else None)
    services = [get_object_or_404(CoreService, pk=pk, site=st)] if pk else None
    label = services[0].name if services else _("all core services")
    tasks.start(
        "core services read",
        _("Read core services of %(s)s: %(l)s") % {"s": code, "l": label},
        lambda task: cs.read(task, code, services),
        user=request.user,
        target=st,
    )
    messages.success(request, _("Reading started; see Tasks. It only reads: nothing is changed."))
    return _back(code, f"cs-{pk}" if pk else None)


@require_POST
@staff_required(super_only=True)
def seed_view(request, code):
    """Make the services of the standard layout that the site has and that do not exist yet (from the last read)."""
    get_object_or_404(Site, code=code)
    skipped = []
    made = cs.seed(code, request.user, skipped=skipped)
    for text in skipped:  # commit-412
        messages.warning(request, text)
    if made:
        messages.success(request, _("Core services made: %(l)s.") % {"l": ", ".join(made)})
    else:
        messages.info(
            request,
            _(
                "Nothing to add: every service of the standard layout this site has exists, "
                "or its VMs were not read yet (Read)."
            ),
        )
    return _back(code)


# --- the configuration of a node (template + the service's differences) --------------------------------------------


@staff_required(Role.SUPPORT)
def config_view(request, code, pk, node):
    """What the node's configuration is made of, read only: the template with the site's context and the service's
    differences from it, passwords hidden."""
    from django.shortcuts import render

    from . import core_deploy as cd

    n = get_object_or_404(CoreServiceNode, pk=node, service__pk=pk, service__site__code=code)
    try:
        text, error = cd.masked(cd.config_of(n, reveal=False)), ""
    except Exception as exc:  # noqa: BLE001 - shown on the page
        text, error = "", str(exc)
    diff = [ln for ln in (n.service.config_diff or "").splitlines() if ln.strip()]
    return render(
        request,
        "inventory/core_config.html",
        {
            "site": n.service.site,
            "svc": n.service,
            "node": n,
            "config": text,
            "error": error,
            "diff": diff,
            "diff_count": len([ln for ln in diff if not ln.startswith("#")]),
        },
    )


@staff_required(Role.SUPPORT)
def check_view(request, code, pk):
    """The full check of a service's definition against its running VMs (reads the platform; nothing changes)."""
    from django.shortcuts import render

    from . import base_deploy as bd
    from . import core_deploy as cd

    svc = get_object_or_404(CoreService.objects.select_related("site"), pk=pk, site__code=code)
    rows = []
    for n in svc.nodes.order_by("name"):
        try:
            p = cd.plan(n)
            rows.append(
                {
                    "node": n,
                    "checks": p["checks"],
                    "ok": p["definition_ok"] and p["complete"],
                    "complete": p["complete"],
                    "error": "",
                    "base": bd.is_base(n),
                }
            )
        except Exception as exc:  # noqa: BLE001 - shown on the page
            rows.append(
                {"node": n, "checks": [], "ok": False, "complete": False, "error": str(exc), "base": bd.is_base(n)}
            )
    return render(
        request,
        "inventory/core_check.html",
        {"site": svc.site, "svc": svc, "rows": rows, "all_ok": all(r["ok"] for r in rows)},
    )


# --- redeploying a VM from its template ------------------------------------------------------------------------


@staff_required(super_only=True)
def plan_view(request, code, pk, node):
    """What redeploying this VM would do (reads the platform; nothing changes) and the form to start it."""
    from django.shortcuts import render

    from . import core_deploy as cd

    n = get_object_or_404(CoreServiceNode, pk=node, service__pk=pk, service__site__code=code)
    from . import base_deploy as bd

    if bd.is_base(n):  # T34 B4 (owner 13:01): one confirmation on the VM's details, no plan page
        return _back(code, f"cs-{pk}")
    try:
        p = cd.plan(n)
        error = ""
    except Exception as exc:  # noqa: BLE001 - shown on the page
        p, error = None, str(exc)
    return render(
        request,
        "inventory/core_plan.html",
        {"site": n.service.site, "svc": n.service, "node": n, "plan": p, "error": error, "running": _running_node(n)},
    )


def _held(n):
    """The nodes one operation on n holds: commit-344 - every node of a front end (onefe), whose zone needs the other
    nodes as they are; n alone otherwise."""
    if n.service.stype == CoreService.Type.ONEFE:
        return list(CoreServiceNode.objects.filter(service_id=n.service_id).order_by("pk"))
    return [n]


def _running_node(n):
    from accounts.models import Task

    return (
        Task.objects.filter(
            kind="core VM redeploy",
            target_type="coreservicenode",
            target_id__in=[str(x.pk) for x in _held(n)],  # commit-344: any node of a front end
            state__in=[Task.State.QUEUED, Task.State.RUNNING],
        ).first()
        or Task.objects.filter(  # T34 B6: a Restore all of its front end holds it too
            kind=RESTORE_KIND,
            target_type="coreservice",
            target_id=str(n.service_id),
            state__in=[Task.State.QUEUED, Task.State.RUNNING],
        ).first()
    )


def _start(request, n, kind, title, fn):
    """commit-281: the VM's row locked as Apply logging locks it: refused while Apply holds the VM, and a claim put on it
    until the task exists, so an Apply starting meanwhile is refused too."""
    import secrets

    from django.db import transaction
    from django.utils import timezone

    from accounts import tasks

    from . import fe_deploy, hostlogging

    token = f"lifecycle-{secrets.token_hex(6)}"
    with transaction.atomic():
        # commit-344: a front end's operations are one at a time over the whole service - its row is locked while the
        # nodes are checked and claimed, so two requests for two nodes cannot both pass
        locked = CoreService.objects.select_for_update().get(pk=n.service_id)
        # commit-516: the VM still belongs to that service and the service is made from the profile the request saw
        # (a Split moves a VM and changes the first one's profile) - else refused; the task works on the row read now
        if (
            CoreServiceNode.objects.select_for_update().filter(pk=n.pk).values_list("service_id", flat=True).first()
            != (n.service_id)
            or locked.profile_id != n.service.profile_id
        ):
            messages.error(request, _("The VM's service changed meanwhile: open the page again."))
            return
        n.service = locked
        held = _held(n)
        rows = list(CoreServiceNode.objects.select_for_update().filter(pk__in=[x.pk for x in held]).order_by("pk"))
        if _running_node(n):
            messages.info(
                request,
                _("Something is already running for this VM; see Tasks.")
                if len(held) == 1
                else _("Something is already running for this front end; see Tasks."),
            )
            return
        if n.service.stype == CoreService.Type.ONEFE and fe_deploy.pending(n.service):  # commit-350
            messages.error(
                request, _("A Restore all of this front end is not rolled back completely: Retry the rollback first.")
            )
            return
        if any(hostlogging.claim_fresh(row.logging_claim) for row in rows):
            messages.error(
                request,
                _("Apply logging of this VM is running: try again when it ends.")
                if len(held) == 1
                else _("Another operation on this front end is starting or running: try again when it ends."),
            )
            return
        CoreServiceNode.objects.filter(pk__in=[r.pk for r in rows]).update(
            logging_claim={"token": token, "at": timezone.now().isoformat(), "by": "redeploy"}
        )
    try:
        tasks.start(kind, title, fn, user=request.user, target=n)
    finally:
        with transaction.atomic():
            for row in CoreServiceNode.objects.select_for_update().filter(pk__in=[x.pk for x in held]):
                if (row.logging_claim or {}).get("token") == token:
                    CoreServiceNode.objects.filter(pk=row.pk).update(logging_claim={})
    messages.success(request, _("Started; see Tasks."))


@require_POST
@staff_required(super_only=True)
def redeploy_view(request, code, pk, node):
    from . import core_deploy as cd

    n = get_object_or_404(CoreServiceNode, pk=node, service__pk=pk, service__site__code=code)
    from . import base_deploy as bd

    base = bd.is_base(n)
    if request.POST.get("confirm") != n.name:
        messages.error(request, _("Type the VM's name to confirm."))
        return _back(code, f"cs-{pk}") if base else redirect("core_plan", code, pk, node)
    if base:  # T34 B4: from its service profile - offered only when the instance gives every value
        ok, why = bd.offer(n)
        if not ok:
            messages.error(request, _("Not redeployed: %(w)s") % {"w": "; ".join(why)})
            return _back(code, f"cs-{pk}")
        keep_old = request.POST.get("keep_old", "1") == "1"
        make_primary = n.service.stype == CoreService.Type.NS and request.POST.get("primary") == "1"  # owner 11:51
        first = request.POST.get("first") == "1"  # commit-402: confirmed as its first deployment (Deploy)
        _start(
            request,
            n,
            "core VM redeploy",
            (_("Deploy %(n)s from its service profile") if first else _("Redeploy %(n)s from its service profile"))
            % {"n": n.name}
            + (" " + _("as the primary DNS server") if make_primary else ""),
            lambda task: bd.redeploy(task, n, request.user, keep_old, make_primary, first=first),
        )
        return _back(code, f"cs-{pk}")
    raw = (request.POST.get("expected") or "").strip()
    expected = int(raw) if raw.isdigit() else None
    force = request.POST.get("force") == "1"
    keep_old = request.POST.get("keep_old") == "1"
    _start(
        request,
        n,
        "core VM redeploy",
        _("Redeploy %(n)s from its template") % {"n": n.name},
        lambda task: cd.redeploy(task, n, request.user, expected, force, keep_old),
    )
    return _back(code, f"cs-{pk}")


@require_POST
@staff_required(super_only=True)
def fe_restore_view(request, code, pk):
    """T34 B6 (mockup 05): every node of a front end made again from the platform's latest backup in the local folder;
    one confirmation (the service's name). Each node is claimed as a redeploy claims it (commit-281)."""
    import secrets

    from django.db import transaction
    from django.utils import timezone

    from accounts import tasks

    from . import fe_deploy, hostlogging

    svc = get_object_or_404(CoreService, pk=pk, site__code=code, stype=CoreService.Type.ONEFE)
    if request.POST.get("confirm") != svc.name:
        messages.error(request, _("Type the service's name to confirm."))
        return _back(code, f"cs-{pk}")
    ok, why, name = fe_deploy.restore_offer(svc)
    if not ok:
        messages.error(request, _("Not restored: %(w)s") % {"w": "; ".join(why)})
        return _back(code, f"cs-{pk}")
    if request.POST.get("backup") != name:
        messages.error(request, _("A newer backup came meanwhile: confirm again."))
        return _back(code, f"cs-{pk}")
    token = f"lifecycle-{secrets.token_hex(6)}"
    with transaction.atomic():
        CoreService.objects.select_for_update().get(pk=svc.pk)  # commit-344: as _start locks it
        rows = list(CoreServiceNode.objects.select_for_update().filter(service=svc).order_by("pk"))
        for row in rows:
            if _running_node(row):
                messages.info(request, _("Something is already running for %(n)s; see Tasks.") % {"n": row.name})
                return _back(code, f"cs-{pk}")
            if hostlogging.claim_fresh(row.logging_claim):
                messages.error(
                    request, _("Apply logging of %(n)s is running: try again when it ends.") % {"n": row.name}
                )
                return _back(code, f"cs-{pk}")
        CoreServiceNode.objects.filter(pk__in=[r.pk for r in rows]).update(
            logging_claim={"token": token, "at": timezone.now().isoformat(), "by": "restore"}
        )
    try:
        tasks.start(
            RESTORE_KIND,
            _("Restore %(s)s from %(b)s") % {"s": svc.name, "b": name},
            lambda task: fe_deploy.restore_all(task, svc, request.user, name),
            user=request.user,
            target=svc,
        )
    finally:
        with transaction.atomic():
            for row in CoreServiceNode.objects.select_for_update().filter(service=svc):
                if (row.logging_claim or {}).get("token") == token:
                    CoreServiceNode.objects.filter(pk=row.pk).update(logging_claim={})
    messages.success(request, _("Started; see Tasks."))
    return _back(code, f"cs-{pk}")


@require_POST
@staff_required(super_only=True)
def fe_confirm_absent_view(request, code, pk, node):
    """commit-351 (owner 11:50): the operator states that no VM was made for a node whose creation got no answer
    (typed the node's name, after checking the platform); then Retry the rollback can settle it."""
    from . import fe_deploy

    svc = get_object_or_404(CoreService, pk=pk, site__code=code, stype=CoreService.Type.ONEFE)
    n = get_object_or_404(CoreServiceNode, pk=node, service=svc)
    if request.POST.get("confirm") != n.name:
        messages.error(request, _("Type the VM's name to confirm."))
        return _back(code, f"cs-{pk}")
    if fe_deploy.confirm_absent(svc, n.pk, request.user, request.POST.get("tag") or ""):  # commit-359
        messages.success(request, _("Recorded: no VM was made for %(n)s. Retry the rollback now.") % {"n": n.name})
    else:
        messages.error(
            request,
            _("Nothing to confirm for %(n)s with that tag: the dialog was older than the restore - check again.")
            % {"n": n.name},
        )
    return _back(code, f"cs-{pk}")


@require_POST
@staff_required(super_only=True)
def fe_retry_view(request, code, pk):
    """commit-350: Retry the rollback of a front end's Restore all from its recovery record; one confirmation."""
    import secrets

    from django.db import transaction
    from django.utils import timezone

    from accounts import tasks

    from . import fe_deploy, hostlogging

    svc = get_object_or_404(CoreService, pk=pk, site__code=code, stype=CoreService.Type.ONEFE)
    if request.POST.get("confirm") != svc.name:
        messages.error(request, _("Type the service's name to confirm."))
        return _back(code, f"cs-{pk}")
    token = f"lifecycle-{secrets.token_hex(6)}"
    with transaction.atomic():
        CoreService.objects.select_for_update().get(pk=svc.pk)
        rows = list(CoreServiceNode.objects.select_for_update().filter(service=svc).order_by("pk"))
        if not fe_deploy.pending(svc):
            messages.info(request, _("Nothing to roll back."))
            return _back(code, f"cs-{pk}")
        if rows and _running_node(rows[0]):
            messages.info(request, _("Something is already running for this front end; see Tasks."))
            return _back(code, f"cs-{pk}")
        if any(hostlogging.claim_fresh(r.logging_claim) for r in rows):  # commit-353: another request is starting
            messages.error(
                request, _("Another operation on this front end is starting or running: try again when it ends.")
            )
            return _back(code, f"cs-{pk}")
        CoreServiceNode.objects.filter(pk__in=[r.pk for r in rows]).update(
            logging_claim={"token": token, "at": timezone.now().isoformat(), "by": "restore"}
        )
    try:
        tasks.start(
            RESTORE_KIND,
            _("Retry the rollback of %(s)s") % {"s": svc.name},
            lambda task: fe_deploy.retry_rollback(task, svc, request.user),
            user=request.user,
            target=svc,
        )
    finally:
        with transaction.atomic():
            for row in CoreServiceNode.objects.select_for_update().filter(service=svc):
                if (row.logging_claim or {}).get("token") == token:
                    CoreServiceNode.objects.filter(pk=row.pk).update(logging_claim={})
    messages.success(request, _("Started; see Tasks."))
    return _back(code, f"cs-{pk}")


@require_POST
@staff_required(super_only=True)
def rollback_view(request, code, pk, node):
    from . import core_deploy as cd

    n = get_object_or_404(CoreServiceNode, pk=node, service__pk=pk, service__site__code=code)
    _start(
        request,
        n,
        "core VM redeploy",
        _("Roll back %(n)s to its old VM") % {"n": n.name},
        lambda task: cd.rollback(task, n, request.user),
    )
    return _back(code, f"cs-{pk}")


@require_POST
@staff_required(Role.SUPPORT)
def refresh_view(request, code):
    """Read the VMs of the site's platforms again (the platform's view: status, host, NICs); quick, reads only."""
    from django.http import JsonResponse

    get_object_or_404(Site, code=code)
    errors = cs.refresh_vms(code)
    if request.headers.get("X-Requested-With") == "fetch":
        return JsonResponse({"ok": not errors, "errors": [f"{k}: {e}" for k, e in errors]})
    for k, e in errors:
        messages.error(request, f"{k}: {e}")
    tab = request.POST.get("tab") or "core"  # back to the tab it was pressed on (the button is on every tab)
    if tab != "core" and tab.isidentifier():
        return redirect(f"{reverse('infra_site', args=[code])}?tab={tab}")
    return _back(code, request.POST.get("sel") or None)


@require_POST
@staff_required(super_only=True)
def clean_view(request, code, pk, node):
    from . import core_deploy as cd

    n = get_object_or_404(CoreServiceNode, pk=node, service__pk=pk, service__site__code=code)
    _start(
        request,
        n,
        "core VM redeploy",
        _("Remove the init script from the context of %(n)s") % {"n": n.name},
        lambda task: cd.clean_context(task, n, request.user),
    )
    return _back(code, f"cs-{pk}")


@require_POST
@staff_required(super_only=True)
def finish_view(request, code, pk, node):
    from . import core_deploy as cd

    n = get_object_or_404(CoreServiceNode, pk=node, service__pk=pk, service__site__code=code)
    _start(
        request,
        n,
        "core VM redeploy",
        _("Remove the old VM of %(n)s") % {"n": n.name},
        lambda task: cd.finish(task, n, request.user),
    )
    return _back(code, f"cs-{pk}")


@require_POST
@staff_required(super_only=True)
def ns_split_view(request, code, pk):
    """commit-506: a name server service of two VMs made before the split becomes ns-primary and ns-secondary (records
    only)."""
    svc = get_object_or_404(CoreService, pk=pk, site__code=code)
    try:
        new = ct.ns_split(svc, request.user)
        messages.success(request, _("Split: %(a)s and %(b)s.") % {"a": svc.name, "b": new.name})
        return _back(code, f"cs-{new.pk}")
    except ct.DefinitionError as exc:
        messages.error(request, _("Not split: %(w)s.") % {"w": exc})
    return _back(code, f"cs-{pk}")


@require_POST
@staff_required(super_only=True)
def snapd_view(request, code, pk, node):
    """T36 B3c (rules v2 C8): Remove snapd from a base-OS VM in place - a task, one at a time with the VM's other
    operations; nothing else on the VM changes."""
    from . import snapd

    n = get_object_or_404(CoreServiceNode, pk=node, service__pk=pk, service__site__code=code)
    if not snapd.removable(n):  # commit-517: only when the last read found it installed or unmasked
        messages.error(request, _("%(n)s: snapd is not known to be there (Read it first).") % {"n": n.name})
        return _back(code, f"cs-{pk}")
    _start(
        request,
        n,
        "core VM redeploy",
        _("Remove snapd from %(n)s") % {"n": n.name},
        lambda task: snapd.remove(task, n, request.user),
    )
    return _back(code, f"cs-{pk}")


# --- the logging of a base-OS VM (owner 2026-11-21 11:58 "service VMs now", T33) ----------------------------------


@require_POST
@staff_required(Role.SUPPORT)
def logging_check_view(request, code, pk, node):
    """Check logging of a core service VM - read only, as a host's."""
    from . import hostlogging

    n = get_object_or_404(CoreServiceNode, pk=node, service__pk=pk, service__site__code=code)
    r = hostlogging.check(n)
    if r["status"] == "compliant":
        messages.success(request, _("Logging is compliant."))
    elif r["status"] == "differs":
        messages.warning(request, _("Logging differs from the services: see Logging in the VM's details."))
    else:
        messages.error(request, _("Logging not checked: %(e)s") % {"e": r.get("error", "")})
    return _back(code, f"cs-{pk}")


@require_POST
@staff_required(super_only=True)
def logging_apply_view(request, code, pk, node):
    """Apply logging to a core service VM: its services' files and config changes, as on a host. The owner says yes
    per VM (11:58): the dialog asks for the VM's name."""
    from . import hostlogging

    n = get_object_or_404(CoreServiceNode, pk=node, service__pk=pk, service__site__code=code)
    if request.POST.get("confirm") != n.name:
        messages.error(request, _("Type the VM's name to confirm."))
        return _back(code, f"cs-{pk}")
    shown = [x for x in (request.POST.get("restarts") or "").split(",") if x]  # commit-285
    try:
        r = hostlogging.apply(request.user, n, restarts=shown)
        (messages.success if r["status"] == "compliant" else messages.warning)(
            request, _("Logging applied: %(s)s.") % {"s": r["status"]}
        )
        if r.get("config_error"):
            messages.error(request, _("Config changes: %(e)s") % {"e": r["config_error"]})
        if r.get("test_message", "sent") != "sent":
            messages.warning(request, _("The test message was %(t)s") % {"t": r["test_message"]})
    except hostlogging.LoggingError as exc:
        messages.error(request, _("Logging not applied: %(e)s") % {"e": exc})
    return _back(code, f"cs-{pk}")
