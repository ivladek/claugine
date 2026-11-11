"""owner 2026-11-04 (migration to licensed updates): the portal's pull check-in client and the tick() that drives the
HA update from the control plane's answer (with the GitHub fallback and the no-downgrade guard)."""
import contextlib
import urllib.error
from unittest import mock

from django.test import TestCase, override_settings

from accounts import autoupdate, checkin
from tenants.models import PortalSettings


class Configured(TestCase):
    def test_configured_needs_all_three(self):
        with self.settings(CHECKIN_URL="https://cp.example", LICENCE_KEY="k", INSTALL_ID="i"):
            self.assertTrue(checkin.configured())
        with self.settings(CHECKIN_URL="", LICENCE_KEY="k", INSTALL_ID="i"):
            self.assertFalse(checkin.configured())
        with self.settings(CHECKIN_URL="https://cp.example", LICENCE_KEY="", INSTALL_ID="i"):
            self.assertFalse(checkin.configured())


@override_settings(CHECKIN_URL="https://cp.example", LICENCE_KEY="k", INSTALL_ID="i")
class CheckInClient(TestCase):
    def test_200_is_ok_with_data(self):
        payload = b'{"state":"active","target_version":"v00.11.17","notices":[]}'
        resp = mock.MagicMock()
        resp.read.return_value = payload
        resp.__enter__.return_value = resp
        with mock.patch("urllib.request.urlopen", return_value=resp):
            ok, data = checkin.check_in("v00.11.16", "stable", {})
        self.assertTrue(ok)
        self.assertEqual(data["target_version"], "v00.11.17")

    def test_401_unknown_licence_is_outage_not_state(self):
        err = urllib.error.HTTPError("u", 401, "unauthorized", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            ok, data = checkin.check_in("v00.11.16", "stable", {})
        self.assertFalse(ok)
        self.assertEqual(data["http"], 401)

    def test_network_error_is_outage(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("no route")):
            ok, data = checkin.check_in("v00.11.16", "stable", {})
        self.assertFalse(ok)
        self.assertIn("error", data)


class Tick(TestCase):
    def setUp(self):
        s = PortalSettings.get()
        s.auto_update, s.update_stream, s.auto_update_state = True, "stable", {}
        s.save()
        # a pair: these tests follow the standby-first path (a single node has its own, accounts/tests_upgrade.py)
        from .testkit import NodeFile
        self.node = NodeFile(SITE="dc1", ADDR="10.0.0.25", PEER_ADDR="10.0.1.25", ROLE="active")
        self.node.__enter__()
        self.addCleanup(self.node.__exit__)

    @contextlib.contextmanager
    def _env(self, checkin_result):
        gh = {"installed": "c1", "installed_tag": "v00.11.16", "latest": {"tag": "v00.11.99", "commit": "c9"}}
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch("accounts.ha.role", return_value="active"))
            st.enter_context(mock.patch("accounts.ha_agent.installed_status", return_value=gh))
            st.enter_context(mock.patch("accounts.ha_agent.read_op", return_value={}))
            self.run_op = st.enter_context(mock.patch("accounts.ha_agent.run_op", return_value={"state": "done"}))
            st.enter_context(mock.patch("accounts.tasks.running_count", return_value=0))
            st.enter_context(mock.patch("accounts.checkin.configured", return_value=True))
            st.enter_context(mock.patch("accounts.checkin.usage", return_value={}))
            st.enter_context(mock.patch("accounts.checkin.check_in", return_value=checkin_result))
            yield

    def _state(self):
        return PortalSettings.get().auto_update_state or {}

    def test_newer_target_runs_ha_update(self):
        with self._env((True, {"state": "active", "target_version": "v00.11.17", "notices": []})):
            out = autoupdate.tick(force=True)
        self.assertIn("installed on both nodes", out)
        self.assertEqual([c.kwargs["ref"] for c in self.run_op.call_args_list], ["v00.11.17", "v00.11.17"])

    def test_the_check_in_happens_even_when_auto_update_is_off(self):
        """owner 2026-11-09 (test VM 10.201.101.26): auto_update gates the **install**, not the check-in.

        It used to gate both, and it is off by default, so a freshly installed portal never checked in: it never
        learned its edition, and because every limit is read from what the check-in cached, a Standard installation
        enforced none of them - S3 backups, an editable schedule and window, and even a second node were all allowed.
        """
        PortalSettings.objects.update(auto_update=False)
        answer = (True, {"state": "active", "target_version": "v00.11.17", "notices": [],
                         "edition": "standard", "features": {"nodes": 1, "s3_backups": False}})
        with self._env(answer):
            out = autoupdate.tick(force=True)
        self.assertIn("off", out)
        self.assertEqual(self.run_op.call_args_list, [])        # nothing was installed
        state = self._state()                                   # but the licence answer was cached
        self.assertEqual(state.get("edition"), "standard")
        self.assertEqual(state.get("latest"), "v00.11.17")
        self.assertTrue(state.get("checked_at"))

        from accounts import edition                            # and the edition is therefore in force
        s = PortalSettings.get()
        self.assertTrue(edition.is_standard(s))
        self.assertIn("backup_path", edition.locked(s))
        self.assertFalse(edition.allows("s3_backups", s))

    def test_no_downgrade_guard(self):
        with self._env((True, {"state": "active", "target_version": "v00.11.15"})):
            out = autoupdate.tick(force=True)
        self.assertIn("no downgrade", out)
        self.run_op.assert_not_called()

    def test_frozen_freezes_updates_and_stores_notice(self):
        with self._env((True, {"state": "frozen", "target_version": None, "notices": ["Licence X ended."]})):
            out = autoupdate.tick(force=True)
        self.assertIn("frozen", out)
        self.run_op.assert_not_called()
        st = self._state()
        self.assertEqual(st["lic_state"], "frozen")
        self.assertEqual(st["notices"], ["Licence X ended."])

    def test_retired_is_terminal_even_when_a_newer_release_is_offered(self):
        """claugine-web 2026-11-09: a released installation receives no more updates.

        The control plane still answers 200 and still sends edition and features, and it may name a
        target_version - the node must not act on it.
        """
        with self._env((True, {"state": "retired", "grace_until": None, "target_version": "v99.99.99",
                               "edition": "standard", "features": {"nodes": 1},
                               "notices": ["Installation inst-1 was released on 2026-11-09: it receives no more "
                                           "updates. Decommission this node."]})):
            out = autoupdate.tick(force=True)
        self.assertIn("retired", out)
        self.run_op.assert_not_called()          # the offered release is ignored
        st = self._state()
        self.assertEqual(st["lic_state"], "retired")
        self.assertEqual(st["edition"], "standard")   # the limits still apply: retired is not unlimited
        self.assertTrue(st["notices"])

    def test_conflict_freezes_updates(self):
        with self._env((True, {"state": "conflict", "target_version": None, "notices": ["bound elsewhere"]})):
            out = autoupdate.tick(force=True)
        self.assertIn("conflict", out)
        self.run_op.assert_not_called()

    def test_outage_keeps_state_no_update(self):
        with self._env((False, {"http": 401})):
            out = autoupdate.tick(force=True)
        self.assertIn("unreachable", out)
        self.run_op.assert_not_called()
        self.assertNotIn("lic_state", self._state())  # state untouched on an outage

    def test_grace_still_updates(self):
        with self._env((True, {"state": "grace", "target_version": "v00.11.17", "grace_until": "2026-12-31",
                               "notices": ["in grace"]})):
            out = autoupdate.tick(force=True)
        self.assertIn("installed on both nodes", out)
        self.assertEqual(self._state()["lic_state"], "grace")

    def test_no_check_in_no_update(self):
        """v01.00.00: without a licence check-in there is no update at all (no GitHub fallback any more)."""
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch("accounts.ha.role", return_value="active"))
            st.enter_context(mock.patch("accounts.ha_agent.installed_status", return_value={"installed_tag": "v1"}))
            run_op = st.enter_context(mock.patch("accounts.ha_agent.run_op", return_value={"state": "done"}))
            st.enter_context(mock.patch("accounts.checkin.configured", return_value=False))
            out = autoupdate.tick(force=True)
        self.assertIn("no updates", out)
        run_op.assert_not_called()


class FqdnInstallId(TestCase):
    """Owner 2026-11-11: "use fqdn of installation instead of inst in installation id"."""

    def _sent(self, **over):
        captured = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"state": "active"}'

        def fake(req, timeout=None):
            import json
            captured.update(json.loads(req.data))
            return Resp()
        with override_settings(CHECKIN_URL="https://cp.example.com", LICENCE_KEY="CLG-x",
                               INSTALL_ID="portal.example.com", **over), \
                mock.patch("urllib.request.urlopen", side_effect=fake):
            ok, _d = checkin.check_in("v1", "stable", {})
        self.assertTrue(ok)
        return captured

    def test_no_previous_id_field(self):
        """v01.00.05: every installation has moved to its FQDN id; previous_install_id is not sent any more."""
        self.assertNotIn("previous_install_id", self._sent())
