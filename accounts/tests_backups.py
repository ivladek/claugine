"""u04 (owner 2026-10-31): backups to an S3 bucket (the portal's and the platforms' front ends), and the front ends'
backups that replace claugine-cli jobs/fe-backup.sh."""

import os
import shutil
import tempfile
import unittest
from datetime import UTC, datetime
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from accounts import edition, fe_backup
from tenants.models import PortalSettings

AWS_KEY, AWS_SECRET = "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


@override_settings(FIELD_ENCRYPTION_KEY="m3Jb0bC1k0qWw8bq3cJmHqk6kVqZ0b9mJxS6m0f4n1A=")
class FrontEndBackups(TestCase):
    def setUp(self):
        from inventory.models import Platform, Site

        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        s = PortalSettings.get()
        s.fe_backup_path, s.fe_backup, s.fe_backup_local = self.dir, True, True  # u06/u08: local folder + its switch
        s.set_secret("backup_password", "Archive-Pass-2026")
        s.set_secret("fe_backup_password", "Archive-Pass-2026")
        s.save()
        site = Site.objects.create(code="dc1")
        self.p = Platform.objects.create(
            key="dc1.cloud",
            site=site,
            name="cloud",
            kind="shared",
            fe_vip="10.0.0.30",
            fe_secrets="secrets.dc1.one",
            fe_mode="primary",
        )

    def fake_fetch(self, host, port, user, key, remote, out, timeout=0):
        self.calls.append((host, remote))
        with open(out, "wb") as fh:
            fh.write(b"data of " + host.encode())
        return ""

    def test_run(self):
        self.calls = []
        nodes = (0, "MASTER", [("2", "one3", "10.0.0.33", "leader"), ("0", "one1", "10.0.0.31", "follower")])
        sec = mock.Mock(**{"has.return_value": True, "get.return_value": "KEY"})
        with (
            mock.patch("accounts.fe_backup._nodes", return_value=nodes),
            mock.patch("inventory.credentials.find", return_value=sec),
            mock.patch("inventory.sshexec.fetch", side_effect=self.fake_fetch),
        ):
            result = fe_backup.run()
        self.assertTrue(result.startswith("ok: 1 platforms"), result)
        folder = self.dir  # owner 2026-11-08: no subfolder - straight into the configured folder
        [b] = os.listdir(folder)
        self.assertTrue(b.startswith("platform_cloud_10.0.0.30_"), b)  # owner 2026-11-08: platform_<name>_<vip>_…
        files = sorted(os.listdir(os.path.join(folder, b)))
        self.assertEqual(files, ["0-one1-10.0.0.31_follower.zip", "2-one3-10.0.0.33_leader.zip", "MANIFEST"])
        import pyzipper

        with pyzipper.AESZipFile(os.path.join(folder, b, "2-one3-10.0.0.33_leader.zip")) as zf:
            zf.setpassword(b"Archive-Pass-2026")
            self.assertEqual(sorted(zf.namelist()), ["config.tar.gz", "onedb-federated.sql.gz", "onedb-zone.sql.gz"])
        self.assertTrue(any("onedb backup" in r and "--federated" in r for _h, r in self.calls))
        self.assertFalse(
            any("onedb" in r for h, r in self.calls if h == "10.0.0.31")
        )  # the database from the leader only
        self.assertIsNone(PortalSettings.get().fe_backup_requested_at)

    def test_settings(self):
        from accounts.testkit import otp, staff

        sa = staff(is_super_admin=True)
        self.client.force_login(sa)
        otp(self.client)
        url = reverse("portal_settings")
        r = self.client.get(url + "?tab=backups")
        self.assertContains(r, "Platforms")
        self.assertContains(r, "dc1.cloud")
        self.client.post(
            url,
            {
                "tab": "backups",
                "section": "fe_backup",
                "fe_backup_local": "on",
                "fe_backup_time": "04:10",
                "fe_backup_keep_days": "7",
                "fe_backup_period_hours": "24",
                "fe_backup_path": self.dir,
                "fe_backup_password": "Plat-Pass-2026",
                "fe_backup_dirs": "/etc/one\n/var/lib/one/.ssh",
            },
        )
        s = PortalSettings.get()
        self.assertEqual(
            (s.fe_backup_time.hour, s.fe_backup_keep_days, fe_backup.dirs(s)), (4, 7, ["/etc/one", "/var/lib/one/.ssh"])
        )
        self.assertTrue(s.has_secret("fe_backup_password"))
        self.client.post(url, {"tab": "backups", "section": "fe_backup_now"})
        self.assertIsNotNone(PortalSettings.get().fe_backup_requested_at)
        self.assertTrue(fe_backup.due(PortalSettings.get()))


@override_settings(FIELD_ENCRYPTION_KEY="m3Jb0bC1k0qWw8bq3cJmHqk6kVqZ0b9mJxS6m0f4n1A=")
class OnceADay(TestCase):
    """u07: one backup a day per block; local and S3 are written by the same run."""

    @unittest.skipIf(
        edition.standard_build(), "a Standard build keeps Standard's limits whatever the licence says (editions step 5)"
    )
    def test_portal_due(self):
        from datetime import time as _t
        from datetime import timedelta

        from accounts import appliance
        from tenants.models import PortalSettings

        s = PortalSettings.get()
        s.backup_local, s.backup_path, s.backup_start = True, "/var/backups/x", _t(0, 45)
        s.backup_s3 = False
        now = datetime(2026, 11, 1, 14, 0, tzinfo=UTC)
        self.assertEqual(appliance.slots(s, now).time().strftime("%H:%M"), "00:45")  # the day's slot
        s.backup_last_at = now - timedelta(days=1)  # last ran yesterday
        self.assertTrue(appliance.due(s, now))  # today's backup not done yet
        s.backup_last_at = datetime(2026, 11, 1, 13, 0, tzinfo=UTC)  # already ran today, after 00:45
        self.assertFalse(appliance.due(s, now))  # once a day — not due again today
        # no destination switched on → never due
        s.backup_local, s.backup_s3 = False, False
        self.assertFalse(appliance.due(s, now))


class BackupsPage(TestCase):
    """owner 2026-11-11: both blocks have one shape, and the Platforms block lists its backups too."""

    def test_both_blocks_list_their_backups(self):
        from accounts.testkit import otp, staff

        sa = staff(is_super_admin=True)
        self.client.force_login(sa)
        otp(self.client)
        fake = {
            "local": [["claugine_my.example.com_20261111-1025", 851000, ""]],
            "s3": [],
            "fe_local": [["platform_zk1_10.201.100.200_20261111-0525", 12000000, ""]],
            "fe_s3": [],
        }
        with mock.patch("accounts.ha_agent.backups_for_page", return_value=fake):
            r = self.client.get(reverse("portal_settings") + "?tab=backups")
        self.assertContains(r, "claugine_my.example.com_20261111-1025")
        self.assertContains(r, "platform_zk1_10.201.100.200_20261111-0525")
        self.assertContains(r, "Available backups", count=2)
        self.assertContains(r, "platform_&lt;name&gt;")  # the hint is text, not a stray tag
