"""Profiles → Logging (owner 2026-11-24 08:54 - 09:36, T33; mockups 13, 17, 18, 19 approved 09:24): the logged services
made by migration 0119 from the machines read on 2026-11-24, a machine's logged services (common always, nvidia only
on a host with GPUs, a management node's front end), the files they make, the page (one list, Queue and Retry above
it, Edit in a dialog), the profiles' picks and claugine-logcap."""

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from django.apps import apps
from django.test import TestCase
from django.urls import reverse

from accounts.tests import Base
from inventory import hostlogging, logconf, logging_views
from inventory.models import CoreService, CoreServiceNode, Host, LoggedService, ServerProfile, ServiceProfile, Site
from tenants.models import PortalSettings

COL = {"host": "10.0.0.18", "port": 15140, "proto": "tcp"}
RULES = {"queue_mb": 100, "retry_seconds": 30}


def names(rows):
    return [x.name for x in rows]


class Seed(TestCase):
    def test_the_rows_read_on_the_machines(self):
        self.assertEqual(
            sorted(LoggedService.objects.values_list("name", flat=True)),
            sorted(
                [
                    "common",
                    "vyos",
                    "auditd",
                    "fail2ban",
                    "bind",
                    "mysql",
                    "opennebula",
                    "prometheus",
                    "one-exporter",
                    "libvirt",
                    "frr",
                    "nvidia",
                    "graylog",
                    "mongodb",
                ]
            ),
        )
        self.assertFalse(LoggedService.objects.filter(name__in=["xrdp", "apache2"]).exists())  # owner 09:31, 09:32
        common, vyos = LoggedService.objects.get(name="common"), LoggedService.objects.get(name="vyos")
        self.assertTrue(common.fixed and vyos.fixed and vyos.is_vyos)
        self.assertEqual(logconf.log_types(common.rsyslog_conf), ["ufw", "auth", "kern", "apt", "syslog"])
        self.assertEqual(vyos.rsyslog_conf, "")
        self.assertEqual(LoggedService.objects.get(name="fail2ban").after_rotation, logconf_flush())
        self.assertEqual(LoggedService.objects.get(name="bind").config["then"], "reload")  # from the service (0104)
        self.assertEqual(LoggedService.objects.get(name="opennebula").log_type, "one")
        self.assertIn("/var/log/libvirt/libvirtd.log", LoggedService.objects.get(name="libvirt").also_rotated)
        for s in LoggedService.objects.all():
            logconf.check_text(s)  # every seeded row is usable as it is
            logconf.check_config(s)

    def test_the_profiles_picks(self):
        kvm = ["common", "libvirt", "one-exporter"]
        fe = ["mysql", "opennebula", "prometheus"]
        want = {  # T36: by the service's key (several services share a type now)
            "admin": ["auditd", "common", "fail2ban"],
            "backup": ["common"],
            "kvm-shared": ["common", "frr", "libvirt", "nvidia", "one-exporter"],
            "kvm-shared-gpu": ["common", "frr", "libvirt", "nvidia", "one-exporter"],
            "kvm-mgmt": sorted(kvm + fe),
            "kvm-simple-fe": sorted(kvm + fe + ["frr"]),
            "kvm-simple": sorted(kvm + ["frr"]),
            "log-collector": ["common", "graylog", "mongodb"],
            "ns-primary": ["bind", "common"],
            "ns-secondary": ["bind", "common"],
            "ns-site": ["bind", "common"],
            "onefe": ["common", "mysql", "opennebula", "prometheus"],
        }
        for p in ServiceProfile.objects.all():
            got = sorted(names(p.logged.all()))
            self.assertEqual(got, ["vyos"] if p.is_vyos else want.get(p.key, ["common"]), p.key)

    def test_logging_configs_leave_the_profiles_and_come_back(self):
        mig = importlib.import_module("inventory.migrations.0119_logged_services")
        p = ServiceProfile.objects.get(key="ns-primary")
        rows = list(p.configs)
        p.configs = rows + [{"path": "/etc/rsyslog.d/20-bind9.conf", "template": "x"}]
        p.save()
        mig.forward(apps, None)  # run again: nothing doubled
        p.refresh_from_db()
        self.assertEqual(p.configs, rows)
        self.assertEqual(p.legacy["logging_configs"][0]["path"], "/etc/rsyslog.d/20-bind9.conf")
        self.assertEqual(LoggedService.objects.filter(name="bind").count(), 1)
        mig.back(apps, None)
        p.refresh_from_db()
        self.assertEqual(p.configs[-1]["path"], "/etc/rsyslog.d/20-bind9.conf")

    @unittest.skipUnless(shutil.which("rsyslogd") or os.path.exists("/usr/sbin/rsyslogd"), "rsyslogd not installed")
    def test_rsyslog_accepts_every_logged_service_at_once(self):
        f = logconf.files(list(LoggedService.objects.all()), COL, RULES)
        with tempfile.TemporaryDirectory() as d:
            for p, text in f.items():
                if p.startswith("/etc/"):
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
            exe = shutil.which("rsyslogd") or "/usr/sbin/rsyslogd"
            out = subprocess.run([exe, "-N1", "-f", conf], capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr + out.stdout)


def logconf_flush():
    return "fail2ban-client flushlogs >/dev/null || true"


class Files(TestCase):
    def test_paths_from_the_text_and_also_rotated(self):
        s = LoggedService(
            name="web",
            log_type="web",
            rsyslog_conf=(
                'input ( type = "imfile" file = "/var/log/web/access.log" tag = "web" )\n'
                '# input ( type = "imfile" file = "/var/log/commented.log" )\n'
                'if ( $msg contains "x(y)" ) then {\n  action ( type = "omfile" file = "/var/log/web.log" )\n'
                '  set $.log_type = "web";\n  call send_logs\n}\n'
            ),
            also_rotated=["/var/log/web/*.err"],
            after_rotation="systemctl kill -s USR1 web",
        )
        self.assertEqual(
            logconf.conf_files(s.rsyslog_conf), [("/var/log/web.log", "omfile"), ("/var/log/web/access.log", "imfile")]
        )
        rot = logconf.service_rotate(s)
        self.assertIn("/var/log/web.log\n{", rot)
        self.assertIn(logconf.REOPEN, rot)  # rsyslog's own file: reopened
        self.assertIn("/var/log/web/access.log\n/var/log/web/*.err\n{", rot)
        self.assertIn("systemctl kill -s USR1 web", rot)
        self.assertIn(f"{logconf.LOGCAP} 200 5 '/var/log/web.log' '/var/log/web/access.log' '/var/log/web/*.err'", rot)

    def test_its_log_type_is_what_its_text_sets(self):
        s = LoggedService(name="web", rsyslog_conf='set $.log_type = "www";\ncall send_logs\n')
        with self.assertRaisesMessage(logconf.LogConfError, 'set $.log_type = "web"'):
            logconf.check_text(s)
        s.log_type = "www"
        logconf.check_text(s)
        s.rsyslog_conf += 'set $.log_type = "other";\n'
        with self.assertRaisesMessage(logconf.LogConfError, "another log_type: other"):
            logconf.check_text(s)
        for bad in (["/etc/x.log"], ["/var/log/a b.log"], ["/var/log/'x'.log"]):
            with self.assertRaises(logconf.LogConfError, msg=bad):
                logconf.check_text(LoggedService(name="x", also_rotated=bad))

    def test_vyos_lines(self):
        v = LoggedService.objects.get(name="vyos")
        lines = logconf.vyos_lines(v, {"host": "10.0.9.18", "port": 15142, "proto": "tcp"})
        self.assertIn("set system syslog remote 10.0.9.18 format octet-counted", lines)
        self.assertIn("set system logs logrotate messages max-size 10", lines)
        self.assertNotIn("octet", logconf.vyos_lines(v, {"host": "x", "port": 514, "proto": "udp"}))

    def test_the_vyos_templates_get_the_lines(self):
        mig = importlib.import_module("inventory.migrations.0120_vyos_logging")
        body = "a\n" + mig.OLD + "b\n"
        from inventory.models import ConfigTemplate

        t = ConfigTemplate.objects.filter(os="vyos").first()
        t.body = body
        t.save()
        mig.forward(apps, None)
        t.refresh_from_db()
        self.assertIn("format octet-counted", t.body)
        mig.forward(apps, None)  # once
        t.refresh_from_db()
        self.assertEqual(t.body.count("logrotate messages rotate"), 1)
        mig.back(apps, None)
        t.refresh_from_db()
        self.assertEqual(t.body, body)


class Machines(TestCase):
    def setUp(self):
        self.site = Site.objects.create(code="dc9")

    def test_a_host_its_gpus_and_its_front_end(self):
        h = Host.objects.create(site=self.site, name="h1")
        self.assertEqual(names(logconf.logged_of(h)), ["common", "frr", "libvirt", "one-exporter"])
        h.profile = ServerProfile.objects.create(code="gpu", gpu_count=8)
        h.save()
        self.assertIn("nvidia", names(logconf.logged_of(h)))  # owner 09:33: only on a host with GPUs
        fe = CoreService.objects.create(site=self.site, name="onefe", stype="onefe", kind="vm")
        CoreServiceNode.objects.create(service=fe, name="mgmt201", address="10.0.0.201", host=h)
        got = names(logconf.logged_of(h))  # owner 09:33: a usual KVM host with the front end added
        for n in ("libvirt", "frr", "opennebula", "mysql", "prometheus"):
            self.assertIn(n, got)
        logconf.files(logconf.logged_of(h), COL, RULES)  # host and front end on one machine: no overlap

    def test_a_vm_and_a_vyos_profile(self):
        ns = CoreService.objects.create(site=self.site, name="ns", stype="ns", kind="vm")
        self.assertEqual(names(logconf.logged_of(CoreServiceNode(service=ns, name="n1"))), ["bind", "common"])
        rr = CoreService.objects.create(
            site=self.site,
            name="rr",
            stype="bgp-rr",
            kind="vm",
            profile=ServiceProfile.objects.filter(os="vyos").first(),
        )
        self.assertEqual(names(logconf.logged_of(CoreServiceNode(service=rr, name="r1"))), ["common"])

    def test_files_not_rotated_are_listed(self):
        listed = [
            "/var/log/syslog",
            "/var/log/syslog.1",
            "/var/log/syslog.2.gz",
            "/var/log/frr/bgpd.log",
            "/var/log/journal/x/system.journal",
            "/var/log/lastlog",
            "/var/log/haproxy.log",
            "/var/log/kern.log-2026112400",
        ]
        patterns = [p for _o, p in logconf.rotation_paths(LoggedService.objects.filter(name__in=["common", "frr"]))]
        self.assertEqual(hostlogging.unrotated(listed, patterns), ["/var/log/haproxy.log"])
        r = hostlogging.compare(
            {}, "R active\nV /var/log/haproxy.log\nL active {OnCalendar=*-*-*00:00:00}\n", [], [], patterns
        )
        self.assertEqual(
            (r["status"], r["unrotated"]), ("compliant", ["/var/log/haproxy.log"])
        )  # listed, not a difference


@unittest.skipUnless(shutil.which("python3") or sys.executable, "python3")
class LogCap(TestCase):
    def test_the_oldest_copies_go_first(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        script = os.path.join(d, "logcap")
        with open(script, "w") as f:
            f.write(logconf.LOGCAP_SCRIPT)
        now = time.time()
        for i in range(1, 5):
            p = os.path.join(d, f"a.log.{i}.gz")
            with open(p, "wb") as f:
                f.write(b"x" * 300 * 1024)
            os.utime(p, (now - i * 86400, now - i * 86400))
        with open(os.path.join(d, "a.log"), "wb") as f:
            f.write(b"x" * 400 * 1024)
        subprocess.run([sys.executable, script, "1", "9", os.path.join(d, "*.log")], check=True, timeout=30)
        self.assertEqual(sorted(os.listdir(d)), ["a.log", "a.log.1.gz", "a.log.2.gz", "logcap"])


class Pages(Base):
    def setUp(self):
        self.sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(self.sa)

    def edit(self, s, **kw):
        data = {f"lg{s.pk}-{k}": ("" if v is None else v) for k, v in logging_views.initial(s).items()}
        data.update({f"lg{s.pk}-{k}": v for k, v in kw.items()})
        return self.client.post(reverse("inv_logged_save", args=[s.pk]), data, follow=True)

    def test_the_page(self):
        r = self.client.get(reverse("infra_logging") + "?all=1")  # T36 B2: the whole list, still reachable
        self.assertContains(r, 'name="log_queue_mb"')
        for text in (
            "14 Logged services",
            "ufw, auth, kern, apt, syslog",
            "libvirt-qemu",
            "/etc/rsyslog.d/40-bind.conf",
            "/etc/rsyslog.d/00-claugine.conf",
            "set system logs logrotate messages max-size 10",
            "dlg-lg-new",
        ):
            self.assertContains(r, text)
        bind = LoggedService.objects.get(name="bind")
        r = self.client.get(reverse("infra_logging") + "?name=bind")  # T36 B2: to its function's page
        self.assertRedirects(
            r, reverse("infra_functions") + f"?sel=fn-{bind.function_id}", fetch_redirect_response=False
        )

    def test_new_edit_delete(self):
        self.client.post(reverse("inv_logged_new"), {"new-name": "haproxy", "new-log_type": ""})
        s = LoggedService.objects.get(name="haproxy")
        conf = PortalSettings.get()  # T36 (mockup m01): the defaults of Profiles → OS's common line
        self.assertEqual((s.kind, s.days, s.size_mb), ("base-os", conf.log_kept_days, conf.log_size_mb))
        conf = (
            'input ( type = "imfile" file = "/var/log/haproxy.log" tag = "haproxy" )\n'
            + 'if ( $syslogtag startswith "haproxy" ) then {\n  set $.log_type = "lb";\n  call send_logs\n  stop\n}\n'
        )
        r = self.edit(s, rsyslog_conf=conf)
        self.assertContains(r, "must set $.log_type = &quot;haproxy&quot;")  # owner 09:32: the text sets its log_type
        r = self.edit(s, name="lb", log_type="lb", rsyslog_conf=conf, days="7", also_rotated="/var/log/haproxy-*.log")
        self.assertContains(r, "Logged service saved")
        s.refresh_from_db()
        self.assertEqual((s.name, s.log_type, s.days, s.also_rotated), ("lb", "lb", 7, ["/var/log/haproxy-*.log"]))
        p = ServiceProfile.objects.get(stype="backup")
        p.logged.add(s)
        r = self.client.post(reverse("inv_logged_delete", args=[s.pk]), follow=True)
        self.assertContains(r, "is used by backup")
        p.logged.remove(s)
        self.client.post(reverse("inv_logged_delete", args=[s.pk]))
        self.assertFalse(LoggedService.objects.filter(pk=s.pk).exists())

    def test_the_predefined_rows(self):
        common, vyos = LoggedService.objects.get(name="common"), LoggedService.objects.get(name="vyos")
        self.edit(common, name="base", log_type="x", size_mb="600")
        common.refresh_from_db()
        self.assertEqual((common.name, common.log_type, common.size_mb), ("common", "", 600))
        self.edit(vyos, size_mb="300", days="9", rsyslog_conf="x")
        vyos.refresh_from_db()
        self.assertEqual((vyos.size_mb, vyos.days, vyos.rsyslog_conf), (300, 5, ""))
        for s in (common, vyos):
            self.client.post(reverse("inv_logged_delete", args=[s.pk]))
            self.assertTrue(LoggedService.objects.filter(pk=s.pk).exists())

    def test_a_rotation_two_services_of_a_profile_share_is_refused(self):
        mysql = LoggedService.objects.get(name="mysql")
        r = self.edit(mysql, also_rotated="/var/log/mysql/*.log\n/var/log/one/oned.log")
        self.assertContains(r, "can rotate one same file")
        mysql.refresh_from_db()
        self.assertEqual(mysql.also_rotated, ["/var/log/mysql/*.log"])

    def test_596_a_saved_path_with_a_comma_survives_an_edit(self):
        s = LoggedService.objects.get(name="mysql")
        LoggedService.objects.filter(pk=s.pk).update(also_rotated=["/var/log/mysql/worker,1.log"])
        s.refresh_from_db()
        self.edit(s, days="9")
        s.refresh_from_db()
        self.assertEqual((s.days, s.also_rotated), (9, ["/var/log/mysql/worker,1.log"]))
        r = self.edit(s, also_rotated="/var/log/mysql/a.log /var/log/mysql/b.log")  # two in one row: refused
        self.assertContains(r, "One per row")
        s.refresh_from_db()
        self.assertEqual(s.also_rotated, ["/var/log/mysql/worker,1.log"])

    def test_the_settings_line(self):
        self.client.post(reverse("inv_logging_settings"), {"log_queue_mb": "200", "log_retry_seconds": "20"})
        conf = PortalSettings.get()
        self.assertEqual((conf.log_queue_mb, conf.log_retry_seconds), (200, 20))

    def test_only_the_super_admin_changes_them(self):
        self.client.force_login(self.staff("rm@corp.kz", is_resource_manager=True))
        r = self.client.get(reverse("infra_logging") + "?all=1")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'name="log_queue_mb"')
        s = LoggedService.objects.get(name="fail2ban")
        self.client.post(reverse("inv_logged_delete", args=[s.pk]))
        self.client.post(reverse("inv_logging_settings"), {"log_queue_mb": "999", "log_retry_seconds": "20"})
        self.assertTrue(LoggedService.objects.filter(pk=s.pk).exists())
        self.assertEqual(PortalSettings.get().log_queue_mb, 100)


class ReviewFixes98a1333(Base):
    """commit-444 .. commit-448 (review of 98a1333; owner 10:22 "fix commit-444, commit-445, commit-446, commit-447,
    commit-448")."""

    def setUp(self):
        self.sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(self.sa)

    def profile_form(self, p, **kw):
        from inventory import services

        data = {k: ("" if v is None else v) for k, v in services.profile_initial(p).items()}
        data.pop("stype")
        data.update(kw)
        return data

    def test_444_a_profile_with_two_rows_rotating_one_file_is_not_saved(self):
        a = LoggedService.objects.create(name="ex-a", also_rotated=["/var/log/example/*.log"])
        b = LoggedService.objects.create(name="ex-b", also_rotated=["/var/log/example/app*.log"])
        p = ServiceProfile.objects.get(stype="backup")
        r = self.client.post(
            reverse("inv_sprofile_edit", args=[p.pk]), self.profile_form(p, logged=[str(a.pk), str(b.pk)]), follow=True
        )
        self.assertContains(r, "can rotate one same file")
        self.assertEqual(names(p.logged.all()), ["common"])

    def test_444_a_host_that_runs_a_core_service_is_checked_with_it(self):
        site = Site.objects.create(code="dc9")
        h = Host.objects.create(site=site, name="mgmt201")
        fe = CoreService.objects.create(site=site, name="onefe", stype="onefe", kind="vm")
        CoreServiceNode.objects.create(service=fe, name="mgmt201", address="10.0.0.201", host=h)
        mysql = LoggedService.objects.get(name="mysql")
        r = self.client.post(
            reverse("inv_logged_save", args=[mysql.pk]),
            {
                f"lg{mysql.pk}-{k}": ("" if v is None else v)
                for k, v in dict(
                    logging_views.initial(mysql), also_rotated="/var/log/mysql/*.log\n/var/log/libvirt/libvirtd.log"
                ).items()
            },
            follow=True,
        )
        self.assertContains(r, "can rotate one same file")  # libvirt's, on the host
        x = LoggedService.objects.create(name="ex-x", also_rotated=["/var/log/one/oned.log"])
        host = ServiceProfile.objects.get(key="kvm-shared")
        picked = [str(y.pk) for y in host.logged.all() if y.name != "common"] + [str(x.pk)]
        r = self.client.post(
            reverse("inv_sprofile_edit", args=[host.pk]), self.profile_form(host, logged=picked), follow=True
        )
        self.assertContains(r, "can rotate one same file")  # opennebula's, on the same host
        self.assertNotIn("ex-x", names(host.logged.all()))

    def test_445_a_marker_follows_the_row_not_its_name(self):
        bind = LoggedService.objects.get(name="bind")
        c = logconf.config_of(bind)
        self.assertEqual((c["marker"], c["adopt"]), (f"logged.{bind.pk}", ["bind", f"logged-{bind.pk}"]))
        data = {f"lg{bind.pk}-{k}": ("" if v is None else v) for k, v in logging_views.initial(bind).items()}
        data[f"lg{bind.pk}-name"] = "named"
        data[f"lg{bind.pk}-rsyslog_conf"] = bind.rsyslog_conf
        data[f"lg{bind.pk}-log_type"] = "bind"
        self.client.post(reverse("inv_logged_save", args=[bind.pk]), data)
        bind.refresh_from_db()
        self.assertEqual(bind.name, "named")
        self.assertEqual(logconf.config_of(bind)["adopt"], ["named", "bind", f"logged-{bind.pk}"])
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "named.options")
        with open(path, "w") as f:
            f.write('logging { channel a { file "x" versions 3 size 10m; }; };\n')
        pending = os.path.join(d, "pending")
        os.makedirs(pending)
        import hashlib
        import json

        bind.config = dict(
            bind.config,
            changes=[{"file": path, "find": bind.config["changes"][0]["find"], "replace": ";"}],
            check="",
            then="reload",
            command=f"touch {d}/reloaded",
        )
        new = logconf.apply_changes(open(path).read(), logconf.config_of(bind)["files"][0]["changes"])
        with open(path, "w") as f:  # changed by an earlier Apply whose reload failed; its marker under the old name
            f.write(new)
        with open(os.path.join(pending, "bind"), "w") as f:
            json.dump({"files": {path: [hashlib.sha256(new.encode()).hexdigest()]}}, f)
        from unittest import mock

        with (
            mock.patch.object(logconf, "LOCK", os.path.join(d, "lock")),
            mock.patch.object(logconf, "PENDING", pending),
        ):
            script = logconf.config_script([bind])
        stub = (
            ""
            if shutil.which("flock")
            else (f"flock() {{ {sys.executable} -c 'import fcntl; fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)'; }}\n")
        )
        p = subprocess.run(["bash", "-c", stub + script], capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        _files, then = logconf.parse_config(p.stdout)
        self.assertEqual([t["state"] for t in then], ["ok"], p.stdout)  # the reload ran, though the file was the same
        self.assertTrue(os.path.exists(f"{d}/reloaded"))
        self.assertEqual(os.listdir(pending), [])
        r = hostlogging.compare({}, f"R active\nP logged.{bind.pk}\n", [], [], None, {f"logged.{bind.pk}": "named"})
        self.assertEqual(r["files"][-1]["service"], "named")

    def test_446_a_star_never_crosses_a_slash(self):
        self.assertFalse(logconf.path_match("/var/log/x/nested/a.log", "/var/log/x/*.log"))
        self.assertTrue(logconf.path_match("/var/log/x/a.log", "/var/log/x/*.log"))
        self.assertFalse(logconf.path_match("/var/log/x/.a.log", "/var/log/x/*.log"))
        self.assertEqual(
            hostlogging.unrotated(["/var/log/x/nested/a.log"], ["/var/log/x/*.log"]), ["/var/log/x/nested/a.log"]
        )
        s = LoggedService(
            name="x",
            log_type="x",
            also_rotated=["/var/log/x/*.log"],
            rsyslog_conf='input ( type = "imfile" file = "/var/log/x/nested/app.log" tag = "x" )\nset $.log_type = "x";\n',
        )
        self.assertIn("/var/log/x/nested/app.log", logconf.rotated(s)[1])  # kept: *.log does not take it
        self.assertTrue(logconf.covers("/var/log/one/*.log", "/var/log/one/[0-9]*.log"))
        self.assertFalse(logconf.covers("/var/log/one/*.log", "/var/log/one/*.error"))
        self.assertFalse(logconf.covers("/var/log/a/*.log", "/var/log/a/x[.]log"))

    def test_447_copies_older_than_days_go_even_under_the_size(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        script = os.path.join(d, "logcap")
        with open(script, "w") as f:
            f.write(logconf.LOGCAP_SCRIPT)
        now = time.time()
        for name, age in (("q.log-2026112103.gz", 3), ("q.log.1.gz", 1)):
            p = os.path.join(d, name)
            with open(p, "w") as f:
                f.write("x")
            os.utime(p, (now - age * 86400, now - age * 86400))
        with open(os.path.join(d, "q.log"), "w") as f:
            f.write("x")
        subprocess.run([sys.executable, script, "500", "1", os.path.join(d, "*.log")], check=True, timeout=30)
        self.assertEqual(sorted(os.listdir(d)), ["logcap", "q.log", "q.log.1.gz"])

    def test_448_the_system_files_are_under_commons_budget(self):
        common = LoggedService.objects.get(name="common")
        f = logconf.files([common], COL, RULES)
        pk = f["/etc/logrotate.d/90-packages.conf"]
        self.assertIn("/var/log/apt/*.log\n{\n  daily\n  rotate 5\n  maxage 5", pk)
        self.assertIn(f"{logconf.LOGCAP} 500 5 ", pk)
        self.assertIn("'/var/log/apt/*.log'", f["/etc/logrotate.d/90-common.conf"])  # one budget for both
        self.assertNotIn("/var/log/apt/history.log\n", f["/etc/logrotate.d/90-common.conf"])  # one rule per file
        self.assertIn("create 0660 root utmp", f["/etc/logrotate.d/90-system.conf"])


class ReviewFixes7fafd53(Base):
    """commit-444, commit-447, commit-449, commit-450 (review of 7fafd53; owner 10:48)."""

    def setUp(self):
        self.client.force_login(self.staff("sa@corp.kz", is_super_admin=True))

    def test_444_two_core_services_on_one_host_are_checked_together(self):
        site = Site.objects.create(code="dc9")
        h = Host.objects.create(site=site, name="mgmt201")
        for stype in ("onefe", "backup"):
            cs = CoreService.objects.create(site=site, name=stype, stype=stype, kind="vm")
            CoreServiceNode.objects.create(service=cs, name=f"{stype}1", address="10.0.0.201", host=h)
        backup = ServiceProfile.objects.get(stype="backup")
        x = LoggedService.objects.create(name="ex-x", also_rotated=["/var/log/one/oned.log"])  # meets opennebula's only
        with self.assertRaisesMessage(logconf.LogConfError, "can rotate one same file"):
            logging_views.check_sets(backup, [LoggedService.objects.get(name="common"), x])
        logging_views.check_sets(ServiceProfile.objects.get(key="ns-primary"), [x])  # not on that host: fine
        backup.logged.add(x)  # already picked: an edit of the row is checked on that host too
        x.also_rotated = ["/var/log/one/oned.log", "/var/log/ex.log"]
        with self.assertRaises(logging_views.LoggingError):
            logging_views.validate(x)

    def test_447_days_and_size_run_daily_without_a_rotation(self):
        f = logconf.files([LoggedService.objects.get(name="common"), LoggedService.objects.get(name="frr")], COL, RULES)
        self.assertIn('[200, 5, ["/var/log/frr/*.log"]]', f[logconf.LOGCAP_CONF])
        self.assertIn(f"ExecStartPost=-{logconf.LOGCAP} --all {logconf.LOGCAP_CONF}", f[logconf.LOGCAP_DROPIN])
        script = logconf.install_script(f)
        self.assertIn(f"cp /etc/claugine/logging.new/logcap.conf {logconf.LOGCAP_CONF}", script)
        self.assertLess(
            script.index("logcap-dropin.conf " + logconf.LOGCAP_DROPIN), script.index("systemctl daemon-reload\n")
        )
        self.assertIn(f"rm -f {logconf.LOGCAP_CONF} {logconf.LOGCAP_DROPIN}", script[script.index("restore()") :])
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        cap = os.path.join(d, "logcap")
        with open(cap, "w") as fh:
            fh.write(logconf.LOGCAP_SCRIPT)
        old = os.path.join(d, "gone.log-2026112103.gz")  # its live log is absent: logrotate rotates nothing
        with open(old, "w") as fh:
            fh.write("x")
        t = time.time() - 9 * 86400
        os.utime(old, (t, t))
        conf = os.path.join(d, "logcap.conf")
        with open(conf, "w") as fh:
            fh.write("# x\n" + json.dumps([500, 5, [os.path.join(d, "gone.log")]]) + "\n")
        subprocess.run([sys.executable, cap, "--all", conf], check=True, timeout=30)
        self.assertFalse(os.path.exists(old))

    def test_449_a_marker_is_taken_only_with_its_files(self):
        r = self.client.post(reverse("inv_logged_new"), {"new-name": "logged.3", "new-log_type": ""})
        self.assertFalse(LoggedService.objects.filter(name="logged.3").exists())  # a dot: never a marker's name
        bind = LoggedService.objects.get(name="bind")
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path, other = os.path.join(d, "named.options"), os.path.join(d, "other.conf")
        for p_ in (path, other):
            with open(p_, "w") as fh:
                fh.write("x\n")
        pending = os.path.join(d, "pending")
        os.makedirs(pending)
        import json

        with open(os.path.join(pending, "bind"), "w") as fh:  # a marker of the old name, for another file
            json.dump({"files": {other: ["0" * 64]}}, fh)
        bind.config = {"changes": [{"file": path, "find": "y", "replace": "z"}], "then": "reload", "command": "true"}
        from unittest import mock

        with (
            mock.patch.object(logconf, "LOCK", os.path.join(d, "lock")),
            mock.patch.object(logconf, "PENDING", pending),
        ):
            script = logconf.config_script([bind])
        stub = (
            ""
            if shutil.which("flock")
            else (f"flock() {{ {sys.executable} -c 'import fcntl; fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)'; }}\n")
        )
        p = subprocess.run(["bash", "-c", stub + script], capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(os.listdir(pending), ["bind"])  # not its files: left as it is
        self.assertTrue(r.status_code in (200, 302))

    def test_450_excluded_files_exactly_directories_by_prefix(self):
        listed = [
            "/var/log/lastlog",
            "/var/log/lastlog-extra.log",
            "/var/log/journal/a/system.journal",
            "/var/log/faillog",
        ]
        self.assertEqual(hostlogging.unrotated(listed, []), ["/var/log/lastlog-extra.log"])


class ReviewFixes2c564f6(Base):
    """commit-447, commit-449, commit-451, commit-452, commit-453 (review of 2c564f6; owner 12:33)."""

    def run_config(self, svc, pending, d):
        from unittest import mock

        with (
            mock.patch.object(logconf, "LOCK", os.path.join(d, "lock")),
            mock.patch.object(logconf, "PENDING", pending),
        ):
            script = logconf.config_script([svc])
        stub = (
            ""
            if shutil.which("flock")
            else (f"flock() {{ {sys.executable} -c 'import fcntl; fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)'; }}\n")
        )
        p = subprocess.run(["bash", "-c", stub + script], capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return logconf.parse_config(p.stdout)

    def test_449_an_old_name_another_service_has_or_had_is_not_taken(self):
        bind = LoggedService.objects.get(name="bind")
        other = LoggedService.objects.create(name="dns2", config={"former": ["bind"]})
        self.assertNotIn("bind", logconf.config_of(bind)["adopt"])
        other.config = {"changes": [{"file": bind.config["changes"][0]["file"], "find": "a", "replace": "b"}]}
        other.save()
        self.assertEqual(logconf.config_of(bind)["adopt"], [])  # one file, two services: nothing taken over
        LoggedService.objects.create(name=f"logged-{bind.pk}")  # a row with the id form as its name
        other.delete()
        self.assertEqual(logconf.config_of(bind)["adopt"], ["bind"])

    def test_451_a_marker_of_7fafd53_is_taken_and_its_reload_made(self):
        import hashlib

        bind = LoggedService.objects.get(name="bind")
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "named.options")
        bind.config = {
            "changes": [{"file": path, "find": "versions 3", "replace": ""}],
            "then": "reload",
            "command": f"touch {d}/reloaded",
        }
        with open(path, "w") as fh:  # already changed by that Apply, its reload failed
            fh.write("ok\n")
        pending = os.path.join(d, "pending")
        os.makedirs(pending)
        with open(os.path.join(pending, f"logged-{bind.pk}"), "w") as fh:
            json.dump({"files": {path: [hashlib.sha256(b"ok\n").hexdigest()]}}, fh)
        _files, then = self.run_config(bind, pending, d)
        self.assertEqual([t["state"] for t in then], ["ok"])
        self.assertTrue(os.path.exists(f"{d}/reloaded"))
        self.assertEqual(os.listdir(pending), [])

    def test_452_a_pattern_is_kept_exactly(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        cap = os.path.join(d, "logcap")
        with open(cap, "w") as fh:
            fh.write(logconf.LOGCAP_SCRIPT)
        name = os.path.join(d, "app\u00a0worker.log")
        old = name + ".3.gz"
        with open(old, "w") as fh:
            fh.write("x")
        t = time.time() - 9 * 86400
        os.utime(old, (t, t))
        conf = os.path.join(d, "logcap.conf")
        self.assertIn("# [Size MB, Days, [files]]", logconf.files([], COL, RULES)[logconf.LOGCAP_CONF])
        with open(conf, "w", encoding="utf-8") as fh:
            fh.write(json.dumps([500, 5, [name]], ensure_ascii=False) + "\n")
        subprocess.run([sys.executable, cap, "--all", conf], check=True, timeout=30)
        self.assertFalse(os.path.exists(old))

    def test_453_common_with_no_stanza_of_its_own_still_has_its_budget(self):
        common = LoggedService.objects.get(name="common")
        common.rsyslog_conf, common.also_rotated = "", []
        self.assertIsNone(logconf.service_rotate(common))
        f = logconf.files([common], COL, RULES)
        lines = [json.loads(x) for x in f[logconf.LOGCAP_CONF].splitlines() if x.startswith("[")]
        self.assertEqual(len(lines), 1)
        self.assertIn("/var/log/apt/*.log", lines[0][2])


class ReviewFixes77b3f53(TestCase):
    """commit-447, commit-452 (review of 77b3f53; owner 12:40)."""

    def test_452_a_line_separator_in_a_name_stays_in_its_record(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        name = os.path.join(d, "app\u2028x.log")
        s = LoggedService(
            name="w",
            log_type="w",
            also_rotated=[],
            rsyslog_conf=(f'input ( type = "imfile" file = "{name}" tag = "w" )\nset $.log_type = "w";\n'),
        )
        conf_text = logconf.files([s], COL, RULES)[logconf.LOGCAP_CONF]
        self.assertNotIn("\u2028", conf_text)  # escaped
        conf = os.path.join(d, "logcap.conf")
        with open(conf, "w", encoding="utf-8") as fh:
            fh.write(conf_text)
        old = name + ".2.gz"
        with open(old, "w") as fh:
            fh.write("x")
        t = time.time() - 9 * 86400
        os.utime(old, (t, t))
        cap = os.path.join(d, "logcap")
        with open(cap, "w") as fh:
            fh.write(logconf.LOGCAP_SCRIPT)
        subprocess.run([sys.executable, cap, "--all", conf], check=True, timeout=30)
        self.assertFalse(os.path.exists(old))
