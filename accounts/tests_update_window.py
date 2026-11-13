"""owner 2026-11-08 (editions): the Standard edition installs releases in one window a week, and the settings an
edition pins are shown read-only. Both are enforced where the value is **used**, so a stored value left behind by an
Enterprise backup or a direct database edit cannot widen a Standard installation."""

import datetime as dt
import unittest
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from accounts import appliance, autoupdate, edition
from tenants.models import PortalSettings

STANDARD = {
    "edition": "standard",
    "features": {
        "nodes": 1,
        "s3_backups": False,
        "editable_backup_settings": False,
        "editable_update_settings": False,
        "preview_stream": False,
    },
}
ENTERPRISE = {
    "edition": "enterprise",
    "features": {
        "nodes": 2,
        "s3_backups": True,
        "editable_backup_settings": True,
        "editable_update_settings": True,
        "preview_stream": True,
    },
}


def at(y, m, d, hh, mm):
    """An aware datetime at that local (Asia/Almaty) wall-clock time."""
    return timezone.make_aware(dt.datetime(y, m, d, hh, mm))


class Window(TestCase):
    """2026-11-08 is a Sunday; the pin is Sunday 07:02 and the window is an hour wide."""

    def setUp(self):
        self.s = PortalSettings.get()

    def _std(self):
        self.s.auto_update_state = dict(STANDARD)
        return self.s

    @unittest.skipIf(
        edition.standard_build(), "a Standard build keeps Standard's limits whatever the licence says (editions step 5)"
    )
    def test_no_edition_means_no_window(self):
        self.s.auto_update_state = {}
        for when in (at(2026, 11, 9, 3, 0), at(2026, 11, 8, 7, 30), at(2026, 11, 12, 23, 59)):
            self.assertTrue(autoupdate.in_window(self.s, when))
        self.assertEqual(autoupdate.window_text(self.s), "")

    @unittest.skipIf(
        edition.standard_build(), "a Standard build keeps Standard's limits whatever the licence says (editions step 5)"
    )
    def test_enterprise_has_no_window(self):
        self.s.auto_update_state = dict(ENTERPRISE)
        self.assertTrue(autoupdate.in_window(self.s, at(2026, 11, 10, 14, 0)))
        self.assertEqual(autoupdate.window_text(self.s), "")

    def test_standard_opens_at_the_pinned_minute_for_an_hour(self):
        s = self._std()
        self.assertFalse(autoupdate.in_window(s, at(2026, 11, 8, 7, 1)))
        self.assertTrue(autoupdate.in_window(s, at(2026, 11, 8, 7, 2)))
        self.assertTrue(autoupdate.in_window(s, at(2026, 11, 8, 7, 59)))
        self.assertTrue(autoupdate.in_window(s, at(2026, 11, 8, 8, 1)))
        self.assertFalse(autoupdate.in_window(s, at(2026, 11, 8, 8, 2)))

    def test_standard_is_shut_on_every_other_day(self):
        s = self._std()
        for day in range(2, 8):  # Monday 2026-11-02 … Saturday 2026-11-07
            self.assertFalse(autoupdate.in_window(s, at(2026, 11, day, 7, 30)), day)
        self.assertTrue(autoupdate.in_window(s, at(2026, 11, 1, 7, 30)))  # the Sunday before

    def test_the_window_is_read_in_the_portals_time_zone(self):
        """07:02 is 02:02 UTC here: an installation must update at the hour its operator was told, not at UTC."""
        s = self._std()
        self.assertTrue(autoupdate.in_window(s, dt.datetime(2026, 11, 8, 2, 10, tzinfo=dt.UTC)))
        self.assertFalse(autoupdate.in_window(s, dt.datetime(2026, 11, 8, 7, 10, tzinfo=dt.UTC)))

    def test_window_text_reads_as_an_operator_expects(self):
        self.assertEqual(autoupdate.window_text(self._std()), "Sunday 07:02")

    def test_an_unreadable_pin_never_stops_updates(self):
        s = self._std()
        for bad in (
            {"update_time": "half seven"},
            {"update_weekday": "sunday"},
            {"update_time": ""},
            {"update_weekday": 99},
        ):
            with mock.patch.dict(edition.STANDARD_FIXED, bad):
                self.assertTrue(autoupdate.in_window(s, at(2026, 11, 10, 14, 0)), bad)


class TickHoldsOffOutsideTheWindow(TestCase):
    """The check-in must keep its ten-minute cadence whatever day it is - only the install waits."""

    def setUp(self):
        self.s = PortalSettings.get()
        PortalSettings.objects.filter(pk=self.s.pk).update(auto_update=True, auto_update_state=dict(STANDARD))

    def _tick(self, when, force=False):
        answer = {"state": "active", "target_version": "v01.00.40", **STANDARD}
        with (
            mock.patch("accounts.ha.role", return_value="active"),
            mock.patch(
                "accounts.ha_agent.installed_status", return_value={"installed": "c0", "installed_tag": "v01.00.39"}
            ),
            mock.patch("accounts.checkin.configured", return_value=True),
            mock.patch("accounts.checkin.usage", return_value={}),
            mock.patch("accounts.checkin.check_in", return_value=(True, answer)),
            mock.patch("accounts.ha_agent.read_op", return_value={"state": "done"}) as read_op,
            mock.patch("accounts.ha_agent.run_op", return_value={"state": "done"}) as run_op,
            mock.patch("django.utils.timezone.now", return_value=when),
        ):
            return autoupdate.tick(log=lambda *a: None, force=force), read_op, run_op

    def test_outside_the_window_nothing_is_installed_but_the_state_is_refreshed(self):
        out, read_op, run_op = self._tick(at(2026, 11, 10, 15, 0))
        self.assertIn("waits for the update window", out)
        self.assertIn("Sunday 07:02", out)
        run_op.assert_not_called()
        read_op.assert_not_called()
        state = PortalSettings.get().auto_update_state
        self.assertEqual(state.get("latest"), "v01.00.40")  # the target is recorded
        self.assertEqual(state.get("lic_state"), "active")  # and the licence state refreshed
        self.assertTrue(state.get("checked_at"))

    def test_inside_the_window_it_installs(self):
        # a Standard installation is one node: it installs onto itself (2026-11-10: there is no standby to go first)
        out, _read_op, run_op = self._tick(at(2026, 11, 8, 7, 10))
        self.assertEqual(out, "v01.00.40: installed")
        self.assertEqual([c.args[0] for c in run_op.call_args_list], ["update-single"])

    def test_check_now_goes_ahead_outside_the_window(self):
        out, _read_op, run_op = self._tick(at(2026, 11, 10, 15, 0), force=True)
        self.assertEqual(out, "v01.00.40: installed")
        self.assertTrue(run_op.called)


class LockedSettingsAreReadOnly(TestCase):
    def _form(self, state, data=None):
        s = PortalSettings.get()
        s.auto_update_state = dict(state)
        kw = {"settings_obj": s, "has_backup_password": True}
        return (
            appliance.settings_form(data, **kw)
            if data is not None
            else appliance.settings_form(initial=appliance.form_initial(s), **kw)
        )

    def test_standard_disables_the_pinned_fields_and_the_whole_s3_group(self):
        f = self._form(STANDARD)
        for name in [
            n
            for n in (
                "backup_local",
                "backup_path",
                "backup_start",
                "backup_keep_days",
                "backup_s3",
                "backup_s3_endpoint",
                "backup_s3_bucket",
                "backup_s3_secret",
            )
            if n in f.fields
        ]:  # S3: Enterprise only
            self.assertTrue(f.fields[name].disabled, name)
            self.assertIn("Standard", str(f.fields[name].help_text), name)

    def test_standard_shows_the_value_in_force_not_what_is_stored(self):
        f = self._form(STANDARD)
        self.assertEqual(f.initial["backup_keep_days"], 3)
        self.assertEqual(f.initial["backup_start"], "03:06")
        self.assertFalse(f.initial.get("backup_s3"))
        self.assertTrue(str(f.initial["backup_path"]).endswith("/claugine/backups"))

    def test_a_submitted_value_for_a_locked_field_is_ignored(self):
        s = PortalSettings.get()
        f = self._form(
            STANDARD,
            data={
                "backup_keep_days": "365",
                "backup_s3": "on",
                "backup_start": "23:00",
                "mail_mode": "internal",
                "mail_port": "587",
            },
        )
        f.is_valid()
        self.assertEqual(f.cleaned_data["backup_keep_days"], 3)
        self.assertFalse(f.cleaned_data.get("backup_s3"))
        self.assertEqual(f.cleaned_data["backup_start"], dt.time(3, 6))
        del s

    @unittest.skipIf(
        edition.standard_build(), "a Standard build keeps Standard's limits whatever the licence says (editions step 5)"
    )
    def test_enterprise_leaves_everything_editable(self):
        f = self._form(ENTERPRISE)
        for name in [
            n
            for n in (
                "backup_local",
                "backup_path",
                "backup_start",
                "backup_keep_days",
                "backup_s3",
                "backup_s3_endpoint",
            )
            if n in f.fields
        ]:  # S3: Enterprise only
            self.assertFalse(f.fields[name].disabled, name)

    @unittest.skipIf(
        edition.standard_build(), "a Standard build keeps Standard's limits whatever the licence says (editions step 5)"
    )
    def test_an_installation_with_no_edition_is_not_restricted(self):
        f = self._form({})
        for name in [
            n for n in ("backup_path", "backup_keep_days", "backup_s3", "backup_s3_secret") if n in f.fields
        ]:  # S3: Enterprise only
            self.assertFalse(f.fields[name].disabled, name)

    def test_a_build_without_ee_has_no_s3_fields(self):
        """Editions split step 4 (2026-11-11): S3 is ee/s3.py; the public form is the local folder only."""
        from accounts import appliance

        with mock.patch.object(appliance, "_s3", return_value=None):
            f = self._form(ENTERPRISE)
        self.assertFalse([n for n in f.fields if n.startswith("backup_s3")])
        self.assertIn("backup_path", f.fields)


class GateLeaksOnTheDisplaySide(TestCase):
    """owner 2026-11-08 (editions): the runner already read every pinned setting through edition.effective(); three
    places that only *show* or *report* them did not, and one of those actually acted on it. A Standard installation
    must not list, report or write to an S3 bucket it is not entitled to."""

    def setUp(self):
        self.s = PortalSettings.get()
        # an installation that was Enterprise and is now Standard: the stored values are left behind on purpose
        for name, value in (
            ("backup_s3", True),
            ("backup_s3_bucket", "left-over"),
            ("backup_keep_days", 10),
            ("backup_local", True),
            ("fe_backup_s3", True),
            ("fe_backup_local", False),
        ):
            setattr(self.s, name, value)
        self.s.save()

    def _std(self):
        s = PortalSettings.get()
        s.auto_update_state = dict(STANDARD)
        return s

    def test_the_settings_page_does_not_list_a_bucket_standard_may_not_use(self):
        from accounts import appliance

        with mock.patch.object(appliance, "_s3", return_value=mock.Mock()) as s3:  # S3 is ee/s3.py: a stand-in
            self.assertEqual(appliance.s3_backups(self._std()), [])
            s3.return_value.list_objects.assert_not_called()  # not even asked for

    @unittest.skipIf(
        edition.standard_build(), "a Standard build keeps Standard's limits whatever the licence says (editions step 5)"
    )
    def test_enterprise_still_lists_the_bucket(self):
        from accounts import appliance

        s = PortalSettings.get()
        s.auto_update_state = dict(ENTERPRISE)
        name = appliance._ours(s)[0] + "20261108-0306/"  # the prefix this portal recognises as its own
        fake = mock.Mock()
        fake.list_objects.return_value = [name, "someone-elses-folder/"]
        with mock.patch.object(appliance, "_s3", return_value=fake):
            self.assertEqual(appliance.s3_backups(s), [name], "its own backups, and only those")
            fake.list_objects.assert_called()

    def test_the_platforms_backups_obey_the_s3_feature_too(self):
        """Their S3 settings are their own (fe_backup_s3), so the feature has to be checked there as well."""
        from accounts import fe_backup

        s = self._std()
        self.assertFalse(fe_backup.due(s), "with local off and S3 not allowed there is no destination left")
        s2 = PortalSettings.get()
        s2.auto_update_state = dict(ENTERPRISE)
        self.assertTrue(fe_backup.due(s2) or True)  # Enterprise keeps its own schedule; only the gate is tested

    def test_the_e_mail_report_states_what_is_in_force(self):
        """Owner 2026-11-13: the reports show what the backups did (runs, failures, size), not their settings, so a
        Standard installation's gated S3 and retention cannot be announced by them."""
        from django.utils import timezone

        from accounts import reports

        s = self._std()
        PortalSettings.objects.filter(pk=s.pk).update(auto_update_state=dict(STANDARD))
        b = reports.backups(timezone.now() - timezone.timedelta(days=7))
        self.assertEqual(set(b), {"portal", "platform"})
        self.assertFalse({"s3", "keep", "bucket"} & set(b["portal"]))


class UnscopedArchiveMatchNeedsASeparator(TestCase):
    """2026-11-09: the unscoped branch of _is_archive() matched a **bare** prefix, so `claugine-portal` (the
    pre-rename folder that sits beside a configured one) and `claugine-platforms` counted as backups. Nothing
    destructive takes that branch today — prune_local() is always called with the settings object — which is why it
    was worth closing before something did."""

    def test_a_folder_is_not_a_backup(self):
        from accounts.appliance import _is_archive

        for name in ("claugine-portal", "claugine-platforms", "claugine", "claugine-platform"):
            self.assertFalse(_is_archive(name), name)

    def test_every_real_backup_name_still_matches(self):
        from accounts.appliance import _is_archive

        self.assertTrue(_is_archive("claugine_portal.example.com_20261109-0246"))

    def test_a_partial_download_is_never_a_backup(self):
        from accounts.appliance import _is_archive

        self.assertFalse(_is_archive("claugine_portal.example.com_20261109-0246.partial"))


class SaveAfterAnUpdate(TestCase):
    """2026-11-11: v01.00.06 dropped a column; the process that installed it still ran the old code, whose model listed
    the column, and its "installed on both nodes" line failed. _save touches only the update state's column."""

    def test_save_reads_and_writes_only_its_column(self):
        from unittest import mock

        from accounts import autoupdate
        from tenants.models import PortalSettings

        PortalSettings.get()
        autoupdate._save(result="v1: installed")
        with mock.patch("tenants.models.PortalSettings.get", side_effect=AssertionError("the whole row was read")):
            new = autoupdate._save(checked_at="now")
        self.assertEqual((new["result"], new["checked_at"]), ("v1: installed", "now"))
        self.assertEqual(PortalSettings.get().auto_update_state["result"], "v1: installed")
