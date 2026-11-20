"""Decision 0010 (job 2026-11-14_from-claugine-web-console-procedures): what the docs told operators to do on the
console is done from the portal - the key to the stored secrets, HAProxy following Settings, an update now."""

import json
import os
import tempfile
from unittest import mock

from cryptography.fernet import Fernet
from django.test import TestCase
from django.urls import reverse

from accounts import appliance, autoupdate, keyrotate
from accounts.tests import Base
from tenants.models import PortalSettings


class KeyRotation(TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        self.old = Fernet.generate_key().decode()
        with os.fdopen(fd, "w") as fh:
            fh.write(f"SECRET_KEY=x\nFIELD_ENCRYPTION_KEY={self.old}\nDB_NAME=portal\n")
        os.chmod(self.path, 0o640)

    def tearDown(self):
        os.unlink(self.path)

    def test_write_keeps_the_rest(self):
        new = Fernet.generate_key().decode()
        keyrotate.write(f"{new},{self.old}", self.path)
        with open(self.path) as fh:
            text = fh.read()
        self.assertIn(f"FIELD_ENCRYPTION_KEY={new},{self.old}\n", text)
        self.assertIn("SECRET_KEY=x\n", text)
        self.assertIn("DB_NAME=portal\n", text)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o640)
        self.assertEqual(keyrotate.read(self.path), f"{new},{self.old}")
        with self.assertRaises(keyrotate.KeyError_):
            keyrotate.write("not-a-key", self.path)

    def test_the_steps_keep_the_old_key_until_every_secret_moved(self):
        written, peer_calls = [], []

        class Op:
            def step(self, name, fn):
                return fn()

        def request(addr, method, path, body, timeout=60):
            peer_calls.append((addr, path, body["value"]))

        with (
            mock.patch.object(keyrotate, "ENV_FILE", self.path),
            mock.patch.object(keyrotate, "read", return_value=self.old),
            mock.patch.object(keyrotate, "write", side_effect=lambda v, path=None: written.append(v)),
            mock.patch.object(keyrotate, "restart_apps", return_value="restarted"),
            mock.patch.object(keyrotate, "manage", return_value=["ok"]) as manage,
            mock.patch("accounts.appliance.backup", return_value="ok: x"),
        ):
            new = keyrotate.run(Op(), "192.0.2.12", request)
        self.assertEqual(written, [f"{new},{self.old}", new])  # both, then the new one only
        self.assertEqual([c[2] for c in peer_calls], written)  # the other node the same
        self.assertEqual(
            [c.args for c in manage.call_args_list],
            [("rotate_keys",), ("appliance", "--backup-now", "--during", "rotate-key")],
        )

    def held(self, lock):
        """Whether someone holds the capture lock now (asked as another holder would)."""
        import fcntl

        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True
        finally:
            os.close(fd)

    def test_no_backup_captures_while_the_keys_change(self):
        """core-040 (nineteenth full review): the rotation holds the capture lock from the new key to the old key's
        removal; its own two backups run outside it."""
        lock = os.path.join(tempfile.mkdtemp(), "capture.lock")
        seen = []

        class Op:
            def step(self, name, fn):
                return fn()

        def manage(*args):
            seen.append((args[0], self.held(lock)))
            return ["ok"]

        def backup(operation=None):
            seen.append(("backup", self.held(lock)))
            return "ok: x"

        with (
            mock.patch.object(appliance, "CAPTURE_LOCK", lock),
            mock.patch.object(keyrotate, "read", return_value=self.old),
            mock.patch.object(
                keyrotate, "write", side_effect=lambda v, path=None: seen.append(("write", self.held(lock)))
            ),
            mock.patch.object(keyrotate, "restart_apps", return_value="restarted"),
            mock.patch.object(keyrotate, "manage", side_effect=manage),
            mock.patch("accounts.appliance.backup", side_effect=backup),
        ):
            keyrotate.run(Op(), "", lambda *a, **k: None)
        self.assertEqual(
            seen,
            [("backup", False), ("write", True), ("rotate_keys", True), ("write", True), ("appliance", False)],
        )

    def test_a_backup_captures_its_dump_and_its_key_together(self):
        """core-040: a key change asked for while a backup dumps waits until the backup has copied portal.env too."""
        import threading
        import time

        lock = os.path.join(tempfile.mkdtemp(), "capture.lock")
        order = []
        changing = []

        def keys_change():
            with appliance.capture_lock():
                order.append("keys changed")

        def dump(tmp):
            t = threading.Thread(target=keys_change)
            changing.append(t)
            t.start()
            time.sleep(0.3)
            with open(f"{tmp}/database.sql", "w") as fh:
                fh.write("-- dump\n")
            order.append("dumped")

        real_copy = appliance.shutil.copyfile

        def copy(src, dst):
            order.append("key copied")
            return real_copy(src, dst)

        from django.test import override_settings

        keyed = override_settings(FIELD_ENCRYPTION_KEY=self.old, ENV_FILE=self.path)
        keyed.enable()
        self.addCleanup(keyed.disable)
        s = PortalSettings.get()
        s.set_secret("backup_password", "Archive-Pass-2026")
        s.save()
        folder = tempfile.mkdtemp()
        with (
            mock.patch.object(appliance, "CAPTURE_LOCK", lock),
            mock.patch("accounts.appliance._dump", side_effect=dump),
            mock.patch("accounts.appliance.shutil.copyfile", side_effect=copy),
            mock.patch.object(appliance, "installed_commit", return_value=""),
        ):
            appliance.make_backup(PortalSettings.get(), folder)
        changing[0].join(5)
        self.assertEqual(order, ["dumped", "key copied", "keys changed"])

    def test_a_failed_re_encryption_keeps_both_keys(self):
        written = []

        class Op:
            def step(self, name, fn):
                return fn()

        with (
            mock.patch.object(keyrotate, "read", return_value=self.old),
            mock.patch.object(keyrotate, "write", side_effect=lambda v, path=None: written.append(v)),
            mock.patch.object(keyrotate, "restart_apps"),
            mock.patch.object(keyrotate, "manage", side_effect=keyrotate.KeyError_("rotate_keys: boom")),
            mock.patch("accounts.appliance.backup", return_value="ok: x"),
            self.assertRaises(keyrotate.KeyError_),
        ):
            keyrotate.run(Op(), "", lambda *a, **k: None)
        self.assertEqual(len(written), 1)
        self.assertTrue(written[0].endswith("," + self.old))  # the old key is still there

    def test_the_restart_names_only_installed_services(self):
        """core-006: v01.00.37 removed claugine-app-test; restarting it made every key change fail."""
        import re
        from pathlib import Path

        removed = set(re.findall(r"rm -f /etc/systemd/system/([\w-]+)\.service", Path("deploy/install.sh").read_text()))
        self.assertIn("claugine-app-test", removed)
        self.assertFalse(removed & set(keyrotate.APPS))
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run:
            keyrotate.restart_apps()
        self.assertEqual(run.call_args.args[0], ["systemctl", "restart", "claugine-app"])

    def test_a_failed_restart_puts_the_keys_back(self):
        """core-006: a restart that fails leaves portal.env as it was - no key added by a failed attempt."""
        written = []
        new = Fernet.generate_key().decode()
        with (
            mock.patch.object(keyrotate, "read", return_value=self.old),
            mock.patch.object(keyrotate, "write", side_effect=lambda v, path=None: written.append(v)),
            mock.patch.object(keyrotate, "restart_apps", side_effect=[keyrotate.KeyError_("restart: no"), "restarted"]),
            self.assertRaises(keyrotate.KeyError_),
        ):
            keyrotate.set_here_and_there(f"{new},{self.old}", "", lambda *a, **k: None)
        self.assertEqual(written, [f"{new},{self.old}", self.old])


class KeyHandover(TestCase):
    """integration-003 (fourth full review): the other node gets a new key before this node encrypts with it; a
    failed or unanswered handover leaves both nodes with the keys they had."""

    def setUp(self):
        self.old, self.new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
        self.events = []

    def run_it(self, peer_fails=False, restart_fails=False):
        events = self.events

        def request(addr, method, path, body, timeout=60):
            events.append(("peer", body["value"]))
            if peer_fails and len([e for e in events if e[0] == "peer"]) == 1:
                raise OSError("timed out")  # it may have taken them: the answer is lost

        restarts = iter([keyrotate.KeyError_("restart: no"), "restarted"] if restart_fails else ["restarted"] * 3)

        def restart():
            r = next(restarts)
            if isinstance(r, Exception):
                raise r
            return r

        with (
            mock.patch.object(keyrotate, "read", return_value=self.old),
            mock.patch.object(keyrotate, "write", side_effect=lambda v, path=None: events.append(("here", v))),
            mock.patch.object(keyrotate, "restart_apps", side_effect=restart),
        ):
            return keyrotate.set_here_and_there(f"{self.new},{self.old}", "192.0.2.12", request)

    def test_the_other_node_first(self):
        self.assertEqual(self.run_it(), "both nodes")
        both = f"{self.new},{self.old}"
        self.assertEqual(self.events, [("peer", both), ("here", both)])

    def test_a_lost_answer_sets_the_other_node_back_and_this_one_is_untouched(self):
        with self.assertRaisesRegex(keyrotate.KeyError_, "did not take the keys"):
            self.run_it(peer_fails=True)
        self.assertEqual(self.events, [("peer", f"{self.new},{self.old}"), ("peer", self.old)])

    def test_a_failed_restart_here_sets_both_back(self):
        with self.assertRaises(keyrotate.KeyError_):
            self.run_it(restart_fails=True)
        both = f"{self.new},{self.old}"
        self.assertEqual(self.events, [("peer", both), ("here", both), ("peer", self.old), ("here", self.old)])

    def test_the_status_shows_hashes(self):
        fp = keyrotate.fingerprints(f"{self.new},{self.old}")
        self.assertEqual(len(fp), 2)
        self.assertNotIn(self.new[:16], fp[0])  # a hash, not the key


class HaproxyFollowsSettings(TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.args = os.path.join(self.dir, "lb.args")
        self.state = os.path.join(self.dir, "lb.state")
        self.script = os.path.join(self.dir, "install-lb.sh")
        with open(self.script, "w") as fh:
            fh.write("#!/bin/bash\n# v1\n")
        self.p = [
            mock.patch.object(appliance, "LB_ARGS", self.args),
            mock.patch.object(appliance, "LB_LIVE", self.dir),
            mock.patch.object(appliance, "LB_STATE", self.state),
            mock.patch.object(appliance, "LB_SCRIPT", self.script),
            mock.patch.object(appliance.os.path, "isfile", side_effect=lambda f: True),
            mock.patch("accounts.thisnode.read", return_value={}),
        ]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def run_with(self, want, have, le=""):
        with open(self.args, "w") as fh:
            fh.write("\n".join(have) + "\n")
        r = mock.Mock(returncode=0, stdout="done", stderr="")
        with (
            mock.patch("accounts.haproxy.args_for", return_value=want),
            mock.patch("accounts.haproxy.get", return_value={"le_email": le}),
            mock.patch.object(appliance.subprocess, "run", return_value=r) as run,
        ):
            appliance.apply_lb(log=lambda m: None)
        return run

    def test_applies_only_a_change(self):
        same = ["--public-name", "my.example.com"]
        self.assertTrue(self.run_with(same, same).called)  # no script recorded yet (before v01.00.36): applied once
        self.assertFalse(self.run_with(same, same).called)
        run = self.run_with(["--public-name", "my.example.com", "--status-port", "55587"], same)
        self.assertEqual(run.call_args.args[0][1:], ["apply"])
        self.assertTrue(appliance.lb_state()["ok"])

    def test_a_new_install_script_is_applied(self):
        """v01.00.35 changed what HAProxy serves (install-lb.sh) without changing the options: it must still apply."""
        same = ["--public-name", "my.example.com"]
        self.run_with(same, same)
        self.assertFalse(self.run_with(same, same).called)
        with open(self.script, "w") as fh:
            fh.write("#!/bin/bash\n# v2\n")
        run = self.run_with(same, same)
        self.assertEqual(run.call_args.args[0][1:], ["apply"])
        self.assertFalse(self.run_with(same, same).called)

    def test_a_failed_script_only_apply_is_retried_after_the_back_off(self):
        """commit-011: a failed apply of a new install-lb.sh (same options) is not repeated within six hours, then is."""
        same = ["--public-name", "my.example.com"]
        self.run_with(same, same)
        with open(self.script, "w") as fh:
            fh.write("#!/bin/bash\n# v2\n")
        bad = mock.Mock(returncode=1, stdout="", stderr="haproxy -c failed")
        with (
            mock.patch("accounts.haproxy.args_for", return_value=same),
            mock.patch("accounts.haproxy.get", return_value={}),
            mock.patch.object(appliance.subprocess, "run", return_value=bad) as run,
        ):
            appliance.apply_lb(log=lambda m: None)
            appliance.apply_lb(log=lambda m: None)
            self.assertEqual(run.call_count, 1)  # failed once, not repeated within six hours
            st = appliance.lb_state()
            st["at"] -= appliance.LB_RETRY + 1
            with open(self.state, "w") as fh:
                json.dump(st, fh)
            appliance.apply_lb(log=lambda m: None)
            self.assertEqual(run.call_count, 2)  # retried after them
        self.assertFalse(self.run_with(same, same).called)  # failed again: the next six hours start now

    def test_a_new_name_gets_its_certificate(self):
        run = self.run_with(
            ["--public-name", "my.example.com,new.example.com"], ["--public-name", "my.example.com"], le="a@example.com"
        )
        self.assertEqual(run.call_args.args[0][1:], ["certificate"])

    def test_a_removed_name_gets_a_new_certificate(self):
        """commit-187: the Admin Portal URL cleared - the certificate is requested again without its name."""
        run = self.run_with(
            ["--public-name", "my.example.com"],
            ["--public-name", "my.example.com,admin.example.com"],
            le="a@example.com",
        )
        self.assertEqual(run.call_args.args[0][1:], ["certificate"])
        run = self.run_with(["--public-name", "b.example.com"], ["--public-name", "a.example.com,b.example.com"])
        self.assertEqual(run.call_args.args[0][1:], ["apply"])  # no Let's Encrypt account: nothing to request

    def issued(self, cert, *names):
        """The lineage's current certificate, for these names (commit-189: what certbot renews for)."""
        import datetime

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])
        now = datetime.datetime.now(datetime.UTC)
        c = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(1)
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False)
            .sign(key, hashes.SHA256())
        )
        os.makedirs(os.path.join(self.dir, cert), exist_ok=True)
        with open(os.path.join(self.dir, cert, "cert.pem"), "wb") as fh:
            fh.write(c.public_bytes(serialization.Encoding.PEM))

    def test_a_removal_applied_before_is_still_reconciled(self):
        """commit-187, 189: HAProxy already applied without the name; the issued certificate still carries it."""
        same = ["--public-name", "my.example.com", "--one-name", "one-dc1.example.com"]
        self.run_with(same, same, le="a@example.com")  # recorded once
        self.issued("claugine-portal", "my.example.com", "admin.example.com")
        self.issued("claugine-one", "one-dc1.example.com")
        run = self.run_with(same, same, le="a@example.com")
        self.assertEqual(run.call_args.args[0][1:], ["certificate"])
        self.assertFalse(self.run_with(same, same, le="a@example.com").called)  # not again within the back-off
        self.issued("claugine-portal", "my.example.com")  # reissued: nothing more to do
        st = appliance.lb_state()
        st["at"] -= appliance.LB_RETRY + 1
        with open(self.state, "w") as fh:
            json.dump(st, fh)
        self.assertFalse(self.run_with(same, same, le="a@example.com").called)

    def test_each_certificate_on_its_own(self):
        """commit-187: a name moved from the portal's certificate to the front end's is a change of both."""
        have = ["--public-name", "my.example.com,x.example.com", "--one-name", "one-dc1.example.com"]
        want = ["--public-name", "my.example.com", "--one-name", "one-dc1.example.com,x.example.com"]
        run = self.run_with(want, have, le="a@example.com")
        self.assertEqual(run.call_args.args[0][1:], ["certificate"])

    def test_a_failure_is_not_repeated_for_the_same_settings(self):
        want, have = ["--public-name", "a.example.com"], ["--public-name", "b.example.com"]
        with open(self.args, "w") as fh:
            fh.write("\n".join(have))
        bad = mock.Mock(returncode=1, stdout="", stderr="haproxy -c failed")
        with (
            mock.patch("accounts.haproxy.args_for", return_value=want),
            mock.patch("accounts.haproxy.get", return_value={}),
            mock.patch.object(appliance.subprocess, "run", return_value=bad) as run,
        ):
            appliance.apply_lb(log=lambda m: None)
            appliance.apply_lb(log=lambda m: None)
        self.assertEqual(run.call_count, 1)
        self.assertFalse(appliance.lb_state()["ok"])


class SettingsActions(Base):
    def setUp(self):
        self.sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(self.sa)

    def test_install_now(self):
        PortalSettings.objects.update_or_create(pk=1, defaults={"auto_update_state": {"edition": "enterprise"}})
        self.client.post(reverse("portal_settings"), {"tab": "portal", "section": "install_now"})
        from accounts import edition

        # Enterprise only: the public (Standard) build has no editable updates whatever the licence says
        enterprise_build = edition.ee("ha_views") is not None
        self.assertEqual(bool(PortalSettings.get().auto_update_state.get("install_requested")), enterprise_build)
        PortalSettings.objects.update_or_create(pk=1, defaults={"auto_update_state": {"edition": "standard"}})
        self.client.post(reverse("portal_settings"), {"tab": "portal", "section": "install_now"})
        self.assertFalse(PortalSettings.get().auto_update_state.get("install_requested"))  # Standard: its window

    def test_a_critical_release_is_shown_when_updates_are_stopped(self):
        """claugine-web job critical-releases (mockup 37): marked critical, and said to go in all the same."""
        state = {"lic_state": "expired", "latest": "v09.00.00", "critical": True, "notices": ["Critical release"]}
        PortalSettings.objects.update_or_create(pk=1, defaults={"auto_update_state": state})
        with mock.patch("accounts.checkin.configured", return_value=True):
            r = self.client.get(reverse("portal_settings") + "?tab=portal")
            self.assertContains(r, "critical")
            self.assertContains(r, "Updates are stopped by the licence, but this release is critical")
            PortalSettings.objects.update(auto_update_state={**state, "lic_state": "active"})
            r = self.client.get(reverse("portal_settings") + "?tab=portal")
            self.assertContains(r, "v09.00.00")
            self.assertNotContains(r, "Updates are stopped by the licence")

    def test_install_now_goes_ahead_once_with_updates_off(self):
        PortalSettings.objects.update_or_create(
            pk=1, defaults={"auto_update": False, "auto_update_state": {"install_requested": True}}
        )
        with (
            mock.patch("accounts.ha.role", return_value="active"),
            mock.patch("accounts.ha_agent.installed_status", return_value={"installed_tag": "v01.00.01"}),
            mock.patch("accounts.checkin.configured", return_value=True),
            mock.patch("accounts.checkin.check_in", return_value=(True, {"target_version": "v01.00.02"})),
            mock.patch("accounts.checkin.usage", return_value={}),
            mock.patch("accounts.ha_agent.read_op", return_value={}),
            mock.patch("accounts.thisnode.read", return_value={}),
            mock.patch("accounts.ha_agent.run_op", return_value={"state": "done"}) as op,
        ):
            r = autoupdate.tick(log=lambda m: None)
        self.assertEqual(r, "v01.00.02: installed")
        op.assert_called_once_with("update-single", by="auto-update", ref="v01.00.02")
        self.assertFalse(PortalSettings.get().auto_update_state.get("install_requested"))

    def test_rotate_key_starts_the_operation(self):
        with (
            mock.patch("accounts.ha.is_active", return_value=True),
            mock.patch("accounts.ha_agent.start_op", return_value={}) as start,
        ):
            self.client.post(reverse("portal_settings"), {"tab": "portal", "section": "rotate_key"})
        start.assert_called_once_with("rotate-key", "sa@corp.kz")
