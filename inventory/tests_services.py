"""Infrastructure → Services (owner 2026-11-21, T33): the services list made by the migration from the machines read on
2026-11-21, how a machine's services are found (no roles: 07:17), the rsyslog and logrotate files they make (checked
with rsyslogd -N1 when it is installed), the pages, and the host deployment's steps (the logging step; the exporters
installed: 07:46)."""

import os
import re
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


class FakeHost:
    """A host's shell for the logging scripts: detection answers the given keys, the install writes its files, the
    check reads them back (base64), rsyslog runs."""

    def __init__(self, detected, files=None, others=(), rsyslog="active", units=None):
        import re as _re

        self.re, self.detected, self.rsyslog = _re, detected, rsyslog
        self.files = dict(files or {})
        self.others, self.units, self.calls = list(others), dict(units or {}), []
        self.timer, self.pending, self.sched = "active", [], ""

    def __call__(self, script, timeout=0, stdin_extra=""):
        import base64

        self.calls.append(script)
        if "CLAUGINE_CONFIG_EOF" in script:  # owner 10:49: config changes, as the machine's python3 makes them
            return self.config(script)
        if "dpkg-query" in script and 'echo "F ' not in script:
            return self.detected
        if "logging.new" in script:
            for rel, text in self.re.findall(
                r"cat > /etc/claugine/logging\.new/(\S+) <<'CLAUGINE_LOGCONF_EOF'\n(.*?)CLAUGINE_LOGCONF_EOF",
                script,
                self.re.S,
            ):
                self.files[logconf.TIMER if rel == "timer.conf" else "/etc/" + rel] = text
            if "cat > /etc/claugine/logging.new/timer.conf" not in script:
                self.files.pop(logconf.TIMER, None)
            self.others = []
            return "ok\n"
        if script.startswith("logger "):
            return ""
        out = []
        for path in self.re.findall(r'echo "F (\S+) \$\(base64', script):
            t = self.files.get(path)
            out.append(f"F {path} " + (base64.b64encode(t.encode()).decode() if t is not None else "-"))
        out += [f"O {p}" for p in self.others]
        if f'echo "T {logconf.TIMER}"' in script and logconf.TIMER in self.files:
            out.append(f"T {logconf.TIMER}")
        if 'echo "L ' in script:  # commit-283
            sched = self.sched or (
                "{OnCalendar=*-*-**:00:00;next_elapse=x}"
                if logconf.TIMER in self.files
                else "{OnCalendar=*-*-*00:00:00}"
            )
            out.append(f"L {self.timer} {sched}")
        out += [f"P {k}" for k in self.pending]
        out.append(f"R {self.rsyslog}")
        for u, st in self.units.items():
            if f'echo "U {u} ' in script:
                out.append(f"U {u} {st}")
        return "\n".join(out) + "\n"

    def config(self, script):
        import base64
        import json

        specs = json.loads(base64.b64decode(self.re.search(r"python3 - (\S+) <<", script).group(1)))
        lines = []
        for svc in specs:
            changed = False
            for f in svc["files"]:
                cur = self.files.get(f["file"])
                if cur is None:
                    lines.append(f"C {svc['key']} {f['file']} skipped not on the machine")
                    continue
                new = logconf.apply_changes(cur, f["changes"])
                if new == cur:
                    lines.append(f"C {svc['key']} {f['file']} same")
                    continue
                self.files[f["file"]] = new
                lines.append(f"C {svc['key']} {f['file']} changed")
                changed = True
            if changed and svc["then"] != "nothing":
                lines.append(f"T {svc['key']} ok {svc['then']}: {svc['command']}")
        return "\n".join(lines) + "\n"


COL = {"host": "10.0.0.18", "port": 15140, "proto": "tcp"}
RULES = {"kept_days": 5, "queue_mb": 100, "retry_seconds": 30, "syslog_level": 4}


def ordered():
    return sorted(HostService.objects.all(), key=lambda s: (services.GROUPS.index(s.group), s.name.lower()))


class Seed(TestCase):
    def test_the_migration_made_the_services_read(self):
        keys = set(HostService.objects.values_list("key", flat=True))
        self.assertEqual(len(keys), 29)  # 0102: OpenNebula node
        for k in ("opennebula", "mysql", "libvirt", "frr", "bind", "auditd", "fail2ban", "xrdp", "nvidia", "graylog"):
            self.assertIn(k, keys)
        one = HostService.objects.get(key="opennebula")
        self.assertEqual(one.log_type, "one")
        self.assertEqual(one.present_when, [{"kind": "package", "value": "opennebula"}])
        vm = next(s for s in one.sources if s["tag"] == "one-vm")
        self.assertTrue(vm["vm_id"])
        self.assertEqual(HostService.objects.get(key="nvidia").present_when, [{"kind": "pci", "value": "10de"}])

    def test_0102_what_the_current_hosts_showed(self):
        """Owner 2026-11-21 10:04 (analysis 2-5)."""
        node = HostService.objects.get(key="opennebula-node")
        self.assertEqual(node.present_when, [{"kind": "package", "value": "opennebula-node-kvm"}])
        self.assertEqual((node.log_type, node.group), ("one", "platform"))
        self.assertEqual(node.sources[0]["match"], "/var/log/one_tproxy*.log")
        self.assertEqual(node.rotate, {"paths": ["/var/log/one_tproxy*.log"], "method": "copytruncate"})
        self.assertIn("/var/log/swtpm/libvirt/qemu/*.log", HostService.objects.get(key="libvirt").rotate["paths"])
        tags = {s["match"]: s["tag"] for s in HostService.objects.get(key="opennebula").sources}
        self.assertEqual(tags["/var/log/one/rank_sched.log"], "one-sched")
        self.assertEqual(tags["/var/log/one/host_error.log"], "one-host-error")
        col = {"host": "10.0.0.18", "port": 15140, "proto": "tcp"}
        f = logconf.files([node], col, RULES)
        text = "".join(f.values())
        self.assertIn('file = "/var/log/one_tproxy*.log"', text)
        self.assertIn("/var/log/one_tproxy*.log", "".join(v for k, v in f.items() if "logrotate" in k))
        self.assertIn("$RepeatedMsgReduction off", f["/etc/rsyslog.conf"])  # 5: every line kept with its own time
        self.assertNotIn("$RepeatedMsgReduction on", f["/etc/rsyslog.conf"])

    def test_0102_keeps_the_owners_changes(self):
        import importlib

        from django.apps import apps

        mig = importlib.import_module("inventory.migrations.0102_services_improvements")
        one = HostService.objects.get(key="opennebula")
        one.sources = [s for s in one.sources if s["tag"] != "one-host-error"] + [
            {"kind": "file", "match": "/var/log/one/host_error.log", "tag": "mine", "send": "warning"}
        ]
        one.save()
        HostService.objects.filter(key="opennebula-node").update(name="Node (renamed)")
        mig.forward(apps, None)  # run again: nothing added twice, nothing of the owner's replaced
        one.refresh_from_db()
        errs = [s for s in one.sources if s["match"] == "/var/log/one/host_error.log"]
        self.assertEqual([s["tag"] for s in errs], ["mine"])
        self.assertEqual(sum(s["match"] == "/var/log/one/rank_sched.log" for s in one.sources), 1)
        self.assertEqual(HostService.objects.get(key="opennebula-node").name, "Node (renamed)")
        lv = HostService.objects.get(key="libvirt")
        self.assertEqual(lv.rotate["paths"].count("/var/log/swtpm/libvirt/qemu/*.log"), 1)

    def test_268_the_way_back_removes_nothing(self):
        import importlib

        from django.apps import apps

        mig = importlib.import_module("inventory.migrations.0102_services_improvements")
        before = {s.key: (s.name, s.sources, s.rotate) for s in HostService.objects.all()}
        mig.back(apps, None)
        after = {s.key: (s.name, s.sources, s.rotate) for s in HostService.objects.all()}
        self.assertEqual(before, after)

    def test_269_no_swtpm_pattern_over_the_owners_own_path(self):
        import importlib

        from django.apps import apps

        mig = importlib.import_module("inventory.migrations.0102_services_improvements")
        lv = HostService.objects.get(key="libvirt")
        mine = "/var/log/swtpm/libvirt/qemu/vm-1-swtpm.log"
        lv.rotate = dict(lv.rotate, paths=[p for p in lv.rotate["paths"] if "swtpm" not in p] + [mine])
        lv.save()
        mig.forward(apps, None)
        lv.refresh_from_db()
        self.assertEqual([p for p in lv.rotate["paths"] if "swtpm" in p], [mine])

    def test_269_no_swtpm_pattern_under_a_parent_pattern(self):
        import importlib

        from django.apps import apps

        mig = importlib.import_module("inventory.migrations.0102_services_improvements")
        lv = HostService.objects.get(key="libvirt")
        wide = "/var/log/*/libvirt/qemu/*.log"
        lv.rotate = dict(lv.rotate, paths=[p for p in lv.rotate["paths"] if "swtpm" not in p] + [wide])
        lv.save()
        mig.forward(apps, None)
        lv.refresh_from_db()
        self.assertNotIn("/var/log/swtpm/libvirt/qemu/*.log", lv.rotate["paths"])
        self.assertIn(wide, lv.rotate["paths"])

    def test_269_no_swtpm_pattern_where_any_libvirt_path_meets_it(self):
        import importlib

        from django.apps import apps

        mig = importlib.import_module("inventory.migrations.0102_services_improvements")
        swtpm = "/var/log/swtpm/libvirt/qemu/*.log"
        for mine, added in (
            ("/var/log/*/libvirt/qemu/guest-*.log", False),  # misses any one sample name, still meets it
            ("/var/log/sw?pm/libvirt/qemu/[!x]*", False),
            ("/var/log/libvirt/*/*.log", True),  # one part more or less: never the same file
            ("/var/log/sw[a-t]pm/*/qemu/*.txt", True),  # .txt and .log never meet
        ):
            lv = HostService.objects.get(key="libvirt")
            lv.rotate = dict(lv.rotate, paths=["/var/log/libvirt/libvirtd.log", mine])
            lv.save()
            mig.forward(apps, None)
            lv.refresh_from_db()
            self.assertEqual(swtpm in lv.rotate["paths"], added, mine)

    def test_269_275_posix_classes_are_read_as_classes(self):
        import importlib

        from django.apps import apps

        mig = importlib.import_module("inventory.migrations.0102_services_improvements")
        swtpm = "/var/log/swtpm/libvirt/qemu/*.log"
        for mine, added in (
            ("/var/log/sw[[:alpha:]]pm/libvirt/qemu/guest-*.log", False),  # commit-269: [:alpha:] has the t
            ("/var/log/swtpm/libvirt/qemu/[[:digit:]].log", False),  # commit-275
            ("/var/log/sw[[:digit:]]pm/libvirt/qemu/*.log", True),  # no digit is a t
            ("/var/log/sw[[=t=]]pm/libvirt/qemu/*.log", False),  # not read for certain: taken as meeting
        ):
            lv = HostService.objects.get(key="libvirt")
            lv.rotate = dict(lv.rotate, paths=["/var/log/libvirt/libvirtd.log", mine])
            lv.save()
            mig.forward(apps, None)
            lv.refresh_from_db()
            self.assertEqual(swtpm in lv.rotate["paths"], added, mine)

    def test_the_migration_and_logconf_test_alike(self):
        import importlib

        mig = importlib.import_module("inventory.migrations.0102_services_improvements")
        pairs = [
            ("/var/log/sw[[:alpha:]]pm/x/*.log", "/var/log/swtpm/x/a.log"),
            ("/var/log/[!a-z]*", "/var/log/syslog"),
            ("/var/log/example/*.log", "/var/log/example/app*.log"),
            ("/var/log/a/*.txt", "/var/log/a/*.log"),
            ("/var/log/[]x].log", "/var/log/].log"),
        ]
        for p, q in pairs:
            self.assertEqual(mig.globs_meet(p, q), logconf.globs_meet(p, q), (p, q))

    def test_274_patterns_that_can_meet_are_refused_before_any_file_exists(self):
        col = {"host": "10.0.0.18", "port": 15140, "proto": "tcp"}
        a = HostService(key="ex-a", name="A", rotate={"paths": ["/var/log/example/*.log"], "method": "copytruncate"})
        b = HostService(key="ex-b", name="B", rotate={"paths": ["/var/log/example/app*.log"], "method": "copytruncate"})
        with self.assertRaises(logconf.LogConfError) as cm:
            logconf.files([a, b], col, RULES)
        self.assertIn("/var/log/example/app*.log", str(cm.exception))
        c = HostService(key="ex-c", name="C", rotate={"paths": ["/var/log/[[:alpha:]]yslog"], "method": "copytruncate"})
        with self.assertRaises(logconf.LogConfError):  # meets the common /var/log/syslog
            logconf.files([c], col, RULES)
        own = HostService(key="ex-d", name="D", rotate={"paths": ["/var/log/syslog"], "method": "own"})
        self.assertTrue(logconf.files([own], col, RULES))  # a service rotating its own logs writes no stanza

    def test_276_a_line_is_read_as_logrotate_reads_it(self):
        import importlib

        from django.apps import apps

        a = HostService(key="ex-a", name="A", rotate={"paths": ["/var/log/example/*.log"], "method": "copytruncate"})
        b = HostService(
            key="ex-b", name="B", rotate={"paths": ['"/var/log/example/app*.log"'], "method": "copytruncate"}
        )
        with self.assertRaisesMessage(logconf.LogConfError, "/var/log/example/app*.log"):
            logconf.files([a, b], COL, RULES)
        two = HostService(key="ex-c", name="C", rotate={"paths": ["/var/log/x.log /var/log/syslog"]})
        with self.assertRaises(logconf.LogConfError):  # two patterns on one line: the second meets the common one
            logconf.files([two], COL, RULES)
        bad = HostService(key="ex-d", name="D", rotate={"paths": ['"/var/log/x.log']})
        with self.assertRaisesMessage(logconf.LogConfError, "cannot be read"):
            logconf.files([bad], COL, RULES)
        mig = importlib.import_module("inventory.migrations.0102_services_improvements")
        swtpm = "/var/log/swtpm/libvirt/qemu/*.log"
        for mine, added in (
            ('"/var/log/swtpm/libvirt/qemu/guest-*.log"', False),
            ('"/var/log/swtpm/libvirt/qemu/x.log', False),  # not read for certain: nothing added
            ('"/var/log/libvirt/qemu/*.log"', True),
        ):
            lv = HostService.objects.get(key="libvirt")
            lv.rotate = dict(lv.rotate, paths=["/var/log/libvirt/libvirtd.log", mine])
            lv.save()
            mig.forward(apps, None)
            lv.refresh_from_db()
            self.assertEqual(swtpm in lv.rotate["paths"], added, mine)

    def test_286_a_line_is_split_as_popt_splits_it(self):
        import importlib

        mig = importlib.import_module("inventory.migrations.0102_services_improvements")
        cases = {
            r'"/var/log/example/app\\*.log"': [r"/var/log/example/app\\*.log"],  # in quotes: both backslashes kept
            r'"/x/a\"b"': ['/x/a"b'],
            r"/x/a\*b": ["/x/a*b"],  # outside quotes: the backslash goes, the * stays a wildcard for glob()
            "'/x/y z'": ["/x/y z"],
            "/a /b": ["/a", "/b"],
            '"/a': None,
            "/var/log/example/app\u00a0worker.log": ["/var/log/example/app\u00a0worker.log"],  # commit-291
            "/a\t/b\v/c": ["/a", "/b", "/c"],
            "/a\\": None,
        }
        for line, want in cases.items():
            self.assertEqual(logconf.rotation_args(line), want, line)
            self.assertEqual(mig.rotation_args(line), want, line)
        a = HostService(
            key="ex-a", name="A", rotate={"paths": [r'"/var/log/example/app\\*.log"'], "method": "copytruncate"}
        )
        b = HostService(
            key="ex-b", name="B", rotate={"paths": ["/var/log/example/app?worker.log"], "method": "copytruncate"}
        )
        with self.assertRaises(logconf.LogConfError):  # no file needed: the patterns themselves meet
            logconf.files([a, b], COL, RULES)
        nb = HostService(
            key="ex-n", name="N", rotate={"paths": ["/var/log/example/app\u00a0worker.log"], "method": "copytruncate"}
        )
        star = HostService(
            key="ex-s", name="S", rotate={"paths": ["/var/log/example/app*worker.log"], "method": "copytruncate"}
        )
        with self.assertRaises(logconf.LogConfError):  # commit-291: one path to logrotate, so it meets app*worker
            logconf.files([nb, star], COL, RULES)

    def test_277_wildcards_never_give_a_leading_dot(self):
        import importlib

        from django.apps import apps

        a = HostService(key="ex-a", name="A", rotate={"paths": ["/var/log/example/*.log"], "method": "copytruncate"})
        h = HostService(
            key="ex-h", name="H", rotate={"paths": ["/var/log/example/.audit.log"], "method": "copytruncate"}
        )
        self.assertTrue(logconf.files([a, h], COL, RULES))
        d = HostService(key="ex-d", name="D", rotate={"paths": ["/srv/logs/.hidden/*.log"], "method": "copytruncate"})
        w = HostService(key="ex-w", name="W", rotate={"paths": ["/srv/logs/*/*.log"], "method": "copytruncate"})
        self.assertTrue(logconf.files([d, w], COL, RULES))  # a directory part too
        dots = HostService(
            key="ex-e", name="E", rotate={"paths": ["/var/log/example/.*.log"], "method": "copytruncate"}
        )
        with self.assertRaises(logconf.LogConfError):
            logconf.files([h, dots], COL, RULES)
        mig = importlib.import_module("inventory.migrations.0102_services_improvements")
        lv = HostService.objects.get(key="libvirt")
        lv.rotate = dict(lv.rotate, paths=["/var/log/libvirt/libvirtd.log", "/var/log/swtpm/libvirt/qemu/.*.log"])
        lv.save()
        mig.forward(apps, None)
        lv.refresh_from_db()
        self.assertIn("/var/log/swtpm/libvirt/qemu/*.log", lv.rotate["paths"])

    def test_271_272_overlaps_are_left_to_the_hosts_own_logrotate_check(self):
        """commit-271/272: no text-level guess; the staged logrotate -d refuses duplicates of real files."""
        col = {"host": "10.0.0.18", "port": 15140, "proto": "tcp"}
        self.assertTrue(logconf.files(list(HostService.objects.all()), col, RULES))
        frr = HostService.objects.get(key="frr")
        frr.rotate = {"paths": ["/var/log/frr/*.log"], "method": "command", "command": "/bin/echo '}' /var/log/syslog"}
        self.assertTrue(logconf.files([frr], col, RULES))  # a brace in a command is no longer read as syntax
        script = logconf.install_script(logconf.files([frr], col, RULES))
        self.assertIn("logrotate -d", script)
        self.assertIn("exit 1", script[script.index("logrotate -d") :].split("\n")[0])

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

    def test_276_a_rotated_file_is_one_plain_path(self):
        for paths in ('"/var/log/x.log"', "/var/log/a.log /var/log/b.log", "var/log/x.log"):
            form = services.ServiceForm(
                {"key": "x", "name": "X", "group": "platform", "rotate_paths": paths, "rotate_method": "copytruncate"}
            )
            self.assertFalse(form.is_valid(), paths)
            self.assertIn("rotate_paths", form.errors)

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
        host = FakeHost("libvirt\nfrr\nssh\nnope\n")
        with mock.patch.object(r, "ssh", side_effect=host):
            out = hostdeploy.s_logging(r)
        self.assertIn("3 services (libvirt, frr, ssh)", out)
        self.assertIn("to onelog1:15140", out)
        self.assertIn("compliant", out)  # checked right after (owner 09:21: applied when deployed)
        self.assertIn("/etc/claugine/logging.new/rsyslog.d/11-libvirt.conf", host.calls[1])
        h.refresh_from_db()
        self.assertEqual(h.services, ["libvirt", "frr", "ssh"])
        self.assertIsNotNone(h.services_at)
        self.assertEqual(h.logging["status"], "compliant")
        self.assertIsNotNone(h.logging_applied_at)  # commit-266: its own field
        self.assertNotIn("applied_at", h.logging)

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

    def _install(self, fail_active, fail_restart=False, fail_restore=False, locked=False, no_calendar=False):
        """The install script run against a copy of /etc in a temporary directory, with stand-ins for the tools."""
        content = logconf.files(self.found("frr"), COL, RULES)
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        for d in ("etc/rsyslog.d", "etc/logrotate.d", "var/log", "run"):
            os.makedirs(os.path.join(root, d))
        for rel, text in (
            ("etc/rsyslog.conf", "OLD main\n"),
            ("etc/rsyslog.d/50-default.conf", "OLD default\n"),
            ("etc/logrotate.conf", "OLD rotate\n"),
            ("etc/logrotate.d/apt", "OLD apt\n"),
        ):
            with open(os.path.join(root, rel), "w") as fh:
                fh.write(text)
        # one pass: the temporary directory may itself be under /var (macOS)
        script = re.sub(
            r"(?<![\w./-])/(etc|var|run)/", lambda m: f"{root}/{m.group(1)}/", logconf.install_script(content)
        )
        # commit-263: the hosts have flock (util-linux); where the tests run it may be missing (macOS) - the same
        # lock (flock(2) on the inherited descriptor 9) taken by Python then
        import sys

        flock = (
            ""
            if shutil.which("flock")
            else f"flock() {{ {sys.executable} -c 'import fcntl; fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)'; }}\n"
        )
        stubs = (
            flock + "rsyslogd() { :; }\nlogrotate() { :; }\njournalctl() { :; }\nsleep() { :; }\n"
            f'systemctl() {{ [ "$1" = is-active ] && return {1 if fail_active else 0}; '
            # commit-290: the package's daily schedule, as `systemctl show -P TimersCalendar` gives it
            + (
                "[ \"$1\" = show ] && { echo '{ next_elapse=x }'; return 0; }; "  # commit-296: no OnCalendar read
                if no_calendar
                else "[ \"$1\" = show ] && { echo '{ OnCalendar=*-*-* 00:00:00 ; next_elapse=x }'; return 0; }; "
            )
            + f'[ "$1" = restart ] && return {1 if fail_restart else 0}; return 0; }}\n'
            + (
                # the install's is-active fails, the way back's succeeds; the copy back fails
                'n=0; systemctl() { [ "$1" = is-active ] && { n=$((n+1)); [ "$n" -gt 1 ] && return 0; return 1; }; return 0; }\n'
                'cp() { case "$*" in *logging.prev/rsyslog.conf*) return 1;; esac; command cp "$@"; }\n'
                if fail_restore
                else ""
            )
        )
        lock = None
        if locked:  # commit-263: another install holds the host's lock
            import fcntl

            lock = open(os.path.join(root, "run/claugine-logging.lock"), "w")  # noqa: SIM115
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            p = subprocess.run(["bash", "-c", stubs + script], capture_output=True, text=True, timeout=60)
        finally:
            if lock:
                lock.close()

        def read(rel):
            with open(os.path.join(root, rel)) as fh:
                return fh.read()

        return p, read, root

    def test_296_a_schedule_that_cannot_be_read(self):
        p, read, root = self._install(fail_active=False, no_calendar=True)
        self.assertEqual(p.returncode, 1, p.stderr)  # the install's own status, not the way back's
        self.assertIn("the schedule of logrotate.timer could not be read", p.stderr)
        self.assertIn("the previous logging files were put back", p.stderr)
        self.assertIn("logrotate.timer runs, but its schedule could not be read", p.stderr)  # the way back says so
        self.assertEqual(read("etc/rsyslog.conf"), "OLD main\n")

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

    def test_263_a_second_install_is_refused_and_changes_nothing(self):
        p, read, root = self._install(fail_active=False, locked=True)
        self.assertEqual(p.returncode, logconf.BUSY, p.stderr)
        self.assertIn("another logging install is running on this host", p.stderr)
        self.assertEqual(read("etc/rsyslog.conf"), "OLD main\n")
        self.assertEqual(read("etc/rsyslog.d/50-default.conf"), "OLD default\n")
        self.assertFalse(os.path.exists(os.path.join(root, "etc/claugine/logging.new")))
        self.assertFalse(os.path.exists(os.path.join(root, "etc/claugine/logging.prev")))

    def test_263_the_lock_comes_before_the_staging(self):
        script = logconf.install_script(logconf.files(self.found("frr"), COL, RULES))
        self.assertLess(script.index("flock -n 9"), script.index(f"rm -rf {logconf.STAGE}"))
        self.assertLess(script.index("flock -n 9"), script.index(f"rm -rf {logconf.PREV}"))

    def test_260_mark_lines_are_dropped_before_anything_is_written(self):
        f = logconf.files(self.found("frr", "opennebula"), COL, RULES)
        common = f["/etc/rsyslog.d/00-common.conf"]
        self.assertLess(common.index("--mark--"), common.index("ruleset"))  # top of the default ruleset
        one = f["/etc/rsyslog.d/11-opennebula.conf"]
        self.assertEqual(one.count("ruleset ( name = "), 10)  # commit-261: every OpenNebula file input (0102: +2)
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


@KEY
class HostLogging(Base):
    """Owner 2026-11-21 09:21 - 09:35 (T33): Check logging (read only) and Apply logging on a deployed host; the
    daily check; the host's details unchanged but for the two buttons and the Logging part at the bottom."""

    def setUp(self):
        from inventory import hostlogging

        self.hl = hostlogging
        self.h, self.c = world()
        self.sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(self.sa)

    def test_a_host_set_up_by_hand_differs_and_says_where(self):
        old = {"/etc/rsyslog.d/00-common.conf": "OLD\n", "/etc/logrotate.conf": "rotate 5\n"}
        host = FakeHost("frr\nssh\n", files=old, others=["/etc/rsyslog.d/40-frr.conf", "/etc/rsyslog.d/92-nvidia.conf"])
        r = self.hl.check(self.h, host)
        self.assertEqual(r["status"], "differs")
        states = {f["path"]: f["state"] for f in r["files"]}
        self.assertEqual(states["/etc/rsyslog.d/00-common.conf"], "differs")
        self.assertEqual(states["/etc/rsyslog.d/10-frr.conf"], "added")
        self.assertEqual(states["/etc/rsyslog.d/92-nvidia.conf"], "moved aside")
        diff = next(f["diff"] for f in r["files"] if f["path"] == "/etc/rsyslog.d/00-common.conf")
        self.assertIn("-OLD", diff)
        self.assertFalse(any("logging.new" in c for c in host.calls))  # the check never writes
        self.h.refresh_from_db()
        self.assertEqual(self.h.logging["status"], "differs")
        self.assertEqual(self.h.services, ["frr", "ssh"])

    def test_apply_then_compliant(self):
        host = FakeHost("frr\nssh\n", others=["/etc/rsyslog.d/40-frr.conf"])
        r = self.hl.apply(self.sa, self.h, host)
        self.assertEqual(r["status"], "compliant", r)
        self.assertEqual(r["test_message"], "sent")
        self.h.refresh_from_db()
        applied = self.h.logging_applied_at
        self.assertIsNotNone(applied)
        self.assertTrue(any(c.startswith("logger ") and "claugine logging check kvm014" in c for c in host.calls))
        r2 = self.hl.check(self.h, host)  # a later check keeps when it was applied
        self.h.refresh_from_db()
        self.assertEqual((r2["status"], self.h.logging_applied_at), ("compliant", applied))

    def test_263_apply_is_refused_while_a_deployment_runs(self):
        host = FakeHost("frr\n")
        with (
            mock.patch.object(self.hl, "deploy_running", return_value=True),
            self.assertRaisesMessage(self.hl.LoggingError, "a deployment of kvm014 is running"),
        ):
            self.hl.apply(self.sa, self.h, host)
        self.assertEqual(host.calls, [])  # nothing sent to the host
        r = self.client.post(reverse("inv_host_logging_apply", args=["zk1", "kvm014"]), follow=True)
        self.assertNotContains(r, "a deployment of kvm014 is running")  # none runs here
        with mock.patch.object(self.hl, "deploy_running", return_value=True):
            r = self.client.post(reverse("inv_host_logging_apply", args=["zk1", "kvm014"]), follow=True)
        self.assertContains(r, "Logging not applied: a deployment of kvm014 is running")

    def test_263_a_deployment_is_refused_while_apply_runs(self):
        from accounts import tasks

        host = FakeHost("frr\n")
        tried = {}

        def install(script, timeout=0, stdin_extra=""):
            if "logging.new" in script:  # the install is under way: a deployment is started now
                self.h.refresh_from_db()
                self.assertTrue(self.hl.claim_fresh(self.h.logging_claim))
                with mock.patch("accounts.ha.forbid_writes"), mock.patch.object(tasks, "start"):
                    try:
                        hostdeploy.start(None, self.h, self.c)
                    except hostdeploy.DeployError as exc:
                        tried["error"] = str(exc)
                with self.assertRaisesMessage(self.hl.LoggingError, "already running"):  # a second Apply too
                    self.hl.apply(self.sa, self.h, FakeHost("frr\n"))
            return host(script, timeout)

        self.hl.apply(self.sa, self.h, install)
        self.assertIn("Apply logging of this host is running", tried.get("error", ""))
        self.assertFalse(HostDeployment.objects.filter(host=self.h).exists())
        self.h.refresh_from_db()
        self.assertEqual(self.h.logging_claim, {})  # released when it ended

    def test_263_the_claim_is_released_when_apply_fails(self):
        def down(script, timeout=0, stdin_extra=""):
            raise RuntimeError("timed out")

        with self.assertRaises(self.hl.LoggingError):
            self.hl.apply(self.sa, self.h, down)
        self.h.refresh_from_db()
        self.assertEqual(self.h.logging_claim, {})

    def test_263_apply_is_refused_while_a_deployment_is_still_starting(self):
        from django.utils import timezone

        HostDeployment.objects.create(  # claimed, its task not listed yet (it starts after the commit)
            host=self.h,
            cluster=self.c,
            state=HostDeployment.State.RUNNING,
            data={"claim": "x", "claim_at": timezone.now().isoformat()},
        )
        host = FakeHost("frr\n")
        with self.assertRaisesMessage(self.hl.LoggingError, "a deployment of kvm014 is running"):
            self.hl.apply(self.sa, self.h, host)
        self.assertEqual(host.calls, [])
        HostDeployment.objects.update(data={"claim": "x", "claim_at": "2000-01-01T00:00:00+00:00"})
        self.hl.apply(self.sa, self.h, host)  # a run left behind long ago is no deployment in progress

    def test_270_a_retirement_is_refused_while_apply_runs(self):
        from django.utils import timezone

        from inventory import hostretire

        Host.objects.filter(pk=self.h.pk).update(logging_claim={"token": "t", "at": timezone.now().isoformat()})
        with (
            mock.patch("accounts.ha.forbid_writes"),
            self.assertRaisesMessage(hostdeploy.DeployError, "Apply logging of this host is running"),
        ):
            hostretire.start(None, self.h, self.c)
        self.assertFalse(HostDeployment.objects.filter(host=self.h).exists())

    def test_273_a_host_edit_keeps_an_apply_claim(self):
        from django.utils import timezone

        from inventory import ops

        loaded_before = Host.objects.get(pk=self.h.pk)  # an edit that read the host before Apply's claim
        claim = {"token": "t", "at": timezone.now().isoformat()}
        Host.objects.filter(pk=self.h.pk).update(logging_claim=claim)
        real = Host.objects.filter

        def stale(*a, **kw):  # the edit's own read returns the copy loaded before the claim
            if "site" in kw and kw.get("name") == self.h.name:
                return mock.Mock(first=lambda: loaded_before)
            return real(*a, **kw)

        with mock.patch.object(ops.Host.objects, "filter", side_effect=stale):
            ops.host_save(self.sa, "zk1", self.h.name, None, "10.201.100.99", new=False)
        self.h.refresh_from_db()
        self.assertEqual((self.h.logging_claim, self.h.ip), (claim, "10.201.100.99"))
        self.assertEqual(loaded_before.logging_claim, {})
        with mock.patch("accounts.ha.forbid_writes"), self.assertRaisesMessage(hostdeploy.DeployError, "Apply logging"):
            hostdeploy.start(None, self.h, self.c)

    def test_264_a_failed_test_message_leaves_the_install_recorded(self):
        from accounts.models import AuditLog

        host = FakeHost("frr\nssh\n")

        def no_logger(script, timeout=0, stdin_extra=""):
            if script.startswith("logger "):
                raise RuntimeError("10.201.100.14:22: timed out")
            return host(script, timeout)

        r = self.hl.apply(self.sa, self.h, no_logger)  # not an error: the files are installed
        self.assertEqual(r["status"], "compliant")
        self.assertIn("not sent", r["test_message"])
        self.assertIn("timed out", r["test_message"])
        self.h.refresh_from_db()
        self.assertIsNotNone(self.h.logging_applied_at)
        actions = set(AuditLog.objects.values_list("action", flat=True))
        self.assertIn("host_logging_applied", actions)
        self.assertIn("host_logging_test_failed", actions)
        self.assertNotIn("host_logging_failed", actions)
        with mock.patch.object(self.hl, "apply", return_value={"status": "compliant", "test_message": "not sent: x"}):
            r = self.client.post(reverse("inv_host_logging_apply", args=["zk1", "kvm014"]), follow=True)
        self.assertContains(r, "Logging applied: compliant.")
        self.assertContains(r, "The test message was not sent: x")

    def test_266_a_check_that_ends_after_an_apply_does_not_put_the_old_state_back(self):
        from datetime import timedelta

        from django.utils import timezone

        host = FakeHost("frr\n", files={"/etc/rsyslog.d/00-common.conf": "OLD\n"})
        started = timezone.now()
        old = self.hl.compare(
            {"/etc/rsyslog.d/00-common.conf": "NEW\n"},
            host(self.hl.check_script(["/etc/rsyslog.d/00-common.conf"], [])),
        )  # read before the apply
        self.hl.apply(self.sa, self.h, FakeHost("frr\n"))  # stores its newer, compliant result
        self.h.refresh_from_db()
        applied, newer = self.h.logging_applied_at, self.h.logging
        services, services_at = self.h.services, self.h.services_at
        old["at"] = (started - timedelta(seconds=1)).isoformat()
        stale = {"services": ["old-set"], "services_at": started - timedelta(seconds=1)}
        self.assertFalse(self.hl._store(self.h, old, **stale))  # the older observation is dropped
        self.h.refresh_from_db()
        self.assertEqual(self.h.logging, newer)
        self.assertEqual((self.h.services, self.h.services_at), (services, services_at))  # commit-267: with it
        self.assertEqual(self.h.logging["status"], "compliant")
        self.assertEqual(self.h.logging_applied_at, applied)
        later = dict(old, at=(timezone.now() + timedelta(seconds=1)).isoformat())
        self.assertTrue(self.hl._store(self.h, later))  # a newer one is kept
        self.h.refresh_from_db()
        self.assertEqual(self.h.logging["status"], "differs")
        self.assertEqual(self.h.logging_applied_at, applied)  # a check never touches it

    def test_265_the_same_name_on_two_sites_is_counted_twice(self):
        other = Host(site=Site(code="at1"), name="kvm014")
        status = {"zk1": "compliant", "at1": "not checked"}
        with (
            mock.patch.object(self.hl, "deployed_hosts", return_value=[self.h, other]),
            mock.patch.object(self.hl, "service_vms", return_value=[]),
            mock.patch.object(self.hl, "check", side_effect=lambda h: {"status": status[h.site.code]}),
        ):
            ok, detail = self.hl.check_all()
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("2 machines, 1 compliant"), detail)
        self.assertIn("not read: at1.kvm014", detail)

    def test_a_failed_apply_is_reported_and_checked(self):
        host = FakeHost("frr\n")

        def failing(script, timeout=0, stdin_extra=""):
            if "logging.new" in script:
                raise RuntimeError("rsyslogd -N1: error in 10-frr.conf")
            return host(script, timeout)

        with self.assertRaisesMessage(self.hl.LoggingError, "rsyslogd -N1"):
            self.hl.apply(self.sa, self.h, failing)
        self.h.refresh_from_db()
        self.assertEqual(self.h.logging["status"], "differs")

    def test_units_not_running_are_reported(self):
        host = FakeHost("frr\n", units={"frr": "failed"})
        r = self.hl.check(self.h, host)
        self.assertEqual(r["units_down"], ["frr (failed)"])

    def test_an_unreachable_host_is_not_checked(self):
        def down(script, timeout=0, stdin_extra=""):
            raise RuntimeError("10.201.100.14:22: timed out")

        r = self.hl.check(self.h, down)
        self.assertEqual(r["status"], "not checked")
        self.assertIn("timed out", r["error"])

    def test_the_daily_check(self):
        with mock.patch.object(self.hl, "check", return_value={"status": "differs"}):
            ok, detail = self.hl.check_all()
        self.assertTrue(ok)  # differing is a report, not a failure of the timer
        self.assertIn("differ: zk1.kvm014", detail)
        with mock.patch.object(self.hl, "check", return_value={"status": "not checked"}):
            ok, detail = self.hl.check_all()
        self.assertFalse(ok)

    def test_the_buttons_and_the_details(self):
        Host.objects.filter(pk=self.h.pk).update(
            logging={
                "status": "differs",
                "at": "2026-11-21T09:30:00+05:00",
                "services": ["frr"],
                "rsyslog": "active",
                "units_down": [],
                "files": [{"path": "/etc/rsyslog.d/00-common.conf", "state": "differs", "diff": "-OLD\n+NEW\n"}],
            }
        )
        r = self.client.get(reverse("infra_site", args=["zk1"]) + "?tab=hosts")
        self.assertContains(r, "Read hardware")
        self.assertContains(r, ">Check logging</button>")
        self.assertContains(r, "Apply logging")
        text = r.content.decode()
        self.assertLess(text.index("Read hardware"), text.index(">Check logging</button>"))
        self.assertLess(text.index(">Check logging</button>"), text.index('data-open="dlg-log-kvm014"'))
        self.assertLess(text.index("Health"), text.index("Services found"))  # Logging at the bottom
        self.assertContains(r, "-OLD")

    def test_check_and_apply_views(self):
        with mock.patch("inventory.hostlogging.check", return_value={"status": "compliant"}) as chk:
            r = self.client.post(reverse("inv_host_logging_check", args=["zk1", "kvm014"]), follow=True)
        chk.assert_called_once()
        self.assertContains(r, "Logging is compliant.")
        with mock.patch("inventory.hostlogging.apply", side_effect=self.hl.LoggingError("no route")):
            r = self.client.post(reverse("inv_host_logging_apply", args=["zk1", "kvm014"]), follow=True)
        self.assertContains(r, "Logging not applied: no route")
        self.client.force_login(self.staff("sup@corp.kz", is_support=True))
        with mock.patch("inventory.hostlogging.apply") as app:
            self.client.post(reverse("inv_host_logging_apply", args=["zk1", "kvm014"]))
        app.assert_not_called()  # Apply logging: the super admin only


NAMED = """logging {
  channel default_log {
    file "/var/log/bind/default.log" versions 3 size 10m;
    severity info;
  };
  channel queries_log {
    file "/var/log/bind/queries.log" versions 3 size 10m;
    severity info;
  };
};
"""


class ConfigChanges(TestCase):
    """Owner 2026-11-21 10:49 "BIND B1 auditd and Graylog too" (mockup 12): a size on a service's rotation (logrotate
    hourly there) and config changes in the services' own files."""

    def test_0104_seeds_bind_auditd_graylog(self):
        bind = HostService.objects.get(key="bind")
        self.assertEqual(bind.rotate["max_mb"], 100)
        self.assertEqual(bind.config["then"], "reload")
        self.assertEqual(
            logconf.apply_changes(NAMED, logconf.config_of(bind)["files"][0]["changes"]).count("versions"), 0
        )
        for key, path in (("auditd", "/var/log/audit/audit.log"), ("graylog", "/var/log/graylog-server/*.log")):
            s = HostService.objects.get(key=key)
            self.assertEqual(s.rotate["method"], "copytruncate", key)
            self.assertIn(path, s.rotate["paths"])
            self.assertTrue(s.config["changes"], key)
            logconf.check_config(s)
        g = logconf.config_of(HostService.objects.get(key="graylog"))
        self.assertEqual([len(f["changes"]) for f in g["files"]], [3, 3])
        self.assertEqual(g["then"], "restart")

    def test_0104_keeps_the_owners_changes(self):
        import importlib

        from django.apps import apps

        mig = importlib.import_module("inventory.migrations.0104_service_config")
        bind = HostService.objects.get(key="bind")
        bind.rotate = {"paths": ["/var/log/bind/*.log"], "method": "copytruncate", "max_mb": 300}
        bind.config = {"changes": [{"file": "/etc/bind/x", "find": "a", "replace": "b"}], "then": "nothing"}
        bind.save()
        ad = HostService.objects.get(key="auditd")
        ad.rotate, ad.config = {"method": "own"}, {}
        ad.notes = "mine"
        ad.save()
        mig.forward(apps, None)
        bind.refresh_from_db()
        ad.refresh_from_db()
        self.assertEqual(bind.rotate["max_mb"], 300)
        self.assertEqual(bind.config["changes"][0]["file"], "/etc/bind/x")
        self.assertEqual(ad.rotate["method"], "copytruncate")  # "own" with no paths: the seed's, changed by 0104
        self.assertEqual(ad.notes, "mine")

    def test_284_no_config_change_where_the_rotation_does_not_cover_the_logs(self):
        import importlib

        from django.apps import apps

        mig = importlib.import_module("inventory.migrations.0104_service_config")
        bind = HostService.objects.get(key="bind")
        bind.rotate, bind.config = {"method": "own"}, {}
        bind.save()
        ad = HostService.objects.get(key="auditd")
        ad.rotate, ad.config = {"paths": ["/var/log/audit/other.log"], "method": "copytruncate"}, {}
        ad.save()
        mig.forward(apps, None)
        bind.refresh_from_db()
        ad.refresh_from_db()
        self.assertEqual((bind.config, ad.config), ({}, {}))
        self.assertNotIn("; true", HostService.objects.get(key="graylog").config["command"])  # commit-278

    def test_the_seeded_changes_on_the_files_read_2026_11_21(self):
        log4j = (
            '<Configuration>\n  <Appenders>\n    <RollingFile name="rolling-file" fileName="/var/log/graylog-server/'
            'server.log" filePattern="/var/log/graylog-server/server.log.%i.gz">\n      <PatternLayout pattern="%m%n"/>'
            '\n      <Policies>\n        <SizeBasedTriggeringPolicy size="50MB"/>\n      </Policies>\n      '
            '<DefaultRolloverStrategy max="10" fileIndex="min"/>\n    </RollingFile>\n  </Appenders>\n</Configuration>\n'
        )
        import xml.dom.minidom

        g = logconf.config_of(HostService.objects.get(key="graylog"))["files"][0]["changes"]
        new = logconf.apply_changes(log4j, g)
        self.assertIn('<File name="rolling-file" fileName="/var/log/graylog-server/server.log" append="true">', new)
        self.assertNotIn("Policies", new)
        self.assertNotIn("RollingFile", new)
        xml.dom.minidom.parseString(new)
        self.assertEqual(logconf.apply_changes(new, g), new)  # nothing a second time
        ad = logconf.config_of(HostService.objects.get(key="auditd"))["files"][0]["changes"]
        conf = "max_log_file = 8\nmax_log_file_action = ROTATE\n"
        self.assertEqual(logconf.apply_changes(conf, ad), "max_log_file = 8\nmax_log_file_action = ignore\n")

    def test_a_size_rotates_hourly_and_keeps_by_age(self):
        bind = HostService.objects.get(key="bind")
        rot = logconf.service_rotate(bind, 5)
        for line in ("maxsize 100M", "dateext", "dateformat -%Y%m%d%H", "maxage 5", "rotate 120", "nodelaycompress"):
            self.assertIn(line, rot)
        out = logconf.files([bind], COL, RULES)
        self.assertIn("OnCalendar=hourly", out[logconf.TIMER])
        frr = HostService.objects.get(key="frr")
        self.assertNotIn(logconf.TIMER, logconf.files([frr], COL, RULES))
        script = logconf.install_script(out)
        self.assertIn(f"cp /etc/claugine/logging.new/timer.conf {logconf.TIMER}", script)
        self.assertIn(f"else rm -f {logconf.TIMER}", logconf.install_script(logconf.files([frr], COL, RULES)))
        self.assertIn("timer.conf", script[script.index("restore()") : script.index("installed=0")])

    def test_a_config_change_that_cannot_work_is_refused(self):
        s = HostService(key="x", name="X")
        for config, err in (
            ({"changes": [{"file": "etc/x", "find": "a", "replace": "b"}]}, "full path"),
            ({"changes": [{"file": "/etc/x", "find": "(", "replace": "b"}]}, "/etc/x"),
            ({"changes": [{"file": "/etc/x", "find": "a", "replace": r"\2"}]}, "/etc/x"),
            ({"changes": [{"file": "/etc/x", "find": "a", "replace": "b"}], "then": "reload"}, "give the reload"),
        ):
            s.config = config
            with self.assertRaisesMessage(logconf.LogConfError, err):
                logconf.check_config(s)

    @unittest.skipUnless(shutil.which("python3"), "python3 on this machine")
    def test_the_machines_script_for_real(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "named.options")
        with open(path, "w") as f:
            f.write(NAMED)

        def run(config):
            s = HostService(key="bind", name="BIND", config=config)
            with (
                mock.patch.object(logconf, "LOCK", os.path.join(d, "lock")),
                mock.patch.object(logconf, "PENDING", os.path.join(d, "pending")),
            ):
                script = logconf.config_script([s])
            import sys

            stub = (  # no flock on the Mac: the same lock with fcntl
                ""
                if shutil.which("flock")
                else f"flock() {{ {sys.executable} -c 'import fcntl; fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)'; }}\n"
            )
            p = subprocess.run(["bash", "-c", stub + script], capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            return logconf.parse_config(p.stdout)

        ok = {
            "changes": [
                {"file": path, "find": r"\s+versions\s+\d+\s+size\s+\w+;", "replace": ";"},
                {"file": os.path.join(d, "missing"), "find": "a", "replace": "b"},
            ],
            "check": "grep -q logging {file}",
            "then": "reload",
            "command": f"touch {d}/reloaded",
        }
        files, then = run(ok)
        self.assertEqual([f["state"] for f in files], ["changed", "skipped"])
        self.assertEqual([t["state"] for t in then], ["ok"])
        self.assertTrue(os.path.exists(f"{d}/reloaded") and os.path.exists(path + ".claugine-prev"))
        self.assertNotIn("versions", open(path).read())
        os.remove(f"{d}/reloaded")
        files, then = run(ok)  # nothing a second time; no reload
        self.assertEqual(([f["state"] for f in files], then), (["same", "skipped"], []))
        self.assertFalse(os.path.exists(f"{d}/reloaded"))
        before = open(path).read()
        files, _t = run({"changes": [{"file": path, "find": "logging", "replace": "loggingX"}], "then": "nothing"})
        self.assertEqual(files[0]["state"], "failed")  # it would change the file again: not made
        self.assertEqual(open(path).read(), before)
        files, _t = run(
            {"changes": [{"file": path, "find": "logging", "replace": "log"}], "check": "false", "then": "nothing"}
        )
        self.assertEqual(files[0]["state"], "failed")
        self.assertIn("previous file is back", files[0]["detail"])
        self.assertEqual(open(path).read(), before)
        # commit-279: a check of the file runs on the staged copy - the live file is never replaced when it fails
        files, _t = run(
            {
                "changes": [{"file": path, "find": "logging", "replace": "log"}],
                "check": "false {file}",
                "then": "nothing",
            }
        )
        self.assertIn("the file was not changed", files[0]["detail"])
        self.assertEqual(open(path).read(), before)
        self.assertFalse(os.path.exists(path + ".claugine-new"))
        # commit-280: the mode of the original kept
        os.chmod(path, 0o640)
        files, _t = run({"changes": [{"file": path, "find": "severity", "replace": "sev"}], "then": "nothing"})
        self.assertEqual(files[0]["state"], "changed")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)
        # commit-278: a failed reload stays pending and is tried again by the next run, files unchanged
        flag = os.path.join(d, "ok")
        cfg = {
            "changes": [{"file": path, "find": "queries_log", "replace": "queries_channel"}],
            "then": "reload",
            "command": f"test -e {flag}",
        }
        files, then = run(cfg)
        self.assertEqual((files[0]["state"], then[0]["state"]), ("changed", "failed"))
        self.assertTrue(os.path.exists(os.path.join(d, "pending", "bind")))
        open(flag, "w").close()
        files, then = run(cfg)
        self.assertEqual((files[0]["state"], then[0]["state"]), ("same", "ok"))
        self.assertFalse(os.path.exists(os.path.join(d, "pending", "bind")))
        files, then = run(cfg)
        self.assertEqual(then, [])  # nothing pending, nothing changed: no reload
        # commit-287: a marker left by an interrupted run (file already changed) makes the next run reload
        import hashlib
        import json

        def mark(*texts):
            with open(os.path.join(d, "pending", "bind"), "w") as m:
                json.dump({"files": {path: [hashlib.sha256(t.encode()).hexdigest() for t in texts]}}, m)

        mark(open(path).read())
        files, then = run(cfg)
        self.assertEqual((files[0]["state"], then[0]["state"]), ("same", "ok"))
        # commit-292: a marker that does not say which files it is for is dropped, nothing reloaded
        open(os.path.join(d, "pending", "bind"), "w").close()
        files, then = run(cfg)
        self.assertEqual(then[0]["state"], "skipped")
        self.assertFalse(os.path.exists(os.path.join(d, "pending", "bind")))
        # commit-295: a failed reload, then a refused second edit of the same file: the first reload still runs
        os.remove(flag)
        fail_first = {
            "changes": [{"file": path, "find": "queries_channel", "replace": "queries_chan"}],
            "then": "reload",
            "command": f"test -e {flag}",
        }
        files, then = run(fail_first)
        self.assertEqual((files[0]["state"], then[0]["state"]), ("changed", "failed"))
        second = {
            "changes": [{"file": path, "find": "logging", "replace": "log"}],
            "check": "! grep -q '^log ' " + path,
            "then": "reload",
            "command": f"test -e {flag}",
        }
        files, then = run(second)
        self.assertIn("previous file is back", files[0]["detail"])
        self.assertEqual(then[0]["state"], "failed")  # still pending: the restored first edit awaits its reload
        self.assertTrue(os.path.exists(os.path.join(d, "pending", "bind")))
        open(flag, "w").close()
        files, then = run(fail_first)
        self.assertEqual((files[0]["state"], then[0]["state"]), ("same", "ok"))
        self.assertFalse(os.path.exists(os.path.join(d, "pending", "bind")))
        # commit-288: the kept previous file has the original's mode, never wider
        self.assertEqual(os.stat(path + ".claugine-prev").st_mode & 0o777, 0o640)
        # commit-292: a marker left by a run interrupted after a put-back (the live file holds the old text again)
        old_text = open(path).read()
        new_text = old_text + "# the change that was put back\n"
        with open(os.path.join(d, "pending", "bind"), "w") as m:
            json.dump({"files": {path: hashlib.sha256(new_text.encode()).hexdigest()}}, m)
        restarted = os.path.join(d, "restarted")
        files, then = run(
            {
                "changes": [{"file": path, "find": "queries_channel", "replace": "queries_channel"}],
                "then": "restart",
                "command": f"touch {restarted}",
            }
        )
        self.assertEqual(then, [])  # nothing restarted for a change that was put back
        self.assertFalse(os.path.exists(restarted))
        self.assertFalse(os.path.exists(os.path.join(d, "pending", "bind")))
        files, then = run(  # a put-back in this run takes its file out of the marker at once
            {
                "changes": [{"file": path, "find": "logging", "replace": "log"}],
                "check": "false",
                "then": "restart",
                "command": f"touch {restarted}",
            }
        )
        self.assertIn("previous file is back", files[0]["detail"])
        self.assertEqual(then, [])
        self.assertFalse(os.path.exists(os.path.join(d, "pending", "bind")) or os.path.exists(restarted))


@KEY
class ConfigOnMachines(Base):
    """The config changes and the hourly timer in Check logging / Apply logging and in a deployment."""

    def setUp(self):
        from inventory import hostlogging

        self.hl = hostlogging
        self.h, self.c = world()
        self.sa = self.staff("sa@corp.kz", is_super_admin=True)

    def test_a_name_server_before_and_after(self):
        host = FakeHost("bind\nssh\n", files={"/etc/bind/named.options": NAMED})
        r = self.hl.check(self.h, host)
        states = {f["path"]: f["state"] for f in r["files"]}
        self.assertEqual(states["/etc/bind/named.options"], "config change not made")
        self.assertEqual(states[logconf.TIMER], "added")
        self.assertEqual(r["status"], "differs")
        r = self.hl.apply(self.sa, self.h, host)
        self.assertEqual(r["status"], "compliant", r)
        self.assertEqual(r["config_error"], "")
        self.assertEqual([t["state"] for t in r["config_then"]], ["ok"])
        self.assertNotIn("versions", host.files["/etc/bind/named.options"])
        states = {f["path"]: f["state"] for f in r["files"]}
        self.assertEqual(states["/etc/bind/named.options"], "config made")

    def test_a_missing_config_file_is_skipped_not_a_difference(self):
        host = FakeHost("bind\n")
        r = self.hl.apply(self.sa, self.h, host)
        self.assertEqual(r["status"], "compliant", r)
        self.assertIn({"path": "/etc/bind/named.options", "state": "not on the machine", "service": "bind"}, r["files"])

    def test_the_timer_is_removed_where_no_service_has_a_size(self):
        host = FakeHost("ssh\n", files={logconf.TIMER: "old"})
        r = self.hl.check(self.h, host)
        self.assertIn({"path": logconf.TIMER, "state": "removed"}, r["files"])
        r = self.hl.apply(self.sa, self.h, host)
        self.assertEqual(r["status"], "compliant", r)

    def test_a_failed_config_change_is_reported(self):
        host = FakeHost("bind\n", files={"/etc/bind/named.options": NAMED})

        def failing(script, timeout=0, stdin_extra=""):
            if "CLAUGINE_CONFIG_EOF" in script:
                return "C bind /etc/bind/named.options failed the check refused it, the previous file is back: x\n"
            return host(script, timeout)

        r = self.hl.apply(self.sa, self.h, failing)
        self.assertIn("the check refused it", r["config_error"])
        self.assertEqual(r["status"], "differs")

    def test_278_a_pending_reload_differs(self):
        host = FakeHost("bind\n", files={"/etc/bind/named.options": NAMED.replace(" versions 3 size 10m", "")})
        host.pending = ["bind"]
        r = self.hl.check(self.h, host)
        self.assertIn({"path": f"{logconf.PENDING}/bind", "state": "reload pending", "service": "bind"}, r["files"])
        self.assertEqual(r["status"], "differs")

    def test_282_no_text_of_a_config_file_is_kept(self):
        secret = NAMED.replace("severity info;", 'severity info;\n    secret "s3cr3t-key";', 1)
        host = FakeHost("bind\n", files={"/etc/bind/named.options": secret})
        r = self.hl.check(self.h, host)
        f = next(x for x in r["files"] if x["path"] == "/etc/bind/named.options")
        self.assertEqual(f["state"], "config change not made")
        self.assertEqual(f["changes"][0]["places"], 2)
        self.assertNotIn("diff", f)
        self.h.refresh_from_db()
        self.assertNotIn("s3cr3t", str(self.h.logging))

    def test_282_283_no_config_text_shown_and_the_way_back_checks_the_timer(self):
        Host.objects.filter(pk=self.h.pk).update(
            logging={
                "status": "differs",
                "at": "2026-11-21T12:00:00+05:00",
                "services": ["bind"],
                "rsyslog": "active",
                "units_down": [],
                "files": [
                    {
                        "path": "/etc/bind/named.options",
                        "state": "config change not made",
                        "service": "bind",
                        "diff": "-key s3cr3t",
                    },
                    {"path": "/etc/rsyslog.d/00-common.conf", "state": "differs", "diff": "-OLD-COMMON"},
                ],
            }
        )
        self.client.force_login(self.sa)
        r = self.client.get(reverse("infra_site", args=["zk1"]) + "?tab=hosts")
        self.assertNotContains(r, "s3cr3t")  # commit-282: a stored config-file text is never shown
        self.assertContains(r, "-OLD-COMMON")  # the portal's own files still are
        script = logconf.install_script(logconf.files([HostService.objects.get(key="frr")], COL, RULES))
        back = script[script.index("restore()") : script.index("installed=0")]
        self.assertIn("systemctl daemon-reload FAILED", back)  # commit-283
        self.assertIn('if [ "$cal" = "OnCalendar=$want" ]', back)

    def test_283_the_timer_must_run_hourly_where_wanted(self):
        host = FakeHost("bind\n", files={"/etc/bind/named.options": NAMED})
        self.hl.apply(self.sa, self.h, host)
        host.timer = "inactive"
        r = self.hl.check(self.h, host)
        self.assertIn({"path": "logrotate.timer", "state": "timer inactive"}, r["files"])
        host.timer = "active"
        host.files.pop(logconf.TIMER)  # its schedule daily now
        r = self.hl.check(self.h, host)
        self.assertIn({"path": "logrotate.timer", "state": "timer not hourly"}, r["files"])
        script = logconf.install_script(logconf.files([HostService.objects.get(key="bind")], COL, RULES))
        self.assertIn("systemctl is-active logrotate.timer >/dev/null || {", script)
        self.assertIn("logrotate.timer runs at $cal, not $want", script)
        self.assertIn("logrotate.timer did NOT start again", script[script.index("restore()") :])

    def test_290_a_weekly_timer_is_not_daily(self):
        host = FakeHost("ssh\n")
        self.hl.apply(self.sa, self.h, host)
        host.sched = "{OnCalendar=Mon*-*-*00:00:00;next_elapse=x}"
        r = self.hl.check(self.h, host)
        self.assertIn({"path": "logrotate.timer", "state": "timer not daily"}, r["files"])
        host.sched = "{OnCalendar=*-*-*00:00:00}{OnCalendar=*-*-**:00:00}"  # two schedules: not exactly daily either
        self.assertEqual(self.hl.check(self.h, host)["status"], "differs")
        script = logconf.install_script(logconf.files([HostService.objects.get(key="frr")], COL, RULES))
        self.assertIn(f"else want='{logconf.DAILY}'", script)
        self.assertIn('[ "$cal" = "OnCalendar=$want" ]', script)

    def test_267_293_an_older_check_keeps_the_services_of_a_later_refused_apply(self):
        from datetime import timedelta

        from django.utils import timezone

        t1 = timezone.now()
        t2 = t1 + timedelta(seconds=5)
        self.assertTrue(self.hl.keep_services(self.h, ["graylog"], t2))  # the refused Apply read at t2
        kept = self.hl._store(  # a Check that read at t1 ends now
            self.h, {"status": "differs", "at": t1.isoformat()}, services=["ssh"], services_at=t1
        )
        self.assertTrue(kept)  # its result is kept (no newer one) ...
        self.h.refresh_from_db()
        self.assertEqual(self.h.services, ["graylog"])  # ... but not its older services
        self.assertFalse(self.hl.keep_services(self.h, ["ssh"], t1))  # nor an older refused Apply's
        self.h.refresh_from_db()
        self.assertEqual(self.h.services, ["graylog"])

    def test_289_a_refused_apply_shows_the_new_restarts(self):
        name = "graylog"
        Host.objects.filter(pk=self.h.pk).update(services=["ssh"])
        self.client.force_login(self.sa)
        host = FakeHost("graylog\n")
        with mock.patch.object(self.hl, "ssh_of", return_value=host):
            r = self.client.post(
                reverse("inv_host_logging_apply", args=["zk1", "kvm014"]), {"restarts": ""}, follow=True
            )
            self.assertContains(r, "Apply would restart Graylog")
            page = self.client.get(reverse("infra_site", args=["zk1"]) + "?tab=hosts")
            self.assertContains(page, f'name="restarts" value="{name}"')  # the dialog shown again names it
            r = self.client.post(
                reverse("inv_host_logging_apply", args=["zk1", "kvm014"]), {"restarts": name}, follow=True
            )
        self.assertContains(r, "Logging applied")

    def test_285_apply_refuses_restarts_not_shown(self):
        host = FakeHost("graylog\n")
        with self.assertRaisesMessage(self.hl.LoggingError, "Apply would restart Graylog"):
            self.hl.apply(self.sa, self.h, host, restarts=[])
        self.assertFalse(any("logging.new" in c for c in host.calls))  # nothing installed
        name = "graylog"
        r = self.hl.apply(self.sa, self.h, host, restarts=[name])
        self.assertEqual(r["config_error"], "")
        Host.objects.filter(pk=self.h.pk).update(services=["graylog"])
        self.client.force_login(self.sa)
        page = self.client.get(reverse("infra_site", args=["zk1"]) + "?tab=hosts")
        self.assertContains(page, f'name="restarts" value="{name}"')

    def test_the_deployment_step_makes_them(self):
        host = FakeHost("bind\n", files={"/etc/bind/named.options": NAMED})
        r = types.SimpleNamespace(h=self.h, ssh=host, keep=lambda **k: None)
        out = hostdeploy.s_logging(r)
        self.assertIn("1 config files changed", out)

        def failing(script, timeout=0, stdin_extra=""):
            if "CLAUGINE_CONFIG_EOF" in script:
                return "C bind /etc/bind/named.options failed no\n"
            return host(script, timeout)

        with self.assertRaisesMessage(hostdeploy.DeployError, "named.options: no"):
            hostdeploy.s_logging(types.SimpleNamespace(h=self.h, ssh=failing, keep=lambda **k: None))


@KEY
class ServiceVMLogging(Base):
    """Owner 2026-11-21 11:58 "service VMs now": Check logging / Apply logging and the Logging part for the base-OS
    VMs of the core services, as for hosts."""

    def setUp(self):
        from inventory import hostlogging
        from inventory.models import ConfigTemplate, CoreService, CoreServiceNode

        self.hl = hostlogging
        self.h, self.c = world()
        self.site = self.h.site
        self.sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(self.sa)
        self.ns = CoreService.objects.create(site=self.site, name="ns", stype="ns", kind="vm")
        self.n = CoreServiceNode.objects.create(service=self.ns, name="szk1ns1", address="10.201.101.11", secret="sa")
        tpl = ConfigTemplate.objects.filter(role="rr-vxlan").first() or ConfigTemplate.objects.create(
            role="rr-vxlan", name="rr", os="vyos"
        )
        self.rr = CoreService.objects.create(site=self.site, name="rrs", stype="bgp-rr", kind="vm", template=tpl)
        self.rn = CoreServiceNode.objects.create(service=self.rr, name="szk1rr1", address="10.201.102.241")

    def test_only_base_os_machines(self):
        self.assertTrue(self.n.base_os)
        self.assertFalse(self.rn.base_os)
        self.assertEqual([n.name for n in self.hl.service_vms()], ["szk1ns1"])
        with self.assertRaisesMessage(self.hl.LoggingError, "not a base-OS machine"):
            self.hl.node_ssh(self.rn)

    def test_check_and_apply_a_name_server(self):
        host = FakeHost("bind\nssh\n", files={"/etc/bind/named.options": NAMED})
        r = self.hl.check(self.n, host)
        self.assertEqual(r["status"], "differs")
        self.n.refresh_from_db()
        self.assertEqual(self.n.services, ["bind", "ssh"])
        r = self.hl.apply(self.sa, self.n, host)
        self.assertEqual(r["status"], "compliant", r)
        self.n.refresh_from_db()
        self.assertIsNotNone(self.n.logging_applied_at)
        self.assertEqual(self.n.logging_claim, {})

    def test_apply_is_refused_while_a_redeploy_runs(self):
        with (
            mock.patch.object(self.hl, "deploy_running", return_value=True),
            self.assertRaisesMessage(self.hl.LoggingError, "a redeploy of szk1ns1 is running"),
        ):
            self.hl.apply(self.sa, self.n, FakeHost("ssh\n"))

    def test_281_a_redeploy_waits_for_apply_and_apply_for_a_starting_redeploy(self):
        from django.utils import timezone

        from inventory.models import CoreServiceNode

        CoreServiceNode.objects.filter(pk=self.n.pk).update(
            logging_claim={"token": "x", "at": timezone.now().isoformat()}
        )
        with mock.patch("accounts.tasks.start") as start:
            r = self.client.post(reverse("core_rollback", args=["zk1", self.ns.pk, self.n.pk]), follow=True)
        start.assert_not_called()
        self.assertContains(r, "Apply logging of this VM is running")
        CoreServiceNode.objects.filter(pk=self.n.pk).update(
            logging_claim={"token": "lifecycle-1", "at": timezone.now().isoformat(), "by": "redeploy"}
        )
        with self.assertRaisesMessage(self.hl.LoggingError, "a redeploy of szk1ns1 is starting"):
            self.hl.apply(self.sa, self.n, FakeHost("ssh\n"))
        CoreServiceNode.objects.filter(pk=self.n.pk).update(logging_claim={})

        def started(*a, **k):  # the claim is held while the task is being made
            self.n.refresh_from_db()
            self.assertEqual(self.n.logging_claim.get("by"), "redeploy")

        with mock.patch("accounts.tasks.start", side_effect=started):
            self.client.post(reverse("core_rollback", args=["zk1", self.ns.pk, self.n.pk]))
        self.n.refresh_from_db()
        self.assertEqual(self.n.logging_claim, {})

    def test_289_a_vm_refused_then_confirmed(self):
        from inventory.models import CoreServiceNode

        name = "graylog"
        url = reverse("core_logging_apply", args=["zk1", self.ns.pk, self.n.pk])
        host = FakeHost("graylog\n")
        with mock.patch.object(self.hl, "ssh_of", return_value=host):
            r = self.client.post(url, {"confirm": "szk1ns1", "restarts": ""}, follow=True)
            self.assertContains(r, "Apply would restart Graylog")
            self.assertEqual(CoreServiceNode.objects.get(pk=self.n.pk).services, ["graylog"])
            page = self.client.get(reverse("infra_site", args=["zk1"]) + "?tab=core")
            self.assertContains(page, f'name="restarts" value="{name}"')
            r = self.client.post(url, {"confirm": "szk1ns1", "restarts": name}, follow=True)
        self.assertContains(r, "Logging applied")

    def test_the_daily_check_has_them(self):
        with mock.patch.object(self.hl, "check", return_value={"status": "differs"}):
            ok, detail = self.hl.check_all()
        self.assertTrue(ok)
        self.assertIn("zk1.szk1ns1", detail)
        self.assertNotIn("szk1rr1", detail)

    def test_the_buttons_the_dialog_and_the_details(self):
        from inventory.models import CoreServiceNode

        CoreServiceNode.objects.filter(pk=self.n.pk).update(
            services=["graylog"],
            logging={
                "status": "differs",
                "at": "2026-11-21T12:00:00+05:00",
                "services": ["graylog"],
                "rsyslog": "active",
                "units_down": [],
                "files": [
                    {
                        "path": "/etc/graylog/server/log4j2.xml",
                        "state": "config change not made",
                        "service": "graylog",
                        "diff": "-RollingFile",
                    }
                ],
            },
        )
        r = self.client.get(reverse("infra_site", args=["zk1"]) + "?tab=core")
        url = reverse("core_logging_check", args=["zk1", self.ns.pk, self.n.pk])
        self.assertContains(r, url)
        self.assertNotContains(r, reverse("core_logging_check", args=["zk1", self.rr.pk, self.rn.pk]))
        self.assertContains(r, f'data-open="dlg-cnlog-{self.n.pk}"')
        self.assertContains(r, "Restarted when their config changes:")
        self.assertContains(r, "config change of")
        self.assertNotContains(r, "-RollingFile")  # commit-282: no config-file text

    def test_the_views(self):
        with mock.patch("inventory.hostlogging.check", return_value={"status": "compliant"}) as chk:
            r = self.client.post(reverse("core_logging_check", args=["zk1", self.ns.pk, self.n.pk]), follow=True)
        chk.assert_called_once()
        self.assertContains(r, "Logging is compliant.")
        url = reverse("core_logging_apply", args=["zk1", self.ns.pk, self.n.pk])
        with mock.patch("inventory.hostlogging.apply") as app:
            r = self.client.post(url, {"confirm": "wrong"}, follow=True)
        app.assert_not_called()  # the owner's yes per VM: its name typed
        self.assertContains(r, "Type the VM&#x27;s name to confirm.")
        with mock.patch(
            "inventory.hostlogging.apply",
            return_value={"status": "compliant", "config_error": "", "test_message": "sent"},
        ) as app:
            r = self.client.post(url, {"confirm": "szk1ns1"}, follow=True)
        app.assert_called_once()
        self.assertContains(r, "Logging applied: compliant.")
        self.client.force_login(self.staff("sup@corp.kz", is_support=True))
        with mock.patch("inventory.hostlogging.apply") as app:
            self.client.post(url, {"confirm": "szk1ns1"})
        app.assert_not_called()


class MigrationsMatch(TestCase):
    def test_no_model_change_without_its_migration(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("makemigrations", "inventory", "--check", "--dry-run", stdout=out)  # SystemExit when one is due
