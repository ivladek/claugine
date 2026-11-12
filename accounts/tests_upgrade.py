"""2026-11-10: updates on a node with no other node, bundle installs, and the upgrade from Standard to Enterprise."""

import os
import tempfile
from contextlib import ExitStack
from unittest import mock

from django.test import TestCase

from tenants.models import PortalSettings

from . import autoupdate
from .testkit import NodeFile, staff

GH = {
    "branch": "main",
    "installed": "a" * 40,
    "installed_tag": "v1",
    "tags": [{"tag": "v1", "commit": "a" * 40, "date": "2026-11-09"}],
    "latest": None,
}


def answer(edition="standard", target="v2"):
    return {"state": "active", "edition": edition, "target_version": target}


class TickBase(TestCase):
    def setUp(self):
        staff(is_super_admin=True)
        self.node = NodeFile(SITE="dc1", ROLE="active")  # one node: no ADDR, no PEER_ADDR
        self.node.__enter__()
        self.addCleanup(self.node.__exit__)

    def tick(self, data, results=(), enterprise_code=False):
        ops = iter(results)
        with ExitStack() as st:
            st.enter_context(mock.patch("accounts.checkin.configured", return_value=True))
            st.enter_context(mock.patch("accounts.checkin.usage", return_value={}))
            st.enter_context(mock.patch("accounts.checkin.check_in", return_value=(True, data)))
            st.enter_context(mock.patch("accounts.ha_agent.installed_status", return_value=GH))
            st.enter_context(mock.patch("accounts.ha_agent.runs_enterprise", return_value=enterprise_code))
            st.enter_context(mock.patch("accounts.ha_agent.read_op", return_value={}))
            run = st.enter_context(mock.patch("accounts.ha_agent.run_op", side_effect=lambda name, **kw: next(ops)))
            out = autoupdate.tick(log=lambda *_a: None, force=True)
        return out, [(c.args[0], c.kwargs.get("ref")) for c in run.call_args_list]


class Tick(TickBase):
    def test_one_node_updates_itself(self):
        """There is no standby to go first: update-standby would fail on every Standard installation."""
        PortalSettings.get() and PortalSettings.objects.update(auto_update=True)
        autoupdate._save(upgrade_result="v1: Enterprise releases from now on")
        out, ops = self.tick(answer(), [{"state": "done"}])
        self.assertEqual(ops, [("update-single", "v2")], out)
        self.assertEqual(out, "v2: installed")
        # the upgrade's line is news only until the next release is in (owner 2026-11-11)
        self.assertEqual(PortalSettings.get().auto_update_state["upgrade_result"], "")

    def test_a_failed_single_update_is_not_retried(self):
        PortalSettings.get() and PortalSettings.objects.update(auto_update=True)
        out, ops = self.tick(answer(), [{"state": "failed", "error": "pip"}])
        self.assertEqual(out, "v2: failed")
        self.assertEqual(PortalSettings.get().auto_update_state["failed_tag"], "v2")
        self.assertIn("failed before", self.tick(answer())[0])

    def test_an_enterprise_licence_on_standard_code_waits_for_the_super_admin(self):
        out, ops = self.tick(answer("enterprise"))
        self.assertEqual(ops, [])
        self.assertIn("Settings", out)
        self.assertTrue(PortalSettings.get().auto_update_state["upgrade_available"])

    def test_the_requested_upgrade_installs_from_the_bundle(self):
        autoupdate._save(upgrade_requested=True, upgrade_requested_by="a@example.com")
        out, ops = self.tick(answer("enterprise"), [{"state": "done"}])
        self.assertEqual(ops, [("upgrade-enterprise", "v2")])
        st = PortalSettings.get().auto_update_state
        self.assertFalse(st["upgrade_requested"])
        self.assertIn("Enterprise edition", st["upgrade_result"])

    def test_the_upgrade_installs_the_running_release_when_nothing_is_newer(self):
        autoupdate._save(upgrade_requested=True)
        out, ops = self.tick(answer("enterprise", target=None), [{"state": "done"}])
        self.assertEqual(ops, [("upgrade-enterprise", "v1")])

    def test_enterprise_code_is_an_ordinary_update(self):
        PortalSettings.get() and PortalSettings.objects.update(auto_update=True)
        out, ops = self.tick(answer("enterprise"), [{"state": "done"}], enterprise_code=True)
        self.assertEqual(ops, [("update-single", "v2")])


class BundleIdentity(TestCase):
    def setUp(self):
        from . import ha_agent

        self.agent = ha_agent
        self.dir = tempfile.mkdtemp()
        p = mock.patch.object(ha_agent, "PREFIX", self.dir)
        p.start()
        self.addCleanup(p.stop)

    def write_commit(self, text):
        with open(os.path.join(self.dir, "COMMIT"), "w", encoding="utf-8") as fh:
            fh.write(text + "\n")

    def test_commit_reads_a_bundle_version(self):
        """A bundle has no git history: COMMIT holds the release, and the update checks compare against it."""
        self.write_commit("v00.16.10")
        self.assertEqual(self.agent.commit(), "v00.16.10")
        self.write_commit("b" * 40)  # a v00.x git commit: no longer an installation's identity
        self.assertEqual(self.agent.commit(), "")
        self.write_commit("not from GitHub")
        self.assertEqual(self.agent.commit(), "")


class BundleLabels(TestCase):
    """2026-11-11: a node on bundles showed its release fingerprint in bold and "GitHub v00.16.14" (production)."""

    def test_a_bundle_node_shows_its_release(self):
        from . import ha_views

        s = {"commit": "v00.16.14", "version": "6b29dcbd56ba"}
        ha_views._mark_bundle(s)
        self.assertEqual((s["tag"], s["bundle"]), ("v00.16.14", True))
        g = {"commit": "a" * 40}
        ha_views._mark_bundle(g)
        self.assertNotIn("bundle", g)
        ha_views._mark_bundle(None)


class LicenceStates(TickBase):
    """Owner 2026-11-11: "expired is a state". Updates run on active and grace only; the portal keeps running."""

    def test_expired_and_unknown_states_stop_updates(self):
        PortalSettings.get() and PortalSettings.objects.update(auto_update=True)
        for state in ("expired", "frozen", "conflict", "something-new"):
            with self.subTest(state):
                out, ops = self.tick({**answer(), "state": state})
                self.assertEqual(ops, [])
                self.assertIn("updates stopped", out)

    def test_grace_still_updates(self):
        PortalSettings.get() and PortalSettings.objects.update(auto_update=True)
        out, ops = self.tick({**answer(), "state": "grace"}, [{"state": "done"}])
        self.assertEqual(ops, [("update-single", "v2")])
