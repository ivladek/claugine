"""Deploy an approved tenant on its OpenNebula platform (internet connection, VyOS igw).

Steps, each one idempotent (an object that already exists by name is reused):

1. group      _<org>_<tenant> on the primary zone of the federation, with its template copy
              (TENANT, QUOTA_<SITE>, QUOTA_<SITE>_<QC>) and the FireEdge views
2. admin      user <org>-<tenant>-admin in the group, admin of the group, password chosen with the order
3. quota      VM quota of the tenant's cluster (lib/claugine_quota: VMS and CPU x2 of RUNNING, MEMORY x2,
              SYSTEM_DISK_SIZE, PCI_DEV = GPUs), the other clusters of the platform -1; datastore quotas:
              IMAGE datastore of the cluster (VMs + images share one size), FILE, BACKUP
4. acl        lib/claugine_acl rules without role groups: the group admin holds the SA rights, the group
              the user rights; zone of the platform
5. network    main public network <group>-main_<site>: VXLAN/EVPN, the lowest address is the egw (and the
              gateway), the next one the igw, the rest of the prefix in a third address range
6. egw        <group>-egw_<site>: provider's VM (group oneadmin), NIC 0 in the exchange network, NIC 1 the
              egw address of the main network; the platform's super admin from the secrets
7. igw        <group>-igw_<site>: tenant's VM (owned by its admin), NIC 0 the igw address, gateway = egw;
              user and ssh key chosen with the order, no password login

Further steps, when the tenant has them:

8. public     extra public subnets: <group>-pub<n>_<site>, the first address is the igw's, attached to the igw
9. corpnet    corporate subnets: <group>-corp_<site> (and corp<n>), the lowest address is the cgw
10. cgw       <group>-cgw_<site>: provider's VM, NIC 0 in the corporate exchange network, a NIC per corporate network
11. l2        per L2 channel two VMs <group>-l2v<vni>a/b_<site> on the s2s network: side a on the tenant network,
              side b on the external VLAN (named at approval)

The first boot sets addresses, routes, host name, time zone, user and key from the context; the rest of each
router's configuration is built from Infrastructure → Templates (inventory/nfconfig.py) and passed as the
one-time init script (ONE_TIME_INIT_SCRIPT_BASE64), run once by vyos-postconfig-bootup.script.
"""

import base64
import ipaddress
import subprocess
import xml.etree.ElementTree as ET

from django.utils import timezone

from . import one_acl

EGW_CPU, EGW_RAM_MB = 2, 4096
# every VM the portal makes (owner, 2026-10-28): the hypervisor only, never a cluster; memory by ballooning
SCHED = "HYPERVISOR=kvm"
MEMORY_RESIZE_MODE = "BALLOONING"
IGW_CPU, IGW_RAM_MB = 1, 1024  # owner 2026-10-29: 1 vCPU, 1 GB
MAIN_MTU = 1500
TIMEZONES = {"almaty": "Asia/Almaty", "astana": "Asia/Almaty", "aqtobe": "Asia/Aqtobe", "atyrau": "Asia/Atyrau"}
FIREEDGE = {
    "DEFAULT_VIEW": "cloud",
    "GROUP_ADMIN_DEFAULT_VIEW": "groupadmin",
    "GROUP_ADMIN_VIEWS": "groupadmin",
    "VIEWS": "cloud",
}
IMAGES_DEFAULT_GB = 10


class DeployError(Exception):
    pass


def _tpl_value(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def template(attrs):
    """OpenNebula template text from a dict: nested dicts become vector attributes, lists repeat the key."""
    lines = []
    for k, v in attrs.items():
        if isinstance(v, list):
            for item in v:
                lines.append(template({k: item}))
        elif isinstance(v, dict):
            inner = ",\n".join(f'  {ik} = "{_tpl_value(iv)}"' for ik, iv in v.items() if iv is not None)
            lines.append(f"{k} = [\n{inner}\n]")
        elif v is not None:
            lines.append(f'{k} = "{_tpl_value(v)}"')
    return "\n".join(lines)


def crypt_sha512(password):
    """SHA-512 crypt hash for CRYPTED_PASSWORD_BASE64 (openssl, as mkpasswd in lib/claugine_vm)."""
    out = subprocess.run(
        ["openssl", "passwd", "-6", "-stdin"], input=password, capture_output=True, text=True, timeout=10, check=True
    )
    return out.stdout.strip()


class One:
    """Thin XML-RPC wrapper over one platform's endpoint."""

    def __init__(self, control):
        from adoption.collector import client_for

        self.key = control.key
        self.client = client_for(control)

    def call(self, method, *args):
        from adoption.collector import OneError

        if not method.endswith((".info", ".version", ".config")):  # a change: only from the active portal node
            from accounts import ha

            try:
                ha.forbid_writes(f"{self.key} {method}")
            except ha.ReadOnlyHere as exc:
                raise DeployError(str(exc)) from exc
        try:
            return self.client._call(method, *args)
        except OneError as exc:
            raise DeployError(str(exc)) from exc

    def xml(self, method, *args):
        return ET.fromstring(self.call(method, *args))

    def find(self, pool_method, tag, name, *args):
        root = self.xml(pool_method, *args)
        for el in root.findall(tag):
            if el.findtext("NAME") == name:
                return el
        return None


def with_public(acc):
    """An account with its ssh public key: VMs get only the public key, never the private one."""
    from inventory.credentials import _public_key

    key = (acc.get("key") or "").strip()
    pub = _public_key(key) if "PRIVATE KEY" in key else (key if key.startswith(("ssh-", "ecdsa-")) else "")
    return {**acc, "public": pub}


class Deployer:
    def __init__(self, tenant, user=None, dry=False):
        from inventory.models import Cluster, Platform, PlatformControl

        self.tenant, self.user, self.dry = tenant, user, dry
        o = tenant.order or {}
        self.platform = Platform.objects.select_related("site").filter(key=o.get("platform")).first()
        if self.platform is None:
            raise DeployError("The project has no platform: approve it with a placement first.")
        self.site = self.platform.site.code
        self.control = PlatformControl.objects.filter(key=self.platform.key).first()
        if self.control is None or not self.control.configured:
            raise DeployError(f"{self.platform.key}: endpoint or credentials not found (inventory and secrets).")
        primary_key = self.platform.fe_primary or self.platform.key
        self.primary_control = PlatformControl.objects.filter(key=primary_key).first() or self.control
        from django.db.models import Q

        self.cluster = (
            Cluster.objects.filter(platform=self.platform, quota_code=o.get("cluster")).first()
            or Cluster.objects.filter(
                Q(quota_code="") | Q(quota_code__isnull=True), platform=self.platform, name=o.get("cluster")
            ).first()
        )
        if self.cluster is None:
            raise DeployError(f"{self.platform.key}: no cluster with quota code {o.get('cluster')}.")
        if not tenant.group_name:
            raise DeployError("The project has no project ID yet.")
        self.group = tenant.group_name
        self.base = object_base(tenant)
        self.steps, self.res, self.dep = [], {}, None
        self.legacy = {}  # new VM name → the names the same VM had before (correction 11), looked up too
        for role in ("egw", "cgw", "igw"):
            self.legacy[self.gw(role)] = [f"{self.base}-{role}_{self.site}"]

    def gw(self, role):
        """The VM name of the tenant's gateway `role` (correction 11): _<org>_<tenant>_<role>_<site>."""
        from .naming import vyos_names

        return vyos_names(self.tenant, role, self.site)[0]

    def gw_host(self, role):
        from .naming import vyos_names

        return vyos_names(self.tenant, role, self.site)[1]

    def _candidates(self, name):
        return [name] + [n for n in self.legacy.get(name, []) if n != name]

    # --- inventory and secrets ------------------------------------------------------------

    def _platform_meta(self):
        return {"sa": self.platform.sa, "shared_owner": self.platform.shared_owner}

    def _timezone(self):
        from inventory.models import Site

        s = Site.objects.filter(code=self.site).first()
        from inventory.models import DEFAULT_TZ

        return s.tz if s else DEFAULT_TZ

    def _dns(self):
        """DNS for tenant VMs: the platform's services network (kept from the import), else the site's servers."""
        from inventory.models import Network, Site

        candidates = [s for s in (self.platform.segments or []) if s.endswith("-services")]
        candidates += [f"{self.platform.name}-services", "cloud-services"]
        for seg in candidates:
            n = Network.objects.filter(site=self.site, name=seg).first()
            dns = list((n.extra or {}).get("dns") or []) if n else []
            if dns:
                return " ".join(str(x) for x in dns)
        s = Site.objects.filter(code=self.site).first()
        return " ".join(s.dns_servers or []) if s else ""

    def _sa(self):
        from inventory.credentials import account

        ref = self._platform_meta().get("sa")
        name, acc = account(ref) if ref else (None, {})
        if not name:
            raise DeployError(
                f"{self.platform.key}: the platform's super admins do not resolve (Infrastructure → Credentials)"
            )
        return name, with_public(acc or {})

    def _main_prefix(self):
        from .models import PrefixAssignment

        p = PrefixAssignment.active().filter(tenant=self.tenant, kind="inet_main", site=self.site).first()
        if p is None:
            raise DeployError("No main public prefix is assigned to the project on this site.")
        return p

    def _ix_segment(self, prefix):
        from inventory.models import DelegatedSubnet

        net = ipaddress.ip_network(prefix.cidr)
        for d in DelegatedSubnet.objects.filter(site__code=self.site, segment__in=self.platform.segments or []):
            pool = ipaddress.ip_network(d.cidr)
            if net.version == pool.version and net.subnet_of(pool):
                return d.segment
        raise DeployError(f"{prefix.cidr} is in no exchange network of {self.platform.key}.")

    # --- helpers -------------------------------------------------------------------------

    def step(self, name, fn):
        """Run one step; its status is saved at once so the tenant page can show the progress."""
        from accounts.models import audit

        entry = next((e for e in self.steps if e["name"] == name), None)
        if entry is None:
            entry = {"name": name, "status": "pending", "detail": ""}
            self.steps.append(entry)
        entry["status"], entry["started"] = "running", timezone.now().isoformat()
        self._save()
        try:
            entry["detail"] = fn() or ""
            entry["status"] = "ok"
        except DeployError as exc:
            entry["status"], entry["detail"] = "failed", str(exc)
            raise
        except Exception as exc:  # noqa: BLE001 - recorded, never with secrets
            entry["status"], entry["detail"] = "failed", f"{type(exc).__name__}: {exc}"
            raise DeployError(entry["detail"]) from exc
        finally:
            entry["finished"] = timezone.now().isoformat()
            self._save()
            if not self.dry:
                audit(
                    self.user,
                    "deploy_step",
                    self.tenant,
                    step=name,
                    status=entry["status"],
                    detail=entry["detail"][:500],
                )
        return entry

    def _save(self):
        if self.dep is not None:
            self.dep.steps, self.dep.resources = self.steps, self.res
            self.dep.save(update_fields=["steps", "resources"])

    def _addresses(self):
        net = ipaddress.ip_network(self._main_prefix().cidr)
        hosts = list(net) if net.prefixlen >= 31 else list(net.hosts())
        if len(hosts) < 2:
            raise DeployError(f"{net} has no room for the egw and the igw.")
        return net, str(hosts[0]), str(hosts[1]), hosts

    # --- lookups (read only) ---------------------------------------------------------------

    def lookups(self):
        self.one = One(self.control)
        self.gone = One(self.primary_control) if self.primary_control.key != self.control.key else self.one
        vip = self.platform.fe_vip
        zones = self.gone.xml("one.zonepool.info")
        zone = next((z for z in zones.findall("ZONE") if f"//{vip}:" in (z.findtext("TEMPLATE/ENDPOINT") or "")), None)
        if zone is None:
            raise DeployError(f"zone of {self.platform.key} ({vip}) not found on {self.primary_control.key}")
        self.zone_id = int(zone.findtext("ID"))
        clusters = self.one.xml("one.clusterpool.info")
        self.cluster_ids = sorted(int(c.findtext("ID")) for c in clusters.findall("CLUSTER"))
        c = next((c for c in clusters.findall("CLUSTER") if c.findtext("NAME") == self.cluster.name), None)
        if c is None:
            raise DeployError(f"cluster {self.cluster.name} not found on {self.platform.key}")
        self.cluster_id = int(c.findtext("ID"))
        # Where a project's own provider routers go - its egw, cgw and the project side of an L2 channel - is a
        # setting of the cluster, not a consequence of where the project itself was placed (owner 2026-11-09).
        # Unmarked, they followed the project, so a GPU project put an egw on a GPU cluster. This is **not**
        # `kind="edge"`: that cluster carries the platform's common network functions - the route reflectors and
        # the L2GW endpoint a VLAN lands on - which are nothing to do with one project's routers.
        # With no cluster marked, the old behaviour stands: the project's own cluster, which is the right answer
        # on a platform that has only one.
        rc = self.platform.gateway_cluster
        if rc is not None and rc.state == "retired":
            rc = None
        ec = next((x for x in clusters.findall("CLUSTER") if rc is not None and x.findtext("NAME") == rc.name), None)
        if rc is not None and ec is None:
            raise DeployError(
                f"{rc.name} is marked for the project routers but is not a cluster on {self.platform.key}"
            )
        self.router_cluster_id = int(ec.findtext("ID")) if ec is not None else self.cluster_id
        self.datastores = {"images": [], "files": None, "backups": None, "all": []}
        for d in self.one.xml("one.datastorepool.info").findall("DATASTORE"):
            did, dtype = int(d.findtext("ID")), d.findtext("TYPE")
            dcl = {int(x.text) for x in d.findall("CLUSTERS/ID")}
            if dtype == "0":
                self.datastores["all"].append(did)
                if self.cluster_id in dcl:
                    self.datastores["images"].append(did)
            elif dtype == "2":
                self.datastores["files"] = did
            elif dtype == "3":
                self.datastores["backups"] = did
        vnt = self.one.xml("one.vntemplatepool.info", -2, -1, -1).findall("VNTEMPLATE")
        self.vntemplate_id = int(vnt[0].findtext("ID")) if len(vnt) == 1 else None
        shared_name = str(self._platform_meta().get("shared_owner") or "shared")
        g = self.gone.find("one.grouppool.info", "GROUP", shared_name)
        self.shared_gid = int(g.findtext("ID")) if g is not None else None
        img = self.one.find("one.imagepool.info", "IMAGE", self.control.nf_image, -2, -1, -1)
        if img is None:
            raise DeployError(f"image {self.control.nf_image!r} not found on {self.platform.key}")
        self.image_id = int(img.findtext("ID"))
        self.res.update(
            platform=self.platform.key,
            zone_id=self.zone_id,
            cluster=self.cluster.name,
            cluster_id=self.cluster_id,
            image=self.control.nf_image,
            image_id=self.image_id,
            dns=self._dns(),
        )
        self.net = None
        if not self.has_inet:
            return f"zone {self.zone_id}, cluster {self.cluster.name} #{self.cluster_id}, image #{self.image_id}"
        prefix = self._main_prefix()
        self.ix_vnet_name = f"{self._ix_segment(prefix)}_{self.site}"
        ix = self.one.find("one.vnpool.info", "VNET", self.ix_vnet_name, -2, -1, -1)
        if ix is None:
            raise DeployError(f"exchange network {self.ix_vnet_name} not found on {self.platform.key}")
        self.ix_vnet_id = int(ix.findtext("ID"))
        self.net, self.egw_ip, self.igw_ip, self.hosts = self._addresses()
        self._refuse_used_addresses()
        self.res.update(main_prefix=str(self.net), egw_ip=self.egw_ip, igw_ip=self.igw_ip, ix_vnet=self.ix_vnet_name)
        return (
            f"zone {self.zone_id}, cluster {self.cluster.name} #{self.cluster_id}, image #{self.image_id}, "
            f"exchange network {self.ix_vnet_name} #{self.ix_vnet_id}"
        )

    def _main_vnet(self):
        """(id, name) of the tenant's main network: the one of its standard name, else the network of the tenant's group
        that holds the main prefix (2026-10-28: delta's is "tenant_delta-main", renamed after its egw was made).
        (None, standard name) when there is none."""
        name = f"{self.base}-main_{self.site}"
        v = self.one.find("one.vnpool.info", "VNET", name, -2, -1, -1)
        if v is not None:
            return int(v.findtext("ID")), name
        net = getattr(self, "net", None)
        if net is None or not self.group:
            return None, name
        for v in self.one.xml("one.vnpool.info", -2, -1, -1).findall("VNET"):
            if v.findtext("GNAME") != self.group:
                continue
            full = self.one.xml("one.vn.info", int(v.findtext("ID")), False)  # its address ranges
            for ar in full.findall("AR_POOL/AR"):
                try:
                    if ar.findtext("IP") and ipaddress.ip_address(ar.findtext("IP")) in net:
                        return int(v.findtext("ID")), v.findtext("NAME")
                except ValueError:
                    continue
        return None, name

    def _refuse_used_addresses(self):
        """The main prefix must not be in any address range of OpenNebula yet, except the tenant's own network."""
        own_id, _own = self._main_vnet()
        for v in self.one.xml("one.vnpool.info", -2, -1, -1).findall("VNET"):
            if own_id is not None and v.findtext("ID") == str(own_id):
                continue
            for ar in v.findall("AR_POOL/AR"):
                ip = ar.findtext("IP")
                if not ip:
                    continue
                try:
                    start, size = ipaddress.ip_address(ip), int(ar.findtext("SIZE") or 0)
                except ValueError:
                    continue
                if start.version != self.net.version or size <= 0:
                    continue
                end = start + (size - 1)
                if start <= self.net.broadcast_address and end >= self.net.network_address:
                    raise DeployError(
                        f"{self.net} is already used in OpenNebula by network {v.findtext('NAME')} "
                        f"({ip}, {size} addresses)"
                    )

    def _free_ix_ip(self):
        return self._free_ip(self.ix_vnet_id, self.ix_vnet_name)

    def _free_ip(self, vnet_id, vnet_name, avoid=(), ar_index=None):
        """First address of the network that is neither leased nor in `avoid` (optionally in one address range only)."""
        vn = self.one.xml("one.vn.info", vnet_id, False)
        used = {ln.findtext("IP") for ln in vn.findall("AR_POOL/AR/LEASES/LEASE")} | set(avoid)
        ars = [ar for ar in vn.findall("AR_POOL/AR") if ar.findtext("TYPE") in ("IP4", "IP4_6")]
        if ar_index is not None and len(ars) > ar_index:
            ars = [ars[ar_index]]
        for ar in ars:
            start, size = ipaddress.ip_address(ar.findtext("IP")), int(ar.findtext("SIZE") or 0)
            for i in range(size):
                ip = str(start + i)
                if ip not in used:
                    return ip
        raise DeployError(f"no free address in {vnet_name}")

    # --- steps that write --------------------------------------------------------------------

    def group_step(self):
        g = self.gone.find("one.grouppool.info", "GROUP", self.group)
        if g is not None:
            self.gid = int(g.findtext("ID"))
            note = "exists"
        elif self.dry:
            self.gid = None
            return f"would create group {self.group}"
        else:
            self.gid = int(self.gone.call("one.group.allocate", self.group))
            note = "created"
        if self.dry:
            self.res["group_id"] = self.gid
            return f"group {self.group} #{self.gid} exists"
        self.gone.call("one.group.update", self.gid, template(self._group_template()), 1)
        self.res["group_id"] = self.gid
        return f"group {self.group} #{self.gid} {note}"

    def _group_template(self):
        t, o = self.tenant, self.tenant.order or {}
        today = timezone.localdate().isoformat()
        rec = next((r for r in t.current_quota_records() if r.site == self.site), None)
        site, qc = self.site.upper(), (o.get("cluster") or "").upper()
        tpl = {"TENANT": {"DATE_CREATED": today, "STATUS": t.status}, "FIREEDGE": FIREEDGE}
        if rec is not None:
            tpl[f"QUOTA_{site}_{qc}"] = {
                "CPU": rec.cpu,
                "RAM_GB": rec.ram_gb,
                "GPU": rec.gpu,
                "DISK_GB": rec.disk_gb,
                "DATE_CREATED": today,
            }
        from .models import Connectivity, PrefixAssignment, split_of
        from .ordering import connections

        conns = connections(o)
        # the site's figures (owner 2026-11-11): images and files are set per pool and shared by the site's pools
        # (their sums); backups is one figure per site, carried by every pool of it
        site_recs = [r for r in t.current_quota_records() if r.site == self.site]
        img = sum(split_of(r)[1] for r in site_recs)
        fil = sum(split_of(r)[2] for r in site_recs)
        site_q = {
            "IMAGES_GB": img,
            "FILES_GB": fil,
            "BACKUPS_GB": max([r.backups_gb or 0 for r in site_recs] or [0]),
            "CONNECTION_TYPE": Connectivity.type_value(conns),
        }
        if self.net is not None:
            mbps = next((c.inet_mbps for c in t.current_connectivity() if c.site == self.site and c.inet_mbps), None)
            site_q.update(
                {
                    "INET_MBPS": mbps or o.get("inet_mbps") or 100,
                    "INET_MAIN_PREFIX": self.net.prefixlen,
                    "INET_MAIN_SUBNET": str(self.net.network_address),
                    # owner 2026-11-09: the type chosen on the order, not a constant. Hardcoded "vyos"
                    # told the platform every project ran an advanced gateway - including the ones where
                    # nothing is created at all (custom) or a Service Virtual Router is (onevr).
                    "IGW_TYPE": self.igw_type() or "vyos",
                }
            )
        mine = PrefixAssignment.active().filter(tenant=t, site=self.site)
        extra = [ipaddress.ip_network(p.cidr) for p in mine.filter(kind="inet_extra")]
        corp = [ipaddress.ip_network(p.cidr) for p in mine.filter(kind="corpnet")]
        if extra:
            site_q.update(
                {
                    "INET_PUBLIC_SUBNETS": " ".join(str(n.network_address) for n in extra),
                    "INET_PUBLIC_PREFIXES": " ".join(str(n.prefixlen) for n in extra),
                }
            )
        if corp:
            site_q.update(
                {
                    "CORPNET_SUBNETS": " ".join(str(n.network_address) for n in corp),
                    "CORPNET_PREFIXES": " ".join(str(n.prefixlen) for n in corp),
                }
            )
        chans = list(t.l2_channels.exclude(state="removed"))
        if chans:
            site_q.update(
                {
                    "L2_VNETS": " ".join(c.tenant_network for c in chans),
                    "L2_VLANS": " ".join(c.vlan_name for c in chans),
                }
            )
        tpl[f"QUOTA_{site}"] = site_q
        return tpl

    def admin_step(self):
        name = self.tenant.one_admin_name
        password = self.tenant.get_admin_password()
        if not password and not self.dry:
            raise DeployError("No admin password was chosen for the project.")
        u = self.gone.find("one.userpool.info", "USER", name)
        if u is not None:
            groups = ({x.text for x in u.findall("GROUPS/ID")} | {u.findtext("GID")}) - {None}
            if (
                groups and self.gid is not None and str(self.gid) not in groups
            ):  # correction 13: never take another tenant's user
                raise DeployError(f"OpenNebula user {name} already exists in another group: choose another admin name.")
            self.uid = int(u.findtext("ID"))
            note = "exists"
            if password and not self.dry and not self.tenant.deployments.filter(state="done").exists():
                # made by a provisioning that failed: Provision again chose a new password
                self.gone.call("one.user.passwd", self.uid, password)
                note = "exists, password set"
        elif self.dry:
            self.uid = None
            return f"would create user {name} in {self.group}"
        else:
            self.uid = int(self.gone.call("one.user.allocate", name, password, "core", [self.gid]))
            note = "created"
        if self.gid is not None and not self.dry:
            g = self.gone.xml("one.group.info", self.gid, False)
            if str(self.uid) not in {x.text for x in g.findall("ADMINS/ID")}:
                self.gone.call("one.group.addadmin", self.gid, self.uid)
        self.res.update(admin_user=name, admin_user_id=self.uid)
        return f"user {name} #{self.uid} {note}, admin of {self.group}"

    def _quota_template(self):
        rec = next((r for r in self.tenant.current_quota_records() if r.site == self.site), None)
        if rec is None:
            raise DeployError("No quota record for this site.")
        from .models import split_of

        cpu, ram, gpu = rec.cpu, rec.ram_gb, rec.gpu or 0
        vms_disk, images, files = split_of(rec)  # one storage number: VM disks + images + files
        vm = [
            {
                "CLUSTER_IDS": str(self.cluster_id),
                "VMS": cpu * 2,
                "RUNNING_VMS": cpu,
                "CPU": cpu * 2,
                "RUNNING_CPU": cpu,
                "MEMORY": ram * 2 * 1024,
                "RUNNING_MEMORY": ram * 1024,
                "SYSTEM_DISK_SIZE": vms_disk * 1024,
                "PCI_DEV": gpu,
                "RUNNING_PCI_DEV": gpu,
                "PCI_NIC": 0,
                "RUNNING_PCI_NIC": 0,
            }
        ]
        reset = {
            k: -1
            for k in (
                "VMS",
                "RUNNING_VMS",
                "CPU",
                "RUNNING_CPU",
                "MEMORY",
                "RUNNING_MEMORY",
                "SYSTEM_DISK_SIZE",
                "PCI_DEV",
                "RUNNING_PCI_DEV",
                "PCI_NIC",
                "RUNNING_PCI_NIC",
            )
        }
        for cid in self.cluster_ids:
            if cid != self.cluster_id:
                vm.append({"CLUSTER_IDS": str(cid), **reset})
        ds = []
        for did in self.datastores["all"]:
            # owner 2026-11-11: another cluster's image datastore is closed (0), not unlimited
            ds.append({"ID": did, "IMAGES": -1, "SIZE": images * 1024 if did in self.datastores["images"] else 0})
        for kind, gb in (("files", files), ("backups", rec.backups_gb if rec.backups_gb is not None else 0)):
            if self.datastores[kind] is not None:
                ds.append({"ID": self.datastores[kind], "IMAGES": -1, "SIZE": gb * 1024})
        return {"VM": vm, "DATASTORE": ds}

    def _wait_replicated(self, seconds=90):
        """A secondary zone knows the new group and user only once the federation replicated them: wait for that."""
        import time

        if (
            self.one is self.gone
            or getattr(self.one, "key", None) == getattr(self.gone, "key", None)
            or self.gid is None
        ):
            return
        end = time.monotonic() + seconds
        while True:
            try:
                self.one.call("one.group.info", self.gid, False)
                if self.uid is not None:
                    self.one.call("one.user.info", self.uid, False)
                return
            except DeployError:
                if time.monotonic() > end:
                    raise DeployError(
                        f"group #{self.gid} is not replicated to {self.platform.key} after {seconds} s"
                    ) from None
                time.sleep(3)

    def quota_step(self):
        q = self._quota_template()
        self.res["quota"] = q
        if self.dry or self.gid is None:
            return "would set the quota"
        self._wait_replicated()
        self.one.call("one.group.quota", self.gid, template(q))
        main = q["VM"][0]
        return (
            f"cluster #{self.cluster_id}: {main['RUNNING_CPU']} vCPU, {main['RUNNING_MEMORY'] // 1024} GB RAM running, "
            f"{main['PCI_DEV']} GPU, {main['SYSTEM_DISK_SIZE'] // 1024} GB disk"
        )

    def acl_rules(self):
        g, u, z, c = self.gid, self.uid, self.zone_id, self.cluster_id
        if g is None or u is None:
            g, u = g if g is not None else "<group>", u if u is not None else "<admin>"
        rules = [
            f"#{u} GROUP/#{g} USE+MANAGE *",
            f"#{u} USER/@{g} USE+MANAGE+ADMIN *",
            f"#{u} USER/* CREATE *",
            f"@{g} ZONE/#{z} USE #{z}",
            f"@{g} CLUSTER/#{c} USE #{z}",
            f"@{g} DATASTORE/%{c} USE #{z}",
            f"@{g} HOST/%{c} MANAGE #{z}",
            f"#{u} MARKETPLACEAPP/* USE #{z}",
            f"#{u} VM+NET+IMAGE+TEMPLATE+DOCUMENT+SECGROUP+VROUTER+BACKUPJOB/@{g} USE+MANAGE #{z}",
            f"#{u} VM+NET+IMAGE+TEMPLATE+DOCUMENT+SECGROUP+VROUTER+BACKUPJOB/* CREATE #{z}",
            f"@{g} NET+IMAGE+TEMPLATE+DOCUMENT+SECGROUP/@{g} USE #{z}",
            f"@{g} VM+TEMPLATE+DOCUMENT/* CREATE #{z}",
        ]
        if self.vntemplate_id is not None:
            rules.insert(8, f"#{u} VNTEMPLATE/#{self.vntemplate_id} USE #{z}")
        if self.shared_gid is not None:
            rules.insert(7, f"@{g} IMAGE+TEMPLATE/@{self.shared_gid} USE #{z}")
        return rules

    def acl_step(self):
        rules = self.acl_rules()
        self.res["acls"] = rules
        if self.dry or self.gid is None:
            return f"would create {len(rules)} rules"
        existing = {a.findtext("STRING") for a in self.gone.xml("one.acl.info").findall("ACL")}
        made = 0
        for r in rules:
            if r in existing:
                continue
            self.gone.call("one.acl.addrule", *one_acl.encode(r))
            made += 1
        return f"{made} rules created, {len(rules) - made} existed"

    def network_step(self):
        vid, name = self._main_vnet()
        self.vnet_name = name
        if vid is not None:
            self.vnet_id = vid
            self.res.update(main_network=name, main_network_id=self.vnet_id)
            return f"{name} #{self.vnet_id} exists"
        net, egw, igw, hosts = self.net, self.egw_ip, self.igw_ip, self.hosts
        # correction 15: the egw's address, then one range from the address after it (the igw's) to the last one
        ars = [{"TYPE": "IP4", "IP": egw, "SIZE": 1}, {"TYPE": "IP4", "IP": igw, "SIZE": len(hosts) - 1}]
        tpl = {
            "NAME": name,
            "VN_MAD": "vxlan",
            "VXLAN_MODE": "evpn",
            "VXLAN_TEP": "local_ip",
            "PHYDEV": "vxlan",
            "AUTOMATIC_VLAN_ID": "YES",
            "MTU": MAIN_MTU,
            "GUEST_MTU": MAIN_MTU,
            "METHOD": "static",
            "IP6_METHOD": "disable",
            "NETWORK_ADDRESS": str(net.network_address),
            "NETWORK_MASK": str(net.netmask),
            "GATEWAY": egw,
            "DNS": self._dns(),
            "FILTER_IP_SPOOFING": "NO",
            "FILTER_MAC_SPOOFING": "NO",
            "AR": ars,
        }
        self.res["main_network_template"] = tpl
        if self.dry:
            self.vnet_id = None
            return f"would create {name}: egw {egw}, igw {igw}"
        self.vnet_id = int(self.one.call("one.vn.allocate", template(tpl), self.cluster_id))
        self.one.call("one.vn.chown", self.vnet_id, -1, self.gid)
        self.one.call("one.vn.chmod", self.vnet_id, 1, 1, 0, 1, 0, 0, 0, 0, 0)
        self.res.update(main_network=name, main_network_id=self.vnet_id)
        return f"{name} #{self.vnet_id} created: egw {egw}, igw {igw}"

    def _init(self, role, channel=None):
        """(base64 one-time init script or None, note): the VM's configuration from Infrastructure → Templates."""
        if self.dry:
            return None, ""
        from inventory import nfconfig

        try:
            text = nfconfig.build(role, tenant=self.tenant, reveal=True, channel=channel, res=self.res)
        except nfconfig.BuildError as exc:
            return None, f"; no init script: {exc}"
        if not nfconfig.is_complete(text):
            return None, "; no init script: values missing"
        script = nfconfig.init_script(text, f"{self.group} {role}")
        return base64.b64encode(script.encode()).decode(), "; with init script"

    def _vm_template(
        self,
        name,
        hostname,
        cpu,
        ram_mb,
        nics,
        user,
        key,
        crypted=None,
        init=None,
        ssh_port=None,
        cluster_id=None,
        image_id=None,
        service=False,
    ):
        """service: a provider router (egw, cgw, L2 gateway) — AUTOSTART "always" (correction 12); the tenant's igw not."""
        ctx = {
            "NETWORK": "yes",
            "SET_HOSTNAME": hostname,
            "USERNAME": user,
            "SSH_PUBLIC_KEY": key,
            "TIMEZONE": self._timezone(),
        }
        if ssh_port:
            ctx["SSH_PORT"] = ssh_port
        if init:
            ctx["ONE_TIME_INIT_SCRIPT_BASE64"] = init
        if crypted:
            ctx["CRYPTED_PASSWORD_BASE64"] = base64.b64encode(crypted.encode()).decode()
        # SCHED_REQUIREMENTS: the hypervisor only — no cluster at all (owner, 2026-10-28): OpenNebula places the VM in a
        # cluster that has its networks and its image (epsilon-poc's egw stayed pending, pinned to a cluster without
        # cloud-vyos)
        return {
            "NAME": name,
            "SCHED_REQUIREMENTS": SCHED,
            "MEMORY_RESIZE_MODE": MEMORY_RESIZE_MODE,
            **({"AUTOSTART": "always"} if service else {}),
            "OS": {"ARCH": "x86_64", "FIRMWARE": "UEFI", "MACHINE": "q35"},
            "CPU": cpu,
            "VCPU": cpu,
            "CPU_MODEL": {"MODEL": "host-passthrough"},
            "MEMORY": ram_mb,
            "VIDEO": {"TYPE": "virtio"},
            "GRAPHICS": {"LISTEN": "0.0.0.0", "TYPE": "VNC"},
            "DISK": {"IMAGE_ID": self.image_id if image_id is None else image_id},
            "NIC": nics,
            "CONTEXT": ctx,
        }

    def _mgmt_nic(self, role, one=None, platform=None, avoid=()):
        """The first NIC (eth0, VRF MGMT) of a provider router: a free address on the site's cloud-vyos network."""
        from inventory.nfconfig import MGMT_NETWORK

        one = one or self.one
        name = f"{MGMT_NETWORK}_{self.site}"
        v = one.find("one.vnpool.info", "VNET", name, -2, -1, -1)
        if v is None:
            raise DeployError(
                f"management network {name} not found on {(platform or self.platform).key}: "
                "create it in OpenNebula first (Infrastructure → Sites → Networks: cloud-vyos)"
            )
        vid = int(v.findtext("ID"))
        saved, self.one = self.one, one
        try:
            ip = self._free_ip(vid, name, avoid=[a for a in avoid if a])
        finally:
            self.one = saved
        self.res[f"{role}_mgmt_ip"] = ip
        return {"NETWORK_ID": vid, "IP": ip, "NAME": "MGMT"}, ip

    def _router_addresses(self, role, vm_id):
        """The management and exchange addresses of an existing provider router (a later run keeps knowing them, so
        the configuration check reaches the router)."""
        from inventory.nfconfig import MGMT_NETWORK

        if self.dry:
            return {}
        out = {}
        try:
            vm = self.one.xml("one.vm.info", vm_id, False)
        except DeployError:
            return out
        for n in vm.findall("TEMPLATE/NIC"):
            net, ip = n.findtext("NETWORK") or "", n.findtext("IP") or ""
            if net == f"{MGMT_NETWORK}_{self.site}" and ip:
                out[f"{role}_mgmt_ip"] = ip
            elif net.endswith(f"-ix_{self.site}") and ip:
                out[f"{role}_ix_ip"] = ip
        return out

    def _vyos_port(self):
        from inventory.nfconfig import management

        return management(self.site)[1]["vyos"]

    def _router_nets(self, *vnet_ids):
        """The networks of a provider router in its cluster (the dedicated edge cluster, when there is one)."""
        rc = getattr(self, "router_cluster_id", None)
        if self.dry or rc is None or rc == self.cluster_id:
            return
        for vid in vnet_ids:
            if vid is None or vid < 0:
                continue
            vn = self.one.xml("one.vn.info", vid, False)
            if rc not in {int(x.text) for x in vn.findall("CLUSTERS/ID")}:
                self.one.call("one.cluster.addvnet", rc, vid)

    def _vm_exists(self, name):
        """The id of the VM of that name — or of the name it had before (a gateway made before correction 11)."""
        for n in self._candidates(name):
            v = self.one.find("one.vmpool.info", "VM", n, -2, -1, -1, -1)
            if v is not None:
                return int(v.findtext("ID"))
        return None

    def egw_step(self):
        name = self.gw("egw")
        existing = self._vm_exists(name)
        if existing is not None:
            self.res.update(egw=name, egw_id=existing, **self._router_addresses("egw", existing))
            return f"{name} #{existing} exists"
        ix_ip = self._free_ix_ip()
        sa_name, sa = self._sa()
        if not sa.get("public"):
            raise DeployError("the platform's super admin has no ssh key in the secrets")
        host = self.gw_host("egw")  # correction 11: the host name follows the VM name
        if self.dry:
            mgmt_nic, mgmt_ip = {"NETWORK_ID": -1}, "«management address»"
        else:
            mgmt_nic, mgmt_ip = self._mgmt_nic("egw")
        nics = [
            mgmt_nic,
            {"NETWORK_ID": self.ix_vnet_id, "IP": ix_ip, "NAME": "VMNIC1"},
            {"NETWORK_ID": self.vnet_id if self.vnet_id is not None else -1, "IP": self.egw_ip, "NAME": "VMNIC2"},
        ]
        self.res.update(egw=name, egw_ix_ip=ix_ip)
        if self.dry:
            return f"would create {name}: {self.ix_vnet_name} {ix_ip}, main {self.egw_ip}; user {sa_name}"
        crypted = crypt_sha512(sa["password"]) if sa.get("password") else None
        init, note = self._init("egw")
        self._router_nets(*[n.get("NETWORK_ID") for n in nics])
        tpl = self._vm_template(
            name,
            host,
            EGW_CPU,
            EGW_RAM_MB,
            nics,
            sa_name,
            sa["public"],
            crypted,
            init,
            ssh_port=self._vyos_port(),
            cluster_id=getattr(self, "router_cluster_id", None),
            service=True,
        )
        vm_id = int(self.one.call("one.vm.allocate", template(tpl), False))
        self.res["egw_id"] = vm_id
        return f"{name} #{vm_id} created: management {mgmt_ip}, {self.ix_vnet_name} {ix_ip}, main {self.egw_ip}{note}"

    def igw_type(self):
        return igw_type_of(self.tenant, self.site)

    def _vrouter(self, name):
        """(virtual router id, [its VM ids]) of the native igw `name` (or the name it had before), or (None, [])."""
        vr = None
        for n in self._candidates(name):
            vr = self.one.find("one.vrouterpool.info", "VROUTER", n, -2, -1, -1)
            if vr is not None:
                break
        if vr is None:
            return None, []
        vid = int(vr.findtext("ID"))
        info = self.one.xml("one.vrouter.info", vid, False)
        return vid, [int(x.text) for x in info.findall("VMS/ID")]

    def _native_igw(self, name, nics, key):
        """The native igw: an OpenNebula virtual router from the platform's Service Virtual Router template (the image
        of the same name), configured by its context: routing and NAT out of eth0 (the main network), the tenant's ssh
        key, the site's DNS and time zone. Virtual router templates are instantiated through a virtual router object
        (one.vrouter.allocate + one.vrouter.instantiate); its NICs carry the fixed addresses (no floating IP).
        Returns the VM id."""
        if (
            self.control.service_images.exists()
            and not self.control.service_images.filter(role="one-router-template").exists()
        ):  # deleted on purpose
            raise DeployError(
                f"{self.platform.key} has no native router template (Infrastructure → Platforms → "
                "Service images): add it, or choose a VyOS igw"
            )
        tpl_name = getattr(self.control, "nr_template", "") or "Service Virtual Router"
        t = self.one.find("one.templatepool.info", "VMTEMPLATE", tpl_name, -2, -1, -1)
        if t is None:
            raise DeployError(
                f"template {tpl_name} not found on {self.platform.key}: import the Service Virtual Router "
                "appliance from the marketplace first"
            )
        vr_id, vms = self._vrouter(name)
        if vr_id is None:
            vr_nics = [{k: v for k, v in n.items() if k in ("NETWORK_ID", "IP", "NAME")} for n in nics]
            for n in vr_nics:
                n["FLOATING_IP"] = "NO"
            vr_id = int(self.one.call("one.vrouter.allocate", template({"NAME": name, "NIC": vr_nics})))
            if self.uid is not None and self.gid is not None:  # the tenant's from the start: removed with its group
                self.one.call("one.vrouter.chown", vr_id, self.uid, self.gid)
        if not vms:
            ctx = {
                "NETWORK": "YES",
                "SSH_PUBLIC_KEY": key,
                "TIMEZONE": self._timezone(),
                "SET_HOSTNAME": self.gw_host("igw"),
                "ONEAPP_VNF_ROUTER4_ENABLED": "YES",
                "ONEAPP_VNF_NAT4_ENABLED": "YES",
                "ONEAPP_VNF_NAT4_INTERFACES_OUT": "eth0",
                "ONEAPP_VNF_DNS_NAMESERVERS": self._dns().replace(" ", ","),
            }
            extra = {
                "CONTEXT": ctx,
                "SCHED_REQUIREMENTS": SCHED,
                "MEMORY_RESIZE_MODE": MEMORY_RESIZE_MODE,
            }  # owner, 2026-10-28
            self.one.call("one.vrouter.instantiate", vr_id, 1, int(t.findtext("ID")), name, False, template(extra))
            _vr, vms = self._vrouter(name)
        if not vms:
            raise DeployError(f"virtual router {name} #{vr_id} has no VM")
        if self.uid is not None and self.gid is not None:
            self.one.call("one.vrouter.chown", vr_id, self.uid, self.gid)
        return vms[0]

    def _igw_vm(self, name):
        """The igw VM: a VM of that name (advanced) or the VM of the virtual router of that name (native)."""
        vm_id = self._vm_exists(name)
        if vm_id is None:
            _vr, vms = self._vrouter(name)
            vm_id = vms[0] if vms else None
        return vm_id

    def igw_step(self):
        name = self.gw("igw")
        kind = self.igw_type()
        if kind == "custom":
            self.res.update(igw="", igw_type="custom")
            return f"custom igw: the tenant's own VM takes {self.igw_ip} (gateway {self.egw_ip}); nothing is created"
        existing = self._igw_vm(name) if kind == "onevr" else self._vm_exists(name)
        pubs = [x for x in self.res.get("public_networks") or [] if x.get("id") is not None]
        if existing is not None:
            self.res.update(igw=name, igw_id=existing, igw_user=self.tenant.igw_username)
            att = [f"{x['name']} {self._attach(existing, x['id'], x['igw'])}" for x in pubs]
            return f"{name} #{existing} exists" + (f"; {', '.join(att)}" if att else "")
        user, key = self.tenant.igw_username, (self.tenant.igw_ssh_key or "").strip()
        if kind == "onevr":
            user = user or "root"
        if not user:
            raise DeployError("The igw user name was not given at provisioning.")
        # correction 13/14: the key is optional; without one the igw lets its user in over ssh with the password
        host = self.gw_host("igw")  # correction 11: the host name follows the VM name
        nics = [
            {
                "NETWORK_ID": self.vnet_id if self.vnet_id is not None else -1,
                "IP": self.igw_ip,
                "GATEWAY": self.egw_ip,
                "DNS": self._dns(),
                "NAME": "VMNIC1",
            }
        ]
        # the extra public networks: eth1, eth2, … in their order, so the first boot finds the interfaces it configures
        nics += [{"NETWORK_ID": x["id"], "IP": x["igw"], "NAME": f"VMNIC{i + 2}"} for i, x in enumerate(pubs)]
        self.res.update(igw=name, igw_user=user, igw_type=kind)
        if self.dry:
            return f"would create {name} ({kind}): main {self.igw_ip} via {self.egw_ip}; user {user}, " + (
                "ssh key" if key else "ssh with the password (no key)"
            )
        if kind == "onevr":
            vm_id = self._native_igw(name, nics, key)
            self.one.call("one.vm.chown", vm_id, self.uid, self.gid)
            self.res["igw_id"] = vm_id
            return f"{name} #{vm_id} (Service Virtual Router) created and given to {self.tenant.one_admin_name}: main {self.igw_ip}"
        init, note = self._init("igw")
        pw = self.tenant.get_igw_password()
        crypted = crypt_sha512(pw) if pw else None
        tpl = self._vm_template(
            name,
            host,
            self.tenant.igw_cpu or IGW_CPU,
            (self.tenant.igw_ram_gb * 1024) or IGW_RAM_MB,
            nics,
            user,
            key,
            crypted,
            init,
            ssh_port=self.tenant.igw_ssh_port,
        )
        vm_id = int(self.one.call("one.vm.allocate", template(tpl), False))
        self.one.call("one.vm.chown", vm_id, self.uid, self.gid)
        self.res["igw_id"] = vm_id
        return f"{name} #{vm_id} created and given to {self.tenant.one_admin_name}: main {self.igw_ip}{note}"

    # --- more networks: extra public subnets, corporate networks + cgw, L2 channels --------------

    def _vnet(self, name, net, gw, extra_ars=True, owner_group=True):
        """Create (or find) a VXLAN/EVPN network of the tenant with `gw` as its gateway. Returns (id or None, note)."""
        v = self.one.find("one.vnpool.info", "VNET", name, -2, -1, -1)
        if v is not None:
            return int(v.findtext("ID")), "exists"
        hosts = list(net) if net.prefixlen >= 31 else list(net.hosts())
        ars = [{"TYPE": "IP4", "IP": gw, "SIZE": 1}]
        rest = [h for h in hosts if str(h) != gw]
        if rest and extra_ars:
            ars.append({"TYPE": "IP4", "IP": str(rest[0]), "SIZE": len(rest)})
        tpl = {
            "NAME": name,
            "VN_MAD": "vxlan",
            "VXLAN_MODE": "evpn",
            "VXLAN_TEP": "local_ip",
            "PHYDEV": "vxlan",
            "AUTOMATIC_VLAN_ID": "YES",
            "MTU": MAIN_MTU,
            "GUEST_MTU": MAIN_MTU,
            "METHOD": "static",
            "IP6_METHOD": "disable",
            "NETWORK_ADDRESS": str(net.network_address),
            "NETWORK_MASK": str(net.netmask),
            "GATEWAY": gw,
            "DNS": self._dns(),
            "FILTER_IP_SPOOFING": "NO",
            "FILTER_MAC_SPOOFING": "NO",
            "AR": ars,
        }
        if self.dry:
            return None, "would create"
        vid = int(self.one.call("one.vn.allocate", template(tpl), self.cluster_id))
        if owner_group and self.gid is not None:
            self.one.call("one.vn.chown", vid, -1, self.gid)
            self.one.call("one.vn.chmod", vid, 1, 1, 0, 1, 0, 0, 0, 0, 0)
        return vid, "created"

    def _attach(self, vm_id, vnet_id, ip):
        """Attach a NIC on the network to a VM unless it has one there already."""
        if vm_id is None or vnet_id is None or self.dry:
            return "would attach"
        vm = self.one.xml("one.vm.info", vm_id, False)
        if any(n.findtext("NETWORK_ID") == str(vnet_id) for n in vm.findall("TEMPLATE/NIC")):
            return "attached"
        self.one.call("one.vm.attachnic", vm_id, template({"NIC": {"NETWORK_ID": vnet_id, "IP": ip}}))
        return "attached now"

    def _prefixes(self, kind):
        from .models import PrefixAssignment

        return [
            ipaddress.ip_network(p.cidr)
            for p in PrefixAssignment.active()
            .filter(tenant=self.tenant, site=self.site, kind=kind)
            .order_by("created_at", "pk")
        ]

    def net_name(self, kind, net, taken=()):
        """The OpenNebula name of a tenant network: the recorded one, else the next free number."""
        from inventory import nfconfig

        known = (nfconfig.pub_names if kind == "pub" else nfconfig.corp_names)(self.tenant, self.site)
        if str(net) in known:
            return known[str(net)]
        used = set(known.values()) | set(taken)
        i = 1
        while True:
            name = (
                f"{self.base}-corp_{self.site}" if kind == "corp" and i == 1 else f"{self.base}-{kind}{i}_{self.site}"
            )
            if name not in used:
                return name
            i += 1

    def public_step(self):
        """The extra public networks of the tenant; an existing igw gets a NIC on each (a new igw is created with them)."""
        out = []
        igw_id = self.res.get("igw_id") or self._igw_vm(self.gw("igw"))
        names = []
        for net in self._prefixes("inet_extra"):
            gw = str(net[0]) if net.prefixlen >= 31 else str(next(net.hosts()))
            name = self.net_name("pub", net, names)
            names.append(name)
            vid, note = self._vnet(name, net, gw)
            att = self._attach(igw_id, vid, gw) if igw_id is not None else "with the igw"
            self.res.setdefault("public_networks", [])
            self.res["public_networks"] = [x for x in self.res["public_networks"] if x["name"] != name] + [
                {"name": name, "id": vid, "prefix": str(net), "igw": gw}
            ]
            out.append(f"{name} {net} {note}, igw {gw} {att}")
        return "; ".join(out) or "none"

    def corpnet_step(self):
        out, self.corp_nets = [], []
        names = []
        for net in self._prefixes("corpnet"):
            hosts = list(net) if net.prefixlen >= 31 else list(net.hosts())
            cgw = str(hosts[0])
            name = self.net_name("corp", net, names)
            names.append(name)
            vid, note = self._vnet(name, net, cgw)
            self.corp_nets.append((name, vid, net, cgw))
            out.append(f"{name} {net} {note}: cgw {cgw}")
        self.res["corp_networks"] = [
            {"name": n, "id": v, "prefix": str(net), "cgw": g} for n, v, net, g in self.corp_nets
        ]
        return "; ".join(out) or "none"

    def cgw_step(self):
        if not getattr(self, "corp_nets", None):
            return "no corporate network"
        name = self.gw("cgw")
        existing = self._vm_exists(name)
        if existing is not None:
            self.res.update(cgw=name, cgw_id=existing, **self._router_addresses("cgw", existing))
            return f"{name} #{existing} exists"
        seg = self._ix_segment_for(self.corp_nets[0][2], "corpnet")
        ix_name = f"{seg}_{self.site}"
        ix = self.one.find("one.vnpool.info", "VNET", ix_name, -2, -1, -1)
        if ix is None:
            raise DeployError(f"corporate exchange network {ix_name} not found on {self.platform.key}")
        ix_id = int(ix.findtext("ID"))
        ix_ip = self._free_ip(ix_id, ix_name)
        sa_name, sa = self._sa()
        if not sa.get("public"):
            raise DeployError("the platform's super admin has no ssh key (Infrastructure → Credentials)")
        host = self.gw_host("cgw")  # correction 11: the host name follows the VM name
        mgmt_nic, mgmt_ip = ({"NETWORK_ID": -1}, "«management address»") if self.dry else self._mgmt_nic("cgw")
        nics = [mgmt_nic, {"NETWORK_ID": ix_id, "IP": ix_ip, "NAME": "VMNIC1"}]
        nics += [
            {"NETWORK_ID": vid if vid is not None else -1, "IP": g, "NAME": f"VMNIC{i + 2}"}
            for i, (_n, vid, _net, g) in enumerate(self.corp_nets)
        ]
        self.res.update(cgw=name, cgw_ix_ip=ix_ip, cgw_ix_vnet=ix_name)
        if self.dry:
            return f"would create {name}: {ix_name} {ix_ip}, corp {', '.join(g for _n, _v, _x, g in self.corp_nets)}"
        crypted = crypt_sha512(sa["password"]) if sa.get("password") else None
        init, note = self._init("cgw")
        self._router_nets(*[n.get("NETWORK_ID") for n in nics])
        tpl = self._vm_template(
            name,
            host,
            EGW_CPU,
            EGW_RAM_MB,
            nics,
            sa_name,
            sa["public"],
            crypted,
            init,
            ssh_port=self._vyos_port(),
            cluster_id=getattr(self, "router_cluster_id", None),
            service=True,
        )
        vm_id = int(self.one.call("one.vm.allocate", template(tpl), False))
        self.res["cgw_id"] = vm_id
        return f"{name} #{vm_id} created: management {mgmt_ip}, {ix_name} {ix_ip}{note}"

    def _ix_segment_for(self, net, purpose):
        from inventory.models import DelegatedSubnet

        for d in DelegatedSubnet.objects.filter(
            site__code=self.site, segment__in=self.platform.segments or [], purpose=purpose
        ):
            pool = ipaddress.ip_network(d.cidr)
            if net.version == pool.version and net.subnet_of(pool):
                return d.segment
        raise DeployError(f"{net} is in no {purpose} exchange network of {self.platform.key}.")

    def l2_step(self):
        """L2 channels are made when their request is approved (tenants.netops): VLAN network and src gateway on the
        management platform, dst gateway on the tenant's platform."""
        from .models import L2Channel

        chans = list(self.tenant.l2_channels.exclude(state=L2Channel.State.REMOVED))
        return "; ".join(f"VNI {c.vni}: {c.get_state_display()}" for c in chans) or "none"

    # --- run -------------------------------------------------------------------------------

    STEPS = ("lookups", "group", "admin", "quota", "acl", "network", "egw", "igw")

    @property
    def has_inet(self):
        from .ordering import connections

        return "inet" in connections(self.tenant.order or {})

    def steps_for(self):
        return steps_for(self.tenant)

    def run(self, dep=None):
        from .models import Deployment

        if dep is None:
            dep = new_deployment(self.tenant, self.user, self.dry, self.platform.key)
        self.dep = dep
        names = self.steps_for()
        known = {e["name"]: e for e in dep.steps or []}
        self.steps = [known.get(n) or {"name": n, "status": "pending", "detail": ""} for n in names]
        fns = {
            "lookups": self.lookups,
            "group": self.group_step,
            "admin": self.admin_step,
            "quota": self.quota_step,
            "acl": self.acl_step,
            "network": self.network_step,
            "egw": self.egw_step,
            "igw": self.igw_step,
            "public": self.public_step,
            "corpnet": self.corpnet_step,
            "cgw": self.cgw_step,
            "l2": self.l2_step,
        }
        try:
            for name in names:
                self.step(name, fns[name])
            if not self.dry:
                dep.state = Deployment.State.DONE
                self._record()
        except DeployError as exc:
            dep.error = str(exc)
            if not self.dry:
                dep.state = Deployment.State.FAILED
        dep.steps, dep.resources, dep.finished_at = self.steps, self.res, timezone.now()
        dep.save()
        return dep

    def _record(self):
        """Fix what OpenNebula assigned on the tenant."""
        t = self.tenant
        fields = []
        if self.res.get("group_id") is not None and t.one_group_id != self.res["group_id"]:
            t.one_group_id = self.res["group_id"]
            fields.append("one_group_id")
        if self.res.get("admin_user_id") is not None:
            t.one_admin_user, t.one_admin_id = self.res["admin_user"], self.res["admin_user_id"]
            fields += ["one_admin_user", "one_admin_id"]
        if fields:
            t.save(update_fields=fields)


def object_base(tenant):
    """The prefix of a tenant's OpenNebula objects: its group name; a tenant-<name> group names its objects
    tenant_<name>-egw_<site>, tenant_<name>-main_<site>, …"""
    g = tenant.group_name or ""
    if g.startswith("tenant-"):
        return "tenant_" + g[len("tenant-") :]
    return g


def igw_type_of(tenant, site=None):
    """The tenant's igw type on a site: vyos (advanced), onevr (native) or custom; "" without the internet."""
    from .models import Connectivity

    site = site or (tenant.order or {}).get("site")
    c = next((c for c in Connectivity.current_for(tenant) if c.site == site), None)
    if c is not None:
        return c.igw_type or ("vyos" if "inet" in c.types else "")
    o = tenant.order or {}
    return (o.get("igw_type") or "vyos") if "inet" in (o.get("connections") or []) else ""


def steps_for(tenant):
    """The steps of a tenant: base, internet (main network, egw, igw), extra public networks, corporate, L2."""
    from .models import PrefixAssignment
    from .ordering import connections

    names = ["lookups", "group", "admin", "quota", "acl"]
    mine = PrefixAssignment.active().filter(tenant=tenant)
    if "inet" in connections(tenant.order or {}):
        names += ["network"] + (["public"] if mine.filter(kind="inet_extra").exists() else []) + ["egw", "igw"]
    if mine.filter(kind="corpnet").exists():
        names += ["corpnet", "cgw"]
    return names


def new_deployment(tenant, user, dry, platform_key=None, notify=()):
    from .models import Deployment

    return Deployment.objects.create(
        tenant=tenant,
        platform_key=platform_key or (tenant.order or {}).get("platform", ""),
        created_by=user,
        state=Deployment.State.PLAN if dry else Deployment.State.RUNNING,
        notify=list(notify or []),
        steps=[{"name": n, "status": "pending", "detail": ""} for n in steps_for(tenant)],
    )


def _execute(dep_id, user_id, dry):
    from accounts.models import User, audit

    from .models import Deployment

    dep = Deployment.objects.select_related("tenant").get(pk=dep_id)
    user = User.objects.filter(pk=user_id).first() if user_id else None
    try:
        d = Deployer(dep.tenant, user, dry)
    except DeployError as exc:
        dep.error, dep.finished_at = str(exc), timezone.now()
        if not dry:
            dep.state = Deployment.State.FAILED
        dep.save()
        return dep
    if not dry:
        audit(user, "deploy_started", dep.tenant, platform=dep.platform_key)
    dep = d.run(dep)
    if not dry:
        audit(
            user,
            "tenant_deployed" if dep.state == "done" else "tenant_deploy_failed",
            dep.tenant,
            platform=dep.platform_key,
            error=dep.error[:300],
        )
        from .gwstatus import refresh  # the Gateways card shows the VMs just made (read only; never raises)

        refresh(dep.tenant)
        if dep.state == "done":  # correction 13: L2 channels that waited for the tenant
            try:
                from .network_views import l2_maybe_start

                for ch in dep.tenant.l2_channels.filter(state="planned"):
                    l2_maybe_start(ch, user)
            except Exception as exc:  # noqa: BLE001 - logged; the channels can be started again by setting the subnet
                audit(user, "l2_start_failed", dep.tenant, error=f"{type(exc).__name__}: {exc}"[:300])
        if dep.state == "done" and (
            dep.notify or dep.tenant.tenant_admin_id
        ):  # correction 15: tenant admin + platform users
            try:
                from .provision import send_credentials

                send_credentials(dep)
            except Exception as exc:  # noqa: BLE001 - the deployment stays done; the failure is logged
                audit(user, "tenant_credentials_failed", dep.tenant, error=f"{type(exc).__name__}: {exc}"[:300])
    return dep


def deploy(tenant, user=None, dry=False):
    """Run (or preview, dry=True) the deployment of a tenant now. Returns the Deployment; never raises DeployError."""
    dep = new_deployment(tenant, user, dry)
    return _execute(dep.pk, getattr(user, "pk", None), dry)


def start(tenant, user=None, dry=False, notify=()):
    """Start a deployment: in a background thread (settings.DEPLOY_ASYNC), else at once. Returns the Deployment.
    notify: user ids who get the credentials when it is done (correction 13)."""
    import threading

    from django.conf import settings
    from django.db import connection

    dep = new_deployment(tenant, user, dry, notify=notify)
    if not getattr(settings, "DEPLOY_ASYNC", True):
        return _execute(dep.pk, getattr(user, "pk", None), dry)

    def worker():
        try:
            _execute(dep.pk, getattr(user, "pk", None), dry)
        except Exception as exc:  # noqa: BLE001 - recorded on the deployment
            from .models import Deployment

            Deployment.objects.filter(pk=dep.pk).update(
                state="failed", error=f"{type(exc).__name__}: {exc}", finished_at=timezone.now()
            )
        finally:
            connection.close()

    threading.Thread(target=worker, name=f"deploy-{tenant.pk}", daemon=True).start()
    return dep


def blocked_reason(tenant):
    """Why an approved tenant is not deployed automatically, or "" when it can be."""
    from inventory.models import PlatformControl

    o = tenant.order or {}
    if tenant.is_deleted:
        return "the project is deleted"
    c = PlatformControl.objects.filter(key=o.get("platform")).first()
    if c is None or c.mode != "execution":
        return f"{o.get('platform') or 'the platform'} is in read-only mode"
    return ""


def set_one_password(tenant, password):
    """Change the password of the tenant's OpenNebula admin (on the primary zone of its platform)."""
    from inventory.models import Platform, PlatformControl

    p = Platform.objects.filter(key=(tenant.order or {}).get("platform")).first()
    if p is None:
        raise DeployError("the project has no platform")
    c = PlatformControl.objects.filter(key=p.fe_primary or p.key).first()
    if c is None or not c.configured:
        raise DeployError(f"{p.fe_primary or p.key}: endpoint or credentials not found")
    One(c).call("one.user.passwd", tenant.one_admin_id, password)


def quota_for(tenant, platform, clusters, datastores):
    """The group quota of a tenant on its platform from the portal's current quota records, one VM entry per cluster
    (2026-10-27: a tenant with several clusters — e.g. a GPU and a CPU one — gets each from its own record; the
    platform's other clusters are reset). clusters: {name: id}; datastores: [(id, type, {cluster ids})]."""
    from inventory.models import Cluster

    from .models import split_of

    recs = [r for r in tenant.current_quota_records() if r.site == platform.site.code]
    if not recs:
        raise DeployError("No quota record for this site.")
    by_code = {}  # quota code → the cluster of that code that is on the platform (not retired)
    for c in Cluster.objects.filter(platform=platform).exclude(quota_code="").exclude(state="retired").order_by("pk"):
        if c.name in clusters:
            by_code.setdefault(c.quota_code, c)
    vm, used, images_gb, files_gb, backups_gb = [], set(), {}, 0, 0
    image_ds = [(did, cl) for did, dtype, cl in datastores if dtype == "0"]
    for r in recs:
        c = by_code.get(r.cluster)
        cid = clusters.get(c.name) if c is not None else None
        if cid is None:
            raise DeployError(f"{platform.key}: no cluster with quota code {r.cluster} on the platform")
        vms_disk, images, files = split_of(r)
        cpu, ram, gpu = r.cpu, r.ram_gb, r.gpu or 0
        vm.append(
            {
                "CLUSTER_IDS": str(cid),
                "VMS": cpu * 2,
                "RUNNING_VMS": cpu,
                "CPU": cpu * 2,
                "RUNNING_CPU": cpu,
                "MEMORY": ram * 2 * 1024,
                "RUNNING_MEMORY": ram * 1024,
                "SYSTEM_DISK_SIZE": vms_disk * 1024,
                "PCI_DEV": gpu,
                "RUNNING_PCI_DEV": gpu,
                "PCI_NIC": 0,
                "RUNNING_PCI_NIC": 0,
            }
        )
        used.add(cid)
        for did, cl in image_ds:
            if cid in cl:
                images_gb[did] = images_gb.get(did, 0) + images
        files_gb += files  # files: set per pool, one datastore per site shared by all the pools: the sum
        # backups: one figure per site, carried by every pool of it (owner 2026-11-09, 2026-11-11) - not a sum
        backups_gb = max(backups_gb, r.backups_gb or 0)
    reset = {
        k: -1
        for k in (
            "VMS",
            "RUNNING_VMS",
            "CPU",
            "RUNNING_CPU",
            "MEMORY",
            "RUNNING_MEMORY",
            "SYSTEM_DISK_SIZE",
            "PCI_DEV",
            "RUNNING_PCI_DEV",
            "PCI_NIC",
            "RUNNING_PCI_NIC",
        )
    }
    vm += [{"CLUSTER_IDS": str(cid), **reset} for cid in sorted(clusters.values()) if cid not in used]
    # owner 2026-11-11: the image datastores of the platform's other clusters are closed (0), not unlimited (-1): the
    # platforms set no default quotas, so -1 let a group with wider permissions store images there without a limit
    ds = [{"ID": did, "IMAGES": -1, "SIZE": images_gb[did] * 1024 if did in images_gb else 0} for did, _cl in image_ds]
    for dtype, gb in (("2", files_gb), ("3", backups_gb)):
        did = next((d for d, t, _cl in datastores if t == dtype), None)
        if did is not None:
            ds.append({"ID": did, "IMAGES": -1, "SIZE": gb * 1024})
    return {"VM": vm, "DATASTORE": ds}


def apply_quota(tenant, user=None):
    """Set the tenant's current quota in OpenNebula (every cluster of its records); a tenant the portal made also gets
    its group's template copy. A tenant-<name> group: only the group quota — nothing else of the group changes. Returns a
    short report."""
    from accounts.models import audit
    from inventory.models import Platform, PlatformControl

    if tenant.one_group_id is None:
        return "not deployed: nothing to change in OpenNebula"
    reason = blocked_reason(tenant)
    if reason:
        return f"not changed in OpenNebula: {reason}"
    platform = Platform.objects.select_related("site").filter(key=(tenant.order or {}).get("platform")).first()
    control = PlatformControl.objects.filter(key=platform.key).first() if platform else None
    if control is None or not control.configured:
        raise DeployError(
            f"{(tenant.order or {}).get('platform') or 'the platform'}: endpoint or credentials not found"
        )
    one = One(control)
    clusters = {c.findtext("NAME"): int(c.findtext("ID")) for c in one.xml("one.clusterpool.info").findall("CLUSTER")}
    datastores = [
        (int(d.findtext("ID")), d.findtext("TYPE"), {int(x.text) for x in d.findall("CLUSTERS/ID")})
        for d in one.xml("one.datastorepool.info").findall("DATASTORE")
    ]
    q = quota_for(tenant, platform, clusters, datastores)
    one.call("one.group.quota", tenant.one_group_id, template(q))
    if not tenant.legacy_names:  # the portal's own tenants keep the quota copy in their group's template
        d = Deployer(tenant, user)
        d.one = one
        d.gone = One(d.primary_control) if d.primary_control.key != d.control.key else one
        d.cluster_ids = sorted(clusters.values())
        d.cluster_id = clusters.get(d.cluster.name)
        d.datastores = {
            "images": [i for i, t, cl in datastores if t == "0" and d.cluster_id in cl],
            "all": [i for i, t, _cl in datastores if t == "0"],
            "files": next((i for i, t, _cl in datastores if t == "2"), None),
            "backups": next((i for i, t, _cl in datastores if t == "3"), None),
        }
        d.gid = tenant.one_group_id
        d.net, d.egw_ip, d.igw_ip, d.hosts = d._addresses()
        d.gone.call("one.group.update", d.gid, template(d._group_template()), 1)
    got = [e for e in q["VM"] if e["RUNNING_CPU"] != -1]
    report = "quota set: " + "; ".join(
        f"cluster #{e['CLUSTER_IDS']} {e['RUNNING_CPU']} vCPU, {e['RUNNING_MEMORY'] // 1024} GB RAM, {e['PCI_DEV']} GPU, "
        f"{e['SYSTEM_DISK_SIZE'] // 1024} GB VM disks"
        for e in got
    )
    audit(
        user,
        "quota_applied",
        tenant,
        platform=platform.key,
        report=report,
        group=tenant.one_group_id,
        legacy_names=tenant.legacy_names,
    )
    return report
