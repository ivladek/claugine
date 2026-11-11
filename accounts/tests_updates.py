"""Release 226 (owner 2026-10-31): updates (Settings → Portal → Updates), automatic updates (off by
default; the standby first, the active node only when that worked) and the version under test at
<public URL>/test-new-version/ (accounts/testmount.py)."""
import os
import tempfile
import unittest
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts import edition
from . import autoupdate, ha, testmount
from .testkit import FakeNode, NodeFile, otp, staff
from tenants.models import PortalSettings

class AutoUpdate(TestCase):
    """A pair: the standby first, the active node only when that worked (v01.00.00: the release the check-in names)."""

    def setUp(self):
        staff(is_super_admin=True)
        self.node = NodeFile(SITE="dc1", ADDR="10.0.0.25", PEER_ADDR="10.0.1.25", ROLE="active")
        self.node.__enter__()
        self.addCleanup(self.node.__exit__)

    def run_tick(self, results, installed="v1", target="v2"):
        ops = iter(results)
        with mock.patch("accounts.checkin.configured", return_value=True), \
                mock.patch("accounts.checkin.usage", return_value={}), \
                mock.patch("accounts.checkin.check_in",
                           return_value=(True, {"state": "active", "edition": "enterprise", "target_version": target})), \
                mock.patch("accounts.ha_agent.runs_enterprise", return_value=True), \
                mock.patch("accounts.ha_agent.installed_status", return_value={"installed_tag": installed}), \
                mock.patch("accounts.ha_agent.read_op", return_value={}), \
                mock.patch("accounts.ha_agent.run_op", side_effect=lambda name, **kw: next(ops)) as run:
            out = autoupdate.tick(log=lambda *_a: None, force=True)
        return out, [c.args[0] for c in run.call_args_list]

    def test_off_by_default_and_only_on_the_active_node(self):
        self.assertFalse(PortalSettings.get().auto_update)
        self.assertEqual(self.run_tick([]), ("off (the licence was checked in)", []))
        PortalSettings.get() and PortalSettings.objects.update(auto_update=True)
        self.node.set(ROLE="standby")
        self.assertEqual(self.run_tick([])[1], [])

    def test_the_standby_first_and_the_active_node_only_when_that_worked(self):
        PortalSettings.get() and PortalSettings.objects.update(auto_update=True)
        out, ops = self.run_tick([{"state": "failed", "error": "pip failed"}])
        self.assertEqual(ops, ["update-standby"])  # the active node is not touched
        self.assertIn("standby failed", out)
        self.assertEqual(PortalSettings.get().auto_update_state["failed_tag"], "v2")
        self.assertIn("stopped", mail.outbox[-1].subject)
        out, ops = self.run_tick([])
        self.assertEqual(ops, [])  # v2 is not tried again
        self.assertIn("failed before", out)
        out, ops = self.run_tick([{"state": "done"}, {"state": "done"}], target="v3")
        self.assertEqual(ops, ["update-standby", "update-active"])
        self.assertEqual(out, "v3: installed on both nodes")
        self.assertEqual(self.run_tick([], installed="v3", target="v3")[0], "up to date (v3)")

    def test_tasks_running_wait_for_the_next_round(self):
        PortalSettings.get() and PortalSettings.objects.update(auto_update=True)
        from .models import Task
        Task.objects.create(kind="x", title="busy", state="running")
        out, ops = self.run_tick([])
        self.assertEqual((out, ops), ("tasks are running: next round", []))


@override_settings(SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies")
class TestMount(TestCase):
    """The version under test: super admins only; read only on the standby; a release with database changes
    shows a page instead."""

    def setUp(self):
        for p in (mock.patch.object(testmount, "ON", True), mock.patch.object(testmount, "pending_migrations", return_value=[])):
            p.start()
            self.addCleanup(p.stop)
        self.node = NodeFile(SITE="dc2", ADDR="10.0.1.25", PEER_ADDR="10.0.0.25", ROLE="standby")
        self.node.__enter__()
        self.addCleanup(self.node.__exit__)

    @unittest.skipUnless(edition.ee("ha"), "the pair roles are Enterprise (ee/ha.py); a Standard build has one node")
    def test_super_admins_read_only(self):
        r = self.client.get(reverse("dashboard"))
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse("login"), r["Location"])
        self.assertEqual(self.client.get(reverse("login")).status_code, 200)  # not the maintenance page of a standby
        user = staff("rm@corp.kz", is_resource_manager=True)
        self.client.force_login(user)
        self.assertContains(self.client.get(reverse("dashboard")), "Only super admins", status_code=403)
        sa = staff(is_super_admin=True)
        self.client.force_login(sa)
        otp(self.client)
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, "New version under test")
        self.assertContains(r, "read only")
        r = self.client.post(reverse("portal_settings"), {"section": "auto_update", "auto_update": "on"})
        self.assertContains(r, "read only", status_code=403)
        self.assertFalse(PortalSettings.get().auto_update)
        from .models import AuditLog, audit
        n = AuditLog.objects.count()
        audit(sa, "login", sa)
        self.assertEqual(AuditLog.objects.count(), n)  # nothing written on the standby
        self.node.set(ROLE="new")  # a copy of the database: changes are fine there
        audit(sa, "login", sa)
        self.assertEqual(AuditLog.objects.count(), n + 1)

    def test_a_release_with_database_changes(self):
        with mock.patch.object(testmount, "pending_migrations", return_value=["tenants.0099_x"]):
            r = self.client.get(reverse("login"))
        self.assertContains(r, "Publish the new version", status_code=503)
        self.assertContains(r, "tenants.0099_x", status_code=503)




class SingleNodePage(TestCase):
    """2026-11-11 (step 3): one node — every Standard installation — has its Updates card. It used to appear only
    with two nodes, so a Standard admin could neither switch automatic updates on nor upgrade from the page."""

    def test_one_node_has_its_updates(self):
        sa = staff(is_super_admin=True)
        self.client.force_login(sa)
        otp(self.client)
        with NodeFile(SITE="dc1", ROLE="active"):
            r = self.client.get(reverse("portal_settings") + "?tab=portal")
        self.assertContains(r, "This portal runs on one node")
        self.assertContains(r, 'name="auto_update"')
        self.assertContains(r, "install new releases by itself")
        self.assertNotContains(r, "Switch over")
        self.assertNotContains(r, "Test address")
