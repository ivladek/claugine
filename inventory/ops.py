"""Editing the infrastructure records (Infrastructure → Sites, Server profiles): the portal's records are the source.

Items follow a simple life cycle: planned -> active -> retired. Removing something that is not deployed yet
deletes it; removing something active marks it retired, so the provisioning engine can take it down. Deleting
is refused while anything still refers to the item. Every change is audited.
"""
import ipaddress
import re

from django.db import transaction
from django.db.models import ProtectedError

from accounts.models import audit

from .models import (Cluster, Host, Network, Platform, PlatformDatastore, PlatformNetwork, ServerProfile, Site, Storage,
                     StorageVolume)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")


class EditError(Exception):
    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = errors or []


def _site(code):
    s = Site.objects.filter(code=code).first()
    if s is None:
        raise EditError(f"Site {code} does not exist.")
    return s


# --- server profiles -------------------------------------------------------------------------------

PROFILE_FIELDS = ("vendor", "chassis", "ipmi_type", "cpu_vendor", "cpu_name", "sockets", "cores_per_socket", "cpu_mhz",
                  "ram_gb", "gpu_model", "gpu_count", "gpu_vram_gb", "boot", "nics", "ram", "reference_host")


def profile_save(user, code, data, new=False):
    p = ServerProfile.objects.filter(code=code).first()
    if new and p is not None:
        raise EditError(f"Server profile {code} already exists.")
    if not new and p is None:
        raise EditError(f"Server profile {code} does not exist.")
    p = p or ServerProfile(code=code)
    for f in PROFILE_FIELDS:
        if f in data and data[f] is not None:
            setattr(p, f, data[f])
    if not p.gpu_count:
        p.gpu_model, p.gpu_vram_gb = "", 0
    p.save()
    audit(user, "profile_saved", p, code=code, new=new)
    return p


def profile_delete(user, code):
    p = ServerProfile.objects.filter(code=code).first()
    if p is None:
        raise EditError(f"Server profile {code} does not exist.")
    if p.hosts.exists():
        raise EditError(f"Server profile {code} is used by {', '.join(h.name for h in p.hosts.all()[:5])}.")
    p.delete()
    audit(user, "profile_deleted", None, code=code)


# --- sites -----------------------------------------------------------------------------------------

def _addresses(vals):
    if isinstance(vals, str):
        vals = vals.replace(",", " ").split()
    return [str(x) for x in vals or [] if str(x).strip()]


def site_save(user, code, data, new=False):
    s = Site.objects.filter(code=code).first()
    if new and s is not None:
        raise EditError(f"Site {code} already exists.")
    if not new and s is None:
        raise EditError(f"Site {code} does not exist.")
    s = s or Site(code=code)
    number = data.get("number")
    if number:
        if s.number and int(s.number) != int(number):
            raise EditError(f"The site code of {code} is {s.number}: it cannot be changed.")
        other = Site.objects.filter(number=int(number)).exclude(code=code).first()
        if other:
            raise EditError(f"Site code {number} is already used by {other.code}.")
        s.number = int(number)
    elif new:
        raise EditError("Give the site code (1-999).")
    for f in ("country", "city", "dc"):
        if f in data:
            setattr(s, f, str(data[f] or ""))
    if "edge_cluster" in data:
        # owner 2026-11-09: "selection cannot be changed if any edge service deployed"
        from .models import _edge_services_deployed
        want = (data.get("edge_cluster") or "").strip()
        have = s.edge_cluster.name if s.edge_cluster_id else ""
        if want != have:
            if s.pk and _edge_services_deployed(s):
                raise EditError(f"{code} has edge services deployed: its edge cluster cannot be changed.")
            if not want:
                s.edge_cluster = None
            else:
                c = Cluster.objects.filter(platform__site=s, name=want).exclude(state="retired").first()
                if c is None:
                    raise EditError(f"{want} is not a cluster of {code}.")
                s.edge_cluster = c
    if data.get("timezone"):
        s.timezone = data["timezone"]
    if "ntp_servers" in data:
        s.ntp_servers = _addresses(data["ntp_servers"])[:4]
    if "dns_servers" in data:
        s.dns_servers = _addresses(data["dns_servers"])[:3]
    if "bootstrap_dns" in data:
        s.bootstrap_dns = _addresses(data["bootstrap_dns"])[:1]  # one green-field DNS server (correction 10)
    if "mgmt_hosts" in data:
        m = dict(s.management or {})
        m["hosts"] = list(data.get("mgmt_hosts") or [])
        if isinstance(data.get("ssh_ports"), list):  # [{key, purpose, port}] (correction 10)
            m["ports"] = {x["key"]: int(x["port"]) for x in data["ssh_ports"] if x.get("key")}
            m["ports_extra"] = [{"purpose": x["purpose"], "port": int(x["port"])} for x in data["ssh_ports"] if not x.get("key")]
        elif "vyos_port" in data or "mgmt_ssh_port" in data:
            from .nfconfig import MGMT_PORTS
            m["ports"] = {"vyos": int(data.get("vyos_port") or MGMT_PORTS["vyos"]),
                          "ssh": int(data.get("mgmt_ssh_port") or MGMT_PORTS["ssh"])}
        s.management = m
    from .models import LOG_COLLECTORS
    if isinstance(data.get("log_collectors"), list):  # [{key, purpose, host, proto, port}] (correction 10)
        logs = {}
        for x in data["log_collectors"]:
            k = x.get("key") or re.sub(r"[^a-z0-9]+", "-", x["purpose"].lower()).strip("-") or "other"
            logs[k] = {"host": x["host"], "proto": x["proto"], "port": int(x["port"]), "label": x["purpose"]}
        s.log_collectors = logs
    elif "log_host" in data or any(f"log_{k}_host" in data for k, _l in LOG_COLLECTORS):
        logs = {}  # one collector (host) with a port per purpose; kept per purpose for the readers
        for k, _l in LOG_COLLECTORS:
            host = (data.get("log_host") or data.get(f"log_{k}_host") or "").strip()
            if host and data.get(f"log_{k}_port"):
                logs[k] = {"host": host, "proto": data.get(f"log_{k}_proto") or "udp", "port": int(data.get(f"log_{k}_port"))}
        s.log_collectors = logs
    s.jump = {}  # no jump host between the sites any more (2026-10-27: every host is reached from either site)
    s.save()
    audit(user, "site_saved", s, code=code, new=new)
    return s


def site_ipmi_save(user, site, settings, hosts):
    """Site IPMI settings and the IPMI user/type of its hosts. hosts: {name: {"user": ref, "type": "ilo6"}}."""
    s = _site(site)
    with transaction.atomic():
        if settings is not None:
            s.ipmi = {k: v for k, v in settings.items() if v not in (None, "")}
            s.save(update_fields=["ipmi"])
        for name, upd in hosts.items():
            h = Host.objects.filter(site=s, name=name).first()
            if h is None:
                continue
            if upd.get("user"):
                h.ipmi_user = upd["user"]
            if upd.get("type"):
                h.ipmi_type = upd["type"]
            h.save(update_fields=["ipmi_user", "ipmi_type"])
    audit(user, "site_ipmi_saved", s, hosts=len(hosts))
    return s


def site_delete(user, code):
    s = _site(code)
    inside = s.hosts.count() + s.platforms.count() + s.storages.count() + Network.objects.filter(site=code).count()
    if inside:
        raise EditError(f"Site {code} still has hosts, networks, storage or platforms ({inside}).")
    s.delete()
    audit(user, "site_deleted", None, code=code)


# --- hosts -----------------------------------------------------------------------------------------

def host_save(user, site, name, profile, ip, state="planned", new=True, ipmi=None, ipmi_user=None, rename=None,
              ipmi_port=None):
    s = _site(site)
    h = Host.objects.filter(site=s, name=name).first()
    if new and h is not None:
        raise EditError(f"Host {name} already exists on {site}.")
    if not new and h is None:
        raise EditError(f"Host {name} does not exist on {site}.")
    if new and not NAME_RE.match(name):
        raise EditError(f"Bad host name {name!r}.")
    h = h or Host(site=s, name=name, state=state)
    if rename and rename != h.name:
        if h.state != "planned":
            raise EditError(f"Only a planned host can be renamed; {name} is {h.state}.")
        if not NAME_RE.match(rename) or Host.objects.filter(site=s, name=rename).exists():
            raise EditError(f"Host name {rename!r} is not valid or already used on {site}.")
        h.name = rename
    if profile is not None:
        h.profile = ServerProfile.objects.filter(code=profile).first() if profile else None
    h.ip = ip
    if ipmi is not None:
        h.ipmi_host = ipmi
    if ipmi_user is not None:
        h.ipmi_user = ipmi_user
    if ipmi_port is not None:
        h.ipmi_port = int(ipmi_port)
    h.save()
    audit(user, "host_saved", h, host=h.key, new=new)
    return h


def host_remove(user, site, name):
    """Delete a host nothing refers to; an active host still in a cluster is retired instead."""
    h = Host.objects.filter(site__code=site, name=name).first()
    if h is None:
        raise EditError(f"Host {name} does not exist on {site}.")
    used = list(h.clusters.all())
    if h.storages.exists():
        raise EditError(f"Host {name} has local storage; delete it first.")
    if not used:
        h.delete()
        audit(user, "host_deleted", None, host=f"{site}.{name}")
        return None
    if h.state == "planned":
        raise EditError(f"Host {name} is used by {', '.join(c.name for c in used[:5])}; take it out of its clusters first.")
    h.state = "retired"
    h.save(update_fields=["state"])
    audit(user, "host_retired", h, host=h.key)
    return h


# --- storage ---------------------------------------------------------------------------------------

def _check_ip(x, what):
    try:
        return str(ipaddress.ip_address(str(x).strip()))
    except ValueError:
        raise EditError(f"{what}: {x} is not an address.")


def storage_save(user, site, name, data, pk=None):
    """data: kind, targets [..], port, fstab, host (local). Volumes are edited one by one."""
    s = _site(site)
    st = Storage.objects.filter(pk=pk, site=s).first() if pk else None
    if pk and st is None:
        raise EditError("No such storage.")
    if not NAME_RE.match(name or ""):
        raise EditError(f"Bad storage name {name!r}.")
    if Storage.objects.filter(site=s, name=name).exclude(pk=pk).exists():
        raise EditError(f"Storage {name} already exists on {site}.")
    kind = data["kind"]
    targets = [_check_ip(x, "target") for x in data.get("targets") or []]
    host = None
    if kind == Storage.Kind.NFS:
        if len(targets) != 1:
            raise EditError("NFS storage has exactly one target address.")
    elif kind == Storage.Kind.ISCSI:
        if not targets:
            raise EditError("iSCSI storage needs at least one target address.")
    else:
        targets = []
        host = Host.objects.filter(site=s, name=data.get("host") or "").first()
        if host is None:
            raise EditError("Local storage belongs to a host of the site: choose it.")
    st = st or Storage(site=s)
    if st.pk and st.kind != kind and st.volumes.exists():
        raise EditError("The type of a storage with volumes cannot be changed.")
    st.name, st.kind, st.targets, st.host = name, kind, targets, host
    st.port = int(data.get("port") or 3260) if kind == Storage.Kind.ISCSI else None
    st.fstab = (data.get("fstab") or "").strip()
    st.save()
    audit(user, "storage_saved", st, storage=f"{site}.{name}", kind=kind)
    return st


def storage_delete(user, site, pk):
    st = Storage.objects.filter(pk=pk, site__code=site).first()
    if st is None:
        raise EditError("No such storage.")
    used = PlatformDatastore.objects.filter(volume__storage=st).select_related("platform")
    if used:
        raise EditError(f"Storage {st.name} is used by datastores {', '.join(f'{d.platform.key}/{d.name}' for d in used[:5])}.")
    name = st.name
    st.delete()
    audit(user, "storage_deleted", None, storage=f"{site}.{name}")


def volume_save(user, storage, data, pk=None):
    v = StorageVolume.objects.filter(pk=pk, storage=storage).first() if pk else None
    if pk and v is None:
        raise EditError("No such volume.")
    name = data["name"]
    if not NAME_RE.match(name or ""):
        raise EditError(f"Bad volume name {name!r}.")
    if storage.volumes.filter(name=name).exclude(pk=pk).exists():
        raise EditError(f"Volume {name} already exists in {storage.name}.")
    clients = [str(x).strip() for x in data.get("clients") or [] if str(x).strip()]
    if storage.kind == Storage.Kind.NFS:
        for c in clients:
            try:
                ipaddress.ip_network(c, strict=False)
            except ValueError:
                raise EditError(f"NFS client {c}: an address or a network.")
    elif storage.kind == Storage.Kind.ISCSI:
        for c in clients:
            if not re.match(r"^(iqn\.\d{4}-\d{2}\.[^\s]+|eui\.[0-9a-fA-F]{16})$", c):
                raise EditError(f"{c} is not an iSCSI initiator name (iqn.yyyy-mm.…).")
    else:
        clients = []
    if not data.get("device"):
        raise EditError("Give the export (NFS) or the device (iSCSI, local).")
    v = v or StorageVolume(storage=storage)
    v.name, v.device, v.size_gb, v.clients = name, data["device"].strip(), int(data.get("size_gb") or 0), clients
    v.mount = (data.get("mount") or "").strip()
    v.save()
    audit(user, "volume_saved", v, volume=f"{storage.site.code}.{storage.name}/{name}")
    return v


def volume_delete(user, storage, pk):
    v = StorageVolume.objects.filter(pk=pk, storage=storage).first()
    if v is None:
        raise EditError("No such volume.")
    try:
        v.delete()
    except ProtectedError:
        raise EditError(f"Volume {v.name} is used by datastores "
                        f"{', '.join(d.name for d in v.datastores.all()[:5])}.")
    audit(user, "volume_deleted", None, volume=f"{storage.site.code}.{storage.name}/{v.name}")


# --- platforms -------------------------------------------------------------------------------------

def platform_save(user, site, name, data, new=False):
    s = _site(site)
    key = f"{site}.{name}"
    p = Platform.objects.filter(key=key).first()
    if new and p is not None:
        raise EditError(f"Platform {key} already exists.")
    if not new and p is None:
        raise EditError(f"Platform {key} does not exist.")
    if new:
        p = Platform(key=key, site=s, name=name, kind=data["kind"], state="planned",
                     provider=(Platform.objects.exclude(provider="").values_list("provider", flat=True).first() or ""))
    was_deployed = not new and (p.state or "deployed") in ("deployed", "updated")
    if not new and data.get("state"):
        p.state = data["state"]
    mode, primary = p.fe_mode or "local", p.fe_primary
    if was_deployed:  # the front-end mode and primary of a deployed platform are fixed
        if (p.fe_mode and data.get("fe_mode") and data["fe_mode"] != mode) or \
                (data.get("fe_primary") and data["fe_primary"] != primary):
            raise EditError(f"{key} is deployed: its front-end mode and primary cannot be changed.")
    else:
        mode = data.get("fe_mode") or "local"
        primary = data.get("fe_primary") if mode == "secondary" else ""
        if mode == "secondary" and not primary:
            raise EditError("A secondary front end needs its primary.")
        if primary == key:
            raise EditError("A platform cannot be its own primary.")
    p.fe_mode, p.fe_primary = mode, primary or ""
    p.fe_name = data.get("fe_name") or p.fe_name or f"{name}_{site}"
    p.fe_vip = data.get("fe_vip") or p.fe_vip or "0.0.0.0"
    for f in ("fe_secrets", "sa"):
        if data.get(f) is not None:
            setattr(p, f, data[f])
    fe = dict(p.fe or {})
    for f in ("type", "hostname", "nic"):
        if data.get(f"fe_{f}") is not None:
            fe[f] = data[f"fe_{f}"]
    if data.get("fe_nodes") is not None:
        fe["nodes"] = list(data["fe_nodes"])
    fe.setdefault("type", "vm")
    p.fe = fe
    if "gateway_cluster" in data:
        # owner 2026-11-09: "selection cannot be changed if any gatway deoloyed"
        from .models import _gateways_deployed
        want = (data.get("gateway_cluster") or "").strip()
        have = p.gateway_cluster.name if p.gateway_cluster_id else ""
        if want != have:
            if p.pk and _gateways_deployed(p):
                raise EditError(f"{key} has projects deployed: its gateway cluster cannot be changed.")
            if not want:
                p.gateway_cluster = None
            else:
                # owner 2026-11-09: any cluster of the platform - the mgmt and ext-managed platforms carry their
                # own gateways, so restricting this to kind="shared" was wrong and left production unrepresentable.
                c = Cluster.objects.filter(platform=p, name=want).exclude(state="retired").first()
                if c is None:
                    raise EditError(f"{want} is not a cluster of {key}.")
                p.gateway_cluster = c
    for f in ("public_url", "vm_url"):  # public endpoints: shared platforms only
        if f in data:
            setattr(p, f, (data[f] or "").strip() if p.kind == "shared" else "")
    for f in ("fireedge_port", "grpc_port", "xmlrpc_port", "onegate_port", "oneflow_port"):
        if f in data:
            setattr(p, f, data[f] if p.kind == "shared" else None)
    p.save()
    audit(user, "platform_saved", p, platform=key, new=new)
    return p


@transaction.atomic
def platform_rename(user, site, old, new):
    """Rename a platform: its key <site>.<name> changes everywhere the portal keeps it (its control and service images,
    secondaries' primary, tenants' orders and deployments, L2 channels, collected tenant data). The front-end (zone)
    name on the platform is not changed. Names derived from the platform's name (e.g. the <name>-services network) are
    looked up by the new name."""
    from adoption.models import Observation, PlatformStats
    from tenants.models import Deployment, L2Channel, Tenant

    from .models import PlatformControl
    if not re.match(r"^[a-z][a-z0-9_-]{1,30}$", new or ""):
        raise EditError("A platform name: lowercase letters, digits, - and _ (2-31), starting with a letter.")
    old_key, new_key = f"{site}.{old}", f"{site}.{new}"
    p = Platform.objects.select_for_update().filter(key=old_key).first()
    if p is None:
        raise EditError(f"Platform {old_key} does not exist.")
    if Platform.objects.filter(key=new_key).exists() or PlatformControl.objects.filter(key=new_key).exists():
        raise EditError(f"Platform {new_key} already exists.")
    p.name, p.key = new, new_key
    p.save(update_fields=["name", "key"])
    n = {"control": PlatformControl.objects.filter(key=old_key).update(key=new_key),
         "primaries": Platform.objects.filter(fe_primary=old_key).update(fe_primary=new_key),
         "deployments": Deployment.objects.filter(platform_key=old_key).update(platform_key=new_key),
         "l2": L2Channel.objects.filter(mgmt_platform=old_key).update(mgmt_platform=new_key),
         "observations": Observation.objects.filter(platform_key=old_key).update(platform_key=new_key),
         "stats": PlatformStats.objects.filter(platform_key=old_key).update(platform_key=new_key), "tenants": 0}
    for t in Tenant.objects.filter(order__platform=old_key):
        t.order = {**t.order, "platform": new_key}
        t.save(update_fields=["order"])
        n["tenants"] += 1
    audit(user, "platform_renamed", p, old=old_key, new=new_key, **{k: str(v) for k, v in n.items()})
    return p


def platform_delete(user, site, name):
    p = Platform.objects.filter(key=f"{site}.{name}").first()
    if p is None:
        raise EditError(f"Platform {site}.{name} does not exist.")
    if str(p.state or "deployed") != "planned":
        raise EditError("Only a planned platform can be deleted; mark an installed one retired instead.")
    p.delete()
    audit(user, "platform_deleted", None, platform=f"{site}.{name}")


def _mark_updated(p):
    if str(p.state or "deployed") == "deployed":
        p.state = "updated"
        p.save(update_fields=["state"])


def datastore_save(user, platform, data, pk=None):
    ds = PlatformDatastore.objects.filter(pk=pk, platform=platform).first() if pk else None
    if pk and ds is None:
        raise EditError("No such datastore.")
    name = data["name"]
    if not NAME_RE.match(name or ""):
        raise EditError(f"Bad datastore name {name!r}.")
    if platform.datastores.filter(name=name).exclude(pk=pk).exists():
        raise EditError(f"Datastore {name} already exists on {platform.key}.")
    vol = StorageVolume.objects.filter(pk=data.get("volume") or 0, storage__site=platform.site).first()
    s3 = data["kind"] == PlatformDatastore.Kind.BACKUPS and data.get("backend") == PlatformDatastore.Backend.S3
    if vol is None and not s3:
        raise EditError("Choose a volume of the site's storage.")
    ds = ds or PlatformDatastore(platform=platform)
    ds.name, ds.kind, ds.volume, ds.path = name, data["kind"], vol, (data.get("path") or "").strip()
    ds.backend, ds.restic_ip = data.get("backend") or "", data.get("restic_ip") or None
    ds.s3_endpoint, ds.s3_bucket = (data.get("s3_endpoint") or "").strip(), (data.get("s3_bucket") or "").strip()
    ds.s3_secret, ds.restic_secret = data.get("s3_secret") or "", data.get("restic_secret") or ""
    if data.get("reserved_pct") is not None:
        ds.reserved_pct = data["reserved_pct"]
    elif ds.pk is None:
        ds.reserved_pct = 20 if ds.kind == "backups" else 10
    ds.save()
    _mark_updated(platform)
    audit(user, "datastore_saved", ds, platform=platform.key, datastore=name)
    return ds


def datastore_delete(user, platform, pk):
    ds = PlatformDatastore.objects.filter(pk=pk, platform=platform).first()
    if ds is None:
        raise EditError("No such datastore.")
    if ds.clusters.exists():
        raise EditError(f"Datastore {ds.name} is used by clusters {', '.join(c.name for c in ds.clusters.all()[:5])}.")
    ds.delete()
    _mark_updated(platform)
    audit(user, "datastore_deleted", None, platform=platform.key, datastore=ds.name)


def vnet_save(user, platform, data, pk=None):
    from .networks import refresh_segments
    vn = PlatformNetwork.objects.filter(pk=pk, platform=platform).first() if pk else None
    if pk and vn is None:
        raise EditError("No such network.")
    name = data["name"]
    if not NAME_RE.match(name or ""):
        raise EditError(f"Bad network name {name!r}.")
    if platform.vnets.filter(name=name).exclude(pk=pk).exists():
        raise EditError(f"Network {name} already exists on {platform.key}.")
    net = Network.objects.filter(pk=data.get("network") or 0, site=platform.site.code).first()
    if net is None:
        raise EditError("Choose a network of the site.")
    kind = data["kind"]
    bridge = (data.get("bridge") or "").strip()
    if kind == PlatformNetwork.Kind.BRIDGE and not re.match(r"^[a-zA-Z][a-zA-Z0-9_.-]{0,14}$", bridge):
        raise EditError("A bridge network names the existing bridge (up to 15 characters).")
    ranges = list(net.ranges.filter(pk__in=[int(x) for x in data.get("ranges") or [] if str(x).isdigit()]))
    with transaction.atomic():
        vn = vn or PlatformNetwork(platform=platform)
        vn.name, vn.kind, vn.network = name, kind, net
        vn.bridge = bridge if kind == PlatformNetwork.Kind.BRIDGE else ""
        vn.save()
        vn.ranges.set(ranges)
        refresh_segments(platform)
    _mark_updated(platform)
    audit(user, "vnet_saved", vn, platform=platform.key, network=name)
    return vn


def vnet_delete(user, platform, pk):
    from .networks import refresh_segments
    vn = PlatformNetwork.objects.filter(pk=pk, platform=platform).first()
    if vn is None:
        raise EditError("No such network.")
    if vn.clusters.exists():
        raise EditError(f"Network {vn.name} is used by clusters {', '.join(c.name for c in vn.clusters.all()[:5])}.")
    vn.delete()
    refresh_segments(platform)
    _mark_updated(platform)
    audit(user, "vnet_deleted", None, platform=platform.key, network=vn.name)


# --- clusters --------------------------------------------------------------------------------------

def _platform(site, name):
    p = Platform.objects.filter(key=f"{site}.{name}").first()
    if p is None:
        raise EditError(f"Platform {site}.{name} does not exist.")
    return p


def _cluster(site, platform, name):
    c = Cluster.objects.filter(platform__key=f"{site}.{platform}", name=name).select_related("platform").first()
    if c is None:
        raise EditError(f"Cluster {name} does not exist.")
    return c


def _refuse_used_hosts(hosts, except_cluster=None):
    """A host belongs to at most one live cluster: refuse hosts already in another planned or active one."""
    for h in hosts:
        for c in h.clusters.exclude(state="retired").select_related("platform"):
            if c != except_cluster:
                raise EditError(f"Host {h.name} is already in cluster {c.name} ({c.platform.key}).")


def _hosts(site, names):
    hosts = list(Host.objects.filter(site__code=site, name__in=names))
    missing = set(names) - {h.name for h in hosts}
    if missing:
        raise EditError(f"No such hosts on {site}: {', '.join(sorted(missing))}.")
    return hosts


CAPACITY = ("vcpu_ratio", "spare_hosts", "reserved_cpu_pct", "reserved_ram_pct", "bundle_cores",
            "bundle_ram_gb")


def cluster_add(user, site, platform, name, kind, quota_code, hosts, datastores=(), networks=(), capacity=None):
    p = _platform(site, platform)
    if not NAME_RE.match(name or ""):
        raise EditError(f"Bad cluster name {name!r}.")
    if not name.endswith(f"_{site}"):
        raise EditError(f"A cluster name ends with _{site}.")
    if p.clusters.filter(name=name).exists():
        raise EditError(f"Cluster {name} already exists.")
    if kind == "mgmt" and (p.kind != "mgmt" or p.clusters.filter(kind="mgmt").exists()):
        raise EditError("The management platform has exactly one cluster of kind mgmt.")
    hs = _hosts(site, hosts)
    _refuse_used_hosts(hs)
    with transaction.atomic():
        c = Cluster.objects.create(platform=p, name=name, kind=kind, quota_code=(quota_code or "").lower(), state="planned",
                                   **{k: v for k, v in (capacity or {}).items() if k in CAPACITY and v is not None})
        c.hosts.set(hs)
        if (capacity or {}).get("spare_hosts") is None and any(h.profile_id and h.profile.gpu_count for h in hs):
            c.spare_hosts = 0  # what a GPU cluster starts at; staff can set a spare (owner 2026-11-09)
            c.save(update_fields=["spare_hosts"])
        c.datastores.set(p.datastores.filter(pk__in=list(datastores)))
        c.networks.set(p.vnets.filter(pk__in=list(networks)))
        _mark_updated(p)
    audit(user, "cluster_added", c, platform=p.key, cluster=name)
    return c


def cluster_save(user, site, platform, name, kind=None, datastores=None, networks=None, hosts=None, capacity=None):
    c = _cluster(site, platform, name)
    p = c.platform
    with transaction.atomic():
        cap = {k: v for k, v in (capacity or {}).items() if k in CAPACITY and v is not None}
        if cap:
            for k, v in cap.items():
                setattr(c, k, v)
            c.save(update_fields=list(cap))
        if hosts is not None:
            want = set(hosts)
            have = {h.name: h for h in c.hosts.all()}
            new = [n for n in want if n not in have]
            if new:
                hs = _hosts(site, new)
                _refuse_used_hosts(hs, except_cluster=c)
                c.hosts.add(*hs)
            for n, h in have.items():
                if n in want:
                    if h.state == "retired":  # chosen again: back in service as planned
                        h.state = "planned"
                        h.save(update_fields=["state"])
                    continue
                if h.state == "planned" or c.state == "planned":
                    c.hosts.remove(h)
                elif h.state != "retired":
                    h.state = "retired"
                    h.save(update_fields=["state"])
        if kind and kind != c.kind:
            if c.state != "planned":
                raise EditError("The type of an installed cluster cannot be changed.")
            c.kind = kind
            c.save(update_fields=["kind"])
        if datastores is not None:
            c.datastores.set(p.datastores.filter(pk__in=list(datastores)))
        if networks is not None:
            c.networks.set(p.vnets.filter(pk__in=list(networks)))
        _mark_updated(p)
    audit(user, "cluster_saved", c, platform=p.key, cluster=name)
    return c


def cluster_remove(user, site, platform, name):
    c = _cluster(site, platform, name)
    if c.state == "planned":
        c.delete()
        audit(user, "cluster_deleted", None, cluster=name)
        return None
    c.state = "retired"
    c.save(update_fields=["state"])
    _mark_updated(c.platform)
    audit(user, "cluster_retired", c, cluster=name)
    return c


def cluster_add_hosts(user, site, platform, name, hosts):
    c = _cluster(site, platform, name)
    hs = _hosts(site, hosts)
    _refuse_used_hosts(hs, except_cluster=c)
    c.hosts.add(*hs)
    _mark_updated(c.platform)
    audit(user, "cluster_hosts_added", c, hosts=", ".join(hosts))
    return c


def cluster_remove_host(user, site, platform, name, host):
    """Take a host out of a cluster: a planned host simply leaves it, an active one is retired."""
    c = _cluster(site, platform, name)
    h = c.hosts.filter(name=host).first()
    if h is None:
        raise EditError(f"{host} is not in {name}.")
    if h.state == "planned" or c.state == "planned":
        c.hosts.remove(h)
    else:
        h.state = "retired"
        h.save(update_fields=["state"])
        _mark_updated(c.platform)
    audit(user, "cluster_host_removed", c, host=host)
    return c
