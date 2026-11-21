"""Network changes of a deployed tenant: public, main, corporate networks and L2 channels (tenants.netops)."""

import ipaddress
import os
from datetime import timedelta
from unittest import mock

from cryptography.fernet import Fernet
from django.test import TestCase as _TC
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

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
        # core-012: a name the project's admin chose on the platform (escaped here as OpenNebula's XML escapes it)
        evil = "x&lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;"
        self.world.add_vnet(778, evil, [{"TYPE": "IP4", "IP": "10.9.8.1", "SIZE": 5}], gname=self.t.group_name)
        from django.core.cache import cache

        cache.clear()  # the project's subnets are cached for 5 minutes
        self.client.force_login(self.adm)
        with self.patches():
            page = self.client.get(
                reverse("tenant_detail", args=[self.t.pk]) + "?igw=configs"
            ).content.decode()  # correction 14
            self.assertIn("Replace the igw", page)
            self.assertNotIn("<script>alert(1)", page)  # core-012: escaped in the L2 data block
            self.assertIn('id="l2-pools-data"', page)
            self.assertIn("x\\u003C/script\\u003E", page)  # it is there, as data
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
        """Owner 2026-11-18 13:16-13:21: an additional public prefix goes on the egw only - a static route to the address
        next to the egw's - with no OpenNebula network and nothing on the igw, which need not exist."""
        from tenants.models import PrefixAssignment

        self.deploy()
        igw = self.world.vm("_romashka_gpulab_igw_dc1")
        igw_nics = list(self.world.vms[igw]["nics"])
        igw_before = self.cfg("igw")
        del self.world.vms[igw]  # citix: the igw is not where the portal would look - it does not matter
        req = self.ask(kind="inet", prefix_len=29)
        self.assertEqual((req.action, req.state), ("add", "pending"))
        req = self.approve(req)
        self.assertEqual((req.state, req.run), ("approved", "done"), (req.error, req.steps))
        net = req.result
        self.assertIsNone(self.world.vnet("_t_romashka_gpulab-pub1_dc1"))  # no OpenNebula network
        egw_cfg = self.cfg("egw")
        self.assertIn(f"set protocols static route {net} next-hop 203.0.113.17", egw_cfg)  # the IP next to the egw's
        self.assertIn(f"set firewall group network-group CLIENT network {net}", egw_cfg)
        self.assertEqual(self.cfg("igw"), igw_before)  # nothing sent to the igw
        self.assertEqual([s["status"] for s in req.steps], ["ok"] * len(req.steps))
        self.assertIn("routed to the project's igw address", req.steps[-1]["detail"])
        self.world.vms[igw] = {"name": "_romashka_gpulab_igw_dc1", "state": "3", "nics": igw_nics}
        self.client.force_login(self.adm)
        r = self.client.get(reverse("tenant_detail", args=[self.t.pk]))
        self.assertContains(r, 'data-open="dlg-subdel-dc1"')  # owner 2026-10-29 20:20: − Subnet, a row action
        req = self.ask(kind="inet", action="delete", target=net, site="nowhere")  # a site the tenant has not: its own
        self.assertEqual((req.action, req.target, req.site), ("delete", net, "dc1"))
        req = self.approve(req)
        self.assertEqual(req.run, "done", (req.error, req.steps))
        self.assertNotIn(net, self.cfg("egw"))
        self.assertEqual(self.cfg("igw"), igw_before)
        self.assertFalse(PrefixAssignment.active().filter(cidr=net).exists())

    def retry(self, req):
        self.client.force_login(self.sa)
        with self.patches():
            self.client.post(reverse("tenant_network_retry", args=[self.t.pk, req.pk]))
        req.refresh_from_db()
        return req

    def test_a_retry_after_the_prefix_was_released_adds_nothing(self):
        """Owner 2026-11-18 16:20 (citix): a failed /29 addition, its /29 then removed (released) by another request -
        Retry of the addition showed "done" with nothing on the egw. It now fails and says to ask again."""
        from tenants.models import PrefixAssignment
        from tenants.netops import DeployError, NetChange

        self.deploy()
        with mock.patch.object(NetChange, "pub_add", side_effect=DeployError("the project has no igw")):
            req = self.approve(self.ask(kind="inet", prefix_len=29))
        self.assertEqual(req.run, "failed", req.steps)
        net = req.result
        rm = self.approve(self.ask(kind="inet", action="delete", target=net))
        self.assertEqual(rm.run, "done", (rm.error, rm.steps))
        self.assertFalse(PrefixAssignment.active().filter(cidr=net).exists())
        steps = list(req.steps)
        req = self.retry(req)
        self.assertEqual(req.run, "failed", req.steps)
        self.assertIn(f"{net} is no longer assigned to this project (released on ", req.error)
        self.assertIn("ask for the prefix with a new request", req.error)
        self.assertEqual(req.steps, steps)  # no step ran
        self.assertNotIn(net, self.cfg("egw"))
        self.assertFalse(PrefixAssignment.active().filter(cidr=net).exists())

    def test_a_retry_while_the_prefix_is_assigned_still_adds_it(self):
        """The check lets a Retry go on while the prefix is still the project's (the usual Retry)."""
        from tenants.netops import DeployError, NetChange

        self.deploy()
        with mock.patch.object(NetChange, "pub_add", side_effect=DeployError("the project has no igw")):
            req = self.approve(self.ask(kind="inet", prefix_len=29))
        self.assertEqual(req.run, "failed", req.steps)
        req = self.retry(req)
        self.assertEqual(req.run, "done", (req.error, req.steps))
        self.assertIn(f"set protocols static route {req.result} next-hop", self.cfg("egw"))

    def test_a_corporate_addition_whose_prefix_was_released_is_not_retried(self):
        """The same for a corporate prefix: released meanwhile, the addition's Retry fails before any step."""
        from tenants import ordering
        from tenants.models import Connectivity, PrefixAssignment
        from tenants.netops import DeployError, NetChange

        org = self.t.organization
        org.kind = "corp"
        org.save()
        self.t.order = {**self.t.order, "connections": ["corpnet"], "corpnet_prefixes": [28]}
        self.t.kind = "corp"
        self.t.save()
        PrefixAssignment.active().filter(tenant=self.t, kind__startswith="inet").update(released_at=timezone.now())
        Connectivity.objects.filter(tenant=self.t).update(connection_type="corpnet")
        ordering.allocate_one(self.t, "corpnet", 28)
        self.deploy()
        with mock.patch.object(NetChange, "corp_add", side_effect=DeployError("stopped")):
            req = self.approve(self.ask(kind="corpnet", prefix_len=29))
        self.assertEqual(req.run, "failed", req.steps)
        PrefixAssignment.active().filter(cidr=req.result).update(released_at=timezone.now())
        with mock.patch.object(NetChange, "corp_add") as corp_add:
            req = self.retry(req)
        corp_add.assert_not_called()
        self.assertEqual(req.run, "failed", req.steps)
        self.assertIn(f"{req.result} is no longer assigned to this project", req.error)

    def test_a_retry_does_not_take_a_later_assignment_of_the_same_prefix(self):
        """commit-109: the released /29 assigned to the project again by a later request - the old addition's Retry
        still stops: it goes on only with the assignment its own approval made."""
        from tenants.models import PrefixAssignment
        from tenants.netops import DeployError, NetChange

        self.deploy()
        with mock.patch.object(NetChange, "pub_add", side_effect=DeployError("the project has no igw")):
            req = self.approve(self.ask(kind="inet", prefix_len=29))
        net = req.result
        mine = PrefixAssignment.active().get(cidr=net)
        self.assertEqual(mine.note, f"network request #{req.pk}")
        rm = self.approve(self.ask(kind="inet", action="delete", target=net))
        self.assertEqual(rm.run, "done", (rm.error, rm.steps))
        PrefixAssignment.objects.create(
            tenant=self.t, site=mine.site, kind="inet_extra", cidr=net, note="network request #9999"
        )
        with mock.patch.object(NetChange, "pub_add") as pub_add:
            req = self.retry(req)
        pub_add.assert_not_called()
        self.assertEqual(req.run, "failed", req.steps)
        self.assertIn(f"{net} is no longer assigned to this project (released on ", req.error)

    def test_a_retry_without_its_own_assignment_stops(self):
        """commit-109: an assignment that does not name the request (or is at another site) does not count."""
        from tenants.models import PrefixAssignment
        from tenants.netops import DeployError, NetChange

        self.deploy()
        with mock.patch.object(NetChange, "pub_add", side_effect=DeployError("stopped")):
            req = self.approve(self.ask(kind="inet", prefix_len=29))
        PrefixAssignment.active().filter(cidr=req.result).update(site="elsewhere")
        req = self.retry(req)
        self.assertEqual(req.run, "failed", req.steps)
        self.assertIn(f"the assignment of {req.result} made for this request is not found at dc1", req.error)

    def test_one_network_change_of_a_project_at_a_time(self):
        """commit-110: while one change of the project runs, another does not start - an approved one waits and the
        timer starts it afterwards; a Retry says to press it again later. So a removal cannot release a prefix in the
        middle of its addition."""
        from tenants import netops
        from tenants.models import NetworkRequest
        from tenants.netops import DeployError, NetChange

        self.deploy()
        with mock.patch.object(NetChange, "pub_add", side_effect=DeployError("stopped")):
            failed = self.approve(self.ask(kind="inet", prefix_len=29))
        self.assertEqual(failed.run, "failed")
        other = self.approve(self.ask(kind="inet", prefix_len=29))
        self.assertEqual(other.run, "done", (other.error, other.steps))
        NetworkRequest.objects.filter(pk=other.pk).update(run="running")  # a change of this project running
        rm = self.ask(kind="inet", action="delete", target=failed.result)
        self.client.force_login(self.sa)
        with self.patches():
            r = self.client.post(reverse("tenant_network_decide", args=[self.t.pk, rm.pk, "approve"]), {}, follow=True)
        rm.refresh_from_db()
        self.assertEqual((rm.state, rm.run), ("approved", ""))  # approved, not started
        self.assertContains(r, "starts by itself after it")
        with self.patches():
            r = self.client.post(reverse("tenant_network_retry", args=[self.t.pk, failed.pk]), follow=True)
        failed.refresh_from_db()
        self.assertEqual(failed.run, "failed")
        self.assertContains(r, "press Retry when it is done")
        self.assertEqual(netops.claim(rm), netops.BUSY)
        NetworkRequest.objects.filter(pk=other.pk).update(run="done")
        NetworkRequest.objects.filter(pk=rm.pk).update(decided_at=timezone.now() - timedelta(minutes=5))
        with self.patches():
            out = netops.start_waiting(log=lambda *a: None)
        rm.refresh_from_db()
        self.assertEqual(rm.run, "done", (out, rm.error, rm.steps))

    def test_a_deployment_reads_the_group_again_after_its_quota(self):
        """Owner 2026-11-18 17:02: each quota change sets the quota on the platform and then reads the touched objects
        again - a deployment (which sets the group's quota) reads the project's group again at its end."""
        with mock.patch("adoption.collector.observe_group") as observe:
            self.deploy()
        self.t.refresh_from_db()
        observe.assert_called_once()
        control, gid, name = observe.call_args.args
        self.assertEqual((control.key, gid, name), ("dc1.cloud", self.t.one_group_id, self.t.one_group_name))

    def test_a_deployment_failing_after_its_quota_still_reads_the_group_again(self):
        """commit-114: the quota is set, a later step (the ACLs) fails - the group the run set the quota of is read
        again all the same, by the ID the run itself has (the project's record may not have it yet)."""
        from tenants import deploy
        from tenants.deploy import DeployError

        with (
            self.patches(),
            mock.patch("adoption.collector.observe_group") as observe,
            mock.patch.object(deploy.Deployer, "acl_step", side_effect=DeployError("acl failed")),
        ):
            dep = deploy.deploy(self.t, None)
        self.assertEqual(dep.state, "failed", dep.steps)
        self.assertEqual([s["status"] for s in dep.steps[:4]], ["ok"] * 4)  # lookups, group, admin, quota
        observe.assert_called_once()
        control, gid, name = observe.call_args.args
        self.assertEqual(control.key, "dc1.cloud")
        self.assertIsInstance(gid, int)
        self.assertEqual(name, self.t.group_name or self.t.one_group_name)

    def test_a_start_refused_as_busy_stays_busy_when_the_other_change_ends_meanwhile(self):
        """commit-111 again: the claim found another change running; that change ends before start() returns - start()
        still says BUSY (the claim's own answer), not "not taken"."""
        from tenants import netops
        from tenants.netops import DeployError, NetChange

        self.deploy()
        with mock.patch.object(NetChange, "pub_add", side_effect=DeployError("stopped")):
            req = self.approve(self.ask(kind="inet", prefix_len=29))
        self.assertEqual(req.run, "failed")
        with self.patches(), mock.patch.object(netops, "busy", side_effect=[True, False, False]):
            self.assertEqual(netops.start(req), netops.BUSY)
        req.refresh_from_db()
        self.assertEqual(req.run, "failed")

    def test_a_route_only_prefix_stays_route_only_after_a_new_main_prefix(self):
        """commit-089: replacing the main prefix deploys the project again - a prefix added as an egw route gets no
        OpenNebula network and no igw NIC from it; it is still routed on the new egw."""
        self.deploy()
        req = self.approve(self.ask(kind="inet", prefix_len=29))
        self.assertEqual(req.run, "done", (req.error, req.steps))
        extra = req.result
        rep = self.approve(self.ask(kind="inet", action="replace", target="203.0.113.16/31", prefix_len=30))
        self.assertEqual(rep.run, "done", (rep.error, rep.steps))
        self.assertIsNone(self.world.vnet("_t_romashka_gpulab-pub1_dc1"))
        igw = self.world.vm("_romashka_gpulab_igw_dc1")
        names = [
            self.world.vnets[n["vid"]]["name"] for n in self.world.vms[igw]["nics"] if n["vid"] in self.world.vnets
        ]
        self.assertFalse(any("-pub" in n for n in names), names)
        self.assertIn(f"set protocols static route {extra} next-hop", self.cfg("egw"))

    def test_removing_a_route_only_prefix_leaves_a_network_of_the_same_name_alone(self):
        """commit-090: a route-only prefix has no network; another network that happens to carry the generated name
        (other addresses) keeps its NICs and stays."""
        self.deploy()
        req = self.approve(self.ask(kind="inet", prefix_len=29))
        net = req.result
        self.world.add_vnet(4321, "_t_romashka_gpulab-pub1_dc1", ars=[{"IP": "198.51.100.1", "SIZE": 1}], gname="other")
        self.world.vms[779] = {"name": "web", "state": "3", "nics": []}
        self.world._nic(779, 4321)
        req = self.approve(self.ask(kind="inet", action="delete", target=net))
        self.assertEqual(req.run, "done", (req.error, req.steps))
        self.assertIn(4321, self.world.vnets)
        self.assertEqual([n["vid"] for n in self.world.vms[779]["nics"]], [4321])
        self.assertNotIn("nics", [s["name"] for s in req.steps])

    def test_an_older_prefix_with_its_own_network_is_removed_with_it(self):
        """commit-090: a prefix added before 2026-11-18 (own_network) has the project's network of that prefix detached
        and deleted - and only when that network's addresses are the prefix's."""
        from tenants.models import PrefixAssignment

        self.deploy()
        main = PrefixAssignment.active().get(tenant=self.t, kind="inet_main")
        old = PrefixAssignment.objects.create(
            tenant=self.t, site=main.site, kind="inet_extra", cidr="198.51.100.8/29", own_network=True
        )
        self.deploy()  # the deployment makes its network and the igw NIC, as before
        vid = self.world.vnet("_t_romashka_gpulab-pub1_dc1")
        self.assertIsNotNone(vid)
        req = self.approve(self.ask(kind="inet", action="delete", target=old.cidr))
        self.assertEqual(req.run, "done", (req.error, req.steps))
        self.assertNotIn(vid, self.world.vnets)

    def test_a_failed_lookup_keeps_the_prefix_assigned(self):
        """commit-097: an OpenNebula error while looking for the prefix's network fails the step - the prefix stays
        assigned and recorded, the network stays, and Retry goes on."""
        from adoption.collector import OneError
        from tenants.models import PrefixAssignment

        self.deploy()
        main = PrefixAssignment.active().get(tenant=self.t, kind="inet_main")
        old = PrefixAssignment.objects.create(
            tenant=self.t, site=main.site, kind="inet_extra", cidr="198.51.100.8/29", own_network=True
        )
        self.deploy()
        vid = self.world.vnet("_t_romashka_gpulab-pub1_dc1")
        from tenants import netops

        real, real_router, after_egw = self.world._call, netops.NetChange._router, []

        def router(obj, role, *a, **kw):  # the egw's change goes through; the lookups after it fail
            out = real_router(obj, role, *a, **kw)
            after_egw.append(role)
            return out

        def flaky(method, *args):
            if after_egw and method in ("one.vnpool.info", "one.vn.info"):
                raise OneError(f"[{method}] timeout")
            return real(method, *args)

        with (
            mock.patch.object(self.world, "_call", side_effect=flaky),
            mock.patch.object(netops.NetChange, "_router", autospec=True, side_effect=router),
        ):
            req = self.approve(self.ask(kind="inet", action="delete", target=old.cidr))
        self.assertEqual(after_egw, ["egw"])
        self.assertEqual(req.run, "failed", req.steps)
        self.assertIn(vid, self.world.vnets)
        self.assertTrue(PrefixAssignment.active().filter(cidr=old.cidr).exists())

    def test_the_upgrade_marks_only_recorded_prefixes(self):
        """commit-089: migration 0102 gives own_network only to a prefix a deployment recorded with its network - not
        to one added as a route only before the upgrade."""
        import importlib

        from django.apps import apps

        from tenants.models import PrefixAssignment

        self.deploy()
        main = PrefixAssignment.active().get(tenant=self.t, kind="inet_main")
        rec = PrefixAssignment.objects.create(tenant=self.t, site=main.site, kind="inet_extra", cidr="198.51.100.8/29")
        bare = PrefixAssignment.objects.create(
            tenant=self.t, site=main.site, kind="inet_extra", cidr="198.51.100.16/29"
        )
        dep = self.t.deployments.exclude(state="plan").first()
        dep.resources = {
            **(dep.resources or {}),
            "public_networks": [{"name": "_t_x-pub1_dc1", "id": 777, "prefix": rec.cidr, "igw": "198.51.100.9"}],
        }
        dep.save(update_fields=["resources"])
        # the same prefix given again as a route only, after an earlier assignment of it was released: the old record
        # (from a deployment before this assignment) is not this assignment's
        PrefixAssignment.objects.create(
            tenant=self.t, site=main.site, kind="inet_extra", cidr="198.51.100.24/29", released_at=timezone.now()
        )
        again = PrefixAssignment.objects.create(
            tenant=self.t, site=main.site, kind="inet_extra", cidr="198.51.100.24/29"
        )
        dep.resources["public_networks"].append(
            {"name": "_t_x-pub2_dc1", "id": 778, "prefix": again.cidr, "igw": "198.51.100.25"}
        )
        dep.save(update_fields=["resources"])
        importlib.import_module("tenants.migrations.0102_prefix_own_network").mark_existing(apps, None)
        for p in (rec, bare, again):
            p.refresh_from_db()
        self.assertEqual((rec.own_network, rec.vnet_id), (True, 777))
        self.assertEqual((bare.own_network, bare.vnet_id), (False, None))
        self.assertEqual((again.own_network, again.vnet_id, again.network_check), (False, None, True))  # commit-103

    def test_a_preview_never_changes_the_recorded_network(self):
        """commit-102: a preview finds a network by its name, and the assignment's recorded ID stays as it was."""
        from tenants import deploy
        from tenants.models import PrefixAssignment

        self.deploy()
        main = PrefixAssignment.active().get(tenant=self.t, kind="inet_main")
        self.world.add_vnet(4600, "_t_romashka_gpulab-pub1_dc1", ars=[{"IP": "198.51.100.9", "SIZE": 1}])
        p = PrefixAssignment.objects.create(
            tenant=self.t, site=main.site, kind="inet_extra", cidr="198.51.100.8/29", own_network=True, vnet_id=4601
        )
        with self.patches():
            deploy.deploy(self.t, None, dry=True)
        p.refresh_from_db()
        self.assertEqual(p.vnet_id, 4601)

    def test_a_network_found_by_name_is_taken_only_when_it_is_the_prefixs(self):
        """commit-090: the deployment records an existing network of the prefix's name only when it is the project's own
        (its group) with all its ranges in the prefix; another is not recorded, and the removal then waits for a check."""
        from tenants import deploy
        from tenants.models import PrefixAssignment

        self.deploy()
        main = PrefixAssignment.active().get(tenant=self.t, kind="inet_main")
        self.world.add_vnet(4700, "_t_romashka_gpulab-pub1_dc1", ars=[{"IP": "198.51.100.9", "SIZE": 1}], gid=999)
        p = PrefixAssignment.objects.create(
            tenant=self.t, site=main.site, kind="inet_extra", cidr="198.51.100.8/29", own_network=True
        )
        with self.patches():
            dep = deploy.deploy(self.t, None)
        self.assertEqual(dep.state, "done", dep.steps)
        p.refresh_from_db()
        self.assertIsNone(p.vnet_id)  # another group's: not recorded
        # commit-104: nor attached to the igw, nor listed for it
        self.assertFalse([c for c in self.world.calls if c[0] == "one.vm.attachnic" and "4700" in str(c[1])])
        igw = self.world.vm("_romashka_gpulab_igw_dc1")
        self.assertNotIn(4700, [n["vid"] for n in self.world.vms[igw]["nics"]])
        self.assertNotIn(4700, [r["id"] for r in dep.resources.get("public_networks") or []])
        req = self.approve(self.ask(kind="inet", action="delete", target=p.cidr))
        self.assertEqual(req.run, "failed", req.steps)  # and not deleted: it still serves the prefix
        self.assertIn(4700, self.world.vnets)
        gid = self.world.vnets[self.world.vnet("_t_romashka_gpulab-main_dc1")]["gid"]
        self.world.pools["one.grouppool.info"] = (  # the platform knows the project's group from now on
            f"<GROUP_POOL><GROUP><ID>{gid}</ID><NAME>{self.t.group_name}</NAME></GROUP></GROUP_POOL>"
        )
        self.world.vnets[4700]["gid"] = gid  # the project's own group: taken
        with self.patches():
            dep = deploy.deploy(self.t, None)
        p.refresh_from_db()
        self.assertEqual(p.vnet_id, 4700)

    def test_an_unclear_prefix_is_kept_while_a_recorded_network_serves_it(self):
        """commit-103: a prefix marked for a check - its removal stops while a network a record names for it is still
        there, and goes through once that network is gone."""
        from tenants.models import PrefixAssignment

        self.deploy()
        main = PrefixAssignment.active().get(tenant=self.t, kind="inet_main")
        p = PrefixAssignment.objects.create(
            tenant=self.t, site=main.site, kind="inet_extra", cidr="198.51.100.8/29", network_check=True
        )
        self.world.add_vnet(4800, "_t_old-pub_dc1", ars=[{"IP": "198.51.100.9", "SIZE": 1}])
        dep = self.t.deployments.exclude(state="plan").first()
        dep.resources = {
            **(dep.resources or {}),
            "public_networks": [{"name": "_t_old-pub_dc1", "id": 4800, "prefix": p.cidr, "igw": "198.51.100.9"}],
        }
        dep.save(update_fields=["resources"])
        self.world.vnets[4800]["ars"][0].update(ip="198.51.100.4", size=6)  # only partly in the prefix: still serves it
        req = self.approve(self.ask(kind="inet", action="delete", target=p.cidr))
        self.assertEqual(req.run, "failed", req.steps)
        self.assertIn("still serves it", req.error or str(req.steps))
        self.assertTrue(PrefixAssignment.active().filter(cidr=p.cidr).exists())
        del self.world.vnets[4800]  # deleted on the platform
        self.client.force_login(self.sa)
        with self.patches():
            self.client.post(reverse("tenant_network_retry", args=[self.t.pk, req.pk]))
        req.refresh_from_db()
        self.assertEqual(req.run, "done", (req.error, req.steps))
        self.assertFalse(PrefixAssignment.active().filter(cidr=p.cidr).exists())

    def test_a_stale_record_does_not_point_a_removal_at_a_network(self):
        """commit-090: a route-only prefix whose CIDR an old record still lists with a network ID - the removal leaves
        that network alone (it goes only by the ID kept on the assignment)."""
        self.deploy()
        req = self.approve(self.ask(kind="inet", prefix_len=29))
        cidr = req.result
        net = ipaddress.ip_network(cidr)
        self.world.add_vnet(4400, "_t_old-pub_dc1", ars=[{"IP": str(net[1]), "SIZE": 2}])
        self.world.vms[780] = {"name": "app", "state": "3", "nics": []}
        self.world._nic(780, 4400)
        dep = self.t.deployments.exclude(state="plan").first()
        dep.resources = {
            **(dep.resources or {}),
            "public_networks": [{"name": "_t_old-pub_dc1", "id": 4400, "prefix": cidr, "igw": str(net[1])}],
        }
        dep.save(update_fields=["resources"])
        req = self.approve(self.ask(kind="inet", action="delete", target=cidr))
        self.assertEqual(req.run, "done", (req.error, req.steps))
        self.assertIn(4400, self.world.vnets)
        self.assertEqual([n["vid"] for n in self.world.vms[780]["nics"]], [4400])

    def test_a_network_reaching_past_the_prefix_fails_the_removal(self):
        """commit-101: the recorded network is there but one of its ranges runs past the prefix - the step fails and the
        prefix stays assigned, the network untouched."""
        from tenants.models import PrefixAssignment

        self.deploy()
        main = PrefixAssignment.active().get(tenant=self.t, kind="inet_main")
        self.world.add_vnet(4500, "_t_romashka_gpulab-pub1_dc1", ars=[{"IP": "198.51.100.9", "SIZE": 12}])
        old = PrefixAssignment.objects.create(
            tenant=self.t, site=main.site, kind="inet_extra", cidr="198.51.100.8/29", own_network=True, vnet_id=4500
        )
        req = self.approve(self.ask(kind="inet", action="delete", target=old.cidr))
        self.assertEqual(req.run, "failed", req.steps)
        self.assertIn("not this project's own network", req.error or str(req.steps))
        self.assertIn(4500, self.world.vnets)
        self.assertTrue(PrefixAssignment.active().filter(cidr=old.cidr).exists())

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

    def test_a_replacement_that_failed_after_the_swap_is_retried_to_the_end(self):
        """core-028 (twelfth full review): the deploy after the swap failed; the old prefix was released and the new one
        is the main one, so Retry stopped at "not reserved" and the project stayed without its main network and egw.
        Now Retry goes on from the deploy; the steps of the first run stay on the request."""
        from tenants import deploy as dp
        from tenants.models import PrefixAssignment

        self.deploy()
        old_egw = self.world.vm("_romashka_gpulab_egw_dc1")
        req = self.ask(kind="inet", action="replace", target="203.0.113.16/31", prefix_len=30)
        real = dp.deploy
        calls = []

        def fails_once(*a, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise dp.DeployError("the platform did not answer")
            return real(*a, **kw)

        with mock.patch.object(dp, "deploy", side_effect=fails_once):
            req = self.approve(req)
            self.assertEqual(req.run, "failed", req.steps)
            self.assertEqual({s["name"]: s["status"] for s in req.steps}["swap"], "ok")
            self.assertTrue(PrefixAssignment.active().filter(tenant=self.t, kind="inet_main", cidr=req.result).exists())
            with self.patches():
                self.client.post(reverse("tenant_network_retry", args=[self.t.pk, req.pk]))
        req.refresh_from_db()
        self.assertEqual(req.run, "done", (req.error, req.steps))
        status = {s["name"]: s["status"] for s in req.steps}
        self.assertEqual((status["swap"], status["deploy"], status["igw-attach"]), ("ok", "ok", "ok"))
        egw = self.world.vm("_romashka_gpulab_egw_dc1")
        self.assertNotIn(egw, (None, old_egw))
        self.assertIsNotNone(self.world.vnet("_t_romashka_gpulab-main_dc1"))
        self.assertEqual(len(calls), 2)

    def test_a_swap_is_all_or_nothing_and_a_half_done_one_is_finished(self):
        """core-028 again (thirteenth full review): the swap's two writes - release the old main prefix, promote the
        new one - were separate; a stop between them left no main prefix and Retry failed in lookups. They are one
        transaction now, and a run finishes a half-done swap left by an older release before its lookups."""
        from tenants import deploy as dp
        from tenants.models import PrefixAssignment

        self.deploy()
        req = self.ask(kind="inet", action="replace", target="203.0.113.16/31", prefix_len=30)
        real_save = PrefixAssignment.save

        def stops_at_the_promotion(obj, *a, **kw):
            if kw.get("update_fields") == ["kind"] and obj.kind == PrefixAssignment.Kind.INET_MAIN:
                raise RuntimeError("the portal stopped")
            return real_save(obj, *a, **kw)

        with mock.patch.object(PrefixAssignment, "save", stops_at_the_promotion):
            req = self.approve(req)
        self.assertEqual(req.run, "failed", req.steps)
        main = PrefixAssignment.active().filter(tenant=self.t, kind="inet_main")
        self.assertEqual([p.cidr for p in main], ["203.0.113.16/31"])  # nothing released: the old one is still main
        with self.patches():
            self.client.post(reverse("tenant_network_retry", args=[self.t.pk, req.pk]))
        req.refresh_from_db()
        self.assertEqual(req.run, "done", (req.error, req.steps))
        self.assertEqual(
            [p.cidr for p in PrefixAssignment.active().filter(tenant=self.t, kind="inet_main")], [req.result]
        )
        # a half-done swap (an older release): the old one released, the new one still next - Retry finishes it
        req2 = self.ask(kind="inet", action="replace", target=req.result, prefix_len=30)
        real = dp.deploy
        with mock.patch.object(dp, "deploy", side_effect=dp.DeployError("stop")):
            req2 = self.approve(req2)
        self.assertEqual(req2.run, "failed")
        PrefixAssignment.objects.filter(tenant=self.t, cidr=req2.result).update(kind="inet_next")  # as before the fix
        self.assertFalse(PrefixAssignment.active().filter(tenant=self.t, kind="inet_main").exists())
        with mock.patch.object(dp, "deploy", side_effect=real), self.patches():
            self.client.post(reverse("tenant_network_retry", args=[self.t.pk, req2.pk]))
        req2.refresh_from_db()
        self.assertEqual(req2.run, "done", (req2.error, req2.steps))
        self.assertIn("swap-finished", [s["name"] for s in req2.steps])
        self.assertEqual(
            [p.cidr for p in PrefixAssignment.active().filter(tenant=self.t, kind="inet_main")], [req2.result]
        )

    def test_a_change_for_another_site_is_not_made_on_the_projects_site(self):
        """core-032 (fourteenth full review): a request naming another site of the project was allocated and run on
        the order's site and platform - a speed change altered the home site, a deletion released the prefix with the
        network left on the other site. The portal makes the project's networks on its own site only: such a change
        is refused on the request, at approval (an older one) and at the run, and a release is this site's only."""
        from tenants import netops
        from tenants.deploy import DeployError
        from tenants.models import Connectivity, NetworkRequest, PrefixAssignment

        self.deploy()
        Connectivity.objects.create(tenant=self.t, site="dc2", version=1, connection_type="inet", inet_mbps=50)
        before = NetworkRequest.objects.count()
        self.client.force_login(self.adm)
        r = self.client.post(
            reverse("tenant_network_request", args=[self.t.pk]),
            {"kind": "inet", "action": "bandwidth", "mbps": 200, "site": "dc2"},
            follow=True,
        )
        self.assertContains(r, "are not made by the portal")
        self.assertEqual(NetworkRequest.objects.count(), before)
        page = self.client.get(reverse("tenant_detail", args=[self.t.pk])).content.decode()
        self.assertIn("Changes on this site: through support", page)
        self.assertNotIn('id="dlg-speed-dc2"', page)
        self.assertIn('id="dlg-speed-dc1"', page)
        # one asked before the check: nothing allocated at approval, it stays pending
        old = NetworkRequest.objects.create(tenant=self.t, site="dc2", kind="inet", action="add", prefix_len=30)
        allocated = PrefixAssignment.active().filter(tenant=self.t).count()
        self.client.force_login(self.sa)
        r = self.client.post(reverse("tenant_network_decide", args=[self.t.pk, old.pk, "approve"]), follow=True)
        self.assertContains(r, "are not made by the portal")
        old.refresh_from_db()
        self.assertEqual((old.state, old.run), ("pending", ""))
        self.assertEqual(PrefixAssignment.active().filter(tenant=self.t).count(), allocated)
        # and the run refuses it: the home site's egw and connectivity stay as they were
        NetworkRequest.objects.filter(pk=old.pk).update(state="approved", action="bandwidth", mbps=300)
        old.refresh_from_db()
        with self.assertRaises(DeployError):
            netops.NetChange(old)
        home = next(c for c in self.t.current_connectivity() if c.site == "dc1")
        self.assertNotEqual(home.inet_mbps, 300)
        # an L2 channel's other site is still its own business (correction 13)
        l2 = NetworkRequest(tenant=self.t, site="dc2", kind="l2", action="add")
        self.assertEqual(netops.other_site(l2), "")

    def test_a_change_approved_while_blocked_waits_and_runs_once(self):
        """core-033 (fourteenth full review): approved while the platform was read only (or cut off right after the
        approval), a change never started - Retry took only failed runs and nothing else started it. It now shows as
        waiting with what holds it and Run; the appliance timer starts it by itself once nothing blocks it; each start
        is a claim, so it runs once."""
        from datetime import timedelta

        from django.utils import timezone

        from tenants import netops
        from tenants.models import NetworkRequest

        self.deploy()
        held = "the platform is read only (a backup is restored)"
        req = self.ask(kind="inet", prefix_len=29)
        with mock.patch.object(netops, "blocked_reason", return_value=held):
            req = self.approve(req)
        self.assertEqual((req.state, req.run), ("approved", ""))
        page = self.client.get(reverse("tenant_detail", args=[self.t.pk])).content.decode()
        self.assertIn(held, page)
        self.assertIn(reverse("tenant_network_retry", args=[self.t.pk, req.pk]), page)
        with mock.patch.object(netops, "blocked_reason", return_value=held):
            self.client.post(reverse("tenant_network_retry", args=[self.t.pk, req.pk]))
        req.refresh_from_db()
        self.assertEqual(req.run, "")  # still held: not started
        with self.patches():
            self.client.post(reverse("tenant_network_retry", args=[self.t.pk, req.pk]))  # Run, once nothing holds it
        req.refresh_from_db()
        self.assertEqual(req.run, "done", (req.error, req.steps))
        # the timer: another one approved while held, older than the grace - started once, by itself
        req2 = self.ask(kind="inet", prefix_len=30)
        with mock.patch.object(netops, "blocked_reason", return_value=held):
            req2 = self.approve(req2)
            self.assertEqual(netops.start_waiting(), "")  # held: left waiting
        NetworkRequest.objects.filter(pk=req2.pk).update(decided_at=timezone.now() - timedelta(minutes=5))
        with self.patches():
            self.assertEqual(netops.start_waiting(), f"#{req2.pk} done")
            self.assertEqual(netops.start_waiting(), "")  # once
        req2.refresh_from_db()
        self.assertEqual(req2.run, "done", (req2.error, req2.steps))

    def test_blocked_waiting_changes_do_not_hold_up_the_others(self):
        """commit-035: the timer took the five oldest waiting changes and skipped those it could not start, so five
        that stay so starved a later one that could run. The limit now counts the changes started."""
        from datetime import timedelta

        from django.utils import timezone

        from tenants import netops
        from tenants.models import NetworkRequest

        self.deploy()
        old = timezone.now() - timedelta(minutes=10)
        skipped = [  # five older ones the timer passes over every round (for another site: never started here)
            NetworkRequest.objects.create(
                tenant=self.t, site="dc2", kind="inet", action="add", prefix_len=30, state="approved", decided_at=old
            )
            for _ in range(5)
        ]
        req = self.ask(kind="inet", prefix_len=29)
        with mock.patch.object(netops, "blocked_reason", return_value="the platform is read only"):
            req = self.approve(req)
        NetworkRequest.objects.filter(pk=req.pk).update(decided_at=old)
        with self.patches():
            self.assertEqual(netops.start_waiting(), f"#{req.pk} done")
        req.refresh_from_db()
        self.assertEqual(req.run, "done", (req.error, req.steps))
        self.assertEqual({NetworkRequest.objects.get(pk=r.pk).run for r in skipped}, {""})

    def test_a_decision_made_meanwhile_stands_and_a_run_is_taken_once(self):
        """core-027 (twelfth full review): an approval read the request, a cancellation or rejection was committed
        before it saved, and the approval overwrote it and dispatched the change (and a stale cancellation could
        overwrite an approval). A decision now ends the request only while it is pending; the loser changes nothing
        and dispatches nothing. A run is claimed: two retries run once."""
        from tenants import netops, network_views
        from tenants.models import NetworkRequest, PrefixAssignment

        self.deploy()
        allocated = PrefixAssignment.active().filter(tenant=self.t).count()
        for meanwhile in ("cancelled", "rejected"):
            with self.subTest(meanwhile=meanwhile):
                req = self.ask(kind="inet", action="add", prefix_len=30)
                decides = network_views.can_decide

                def decided_first(tenant, user, req=req, meanwhile=meanwhile, decides=decides):
                    NetworkRequest.objects.filter(pk=req.pk).update(state=meanwhile)
                    return decides(tenant, user)

                self.client.force_login(self.sa)
                with (
                    mock.patch.object(network_views, "can_decide", side_effect=decided_first),
                    mock.patch.object(netops, "start") as start,
                ):
                    r = self.client.post(
                        reverse("tenant_network_decide", args=[self.t.pk, req.pk, "approve"]), follow=True
                    )
                start.assert_not_called()
                req.refresh_from_db()
                self.assertEqual((req.state, req.decided_by), (meanwhile, None))
                self.assertEqual(PrefixAssignment.active().filter(tenant=self.t).count(), allocated)  # rolled back
                self.assertContains(r, "nothing was changed")
        # a cancellation that loses to an approval leaves the approval as it is
        req = self.ask(kind="inet", action="add", prefix_len=30)
        asks = network_views.can_request

        def approved_first(tenant, user, req=req):
            NetworkRequest.objects.filter(pk=req.pk).update(state="approved")
            return asks(tenant, user)

        self.client.force_login(self.adm)
        with mock.patch.object(network_views, "can_request", side_effect=approved_first):
            self.client.post(reverse("tenant_network_decide", args=[self.t.pk, req.pk, "cancel"]))
        req.refresh_from_db()
        self.assertEqual(req.state, "approved")
        # two starts of one failed run: the second finds it taken
        NetworkRequest.objects.filter(pk=req.pk).update(run="failed")
        self.assertTrue(netops.claim(req))
        self.assertFalse(netops.claim(req))
        self.assertIsNone(netops.start(req))
        NetworkRequest.objects.filter(pk=req.pk).update(state="cancelled", run="failed")
        self.assertFalse(netops.claim(req))  # and a request no longer approved is never run

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

    def l2_on_the_management_platform(self):
        """An external L2 channel made through the site's management platform: (channel, the mgmt world, its control)."""
        from inventory.models import Platform, PlatformControl, Site
        from tenants.models import L2Channel

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
        self.world.vnets[self.world.vnet("_t_romashka_gpulab-lan1_dc1")]["gname"] = self.t.group_name  # the project's
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
        return L2Channel.objects.get(tenant=self.t), mgmt, ctl, req

    def test_a_projects_deletion_removes_its_l2_channels_on_the_management_platform(self):
        """core-009 again (fifteenth full review): a deletion found other platforms only through the project's quota
        records, so an L2 channel's gateway and VLAN network on the site's management platform stayed, while the
        channel was marked removed. The deletion removes each channel first with its own removal; a management
        platform that cannot be changed stops the deletion and the channel stays as it is."""
        import contextlib

        from tenants import undeploy
        from tenants.deploy import DeployError
        from tenants.models import L2Channel

        ch, mgmt, ctl, _req = self.l2_on_the_management_platform()
        self.assertIsNotNone(mgmt.vm("_romashka_gpulab_l2src1721_dc1"))
        # this test world cannot list every pool the zone steps read: those steps are another test's (tests.py)
        rest = {
            f: mock.MagicMock(return_value="(another test's)")
            for f in ("vms_step", "pools_step", "verify_step", "acl_step", "users_step", "group_step", "check_step")
        }
        quiet = [mock.patch.object(undeploy.Undeployer, "inventory", return_value={})]
        quiet += [mock.patch.object(undeploy.Undeployer, f, m) for f, m in rest.items()]
        ctl.mode = "adoption"  # the management platform read only: nothing may be left behind unseen
        ctl.save()
        with self.patches(), contextlib.ExitStack() as st, self.assertRaises(DeployError):
            for q in quiet:
                st.enter_context(q)
            undeploy.archive(self.t, self.sa)
        ch.refresh_from_db()
        self.t.refresh_from_db()
        self.assertEqual(ch.state, "active")
        self.assertNotEqual(self.t.status, "archived")
        self.assertIsNotNone(mgmt.vm("_romashka_gpulab_l2src1721_dc1"))
        ctl.mode = "execution"
        ctl.save()
        # commit-036: the provisioning lookups (zone, cluster, the network-function image, the exchange network) are
        # not needed to remove a channel - with them failing, the channel still goes
        from tenants.deploy import Deployer

        gone = mock.patch.object(Deployer, "lookups", side_effect=DeployError("the network-function image is gone"))
        with self.patches(), contextlib.ExitStack() as st:
            for q in [*quiet, gone]:
                st.enter_context(q)
            steps = undeploy.archive(self.t, self.sa)
        self.assertEqual([s["name"] for s in steps][:2], ["l2", "vms"])  # first, before the zones
        self.assertIn("VNI 1721", steps[0]["detail"])
        ch.refresh_from_db()
        self.assertEqual(ch.state, L2Channel.State.REMOVED)
        self.assertIsNone(mgmt.vm("_romashka_gpulab_l2src1721_dc1"))
        self.assertIsNone(mgmt.vnet("s2s_1721_romashka-gpulab"))
        self.assertIsNone(self.world.vm("_romashka_gpulab_l2dst1721_dc1"))

    def test_an_l2_removal_approved_while_held_waits_and_starts(self):
        """core-033 again (sixteenth full review): every L2 request was left out of the waiting changes, to leave alone
        an addition still without its VLAN - so an approved L2 removal that was held never started and Run answered
        404. Only that addition waits on purpose now."""
        from datetime import timedelta

        from django.utils import timezone

        from tenants import netops
        from tenants.models import L2Channel, NetworkRequest

        ch, mgmt, _ctl, _req = self.l2_on_the_management_platform()
        # an addition still without its VLAN is not "waiting"
        plan = NetworkRequest.objects.create(tenant=self.t, kind="l2", action="add", state="approved")
        L2Channel.objects.create(tenant=self.t, site="dc1", name="external", vni=L2Channel.next_vni(), request=plan)
        held = "the platform is read only"
        rm = self.ask(kind="l2", action="delete", target=str(ch.vni))
        with mock.patch.object(netops, "blocked_reason", return_value=held):
            rm = self.approve(rm)
        self.assertEqual((rm.state, rm.run), ("approved", ""))
        self.assertEqual(list(netops.waiting(self.t)), [rm])
        page = self.client.get(reverse("tenant_detail", args=[self.t.pk])).content.decode()
        self.assertIn(reverse("tenant_network_retry", args=[self.t.pk, rm.pk]), page)
        NetworkRequest.objects.filter(pk=rm.pk).update(decided_at=timezone.now() - timedelta(minutes=5))
        with self.patches():
            self.assertEqual(netops.start_waiting(), f"#{rm.pk} done")
        ch.refresh_from_db()
        self.assertEqual(ch.state, "removed")
        self.assertIsNone(mgmt.vm("_romashka_gpulab_l2src1721_dc1"))

    def test_l2_channel_on_the_management_platform(self):
        from inventory.models import PlatformControl
        from tenants.models import L2Channel, NetworkRequest

        ch, mgmt, ctl, req = self.l2_on_the_management_platform()
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
            self.world.vnets[self.world.vnet("_t_romashka_gpulab-lan2_dc1")]["gname"] = self.t.group_name
            self.client.force_login(self.adm)
            self.client.post(
                reverse("tenant_l2_network", args=[self.t.pk, ch.pk]), {"tenant_network": "_t_romashka_gpulab-lan2_dc1"}
            )
        mv = NetworkRequest.objects.filter(tenant=self.t, action="network").get()
        self.assertEqual((mv.state, mv.run), ("approved", "done"), (mv.error, mv.steps))
        ch.refresh_from_db()
        self.assertEqual((ch.tenant_network, ch.state), ("_t_romashka_gpulab-lan2_dc1", "active"))
        # core-008: another project's network is refused, however the request names it
        self.world.add_vnet(
            888, "_t_other_lan_dc1", [{"TYPE": "IP4", "IP": "10.8.8.1", "SIZE": 9}], gname="tenant_other"
        )
        with self.patches():
            self.client.force_login(self.adm)
            self.client.post(
                reverse("tenant_l2_network", args=[self.t.pk, ch.pk]), {"tenant_network": "_t_other_lan_dc1"}
            )
        self.assertEqual(NetworkRequest.objects.filter(tenant=self.t, action="network").count(), 1)
        ch.refresh_from_db()
        self.assertEqual(ch.tenant_network, "_t_romashka_gpulab-lan2_dc1")
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
        self.assertRegex(r.content.decode(), r'<select name="fe_mode"[^>]* disabled')  # fixed: the platform is deployed
        from inventory.edit_views import PlatformForm

        p = Platform.objects.get(key="dc1.cloud")
        f = PlatformForm(
            {"site": "dc1", "name": "cloud", "kind": "shared", "state": "deployed", "fe_mode": "local"}
            | {f"fe_ip{i}": f"192.0.2.3{i}" for i in (1, 2, 3)},  # the nodes' IPs stay editable and required
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
    test_a_replacement_that_failed_after_the_swap_is_retried_to_the_end = None
    test_a_decision_made_meanwhile_stands_and_a_run_is_taken_once = None
    test_a_swap_is_all_or_nothing_and_a_half_done_one_is_finished = None
    test_a_change_for_another_site_is_not_made_on_the_projects_site = None
    test_a_change_approved_while_blocked_waits_and_runs_once = None
    test_blocked_waiting_changes_do_not_hold_up_the_others = None
    test_a_projects_deletion_removes_its_l2_channels_on_the_management_platform = None
    test_an_l2_removal_approved_while_held_waits_and_starts = None
    test_corporate_network_added_and_deleted = None
    test_l2_channel_added_and_deleted = None

    def test_an_igw_change_deferred_by_another_change_says_so(self):
        """commit-111: the igw replacement while another change of the project runs - not started, and said so."""
        from tenants import netops

        self.deploy()
        self.client.force_login(self.adm)
        with self.patches(), mock.patch.object(netops, "busy", return_value=True):
            r = self.client.post(
                reverse("tenant_network_request", args=[self.t.pk]),
                {"kind": "igw", "action": "replace", "igw_type": "onevr"},
                follow=True,
            )
        req = self.t.network_requests.filter(kind="igw").first()
        self.assertEqual((req.state, req.run), ("approved", ""))
        self.assertContains(r, "starts by itself after it")
        self.assertNotContains(r, "The igw is being replaced")

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


class DeferredStarts(_TC):
    """commit-111: a change deferred by another change of the same project (netops.start() == BUSY) is not reported as
    under way - a never started one starts by itself afterwards, a failed one waits for Retry."""

    def req(self, run):
        from tenants.models import NetworkRequest

        r = mock.MagicMock()
        r.run = run
        r.state = NetworkRequest.State.APPROVED
        return r

    def test_l2_start_is_true_only_when_it_took_the_change(self):
        from tenants import netops, network_views
        from tenants.models import L2Channel, NetworkRequest

        for run, said in ((NetworkRequest.Run.NONE, "info"), (NetworkRequest.Run.FAILED, "warning")):
            req = self.req(run)
            ch = mock.MagicMock(state=L2Channel.State.PLANNED, vlan=1721, tenant_network="lan", request=req)
            with (
                mock.patch.object(netops, "blocked_reason", return_value=""),
                mock.patch.object(netops, "start", return_value=netops.BUSY),
                mock.patch.object(network_views, "messages") as msgs,
            ):
                self.assertFalse(network_views.l2_maybe_start(ch, None, object()))
            text = str(getattr(msgs, said).call_args.args[1])
            self.assertIn("Another network change of this project is running", text)
            msgs.info.assert_called_once() if said == "info" else msgs.info.assert_not_called()
        req = self.req(NetworkRequest.Run.NONE)
        ch = mock.MagicMock(state=L2Channel.State.PLANNED, vlan=1721, tenant_network="lan", request=req)
        with (
            mock.patch.object(netops, "blocked_reason", return_value=""),
            mock.patch.object(netops, "start", return_value=req),
            mock.patch.object(network_views, "messages"),
        ):
            self.assertTrue(network_views.l2_maybe_start(ch, None, object()))

    def test_the_message_follows_the_requests_state(self):
        from tenants import network_views
        from tenants.models import NetworkRequest

        with mock.patch.object(network_views, "messages") as msgs:
            network_views._deferred(object(), self.req(NetworkRequest.Run.NONE))
            network_views._deferred(object(), self.req(NetworkRequest.Run.FAILED))
            network_views._deferred(None, self.req(NetworkRequest.Run.NONE))  # a background start: nothing to say
        self.assertIn("starts by itself after it", str(msgs.info.call_args.args[1]))
        self.assertIn("press Retry when it is done", str(msgs.warning.call_args.args[1]))
        self.assertEqual((msgs.info.call_count, msgs.warning.call_count), (1, 1))


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
