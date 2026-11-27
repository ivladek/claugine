"""T36 B3a (owner 2026-11-25, states QS, rules v2 C8): the state of each VM of a site service - planned → deployed ⇄
non-compliant → retired; the service's state follows its VMs'."""

from cryptography.fernet import Fernet
from django.test import TestCase, override_settings

from . import tests_base_deploy as tbd  # its world only (not its tests: never imported by name here)


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode(), DEPLOY_ASYNC=False)
class States(TestCase):
    setUp = tbd.BaseDeploy.setUp
    run_redeploy = tbd.BaseDeploy.run_redeploy

    def set(self, *states):
        from inventory.models import CoreServiceNode

        self.svc.nodes.exclude(pk=self.node.pk).delete()
        nodes = [self.node] + [
            CoreServiceNode.objects.create(service=self.svc, name=f"vm{i}", address=f"10.0.101.{30 + i}")
            for i in range(len(states) - 1)
        ]
        for n, st in zip(nodes, states, strict=True):
            CoreServiceNode.objects.filter(pk=n.pk).update(state=st)
        self.svc.refresh_from_db()
        return self.svc.sync_state()

    def test_the_service_follows_its_vms(self):
        self.assertEqual(self.set("planned", "planned"), "planned")
        self.assertEqual(self.set("deployed"), "deployed")
        self.assertEqual(self.set("deployed", "planned"), "non-compliant")  # QB3-1: made in part
        self.assertEqual(self.set("deployed", "retired"), "deployed")
        self.assertEqual(self.set("non-compliant", "deployed"), "non-compliant")

    def test_a_retired_service_stays_retired(self):
        from inventory.models import CoreService

        CoreService.objects.filter(pk=self.svc.pk).update(state="retired")
        self.assertEqual(self.set("deployed"), "retired")

    def test_a_redeploy_makes_the_vm_deployed_and_a_rollback_puts_its_state_back(self):
        from inventory.models import CoreServiceNode

        CoreServiceNode.objects.filter(pk=self.node.pk).update(state="non-compliant")
        t = self.run_redeploy()
        self.assertEqual(t.state, "done", t.error)
        self.node.refresh_from_db()
        self.svc.refresh_from_db()
        self.assertEqual((self.node.state, self.svc.state), ("deployed", "deployed"))
        t = self.run_redeploy(how="rollback")
        self.assertEqual(t.state, "done", t.error)
        self.node.refresh_from_db()
        self.svc.refresh_from_db()
        self.assertEqual((self.node.state, self.svc.state), ("non-compliant", "non-compliant"))

    def test_failed_checks_leave_the_state(self):
        from inventory.models import CoreServiceNode

        CoreServiceNode.objects.filter(pk=self.node.pk).update(state="planned")
        t = self.run_redeploy(checks_ok=False)
        self.assertEqual(t.state, "failed")
        self.node.refresh_from_db()
        self.assertEqual(self.node.state, "planned")

    def platform(self, *vms, key="dc1.mgmt"):
        from inventory.models import PlatformControl

        PlatformControl.objects.filter(key=key).update(vms=[{"name": self.node.name, **v} for v in vms])

    def read_as(self, state, vm=None, checks=(), deployed=None, fresh=("dc1.mgmt",), facts=None):
        from inventory import core_services as cs
        from inventory.models import CoreServiceNode

        self.platform(*([vm] if vm else []))
        CoreServiceNode.objects.filter(pk=self.node.pk).update(
            state=state, deployed_vm_id=deployed, facts={"checks": list(checks), **(facts or {})}
        )
        self.node.refresh_from_db()
        why = cs.judge(self.node, set(fresh))
        self.node.refresh_from_db()
        return self.node.state, why

    def test_read_marks_a_differing_vm_non_compliant_and_never_deployed(self):
        vm = {"id": 70, "vcpu": 2, "ram_gb": 8, "disks_gb": [30]}
        self.assertEqual(self.read_as("deployed", vm, deployed=70), ("deployed", []))
        st, why = self.read_as("deployed", vm, deployed=71)
        self.assertEqual(st, "non-compliant")
        self.assertIn("not the one made", why[0])
        st, why = self.read_as("deployed", {**vm, "vcpu": 4}, deployed=70)
        self.assertEqual(st, "non-compliant")
        self.assertIn("vCPU 4", why[0])
        st, _why = self.read_as("deployed", vm, checks=[{"check": "unit gdm3", "ok": False}], deployed=70)
        self.assertEqual(st, "non-compliant")
        self.assertEqual(self.read_as("deployed", None, deployed=70)[0], "non-compliant")  # gone
        self.assertEqual(self.read_as("non-compliant", vm, deployed=70), ("non-compliant", []))  # never back
        self.assertEqual(self.read_as("planned"), ("planned", []))
        self.assertEqual(self.read_as("planned", vm)[0], "non-compliant")  # a VM the portal did not make

    def test_read_leaves_a_vm_with_a_redeploy_open(self):
        vm = {"id": 72, "vcpu": 2, "ram_gb": 8, "disks_gb": [30]}
        open_ = {"redeploy": {"old_id": 70}}
        self.assertEqual(self.read_as("deployed", vm, deployed=70, facts=open_), ("deployed", []))
        self.assertEqual(self.read_as("deployed", vm, deployed=None, facts=open_), ("deployed", []))

    def test_502_a_finished_redeploy_keeping_its_old_vm_is_judged(self):
        vm = {"id": 71, "vcpu": 4, "ram_gb": 8, "disks_gb": [30]}
        st, why = self.read_as("deployed", vm, deployed=71, facts={"redeploy": {"old_id": 70}})
        self.assertEqual(st, "non-compliant")
        self.assertIn("vCPU 4", why[0])

    def test_501_a_platform_not_read_now_proves_nothing_about_the_vm(self):
        self.assertEqual(self.read_as("deployed", None, deployed=70, fresh=()), ("deployed", []))
        st, _why = self.read_as("deployed", None, deployed=70, checks=[{"check": "unit x", "ok": False}], fresh=())
        self.assertEqual(st, "non-compliant")  # a check over ssh still counts

    def test_504_never_another_platforms_vm_of_the_same_name(self):
        from inventory.models import PlatformControl

        PlatformControl.objects.create(key="dc1.cloud", vms=[{"name": self.node.name, "id": 70, "vcpu": 2}])
        st, why = self.read_as("deployed", None, deployed=70, fresh=("dc1.mgmt", "dc1.cloud"))
        self.assertEqual(st, "non-compliant")
        self.assertIn("not on the platform", why[0])

    def test_500_a_whole_read_leaves_the_service_with_its_vms_state(self):
        from unittest import mock

        from accounts import tasks
        from inventory import core_services as cs
        from inventory.models import CoreServiceNode

        CoreServiceNode.objects.filter(pk=self.node.pk).update(state="deployed", deployed_vm_id=70)
        self.svc.sync_state()

        def vms(c):
            c.vms = [{"name": self.node.name, "id": 71, "vcpu": 2, "ram_gb": 8, "disks_gb": [30]}]
            c.save(update_fields=["vms"])
            return c.vms

        def node(n, site):
            CoreServiceNode.objects.filter(pk=n.pk).update(facts={"checks": []})
            n.facts = {"checks": []}
            return True

        with (
            mock.patch.object(cs, "read_platform_vms", side_effect=vms),
            mock.patch.object(cs, "read_init_flags", return_value=0),
            mock.patch.object(cs, "read_node", side_effect=node),
        ):
            t = tasks.start("core services read", "test", lambda task: cs.read(task, "dc1"))
        self.assertEqual(t.state, "done", t.error)
        self.node.refresh_from_db()
        self.svc.refresh_from_db()
        self.assertEqual((self.node.state, self.svc.state), ("non-compliant", "non-compliant"))

    def test_a_judgement_on_a_vm_redeployed_meanwhile_is_dropped(self):
        from inventory import core_services as cs
        from inventory.models import CoreServiceNode

        CoreServiceNode.objects.filter(pk=self.node.pk).update(state="deployed", deployed_vm_id=71, facts={})
        self.node.refresh_from_db()
        CoreServiceNode.objects.filter(pk=self.node.pk).update(deployed_vm_id=72)  # a redeploy ended in between
        self.assertTrue(cs.judge(self.node, {"dc1.mgmt"}))
        self.node.refresh_from_db()
        self.assertEqual(self.node.state, "deployed")


class Migration0123(TestCase):
    def test_existing_vms_take_their_service_state_or_are_non_compliant(self):
        from django.apps import apps
        from django.utils.module_loading import import_string

        from inventory.models import CoreService, CoreServiceNode, Site

        forward = import_string("inventory.migrations.0123_t36_node_state.forward")
        site = Site.objects.create(code="dc9", timezone="Asia/Almaty")
        made = {}
        for st in ("planned", "deployed", "non-compliant", "retired"):
            svc = CoreService.objects.create(site=site, name=f"s-{st}", state=st)
            made[st] = CoreServiceNode.objects.create(service=svc, name=f"n-{st}", address="x")
        forward(apps, None)
        got = {k: CoreServiceNode.objects.get(pk=n.pk).state for k, n in made.items()}
        self.assertEqual(
            got,
            {
                "planned": "planned",
                "deployed": "non-compliant",
                "non-compliant": "non-compliant",
                "retired": "retired",
            },
        )


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode(), DEPLOY_ASYNC=False)
class AddByRules(TestCase):
    """T36 B3b (rules v2 A6, B5, C4; owner 2026-11-25 17:40): the primary site, the ns split, onefe only for another
    platform, claugine, the hosting platform."""

    setUp0 = tbd.BaseDeploy.setUp

    def setUp(self):
        from inventory.models import Site

        self.setUp0()
        Site.objects.filter(pk=self.site.pk).update(primary=True, dns_servers=["10.0.101.3", "10.0.101.4"])

    def form(self, stype, profile=None, **data):
        from inventory import core_types as ct

        f = ct.FORMS[stype](data, site="dc1", profile=profile)
        self.assertTrue(f.is_valid(), f.errors)
        return f

    def test_the_first_site_is_primary_and_stays(self):
        from unittest import mock

        from inventory import ops
        from inventory.models import Site

        self.enterprise = mock.patch.object(ops, "_standard", return_value=False)  # commit-521: several sites
        self.enterprise.start()
        self.addCleanup(self.enterprise.stop)

        ops.site_save(None, "dc2", {"number": 82}, new=True)
        self.assertFalse(Site.objects.get(code="dc2").primary)
        Site.objects.update(primary=False)
        ops.site_save(None, "dc3", {"number": 83}, new=True)
        self.assertTrue(Site.objects.get(code="dc3").primary)
        s = Site.objects.get(code="dc3")
        s.city = "Almaty"
        Site.objects.filter(pk=s.pk).update(primary=False)
        s.save()  # a row read before keeps nothing of primary
        self.assertFalse(Site.objects.get(code="dc3").primary)
        Site.objects.filter(code="dc3").update(primary=True)
        with self.assertRaisesMessage(ops.EditError, "primary site"):
            ops.site_delete(None, "dc3")

    def test_migration_0124_takes_zk1_else_the_first_site(self):
        from django.apps import apps
        from django.utils.module_loading import import_string

        from inventory.models import Site

        forward = import_string("inventory.migrations.0124_t36_primary_site.forward")
        Site.objects.update(primary=False)
        Site.objects.create(code="zk1", timezone="Asia/Almaty")
        forward(apps, None)
        self.assertEqual(list(Site.objects.filter(primary=True).values_list("code", flat=True)), ["zk1"])
        Site.objects.filter(code="zk1").delete()
        forward(apps, None)
        self.assertEqual(list(Site.objects.filter(primary=True).values_list("code", flat=True)), ["dc1"])

    def test_ns_the_primary_site_has_two_each_with_its_own_server(self):
        from inventory import core_types as ct
        from inventory.models import ServiceProfile, Site

        p1, p2 = ServiceProfile.objects.get(key="ns-primary"), ServiceProfile.objects.get(key="ns-secondary")
        ServiceProfile.objects.filter(pk__in=[p1.pk, p2.pk]).update(vms=1)
        p1.refresh_from_db()
        p2.refresh_from_db()
        a = ct.save("dc1", "ns", self.form("ns", p1, vcpu=1, ram_gb=2, disk_gb=10), profile=p1)
        b = ct.save("dc1", "ns", self.form("ns", p2, vcpu=1, ram_gb=2, disk_gb=10), profile=p2)
        self.assertEqual((a.name, [n.ips for n in a.nodes.all()]), ("ns-primary", [["10.0.101.3"]]))
        self.assertEqual((b.name, [n.ips for n in b.nodes.all()]), ("ns-secondary", [["10.0.101.4"]]))
        with self.assertRaisesMessage(ct.DefinitionError, "name server service"):
            ct.save("dc1", "ns", self.form("ns", p1, vcpu=1, ram_gb=2, disk_gb=10), profile=p1)
        Site.objects.filter(pk=self.site.pk).update(primary=False)
        self.assertEqual(ct.ns_limit("dc1"), 1)

    def test_onefe_never_for_the_mgmt_or_simple_platform(self):
        from inventory import core_types as ct

        self.mgmt.fe = {
            "type": "vm",
            "nodes": ["a", "b", "c"],
            "node_ips": ["10.0.101.11", "10.0.101.12", "10.0.101.13"],
        }
        self.mgmt.save()
        f = ct.FORMS["onefe"](site="dc1")
        self.assertNotIn(str(self.mgmt.pk), [v for v, _l in f.fields["platform"].choices])
        with self.assertRaisesMessage(ct.DefinitionError, "its front end runs on its own hosts"):
            ct.definition("dc1", "onefe", {"platform": self.mgmt.pk})

    def test_claugine_first_on_the_primary_site_second_with_enterprise_elsewhere(self):
        from unittest import mock

        from inventory import core_types as ct
        from inventory.models import Cluster, Platform, Site

        Site.objects.filter(pk=self.site.pk).update(primary=False)
        with self.assertRaisesMessage(ct.DefinitionError, "primary site"):
            ct.definition("dc1", "claugine", {"address": "10.0.101.50"})
        Site.objects.filter(pk=self.site.pk).update(primary=True)
        svc = ct.save("dc1", "claugine", self.form("claugine", address="10.0.101.50", vcpu=4, ram_gb=8, disk_gb=50))
        self.assertEqual((svc.name, svc.nodes.get().ips, svc.state), ("claugine", ["10.0.101.50"], "planned"))
        with mock.patch.object(ct, "second_node_allowed", return_value=False):
            self.assertIn("Enterprise", ct.claugine_why("dc1", self.mgmt))
        with mock.patch.object(ct, "second_node_allowed", return_value=True):
            self.assertIn("another platform", ct.claugine_why("dc1", self.mgmt))
            other = Platform.objects.create(key="dc1.m2", site=self.site, name="m2", kind="mgmt")
            Cluster.objects.create(platform=other, name="m2_dc1", kind="mgmt")
            self.assertEqual(ct.claugine_why("dc1", other), "")

    def test_the_hosting_platform_is_mgmt_or_simple(self):
        from inventory import core_types as ct
        from inventory.models import Platform

        shared = Platform.objects.create(key="dc1.cloud", site=self.site, name="cloud", kind="shared")
        f = ct.FORMS["ns"](site="dc1")
        self.assertNotIn(str(shared.pk), [v for v, _l in f.fields[ct.HOST_PLATFORM].choices])
        self.assertIn(str(self.mgmt.pk), [v for v, _l in f.fields[ct.HOST_PLATFORM].choices])


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode(), DEPLOY_ASYNC=False)
class Snapd(TestCase):
    """T36 B3c (rules v2 C1, C8): Read reports snapd; a VM with it is non-compliant; Remove snapd in place."""

    setUp = tbd.BaseDeploy.setUp

    def test_read_reports_it(self):
        from inventory import core_services as cs

        self.assertIn("##snapd", cs.READ_SCRIPT)
        self.assertEqual(cs.parse("##host\nx\n##snapd\ninstalled\n")["snapd"], "installed")
        self.assertNotIn("snapd", cs.parse("##host\nx\n"))

    def vm(self, snapd, deployed=70):
        from inventory.models import CoreServiceNode, PlatformControl

        PlatformControl.objects.filter(key="dc1.mgmt").update(
            vms=[{"name": self.node.name, "id": 70, "vcpu": 2, "ram_gb": 8, "disks_gb": [30]}]
        )
        CoreServiceNode.objects.filter(pk=self.node.pk).update(
            state="deployed", deployed_vm_id=deployed, facts={"checks": [], "snapd": snapd}
        )
        self.node.refresh_from_db()

    def test_a_vm_with_snapd_differs_from_its_os(self):
        from inventory import core_services as cs

        self.vm("installed")
        self.assertEqual(cs.compliance(self.node), ["snapd is installed: Remove snapd"])
        self.vm("absent")
        self.assertEqual(cs.compliance(self.node), [])

    def remove(self, refresh_errors=(), read_ok=True, after="absent"):
        from unittest import mock

        from accounts import tasks
        from inventory import snapd

        scripts = []

        def ssh(n):
            def run(script, timeout=300):
                scripts.append(script)
                return "##snapd\nabsent\n"

            return run

        def read(n, site):
            from inventory.models import CoreServiceNode

            facts = {**(n.facts or {}), "snapd": after}
            CoreServiceNode.objects.filter(pk=n.pk).update(facts=facts, facts_error="" if read_ok else "ssh: timed out")
            return read_ok

        with (
            mock.patch("inventory.hostlogging.node_ssh", side_effect=ssh),
            mock.patch("inventory.core_services.refresh_vms", return_value=list(refresh_errors)),
            mock.patch("inventory.core_services.read_node", side_effect=read),
        ):
            t = tasks.start("core VM redeploy", "test", lambda task: snapd.remove(task, self.node))
        self.assertIn("apt-get purge -y -q snapd", scripts[0])
        self.assertIn("systemctl mask", scripts[0])
        self.node.refresh_from_db()
        self.svc.refresh_from_db()
        return t

    def test_removed_in_place_and_deployed_when_nothing_else_differs(self):
        from inventory.models import CoreServiceNode

        self.vm("installed")
        CoreServiceNode.objects.filter(pk=self.node.pk).update(state="non-compliant")
        self.node.refresh_from_db()
        t = self.remove()
        self.assertEqual(t.state, "done", t.error)
        self.assertEqual((self.node.state, self.svc.state), ("deployed", "deployed"))

    def test_a_vm_the_portal_did_not_make_stays_non_compliant(self):
        from inventory.models import CoreServiceNode

        self.vm("installed", deployed=None)
        CoreServiceNode.objects.filter(pk=self.node.pk).update(state="non-compliant")
        self.node.refresh_from_db()
        t = self.remove()
        self.assertEqual(t.state, "done", t.error)
        self.assertEqual(self.node.state, "non-compliant")

    def test_a_platform_not_read_keeps_it_non_compliant(self):
        from inventory.models import CoreServiceNode

        self.vm("installed")
        CoreServiceNode.objects.filter(pk=self.node.pk).update(state="non-compliant")
        self.node.refresh_from_db()
        t = self.remove(refresh_errors=[("dc1.mgmt", "timeout")])
        self.assertEqual(t.state, "done", t.error)
        self.assertEqual(self.node.state, "non-compliant")

    def test_the_page_offers_it_on_a_vm_with_snapd(self):
        from django.urls import reverse

        from accounts.models import Kind, User

        sa = User.objects.create_user(
            "sa@corp.kz",
            "Str0ng-PASS-123",
            kind=Kind.STAFF,
            email_verified=True,
            is_super_admin=True,
            totp_enabled=True,
        )
        self.client.force_login(sa)
        s = self.client.session
        s["otp_verified"] = True
        s.save()
        url = reverse("infra_site", args=["dc1"]) + "?tab=core"
        self.vm("absent")
        self.assertNotIn("Remove snapd</button>", self.client.get(url).content.decode())
        self.vm("installed")
        page = self.client.get(url).content.decode()
        self.assertIn(reverse("core_snapd", args=["dc1", self.svc.pk, self.node.pk]), page)

    def test_503_a_failed_read_or_snapd_still_there_keeps_it_non_compliant(self):
        from inventory.models import CoreServiceNode

        for kw in ({"read_ok": False}, {"after": "unmasked"}):
            self.vm("installed")
            CoreServiceNode.objects.filter(pk=self.node.pk).update(state="non-compliant")
            self.node.refresh_from_db()
            self.remove(**kw)
            self.assertEqual(self.node.state, "non-compliant", kw)

    def test_505_every_unit_probed_and_masked_and_the_scripts_parse(self):
        import subprocess

        from inventory import snapd

        for u in snapd.UNITS:
            self.assertIn(u, snapd.PROBE)
            self.assertIn(u, snapd.REMOVE)
        self.assertIn("LoadState", snapd.PROBE)
        self.assertTrue(snapd.REMOVE.rstrip().endswith("fi"))  # it ends with the probe
        for text in (snapd.PROBE, snapd.REMOVE):
            subprocess.run(["bash", "-n"], input=text, text=True, check=True)


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode(), DEPLOY_ASYNC=False)
class BlankRoles(TestCase):
    """T36 B3d: a site service profile 0122 left without roles is shown to be filled, with its services' VM counts."""

    setUp0 = tbd.BaseDeploy.setUp

    def setUp(self):
        from accounts.models import Kind, User

        self.setUp0()
        sa = User.objects.create_user(
            "sa@corp.kz",
            "Str0ng-PASS-123",
            kind=Kind.STAFF,
            email_verified=True,
            is_super_admin=True,
            totp_enabled=True,
        )
        self.client.force_login(sa)
        s = self.client.session
        s["otp_verified"] = True
        s.save()

    def test_listed_to_fill(self):
        from django.urls import reverse

        from inventory.models import ServiceProfile

        p = ServiceProfile.objects.get(key="admin")
        self.svc.profile = p
        self.svc.save()
        url = reverse("infra_services")
        page = self.client.get(url).content.decode()
        self.assertNotIn("roles to fill", page)
        ServiceProfile.objects.filter(pk=p.pk).update(roles=[])
        page = self.client.get(url).content.decode()
        self.assertIn("roles to fill", page)
        self.assertIn("its services have 1 VMs", page)


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode(), DEPLOY_ASYNC=False)
class ReviewFixes506to512(TestCase):
    """Codex commit-506 .. commit-512 (owner 2026-11-25 18:10)."""

    setUp0 = tbd.BaseDeploy.setUp

    def setUp(self):
        from inventory.models import Site

        self.setUp0()
        Site.objects.filter(pk=self.site.pk).update(primary=True, dns_servers=["10.0.101.3", "10.0.101.4"])
        self.site.refresh_from_db()

    def ns(self, key):
        from inventory import core_types as ct
        from inventory.models import ServiceProfile

        p = ServiceProfile.objects.get(key=key)
        f = ct.FORMS["ns"]({"vcpu": 1, "ram_gb": 2, "disk_gb": 10}, site="dc1", profile=p)
        self.assertTrue(f.is_valid(), f.errors)
        return ct.save("dc1", "ns", f, profile=p)

    def test_507_each_profile_takes_its_own_server_in_any_order(self):
        from inventory import core_types as ct
        from inventory.models import ServiceProfile, Site

        b = self.ns("ns-secondary")
        a = self.ns("ns-primary")
        self.assertEqual([n.ips for n in b.nodes.all()], [["10.0.101.4"]])
        self.assertEqual([n.ips for n in a.nodes.all()], [["10.0.101.3"]])
        self.assertIn("non-primary", ct.ns_why("dc1", ServiceProfile.objects.get(key="ns-site")))
        Site.objects.filter(pk=self.site.pk).update(primary=False)
        self.assertIn("primary site", ct.ns_why("dc1", ServiceProfile.objects.get(key="ns-secondary")))
        with self.assertRaisesMessage(ct.DefinitionError, "Not a name server here"):
            self.ns("ns-secondary")

    def test_506_a_two_vm_name_server_is_split_keeping_its_vms(self):
        from inventory import core_types as ct
        from inventory.models import CoreService, CoreServiceNode, ServiceProfile

        old = CoreService.objects.create(
            site=self.site, name="ns", stype="ns", platform=self.mgmt, state="non-compliant", vcpu=1, ram_gb=2
        )
        a = CoreServiceNode.objects.create(service=old, name="core-ns1_dc1", address="x", ips=["10.0.101.3"])
        b = CoreServiceNode.objects.create(
            service=old, name="core-ns2_dc1", address="y", ips=["10.0.101.4"], deployed_vm_id=91, state="deployed"
        )
        self.assertIn("has its name server service", ct.ns_why("dc1", ServiceProfile.objects.get(key="ns-secondary")))
        self.assertEqual(ct.ns_split_why(old), "")
        new = ct.ns_split(old)
        old.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((old.profile.key, new.profile.key, new.name), ("ns-primary", "ns-secondary", "ns-secondary"))
        self.assertEqual((b.service_id, b.deployed_vm_id, b.state), (new.pk, 91, "deployed"))
        self.assertEqual(list(old.nodes.values_list("pk", flat=True)), [a.pk])
        self.assertEqual(new.state, "deployed")
        self.assertTrue(ct.ns_split_why(old))  # once
        d = ct.definition("dc1", "ns", {"vcpu": 1, "ram_gb": 2}, existing=old)  # edits keep each its server
        self.assertEqual([n["ips"] for n in d["nodes"]], [["10.0.101.3"]])

    def test_508_site_creation_and_delete_take_the_installation_lock(self):
        from unittest import mock

        from inventory import ops

        with (
            mock.patch.object(ops, "installation_lock") as lock,
            mock.patch.object(ops, "_standard", return_value=False),
        ):
            ops.site_save(None, "dc2", {"number": 82}, new=True)
            ops.site_delete(None, "dc2")
        self.assertEqual(lock.call_count, 2)

    def test_509_a_second_node_only_with_a_confirmed_two_node_enterprise_licence(self):
        from unittest import mock

        from accounts import edition
        from inventory import core_types as ct

        with mock.patch.object(edition, "standard_build", return_value=False):
            with mock.patch.object(edition, "current", return_value=""):
                self.assertFalse(ct.second_node_allowed())  # not said
            with (
                mock.patch.object(edition, "current", return_value="enterprise"),
                mock.patch.object(edition, "allows", return_value=False),
            ):
                self.assertFalse(ct.second_node_allowed())  # a one-node licence
            with (
                mock.patch.object(edition, "current", return_value="enterprise"),
                mock.patch.object(edition, "allows", return_value=True),
            ):
                self.assertTrue(ct.second_node_allowed())

    def test_510_511_an_unlinked_service_counts_for_one_profile_and_no_empty_layout(self):
        from inventory import services
        from inventory.models import CoreService, CoreServiceNode, ServiceProfile

        old = CoreService.objects.create(site=self.site, name="ns", stype="ns", platform=self.mgmt)
        CoreServiceNode.objects.create(service=old, name="n1", address="x")
        own = ServiceProfile.of_type("ns")
        other = ServiceProfile.objects.get(key="ns-secondary")
        self.assertEqual(services.unlinked_profile(old), own)
        self.assertIn(old, services._instances(own))
        self.assertNotIn(old, services._instances(other))
        empty = CoreService.objects.create(site=self.site, name="ns-e", stype="ns", platform=self.mgmt, profile=other)
        self.assertEqual(services._instances(other), [empty])
        self.assertEqual(services.layout_counts([empty, old]), [1])  # commit-511

    def test_512_the_probe_never_says_absent_when_systemd_did_not_answer(self):
        import os
        import subprocess
        import tempfile

        from inventory import snapd

        def run(systemctl, installed=False):
            with tempfile.TemporaryDirectory(dir=os.environ.get("HOME")) as d:
                with open(f"{d}/systemctl", "w") as fh:
                    fh.write("#!/bin/bash\n" + systemctl + "\n")
                with open(f"{d}/dpkg-query", "w") as fh:
                    fh.write("#!/bin/bash\n" + ("echo 'install ok installed'" if installed else "exit 1") + "\n")
                os.chmod(f"{d}/systemctl", 0o755)
                os.chmod(f"{d}/dpkg-query", 0o755)
                env = {**os.environ, "PATH": f"{d}:/usr/bin:/bin"}
                out = subprocess.run(["bash", "-c", snapd.PROBE], env=env, capture_output=True, text=True).stdout
                return out.split("##snapd\n", 1)[1].strip()

        self.assertEqual(run("echo masked"), "absent")
        self.assertEqual(run('[ "$5" = snapd.socket ] && echo loaded || echo not-found'), "unmasked")
        self.assertEqual(run("exit 1"), "unknown")
        self.assertEqual(run("echo bad-setting"), "unknown")
        self.assertEqual(run("echo masked", installed=True), "installed")


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode(), DEPLOY_ASYNC=False)
class ReviewFixes513to517(TestCase):
    """Codex commit-513 .. commit-517 (owner 2026-11-25 18:25)."""

    setUp0 = tbd.BaseDeploy.setUp

    def setUp(self):
        from inventory.models import Site

        self.setUp0()
        Site.objects.filter(pk=self.site.pk).update(primary=True, dns_servers=["10.0.101.3", "10.0.101.4"])
        self.site.refresh_from_db()

    def form(self, profile=None):
        from inventory import core_types as ct

        f = ct.FORMS["ns"]({"vcpu": 1, "ram_gb": 2, "disk_gb": 10}, site="dc1", profile=profile)
        self.assertTrue(f.is_valid(), f.errors)
        return f

    def test_513_the_typed_route_uses_the_default_profiles_slot(self):
        from inventory import core_types as ct
        from inventory.models import ServiceProfile

        ServiceProfile.objects.filter(key="ns-primary").update(vms=2)  # a profile 0122 kept at two VMs
        a = ct.save("dc1", "ns", self.form())
        self.assertEqual((a.profile.key, [n.ips for n in a.nodes.all()]), ("ns-primary", [["10.0.101.3"]]))
        p2 = ServiceProfile.objects.get(key="ns-secondary")
        b = ct.save("dc1", "ns", self.form(p2), profile=p2)
        self.assertEqual([n.ips for n in b.nodes.all()], [["10.0.101.4"]])

    def test_514_a_slot_is_one_profiles(self):
        from inventory import services
        from inventory.models import ServiceProfile

        p = ServiceProfile.objects.create(key="ns-other", stype="ns", vms=1)
        data = {k: v for k, v in services.profile_initial(p).items() if v is not None}
        data["dns_slot"] = "2"
        f = services.ProfileForm(data)
        self.assertTrue(f.is_valid(), f.errors)
        with self.assertRaisesMessage(services.ServiceError, "DNS slot 2"):
            services.save_profile(None, p.pk, f.cleaned_data)
        f = services.ProfileForm({**data, "dns_slot": ""})
        self.assertTrue(f.is_valid(), f.errors)
        services.save_profile(None, p.pk, f.cleaned_data)  # no slot: saved

    def two_vm_ns(self, ips=("10.0.101.3", "10.0.101.4")):
        from inventory.models import CoreService, CoreServiceNode

        svc = CoreService.objects.create(site=self.site, name="ns", stype="ns", platform=self.mgmt)
        for i, ip in enumerate(ips):
            CoreServiceNode.objects.create(service=svc, name=f"core-ns{i + 1}_dc1", address=ip, ips=[ip])
        return svc

    def test_515_split_needs_each_server_matched_by_one_vm(self):
        from inventory import core_types as ct
        from inventory.models import Site

        svc = self.two_vm_ns()
        Site.objects.filter(pk=self.site.pk).update(dns_servers=["10.0.101.3", "10.0.101.9"])
        svc.refresh_from_db()
        self.assertIn("10.0.101.9", ct.ns_split_why(svc))
        with self.assertRaises(ct.DefinitionError):
            ct.ns_split(svc)
        self.assertEqual(svc.nodes.count(), 2)  # nothing changed

    def test_516_split_refused_while_a_vm_is_busy_and_admission_rechecks_the_service(self):
        from django.utils import timezone

        from accounts.models import Task
        from inventory import core_types as ct
        from inventory.models import CoreServiceNode

        svc = self.two_vm_ns()
        n = svc.nodes.order_by("pk").last()
        t = Task.objects.create(kind="core VM redeploy", title="x", target_type="coreservicenode", target_id=str(n.pk))
        self.assertIn("task", ct.ns_split_why(svc))
        Task.objects.filter(pk=t.pk).update(state=Task.State.DONE)
        CoreServiceNode.objects.filter(pk=n.pk).update(
            logging_claim={"token": "t", "at": timezone.now().isoformat(), "by": "x"}
        )
        self.assertIn("operation", ct.ns_split_why(svc))
        CoreServiceNode.objects.filter(pk=n.pk).update(logging_claim={})
        new = ct.ns_split(svc)
        from unittest import mock

        from django.test import RequestFactory

        from inventory import core_views

        req = RequestFactory().post("/")
        stale = CoreServiceNode.objects.get(pk=n.pk)
        stale.service_id = svc.pk  # read before the split
        started = []
        with (
            mock.patch.object(core_views, "messages") as msgs,
            mock.patch("accounts.tasks.start", side_effect=lambda *a, **k: started.append(a)),
        ):
            core_views._start(req, stale, "core VM redeploy", "t", lambda task: None)
        self.assertEqual(started, [])
        self.assertTrue(msgs.error.called)
        self.assertEqual(CoreServiceNode.objects.get(pk=n.pk).service_id, new.pk)

    def test_517_remove_snapd_only_where_snapd_surely_is(self):
        from inventory import snapd
        from inventory.models import CoreServiceNode

        n = CoreServiceNode.objects.get(pk=self.node.pk)
        for st, want in (("installed", True), ("unmasked", True), ("unknown", False), ("absent", False)):
            n.facts = {"snapd": st}
            self.assertEqual(snapd.removable(n), want, st)
        n.facts = {"snapd": "unknown"}
        self.assertTrue(snapd.found(n))  # still non-compliant

    def test_515_two_different_vms_and_the_site_read_under_its_lock(self):
        from inventory import core_types as ct
        from inventory.models import Site

        svc = self.two_vm_ns()
        Site.objects.filter(pk=self.site.pk).update(dns_servers=["10.0.101.3", "10.0.101.3"])
        stale = type(svc).objects.get(pk=svc.pk)
        stale.site.dns_servers = ["10.0.101.3", "10.0.101.4"]  # read before the site was edited
        with self.assertRaisesMessage(ct.DefinitionError, "one address"):
            ct.ns_split(stale)
        self.assertEqual(svc.nodes.count(), 2)

    def test_516_the_first_vm_with_a_stale_profile_is_refused(self):
        from unittest import mock

        from django.test import RequestFactory

        from inventory import core_types as ct
        from inventory import core_views
        from inventory.models import CoreServiceNode

        svc = self.two_vm_ns()
        first = CoreServiceNode.objects.select_related("service").get(pk=svc.nodes.order_by("pk").first().pk)
        ct.ns_split(svc)  # the first VM stays; its service now has the slot-1 profile
        started = []
        with (
            mock.patch.object(core_views, "messages") as msgs,
            mock.patch("accounts.tasks.start", side_effect=lambda *a, **k: started.append(a)),
        ):
            core_views._start(RequestFactory().post("/"), first, "core VM redeploy", "t", lambda task: None)
        self.assertEqual(started, [])
        self.assertTrue(msgs.error.called)

    def test_518_add_uses_the_profile_as_stored_at_the_save(self):
        from inventory import core_types as ct
        from inventory.models import ServiceProfile

        p = ServiceProfile.objects.get(key="ns-primary")  # chosen on the page
        ServiceProfile.objects.filter(key="ns-primary").update(dns_slot=None)  # edited before the save
        with self.assertRaisesMessage(ct.DefinitionError, "no DNS slot"):
            ct.save("dc1", "ns", self.form(p), profile=p)
        ServiceProfile.objects.filter(key="ns-primary").update(dns_slot=2)
        svc = ct.save("dc1", "ns", self.form(p), profile=p)
        self.assertEqual([n.ips for n in svc.nodes.all()], [["10.0.101.4"]])  # the stored slot, not the page's

    def test_standard_has_one_site(self):
        """Owner 2026-11-25 18:42: "Standard liscense only for one site"."""
        from unittest import mock

        from inventory import ops

        with mock.patch.object(ops, "_standard", return_value=True):
            with self.assertRaisesMessage(ops.EditError, "one site"):
                ops.site_save(None, "dc2", {"number": 82}, new=True)
        with mock.patch.object(ops, "_standard", return_value=False):
            ops.site_save(None, "dc2", {"number": 82}, new=True)

    def test_519_a_profile_deleted_since_it_was_chosen(self):
        from inventory import core_types as ct
        from inventory.models import ServiceProfile

        p = ServiceProfile.objects.create(key="ns-gone", stype="ns", vms=1, dns_slot=None)
        f = self.form(p)
        ServiceProfile.objects.filter(pk=p.pk).delete()
        with self.assertRaisesMessage(ct.DefinitionError, "was deleted"):
            ct.save("dc1", "ns", f, profile=p)

    def test_520_the_sites_page(self):
        from unittest import mock

        from django.urls import reverse

        from accounts.models import Kind, User
        from inventory import ops

        def login(**kw):
            u = User.objects.create_user(
                f"u{User.objects.count()}@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, email_verified=True, **kw
            )
            self.client.force_login(u)
            s = self.client.session
            s["otp_verified"] = True
            s.save()

        login(is_super_admin=True, totp_enabled=True)
        url = reverse("inventory")
        with mock.patch.object(ops, "_standard", return_value=True):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200)
            self.assertContains(r, "The Standard edition has one site")
        with mock.patch.object(ops, "_standard", return_value=False):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200)
            self.assertNotContains(r, "The Standard edition has one site")
        login(totp_enabled=True)  # staff without a role
        self.assertNotEqual(self.client.get(url).status_code, 200)
