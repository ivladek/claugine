"""Network changes of a deployed tenant, run after approval (Connectivity → requests).

    public network, add       vnet with the delegated subnet in the tenant; a NIC on the igw; igw config
                              (interface, PUB group and zone); egw config (CLIENT group, static route via the igw)
    public network, delete    egw config (route and CLIENT group); igw config; NICs of every VM on the network
                              detached; vnet deleted; prefix released
    main network, replace     egw removed; NICs of every VM on the old main network detached (igw too); old main
                              network deleted and its prefix released; the new prefix becomes the main one; new
                              main network (one range from the address after the egw) and egw made by a deployment
                              run; the igw (kept) connected again with the address after the egw (correction 15)
    corporate network, add    vnet in the tenant; a NIC on the cgw; cgw config (interface, CLIENT group and zone)
                              — a tenant without a cgw gets one from a deployment run
    corporate network, delete cgw config; NICs of every VM on the network detached; vnet deleted; prefix released
    L2 channel, add           on the site's management platform: the VLAN network (802.1Q, ethernet only) and the
                              src gateway; on the tenant's platform: an ethernet address range on the tenant
                              network and the dst gateway; VXLAN between them over cloud-s2s
    L2 channel, delete        both gateways, the VLAN network and the ethernet address range

Router configurations are changed over ssh (tenants.routercfg), always from Infrastructure → Templates.
Every step is kept on the request and in the log.
"""
import ipaddress
import time

from django.conf import settings
from django.utils import timezone

from .deploy import EGW_CPU, EGW_RAM_MB, DeployError, Deployer, One, crypt_sha512, template, with_public

L2_CPU, L2_RAM_MB = 1, 1024


def _poll():
    return getattr(settings, "NETOPS_POLL_SECONDS", 3)


class NetChange(Deployer):
    def __init__(self, req, user=None):
        super().__init__(req.tenant, user)
        self.req = req
        self.gid, self.uid = req.tenant.one_group_id, req.tenant.one_admin_id
        dep = req.tenant.deployments.exclude(state="plan").first()
        self.dep_rec = dep
        self.known = dict((dep.resources or {}) if dep else {})

    # --- bookkeeping ----------------------------------------------------------------------------

    def _save(self):
        self.req.steps = self.steps
        self.req.save(update_fields=["steps"])

    def _record(self, key, rows):
        """Keep the tenant's network list (deployment resources) in step with the change."""
        if self.dep_rec is None:
            return
        res = dict(self.dep_rec.resources or {})
        res[key] = rows
        self.dep_rec.resources = res
        self.dep_rec.save(update_fields=["resources"])
        self.known = res

    def _rows(self, key):
        return list(self.known.get(key) or [])

    # --- OpenNebula helpers -----------------------------------------------------------------------

    def _vnet_id(self, name, one=None):
        v = (one or self.one).find("one.vnpool.info", "VNET", name, -2, -1, -1)
        return int(v.findtext("ID")) if v is not None else None

    def _wait(self, vm_id, gone=False, one=None, timeout=300):
        """Until the VM runs again (after a hotplug) or is gone (after terminate)."""
        one = one or self.one
        end = time.monotonic() + timeout
        while True:
            try:
                vm = one.xml("one.vm.info", vm_id, False)
                state, lcm = vm.findtext("STATE"), vm.findtext("LCM_STATE")
            except DeployError:
                if gone:
                    return
                raise
            if gone and state == "6":
                return
            if not gone and state == "3" and lcm == "3":
                return
            if not gone and state in ("7", "8") or lcm in ("36", "37", "38", "39", "40", "41", "42", "43", "44"):
                raise DeployError(f"VM #{vm_id} is in a failure state ({state}/{lcm})")
            if time.monotonic() > end:
                raise DeployError(f"VM #{vm_id}: still {state}/{lcm} after {timeout} s")
            time.sleep(_poll())

    def _users_of(self, vid, one=None):
        """{vm id: [nic ids]} of the VMs with a NIC on the network."""
        one = one or self.one
        vn = one.xml("one.vn.info", vid, False)
        ids = sorted({int(l.findtext("VM")) for l in vn.findall("AR_POOL/AR/LEASES/LEASE")
                      if (l.findtext("VM") or "").lstrip("-").isdigit() and int(l.findtext("VM")) >= 0})
        out = {}
        for vm_id in ids:
            vm = one.xml("one.vm.info", vm_id, False)
            nics = [int(n.findtext("NIC_ID")) for n in vm.findall("TEMPLATE/NIC") if n.findtext("NETWORK_ID") == str(vid)]
            if nics:
                out[vm_id] = nics
        return out

    def _detach_all(self, vid, name, one=None):
        one = one or self.one
        users = self._users_of(vid, one)
        out = []
        for vm_id, nics in users.items():
            for nic in nics:
                one.call("one.vm.detachnic", vm_id, nic)
                self._wait(vm_id, one=one)
            out.append(f"#{vm_id} ({len(nics)})")
        return f"{name}: NICs detached from " + (", ".join(out) if out else "no VM")

    def _delete_vnet(self, vid, name, one=None):
        (one or self.one).call("one.vn.delete", vid)
        return f"{name} #{vid} deleted"

    def _terminate(self, name):
        vr_id, vms = self._vrouter(name)
        if vr_id is not None:  # a native igw: the virtual router goes with its VMs
            self.one.call("one.vrouter.delete", vr_id)
            for v in vms:
                self._wait(v, gone=True)
            return f"virtual router {name} #{vr_id} removed"
        vm_id = self._vm_exists(name)
        if vm_id is None:
            return f"{name}: not found"
        self.one.call("one.vm.action", "terminate-hard", vm_id)
        self._wait(vm_id, gone=True)
        return f"{name} #{vm_id} removed"

    def _release(self, cidr):
        from .models import PrefixAssignment
        n = PrefixAssignment.active().filter(tenant=self.tenant, cidr=cidr).update(released_at=timezone.now())
        return f"{cidr} released" if n else f"{cidr} was not assigned"

    def _router(self, role, without=None, ifaces_key=None, vm_id=None):
        """Change a router's configuration (before: with `without`; after: without it — or the other way round)."""
        from inventory import nfconfig
        from . import routercfg
        add = self.req.action == self.req.Action.ADD

        def build(ifaces, phase):
            res = {}
            if ifaces_key and vm_id is not None:
                res[ifaces_key] = routercfg.nic_ifaces(self.one, vm_id, ifaces)
            skip = (phase == "before") == add  # adding: "before" lacks it; deleting: "after" lacks it
            if skip and without:
                res["without"] = [without]
            return nfconfig.build(role, tenant=self.tenant, reveal=True, res=res)
        return routercfg.apply(self.tenant, role, build)

    # --- public networks ---------------------------------------------------------------------------

    def _gw(self, net):
        return str(net[0]) if net.prefixlen >= 31 else str(next(net.hosts()))

    def pub_add(self):
        net = ipaddress.ip_network(self.req.result)
        gw = self._gw(net)
        kind = self.igw_type()
        igw_id = self._igw_vm(self.gw("igw")) if kind != "custom" else None
        if igw_id is None and kind != "custom":
            raise DeployError("the project has no igw")
        st = {}

        def network():
            name = self.net_name("pub", net)
            vid, note = self._vnet(name, net, gw)
            st.update(name=name, vid=vid)
            self._record("public_networks", [x for x in self._rows("public_networks") if x.get("prefix") != str(net)]
                         + [{"name": name, "id": vid, "prefix": str(net), "igw": gw}])
            return f"{name} {net} {note}"

        def nic():
            if kind == "onevr":  # a virtual router's VM gets its NICs through the router
                vr_id, _vms = self._vrouter(self.gw("igw"))
                info = self.one.xml("one.vrouter.info", vr_id, False)
                if any(n.findtext("NETWORK_ID") == str(st["vid"]) for n in info.findall("TEMPLATE/NIC")):
                    return f"igw router #{vr_id}: {gw} attached"
                self.one.call("one.vrouter.attachnic", vr_id, template({"NIC": {"NETWORK_ID": st["vid"], "IP": gw,
                                                                                  "FLOATING_IP": "NO"}}))
                self._wait(igw_id)
                return f"igw router #{vr_id}: {gw} attached now"
            note = self._attach(igw_id, st["vid"], gw)
            if note == "attached now":
                self._wait(igw_id)
            return f"igw #{igw_id}: {gw} {note}"
        self.step("network", network)
        if kind == "custom":
            self.step("igw-nic", lambda: f"custom igw: attach {st.get('name')} with {gw} to your igw VM")
        else:
            self.step("igw-nic", nic)
        if kind == "vyos":
            self.step("igw", lambda: self._router("igw", str(net), "igw_ifaces", igw_id))
        self.step("egw", lambda: self._router("egw", str(net)))

    def pub_delete(self):
        cidr = self.req.target
        row = next((x for x in self._rows("public_networks") if x.get("prefix") == cidr), None)
        name = row["name"] if row else self.net_name("pub", ipaddress.ip_network(cidr))
        igw_id = self._igw_vm(self.gw("igw"))
        self.step("egw", lambda: self._router("egw", cidr))
        if self.igw_type() == "vyos" and igw_id is not None:
            self.step("igw", lambda: self._router("igw", cidr, "igw_ifaces", igw_id))
        vid = self._vnet_id(name)
        if vid is not None:
            self.step("nics", lambda: self._detach_all(vid, name))
            self.step("network", lambda: self._delete_vnet(vid, name))
        self.step("release", lambda: self._release(cidr))
        self._record("public_networks", [x for x in self._rows("public_networks") if x.get("prefix") != cidr])

    def main_replace(self):
        from .models import PrefixAssignment
        old = self.req.target
        new = PrefixAssignment.active().filter(tenant=self.tenant, kind="inet_next", cidr=self.req.result).first()
        if new is None:
            raise DeployError(f"the new main prefix {self.req.result} is not reserved")
        main_name = f"{self.base}-main_{self.site}"
        # correction 15: the igw stays (it is the tenant's, with its configuration); only the egw is made again
        self.step("egw-remove", lambda: self._terminate(self.gw("egw")))
        vid, main_name = self._main_vnet()
        if vid is not None:
            self.step("nics", lambda: self._detach_all(vid, main_name))
            self.step("network-remove", lambda: self._delete_vnet(vid, main_name))

        def swap():
            now = timezone.now()
            PrefixAssignment.active().filter(tenant=self.tenant, kind="inet_main", cidr=old).update(released_at=now)
            new.kind = PrefixAssignment.Kind.INET_MAIN
            new.save(update_fields=["kind"])
            return f"main {old} released; {new.cidr} is the main prefix"
        self.step("swap", swap)

        def redeploy():
            from . import deploy as dp
            dep = dp.deploy(self.tenant, self.user)
            if dep.state != dep.State.DONE:
                raise DeployError(f"deployment #{dep.pk}: {dep.error or dep.state}")
            made = [s["detail"] for s in dep.steps if s["name"] in ("network", "egw", "igw")]
            return f"deployment #{dep.pk}: " + "; ".join(made)
        self.step("deploy", redeploy)

        def igw_attach():
            vid2, _name = self._main_vnet()
            igw_id = self._vm_exists(self.gw("igw"))
            net = ipaddress.ip_network(new.cidr)
            hosts = list(net) if net.prefixlen >= 31 else list(net.hosts())
            if igw_id is None:
                return "no igw VM: nothing to connect"
            return f"igw #{igw_id} on {new.cidr}: {hosts[1]} " + self._attach(igw_id, vid2, str(hosts[1]))
        self.step("igw-attach", igw_attach)

    def bandwidth(self):
        """A new connectivity version with the bandwidth, then the egw's shaper."""
        from .models import Connectivity

        def record():
            cur = next((c for c in Connectivity.current_for(self.tenant) if c.site == self.site), None)
            vals = cur.values() if cur else {}
            old = vals.get("inet_mbps")
            vals["inet_mbps"] = self.req.mbps
            Connectivity.objects.create(tenant=self.tenant, site=self.site, version=Connectivity.next_version(self.tenant, self.site),
                                        created_by=self.user, **vals)
            return f"{old or '?'} → {self.req.mbps} Mbps"
        self.step("record", record)
        if self._vm_exists(self.gw("egw")) is not None:
            self.step("egw", lambda: self._router("egw"))  # correction 17: the shaper; the old limiter goes

        def group():  # the group's template copy (QUOTA_<SITE> INET_MBPS) follows the record
            from . import deploy as dp
            return dp.apply_quota(self.tenant, self.user)
        self.step("group", group)

    def igw_replace(self):
        """Another igw type: the igw VM goes (its configuration is not kept), the record changes, the deployment makes
        the new one (advanced and native; a custom igw is the tenant's own VM)."""
        from .models import Connectivity
        new = self.req.result
        self.step("igw-remove", lambda: self._terminate(self.gw("igw")))

        def record():
            cur = next((c for c in Connectivity.current_for(self.tenant) if c.site == self.site), None)
            vals = cur.values() if cur else {}
            vals["igw_type"] = new
            Connectivity.objects.create(tenant=self.tenant, site=self.site, version=Connectivity.next_version(self.tenant, self.site),
                                        created_by=self.user, **vals)
            return f"igw {self.req.target} → {new}"
        self.step("record", record)

        def redeploy():
            from . import deploy as dp
            dep = dp.deploy(self.tenant, self.user)
            if dep.state != dep.State.DONE:
                raise DeployError(f"deployment #{dep.pk}: {dep.error or dep.state}")
            return f"deployment #{dep.pk}: " + "; ".join(s["detail"] for s in dep.steps if s["name"] in ("igw", "public"))
        self.step("deploy", redeploy)

    # --- corporate networks ------------------------------------------------------------------------

    def corp_add(self):
        net = ipaddress.ip_network(self.req.result)
        cgw_id = self._vm_exists(self.gw("cgw"))
        if cgw_id is None:  # the first corporate network: network and cgw from a deployment run
            def deploy_run():
                from . import deploy as dp
                dep = dp.deploy(self.tenant, self.user)
                if dep.state != dep.State.DONE:
                    raise DeployError(f"deployment #{dep.pk}: {dep.error or dep.state}")
                return f"deployment #{dep.pk}: " + "; ".join(s["detail"] for s in dep.steps if s["name"] in ("corpnet", "cgw"))
            self.step("deploy", deploy_run)
            return
        hosts = list(net) if net.prefixlen >= 31 else list(net.hosts())
        gw = str(hosts[0])
        st = {}

        def network():
            name = self.net_name("corp", net)
            vid, note = self._vnet(name, net, gw)
            st.update(name=name, vid=vid)
            self._record("corp_networks", [x for x in self._rows("corp_networks") if x.get("prefix") != str(net)]
                         + [{"name": name, "id": vid, "prefix": str(net), "cgw": gw}])
            return f"{name} {net} {note}"

        def nic():
            note = self._attach(cgw_id, st["vid"], gw)
            if note == "attached now":
                self._wait(cgw_id)
            return f"cgw #{cgw_id}: {gw} {note}"
        self.step("network", network)
        self.step("cgw-nic", nic)
        self.step("cgw", lambda: self._router("cgw", str(net), "cgw_ifaces", cgw_id))

    def corp_delete(self):
        cidr = self.req.target
        row = next((x for x in self._rows("corp_networks") if x.get("prefix") == cidr), None)
        name = row["name"] if row else self.net_name("corp", ipaddress.ip_network(cidr))
        cgw_id = self._vm_exists(self.gw("cgw"))
        if cgw_id is not None:
            self.step("cgw", lambda: self._router("cgw", cidr, "cgw_ifaces", cgw_id))
        vid = self._vnet_id(name)
        if vid is not None:
            self.step("nics", lambda: self._detach_all(vid, name))
            self.step("network", lambda: self._delete_vnet(vid, name))
        self.step("release", lambda: self._release(cidr))
        self._record("corp_networks", [x for x in self._rows("corp_networks") if x.get("prefix") != cidr])

    # --- L2 channels ------------------------------------------------------------------------------

    def _platform_one(self, p):
        """(control, One) of a platform for the external side of L2 channels (execution mode required)."""
        from inventory.models import PlatformControl
        if p.pk == self.platform.pk:
            return self.control, self.one
        c = PlatformControl.objects.filter(key=p.key).first()
        if c is None or not c.configured:
            raise DeployError(f"{p.key}: endpoint or credentials not found")
        if c.mode != c.Mode.EXECUTION:
            raise DeployError(f"{p.key} is read-only: enable operations on it (Infrastructure → Sites → Platforms)")
        return c, One(c)

    def _edge(self):
        """(platform, control, One, dedicated edge cluster name or "") for the external side of L2 channels: the
        tenant's platform with its dedicated edge cluster (kind edge: egw and L2 bridges, on the tenant's EVPN fabric,
        so one bridge VM is enough), else the site's management platform (src there, dst on the tenant's cluster)."""
        from inventory.models import Platform
        # owner 2026-11-09: the edge cluster is chosen on the **site** now, not found by kind. It is used here
        # only when it is deployed and sits on this platform - a cluster on another platform of the site cannot
        # be reached through this platform's One, so the management fallback below is the right answer there.
        ec = self.platform.site.edge_cluster if self.platform.kind == "shared" else None
        if ec is not None and (ec.state != "active" or ec.platform_id != self.platform.pk):
            ec = None
        if ec is not None:
            return self.platform, self.control, self.one, ec.name
        p = Platform.objects.filter(site__code=self.site, kind="mgmt").first()
        if p is None:
            raise DeployError(f"{self.site} has no management platform for the external side of L2 channels")
        c, one = self._platform_one(p)
        return p, c, one, ""

    def _mgmt(self, key=None):
        """(platform, control, One) of the platform the external side runs on (the channel's, or the edge's now)."""
        from inventory.models import Platform
        if key:
            p = Platform.objects.filter(key=key).first()
            if p is None:
                raise DeployError(f"platform {key} not found")
            c, one = self._platform_one(p)
            return p, c, one
        p, c, one, _ec = self._edge()
        return p, c, one

    def _cluster_id(self, one, name):
        cl = one.find("one.clusterpool.info", "CLUSTER", name)
        if cl is None:
            raise DeployError(f"cluster {name} not found")
        return int(cl.findtext("ID"))

    def _in_cluster(self, one, cluster_id, vid, name):
        """Put a network into a cluster when it is not there yet (a VM there needs all its networks)."""
        vn = one.xml("one.vn.info", vid, False)
        if cluster_id in {int(x.text) for x in vn.findall("CLUSTERS/ID")}:
            return f"{name} in the cluster"
        one.call("one.cluster.addvnet", cluster_id, vid)
        return f"{name} added to the cluster"

    def _l2_names(self, ch):
        """(VLAN network, src gateway, dst gateway) names of a channel. The gateways (correction 11):
        _<org>_<tenant>_l2src<vlan>_<site> (management platform) and _<org>_<tenant>_l2dst<vlan>_<site>, host names
        l2src<vlan>-<org>-<tenant>-<site> …; the portal's names before (<group>-s2s<vlan>src_<site>) are looked up too. The
        VLAN network keeps its name."""
        from .naming import GROUP_RE, vyos_names
        src = vyos_names(self.tenant, f"l2src{ch.vlan:04d}", self.site)[0]
        dst = vyos_names(self.tenant, f"l2dst{ch.vlan:04d}", self.site)[0]
        if self.group.startswith("tenant-") and ch.vlan:
            vlan_net = f"s2s_{ch.vlan:04d}_{self.group[len('tenant-'):]}"
        else:
            m = GROUP_RE.match(self.group)
            vlan_net = f"s2s_{ch.vlan:04d}_{m.group(1)}-{m.group(2)}" if m else f"s2s_{ch.vlan:04d}_{self.group}"
        old = (f"{self.base}-s2s{ch.vlan}src_{self.site}", f"{self.base}-s2s{ch.vlan}dst_{self.site}")
        self.legacy[src], self.legacy[dst] = [old[0]], [old[1]]
        return vlan_net, src, dst

    def _l2br_name(self, ch):
        """The bridge gateway of an edge cluster (correction 11 names; the one it had before is looked up too)."""
        from .naming import vyos_names
        name = vyos_names(self.tenant, f"l2br{ch.vlan:04d}", self.site)[0]
        self.legacy[name] = [f"{self.base}-l2br{ch.vlan}_{self.site}"]
        return name

    def _l2_host(self, ch, side):
        from .naming import vyos_names
        return vyos_names(self.tenant, f"l2{side}{ch.vlan:04d}", self.site)[1]

    def l2_add(self, ch=None):
        ch = ch or self.req.channel
        if not ch.vlan or not ch.tenant_network:
            raise DeployError("the L2 channel needs its VLAN and its subnet first")
        mp, mc, mone, edge = self._edge()
        if mp.pk == self.platform.pk and edge:
            return self.l2_bridge(ch, edge)
        vlan_name, src_name, dst_name = self._l2_names(ch)
        s2s_name = f"cloud-s2s_{self.site}"
        st = {}

        def lookups():
            ms2s = mone.find("one.vnpool.info", "VNET", s2s_name, -2, -1, -1)
            cs2s = self.one.find("one.vnpool.info", "VNET", s2s_name, -2, -1, -1)
            if ms2s is None or cs2s is None:
                raise DeployError(f"{s2s_name} not found on {mp.key if ms2s is None else self.platform.key}")
            st.update(ms2s=int(ms2s.findtext("ID")), cs2s=int(cs2s.findtext("ID")), phydev=ms2s.findtext("PHYDEV") or "bond0",
                      mcluster=(self._cluster_id(mone, edge) if edge
                                else int((ms2s.findall("CLUSTERS/ID") or [None])[0].text or 0)))
            if edge:  # the gateway there needs its networks in the edge cluster
                mgmt = mone.find("one.vnpool.info", "VNET", f"cloud-vyos_{self.site}", -2, -1, -1)
                for v, n in ((st["ms2s"], s2s_name), (int(mgmt.findtext("ID")) if mgmt is not None else None, f"cloud-vyos_{self.site}")):
                    if v is not None:
                        self._in_cluster(mone, st["mcluster"], v, n)
            img = mone.find("one.imagepool.info", "IMAGE", mc.nf_image, -2, -1, -1)
            if img is None:
                raise DeployError(f"image {mc.nf_image!r} not found on {mp.key}")
            st["mimage"] = int(img.findtext("ID"))
            tn = self.one.find("one.vnpool.info", "VNET", ch.tenant_network, -2, -1, -1)
            if tn is None:
                raise DeployError(f"tenant network {ch.tenant_network} not found")
            st["tnet"] = int(tn.findtext("ID"))
            return f"{mp.key}: {s2s_name} #{st['ms2s']}, image #{st['mimage']}; {ch.tenant_network} #{st['tnet']}"

        def vlan_net():
            vid = self._vnet_id(vlan_name, mone)
            note = "exists"
            if vid is None:
                tpl = {"NAME": vlan_name, "VN_MAD": "802.1Q", "PHYDEV": st["phydev"], "VLAN_ID": ch.vlan,
                       "BRIDGE": f"s2s-{ch.vlan:04d}", "METHOD": "skip", "IP6_METHOD": "disable", "MTU": 1500,
                       "GUEST_MTU": 1500, "AR": {"TYPE": "ETHER", "SIZE": 1}}
                vid = int(mone.call("one.vn.allocate", template(tpl), st["mcluster"]))
                note = "created"
            ch.vlan_net_id, ch.vlan_name, ch.mgmt_platform = vid, vlan_name, mp.key
            ch.mode, ch.edge_cluster = "tunnel", edge
            ch.save(update_fields=["vlan_net_id", "vlan_name", "mgmt_platform", "mode", "edge_cluster"])
            return f"{mp.key}: {vlan_name} #{vid} (VLAN {ch.vlan}) {note}" + (f", edge cluster {edge}" if edge else "")

        def ether():
            vn = self.one.xml("one.vn.info", st["tnet"], False)
            ars = [a for a in vn.findall("AR_POOL/AR") if a.findtext("TYPE") == "ETHER"]
            mine = next((a for a in ars if ch.ether_ar is not None and a.findtext("AR_ID") == str(ch.ether_ar)), None)
            if mine is None:
                self.one.call("one.vn.add_ar", st["tnet"], template({"AR": {"TYPE": "ETHER", "SIZE": 1}}))
                vn = self.one.xml("one.vn.info", st["tnet"], False)
                ars = [a for a in vn.findall("AR_POOL/AR") if a.findtext("TYPE") == "ETHER"]
                mine = max(ars, key=lambda a: int(a.findtext("AR_ID")))
            ch.ether_ar = int(mine.findtext("AR_ID"))
            ch.save(update_fields=["ether_ar"])
            st["mac"] = mine.findtext("MAC")
            return f"{ch.tenant_network}: ethernet range #{ch.ether_ar} {st['mac']}"

        def addresses():
            if not ch.ip_b:
                ch.ip_b = self._free_ip_on(mone, st["ms2s"], s2s_name, avoid=[ch.ip_a])
            if not ch.ip_a:
                ch.ip_a = self._free_ip_on(self.one, st["cs2s"], s2s_name, avoid=[ch.ip_b])
            ch.save(update_fields=["ip_a", "ip_b"])
            return f"dst {ch.ip_a} ({self.platform.key}) ↔ src {ch.ip_b} ({mp.key})"

        sa_name, sa = self._sa()
        msa = self._sa_of(mp)

        def src():
            vm_id = self._vm_exists_on(mone, src_name)
            if vm_id is None:
                mgmt_nic, _ip = self._mgmt_nic("l2-b", one=mone, platform=mp)
                nics = [mgmt_nic, {"NETWORK_ID": st["ms2s"], "IP": ch.ip_b, "NAME": "VMNIC1"},
                        {"NETWORK_ID": ch.vlan_net_id, "METHOD": "skip", "NAME": "VMNIC2"}]
                init, note = self._init("l2-b", channel=ch)
                crypted = crypt_sha512(msa[1]["password"]) if msa[1].get("password") else None
                tpl = self._vm_template(src_name, self._l2_host(ch, "src"), L2_CPU, L2_RAM_MB, nics, msa[0],
                                        msa[1]["public"], crypted, init, ssh_port=self._vyos_port(),
                                        cluster_id=st["mcluster"], image_id=st["mimage"], service=True)
                vm_id = int(mone.call("one.vm.allocate", template(tpl), False))
                ch.vm_b = vm_id
                ch.save(update_fields=["vm_b"])
                return f"{mp.key}: {src_name} #{vm_id} created: management {_ip}, {s2s_name} {ch.ip_b}, eth2 {vlan_name}{note}"
            ch.vm_b = vm_id
            ch.save(update_fields=["vm_b"])
            return f"{mp.key}: {src_name} #{vm_id} exists"

        def dst():
            vm_id = self._vm_exists(dst_name)
            if vm_id is None:
                mgmt_nic, _ip = self._mgmt_nic("l2-a")
                nics = [mgmt_nic, {"NETWORK_ID": st["cs2s"], "IP": ch.ip_a, "NAME": "VMNIC1"},
                        {"NETWORK_ID": st["tnet"], "MAC": st["mac"], "NAME": "VMNIC2"}]
                init, note = self._init("l2-a", channel=ch)
                crypted = crypt_sha512(sa["password"]) if sa.get("password") else None
                tpl = self._vm_template(dst_name, self._l2_host(ch, "dst"), L2_CPU, L2_RAM_MB, nics, sa_name,
                                        sa["public"], crypted, init, ssh_port=self._vyos_port(), service=True)
                vm_id = int(self.one.call("one.vm.allocate", template(tpl), False))
                ch.vm_a = vm_id
                ch.save(update_fields=["vm_a"])
                return f"{dst_name} #{vm_id} created: management {_ip}, {s2s_name} {ch.ip_a}, eth2 {ch.tenant_network}{note}"
            ch.vm_a = vm_id
            ch.save(update_fields=["vm_a"])
            return f"{dst_name} #{vm_id} exists"

        def tunnel():
            """New random keys for the tunnel whenever its gateways are made. A gateway left from an earlier run has
            keys nobody knows any more: it is removed and made again with the other one."""
            from inventory.nfconfig import tunnel_keys
            ch.tunnel_keys = tunnel_keys()  # in memory only: into the two gateways' first boot, kept nowhere
            src_vm, dst_vm = self._vm_exists_on(mone, src_name), self._vm_exists(dst_name)
            if src_vm is not None and dst_vm is not None:
                return "both gateways exist: their tunnel stays as it is"
            notes = []
            for vm_id, one, name in ((src_vm, mone, src_name), (dst_vm, self.one, dst_name)):
                if vm_id is not None:
                    one.call("one.vm.action", "terminate-hard", vm_id)
                    self._wait(vm_id, gone=True, one=one)
                    notes.append(f"{name} #{vm_id} removed (its tunnel key is not known)")
            return "; ".join(notes + ["new random tunnel keys and password for both gateways (not kept)"])

        self.step("l2-lookups", lookups)
        self.step("vlan", vlan_net)
        self.step("ether", ether)
        self.step("addresses", addresses)
        self.step("tunnel", tunnel)
        self.step("src", src)
        self.step("dst", dst)
        ch.state = ch.State.ACTIVE
        ch.save(update_fields=["state"])

    def l2_bridge(self, ch, edge):
        """The edge cluster is on the tenant's platform: one VM there bridges the external VLAN and the tenant
        network (both networks in the edge cluster)."""
        vlan_name, _src, _dst = self._l2_names(ch)
        br_name = self._l2br_name(ch)
        st = {}

        def lookups():
            st["cluster"] = self._cluster_id(self.one, edge)
            tn = self.one.find("one.vnpool.info", "VNET", ch.tenant_network, -2, -1, -1)
            if tn is None:
                raise DeployError(f"tenant network {ch.tenant_network} not found")
            st["tnet"] = int(tn.findtext("ID"))
            s2s = self.one.find("one.vnpool.info", "VNET", f"cloud-s2s_{self.site}", -2, -1, -1)
            st["phydev"] = (s2s.findtext("PHYDEV") if s2s is not None else "") or "bond0"
            return f"edge cluster {edge} #{st['cluster']}; {ch.tenant_network} #{st['tnet']}"

        def edge_nets():
            notes = [self._in_cluster(self.one, st["cluster"], st["tnet"], ch.tenant_network)]
            mgmt = self.one.find("one.vnpool.info", "VNET", f"cloud-vyos_{self.site}", -2, -1, -1)
            if mgmt is not None:
                notes.append(self._in_cluster(self.one, st["cluster"], int(mgmt.findtext("ID")), f"cloud-vyos_{self.site}"))
            return "; ".join(notes)

        def vlan_net():
            vid = self._vnet_id(vlan_name)
            note = "exists"
            if vid is None:
                tpl = {"NAME": vlan_name, "VN_MAD": "802.1Q", "PHYDEV": st["phydev"], "VLAN_ID": ch.vlan,
                       "BRIDGE": f"s2s-{ch.vlan:04d}", "METHOD": "skip", "IP6_METHOD": "disable", "MTU": 1500,
                       "GUEST_MTU": 1500, "AR": {"TYPE": "ETHER", "SIZE": 1}}
                vid = int(self.one.call("one.vn.allocate", template(tpl), st["cluster"]))
                note = "created"
            ch.vlan_net_id, ch.vlan_name, ch.mgmt_platform, ch.mode, ch.edge_cluster = vid, vlan_name, self.platform.key, "bridge", edge
            ch.save(update_fields=["vlan_net_id", "vlan_name", "mgmt_platform", "mode", "edge_cluster"])
            return f"{vlan_name} #{vid} (VLAN {ch.vlan}) {note} in {edge}"

        def ether():
            vn = self.one.xml("one.vn.info", st["tnet"], False)
            ars = [a for a in vn.findall("AR_POOL/AR") if a.findtext("TYPE") == "ETHER"]
            mine = next((a for a in ars if ch.ether_ar is not None and a.findtext("AR_ID") == str(ch.ether_ar)), None)
            if mine is None:
                self.one.call("one.vn.add_ar", st["tnet"], template({"AR": {"TYPE": "ETHER", "SIZE": 1}}))
                vn = self.one.xml("one.vn.info", st["tnet"], False)
                ars = [a for a in vn.findall("AR_POOL/AR") if a.findtext("TYPE") == "ETHER"]
                mine = max(ars, key=lambda a: int(a.findtext("AR_ID")))
            ch.ether_ar = int(mine.findtext("AR_ID"))
            ch.save(update_fields=["ether_ar"])
            st["mac"] = mine.findtext("MAC")
            return f"{ch.tenant_network}: ethernet range #{ch.ether_ar} {st['mac']}"

        sa_name, sa = self._sa()

        def bridge():
            vm_id = self._vm_exists(br_name)
            if vm_id is None:
                mgmt_nic, _ip = self._mgmt_nic("l2-br")
                nics = [mgmt_nic, {"NETWORK_ID": ch.vlan_net_id, "METHOD": "skip", "NAME": "VMNIC1"},
                        {"NETWORK_ID": st["tnet"], "MAC": st["mac"], "NAME": "VMNIC2"}]
                init, note = self._init("l2-br", channel=ch)
                crypted = crypt_sha512(sa["password"]) if sa.get("password") else None
                tpl = self._vm_template(br_name, self._l2_host(ch, "br"), L2_CPU, L2_RAM_MB, nics, sa_name,
                                        sa["public"], crypted, init, ssh_port=self._vyos_port(), cluster_id=st["cluster"], service=True)
                vm_id = int(self.one.call("one.vm.allocate", template(tpl), False))
                ch.vm_a = vm_id
                ch.save(update_fields=["vm_a"])
                return f"{br_name} #{vm_id} created in {edge}: management {_ip}, eth1 {vlan_name}, eth2 {ch.tenant_network}{note}"
            ch.vm_a = vm_id
            ch.save(update_fields=["vm_a"])
            return f"{br_name} #{vm_id} exists"

        self.step("l2-lookups", lookups)
        self.step("edge", edge_nets)
        self.step("vlan", vlan_net)
        self.step("ether", ether)
        self.step("bridge", bridge)
        ch.state = ch.State.ACTIVE
        ch.save(update_fields=["state"])

    def l2_delete(self):
        from .models import L2Channel
        ch = L2Channel.objects.filter(tenant=self.tenant, vni=int(self.req.target or 0)).first()
        if ch is None:
            raise DeployError(f"no L2 channel VNI {self.req.target}")
        vlan_name, src_name, dst_name = self._l2_names(ch) if ch.vlan else (ch.vlan_name, None, None)
        if ch.mode == "bridge":
            dst_name = self._l2br_name(ch) if ch.vlan else None

        def dst():
            vm_id = self._vm_exists(dst_name) if dst_name else ch.vm_a
            if vm_id is None:
                return "dst: not found"
            self.one.call("one.vm.action", "terminate-hard", vm_id)
            self._wait(vm_id, gone=True)
            return f"dst #{vm_id} removed"
        self.step("dst", dst)

        def ether():
            tn = self._vnet_id(ch.tenant_network)
            if tn is None or ch.ether_ar is None:
                return "no ethernet range"
            self.one.call("one.vn.rm_ar", tn, ch.ether_ar)
            return f"{ch.tenant_network}: ethernet range #{ch.ether_ar} removed"
        self.step("ether", ether)
        if ch.mode == "bridge":
            if ch.vlan_net_id:
                self.step("vlan", lambda: self._delete_vnet(ch.vlan_net_id, vlan_name, self.one))
        elif ch.mgmt_platform:
            _mp, _mc, mone = self._mgmt(ch.mgmt_platform)

            def src():
                vm_id = self._vm_exists_on(mone, src_name) if src_name else ch.vm_b
                if vm_id is None:
                    return "src: not found"
                mone.call("one.vm.action", "terminate-hard", vm_id)
                self._wait(vm_id, gone=True, one=mone)
                return f"src #{vm_id} removed"
            self.step("src", src)
            if ch.vlan_net_id:
                self.step("vlan", lambda: self._delete_vnet(ch.vlan_net_id, vlan_name, mone))
        ch.state = ch.State.REMOVED
        ch.save(update_fields=["state"])

    def l2_network(self):
        """Correction 13: the channel extends another tenant network. Its tenant-side gateway (dst, or the bridge) and
        the ethernet range on the old network are removed, then the channel is made again (l2_add / l2_bridge: what
        exists is kept; a tunnel gets new keys on both sides)."""
        from .models import L2Channel
        ch = L2Channel.objects.filter(tenant=self.tenant, vni=int(self.req.target or 0)).first()
        if ch is None:
            raise DeployError(f"no L2 channel VNI {self.req.target}")
        new = self.req.tenant_network
        if self._vnet_id(new) is None:
            raise DeployError(f"tenant network {new} not found")
        _vlan, _src, dst_name = self._l2_names(ch)
        if ch.mode == "bridge":
            dst_name = self._l2br_name(ch)

        def dst():
            vm_id = self._vm_exists(dst_name)
            if vm_id is None:
                return f"{dst_name}: not found"
            self.one.call("one.vm.action", "terminate-hard", vm_id)
            self._wait(vm_id, gone=True)
            return f"{dst_name} #{vm_id} removed"
        self.step("dst-old", dst)

        def ether():
            tn = self._vnet_id(ch.tenant_network)
            if tn is None or ch.ether_ar is None:
                return "no ethernet range"
            self.one.call("one.vn.rm_ar", tn, ch.ether_ar)
            return f"{ch.tenant_network}: ethernet range #{ch.ether_ar} removed"
        self.step("ether-old", ether)
        old = ch.tenant_network
        ch.tenant_network, ch.ether_ar, ch.vm_a = new, None, None
        ch.save(update_fields=["tenant_network", "ether_ar", "vm_a"])
        self.step("network", lambda: f"{old} ⇒ {new}")
        self.l2_add(ch)

    # --- helpers for other platforms ----------------------------------------------------------------

    def _vm_exists_on(self, one, name):
        for n in self._candidates(name):
            v = one.find("one.vmpool.info", "VM", n, -2, -1, -1, -1)
            if v is not None:
                return int(v.findtext("ID"))
        return None

    def _free_ip_on(self, one, vid, name, avoid=()):
        saved, self.one = self.one, one
        try:
            return self._free_ip(vid, name, avoid=[a for a in avoid if a])
        finally:
            self.one = saved

    def _sa_of(self, platform):
        from inventory.credentials import account
        name, acc = account(platform.sa) if platform.sa else (None, {})
        if not name:
            name, acc = self._sa()
        acc = with_public(acc or {})
        if not acc.get("public"):
            raise DeployError(f"{platform.key}: the super admin has no ssh key")
        return name, acc

    # --- run --------------------------------------------------------------------------------------

    def run_change(self):
        r, K, A = self.req, self.req.Kind, self.req.Action
        self.step("lookups", self.lookups)
        if r.kind == K.INET and r.action == A.ADD:
            self.pub_add()
        elif r.kind == K.INET and r.action == A.DELETE:
            self.pub_delete()
        elif r.kind == K.INET and r.action == A.REPLACE:
            self.main_replace()
        elif r.kind == K.INET and r.action == A.BANDWIDTH:
            self.bandwidth()
        elif r.kind == K.IGW:
            self.igw_replace()
        elif r.kind == K.CORPNET and r.action == A.ADD:
            self.corp_add()
        elif r.kind == K.CORPNET and r.action == A.DELETE:
            self.corp_delete()
        elif r.kind == K.L2 and r.action == A.ADD:
            self.l2_add()
        elif r.kind == K.L2 and r.action == A.DELETE:
            self.l2_delete()
        elif r.kind == K.L2 and r.action == A.NETWORK:
            self.l2_network()
        else:
            raise DeployError(f"unknown change {r.kind}/{r.action}")


def _execute(req_id, user_id):
    from accounts.models import User, audit
    from .models import NetworkRequest
    req = NetworkRequest.objects.select_related("tenant").get(pk=req_id)
    user = User.objects.filter(pk=user_id).first() if user_id else None
    req.run, req.error, req.steps = NetworkRequest.Run.RUNNING, "", []
    req.save(update_fields=["run", "error", "steps"])
    try:
        ch = NetChange(req, user)
        ch.run_change()
        req.run = NetworkRequest.Run.DONE
    except DeployError as exc:
        req.run, req.error = NetworkRequest.Run.FAILED, str(exc)[:2000]
    except Exception as exc:  # noqa: BLE001 - recorded
        req.run, req.error = NetworkRequest.Run.FAILED, f"{type(exc).__name__}: {exc}"[:2000]
    req.finished_at = timezone.now()
    req.save(update_fields=["run", "error", "finished_at", "steps"])
    audit(user, "network_change_" + req.run, req.tenant, kind=req.kind, change=req.action, what=req.what,
          error=req.error[:300])
    return req


def start(req, user=None):
    """Run the change now (DEPLOY_ASYNC=False) or in a background thread."""
    import threading
    from django.db import connection
    if not getattr(settings, "DEPLOY_ASYNC", True):
        return _execute(req.pk, getattr(user, "pk", None))

    def worker():
        try:
            _execute(req.pk, getattr(user, "pk", None))
        finally:
            connection.close()
    threading.Thread(target=worker, daemon=True).start()
    return req


def blocked_reason(tenant):
    from . import deploy as dp
    if not tenant.one_group_id:
        return "the project is not deployed yet"
    return dp.blocked_reason(tenant)


__all__ = ["NetChange", "start", "blocked_reason", "EGW_CPU", "EGW_RAM_MB"]
