"""owner 2026-11-08 (editions): Standard / Enterprise, and the rule that an edition the control plane has not named
is not enforced — a running installation must never lose its second node or its S3 backups because the control plane
predates editions."""

import unittest
from unittest import mock

from django.test import TestCase

from accounts import edition
from accounts.testkit import NodeFile
from tenants.models import PortalSettings

ALL = edition.FEATURES


class FeaturesFrom(TestCase):
    """features_from() is pure: a cached check-in answer in, a feature map out."""

    def test_nothing_said_enforces_nothing(self):
        for state in ({}, {"checked_at": "t", "lic_state": "active"}, None, [], "junk"):
            self.assertEqual(edition.features_from(state), {f: True for f in ALL})

    def test_standard_switches_everything_off(self):
        self.assertEqual(edition.features_from({"edition": "standard"}), {f: False for f in ALL})

    def test_enterprise_switches_everything_on(self):
        self.assertEqual(edition.features_from({"edition": "enterprise"}), {f: True for f in ALL})

    def test_name_is_tolerant_but_unknown_means_not_said(self):
        self.assertEqual(edition.features_from({"edition": " Standard "}), {f: False for f in ALL})
        self.assertEqual(edition.features_from({"edition": "pro"}), {f: True for f in ALL})

    def test_explicit_features_override_the_edition(self):
        got = edition.features_from({"edition": "standard", "features": {"s3_backups": True}})
        self.assertTrue(got["s3_backups"])
        self.assertFalse(got["second_node"])
        got = edition.features_from({"edition": "enterprise", "features": {"preview_stream": False}})
        self.assertFalse(got["preview_stream"])

    def test_nodes_is_a_synonym_for_second_node(self):
        self.assertTrue(edition.features_from({"edition": "standard", "features": {"nodes": 2}})["second_node"])
        self.assertFalse(edition.features_from({"edition": "enterprise", "features": {"nodes": 1}})["second_node"])
        self.assertTrue(edition.features_from({"features": {"nodes": "junk"}})["second_node"])
        self.assertTrue(edition.features_from({"features": {"nodes": 1, "second_node": True}})["second_node"])

    def test_bad_shapes_do_not_raise(self):
        for junk in ({"features": "no"}, {"features": None}, {"features": []}):
            edition.features_from(junk)


class FromSettings(TestCase):
    def setUp(self):
        self.s = PortalSettings.get()

    def _state(self, **kw):
        self.s.auto_update_state = kw
        self.s.save(update_fields=["auto_update_state"])

    @unittest.skipIf(
        edition.standard_build(), "a Standard build keeps Standard's limits whatever the licence says (editions step 5)"
    )
    def test_pre_edition_installation_is_unrestricted(self):
        self._state(checked_at="t", source="control-plane", lic_state="active")
        self.assertEqual(edition.current(self.s), "")
        self.assertFalse(edition.is_standard(self.s))
        self.assertTrue(edition.allows("second_node", self.s))
        self.assertTrue(edition.allows("s3_backups", self.s))
        self.assertEqual(edition.fixed(self.s), {})

    def test_standard_is_restricted_and_pins_its_values(self):
        self._state(edition="standard")
        self.assertTrue(edition.is_standard(self.s))
        self.assertFalse(edition.allows("second_node", self.s))
        self.assertEqual(edition.fixed(self.s)["backup_keep_days"], 3)
        self.assertEqual(edition.fixed(self.s)["update_stream"], "stable")

    @unittest.skipIf(
        edition.standard_build(), "a Standard build keeps Standard's limits whatever the licence says (editions step 5)"
    )
    def test_enterprise_allows_everything(self):
        self._state(edition="enterprise")
        self.assertEqual(edition.current(self.s), "enterprise")
        self.assertTrue(all(edition.features(self.s).values()))
        self.assertEqual(edition.fixed(self.s), {})

    def test_unknown_feature_name_is_allowed(self):
        self._state(edition="standard")
        self.assertTrue(edition.allows("something_new", self.s))


class CachedByCheckIn(TestCase):
    """autoupdate caches the edition only when the control plane names it (accounts/autoupdate.py)."""

    def setUp(self):
        self.s = PortalSettings.get()

    def _merge(self, **state):  # the merge _save() performs
        self.s.refresh_from_db()
        self.s.auto_update_state = {**(self.s.auto_update_state or {}), **state}
        self.s.save(update_fields=["auto_update_state"])
        return self.s.auto_update_state

    def test_an_answer_without_an_edition_keeps_the_cached_one(self):
        self._merge(edition="standard")
        self._merge(checked_at="t2", lic_state="active")  # no edition in the answer -> nothing passed
        self.assertEqual(edition.current(self.s), "standard")

    def test_an_answer_that_names_one_replaces_it(self):
        self._merge(edition="standard")
        self._merge(edition="enterprise")
        self.assertEqual(edition.current(self.s), "enterprise")


class ControlPlaneContract(TestCase):
    """The exact payloads cp.example.com sends (job 2026-11-08_from-claugine-web-editions-agreed, 2026-11-08).

    It sends `nodes`, not `second_node`; the portal must map it. Standard {1, false...}, Enterprise {2, true...}.
    """

    STANDARD = {
        "nodes": 1,
        "s3_backups": False,
        "editable_backup_settings": False,
        "editable_update_settings": False,
        "preview_stream": False,
    }
    ENTERPRISE = {
        "nodes": 2,
        "s3_backups": True,
        "editable_backup_settings": True,
        "editable_update_settings": True,
        "preview_stream": True,
    }

    def test_standard_payload(self):
        got = edition.features_from({"edition": "standard", "features": self.STANDARD})
        self.assertEqual(got, {f: False for f in ALL})

    def test_enterprise_payload(self):
        got = edition.features_from({"edition": "enterprise", "features": self.ENTERPRISE})
        self.assertEqual(got, {f: True for f in ALL})

    def test_features_win_over_a_mismatched_edition(self):
        """If the two ever disagree, the explicit map decides — it is the finer-grained statement."""
        got = edition.features_from({"edition": "standard", "features": self.ENTERPRISE})
        self.assertEqual(got, {f: True for f in ALL})

    def test_edition_alone_still_works_if_features_are_absent(self):
        """The contract says both are sent, but the portal must not depend on `features` being there."""
        self.assertEqual(edition.features_from({"edition": "standard"}), {f: False for f in ALL})
        self.assertEqual(edition.features_from({"edition": "enterprise"}), {f: True for f in ALL})


class Effective(TestCase):
    """Settings are read through the edition where they are USED, so a Standard installation obeys its limits even
    when the stored values say otherwise (a restore from an Enterprise backup, a direct database edit)."""

    def setUp(self):
        self.s = PortalSettings.get()
        # Enterprise-looking values left in the database on purpose
        self.s.backup_local = True
        self.s.backup_path = "/srv/elsewhere"
        self.s.backup_keep_days = 99
        self.s.backup_s3 = True
        self.s.update_stream = "all"
        self.s.save()

    def _edition(self, name):
        self.s.auto_update_state = {"edition": name} if name else {}
        self.s.save(update_fields=["auto_update_state"])

    def test_standard_overrides_the_stored_values(self):
        self._edition("standard")
        self.assertEqual(edition.effective(self.s, "backup_keep_days"), 3)
        self.assertFalse(edition.effective(self.s, "backup_s3"))
        self.assertEqual(edition.effective(self.s, "update_stream"), "stable")
        self.assertEqual(edition.effective(self.s, "backup_start"), "03:06")

    @unittest.skipIf(
        edition.standard_build(), "a Standard build keeps Standard's limits whatever the licence says (editions step 5)"
    )
    def test_enterprise_and_not_said_keep_the_stored_values(self):
        for name in ("enterprise", ""):
            self._edition(name)
            self.assertEqual(edition.effective(self.s, "backup_keep_days"), 99)
            self.assertTrue(edition.effective(self.s, "backup_s3"))
            self.assertEqual(edition.effective(self.s, "update_stream"), "all")
            self.assertEqual(edition.effective(self.s, "backup_path"), "/srv/elsewhere")

    def test_a_setting_that_is_never_pinned_passes_through(self):
        self._edition("standard")
        self.assertEqual(edition.effective(self.s, "backup_s3_bucket"), self.s.backup_s3_bucket)

    def test_the_pinned_backup_path_expands_against_the_backup_owner(self):
        """owner 2026-11-08: ~/claugine/backups in the service account's home — not root's, though backups run as root."""
        import os
        import pwd

        self._edition("standard")
        me = pwd.getpwuid(os.getuid())
        self.s.backup_owner = f"{me.pw_name}:{me.pw_name}"
        self.s.save(update_fields=["backup_owner"])
        self.assertEqual(edition.effective(self.s, "backup_path"), os.path.join(me.pw_dir, "claugine/backups"))
        self.assertFalse(edition.effective(self.s, "backup_path").startswith("~"))

    def test_an_unknown_backup_owner_falls_back_without_raising(self):
        self._edition("standard")
        for who in ("", "nosuchuser:nosuchgroup", "999999:999999"):
            self.s.backup_owner = who
            self.s.save(update_fields=["backup_owner"])
            got = edition.effective(self.s, "backup_path")
            self.assertTrue(got.endswith("claugine/backups"), got)
            self.assertFalse(got.startswith("~"), got)

    @unittest.skipIf(
        edition.standard_build(), "a Standard build keeps Standard's limits whatever the licence says (editions step 5)"
    )
    def test_locked_names(self):
        self._edition("standard")
        self.assertIn("backup_s3", edition.locked(self.s))
        self._edition("enterprise")
        self.assertEqual(edition.locked(self.s), set())


class GatedAtPointOfUse(TestCase):
    """The routing itself: appliance and autoupdate must read through the edition."""

    def setUp(self):
        self.s = PortalSettings.get()
        self.s.backup_path = "/srv/elsewhere"
        self.s.backup_start = "23:45"
        self.s.backup_local = True
        self.s.auto_update_state = {"edition": "standard"}
        self.s.save()

    def test_the_backup_folder_is_the_pinned_one(self):
        import os
        import pwd

        from accounts import appliance

        # owner 2026-11-08: no subfolder, and ~ is the backup owner's home
        me = pwd.getpwuid(os.getuid())
        self.s.backup_owner = f"{me.pw_name}:{me.pw_name}"
        self.s.save(update_fields=["backup_owner"])
        self.assertEqual(appliance.local_backup_dir(self.s), os.path.join(me.pw_dir, "claugine/backups"))

    @unittest.skipIf(
        edition.standard_build(), "a Standard build keeps Standard's limits whatever the licence says (editions step 5)"
    )
    def test_the_backup_time_is_the_pinned_one(self):
        from datetime import datetime

        from django.utils import timezone

        from accounts import appliance

        now = timezone.make_aware(datetime(2026, 11, 8, 12, 0))
        # slots() returns the day's single scheduled time; Standard pins it to 03:06 whatever the row says
        self.assertEqual(appliance.slots(self.s, now).strftime("%H:%M"), "03:06")
        self.s.auto_update_state = {"edition": "enterprise"}
        self.s.save(update_fields=["auto_update_state"])
        self.assertEqual(appliance.slots(self.s, now).strftime("%H:%M"), "23:45")  # the stored value again

    def test_the_restore_steps_name_the_pinned_folder(self):
        from accounts import appliance

        steps = appliance.restore_steps(self.s)
        self.assertNotIn("/srv/elsewhere", " ".join(cmd for _t, cmd in steps))

    def test_standard_checks_in_as_stable_even_when_the_database_says_all(self):
        from unittest import mock

        from accounts import autoupdate

        self.s.update_stream = "all"
        self.s.auto_update = True
        self.s.save()
        with (
            mock.patch("accounts.ha.role", return_value="active"),
            mock.patch(
                "accounts.ha_agent.installed_status",
                return_value={"installed": "c" * 40, "installed_tag": "v1", "latest": {}},
            ),
            mock.patch("accounts.checkin.configured", return_value=True),
            mock.patch("accounts.checkin.usage", return_value={}),
            mock.patch("accounts.checkin.check_in", return_value=(False, {"error": "x"})) as sent,
        ):
            autoupdate.tick(force=True)
        self.assertEqual(sent.call_args[0][1], "stable")


class BackupNaming(TestCase):
    """owner 2026-11-08: backups go straight into the configured folder; the portal's are claugine_<fqdn>_<time>
    and the platforms' are platform_<name>_<vip>_<time>. Both kinds now share one folder, so matching must be exact
    — prune_local() deletes whatever _is_archive() matches."""

    @unittest.skipIf(
        edition.standard_build(), "a Standard build keeps Standard's limits whatever the licence says (editions step 5)"
    )
    def test_the_folder_has_no_subfolder(self):
        from accounts import appliance

        s = PortalSettings.get()
        s.backup_path = "/srv/backups/claugine"
        s.auto_update_state = {}
        s.save()
        self.assertEqual(appliance.local_backup_dir(s), "/srv/backups/claugine")

    def test_a_portal_backup_is_recognised(self):
        from accounts.appliance import _is_archive

        self.assertTrue(_is_archive("claugine_my.portal.example_20261108-1906"))

    def test_a_platform_backup_is_not_taken_for_a_portal_one(self):
        """The dangerous case: both kinds share a folder and prune_local deletes what this matches."""
        from accounts.appliance import _is_archive

        self.assertFalse(_is_archive("platform_portal_10.0.0.60_20261108-0530"))
        self.assertFalse(_is_archive("claugine-platform_portal_10.0.0.60_20261108-0530"))  # the older platform name
        self.assertFalse(_is_archive("claugine-platforms"))  # the older platform folder

    def test_old_names_are_not_ours_any_more(self):
        """2026-11-12: the names from before the 2026-11-08 rename have aged out (kept 10 days) and are left alone."""
        from accounts.appliance import _is_archive

        self.assertFalse(_is_archive("claugine-portal_my.portal.example_20261105-2225"))
        self.assertFalse(_is_archive("claugine-portal-20261101-1509"))

    def test_a_half_written_backup_is_ignored(self):
        from accounts.appliance import _is_archive

        self.assertFalse(_is_archive("claugine_my.portal.example_20261108-1906.partial"))
        self.assertFalse(_is_archive(".claugine_my.portal.example_20261108-1906.partial"))

    def test_the_names_the_portal_builds_match_what_it_looks_for(self):
        """Round trip: whatever backup_name() produces must be found by _is_archive()."""
        from accounts.appliance import _is_archive, portal_backup_name

        s = PortalSettings.get()
        self.assertTrue(_is_archive(portal_backup_name(s)))
        self.assertTrue(portal_backup_name(s).startswith("claugine_"))

    def test_the_platform_prefix(self):
        from accounts import fe_backup

        self.assertEqual(fe_backup.PREFIX, "platform_")
        self.assertEqual(fe_backup.FOLDER, "")


class PruneScope(TestCase):
    """owner 2026-11-08: prune claugine_<fqdn>_* for the portal and platform_* for the platforms, nothing else.

    One folder can now hold this portal's backups, another portal's, and the platforms'. prune_local() deletes what
    _is_archive() matches, so the scope is what stands between a nightly prune and someone else's backups.
    """

    def setUp(self):
        self.s = PortalSettings.get()
        self.s.public_url = "https://my.portal.example"
        self.s.save()

    def test_our_own_backups_are_in_scope(self):
        from accounts.appliance import _is_archive, portal_backup_name

        self.assertTrue(_is_archive(portal_backup_name(self.s), self.s))
        self.assertTrue(_is_archive("claugine_my.portal.example_20261108-1906", self.s))

    def test_another_portals_backups_are_never_pruned(self):
        from accounts.appliance import _is_archive

        other = "claugine_other.portal.example_20261108-1906"
        self.assertTrue(_is_archive(other))  # it is a portal backup
        self.assertFalse(_is_archive(other, self.s))  # but not ours, so we must not delete it

    def test_the_platforms_backups_are_never_pruned_by_the_portal(self):
        from accounts.appliance import _is_archive

        for name in (
            "platform_portal_10.0.0.60_20261108-0530",
            "claugine-platform_portal_10.0.0.60_20261108-0530",
            "claugine-platforms",
        ):
            self.assertFalse(_is_archive(name, self.s))
            self.assertFalse(_is_archive(name))

    def test_prune_deletes_only_what_is_in_scope(self):
        import os
        import tempfile
        import time

        from accounts.appliance import prune_local

        folder = tempfile.mkdtemp()
        names = [
            "claugine_my.portal.example_20261101-0100",  # ours - goes
            "claugine_other.portal.example_20261101-0100",  # another portal - stays
            "platform_portal_10.0.0.60_20261101-0100",  # a platform - stays
            "claugine_my.portal.example_20261108-1906",
        ]  # ours but fresh - stays
        old = time.time() - 40 * 86400
        for n in names:
            os.makedirs(os.path.join(folder, n))
            if not n.endswith("1906"):
                os.utime(os.path.join(folder, n), (old, old))
        removed = prune_local(folder, 10, self.s)
        left = sorted(os.listdir(folder))
        self.assertEqual(removed, 1)
        self.assertEqual(
            left,
            sorted(
                [
                    "claugine_other.portal.example_20261101-0100",
                    "platform_portal_10.0.0.60_20261101-0100",
                    "claugine_my.portal.example_20261108-1906",
                ]
            ),
        )


class BackupOwnership(TestCase):
    """owner 2026-11-08: "just assign sa user as owner to each created backup dir/file".

    The folder is shared with the platforms' backups now, so a run must stamp only what it created — walking the
    whole folder would restamp the other product's files (and anything else the operator keeps there).
    """

    def setUp(self):
        import tempfile

        self.s = PortalSettings.get()
        self.dir = tempfile.mkdtemp()
        self.s.backup_path = self.dir
        self.s.backup_owner = ""
        self.s.auto_update_state = {}
        self.s.save()

    def test_the_configured_owner_wins(self):
        import os
        import pwd

        from accounts.appliance import _owner

        self.s.backup_owner = f"{pwd.getpwuid(os.getuid()).pw_name}"
        self.s.save()
        self.assertEqual(_owner(self.dir, self.s)[0], os.getuid())

    def test_without_one_it_inherits_the_configured_folder_not_its_parent(self):
        """The subfolder is gone, so the folder to inherit from is the configured one itself."""
        import os

        from accounts.appliance import _owner

        self.assertEqual(_owner(self.dir, self.s), (os.stat(self.dir).st_uid, os.stat(self.dir).st_gid))

    def test_fix_one_touches_a_single_path(self):
        import os

        from accounts.appliance import DIR_MODE, _fix_one

        sibling = os.path.join(self.dir, "platform_cloud_10.0.0.30_20261108-0530")
        os.makedirs(sibling)
        os.chmod(sibling, 0o777)
        _fix_one(self.dir, None)
        self.assertEqual(oct(os.stat(self.dir).st_mode & 0o777), oct(DIR_MODE))
        self.assertEqual(oct(os.stat(sibling).st_mode & 0o777), "0o777")  # untouched

    def test_fix_modes_walks_only_what_it_is_given(self):
        import os

        from accounts.appliance import DIR_MODE, FILE_MODE, _fix_modes

        ours = os.path.join(self.dir, "claugine_my.portal.example_20261108-1906")
        os.makedirs(ours)
        open(os.path.join(ours, "database.sql.gz"), "w").close()
        theirs = os.path.join(self.dir, "platform_cloud_10.0.0.30_20261108-0530")
        os.makedirs(theirs)
        f = os.path.join(theirs, "MANIFEST")
        open(f, "w").close()
        os.chmod(theirs, 0o777)
        os.chmod(f, 0o666)
        _fix_modes(ours, None)
        self.assertEqual(oct(os.stat(ours).st_mode & 0o777), oct(DIR_MODE))
        self.assertEqual(oct(os.stat(os.path.join(ours, "database.sql.gz")).st_mode & 0o777), oct(FILE_MODE))
        self.assertEqual(oct(os.stat(theirs).st_mode & 0o777), "0o777")  # the platforms' backup is not ours to stamp
        self.assertEqual(oct(os.stat(f).st_mode & 0o777), "0o666")


class HaSwitch(TestCase):
    """Phase 4: accounts/ha.py is the switch. Step 2 (2026-11-11): the pair is ee/ha.py; without ee/ one node."""

    def reload_without_ee(self):
        import importlib

        from accounts import edition, ha

        with mock.patch.object(edition, "ee", return_value=None):
            importlib.reload(ha)
        self.addCleanup(importlib.reload, ha)
        return ha

    def test_with_ee_the_pair(self):
        pair = edition.ee("ha")  # by name, never `import ee…`: this file is in the public snapshot too
        if pair is None:
            self.skipTest("a Standard build: no ee/")
        from accounts import ha

        self.assertIs(ha.IMPL, pair)
        self.assertEqual(ha.ROLES, ("active", "standby", "maintenance", "new"))
        self.assertIs(ha.RoleMiddleware, pair.RoleMiddleware)

    def test_without_ee_one_node(self):
        from accounts import single

        ha = self.reload_without_ee()
        self.assertIs(ha.IMPL, single)
        self.assertEqual(ha.ROLES, ("active", "maintenance"))
        self.assertFalse(ha.has_peer())
        with NodeFile(SITE="dc1", ADDR="10.0.0.25", PEER_ADDR="10.0.1.25", ROLE="standby"):
            self.assertEqual(ha.role(), "maintenance")  # half of a pair: never claims /active (2026-11-09 incident)
        with NodeFile(SITE="dc1", ROLE="standby"):
            self.assertEqual(ha.role(), "active")  # one node restored from a pair: serves

    def test_a_leftover_ee_directory_is_not_enterprise(self):
        """ee/ with only __pycache__ in it (an install over an Enterprise build) is a namespace package: absent."""
        import importlib.machinery

        ns = importlib.machinery.ModuleSpec("ee", None, is_package=True)
        with (
            mock.patch("importlib.util.find_spec", return_value=ns),
            mock.patch("importlib.import_module", side_effect=AssertionError("must not import")),
        ):
            self.assertIsNone(edition.ee("ha"))

    def test_one_exception_class(self):
        from accounts import ha, single

        self.assertIs(ha.ReadOnlyHere, single.ReadOnlyHere)
        self.assertIs(ha.IMPL.ReadOnlyHere, single.ReadOnlyHere)

    def test_no_public_module_imports_ee(self):
        """The public snapshot drops ee/: nothing outside it may import it by name (publish-portal.sh checks too)."""
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        bad = []
        for f in root.rglob("*.py"):
            if "ee" in f.relative_to(root).parts[:1] or ".venv" in f.parts or "node_modules" in f.parts:
                continue
            if re.search(
                r"^\s*(from ee[. ]|import ee$|import ee[. ])", f.read_text(encoding="utf-8", errors="ignore"), re.M
            ):
                bad.append(str(f.relative_to(root)))
        self.assertEqual(bad, [])

    def test_patching_role_still_governs_the_rest(self):
        from unittest import mock

        from accounts import ha

        with mock.patch("accounts.ha.role", return_value="new"):
            self.assertFalse(ha.is_active())
            with self.assertRaises(ha.ReadOnlyHere):
                ha.forbid_writes("a test")


class StandardBuild(TestCase):
    """Editions split step 5 (2026-11-11): a build without ee/ keeps Standard's limits whatever the control plane
    says — not said, or an Enterprise licence before its upgrade — because the code that lifts them is not in it."""

    def test_the_pins_hold_without_ee(self):
        s = PortalSettings.get()
        for state in ({}, {"edition": "enterprise"}):
            s.auto_update_state = state
            with mock.patch.object(edition, "standard_build", return_value=True):
                self.assertEqual(edition.effective(s, "backup_keep_days"), edition.STANDARD_FIXED["backup_keep_days"])
                self.assertFalse(edition.allows("editable_backup_settings", s))
                self.assertFalse(edition.allows("preview_stream", s))
                self.assertFalse(edition.allows("s3_backups", s))
                # second_node stays the licence's (not said: allowed, as before editions), so an Enterprise licence
                # is still offered the upgrade to the Enterprise releases
                self.assertTrue(edition.allows("second_node", s))
