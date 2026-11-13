"""A tenant's gateway made again from the templates (the tenant page's Redeploy: egw, cgw; the igw with a saved
configuration, tenants.igwconfig). The old VM is set aside, the new one made on the same networks and addresses, and
the old one removed once the new one passes its checks. Each run is a Deployment of the tenant (its steps on the
tenant page and in Tasks).
"""

import time
import types

from django.utils import timezone

from .deploy import Deployer, DeployError, template
from .netops import NetChange


def order_from_records(tenant):
    """Where a tenant without an order lives, as an order: platform, cluster and connections from its records."""
    from inventory.models import Cluster, Platform

    from .models import Connectivity

    recs = list(tenant.current_quota_records())
    conns = list(Connectivity.current_for(tenant))
    site = (recs[0].site if recs else None) or (conns[0].site if conns else None)
    if not site:
        raise DeployError("the project has no resource or connection record")
    code = recs[0].cluster if recs else ""
    cl = (
        Cluster.objects.select_related("platform").filter(platform__site__code=site, quota_code=code).first()
        if code
        else None
    )
    platform = cl.platform if cl else Platform.objects.filter(site__code=site, kind="shared").order_by("key").first()
    if platform is None:
        raise DeployError(f"no shared platform on {site}")
    connections = []
    for c in conns:
        if c.connection_type in ("inet", "corpnet") and c.connection_type not in connections:
            connections.append(c.connection_type)
        if c.l2_vlans and "l2" not in connections:
            connections.append("l2")
    return {
        "platform": platform.key,
        "site": site,
        "cluster": code or (cl.name if cl else ""),
        "connections": connections,
        "igw_type": "vyos",
    }


def ensure_order(tenant):
    if not (tenant.order or {}).get("platform"):
        tenant.order = {**(tenant.order or {}), **order_from_records(tenant)}
        tenant.save(update_fields=["order"])
    return tenant


def old_vm(one, name):
    """What the old VM is: id, owner, size and its NICs in order (iface, network id and name, ip), or None. name: one
    name or several (a gateway's name and the one it had before correction 11), the first found."""
    v = None
    for n in [name] if isinstance(name, str) else list(name):
        v = one.find("one.vmpool.info", "VM", n, -2, -1, -1, -1)
        if v is not None:
            break
    if v is None:
        return None
    full = one.xml("one.vm.info", int(v.findtext("ID")), False)
    nics = []
    for i, n in enumerate(full.findall("TEMPLATE/NIC")):
        nics.append(
            {
                "iface": f"eth{i}",
                "network_id": int(n.findtext("NETWORK_ID") or -1),
                "network": n.findtext("NETWORK") or "",
                "ip": n.findtext("IP") or "",
                "mac": n.findtext("MAC") or "",
                "nic_id": int(n.findtext("NIC_ID") or i),
            }
        )
    return {
        "id": int(full.findtext("ID")),
        "name": full.findtext("NAME") or "",
        "uid": int(full.findtext("UID")),
        "gid": int(full.findtext("GID")),
        "cpu": full.findtext("TEMPLATE/VCPU") or full.findtext("TEMPLATE/CPU") or "1",
        "memory": full.findtext("TEMPLATE/MEMORY") or "1024",
        "nics": nics,
        "state": full.findtext("STATE"),
    }


class Takeover(NetChange):
    """A tenant's gateway made again by the portal (a Deployment of the tenant)."""

    def __init__(self, tenant, user=None):
        ensure_order(tenant)
        Deployer.__init__(self, tenant, user)
        self.req = None
        self.gid, self.uid = tenant.one_group_id, tenant.one_admin_id
        self.dep_rec, self.known = None, {}
        self.fixed_ips = {}

    def _save(self):
        Deployer._save(self)

    def _free_ip(self, vnet_id, vnet_name, avoid=(), ar_index=None):
        if vnet_id in self.fixed_ips:  # the old gateway's address on that network
            return self.fixed_ips[vnet_id]
        return Deployer._free_ip(self, vnet_id, vnet_name, avoid, ar_index)

    def find_group(self):
        g = self.gone.find("one.grouppool.info", "GROUP", self.group)
        if g is None:
            raise DeployError(f"group {self.group} not found")
        self.gid = int(g.findtext("ID"))
        if self.tenant.one_group_id != self.gid:
            self.tenant.one_group_id = self.gid
            self.tenant.save(update_fields=["one_group_id"])
        self.res["group_id"] = self.gid
        return f"group {self.group} #{self.gid}"

    def _remove_old(self, role, name):
        old = old_vm(self.one, self._candidates(name))
        if old is None:
            return None, f"{name}: no old VM"
        self.res[f"old_{role}"] = old
        self._save()
        self.one.call("one.vm.action", "terminate-hard", old["id"])
        self._wait(old["id"], gone=True)
        return (
            old,
            f"{name} #{old['id']} removed (recorded: {', '.join(n['network'] + ' ' + n['ip'] for n in old['nics'])})",
        )

    # --- L2 channel made again (owner 2026-11-11) ----------------------------------------------------

    def redeploy_l2(self, ch):
        """One L2 channel's gateways made again: removed (l2_delete), then made on the same channel (l2_add) with the
        standard names. The channel's VLAN and tenant network stay; its traffic stops for a few minutes."""
        from .models import L2Channel

        self.req = types.SimpleNamespace(channel=ch, target=str(ch.vni))
        self.l2_delete()
        ch.refresh_from_db()
        ch.vm_a = ch.vm_b = ch.vlan_net_id = ch.ether_ar = None
        ch.state = L2Channel.State.PLANNED
        ch.save(update_fields=["vm_a", "vm_b", "vlan_net_id", "ether_ar", "state"])
        self.l2_add()
        self.req = None
        return f"VNI {ch.vni} (VLAN {ch.vlan}) made again"

    def run_l2(self, dep, vlan):
        from .models import L2Channel

        ch = self.tenant.l2_channels.exclude(state=L2Channel.State.REMOVED).filter(vlan=vlan).first()
        if ch is None:
            raise DeployError(f"no L2 channel on VLAN {vlan}")
        self.res["redeploy"] = f"l2 {vlan}"
        return self._run(
            dep,
            ["lookups", "group", "l2"],
            {"lookups": self.lookups, "group": self.find_group, "l2": lambda: self.redeploy_l2(ch)},
        )

    # --- igw --------------------------------------------------------------------------------------

    def new_igw(self):
        from inventory import nfconfig

        from . import igwport

        name = self.gw("igw")
        text = self.tenant.get_igw_config()
        result = igwport.check(text, (self.res.get("old_igw") or {}).get("nics") or [])
        if not result["transferable"]:
            raise DeployError("the configuration is not transferable: " + "; ".join(result["problems"][:5]))
        old = self.res["old_igw"]
        user, key = igwport.login_user(text)
        if not user:
            user, key = self.tenant.igw_username or "vyos", (self.tenant.igw_ssh_key or "").strip()
        if not key:
            raise DeployError("no login user with an ssh key in the configuration, and none on the project")
        lines = list(result["kept"])
        su = nfconfig.service_user()
        if su:  # the portal reads the igw's configuration with its service user
            lines += [
                f"set system login user {su['name']} authentication public-keys {su['name']} key {su['key']}",
                f"set system login user {su['name']} authentication public-keys {su['name']} type {su['type']}",
            ]
        init = (
            __import__("base64")
            .b64encode(nfconfig.init_script("\n".join(lines) + "\n", f"{self.group} igw").encode())
            .decode()
        )
        nics = [
            {"NETWORK_ID": n["network_id"], "IP": n["ip"], "NAME": f"VMNIC{i + 1}"}
            if n["ip"]
            else {"NETWORK_ID": n["network_id"], "NAME": f"VMNIC{i + 1}"}
            for i, n in enumerate(old["nics"])
        ]
        tpl = self._vm_template(
            name, name.replace("_", "-"), int(float(old["cpu"])), int(float(old["memory"])), nics, user, key, None, init
        )
        vm_id = int(self.one.call("one.vm.allocate", template(tpl), False))
        self.one.call("one.vm.chown", vm_id, old["uid"], old["gid"])
        self.res.update(igw=name, igw_id=vm_id, igw_type="vyos")
        return (
            f"{name} #{vm_id} created with the uploaded configuration ({len(result['kept'])} commands) on "
            f"{', '.join(n['network'] + ' ' + n['ip'] for n in old['nics'])}; given to the old owner"
        )

    def record_igw(self):
        from .models import Connectivity

        cur = next((c for c in Connectivity.current_for(self.tenant) if c.site == self.site), None)
        if cur is not None and cur.igw_type != "vyos":
            vals = cur.values()
            vals["igw_type"] = "vyos"
            Connectivity.objects.create(
                tenant=self.tenant,
                site=self.site,
                version=Connectivity.next_version(self.tenant, self.site),
                created_by=self.user,
                **vals,
            )
        return "igw recorded"

    def run_igw(self, dep):
        from . import igwport

        self.dep = dep
        name = self.gw("igw")

        def check():
            old = old_vm(self.one, self._candidates(name))
            if old is None:
                raise DeployError(f"{name}: no old igw")
            result = igwport.check(self.tenant.get_igw_config(), old["nics"])
            if not result["transferable"]:  # nothing is removed
                raise DeployError("the configuration is not transferable: " + "; ".join(result["problems"][:5]))
            return f"{name} #{old['id']}: {result['commands']} commands, transferable" + (
                f"; {'; '.join(result['warnings'])}" if result["warnings"] else ""
            )

        def remove():
            old, note = self._remove_old("igw", name)
            if old is None:
                raise DeployError(f"{name}: no old igw")
            return note

        fns = {
            "lookups": self.lookups,
            "group": self.find_group,
            "check": check,
            "igw-remove": remove,
            "igw": self.new_igw,
            "record": self.record_igw,
        }
        return self._run(dep, list(fns), fns)

    # --- a gateway made again (correction 11) -------------------------------------------------------------

    CHECK_TIMEOUT = 600  # s: the new gateway answers ssh in VRF MGMT and has its BGP peers established
    BGP = "vtysh -c 'show bgp summary json'"

    def _poll(self):
        from .netops import _poll

        return _poll()

    def _wait_state(self, vm_id, state, timeout):
        end = time.monotonic() + timeout
        while True:
            if self.one.xml("one.vm.info", vm_id, False).findtext("STATE") == state:
                return
            if time.monotonic() > end:
                raise DeployError(f"VM #{vm_id} is not in state {state} after {timeout} s")
            time.sleep(self._poll())

    def _off(self, vm_id):
        """Power off: soft, hard when the guest has not stopped within 120 s. Returns how."""
        self.one.call("one.vm.action", "poweroff", vm_id)
        try:
            self._wait_state(vm_id, "8", 120)
            return "soft"
        except DeployError:
            self.one.call("one.vm.action", "poweroff-hard", vm_id)
            self._wait_state(vm_id, "8", 300)
            return "hard"

    def gateway_check(self, role):
        """The new gateway checked as a route reflector (tenants.gwstatus.run_checks): it answers ssh on its management
        address (VRF MGMT), the platform's addresses are on it, its BGP peers are established (ready with at least one).
        The last result is stored on the tenant (its Gateways card), passed or not."""
        from inventory.credentials import account
        from inventory.sshexec import SshError

        from . import gwstatus

        ip = self.res.get(f"{role}_mgmt_ip")
        if not ip:
            raise DeployError(f"the new {role} has no management address")
        user, acc = account(self.platform.sa)
        port = self._vyos_port()
        vm_id = self.res.get(f"{role}_id")
        end, res = time.monotonic() + self.CHECK_TIMEOUT, None
        while True:
            try:
                x = self.one.xml("one.vm.info", vm_id, False) if vm_id is not None else None
                nics = (
                    [(n.findtext("NETWORK") or "", n.findtext("IP") or "") for n in x.findall("TEMPLATE/NIC")]
                    if x is not None
                    else None
                )
                checks, facts = gwstatus.run_checks(ip, port, user, acc.get("key") or "", self.site, nics)
                res = gwstatus.result(checks, facts)
                if gwstatus.ready(checks):
                    gwstatus.store(self.tenant, role, res)
                    return f"{ip}:{port} (VRF MGMT): {res['detail']}"
            except (SshError, ValueError) as exc:
                res = gwstatus.result(
                    [
                        {
                            "check": "answers ssh on the management address (VRF MGMT)",
                            "ok": False,
                            "detail": f"{ip}:{port}: {str(exc)[:200]}",
                        }
                    ]
                )
            if time.monotonic() > end:
                gwstatus.store(self.tenant, role, res)
                raise DeployError(f"the new {role} is not ready after {self.CHECK_TIMEOUT} s: {res['detail']}")
            time.sleep(self._poll() * 5)

    def replace(self, role):
        """Correction 11: the egw or cgw made again from the templates — nothing is read from the old one's configuration.

        The new VM gets a new management address (VRF MGMT) and a new address on the exchange network; it keeps only the
        tenant-side addresses (egw: the main network; cgw: the corporate networks). The old VM is powered off and gives up
        those NICs (renamed <name>-old) — the tenant's traffic stops until the new one is up —, the new one is made under
        the new name (_<org>_<tenant>_<role>_<site>, host name <role>-<org>-<tenant>-<site>) and must answer ssh in VRF
        MGMT with its BGP peers established; then the old VM is removed. If the check fails, the new VM is removed and
        the old one comes back as it was (and the run fails). Steps: <role> prepare, <role> old aside, <role>,
        <role> check, <role> old removed (or <role> rollback)."""
        import ipaddress

        from .models import PrefixAssignment

        name = self.gw(role)
        st = {"old": None, "keep": [], "new": None}

        def prepare():
            old = old_vm(self.one, self._candidates(name))
            st["old"] = old
            if role == "egw":
                self.network_step()
                st["keep"] = [n for n in (old or {}).get("nics", []) if n["network_id"] == self.vnet_id and n["ip"]]
                if st["keep"]:
                    self.egw_ip = st["keep"][0]["ip"]
            else:
                self.corpnet_step()
                corp = [
                    ipaddress.ip_network(p.cidr)
                    for p in PrefixAssignment.active().filter(tenant=self.tenant, kind="corpnet")
                ]
                ids = {vid for _n, vid, _net, _g in self.corp_nets}
                st["keep"] = [
                    n
                    for n in (old or {}).get("nics", [])
                    if n["ip"] and (n["network_id"] in ids or any(ipaddress.ip_address(n["ip"]) in c for c in corp))
                ]
            if old is None:
                return f"{name}: no {role} on {self.platform.key} — a new one is made"
            return (
                f"{old['name']} #{old['id']}: keeps "
                + (", ".join(f"{n['network']} {n['ip']}" for n in st["keep"]) or "nothing")
                + "; management and exchange addresses: new"
            )

        def aside():
            old = st["old"]
            if old is None:
                return "no old VM"
            how = self._off(old["id"])
            for n in st["keep"]:
                self.one.call("one.vm.detachnic", old["id"], n["nic_id"])
                self._wait_state(old["id"], "8", 300)
            self.one.call("one.vm.rename", old["id"], f"{name}-old")
            return (
                f"{old['name']} #{old['id']} powered off{' (hard)' if how == 'hard' else ''}, "
                f"{len(st['keep'])} NIC(s) detached, renamed {name}-old"
            )

        def make():
            self.res.pop(f"{role}_id", None)
            note = (self.egw_step if role == "egw" else self.cgw_step)()
            st["new"] = self.res.get(f"{role}_id")
            return note

        def check():
            self._wait(st["new"], timeout=600)
            return self.gateway_check(role)

        def remove():
            old = st["old"]
            if old is None:
                return "no old VM"
            self.one.call("one.vm.action", "terminate-hard", old["id"])
            self._wait(old["id"], gone=True)
            return f"{name}-old #{old['id']} removed"

        def rollback():
            out = []
            if st["new"] is not None:
                self.one.call("one.vm.action", "terminate-hard", st["new"])
                self._wait(st["new"], gone=True)
                out.append(f"new #{st['new']} removed")
            old = st["old"]
            if old is not None:
                if self.one.xml("one.vm.info", old["id"], False).findtext("NAME") != old["name"]:
                    self.one.call("one.vm.rename", old["id"], old["name"])
                for n in st["keep"]:
                    self.one.call(
                        "one.vm.attachnic", old["id"], template({"NIC": {"NETWORK_ID": n["network_id"], "IP": n["ip"]}})
                    )
                    self._wait_state(old["id"], "8", 300)
                if self.one.xml("one.vm.info", old["id"], False).findtext("STATE") == "8":
                    self.one.call("one.vm.action", "resume", old["id"])
                    self._wait(old["id"], timeout=600)
                out.append(
                    f"{old['name']} #{old['id']} back with "
                    + (", ".join(f"{n['network']} {n['ip']}" for n in st["keep"]) or "its NICs")
                    + ", running"
                )
            return "; ".join(out) or "nothing to undo"

        self.step(f"{role} prepare", prepare)
        self.step(f"{role} old aside", aside)
        try:
            self.step(role, make)
            self.step(f"{role} check", check)
        except DeployError:
            try:
                self.step(f"{role} rollback", rollback)
            except DeployError:
                pass
            raise
        self.step(f"{role} old removed", remove)
        return f"{role} made again"

    @staticmethod
    def replace_steps(role):
        return [f"{role} prepare", f"{role} old aside", role, f"{role} check", f"{role} old removed"]

    def run_replace(self, dep, role):
        """One gateway made again (the tenant page's Redeploy): a Deployment of the tenant with these steps. The
        deployment's resources start from the last finished deployment's (the other routers stay known)."""
        from .models import Deployment

        self.dep = dep
        prev = self.tenant.deployments.filter(state=Deployment.State.DONE).exclude(pk=dep.pk).first()
        self.res.update({k: v for k, v in ((prev.resources or {}) if prev else {}).items() if k != "redeploy"})
        self.res["redeploy"] = role
        self.steps = [
            {"name": n, "status": "pending", "detail": ""} for n in ["lookups", "group"] + self.replace_steps(role)
        ]
        try:
            self.step("lookups", self.lookups)
            self.step("group", self.find_group)
            self.replace(role)
            dep.state = Deployment.State.DONE
        except DeployError as exc:
            dep.error = str(exc)
            dep.state = Deployment.State.FAILED
        if not self.dry:
            from .gwstatus import refresh

            refresh(self.tenant)  # the page shows the platform as it is now
        dep.steps, dep.resources, dep.finished_at = self.steps, self.res, timezone.now()
        dep.save()
        return dep

    # --- run ---------------------------------------------------------------------------------------

    def _run(self, dep, names, fns):
        from .models import Deployment

        self.steps = [{"name": n, "status": "pending", "detail": ""} for n in names]
        try:
            for n in names:
                self.step(n, fns[n])
            dep.state = Deployment.State.DONE
        except DeployError as exc:
            dep.error = str(exc)
            dep.state = Deployment.State.FAILED
        dep.steps, dep.resources, dep.finished_at = self.steps, self.res, timezone.now()
        dep.save()
        return dep


def start(tenant, what, user=None):
    """Start a run in the background (what: egw, cgw, l2<vlan> - that L2 channel's gateways - or igw with its saved
    configuration). Returns the Deployment."""
    import threading

    from django.conf import settings
    from django.db import connection

    from accounts.models import audit

    from .models import Deployment

    ensure_order(tenant)
    dep = Deployment.objects.create(
        tenant=tenant,
        platform_key=(tenant.order or {}).get("platform", ""),
        created_by=user,
        state=Deployment.State.RUNNING,
        steps=[],
        resources={"redeploy": what},
    )
    kind = f"gateway_{'l2' if what.startswith('l2') else what}_redeploy"
    audit(user, f"{kind}_started", tenant, platform=dep.platform_key)

    def work():
        try:
            t = Takeover(tenant, user)
            if what in ("egw", "cgw"):
                d = t.run_replace(dep, what)
            else:
                d = t.run_l2(dep, int(what[2:])) if what.startswith("l2") else t.run_igw(dep)
        except DeployError as exc:
            Deployment.objects.filter(pk=dep.pk).update(state="failed", error=str(exc), finished_at=timezone.now())
            d = Deployment.objects.get(pk=dep.pk)
        audit(user, f"{kind}_{'done' if d.state == 'done' else 'failed'}", tenant, error=(d.error or "")[:300])
        return d

    if not getattr(settings, "DEPLOY_ASYNC", True):
        return work()

    def worker():
        try:
            work()
        except Exception as exc:  # noqa: BLE001 - recorded on the deployment
            Deployment.objects.filter(pk=dep.pk).update(
                state="failed", error=f"{type(exc).__name__}: {exc}", finished_at=timezone.now()
            )
        finally:
            connection.close()

    threading.Thread(target=worker, name=f"takeover-{tenant.pk}", daemon=True).start()
    return dep
