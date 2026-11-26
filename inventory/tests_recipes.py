"""Profiles → Services (owner 2026-11-21 12:29-13:44, T34): a service is the whole recipe (install), a service profile
is what a machine of a kind is made of; the seeded parts and profiles (from the at1 VMs), the first-boot script they
make (bash -n), the edit forms and the pages. The four empty base-OS templates are gone (Q15)."""

import shutil
import subprocess
import tempfile
import unittest

from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from accounts.tests import Base
from inventory import recipes, services
from inventory.models import ConfigTemplate, CoreService, HostService, ServiceProfile


def by_key():
    return {s.key: s for s in HostService.objects.all()}


def tpl(p):
    """The templates of a profile, as the deployment takes them (T34 S2)."""
    from inventory.base_deploy import templates_of

    return templates_of(p)


def steps(profile):
    """{name: step} of a profile's installation template (T34 S2: what its services installed)."""
    p = ServiceProfile.objects.get(key=profile)
    return {s["name"]: s for s in recipes.parse_steps(tpl(p)[p.install_template].body)}


def pre_t36():
    """T36: the data as it was before 0122 (by its own reverse) - the tests of the earlier migrations run on it."""
    import importlib

    from django.apps import apps as _apps

    importlib.import_module("inventory.migrations.0122_t36_data").back(_apps, None)


def _pk(key):
    """A service profile's id by its Name (owner 2026-11-23 U4: the pages find a profile by its id)."""
    return ServiceProfile.objects.get(key=key).pk


class Seeded(TestCase):
    def test_the_profiles_and_their_parts(self):
        got = {p.key: (p.stype, p.services) for p in ServiceProfile.objects.all()}
        # T36 (rules v2 C2): the functions renamed - bind → ns, graylog → graylog-server, opennebula → one-fe, jump → admin
        self.assertEqual(got["ns-primary"], ("ns", ["base-os", "ns"]))
        self.assertEqual(got["log-collector"], ("logs", ["base-os", "graylog-server"]))
        self.assertEqual(got["onefe"], ("onefe", ["base-os", "one-fe"]))
        self.assertEqual(got["admin"], ("admin", ["base-os", "admin", "fail2ban"]))
        self.assertEqual(got["backup"], ("backup", ["base-os", "backup-ds"]))
        self.assertEqual(got["kvm-shared"][0], "host")
        self.assertIn("libvirt", got["kvm-shared"][1])
        for prof, k in (
            ("ns-primary", "ns"),
            ("log-collector", "graylog-server"),
            ("onefe", "one-fe"),
            ("admin", "admin"),
            ("admin", "fail2ban"),
            ("backup", "backup-ds"),
            ("ns-primary", "base-os"),
        ):
            self.assertIn(k, steps(prof), k)  # T34 S2: each service's install is a step of its profile's template
        self.assertTrue(HostService.objects.get(key="ns").install)  # commit-382: kept, no longer read

    def test_no_secret_in_a_recipe(self):
        for t in ConfigTemplate.objects.filter(kind__in=("steps", "config")):
            self.assertNotRegex(t.body, r"(password_secret|root_password_sha2)\s*[:=]\s*[0-9a-f]{16}", t.role)

    def test_the_base_os_templates_are_gone(self):
        roles = set(ConfigTemplate.objects.values_list("role", flat=True))
        self.assertFalse(
            roles & {"core-services-ns", "core-services-graylog", "core-services-admin", "core-services-backup"}
        )

    def test_jump_host_rdp_is_ubuntus_own(self):
        j = steps("admin")["admin"]
        self.assertIn("gnome-remote-desktop", j["packages"])
        self.assertNotIn("xrdp", j["packages"] + j.get("tools", []))
        self.assertNotIn("xorriso", j["packages"] + j.get("tools", []))
        self.assertEqual(j["tools"], ["nfs-common", "opennebula-tools"])

    def test_backup_never_formats(self):
        text = recipes.dump_steps([steps("backup")["backup-ds"]])
        self.assertNotIn("mkfs", text)
        self.assertNotIn("pvcreate", text)
        self.assertIn("vgs {{ storage.vg }}", text)


class Scripts(TestCase):
    def test_each_profile_makes_a_script(self):
        # T36: a VyOS service (project gateways too) has no script; claugine's installation comes with B5
        for p in (
            ServiceProfile.objects.exclude(stype=ServiceProfile.HOST).exclude(os="vyos").exclude(install_template="")
        ):
            out = recipes.render_script(p, tpl(p), recipes.sample_values(p))
            self.assertTrue(out.startswith("#!/bin/bash\n"), p.key)
            self.assertIn('step "done"', out)
            self.assertNotIn("{{", out, p.key)
            for k in p.services:
                self.assertIn(f'step "{k}: ', out)

    @unittest.skipUnless(shutil.which("bash"), "bash")
    def test_bash_accepts_them(self):
        for p in ServiceProfile.objects.exclude(stype=ServiceProfile.HOST).exclude(stype__in=services.VYOS_STYPES):
            with tempfile.NamedTemporaryFile("w", suffix=".sh") as f:
                f.write(recipes.render_script(p, tpl(p), recipes.sample_values(p)))
                f.flush()
                r = subprocess.run(["bash", "-n", f.name], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"{p.key}: {r.stderr}")

    def test_secrets_are_masked_in_a_preview(self):
        p = ServiceProfile.objects.get(key="log-collector")
        out = recipes.render_script(p, tpl(p), recipes.sample_values())
        self.assertIn(recipes.MASK, out)

    def test_order_files_then_commands_then_mounts_then_units(self):
        p = ServiceProfile.objects.get(key="backup")
        out = recipes.render_script(p, tpl(p), recipes.sample_values())
        self.assertLess(out.index("iscsiadm -m node"), out.index("mount /var/lib/one/datastores"))
        self.assertLess(out.index("mount /var/lib/one/datastores"), out.index('step "backup-ds: units"'))

    @unittest.skipUnless(shutil.which("python3"), "python3")
    def test_set_lines_in_a_file(self):
        import grp
        import os
        import pwd

        me = f"{pwd.getpwuid(os.getuid()).pw_name}:{grp.getgrgid(os.getgid()).gr_name}"
        svc = HostService(
            key="x",
            install={
                "files": [
                    {
                        "path": "/tmp/x.conf",
                        "owner": me,
                        "mode": "0600",
                        "set": {"a": "1", "b": "{{ node.hostname }}"},
                        "then": "nothing",
                    }
                ]
            },
        )
        line = [x for x in recipes.script_of(svc, recipes.sample_values()) if x.startswith("python3 - ")][0]
        with tempfile.TemporaryDirectory() as d:
            path = f"{d}/x.conf"
            open(path, "w").write("# a = 0\nkeep = 1\nb = old\nb = older\n")
            cmd = line.replace("/tmp/x.conf", path)
            subprocess.run(["bash", "-c", cmd], check=True)
            self.assertEqual(open(path).read(), "a = 1\nkeep = 1\nb = sat1ns2\n")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)  # commit-303: the mode set before it is moved in
            self.assertEqual(sorted(os.listdir(d)), ["x.conf"])

    def test_a_missing_template_is_an_error(self):
        p = ServiceProfile(key="z", install_template="nope")
        with self.assertRaisesMessage(recipes.RecipeError, "no template nope"):
            recipes.render_script(p, tpl(p), recipes.sample_values())


class Parse(TestCase):
    def test_errors(self):
        for text in (
            "- a",
            "colours: [red]",
            "packages: ['bad name']",
            "files: [{path: rel}]",
            "files: [{path: /x, text: a, set: {a: 1}}]",
            "files: [{path: /x, text: '{% if %}'}]",
            "files: [{path: /x, text: a, then: explode}]",
            "repos: [{name: r, line: deb x, key: 'http://insecure'}]",
            "mounts: [{what: a}]",
            "packages: [",
            "repos: 42",  # commit-300
            "files: 7",
            "users: [{name: a, uid: x}]",
            "users: [{name: a, uid: 70000}]",
            "commands: [{a: 1}]",
            "files: [{path: /x, set: {a: [1]}}]",
            "files: [{path: /x, text: '{% include \"missing.html\" %}'}]",  # commit-299
            "files: [{path: /x, text: '{% load static %}'}]",
            "files: [{path: /x, text: '{{ node.name|nofilter }}'}]",
        ):
            with self.assertRaises(recipes.RecipeError, msg=text):
                recipes.parse(text)

    def test_empty(self):
        self.assertEqual(recipes.parse(""), {})
        self.assertEqual(recipes.dump({}), "")


class Pages(Base):
    def setUp(self):
        self.sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(self.sa)

    def test_profiles_on_the_services_tab(self):
        r = self.client.get(reverse("infra_services"))
        self.assertContains(r, 'data-key="sprofiles"')
        self.assertNotContains(r, 'data-key="services"')  # T34 S2d: the services' list left the page
        self.assertContains(r, "Name server")
        self.assertContains(r, f'data-id="sp-{_pk("admin")}"')
        self.assertContains(r, "Preview")
        self.assertContains(r, "<h1>Profiles</h1>")

    def test_no_generated_logging_files_among_the_configs(self):
        """Owner 2026-11-24 09:13 (mockup 18): Configs = the service's config templates only; the logged services in
        their own line, each a link to Profiles → Logging."""
        from inventory import logconf
        from inventory.models import LoggedService
        from tenants.models import PortalSettings

        found = list(LoggedService.objects.filter(name__in=["common", "bind"]))
        got = dict(logconf.generated(found, logconf.rules_of(PortalSettings.get())))
        self.assertEqual(got["/etc/rsyslog.d/00-claugine.conf"], "")
        self.assertEqual(got["/etc/rsyslog.d/90-common.conf"], "common")
        self.assertEqual(got["/etc/rsyslog.d/40-bind.conf"], "bind")
        self.assertEqual(got["/etc/logrotate.d/40-bind.conf"], "bind")
        r = self.client.get(reverse("infra_services"))
        self.assertNotContains(r, "generated from the log sources")
        self.assertContains(r, "Logged services")
        bind = LoggedService.objects.get(name="bind")
        self.assertContains(r, f"{reverse('infra_logging')}?sel=lg-{bind.pk}")

    def test_install_is_not_a_profiles_own_lines(self):
        """T34 S2: what is installed is the profile's templates."""
        self.assertNotIn("own_lines", services.ProfileForm.base_fields)
        r = self.client.get(reverse("infra_services"))
        self.assertContains(r, "admin-install")
        self.assertContains(r, "fail2ban-10-claugine-local")

    def test_add_edit_delete_profile(self):
        from inventory.models import LoggedService

        f2b = LoggedService.objects.get(name="fail2ban")
        form = {
            "key": "relay",
            "name": "Mail relay",
            "os": "base-os",
            "stype": "backup",
            "vcpu": 1,
            "ram_gb": 2,  # T36 commit-479: never below base-os's minimum
            "disk_gb": 5,
            "checks": "unit postfix\nport 25",
            "notes": "",
        }
        self.client.post(reverse("inv_sprofile_new"), form)  # T36: a type may have several services
        p = ServiceProfile.objects.get(key="relay")
        self.assertEqual(p.checks, [{"kind": "unit", "value": "postfix"}, {"kind": "port", "value": "25"}])
        self.assertEqual([x.name for x in p.logged.all()], ["common"])  # owner 09:13: common always
        self.client.post(reverse("inv_sprofile_edit", args=[_pk("relay")]), dict(form, logged=[str(f2b.pk)]))
        self.assertEqual(sorted(x.name for x in p.logged.all()), ["common", "fail2ban"])
        self.client.post(reverse("inv_sprofile_edit", args=[_pk("relay")]), dict(form, logged=["99999"]))
        self.assertEqual(sorted(x.name for x in p.logged.all()), ["common", "fail2ban"])  # not a choice: refused
        self.client.post(reverse("inv_sprofile_delete", args=[_pk("relay")]))
        self.assertFalse(ServiceProfile.objects.filter(key="relay").exists())

    def test_a_profile_in_use_is_not_deleted(self):
        self.client.post(reverse("inv_sprofile_delete", args=[_pk("kvm-shared")]))
        self.assertTrue(ServiceProfile.objects.filter(key="kvm-shared").exists())
        self.assertFalse(CoreService.objects.filter(stype="ns").exists())
        self.client.post(reverse("inv_sprofile_delete", args=[_pk("ns-primary")]))
        self.assertFalse(ServiceProfile.objects.filter(key="ns-primary").exists())

    def test_others_cannot_edit(self):
        self.client.force_login(self.staff("rm@corp.kz"))
        self.client.post(reverse("inv_sprofile_delete", args=[_pk("ns-primary")]))
        self.assertTrue(ServiceProfile.objects.filter(key="ns-primary").exists())


class ReviewFixes(Base):
    """commit-298 .. commit-303 (review of 7beb5c5)."""

    def setUp(self):
        self.sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(self.sa)

    def test_298_a_profiles_type_is_fixed(self):
        form = services.profile_initial(ServiceProfile.objects.get(key="kvm-shared"))
        form = {k: ("" if v is None else v) for k, v in form.items()}
        ServiceProfile.objects.filter(key="backup").delete()
        form["stype"] = "backup"
        self.client.post(reverse("inv_sprofile_edit", args=[_pk("kvm-shared")]), form)
        self.assertEqual(ServiceProfile.objects.get(key="kvm-shared").stype, "host")
        form.pop("stype")  # the edit form does not post it (disabled): the stored one is kept
        form["name"] = "KVM hosts"
        self.client.post(reverse("inv_sprofile_edit", args=[_pk("kvm-shared")]), form)
        p = ServiceProfile.objects.get(key="kvm-shared")
        self.assertEqual((p.stype, p.name), ("host", "KVM hosts"))

    def test_299_a_bad_template_never_takes_the_page_down(self):
        ConfigTemplate.objects.filter(role="fail2ban-10-claugine-local").update(
            body="{% include 'missing.html' %}"
        )  # as if it got in before the check
        r = self.client.get(reverse("infra_services"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "is not allowed in a recipe")

    def test_302_a_secret_with_an_apostrophe_is_one_word(self):
        v = recipes.sample_values()
        v["secret"] = {"rdp": "it's <&> $x"}
        jump = recipes._Part("admin", steps("admin")["admin"])
        line = [x for x in recipes.script_of(jump, v) if "set-credentials" in x][0]
        r = subprocess.run(
            ["bash", "-c", line.replace("grdctl --system rdp set-credentials portal", "printf '[%s]'")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.stdout, "[it's <&> $x]")

    def test_301_the_primary_writes_its_zones_and_loads_them(self):
        p = ServiceProfile.objects.get(key="ns-primary")
        v = recipes.sample_values(p)
        v["dns"]["role"] = "primary"
        out = recipes.render_script(p, tpl(p), v)
        self.assertIn("has no records in the portal", out)  # no zone text yet: the script stops
        for z in v["dns"]["zones"]:
            z["text"] = "$TTL 300\n@ IN SOA ns1. hostmaster. 1 3600 600 1209600 300\n"
        out = recipes.render_script(p, tpl(p), v)
        self.assertNotIn("has no records", out)
        self.assertIn("/var/cache/bind/zones/portal.internal.zone", out)
        self.assertLess(out.index("zones/portal.internal.zone\n"), out.index("named-checkconf -z"))
        self.assertLess(out.index("named-checkconf -z"), out.index('step "ns: units"'))
        v["dns"]["role"] = "secondary"
        self.assertNotIn("/dev/stdin", recipes.render_script(p, tpl(p), v))

    def test_303_text_files_are_private_until_moved_in(self):
        p = ServiceProfile.objects.get(key="ns-primary")
        out = recipes.render_script(p, tpl(p), recipes.sample_values())
        i = out.index("readlink -f -- /etc/sudoers.d/90-portal")
        self.assertIn("umask 077", out[i:].splitlines()[1])
        self.assertIn('CLAUGINE_EOF\nchown root:root "$tmp"\nchmod 0440 "$tmp"; mv -f "$tmp" "$dst"', out[i:])

    def test_values_are_not_html_escaped(self):
        v = recipes.sample_values()
        v["node"]["hostname"] = "a&b<c>"
        self.assertEqual(recipes.render("{{ node.hostname }}", v), "a&b<c>")


def _gnu():
    r = subprocess.run(["chgrp", "--version"], capture_output=True, text=True)
    return r.returncode == 0 and "GNU" in r.stdout


class SecondFixes(TestCase):
    """commit-300, commit-304 .. commit-306 (review of b3c3fe9), commit-307 (review of 8d03504)."""

    def run_script(self, install):
        svc = HostService(key="x", install=recipes.parse(install))
        lines = ["set -e", "step() { :; }"] + recipes.script_of(svc, recipes.sample_values())
        return subprocess.run(["bash", "-c", "\n".join(lines)], capture_output=True, text=True)

    def test_300_keys_that_are_not_names(self):
        for text in (
            "{1: a, x: b}",
            "files: [{path: /x, text: a, 1: b}]",
            "files: [{path: /x, set: {1: a}}]",
            "repos: [{name: r, line: deb x, 2: y}]",
            "users: [{name: a, 3: b}]",
            "mounts: [{4: a}]",
        ):
            with self.assertRaises(recipes.RecipeError, msg=text):
                recipes.parse(text)

    def test_306_empty_mount_fields_take_the_defaults(self):
        self.assertEqual(
            recipes.parse("mounts: [{what: 'h:/x', where: /m, type: , options: }]")["mounts"][0],
            {"what": "h:/x", "where": "/m", "type": "nfs4", "options": "defaults,_netdev"},
        )

    def test_304_an_owner_without_a_group_keeps_the_files_group(self):
        import os
        import pwd

        me = pwd.getpwuid(os.getuid()).pw_name
        with tempfile.TemporaryDirectory() as d:
            path = f"{d}/a.conf"
            open(path, "w").write("a = 0\n")
            gid = os.stat(path).st_gid
            r = self.run_script(f"files: [{{path: {path}, owner: {me}, mode: '0600', set: {{a: '1'}}}}]")
            self.assertEqual(r.returncode, 0, r.stderr)  # no group named like the user is needed
            self.assertEqual((open(path).read(), os.stat(path).st_gid), ("a = 1\n", gid))
            if _gnu():
                r = self.run_script(f"files: [{{path: {path}, owner: {me}, mode: '0600', text: new}}]")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual((open(path).read(), os.stat(path).st_gid), ("new\n", gid))

    def test_305_through_a_link_to_its_target(self):
        import os
        import pwd

        me = pwd.getpwuid(os.getuid()).pw_name
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(f"{d}/real")
            os.makedirs(f"{d}/dir.conf")
            open(f"{d}/real/c.conf", "w").write("a = 0\n")
            os.symlink(f"{d}/real/c.conf", f"{d}/c.conf")
            r = self.run_script(f"files: [{{path: {d}/c.conf, owner: {me}, mode: '0640', set: {{a: '1'}}}}]")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(os.path.islink(f"{d}/c.conf"))
            self.assertEqual(open(f"{d}/real/c.conf").read(), "a = 1\n")
            self.assertEqual(sorted(os.listdir(f"{d}/real")), ["c.conf"])
            r = self.run_script(f"files: [{{path: {d}/dir.conf, owner: {me}, set: {{a: b}}}}]")
            self.assertNotEqual(r.returncode, 0)
            if _gnu():
                r = self.run_script(f"files: [{{path: {d}/c.conf, owner: {me}, mode: '0600', text: new}}]")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertTrue(os.path.islink(f"{d}/c.conf"))
                self.assertEqual(open(f"{d}/real/c.conf").read(), "new\n")
                r = self.run_script(f"files: [{{path: {d}/dir.conf, owner: {me}, text: x}}]")
                self.assertIn("is not a regular file", r.stderr)

    def test_307_a_target_name_ending_in_a_newline(self):
        import os
        import pwd

        if not _gnu():
            self.skipTest("GNU coreutils")
        me = pwd.getpwuid(os.getuid()).pw_name
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(f"{d}/real")
            open(f"{d}/real/c.conf\n", "w").write("old\n")
            open(f"{d}/real/c.conf", "w").write("other\n")
            os.symlink(f"{d}/real/c.conf\n", f"{d}/c.conf")
            r = self.run_script(f"files: [{{path: {d}/c.conf, owner: {me}, mode: '0600', text: new}}]")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(open(f"{d}/real/c.conf\n").read(), "new\n")
            self.assertEqual(open(f"{d}/real/c.conf").read(), "other\n")


class FromTemplates(TestCase):
    """T34 S2 (owner 2026-11-22 16:59 / 17:08 / 20:00): a profile is built from its templates - the installation
    (steps or script) and a config template for every whole file; migration 0114 made them from the services."""

    def profile(self, install="", configs=(), kind="steps"):
        if install is not None:
            ConfigTemplate.objects.create(role="t-install", kind=kind, os="ubuntu", name="i", body=install)
        return ServiceProfile(
            key="t", stype="t", install_template="t-install" if install is not None else "", configs=list(configs)
        )

    def config(self, key="t-conf", body="a = {{ node.hostname }}\n", kind="config"):
        ConfigTemplate.objects.create(role=key, kind=kind, os="ubuntu", name=key, body=body)

    def test_the_migration_made_the_templates(self):
        pre_t36()  # T36: an earlier migration, on the data before 0122
        for p in ServiceProfile.objects.exclude(stype=ServiceProfile.HOST).exclude(stype__in=services.VYOS_STYPES):
            self.assertEqual(p.install_template, f"{p.key}-install")
            self.assertEqual(ConfigTemplate.objects.get(role=p.install_template).kind, "steps")
            self.assertIn("/etc/sudoers.d/90-portal", [c["path"] for c in p.configs])
            self.assertEqual(p.own_lines, "")
        ns = ServiceProfile.objects.get(key="ns")
        self.assertEqual(
            [c["path"] for c in ns.configs],
            [
                "/etc/sudoers.d/90-portal",
                "/etc/bind/named.options",
                "/etc/bind/named.conf.zones",
                "/etc/bind/named.conf",
            ],
        )
        self.assertEqual(ns.nics, [{"network": "core-cloud"}])
        # 0114 seeded iscsi; 0115 (Q22 A) made it the instance's storage network, optional
        self.assertEqual(ServiceProfile.objects.get(key="backup").nics[1], {"network": "storage", "optional": True})
        # one template per service file, shared by every profile with that service
        users = [p.key for p in ServiceProfile.objects.all() if "base-os-90-portal" in p.template_keys()]
        self.assertEqual(sorted(users), ["admin", "backup", "logs", "ns", "onefe"])
        self.assertEqual(ConfigTemplate.objects.get(role="base-os-90-portal").kind, "config")
        # the services' install is kept (commit-382), no longer read: the templates are what is built
        self.assertTrue(HostService.objects.get(key="bind").sources)

    def test_the_steps_are_the_services_in_order(self):
        p = ServiceProfile.objects.get(key="admin")
        steps = recipes.parse_steps(tpl(p)[p.install_template].body)
        self.assertEqual([s["name"] for s in steps], ["base-os", "admin", "fail2ban"])
        self.assertEqual(steps[1]["configs"], ["/etc/ssh/sshd_config.d/10-claugine.conf"])
        self.assertEqual(steps[1]["tools"], ["nfs-common", "opennebula-tools"])
        self.assertEqual(recipes.parse_steps(recipes.dump_steps(steps)), steps)
        out = recipes.render_script(p, tpl(p), recipes.sample_values(p))
        self.assertLess(
            out.index('step "admin: packages"'), out.index('step "admin: /etc/ssh/sshd_config.d/10-claugine.conf"')
        )
        self.assertLess(
            out.index('step "admin: /etc/ssh/sshd_config.d/10-claugine.conf"'), out.index('step "fail2ban: ')
        )

    def test_steps_errors(self):
        for text, msg in (
            ("packages: [a]", "a list"),
            ("- packages: [a]", "a name"),
            ("- name: 'Bad \"name'", "a name"),
            ("- name: a\n  colours: [red]", "unknown colours"),
            ("- name: a\n  configs: [rel]", "configs are the paths"),
            ("- name: a\n  files: [{path: /etc/x, text: y}]", "make it a config of the profile"),
            ("- name: a\n  packages: ['bad name']", "is not a name"),
        ):
            with self.assertRaisesMessage(recipes.RecipeError, msg):
                recipes.parse_steps(text)

    def test_a_config_at_its_step_after_the_set_lines(self):
        self.config()
        p = self.profile(
            "- name: one\n  packages: [x]\n  configs: [/etc/t.conf]\n"
            "  files: [{path: /etc/other, set: {k: v}, then: nothing}]\n- name: two\n  commands: [echo two]\n",
            [{"path": "/etc/t.conf", "template": "t-conf", "mode": "0600", "then": "restart t"}],
        )
        out = recipes.render_script(p, tpl(p), recipes.sample_values())
        self.assertLess(out.index('step "one: /etc/other"'), out.index('step "one: /etc/t.conf"'))
        self.assertLess(out.index('step "one: /etc/t.conf"'), out.index('step "two: commands"'))
        self.assertIn("a = sat1ns2", out)
        self.assertIn('chmod 0600 "$tmp"', out)
        self.assertLess(out.index("systemctl restart t"), out.index('step "two: commands"'))

    def test_a_config_no_step_names_comes_after_the_installation(self):
        self.config()
        p = self.profile("- name: one\n  commands: [echo one]\n", [{"path": "/etc/t.conf", "template": "t-conf"}])
        out = recipes.render_script(p, tpl(p), recipes.sample_values())
        self.assertLess(out.index('step "one: commands"'), out.index('step "configs: /etc/t.conf"'))
        self.assertLess(out.index('step "configs: /etc/t.conf"'), out.index('step "done"'))

    def test_an_installation_script(self):
        self.config()
        p = self.profile(
            "apt-get install -y {{ node.hostname }}\n", [{"path": "/etc/t.conf", "template": "t-conf"}], kind="script"
        )
        out = recipes.render_script(p, tpl(p), recipes.sample_values())
        self.assertIn('step "installation"\napt-get install -y sat1ns2\n', out)
        self.assertLess(out.index("apt-get install"), out.index('step "configs: /etc/t.conf"'))

    def test_wrong_kinds_and_paths(self):
        self.config(kind="steps")
        p = self.profile("- name: one\n", [{"path": "/etc/t.conf", "template": "t-conf"}])
        with self.assertRaisesMessage(recipes.RecipeError, "needs a template of the kind config"):
            recipes.render_script(p, tpl(p), recipes.sample_values())
        ConfigTemplate.objects.filter(role="t-conf").update(kind="config")
        p.configs = [{"path": "/etc/t.conf", "template": "t-conf"}] * 2
        with self.assertRaisesMessage(recipes.RecipeError, "/etc/t.conf is written twice"):
            recipes.render_script(p, tpl(p), recipes.sample_values())
        p.configs = [{"path": "/etc/t.conf", "template": "t-conf"}]
        ConfigTemplate.objects.filter(role="t-install").update(body="- name: one\n  configs: [/etc/nope]\n")
        with self.assertRaisesMessage(recipes.RecipeError, "/etc/nope is not a config of the profile"):
            recipes.render_script(p, tpl(p), recipes.sample_values())
        ConfigTemplate.objects.filter(role="t-install").update(kind="config")
        with self.assertRaisesMessage(recipes.RecipeError, "is not an installation template"):
            recipes.render_script(p, tpl(p), recipes.sample_values())

    def test_references_come_from_the_templates(self):
        self.config(body="{{ secret.conf_only }} {{ storage.iqn }}")
        p = self.profile(
            "- name: one\n  users: [{name: u, keys: [platform]}]\n  commands: ['{{ secret.inst }}']\n",
            [{"path": "/etc/t.conf", "template": "t-conf"}],
        )
        used, keys = recipes.references(p, tpl(p))
        self.assertEqual(used["secret"], {"conf_only", "inst"})
        self.assertEqual(used["storage"], {"iqn"})
        self.assertEqual(keys, {"platform"})

    def test_the_bind_files_are_the_ns_profiles_configs(self):
        p = ServiceProfile.objects.get(key="ns-primary")
        c, body = recipes.config_file(p, tpl(p), "/etc/bind/named.conf")
        self.assertEqual(c["template"], "bind-named-conf")
        self.assertIn('view "internal"', body)
        self.assertIsNone(recipes.config_file(p, tpl(p), "/etc/bind/none"))


class Commit380to382(TestCase):
    """commit-380/381: no file written twice or written whole and patched; commit-382: 0113 is reversible and keeps
    the services' install."""

    def setUp(self):
        pre_t36()

    def mig(self):
        import importlib

        return importlib.import_module("inventory.migrations.0114_profiles_from_templates")

    def test_the_install_is_kept(self):
        self.assertTrue(HostService.objects.get(key="bind").install)

    def test_backwards_then_forwards(self):
        from django.apps import apps

        before = {p.key: (p.install_template, p.configs, p.nics) for p in ServiceProfile.objects.all()}
        bodies = dict(ConfigTemplate.objects.filter(kind__in=("steps", "config")).values_list("role", "body"))
        from importlib import import_module

        m15 = import_module("inventory.migrations.0115_profile_nic_rows")  # after 0114: undone first, done last
        m15.backwards(apps, None)
        self.mig().backwards(apps, None)
        self.assertFalse(ConfigTemplate.objects.filter(kind__in=("steps", "config")).exists())
        self.assertTrue(
            all(
                not p.install_template and not p.configs
                for p in ServiceProfile.objects.exclude(stype__in=services.VYOS_STYPES)  # 0116's: their VyOS template
            )
        )
        self.mig().forwards(apps, None)
        m15.forwards(apps, None)
        self.assertEqual({p.key: (p.install_template, p.configs, p.nics) for p in ServiceProfile.objects.all()}, before)
        self.assertEqual(
            dict(ConfigTemplate.objects.filter(kind__in=("steps", "config")).values_list("role", "body")), bodies
        )

    def test_own_lines_come_back(self):
        from django.apps import apps

        self.mig().backwards(apps, None)
        ServiceProfile.objects.filter(key="ns").update(own_lines="echo one\necho two")
        self.mig().forwards(apps, None)
        self.assertEqual(ServiceProfile.objects.get(key="ns").own_lines, "")
        self.mig().backwards(apps, None)
        self.assertEqual(ServiceProfile.objects.get(key="ns").own_lines, "echo one\necho two")

    def test_a_file_written_twice_or_patched_stops_it_before_any_change(self):
        from django.apps import apps
        from django.core.exceptions import ImproperlyConfigured

        self.mig().backwards(apps, None)
        s = HostService.objects.get(key="fail2ban")
        s.install["files"].append(dict(s.install["files"][0], text="other"))
        s.save()
        b = HostService.objects.get(key="bind")
        b.install["files"].append({"path": "/etc/bind/named.conf", "set": {"a": "1"}, "then": "nothing"})
        b.save()
        with self.assertRaisesMessage(ImproperlyConfigured, "admin: /etc/fail2ban/jail.d/10-claugine.local"):
            self.mig().forwards(apps, None)
        try:
            self.mig().forwards(apps, None)
        except ImproperlyConfigured as exc:
            self.assertIn("ns: /etc/bind/named.conf", str(exc))
        self.assertFalse(ConfigTemplate.objects.filter(kind__in=("steps", "config")).exists())

    def test_set_lines_on_a_config_are_refused(self):
        ConfigTemplate.objects.create(role="t-conf", kind="config", os="ubuntu", name="c", body="a\n")
        ConfigTemplate.objects.create(
            role="t-install",
            kind="steps",
            os="ubuntu",
            name="i",
            body="- name: one\n  files: [{path: /etc/t.conf, set: {k: v}, then: nothing}]\n",
        )
        p = ServiceProfile(
            key="t", install_template="t-install", configs=[{"path": "/etc/t.conf", "template": "t-conf"}]
        )
        with self.assertRaisesMessage(recipes.RecipeError, "is a config of the profile - change its template"):
            recipes.render_script(p, tpl(p), recipes.sample_values())


class Commit381And386(TestCase):
    """commit-381: one file, one spelling; commit-386: 0113's reversal reads only its own record."""

    def setUp(self):
        pre_t36()

    def mig(self):
        import importlib

        return importlib.import_module("inventory.migrations.0114_profiles_from_templates")

    def test_paths_are_written_plainly(self):
        for path in ("/etc//x.conf", "/etc/./x.conf", "/etc/a/../x.conf", "/etc/x/"):
            self.assertFalse(recipes.plain_path(path), path)
            with self.assertRaisesMessage(recipes.RecipeError, "written plainly"):
                recipes.parse_data({"files": [{"path": path, "text": "x"}]})
            with self.assertRaisesMessage(recipes.RecipeError, "written plainly"):
                recipes.parse_data({"mounts": [{"what": "a:/b", "where": path}]})
            with self.assertRaisesMessage(recipes.RecipeError, "configs are the paths"):
                recipes.parse_steps(f"- name: a\n  configs: ['{path}']\n")
        self.assertTrue(recipes.plain_path("/etc/bind/named.conf.zones"))

    def test_another_spelling_is_the_same_file_for_the_migration(self):
        from django.apps import apps
        from django.core.exceptions import ImproperlyConfigured

        self.assertEqual(self.mig()._norm("/etc//bind/./named.conf/"), "/etc/bind/named.conf")
        self.assertIsNone(self.mig()._norm("/etc/../x"))
        self.mig().backwards(apps, None)
        b = HostService.objects.get(key="bind")
        b.install["files"].append({"path": "/etc//bind/named.conf", "set": {"a": "1"}, "then": "nothing"})
        b.install["mounts"] = [{"what": "a:/b", "where": "/var/../x"}]
        b.save()
        with self.assertRaises(ImproperlyConfigured) as e:
            self.mig().forwards(apps, None)
        self.assertIn("ns: /etc/bind/named.conf", str(e.exception))
        self.assertIn("ns: /var/../x (a .. part)", str(e.exception))
        self.assertFalse(ConfigTemplate.objects.filter(kind__in=("steps", "config")).exists())

    def test_a_reversal_after_the_templates_were_edited(self):
        from django.apps import apps

        self.mig().backwards(apps, None)
        ServiceProfile.objects.filter(key="ns").update(own_lines="echo one", nics=[{"network": "x"}])
        self.mig().forwards(apps, None)
        ns = ServiceProfile.objects.get(key="ns")
        self.assertEqual(ns.legacy["own_lines"], "echo one")
        made = list(ConfigTemplate.objects.filter(kind__in=("steps", "config")).values_list("pk", flat=True))
        ConfigTemplate.objects.filter(pk__in=made).update(description="edited", body="- name: x\n")
        self.mig().backwards(apps, None)
        ns = ServiceProfile.objects.get(key="ns")
        self.assertEqual(
            (ns.own_lines, ns.nics, ns.install_template, ns.configs, ns.legacy),
            ("echo one", [{"network": "x"}], "", [], {}),
        )
        self.assertFalse(ConfigTemplate.objects.filter(pk__in=made).exists())
        self.assertTrue(ConfigTemplate.objects.filter(role="egw").exists())  # nothing else deleted


class Commit387And388(TestCase):
    """commit-387: the record fields in 0113, the conversion alone in 0114 (a refused run is retried as is);
    commit-388: the templates 0114 made are found by their origin, also those of a profile deleted since."""

    def setUp(self):
        pre_t36()

    def mig(self):
        import importlib

        return importlib.import_module("inventory.migrations.0114_profiles_from_templates")

    def test_the_schema_and_the_conversion_are_apart(self):
        import importlib

        schema = importlib.import_module("inventory.migrations.0113_profile_records").Migration.operations
        data = self.mig().Migration.operations
        self.assertEqual([type(o).__name__ for o in schema], ["SeparateDatabaseAndState"])  # commit-389/390
        self.assertEqual([type(o).__name__ for o in schema[0].state_operations], ["AddField", "AddField"])
        self.assertEqual([type(o).__name__ for o in schema[0].database_operations], ["RunPython"])
        self.assertEqual([type(o).__name__ for o in data], ["RunPython"])
        self.assertTrue(data[0].atomic)

    def test_a_refused_conversion_runs_again_once_corrected(self):
        from django.apps import apps
        from django.core.exceptions import ImproperlyConfigured

        self.mig().backwards(apps, None)
        b = HostService.objects.get(key="bind")
        good = b.install
        b.install = dict(good, files=good["files"] + [dict(good["files"][0])])
        b.save()
        with self.assertRaises(ImproperlyConfigured):
            self.mig().forwards(apps, None)
        self.assertFalse(ConfigTemplate.objects.filter(origin="0114").exists())
        b.install = good
        b.save()
        self.mig().forwards(apps, None)
        self.assertEqual(ServiceProfile.objects.get(key="ns").install_template, "ns-install")

    def test_a_reversal_after_a_converted_profile_was_deleted(self):
        from django.apps import apps

        made = set(ConfigTemplate.objects.filter(origin="0114").values_list("role", flat=True))
        self.assertIn("fail2ban-10-claugine-local", made)  # only the admin profile has fail2ban
        ServiceProfile.objects.filter(key="admin").delete()
        self.mig().backwards(apps, None)
        self.assertFalse(ConfigTemplate.objects.filter(role__in=made).exists())
        self.assertTrue(ConfigTemplate.objects.filter(role="egw").exists())


def _columns_back():
    """The service profile table's columns as the current model has them (after a rebuild from an earlier state)."""
    from django.db import connection

    # T36: ALTER TABLE ADD COLUMN, not a rebuild - SQLite's rebuild copies every field of the model, and a column the
    # old table lacks is read as the string of its name ("roles"), which a JSON column refuses (0116 vms, 0118 os, 0121)
    with connection.cursor() as cur:
        have = {c.name for c in connection.introspection.get_table_description(cur, ServiceProfile._meta.db_table)}
    for f in [f for f in ServiceProfile._meta.local_fields if f.column not in have]:
        with connection.schema_editor() as ed:
            definition, params = ed.column_sql(ServiceProfile, f, include_default=True)
            ed.execute(
                f"ALTER TABLE {ed.quote_name(ServiceProfile._meta.db_table)} ADD COLUMN {ed.quote_name(f.column)} "
                + definition,
                params,
            )
    with connection.cursor() as cur:
        cons = connection.introspection.get_constraints(cur, ServiceProfile._meta.db_table)
    if any(c["unique"] and c["columns"] == ["stype"] for c in cons.values()):  # 0116: stype no longer unique
        new = ServiceProfile._meta.get_field("stype")
        old = new.clone()
        old.set_attributes_from_name("stype")
        old._unique = True
        old.model = ServiceProfile
        with connection.schema_editor() as ed:
            ed.alter_field(ServiceProfile, old, new)


class Commit389And390(TransactionTestCase):
    """commit-389/390: 0113 adds each column only when the table lacks it, and drops only those present."""

    serialized_rollback = True

    def setUp(self):  # the tables are rebuilt from 0112/0113's state, where a type had one profile (unique stype):
        pre_t36()  # T36: without 0122's services (several per type)
        ServiceProfile.objects.filter(stype__in=services.VYOS_STYPES).delete()  # without 0116's VyOS profiles

    def tearDown(self):
        _columns_back()  # a rebuild from 0112/0113's state left out the columns added after them (0116: vms)

    def cols(self, table):
        from django.db import connection

        with connection.cursor() as cur:
            return {c.name for c in connection.introspection.get_table_description(cur, table)}

    def test_resumes_with_one_column_already_there(self):
        import importlib

        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        mig = importlib.import_module("inventory.migrations.0113_profile_records")
        loader = MigrationExecutor(connection).loader
        after = loader.project_state(("inventory", "0113_profile_records")).apps
        before = loader.project_state(("inventory", "0112_service_templates")).apps
        tpl = after.get_model("inventory", "ConfigTemplate")
        with connection.schema_editor() as ed:  # as if stopped after legacy was added (390), or an earlier 0113 (389)
            ed.remove_field(tpl, tpl._meta.get_field("origin"))
        self.assertIn("legacy", self.cols("inventory_serviceprofile"))
        self.assertNotIn("origin", self.cols("inventory_configtemplate"))
        with connection.schema_editor() as ed:
            mig.add_missing(before, ed)
        self.assertIn("origin", self.cols("inventory_configtemplate"))
        with connection.schema_editor() as ed:
            mig.add_missing(before, ed)  # nothing left to add: no error
        with connection.schema_editor() as ed:
            mig.drop_present(before, ed)
        self.assertNotIn("legacy", self.cols("inventory_serviceprofile"))
        with connection.schema_editor() as ed:
            mig.drop_present(before, ed)  # nothing left to drop: no error
            mig.add_missing(before, ed)
        self.assertIn("legacy", self.cols("inventory_serviceprofile"))

    def test_a_conversion_by_the_earlier_form_is_marked(self):
        from django.apps import apps

        conv = __import__("importlib").import_module("inventory.migrations.0114_profiles_from_templates")
        ids = list(ConfigTemplate.objects.filter(origin="0114").values_list("pk", flat=True))
        ConfigTemplate.objects.filter(pk__in=ids).update(origin="")  # as the earlier form left them
        conv.forwards(apps, None)
        self.assertEqual(ConfigTemplate.objects.filter(pk__in=ids, origin="0114").count(), len(ids))


class Commit391(TestCase):
    """commit-391: a profile converted by the first forms of 0113 (no legacy record) stops 0114 before any change."""

    def setUp(self):
        pre_t36()

    def test_refused_with_how_to_recover(self):
        import importlib

        from django.apps import apps
        from django.core.exceptions import ImproperlyConfigured

        conv = importlib.import_module("inventory.migrations.0114_profiles_from_templates")
        ServiceProfile.objects.filter(key="ns").update(legacy={})
        ConfigTemplate.objects.filter(origin="0114").update(origin="")
        with self.assertRaises(ImproperlyConfigured) as e:
            conv.forwards(apps, None)
        msg = str(e.exception)
        self.assertIn("the profiles ns were converted by an earlier", msg)
        self.assertIn("Converted by 568e5d6: its reverse undoes nothing - restore the database backup", msg)  # 393
        self.assertIn("Converted by bfcdfd3: with that revision run 'manage.py migrate inventory 0112'", msg)
        self.assertFalse(ConfigTemplate.objects.filter(origin="0114").exists())  # nothing changed


class Commit392(TransactionTestCase):
    """commit-392: 0113 reversed and applied again where SQLite rebuilds the table to drop a column."""

    serialized_rollback = True

    def setUp(self):  # the tables are rebuilt from 0112/0113's state, where a type had one profile (unique stype):
        pre_t36()  # T36: without 0122's services (several per type)
        ServiceProfile.objects.filter(stype__in=services.VYOS_STYPES).delete()  # without 0116's VyOS profiles

    def tearDown(self):
        _columns_back()  # a rebuild from 0112/0113's state left out the columns added after them (0116: vms)

    def test_the_rebuild_path(self):
        import importlib
        from unittest import mock

        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        mig = importlib.import_module("inventory.migrations.0113_profile_records")
        loader = MigrationExecutor(connection).loader
        for label in ("0112_service_templates", "0113_profile_records"):
            with mock.patch.object(connection.features, "can_alter_table_drop_column", False):
                with connection.schema_editor() as ed:
                    mig.drop_present(loader.project_state(("inventory", label)).apps, ed)
            with connection.cursor() as cur:
                names = {
                    c.name for c in connection.introspection.get_table_description(cur, "inventory_serviceprofile")
                }
            self.assertNotIn("legacy", names, label)
            with connection.schema_editor() as ed:
                mig.add_missing(loader.project_state(("inventory", "0112_service_templates")).apps, ed)


class S2dPages(Base):
    """T34 S2d (ui-14, ui-16): Profiles → Services = the service profiles; Edit is a page; a profile's installation,
    configs and networks are edited there, its services' log sources from its Logging card."""

    def setUp(self):
        self.sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(self.sa)

    def form(self, key, **kw):
        data = {
            k: ("" if v is None else v)
            for k, v in services.profile_initial(ServiceProfile.objects.get(key=key)).items()
        }
        data.pop("stype")  # disabled on the edit page: not posted (its Name is: U5)
        data.update(kw)
        return data

    def test_the_list_and_details(self):
        r = self.client.get(reverse("infra_services"))
        for text in (
            "Type of service",
            "Installation",
            "Networks",
            "Configs",
            "Logged services",
            "Instances",
            "core-cloud",
        ):
            self.assertContains(r, text)
        self.assertContains(r, reverse("inv_sprofile_edit", args=[_pk("ns-primary")]))
        self.assertContains(r, reverse("inv_sprofile_new"))
        html = r.content.decode()
        d = html[html.index(f'data-for="sp-{_pk("ns-primary")}"') :]
        d = d[: d.index("</div>\n  </div>") if "</div>\n  </div>" in d else len(d)]
        self.assertLess(d.index(">Logged services<"), d.index(">Instances<"))  # R11: instances last

    def test_a_profile_with_instances_has_no_delete(self):
        from inventory.models import Site

        site = Site.objects.create(code="dc1")
        CoreService.objects.create(site=site, stype="ns", name="ns")
        r = self.client.get(reverse("infra_services"))
        self.assertNotContains(r, reverse("inv_sprofile_delete", args=[_pk("ns-primary")]))
        self.assertContains(r, reverse("inv_sprofile_delete", args=[_pk("backup")]))

    def test_the_edit_page(self):
        r = self.client.get(reverse("inv_sprofile_edit", args=[_pk("ns-primary")]))
        self.assertContains(r, "<h1>Edit service profile</h1>")
        html = r.content.decode()
        self.assertLess(html.index(">Save<"), html.index('name="name"'))  # R8: Save and Cancel on top
        self.assertContains(r, 'name="configs"')
        self.assertContains(r, 'name="nics"')
        self.assertContains(r, 'name="logged"')  # owner 09:21: picked like the countries in Settings
        self.assertContains(r, "data-duallist")
        self.assertNotContains(r, 'id="dlg-sv-')  # no log sources edited here (Profiles → Logging)
        r = self.client.get(reverse("inv_sprofile_new"))
        self.assertContains(r, "<h1>New service profile</h1>")
        self.assertContains(r, 'name="key"')

    def test_others_cannot_open_it(self):
        self.client.force_login(self.staff("rm@corp.kz"))
        r = self.client.get(reverse("inv_sprofile_edit", args=[_pk("ns-primary")]))
        self.assertNotEqual(r.status_code, 200)

    def test_save_installation_configs_networks(self):
        p = ServiceProfile.objects.get(key="ns-primary")
        before = (p.install_template, p.configs)
        r = self.client.post(
            reverse("inv_sprofile_edit", args=[_pk("ns-primary")]), self.form("ns-primary", nics="core-cloud\nnfs")
        )
        self.assertRedirects(
            r, reverse("infra_services") + f"?sel=sp-{_pk('ns-primary')}", fetch_redirect_response=False
        )
        p.refresh_from_db()
        self.assertEqual(p.nics, [{"network": "core-cloud"}, {"network": "nfs"}])
        self.assertEqual((p.install_template, p.configs), before)  # unchanged rows read back as they were

    def test_refused(self):
        p = ServiceProfile.objects.get(key="ns-primary")
        before = (p.install_template, list(p.configs), list(p.nics))
        cfg = ConfigTemplate.objects.filter(kind="config").first()
        first = services.configs_text(p).splitlines()[0]
        for bad in (
            {"nics": "core-kvm"},  # not a network of a service VM
            {"install_template": cfg.role},  # a config is no installation
            {"configs": first + "\n" + first},  # a file twice
            {"configs": "/etc//x.conf ¦ " + cfg.role},  # not written plainly
            {"configs": "/etc/x.conf ¦ " + p.install_template},  # an installation is no config
            {"configs": "/etc/x.conf ¦ nope"},  # no such template
            {"configs": "/etc/x.conf ¦ " + cfg.role + " ¦ ¦ ¦ ¦ kill named"},  # then: nothing, reload or restart
        ):
            r = self.client.post(reverse("inv_sprofile_edit", args=[_pk("ns-primary")]), self.form("ns-primary", **bad))
            self.assertEqual(r.status_code, 200, bad)  # the page again, with the error
            p.refresh_from_db()
            self.assertEqual((p.install_template, p.configs, p.nics), before, bad)

    def test_the_logged_services_of_a_vyos_profile(self):
        """Owner 09:13: "vyos based service always use only vyos/vyos"."""
        from inventory.models import LoggedService

        rr = ServiceProfile.objects.filter(os="vyos").first()
        data = {k: ("" if v is None else v) for k, v in services.profile_initial(rr).items()}
        data.pop("stype")
        data["logged"] = [str(LoggedService.objects.get(name="bind").pk)]
        self.client.post(reverse("inv_sprofile_edit", args=[rr.pk]), data)
        self.assertEqual([x.name for x in rr.logged.all()], ["vyos"])


class Commit394to397(Base):
    """commit-394 .. commit-397 (review of 9087a90)."""

    def setUp(self):
        self.client.force_login(self.staff("sa@corp.kz", is_super_admin=True))

    def form(self, key, **kw):
        data = {
            k: ("" if v is None else v)
            for k, v in services.profile_initial(ServiceProfile.objects.get(key=key)).items()
        }
        data.pop("stype")
        data.update(kw)
        return data

    def test_394_file_in_a_check_is_the_file(self):
        p = ServiceProfile.objects.get(key="ns-primary")
        lines = services.configs_text(p).splitlines()
        f = lines[-1].split(" ¦ ")
        f[4] = "named-checkconf {file}"
        lines[-1] = " ¦ ".join(f)
        self.client.post(
            reverse("inv_sprofile_edit", args=[_pk("ns-primary")]), self.form("ns-primary", configs="\n".join(lines))
        )
        p.refresh_from_db()
        self.assertEqual(p.configs[-1]["check"], "named-checkconf {file}")
        script = recipes.render_script(p, tpl(p), recipes.sample_values(p))
        self.assertIn(f"named-checkconf {p.configs[-1]['path']}", script)
        self.assertNotIn("{file}", script)

    def test_396_then_names_its_unit(self):
        p = ServiceProfile.objects.get(key="ns-primary")
        cfg = ConfigTemplate.objects.filter(kind="config").first()
        for bad in ("reload", "restart", "kill named", "reload named; rm -rf /"):
            r = self.client.post(
                reverse("inv_sprofile_edit", args=[_pk("ns-primary")]),
                self.form("ns-primary", configs=f"/etc/x.conf ¦ {cfg.role} ¦ ¦ ¦ ¦ {bad}"),
            )
            self.assertEqual(r.status_code, 200, bad)
            self.assertNotIn(
                "/etc/x.conf", [c["path"] for c in ServiceProfile.objects.get(key="ns-primary").configs], bad
            )
        p.configs = [{"path": "/etc/x.conf", "template": cfg.role, "then": "reload"}]
        p.install_template = ""
        with self.assertRaises(recipes.RecipeError):
            recipes.render_script(p, tpl(p), recipes.sample_values(p))
        p.configs[0]["then"] = "reload named"
        self.assertIn("systemctl reload named", recipes.render_script(p, tpl(p), recipes.sample_values(p)))


class Commit397And398(Base):
    """commit-397 (again) and commit-398 (review of 85eb1c1)."""

    def setUp(self):
        self.client.force_login(self.staff("sa@corp.kz", is_super_admin=True))

    def test_398_rows_with_storage_and_optional(self):
        data = {
            k: ("" if v is None else v)
            for k, v in services.profile_initial(ServiceProfile.objects.get(key="backup")).items()
        }
        self.assertEqual(data["nics"], "core-cloud\nstorage ¦ yes")
        data.pop("stype")
        data["nics"] = "core-cloud\nstorage ¦ maybe"
        self.client.post(reverse("inv_sprofile_edit", args=[_pk("backup")]), data)
        self.assertEqual(ServiceProfile.objects.get(key="backup").nics[1], {"network": "storage", "optional": True})
        data["nics"] = "core-cloud\nstorage"
        self.client.post(reverse("inv_sprofile_edit", args=[_pk("backup")]), data)
        self.assertEqual(ServiceProfile.objects.get(key="backup").nics[1], {"network": "storage"})
        r = self.client.get(reverse("infra_services"))
        self.assertContains(r, "the network of its volume: nfs or iscsi")
        data["nics"] = "\n".join(["core-cloud"] + ["nfs ¦ yes"] * 16)  # commit-401: at most 16
        r = self.client.post(reverse("inv_sprofile_edit", args=[_pk("backup")]), data)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(ServiceProfile.objects.get(key="backup").nics), 2)


class S3bProfiles(Base):
    """T34 S3b (owner 2026-11-23): Q24 a service profile's VM count; Q23 A VyOS service profiles, several per type;
    every site service linked to its profile."""

    def setUp(self):
        self.sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(self.sa)

    def test_0116_counts_and_vyos_profiles(self):
        pre_t36()  # T36: an earlier migration, on the data before 0122
        vms = dict(ServiceProfile.objects.values_list("key", "vms"))
        self.assertEqual(
            {k: vms[k] for k in ("ns", "logs", "onefe", "admin", "backup")},
            {"ns": 2, "logs": 1, "onefe": 3, "admin": 3, "backup": 1},
        )
        rr = ServiceProfile.objects.filter(stype="bgp-rr").order_by("key")
        self.assertEqual(
            [(p.key, p.install_template, p.vms) for p in rr],
            [("rr-ix", "rr-ix", 3), ("rr-vxlan", "rr-vxlan", 3)],  # 0117 (Q9): rr-inet and rr-corpnet are rr-ix
        )
        self.assertEqual(ServiceProfile.objects.get(key="cloud-gw").install_template, "igw")

    def test_0116_links_and_reverse(self):
        pre_t36()  # T36: an earlier migration, on the data before 0122
        import importlib

        from django.apps import apps

        from inventory.models import Network, Site

        site = Site.objects.create(code="dc1")
        evpn = Network.objects.create(
            site="dc1", name="vxlan", kind="evpn", interface="bond0", vlan=1, subnet="10.9.0.0", prefix=24
        )
        rr = CoreService.objects.create(site=site, stype="bgp-rr", name="vxlan-rrs", params={"network": evpn.pk})
        ns = CoreService.objects.create(site=site, stype="ns", name="ns")
        mig = importlib.import_module("inventory.migrations.0116_profile_vms_vyos")
        mig.forwards(apps, None)
        rr.refresh_from_db()
        ns.refresh_from_db()
        self.assertEqual((rr.profile.key, ns.profile.key), ("rr-vxlan", "ns"))
        mig.backwards(apps, None)
        self.assertFalse(ServiceProfile.objects.filter(stype="bgp-rr").exists())
        rr.refresh_from_db()
        self.assertIsNone(rr.profile)
        mig.forwards(apps, None)  # again: made once
        self.assertEqual(ServiceProfile.objects.filter(stype="bgp-rr").count(), 3)

    def test_profile_of_follows_the_link(self):
        from inventory import base_deploy as bd
        from inventory import core_types as ct
        from inventory.models import Site

        site = Site.objects.create(code="dc1")
        svc = CoreService.objects.create(site=site, stype="bgp-rr", name="x")
        self.assertEqual(ct.default_profile("bgp-rr", {"template": "rr-ix"}).key, "nf-ix")
        svc.profile = ServiceProfile.objects.get(key="nf-evpn")
        svc.save()
        self.assertEqual(bd.profile_of(svc).key, "nf-evpn")
        ns = CoreService.objects.create(site=site, stype="ns", name="ns")  # saved without a link: the type's
        self.assertEqual(bd.profile_of(ns).key, "ns-primary")

    def test_a_vyos_profile_names_a_vyos_template_and_has_no_configs(self):
        d = {"key": "rr-test", "name": "RR", "os": "vyos", "stype": "bgp-rr", "services": [], "vms": 3}
        d.update(install_template="rr-ix", configs=[])
        p = services.save_profile(self.sa, None, d, new=True)  # a second bgp-rr profile: allowed
        self.assertEqual((p.vms, p.install_template), (3, "rr-ix"))
        with self.assertRaisesMessage(services.ServiceError, "names a VyOS template"):
            services.save_profile(self.sa, p.pk, dict(d, install_template="bind-install"))
        ns2 = {"key": "ns-two", "name": "x", "os": "base-os", "stype": "ns", "services": ["base-os"]}
        self.assertEqual(services.save_profile(self.sa, None, ns2, new=True).stype, "ns")  # T36: several per type

    def test_the_list_shows_vyos_profiles_and_counts(self):
        r = self.client.get(reverse("infra_services"))
        html = r.content.decode()
        row = html[html.index(f'data-id="sp-{_pk("nf-ix")}"') :]
        row = row[: row.index("</tr>")]
        self.assertIn("vm ×3: vyos · nf-ix", row)  # T36 B2d (mockup m03): its roles, then its size
        self.assertIn("2/4/2", row)
        self.assertNotIn(f'data-open="dlg-spv-{_pk("nf-ix")}"', html)  # no first-boot script preview for VyOS

    def test_delete_blocked_by_a_linked_service(self):
        from inventory.models import Site

        site = Site.objects.create(code="dc1")
        CoreService.objects.create(
            site=site, stype="bgp-rr", name="rr", profile=ServiceProfile.objects.get(key="nf-ix")
        )
        r = self.client.get(reverse("infra_services"))
        self.assertNotContains(r, reverse("inv_sprofile_delete", args=[_pk("nf-ix")]))
        self.assertContains(r, reverse("inv_sprofile_delete", args=[_pk("nf-evpn")]))

    def test_409_a_seeded_key_held_by_another_type(self):
        pre_t36()  # T36: an earlier migration, on the data before 0122
        import importlib

        from django.apps import apps

        from inventory.models import Network, Site

        mig = importlib.import_module("inventory.migrations.0116_profile_vms_vyos")
        ServiceProfile.objects.filter(key="rr-vxlan").delete()
        ServiceProfile.objects.create(key="rr-vxlan", name="not a route reflector", stype="admin", services=[])
        site = Site.objects.create(code="dc1")
        evpn = Network.objects.create(
            site="dc1", name="vxlan", kind="evpn", interface="bond0", vlan=1, subnet="10.9.0.0", prefix=24
        )
        rr = CoreService.objects.create(site=site, stype="bgp-rr", name="vxlan-rrs", params={"network": evpn.pk})
        mig.forwards(apps, None)
        rr.refresh_from_db()
        self.assertEqual((rr.profile.key, rr.profile.stype), ("rr-vxlan-vyos", "bgp-rr"))
        self.assertEqual(ServiceProfile.objects.get(key="rr-vxlan").stype, "admin")  # kept as it was

    def test_410_the_reverse_stops_before_any_change(self):
        pre_t36()  # T36: an earlier migration, on the data before 0122
        import importlib

        from django.apps import apps
        from django.core.exceptions import ImproperlyConfigured

        from inventory.models import Site

        mig = importlib.import_module("inventory.migrations.0116_profile_vms_vyos")
        for k in ("rr-a", "rr-b"):
            ServiceProfile.objects.create(key=k, name=k, stype="bgp-rr", services=[], install_template="rr-ix")
        site = Site.objects.create(code="dc1")
        svc = CoreService.objects.create(
            site=site, stype="bgp-rr", name="rr", profile=ServiceProfile.objects.get(key="rr-ix")
        )
        with self.assertRaisesMessage(ImproperlyConfigured, "bgp-rr: rr-a, rr-b"):
            mig.backwards(apps, None)
        svc.refresh_from_db()
        self.assertEqual(svc.profile.key, "rr-ix")  # nothing changed
        self.assertTrue(ServiceProfile.objects.filter(key="rr-vxlan").exists())

    def test_413_saves_take_the_host_profiles_lock_first(self):
        from unittest import mock

        calls = []
        real = ServiceProfile.objects.select_for_update

        def sfu(*a, **k):
            qs = real(*a, **k)
            orig = qs.filter

            def filt(*fa, **fk):
                calls.append(fk)
                return orig(*fa, **fk)

            qs.filter = filt
            return qs

        d = {"key": "rr-lock", "name": "RR", "os": "vyos", "stype": "bgp-rr", "services": [], "vms": 3}
        d.update(install_template="rr-ix", configs=[])
        with mock.patch.object(ServiceProfile.objects, "select_for_update", side_effect=sfu):
            services.save_profile(self.sa, None, d, new=True)
        self.assertEqual(calls[0], {"stype": ServiceProfile.HOST})

    def test_414_the_edit_page_of_a_vyos_profile(self):
        html = self.client.get(reverse("inv_sprofile_edit", args=[_pk("nf-ix")])).content.decode()
        self.assertIn("VyOS", html)
        self.assertNotIn("Ubuntu", html)

    def test_420_a_vyos_profile_names_the_template_its_services_use(self):
        from inventory import core_types as ct

        d = {"key": "gw-egw", "name": "GW", "os": "vyos", "stype": "cloud-gw", "services": [], "vms": 2}
        d.update(install_template="egw", configs=[])
        with self.assertRaisesMessage(services.ServiceError, "A cloud-gw service is made with igw"):
            services.save_profile(self.sa, None, d, new=True)
        wrong = ServiceProfile.objects.create(
            key="gw-x", name="x", stype="cloud-gw", services=[], install_template="egw"
        )
        self.assertFalse(ct.fits(wrong, "cloud-gw", {"template": "igw"}))
        self.assertEqual(ct.default_profile("cloud-gw", {"template": "igw"}).key, "cloud-gw")
        self.assertIsNone(ct.default_profile("portal-gw", {"template": "igw"}))


class ProfilesUpdates(Base):
    """Owner 2026-11-23 16:01 (U4-U8, mockups p03/p04): found by its id, Name editable, Description, OS, no Ubuntu part
    on a VyOS profile; rr-ix (Q6, Q9)."""

    def setUp(self):
        self.sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(self.sa)

    def form(self, of, **kw):
        data = {
            k: ("" if v is None else v) for k, v in services.profile_initial(ServiceProfile.objects.get(key=of)).items()
        }
        data.pop("stype")
        data.pop("os")
        data.update(kw)
        return data

    def test_a_new_name_keeps_the_links(self):
        """U4/U5: the Name changes; the site services and the pages follow the id."""
        from inventory.models import Site

        p = ServiceProfile.objects.get(key="ns-primary")
        svc = CoreService.objects.create(site=Site.objects.create(code="dc1"), stype="ns", name="ns", profile=p)
        r = self.client.post(
            reverse("inv_sprofile_edit", args=[p.pk]), self.form("ns-primary", key="dns", name="Name server")
        )
        self.assertRedirects(r, reverse("infra_services") + f"?sel=sp-{p.pk}", fetch_redirect_response=False)
        p.refresh_from_db()
        svc.refresh_from_db()
        self.assertEqual((p.key, p.name, svc.profile_id), ("dns", "Name server", p.pk))
        r = self.client.post(reverse("inv_sprofile_edit", args=[p.pk]), self.form("dns", key="admin"), follow=True)
        self.assertContains(r, "Profile admin already exists")  # Q4: unique
        r = self.client.post(reverse("inv_sprofile_edit", args=[p.pk]), self.form("dns", key="has space"), follow=True)
        self.assertEqual(ServiceProfile.objects.get(pk=p.pk).key, "dns")  # no spaces

    def test_os_and_type_are_fixed_and_follow_each_other(self):
        """U7/Q5: the OS is chosen when the profile is made, the types of that OS only, both fixed afterwards."""
        self.assertEqual(ServiceProfile.objects.get(key="nf-ix").os, "vyos")  # 0118
        self.assertEqual(ServiceProfile.objects.get(key="ns-primary").os, "base-os")
        ServiceProfile.objects.filter(key="backup").delete()
        new = {"key": "b2", "name": "", "os": "vyos", "stype": "backup", "services": "base-os", "vcpu": 1}
        self.client.post(reverse("inv_sprofile_new"), new)
        self.assertFalse(ServiceProfile.objects.filter(key="b2").exists())  # backup is not a VyOS type
        p = ServiceProfile.objects.get(key="ns-primary")
        self.client.post(
            reverse("inv_sprofile_edit", args=[p.pk]), dict(self.form("ns-primary"), os="vyos", stype="bgp-rr")
        )
        p.refresh_from_db()
        self.assertEqual((p.os, p.stype), ("base-os", "ns"))

    def test_a_vyos_profile_has_no_ubuntu_part(self):
        """U8: "dont add any ubuntu configs to VyOS based service profiles"."""
        p = ServiceProfile.objects.get(key="nf-ix")
        r = self.client.get(reverse("infra_services"))
        html = r.content.decode()
        d = html[html.index(f'data-for="sp-{p.pk}"') :]
        d = d[: d.index('<div class="odetail"', 10)] if '<div class="odetail"' in d[10:] else d
        self.assertNotIn(">Configs<", d)
        self.assertNotIn(">Logging<", d)
        self.assertNotIn("rsyslog", d)
        self.assertIn("VyOS template", d)
        html = self.client.get(reverse("inv_sprofile_edit", args=[p.pk])).content.decode()
        self.assertNotIn('name="configs"', html)
        self.assertNotIn('name="services"', html)
        r = self.client.post(
            reverse("inv_sprofile_edit", args=[p.pk]), dict(self.form("nf-ix"), checks="unit frr"), follow=True
        )
        self.assertEqual(ServiceProfile.objects.get(pk=p.pk).checks, [])

    def test_rr_ix_is_one_profile_and_one_template(self):
        """Q6/Q9 (0117): rr-inet and rr-corpnet became rr-ix; a corporate exchange still peers with the cgw."""
        pre_t36()  # T36: an earlier migration, on the data before 0122
        self.assertFalse(ConfigTemplate.objects.filter(role__in=["rr-inet", "rr-corpnet"]).exists())
        self.assertTrue(ConfigTemplate.objects.filter(role="rr-ix").exists())
        self.assertEqual(
            sorted(ServiceProfile.objects.filter(stype="bgp-rr").values_list("key", flat=True)), ["rr-ix", "rr-vxlan"]
        )
        self.assertEqual(
            ServiceProfile.objects.get(key="rr-ix").name, "Route reflector, any network with external routers"
        )

    def mig17(self):
        import importlib

        return importlib.import_module("inventory.migrations.0117_rr_ix")

    def test_429_a_profile_with_its_own_settings_is_kept_and_put_back(self):
        """commit-429: rr-corpnet merged only with rr-inet's settings; its own size stays; the reverse restores it."""
        pre_t36()  # T36: an earlier migration, on the data before 0122
        from django.apps import apps

        from inventory.models import Site

        m = self.mig17()
        m.backwards(apps, None)
        self.assertEqual(
            sorted(ServiceProfile.objects.filter(stype="bgp-rr").values_list("key", "install_template")),
            [("rr-corpnet", "rr-corpnet"), ("rr-inet", "rr-inet"), ("rr-vxlan", "rr-vxlan")],
        )
        self.assertTrue(ConfigTemplate.objects.filter(role="rr-corpnet").exists())
        ServiceProfile.objects.filter(key="rr-corpnet").update(vms=5)
        corp = ServiceProfile.objects.get(key="rr-corpnet")
        svc = CoreService.objects.create(site=Site.objects.create(code="dc1"), stype="bgp-rr", name="c", profile=corp)
        m.forwards(apps, None)
        corp.refresh_from_db()
        svc.refresh_from_db()
        self.assertEqual((corp.key, corp.install_template, corp.vms), ("rr-corpnet", "rr-ix", 5))  # kept, its own size
        self.assertEqual(svc.profile_id, corp.pk)
        m.backwards(apps, None)
        corp.refresh_from_db()
        self.assertEqual(corp.install_template, "rr-corpnet")
        ServiceProfile.objects.filter(key="rr-corpnet").update(vms=3)
        m.forwards(apps, None)  # the same settings now: merged, its service moves to rr-ix
        svc.refresh_from_db()
        self.assertEqual(svc.profile.key, "rr-ix")
        m.backwards(apps, None)
        svc.refresh_from_db()
        self.assertEqual((svc.profile.key, svc.profile.vms), ("rr-corpnet", 3))  # recreated whole, its service back

    def test_431_the_reverse_takes_a_free_key(self):
        """commit-431: rr-inet held since by another profile - the reverse gives the route reflector rr-inet-vyos."""
        pre_t36()  # T36: an earlier migration, on the data before 0122
        from django.apps import apps

        m = self.mig17()
        ix = ServiceProfile.objects.get(key="rr-ix")
        ServiceProfile.objects.create(key="rr-inet", name="x", stype="admin", services=[])
        m.backwards(apps, None)
        ix.refresh_from_db()
        self.assertEqual((ix.key, ix.install_template), ("rr-inet-vyos", "rr-inet"))
        self.assertEqual(ServiceProfile.objects.get(key="rr-inet").stype, "admin")

    def test_433_a_free_key_within_forty_characters(self):
        """commit-433: "-vyos", "-vyos2", ... fitted into the field's 40 characters."""
        pre_t36()  # T36: an earlier migration, on the data before 0122
        from django.apps import apps

        m = self.mig17()
        sp = apps.get_model("inventory", "ServiceProfile")
        k = "r" * 40
        ServiceProfile.objects.create(key=k, name="x", stype="admin", services=[])
        free = m._free(sp, k)
        self.assertEqual((len(free), free.endswith("-vyos")), (40, True))
        ServiceProfile.objects.create(key=free, name="x", stype="admin", services=[])
        again = m._free(sp, k)
        self.assertEqual((len(again), again.endswith("-vyos2")), (40, True))

    def test_434_a_merged_profile_gets_its_own_legacy_back(self):
        """commit-434: a merged profile 0116 had reused (no seed marker) is put back without one."""
        pre_t36()  # T36: an earlier migration, on the data before 0122
        from django.apps import apps

        m = self.mig17()
        m.backwards(apps, None)
        ServiceProfile.objects.filter(key="rr-corpnet").update(legacy={})  # an operator's profile 0116 reused
        m.forwards(apps, None)
        self.assertFalse(ServiceProfile.objects.filter(key="rr-corpnet").exists())  # merged
        m.backwards(apps, None)
        self.assertEqual(ServiceProfile.objects.get(key="rr-corpnet").legacy, {})

    def test_432_a_route_reflector_added_since_goes_back_by_its_network(self):
        """commit-432: a corporate-exchange route reflector added after 0117 gets rr-corpnet's template and profile."""
        pre_t36()  # T36: an earlier migration, on the data before 0122
        from django.apps import apps

        from inventory.models import Network, Site

        m = self.mig17()
        site = Site.objects.create(code="dc1")
        corp_net = Network.objects.create(
            site="dc1", name="corpnet-ix", kind="ix", interface="bond0", vlan=7, subnet="10.7.0.0", prefix=24
        )
        inet_net = Network.objects.create(
            site="dc1", name="cloud-ix", kind="ix", interface="bond0", vlan=8, subnet="10.8.0.0", prefix=24
        )
        ix = ServiceProfile.objects.get(key="rr-ix")
        tpl = ConfigTemplate.objects.get(role="rr-ix")
        corp = CoreService.objects.create(
            site=site, stype="bgp-rr", name="c", profile=ix, template=tpl, params={"network": corp_net.pk}
        )
        inet = CoreService.objects.create(
            site=site, stype="bgp-rr", name="i", profile=ix, template=tpl, params={"network": inet_net.pk}
        )
        m.backwards(apps, None)
        corp.refresh_from_db()
        inet.refresh_from_db()
        self.assertEqual((corp.template.role, corp.profile.key), ("rr-corpnet", "rr-corpnet"))
        self.assertEqual((inet.template.role, inet.profile.key), ("rr-inet", "rr-inet"))

    def test_432_with_an_rr_ix_template_there_before(self):
        """commit-432: rr-ix there before 0117 (not renamed) - both merged templates come back, and a corporate route
        reflector added since goes to rr-corpnet's template."""
        pre_t36()  # T36: an earlier migration, on the data before 0122
        from django.apps import apps

        from inventory.models import Network, Site

        m = self.mig17()
        m.backwards(apps, None)
        pre = ConfigTemplate.objects.create(role="rr-ix", name="rr-ix", os="vyos", body="x")
        m.forwards(apps, None)
        self.assertFalse(ConfigTemplate.objects.filter(role__in=["rr-inet", "rr-corpnet"]).exists())
        site = Site.objects.create(code="dc1")
        net = Network.objects.create(
            site="dc1", name="corpnet-ix", kind="ix", interface="bond0", vlan=7, subnet="10.7.0.0", prefix=24
        )
        svc = CoreService.objects.create(
            site=site,
            stype="bgp-rr",
            name="c",
            template=pre,
            profile=ServiceProfile.objects.get(key="rr-ix"),
            params={"network": net.pk},
        )
        m.backwards(apps, None)
        svc.refresh_from_db()
        self.assertEqual(
            sorted(ConfigTemplate.objects.filter(role__startswith="rr-").values_list("role", flat=True)),
            ["rr-corpnet", "rr-inet", "rr-ix", "rr-vxlan"],
        )
        self.assertEqual((svc.template.role, svc.profile.key), ("rr-corpnet", "rr-corpnet"))

    def test_436_a_link_from_before_the_upgrade_is_kept(self):
        """commit-436: a corporate route reflector already on rr-inet's template and profile before 0117 keeps them on
        the reverse; only links made since follow the network rule."""
        pre_t36()  # T36: an earlier migration, on the data before 0122
        from django.apps import apps

        from inventory.models import Network, Site

        m = self.mig17()
        m.backwards(apps, None)
        site = Site.objects.create(code="dc1")
        net = Network.objects.create(
            site="dc1", name="corpnet-ix", kind="ix", interface="bond0", vlan=7, subnet="10.7.0.0", prefix=24
        )
        old = CoreService.objects.create(
            site=site,
            stype="bgp-rr",
            name="o",
            template=ConfigTemplate.objects.get(role="rr-inet"),
            profile=ServiceProfile.objects.get(key="rr-inet"),
            params={"network": net.pk},
        )
        m.forwards(apps, None)
        new = CoreService.objects.create(
            site=site,
            stype="bgp-rr",
            name="n",
            template=ConfigTemplate.objects.get(role="rr-ix"),
            profile=ServiceProfile.objects.get(key="rr-ix"),
            params={"network": net.pk},
        )
        m.backwards(apps, None)
        old.refresh_from_db()
        new.refresh_from_db()
        self.assertEqual((old.template.role, old.profile.key), ("rr-inet", "rr-inet"))  # as it was
        self.assertEqual((new.template.role, new.profile.key), ("rr-corpnet", "rr-corpnet"))  # by its network

    def test_438_each_link_by_its_own_history(self):
        """commit-438: a corporate route reflector on rr-corpnet (template and its own-sized profile) at the upgrade,
        only its profile switched to rr-ix since - the reverse gives it rr-corpnet's template and profile both."""
        pre_t36()  # T36: an earlier migration, on the data before 0122
        from django.apps import apps

        from inventory.models import Network, Site

        m = self.mig17()
        m.backwards(apps, None)
        ServiceProfile.objects.filter(key="rr-corpnet").update(vms=5)  # its own size: kept, not merged
        site = Site.objects.create(code="dc1")
        net = Network.objects.create(
            site="dc1", name="corpnet-ix", kind="ix", interface="bond0", vlan=7, subnet="10.7.0.0", prefix=24
        )
        svc = CoreService.objects.create(
            site=site,
            stype="bgp-rr",
            name="c",
            template=ConfigTemplate.objects.get(role="rr-corpnet"),
            profile=ServiceProfile.objects.get(key="rr-corpnet"),
            params={"network": net.pk},
        )
        m.forwards(apps, None)
        CoreService.objects.filter(pk=svc.pk).update(profile=ServiceProfile.objects.get(key="rr-ix"))  # since
        m.backwards(apps, None)
        svc.refresh_from_db()
        self.assertEqual((svc.template.role, svc.profile.key), ("rr-corpnet", "rr-corpnet"))

    def test_440_a_newer_profile_follows_the_template(self):
        """commit-440: a corporate route reflector kept on rr-inet's template with its own rr-inet-sized profile, its
        profile switched to rr-ix since - the reverse leaves it on rr-inet's profile, as its template says."""
        pre_t36()  # T36: an earlier migration, on the data before 0122
        from django.apps import apps

        from inventory.models import Network, Site

        m = self.mig17()
        m.backwards(apps, None)
        own = ServiceProfile.objects.create(
            key="rr-inet-big", name="x", stype="bgp-rr", services=[], install_template="rr-inet", vms=7
        )
        site = Site.objects.create(code="dc1")
        net = Network.objects.create(
            site="dc1", name="corpnet-ix", kind="ix", interface="bond0", vlan=7, subnet="10.7.0.0", prefix=24
        )
        svc = CoreService.objects.create(
            site=site,
            stype="bgp-rr",
            name="c",
            template=ConfigTemplate.objects.get(role="rr-inet"),
            profile=own,
            params={"network": net.pk},
        )
        m.forwards(apps, None)
        CoreService.objects.filter(pk=svc.pk).update(profile=ServiceProfile.objects.get(key="rr-ix"))  # since
        m.backwards(apps, None)
        svc.refresh_from_db()
        self.assertEqual((svc.template.role, svc.profile.install_template), ("rr-inet", "rr-inet"))

    def test_440_with_a_survivor_that_was_rr_ix(self):
        """commit-440: the surviving profile was made with rr-ix before - a newer link of a reflector on rr-inet's
        template goes to a profile made with rr-inet on the reverse."""
        pre_t36()  # T36: an earlier migration, on the data before 0122
        from django.apps import apps

        from inventory.models import Network, Site

        m = self.mig17()
        m.backwards(apps, None)
        ServiceProfile.objects.filter(key="rr-inet").update(install_template="rr-ix")  # the survivor, as it was
        own = ServiceProfile.objects.create(
            key="rr-inet-big", name="x", stype="bgp-rr", services=[], install_template="rr-inet", vms=7
        )
        site = Site.objects.create(code="dc1")
        net = Network.objects.create(
            site="dc1", name="corpnet-ix", kind="ix", interface="bond0", vlan=7, subnet="10.7.0.0", prefix=24
        )
        svc = CoreService.objects.create(
            site=site,
            stype="bgp-rr",
            name="c",
            template=ConfigTemplate.objects.get(role="rr-inet"),
            profile=own,
            params={"network": net.pk},
        )
        m.forwards(apps, None)
        survivor = ServiceProfile.objects.get(key="rr-ix")
        CoreService.objects.filter(pk=svc.pk).update(profile=survivor)  # since
        m.backwards(apps, None)
        svc.refresh_from_db()
        self.assertEqual(
            (svc.template.role, svc.profile.install_template, svc.profile.pk), ("rr-inet", "rr-inet", own.pk)
        )

    def test_441_a_newer_corporate_link_gets_the_standard_profile(self):
        """commit-441: rr-corpnet merged (recreated on the reverse) and a custom rr-corpnet profile kept - a corporate
        reflector linked to rr-ix since gets the recreated standard one, whatever sorts first."""
        pre_t36()  # T36: an earlier migration, on the data before 0122
        from django.apps import apps

        from inventory.models import Network, Site

        m = self.mig17()
        m.backwards(apps, None)
        ServiceProfile.objects.create(
            key="a-corp", name="x", stype="bgp-rr", services=[], install_template="rr-corpnet", vms=7
        )
        m.forwards(apps, None)
        site = Site.objects.create(code="dc1")
        net = Network.objects.create(
            site="dc1", name="corpnet-ix", kind="ix", interface="bond0", vlan=7, subnet="10.7.0.0", prefix=24
        )
        svc = CoreService.objects.create(
            site=site,
            stype="bgp-rr",
            name="c",
            template=ConfigTemplate.objects.get(role="rr-ix"),
            profile=ServiceProfile.objects.get(key="rr-ix"),
            params={"network": net.pk},
        )
        m.backwards(apps, None)
        svc.refresh_from_db()
        self.assertEqual(svc.template.role, "rr-corpnet")
        self.assertEqual(svc.profile_id, ServiceProfile.objects.get(key="rr-corpnet").pk)
