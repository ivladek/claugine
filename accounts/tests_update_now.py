"""claugine-web 2026-11-27 (job update-now, the contract's "Update now"): cp.example.com asks an installation to install
a version at once (update_now: true with that target_version) - outside the update window and with Automatic updates
off; every check-in reports the portal's Automatic updates setting (auto_updates)."""

import json
from contextlib import ExitStack
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from accounts import autoupdate, checkin
from accounts.tests import Base
from tenants.models import PortalSettings


def tick(answer, auto=False, window=False, state=None, peer="", op=None, installed="v01.01.60", unfinished=""):
    """One round with this answer from the control plane; returns (result, run_op calls, check_in calls)."""
    PortalSettings.objects.filter(pk=PortalSettings.get().pk).update(auto_update=auto, auto_update_state=state or {})
    with ExitStack() as st:
        st.enter_context(mock.patch.object(checkin, "configured", return_value=True))
        sent = st.enter_context(mock.patch.object(checkin, "check_in", return_value=(True, answer)))
        st.enter_context(mock.patch("accounts.ha.role", return_value="active"))
        st.enter_context(mock.patch("accounts.ha_agent.installed_status", return_value={"installed_tag": installed}))
        st.enter_context(mock.patch.object(autoupdate, "_unfinished_pair", return_value=unfinished))
        st.enter_context(mock.patch("accounts.ha_agent.read_op", return_value={}))
        st.enter_context(mock.patch("accounts.ha_agent.runs_enterprise", return_value=False))
        st.enter_context(mock.patch("accounts.tasks.running_count", return_value=0))
        st.enter_context(mock.patch("accounts.thisnode.read", return_value={"PEER_ADDR": peer} if peer else {}))
        st.enter_context(mock.patch.object(autoupdate, "in_window", return_value=window))
        run = st.enter_context(mock.patch("accounts.ha_agent.run_op", return_value=op or {"state": "done"}))
        out = autoupdate.tick(log=lambda *a: None)
    return out, run, sent


class UpdateNow(TestCase):
    def test_update_now_installs_outside_the_window_with_automatic_updates_off(self):
        out, run, _sent = tick({"state": "active", "target_version": "v01.01.62", "update_now": True})
        self.assertEqual(out, "v01.01.62: installed")
        run.assert_called_once_with("update-single", by="control plane: update now", ref="v01.01.62")
        st = PortalSettings.get().auto_update_state
        self.assertEqual((st["update_now"], st["update_now_tag"]), (True, "v01.01.62"))

    def test_without_it_nothing_changes(self):
        for ans in (
            {"state": "active", "target_version": "v01.01.62", "update_now": False},
            {"state": "active", "target_version": "v01.01.62"},  # an older control plane
        ):
            out, run, _sent = tick(ans)
            self.assertEqual(out, "off (the licence was checked in)", ans)
            run.assert_not_called()
        out, run, _sent = tick({"state": "active", "target_version": "v01.01.62"}, auto=True)
        self.assertIn("waits for the update window", out)
        self.assertNotIn("update_now", PortalSettings.get().auto_update_state)

    def test_the_setting_is_reported(self):
        for auto in (True, False):
            _out, _run, sent = tick({"state": "active", "target_version": ""}, auto=auto)
            self.assertIs(sent.call_args.kwargs["auto_updates"], auto)

    @override_settings(CHECKIN_URL="https://cp.example.com", LICENCE_KEY="k", INSTALL_ID="portal.example.com")
    def test_auto_updates_in_the_request(self):
        seen = {}

        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"{}"

        def urlopen(req, timeout):
            seen.update(json.loads(req.data))
            return R()

        with mock.patch("urllib.request.urlopen", side_effect=urlopen):
            checkin.check_in("v01.01.60", "stable", use={}, auto_updates=False)
        self.assertIs(seen["auto_updates"], False)
        seen.clear()
        with mock.patch("urllib.request.urlopen", side_effect=urlopen):
            checkin.check_in("v01.01.60", "stable", use={})
        self.assertNotIn("auto_updates", seen)


class ThePage(Base):
    def test_settings_portal_shows_the_request(self):
        self.client.force_login(self.staff("sa@corp.kz", is_super_admin=True))
        PortalSettings.objects.filter(pk=PortalSettings.get().pk).update(
            auto_update_state={"update_now": True, "update_now_tag": "v01.01.62"}
        )
        page = self.client.get(reverse("portal_settings") + "?tab=portal").content.decode()
        self.assertIn("Claugine asked this installation to install v01.01.62 now", page)
        PortalSettings.objects.filter(pk=PortalSettings.get().pk).update(auto_update_state={"update_now": False})
        page = self.client.get(reverse("portal_settings") + "?tab=portal").content.decode()
        self.assertNotIn("Claugine asked this installation", page)


class Review601602(TestCase):
    """commit-601 (a), commit-602 (review of 41ff41f, owner 2026-11-27 11:12 "fix commit-601 (a), commit-602")."""

    ASK = {"state": "active", "target_version": "v01.01.62", "update_now": True}

    def test_601_a_failed_release_is_tried_again_when_asked_again(self):
        from datetime import timedelta

        from django.utils import timezone

        failed = {"failed_tag": "v01.01.62", "failed_at": timezone.now().isoformat(), "now_reasked": False}
        out, run, _ = tick(self.ASK, state=failed)  # the same request right after the failure: waits
        self.assertIn("tries it again when asked again", out)
        run.assert_not_called()
        # cancelled (an answer says false), then asked again: tried
        tick({"state": "active", "target_version": "v01.01.62", "update_now": False}, state=failed)
        self.assertTrue(PortalSettings.get().auto_update_state["now_reasked"])
        out, run, _ = tick(self.ASK, state={**failed, "now_reasked": True})
        self.assertEqual(out, "v01.01.62: installed")
        # an hour after the failure: tried
        old = {**failed, "failed_at": (timezone.now() - timedelta(hours=1, minutes=1)).isoformat()}
        out, run, _ = tick(self.ASK, state=old)
        self.assertEqual(out, "v01.01.62: installed")
        # a plain automatic round still waits for a newer release
        out, run, _ = tick({"state": "active", "target_version": "v01.01.62"}, auto=True, window=True, state=old)
        self.assertIn("waiting for a newer release", out)
        run.assert_not_called()

    def test_601_a_failure_starts_the_wait_again(self):
        out, run, _ = tick(self.ASK, state={"now_reasked": True}, op={"state": "failed", "error": "x"})
        self.assertEqual(out, "v01.01.62: failed")
        st = PortalSettings.get().auto_update_state
        self.assertEqual((st["failed_tag"], st["now_reasked"]), ("v01.01.62", False))
        self.assertTrue(st["failed_at"])

    def test_602_an_interrupted_pair_update_is_finished_after_the_request_is_gone(self):
        out, run, _ = tick(self.ASK, peer="192.0.2.2")
        run.assert_called_once_with("update-portal", by="control plane: update now", ref="v01.01.62")
        self.assertEqual(PortalSettings.get().auto_update_state["update_now_finish"], "v01.01.62")
        st = {k: v for k, v in PortalSettings.get().auto_update_state.items() if k != "checked_at"}  # a later round
        # interrupted after the promotion: the new active node runs v01.01.62, the other one v01.01.60; the request
        # is over (update_now false), Automatic updates off, outside the window
        out, run, _ = tick(
            {"state": "active", "target_version": "v01.01.62", "update_now": False},
            state=st,
            peer="192.0.2.2",
            installed="v01.01.62",
            unfinished="v01.01.60",
        )
        run.assert_called_once_with("update-portal-finish", by="control plane: update now", ref="v01.01.62")
        self.assertEqual(PortalSettings.get().auto_update_state["update_now_finish"], "")
        # once only: a failed finish waits for Update portal again
        out, run, _ = tick(
            {"state": "active", "target_version": "v01.01.62", "update_now": False},
            state={**st, "finish_failed_tag": "v01.01.62"},
            peer="192.0.2.2",
            installed="v01.01.62",
            unfinished="v01.01.60",
        )
        self.assertIn("Update portal finishes the update", out)
        run.assert_not_called()


class Review605(TestCase):
    """commit-605 (review of 7d0e0c0, owner 2026-11-27 11:25): a failure recorded before failed_at existed."""

    ASK = {"state": "active", "target_version": "v01.01.62", "update_now": True}

    def test_605_an_old_failure_counts_from_when_it_was_recorded(self):
        from datetime import timedelta

        from django.utils import timezone

        recent = {"failed_tag": "v01.01.62", "at": timezone.now().isoformat()}
        out, run, _ = tick(self.ASK, state=recent)
        self.assertIn("tries it again when asked again", out)
        run.assert_not_called()
        old = {"failed_tag": "v01.01.62", "at": (timezone.now() - timedelta(hours=2)).isoformat()}
        out, run, _ = tick(self.ASK, state=old)
        self.assertEqual(out, "v01.01.62: installed")


class Review606(TestCase):
    """commit-606 (review of 5c5695d, owner 2026-11-27 11:35): a legacy failure keeps its time when "at" changes."""

    def test_606_an_unrelated_result_does_not_restart_the_hour(self):
        from datetime import timedelta

        from django.utils import timezone

        then = (timezone.now() - timedelta(hours=2)).isoformat()
        # the first round after the update records failed_at from "at" (nothing to install: an unrelated answer)
        tick({"state": "active", "target_version": ""}, state={"failed_tag": "v01.01.62", "at": then})
        st = PortalSettings.get().auto_update_state
        self.assertEqual(st["failed_at"], then)
        # an unrelated result rewrites "at" (as a failed Enterprise upgrade does), then Update now asks for v01.01.62
        st = {k: v for k, v in st.items() if k != "checked_at"}
        st["at"] = timezone.now().isoformat()
        out, run, _ = tick({"state": "active", "target_version": "v01.01.62", "update_now": True}, state=st)
        self.assertEqual(out, "v01.01.62: installed")

    def test_606_a_record_before_the_first_round(self):
        from datetime import timedelta

        from django.utils import timezone

        then = (timezone.now() - timedelta(hours=2)).isoformat()
        PortalSettings.objects.filter(pk=PortalSettings.get().pk).update(
            auto_update_state={"failed_tag": "v01.01.62", "at": then}
        )
        with mock.patch("accounts.ha.role", return_value="active"):
            autoupdate.record(finish_failed_tag="v01.01.62", at=timezone.now().isoformat())  # the agent's finish
        st = PortalSettings.get().auto_update_state
        self.assertEqual(st["failed_at"], then)
        out, run, _ = tick({"state": "active", "target_version": "v01.01.62", "update_now": True}, state=st)
        self.assertEqual(out, "v01.01.62: installed")
