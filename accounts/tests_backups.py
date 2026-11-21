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
            sa="secrets.dc1.sa",  # owner 2026-11-18: the backup logs in with the SA secret
        )

    def fake_fetch(self, host, port, user, key, remote, out, timeout=0):
        self.calls.append((host, remote))
        self.users = getattr(self, "users", set()) | {user}
        from accounts.tests_fe_backup import small_tgz

        with open(out, "wb") as fh:
            fh.write(small_tgz() if " tar " in remote else b"data of " + host.encode())
        return ""

    def test_run(self):
        self.calls = []
        nodes = (0, "MASTER", [("2", "one3", "10.0.0.33", "leader"), ("0", "one1", "10.0.0.31", "follower")])
        from inventory.models import Secret

        sec = mock.Mock(
            kind=Secret.Kind.USER, username="portal", **{"has.return_value": True, "get.return_value": "KEY"}
        )
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
        self.assertEqual(files, [f"{b}.7z", f"{b}.sh"])  # owner 2026-11-12: one archive and one script
        import subprocess

        # the script checks the archive and unpacks it (owner: "platform - only unpack")
        out = os.path.join(self.dir, "unpacked")
        r = subprocess.run(
            ["bash", os.path.join(folder, b, f"{b}.sh"), out], input=b"Archive-Pass-2026\n", capture_output=True
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(os.listdir(out)), ["0-one1-10.0.0.31_follower", "2-one3-10.0.0.33_leader"])
        self.assertEqual(
            sorted(os.listdir(os.path.join(out, "2-one3-10.0.0.33_leader"))),
            ["config.tar.gz", "onedb-federated.sql.gz", "onedb-zone.sql.gz"],
        )
        self.assertEqual(os.listdir(os.path.join(out, "0-one1-10.0.0.31_follower")), ["config.tar.gz"])
        self.assertIn(b"onedb restore -f onedb-zone.sql", r.stdout)  # how to put a node back, printed after unpacking
        self.assertIn("# node      2 one3 10.0.0.33 leader", open(os.path.join(folder, b, f"{b}.sh")).read())
        # a damaged archive is refused before anything is unpacked
        with open(os.path.join(folder, b, f"{b}.7z"), "ab") as fh:
            fh.write(b"x")
        r = subprocess.run(
            ["bash", os.path.join(folder, b, f"{b}.sh"), out + "2"], input=b"Archive-Pass-2026\n", capture_output=True
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(b"does not match", r.stderr)
        self.assertFalse(os.path.exists(out + "2"))
        self.assertTrue(any("onedb backup" in r and "--federated" in r for _h, r in self.calls))
        # owner 2026-11-18: the SA user, with sudo - the folders as root, the database as oneadmin
        self.assertEqual(self.users, {"portal"})
        self.assertTrue(all(r.startswith("sudo -n ") for _h, r in self.calls))
        self.assertTrue(any(r.startswith("sudo -n tar ") for _h, r in self.calls))
        self.assertTrue(any(r.startswith("sudo -n -u oneadmin sh -c ") for _h, r in self.calls))
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
class FrontEndLogin(FrontEndBackups):
    """owner 2026-11-18 (mockup 70b): "Connect to Front-Ends using" - each platform's SA secret, or the account secret
    Settings names; sudo without a password is needed, and its absence is said plainly."""

    test_run = test_settings = None  # FrontEndBackups' own, run there

    def secret(self, name, user, key=True):
        from inventory.models import Secret

        s = Secret(kind=Secret.Kind.USER, name=name)
        s.username = user
        if key:
            s.put("private_key", f"KEY-{name}")
        s.save()
        return s

    def test_the_platforms_sa_secret_or_the_chosen_one(self):
        sa = self.secret("sa-dc1", "portal")
        chosen = self.secret("backup-sa", "claugine-backup")
        self.p.sa = sa.ref
        self.p.save()
        s = PortalSettings.get()
        self.assertEqual(fe_backup.login(self.p, s), ("portal", "KEY-sa-dc1"))
        s.fe_backup_login = chosen.ref
        self.assertEqual(fe_backup.login(self.p, s), ("claugine-backup", "KEY-backup-sa"))
        s.fe_backup_login = self.secret("no-key", "x", key=False).ref
        self.assertEqual(fe_backup.login(self.p, s), ("", ""))

    def test_no_login_is_an_error_of_the_platform(self):
        self.p.sa = ""
        self.p.save()
        path, errors = fe_backup.backup_platform(self.p, self.dir, PortalSettings.get(), log=lambda m: None)
        self.assertEqual(path, "")
        self.assertIn("Connect to Front-Ends using", errors[0])

    def test_sudo_asking_for_a_password_is_said_plainly(self):
        from inventory.sshexec import SshError

        err = SshError("10.0.0.31: sudo: a password is required")
        self.assertEqual(fe_backup._why(err, "portal"), "portal cannot use sudo without a password there")
        self.assertEqual(fe_backup._why(SshError("timeout"), "portal"), "timeout")

    def test_the_setting_counts_as_a_use_of_its_secret(self):
        """commit-077: the chosen secret shows the backups among its uses, cannot be archived, and a replacement moves
        the setting to the new secret - which must have an ssh private key."""
        from inventory import credentials as cr
        from inventory import secret_ops

        chosen = self.secret("backup-sa", "claugine-backup")
        s = PortalSettings.get()
        s.fe_backup_login = chosen.ref
        s.save()
        self.assertIn(cr.BACKUP_LOGIN, cr.contributors().get(chosen.pk, []))
        self.assertTrue(secret_ops.archive_blockers(chosen))
        keyless = self.secret("keyless", "x", key=False)
        with self.assertRaises(ValueError):
            secret_ops.replace(chosen, keyless)
        new = self.secret("backup-sa-2", "claugine-backup")
        with mock.patch("accounts.tasks.start"):
            secret_ops.replace(chosen, new)
        self.assertEqual(PortalSettings.get().fe_backup_login, new.ref)

    def test_a_save_choosing_a_replaced_secret_is_refused(self):
        """commit-080: the page was read while the secret was active; a replacement moved everything to a new secret
        meanwhile - the save is checked again under the Settings row's lock and refused."""
        from accounts.testkit import otp, staff
        from inventory.models import Secret

        chosen = self.secret("backup-sa", "claugine-backup")
        Secret.objects.filter(pk=chosen.pk).update(state=Secret.State.REPLACED)
        self.client.force_login(staff(is_super_admin=True))
        otp(self.client)
        base = {"tab": "backups", "section": "fe_backup", "fe_backup_local": "on", "fe_backup_path": self.dir}
        base.update(fe_backup_time="05:25", fe_backup_keep_days="10", fe_backup_dirs="/etc/one")
        with mock.patch("inventory.credentials.ref_choices", return_value=[(chosen.ref, "backup-sa")]):
            self.client.post(reverse("portal_settings"), {**base, "fe_backup_login": chosen.ref})  # the page's list
        self.assertEqual(PortalSettings.get().fe_backup_login, "")

    def test_the_old_secret_is_replaced_before_the_lock_is_let_go(self):
        """commit-080: the old secret is marked replaced inside the transaction that holds the Settings row's lock (one
        level deeper than the caller), after the repoint - a Backups save waiting on that lock finds it replaced."""
        from django.db import transaction

        from inventory import credentials as cr
        from inventory import secret_ops
        from inventory.models import Secret

        chosen = self.secret("backup-sa", "claugine-backup")
        new = self.secret("backup-sa-2", "claugine-backup")
        s = PortalSettings.get()
        s.fe_backup_login = chosen.ref
        s.save()
        depth = lambda: len(transaction.get_connection().savepoint_ids)  # noqa: E731
        outside, seen = depth(), []
        real_repoint, real_save = cr.repoint, Secret.save

        def repoint(old, new_, user=None):
            seen.append(("repointed", depth()))
            return real_repoint(old, new_, user)

        def save(self_, *a, **kw):
            if self_.pk == chosen.pk:
                seen.append((self_.state, depth()))
            return real_save(self_, *a, **kw)

        with (
            mock.patch.object(cr, "repoint", side_effect=repoint),
            mock.patch.object(Secret, "save", save),
            mock.patch("accounts.tasks.start"),
        ):
            secret_ops.replace(chosen, new)
        self.assertEqual([x[0] for x in seen], ["repointed", Secret.State.REPLACED])
        self.assertTrue(all(d > outside for _w, d in seen), seen)  # both inside the locked transaction
        self.assertEqual(PortalSettings.get().fe_backup_login, new.ref)

    def test_the_setting_is_saved_and_checked(self):
        from accounts.testkit import otp, staff

        chosen = self.secret("backup-sa", "claugine-backup")
        self.client.force_login(staff(is_super_admin=True))
        otp(self.client)
        url = reverse("portal_settings")
        page = self.client.get(url + "?tab=backups")
        self.assertContains(page, "Connect to Front-Ends using")
        self.assertContains(page, "each platform's SA secret")
        self.assertContains(page, f'<option value="{chosen.ref}"')
        base = {"tab": "backups", "section": "fe_backup", "fe_backup_local": "on", "fe_backup_path": self.dir}
        base.update(fe_backup_time="05:25", fe_backup_keep_days="10", fe_backup_dirs="/etc/one")
        self.client.post(url, {**base, "fe_backup_login": chosen.ref})
        self.assertEqual(PortalSettings.get().fe_backup_login, chosen.ref)
        self.client.post(url, {**base, "fe_backup_login": "secrets.nowhere"})
        self.assertEqual(PortalSettings.get().fe_backup_login, chosen.ref)  # refused: kept
        self.client.post(url, {**base, "fe_backup_login": ""})
        self.assertEqual(PortalSettings.get().fe_backup_login, "")


class SelectionAndRename(FrontEndBackups):
    """commit-039: the platforms chosen for backups are read and written under the Settings row's lock by the rename
    and by the Settings save; a page read before a rename cannot put the old key back, and the save writes only its
    own fields."""

    test_run = test_settings = None  # FrontEndBackups' own, run there

    def save(self, platforms):
        from accounts.models import User
        from accounts.testkit import otp, staff

        sa = User.objects.filter(is_super_admin=True).first() or staff(is_super_admin=True)
        self.client.force_login(sa)
        otp(self.client)
        data = {
            "tab": "backups",
            "section": "fe_backup",
            "fe_backup_local": "on",
            "fe_backup_time": "04:10",
            "fe_backup_keep_days": "7",
            "fe_backup_path": self.dir,
            "fe_backup_dirs": "/etc/one",
            "fe_backup_platforms": platforms,
        }
        return self.client.post(reverse("portal_settings"), data, follow=True), sa

    def test_a_selection_saved_then_a_rename_keeps_both(self):
        from inventory import ops

        _r, sa = self.save(["dc1.cloud"])
        self.assertEqual(PortalSettings.get().fe_backup_platforms, ["dc1.cloud"])
        ops.platform_rename(sa, "dc1", "cloud", "cloud2")
        s = PortalSettings.get()
        self.assertEqual((s.fe_backup_platforms, s.fe_backup_keep_days), (["dc1.cloud2"], 7))

    def test_a_page_read_before_a_rename_cannot_put_the_old_key_back(self):
        from inventory import ops

        _r, sa = self.save(["dc1.cloud"])
        ops.platform_rename(sa, "dc1", "cloud", "cloud2")
        r, _sa = self.save(["dc1.cloud"])  # the form as it was shown before the rename
        self.assertContains(r, "The platforms changed meanwhile (dc1.cloud)")
        self.assertEqual(PortalSettings.get().fe_backup_platforms, ["dc1.cloud2"])

    def test_the_save_writes_only_its_own_fields(self):
        from accounts import views

        stale = PortalSettings.get()  # what the request read
        PortalSettings.objects.filter(pk=stale.pk).update(public_url="https://portal.example.com")  # changed meanwhile
        with mock.patch.object(views.PortalSettings, "get", return_value=stale):
            self.save([])
        s = PortalSettings.get()
        self.assertEqual((s.public_url, s.fe_backup_keep_days), ("https://portal.example.com", 7))


@override_settings(FIELD_ENCRYPTION_KEY="m3Jb0bC1k0qWw8bq3cJmHqk6kVqZ0b9mJxS6m0f4n1A=")
class DumpNode(FrontEndBackups):
    """core-019 (sixth full review): the node the database is dumped on is found from each node's own Raft state, and
    a backup without the database dump fails (and prunes nothing)."""

    test_run = test_settings = None  # FrontEndBackups' own, run there

    def fake_client(self, fes):
        class Client:
            def _call(self, method, *a):
                return "<CONFIG><FEDERATION><ZONE_ID>0</ZONE_ID><MODE>STANDALONE</MODE></FEDERATION></CONFIG>"

            def frontends(self):
                return fes

        return Client()

    def nodes(self, fes):
        from inventory.models import PlatformControl

        PlatformControl.objects.get_or_create(key=self.p.key)
        with mock.patch("adoption.collector.client_for", return_value=self.fake_client(fes)):
            return fe_backup._nodes(self.p)[2]

    def test_the_dump_node(self):
        solo = [{"id": "0", "name": "one1", "addr": "10.0.0.31", "state": "solo"}]
        self.assertEqual(self.nodes(solo), [("0", "one1", "10.0.0.31", "leader")])  # a front end without HA
        ha = [
            {"id": "0", "name": "one1", "addr": "10.0.0.31", "state": "follower"},  # the endpoint is a follower
            {"id": "1", "name": "one2", "addr": "10.0.0.32", "state": "leader"},
        ]
        self.assertEqual([n[3] for n in self.nodes(ha)], ["leader", "follower"])
        self.assertEqual(self.nodes(ha)[0][1], "one2")
        none = [{**f, "state": "candidate"} for f in ha]
        self.assertEqual([n[3] for n in self.nodes(none)], ["follower", "follower"])

    def test_no_database_dump_is_a_failed_backup(self):
        self.calls = []
        followers = (0, "STANDALONE", [("0", "one1", "10.0.0.31", "follower"), ("1", "one2", "10.0.0.32", "follower")])
        from inventory.models import Secret

        sec = mock.Mock(
            kind=Secret.Kind.USER, username="portal", **{"has.return_value": True, "get.return_value": "KEY"}
        )
        old = os.path.join(self.dir, "platform_cloud_10.0.0.30_20200101-0000")
        os.makedirs(old)
        with (
            mock.patch("accounts.fe_backup._nodes", return_value=followers),
            mock.patch("inventory.credentials.find", return_value=sec),
            mock.patch("inventory.sshexec.fetch", side_effect=self.fake_fetch),
            mock.patch("accounts.fe_backup.prune") as prune,
        ):
            result = fe_backup.run()
        self.assertTrue(result.startswith("failed"), result)
        self.assertIn("no leader among the front-end nodes", result)
        prune.assert_not_called()
        self.assertEqual(self.calls, [])  # nothing was even read

        from inventory.sshexec import SshError

        def dump_fails(host, port, user, key, remote, out, timeout=0):
            if "onedb backup" in remote:
                raise SshError("onedb: no space left")
            return self.fake_fetch(host, port, user, key, remote, out, timeout)

        leader = (0, "STANDALONE", [("0", "one1", "10.0.0.31", "leader")])
        with (
            mock.patch("accounts.fe_backup._nodes", return_value=leader),
            mock.patch("inventory.credentials.find", return_value=sec),
            mock.patch("inventory.sshexec.fetch", side_effect=dump_fails),
            mock.patch("accounts.fe_backup.prune") as prune,
        ):
            result = fe_backup.run()
        self.assertIn("the database was not dumped", result)
        prune.assert_not_called()
        self.assertEqual(sorted(os.listdir(self.dir)), ["platform_cloud_10.0.0.30_20200101-0000"])  # nothing kept


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
        self.assertContains(r, "<summary>Backups (", count=2)  # owner 2026-11-17: the backups under an arrow
        self.assertContains(r, "platform_&lt;name&gt;")  # the hint is text, not a stray tag


class InstalledRelease(TestCase):
    def test_release_tag_or_commit(self):
        """The release in a backup's script header: /opt/claugine-portal/COMMIT holds the tag (vNN.NN.NN) since
        v01.00.00; a 40-character commit is still read, anything else is not."""
        from accounts import appliance

        for text, want in (("v01.00.16\n", "v01.00.16"), ("0" * 40, "0" * 40), ("garbage", "")):
            with tempfile.NamedTemporaryFile("w", suffix=".commit") as fh:
                fh.write(text)
                fh.flush()
                with mock.patch.dict(os.environ, {"PORTAL_COMMIT_FILE": fh.name}):
                    self.assertEqual(appliance.installed_commit(), want)


def dump_with_secrets(tmp):
    """A stand-in for mysqldump that carries the stored secrets as a dump does (commit-064: the check reads the dump)."""
    from inventory import crypto

    out = os.path.join(tmp, "database.sql")
    with open(out, "w") as fh:
        fh.write("-- dump\n")
        for model, name in crypto.encrypted_fields():
            for token in model.objects.exclude(**{name: ""}).values_list(name, flat=True):
                fh.write(f"INSERT INTO `{model._meta.db_table}` (`{name}`) VALUES ('{token}');\n")
    return out


@override_settings(FIELD_ENCRYPTION_KEY="m3Jb0bC1k0qWw8bq3cJmHqk6kVqZ0b9mJxS6m0f4n1A=")
class BackupNeedsItsKey(TestCase):
    """core-048 (twenty-fifth full review): a portal backup without portal.env, or with one that holds no
    FIELD_ENCRYPTION_KEY, fails before anything is made, published or pruned - the older backups stay."""

    def setUp(self):
        from accounts import appliance

        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        s = PortalSettings.get()
        s.backup_path = self.dir
        s.set_secret("backup_password", "Archive-Pass-2026")
        s.save()
        self.old = os.path.join(self.dir, f"{appliance.ARCHIVE_PREFIX}_{appliance.portal_fqdn(s)}_20260101-0045")
        os.makedirs(self.old)
        os.utime(self.old, (0, 0))

    def backup(self, env_text):
        from accounts import appliance

        env = os.path.join(self.dir, "..", os.path.basename(self.dir) + ".env")
        self.addCleanup(lambda: os.path.exists(env) and os.unlink(env))
        if env_text is not None:
            with open(env, "w") as fh:
                fh.write(env_text)

        with (
            override_settings(ENV_FILE=env),
            mock.patch("accounts.appliance._dump", side_effect=dump_with_secrets),
            mock.patch("accounts.backup_archive.make") as make,
            mock.patch.object(appliance, "local_backup_dir", return_value=self.dir),
        ):
            r = appliance.backup(PortalSettings.get())
        make.assert_not_called()
        return r

    def test_no_portal_env_fails_and_keeps_the_older_backups(self):
        from cryptography.fernet import Fernet

        other = Fernet.generate_key().decode()
        for text in (
            None,
            "",
            "DB_NAME=portal\n",
            "FIELD_ENCRYPTION_KEY=\n",
            "FIELD_ENCRYPTION_KEY=   \n",  # commit-064: blank once trimmed
            "# FIELD_ENCRYPTION_KEY=x\n",
            "FIELD_ENCRYPTION_KEY=not-a-key\n",  # commit-064: malformed
            f"FIELD_ENCRYPTION_KEY={other}\n",  # commit-064: a key, but not the one the secrets were stored with
            "FIELD_ENCRYPTION_KEY=not-a-key\nFIELD_ENCRYPTION_KEY=m3Jb0bC1k0qWw8bq3cJmHqk6kVqZ0b9mJxS6m0f4n1A=\n",  # the first assignment counts
        ):
            with self.subTest(text=text):
                r = self.backup(text)
                self.assertTrue(r.startswith("failed:"), r)
                self.assertIn("portal.env", r)
                self.assertEqual(os.listdir(self.dir), [os.path.basename(self.old)])

    def test_a_portal_env_that_reads_the_secrets_is_taken(self):
        """commit-064: the key the secrets were stored with, alone or after a new one (a key change in progress)."""
        from cryptography.fernet import Fernet

        new = Fernet.generate_key().decode()
        for text in (
            "FIELD_ENCRYPTION_KEY=m3Jb0bC1k0qWw8bq3cJmHqk6kVqZ0b9mJxS6m0f4n1A=\n",
            f" FIELD_ENCRYPTION_KEY = {new} , m3Jb0bC1k0qWw8bq3cJmHqk6kVqZ0b9mJxS6m0f4n1A=\n",
        ):
            with (
                self.subTest(text=text),
                mock.patch("accounts.backup_archive.make"),
                mock.patch("accounts.backup_archive.write_script"),
                mock.patch("accounts.appliance.prune_local"),
            ):
                from accounts import appliance

                env = os.path.join(self.dir, "..", os.path.basename(self.dir) + ".ok.env")
                self.addCleanup(lambda e=env: os.path.exists(e) and os.unlink(e))
                with open(env, "w") as fh:
                    fh.write(text)

                with (
                    override_settings(ENV_FILE=env),
                    mock.patch("accounts.appliance._dump", side_effect=dump_with_secrets),  # the secrets, as a dump
                    mock.patch.object(appliance, "local_backup_dir", return_value=self.dir),
                ):
                    r = appliance.backup(PortalSettings.get())
                self.assertTrue(r.startswith("ok:"), r)


@override_settings(FIELD_ENCRYPTION_KEY="m3Jb0bC1k0qWw8bq3cJmHqk6kVqZ0b9mJxS6m0f4n1A=")
class BackupKeyAndRotation(TestCase):
    """commit-064, commit-071, commit-072: the copied portal.env is checked against every stored secret, in the
    database as it is under the capture lock, with the key form both restore paths accept."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.env = os.path.join(self.dir, "portal.env")
        s = PortalSettings.get()
        s.set_secret("backup_password", "Archive-Pass-2026")
        s.set_secret("mail_password", "Mail-Pass")
        s.save()

    def make(self, s, lock=None, after_dump=None):
        from accounts import appliance

        def dump(tmp):
            out = dump_with_secrets(tmp)
            if after_dump:
                after_dump()  # what changes in the database after mysqldump's snapshot
            return out

        out = os.path.join(self.dir, "out")
        with (
            override_settings(ENV_FILE=self.env),
            mock.patch("accounts.appliance._dump", side_effect=dump),
            mock.patch("accounts.backup_archive.make"),
            mock.patch("accounts.backup_archive.write_script"),
            mock.patch.object(appliance, "installed_commit", return_value=""),
            mock.patch.object(appliance, "capture_lock", lock or appliance.capture_lock),
        ):
            return appliance.make_backup(s, out)

    def test_a_secret_under_a_lost_key_fails_the_backup(self):
        """commit-064: the backup password opens with the carried key, but another secret was stored under a key
        portal.env no longer has (a rotation left half way, the old key then lost)."""
        from cryptography.fernet import Fernet

        from tenants.models import PortalSettings as PS

        old = "m3Jb0bC1k0qWw8bq3cJmHqk6kVqZ0b9mJxS6m0f4n1A="
        new = Fernet.generate_key().decode()
        with override_settings(FIELD_ENCRYPTION_KEY=f"{new},{old}"):
            s = PS.get()
            s.set_secret("backup_password", "Archive-Pass-2026")  # under the new key; mail_password under the old
            s.save()
            with open(self.env, "w") as fh:
                fh.write(f"FIELD_ENCRYPTION_KEY={new}\n")  # the old key lost
            with self.assertRaises(RuntimeError) as e:
                self.make(PS.get())
        self.assertIn("does not open 1 stored secret(s) in the dump", str(e.exception))

    def test_a_credential_changed_after_the_dump_still_counts(self):
        """commit-064: the dump carries a secret under a key portal.env no longer has; the credential is replaced in the
        live database after the dump - the backup still fails, as its dump would not restore that secret."""
        from cryptography.fernet import Fernet

        from tenants.models import PortalSettings as PS

        old = "m3Jb0bC1k0qWw8bq3cJmHqk6kVqZ0b9mJxS6m0f4n1A="
        new = Fernet.generate_key().decode()
        with override_settings(FIELD_ENCRYPTION_KEY=f"{new},{old}"):
            s = PS.get()
            s.set_secret("backup_password", "Archive-Pass-2026")  # under the new key; mail_password under the old
            s.save()
            with open(self.env, "w") as fh:
                fh.write(f"FIELD_ENCRYPTION_KEY={new}\n")

            def replace_mail():
                p = PS.get()
                p.set_secret("mail_password", "New-Mail-Pass")  # now under the new key in the live database
                p.save()

            with self.assertRaises(RuntimeError) as e:
                self.make(PS.get(), after_dump=replace_mail)
        self.assertIn("does not open 1 stored secret(s) in the dump", str(e.exception))

    def test_a_rotation_just_before_the_lock_still_backs_up(self):
        """commit-071: the backup read its settings before a key change that ended before it took the capture lock:
        the dump and portal.env agree on the new key, and the check reads the database as it is then."""
        import contextlib

        from cryptography.fernet import Fernet

        from accounts import appliance
        from inventory import crypto

        new = Fernet.generate_key().decode()
        with open(self.env, "w") as fh:
            fh.write("FIELD_ENCRYPTION_KEY=m3Jb0bC1k0qWw8bq3cJmHqk6kVqZ0b9mJxS6m0f4n1A=\n")
        stale = PortalSettings.get()  # read before the change
        rotated = override_settings(FIELD_ENCRYPTION_KEY=f"{new},m3Jb0bC1k0qWw8bq3cJmHqk6kVqZ0b9mJxS6m0f4n1A=")

        real_lock = appliance.capture_lock  # the lock itself, before the test puts this one in its place

        @contextlib.contextmanager
        def lock_after_rotation():
            rotated.enable()
            crypto.rotate()
            with open(self.env, "w") as fh:
                fh.write(f"FIELD_ENCRYPTION_KEY={new}\n")  # the old key removed
            with override_settings(FIELD_ENCRYPTION_KEY=new), real_lock():
                yield

        try:
            path = self.make(stale, lock=lock_after_rotation)
        finally:
            rotated.disable()
        self.assertTrue(os.path.basename(path).startswith(appliance.ARCHIVE_PREFIX))

    def test_a_standard_base64_key_backs_up_and_restores_alike(self):
        """commit-072: a key in the standard Base64 alphabet (+ /), which Fernet reads, passes the backup's check and
        both restore paths."""
        import base64
        import subprocess

        from cryptography.fernet import Fernet

        from tenants.models import PortalSettings as PS

        while True:
            k = Fernet.generate_key()
            std = base64.b64encode(base64.urlsafe_b64decode(k)).decode()
            if "+" in std or "/" in std:
                break
        with override_settings(FIELD_ENCRYPTION_KEY=std):
            s = PS.get()
            s.set_secret("backup_password", "Archive-Pass-2026")
            s.set_secret("mail_password", "Mail-Pass")
            s.save()
            with open(self.env, "w") as fh:
                fh.write(f"FIELD_ENCRYPTION_KEY={std}\n")
            self.make(PS.get())
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for script in ("deploy/restore.sh", "deploy/claugine-install.sh"):
            text = open(os.path.join(repo, script)).read()
            i = text.index("fernet_keys_ok() {")
            fn = text[i : text.index("\n}\n", i) + 3]
            r = subprocess.run(["bash", "-c", fn + f'\nfernet_keys_ok "{self.env}"'], capture_output=True)
            self.assertEqual(r.returncode, 0, script)


class BackupEdition(unittest.TestCase):
    """core-024 (owner 2026-11-08: "restore possible only to the same installation - standard or enterprise.
    standard cant be restore to enterprise and vise versa."): a backup records its edition inside the archive; the
    installer refuses another edition's backup before anything is written, restore.sh checks it again, and a backup
    made before editions were recorded is accepted as a legacy backup. The real shell, against real archives."""

    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    INSTALLER = os.path.join(REPO, "deploy", "claugine-install.sh")
    RESTORE = os.path.join(REPO, "deploy", "restore.sh")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pw = os.path.join(self.tmp, "pw")
        with open(self.pw, "w") as fh:
            fh.write("Archive-Pass-2026")

    def archive(self, which, env=True):
        """A backup archive as make_backup writes one: AES-256, names encrypted; `which` None makes a legacy one;
        env False leaves portal.env out (core-048), a string is its text (commit-065)."""
        import subprocess

        content = tempfile.mkdtemp(dir=self.tmp)
        with open(os.path.join(content, "database.sql"), "w") as fh:
            fh.write("-- dump\n")
        if which:
            with open(os.path.join(content, "edition"), "w") as fh:
                fh.write(which + "\n")
            if env:
                with open(os.path.join(content, "portal.env"), "w") as fh:
                    fh.write(
                        env
                        if isinstance(env, str)
                        else "DB_NAME=portal\nFIELD_ENCRYPTION_KEY=m3Jb0bC1k0qWw8bq3cJmHqk6kVqZ0b9mJxS6m0f4n1A=\n"
                    )
        out = tempfile.mktemp(dir=self.tmp, prefix=f"claugine_{which or 'legacy'}_", suffix=".7z")
        subprocess.run(
            ["7z", "a", "-t7z", "-mhe=on", "-p", "-bso0", "-bsp0", "-y", out, "."],
            cwd=content,
            input=b"Archive-Pass-2026\n",
            check=True,
            capture_output=True,
        )
        return out

    def install_check(self, backup, licence, pw=None):
        import subprocess

        env = {"PATH": os.environ["PATH"], "HOME": self.tmp, "CLAUGINE_UPDATE_SOURCE_ONLY": "1"}
        cmd = f'source "{self.INSTALLER}"; check_backup_edition "{backup}" "{pw or self.pw}" {licence}; echo GOING-ON'
        return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=env, timeout=60)

    def test_the_installer_refuses_the_other_edition(self):
        for made, licence in (("standard", "enterprise"), ("enterprise", "standard")):
            with self.subTest(made=made, licence=licence):
                r = self.install_check(self.archive(made), licence)
                self.assertNotEqual(r.returncode, 0)
                self.assertNotIn("GOING-ON", r.stdout)
                self.assertIn(f"the backup is of a {made.title()} installation and this licence is", r.stderr)

    def test_the_installer_goes_on_with_the_same_edition_and_a_legacy_backup(self):
        for made in ("standard", "enterprise"):
            with self.subTest(made=made):
                r = self.install_check(self.archive(made), made)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("GOING-ON", r.stdout)
        r = self.install_check(self.archive(None), "enterprise")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("a legacy backup", r.stdout)
        self.assertIn("GOING-ON", r.stdout)

    def test_the_installer_refuses_a_backup_without_its_key(self):
        """core-048, commit-065: a backup of the current format without portal.env, or whose key is missing, blank or
        malformed, is refused before anything is installed."""
        for env in (False, "DB_NAME=portal\n", "FIELD_ENCRYPTION_KEY=   \n", "FIELD_ENCRYPTION_KEY=test-key\n"):
            with self.subTest(env=env):
                r = self.install_check(self.archive("enterprise", env=env), "enterprise")
                self.assertNotEqual(r.returncode, 0)
                self.assertNotIn("GOING-ON", r.stdout)
                self.assertIn("no portal.env with a valid FIELD_ENCRYPTION_KEY", r.stderr)

    def keys_ok(self, script, text):
        import subprocess

        f = tempfile.mktemp(dir=self.tmp)
        with open(f, "w") as fh:
            fh.write(text)
        fn = open(script).read()
        fn = fn[fn.index("fernet_keys_ok() {") : fn.index("\n}\n", fn.index("fernet_keys_ok() {")) + 3]
        return subprocess.run(["bash", "-c", fn + f'\nfernet_keys_ok "{f}"'], capture_output=True).returncode == 0

    def test_both_restore_paths_read_the_key_alike(self):
        """commit-065: the same check in the installer and restore.sh (no 7-Zip needed), as settings.py reads it."""
        new = "a" * 43 + "="
        cases = {
            "FIELD_ENCRYPTION_KEY=m3Jb0bC1k0qWw8bq3cJmHqk6kVqZ0b9mJxS6m0f4n1A=\n": True,
            "FIELD_ENCRYPTION_KEY=i0qBnfPIy+npwwjpMlqqPGOFqd/YmxF8SmeQRivksjA=\n": True,  # commit-072: standard Base64
            f"  FIELD_ENCRYPTION_KEY = {new} , m3Jb0bC1k0qWw8bq3cJmHqk6kVqZ0b9mJxS6m0f4n1A= ,\n": True,
            "FIELD_ENCRYPTION_KEY=x\nFIELD_ENCRYPTION_KEY=m3Jb0bC1k0qWw8bq3cJmHqk6kVqZ0b9mJxS6m0f4n1A=\n": False,  # the first assignment counts
            "FIELD_ENCRYPTION_KEY=\n": False,
            "FIELD_ENCRYPTION_KEY=  ,  \n": False,
            "# FIELD_ENCRYPTION_KEY=m3Jb0bC1k0qWw8bq3cJmHqk6kVqZ0b9mJxS6m0f4n1A=\n": False,
            "FIELD_ENCRYPTION_KEY='m3Jb0bC1k0qWw8bq3cJmHqk6kVqZ0b9mJxS6m0f4n1A='\n": False,  # settings.py keeps quotes: not a key
            "FIELD_ENCRYPTION_KEY=m3Jb0bC1k0qWw8bq3cJmHqk6kVqZ0b9mJxS6m0f4n1 A=\n": False,
            "DB_NAME=portal\n": False,
        }
        for script in (self.INSTALLER, self.RESTORE):
            for text, ok in cases.items():
                with self.subTest(script=os.path.basename(script), text=text):
                    self.assertEqual(self.keys_ok(script, text), ok)

    def test_an_unreadable_archive_is_not_taken_for_a_legacy_backup(self):
        bad = os.path.join(self.tmp, "bad")
        with open(bad, "w") as fh:
            fh.write("not-the-password")
        r = self.install_check(self.archive("enterprise"), "standard", pw=bad)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("GOING-ON", r.stdout)
        self.assertNotIn("legacy", r.stdout)

    def restore(self, which, *args, ee=False):
        """restore.sh against a backup folder whose install.sh only says it was reached: no portal.env in the backup,
        so nothing is written outside the test's folder before that point."""
        import subprocess

        backup = tempfile.mkdtemp(dir=self.tmp)
        with open(os.path.join(backup, "database.sql"), "w") as fh:
            fh.write("-- dump\n")
        if which:
            with open(os.path.join(backup, "edition"), "w") as fh:
                fh.write(which + "\n")
        src = tempfile.mkdtemp(dir=self.tmp)
        os.makedirs(os.path.join(src, "deploy"))
        with open(os.path.join(src, "deploy", "install.sh"), "w") as fh:
            fh.write("echo INSTALL-REACHED; exit 7\n")
        if ee:
            os.makedirs(os.path.join(src, "ee"))
            open(os.path.join(src, "ee", "__init__.py"), "w").close()
        cmd = ["bash", self.RESTORE, "--site-url", "https://portal.example.com", "--backup", backup, "--src", src]
        return subprocess.run([*cmd, *args], capture_output=True, text=True, timeout=60)

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "restore.sh runs as root")
    def test_restore_sh_checks_it_again(self):
        r = self.restore("standard", "--edition", "enterprise")
        self.assertEqual(r.returncode, 1)
        self.assertIn("a backup restores only to an installation of the same edition", r.stderr)
        self.assertNotIn("INSTALL-REACHED", r.stdout)
        r = self.restore("enterprise")  # by hand: the distribution's edition, here a Standard one (no ee/)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("INSTALL-REACHED", r.stdout)
        r = self.restore("enterprise", ee=True)  # core-048: the current format without portal.env: refused
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("no portal.env with a valid FIELD_ENCRYPTION_KEY in the backup", r.stderr)
        self.assertNotIn("INSTALL-REACHED", r.stdout)
        r = self.restore(None, "--edition", "standard")
        self.assertEqual(r.returncode, 7, r.stderr)
        self.assertIn("A legacy backup", r.stdout)
        self.assertIn("INSTALL-REACHED", r.stdout)
