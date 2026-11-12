"""The portal on two sites: node roles, the status page, the role gate, the HA agent's operations (MySQL and the other
node mocked), the Publishing tab."""

import os
import tempfile
import unittest
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from accounts import edition

from . import thisnode
from .testkit import NodeFile, otp, staff


class Roles(TestCase):
    @unittest.skipUnless(edition.ee("ha"), "the pair roles are Enterprise (ee/ha.py); a Standard build has one node")
    def test_status_page_gate_mail_and_platform_writes(self):
        # no node.env: one active portal, as before
        with override_settings(NODE_ENV_FILE="/nonexistent/node.env"):
            thisnode.forget()
            self.assertEqual(self.client.get("/active").status_code, 200)
            self.assertEqual(self.client.get("/standby").status_code, 503)
        with NodeFile(SITE="dc3", ADDR="10.0.0.25", PEER_ADDR="10.0.1.25", ROLE="standby") as nf:
            self.assertEqual(self.client.get("/standby").status_code, 200)
            self.assertEqual(self.client.get("/active").status_code, 503)
            r = self.client.get(reverse("login"))
            self.assertContains(r, "under maintenance", status_code=503)  # a standby serves nothing
            nf.set(ROLE="new")
            sa = staff(is_super_admin=True)
            user = staff("rm@corp.kz", is_resource_manager=True)
            self.client.force_login(user)
            r = self.client.get(reverse("dashboard"))
            self.assertContains(r, "Only super admins", status_code=403)  # others are signed out
            self.assertNotIn("_auth_user_id", self.client.session)
            self.client.force_login(sa)
            otp(self.client)
            self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
            # a new version never changes a platform, a router or an iLO, and never mails users
            from tenants.deploy import DeployError, One

            one = One.__new__(One)
            one.key, one.client = "dc1.cloud", mock.Mock()
            with self.assertRaises(DeployError):
                one.call("one.vm.allocate", "x")
            one.client._call.return_value = "<VM/>"
            self.assertEqual(one.call("one.vm.info", 1), "<VM/>")  # reads are fine
            from inventory.sshexec import SshError, run

            with self.assertRaises(SshError):
                run("10.0.0.1", 22, "u", "KEY", "vbash -s")
            from django.core.mail import EmailMessage

            from . import appliance

            work = tempfile.mkdtemp()
            with (
                override_settings(EMAIL_FILE_PATH=work),
                mock.patch("django.core.mail.backends.smtp.EmailBackend.send_messages") as smtp,
            ):
                appliance.EmailBackend().send_messages([EmailMessage("s", "b", None, ["x@example.kz"])])
            smtp.assert_not_called()
            self.assertTrue(os.listdir(work))
            # the timers do nothing off the active node
            from io import StringIO

            from django.core.management import call_command

            out = StringIO()
            call_command("collect_platforms", stdout=out)
            self.assertIn("not active", out.getvalue())


class Agent(TestCase):
    def setUp(self):
        from . import ha_agent

        self.state = tempfile.mkdtemp()
        self.patches = [
            mock.patch.object(ha_agent, "OP_FILE", os.path.join(self.state, "op.json")),
            mock.patch.object(ha_agent, "STATE_DIR", self.state),
            mock.patch.object(ha_agent, "version", return_value="v1"),
            mock.patch.object(ha_agent, "sql1", return_value="uuid:1-100,\\nuuid2:1-3"),
            mock.patch.object(ha_agent, "sql", return_value=[]),
        ]
        for p in self.patches:
            p.start()
        self.agent = ha_agent

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_agent_needs_the_token(self):
        with override_settings(HA_TOKEN="s3cret-token"):
            self.assertTrue(self.agent.check_token("s3cret-token"))
            self.assertFalse(self.agent.check_token("wrong"))
            self.assertFalse(self.agent.check_token(""))
        with override_settings(HA_TOKEN=""):
            self.assertFalse(self.agent.check_token(""))


class DirectMail(TestCase):
    def test_postfix_delivers_itself_under_the_node_mail_name(self):
        from tenants.models import PortalSettings

        from . import appliance

        s = PortalSettings.objects.first() or PortalSettings.objects.create()
        s.mail_mode, s.mail_from, s.mail_host = "direct", "Example Cloud <portal@cp.example.com>", ""
        conf = {
            "relayhost": "[smtp-relay.brevo.com]:587",
            "myhostname": "portal.example.com",
            "inet_interfaces": "loopback-only",
        }
        runs = []

        def run(cmd, **kw):
            runs.append(cmd)
            if cmd[:2] == ["postconf", "-e"]:
                conf.update(x.split("=", 1) for x in cmd[2:])
            return mock.Mock(returncode=0, stdout="")

        with (
            NodeFile(SITE="dc1", ADDR="10.0.0.25", ROLE="active", MAIL_NAME="service2-dc1.provider.example"),
            mock.patch("shutil.which", return_value="/usr/sbin/postconf"),
            mock.patch.object(appliance, "_postconf", side_effect=lambda k: conf.get(k, "")),
            mock.patch("subprocess.run", side_effect=run),
        ):
            log = []
            appliance.apply_mail(s, log.append)
            self.assertEqual(
                (conf["relayhost"], conf["myhostname"], conf["myorigin"], conf["mydestination"]),
                ("", "service2-dc1.provider.example", "$myhostname", "$myhostname, localhost"),
            )
            self.assertIn(["systemctl", "restart", "postfix"], runs)
            runs.clear()
            appliance.apply_mail(s, log.append)  # nothing differs: nothing changes
            self.assertEqual(runs, [])


class PlatformPublicEndpoints(TestCase):
    def test_one_url_with_ports_and_one_for_vms(self):
        from inventory.models import Platform

        p = Platform(
            key="dc1.cloud",
            name="cloud",
            kind="shared",
            public_url="https://one-dc1.claugine.com/",
            fireedge_port=55516,
            grpc_port=55534,
            xmlrpc_port=55533,
            vm_url="https://one-dc1.claugine.com",
            onegate_port=55530,
            oneflow_port=55574,
        )
        self.assertEqual(
            p.endpoints,
            [
                ("FireEdge", "https://one-dc1.claugine.com:55516"),
                ("gRPC API", "https://one-dc1.claugine.com:55534"),
                ("XML-RPC API", "https://one-dc1.claugine.com:55533/RPC2"),
                ("OneGate", "https://one-dc1.claugine.com:55530"),
                ("OneFlow", "https://one-dc1.claugine.com:55574"),
            ],
        )
        self.assertEqual(p.fireedge_link, "https://one-dc1.claugine.com:55516")
        p.fireedge_port = None  # no port: the URL as it is
        p.public_url = "https://dc2-one.provider.example"
        self.assertEqual(p.fireedge_link, "https://dc2-one.provider.example")
        self.assertEqual(Platform(key="x.y", name="y", kind="shared").endpoints, [])


class OsPatch(TestCase):
    def test_os_patch_no_reboot(self):
        from . import ha_agent

        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            out = "3 upgraded, 0 newly installed, 0 to remove." if "dist-upgrade" in argv else ""
            return mock.Mock(returncode=0, stdout=out, stderr="")

        with (
            mock.patch.object(ha_agent.subprocess, "run", side_effect=fake_run),
            mock.patch.object(ha_agent.os.path, "exists", return_value=False),
        ):
            r = ha_agent.os_patch(reboot=True)
        self.assertEqual(r["upgraded"], "3")
        self.assertFalse(r["rebooting"])
        self.assertIn(["apt-get", "update", "-q"], calls)
        self.assertTrue(any("dist-upgrade" in c for c in calls))
        self.assertFalse(any("systemd-run" in c for c in calls))

    def test_os_patch_on_a_standby_lifts_the_persisted_read_only(self):
        """2026-11-12: the mysql-server package's temporary server must not start super_read_only."""
        from . import ha_agent

        order = []
        with (
            mock.patch.object(ha_agent, "sql1", return_value="1"),
            mock.patch.object(ha_agent, "sql", side_effect=lambda q, **k: order.append(q)),
            mock.patch.object(
                ha_agent.subprocess,
                "run",
                side_effect=lambda argv, **kw: (
                    order.append(argv[-1])
                    or mock.Mock(returncode=int(argv[-1] == "dist-upgrade"), stdout="", stderr="x")
                ),
            ),
            mock.patch.object(ha_agent.os.path, "exists", return_value=False),
        ):
            with self.assertRaises(ha_agent.AgentError):
                ha_agent.os_patch(reboot=False)
        self.assertEqual(
            order, ["-q", "RESET PERSIST IF EXISTS super_read_only", "dist-upgrade", "SET PERSIST super_read_only=ON"]
        )

    def test_os_patch_reboots_when_required(self):
        from . import ha_agent

        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            out = "5 upgraded, 1 newly installed" if "dist-upgrade" in argv else ""
            return mock.Mock(returncode=0, stdout=out, stderr="")

        with (
            mock.patch.object(ha_agent.subprocess, "run", side_effect=fake_run),
            mock.patch.object(ha_agent.os.path, "exists", return_value=True),
        ):
            r = ha_agent.os_patch(reboot=True)
        self.assertEqual(r["upgraded"], "5")
        self.assertTrue(r["rebooting"])
        self.assertTrue(any("systemd-run" in c for c in calls))

    def test_os_patch_reboot_required_but_deferred(self):
        from . import ha_agent

        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return mock.Mock(returncode=0, stdout="1 upgraded", stderr="")

        with (
            mock.patch.object(ha_agent.subprocess, "run", side_effect=fake_run),
            mock.patch.object(ha_agent.os.path, "exists", return_value=True),
        ):
            r = ha_agent.os_patch(reboot=False)
        self.assertFalse(r["rebooting"])
        self.assertIn("reboot", r["result"])
        self.assertFalse(any("systemd-run" in c for c in calls))

    def test_os_patch_update_failure_raises(self):
        from . import ha_agent

        with (
            mock.patch.object(
                ha_agent.subprocess,
                "run",
                return_value=mock.Mock(returncode=1, stdout="", stderr="could not resolve mirror"),
            ),
            mock.patch.object(ha_agent.os.path, "exists", return_value=False),
        ):
            with self.assertRaises(ha_agent.AgentError):
                ha_agent.os_patch()


class StatusQuiet(TestCase):
    def test_a_role_this_node_does_not_have_is_not_logged_as_an_error(self):
        """v01.00.09: HAProxy's checks of /standby and /new filled the journal with "Service Unavailable"."""
        with self.assertNoLogs("django.request", level="WARNING"):
            r = self.client.get("/standby")
        self.assertEqual(r.status_code, 503)
