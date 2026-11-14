"""Editing the infrastructure by the super admin (dialogs): sites, hosts, storage, platforms, clusters (see ops.py)."""

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from accounts.models import Role
from accounts.utils import staff_required

from . import ops
from .models import Cluster, Host, Network, Platform, PlatformDatastore, PlatformNetwork, ServerProfile, Site, Storage

CODE = r"^[a-z][a-z0-9-]{0,30}$"
NAME = r"^[a-z0-9][a-z0-9_.-]{0,62}$"


class AddressList(forms.CharField):
    """Up to `limit` IPv4 addresses or host names, separated by spaces or commas."""

    def __init__(self, *args, limit=3, required=False, **kwargs):
        self.limit = limit
        super().__init__(*args, max_length=300, required=required, **kwargs)
        if limit > 1:  # a list of fields, one address per row
            from .network_views import RowsTextarea

            self.widget = RowsTextarea([("x", _l("Address or host name"), "10.0.0.1")], _l("Address"))

    def clean(self, value):
        import ipaddress
        import re

        items = [x for x in (super().clean(value) or "").replace(",", " ").split() if x]
        if len(items) > self.limit:
            raise forms.ValidationError(_l("At most %(n)s.") % {"n": self.limit})
        for x in items:
            try:
                ipaddress.ip_address(x)
            except ValueError:
                if not re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", x, re.I):
                    raise forms.ValidationError(_l("%(x)s is not an address or a host name.") % {"x": x}) from None
        return items


class SiteForm(forms.Form):
    code = forms.RegexField(label=_l("Code"), regex=CODE)
    number = forms.IntegerField(
        label=_l("Site code"),
        min_value=1,
        max_value=999,
        help_text=_l("1-999, part of the AS numbers; fixed once set."),
    )
    # owner 2026-11-09: one edge cluster per site, chosen here - it carries what serves the whole site.
    edge_cluster = forms.ChoiceField(
        label=_l("Edge cluster"),
        required=False,
        help_text=_l(
            "The route reflectors and the L2 gateway endpoints that connect "
            "to VLANs run here. Empty: the site's management platform."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        code = (self.data or {}).get("code") or (self.initial or {}).get("code")
        cl = (
            Cluster.objects.filter(platform__site__code=code).exclude(state="retired").order_by("name")
            if code
            else Cluster.objects.none()
        )
        self.fields["edge_cluster"].choices = [("", _l("— none —"))] + [
            (c.name, f"{c.platform.name} · {c.name}") for c in cl
        ]
        site = Site.objects.filter(code=code).first() if code else None
        if site is not None:
            from .models import _edge_services_deployed

            if _edge_services_deployed(site):
                f = self.fields["edge_cluster"]
                f.disabled = True
                f.help_text = _l("Fixed: edge services are deployed on it.")
        if (self.initial or {}).get("number"):
            self.fields["number"].disabled = True
        from .models import (  # only real time zones (correction 09); a former city name shows as its zone
            iana_zones,
            site_tz,
        )

        self.fields["timezone"].choices = [(z, z) for z in sorted(iana_zones())]
        if "timezone" in (self.initial or {}):
            self.initial["timezone"] = site_tz(self.initial["timezone"])
        from .network_views import rows_widget

        rows_widget(self.fields["mgmt_hosts"], [("x", _l("Address or network"), "10.82.101.22/31")], _l("Host"))
        rows_widget(
            self.fields["ssh_ports"],
            [("p", _l("Purpose"), "VyOS VMs"), ("n", _l("Port"), "22", "90px")],
            _l("Port"),
            sep=" | ",
        )
        rows_widget(
            self.fields["log_collectors"],
            [
                ("p", _l("Purpose"), "VyOS VMs", "130px"),
                ("h", _l("IP / host name"), "10.0.0.18"),
                ("t", _l("Protocol"), "", "72px", ["udp", "tcp"]),
                ("n", _l("Port"), "514", "80px"),
            ],
            _l("Collector"),
            sep=" | ",
        )

    country = forms.CharField(label=_l("Country"), max_length=8)
    city = forms.CharField(label=_l("City"), max_length=100)
    dc = forms.CharField(label=_l("Data center"), max_length=8)
    timezone = forms.ChoiceField(
        label=_l("Time zone"), initial="Asia/Almaty", help_text=_l("IANA; the IPMI gets its UTC offset.")
    )
    # every site setting is required (2026-10-27: "all fields required to be defined in site settings")
    ntp_servers = AddressList(label=_l("NTP servers"), limit=4, required=True, help_text=_l("Up to 4."))
    dns_servers = AddressList(label=_l("DNS servers"), limit=3, required=True, help_text=_l("Up to 3."))
    bootstrap_dns = AddressList(
        label=_l("Green-field DNS server"), limit=1, required=True, help_text=_l("While only the appliance is up.")
    )
    mgmt_hosts = forms.CharField(
        label=_l("Management hosts"),
        widget=forms.Textarea(attrs={"rows": 3, "class": "mono"}),
        help_text=_l("The only sources allowed to manage the site (ssh to VRF MGMT)."),
    )
    ssh_ports = forms.CharField(
        label=_l("SSH ports"),
        widget=forms.Textarea(attrs={"rows": 3, "class": "mono"}),
        help_text=_l("Required: VyOS VMs, Core services."),
    )
    log_collectors = forms.CharField(
        label=_l("Log collectors"),
        widget=forms.Textarea(attrs={"rows": 4, "class": "mono"}),
        help_text=_l("Required: IPMI, VyOS VMs, Core services."),
    )

    def clean_mgmt_hosts(self):
        import ipaddress

        out = []
        for x in (self.cleaned_data.get("mgmt_hosts") or "").replace(",", " ").split():
            try:
                out.append(str(ipaddress.ip_network(x, strict=True)) if "/" in x else str(ipaddress.ip_address(x)))
            except ValueError:
                raise forms.ValidationError(
                    _l("%(x)s is not an address or a host group (1.2.3.0/28).") % {"x": x}
                ) from None
        return out

    @staticmethod
    def _rows(text):
        return [[c.strip() for c in line.split("|")] for line in (text or "").splitlines() if line.strip()]

    def clean_ssh_ports(self):
        """[{key, purpose, port}]: the required purposes (VyOS VMs, Core services) and optional others."""
        from .models import PURPOSE_ALIASES, SSH_REQUIRED

        out, seen = [], set()
        for row in self._rows(self.cleaned_data.get("ssh_ports")):
            purpose, port = (row + ["", ""])[:2]
            if not purpose or not port.isdigit() or not 1 <= int(port) <= 65535:
                raise forms.ValidationError(_l("Each line: a purpose and a port (1-65535)."))
            key = {"vyos": "vyos", "core": "ssh"}.get(PURPOSE_ALIASES.get(purpose.lower(), ""), "")
            if key and key in seen or purpose.lower() in seen:
                raise forms.ValidationError(_l("%(p)s is given twice.") % {"p": purpose})
            seen.add(key or purpose.lower())
            out.append({"key": key, "purpose": purpose, "port": int(port)})
        missing = [str(label) for k, label in SSH_REQUIRED if k not in seen]
        if missing:
            raise forms.ValidationError(_l("Required: %(m)s.") % {"m": ", ".join(missing)})
        return out

    def clean_log_collectors(self):
        """[{key, purpose, host, proto, port}]: the required purposes (IPMI, VyOS VMs, Core services) and optional others."""
        import re

        from .models import LOG_REQUIRED, PURPOSE_ALIASES

        host_re = re.compile(
            r"^([0-9]{1,3}(\.[0-9]{1,3}){3}|[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*)$"
        )
        out, seen = [], set()
        for row in self._rows(self.cleaned_data.get("log_collectors")):
            purpose, host, proto, port = (row + ["", "", "", ""])[:4]
            proto = (proto or "udp").lower()
            if (
                not purpose
                or not host_re.match(host)
                or proto not in ("udp", "tcp")
                or not port.isdigit()
                or not 1 <= int(port) <= 65535
            ):
                raise forms.ValidationError(
                    _l("Each line: a purpose, an IP or host name, UDP or TCP and a port (1-65535).")
                )
            key = PURPOSE_ALIASES.get(purpose.lower(), "")
            if (key or purpose.lower()) in seen:
                raise forms.ValidationError(_l("%(p)s is given twice.") % {"p": purpose})
            seen.add(key or purpose.lower())
            out.append({"key": key, "purpose": purpose, "host": host, "proto": proto, "port": int(port)})
        missing = [str(label) for k, label in LOG_REQUIRED if k not in seen]
        if missing:
            raise forms.ValidationError(_l("Required: %(m)s.") % {"m": ", ".join(missing)})
        return out


class HostForm(forms.Form):
    site = forms.ChoiceField(label=_l("Site"))
    name = forms.RegexField(label=_l("Host name"), regex=NAME, help_text=_l("Also the host name of the server."))
    ip = forms.GenericIPAddressField(label=_l("Address (ssh)"), protocol="IPv4")
    ipmi = forms.GenericIPAddressField(label=_l("IPMI address"), protocol="IPv4", required=False)
    ipmi_port = forms.IntegerField(
        label=_l("IPMI https port"),
        min_value=1,
        max_value=65535,
        initial=443,
        required=False,
        help_text=_l("Redfish over https."),
    )
    ipmi_user = forms.RegexField(label=_l("IPMI user (secret)"), regex=r"^secrets(\.[A-Za-z0-9_-]+)+$", required=False)
    profile = forms.ChoiceField(
        label=_l("Server profile"),
        required=False,
        help_text=_l(
            "Optional: the profile is detected from the hardware; a wrong choice is "
            "corrected and a new profile is made when none fits."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["site"].choices = [(s.code, s.code) for s in Site.objects.all()]
        self.fields["profile"].choices = [("", _l("detect"))] + [(p.code, p.code) for p in ServerProfile.objects.all()]
        from .credentials import ref_field

        ref_field(self, "ipmi_user", "ipmi")


class HostEditForm(forms.Form):
    name = forms.RegexField(
        label=_l("Host name"),
        regex=NAME,
        help_text=_l("Also the server's host name; only a planned host can be renamed."),
    )
    ip = forms.GenericIPAddressField(label=_l("Address (ssh)"), protocol="IPv4")
    ipmi = forms.GenericIPAddressField(label=_l("IPMI address"), protocol="IPv4", required=False)
    ipmi_port = forms.IntegerField(
        label=_l("IPMI https port"),
        min_value=1,
        max_value=65535,
        initial=443,
        required=False,
        help_text=_l("Redfish over https."),
    )
    ipmi_user = forms.RegexField(label=_l("IPMI user (secret)"), regex=r"^secrets(\.[A-Za-z0-9_-]+)+$", required=False)
    profile = forms.ChoiceField(label=_l("Server profile"), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["profile"].choices = [("", "—")] + [(p.code, p.code) for p in ServerProfile.objects.all()]
        from .credentials import ref_field

        ref_field(self, "ipmi_user", "ipmi")


def _lines(text):
    return [x for x in (text or "").replace(",", " ").split() if x]


class StorageForm(forms.Form):
    name = forms.RegexField(label=_l("Name"), regex=NAME)
    kind = forms.ChoiceField(label=_l("Type"), choices=Storage.Kind.choices)
    targets = forms.CharField(
        label=_l("Target addresses"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 2, "class": "mono"}),
        help_text=_l("NFS: one address; iSCSI: one or more, one per line."),
    )
    port = forms.IntegerField(label=_l("iSCSI port"), min_value=1, max_value=65535, required=False, initial=3260)
    fstab = forms.CharField(
        label=_l("fstab parameters"),
        max_length=300,
        required=False,
        help_text=_l("e.g. nfs4 _netdev,rw,vers=4.2,noatime 0 0, or xfs _netdev,nofail 0 2"),
    )
    host = forms.ChoiceField(label=_l("Host (local)"), required=False)

    def __init__(self, *args, site=None, **kwargs):
        super().__init__(*args, **kwargs)
        from .network_views import rows_widget

        rows_widget(self.fields["targets"], [("x", _l("Target address"), "10.0.30.10")], _l("Target"))
        hosts = Host.objects.filter(site=site) if site else Host.objects.none()
        self.fields["host"].choices = [("", "—")] + [(h.name, h.name) for h in hosts]


class VolumeForm(forms.Form):
    name = forms.RegexField(label=_l("Name"), regex=NAME)
    device = forms.CharField(
        label=_l("Export or device"),
        max_length=200,
        help_text=_l(
            "NFS: the export (/mgmt_vms); iSCSI and local: the device as lsblk lists it (/dev/mapper/mpatha, /dev/sda)."
        ),
    )
    size_gb = forms.IntegerField(label=_l("Size, GiB"), min_value=0)
    clients = forms.CharField(
        label=_l("Clients"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "class": "mono"}),
        help_text=_l("NFS: client addresses or networks; iSCSI: initiator IQNs. One per line."),
    )
    mount = forms.CharField(label=_l("Mount point"), max_length=200, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .network_views import rows_widget

        rows_widget(self.fields["clients"], [("x", _l("Client network or IQN"), "10.0.0.0/24")], _l("Client"))


PUBLIC_FIELDS = ("public_url", "fireedge_port", "grpc_port", "xmlrpc_port", "vm_url", "onegate_port", "oneflow_port")


class PlatformForm(forms.Form):
    site = forms.ChoiceField(label=_l("Site"))
    name = forms.RegexField(label=_l("Platform name"), regex=r"^[a-z][a-z0-9_-]{1,30}$")
    kind = forms.ChoiceField(
        label=_l("Type"),
        choices=[("shared", "shared"), ("mgmt", "mgmt"), ("ext-managed", "ext-managed")],
        help_text=_l(
            "shared: projects are created on it; mgmt: the site's management platform; "
            "ext-managed: managed by Claugine, its payload by a third-party system."
        ),
    )
    state = forms.ChoiceField(
        label=_l("State"),
        required=False,
        choices=[
            ("planned", _l("planned (not installed)")),
            ("deployed", _l("deployed")),
            ("updated", _l("updated (changes to deploy)")),
            ("retired", _l("retired")),
        ],
    )
    fe_name = forms.CharField(label=_l("Front-end (zone) name"), max_length=100, required=False)
    fe_mode = forms.ChoiceField(
        label=_l("Front-end mode"),
        choices=[(m, m) for m in ("single", "local", "primary", "secondary")],
        initial="local",
    )
    fe_primary = forms.ChoiceField(label=_l("Primary (for a secondary)"), required=False)
    fe_type = forms.ChoiceField(
        label=_l("Front end runs in"),
        choices=[("vm", _l("VMs")), ("host", _l("the host OS"))],
        initial="vm",
        required=False,
    )
    fe_nodes = forms.CharField(
        label=_l("Front-end nodes"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "class": "mono"}),
        help_text=_l("One per line."),
    )
    fe_hostname = forms.CharField(label=_l("Front-end host name"), max_length=100, required=False)
    fe_vip = forms.GenericIPAddressField(label=_l("Front-end VIP"), protocol="IPv4", required=False)
    fe_secrets = forms.RegexField(
        label=_l("Front-end secrets"),
        regex=r"^secrets(\.[A-Za-z0-9_-]+)+$",
        required=False,
        help_text=_l("Reference to Secrets: the OpenNebula passwords of the front end."),
    )
    sa = forms.RegexField(
        label=_l("Super admins"),
        regex=r"^secrets(\.[A-Za-z0-9_-]+)+$",
        required=False,
        help_text=_l("Reference to Secrets: the platform's super admin accounts (user and ssh key)."),
    )
    public_url = forms.URLField(
        label=_l("Public URL"),
        required=False,
        max_length=200,
        help_text=_l(
            "Shared platforms: FireEdge, gRPC API and XML-RPC API, each on its port, e.g. "
            "https://one-dc1.claugine.com. It can be changed on a deployed platform too."
        ),
    )
    fireedge_port = forms.IntegerField(
        label=_l("FireEdge port"),
        required=False,
        min_value=1,
        max_value=65535,
        help_text=_l("e.g. 55516; empty: the port of the URL"),
    )
    grpc_port = forms.IntegerField(
        label=_l("gRPC API port"), required=False, min_value=1, max_value=65535, help_text=_l("e.g. 55534")
    )
    xmlrpc_port = forms.IntegerField(
        label=_l("XML-RPC API port"), required=False, min_value=1, max_value=65535, help_text=_l("e.g. 55533")
    )
    vm_url = forms.URLField(
        label=_l("Public URL for VMs"),
        required=False,
        max_length=200,
        help_text=_l(
            "OneGate and OneFlow, each on its port; usually reachable only from the site's "
            "own subnets, e.g. https://one-dc1.claugine.com."
        ),
    )
    onegate_port = forms.IntegerField(
        label=_l("OneGate port"), required=False, min_value=1, max_value=65535, help_text=_l("e.g. 55530")
    )
    oneflow_port = forms.IntegerField(
        label=_l("OneFlow port"), required=False, min_value=1, max_value=65535, help_text=_l("e.g. 55574")
    )

    def __init__(self, *args, locked=False, site=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["site"].choices = [(s.code, s.code) for s in Site.objects.all()]
        self.fields["fe_primary"].choices = [("", "—")] + [(p.key, p.key) for p in Platform.objects.all()]
        from .network_views import rows_widget

        rows_widget(self.fields["fe_nodes"], [("x", _l("Node"), "10.0.0.31")], _l("Node"))
        from .credentials import ref_field

        ref_field(self, "fe_secrets", "opennebula")
        ref_field(self, "sa", "account")
        if locked:  # a deployed platform: everything but its state is fixed
            for name, f in self.fields.items():
                if name in ("state",) + PUBLIC_FIELDS:
                    continue
                f.disabled = True
                f.required = False
                if name not in ("site", "name"):
                    f.help_text = _l("Fixed: the platform is deployed.")


@require_POST
@staff_required(super_only=True)
def platform_gateway_view(request, site, name):
    """Set the platform's gateway cluster from the platform page (owner 2026-11-09: "on platform page").

    It is deliberately not in the edit dialog: that form is long, the choice is one field, and it is refused
    outright once projects are deployed on the platform.
    """
    p = get_object_or_404(Platform, key=f"{site}.{name}")
    _run(
        request,
        ops.platform_save,
        _("Gateway cluster set."),
        site,
        name,
        {"kind": p.kind, "gateway_cluster": request.POST.get("gateway_cluster") or ""},
    )
    return _back("platforms", site)


def platform_initial(p):
    fe = p.fe or {}
    return {
        "site": p.site.code,
        "name": p.name,
        "kind": p.kind,
        "state": p.state or "deployed",
        "fe_name": p.fe_name,
        "fe_mode": p.fe_mode or "local",
        "fe_primary": p.fe_primary,
        "fe_vip": p.fe_vip,
        "fe_type": fe.get("type", "vm"),
        "fe_nodes": "\n".join(str(x) for x in fe.get("nodes") or []),
        "fe_hostname": fe.get("hostname", ""),
        "fe_secrets": p.fe_secrets,
        "sa": p.sa,
        **{f: getattr(p, f) for f in PUBLIC_FIELDS},
    }


class DatastoreForm(forms.Form):
    name = forms.RegexField(label=_l("Name"), regex=NAME)
    kind = forms.ChoiceField(label=_l("Type"), choices=PlatformDatastore.Kind.choices)
    volume = forms.ChoiceField(label=_l("Storage volume"))
    path = forms.CharField(
        label=_l("Path"), max_length=200, required=False, help_text=_l("Optional, inside the volume.")
    )
    reserved_pct = forms.IntegerField(
        label=_l("Reserved, %"),
        min_value=0,
        max_value=90,
        required=False,
        help_text=_l("Kept free: 10% by default, 20% for backups."),
    )
    # backups only
    backend = forms.ChoiceField(
        label=_l("Backend"),
        required=False,
        choices=[("", "—")] + PlatformDatastore.Backend.choices,
        help_text=_l("Backups: iSCSI — restic on an iSCSI volume of the restic VM; S3 — restic in an S3 bucket."),
    )
    restic_ip = forms.GenericIPAddressField(
        label=_l("Restic VM address"),
        protocol="IPv4",
        required=False,
        help_text=_l("Backups: the VM that provides the restic service (its first NIC)."),
    )
    s3_endpoint = forms.RegexField(
        label=_l("S3 endpoint"),
        regex=r"^[A-Za-z0-9.:-]+$",
        max_length=200,
        required=False,
        help_text=_l("S3: IP or host name."),
    )
    s3_bucket = forms.RegexField(
        label=_l("S3 bucket"), regex=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", max_length=63, required=False
    )
    s3_secret = forms.ChoiceField(label=_l("S3 secret"), required=False)
    restic_secret = forms.ChoiceField(
        label=_l("Restic password"), required=False, help_text=_l("Backups: a secret of type password.")
    )

    BACKUP_FIELDS = ("backend", "restic_ip", "s3_endpoint", "s3_bucket", "s3_secret", "restic_secret")

    def __init__(self, *args, platform=None, **kwargs):
        super().__init__(*args, **kwargs)
        from .credentials import ref_choices
        from .models import StorageVolume

        vols = StorageVolume.objects.filter(storage__site=platform.site).select_related("storage") if platform else []
        self.fields["volume"].choices = [("", "—")] + [
            (v.pk, f"{v.storage.name} / {v.name} · {v.size_gb} GiB") for v in vols
        ]
        self.fields["volume"].required = False
        self.fields["s3_secret"].choices = [("", "—")] + ref_choices("s3")
        self.fields["restic_secret"].choices = [("", "—")] + ref_choices("password")

    def clean(self):
        d = super().clean()
        backups = d.get("kind") == PlatformDatastore.Kind.BACKUPS
        s3 = backups and d.get("backend") == PlatformDatastore.Backend.S3
        if not s3 and not d.get("volume"):
            self.add_error("volume", _l("Choose a volume of the site's storage."))
        if backups:
            for f, need in (
                ("backend", True),
                ("restic_ip", True),
                ("restic_secret", True),
                ("s3_endpoint", s3),
                ("s3_bucket", s3),
                ("s3_secret", s3),
            ):
                if need and not d.get(f):
                    self.add_error(f, _l("Required for backups."))
        else:
            for f in self.BACKUP_FIELDS:
                d[f] = None if f == "restic_ip" else ""
        if backups and not s3:
            d["s3_endpoint"] = d["s3_bucket"] = d["s3_secret"] = ""
        return d


class VnetForm(forms.Form):
    name = forms.RegexField(label=_l("Name"), regex=NAME)
    kind = forms.ChoiceField(label=_l("Type"), choices=PlatformNetwork.Kind.choices)
    network = forms.ChoiceField(label=_l("Site network"))
    bridge = forms.RegexField(
        label=_l("Bridge"),
        regex=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,14}$",
        required=False,
        help_text=_l("bridge: the existing bridge it is linked to."),
    )
    ranges = forms.MultipleChoiceField(
        label=_l("Ranges to create"),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text=_l("Address ranges of the site network created in OpenNebula."),
    )

    def __init__(self, *args, platform=None, **kwargs):
        super().__init__(*args, **kwargs)
        nets = list(Network.objects.filter(site=platform.site.code).prefetch_related("ranges")) if platform else []
        self.fields["network"].choices = [("", "—")] + [(n.pk, f"{n.name} · VLAN {n.vlan} · {n.cidr}") for n in nets]
        self.fields["ranges"].choices = [
            (r.pk, f"{n.name}: {r.name} ({r.start} + {r.length})") for n in nets for r in n.ranges.all()
        ]


class CapacityFields(forms.Form):
    vcpu_ratio = forms.IntegerField(
        label=_l("vCores per pCore"),
        min_value=1,
        max_value=100,
        initial=10,
        required=False,
        help_text=_l("vCPUs one physical core gives (hyperthreads not counted)."),
    )
    spare_hosts = forms.IntegerField(
        label=_l("Spare hosts (N+X)"),
        min_value=0,
        max_value=16,
        initial=1,
        required=False,
        help_text=_l("X hosts are left out of the available resources (no host is a dedicated spare)."),
    )
    reserved_cpu_pct = forms.IntegerField(
        label=_l("Reserved CPU, %"), min_value=0, max_value=90, initial=5, required=False
    )
    reserved_ram_pct = forms.IntegerField(
        label=_l("Reserved RAM, %"), min_value=0, max_value=90, initial=5, required=False
    )
    bundle_cores = forms.IntegerField(label=_l("vCores per GPU bundle"), min_value=0, max_value=1024, required=False)
    bundle_ram_gb = forms.IntegerField(label=_l("GB RAM per GPU bundle"), min_value=0, max_value=8192, required=False)

    # owner 2026-11-09 (gpu-bundles): a cluster is one kind or the other, so it is shown one set of fields or the
    # other. A GPU cluster is ordered in bundles, and the head-room is inside the bundle - showing it a vCPU ratio
    # and two reserve percentages as well would invite someone to set a figure that does nothing.
    GPU_ONLY = ("bundle_cores", "bundle_ram_gb")
    CPU_ONLY = ("vcpu_ratio", "reserved_cpu_pct", "reserved_ram_pct")

    def shape_for(self, cluster):
        """Keep the fields that apply, drop the rest. `cluster` is None while one is being created."""
        drop = self.GPU_ONLY if cluster is None or not cluster.has_gpu() else self.CPU_ONLY
        for f in drop:
            self.fields.pop(f, None)
        if cluster is not None and cluster.has_gpu():
            # owner 2026-11-09: an empty box told nobody what the cluster actually orders by. Say what is in
            # force, and only then what the hardware suggests.
            d, b = cluster.default_bundle(), cluster.bundle()
            set_by_hand = bool(cluster.bundle_cores or cluster.bundle_ram_gb)
            now = _l("In force: %(c)s vCores + %(r)s GB per card.") % {"c": b["cores"], "r": b["ram_gb"]}
            if not set_by_hand:
                now = f"{now} {_l('Taken from the hardware; set a value to pin it.')}"
            self.fields["bundle_cores"].help_text = f"{now} {_l('This hardware suggests %(n)s.') % {'n': d['cores']}}"
            self.fields["bundle_ram_gb"].help_text = _l("This hardware suggests %(n)s GB.") % {"n": d["ram_gb"]}


class ClusterForm(CapacityFields):
    platform = forms.ChoiceField(label=_l("Platform"))
    name = forms.RegexField(label=_l("Cluster name"), regex=NAME, help_text=_l("Must end with _<site>."))
    kind = forms.ChoiceField(label=_l("Type"), choices=Cluster.Kind.choices)
    hosts = forms.MultipleChoiceField(label=_l("Hosts"), required=False, widget=forms.CheckboxSelectMultiple)
    field_order = [
        "platform",
        "name",
        "kind",
        "hosts",
        "vcpu_ratio",
        "spare_hosts",
        "reserved_cpu_pct",
        "reserved_ram_pct",
        "bundle_cores",
        "bundle_ram_gb",
    ]

    def __init__(self, *args, site=None, **kwargs):
        super().__init__(*args, **kwargs)
        from .views import free_hosts

        platforms = Platform.objects.select_related("site")
        if site:
            platforms = platforms.filter(site__code=site)
            hosts = free_hosts(Site.objects.get(code=site))
        else:
            hosts = [h for s in Site.objects.all() for h in free_hosts(s)]
        self.fields["platform"].choices = [(p.key, p.key) for p in platforms]
        self.fields["hosts"].choices = [
            (f"{h.site.code}/{h.name}", f"{h.name} ({h.state})" if site else f"{h.site.code} · {h.name} ({h.state})")
            for h in hosts
        ]
        self.shape_for(None)  # no hosts yet: the bundle is offered once the cluster has GPU hosts


class ClusterEditForm(CapacityFields):
    hosts_shown = forms.CharField(widget=forms.HiddenInput, initial="1", required=False)
    kind = forms.ChoiceField(label=_l("Type"), choices=Cluster.Kind.choices)
    hosts = forms.MultipleChoiceField(
        label=_l("Hosts"),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text=_l(
            "The cluster's hosts and the hosts in no cluster. Taking an active host "
            "out of an installed cluster retires it."
        ),
    )
    datastores = forms.MultipleChoiceField(label=_l("Datastores"), required=False, widget=forms.CheckboxSelectMultiple)
    networks = forms.MultipleChoiceField(label=_l("Networks"), required=False, widget=forms.CheckboxSelectMultiple)
    field_order = [
        "kind",
        "hosts",
        "datastores",
        "networks",
        "vcpu_ratio",
        "spare_hosts",
        "reserved_cpu_pct",
        "reserved_ram_pct",
        "bundle_cores",
        "bundle_ram_gb",
    ]

    def __init__(self, *args, platform=None, cluster=None, **kwargs):
        super().__init__(*args, **kwargs)
        from .views import free_hosts

        self.fields["datastores"].choices = (
            [(d.pk, f"{d.name} ({d.kind})") for d in platform.datastores.all()] if platform else []
        )
        self.fields["networks"].choices = (
            [(n.pk, f"{n.name} ({n.kind})") for n in platform.vnets.all()] if platform else []
        )
        hosts = list(cluster.hosts.all()) if cluster else []
        if platform:
            hosts += [h for h in free_hosts(platform.site) if h not in hosts]
        self.fields["hosts"].choices = [
            (h.name, f"{h.name} ({h.state}{', ' + h.profile.code if h.profile else ''})")
            for h in sorted(hosts, key=lambda h: h.name)
        ]
        self.shape_for(cluster)


def _back(tab, site=None, sel=None):
    """Back to the page the form was on: Sites, Server profiles, or a tab of a site (with the item selected)."""
    if tab == "profiles":
        return redirect("infra_profiles")
    if tab == "sites" or not site:
        return redirect("inventory")
    return redirect(f"{reverse('infra_site', args=[site])}?tab={tab}" + (f"&sel={sel}" if sel else ""))


def _run(request, fn, ok, *args, **kwargs):
    try:
        out = fn(request.user, *args, **kwargs)
    except ops.EditError as exc:
        messages.error(request, str(exc))
        for e in exc.errors[:8]:
            messages.error(request, e)
        return None, False
    if ok:
        messages.success(request, ok)
    return out, True


def _form_errors(request, form):
    for field, errs in form.errors.items():
        for e in errs:
            messages.error(request, f"{form.fields[field].label if field in form.fields else ''}: {e}".lstrip(": "))


@require_POST
@staff_required(super_only=True)
def site_save_view(request):
    form = SiteForm(request.POST)
    if form.is_valid():
        d = form.cleaned_data
        _run(request, ops.site_save, _("Site saved."), d["code"], d, new=request.POST.get("new") == "1")
    else:
        _form_errors(request, form)
    if request.POST.get("back") == "site" and request.POST.get("code"):
        return redirect("infra_site", code=request.POST["code"])
    return _back("sites")


@require_POST
@staff_required(super_only=True)
def site_delete_view(request, code):
    _run(request, ops.site_delete, _("Site deleted."), code)
    return _back("sites")


# --- hosts -----------------------------------------------------------------------------------------


@require_POST
@staff_required(super_only=True)
def host_add_view(request):
    """Add a planned host; its hardware is read (ssh, IPMI) to find or make its server profile."""
    from . import profiles

    form = HostForm(request.POST)
    if not form.is_valid():
        _form_errors(request, form)
        return _back("hosts", request.POST.get("site"))
    d = form.cleaned_data
    h, ok = _run(
        request,
        ops.host_save,
        None,
        d["site"],
        d["name"],
        d["profile"] or "",
        d["ip"],
        ipmi=d.get("ipmi") or "",
        ipmi_user=d.get("ipmi_user") or "",
        ipmi_port=d.get("ipmi_port") or 443,
    )
    if ok:
        note = profiles.place(request.user, h, chosen=d["profile"] or None)
        messages.success(request, _("Host %(h)s added as planned.") % {"h": h.name} + (" " + note if note else ""))
    return _back("hosts", d["site"], f"h-{d['name']}")


@require_POST
@staff_required(super_only=True)
def host_edit_view(request, site, name):
    from . import profiles

    form = HostEditForm(request.POST)
    if form.is_valid():
        d = form.cleaned_data
        h, ok = _run(
            request,
            ops.host_save,
            _("Host saved."),
            site,
            name,
            d["profile"] or "",
            d["ip"],
            new=False,
            ipmi=d.get("ipmi") or "",
            ipmi_user=d.get("ipmi_user") or "",
            rename=d["name"],
            ipmi_port=d.get("ipmi_port") or 443,
        )
        name = h.name if ok else name
        if ok and h.facts and h.profile:
            diffs = profiles.compare(h.profile, h.facts)
            if diffs:
                messages.warning(
                    request,
                    _("%(h)s does not match profile %(p)s: %(d)s")
                    % {"h": h.name, "p": h.profile.code, "d": "; ".join(diffs[:4])},
                )
    else:
        _form_errors(request, form)
    return _back("hosts", site, f"h-{name}")


@require_POST
@staff_required(Role.SUPPORT)
def host_detect_view(request, site, name):
    """Read the host's hardware now and check (or find, or make) its server profile."""
    from . import profiles

    h = get_object_or_404(Host, site__code=site, name=name)
    note = profiles.place(
        request.user, h, chosen=h.profile.code if h.profile else None, allow_create=request.user.is_super_admin
    )
    from . import hosthealth

    h.refresh_from_db()
    hosthealth.check([h])  # the health too (2026-10-29)
    (messages.success if h.facts and not h.facts_error else messages.warning)(request, note or _("Nothing changed."))
    return _back("hosts", site, f"h-{name}")


@require_POST
@staff_required(super_only=True)
def host_remove_view(request, site, name):
    _run(request, ops.host_remove, _("Host removed."), site, name)
    return _back("hosts", site)


# --- storage ---------------------------------------------------------------------------------------


@require_POST
@staff_required(super_only=True)
def storage_save_view(request, site, pk=None):
    st = get_object_or_404(Site, code=site)
    form = StorageForm(request.POST, site=st)
    if not form.is_valid():
        _form_errors(request, form)
        return _back("storage", site)
    d = form.cleaned_data
    obj, _ok = _run(
        request,
        ops.storage_save,
        _("Storage saved."),
        site,
        d["name"],
        {
            "kind": d["kind"],
            "targets": _lines(d["targets"]),
            "port": d.get("port"),
            "fstab": d["fstab"],
            "host": d.get("host"),
        },
        pk=pk,
    )
    return _back("storage", site, f"st-{obj.pk}" if obj else None)


@require_POST
@staff_required(super_only=True)
def storage_delete_view(request, site, pk):
    _run(request, ops.storage_delete, _("Storage deleted."), site, pk)
    return _back("storage", site)


@require_POST
@staff_required(super_only=True)
def volume_save_view(request, site, pk, vid=None):
    st = get_object_or_404(Storage, pk=pk, site__code=site)
    form = VolumeForm(request.POST)
    if form.is_valid():
        d = dict(form.cleaned_data, clients=_lines(form.cleaned_data["clients"]))
        _run(request, ops.volume_save, _("Volume saved."), st, d, pk=vid)
    else:
        _form_errors(request, form)
    return _back("storage", site, f"st-{st.pk}")


@require_POST
@staff_required(super_only=True)
def volume_delete_view(request, site, pk, vid):
    st = get_object_or_404(Storage, pk=pk, site__code=site)
    _run(request, ops.volume_delete, _("Volume deleted."), st, vid)
    return _back("storage", site, f"st-{st.pk}")


# --- platforms -------------------------------------------------------------------------------------


@require_POST
@staff_required(super_only=True)
def platform_save_view(request):
    new = request.POST.get("new") == "1"
    p = None if new else Platform.objects.filter(key=f"{request.POST.get('site')}.{request.POST.get('name')}").first()
    form = PlatformForm(
        request.POST,
        locked=bool(p and p.fe_locked),
        site=request.POST.get("site"),
        initial=platform_initial(p) if p else None,
    )
    name = request.POST.get("name")
    if form.is_valid():
        d = dict(form.cleaned_data)
        d["fe_nodes"] = _lines(d.get("fe_nodes"))
        obj, ok = _run(request, ops.platform_save, _("Platform saved."), d["site"], d["name"], d, new=new)
        rename = (request.POST.get("rename") or "").strip()
        if ok and not new and rename and rename != d["name"]:
            obj, ok = _run(request, ops.platform_rename, _("Platform renamed."), d["site"], d["name"], rename)
            if ok:
                name = rename
    else:
        _form_errors(request, form)
    return _back("platforms", request.POST.get("site"), f"pl-{name}")


@require_POST
@staff_required(super_only=True)
def platform_delete_view(request, site, name):
    _run(request, ops.platform_delete, _("Platform deleted."), site, name)
    return _back("platforms", site)


def _platform(site, name):
    return get_object_or_404(Platform.objects.select_related("site"), key=f"{site}.{name}")


@require_POST
@staff_required(super_only=True)
def datastore_save_view(request, site, name, pk=None):
    p = _platform(site, name)
    form = DatastoreForm(request.POST, platform=p)
    if form.is_valid():
        _run(request, ops.datastore_save, _("Datastore saved."), p, form.cleaned_data, pk=pk)
    else:
        _form_errors(request, form)
    return _back("platforms", site, f"pl-{name}")


@require_POST
@staff_required(super_only=True)
def datastore_delete_view(request, site, name, pk):
    p = _platform(site, name)
    _run(request, ops.datastore_delete, _("Datastore deleted."), p, pk)
    return _back("platforms", site, f"pl-{name}")


@require_POST
@staff_required(super_only=True)
def vnet_save_view(request, site, name, pk=None):
    p = _platform(site, name)
    form = VnetForm(request.POST, platform=p)
    if form.is_valid():
        _run(request, ops.vnet_save, _("Network saved."), p, form.cleaned_data, pk=pk)
    else:
        _form_errors(request, form)
    return _back("platforms", site, f"pl-{name}")


@require_POST
@staff_required(super_only=True)
def vnet_delete_view(request, site, name, pk):
    p = _platform(site, name)
    _run(request, ops.vnet_delete, _("Network deleted."), p, pk)
    return _back("platforms", site, f"pl-{name}")


# --- clusters --------------------------------------------------------------------------------------


@require_POST
@staff_required(super_only=True)
def cluster_add_view(request):
    form = ClusterForm(request.POST)
    if form.is_valid():
        d = form.cleaned_data
        site, platform = d["platform"].split(".", 1)
        hosts = [h.split("/", 1)[1] for h in d["hosts"] if h.split("/", 1)[0] == site]
        _run(
            request,
            ops.cluster_add,
            _("Cluster added as planned."),
            site,
            platform,
            d["name"],
            d["kind"],
            "",
            hosts,
            capacity={k: d[k] for k in ops.CAPACITY if k in d},
        )
        return _back("clusters", site, f"cl-{platform}-{d['name']}")
    _form_errors(request, form)
    return _back("clusters", (request.POST.get("platform") or "").split(".")[0])


@require_POST
@staff_required(super_only=True)
def cluster_save_view(request, site, platform, name):
    p = _platform(site, platform)
    c = p.clusters.filter(name=name).first()
    form = ClusterEditForm(request.POST, platform=p, cluster=c)
    if form.is_valid():
        d = form.cleaned_data
        _run(
            request,
            ops.cluster_save,
            _("Cluster saved."),
            site,
            platform,
            name,
            kind=d["kind"],
            datastores=[int(x) for x in d["datastores"]],
            networks=[int(x) for x in d["networks"]],
            hosts=d["hosts"] if d.get("hosts_shown") else None,
            capacity={k: d[k] for k in ops.CAPACITY if k in d},
        )
    else:
        _form_errors(request, form)
    return _back("clusters", site, f"cl-{platform}-{name}")


@require_POST
@staff_required(super_only=True)
def cluster_remove_view(request, site, platform, name):
    _run(request, ops.cluster_remove, _("Cluster removed."), site, platform, name)
    return _back("clusters", site)


@require_POST
@staff_required(super_only=True)
def cluster_hosts_view(request, site, platform, name):
    """Add existing free hosts to a cluster (new hosts are added on the Hosts tab)."""
    hosts = request.POST.getlist("hosts")
    if hosts:
        _run(request, ops.cluster_add_hosts, _("Hosts added to the cluster."), site, platform, name, hosts)
    return _back("clusters", site, f"cl-{platform}-{name}")


@require_POST
@staff_required(super_only=True)
def cluster_host_remove_view(request, site, platform, name, host):
    _run(request, ops.cluster_remove_host, _("Host taken out of the cluster."), site, platform, name, host)
    return _back("clusters", site, f"cl-{platform}-{name}")
