"""core-036: a platform backup whose configuration archive missed a file (unreadable, changed or vanished while tar
read it) is not a complete backup - the run fails and nothing older is pruned."""

import os
import tempfile
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, TestCase

from accounts import fe_backup


def small_tgz():
    """A real gzip tar archive, as tar on a node sends one (commit-078: the backup checks it reads)."""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        data = b"x = 1\n"
        info = tarfile.TarInfo("etc/one/oned.conf")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _fetch(warning, tgz=True):
    def fake(addr, port, user, key, cmd, out, timeout=None):
        with open(out, "wb") as f:
            f.write(small_tgz() if tgz else b"")
        return warning

    return fake


class NodeConfig(SimpleTestCase):
    def setUp(self):
        self.content = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.content, ignore_errors=True))
        self.p = SimpleNamespace(key="dc1", fe_mode="", fe_name="fe")
        self.node = ("0", "fe1", "192.0.2.10", "follower")

    def backup(self, warning, tgz=True):
        with mock.patch("inventory.sshexec.fetch", _fetch(warning, tgz)):
            return fe_backup._node_backup(
                self.p, self.node, "portal", "key", self.content, ["/etc/one"], lambda m: None
            )

    def test_sudo_refusing_tar_leaves_no_empty_archive(self):
        """commit-078: sudo refusing tar exits 1, which the command accepts; the empty output is not a configuration
        archive - the node's config is an error, said plainly, and no config.tar.gz is kept."""
        base, errors = self.backup("sudo: a password is required\n", tgz=False)
        self.assertEqual(base, "")
        self.assertEqual(errors, ["fe1 config: portal cannot use sudo without a password there"])

    def test_a_damaged_gzip_ending_is_not_a_configuration_archive(self):
        """commit-081: the tar reads to its end marker, but the gzip trailer (CRC, length) is missing or wrong."""
        good = small_tgz()
        for bad in (good[:-8], good[:-8] + b"\x00" * 8):
            with self.subTest(trailer="missing" if len(bad) < len(good) else "wrong"):
                path = os.path.join(self.content, "c.tar.gz")
                with open(path, "wb") as fh:
                    fh.write(bad)
                self.assertFalse(fe_backup._readable_tar(path))
        # commit-085: damaged compressed data (zlib.error) is refused too, not raised
        damaged = good[:12] + bytes(b ^ 0xFF for b in good[12:-8]) + good[-8:]
        with open(path, "wb") as fh:
            fh.write(damaged)
        self.assertFalse(fe_backup._readable_tar(path))
        with open(path, "wb") as fh:
            fh.write(good)
        self.assertTrue(fe_backup._readable_tar(path))

    def test_a_file_tar_could_not_read_makes_the_backup_an_error_naming_the_file(self):
        base, errors = self.backup(
            "tar: Removing leading `/' from member names\ntar: /etc/one/secret.key: Cannot open: Permission denied\n"
        )
        self.assertEqual(base, "0-fe1-192.0.2.10_follower")
        self.assertTrue(os.path.isfile(os.path.join(self.content, base, "config.tar.gz")))  # kept, for what it holds
        self.assertEqual(len(errors), 1)
        self.assertIn("not complete", errors[0])
        self.assertIn("/etc/one/secret.key", errors[0])
        self.assertNotIn("Removing leading", errors[0])

    def test_a_file_that_changed_while_read_is_an_error_too(self):
        _base, errors = self.backup("tar: /etc/one/oned.conf: file changed as we read it\n")
        self.assertEqual(len(errors), 1)
        self.assertIn("oned.conf", errors[0])

    def test_the_leading_slash_notice_alone_is_no_error(self):
        _base, errors = self.backup("tar: Removing leading `/' from member names\n")
        self.assertEqual(errors, [])
        self.node = ("1", "fe2", "192.0.2.11", "follower")
        _base, errors = self.backup("")
        self.assertEqual(errors, [])


class RunKeepsOlderBackups(TestCase):
    def test_an_incomplete_node_fails_the_run_and_nothing_is_pruned(self):
        from tenants.models import PortalSettings

        folder = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        s = PortalSettings.get()
        s.fe_backup_local, s.fe_backup_path, s.fe_backup_s3 = True, folder, False
        s.save()
        made = os.path.join(folder, "platform_dc1_20261117-0300")
        os.makedirs(made)
        open(os.path.join(made, "x"), "w").close()
        err = ["fe1 config: not complete - tar: /etc/one/secret.key: Cannot open: Permission denied"]
        with (
            mock.patch.object(fe_backup, "platforms", return_value=[SimpleNamespace(key="dc1")]),
            mock.patch.object(fe_backup, "backup_platform", return_value=(made, err)),
            mock.patch.object(fe_backup, "prune") as prune,
            mock.patch("accounts.appliance.notify_backup_failure"),
        ):
            result = fe_backup.run(s, log=lambda m: None)
        self.assertTrue(result.startswith("failed: 1 errors"), result)
        self.assertIn("secret.key", result)
        prune.assert_not_called()


class ChosenPlatformGone(TestCase):
    """core-042 (twentieth full review): a platform chosen for backups that is not there to back up (renamed, deleted,
    or without its VIP or front-end secret) fails the run, named, and nothing is pruned - it was left out silently,
    "ok: 0 platforms", and its older backups expired."""

    def test_the_run_fails_and_keeps_the_older_backups(self):
        from tenants.models import PortalSettings

        folder = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        s = PortalSettings.get()
        s.fe_backup_local, s.fe_backup_path, s.fe_backup_s3 = True, folder, False
        s.fe_backup_platforms = ["dc1.old"]
        s.save()
        with (
            mock.patch.object(fe_backup, "platforms", return_value=[]),
            mock.patch.object(fe_backup, "prune") as prune,
            mock.patch("accounts.appliance.notify_backup_failure") as told,
        ):
            result = fe_backup.run(s, log=lambda m: None)
        self.assertTrue(result.startswith("failed: 1 errors"), result)
        self.assertIn("dc1.old", result)
        prune.assert_not_called()
        told.assert_called_once()


class LockFailure(SimpleTestCase):
    """commit-082: a backup lock whose flock fails closes its descriptor (Python does not call __exit__ when
    __enter__ raises) - the run lock and the capture lock alike."""

    def test_the_descriptor_is_closed(self):
        import fcntl

        from accounts import appliance

        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        for make in (
            lambda: appliance.run_lock("portal-backup"),
            lambda: appliance.capture_lock(),
        ):
            seen = []

            def flock(fd, op, seen=seen):
                seen.append(fd)
                raise OSError("no locks")

            with (
                mock.patch.object(appliance, "RUN_LOCK_DIR", d),
                mock.patch.object(appliance, "CAPTURE_LOCK", os.path.join(d, "capture.lock")),
                mock.patch.object(fcntl, "flock", side_effect=flock),
                self.assertRaises(OSError),
            ):
                with make():
                    pass
            with self.assertRaises(OSError):
                os.fstat(seen[0])
