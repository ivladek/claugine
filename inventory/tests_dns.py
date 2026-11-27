"""T34 B5: Infrastructure → DNS (inventory/dns.py, dns_views.py)."""

from unittest import mock

from cryptography.fernet import Fernet
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Kind, User


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode(), DEPLOY_ASYNC=False)
class Dns(TestCase):
    def setUp(self):
        from inventory.models import CoreService, CoreServiceNode, DnsSettings, Host, Network, Platform, Site
        from tenants.models import PortalSettings

        PortalSettings.objects.filter(pk=PortalSettings.get().pk).update(internal_domain="Example.Cloud")
        self.site = Site.objects.create(code="dc1", timezone="Asia/Almaty")
        Site.objects.create(code="dc2", timezone="Asia/Almaty")
        self.core = Network.objects.create(
            site="dc1",
            name="cloud-core",
            kind="core-cloud",
            interface="bond0",
            vlan=101,
            subnet="10.0.101.0",
            prefix=24,
        )
        self.kvm = Network.objects.create(
            site="dc1", name="one-core", kind="core-kvm", interface="bond0", vlan=100, subnet="10.0.100.0", prefix=24
        )
        self.ns = CoreService.objects.create(site=self.site, name="ns", stype="ns", kind="vm", nic1_network=self.core)
        self.n1 = CoreServiceNode.objects.create(
            service=self.ns, name="core-ns1_dc1", hostname="sdc1ns1", address="10.0.101.11", ips=["10.0.101.11"]
        )
        self.n2 = CoreServiceNode.objects.create(
            service=self.ns, name="core-ns2_dc1", hostname="sdc1ns2", address="10.0.101.12", ips=["10.0.101.12"]
        )
        Host.objects.create(site=self.site, name="hdc1kvm201", ip="10.0.100.201", state="planned")
        Host.objects.create(site=self.site, name="hdc1old", ip="10.0.100.9", state="retired")
        Platform.objects.create(
            key="dc1.cloud",
            site=self.site,
            name="cloud",
            kind="shared",
            fe_vip="10.0.101.30",
            fe={"type": "vm", "hostname": "sdc1one", "nodes": ["sdc1one1"], "node_ips": ["10.0.101.31"]},
        )
        st = DnsSettings.get()
        st.primary = self.n1
        st.forwarders = ["8.8.8.8"]
        st.sites = {"dc1": {"internal": ["10.0.0.0/16"], "public": ["203.0.113.0/24"]}}
        st.save()

    def zones(self):
        from inventory import dns

        return {z.name: z for z in dns.sync_zones()}

    def test_automatic_zones_follow_the_portal(self):
        from inventory.models import DnsRecord, DnsZone
        from tenants.models import PortalSettings

        z = self.zones()
        self.assertEqual(
            sorted((n, x.kind, x.source) for n, x in z.items()),
            [
                ("100.0.10.in-addr.arpa", "reverse", f"network:{self.kvm.pk}"),
                ("101.0.10.in-addr.arpa", "reverse", f"network:{self.core.pk}"),
                ("example.cloud", "direct", "domain"),
            ],
        )
        DnsRecord.objects.create(zone=z["example.cloud"], name="www", rtype="CNAME", value="sdc1one")
        PortalSettings.objects.update(internal_domain="corp.cloud")  # renamed there, renamed here
        z = self.zones()
        self.assertIn("corp.cloud", z)
        self.assertNotIn("example.cloud", z)
        self.assertEqual(z["corp.cloud"].records.count(), 1)
        self.kvm.state = "retired"
        self.kvm.save()
        self.assertNotIn("100.0.10.in-addr.arpa", self.zones())
        self.assertFalse(DnsZone.objects.filter(name="100.0.10.in-addr.arpa").exists())

    def test_a_wide_network_gets_a_zone_per_24(self):
        from inventory import dns

        self.core.prefix = 23
        self.assertEqual(dns.reverse_zones_of(self.core), ["100.0.10.in-addr.arpa", "101.0.10.in-addr.arpa"])
        self.assertEqual(dns.ptr_label("10.0.101.11", "101.0.10.in-addr.arpa"), "11")
        self.assertEqual(dns.ptr_label("10.0.101.11", "0.10.in-addr.arpa"), "11.101")

    def test_automatic_records(self):
        from inventory import dns

        self.zones()
        auto, notes = dns.automatic()
        direct = {(r["name"], r["value"]) for r in auto["example.cloud"]}
        self.assertEqual(
            direct,
            {
                ("sdc1ns1", "10.0.101.11"),
                ("sdc1ns2", "10.0.101.12"),
                ("hdc1kvm201", "10.0.100.201"),  # planned hosts too; retired ones not
                ("sdc1one", "10.0.101.30"),
                ("sdc1one1", "10.0.101.31"),
            },
        )
        self.assertIn(
            {"name": "11", "rtype": "PTR", "value": "sdc1ns1.example.cloud.", "source": mock.ANY},
            auto["101.0.10.in-addr.arpa"],
        )
        self.assertEqual([r["name"] for r in auto["100.0.10.in-addr.arpa"]], ["201"])
        self.assertEqual(notes, [])

    def test_zone_text_and_state(self):
        from inventory import dns
        from inventory.models import DnsRecord, DnsSettings

        z = self.zones()["example.cloud"]
        DnsRecord.objects.create(zone=z, name="info", rtype="TXT", value='say "hi"', ttl=60)
        text = dns.zone_text(z, 2026112101)
        self.assertIn(
            "@ IN SOA sdc1ns1.example.cloud. hostmaster.example.cloud. ( 2026112101 3600 600 1209600 300 )", text
        )
        self.assertIn("@ IN NS sdc1ns1.example.cloud.", text)
        self.assertIn("@ IN NS sdc1ns2.example.cloud.", text)
        self.assertIn("sdc1ns1 IN A 10.0.101.11", text)
        self.assertIn('info 60 IN TXT "say \\"hi\\""', text)
        self.assertEqual(dns.state(z), "to apply")
        st = DnsSettings.get()
        st.applied = {z.name: [dns.digest(dns.zone_text(z, st.serial)), "x"]}
        st.save()
        self.assertEqual(dns.state(z), "applied")
        DnsRecord.objects.create(zone=z, name="more", rtype="A", value="10.0.101.99")
        self.assertEqual(dns.state(z), "to apply")

    def test_values_of_a_name_server(self):
        from inventory import base_deploy as bd
        from inventory import dns
        from inventory.models import DnsSettings

        self.zones()
        dns.reserve_for_primary(self.n1)
        dns.published(self.n1, True)
        p = dns.values(self.n1)
        self.assertEqual((p["role"], p["primary"]), ("primary", "10.0.101.11"))
        self.assertEqual(
            p["sites"],
            [
                {
                    "code": "dc1",
                    "internal": ["10.0.0.0/16"],
                    "public": ["203.0.113.0/24"],
                    "ns": ["10.0.101.11", "10.0.101.12"],
                },
            ],
        )
        self.assertEqual(len(p["zones"]), 3)
        self.assertTrue(all(z["text"].startswith("; made by claugine") for z in p["zones"]))
        s = dns.values(self.n2)
        self.assertEqual(s["role"], "secondary")
        self.assertEqual({z["text"] for z in s["zones"]}, {""})
        self.assertFalse([m for m in bd.missing(self.n2) if "dns" in m.lower() or "DNS" in m])
        DnsSettings.objects.update(primary=None)  # owner 2026-11-22: no primary yet - n2 would become it
        self.assertFalse([m for m in bd.missing(self.n2) if "dns" in m.lower()])
        self.assertEqual(dns.values(self.n2, reveal=False)["role"], "primary")
        DnsSettings.objects.update(primary=self.n1, forwarders=[])
        self.assertIn("dns.forwarders", bd.missing(self.n2))

    def test_the_first_name_server_made_becomes_the_primary(self):
        """owner 2026-11-22 09:59: "first deployed VM is primary, other - secondaries" - no choice in Edit"""
        from inventory import dns
        from inventory.models import DnsSettings

        DnsSettings.objects.update(primary=None)
        for n in (self.n1, self.n2):  # with no primary, any name server would become it
            v = dns.values(n, reveal=False)
            self.assertEqual((v["role"], v["first"]), ("primary", True))
        dns.reserve_for_primary(self.n2)
        dns.published(self.n2, False)  # its redeploy failed: still no primary
        self.assertIsNone(DnsSettings.get().primary_id)
        dns.reserve_for_primary(self.n2)
        dns.published(self.n2, True)
        self.assertEqual(DnsSettings.get().primary_id, self.n2.pk)
        self.assertEqual(dns.values(self.n1, reveal=False)["role"], "secondary")
        v = dns.values(self.n2, reveal=False)
        self.assertEqual((v["role"], v["first"]), ("primary", False))
        self.assertNotIn("primary", __import__("inventory.dns_views", fromlist=["x"]).SettingsForm().fields)

    def test_commit_354_an_unfinished_first_primary_holds_the_place(self):
        from inventory import base_deploy as bd
        from inventory import dns
        from inventory.models import DnsSettings

        self.zones()
        DnsSettings.objects.update(primary=None)
        dns.reserve_for_primary(self.n1, first=True)  # its redeploy began and stopped before it was recorded
        v = dns.values(self.n2, reveal=False)
        self.assertEqual((v["role"], v["waiting_for"]), ("secondary", "sdc1ns1"))
        self.assertTrue(any("has not ended" in m for m in bd.missing(self.n2)))
        with self.assertRaises(dns.DnsError):
            dns.reserve_for_primary(self.n2, first=True)
        dns.published(self.n2, False)  # another VM's end never touches n1's candidate
        self.assertEqual(DnsSettings.get().candidate["node"], self.n1.pk)
        v = dns.values(self.n1, reveal=False)
        self.assertEqual((v["role"], v["first"], v["waiting_for"]), ("primary", True, ""))
        dns.reserve_for_primary(self.n1, first=True)  # redeployed again
        dns.published(self.n1, True)
        self.assertEqual(DnsSettings.get().primary_id, self.n1.pk)
        self.assertEqual(dns.values(self.n2, reveal=False)["waiting_for"], "")

    def test_commit_354_a_failed_retry_keeps_the_pending_first_primary(self):
        """the retry reserves again, then fails before any VM change: the earlier candidate is put back"""
        from accounts import tasks
        from inventory import base_deploy as bd
        from inventory import dns
        from inventory.models import DnsSettings

        self.zones()
        DnsSettings.objects.update(primary=None)
        dns.reserve_for_primary(self.n1, first=True)
        before = DnsSettings.get().candidate

        def fails_after_prepare(*args, **kw):
            kw["prepare"]()  # the candidate reserved again, then a check before the first VM change fails
            raise bd.RedeployError("the super admin secret has no ssh key")

        with mock.patch.object(bd, "_redeploy", side_effect=fails_after_prepare):
            t = tasks.start("core VM redeploy", "retry", lambda tk: bd.redeploy(tk, self.n1))
        self.assertEqual(t.state, "failed")
        self.assertEqual(
            {k: v for k, v in DnsSettings.get().candidate.items() if k != "rev"},
            {k: v for k, v in before.items() if k != "rev"},
        )  # commit-363: only its revision is new
        self.assertEqual(dns.values(self.n2, reveal=False)["waiting_for"], "sdc1ns1")

    def test_commit_354_a_rollback_to_a_vm_made_as_the_primary_keeps_the_place(self):
        from accounts import tasks
        from inventory import base_deploy as bd
        from inventory import dns
        from inventory.models import DnsSettings

        self.zones()
        DnsSettings.objects.update(primary=None)
        dns.reserve_for_primary(self.n1, first=True)
        dns.candidate_vm(self.n1, 70)  # the first attempt made VM 70 and stopped
        dns.reserve_for_primary(self.n1, first=True)  # a retry: the earlier attempt's VM stays recorded
        self.assertEqual(DnsSettings.get().candidate["vms"], [70])
        dns.candidate_vm(self.n1, 71)  # the retry made 71 and stopped too
        with mock.patch.object(bd, "_rollback_by_hand", return_value={"old_id": 70}):  # 71 removed, 70 back
            t = tasks.start("core VM redeploy", "rollback", lambda tk: bd.rollback(tk, self.n1))
        self.assertEqual(t.state, "done", t.error)
        self.assertTrue(DnsSettings.get().candidate.get("first"))  # 70 may serve: the others still wait
        self.assertEqual(dns.values(self.n2, reveal=False)["waiting_for"], "sdc1ns1")
        # a VM made by an attempt that stopped before its id was saved: the platform shows its allocation's tag
        with (
            mock.patch.object(bd, "_rollback_by_hand", return_value={"old_id": 72}),
            mock.patch.object(dns, "first_tag_of", return_value=str(self.n1.pk)),
        ):
            t = tasks.start("core VM redeploy", "rollback", lambda tk: bd.rollback(tk, self.n1))
        self.assertTrue(DnsSettings.get().candidate.get("first"))
        with (  # a VM that cannot be read is not proof: kept
            mock.patch.object(bd, "_rollback_by_hand", return_value={"old_id": 73}),
            mock.patch.object(dns, "first_tag_of", side_effect=RuntimeError("no answer")),
        ):
            t = tasks.start("core VM redeploy", "rollback", lambda tk: bd.rollback(tk, self.n1))
        self.assertTrue(DnsSettings.get().candidate.get("first"))
        with (
            mock.patch.object(bd, "_rollback_by_hand", return_value={"old_id": 12}),  # the VM from before the portal
            mock.patch.object(dns, "first_tag_of", return_value=""),
            mock.patch.object(dns, "first_alive", return_value=[]),  # and no VM made for it is left
        ):
            t = tasks.start("core VM redeploy", "rollback", lambda tk: bd.rollback(tk, self.n1))
        self.assertEqual(DnsSettings.get().candidate, {})

    def test_commit_354_a_first_primarys_allocation_carries_its_tag(self):
        from accounts import tasks
        from inventory import base_deploy as bd
        from inventory import dns
        from inventory.models import DnsSettings
        from tenants.fakeone import World

        self.zones()
        DnsSettings.objects.update(primary=None)
        got = {}

        def stop(*a, **kw):
            got.update(kw)
            raise bd.RedeployError("stop")

        with mock.patch.object(bd, "_redeploy", side_effect=stop):
            tasks.start("core VM redeploy", "t", lambda tk: bd.redeploy(tk, self.n1))
        self.assertEqual(got["tags"], {dns.FIRST_TAG: str(self.n1.pk)})
        self.assertTrue(callable(got["allocated"]))
        w = World({})  # the platform keeps it in USER_TEMPLATE, where first_tag_of reads it
        vid = w._call("one.vm.allocate", f'NAME = "x"\n{dns.FIRST_TAG} = "{self.n1.pk}"\n', False)
        self.assertIn(f"<USER_TEMPLATE><{dns.FIRST_TAG}>{self.n1.pk}</", w._call("one.vm.info", vid))

    def test_commit_354_an_allocation_with_no_answer_keeps_the_place_whatever_the_pool_shows(self):
        """the first pool read is empty, the tagged VM may appear later: unsure - kept, also across a retry"""
        from accounts import tasks
        from inventory import base_deploy as bd
        from inventory import dns
        from inventory.models import DnsSettings

        self.zones()
        DnsSettings.objects.update(primary=None)

        def no_answer(*a, **kw):
            kw["prepare"]()
            kw["unsure"]()  # the allocate raised without OpenNebula's own refusal
            raise bd.RedeployError("rolled back: [one.vm.allocate] the connection was reset")

        with (
            mock.patch.object(bd, "_redeploy", side_effect=no_answer),
            mock.patch.object(dns, "first_alive", return_value=[]),
        ):
            tasks.start("core VM redeploy", "t", lambda tk: bd.redeploy(tk, self.n1))
        cand = DnsSettings.get().candidate
        self.assertTrue(cand.get("first") and cand.get("unsure"))
        self.assertEqual(dns.values(self.n2, reveal=False)["waiting_for"], "sdc1ns1")
        dns.reserve_for_primary(self.n1, first=True)  # a retry keeps it unsure
        self.assertTrue(DnsSettings.get().candidate["unsure"])
        # a retry through redeploy() from a candidate that was sure: its own lost allocation is not forgotten
        DnsSettings.objects.update(candidate={**DnsSettings.get().candidate, "unsure": False, "vms": [70]})

        def retry_no_answer(*a, **kw):
            kw["prepare"]()
            kw["unsure"]()
            raise bd.RedeployError("rolled back: [one.vm.allocate] timed out")

        with (
            mock.patch.object(bd, "_redeploy", side_effect=retry_no_answer),
            mock.patch.object(dns, "first_alive", return_value=[]),
        ):
            tasks.start("core VM redeploy", "retry", lambda tk: bd.redeploy(tk, self.n1))
        cand = DnsSettings.get().candidate
        self.assertTrue(cand["unsure"])
        self.assertEqual(cand["vms"], [70])
        self.assertFalse(dns.end_first(self.n1))  # a rollback by hand cannot end it on an empty pool
        dns.published(self.n1, True)  # only the node's successful redeploy ends it
        self.assertEqual(DnsSettings.get().primary_id, self.n1.pk)

    def test_commit_354_a_lost_allocation_reply_keeps_the_place(self):
        """the first attempt's VM was made but its id never came back: found by its tag, the candidate stays"""
        from accounts import tasks
        from inventory import base_deploy as bd
        from inventory import dns
        from inventory.models import DnsSettings

        self.zones()
        DnsSettings.objects.update(primary=None)

        def lost(*a, **kw):
            kw["prepare"]()
            raise bd.RedeployError("rolled back: [one.vm.allocate] the connection was reset")

        for alive, kept in (([91], True), (None, True), ([], False)):
            DnsSettings.objects.update(candidate={})
            with (
                mock.patch.object(bd, "_redeploy", side_effect=lost),
                mock.patch.object(dns, "first_alive", return_value=alive),
            ):
                tasks.start("core VM redeploy", "t", lambda tk: bd.redeploy(tk, self.n1))
            cand = DnsSettings.get().candidate
            self.assertEqual(bool(cand.get("first")), kept, alive)
            if alive:
                self.assertEqual(cand["vms"], alive)

    def test_a_server_made_the_primary_during_its_deployment(self):
        """owner 2026-11-22 11:51: "add check box to make server as primary during deployment" """
        from accounts import tasks
        from inventory import base_deploy as bd
        from inventory import dns
        from inventory.models import DnsSettings

        self.zones()
        self.assertEqual(DnsSettings.get().primary_id, self.n1.pk)
        self.assertEqual(dns.values(self.n2, reveal=False)["role"], "secondary")  # not ticked
        v = dns.values(self.n2, reveal=False, make_primary=True)
        self.assertEqual((v["role"], v["first"], v["primary"]), ("primary", True, "10.0.101.12"))
        v = dns.values(self.n1, reveal=False, make_primary=True)  # the primary itself: nothing moves
        self.assertEqual((v["role"], v["first"]), ("primary", False))
        got = {}

        def passes(*a, **kw):
            kw["prepare"]()
            got["cand"] = DnsSettings.get().candidate
            got["recipe_role"] = dns.values(self.n2, reveal=True)["role"]  # what its recipe is rendered with
            got["n1_waits"] = dns.values(self.n1, reveal=False)["waiting_for"]
            return {"new_id": 99}

        with (
            mock.patch.object(bd, "_redeploy", side_effect=passes),
            mock.patch.object(dns, "apply") as rp,
        ):
            t = tasks.start("core VM redeploy", "t", lambda tk: bd.redeploy(tk, self.n2, None, True, True))
        self.assertEqual(t.state, "done", t.error)
        self.assertEqual(DnsSettings.get().published, got["cand"]["texts"])  # commit-362: kept for the handoff
        self.assertTrue(got["cand"]["first"])
        self.assertEqual(got["recipe_role"], "primary")
        self.assertEqual(got["n1_waits"], "sdc1ns2")
        self.assertIn("SOA sdc1ns2.", next(iter(got["cand"]["texts"].values())))
        st = DnsSettings.get()
        self.assertEqual((st.primary_id, st.candidate), (self.n2.pk, {}))
        rp.assert_called_once()  # every server rewritten: the old primary becomes a secondary
        self.assertEqual(dns.values(self.n1, reveal=False)["role"], "secondary")

    def test_commit_362_a_failed_handoff_apply_is_said_and_marked(self):
        from accounts import tasks
        from inventory import base_deploy as bd
        from inventory import dns
        from inventory.models import DnsSettings

        self.zones()

        def passes(*a, **kw):
            kw["prepare"]()
            return {"new_id": 99}

        with (
            mock.patch.object(bd, "_redeploy", side_effect=passes),
            mock.patch.object(dns, "apply", side_effect=dns.DnsError("The primary sdc1ns2: not changed")),
        ):
            t = tasks.start("core VM redeploy", "t", lambda tk: bd.redeploy(tk, self.n2, None, True, True))
        self.assertEqual(t.result["handoff"], "incomplete")
        self.assertEqual([s["status"] for s in t.steps if s["name"] == "handoff"], ["failed"])
        st = DnsSettings.get()
        self.assertEqual((st.primary_id, st.handoff["from"]), (self.n2.pk, self.n1.pk))
        self.assertTrue(st.published)  # what the new primary was verified with

    def test_commit_362_the_handoff_mark_stays_until_every_server_follows(self):
        """the new primary's rollout passes, the former primary's fails: still marked; a full Apply clears it"""
        from inventory import dns
        from inventory.models import DnsSettings, DnsZone

        self.zones()
        dns.reserve_for_primary(self.n2, first=True)  # n2 made the primary during its deployment (owner 11:51)
        dns.published(self.n2, True, handoff=True)
        st = DnsSettings.get()
        self.assertEqual((st.primary_id, st.handoff["node"]), (self.n2.pk, self.n2.pk))  # in the same write
        fail = {"on": True}

        def answer(n, script):
            if "dig" in script:
                return "".join(f"{z.name} {DnsSettings.get().serial}\n" for z in DnsZone.objects.all())
            if n.pk == self.n1.pk and fail["on"]:
                raise RuntimeError("connection refused")
            return "CLAUGINE-APPLIED\n"

        t = self.run_task(lambda task: dns.apply(task), answer)
        self.assertEqual(t.state, "failed")
        st = DnsSettings.get()
        self.assertEqual(st.handoff["node"], self.n2.pk)  # the former primary did not follow yet
        self.assertTrue(st.published)
        # a redeploy of the current primary meanwhile: its own uncertain mark comes and goes, the handoff stays
        dns.mark_uncertain(self.n2, "redeploy", keep_candidate=True)
        dns.reserve_for_primary(self.n2)
        dns.published(self.n2, True)
        st = DnsSettings.get()
        self.assertEqual((st.uncertain, st.handoff["node"]), ({}, self.n2.pk))
        fail["on"] = False
        t = self.run_task(lambda task: dns.apply(task), answer)
        self.assertEqual(t.state, "done", t.error)
        self.assertEqual((DnsSettings.get().uncertain, DnsSettings.get().handoff), ({}, {}))

    def test_commit_367_a_handoff_kept_the_earlier_way_is_moved_on_upgrade(self):
        """the earlier form (uncertain why=handoff) moves to DnsSettings.handoff; a redeploy of the current primary
        then keeps the warning"""
        import importlib

        from django.apps import apps

        from inventory import dns
        from inventory.models import DnsSettings

        self.zones()
        DnsSettings.objects.update(
            primary=self.n2, uncertain={"node": self.n2.pk, "why": "handoff", "at": "2026-11-22T12:00:00"}, handoff={}
        )
        importlib.import_module("inventory.migrations.0111_dns_handoff").move_legacy(apps, None)
        st = DnsSettings.get()
        self.assertEqual((st.uncertain, st.handoff["node"], st.handoff["at"]), ({}, self.n2.pk, "2026-11-22T12:00:00"))
        dns.mark_uncertain(self.n2, "redeploy", keep_candidate=True)  # a redeploy of the current primary
        dns.reserve_for_primary(self.n2)
        dns.published(self.n2, True)
        self.assertEqual(DnsSettings.get().handoff["node"], self.n2.pk)  # the warning stays
        self.login()
        from django.urls import reverse

        page = self.client.get(reverse("infra_dns") + "?tab=servers").content.decode()
        self.assertIn("The primary moved and not every name server follows it yet", page)
        importlib.import_module("inventory.migrations.0111_dns_handoff").move_legacy(apps, None)  # run again: no change
        self.assertEqual(DnsSettings.get().handoff["node"], self.n2.pk)

    def test_commit_368_migration_0111_round_trip_keeps_the_handoff(self):
        """back to 0110 the pending handoff returns to uncertain (where 0110's code reads it), forward again it moves
        back to its own field; the migration runs move_back when reversed"""
        import importlib

        from django.apps import apps
        from django.db import migrations

        from inventory.models import DnsSettings

        m = importlib.import_module("inventory.migrations.0111_dns_handoff")
        run = [op for op in m.Migration.operations if isinstance(op, migrations.RunPython)]
        self.assertEqual((run[0].code, run[0].reverse_code), (m.move_legacy, m.move_back))
        self.zones()
        DnsSettings.objects.update(
            handoff={"node": self.n2.pk, "from": self.n1.pk, "at": "2026-11-22T13:00:00"}, uncertain={}
        )
        m.move_back(apps, None)  # reversed: before the field is removed
        st = DnsSettings.get()
        self.assertEqual(
            st.uncertain, {"node": self.n2.pk, "why": "handoff", "at": "2026-11-22T13:00:00", "from": self.n1.pk}
        )
        DnsSettings.objects.update(handoff={})  # 0110 has no such field
        m.move_legacy(apps, None)  # forward again
        st = DnsSettings.get()
        self.assertEqual((st.handoff["node"], st.handoff["at"], st.uncertain), (self.n2.pk, "2026-11-22T13:00:00", {}))
        # commit-369: both marks at once - a redeploy's uncertain mark and the handoff - both come back
        red = {"node": self.n2.pk, "why": "redeploy", "at": "2026-11-22T13:20:00"}
        DnsSettings.objects.update(uncertain=red)
        m.move_back(apps, None)
        once = DnsSettings.get().uncertain
        m.move_back(apps, None)  # commit-370: a retried downgrade changes nothing
        self.assertEqual(DnsSettings.get().uncertain, once)
        self.assertEqual(once["why"], "handoff")  # what 0110's code reads first
        DnsSettings.objects.update(handoff={})
        m.move_legacy(apps, None)
        st = DnsSettings.get()
        self.assertEqual((st.uncertain, st.handoff["node"], st.handoff["from"]), (red, self.n2.pk, self.n1.pk))

    def test_commit_331_check_holds_the_claim_or_stops(self):
        from inventory import dns
        from inventory.models import DnsSettings, DnsZone

        self.zones()
        token = dns.claim("check")
        held = []
        real = dns.hold

        def hold(tok):
            held.append(tok)
            return real(tok)

        def answer(n, script):
            return "".join(f"{z.name} 1\n" for z in DnsZone.objects.all())

        with mock.patch.object(dns, "hold", side_effect=hold):
            t = self.run_task(lambda task: dns.check(task, token), answer)
        self.assertEqual((t.state, held), ("done", [token, token]))  # one refresh before each server
        self.assertTrue(DnsSettings.get().last_check.get("servers"))
        dns.release(token)
        # the claim expired and another task took it: the stale Check stops and saves nothing
        DnsSettings.objects.update(last_check={})
        other = dns.claim("redeploy sdc1ns1")

        def taken_meanwhile(n, script):
            return answer(n, script)

        t = self.run_task(lambda task: dns.check(task, "lost-token"), taken_meanwhile)
        self.assertEqual(t.state, "failed")
        self.assertIn("claim of this task was lost", t.error)
        self.assertEqual(DnsSettings.get().last_check, {})
        dns.release(other)

    def test_commit_361_apply_waits_while_a_server_becomes_the_primary(self):
        from accounts import tasks
        from inventory import dns
        from inventory.models import DnsSettings

        self.zones()
        dns.reserve_for_primary(self.n2, first=True)  # a promotion whose redeploy has not ended
        before = (DnsSettings.get().reserved, DnsSettings.get().serial)
        t = tasks.start("dns", "apply", lambda tk: dns.apply(tk))
        self.assertIn("becoming the primary", t.error)
        self.assertEqual((DnsSettings.get().reserved, DnsSettings.get().serial), before)  # no serial reserved

    def test_a_failed_move_keeps_the_primary(self):
        from accounts import tasks
        from inventory import base_deploy as bd
        from inventory import dns
        from inventory.models import DnsSettings

        self.zones()

        def fails(*a, **kw):
            kw["prepare"]()
            raise bd.RedeployError("rolled back: the profile's checks failed")

        with (
            mock.patch.object(bd, "_redeploy", side_effect=fails),
            mock.patch.object(dns, "first_alive", return_value=[]),
            mock.patch.object(dns, "republish") as rp,
        ):
            tasks.start("core VM redeploy", "t", lambda tk: bd.redeploy(tk, self.n2, None, True, True))
        st = DnsSettings.get()
        self.assertEqual((st.primary_id, st.candidate), (self.n1.pk, {}))
        rp.assert_not_called()

    def test_commit_358_a_retired_primary_to_be_blocks_until_released(self):
        from inventory import base_deploy as bd
        from inventory import dns
        from inventory.models import CoreService, CoreServiceNode, DnsSettings

        self.zones()
        ns2 = CoreService.objects.create(site=self.site, name="ns-b", stype="ns", kind="vm", nic1_network=self.core)
        CoreServiceNode.objects.filter(pk=self.n2.pk).update(service=ns2)
        self.n2.refresh_from_db()
        DnsSettings.objects.update(primary=None)
        dns.reserve_for_primary(self.n1, first=True)
        CoreService.objects.filter(pk=self.ns.pk).update(state=CoreService.State.RETIRED)  # n1's service
        st = DnsSettings.get()
        p = dns.first_pending(st, dns.servers(st))
        self.assertEqual((p["host"], p["eligible"]), ("sdc1ns1", False))
        self.assertTrue(any("has not ended" in m for m in bd.missing(self.n2)))  # still blocked, and said so
        with self.assertRaises(dns.DnsError):
            dns.reserve_for_primary(self.n2, first=True)
        self.assertTrue(dns.release_first())
        self.assertFalse(dns.release_first())  # nothing left
        v = dns.values(self.n2, reveal=False)
        self.assertEqual((v["role"], v["first"], v["waiting_for"]), ("primary", True, ""))
        dns.reserve_for_primary(self.n2, first=True)
        dns.published(self.n2, True)
        self.assertEqual(DnsSettings.get().primary_id, self.n2.pk)

    def test_commit_358_the_page_names_the_primary_to_be_and_releases_it(self):
        from django.urls import reverse

        from inventory import dns
        from inventory.models import DnsSettings

        self.zones()
        dns.reserve_for_primary(self.n2, first=True)  # made the primary during its deployment - not ended
        self.login()
        page = self.client.get(reverse("infra_dns") + "?tab=servers").content.decode()
        self.assertIn("dlg-dns-release", page)
        self.assertIn("sdc1ns2 - its redeploy has not ended", page)
        self.client.post(reverse("dns_release_first"), {"confirm": "wrong"})
        self.assertTrue(DnsSettings.get().candidate.get("first"))
        old = DnsSettings.get().candidate["rev"]
        dns.reserve_for_primary(self.n2, first=True)  # a later attempt on the same server: a new revision
        self.client.post(reverse("dns_release_first"), {"confirm": "sdc1ns2", "rev": old})  # commit-363: stale
        self.assertTrue(DnsSettings.get().candidate.get("first"))
        dns.candidate_vm(self.n2, 77)  # a VM recorded since the page was shown
        rev = DnsSettings.get().candidate["rev"]
        with self.assertRaises(dns.DnsError):
            dns.release_first(None, "sdc1ns2", old)
        self.client.post(reverse("dns_release_first"), {"confirm": "sdc1ns2", "rev": rev})
        self.assertEqual(DnsSettings.get().candidate, {})
        self.assertEqual(DnsSettings.get().primary_id, self.n1.pk)  # the primary stays

    def test_commit_355_a_retired_primary_gives_way(self):
        from inventory import dns
        from inventory.models import CoreService, CoreServiceNode, DnsSettings

        self.zones()
        ns2 = CoreService.objects.create(site=self.site, name="ns-b", stype="ns", kind="vm", nic1_network=self.core)
        CoreServiceNode.objects.filter(pk=self.n2.pk).update(service=ns2)
        CoreService.objects.filter(pk=self.ns.pk).update(state=CoreService.State.RETIRED)  # n1's service
        self.assertEqual(dns.stale_primary().pk, self.n1.pk)
        self.n2.refresh_from_db()
        self.assertEqual(dns.values(self.n2, reveal=False)["role"], "primary")  # the next made takes over
        self.assertTrue(dns.drop_stale_primary())
        self.assertIsNone(DnsSettings.get().primary_id)
        dns.reserve_for_primary(self.n2, first=True)
        dns.published(self.n2, True)
        self.assertEqual(DnsSettings.get().primary_id, self.n2.pk)
        self.assertFalse(dns.drop_stale_primary())

    def test_commit_356_no_internal_domain_is_a_missing_value_not_an_error(self):
        from inventory import base_deploy as bd
        from inventory.models import DnsSettings
        from tenants.models import PortalSettings

        self.zones()
        DnsSettings.objects.update(primary=None)
        PortalSettings.objects.update(internal_domain="")
        self.assertIn(bd._("the internal domain (Settings → Customer)"), bd.missing(self.n2))

    def test_the_primarys_script_renders_its_zone_files(self):
        import subprocess

        from inventory import base_deploy as bd
        from inventory import dns

        self.zones()
        dns.reserve_for_primary(self.n1)  # the candidate is what the new primary gets
        text = bd.script_of(self.n1, reveal=True)
        self.assertIn("/var/cache/bind/zones/example.cloud.zone", text)
        self.assertIn("type primary;", text)
        self.assertEqual(subprocess.run(["bash", "-n"], input=text, text=True).returncode, 0)
        self.assertIn("primaries { ns-primary; };", bd.script_of(self.n2, reveal=True))

    def run_task(self, fn, answer):
        from accounts import tasks

        self.sent = []

        def node_ssh(n):
            def run(script, timeout=300):
                self.sent.append((n.name, script))
                return answer(n, script)

            return run

        with mock.patch("inventory.hostlogging.node_ssh", side_effect=node_ssh):
            return tasks.start("dns", "test", fn)

    def test_apply_writes_the_primary_then_the_bind_files(self):
        from inventory import dns
        from inventory.models import DnsSettings, DnsZone

        self.zones()

        def answer(n, script):
            if "dig" in script:
                out = "".join(f"{z.name} {DnsSettings.get().serial}\n" for z in DnsZone.objects.all())
                if n.pk == self.n1.pk:
                    st = DnsSettings.get()
                    out += "--files\n" + "".join(f"{z} {v[1]}\n" for z, v in st.applied.items())
                return out
            return "CLAUGINE-APPLIED\n"

        t = self.run_task(lambda task: dns.apply(task), answer)
        self.assertEqual(t.state, "done", t.error)
        first = self.sent[0]
        self.assertEqual(first[0], "core-ns1_dc1")
        self.assertIn("named-checkzone -q example.cloud /var/lib/claugine/dns-", first[1])
        self.assertIn("grep -qs /etc/bind/named.conf.zones /etc/bind/named.conf", first[1])
        for path in ("/etc/bind/named.options", "/etc/bind/named.conf.zones", "/etc/bind/named.conf"):
            self.assertIn(f" {path} || fail", first[1])  # commit-321: the three bind files together
        self.assertIn("rndc reload", first[1])
        self.assertEqual(self.sent[1][0], "core-ns2_dc1")
        self.assertNotIn("zones/example.cloud.zone", self.sent[1][1])
        self.assertIn(" /etc/bind/named.options || fail", self.sent[1][1])
        st = DnsSettings.get()
        self.assertEqual(st.uncertain, {})
        self.assertGreaterEqual(st.serial, 2026010100)
        self.assertEqual(set(st.published), {z.name for z in DnsZone.objects.all()})
        for z in DnsZone.objects.all():
            self.assertEqual(dns.state(z), "applied")
        self.assertEqual(set(st.last_check["files"].values()), {"as applied"})

    def test_commit_321_a_new_primary_reaches_the_secondaries_bind_files(self):
        import base64
        import re

        from inventory import dns
        from inventory.models import DnsSettings

        self.zones()
        DnsSettings.objects.update(primary=self.n2, forwarders=["9.9.9.9"])
        t = self.run_task(lambda task: dns.apply(task), lambda n, s: "" if "dig" in s else "CLAUGINE-APPLIED\n")
        sec = next(s for name, s in self.sent if name == "core-ns1_dc1" and "named.options" in s)
        texts = [base64.b64decode(b).decode() for b in re.findall(r"echo (\S+) \| base64 -d", sec)]
        self.assertTrue(any("primaries ns-primary { 10.0.101.12; };" in x for x in texts))
        self.assertTrue(any("forwarders { 9.9.9.9; };" in x for x in texts))
        self.assertTrue(any("type secondary;" in x for x in texts))
        self.assertIn(t.state, ("done", "failed"))

    def test_a_refused_primary_changes_nothing(self):
        from inventory import dns
        from inventory.models import DnsSettings
        from inventory.sshexec import SshError

        self.zones()

        def answer(n, script):
            raise SshError("10.0.101.11:22: CLAUGINE-UNCHANGED: its named.conf does not include the zone list")

        t = self.run_task(lambda task: dns.apply(task), answer)
        self.assertEqual(t.state, "failed")
        self.assertIn("not changed", t.error)
        self.assertEqual((DnsSettings.get().serial, DnsSettings.get().applied), (0, {}))
        self.assertEqual(len(self.sent), 1)

    def test_commit_322_outcomes(self):
        from inventory import dns
        from inventory.sshexec import SshError

        self.assertEqual(dns._outcome(SshError("x: CLAUGINE-RESTORED: named-checkconf"))[0], "put back as it was")
        self.assertIn("NOT put back", dns._outcome(SshError("x: CLAUGINE-RESTORE-FAILED: rndc"))[0])
        self.assertIn("state unknown", dns._outcome(SshError("x: no answer in 300 s"))[0])

    def test_commit_322_the_rollout_puts_everything_back(self):
        """The script itself, in a sandbox: paths moved under a folder, BIND's tools stubbed."""
        import os
        import stat
        import subprocess
        import tempfile
        from pathlib import Path

        from inventory import dns

        def run(fail_at):
            d = Path(tempfile.mkdtemp())
            etc, var, zones, bin_ = d / "etc", d / "var", d / "zones", d / "bin"
            for x in (etc, var, zones, bin_):
                x.mkdir()
            (etc / "named.conf").write_text(f'include "{etc}/named.conf.zones"; old\n')
            (etc / "named.options").write_text("old options\n")
            stubs = {
                "named-checkzone": "exit 1" if fail_at == "zone" else "exit 0",
                "named-checkconf": f'[ -e "{d}/fail-check" ] && {{ rm -f "{d}/fail-check"; exit 1; }}; exit 0',
                "rndc": "exit 0",
                "install": 'args=(); while [ $# -gt 0 ]; do case "$1" in -o|-g) shift 2;; *) args+=("$1"); shift;; esac; '
                'done; exec /usr/bin/env -i PATH=/usr/bin:/bin install "${args[@]}"',
            }
            for name, body in stubs.items():
                f = bin_ / name
                f.write_text("#!/bin/bash\n" + body + "\n")
                f.chmod(f.stat().st_mode | stat.S_IEXEC)
            if fail_at == "check":
                (d / "fail-check").write_text("")
            if fail_at == "backup":  # the copy of named.options cannot be verified
                (bin_ / "cmp").write_text('#!/bin/bash\ncase "$3" in *0.bak) exit 1;; esac; exec /usr/bin/cmp "$@"\n')
                (bin_ / "cmp").chmod(0o755)
            if fail_at == "cleanup":  # its journal gone, a backup already deleted, the manifest still there
                left = var / "dns-done"
                left.mkdir()
                (left / "manifest").write_text(f"0 had {etc}/named.options\n")
            if fail_at == "halfway":  # an earlier attempt swapped named.options in and was cut off
                old = var / "dns-old"
                old.mkdir()
                (old / "0.bak").write_text("old options\n")
                (old / "manifest").write_text(f"0 had {etc}/named.options\n1 new {etc}/stray.conf\n")
                (old / "swapping").write_text("")
                (etc / "named.options").write_text("half-way options\n")
                (old / "2.bak").write_text(f'include "{etc}/named.conf.zones"; old\n')  # named.conf cut half way
                with open(old / "manifest", "a") as m:
                    m.write(f"2 had {etc}/named.conf\n")
                (etc / "named.conf").write_text("inclu")
                (etc / "stray.conf").write_text("x\n")
            files = {
                "/etc/bind/named.options": ("new options\n", "0644", "root:bind"),
                "/etc/bind/named.conf.zones": ("new zones\n", "0644", "root:bind"),
                f"{dns.ZONE_DIR}/example.cloud.zone": ("zone\n", "0644", "root:bind"),
            }
            script = (
                dns.rollout(files, "t1", True)
                .replace("/etc/bind", str(etc))
                .replace(dns.ZONE_DIR, str(zones))
                .replace("/var/lib/claugine", str(var))
            )
            r = subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                env={"PATH": f"{bin_}:/usr/bin:/bin", "HOME": str(d)},
            )
            return r, etc, zones

        r, etc, zones = run(None)
        self.assertIn("CLAUGINE-APPLIED", r.stdout, r.stderr)
        self.assertEqual(list((etc.parent / "var").glob("dns-*")), [])  # commit-333: no folder left
        self.assertEqual((etc / "named.options").read_text(), "new options\n")
        self.assertTrue((etc / "named.conf.zones").exists())
        r, etc, zones = run("check")  # failed after the swap: everything as it was, the new files gone
        self.assertEqual(r.returncode, 10, r.stderr)
        self.assertIn("CLAUGINE-RESTORED", r.stderr)
        self.assertEqual(list((etc.parent / "var").glob("dns-*")), [])
        self.assertEqual((etc / "named.options").read_text(), "old options\n")
        self.assertFalse((etc / "named.conf.zones").exists())
        self.assertFalse((zones / "example.cloud.zone").exists())
        r, etc, zones = run("backup")  # commit-329: a backup that fails stops before anything changes
        self.assertEqual(r.returncode, 9, r.stderr)
        self.assertIn("CLAUGINE-UNCHANGED: backup", r.stderr)
        self.assertEqual((etc / "named.options").read_text(), "old options\n")
        self.assertEqual((etc / "named.conf").read_text().endswith("old\n"), True)
        self.assertFalse((etc / "named.conf.zones").exists())
        r, etc, zones = run("halfway")  # commit-322: an attempt cut off while it swapped is undone first
        self.assertIn("recovered an attempt left half way", r.stderr)
        self.assertEqual(list((etc.parent / "var").glob("dns-*")), [])
        self.assertIn("CLAUGINE-APPLIED", r.stdout, r.stderr)
        self.assertEqual((etc / "named.options").read_text(), "new options\n")
        r, etc, zones = run("cleanup")  # commit-339: an earlier folder whose journal was cleared, cut off mid-delete
        self.assertNotIn("recovered", r.stderr)  # not taken for an attempt left half way
        self.assertIn("CLAUGINE-APPLIED", r.stdout, r.stderr)
        self.assertEqual((etc / "named.options").read_text(), "new options\n")
        self.assertEqual(list((etc.parent / "var").glob("dns-*")), [])
        script = dns.rollout({"/etc/bind/named.options": ("x\n", "0644", "root:bind")}, "t2", True)
        tail = script[script.rindex("rndc reload") :]
        self.assertLess(
            tail.index("rm -f /var/lib/claugine/dns-t2/swapping"), tail.index("rm -rf /var/lib/claugine/dns-t2")
        )
        r, etc, zones = run("zone")  # a zone file that does not pass: nothing changed
        self.assertEqual(r.returncode, 9, r.stderr)
        self.assertIn("CLAUGINE-UNCHANGED", r.stderr)
        self.assertEqual((etc / "named.options").read_text(), "old options\n")
        self.assertFalse(os.path.exists(etc / "named.conf.zones"))

    def test_commit_323_330_a_primarys_redeploy_reserves_and_publishes_only_when_it_passes(self):
        from inventory import dns
        from inventory.models import DnsRecord, DnsSettings, DnsZone

        self.zones()
        s1 = dns.reserve_for_primary(self.n1)
        self.assertEqual(DnsSettings.get().published, {})  # nothing published before the new primary passes
        dns.published(self.n1, False)  # it did not pass: dropped, the serial stays reserved
        st = DnsSettings.get()
        self.assertEqual((st.published, st.candidate, st.reserved), ({}, {}, s1))
        s2 = dns.reserve_for_primary(self.n1)
        self.assertGreater(s2, s1)  # never reused
        dns.published(self.n1, True)
        self.assertEqual(DnsSettings.get().serial, s2)
        self.assertEqual(dns.reserve_for_primary(self.n1), s2)  # nothing changed since: the same serial
        cand = DnsSettings.get().candidate  # commit-342: recorded all the same - what the new primary is given
        self.assertEqual((cand["serial"], cand["texts"]), (s2, DnsSettings.get().published))
        dns.published(self.n1, False)
        # commit-343: the same zones, but an Apply reserved a higher serial (the primary may have served it)
        pub = DnsSettings.get().published
        higher = dns.reserve_serial()
        self.assertGreater(higher, s2)
        s = dns.reserve_for_primary(self.n1)
        self.assertGreater(s, higher)
        cand = DnsSettings.get().candidate
        self.assertEqual((cand["serial"], DnsSettings.get().reserved), (s, s))
        self.assertEqual(set(cand["texts"]), set(pub))
        for name, text in cand["texts"].items():
            self.assertIn(f"( {s} ", text)
            self.assertEqual(text.replace(str(s), "S"), pub[name].replace(str(s2), "S"))  # only the serial differs
        dns.published(self.n1, True)
        self.assertEqual(DnsSettings.get().serial, s)
        self.assertEqual(dns.reserve_for_primary(self.n1), s)  # nothing reserved above it now: reused
        dns.published(self.n1, False)
        s2 = s
        DnsRecord.objects.create(zone=DnsZone.objects.get(source="domain"), name="new", rtype="A", value="10.0.101.77")
        s3 = dns.reserve_for_primary(self.n1)
        self.assertGreater(s3, s2)
        text = DnsSettings.get().candidate["texts"]["example.cloud"]
        self.assertIn(f"( {s3} ", text)
        self.assertIn("new IN A 10.0.101.77", text)
        p = dns.values(self.n1)  # the new primary gets the candidate
        self.assertIn("new IN A", next(z["text"] for z in p["zones"] if z["name"] == "example.cloud"))
        self.assertGreater(dns.next_serial(DnsSettings.get().serial, DnsSettings.get().reserved), s3)

    def test_commit_330_a_refused_primary_redeploy_publishes_nothing(self):
        from accounts import tasks
        from inventory import base_deploy as bd
        from inventory import dns
        from inventory.models import DnsSettings

        self.zones()
        with mock.patch.object(bd, "_redeploy", side_effect=bd.RedeployError("refused before anything")):
            t = tasks.start("core VM redeploy", "test", lambda task: bd.redeploy(task, self.n1))
        self.assertEqual(t.state, "failed")
        st = DnsSettings.get()
        self.assertEqual((st.published, st.candidate, st.claim), ({}, {}, {}))
        # commit-341: refused before any VM change - the DNS is left as it was (published, applied, checked)
        dns.reserve_for_primary(self.n1)
        dns.published(self.n1, True)
        DnsSettings.objects.update(applied={"example.cloud": ["d", "x"]}, last_check={"at": "x"})
        before = DnsSettings.get()
        snap = (before.published, before.applied, before.last_check, before.serial, before.uncertain)
        self.assertTrue(snap[0])
        p = {"ok": True, "platform": "p", "nics": [], "image": "i", "image_id": 1, "checks": [], "old_exists": True}
        with mock.patch.object(bd, "plan", return_value=p):  # the real _redeploy: refused at "-old exists"
            t = tasks.start("core VM redeploy", "test", lambda task: bd.redeploy(task, self.n1))
        self.assertEqual(t.state, "failed")
        self.assertIn("-old exists", t.error)
        with (  # the real _rollback_by_hand: nothing to roll back
            mock.patch.object(bd, "_one", return_value=(mock.Mock(), mock.Mock())),
            mock.patch.object(bd, "_vm", return_value=None),
            mock.patch.object(bd, "_reread"),
        ):
            t = tasks.start("core VM redeploy", "test", lambda task: bd.rollback(task, self.n1))
        self.assertEqual(t.state, "failed")
        self.assertIn("nothing to roll back", t.error)
        st = DnsSettings.get()
        self.assertEqual((st.published, st.applied, st.last_check, st.serial, st.uncertain), snap)
        self.assertEqual(st.claim, {})

    def test_commit_331_a_live_claim_lasts_while_it_is_refreshed(self):
        from datetime import timedelta

        from django.utils import timezone

        from inventory import dns
        from inventory.models import DnsSettings

        token = dns.claim("apply")
        old = (timezone.now() - timedelta(minutes=30)).isoformat()
        DnsSettings.objects.update(claim={"token": token, "at": old, "by": "apply"})
        self.assertIsNone(dns.claim(f"redeploy {self.n1.name}"))  # 30 min: still live (not hostlogging's 15)
        dns.touch(token)
        self.assertGreater(DnsSettings.get().claim["at"], old)
        DnsSettings.objects.update(claim={"token": token, "at": (timezone.now() - timedelta(hours=3)).isoformat()})
        self.assertIsNotNone(dns.claim("apply"))  # abandoned: taken over

    def test_commit_332_saving_the_settings_keeps_a_claim_taken_meanwhile(self):
        from inventory import dns
        from inventory.models import DnsSettings

        self.login()
        DnsSettings.get()  # the form's request has read the row ...
        real_get = DnsSettings.get
        token = {}

        def get():  # ... and a DNS task takes the claim before the form is saved
            st = real_get()
            if not token:
                token["t"] = None  # once: claim() reads the row through get() too
                token["t"] = dns.claim("apply")
            return st

        with mock.patch.object(DnsSettings, "get", side_effect=get):
            self.client.post(
                reverse("dns_settings"),
                {
                    "forwarders": "1.1.1.1",
                    "forward_zones": "",
                    "sites": "",
                    "internal_extra": "",
                    "notify_extra": "",
                    "ttl": 300,
                    "refresh": 3600,
                    "retry": 600,
                    "expire": 1209600,
                    "minimum": 300,
                },
            )
        st = DnsSettings.objects.get()
        self.assertEqual(st.forwarders, ["1.1.1.1"])
        self.assertEqual(st.claim.get("token"), token["t"])

    def test_commit_336_check_ends_well_with_no_journal(self):
        import stat
        import subprocess
        import tempfile
        from pathlib import Path

        from inventory import dns

        d = Path(tempfile.mkdtemp())
        (d / "state").mkdir()
        (d / "zones").mkdir()
        (d / "zones" / "example.cloud.zone").write_text("zone\n")
        (d / "bin").mkdir()
        dig = d / "bin" / "dig"
        dig.write_text(
            "#!/bin/bash\necho 'ns.example.cloud. hostmaster.example.cloud. 2026112101 3600 600 1209600 300'\n"
        )
        dig.chmod(dig.stat().st_mode | stat.S_IEXEC)
        script = (
            dns.check_script(["example.cloud"], True)
            .replace(dns.STATE_DIR, str(d / "state"))
            .replace(dns.ZONE_DIR, str(d / "zones"))
        )
        r = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, env={"PATH": f"{d}/bin:/usr/bin:/bin"}
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("example.cloud 2026112101", r.stdout)
        self.assertTrue(r.stdout.rstrip().endswith("--halfway"))
        (d / "state" / "dns-x").mkdir()
        (d / "state" / "dns-x" / "swapping").write_text("")
        r = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, env={"PATH": f"{d}/bin:/usr/bin:/bin"}
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("dns-x/swapping", r.stdout)

    def test_commit_337_a_serial_is_reserved_before_the_primary_is_contacted(self):
        from inventory import dns
        from inventory.models import DnsSettings
        from inventory.sshexec import SshError

        self.zones()

        def lost(n, script):
            raise SshError("10.0.101.11:22: no answer in 300 s")  # it may have applied and reloaded

        t = self.run_task(lambda task: dns.apply(task), lost)
        self.assertEqual(t.state, "failed")
        self.assertIn("state unknown", t.error)
        st = DnsSettings.get()
        self.assertEqual((st.serial, st.published), (0, {}))
        first = st.reserved
        self.assertGreater(first, 0)
        self.run_task(lambda task: dns.apply(task), lost)
        self.assertGreater(DnsSettings.get().reserved, first)  # never the same serial twice

    @staticmethod
    def stopped(*a, before=None, **k):  # cut off after the VM changes began
        before()
        raise RuntimeError("worker stopped")

    @staticmethod
    def rolled_back(*a, prepare=None, before=None, **k):
        from inventory import base_deploy as bd

        prepare()
        before()
        raise bd.RedeployError("rolled back: checks failed")

    def test_commit_342_the_new_primary_is_given_what_is_published(self):
        from accounts import tasks
        from inventory import base_deploy as bd
        from inventory import dns
        from inventory.models import CoreService, DnsRecord, DnsSettings, DnsZone

        self.zones()
        p = {"ok": True, "platform": "p", "nics": [], "image": "i", "image_id": 1, "checks": [], "old_exists": False}
        p["old_id"] = None
        got = {}
        real = bd.script_of

        def script_of(node, reveal=False):
            got["recipe"] = real(node, reveal=reveal)
            got["cand_at_render"] = DnsSettings.get().candidate
            return got["recipe"]

        def first_change(*a, **k):  # the first VM change: the redeploy passes from here
            st = DnsSettings.get()
            got["at_change"] = (st.candidate, st.uncertain, st.published)
            raise bd.RedeployError("stop here")

        def run():
            got.clear()
            with (
                mock.patch.object(bd, "plan", return_value=p),
                mock.patch.object(bd, "script_of", side_effect=script_of),
                mock.patch.object(bd, "_one", return_value=(mock.Mock(), mock.Mock())),
                mock.patch.object(CoreService, "platform", new=mock.Mock(sa="sa", key="p")),  # no platform here
                mock.patch("inventory.credentials.account", return_value=("oneadmin", {"public": "ssh-ed25519 K"})),
                mock.patch("tenants.deploy.with_public", side_effect=lambda a: a),
                mock.patch("accounts.models.audit", side_effect=first_change),
            ):
                return tasks.start("core VM redeploy", "test", lambda task: bd.redeploy(task, self.n1))

        for change in (False, True):  # the zones as published, then a record added since
            if change:
                DnsRecord.objects.create(
                    zone=DnsZone.objects.get(source="domain"), name="fresh", rtype="A", value="10.0.101.78"
                )
            t = run()
            self.assertEqual(t.state, "failed")
            self.assertIn("at_change", got, t.error)
            cand, uncertain, published = got["at_change"]
            self.assertEqual(got["cand_at_render"], cand)  # rendered from the candidate kept through the mark
            self.assertEqual((uncertain["why"], published), ("redeploy", {}))
            self.assertEqual(set(cand["texts"]), {z.name for z in DnsZone.objects.all()})
            for text in cand["texts"].values():
                self.assertIn(str(cand["serial"]), text)
                for line in text.splitlines():
                    if line.strip() and "'" not in line:
                        self.assertIn(line, got["recipe"])
            self.assertEqual("fresh" in got["recipe"], change)
            self.assertEqual(DnsSettings.get().candidate, {})  # it stopped after the VM change: dropped, still marked
            DnsSettings.objects.update(candidate=cand)
            dns.published(self.n1, True)  # as if it had passed: what it was given is what is published
            st = DnsSettings.get()
            self.assertEqual((st.published, st.serial, st.uncertain), (cand["texts"], cand["serial"], {}))
        DnsSettings.objects.update(published={"example.cloud": "old"}, applied={"x": ["d", "s"]})
        with (
            mock.patch.object(bd, "plan", return_value=p),
            mock.patch.object(bd, "script_of", side_effect=OSError("x")),
        ):
            t = tasks.start("core VM redeploy", "test", lambda task: bd.redeploy(task, self.n1))
        self.assertEqual(t.state, "failed")  # failed before any VM change: only the candidate is dropped
        st = DnsSettings.get()
        self.assertEqual(
            (st.published, st.applied, st.candidate, st.uncertain),
            ({"example.cloud": "old"}, {"x": ["d", "s"]}, {}, {}),
        )

    def test_commit_338_a_primarys_rollback_republishes(self):
        from accounts import tasks
        from inventory import base_deploy as bd
        from inventory import dns
        from inventory.models import DnsSettings

        self.zones()
        from inventory.models import DnsZone

        names = [z.name for z in DnsZone.objects.all()]
        DnsSettings.objects.update(serial=7)
        DnsSettings.objects.update(
            applied={z.name: [dns.digest(dns.zone_text(z, 7)), "x"] for z in DnsZone.objects.all()}
        )

        def checked():  # commit-340: a check that matched the old primary
            DnsSettings.objects.update(
                last_check={
                    "at": "x",
                    "revision": dns.revision(
                        {"st": DnsSettings.get(), "soa": dns.soa(), "domain": dns.domain(), "srv": dns.servers()}
                    ),
                    "servers": {
                        "sdc1ns1": {"serials": dict.fromkeys(names, "7"), "role": "primary", "ip": "10.0.101.11"}
                    },
                }
            )

        def row():
            page = self.client.get(reverse("infra_dns") + "?tab=servers").content.decode()
            return page.split("sdc1ns1", 2)[-1].split("</tr>")[0]

        checked()
        self.login()
        self.assertEqual({dns.state(z) for z in DnsZone.objects.all()}, {"applied"})
        self.assertIn(">answers<", row())
        dns.reserve_for_primary(self.n1)
        dns.published(self.n1, True)
        self.assertTrue(DnsSettings.get().published)
        reserved, serial = DnsSettings.get().reserved, DnsSettings.get().serial
        with (
            mock.patch.object(
                bd, "_rollback_by_hand", side_effect=lambda *a, before=None, **k: before() or {"old_id": 1}
            ),
            mock.patch.object(dns, "apply", side_effect=dns.DnsError("The primary sdc1ns1: not changed")) as ap,
        ):
            t = tasks.start("core VM redeploy", "test", lambda task: bd.rollback(task, self.n1))
        self.assertEqual(t.state, "done", t.error)
        ap.assert_called_once()
        self.assertTrue(any("Apply them" in s["detail"] for s in t.steps))
        st = DnsSettings.get()
        self.assertEqual((st.published, st.claim), ({}, {}))  # not known what it serves: published again on Apply
        self.assertEqual(st.uncertain["why"], "rollback")  # kept until an Apply succeeds
        # commit-340: the old Apply and Check no longer count; the serial and its reservation stay
        self.assertEqual((st.applied, st.last_check, st.serial), ({}, {}, serial))
        self.assertGreaterEqual(st.reserved, reserved)  # a reservation is never given back
        self.assertEqual({dns.state(z) for z in DnsZone.objects.all()}, {"to apply"})
        self.assertNotIn(">answers<", row())
        checked()  # even a check that matches: not green while the mark is there
        self.assertIn("zones not known", row())
        self.assertNotIn(">answers<", row())
        page = self.client.get(reverse("infra_dns")).content.decode()
        self.assertIn("what it serves is not known", page)
        DnsSettings.objects.update(uncertain={})
        with (  # commit-338: cut off after the VM is back, before the zones are applied again
            mock.patch.object(bd, "_rollback_by_hand", side_effect=self.stopped),
            mock.patch.object(dns, "apply") as ap,
        ):
            t = tasks.start("core VM redeploy", "test", lambda task: bd.rollback(task, self.n1))
        self.assertEqual(t.state, "failed")
        ap.assert_not_called()
        self.assertEqual(DnsSettings.get().uncertain["why"], "rollback")  # the mark outlives the task
        DnsSettings.objects.update(uncertain={})
        dns.reserve_for_primary(self.n1)
        with (
            mock.patch.object(bd, "_redeploy", side_effect=self.rolled_back),
            mock.patch.object(dns, "apply") as ap,
        ):
            t = tasks.start("core VM redeploy", "test", lambda task: bd.redeploy(task, self.n1))
        self.assertEqual(t.state, "failed")
        ap.assert_called_once()
        self.assertEqual(DnsSettings.get().candidate, {})

    def test_commit_324_one_dns_task_at_a_time(self):
        from inventory import dns

        token = dns.claim("apply")
        self.assertIsNotNone(token)
        self.assertIsNone(dns.claim("apply"))
        self.login()
        with mock.patch("inventory.dns.apply") as ap:
            self.client.post(reverse("dns_apply"), {"confirm": "apply"})
            ap.assert_not_called()
        dns.release(token)
        self.assertIsNotNone(dns.claim("apply"))

    def test_commit_325_numeric_and_alphabetic_host_names(self):
        from inventory import dns
        from inventory.models import Host

        Host.objects.create(site=self.site, name="123", ip="10.0.100.123")
        self.zones()
        auto, _n = dns.automatic()
        self.assertIn("123", [r["name"] for r in auto["example.cloud"]])
        self.assertEqual([r["name"] for r in auto["100.0.10.in-addr.arpa"]], ["123", "201"])

    def test_commit_326_the_most_specific_reverse_zone(self):
        from inventory import dns
        from inventory.models import DnsZone

        self.core.prefix, self.core.subnet = 16, "10.0.0.0"
        self.core.save()
        self.zones()
        self.assertEqual(
            sorted(z.name for z in DnsZone.objects.filter(kind="reverse")),
            ["0.10.in-addr.arpa", "100.0.10.in-addr.arpa"],
        )
        auto, _n = dns.automatic()
        self.assertEqual([r["name"] for r in auto["100.0.10.in-addr.arpa"]], ["201"])  # the /24, not the /16
        self.assertIn("11.101", [r["name"] for r in auto["0.10.in-addr.arpa"]])
        DnsZone.objects.create(name="101.0.10.in-addr.arpa", kind="reverse", source="")  # added, more specific
        auto, _n = dns.automatic()
        self.assertNotIn("11.101", [r["name"] for r in auto["0.10.in-addr.arpa"]])
        self.assertNotIn("101.0.10.in-addr.arpa", auto)

    def test_commit_327_a_new_zone_is_not_checked_yet(self):
        from inventory.models import DnsSettings, DnsZone

        self.zones()
        names = [z.name for z in DnsZone.objects.all()]
        DnsSettings.objects.update(
            serial=7,
            last_check={"at": "x", "servers": {"sdc1ns1": {"serials": dict.fromkeys(names, "7")}}},
        )
        self.login()
        from inventory import dns

        srv = dns.servers()
        DnsSettings.objects.update(
            last_check={
                "at": "x",
                "revision": dns.revision(
                    {"st": DnsSettings.get(), "soa": dns.soa(), "domain": dns.domain(), "srv": srv}
                ),
                "servers": {"sdc1ns1": {"serials": dict.fromkeys(names, "7"), "role": "primary", "ip": "10.0.101.11"}},
            }
        )
        page = self.client.get(reverse("infra_dns") + "?tab=servers").content.decode()
        self.assertIn(">answers<", page.split("sdc1ns1", 2)[-1].split("</tr>")[0])
        DnsSettings.objects.update(primary=self.n2)  # the configuration changed since the check: not green
        page = self.client.get(reverse("infra_dns") + "?tab=servers").content.decode()
        self.assertNotIn(">answers<", page.split("sdc1ns1", 2)[-1].split("</tr>")[0])
        DnsSettings.objects.update(primary=self.n1)
        DnsZone.objects.create(name="corp.example", kind="direct", source="")
        page = self.client.get(reverse("infra_dns") + "?tab=servers").content.decode()
        self.assertIn("corp.example", page.split("sdc1ns1", 2)[-1])
        self.assertNotIn(">answers<", page.split("sdc1ns1", 2)[-1].split("</tr>")[0])

    def test_import_adds_what_the_portal_does_not_make(self):
        from inventory import dns

        self.zones()
        text = (
            "example.cloud. 300 IN SOA x. y. 1 2 3 4 5\n"
            "example.cloud. 300 IN NS old.example.cloud.\n"
            "sdc1ns1.example.cloud. 300 IN A 10.0.101.11\n"
            "graylog.example.cloud. 300 IN CNAME sat1onelog1.example.cloud.\n"
            "_kerberos._tcp.example.cloud. 600 IN SRV 0 0 88 dc1.corp.\n"
            "other.zone. 300 IN A 1.2.3.4\n"
        )
        zone, added, skipped = dns.import_zone("example.cloud", text)
        self.assertEqual((added, skipped), (2, 4))
        self.assertEqual(
            sorted((r.name, r.rtype, r.ttl) for r in zone.records.all()),
            [("_kerberos._tcp", "SRV", 600), ("graylog", "CNAME", None)],
        )
        self.assertEqual(dns.import_zone("example.cloud", text)[1], 0)  # once is enough
        z, added, _s = dns.import_zone("20.83.10.in-addr.arpa", "5.20.83.10.in-addr.arpa. 300 IN PTR a.corp.\n")
        self.assertEqual((z.kind, z.source, added), ("reverse", "", 1))

    def login(self):
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

    def test_the_page_and_its_forms(self):
        from inventory.models import DnsRecord, DnsSettings, DnsZone

        self.login()
        page = self.client.get(reverse("infra_dns")).content.decode()
        for x in ("example.cloud", "101.0.10.in-addr.arpa", "sdc1ns1", 'href="/infrastructure/dns/"'):
            self.assertIn(x, page)
        page = self.client.get(reverse("infra_dns") + "?tab=servers").content.decode()
        self.assertIn("sdc1ns2", page)
        self.client.post(reverse("dns_zone_new"), {"name": "Corp.Example."})
        z = DnsZone.objects.get(name="corp.example")
        self.assertEqual((z.kind, z.source), ("direct", ""))
        self.client.post(reverse("dns_record_new", args=[z.pk]), {"name": "www", "rtype": "A", "value": "10.1.1.1"})
        self.client.post(reverse("dns_record_new", args=[z.pk]), {"name": "bad", "rtype": "A", "value": "nope"})
        self.client.post(
            reverse("dns_record_new", args=[z.pk]), {"name": "x", "rtype": "CNAME", "value": "a\nb IN A 1.1.1.1"}
        )
        self.assertEqual(list(z.records.values_list("name", flat=True)), ["www"])
        r = z.records.get()
        self.client.post(reverse("dns_record_delete", args=[z.pk, r.pk]))
        self.assertFalse(DnsRecord.objects.exists())
        auto = DnsZone.objects.get(source="domain")
        self.client.post(reverse("dns_zone_delete", args=[auto.pk]))
        self.assertTrue(DnsZone.objects.filter(pk=auto.pk).exists())  # automatic: not deleted here
        self.client.post(
            reverse("dns_settings"),
            {
                "primary": str(self.n2.pk),  # owner 2026-11-22: not a field any more - ignored
                "forwarders": "1.1.1.1 8.8.4.4",
                "forward_zones": "corp.example: 10.83.128.249",
                "sites": "dc1 | 10.0.0.0/16 | 203.0.113.0/24",
                "internal_extra": "10.0.0.0/8",
                "notify_extra": "",
                "ttl": 300,
                "refresh": 3600,
                "retry": 600,
                "expire": 1209600,
                "minimum": 300,
            },
        )
        st = DnsSettings.get()
        self.assertEqual((st.primary_id, st.forwarders), (self.n1.pk, ["1.1.1.1", "8.8.4.4"]))  # the first deployed
        self.assertEqual(st.forward_zones, [{"name": "corp.example", "servers": ["10.83.128.249"]}])
        with mock.patch("inventory.dns.apply") as ap:
            self.client.post(reverse("dns_apply"), {"confirm": "no"})
            ap.assert_not_called()


class Review574(TestCase):
    """commit-574 (review of df9bcd4, owner 2026-11-26 13:16): DNS Apply writes each server's bind files from its own
    role's functions, as its Redeploy does."""

    setUp = Dns.setUp  # the site and its two name servers (not Dns's tests again)

    def test_574_each_server_its_own_roles_files(self):
        from inventory import dns
        from inventory.base_deploy import profile_of
        from inventory.models import ConfigTemplate, HostService

        p = profile_of(self.ns)
        ns = HostService.objects.get(key="ns")
        other = ConfigTemplate.objects.create(role="named-other", kind="config", os="ubuntu", name="o", body="// b\n")
        alt = HostService.objects.create(
            key="ns-alt",
            name="alt",
            group="function",
            os="base-os",
            legacy={},
            configs=[
                {**c, "template": other.pk} if c["path"] == "/etc/bind/named.conf" else c for c in ns.configs or []
            ],
        )
        r = p.roles[0]
        p.roles = [{**r, "name": "a", "count": 1}, {**r, "name": "b", "count": 1, "functions": [alt.pk]}]
        p.save(update_fields=["roles"])
        self.n1.params, self.n2.params = {"role": "a"}, {"role": "b"}
        self.n1.save(update_fields=["params"])
        self.n2.save(update_fields=["params"])
        snap = dns.snapshot()
        a = dns._bind_files(self.n1, snap, [])
        b = dns._bind_files(self.n2, snap, [])
        self.assertEqual(b["/etc/bind/named.conf"][0], "// b\n")
        self.assertNotEqual(a["/etc/bind/named.conf"][0], "// b\n")
