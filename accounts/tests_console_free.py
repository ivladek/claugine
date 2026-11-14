"""Decision 0010 (job 2026-11-14_from-claugine-web-console-procedures): what the docs told operators to do on the
console is done from the portal - the key to the stored secrets, HAProxy following Settings, an update now."""

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
        self.assertEqual([c.args for c in manage.call_args_list], [("rotate_keys",), ("appliance", "--backup-now")])

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


class HaproxyFollowsSettings(TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.args = os.path.join(self.dir, "lb.args")
        self.state = os.path.join(self.dir, "lb.state")
        self.p = [
            mock.patch.object(appliance, "LB_ARGS", self.args),
            mock.patch.object(appliance, "LB_STATE", self.state),
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
        self.assertFalse(self.run_with(same, same).called)
        run = self.run_with(["--public-name", "my.example.com", "--local-nets", "192.0.2.0/24"], same)
        self.assertEqual(run.call_args.args[0][1:], ["apply"])
        self.assertTrue(appliance.lb_state()["ok"])

    def test_a_new_name_gets_its_certificate(self):
        run = self.run_with(
            ["--public-name", "my.example.com,new.example.com"], ["--public-name", "my.example.com"], le="a@example.com"
        )
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
