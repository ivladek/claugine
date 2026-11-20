"""Release 226 (owner 2026-10-31): updates (Settings → Portal → Updates), automatic updates (off by default). A pair
takes a release as the "Update portal" button does (owner 2026-11-15): update-portal, one node at a time with a switch
over and back. The version under test is gone ("lets drop test at all")."""

from unittest import mock

from django.core import mail
from django.test import TestCase
from django.urls import reverse

from tenants.models import PortalSettings

from . import autoupdate
from .testkit import NodeFile, otp, staff


class AutoUpdate(TestCase):
    """A pair: the standby first, the active node only when that worked (v01.00.00: the release the check-in names)."""

    def setUp(self):
        staff(is_super_admin=True)
        self.node = NodeFile(SITE="dc1", ADDR="10.0.0.25", PEER_ADDR="10.0.1.25", ROLE="active")
        self.node.__enter__()
        self.addCleanup(self.node.__exit__)

    def run_tick(self, results, installed="v1", target="v2", behind="", asked=False):
        ops = iter(results)
        if asked:
            autoupdate._save(install_requested=True)
        with (
            mock.patch("accounts.autoupdate._unfinished_pair", return_value=behind),
            mock.patch("accounts.checkin.configured", return_value=True),
            mock.patch("accounts.checkin.usage", return_value={}),
            mock.patch(
                "accounts.checkin.check_in",
                return_value=(True, {"state": "active", "edition": "enterprise", "target_version": target}),
            ),
            mock.patch("accounts.ha_agent.runs_enterprise", return_value=True),
            mock.patch("accounts.ha_agent.installed_status", return_value={"installed_tag": installed}),
            mock.patch("accounts.ha_agent.read_op", return_value={}),
            mock.patch("accounts.ha_agent.run_op", side_effect=lambda name, **kw: next(ops)) as run,
        ):
            out = autoupdate.tick(log=lambda *_a: None, force=True)
        self.last_run = run
        return out, [c.args[0] for c in run.call_args_list]

    def test_a_pair_left_half_updated_is_finished(self):
        """integration-002: the second half stopped - this node runs v2, the other still v1, and the control plane
        offers v2. The check said "up to date"; now the other node gets v2 and the two switch back: by itself once
        (automatic updates on), then only when a super admin asks (Update portal)."""
        PortalSettings.get() and PortalSettings.objects.update(auto_update=True)
        failed = {"state": "failed", "error": "pip failed", "steps": []}
        out, ops = self.run_tick([failed], installed="v2", target="v2", behind="v1")
        self.assertEqual(ops, ["update-portal-finish"])
        self.assertEqual(self.last_run.call_args.kwargs["ref"], "v2")
        self.assertEqual(PortalSettings.get().auto_update_state["finish_failed_tag"], "v2")
        out, ops = self.run_tick([], installed="v2", target="v2", behind="v1")
        self.assertEqual(ops, [])  # not by itself again
        self.assertIn("Update portal tries again", out)
        out, ops = self.run_tick([{"state": "done"}], installed="v2", target="v2", behind="v1", asked=True)
        self.assertEqual(ops, ["update-portal-finish"])
        self.assertIn("switched back", out)
        self.assertEqual(PortalSettings.get().auto_update_state["finish_failed_tag"], "")
        # with automatic updates off, only the button does it
        PortalSettings.objects.update(auto_update=False)
        out, ops = self.run_tick([], installed="v2", target="v2", behind="v1")
        self.assertEqual(ops, [])
        self.assertIn("Update portal finishes the update", out)

    def test_a_failed_finish_with_equal_releases_is_finished_again(self):
        """commit-023: the finish failed at the catch-up and rolled back - both on v2, replicating: nothing in the
        releases, but the recorded failure. Update portal runs the finish; a finish that completes clears it."""
        PortalSettings.get() and PortalSettings.objects.update(auto_update=True)
        autoupdate._save(finish_failed_tag="v2")
        out, ops = self.run_tick([], installed="v2", target="v2", behind="")
        self.assertEqual(ops, [])  # not by itself: the failure waits for the button
        self.assertIn("Update portal tries again", out)
        out, ops = self.run_tick([{"state": "done"}], installed="v2", target="v2", behind="", asked=True)
        self.assertEqual(ops, ["update-portal-finish"])
        self.assertEqual(PortalSettings.get().auto_update_state["finish_failed_tag"], "")
        out, ops = self.run_tick([], installed="v2", target="v2", behind="")
        self.assertIn("up to date", out)

    def test_the_other_nodes_release_is_read_from_its_status(self):
        with mock.patch("accounts.ha_agent.peer_request", return_value={"role": "standby", "commit": "v1"}):
            self.assertEqual(autoupdate._unfinished_pair("v2"), "v1")
            self.assertEqual(autoupdate._unfinished_pair("v1"), "")
        with mock.patch("accounts.ha_agent.peer_request", return_value={"role": "active", "commit": "v1"}):
            self.assertEqual(autoupdate._unfinished_pair("v2"), "")
        with mock.patch("accounts.ha_agent.peer_request", side_effect=OSError("no route")):
            self.assertEqual(autoupdate._unfinished_pair("v2"), "")

    def test_the_same_release_not_replicating_is_unfinished(self):
        """integration-002 (full review 2026-11-16): a finish that installed the release and then stopped."""
        follows = {"io": "Yes", "sql": "Yes", "source": ""}
        for replica, want in (
            (follows, ""),
            ({**follows, "sql": "No"}, "v2"),
            ({}, "v2"),  # not replicating at all
            ({**follows, "source": "10.9.9.9"}, "v2"),  # from another node
        ):
            st = {"role": "standby", "commit": "v2", "db": {"replica": replica}}
            with self.subTest(replica), mock.patch("accounts.ha_agent.peer_request", return_value=st):
                self.assertEqual(autoupdate._unfinished_pair("v2"), want)
        st = {"role": "standby", "commit": "v2", "db": {"error": "mysql down"}}
        with mock.patch("accounts.ha_agent.peer_request", return_value=st):
            self.assertEqual(autoupdate._unfinished_pair("v2"), "")  # not known: left alone

    def test_off_by_default_and_only_on_the_active_node(self):
        self.assertFalse(PortalSettings.get().auto_update)
        self.assertEqual(self.run_tick([]), ("off (the licence was checked in)", []))
        PortalSettings.get() and PortalSettings.objects.update(auto_update=True)
        self.node.set(ROLE="standby")
        self.assertEqual(self.run_tick([])[1], [])

    def test_one_operation_for_both_nodes_and_a_failure_stops_it(self):
        PortalSettings.get() and PortalSettings.objects.update(auto_update=True)
        out, ops = self.run_tick([{"state": "failed", "error": "pip failed", "steps": []}])
        self.assertEqual(ops, ["update-portal"])
        self.assertIn("failed", out)
        self.assertEqual(PortalSettings.get().auto_update_state["failed_tag"], "v2")
        self.assertIn("stopped", mail.outbox[-1].subject)
        out, ops = self.run_tick([])
        self.assertEqual(ops, [])  # v2 is not tried again
        self.assertIn("failed before", out)
        out, ops = self.run_tick([{"state": "done"}], target="v3")
        self.assertEqual(ops, ["update-portal"])  # its second half runs on the other node, which serves v3 now
        self.assertIn("finishes the update", out)
        self.assertEqual(self.run_tick([], installed="v3", target="v3")[0], "up to date (v3)")

    def test_a_standby_after_the_switch_records_through_the_other_node(self):
        """commit-013: after update-portal's first half this node is a standby (read only): the result, or the
        failure and its e-mail, go to the other node, which is active now."""
        PortalSettings.get() and PortalSettings.objects.update(auto_update=True)

        def switched(result):
            def run(name, **kw):
                self.node.set(ROLE="standby")
                return result

            return run

        for result, want in (
            ({"state": "done"}, {"result": "v2: installed; the other node switches back", "failed_tag": ""}),
            ({"state": "failed", "error": "the other node did not start", "steps": []}, {"failed_tag": "v2"}),
        ):
            self.node.set(ROLE="active")
            before = dict(PortalSettings.get().auto_update_state or {})
            with (
                mock.patch("accounts.checkin.configured", return_value=True),
                mock.patch("accounts.checkin.usage", return_value={}),
                mock.patch(
                    "accounts.checkin.check_in", return_value=(True, {"state": "active", "target_version": "v2"})
                ),
                mock.patch("accounts.ha_agent.runs_enterprise", return_value=True),
                mock.patch("accounts.ha_agent.installed_status", return_value={"installed_tag": "v1"}),
                mock.patch("accounts.ha_agent.read_op", return_value={}),
                mock.patch("accounts.ha_agent.run_op", side_effect=switched(result)),
                mock.patch("accounts.autoupdate._save", wraps=autoupdate._save) as save,
                mock.patch("accounts.ha_agent.peer_request") as peer,
            ):
                autoupdate.tick(log=lambda *_a: None, force=True)
            calls = [c for c in peer.call_args_list if c.args[2] == "/update-state"]
            self.assertEqual(len(calls), 1)
            sent = calls[0].args[3]
            self.assertEqual({k: sent["state"][k] for k in want}, want)
            self.assertEqual(bool(sent["notify"]), result["state"] == "failed")
            # nothing about the release itself is written here once this node is a standby
            self.assertFalse([c for c in save.call_args_list if "result" in c.kwargs or "failed_tag" in c.kwargs])
            self.assertEqual(
                {k: (PortalSettings.get().auto_update_state or {}).get(k) for k in ("result", "failed_tag")},
                {k: before.get(k) for k in ("result", "failed_tag")},
            )

    def test_tasks_running_wait_for_the_next_round(self):
        PortalSettings.get() and PortalSettings.objects.update(auto_update=True)
        from .models import Task

        Task.objects.create(kind="x", title="busy", state="running")
        out, ops = self.run_tick([])
        self.assertEqual((out, ops), ("tasks are running: next round", []))


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
        self.assertContains(r, "install new releases by themselves")
        self.assertContains(r, "Update OS")  # owner 2026-11-15: the actions on top, one node too
        self.assertContains(r, "Update portal")
        self.assertContains(r, "Change encryption key")
        self.assertNotContains(r, "Switch over")
        self.assertNotContains(r, "Test address")


class UpdateOsOnOneNode(TestCase):
    """Owner 2026-11-15: "Update OS" on a node with no other node - its OS updates, rebooted if they need it."""

    def test_the_button_and_the_operation(self):
        import os
        import tempfile

        from . import ha_agent

        sa = staff(is_super_admin=True)
        self.client.force_login(sa)
        otp(self.client)
        with NodeFile(SITE="dc1", ROLE="active"), mock.patch("accounts.ha_agent.start_op") as start:
            self.client.post(reverse("portal_settings"), {"section": "update_os", "tab": "portal"})
        start.assert_called_once_with("update-os-single", sa.email)
        state = tempfile.mkdtemp()
        with (
            NodeFile(SITE="dc1", ROLE="active"),
            mock.patch.object(ha_agent, "OP_FILE", os.path.join(state, "op.json")),
            mock.patch.object(ha_agent, "STATE_DIR", state),
            mock.patch.object(ha_agent, "os_patch", return_value={"result": "3 upgraded; rebooting"}) as patch,
        ):
            data = ha_agent.run_op("update-os-single", by=sa.email)
        self.assertEqual(data["state"], "done", data)
        patch.assert_called_once_with(reboot=True)
