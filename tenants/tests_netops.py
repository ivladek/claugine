"""Network changes of a deployed tenant: public, main, corporate networks and L2 channels (tenants.netops)."""

import os
from unittest import mock

from cryptography.fernet import Fernet
from django.test import TestCase as _TC
from django.test import override_settings
from django.urls import reverse

from inventory.testing import secrets_loaded
from tenants.fakeone import Routers, World
from tenants.tests import POOLS, InfraAndNetworks

CORP_YAML = (
    "interface: bond0\nvlan: 12\nsubnet: 10.0.12.0\nprefix: 24\n"
    "bgp:\n  local_asn: 4220101003\n  remote_asn: 4220101001\n"
    "  ibgp:\n    peer_groups:\n      cgw:\n        password: secrets.acme.cloud.network.corpnet-ix.dc1.cgw\n"
    "        hosts:\n          - 10.0.12.10/31\n      rrs:\n        password: secrets.acme.cloud.network.corpnet-ix.dc1.rrs\n"
    "        hosts:\n          - 10.0.12.240/30\n  rrs:\n    - 10.0.12.241\n    - 10.0.12.242\n"
)


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode(), NETOPS_POLL_SECONDS=0)
class NetworkChanges(InfraAndNetworks):
    test_secrets_by_type = None
    test_configurations_from_templates = None
    test_network_requests_and_their_deployment = None

    def setUp(self):
        super().setUp()
        import yaml

        from inventory.networks import import_site

        import_site("dc1", {"corpnet-ix": yaml.safe_load(CORP_YAML)})
        os.makedirs(os.path.join(self.secrets, "cloud", "network", "corpnet-ix"))
        with open(os.path.join(self.secrets, "cloud", "network", "corpnet-ix", "dc1.yaml"), "w") as f:
            f.write("cgw: Bgp-Cgw-Secret-5\nrrs: Bgp-Rrs-Secret-4\n")
        self.world = World(
            POOLS,
            vnets=[
                {"id": 0, "name": "cloud-ix_dc1", "ars": [{"TYPE": "IP4", "IP": "10.0.10.10", "SIZE": 20}]},
                {"id": 1, "name": "corpnet-ix_dc1", "ars": [{"TYPE": "IP4", "IP": "10.0.12.10", "SIZE": 20}]},
                {
                    "id": 2,
                    "name": "cloud-s2s_dc1",
                    "ars": [{"TYPE": "IP4", "IP": "10.0.13.10", "SIZE": 20}],
                    "phydev": "bond0",
                },
                {"id": 3, "name": "cloud-vyos_dc1", "ars": [{"TYPE": "IP4", "IP": "10.0.20.10", "SIZE": 20}]},
            ],
        )
        self.routers = Routers(self.world)

    def patches(self):
        from contextlib import ExitStack

        st = ExitStack()
        worlds = getattr(self, "worlds", {})
        st.enter_context(secrets_loaded(self.secrets, DEPLOY_ASYNC=False))
        st.enter_context(
            mock.patch("adoption.collector.client_for", side_effect=lambda c: worlds.get(c.key, self.world))
        )
        st.enter_context(mock.patch("tenants.deploy.crypt_sha512", return_value="$6$x$y"))
        st.enter_context(mock.patch("inventory.sshexec.run", side_effect=self.routers.run))
        return st

    def deploy(self):
        """Deploy the tenant and give its routers the configuration their first boot sets."""
        from inventory import nfconfig
        from tenants import deploy

        with self.patches():
            dep = deploy.deploy(self.t, None)
            self.assertEqual(dep.state, "done", dep.steps)
            self.t.refresh_from_db()
            res = dep.resources
            for role, ip in (
                ("egw", res.get("egw_mgmt_ip")),
                ("igw", res.get("igw_ip")),
                ("cgw", res.get("cgw_mgmt_ip")),
            ):
                if ip:
                    self.routers.config[ip] = [
                        x for x in nfconfig.build(role, tenant=self.t, reveal=True).splitlines() if x.startswith("set ")
                    ]
        return dep

    def ask(self, **data):
        from tenants.models import NetworkRequest

        self.client.force_login(self.adm)
        self.client.post(reverse("tenant_network_request", args=[self.t.pk]), data)
        return NetworkRequest.objects.filter(tenant=self.t).order_by("-pk").first()

    def test_gateways_card_check_and_who_redeploys(self):
        """Correction 11: everybody who sees the tenant sees its gateways; the super admin and support engineers check
        and redeploy them."""
        from accounts.models import Kind, User
        from inventory.models import PlatformControl

        self.deploy()
        egw = self.world.vm("_romashka_gpulab_egw_dc1")
        self.world.vms[egw]["gname"] = (
            "oneadmin"  # the provider's VM: the platform read (Check reads it again) lists it
        )
        nics = [[self.world.vnets[n["vid"]]["name"], n["ip"]] for n in self.world.vms[egw]["nics"]]
        PlatformControl.objects.filter(key="dc1.cloud").update(
            vms=[{"name": "_romashka_gpulab_egw_dc1", "id": egw, "status": "running", "nics": nics, "host": "hkvm01"}]
        )
        self.client.force_login(self.adm)
        h = self.client.get(reverse("tenant_detail", args=[self.t.pk])).content.decode()
        self.assertIn('id="gateways"', h)
        # correction 12: a tenant user sees only whether each gateway is ok — no name, address or details
        self.assertNotIn("_romashka_gpulab_egw_dc1", h)
        self.assertNotIn('data-open="dlg-gw-egw"', h)
        self.assertIn("igw", h)
        self.assertNotIn(reverse("tenant_gw_redeploy", args=[self.t.pk, "egw"]), h)  # sees, does not redeploy
        self.assertEqual(self.client.post(reverse("tenant_gw_redeploy", args=[self.t.pk, "egw"])).status_code, 403)
        self.assertEqual(self.client.post(reverse("tenant_gw_check", args=[self.t.pk])).status_code, 403)
        se = User.objects.create_user(
            "se@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_support=True, email_verified=True
        )
        self.client.force_login(se)
        self.assertIn(
            reverse("tenant_gw_redeploy", args=[self.t.pk, "egw"]),
            self.client.get(reverse("tenant_detail", args=[self.t.pk])).content.decode(),
        )
        with self.patches():
            self.client.post(reverse("tenant_gw_check", args=[self.t.pk]))
        self.t.refresh_from_db()
        self.assertTrue(self.t.gw_status["egw"]["ok"], self.t.gw_status)
        st = self.t.gw_status["egw"]  # checked as a route reflector (2026-10-28)
        self.assertIn("BGP 2/2", st["detail"])
        self.assertEqual(
            {c["check"]: c["ok"] for c in st["checks"]},
            {
                "answers ssh on the management address (VRF MGMT)": True,
                "BGP (ipv4) peers established": True,
                "the platform's addresses on the VM": True,
            },
        )
        h = self.client.get(
            reverse("tenant_detail", args=[self.t.pk])
        ).content.decode()  # the rows of the route reflectors
        for text in (
            "Management IP",
            'data-open="dlg-gw-egw"',
            "compliant",
            "checks ok",
            "the platform&#x27;s addresses on the VM",
            "Redeploy…",
            'name="key" value="egw"',
            'data-open="dlg-gw-igw"',
            "_romashka_gpulab_egw_dc1",
        ):
            self.assertIn(text, h)
        self.assertNotIn("Check logs in on the management address", h)  # no text under the header, no Check at the top
        with self.patches():  # one gateway checked from its row
            self.client.post(reverse("tenant_gw_check", args=[self.t.pk]), {"key": "egw"})
        self.client.force_login(self.adm)
        h = self.client.get(reverse("tenant_detail", args=[self.t.pk])).content.decode()
        self.assertIn("ok", h)

    def test_egw_redeploy_finds_a_main_network_of_another_name(self):
        """2026-10-28: delta's main network is "tenant_delta-main" (renamed after its egw was made): the redeploy
        takes it as the tenant's own by its group and prefix, not as a network that already uses the prefix."""
        from tenants.deploy import Deployer

        self.deploy()
        main = self.world.vnet("_t_romashka_gpulab-main_dc1")
        self.world.vnets[main].update(name="romashka-gpulab-main", gname=self.t.group_name)
        with self.patches():
            d = Deployer(self.t, None)
            d.lookups()  # refused the prefix before: "already used in OpenNebula by network romashka-gpulab-main"
            note = d.network_step()
        self.assertEqual((d.vnet_id, d.vnet_name), (main, "romashka-gpulab-main"), note)
        self.assertEqual(len([v for v in self.world.vnets.values() if "-main" in v["name"]]), 1)  # none made

    def test_igw_page_type_and_networks(self):
        """Correction 12: the igw's Edit — its type, and (VyOS) the tenant's subnets connected to it by checkboxes."""
        self.deploy()
        igw = self.world.vm("_romashka_gpulab_igw_dc1")
        for _vid, v in self.world.vnets.items():
            if v["name"].startswith("_t_romashka_gpulab"):
                v["gname"] = self.t.group_name
        self.world.add_vnet(
            777, "_t_romashka_gpulab-lan9_dc1", [{"TYPE": "IP4", "IP": "10.9.9.1", "SIZE": 50}], gname=self.t.group_name
        )
        self.client.force_login(self.adm)
        with self.patches():
            page = self.client.get(
                reverse("tenant_detail", args=[self.t.pk]) + "?igw=configs"
            ).content.decode()  # correction 14
            self.assertIn("Replace the igw", page)
            self.assertIn("_t_romashka_gpulab-lan9_dc1", page)
            before = [n["vid"] for n in self.world.vms[igw]["nics"]]
            self.assertNotIn(777, before)
            self.client.post(
                reverse("tenant_igw", args=[self.t.pk]),
                {"action": "networks", "net": [str(v) for v in before[1:]] + ["777"]},
            )
        self.assertIn(777, [n["vid"] for n in self.world.vms[igw]["nics"]])
        self.assertEqual([n["vid"] for n in self.world.vms[igw]["nics"]][0], before[0])  # the main network stays
        with self.patches():
            self.client.post(
                reverse("tenant_igw", args=[self.t.pk]), {"action": "networks", "net": [str(v) for v in before[1:]]}
            )
        self.assertNotIn(777, [n["vid"] for n in self.world.vms[igw]["nics"]])

    def test_internet_and_corporate_network_never_together(self):
        """Correction 11: a tenant-<name> group (no order) with the internet cannot add the corporate network; L2 can."""
        from tenants.models import NetworkRequest

        org = self.t.organization
        org.kind = "corp"
        org.save()
        self.deploy()
        self.t.order = {
            k: v for k, v in self.t.order.items() if k in ("site", "platform")
        }  # as adopted: no connections
        self.t.kind = "corp"
        self.t.save()
        before = NetworkRequest.objects.filter(tenant=self.t).count()
        self.ask(kind="corpnet", prefix_len=29)
        self.assertEqual(NetworkRequest.objects.filter(tenant=self.t).count(), before)  # refused: it has the internet
        h = self.client.get(reverse("tenant_detail", args=[self.t.pk])).content.decode()
        self.assertNotIn('data-open="dlg-net-corpnet"', h)
        self.assertIn('data-open="dlg-l2-add"', h)

    def approve(self, req, **data):
        self.client.force_login(self.sa)
        with self.patches():
            self.client.post(reverse("tenant_network_decide", args=[self.t.pk, req.pk, "approve"]), data)
        req.refresh_from_db()
        return req

    def cfg(self, role):
        res = self.t.deployments.exclude(state="plan").first().resources
        ip = {"egw": res.get("egw_mgmt_ip"), "igw": res.get("igw_ip"), "cgw": res.get("cgw_mgmt_ip")}[role]
        return "\n".join(self.routers.config.get(ip, []))

    def test_public_network_added_and_deleted(self):
        from tenants.models import PrefixAssignment

        self.deploy()
        igw = self.world.vm("_romashka_gpulab_igw_dc1")
        req = self.ask(kind="inet", prefix_len=29)
        self.assertEqual((req.action, req.state), ("add", "pending"))
        req = self.approve(req)
        self.assertEqual((req.state, req.run), ("approved", "done"), (req.error, req.steps))
        net = req.result
        gw = net.rsplit(".", 1)[0] + "." + str(int(net.split("/")[0].rsplit(".", 1)[1]) + 1)
        vid = self.world.vnet("_t_romashka_gpulab-pub1_dc1")
        self.assertIsNotNone(vid)
        self.assertIn(vid, [n["vid"] for n in self.world.vms[igw]["nics"]])
        igw_cfg, egw_cfg = self.cfg("igw"), self.cfg("egw")
        self.assertIn(f"set interfaces ethernet eth1 address {gw}/29", igw_cfg)
        self.assertIn(f"set firewall group network-group PUB network {net}", igw_cfg)
        self.assertIn("set firewall zone PUB member interface eth1", igw_cfg)
        self.assertIn(f"set protocols static route {net} next-hop 203.0.113.17", egw_cfg)
        self.assertIn(f"set firewall group network-group CLIENT network {net}", egw_cfg)
        self.assertEqual([s["status"] for s in req.steps], ["ok"] * len(req.steps))
        # a VM of the tenant on the network: its NIC goes with the network
        self.world.vms[777] = {"name": "web", "state": "3", "nics": []}
        self.world._nic(777, vid, gw.rsplit(".", 1)[0] + "." + str(int(gw.rsplit(".", 1)[1]) + 1))
        self.client.force_login(self.adm)
        r = self.client.get(reverse("tenant_detail", args=[self.t.pk]))
        self.assertContains(r, 'data-open="dlg-subdel-dc1"')  # owner 2026-10-29 20:20: − Subnet, a row action
        req = self.ask(kind="inet", action="delete", target=net, site="nowhere")  # a site the tenant has not: its own
        self.assertEqual((req.action, req.target, req.site), ("delete", net, "dc1"))
        req = self.approve(req)
        self.assertEqual(req.run, "done", (req.error, req.steps))
        self.assertIsNone(self.world.vnet("_t_romashka_gpulab-pub1_dc1"))
        self.assertEqual(self.world.vms[777]["nics"], [])
        self.assertNotIn(vid, [n["vid"] for n in self.world.vms[igw]["nics"]])
        igw_cfg, egw_cfg = self.cfg("igw"), self.cfg("egw")
        self.assertNotIn(net, igw_cfg + egw_cfg)
        self.assertNotIn("zone PUB", igw_cfg)  # the last public network: the whole zone goes
        self.assertIn("delete firewall zone PUB", "".join(s for _h, s in self.routers.scripts))
        self.assertIn("set firewall zone EXT member interface eth0", igw_cfg)  # the rest stays
        self.assertFalse(PrefixAssignment.active().filter(cidr=net).exists())

    def test_main_network_replaced(self):
        from tenants.models import PrefixAssignment

        self.deploy()
        old_egw, old_igw = self.world.vm("_romashka_gpulab_egw_dc1"), self.world.vm("_romashka_gpulab_igw_dc1")
        old_main = self.world.vnet("_t_romashka_gpulab-main_dc1")
        self.world.vms[778] = {"name": "db", "state": "3", "nics": []}
        self.world._nic(778, old_main)  # a VM on the main network
        req = self.ask(kind="inet", action="replace", target="203.0.113.16/31", prefix_len=30)
        req = self.approve(req)
        self.assertEqual(req.run, "done", (req.error, req.steps))
        new = PrefixAssignment.active().get(tenant=self.t, kind="inet_main")
        self.assertEqual((new.cidr, req.result), (req.result, new.cidr))
        self.assertTrue(new.cidr.endswith("/30"))
        self.assertFalse(PrefixAssignment.active().filter(cidr="203.0.113.16/31").exists())
        # correction 15: the egw is made again; the igw is kept and connected to the new network after the egw
        self.assertEqual((self.world.vms[old_egw]["state"], self.world.vms[old_igw]["state"]), ("6", "3"))
        self.assertNotIn(old_main, self.world.vnets)
        self.assertEqual(self.world.vms[778]["nics"], [])  # other VMs stay disconnected
        egw, igw = self.world.vm("_romashka_gpulab_egw_dc1"), self.world.vm("_romashka_gpulab_igw_dc1")
        self.assertNotIn(egw, (None, old_egw))
        self.assertEqual(igw, old_igw)
        main = self.world.vnet("_t_romashka_gpulab-main_dc1")
        import ipaddress

        hosts = list(ipaddress.ip_network(new.cidr).hosts())
        self.assertIn((main, str(hosts[1])), [(n["vid"], n.get("ip")) for n in self.world.vms[igw]["nics"]])
        self.assertIn('AUTOSTART = "always"', self.world.vms[egw]["template"])
        self.assertIn("igw-attach", [st["name"] for st in req.steps])

    def test_corporate_network_added_to_the_cgw_and_deleted(self):
        from tenants import ordering
        from tenants.models import PrefixAssignment

        org = self.t.organization
        org.kind = "corp"
        org.save()
        # a corporate tenant (the internet and the corporate network are not combined in one tenant)
        self.t.order = {**self.t.order, "connections": ["corpnet"], "corpnet_prefixes": [28]}
        self.t.kind = "corp"
        self.t.save()
        # correction 11: the tenant is corporate only — no internet prefix, connectivity corpnet
        from django.utils import timezone

        from tenants.models import Connectivity

        PrefixAssignment.active().filter(tenant=self.t, kind__startswith="inet").update(released_at=timezone.now())
        Connectivity.objects.filter(tenant=self.t).update(connection_type="corpnet")
        ordering.allocate_one(self.t, "corpnet", 28)
        self.deploy()
        cgw = self.world.vm("_romashka_gpulab_cgw_dc1")
        self.assertIsNotNone(cgw)
        tpl = self.world.vms[cgw]["template"]
        self.assertIn('SSH_PUBLIC_KEY = "ssh-ed25519 AAAAsa opsuser"', tpl)
        self.assertIn('AUTOSTART = "always"', tpl)  # a service VM (correction 12)
        req = self.approve(self.ask(kind="corpnet", prefix_len=29))
        self.assertEqual(req.run, "done", (req.error, req.steps))
        cfg = self.cfg("cgw")
        net = req.result
        gw = net.rsplit(".", 1)[0] + "." + str(int(net.split("/")[0].rsplit(".", 1)[1]) + 1)
        self.assertIn(f"set interfaces ethernet eth3 address {gw}/29", cfg)  # eth0 management, eth1 exchange
        self.assertIn("set firewall zone CLIENT member interface eth3", cfg)
        self.assertIn(f"set firewall group network-group CLIENT network {net}", cfg)
        self.assertIn("set firewall zone CLIENT member interface eth2", cfg)
        self.assertIsNotNone(self.world.vnet("_t_romashka_gpulab-corp2_dc1"))
        first = PrefixAssignment.active().filter(tenant=self.t, kind="corpnet").exclude(cidr=net).get().cidr
        req = self.approve(self.ask(kind="corpnet", action="delete", target=first))
        self.assertEqual(req.run, "done", (req.error, req.steps))
        self.assertIsNone(self.world.vnet("_t_romashka_gpulab-corp_dc1"))
        cfg = self.cfg("cgw")
        self.assertNotIn(first, cfg)
        self.assertNotIn("member interface eth2", cfg)
        self.assertIn(f"set firewall group network-group CLIENT network {net}", cfg)
        # the last corporate network stays
        r = self.ask(kind="corpnet", action="delete", target=net)
        self.assertNotEqual((r.action, r.target), ("delete", net))

    def test_l2_channel_on_the_management_platform(self):
        from inventory.models import Platform, PlatformControl, Site
        from tenants.models import L2Channel, NetworkRequest

        site = Site.objects.get(code="dc1")
        from inventory.networks import import_site

        import_site(
            "dc1", {"cloud-s2s": {"interface": "bond0", "vlan": 112, "subnet": "10.0.12.0", "prefix": 23, "mtu": 9000}}
        )
        Platform.objects.create(
            key="dc1.mgmt",
            site=site,
            name="mgmt",
            kind="mgmt",
            fe_vip="10.0.0.40",
            fe_secrets="secrets.acme.cloud.one",
            segments=["cloud-s2s"],
        )
        ctl = PlatformControl.objects.create(key="dc1.mgmt", mode="adoption")
        mgmt = World(
            POOLS,
            vnets=[
                {
                    "id": 9,
                    "name": "cloud-s2s_dc1",
                    "ars": [{"TYPE": "IP4", "IP": "10.0.12.10", "SIZE": 20}],
                    "phydev": "bond0",
                    "clusters": [0],
                },
                {"id": 8, "name": "cloud-vyos_dc1", "ars": [{"TYPE": "IP4", "IP": "10.0.20.100", "SIZE": 20}]},
            ],
        )
        self.worlds = {"dc1.mgmt": mgmt}
        self.deploy()
        with self.patches():
            from tenants.deploy import One, template

            One(PlatformControl.objects.get(key="dc1.cloud")).call(
                "one.vn.allocate",
                template(
                    {"NAME": "_t_romashka_gpulab-lan1_dc1", "AR": {"TYPE": "IP4", "IP": "192.168.1.1", "SIZE": 200}}
                ),
                5,
            )
        req = self.ask(
            kind="l2", tenant_network="_t_romashka_gpulab-lan1_dc1", tenant_network_text="_t_romashka_gpulab-lan1_dc1"
        )
        req = self.approve(req)  # correction 13: the resource manager approves without a VLAN
        self.assertEqual((req.state, req.run), ("approved", ""))
        ch = L2Channel.objects.get(tenant=self.t)
        self.assertEqual((ch.state, ch.vlan, ch.name), ("planned", None, "external"))
        self.client.force_login(self.sa)  # the support engineer (here the super admin) sets the VLAN: it is made
        with self.patches():
            self.client.post(reverse("tenant_l2_vlan", args=[self.t.pk, ch.pk]), {"vlan": 1721})
        req.refresh_from_db()
        self.assertEqual((req.state, req.run), ("approved", "failed"))  # the management platform is read-only
        self.assertIn("read-only", req.error)
        ctl.mode = "execution"
        ctl.save()
        self.client.force_login(self.sa)
        with self.patches():
            self.client.post(reverse("tenant_network_retry", args=[self.t.pk, req.pk]))
        req.refresh_from_db()
        self.assertEqual(req.run, "done", (req.error, req.steps))
        ch = L2Channel.objects.get(tenant=self.t)
        self.assertEqual((ch.vlan, ch.vni, ch.state, ch.mgmt_platform), (1721, 1721, "active", "dc1.mgmt"))
        vlan = mgmt.vnet("s2s_1721_romashka-gpulab")
        self.assertIsNotNone(vlan)
        self.assertEqual(mgmt.vnets[vlan]["ars"][0]["type"], "ETHER")
        src = mgmt.vm("_romashka_gpulab_l2src1721_dc1")
        dst = self.world.vm("_romashka_gpulab_l2dst1721_dc1")
        self.assertIsNotNone(src)
        self.assertIsNotNone(dst)
        self.assertEqual([n["vid"] for n in mgmt.vms[src]["nics"]], [8, 9, vlan])  # management, s2s, VLAN
        for side, w in ((src, mgmt), (dst, self.world)):  # L2 gateways are service VMs (correction 12)
            self.assertIn('AUTOSTART = "always"', w.vms[side]["template"])
        self.assertTrue(ch.ip_b.startswith("10.0.12.") and ch.ip_a.startswith("10.0.13."))
        lan = self.world.vnet("_t_romashka_gpulab-lan1_dc1")
        ether = [a for a in self.world.vnets[lan]["ars"] if a["type"] == "ETHER"]
        self.assertEqual(len(ether), 1)
        self.assertIn(ether[0]["mac"], [n["mac"] for n in self.world.vms[dst]["nics"]])
        self.assertIn('VLAN_ID = "1721"', [a[0] for m, a in mgmt.calls if m == "one.vn.allocate"][0])
        # the tunnel: VXLAN inside WireGuard, with random keys and a pre-shared key in the two first boots only
        import base64
        import re as _re

        def init_of(vm):
            b64 = _re.search(r'ONE_TIME_INIT_SCRIPT_BASE64 = "([^"]+)"', vm["template"]).group(1)
            return base64.b64decode(b64).decode()

        steps = {x["name"]: x["detail"] for x in req.steps}
        self.assertIn("with init script", steps.get("dst", ""), steps)
        a_cfg, b_cfg = init_of(self.world.vms[dst]), init_of(mgmt.vms[src])

        def wg(cfg, what):
            return _re.search(rf"wireguard wg1721 (?:peer \S+ )?{what} '([^']+)'", cfg).group(1)

        self.assertEqual(wg(a_cfg, "preshared-key"), wg(b_cfg, "preshared-key"))
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

        def pub(priv):
            return base64.b64encode(
                X25519PrivateKey.from_private_bytes(base64.b64decode(priv))
                .public_key()
                .public_bytes(  # noqa: E731
                    serialization.Encoding.Raw, serialization.PublicFormat.Raw
                )
            ).decode()

        self.assertEqual(
            pub(wg(a_cfg, "private-key")), wg(b_cfg, "public-key")
        )  # each side knows the other's public key
        self.assertEqual(pub(wg(b_cfg, "private-key")), wg(a_cfg, "public-key"))
        self.assertIn("vxlan vxlan1721 remote 169.254.0.2", a_cfg)
        self.assertIn(f"wireguard wg1721 peer src address {ch.ip_b}", a_cfg)
        psk = wg(a_cfg, "preshared-key")
        from django.db import connection

        with connection.cursor() as cur:  # the password is kept nowhere in the portal
            for table in connection.introspection.table_names():
                cur.execute(f'SELECT * FROM "{table}"')
                self.assertFalse(any(psk in str(row) for row in cur.fetchall()), table)
        # correction 13: another subnet, no approval: the tenant-side gateway is made again there (new tunnel keys)
        with self.patches():
            from tenants.deploy import One, template

            One(PlatformControl.objects.get(key="dc1.cloud")).call(
                "one.vn.allocate",
                template(
                    {"NAME": "_t_romashka_gpulab-lan2_dc1", "AR": {"TYPE": "IP4", "IP": "192.168.2.1", "SIZE": 200}}
                ),
                5,
            )
            self.client.force_login(self.adm)
            self.client.post(
                reverse("tenant_l2_network", args=[self.t.pk, ch.pk]), {"tenant_network": "_t_romashka_gpulab-lan2_dc1"}
            )
        mv = NetworkRequest.objects.filter(tenant=self.t, action="network").get()
        self.assertEqual((mv.state, mv.run), ("approved", "done"), (mv.error, mv.steps))
        ch.refresh_from_db()
        self.assertEqual((ch.tenant_network, ch.state), ("_t_romashka_gpulab-lan2_dc1", "active"))
        lan2 = self.world.vnet("_t_romashka_gpulab-lan2_dc1")
        self.assertEqual([a for a in self.world.vnets[lan]["ars"] if a["type"] == "ETHER"], [])  # the old range is gone
        ether2 = [a for a in self.world.vnets[lan2]["ars"] if a["type"] == "ETHER"]
        dst = self.world.vm("_romashka_gpulab_l2dst1721_dc1")
        self.assertIn(ether2[0]["mac"], [n["mac"] for n in self.world.vms[dst]["nics"]])
        self.assertIsNotNone(mgmt.vm("_romashka_gpulab_l2src1721_dc1"))
        # deleting the channel removes both sides
        req = self.approve(self.ask(kind="l2", action="delete", target=str(ch.vni)))
        self.assertEqual(req.run, "done", (req.error, req.steps))
        ch.refresh_from_db()
        self.assertEqual(ch.state, "removed")
        self.assertIsNone(mgmt.vm("_romashka_gpulab_l2src1721_dc1"))
        self.assertIsNone(self.world.vm("_romashka_gpulab_l2dst1721_dc1"))
        self.assertIsNone(mgmt.vnet("s2s_1721_romashka-gpulab"))
        self.assertEqual([a for a in self.world.vnets[lan2]["ars"] if a["type"] == "ETHER"], [])

    def test_router_change_plan(self):
        from tenants.routercfg import plan, script

        before = "set a b c 1\nset a b d 2\nset x y z 1\nset x y w 2\n"
        after = "set a b c 1\nset a b d 3\n"
        running = "set a b c 1\nset a b d 2\nset x y z 1\nset x y w 2\nset x y manual 9\nset system host-name r1\n"
        deletes, sets = plan(before, after, running)
        self.assertEqual(sets, ["set a b d 3"])
        self.assertIn("delete a b d", deletes)
        self.assertIn("delete x y z", deletes)  # the manual line under x y stays, so not the whole node
        self.assertNotIn("delete x y", deletes)
        self.assertTrue(
            script(deletes, sets).startswith("source /opt/vyatta/etc/functions/script-template\nconfigure\n")
        )


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode())
class JumpHostsAndKeys(InfraAndNetworks):
    test_secrets_by_type = None
    test_configurations_from_templates = None
    test_network_requests_and_their_deployment = None

    def test_no_jump_host_between_the_sites(self):
        """2026-10-27: every host is reached from either site directly; a jump host left in the data is not used."""
        from inventory import sshexec
        from inventory.models import Site
        from tenants.models import PortalSettings

        s = PortalSettings.get()
        s.portal_site = "dc3"
        s.save()
        Site.objects.filter(code="dc1").update(
            jump={"host": "10.0.0.21", "port": 55523, "account": "secrets.acme.cloud.admins.sa"}
        )
        self.assertIsNone(sshexec.jump_for("dc1"))
        cmd = sshexec.command("10.0.10.11", 55522, "opsuser", "/k", None, None)
        self.assertFalse(any(x.startswith("ProxyCommand=") for x in cmd))
        with mock.patch("inventory.sshexec.subprocess.run") as run, secrets_loaded(self.secrets):
            run.return_value = mock.Mock(returncode=0, stdout="ok\n", stderr="")
            self.assertEqual(sshexec.run("10.0.10.11", 55522, "opsuser", "PRIVATE", "true", site="dc1"), "ok\n")
            self.assertFalse(any(str(x).startswith("ProxyCommand=") for x in run.call_args.args[0]))

    def test_vms_get_the_public_key_only(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        k = Ed25519PrivateKey.generate()
        private = k.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()
        ).decode()
        public = (
            k.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
        )
        with open(os.path.join(self.secrets, "cloud", "admins.yaml"), "w") as f:
            f.write(
                "sa:\n  opsuser:\n    password: sapw\n    key: |\n"
                + "".join("      " + x + "\n" for x in private.splitlines())
            )
        dep, fake = self._run(dry=False)
        egw = next(a[0] for m, a in fake.calls if m == "one.vm.allocate" and "egw" in a[0])
        self.assertIn(f'SSH_PUBLIC_KEY = "{public}"', egw)
        self.assertNotIn("PRIVATE KEY", egw)

    def test_platform_mode_and_deployed_front_end(self):
        from inventory.models import Platform, PlatformControl

        self.client.force_login(self.sa)
        Platform.objects.filter(key="dc1.cloud").update(state="deployed", fe_mode="secondary", fe_primary="dc1.portal")
        self.client.post(reverse("platform_control_mode", args=["dc1.cloud", "adoption"]))
        self.assertEqual(PlatformControl.objects.get(key="dc1.cloud").mode, "adoption")
        self.client.post(reverse("platform_control_mode", args=["dc1.cloud", "execution"]))
        self.assertEqual(PlatformControl.objects.get(key="dc1.cloud").mode, "execution")
        Platform.objects.filter(key="dc1.portal").update(state="planned")
        self.client.post(reverse("platform_control_mode", args=["dc1.portal", "execution"]))
        self.assertNotEqual(
            PlatformControl.objects.filter(key="dc1.portal").values_list("mode", flat=True).first(), "execution"
        )
        r = self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=platforms")
        self.assertContains(r, "FE mode")
        self.assertContains(r, "Fixed: the platform is deployed.")
        from inventory.edit_views import PlatformForm

        p = Platform.objects.get(key="dc1.cloud")
        f = PlatformForm(
            {"site": "dc1", "name": "cloud", "kind": "shared", "state": "deployed", "fe_mode": "local"},
            locked=True,
            initial={"fe_mode": "secondary", "fe_primary": "dc1.portal"},
        )
        self.assertTrue(f.is_valid(), f.errors)
        self.assertEqual((f.cleaned_data["fe_mode"], f.cleaned_data["fe_primary"]), ("secondary", "dc1.portal"))
        self.assertTrue(p.fe_locked)


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode(), NETOPS_POLL_SECONDS=0)
class IgwTypesAndBandwidth(NetworkChanges):
    test_public_network_added_and_deleted = None
    test_main_network_replaced = None
    test_corporate_network_added_and_deleted = None
    test_l2_channel_added_and_deleted = None

    def test_igw_type_change_and_bandwidth(self):
        from tenants.deploy import igw_type_of
        from tenants.models import Connectivity

        self.deploy()
        old = self.world.vm("_romashka_gpulab_igw_dc1")
        self.assertIsNotNone(old)
        # bandwidth: approved by support, the egw's limit changes
        req = self.ask(kind="inet", action="bandwidth", mbps=300)
        self.assertEqual((req.action, req.target, req.mbps), ("bandwidth", "100 Mbps", 300))
        req = self.approve(req)
        self.assertEqual(req.run, "done", (req.error, req.steps))
        self.assertIn("set qos policy shaper LIMIT-OUT bandwidth 300mbit", self.cfg("egw"))
        self.assertEqual(Connectivity.current_for(self.t)[0].inet_mbps, 300)
        # igw: advanced -> native, at once (the tenant's own router)
        self.client.force_login(self.adm)
        with self.patches():
            self.client.post(
                reverse("tenant_network_request", args=[self.t.pk]),
                {"kind": "igw", "action": "replace", "igw_type": "onevr"},
            )
        req = self.t.network_requests.filter(kind="igw").first()
        self.assertEqual((req.state, req.run), ("approved", "done"), (req.error, req.steps))
        self.assertEqual(igw_type_of(self.t), "onevr")
        new = self.world.vm("_romashka_gpulab_igw_dc1")
        self.assertNotEqual(new, old)
        self.assertEqual(self.world.vms[old]["state"], "6")
        self.assertEqual(self.world.vms[new]["from_template"], 77)
        self.assertIn("203.0.113.17", [n["ip"] for n in self.world.vms[new]["nics"]])  # the address after the egw
        self.assertIn("ONEAPP_VNF_NAT4_ENABLED", self.world.vms[new]["template"])
        # virtual router templates are instantiated through a virtual router (one.template.instantiate refuses them)
        self.assertIn("vrouter", self.world.vms[new])
        self.assertNotIn("one.template.instantiate", [m for m, _a in self.world.calls])
        # native -> custom: nothing is created
        with self.patches():
            self.client.post(
                reverse("tenant_network_request", args=[self.t.pk]),
                {"kind": "igw", "action": "replace", "igw_type": "custom"},
            )
        self.assertIsNone(self.world.vm("_romashka_gpulab_igw_dc1"))
        self.assertEqual(self.world.vrouters, {})  # the router went with its VM
        self.assertEqual(igw_type_of(self.t), "custom")
        # owner 2026-11-09: and the platform must be told so. IGW_TYPE was hardcoded "vyos", so every project
        # claimed an advanced gateway - including this one, where the portal creates nothing at all.
        tpl = [a for m, a in self.world.calls if m == "one.group.update"][-1][1]
        self.assertIn('IGW_TYPE = "custom"', tpl)
        self.assertNotIn('IGW_TYPE = "vyos"', tpl)
        # a later run finds the egw and still knows its management address (the configuration check uses it)
        first = self.t.deployments.filter(state="done").last().resources["egw_mgmt_ip"]
        again = self.deploy()
        self.assertEqual(again.resources.get("egw_mgmt_ip"), first)


class EgwShaper(_TC):
    """Correction 17: the egw's bandwidth is a shaper on eth1 and eth2 egress; the old limiter is deleted."""

    def test_template_and_plan(self):
        from django.template import Context, Engine

        from inventory import nf_templates
        from tenants.routercfg import obsolete_for, plan

        body = Engine().from_string(nf_templates.EGW_QOS).render(Context({"mbps": 200}))
        after = "\n".join(x for x in body.splitlines() if x.strip())
        self.assertIn("set qos interface eth2 egress LIMIT-OUT", after)
        self.assertIn("set qos policy shaper LIMIT-OUT bandwidth 200mbit", after)
        self.assertIn("set qos policy shaper LIMIT-OUT default queue-type fq-codel", after)
        running = "\n".join(
            [
                "set interfaces ethernet eth1 address '192.0.2.18/24'",
                "set qos interface eth1 egress 'LIMIT-OUT'",
                "set qos interface eth1 ingress 'LIMIT-IN'",
                "set qos policy limiter LIMIT-IN default bandwidth '100mbit'",
                "set qos policy rate-control LIMIT-OUT bandwidth '100mbit'",
                "set qos policy shaper MANUAL bandwidth '50mbit'",
            ]
        )
        deletes, sets = plan(after, after, running, obsolete=obsolete_for("egw"))
        self.assertEqual(
            sorted(deletes),
            [
                "delete qos interface eth1 ingress",
                "delete qos policy limiter LIMIT-IN",
                "delete qos policy rate-control LIMIT-OUT",
            ],
        )
        self.assertNotIn("set qos interface eth1 egress LIMIT-OUT", sets)  # already there
        self.assertIn("set qos policy shaper LIMIT-OUT bandwidth 200mbit", sets)
        self.assertFalse(any("MANUAL" in d for d in deletes))  # manual additions stay

    def test_stored_template_has_the_shaper(self):
        from inventory.models import ConfigTemplate

        t = ConfigTemplate.objects.filter(role="egw").first()
        if t is not None:
            self.assertIn("set qos policy shaper LIMIT-OUT", t.body)
            self.assertNotIn("limiter LIMIT-IN", t.body)
