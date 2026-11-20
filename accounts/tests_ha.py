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
from .testkit import NodeFile


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
            nf.set(ROLE="new")  # owner 2026-11-15: no version under test any more - an unknown role serves nothing
            self.assertContains(self.client.get(reverse("login")), "under maintenance", status_code=503)
            # a node that is not active never changes a platform, a router or an iLO, and never mails users
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

    def test_one_operation_at_a_time(self):
        """integration-001: an operation is claimed before it starts or is answered, by the agent and the timer
        alike; a second one is refused while the first holds the claim."""
        import threading

        agent = self.agent
        gate, entered = threading.Event(), []

        def slow(op, name, ref):
            entered.append(name)
            gate.wait(5)

        pair = mock.Mock(run=slow, after_failure=mock.Mock(), handle=lambda path, body: None, PAIR_REF_OPS=())
        self.addCleanup(gate.set)
        with (
            mock.patch.object(agent, "ops", return_value={"switchover", "update-os"}),
            mock.patch.object(agent, "_pair_required", return_value=pair),
            mock.patch.object(agent, "_pair", return_value=pair),
            mock.patch.object(agent, "note"),
        ):
            t = threading.Thread(target=agent.run_op, args=("switchover", "a"))
            t.start()
            for _ in range(100):
                if entered:
                    break
                threading.Event().wait(0.02)
            with self.assertRaises(agent.AgentError):
                agent.run_op("update-os", "b")
            self.assertEqual(agent.handle("POST", "/op", {"name": "update-os"})[0], 409)
            gate.set()
            t.join(5)
            self.assertEqual(entered, ["switchover"])
            self.assertEqual(agent.read_op()["state"], "done")
            lock = agent.claim()  # free again
            agent.release(lock)

    def test_an_interrupted_operation_no_longer_blocks(self):
        """enterprise-005: op.json left "running" by a restart (nobody holds the claim) is recorded as interrupted,
        with the step it reached, and the next operation may start."""
        import json

        with open(self.agent.OP_FILE, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "name": "update-portal",
                    "state": "running",
                    "steps": [{"name": "backup", "status": "ok"}, {"name": "other node: install", "status": "running"}],
                },
                fh,
            )
        with mock.patch.object(self.agent, "note") as note:
            op = self.agent.read_op()
        self.assertEqual(op["state"], "failed")
        self.assertTrue(op["interrupted"])
        self.assertIn("other node: install", op["error"])
        self.assertEqual(op["steps"][-1]["status"], "interrupted")
        note.assert_called_once()
        lock = self.agent.claim()
        self.agent.release(lock)

    def _conn(self, der):
        conn = mock.Mock()
        conn.sock.getpeercert.return_value = der
        resp = mock.Mock(status=200)
        resp.read.return_value = b'{"role": "standby"}'
        conn.getresponse.return_value = resp
        return conn

    def test_the_other_nodes_certificate_is_checked_before_the_token_goes(self):
        """enterprise-002: whoever answers at the other node's address gets nothing unless its certificate is the
        one recorded for the other node."""
        import hashlib

        from accounts.testkit import NodeFile

        good, bad = b"cert-of-the-other-node", b"someone-else"
        with (
            NodeFile(
                SITE="dc1",
                ADDR="10.0.0.25",
                PEER_ADDR="10.0.1.25",
                ROLE="active",
                PEER_TLS_SHA256=hashlib.sha256(good).hexdigest(),
            ),
            override_settings(HA_TOKEN="s3cret-token"),
        ):
            conn = self._conn(bad)
            with mock.patch("http.client.HTTPSConnection", return_value=conn):
                with self.assertRaises(self.agent.AgentError) as e:
                    self.agent.peer_request("10.0.1.25", "GET", "/status")
            self.assertIn("not the one recorded", str(e.exception))
            conn.request.assert_not_called()  # the token never left
            conn = self._conn(good)
            with mock.patch("http.client.HTTPSConnection", return_value=conn):
                self.assertEqual(self.agent.peer_request("10.0.1.25", "GET", "/status"), {"role": "standby"})
            self.assertEqual(conn.request.call_args.kwargs["headers"]["X-Claugine-Token"], "s3cret-token")

    def test_the_first_contact_records_the_certificate(self):
        import hashlib

        from accounts import thisnode
        from accounts.testkit import NodeFile

        der = b"cert-of-the-other-node"
        with NodeFile(SITE="dc1", ADDR="10.0.0.25", PEER_ADDR="10.0.1.25", ROLE="active"):
            with (
                mock.patch("http.client.HTTPSConnection", return_value=self._conn(der)),
                mock.patch("os.geteuid", return_value=0),
                mock.patch.object(self.agent, "note"),
            ):
                self.agent.peer_request("10.0.1.25", "GET", "/status")
            self.assertEqual(thisnode.read()["PEER_TLS_SHA256"], hashlib.sha256(der).hexdigest())


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
    def test_the_public_fqdn_on_443(self):
        """Owner 2026-11-15: the platform's public FQDN only; FireEdge, the XML-RPC API and OneFlow under it on 443, no
        gRPC (its client has no TLS) and no OneGate (VMs reach it through their host); the old ports are not read."""
        from inventory.models import Platform

        p = Platform(
            key="dc1.cloud",
            name="cloud",
            kind="shared",
            public_url="https://one-dc1.example.com/",
            fireedge_port=55516,
            grpc_port=55534,
            vm_url="https://one-dc1.example.com",
            onegate_port=55530,
        )
        self.assertEqual(p.public_fqdn, "one-dc1.example.com")
        self.assertEqual(
            p.endpoints,
            [
                ("FireEdge", "https://one-dc1.example.com/fireedge/sunstone"),
                ("XML-RPC API", "https://one-dc1.example.com/RPC2"),
                ("OneFlow", "https://one-dc1.example.com/oneflow"),
            ],
        )
        self.assertEqual(p.fireedge_link, "https://one-dc1.example.com/fireedge/sunstone")
        self.assertEqual(Platform(key="x.y", name="y", kind="shared").endpoints, [])
        self.assertEqual(Platform(key="x.y", name="y", kind="shared").fireedge_link, "")


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
