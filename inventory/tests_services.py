"""Infrastructure → Services (owner 2026-11-21, T33): the services list made by the migration from the machines read on
2026-11-21, how a machine's services are found (no roles: 07:17), the rsyslog and logrotate files they make (checked
with rsyslogd -N1 when it is installed), the pages, and the host deployment's steps (the logging step; the exporters
installed: 07:46)."""

import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from accounts.tests import Base
from inventory import hostdeploy, logconf, services
from inventory.models import Host, HostDeployment, HostService, Site
from inventory.tests_hostdeploy import KEY, world
from tenants.models import PortalSettings

COL = {"host": "10.0.0.18", "port": 15140, "proto": "tcp"}
RULES = {"kept_days": 5, "queue_mb": 100, "retry_seconds": 30, "syslog_level": 4}


def ordered():
    return sorted(HostService.objects.all(), key=lambda s: (services.GROUPS.index(s.group), s.name.lower()))


class Seed(TestCase):
    def test_the_migration_made_the_services_read(self):
        keys = set(HostService.objects.values_list("key", flat=True))
        self.assertEqual(len(keys), 28)
        for k in ("opennebula", "mysql", "libvirt", "frr", "bind", "auditd", "fail2ban", "xrdp", "nvidia", "graylog"):
            self.assertIn(k, keys)
        one = HostService.objects.get(key="opennebula")
        self.assertEqual(one.log_type, "one")
        self.assertEqual(one.present_when, [{"kind": "package", "value": "opennebula"}])
        vm = next(s for s in one.sources if s["tag"] == "one-vm")
        self.assertTrue(vm["vm_id"])
        self.assertEqual(HostService.objects.get(key="nvidia").present_when, [{"kind": "pci", "value": "10de"}])

    def test_the_rules_defaults(self):
        conf = PortalSettings.get()
        self.assertEqual(
            logconf.rules_of(conf), {"kept_days": 5, "queue_mb": 100, "retry_seconds": 30, "syslog_level": 4}
        )


class Detect(TestCase):
    def test_the_checks(self):
        s = list(HostService.objects.filter(key__in=("opennebula", "nvidia", "mysql", "ssh")))
        text = logconf.detect_script(s)
        self.assertIn("dpkg-query -W -f='${Status}' opennebula", text)
        self.assertIn("grep -qix 0x10de", text)
        self.assertIn("systemctl cat mysql.service", text)
        self.assertIn("echo ssh\n", text)  # no check: every machine
        self.assertEqual(logconf.parse_detected("opennebula\nnope\n ssh \n", s), ["opennebula", "ssh"])

    def test_a_check_of_another_kind_is_refused(self):
        with self.assertRaises(logconf.LogConfError):
            logconf.detect_script([types.SimpleNamespace(key="x", present_when=[{"kind": "?", "value": "a"}])])


class Files(TestCase):
    def found(self, *keys):
        return [s for s in ordered() if s.key in keys]

    def test_a_kvm_host(self):
        f = logconf.files(self.found("libvirt", "frr", "one-exporter", "lldpd", "tuned", "ssh"), COL, RULES)
        names = sorted(p for p in f if p.startswith("/etc/rsyslog.d/"))
        self.assertEqual(
            [n.rsplit("/", 1)[1] for n in names],
            [
                "00-common.conf",
                "10-frr.conf",
                "11-libvirt.conf",
                "12-one-exporter.conf",
                "91-ufw.conf",
                "93-auth.conf",
                "94-kern.conf",
                "95-apt.conf",
                "99-syslog.conf",
            ],
        )
        common = f["/etc/rsyslog.d/00-common.conf"]
        self.assertIn('target = "10.0.0.18"', common)
        self.assertIn('queue.maxDiskSpace = "100m"', common)  # owner 07:08: 100 MB
        self.assertIn('action.resumeInterval = "30"', common)
        self.assertIn('contains_i "--mark--"', common)  # owner 07:32 "drop mark"
        self.assertIn('tcp_framing = "octet-counted"', common)
        self.assertIn("rotate 5", f["/etc/logrotate.conf"])
        self.assertIn("/var/log/frr/*.log\n{\n  copytruncate", f["/etc/logrotate.d/10-frr.conf"])
        self.assertIn('$programname == "node_exporter"', f["/etc/rsyslog.d/12-one-exporter.conf"])
        self.assertIn("level=(warn|error)", f["/etc/rsyslog.d/12-one-exporter.conf"])
        self.assertNotIn("/etc/rsyslog.d/92-nvidia.conf", f)  # nvidia is a service now
        for text in f.values():
            self.assertTrue(text.startswith(logconf.HEADER))

    def test_the_front_end_vm_logs(self):
        f = logconf.files(self.found("opennebula"), COL, RULES)
        one = f["/etc/rsyslog.d/10-opennebula.conf"]
        self.assertIn('file = "/var/log/one/[0-9]*.log"', one)
        self.assertIn("call send_logs_onevm", one)
        self.assertIn("re_extract($!metadata!filename", one)
        self.assertIn("optimal host(.|\\n)*VM", one)  # a line break in the regular expression
        self.assertIn('not re_match($msg, "successfully monitored")', one)

    def test_a_service_keeps_its_own_days_and_its_command(self):
        s = HostService.objects.get(key="fail2ban")
        s.kept_days = 30
        rot = logconf.service_rotate(s)
        self.assertIn("rotate 30", rot)
        self.assertIn("fail2ban-client flushlogs", rot)
        s.rotate = {"paths": ["/var/log/x.log"], "method": "command"}
        with self.assertRaises(logconf.LogConfError):
            logconf.service_rotate(s)

    def test_no_collector_no_files(self):
        site = Site.objects.create(code="xx", log_collectors={"ipmi": {"host": "a", "port": 1, "proto": "udp"}})
        with self.assertRaisesMessage(logconf.LogConfError, "no log collector for the core services"):
            logconf.collector(site)
        site.log_collectors["core"] = {"host": "10.0.0.18", "port": 15140}
        self.assertEqual(logconf.collector(site), COL)

    def test_the_install_script_checks_before_it_changes_anything(self):
        text = logconf.install_script(logconf.files(self.found("frr"), COL, RULES))
        check = text.index("rsyslogd -N1")
        self.assertLess(check, text.index('mv "$f"'))
        self.assertLess(text.index("logrotate -d"), text.index("cp /etc/claugine/logging.new/rsyslog.conf"))
        self.assertIn("/etc/claugine/dist", text)
        self.assertIn("rm -f /var/log/ufnvidiaw.log", text)
        self.assertIn("systemctl restart rsyslog", text)

    @unittest.skipUnless(shutil.which("rsyslogd") or os.path.exists("/usr/sbin/rsyslogd"), "rsyslogd not installed")
    def test_rsyslog_accepts_every_service_at_once(self):
        f = logconf.files(ordered(), COL, RULES)
        with tempfile.TemporaryDirectory() as d:
            for p, text in f.items():
                q = os.path.join(d, p.removeprefix("/etc/"))
                os.makedirs(os.path.dirname(q), exist_ok=True)
                with open(q, "w") as fh:
                    fh.write(text)
            conf = os.path.join(d, "check.conf")
            with open(conf, "w") as fh:
                fh.write(
                    f["/etc/rsyslog.conf"]
                    .replace("/etc/rsyslog.d/", d + "/rsyslog.d/")
                    .replace("/var/spool/rsyslog", d)
                )
            rsyslogd = shutil.which("rsyslogd") or "/usr/sbin/rsyslogd"
            out = subprocess.run([rsyslogd, "-N1", "-f", conf], capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr + out.stdout)


class Pages(Base):
    def setUp(self):
        self.sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(self.sa)

    def test_the_tabs(self):
        r = self.client.get(reverse("infra_services"))
        self.assertContains(r, "OpenNebula front end")
        self.assertContains(r, "level=(warn|error)")
        r = self.client.get(reverse("infra_services") + "?tab=detected")
        self.assertContains(r, "No machine has been deployed with this step yet.")
        r = self.client.get(reverse("infra_services") + "?tab=rules")
        self.assertContains(r, "Queue when the collector is down, MB")

    def test_add_edit_delete(self):
        self.client.post(
            reverse("inv_service_new"),
            {
                "key": "haproxy",
                "name": "HAProxy",
                "group": "platform",
                "log_type": "haproxy",
                "present_when": "package haproxy",
                "sources": "file ¦ /var/log/haproxy.log ¦ haproxy ¦ matching ¦ (503|504)",
                "rotate_paths": "/var/log/haproxy.log",
                "rotate_method": "reopen",
                "units": "haproxy",
            },
        )
        s = HostService.objects.get(key="haproxy")
        self.assertEqual(s.present_when, [{"kind": "package", "value": "haproxy"}])
        self.assertEqual(
            s.sources,
            [
                {
                    "kind": "file",
                    "match": "/var/log/haproxy.log",
                    "tag": "haproxy",
                    "send": "matching",
                    "filter": "(503|504)",
                }
            ],
        )
        self.assertEqual(services.sources_text(s), "file ¦ /var/log/haproxy.log ¦ haproxy ¦ matching ¦ (503|504)")
        self.client.post(
            reverse("inv_service_edit", args=["haproxy"]),
            {**services.initial(s), "name": "HAProxy LB", "kept_days": "14", "key": ""},
        )
        s.refresh_from_db()
        self.assertEqual((s.name, s.kept_days), ("HAProxy LB", 14))
        self.assertEqual(s.sources[0]["filter"], "(503|504)")
        self.client.post(reverse("inv_service_delete", args=["haproxy"]))
        self.assertFalse(HostService.objects.filter(key="haproxy").exists())

    def test_a_bad_source_is_refused(self):
        r = self.client.post(
            reverse("inv_service_new"),
            {
                "key": "bad",
                "name": "Bad",
                "group": "platform",
                "sources": "file ¦ relative.log ¦ x",
                "rotate_method": "own",
            },
            follow=True,
        )
        self.assertContains(r, "A file is a full path")
        self.assertFalse(HostService.objects.filter(key="bad").exists())

    def test_the_rules(self):
        self.client.post(
            reverse("inv_logging_rules"),
            {"log_kept_days": "7", "log_queue_mb": "200", "log_retry_seconds": "20", "log_syslog_level": "5"},
        )
        conf = PortalSettings.get()
        self.assertEqual(
            (conf.log_kept_days, conf.log_queue_mb, conf.log_retry_seconds, conf.log_syslog_level), (7, 200, 20, 5)
        )

    def test_only_the_super_admin_changes_them(self):
        self.client.force_login(self.staff("rm@corp.kz", is_resource_manager=True))
        r = self.client.post(reverse("inv_service_delete", args=["mysql"]))
        self.assertNotEqual(r.status_code, 200)
        self.assertTrue(HostService.objects.filter(key="mysql").exists())


@KEY
class Deployment(TestCase):
    def test_the_steps_and_the_packages(self):
        keys = hostdeploy.STEP_KEYS
        self.assertLess(keys.index("security"), keys.index("logging"))
        self.assertLess(keys.index("logging"), keys.index("reboot"))
        self.assertIn("opennebula-prometheus-kvm", hostdeploy.PACKAGES)  # owner 07:46

    def test_the_packages_step_starts_the_exporters(self):
        r = mock.Mock()
        hostdeploy.s_packages(r)
        script = r.ssh.call_args.args[0]
        self.assertIn("systemctl enable --now opennebula-node-exporter opennebula-libvirt-exporter", script)
        self.assertLess(script.index("apt-get install"), script.index("systemctl enable --now opennebula-node"))

    def test_the_logging_step(self):
        h, c = world()
        r = hostdeploy.Run(HostDeployment.objects.create(host=h, cluster=c))
        calls = []

        def ssh(script, timeout=1800, stdin_extra=""):
            calls.append(script)
            return "libvirt\nfrr\nssh\nnope\n" if len(calls) == 1 else "ok\n"

        with mock.patch.object(r, "ssh", side_effect=ssh):
            out = hostdeploy.s_logging(r)
        self.assertIn("3 services (libvirt, frr, ssh)", out)
        self.assertIn("to onelog1:15140", out)
        self.assertIn("/etc/claugine/logging.new/rsyslog.d/11-libvirt.conf", calls[1])
        h.refresh_from_db()
        self.assertEqual(h.services, ["libvirt", "frr", "ssh"])
        self.assertIsNotNone(h.services_at)

    def test_no_core_collector_stops_the_checks(self):
        h, c = world()
        Site.objects.filter(pk=h.site.pk).update(log_collectors={"ipmi": {"host": "a", "port": 1, "proto": "udp"}})
        h = Host.objects.get(pk=h.pk)
        out = hostdeploy.problems(h, c, confirmed=True, power="Off")
        self.assertTrue(any("no log collector for the core services" in x for x in out), out)


class ReviewFixes(TestCase):
    """commit-257, commit-258, commit-259, commit-184, commit-260 (review of 7a6fb3e; owner 08:22 "all five")."""

    def found(self, *keys):
        return [s for s in ordered() if s.key in keys]

    def test_257_logs_are_not_world_readable(self):
        f = logconf.files(self.found("frr"), COL, RULES)
        self.assertIn("$FileCreateMode 0640", f["/etc/rsyslog.conf"])
        self.assertIn("create 0640 root adm", f["/etc/logrotate.conf"])
        actions = [ln for text in f.values() for ln in text.splitlines() if 'type = "omfile"' in ln]
        self.assertTrue(actions)
        for ln in actions:  # an action() ignores $FileCreateMode (review of 52d0d71)
            self.assertIn('fileCreateMode = "0640"', ln)
        self.assertNotIn("0644 root adm", f["/etc/logrotate.conf"])

    def _install(self, fail_active, fail_restart=False, fail_restore=False):
        """The install script run against a copy of /etc in a temporary directory, with stand-ins for the tools."""
        content = logconf.files(self.found("frr"), COL, RULES)
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        for d in ("etc/rsyslog.d", "etc/logrotate.d", "var/log"):
            os.makedirs(os.path.join(root, d))
        for rel, text in (
            ("etc/rsyslog.conf", "OLD main\n"),
            ("etc/rsyslog.d/50-default.conf", "OLD default\n"),
            ("etc/logrotate.conf", "OLD rotate\n"),
            ("etc/logrotate.d/apt", "OLD apt\n"),
        ):
            with open(os.path.join(root, rel), "w") as fh:
                fh.write(text)
        script = logconf.install_script(content).replace("/etc/", root + "/etc/").replace("/var/", root + "/var/")
        stubs = (
            "rsyslogd() { :; }\nlogrotate() { :; }\njournalctl() { :; }\nsleep() { :; }\n"
            f'systemctl() {{ [ "$1" = is-active ] && return {1 if fail_active else 0}; '
            f'[ "$1" = restart ] && return {1 if fail_restart else 0}; return 0; }}\n'
            + (
                # the install's is-active fails, the way back's succeeds; the copy back fails
                'n=0; systemctl() { [ "$1" = is-active ] && { n=$((n+1)); [ "$n" -gt 1 ] && return 0; return 1; }; return 0; }\n'
                'cp() { case "$*" in *logging.prev/rsyslog.conf*) return 1;; esac; command cp "$@"; }\n'
                if fail_restore
                else ""
            )
        )
        p = subprocess.run(["bash", "-c", stubs + script], capture_output=True, text=True, timeout=60)

        def read(rel):
            with open(os.path.join(root, rel)) as fh:
                return fh.read()

        return p, read, root

    def test_258_a_failed_restart_puts_the_previous_files_back(self):
        p, read, root = self._install(fail_active=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("the previous logging files were put back", p.stderr)
        self.assertEqual(read("etc/rsyslog.conf"), "OLD main\n")
        self.assertEqual(read("etc/rsyslog.d/50-default.conf"), "OLD default\n")
        self.assertEqual(read("etc/logrotate.d/apt"), "OLD apt\n")
        self.assertFalse(os.path.exists(os.path.join(root, "etc/rsyslog.d/10-frr.conf")))

    def test_258_a_failed_way_back_says_so(self):
        p, read, root = self._install(fail_active=True, fail_restart=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("the previous logging files were put back", p.stderr)
        self.assertIn("rsyslog did NOT start again", p.stderr)
        self.assertNotIn("rsyslog runs with them", p.stderr)
        self.assertEqual(read("etc/rsyslog.conf"), "OLD main\n")

    def test_258_files_not_put_back_are_never_called_the_previous_ones(self):
        p, read, root = self._install(fail_active=True, fail_restore=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("putting the previous logging files back FAILED", p.stderr)
        self.assertIn("its configuration is not known", p.stderr)
        self.assertNotIn("rsyslog runs with them", p.stderr)
        self.assertNotIn("the previous logging files were put back", p.stderr)

    def test_258_a_good_install_replaces_them(self):
        p, read, root = self._install(fail_active=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(read("etc/rsyslog.conf").startswith(logconf.HEADER))
        self.assertTrue(os.path.exists(os.path.join(root, "etc/rsyslog.d/10-frr.conf")))
        self.assertEqual(read("etc/claugine/dist/rsyslog.d/50-default.conf"), "OLD default\n")
        self.assertFalse(os.path.exists(os.path.join(root, "etc/claugine/logging.prev")))

    def test_260_mark_lines_are_dropped_before_anything_is_written(self):
        f = logconf.files(self.found("frr", "opennebula"), COL, RULES)
        common = f["/etc/rsyslog.d/00-common.conf"]
        self.assertLess(common.index("--mark--"), common.index("ruleset"))  # top of the default ruleset
        one = f["/etc/rsyslog.d/11-opennebula.conf"]
        self.assertEqual(one.count("ruleset ( name = "), 8)  # commit-261: every OpenNebula file input too
        for text in (f["/etc/rsyslog.d/10-frr.conf"], one, f["/etc/rsyslog.d/95-apt.conf"]):
            for rs in text.split("ruleset ( name = ")[1:]:
                self.assertTrue(rs.split("{", 1)[1].lstrip().startswith('if ( $msg contains "-- MARK --"'), rs)

    def test_184_no_machine_names_in_the_source(self):
        import inspect

        from inventory.migrations import __path__ as mig

        texts = [inspect.getsource(logconf)]
        with open(os.path.join(mig[0], "0099_host_services.py")) as fh:
            texts.append(fh.read())
        for text in texts:
            for name in ("szk1", "sat1", "hat1", "hzk1"):
                self.assertNotIn(name, text)


@KEY
class CollectorBeforeInstall(TestCase):  # commit-259
    def test_b0_checks_the_whole_collector(self):
        h, c = world()
        Site.objects.filter(pk=h.site.pk).update(
            log_collectors={"ipmi": {"host": "a", "port": 1, "proto": "udp"}, "core": {"host": "onelog1"}}
        )
        h = Host.objects.get(pk=h.pk)
        out = hostdeploy.problems(h, c, confirmed=True, power="Off")
        self.assertTrue(any("no log collector for the core services" in x for x in out), out)

    def test_resume_before_the_install_refuses_without_it(self):
        h, c = world()
        dep = HostDeployment.objects.create(
            host=h, cluster=c, steps={"checks": {"status": "done"}}, data={"installer": {"file": "u.iso"}}
        )
        Site.objects.filter(pk=h.site.pk).update(log_collectors={"core": {"host": "onelog1", "proto": "tcp"}})
        dep = HostDeployment.objects.get(pk=dep.pk)
        with (
            mock.patch("accounts.hostmedia.on_site_node", return_value=(True, "")),
            mock.patch.object(hostdeploy, "firewall_rules", return_value=[]),
        ):
            self.assertFalse(hostdeploy.run(dep, sleep=lambda s: None))
        dep.refresh_from_db()
        self.assertEqual(dep.state, HostDeployment.State.FAILED)
        self.assertIn("no log collector for the core services", dep.log)
