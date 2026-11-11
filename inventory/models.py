"""The provider's own infrastructure as the portal's records: sites, hosts, storage, platforms, networks, secrets,
core services.

**The records are the source**: they are entered and edited in the portal (`ops.py`, `edit_views.py`); the portal
imports and exports no inventory files (a redesign is planned). `Secret` values are encrypted with a key kept
outside the database (`inventory/crypto.py`), and every look at one is logged. An `IPSubnet`'s `max_prefix` is
enforced where addresses are handed out, not only where the subnet is defined.
"""
from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


# --- the infrastructure ----------------------------------------------------------------------

DEFAULT_TZ = "Asia/Almaty"
# the values of the site settings before 2026-10-27 (a city name); since then an IANA time zone, checked on input
LEGACY_TZ = {"almaty": "Asia/Almaty", "astana": "Asia/Almaty", "aqtobe": "Asia/Aqtobe", "atyrau": "Asia/Atyrau"}


def iana_zones():
    import zoneinfo
    return zoneinfo.available_timezones()


def site_tz(value):
    """The IANA time zone of a site's setting (correction 09): itself when valid, else a former city name, else Asia/Almaty."""
    v = (value or "").strip()
    if v in iana_zones():
        return v
    return LEGACY_TZ.get(v.lower(), DEFAULT_TZ)


class Site(models.Model):
    code = models.CharField(max_length=16, unique=True)
    number = models.PositiveSmallIntegerField(_("site code"), null=True, blank=True,
                                              help_text="numeric site code (resources/<site>.yaml: site_code), e.g. 81; "
                                                        "fixed once set; used in AS numbers 42SSS…")
    country = models.CharField(max_length=8, blank=True)
    city = models.CharField(max_length=100, blank=True)
    dc = models.CharField(max_length=8, blank=True)
    timezone = models.CharField(max_length=60, blank=True, help_text="IANA time zone, e.g. Asia/Almaty")
    ntp_servers = models.JSONField(default=list, blank=True, help_text="up to 4")
    dns_servers = models.JSONField(default=list, blank=True, help_text="up to 3, for normal operation")
    bootstrap_dns = models.JSONField(default=list, blank=True,
                                     help_text="up to 2, for a green-field deployment (only the claugine appliance is up)")
    # owner 2026-11-09: one edge cluster per site, chosen here. It carries what serves the whole site - the route
    # reflectors and the L2 gateway endpoints a VLAN lands on - so the site, not a platform, owns the choice.
    edge_cluster = models.ForeignKey("Cluster", on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="edge_for", verbose_name=_("edge cluster"),
                                     help_text=_("Where the route reflectors and the L2 gateway endpoints that "
                                                 "connect to VLANs run. One per site; empty means the site's "
                                                 "management platform carries them."))
    ipmi = models.JSONField(default=dict, blank=True, help_text="site IPMI settings: type, user (credential), settings")
    management = models.JSONField(default=dict, blank=True,
                                  help_text="management hosts (addresses, prefixes) and ports {vyos, ssh, rdp}")
    jump = models.JSONField(default=dict, blank=True,
                            help_text="ssh jump host to reach the site's endpoints from another site: host, port, account")
    log_collectors = models.JSONField(default=dict, blank=True,
                                      help_text="{ipmi|vyos|internal|public: {host, proto (tcp/udp), port}}")
    extra = models.JSONField(default=dict, blank=True, help_text="kept from the import: repos, ...")

    class Meta:
        ordering = ["code"]

    @property
    def tz(self):
        """The site's IANA time zone: the routers' and VMs' time zone (context and templates) and the IPMI's offset."""
        return site_tz(self.timezone)

    def __str__(self):
        return self.code


# correction 10 (2026-10-28): log collectors and ssh ports are lists (purpose, …) with required lines; other lines are
# optional, kept with their own purpose label. Keys: what the readers look for (vyos: the routers' syslog; ipmi: the
# hosts' IPMI; core: the core services); ssh ports: vyos (the VyOS VMs' management ssh), ssh (the core services' ssh).
LOG_REQUIRED = [("ipmi", _("IPMI")), ("vyos", _("VyOS VMs")), ("core", _("Core services"))]
LOG_COLLECTORS = LOG_REQUIRED  # the purposes every site has
SSH_REQUIRED = [("vyos", _("VyOS VMs")), ("ssh", _("Core services"))]
PURPOSE_ALIASES = {"ipmi": "ipmi", "vyos": "vyos", "vyos vms": "vyos", "vyos routers": "vyos", "core": "core",
                   "core services": "core", "internal": "core", "internal services": "core"}


ALIASES = ("nic-data1", "nic-data2", "nic-storage1", "nic-storage2")
DATA_MTU, STORAGE_MTU = 9100, 9000


class ServerProfile(models.Model):
    """A server model as it is installed: chassis, CPU, RAM, GPUs, boot disk with its volumes and network cards.

    Hosts of one profile must be the same; the check compares what each host reports (Host.facts) with it.
    ``model``, ``cpu_model`` and ``cores`` are derived (vendor + chassis, CPU vendor + name, sockets × cores).
    """
    code = models.CharField(max_length=32, unique=True)
    vendor = models.CharField(max_length=60, blank=True)
    chassis = models.CharField(max_length=140, blank=True)
    ipmi_type = models.CharField(max_length=16, blank=True)
    model = models.CharField(max_length=200, blank=True)
    cpu_vendor = models.CharField(max_length=60, blank=True)
    cpu_name = models.CharField(max_length=140, blank=True)
    cpu_model = models.CharField(max_length=200, blank=True)
    sockets = models.PositiveIntegerField(default=1)
    cores_per_socket = models.PositiveIntegerField(default=0)
    cpu_mhz = models.PositiveIntegerField(default=0)
    cores = models.PositiveIntegerField(default=0, help_text="sockets × cores")
    ram_gb = models.PositiveIntegerField(default=0)
    ram = models.JSONField(default=dict, blank=True, help_text="dimm: size_gb, freq, slots, modules")
    gpu_model = models.CharField(max_length=100, blank=True)
    gpu_count = models.PositiveIntegerField(default=0)
    gpu_vram_gb = models.PositiveIntegerField(default=0)
    boot = models.JSONField(default=dict, blank=True,
                            help_text="bus, controller, dev, size_gb, volumes [{name, size_gb, fs, path}]")
    nics = models.JSONField(default=dict, blank=True,
                            help_text="cards [{name, vendor, model, ports, speed, media, bus, slot, mtu, links[]}], "
                                      "bonds [{name, links[], mtu}]")
    reference_host = models.CharField(max_length=100, blank=True, help_text="the host the profile was made from")
    verified_at = models.DateTimeField(null=True, blank=True, help_text="verified: only the volumes can change")
    verified_by = models.CharField(max_length=150, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)
    check_result = models.JSONField(default=dict, blank=True, help_text="last consistency check: {host: [differences]}")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return self.code

    def save(self, *args, **kwargs):
        self.model = " ".join(x for x in (self.vendor, self.chassis) if x)
        self.cpu_model = " ".join(x for x in (self.cpu_vendor, self.cpu_name) if x)
        if self.cores_per_socket:
            self.cores = (self.sockets or 1) * self.cores_per_socket
        super().save(*args, **kwargs)

    @property
    def cards(self):
        return list((self.nics or {}).get("cards") or [])

    @property
    def fabrics(self):
        """2: data and storage fabrics (bond0, bond1); 1: one fabric, both traffics in bond0."""
        return 1 if int((self.nics or {}).get("fabrics") or 2) == 1 else 2

    @property
    def assign(self):
        """{alias: port}: nic-data1 → ens1f0np0 …"""
        return dict((self.nics or {}).get("assign") or {})

    @property
    def bonds(self):
        """The bonds the aliases make (bond0: data, bond1: storage); a profile without aliases keeps its own list."""
        a = self.assign
        if not a:
            return list((self.nics or {}).get("bonds") or [])
        out = [{"name": "bond0", "links": [a[x] for x in ("nic-data1", "nic-data2") if a.get(x)],
                "aliases": ["nic-data1", "nic-data2"], "mtu": DATA_MTU,
                "carries": "data and storage" if self.fabrics == 1 else "data"}]
        if self.fabrics == 2:
            out.append({"name": "bond1", "links": [a[x] for x in ("nic-storage1", "nic-storage2") if a.get(x)],
                        "aliases": ["nic-storage1", "nic-storage2"], "mtu": STORAGE_MTU, "carries": "storage"})
        return out

    def aliases_needed(self):
        return ALIASES if self.fabrics == 2 else ALIASES[:2]

    @property
    def assign_complete(self):
        a = self.assign
        return all(a.get(x) for x in self.aliases_needed())

    @property
    def assign_locked(self):
        """The aliases cannot be reassigned while an active host of a cluster uses the verified profile."""
        return bool(self.verified_at) and self.hosts.filter(state="active", clusters__isnull=False).exists()

    @property
    def boot_volumes(self):
        return list((self.boot or {}).get("volumes") or [])

    @property
    def gpu_slice(self):
        """CPU and RAM of one GPU share of the host (no host reserve)."""
        if not self.gpu_count:
            return None
        return {"cores": self.cores // self.gpu_count, "ram_gb": self.ram_gb // self.gpu_count}


class Host(models.Model):
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="hosts")
    name = models.CharField(max_length=100)
    profile = models.ForeignKey(ServerProfile, null=True, on_delete=models.SET_NULL, related_name="hosts")
    ip = models.CharField(max_length=64, blank=True)
    state = models.CharField(max_length=16, default="active", help_text="planned, active or retired")
    ipmi_host = models.CharField(max_length=64, blank=True)
    ipmi_type = models.CharField(max_length=16, blank=True, help_text="ilo6, ilo7, ...")
    ipmi_user = models.CharField(max_length=200, blank=True, help_text="secrets reference of the IPMI user")
    ipmi_port = models.PositiveIntegerField(_("IPMI https port"), default=443,
                                            help_text=_("Redfish over https; 443 unless the iLO/BMC listens elsewhere."))
    facts = models.JSONField(default=dict, blank=True, help_text="hardware as the host reports it (OS and IPMI)")
    facts_at = models.DateTimeField(null=True, blank=True)
    facts_error = models.CharField(max_length=300, blank=True)
    health = models.JSONField(default=dict, blank=True,
                              help_text="the IPMI's health of the host (inventory.hosthealth): ok, problems, bios, ipmi_fw, …")
    health_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("site", "name")]
        ordering = ["site", "name"]

    def __str__(self):
        return self.name

    @property
    def key(self):
        return f"{self.site.code}.{self.name}"

    @property
    def ipmi_url(self):
        """The iLO/BMC web interface (Redfish is always https)."""
        if not self.ipmi_host:
            return ""
        return f"https://{self.ipmi_host}" + (f":{self.ipmi_port}" if self.ipmi_port and self.ipmi_port != 443 else "") + "/"


class Segment(models.Model):
    """L2 segment of a site (resources/<site>/networks)."""

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="segments")
    name = models.CharField(max_length=100)
    vlan = models.PositiveIntegerField(null=True)
    cidr = models.CharField(max_length=43, blank=True)

    class Meta:
        unique_together = [("site", "name")]
        ordering = ["site", "vlan"]


class DelegatedSubnet(models.Model):
    """Public or corporate range announced through an exchange segment (bgp.subnets)."""

    class Purpose(models.TextChoices):
        INET = "inet", _("Internet")
        CORPNET = "corpnet", _("Corporate network")
        OTHER = "other", _("Other")

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="delegated_subnets")
    segment = models.CharField(max_length=100)
    cidr = models.CharField(max_length=43)
    purpose = models.CharField(max_length=16, choices=Purpose.choices)

    class Meta:
        ordering = ["site", "segment", "cidr"]

    def __str__(self):
        return self.cidr


class IPSubnet(models.Model):
    """u10 (owner 2026-11-02): a public/corporate IP range of a site, defined here (IP Subnets tab) and then carried by
    one exchange (ix) network. It is the source of the prefixes delegated to projects; its ``max`` is the largest block
    (smallest prefix length number) a project may be given. Immutable once added; cannot be deleted while it is in use."""

    class Purpose(models.TextChoices):
        INET = "inet", _("Internet")
        CORPNET = "corpnet", _("Corporate network")

    class Routing(models.TextChoices):
        BGP = "bgp", _("BGP (ix)")            # announced outside the platforms over BGP through the exchange
        STATIC = "static", _("Static gateway")  # routed by a static gateway (defined below)
        ORG = "org", _("Organization")        # the organization routes it; the portal only delegates the block

    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="ip_subnets")
    subnet = models.GenericIPAddressField(_("subnet"), protocol="IPv4")
    prefix = models.PositiveSmallIntegerField(_("prefix"))
    max_prefix = models.PositiveSmallIntegerField(
        _("max"), help_text=_("the largest block (smallest prefix length) that may be delegated to a project"))
    routing = models.CharField(_("routing"), max_length=8, choices=Routing.choices, default=Routing.BGP)
    static_gateway = models.GenericIPAddressField(_("static gateway"), protocol="IPv4", null=True, blank=True,
                                                  help_text=_("required when routing is a static gateway"))
    purpose = models.CharField(_("type"), max_length=8, choices=Purpose.choices)  # auto: private → corp, public → inet
    network = models.ForeignKey("Network", null=True, blank=True, on_delete=models.SET_NULL, related_name="ip_subnets",
                                help_text=_("the exchange (ix) network that carries this subnet"))
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["site", "subnet"]
        unique_together = [("site", "subnet", "prefix")]

    def __str__(self):
        return self.cidr

    @property
    def cidr(self):
        return f"{self.subnet}/{self.prefix}"

    @property
    def max_prefix_ceiling(self):
        """owner 2026-11-03: the biggest block (smallest prefix number) a project may be delegated from this subnet
        = subnet prefix + 2, but never bigger than a /24."""
        return min(max(self.prefix + 2, 24), 31)

    @property
    def net(self):
        import ipaddress
        return ipaddress.ip_network(self.cidr, strict=False)

    @staticmethod
    def detect_purpose(subnet, prefix):
        """Type from the address: a private (RFC1918) range is corporate, a public one is internet."""
        import ipaddress
        try:
            return IPSubnet.Purpose.CORPNET if ipaddress.ip_network(f"{subnet}/{prefix}", strict=False).is_private \
                else IPSubnet.Purpose.INET
        except ValueError:
            return IPSubnet.Purpose.INET

    def prefix_choices(self):
        """The prefix lengths a project may pick from this subnet: 31, 29, 28 … down to ``max`` (/30 is skipped)."""
        return [n for n in [31] + list(range(29, self.max_prefix - 1, -1)) if n >= self.prefix]

    def save(self, *args, **kwargs):
        self.purpose = self.detect_purpose(self.subnet, self.prefix)
        super().save(*args, **kwargs)


def _edge_services_deployed(site):
    """Owner 2026-11-09: the edge choice is fixed once edge services run on it.

    "Deployed" means a VM actually exists - a node with `deployed_vm_id` - on an active service of a type that the
    edge cluster carries: the route reflectors and the core gateways that terminate the site's VLANs. A service
    that is only planned does not lock anything.
    """
    from django.db.models import Q
    EDGE_TYPES = ("bgp-rr", "cloud-egw", "cloud-igw", "cloud-gw", "portal-gw")
    return (CoreService.objects.filter(site=site, state="active", stype__in=EDGE_TYPES)
            .filter(Q(nodes__deployed_vm_id__isnull=False)).exists())


def _gateways_deployed(platform):
    """The gateway choice is fixed once a project has been provisioned on the platform: its egw, cgw and the
    overlay side of its L2 gateways live on the chosen cluster, and moving the choice would strand them."""
    from tenants.models import Deployment
    return Deployment.objects.filter(platform_key=platform.key, state="done").exists()


class Platform(models.Model):
    key = models.CharField(max_length=100, unique=True, help_text="<site>.<platform>")
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="platforms")
    name = models.CharField(max_length=64)
    kind = models.CharField(max_length=16)
    state = models.CharField(max_length=16, blank=True)
    fe_name = models.CharField(max_length=100, blank=True)
    fe_mode = models.CharField(max_length=16, blank=True)
    fe_vip = models.CharField(max_length=64, blank=True)
    fe_secrets = models.CharField(max_length=200, blank=True, help_text="fe.yaml: secrets - reference to the FE secrets")
    fe_primary = models.CharField(max_length=100, blank=True)
    segments = models.JSONField(default=list, blank=True,
                                help_text="Site networks this platform is connected to (derived from its networks).")
    provider = models.CharField(max_length=60, blank=True)
    sa = models.CharField(_("super admins"), max_length=200, blank=True,
                          help_text="secrets reference of the platform's super admin accounts")
    shared_owner = models.CharField(max_length=60, blank=True)
    # owner 2026-11-09: one gateway cluster per platform, chosen here rather than ticked on each cluster. A
    # selector cannot express "two", so the limit holds by construction instead of by validation. A platform may
    # have none: the project's own cluster then carries its routers, which is right where there is only one.
    gateway_cluster = models.ForeignKey("Cluster", on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name="gateway_for",
                                        verbose_name=_("gateway cluster"),
                                        help_text=_("Where the project gateways run - the egw, the cgw and the "
                                                    "overlay side of an L2 gateway - wherever the project itself "
                                                    "is placed. Shared clusters only; empty means the project's "
                                                    "own cluster."))
    fe = models.JSONField(default=dict, blank=True, help_text="front end: type (vm, host), nodes, nic, hostname, empty_bridges")
    # public endpoints (shared platforms): one URL for FireEdge and the APIs, one for what VMs call (OneGate, OneFlow)
    public_url = models.URLField(_("public URL"), blank=True,
                                 help_text=_("FireEdge, gRPC API and XML-RPC API, e.g. https://one-dc1.claugine.com; "
                                             "each on its port below. Shared platforms only."))
    fireedge_port = models.PositiveIntegerField(_("FireEdge port"), null=True, blank=True)
    grpc_port = models.PositiveIntegerField(_("gRPC API port"), null=True, blank=True)
    xmlrpc_port = models.PositiveIntegerField(_("XML-RPC API port"), null=True, blank=True)
    vm_url = models.URLField(_("public URL for VMs"), blank=True,
                             help_text=_("OneGate and OneFlow, e.g. https://one-dc1.claugine.com; each on its port below."))
    onegate_port = models.PositiveIntegerField(_("OneGate port"), null=True, blank=True)
    oneflow_port = models.PositiveIntegerField(_("OneFlow port"), null=True, blank=True)

    class Meta:
        ordering = ["site", "name"]

    @staticmethod
    def _at(url, port, path=""):
        if not url:
            return ""
        from urllib.parse import urlsplit
        u = urlsplit(url.rstrip("/"))
        host = u.hostname or ""
        return f"{u.scheme}://{host}{f':{port}' if port else (f':{u.port}' if u.port else '')}{path}"

    @property
    def endpoints(self):
        """The public endpoints as (label, URL), in a fixed order; only those that are set."""
        rows = [("FireEdge", self._at(self.public_url, self.fireedge_port)),
                ("gRPC API", self._at(self.public_url, self.grpc_port) if self.grpc_port else ""),
                ("XML-RPC API", self._at(self.public_url, self.xmlrpc_port, "/RPC2") if self.xmlrpc_port else ""),
                ("OneGate", self._at(self.vm_url, self.onegate_port) if self.onegate_port else ""),
                ("OneFlow", self._at(self.vm_url, self.oneflow_port) if self.oneflow_port else "")]
        return [(k, v) for k, v in rows if v]

    @property
    def fireedge_link(self):
        """Where tenants open OpenNebula: the public URL on the FireEdge port."""
        return self._at(self.public_url, self.fireedge_port)

    def __str__(self):
        return self.key

    @property
    def frontend(self):
        """The OpenNebula front end this platform belongs to: a secondary zone belongs to its primary."""
        return self.fe_primary if self.fe_mode == "secondary" and self.fe_primary else self.key

    @property
    def fe_locked(self):
        """Deployed: the platform's definition is fixed (only its state changes)."""
        return self.state in ("", "deployed", "updated")

    @property
    def is_active(self):
        """Installed and in service: deployed, or deployed with changes pending (updated)."""
        return self.state in ("", "deployed", "updated")

    @property
    def display_state(self):
        return "active" if self.is_active else (self.state or "active")

    def delegated_subnets(self, purpose=None):
        qs = DelegatedSubnet.objects.filter(site=self.site, segment__in=self.segments or [])
        return qs.filter(purpose=purpose) if purpose else qs


class Cluster(models.Model):
    class Kind(models.TextChoices):
        MGMT = "mgmt", _("mgmt: the cluster of the management platform")
        SHARED = "shared", _("shared: projects are created on it")
        DEDICATED = "dedicated", _("dedicated: one organization or project")
        PLATFORM = "platform", _("platform: managed by an external platform")
        EDGE = "edge", _("edge: hosts network functions")
        EXT_MANAGED = "ext-managed", _("ext-managed: managed by Claugine, its payload by a third-party system")

    platform = models.ForeignKey(Platform, on_delete=models.CASCADE, related_name="clusters")
    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=16)
    quota_code = models.CharField(max_length=16, blank=True,
                                  help_text="kept for the orders and quotas of existing projects; not edited any more")
    state = models.CharField(max_length=16, default="active", help_text="planned, active or retired")
    hosts = models.ManyToManyField(Host, related_name="clusters")
    vcpu_ratio = models.PositiveSmallIntegerField(_("vCores per pCore"), default=10,  # a whole number (owner 2026-10-29 20:59)
                                     help_text=_("How many vCPUs one physical core (no hyperthreads) gives."))
    spare_hosts = models.PositiveSmallIntegerField(_("spare hosts (N+X)"), default=1,
                                                   help_text=_("X: hosts left out of the available resources; none is "
                                                               "a dedicated spare."))
    reserved_cpu_pct = models.PositiveSmallIntegerField(_("reserved CPU, %"), default=5)
    reserved_ram_pct = models.PositiveSmallIntegerField(_("reserved RAM, %"), default=5)
    # owner 2026-11-09 (gpu-bundles): on a GPU cluster the unit that is ordered is a bundle - one GPU card with a
    # fixed share of its host's cores and RAM - so that share is a setting of the cluster, not something worked out
    # from the reserve percentages at read time. Those percentages and vcpu_ratio do not apply to a GPU cluster:
    # the head-room is already inside the bundle, and counting it twice would advertise capacity nobody can order.
    # 0 means "not set" - fall back to the hardware's default, which is what a cluster with no hosts yet has.
    bundle_cores = models.PositiveSmallIntegerField(_("vCores per GPU bundle"), default=0)
    bundle_ram_gb = models.PositiveSmallIntegerField(_("GB RAM per GPU bundle"), default=0)
    # owner 2026-11-09. Two kinds of cluster carry network functions, and they are told apart by what the
    # endpoints connect to:
    #   edge cluster (kind="edge")  the route reflectors, and the l2gw endpoints that connect to **VLANs**
    #   gateway cluster (this flag) the egw and cgw, and the l2gw endpoints that connect to **organizations'
    #                               subnets** - the overlays
    # A cluster is one `kind`, so this has to be a flag: the cluster carrying the gateways is usually an
    # ordinary shared one. Unmarked, these endpoints followed the project onto its own cluster, so a GPU project
    # put an egw on a GPU cluster, competing for hosts that should be running workloads.
    datastores = models.ManyToManyField("PlatformDatastore", related_name="clusters", blank=True)
    networks = models.ManyToManyField("PlatformNetwork", related_name="clusters", blank=True)

    class Meta:
        unique_together = [("platform", "name")]
        ordering = ["platform", "name"]

    def __str__(self):
        return self.name

    def gpu_cards(self):
        """GPU cards in this cluster's active hosts, and how many sit in the spare hosts.

        The spare hosts are the largest ones - the same ones ``inventory.capacity`` holds back from CPU and RAM. A
        spare exists to absorb a failure, so what it carries is not available to order (owner 2026-11-09).
        """
        per = [{"cores": h.profile.cores, "ram": h.profile.ram_gb, "gpus": h.profile.gpu_count}
               for h in self.hosts.all() if h.state == "active" and h.profile]
        spare = sorted(per, key=lambda x: (x["cores"], x["ram"]), reverse=True)[:min(self.spare_hosts or 0, len(per))]
        total, in_spare = sum(x["gpus"] for x in per), sum(x["gpus"] for x in spare)
        return {"total": total, "spare": in_spare, "available": max(total - in_spare, 0)}

    def gpu_model(self):
        """The GPU model its active hosts carry - "H200", or "H200 + L40S" on the rare mixed cluster."""
        models_ = sorted({(h.profile.gpu_model or "").strip() for h in self.hosts.all()
                          if h.state == "active" and h.profile and h.profile.gpu_count} - {""})
        return " + ".join(models_)

    def has_gpu(self):
        return self.gpu_cards()["total"] > 0

    def default_bundle(self):
        """What this cluster's hardware suggests: one host's cores and RAM divided by that host's own GPU cards.

        Per host, not a cluster average (owner 2026-11-09): a GPU is passed through from the machine it sits in, so
        a bundle has to fit on one host. A GPU cluster's hosts are alike, so the smallest is also the typical one;
        on a mixed cluster the smallest is the only safe answer. 10% head-room; RAM down to a multiple of 8 GB.
        """
        import math
        best = None
        for h in self.hosts.all():
            if h.state != "active" or not h.profile or not h.profile.gpu_count:
                continue
            g = h.profile.gpu_count
            one = {"cores": math.floor(h.profile.cores / g * 0.9),
                   "ram_gb": math.floor(h.profile.ram_gb / g * 0.9 / 8) * 8}
            if best is None or (one["cores"], one["ram_gb"]) < (best["cores"], best["ram_gb"]):
                best = one
        return best or {"cores": 0, "ram_gb": 0}

    def bundle(self):
        """The bundle in force: what staff set, or the hardware's default until they do."""
        d = self.default_bundle()
        return {"cores": self.bundle_cores or d["cores"], "ram_gb": self.bundle_ram_gb or d["ram_gb"]}

    @property
    def orderable(self):
        return (self.kind == "shared" and self.platform.kind == "shared" and self.state == "active"
                and self.platform.is_active)

    @property
    def order_code(self):
        """How orders name the cluster: its quota code (older tenants) or its name."""
        return self.quota_code or self.name


# --- storage of a site and what platforms make of it -----------------------------------------------

class Storage(models.Model):
    """A storage system of a site: NFS (one target), iSCSI (several targets and a port) or local disks of a host."""

    class Kind(models.TextChoices):
        NFS = "nfs", "NFS"
        ISCSI = "iscsi", "iSCSI"
        LOCAL = "local", _("local")

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="storages")
    name = models.CharField(_("name"), max_length=100)
    kind = models.CharField(_("type"), max_length=8, choices=Kind.choices)
    targets = models.JSONField(default=list, blank=True, help_text="NFS: one address; iSCSI: several")
    port = models.PositiveIntegerField(_("iSCSI port"), null=True, blank=True)
    fstab = models.CharField(_("fstab parameters"), max_length=300, blank=True)
    host = models.ForeignKey(Host, null=True, blank=True, on_delete=models.PROTECT, related_name="storages",
                             help_text="local: the host the disks are in")
    state = models.CharField(max_length=16, default="active")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("site", "name")]
        ordering = ["site", "kind", "name"]

    def __str__(self):
        return self.name

    @property
    def total_gb(self):
        return sum(v.size_gb or 0 for v in self.volumes.all())


class StorageVolume(models.Model):
    """A volume of a storage: NFS export, iSCSI device (with the initiators' IQNs) or a local device."""
    storage = models.ForeignKey(Storage, on_delete=models.CASCADE, related_name="volumes")
    name = models.CharField(_("name"), max_length=100)
    device = models.CharField(_("export or device"), max_length=200, blank=True,
                              help_text="NFS: export path; iSCSI and local: device as lsblk lists it")
    size_gb = models.PositiveIntegerField(_("size, GiB"), default=0)
    clients = models.JSONField(default=list, blank=True, help_text="NFS: client networks; iSCSI: initiator IQNs")
    mount = models.CharField(_("mount point"), max_length=200, blank=True)

    class Meta:
        unique_together = [("storage", "name")]
        ordering = ["storage", "name"]

    def __str__(self):
        return f"{self.storage.name}/{self.name}"


class PlatformDatastore(models.Model):
    class Kind(models.TextChoices):
        VMS = "vms", _("vms")
        IMAGES = "images", _("images")
        FILES = "files", _("files")
        BACKUPS = "backups", _("backups")

    platform = models.ForeignKey(Platform, on_delete=models.CASCADE, related_name="datastores")
    name = models.CharField(_("name"), max_length=100)
    kind = models.CharField(_("type"), max_length=16, choices=Kind.choices)
    volume = models.ForeignKey(StorageVolume, null=True, blank=True, on_delete=models.PROTECT, related_name="datastores")
    path = models.CharField(_("path"), max_length=200, blank=True)
    reserved_pct = models.PositiveSmallIntegerField(_("reserved, %"), default=10,
                                                    help_text=_("Kept free: 10% by default, 20% for backups."))
    # backups: where restic keeps them (an iSCSI volume behind the restic VM, or an S3 bucket) and its secrets
    class Backend(models.TextChoices):
        ISCSI = "iscsi", "iSCSI"
        S3 = "s3", "S3"

    backend = models.CharField(_("backend"), max_length=8, choices=Backend.choices, blank=True)
    restic_ip = models.GenericIPAddressField(_("restic VM address"), protocol="IPv4", null=True, blank=True,
                                             help_text=_("The VM that provides the restic service (its first NIC)."))
    s3_endpoint = models.CharField(_("S3 endpoint"), max_length=200, blank=True, help_text=_("IP or host name."))
    s3_bucket = models.CharField(_("S3 bucket"), max_length=100, blank=True)
    s3_secret = models.CharField(_("S3 secret"), max_length=40, blank=True, help_text=_("A secret of type S3."))
    restic_secret = models.CharField(_("restic password"), max_length=40, blank=True, help_text=_("A secret of type password."))
    extra = models.JSONField(default=dict, blank=True, help_text="kept from the import: service, ...")

    class Meta:
        unique_together = [("platform", "name")]
        ordering = ["platform", "kind", "name"]

    def __str__(self):
        return self.name


class PlatformNetwork(models.Model):
    """A virtual network of a platform: an 802.1Q VLAN of a site network, or a link to an existing bridge."""

    class Kind(models.TextChoices):
        VLAN = "vlan", _("vlan: 802.1q network")
        BRIDGE = "bridge", _("bridge: linked to an existing bridge")

    platform = models.ForeignKey(Platform, on_delete=models.CASCADE, related_name="vnets")
    name = models.CharField(_("name"), max_length=100)
    kind = models.CharField(_("type"), max_length=8, choices=Kind.choices, default=Kind.VLAN)
    network = models.ForeignKey("Network", null=True, blank=True, on_delete=models.PROTECT, related_name="vnets")
    bridge = models.CharField(_("bridge"), max_length=32, blank=True)
    ranges = models.ManyToManyField("NetworkRange", blank=True, related_name="vnets")

    class Meta:
        unique_together = [("platform", "name")]
        ordering = ["platform", "name"]

    def __str__(self):
        return self.name


# --- control of platforms: mode and endpoint (not part of the inventory) -------

ONE_XMLRPC_PORT = 2633


class PlatformControl(models.Model):
    """How the portal talks to one OpenNebula installation, and whether it may write.

    ``key`` is the inventory platform key (e.g. ``dc1.cloud``) or a free name
    for an endpoint that is not in the inventory (e.g. ``stage``).
    """

    class Mode(models.TextChoices):
        ADOPTION = "adoption", _("Read-only: read; write only to portal test projects")
        EXECUTION = "execution", _("Execution: the portal provisions projects")

    key = models.CharField(max_length=100, unique=True)
    mode = models.CharField(max_length=16, choices=Mode.choices, default=Mode.ADOPTION)
    xmlrpc_url = models.CharField(_("XML-RPC endpoint"), max_length=300, blank=True)
    username = models.CharField(max_length=100, blank=True)
    password_encrypted = models.TextField(blank=True)
    collect_enabled = models.BooleanField(_("collect project data"), default=True)
    nf_image = models.CharField(_("network function image"), max_length=200, default="VyOS Router 2026.03",
                                help_text=_("OpenNebula image the egw and igw of projects are created from."))
    nr_template = models.CharField(_("native router template"), max_length=200, default="Service Virtual Router",
                                   help_text=_("OpenNebula template (and the image of the same name) of the native igw: "
                                               "the OpenNebula Service Virtual Router."))
    last_collected_at = models.DateTimeField(null=True, blank=True)
    # owner 2026-11-11: portal and platform consistency, checked at every refresh (tenants/consistency.py)
    consistency = models.JSONField(default=dict, blank=True)
    last_error = models.TextField(blank=True)
    health_ok = models.BooleanField(null=True, blank=True)
    health_checked_at = models.DateTimeField(null=True, blank=True)
    health = models.JSONField(default=dict, blank=True, help_text="version, image {id, state}, problems")
    vms = models.JSONField(default=list, blank=True,
                           help_text="the platform's own VMs (group oneadmin), read with the core services: name, id, "
                                     "vcpu, ram_gb, disks_gb, nics [network, ip], state")
    vms_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]

    def __str__(self):
        return self.key

    def set_password(self, raw):
        from .crypto import encrypt
        self.password_encrypted = encrypt(raw) if raw else ""

    def get_password(self):
        from .crypto import decrypt
        return decrypt(self.password_encrypted) if self.password_encrypted else ""

    # --- endpoint: from the inventory and its secrets, or entered by hand (endpoints outside the inventory)

    @property
    def platform(self):
        if not hasattr(self, "_platform"):
            self._platform = Platform.objects.filter(key=self.key).first()
        return self._platform

    @property
    def endpoint_url(self):
        p = self.platform
        if p is not None:
            return f"http://{p.fe_vip}:{ONE_XMLRPC_PORT}/RPC2" if p.fe_vip and p.fe_vip != "0.0.0.0" else ""
        return self.xmlrpc_url

    @property
    def secrets_ref(self):
        p = self.platform
        return p.fe_secrets if p is not None else ""

    def credentials(self):
        """(url, "user:password") to connect with. Raises SecretsError when the inventory's secrets do not resolve."""
        if self.platform is not None:
            from .credentials import value
            from .credentials import SecretsError
            if not self.endpoint_url:
                raise SecretsError(f"{self.key}: fe.yaml has no vip")
            password = value(self.secrets_ref, "oneadmin")
            if not password:
                raise SecretsError(f"{self.secrets_ref}: no oneadmin password")
            return self.endpoint_url, f"oneadmin:{password}"
        return self.xmlrpc_url, f"{self.username}:{self.get_password()}"

    @property
    def fireedge_url(self):
        p = self.platform
        if p is not None and p.fe_vip and p.fe_vip != "0.0.0.0":
            return f"http://{p.fe_vip}:2616/fireedge/sunstone"
        return ""

    @property
    def credential_defined(self):
        """The OpenNebula oneadmin password is kept in the portal (Infrastructure → Secrets → OpenNebula)."""
        from .credentials import has
        return (self.platform is not None and has(self.secrets_ref, "oneadmin")) or (
            self.platform is None and bool(self.password_encrypted))

    @property
    def configured(self):
        if self.platform is not None:
            from .credentials import has
            return bool(self.endpoint_url and self.secrets_ref and has(self.secrets_ref, "oneadmin"))
        return bool(self.xmlrpc_url and self.username and self.password_encrypted)


class Secret(models.Model):
    """A secret of the infrastructure, by type (Infrastructure → Secrets): users, BGP, OpenNebula, public keys.

    Contributors (what uses a secret) refer to it by id: ``secret:<id>`` (BGP: ``secret:<id>#rrs|igw|egw``), so the
    name can change; older references by name still resolve (inventory.credentials). Values are encrypted
    with FIELD_ENCRYPTION_KEY. Status: new (not used), used, replaced (contributors moved to another secret, the old
    one may still be in use on systems), archived (not used; kept for the record).
    """

    class Kind(models.TextChoices):
        USER = "user", _("Users")
        BGP = "bgp", _("BGP")
        ONE = "opennebula", _("OpenNebula")
        KEY = "key", _("Keys")
        PASSWORD = "password", _("Passwords")
        S3 = "s3", "S3"

    class State(models.TextChoices):
        ACTIVE = "active", _("active")
        REPLACED = "replaced", _("replaced")
        ARCHIVED = "archived", _("archived")

    NET_TYPES = [("ix", "ix"), ("evpn", "evpn")]
    OS_USER, OS_UID, ONE_USER, ONE_UID = "oneadmin", 9869, "oneadmin", 0

    kind = models.CharField(max_length=16, choices=Kind.choices)
    name = models.CharField(_("name"), max_length=200, help_text=_("Unique within its type."))
    state = models.CharField(max_length=16, choices=State.choices, default=State.ACTIVE)
    replaced_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="replaces")
    purpose = models.TextField(_("purpose"), blank=True)
    # users
    username = models.CharField(_("user name"), max_length=100, blank=True)
    password_encrypted = models.TextField(blank=True)
    private_key_encrypted = models.TextField(blank=True)
    public_key = models.TextField(_("public key"), blank=True, help_text=_("Calculated from the private key (users); the key itself (keys)."))
    # keys
    key_type = models.CharField(_("type"), max_length=40, blank=True)
    comment = models.CharField(_("comment"), max_length=200, blank=True)
    # BGP: one exchange or EVPN network of a site
    net_type = models.CharField(_("type"), max_length=8, choices=NET_TYPES, blank=True)
    site = models.CharField(max_length=16, blank=True)
    network = models.CharField(max_length=64, blank=True)
    rrs_encrypted = models.TextField(blank=True)
    igw_encrypted = models.TextField(blank=True)
    egw_encrypted = models.TextField(blank=True)
    # OpenNebula front end: the OS user oneadmin, its database, OpenNebula's oneadmin
    os_password_encrypted = models.TextField(blank=True)
    os_private_key_encrypted = models.TextField(blank=True)
    db_name = models.CharField(_("database"), max_length=64, blank=True, default="")
    db_user = models.CharField(_("database user"), max_length=64, blank=True, default="")
    db_password_encrypted = models.TextField(blank=True)
    one_password_encrypted = models.TextField(blank=True)
    # S3: access key and secret key (passwords keep theirs in password_encrypted)
    s3_key_encrypted = models.TextField(blank=True)
    s3_secret_encrypted = models.TextField(blank=True)
    extra = models.JSONField(default=dict, blank=True, help_text="what is not secret: known_hosts of an ssh target, ...")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    archived_at = models.DateTimeField(null=True, blank=True)
    archived_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    SECRET_FIELDS = ("password", "private_key", "rrs", "igw", "egw", "os_password", "os_private_key", "db_password",
                     "one_password", "s3_key", "s3_secret")

    class Meta:
        unique_together = [("kind", "name")]
        ordering = ["kind", "name"]

    def __str__(self):
        return self.name

    @property
    def ref(self):
        return f"secret:{self.pk}"

    def get(self, field):
        from .crypto import decrypt
        v = getattr(self, f"{field}_encrypted")
        return decrypt(v) if v else ""

    def put(self, field, raw):
        from .crypto import encrypt
        setattr(self, f"{field}_encrypted", encrypt(raw) if raw else "")

    def has(self, field):
        return bool(getattr(self, f"{field}_encrypted"))

    def length(self, field):
        return len(self.get(field)) if self.has(field) else 0

    @property
    def private_key(self):
        return self.get("private_key")


class ServiceImage(models.Model):
    """An OpenNebula image or template a platform needs for the portal's services (site → Platforms → Service images).

    Rows with a role: the VyOS image of the network functions and VyOS core services (mandatory: only its name and
    customization can be changed), the Service Virtual Router template of the native igw and that template's image
    (its name is the template's; both can be deleted together). The id and status come from the platform (health
    check). The names are what deployments look up (nf_image, nr_template).
    """

    class Kind(models.TextChoices):
        IMAGE = "image", _("image")
        TEMPLATE = "template", _("template")

    VYOS, NR_TEMPLATE, NR_IMAGE = "vyos", "one-router-template", "one-router-image"
    TYPES = [("vyos", "vyos"), ("one-router", "one-router"), ("base-os", "base-os")]  # what the image is for
    MANDATORY = (  # role, purpose, kind, default name
        (VYOS, "vyos", "image", "VyOS Router 2026.03"),
        (NR_TEMPLATE, "one-router", "template", "Service Virtual Router"),
        (NR_IMAGE, "one-router", "image", "Service Virtual Router"),
    )

    control = models.ForeignKey(PlatformControl, on_delete=models.CASCADE, related_name="service_images")
    role = models.CharField(max_length=32, blank=True, help_text="a mandatory row: vyos, one-router-template, one-router-image")
    purpose = models.CharField(_("type"), max_length=100, choices=TYPES,
                               help_text=_("vyos: routers and VyOS core services; one-router: the native router; "
                                           "base-os: the core services on a base OS (ns, logs, admin, backup)."))
    kind = models.CharField(_("image or template"), max_length=16, choices=Kind.choices, default=Kind.IMAGE)
    name = models.CharField(_("name"), max_length=200)
    one_id = models.IntegerField(null=True, blank=True)
    state = models.CharField(max_length=32, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)
    position = models.PositiveIntegerField(default=100)

    class Customization(models.TextChoices):
        CONTEXT = "context", _("context")
        SSH = "ssh", _("ssh")

    customization = models.CharField(
        _("customization"), max_length=8, choices=Customization.choices, default=Customization.SSH,
        help_text=_("How a template is applied to a VM made from it: context — through a context variable at first boot "
                    "(VyOS, like the projects' network functions); ssh — turned into a script run over ssh (a base OS)."))

    class Meta:
        ordering = ["position", "pk"]

    def __str__(self):
        return f"{self.control.key}: {self.purpose} {self.kind} {self.name}"

    @property
    def mandatory(self):
        """The VyOS image: the tenants' network functions and the VyOS core services are made from it."""
        return self.role == self.VYOS

    @property
    def name_editable(self):
        return self.role != self.NR_IMAGE

    @property
    def label(self):
        return f"{self.control.key} · {self.name}"


# --- configuration templates of network functions (Infrastructure → Templates) ----------------

class ConfigTemplate(models.Model):
    """Source of the configuration of a network function VM (VyOS) or of a host service (FRR).

    The body is a Django template rendered with the context of a tenant (egw, igw, cgw, L2 sides) or of
    a site's exchange network (route reflectors, host FRR); see inventory/nfconfig.py for the variables.
    Secrets come from Credentials and are masked unless revealed by the super admin.
    """

    class Role(models.TextChoices):
        EGW = "egw", _("egw: internet edge of a project")
        IGW = "igw", _("igw: project gateway")
        CGW = "cgw", _("cgw: corporate network edge of a project")
        L2A = "l2-a", _("L2 channel, project side")
        L2B = "l2-b", _("L2 channel, external side")
        L2BR = "l2-br", _("L2 channel, VLAN-to-network bridge (dedicated edge cluster)")
        RR_INET = "rr-inet", _("route reflector of the internet exchange")
        RR_CORPNET = "rr-corpnet", _("route reflector of the corporate exchange")
        RR_VXLAN = "rr-vxlan", _("route reflector of VXLAN/EVPN")
        CLOUD_IGW = "cloud-igw", _("cloud-igw: the site's igw (core service cloud-gw): the project igw with the management VRF")
        HOST_FRR = "host-frr", _("FRR of a KVM host (VXLAN/EVPN)")
        CS_NS = "core-services-ns", _("core service: name servers (base OS)")
        CS_LOGS = "core-services-graylog", _("core service: log collector, Graylog (base OS)")
        CS_ADMIN = "core-services-admin", _("core service: admin jump hosts (base OS)")
        CS_BACKUP = "core-services-backup", _("core service: backup storage, restic (base OS)")
        VYOS_IMAGE_PREPARE = "vyos-image-prepare", _("VyOS image: prepare the installed image (run once on the ISO)")
        VYOS_BOOTUP = "vyos-postconfig-bootup", _("VyOS image: first boot from the OpenNebula context")

    class Os(models.TextChoices):
        VYOS = "vyos", "VyOS"
        FRR = "frr", "FRR"
        BASE = "base-os", _("base OS")

    role = models.CharField(max_length=32, choices=Role.choices, unique=True)
    name = models.CharField(max_length=100)
    os = models.CharField(_("target"), max_length=16, choices=Os.choices, default=Os.VYOS,
                          help_text=_("What the configuration is for: VyOS routers or FRR on KVM hosts."))
    os_version = models.CharField(max_length=60, blank=True, help_text=_("Image or release the template was taken from."))
    description = models.TextField(blank=True)
    body = models.TextField()
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["role"]

    def __str__(self):
        return self.name

    IMAGE_ROLES = ("vyos-image-prepare", "vyos-postconfig-bootup")  # scripts of the VyOS image: kept as they are

    @property
    def scope(self):
        if self.role in self.IMAGE_ROLES:
            return "image"
        return "tenant" if self.role in ("egw", "igw", "cgw", "l2-a", "l2-b", "l2-br") else "site"


# --- core services of a site ------------------------------------------------------------------------------------

class CoreService(models.Model):
    """A service the site itself runs (name servers, jump hosts, front ends, route reflectors, gateways, backups, …).

    on hosts: deployed on hosts that are already there (their OS), e.g. the front end of the management platform or
    NTP on its hosts; from scratch: its own VMs are made for it — all of the same size and networks."""

    class Kind(models.TextChoices):
        HOST = "host", _("on host")
        VM = "vm", "vm"

    class State(models.TextChoices):
        PLANNED = "planned", _("planned")
        ACTIVE = "active", _("active")
        RETIRED = "retired", _("retired")

    class Type(models.TextChoices):  # what it is: each type has its own edit form (inventory/core_types.py)
        BGP_RR = "bgp-rr", _("bgp-rr: route reflectors of a BGP network")
        CLOUD_EGW = "cloud-egw", _("cloud-egw: internet edge of the core services")
        CLOUD_IGW = "cloud-igw", _("cloud-igw: gateway of the core services")
        CLOUD_GW = "cloud-gw", _("cloud-gw: the core services' egw and igw (two VMs)")
        PORTAL_GW = "portal-gw", _("portal-gw: the egws of a portal platform (two VMs, VRRP on the public overlays)")
        NS = "ns", _("ns: name servers")
        LOGS = "logs", _("logs: log collector")
        ADMIN = "admin", _("admin: admin jump hosts")
        BACKUP = "backup", _("backup: backup storage of a datastore")
        CORE_VM = "core-vm", _("core-vm: any other VMs or hosts")

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="core_services")
    stype = models.CharField(_("service type"), max_length=16, choices=Type.choices, default=Type.CORE_VM)
    params = models.JSONField(default=dict, blank=True,
                              help_text="what the type's form asks (network, volumes, first address, instances, datastore …)")
    name = models.SlugField(_("name"), max_length=60, help_text=_("A short name without spaces, e.g. cloud-vxlan-rrs."))
    kind = models.CharField(_("deployment"), max_length=8, choices=Kind.choices, default=Kind.VM)
    state = models.CharField(_("state"), max_length=16, choices=State.choices, default=State.ACTIVE)
    description = models.TextField(_("description"), blank=True)
    template = models.ForeignKey("ConfigTemplate", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
                                 verbose_name=_("template"), help_text=_("The template its configuration is made from."))
    platform = models.ForeignKey(Platform, null=True, blank=True, on_delete=models.SET_NULL, related_name="core_services",
                                 verbose_name=_("platform hosting the VMs"), help_text=_("From scratch: where its VMs run."))
    serves = models.ForeignKey(Platform, null=True, blank=True, on_delete=models.SET_NULL, related_name="served_by",
                               verbose_name=_("platform served"),
                               help_text=_("The platform the service contributes to; empty: a common (core) service of the site."))
    image = models.ForeignKey("ServiceImage", null=True, blank=True, on_delete=models.SET_NULL, related_name="core_services",
                              verbose_name=_("image"), help_text=_("From scratch: the image its VMs are made from."))
    ssh_port = models.PositiveIntegerField(_("ssh port"), default=22)
    ssh_secret = models.CharField(_("ssh secret"), max_length=200, blank=True,
                                  help_text=_("The user secret the portal logs in to its hosts and VMs with."))
    service_secret = models.CharField(_("service secret"), max_length=200, blank=True,
                                      help_text=_("The secret the service itself uses (e.g. a BGP password)."))
    firewall = models.JSONField(_("firewall rules"), default=list, blank=True,
                                help_text='ufw rules of a base-OS service: [{"port": "22", "proto": "tcp", "source": "10.0.0.0/8"}, …]')
    # from scratch: every VM of the service has these resources and networks
    vcpu = models.PositiveSmallIntegerField(_("vCPU"), null=True, blank=True)
    ram_gb = models.PositiveIntegerField(_("RAM, GB"), null=True, blank=True)
    disk_gb = models.PositiveIntegerField(_("disk, GB"), null=True, blank=True)
    nic1_network = models.ForeignKey("Network", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
                                     verbose_name=_("NIC 1 network"))
    nic1_gateway = models.GenericIPAddressField(_("NIC 1 gateway"), protocol="IPv4", null=True, blank=True)
    more_nics = models.JSONField(_("further NICs"), default=list, blank=True,
                                 help_text='NIC 2, 3 …: [{"network": <site network id>, "gateway": "" (empty: the subnet\'s)}, …]')
    routes = models.JSONField(_("routes"), default=list, blank=True,
                              help_text='[{"nic": 1, "subnet": "10.0.0.0/8", "via": "10.1.1.1", "metric": 20}, …]')
    config_diff = models.TextField(_("differences from the template"), blank=True,
                                   help_text=_("set/delete lines applied after the template: what this service has "
                                               "beyond the standard configuration"))
    checks = models.JSONField(_("checks"), default=dict, blank=True,
                              help_text="what a read verifies on every host/VM: units (systemd, active), ports (listening), "
                                        "ntp (synchronized), bgp (ipv4 | evpn: every peer established)")
    read_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        unique_together = [("site", "name")]
        ordering = ["site", "name"]

    def __str__(self):
        return f"{self.name}_{self.site.code}"

    def nic_list(self):
        """[(Network or None, gateway)] of NIC 1, 2 … as defined."""
        if not hasattr(self, "_nics"):
            rows = [(self.nic1_network_id, self.nic1_gateway or "")] if self.nic1_network_id else []
            rows += [(x.get("network"), x.get("gateway") or "") for x in (self.more_nics or []) if x.get("network")]
            nets = {n.pk: n for n in Network.objects.filter(pk__in=[int(r[0]) for r in rows])}
            self._nics = [(nets.get(int(a)), gw) for a, gw in rows]
        return self._nics

    def bgp_secret(self):
        """The secret of the BGP passwords of the service's network (its peer groups use its slots: secret:<id>#rrs …),
        when they all use one; "" otherwise."""
        net = self.service_network()
        if net is None or not net.has_bgp:
            return ""
        refs = {(g.password or "").split("#")[0] for g in net.peer_groups.all() if g.password}
        return refs.pop() if len(refs) == 1 else ""

    def service_network(self):
        """The network the service works on: its first NIC that is not on the management network (cloud-vyos)."""
        from .nfconfig import MGMT_NETWORK
        return next((n for n, _gw in self.nic_list() if n is not None and n.name != MGMT_NETWORK), None)

    def mgmt_nic(self):
        """True when NIC 1 is on the site's management network (a VyOS service defined the current way)."""
        from .nfconfig import MGMT_NETWORK
        nics = self.nic_list()
        return bool(nics) and nics[0][0] is not None and nics[0][0].name == MGMT_NETWORK


class CoreServiceNode(models.Model):
    """A host or VM of a core service and how the portal logs in to it (read only)."""
    service = models.ForeignKey(CoreService, on_delete=models.CASCADE, related_name="nodes")
    name = models.CharField(_("name"), max_length=100, help_text=_("The VM's name on the platform, or the host's name."))
    address = models.CharField(_("address"), max_length=255, help_text=_("IP address or host name."))
    hostname = models.CharField(_("host name"), max_length=100, blank=True)
    ips = models.JSONField(_("addresses"), default=list, blank=True,
                           help_text="its address on NIC 1, 2 … as defined (empty: assigned when the VM is made)")
    login_hash = models.TextField(blank=True, help_text="encrypted: the crypt hash of its login user's password, taken from "
                                                        "the running VM so a redeployed one keeps it")
    ssh_port = models.PositiveIntegerField(_("ssh port"), default=22)
    secret = models.CharField(_("secret"), max_length=200, blank=True, help_text=_("The user secret to log in with."))
    host = models.ForeignKey(Host, null=True, blank=True, on_delete=models.SET_NULL, related_name="core_nodes")
    facts = models.JSONField(default=dict, blank=True,
                             help_text="read: hostname, os, vcpu, ram_gb, disk_gb, addresses; vm: the platform's view")
    facts_at = models.DateTimeField(null=True, blank=True)
    facts_error = models.CharField(max_length=300, blank=True)
    deployed_vm_id = models.IntegerField(null=True, blank=True,
                                         help_text="the platform VM the portal made for it (a redeploy): compliant while it runs")
    params = models.JSONField(default=dict, blank=True,
                              help_text="a VM of a two-VM service (cloud-gw): role (egw, igw), nics [network ids], checks")

    class Meta:
        ordering = ["service", "name"]

    @property
    def role(self):
        """egw or igw for a VM of cloud-gw; else the service's template role."""
        return (self.params or {}).get("role") or (self.service.template.role if self.service.template else "")

    def nic_list(self):
        """[(Network or None, gateway)] of the VM's NIC 1, 2 …: its own layout (cloud-gw) or its service's."""
        ids = (self.params or {}).get("nics")
        if not ids:
            return self.service.nic_list()
        if not hasattr(self, "_nics"):
            nets = {n.pk: n for n in Network.objects.filter(pk__in=[int(x) for x in ids])}
            self._nics = [(nets.get(int(x)), "") for x in ids]
        return self._nics

    def __str__(self):
        return self.name


# --- site networks (kept in the portal; imported once from resources/<site>/networks) ------------------

class Network(models.Model):
    """A VLAN of a site: the core networks (one of each core-… type per site, and any number of plain core ones),
    internet/corporate exchanges (ix), EVPN/VXLAN underlays (evpn) and storage (nfs, iscsi)."""

    class Kind(models.TextChoices):
        CORE_SERVICES = "core-services", _("core-services: public addresses of the core services (cloud-services)")
        CORE_CLOUD = "core-cloud", _("core-cloud: the core servers' VMs (cloud-core)")
        CORE_VYOS = "core-vyos", _("core-vyos: VyOS management (cloud-vyos)")
        CORE_S2S = "core-s2s", _("core-s2s: L2 VLAN to network (cloud-s2s)")
        CORE_KVM = "core-kvm", _("core-kvm: KVM hosts (one-core)")
        CORE = "core", _("core")
        IX = "ix", _("ix")
        EVPN = "evpn", _("evpn")
        NFS = "nfs", "nfs"
        ISCSI = "iscsi", "iscsi"
        PUBLIC = "public", _("public: a public overlay of a portal platform (portal-publicNN, one per delegated subnet)")

    ONE_PER_SITE = ("core-services", "core-cloud", "core-vyos", "core-s2s", "core-kvm")
    BY_NAME = {"cloud-services": "core-services", "cloud-core": "core-cloud", "cloud-vyos": "core-vyos",
               "cloud-s2s": "core-s2s", "one-core": "core-kvm", "data-nfs": "nfs", "data-iscsi": "iscsi"}

    class State(models.TextChoices):
        PLANNED = "planned", _("planned")
        ACTIVE = "active", _("active")
        RETIRED = "retired", _("retired")

    IX_NAMES = ("cloud-ix", "portal-ix", "corpnet-ix")
    EVPN_NAMES = ("cloud-vxlan", "portal-vxlan")

    site = models.CharField(max_length=16, help_text="site code (the site index is rebuilt from the inventory)")
    name = models.CharField(_("name"), max_length=64)
    kind = models.CharField(_("type"), max_length=16, choices=Kind.choices)
    interface = models.CharField(_("interface"), max_length=32)
    vlan = models.PositiveIntegerField(_("VLAN"))
    mtu = models.PositiveIntegerField(_("MTU"), default=1500)
    subnet = models.GenericIPAddressField(_("subnet"), protocol="IPv4")
    prefix = models.PositiveSmallIntegerField(_("prefix"))
    gateway = models.GenericIPAddressField(_("gateway"), protocol="IPv4", null=True, blank=True)
    state = models.CharField(_("state"), max_length=16, choices=State.choices, default=State.ACTIVE)
    local_asn = models.BigIntegerField(_("local AS"), null=True, blank=True)
    remote_asn = models.BigIntegerField(_("remote AS"), null=True, blank=True)
    announced = models.JSONField(default=list, blank=True, help_text="ix: subnets announced to the remote AS")
    rrs = models.JSONField(default=list, blank=True, help_text="route reflectors (addresses)")
    extra = models.JSONField(default=dict, blank=True, help_text="kept from the import: bridge, routes, dns")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        unique_together = [("site", "name")]
        ordering = ["site", "kind", "vlan", "name"]

    def __str__(self):
        return f"{self.name}_{self.site}"

    @classmethod
    def kind_for(cls, name):
        if name in cls.IX_NAMES:
            return cls.Kind.IX
        if name in cls.EVPN_NAMES:
            return cls.Kind.EVPN
        return cls.BY_NAME.get(name, cls.Kind.CORE)

    @property
    def cidr(self):
        return f"{self.subnet}/{self.prefix}"

    @property
    def net(self):
        import ipaddress
        return ipaddress.ip_network(self.cidr, strict=False)

    @property
    def has_bgp(self):
        return self.kind in (self.Kind.IX, self.Kind.EVPN)

    @property
    def peer_role(self):
        """The ibgp peer group of the gateways on an exchange: cgw on the corporate one, egw on the others."""
        return "cgw" if self.name.startswith("corpnet") else "egw"

    def default_asn(self, suffix):
        """42 SSS VVV xx: site code, third octet of the subnet, 00 single AS / 01 remote / 02 internal."""
        s = Site.objects.filter(code=self.site).first()
        if not s or not s.number:
            return None
        return int(f"42{s.number:03d}{int(str(self.subnet).split('.')[2]):03d}{suffix}")

    @property
    def family(self):
        return "portal" if self.name.startswith("portal") else "cloud"

    def password_ref(self, group):
        """The reference of a peer group's password: the slot of the network's BGP secret it uses."""
        slot = {"rrs": "rrs", "ext": "egw"}.get(group, "igw")
        sec = Secret.objects.filter(kind=Secret.Kind.BGP, site=self.site, network=self.name).exclude(
            state=Secret.State.ARCHIVED).first()
        return f"{sec.ref}#{slot}" if sec else f"secret:bgp:{self.site}.{self.name}#{slot}"


class NetworkRange(models.Model):
    """An address range of a site network: used for OpenNebula address ranges and BGP peer groups."""
    network = models.ForeignKey(Network, on_delete=models.CASCADE, related_name="ranges")
    name = models.CharField(_("name"), max_length=32)
    start = models.GenericIPAddressField(_("start"), protocol="IPv4")
    length = models.PositiveIntegerField(_("length"))

    class Meta:
        unique_together = [("network", "name")]
        ordering = ["network", "start"]

    def __str__(self):
        return self.name

    @property
    def first(self):
        import ipaddress
        return ipaddress.ip_address(self.start)

    @property
    def last(self):
        return self.first + (self.length - 1)

    def cidrs(self):
        """The range as the fewest networks (for BGP peer ranges and firewall groups)."""
        import ipaddress
        return [str(n) for n in ipaddress.summarize_address_range(self.first, self.last)]


class PeerGroup(models.Model):
    """A BGP peer group of an exchange or EVPN network: who peers (a range) and the password (a credential)."""
    network = models.ForeignKey(Network, on_delete=models.CASCADE, related_name="peer_groups")
    name = models.CharField(max_length=16)
    session = models.CharField(max_length=8, default="ibgp", help_text="ibgp or ebgp")
    range = models.ForeignKey(NetworkRange, null=True, blank=True, on_delete=models.SET_NULL, related_name="peer_groups",
                              help_text="the range the hosts are, when they are exactly one")
    hosts = models.JSONField(default=list, blank=True, help_text="peer prefixes as in the file")
    password = models.CharField(max_length=200, blank=True, help_text="secrets reference of the password (Credentials)")

    class Meta:
        unique_together = [("network", "name")]
        ordering = ["network", "session", "name"]

    def cidrs(self):
        return list(self.hosts or []) or (self.range.cidrs() if self.range_id else [])

    @property
    def password_defined(self):
        from .credentials import has
        return bool(self.password) and has(self.password)


