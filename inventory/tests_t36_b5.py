"""T36 B5 (owner 2026-11-26 10:18 "go ahead"; QB5-1..5 11:29). B5a - a base-os VM is installed from its units: base-os,
then its service's functions in role order, each from its own installation template and config files (QB5-1 "drop
it": a service profile has none); migration 0129 moved them there, the machines' scripts unchanged."""

import importlib
import types

from django.apps import apps
from django.test import TestCase
from django.urls import reverse

from accounts.tests import Base
from inventory import recipes, services
from inventory.base_deploy import templates_of
from inventory.models import ConfigTemplate, HostService, ServiceProfile


def m0129():
    return importlib.import_module("inventory.migrations.0129_t36_function_install")


def m0130():
    return importlib.import_module("inventory.migrations.0130_t36_graylog_split")


def scripts(units_of=None):
    """{profile: its script without the title line} of every base-os site service profile."""
    out = {}
    for p in ServiceProfile.objects.filter(os="base-os").exclude(stype="host").order_by("key"):
        units = units_of(p) if units_of else None
        tpls = ConfigTemplate.objects.in_bulk()  # owner 2026-11-26 12:09: by id
        out[p.key] = recipes.render_script(p, tpls, recipes.sample_values(p), units=units).split("\n", 2)[2]
    return out


def as_before(p):
    """The profile's installation as it was before 0129: one unit of its own template and configs (by key then)."""
    ids = dict(ConfigTemplate.objects.values_list("role", "pk"))
    return [
        types.SimpleNamespace(
            key="profile",
            install_template_id=ids.get(p.install_template),
            configs=[{**c, "template": ids.get(c.get("template"))} for c in p.configs or []],
        )
    ]


def m0131():
    return importlib.import_module("inventory.migrations.0131_t36_reference_ids")


def m0132():
    return importlib.import_module("inventory.migrations.0132_t36_admin_tools")


def m0133():
    return importlib.import_module("inventory.migrations.0133_t36_time")


def m0137():
    return importlib.import_module("inventory.migrations.0137_t36_claugine_function")


def undo_late():
    """The data migrations after 0132, undone newest first: 0137 (B5f claugine), 0133 (B5d time)."""
    m0137().backward(apps, None)
    m0133().backward(apps, None)


class Migration0129(TestCase):
    """0129 on the data as it made it: 0130 (B5b) undone first, 0131 (ids) around it."""

    def back(self):
        m0131().backward(apps, None)
        m0129().backward(apps, None)

    def forward(self):
        m0129().forward(apps, None)
        m0131().forward(apps, None)

    def test_the_scripts_are_the_same(self):
        undo_late()  # T36 B5f, B5d first
        m0132().backward(apps, None)
        m0130().backward(apps, None)
        after = scripts()
        self.back()
        before = scripts(as_before)
        self.assertEqual(set(after), set(before))
        for k in after:
            if k == "claugine":  # it had no installation: now base-os's (B5f's own is undone above)
                self.assertNotIn('step "base-os', before[k])
                self.assertTrue(before[k].endswith('step "done"\n'))
                self.assertIn('step "base-os: packages"', after[k])
                continue
            self.assertEqual(after[k], before[k], k)

    def test_back_then_forward_is_the_same(self):
        def snap():
            roles = dict(ConfigTemplate.objects.values_list("pk", "role"))
            return (
                sorted(ConfigTemplate.objects.values_list("role", "kind", "body")),
                sorted(ServiceProfile.objects.values_list("key", "install_template", "configs", "services")),
                sorted(
                    (
                        h.key,
                        roles.get(h.install_template_id),
                        str([{**c, "template": roles.get(c.get("template"))} for c in h.configs]),
                    )
                    for h in HostService.objects.all()
                ),
            )

        undo_late()  # T36 B5f, B5d first

        m0132().backward(apps, None)
        m0130().backward(apps, None)
        after = snap()
        self.back()
        old = {p.key: p.install_template for p in ServiceProfile.objects.filter(os="base-os").exclude(stype="host")}
        self.assertEqual(old["ns-primary"], "ns-install")
        self.assertEqual(old["admin"], "admin-install")
        self.assertIn("- name: base-os\n", ConfigTemplate.objects.get(role="admin-install").body)
        self.assertFalse(HostService.objects.filter(install_template__isnull=False).exists())
        self.assertFalse(ConfigTemplate.objects.filter(role="base-os-install").exists())
        ids = sorted(ConfigTemplate.objects.values_list("pk", flat=True))
        self.forward()
        self.back()
        self.assertEqual(sorted(ConfigTemplate.objects.values_list("pk", flat=True)), ids)  # 0114 finds them by id
        self.forward()
        self.assertEqual(snap(), after)

    def test_a_second_forward_changes_nothing(self):
        undo_late()  # T36 B5f, B5d first
        m0132().backward(apps, None)
        m0130().backward(apps, None)
        before = HostService.objects.get(key="base-os").legacy
        m0129().forward(apps, None)
        self.assertEqual(HostService.objects.get(key="base-os").legacy, before)

    def test_each_config_goes_to_the_function_writing_it(self):
        undo_late()  # T36 B5f, B5d first
        m0132().backward(apps, None)
        m0130().backward(apps, None)
        self.assertEqual(
            [c["path"] for c in HostService.objects.get(key="ns").configs],
            ["/etc/bind/named.options", "/etc/bind/named.conf.zones", "/etc/bind/named.conf"],
        )
        self.assertEqual(
            [c["path"] for c in HostService.objects.get(key="base-os").configs], ["/etc/sudoers.d/90-portal"]
        )
        self.assertFalse(ServiceProfile.objects.exclude(configs=[]).exists())
        self.assertFalse(ServiceProfile.objects.filter(os="base-os").exclude(install_template="").exists())
        self.assertTrue(ServiceProfile.objects.filter(os="vyos").exclude(install_template="").exists())  # kept

    def test_differing_steps_stop(self):
        undo_late()  # T36 B5f, B5d first
        m0132().backward(apps, None)
        m0130().backward(apps, None)
        self.back()
        t = ConfigTemplate.objects.get(role="ns-install")
        ConfigTemplate.objects.create(
            role="ns2-install", kind="steps", os="ubuntu", name="x", body=t.body.replace("bind9", "bind9-other")
        )
        ServiceProfile.objects.filter(key="ns-site").update(install_template="ns2-install")
        from django.core.exceptions import ImproperlyConfigured

        # commit-567 (verify of 566d0ab): ns-site's VMs would run ns-primary's installation - stop, nothing changed
        with self.assertRaisesMessage(ImproperlyConfigured, "ns-site: its ns installation differs from ns-primary's"):
            m0129().forward(apps, None)
        self.assertTrue(ConfigTemplate.objects.filter(role="ns2-install").exists())
        self.assertEqual(ServiceProfile.objects.get(key="ns-site").install_template, "ns2-install")


class Units(TestCase):
    def test_base_os_then_the_roles_functions(self):
        self.assertEqual(ServiceProfile.objects.get(key="admin").unit_keys(), ["base-os", "admin", "fail2ban", "tools"])
        self.assertEqual(
            ServiceProfile.objects.get(key="log-collector").unit_keys(),
            ["base-os", "mongodb", "graylog-server", "graylog-datanode"],
        )
        self.assertEqual(ServiceProfile.objects.get(key="kvm-mgmt").unit_keys(), [])  # the host deployment
        self.assertEqual(ServiceProfile.objects.filter(os="vyos").first().unit_keys(), [])

    def test_roles_to_fill_take_the_services_list(self):
        ServiceProfile.objects.filter(key="onefe").update(roles=[])
        self.assertEqual(ServiceProfile.objects.get(key="onefe").unit_keys(), ["base-os", "one-fe"])

    def test_the_bind_files_are_the_ns_functions(self):
        p = ServiceProfile.objects.get(key="ns-site")
        c, body = recipes.config_file(p, templates_of(p), "/etc/bind/named.conf")
        self.assertEqual(ConfigTemplate.objects.get(pk=c["template"]).role, "bind-named-conf")  # by id


class Pages(Base):
    def setUp(self):
        self.client.force_login(self.staff("sa@corp.kz", is_super_admin=True))

    def test_the_services_page_names_its_units(self):
        r = self.client.get(reverse("infra_services"))
        admin = HostService.objects.get(key="admin")
        self.assertContains(r, f"{reverse('infra_functions')}?sel=fn-{admin.pk}")
        self.assertContains(r, "nothing yet")  # tools: no installation of its own yet

    def test_the_functions_page_edits_them(self):
        ns = HostService.objects.get(key="ns")
        r = self.client.get(reverse("infra_functions"))
        self.assertContains(r, f'name="fn{ns.pk}-install_template"')
        self.assertContains(r, f'name="fn{ns.pk}-configs"')
        self.assertContains(r, "ns-install")
        vyos = HostService.objects.filter(os="vyos", group="function").first()
        self.assertNotContains(r, f'name="fn{vyos.pk}-install_template"')

    def test_a_change_that_breaks_a_service_is_refused(self):
        from inventory.tests_recipes import fn_form

        ns = HostService.objects.get(key="ns")
        bad = ConfigTemplate.objects.create(
            role="bad-install", kind="steps", os="ubuntu", name="bad", body="- name: ns\n  configs: [/etc/nope]\n"
        )
        r = self.client.post(
            reverse("inv_function_save", args=[ns.pk]), fn_form(ns, install_template=str(bad.pk)), follow=True
        )
        self.assertContains(r, "Service ns-primary")
        self.assertEqual(HostService.objects.get(key="ns").install_template.role, "ns-install")

    def test_the_base_os_installation_on_the_os_page(self):
        r = self.client.get(reverse("infra_os"))
        self.assertContains(r, 'name="os-base-os-install_template"')
        self.assertContains(r, "base-os-install")
        self.assertNotContains(r, 'name="os-vyos-install_template"')
        base = HostService.objects.get(key="base-os")
        data = {
            "os-base-os-version": "26.04",
            "os-base-os-min_vcpu": "1",
            "os-base-os-min_ram_gb": "2",
            "os-base-os-min_disk_gb": "5",
            "os-base-os-install_template": str(base.install_template_id),
            "os-base-os-configs": "",  # its sudoers file gone: its step writes it - refused
        }
        r = self.client.post(reverse("inv_os_save", args=["base-os"]), data, follow=True)
        self.assertContains(r, "is not a config of base-os")
        self.assertEqual(HostService.objects.get(key="base-os").configs, base.configs)
        self.assertIn("0440", services.configs_text(base))
        data["os-base-os-configs"] = services.configs_text(base).replace("0440", "0400")
        self.client.post(reverse("inv_os_save", args=["base-os"]), data)
        self.assertEqual(HostService.objects.get(key="base-os").configs[0]["mode"], "0400")
        data["os-base-os-install_template"] = "ns-install"  # base-os would write the bind files the ns function writes
        data["os-base-os-configs"] = services.configs_text(base)
        r = self.client.post(reverse("inv_os_save", args=["base-os"]), data, follow=True)
        self.assertEqual(HostService.objects.get(key="base-os").install_template, base.install_template)

    def test_a_functions_template_is_not_deleted(self):
        t = ConfigTemplate.objects.get(role="ns-install")
        r = self.client.post(reverse("infra_template_delete", args=[t.pk]), follow=True)
        self.assertContains(r, "function ns")
        self.assertTrue(ConfigTemplate.objects.filter(pk=t.pk).exists())


class GraylogSplit(TestCase):
    """B5b (rules v2 C2/C4, N4a): mongodb, graylog-server and graylog-datanode each their own installation."""

    def script(self):
        p = ServiceProfile.objects.get(key="log-collector")
        return recipes.render_script(p, templates_of(p), recipes.sample_values(p))

    def test_three_installations_in_role_order(self):
        roles = {
            k: HostService.objects.get(key=k).install_template.role
            for k in ("mongodb", "graylog-server", "graylog-datanode")
        }
        self.assertEqual(roles, {k: f"{k}-install" for k in roles})
        out = self.script()
        order = [out.index(f'step "{k}: packages"') for k in ("mongodb", "graylog-server", "graylog-datanode")]
        self.assertEqual(order, sorted(order))
        node = recipes.parse_steps(ConfigTemplate.objects.get(role="graylog-datanode-install").body)[0]
        self.assertEqual(node["files"][0]["set"]["opensearch_heap"], "8g")
        self.assertEqual(
            node["configs"], ["/etc/sysctl.d/99-graylog-datanode.conf", "/etc/graylog/datanode/datanode-overrides.conf"]
        )
        self.assertIn("-Xms4g -Xmx4g", out)  # N4a: the server's heap, its /etc/default file
        self.assertIn("systemctl restart graylog-server", out)
        mongo = recipes.parse_steps(ConfigTemplate.objects.get(role="mongodb-install").body)[0]
        self.assertEqual(mongo["mounts"][0]["where"], "/var/lib/mongodb")
        self.assertNotIn(
            "mongodb-org",
            recipes.parse_steps(ConfigTemplate.objects.get(role="graylog-server-install").body)[0]["packages"],
        )

    def test_back_then_forward(self):
        before = self.script()
        old = ConfigTemplate.objects.get(role="graylog-server-install").body
        undo_late()  # T36 B5f, B5d first
        m0132().backward(apps, None)
        m0130().backward(apps, None)
        self.assertIn("mongodb-org", ConfigTemplate.objects.get(role="graylog-server-install").body)
        self.assertFalse(ConfigTemplate.objects.filter(role__in=["mongodb-install", "graylog-server-default"]).exists())
        self.assertIsNone(HostService.objects.get(key="mongodb").install_template_id)
        m0130().forward(apps, None)
        m0133().forward(apps, None)  # T36 B5d: base-os's time step back
        m0137().forward(apps, None)  # T36 B5f
        self.assertEqual(ConfigTemplate.objects.get(role="graylog-server-install").body, old)
        self.assertEqual(self.script(), before)

    def test_an_edited_template_is_left(self):
        undo_late()  # T36 B5f, B5d first
        m0132().backward(apps, None)
        m0130().backward(apps, None)
        ConfigTemplate.objects.filter(role="graylog-server-install").update(
            body="- name: graylog-server\n  packages: [x]\n"
        )
        from io import StringIO
        from unittest import mock

        with mock.patch("sys.stdout", new_callable=StringIO) as out:
            m0130().forward(apps, None)
        self.assertIn("not as 0129 made it - not split", out.getvalue())
        self.assertIsNone(HostService.objects.get(key="mongodb").install_template_id)


class Review558to561(Base):
    """commit-558 .. commit-561 (review of edd3b0e, owner 2026-11-26 12:09 "fix commit-558, commit-559, commit-560,
    commit-561")."""

    def setUp(self):
        self.sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(self.sa)

    def before_0129(self):
        undo_late()  # T36 B5f, B5d first
        m0132().backward(apps, None)
        m0130().backward(apps, None)
        m0131().backward(apps, None)
        m0129().backward(apps, None)

    def test_558_a_step_of_a_function_the_profile_lacks_stops_it(self):
        from django.core.exceptions import ImproperlyConfigured

        self.before_0129()
        t = ConfigTemplate.objects.get(role="backup-install")
        ConfigTemplate.objects.filter(pk=t.pk).update(body=t.body + "- name: tools\n  packages: [jq]\n")
        before = sorted(ConfigTemplate.objects.values_list("pk", "role", "body"))
        with self.assertRaisesMessage(ImproperlyConfigured, "backup: its installation backup-install has a step tools"):
            m0129().forward(apps, None)
        self.assertEqual(sorted(ConfigTemplate.objects.values_list("pk", "role", "body")), before)  # nothing changed
        self.assertFalse(HostService.objects.filter(install_template__isnull=False).exists())

    def test_558_steps_in_another_order_stop_it(self):
        from django.core.exceptions import ImproperlyConfigured

        self.before_0129()
        p = ServiceProfile.objects.get(key="admin")
        roles = p.roles
        roles[0]["functions"] = ["fail2ban", "admin", "tools"]
        ServiceProfile.objects.filter(pk=p.pk).update(roles=roles)
        with self.assertRaisesMessage(
            ImproperlyConfigured, "admin: the steps of admin-install run base-os, admin, fail2ban"
        ):
            m0129().forward(apps, None)

    def test_559_free_keys_within_40_characters(self):
        key = m0129()._key
        self.assertEqual(key("ns", {"ns-install"}), "ns-install-2")
        self.assertEqual(key("ns", {"ns-install", "ns-install-2"}), "ns-install-3")
        long = "f" * 40
        k = key(long, set())
        self.assertEqual((len(k), k.endswith("-install")), (40, True))
        k2 = key(long, {k})
        self.assertEqual((len(k2), k2.endswith("-install-2")), (40, True))

    def test_559_a_long_function_key_migrates(self):
        self.before_0129()
        long = "x" * 40
        HostService.objects.create(key=long, name="long", group="function", os="base-os", legacy={})
        ConfigTemplate.objects.create(role=long[:32] + "-install", kind="config", os="ubuntu", name="taken", body="x")
        p = ServiceProfile.objects.get(key="backup")
        roles = p.roles
        roles[0]["functions"] = ["backup-ds", long]
        ServiceProfile.objects.filter(pk=p.pk).update(roles=roles)
        t = ConfigTemplate.objects.get(role="backup-install")
        ConfigTemplate.objects.filter(pk=t.pk).update(body=t.body + f"- name: {long}\n  packages: [jq]\n")
        m0129().forward(apps, None)
        role = HostService.objects.get(key=long).install_template.role
        self.assertEqual((len(role), role.endswith("-install-2")), (40, True))

    def two_roles(self):
        """admin with a second role, tools only, of 1 VM (its 3 VMs: 2 + 1)."""
        p = ServiceProfile.objects.get(key="admin")
        tools = HostService.objects.get(key="tools")
        ids = [HostService.objects.get(key=k).pk for k in ("admin", "fail2ban")]
        p.roles = [
            {**p.roles[0], "name": "desk", "count": 2, "functions": ids},
            {"name": "cli", "count": 1, "os": "base-os", "functions": [tools.pk], "nics": []},
        ]
        p.save(update_fields=["roles"])
        return p

    def test_560_each_vm_its_own_roles_functions(self):
        p = self.two_roles()
        self.assertEqual(p.unit_keys("desk"), ["base-os", "admin", "fail2ban"])
        self.assertEqual(p.unit_keys("cli"), ["base-os", "tools"])
        from inventory.base_deploy import RedeployError, role_of
        from inventory.models import CoreService, CoreServiceNode, Site

        svc = CoreService.objects.create(site=Site.objects.create(code="dc1"), stype="admin", name="admin", profile=p)
        a = CoreServiceNode.objects.create(service=svc, name="a", params={"role": "cli"})
        b = CoreServiceNode.objects.create(service=svc, name="b")
        self.assertEqual(role_of(a, p), "cli")
        with self.assertRaisesMessage(RedeployError, "b has no role of admin (desk, cli)"):
            role_of(b, p)
        p.roles = p.roles[:1]
        self.assertEqual(role_of(b, p), "desk")  # one role: that one

    def test_560_two_roles_may_write_one_path(self):
        p = self.two_roles()
        admin = HostService.objects.get(key="admin")
        tools = HostService.objects.get(key="tools")
        tools.configs = [dict(c) for c in admin.configs]  # the same file on the cli VM
        tools.save(update_fields=["configs"])
        out = services.preview(ServiceProfile.objects.get(pk=p.pk))
        self.assertIn("role desk", out)
        self.assertIn("role cli", out)
        self.assertNotIn("written twice", out)
        r = self.client.post(reverse("inv_sprofile_edit", args=[p.pk]), {**services.profile_initial(p), "stype": ""})
        self.assertNotContains(r, "written twice", status_code=r.status_code)

    def test_561_every_save_takes_the_base_os_lock_first(self):
        from unittest import mock

        from django.db.models.query import QuerySet

        from inventory.tests_recipes import fn_form

        real = QuerySet._fetch_all
        seen = []

        def fetch(qs):
            ran = qs._result_cache is None
            real(qs)
            if ran and qs.query.select_for_update:
                seen.append((qs.model.__name__, [getattr(x, "key", None) for x in qs._result_cache or []]))

        base = HostService.objects.get(key="base-os")
        ns = HostService.objects.get(key="ns")
        p = ServiceProfile.objects.get(key="ns-site")
        data_os = {
            "os-base-os-version": "26.04",
            "os-base-os-min_vcpu": "1",
            "os-base-os-min_ram_gb": "2",
            "os-base-os-min_disk_gb": "5",
            "os-base-os-install_template": str(base.install_template_id),
            "os-base-os-configs": services.configs_text(base),
        }
        data_p = {k: ("" if v is None else v) for k, v in services.profile_initial(p).items()}
        data_p.pop("stype")
        for post in (
            lambda: self.client.post(reverse("inv_os_save", args=["base-os"]), data_os),
            lambda: self.client.post(reverse("inv_function_save", args=[ns.pk]), fn_form(ns)),
            lambda: self.client.post(reverse("inv_sprofile_edit", args=[p.pk]), data_p),
        ):
            seen.clear()
            with mock.patch.object(QuerySet, "_fetch_all", fetch):
                post()
            self.assertEqual(seen[0], ("HostService", ["base-os"]), seen)


class Review562to565(TestCase):
    """commit-562 .. commit-565 (review of 4882687, owner 2026-11-26 12:28)."""

    def setUp(self):
        undo_late()  # T36 B5f, B5d first
        m0132().backward(apps, None)
        m0131().backward(apps, None)  # 0130 does not read roles; undone in order all the same
        m0130().backward(apps, None)
        m0131().forward(apps, None)

    def test_562_an_enable_only_edit_is_left(self):
        t = HostService.objects.get(key="graylog-server").install_template
        body = t.body.replace("  - graylog-datanode\n  - graylog-server\n", "  - graylog-server\n", 1)
        self.assertNotEqual(body, t.body)  # the enable list without graylog-datanode
        ConfigTemplate.objects.filter(pk=t.pk).update(body=body)
        m0130().forward(apps, None)
        self.assertEqual(ConfigTemplate.objects.get(pk=t.pk).body, body)  # left as it is
        self.assertIsNone(HostService.objects.get(key="mongodb").install_template_id)

    def test_562_edited_config_files_are_left(self):
        gs = HostService.objects.get(key="graylog-server")
        HostService.objects.filter(pk=gs.pk).update(configs=gs.configs[:1])
        m0130().forward(apps, None)
        self.assertIsNone(HostService.objects.get(key="graylog-datanode").install_template_id)

    def test_563_an_existing_key_of_another_kind(self):
        old = ConfigTemplate.objects.create(
            role="graylog-server-default", kind="steps", os="ubuntu", name="x", body="- name: x\n"
        )
        m0130().forward(apps, None)
        c = HostService.objects.get(key="graylog-server").configs[0]
        mine = ConfigTemplate.objects.get(pk=c["template"])
        self.assertEqual((mine.kind, mine.role), ("config", "graylog-server-default-2"))
        self.assertIn("-Xms4g -Xmx4g", mine.body)
        self.assertEqual(ConfigTemplate.objects.get(pk=old.pk).body, "- name: x\n")  # untouched
        p = ServiceProfile.objects.get(key="log-collector")
        self.assertIn("-Xms4g", recipes.render_script(p, templates_of(p), recipes.sample_values(p)))

    def test_564_both_names_taken(self):
        for r in ("mongodb-install", "mongodb-install-2", "graylog-datanode-install"):
            ConfigTemplate.objects.create(role=r, kind="config", os="ubuntu", name=r, body="x")
        m0130().forward(apps, None)
        self.assertEqual(HostService.objects.get(key="mongodb").install_template.role, "mongodb-install-3")
        self.assertEqual(
            HostService.objects.get(key="graylog-datanode").install_template.role, "graylog-datanode-install-2"
        )

    def test_565_the_reverse_restores_its_own_row(self):
        m0130().forward(apps, None)
        gs = HostService.objects.get(key="graylog-server")
        mine = gs.install_template
        other = ConfigTemplate.objects.create(
            role="my-graylog", kind="steps", os="ubuntu", name="mine", body="- name: graylog-server\n"
        )
        HostService.objects.filter(pk=gs.pk).update(install_template=other)  # reassigned by hand
        undo_late()  # T36 B5f, B5d first
        m0132().backward(apps, None)
        m0130().backward(apps, None)
        self.assertEqual(ConfigTemplate.objects.get(pk=other.pk).body, "- name: graylog-server\n")  # untouched
        self.assertIn("mongodb-org", ConfigTemplate.objects.get(pk=mine.pk).body)  # its own row, as it was
        self.assertEqual(HostService.objects.get(key="graylog-server").install_template_id, mine.pk)


class AdminTools(Base):
    """B5c (rules v2 C2/C6/C7; N3; QB5-5): admin, fail2ban and tools each their own installation."""

    def script(self, values=None):
        p = ServiceProfile.objects.get(key="admin")
        return recipes.render_script(p, templates_of(p), values or recipes.sample_values(p))

    def test_admin_fail2ban_tools(self):
        out = self.script()
        self.assertLess(out.index("Pin: origin packages.mozilla.org"), out.index('step "admin: packages"'))
        self.assertNotIn("ufw allow 55275/tcp", out)  # RDP is opened per address by fail2ban only
        self.assertIn("actionban = ufw allow from <ip> to any port 55275 proto tcp", out)
        self.assertIn("ignoreip = 127.0.0.1/8 ::1 10.61.101.21 10.201.101.21", out)  # the admin VMs only
        self.assertIn("[ssh-to-rdp]", out)
        for jail in ("maxretry = 3\nfindtime = 1h\nbantime = 50d", "maxretry = 2\nfindtime = 100d\nbantime = 500d"):
            self.assertIn(jail, out)
        self.assertLess(
            out.index('step "fail2ban: /etc/fail2ban/filter.d/ssh-to-rdp.conf"'),
            out.index('step "fail2ban: /etc/fail2ban/jail.d/10-claugine.local"'),
        )
        tools = recipes.parse_steps(HostService.objects.get(key="tools").install_template.body)[0]["packages"]
        self.assertIn("gnome-text-editor", tools)
        self.assertNotIn(
            "genisoimage",
            recipes.parse_steps(HostService.objects.get(key="admin").install_template.body)[1]["packages"],
        )

    def test_no_rdp_no_ssh_to_rdp_jail(self):
        v = recipes.sample_values()
        v["node"]["rdp_port"] = ""
        self.assertNotIn("[ssh-to-rdp]", self.script(v))

    def test_the_values_rdp_port_and_admins(self):
        from inventory.base_deploy import values_of
        from inventory.models import CoreService, CoreServiceNode, Site
        from tenants.models import PortalSettings

        PortalSettings.objects.filter(pk=PortalSettings.get().pk).update(public_rdp_port=55999)
        site = Site.objects.create(code="dc1")
        adm = CoreService.objects.create(site=site, stype="admin", name="admin")
        n = CoreServiceNode.objects.create(service=adm, name="a1", address="10.0.0.21")
        CoreServiceNode.objects.create(service=adm, name="a2", address="10.0.0.22", state="retired")
        ns = CoreService.objects.create(site=site, stype="ns", name="ns")
        m = CoreServiceNode.objects.create(service=ns, name="n1", address="10.0.0.11")
        self.assertEqual(values_of(n)["node"]["rdp_port"], 55999)  # N3: Settings → Security
        self.assertEqual(values_of(n)["site"]["admins"], ["10.0.0.21"])  # not retired, admin VMs only
        self.assertNotEqual(values_of(m)["node"]["rdp_port"], 55999)

    def test_back_then_forward_and_an_edited_admin_left(self):
        before = self.script()
        undo_late()  # T36 B5f, B5d first
        m0132().backward(apps, None)
        self.assertIsNone(HostService.objects.get(key="tools").install_template_id)
        self.assertIn("ufw allow {{ node.rdp_port }}/tcp", HostService.objects.get(key="admin").install_template.body)
        m0132().forward(apps, None)
        m0133().forward(apps, None)
        self.assertEqual(self.script(), before)
        undo_late()  # T36 B5f, B5d first
        m0132().backward(apps, None)
        t = HostService.objects.get(key="admin").install_template
        ConfigTemplate.objects.filter(pk=t.pk).update(body=t.body + "  - qemu-utils\n")
        m0132().forward(apps, None)
        m0133().forward(apps, None)
        self.assertIn("qemu-utils", ConfigTemplate.objects.get(pk=t.pk).body)  # left as it is
        self.assertIsNone(HostService.objects.get(key="tools").install_template_id)
        # commit-569: fail2ban changes only with admin - left too, the RDP port still open to all
        self.assertEqual(
            [c["path"] for c in HostService.objects.get(key="fail2ban").configs],
            ["/etc/fail2ban/jail.d/10-claugine.local"],
        )
        self.assertIn("ufw allow {{ node.rdp_port }}/tcp", ConfigTemplate.objects.get(pk=t.pk).body)


class Review566to571(Base):
    """commit-558, 560, 566 .. 571 (verifies of df9bcd4 / b333d43, review of 2a9064d; owner 2026-11-26 12:43)."""

    def setUp(self):
        self.sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(self.sa)

    def before_0129(self):
        undo_late()  # T36 B5f, B5d first
        m0132().backward(apps, None)
        m0130().backward(apps, None)
        m0131().backward(apps, None)
        m0129().backward(apps, None)

    def test_558_a_step_not_in_every_role_stops_it(self):
        from django.core.exceptions import ImproperlyConfigured

        self.before_0129()
        p = ServiceProfile.objects.get(key="admin")
        r = p.roles[0]
        ServiceProfile.objects.filter(pk=p.pk).update(
            roles=[
                {**r, "name": "a", "count": 2, "functions": ["admin"]},
                {**r, "name": "b", "count": 1, "functions": ["admin", "fail2ban"]},
            ]
        )
        with self.assertRaisesMessage(ImproperlyConfigured, "admin: its VMs of every role ran the steps fail2ban"):
            m0129().forward(apps, None)

    def test_567_a_script_goes_to_its_function_never_base_os(self):
        from django.core.exceptions import ImproperlyConfigured

        self.before_0129()
        ConfigTemplate.objects.create(role="bk-script", kind="script", os="ubuntu", name="s", body="echo backup\n")
        ServiceProfile.objects.filter(key="backup").update(
            install_template="bk-script", roles=[], services=["base-os", "backup-ds"]
        )
        m0129().forward(apps, None)
        self.assertEqual(HostService.objects.get(key="backup-ds").install_template.body, "echo backup\n")
        self.assertNotEqual(HostService.objects.get(key="base-os").install_template.body, "echo backup\n")
        m0129().backward(apps, None)
        ServiceProfile.objects.filter(key="backup").update(services=["base-os"])  # no function to give it to
        with self.assertRaisesMessage(ImproperlyConfigured, "backup: its installation bk-script is a script"):
            m0129().forward(apps, None)
        self.assertEqual(ServiceProfile.objects.get(key="backup").services, ["base-os"])  # nothing changed

    def test_566_0130_reads_what_0129_gave(self):
        self.before_0129()
        # two profiles with graylog steps, the first (by id) using the newer template
        t = ConfigTemplate.objects.get(role="logs-install")
        newer = ConfigTemplate.objects.create(role="logs2-install", kind="steps", os="ubuntu", name="x", body=t.body)
        ServiceProfile.objects.filter(key="log-collector").update(install_template=newer.role)
        lc = ServiceProfile.objects.get(key="log-collector")
        ServiceProfile.objects.create(
            key="logs-b", stype="logs", os="base-os", install_template=t.role, roles=lc.roles, configs=lc.configs, vms=1
        )
        m0129().forward(apps, None)
        m0131().forward(apps, None)
        m0130().forward(apps, None)
        self.assertTrue(HostService.objects.get(key="mongodb").install_template_id)  # split, not taken for edited

    def test_568_recidive_never_counts_rdp_grants(self):
        out = recipes.render_script(
            ServiceProfile.objects.get(key="admin"),
            templates_of(ServiceProfile.objects.get(key="admin")),
            recipes.sample_values(),
        )
        self.assertIn("filter = recidive-claugine", out)
        self.assertIn(
            "[DEFAULT]\n# recidive.conf reads it from [DEFAULT]: its regex skips the bans of these jails\n_jailname = (?:recidive|ssh-to-rdp)",
            out,
        )
        import re

        rx = r"NOTICE\s+\[(?!(?:recidive|ssh-to-rdp)\])(?:.*)\]\s+Ban\s+(\S+)"  # recidive.conf's, with ours
        self.assertIsNone(re.search(rx, "fail2ban.actions [1]: NOTICE  [ssh-to-rdp] Ban 203.0.113.9"))
        self.assertIsNotNone(re.search(rx, "fail2ban.actions [1]: NOTICE  [sshd] Ban 203.0.113.9"))

    def test_569_an_edited_fail2ban_leaves_admin_too(self):
        undo_late()  # T36 B5f, B5d first
        m0132().backward(apps, None)
        j = next(c for c in HostService.objects.get(key="fail2ban").configs if c["path"].endswith("10-claugine.local"))
        ConfigTemplate.objects.filter(pk=j["template"]).update(body="[sshd]\nenabled = true\n")
        m0132().forward(apps, None)
        m0133().forward(apps, None)
        self.assertIn("ufw allow {{ node.rdp_port }}/tcp", HostService.objects.get(key="admin").install_template.body)
        self.assertIsNone(HostService.objects.get(key="tools").install_template_id)

    def test_570_no_admin_vm_recorded_is_a_value(self):
        from inventory.base_deploy import missing
        from inventory.models import CoreService, CoreServiceNode, Site

        site = Site.objects.create(code="dc1")
        adm = CoreService.objects.create(
            site=site, stype="admin", name="admin", profile=ServiceProfile.objects.get(key="admin")
        )
        n = CoreServiceNode.objects.create(service=adm, name="a1", address="", ips=["10.0.0.21"])
        self.assertNotIn("site.admins", missing(n))

    def test_571_rdp_grants_ignore_loopback_only(self):
        out = recipes.render_script(
            ServiceProfile.objects.get(key="admin"),
            templates_of(ServiceProfile.objects.get(key="admin")),
            recipes.sample_values(),
        )
        jail = out[out.index("[ssh-to-rdp]") : out.index("[recidive]")]
        self.assertIn("ignoreip = 127.0.0.1/8 ::1\n", jail)
        self.assertNotIn("10.61.101.21", jail)
        self.assertIn("ignoreip = 127.0.0.1/8 ::1 10.61.101.21 10.201.101.21", out)  # [DEFAULT]: the punitive jails

    def two_roles(self):
        p = ServiceProfile.objects.get(key="admin")
        tools = HostService.objects.get(key="tools")
        ids = [HostService.objects.get(key=k).pk for k in ("admin", "fail2ban")]
        p.roles = [
            {**p.roles[0], "name": "desk", "count": 2, "functions": ids},
            {"name": "cli", "count": 1, "os": "base-os", "functions": [tools.pk], "nics": []},
        ]
        p.save(update_fields=["roles"])
        return p

    def test_560_new_vms_get_their_role(self):
        from inventory.models import CoreServiceNode

        p = self.two_roles()
        self.assertEqual([p.role_for(i) for i in range(4)], ["desk", "desk", "cli", None])
        n = CoreServiceNode(name="x")
        p.give_role(n, 2)
        self.assertEqual(n.params["role"], "cli")
        n.params["role"] = "desk"
        p.give_role(n, 2)
        self.assertEqual(n.params["role"], "desk")  # kept

    def test_560_a_save_gives_or_keeps_roles_and_refuses_a_lost_one(self):
        from inventory.models import CoreService, CoreServiceNode, Site

        p = ServiceProfile.objects.get(key="admin")
        svc = CoreService.objects.create(site=Site.objects.create(code="dc1"), stype="admin", name="admin", profile=p)
        a, b, c = (CoreServiceNode.objects.create(service=svc, name=f"a{i}") for i in (1, 2, 3))
        d = {k: ("" if v is None else v) for k, v in services.profile_initial(p).items()}
        d.pop("stype")
        d.pop("role_functions")  # the roles as one text (the form before 2026-11-27)
        d["roles"] = "desk 3 base-os: admin, fail2ban, tools"  # one rename first (commit-494: its NICs kept)
        self.client.post(reverse("inv_sprofile_edit", args=[p.pk]), d)
        d["roles"] = "desk 2 base-os: admin, fail2ban\ncli 1 base-os: tools"  # a second role: the VMs by their place
        self.client.post(reverse("inv_sprofile_edit", args=[p.pk]), d)
        self.assertEqual(
            [CoreServiceNode.objects.get(pk=x.pk).params.get("role") for x in (a, b, c)], ["desk", "desk", "cli"]
        )
        d["roles"] = "desk 2 base-os: admin, fail2ban\nterm 1 base-os: tools"  # one rename
        self.client.post(reverse("inv_sprofile_edit", args=[p.pk]), d)
        self.assertEqual(CoreServiceNode.objects.get(pk=c.pk).params["role"], "term")
        d["roles"] = "desk 2 base-os: admin, fail2ban\nx 1 base-os: tools\ny 0 base-os: tools"
        r = self.client.post(reverse("inv_sprofile_edit", args=[p.pk]), d)
        self.assertContains(r, "would have no role of admin")


class Review558567572573(Base):
    """commit-558, 567 (again), 572, 573 (review of 5fa434f; owner 2026-11-26 13:00)."""

    def setUp(self):
        self.sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(self.sa)

    def before_0129(self):
        undo_late()  # T36 B5f, B5d first
        m0132().backward(apps, None)
        m0130().backward(apps, None)
        m0131().backward(apps, None)
        m0129().backward(apps, None)

    def test_558_each_roles_own_order(self):
        from django.core.exceptions import ImproperlyConfigured

        self.before_0129()
        p = ServiceProfile.objects.get(key="admin")
        r = p.roles[0]
        ServiceProfile.objects.filter(pk=p.pk).update(
            roles=[
                {**r, "name": "a", "count": 2, "functions": ["admin", "fail2ban"]},
                {**r, "name": "b", "count": 1, "functions": ["fail2ban", "admin"]},
            ]
        )
        with self.assertRaisesMessage(ImproperlyConfigured, "a role lists them fail2ban, admin"):
            m0129().forward(apps, None)

    def test_567_a_function_shared_with_a_profile_that_lacked_its_step(self):
        from django.core.exceptions import ImproperlyConfigured

        self.before_0129()
        b = ServiceProfile.objects.get(key="backup")
        roles = b.roles
        roles[0]["functions"] = ["backup-ds", "fail2ban"]  # fail2ban: admin's step only - backup's VMs never ran it
        ServiceProfile.objects.filter(pk=b.pk).update(roles=roles)
        with self.assertRaisesMessage(ImproperlyConfigured, "backup: its VMs would now run fail2ban's installation"):
            m0129().forward(apps, None)
        self.assertFalse(HostService.objects.filter(install_template__isnull=False).exists())  # nothing changed

    def test_567_a_profile_with_no_installation_was_never_deployable(self):
        self.before_0129()
        ServiceProfile.objects.filter(key="backup").update(install_template="", configs=[])
        m0129().forward(apps, None)  # no stop: its VMs ran nothing
        self.assertTrue(HostService.objects.get(key="backup-ds").install_template is None)

    def test_572_service_saves_take_the_profiles_lock_first(self):
        from unittest import mock

        from django.db.models.query import QuerySet

        from inventory import core_services, core_types, core_views

        real = QuerySet._fetch_all
        seen = []

        def fetch(qs):
            ran = qs._result_cache is None
            real(qs)
            if ran and qs.query.select_for_update:
                seen.append((qs.model.__name__, [getattr(x, "key", None) for x in qs._result_cache or []]))

        for mod, call in (
            (core_types, lambda: core_types.save("dc1", "ns", mock.Mock())),
            (core_views, lambda: core_views._save(mock.Mock(), {}, None, True)),
        ):
            seen.clear()
            with (
                mock.patch.object(QuerySet, "_fetch_all", fetch),
                mock.patch.object(mod, "_save_locked", return_value=None),
            ):
                call()
            self.assertEqual(seen[0], ("HostService", ["base-os"]), seen)
        seen.clear()
        with (
            mock.patch.object(QuerySet, "_fetch_all", fetch),
            mock.patch.object(core_services, "site_vms", return_value={}),
            mock.patch.object(core_services, "_seed", return_value=[]),
        ):
            core_services.seed("dc1")
        self.assertEqual(seen[0], ("HostService", ["base-os"]), seen)

    def test_573_several_then_one_then_several(self):
        from inventory.models import CoreService, CoreServiceNode, Site

        p = ServiceProfile.objects.get(key="admin")
        svc = CoreService.objects.create(site=Site.objects.create(code="dc1"), stype="admin", name="admin", profile=p)
        a, b, c = (CoreServiceNode.objects.create(service=svc, name=f"a{i}") for i in (1, 2, 3))
        d = {k: ("" if v is None else v) for k, v in services.profile_initial(p).items()}
        d.pop("stype")
        d.pop("role_functions")  # the roles as one text (the form before 2026-11-27)
        url = reverse("inv_sprofile_edit", args=[p.pk])
        for roles in (
            "desk 3 base-os: admin, fail2ban, tools",
            "desk 2 base-os: admin, fail2ban\ncli 1 base-os: tools",
            "desk 3 base-os: admin, fail2ban, tools",  # merged: the stored roles cleared
        ):
            self.client.post(url, {**d, "roles": roles})
        self.assertEqual([CoreServiceNode.objects.get(pk=x.pk).params.get("role") for x in (a, b, c)], [None] * 3)
        r = self.client.post(url, {**d, "roles": "desk 2 base-os: admin, fail2ban\nterm 1 base-os: tools"})
        self.assertNotContains(r, "would have no role", status_code=r.status_code)
        self.assertEqual(
            [CoreServiceNode.objects.get(pk=x.pk).params.get("role") for x in (a, b, c)], ["desk", "desk", "term"]
        )


class Review567572Again(Base):
    """commit-567 / commit-572 again (verify of c143666)."""

    def test_567_a_roleless_script_profile_reads_its_services_list(self):
        from django.core.exceptions import ImproperlyConfigured

        undo_late()  # T36 B5f, B5d first

        m0132().backward(apps, None)
        m0130().backward(apps, None)
        m0131().backward(apps, None)
        m0129().backward(apps, None)
        ConfigTemplate.objects.create(role="bk-script", kind="script", os="ubuntu", name="s", body="echo b\n")
        # its script goes to backup-ds; its services list also names fail2ban, whose installation (admin's step)
        # its VMs never ran
        ServiceProfile.objects.filter(key="backup").update(
            install_template="bk-script", roles=[], services=["base-os", "backup-ds", "fail2ban"], configs=[]
        )
        with self.assertRaisesMessage(ImproperlyConfigured, "backup: its VMs would now run fail2ban's installation"):
            m0129().forward(apps, None)

    def test_572_a_rename_before_the_lock_is_seen(self):
        from unittest import mock

        from inventory import core_types

        p = ServiceProfile.objects.get(key="admin")
        stale = ServiceProfile.objects.get(pk=p.pk)
        ServiceProfile.objects.filter(pk=p.pk).update(name="renamed meanwhile")
        got = {}

        def locked(site, stype, form, svc, user, profile):
            got["p"] = profile

        with mock.patch.object(core_types, "_save_locked", locked):
            core_types.save("dc1", "admin", mock.Mock(), profile=stale)
        self.assertEqual(got["p"].name, "renamed meanwhile")  # read again under the lock
        ServiceProfile.objects.filter(pk=p.pk).delete()
        with self.assertRaisesMessage(core_types.DefinitionError, "was deleted"):
            core_types.save("dc1", "admin", mock.Mock(), profile=stale)


class Review567Third(Base):
    """commit-567 (verify of 566d0ab against df9bcd4 / 5fa434f): two profiles' different scripts for one function."""

    def test_567_two_scripts_for_one_function_stop(self):
        from django.core.exceptions import ImproperlyConfigured

        undo_late()  # T36 B5f, B5d first

        m0132().backward(apps, None)
        m0130().backward(apps, None)
        m0131().backward(apps, None)
        m0129().backward(apps, None)
        for k, body in (("a", "echo a\n"), ("b", "echo b\n")):
            ConfigTemplate.objects.create(role=f"bk-{k}", kind="script", os="ubuntu", name=k, body=body)
        ServiceProfile.objects.filter(key="backup").update(
            install_template="bk-a", roles=[], services=["base-os", "backup-ds"], configs=[]
        )
        ServiceProfile.objects.create(
            key="backup2", name="b2", os="base-os", install_template="bk-b", roles=[], services=["base-os", "backup-ds"]
        )
        with self.assertRaisesMessage(
            ImproperlyConfigured, "backup2: its backup-ds installation differs from backup's"
        ):
            m0129().forward(apps, None)
        self.assertEqual(ServiceProfile.objects.get(key="backup2").install_template, "bk-b")


class Review575(Base):
    """commit-575 (review of 7e18c16, owner 2026-11-26 13:44): 0129's checks run before its schema changes - a stop
    leaves no column behind (MySQL), and the corrected update runs."""

    def test_575_the_checks_come_first(self):
        ops = m0129().Migration.operations
        self.assertEqual([type(o).__name__ for o in ops], ["RunPython", "AddField", "AddField", "RunPython"])
        self.assertIs(ops[0].code, m0129().check)

    def test_575_a_stop_before_the_columns_then_a_retry(self):
        from django.core.exceptions import ImproperlyConfigured
        from django.db import connection
        from django.db.migrations.loader import MigrationLoader

        undo_late()  # T36 B5f, B5d first

        m0132().backward(apps, None)
        m0130().backward(apps, None)
        m0131().backward(apps, None)
        m0129().backward(apps, None)
        for k, body in (("a", "echo a\n"), ("b", "echo b\n")):
            ConfigTemplate.objects.create(role=f"bk-{k}", kind="script", os="ubuntu", name=k, body=body)
        ServiceProfile.objects.filter(key="backup").update(
            install_template="bk-a", roles=[], services=["base-os", "backup-ds"], configs=[]
        )
        ServiceProfile.objects.create(
            key="backup2", name="b2", os="base-os", install_template="bk-b", roles=[], services=["base-os", "backup-ds"]
        )
        # the models as 0128 leaves them: no install_template / configs on HostService
        old = MigrationLoader(connection).project_state(("inventory", "0128_t36_hosts_file_lock")).apps
        self.assertNotIn("configs", [f.name for f in old.get_model("inventory", "HostService")._meta.fields])
        with self.assertRaisesMessage(ImproperlyConfigured, "backup2: its backup-ds installation differs"):
            m0129().check(old, None)
        self.assertEqual(ServiceProfile.objects.get(key="backup2").install_template, "bk-b")  # nothing changed
        ServiceProfile.objects.filter(key="backup2").update(install_template="bk-a")  # corrected
        m0129().check(old, None)  # passes; it writes nothing
        self.assertFalse("b5" in (HostService.objects.get(key="base-os").legacy or {}))
        m0129().forward(apps, None)
        self.assertEqual(HostService.objects.get(key="backup-ds").install_template.body, "echo a\n")


class ClaugineFunction(TestCase):
    """T36 B5f (QB5-4 "ok"): the function claugine has its own installation, which only prepares the VM for the
    portal's installer; the portal itself is installed by B6 (init)."""

    def script(self):
        return scripts()["claugine"]

    def test_its_own_installation_after_base_os(self):
        h = HostService.objects.get(key="claugine")
        self.assertEqual(h.install_template.kind, "steps")
        steps = recipes.parse_steps(h.install_template.body)
        self.assertEqual([s["name"] for s in steps], ["claugine"])
        self.assertIn('"$(getent passwd portal | cut -d: -f6)/claugine"', steps[0]["commands"][0])
        for p in ("curl", "tar", "coreutils", "python3", "ca-certificates", "iproute2", "openssh-client", "7zip"):
            self.assertIn(p, steps[0]["packages"])
        s = self.script()
        self.assertLess(s.index('step "base-os: packages"'), s.index('step "claugine: packages"'))
        self.assertIn("runuser -u portal -- sudo -n true", s)
        self.assertIn('/claugine"', s)  # its work folder, in portal's home
        self.assertNotIn("claugine-install.sh", s)  # the portal is B6's
        self.assertNotIn("apache2", s)

    def test_back_then_forward(self):
        before = self.script()
        undo_late()
        self.assertIsNone(HostService.objects.get(key="claugine").install_template_id)
        self.assertFalse(ConfigTemplate.objects.filter(origin="0137").exists())
        m0133().forward(apps, None)
        m0137().forward(apps, None)
        m0137().forward(apps, None)  # nothing more
        self.assertEqual(ConfigTemplate.objects.filter(origin="0137").count(), 1)
        self.assertEqual(self.script(), before)

    def test_an_installation_already_there_is_left(self):
        from io import StringIO
        from unittest import mock

        m0137().backward(apps, None)
        t = ConfigTemplate.objects.create(role="own-claugine", kind="steps", os="ubuntu", name="x", body="")
        HostService.objects.filter(key="claugine").update(install_template=t)
        with mock.patch("sys.stdout", new_callable=StringIO) as out:
            m0137().forward(apps, None)
        self.assertIn("claugine has an installation already", out.getvalue())
        self.assertEqual(HostService.objects.get(key="claugine").install_template_id, t.pk)
        m0137().backward(apps, None)  # nothing of its own to undo
        self.assertEqual(HostService.objects.get(key="claugine").install_template_id, t.pk)


class Review592593(TestCase):
    """commit-592, commit-593 (review of d7e1adf, owner 2026-11-26 17:11 "fix commit-592, commit-593")."""

    def test_592_a_key_differing_only_in_case_is_taken(self):
        m0137().backward(apps, None)
        ConfigTemplate.objects.create(role="CLAUGINE-INSTALL", kind="config", os="ubuntu", name="x", body="")
        m0137().forward(apps, None)
        t = HostService.objects.get(key="claugine").install_template
        self.assertEqual(t.role, "claugine-install-2")
        self.assertEqual(t.origin, "0137")

    def test_592_the_databases_own_equality_decides(self):
        from unittest import mock

        real = ConfigTemplate.objects.filter

        def ci(*a, **kw):  # as MySQL's utf8mb4_0900_ai_ci: role= ignores case and accents
            if set(kw) == {"role"} and kw["role"] == "claugine-install":
                return real(role="clàugine-install")
            return real(*a, **kw)

        m0137().backward(apps, None)
        ConfigTemplate.objects.create(role="clàugine-install", kind="config", os="ubuntu", name="x", body="")
        with mock.patch.object(type(ConfigTemplate.objects), "filter", side_effect=ci, autospec=False):
            key = m0137()._free(ConfigTemplate, "claugine-install")
        self.assertEqual(key, "claugine-install-2")

    def test_593_a_template_another_function_uses_is_kept(self):
        from io import StringIO
        from unittest import mock

        t = HostService.objects.get(key="claugine").install_template
        HostService.objects.filter(key="backup-ds").update(install_template=t)
        with mock.patch("sys.stdout", new_callable=StringIO) as out:
            m0137().backward(apps, None)
        self.assertIn(f"template {t.role} kept - used by backup-ds", out.getvalue())
        self.assertEqual(HostService.objects.get(key="backup-ds").install_template_id, t.pk)
        self.assertIsNone(HostService.objects.get(key="claugine").install_template_id)

    def test_593_a_config_use_keeps_it_too(self):
        t = HostService.objects.get(key="claugine").install_template
        h = HostService.objects.get(key="tools")
        h.configs = list(h.configs or []) + [{"path": "/etc/x", "template": t.pk, "owner": "root:root", "mode": "0644"}]
        h.save(update_fields=["configs"])
        m0137().backward(apps, None)
        self.assertTrue(ConfigTemplate.objects.filter(pk=t.pk).exists())


class Review592Earlier(TestCase):
    """commit-592 in 0130, 0132 and 0133 too (owner 2026-11-27 07:35 "fix it for v01.01.63"): a key taken by one that
    differs only in case is not free."""

    def test_0130(self):
        undo_late()
        m0132().backward(apps, None)
        m0130().backward(apps, None)
        ConfigTemplate.objects.create(role="MONGODB-INSTALL", kind="config", os="ubuntu", name="x", body="")
        m0130().forward(apps, None)
        self.assertEqual(HostService.objects.get(key="mongodb").install_template.role, "mongodb-install-2")

    def test_0132(self):
        undo_late()
        m0132().backward(apps, None)
        ConfigTemplate.objects.create(role="Tools-Install", kind="config", os="ubuntu", name="x", body="")
        m0132().forward(apps, None)
        self.assertEqual(HostService.objects.get(key="tools").install_template.role, "tools-install-2")

    def test_0133(self):
        undo_late()
        ConfigTemplate.objects.create(role="BASE-OS-TIMESYNCD", kind="config", os="ubuntu", name="x", body="")
        m0133().forward(apps, None)
        b = HostService.objects.get(key="base-os")
        c = next(c for c in b.configs if c["path"] == m0133().PATH)
        self.assertEqual(ConfigTemplate.objects.get(pk=c["template"]).role, "base-os-timesyncd-2")

    def test_the_databases_own_equality_decides(self):
        from unittest import mock

        real = ConfigTemplate.objects.filter

        def ci(*a, **kw):  # as MySQL's utf8mb4_0900_ai_ci: role= ignores case and accents
            if set(kw) == {"role"} and kw["role"] == "tools-install":
                return real(role="tööls-install")
            return real(*a, **kw)

        ConfigTemplate.objects.create(role="tööls-install", kind="config", os="ubuntu", name="x", body="")
        for m in (m0130(), m0132(), m0133()):
            with mock.patch.object(type(ConfigTemplate.objects), "filter", side_effect=ci, autospec=False):
                self.assertEqual(m._free(set(), "tools-install", ConfigTemplate), "tools-install-2", m.__name__)


class RolesRows(Base):
    """Owner 2026-11-27 08:45/08:46 "use b": a service profile's roles a row each (name, VMs, OS) and their functions
    a row each (role, function), numbered in the order they are installed."""

    def setUp(self):
        self.client.force_login(self.staff("sa@corp.kz", is_super_admin=True))

    def data(self, p, **kw):
        d = {k: ("" if v is None else v) for k, v in services.profile_initial(p).items()}
        d.pop("stype")
        d.update(kw)
        return d

    def test_the_rows_round_trip(self):
        p = ServiceProfile.objects.get(key="cloud-gw")
        init = services.profile_initial(p)
        self.assertEqual(init["roles"], "egw ¦ 1 ¦ vyos\nigw ¦ 1 ¦ vyos")
        self.assertEqual(init["role_functions"], "egw ¦ nf-egw\nigw ¦ nf-xgw")
        page = self.client.get(reverse("inv_sprofile_edit", args=[p.pk])).content.decode()
        self.assertIn('data-numbered="1"', page)
        self.assertIn('data-options-from="id_roles"', page)

    def test_two_roles_saved_from_rows(self):
        p = ServiceProfile.objects.get(key="admin")
        self.client.post(  # one rename at a time (commit-494: its NICs kept)
            reverse("inv_sprofile_edit", args=[p.pk]),
            self.data(p, roles="desk ¦ 3 ¦ base-os", role_functions="desk ¦ admin\ndesk ¦ fail2ban\ndesk ¦ tools"),
        )
        p.refresh_from_db()
        r = self.client.post(
            reverse("inv_sprofile_edit", args=[p.pk]),
            self.data(
                p,
                roles="desk ¦ 2 ¦ base-os\r\ncli ¦ 1 ¦ base-os",
                role_functions="desk ¦ admin\r\ndesk ¦ fail2ban\r\ncli ¦ tools",
                vms=3,
            ),
            follow=True,
        )
        self.assertNotContains(r, 'class="msg error"')
        p.refresh_from_db()
        keys = dict(HostService.objects.values_list("pk", "key"))
        self.assertEqual(
            [(x["name"], x["count"], [keys[i] for i in x["functions"]]) for x in p.roles],
            [("desk", 2, ["admin", "fail2ban"]), ("cli", 1, ["tools"])],
        )

    def test_rows_that_do_not_fit_are_refused(self):
        p = ServiceProfile.objects.get(key="admin")
        before = p.roles
        url = reverse("inv_sprofile_edit", args=[p.pk])
        for roles, fns, msg in (
            ("vm ¦ 3 ¦ base-os", "desk ¦ admin", "Functions: no role desk."),
            ("vm ¦ 3 ¦ base-os", "vm ¦ admin fail2ban", "a role and one function"),
            ("vm ¦ 3 ¦ base-os", "vm ¦ admin,fail2ban,tools", "a role and one function"),  # commit-597
            ("vm ¦ 3 ¦ base-os\nvm ¦ 1 ¦ base-os", "vm ¦ admin", "Roles: vm twice."),
            ("vm ¦ 3 ¦ base-os ¦ x", "vm ¦ admin", "a name, its VMs and its OS"),
        ):
            r = self.client.post(url, self.data(p, roles=roles, role_functions=fns), follow=True)
            self.assertContains(r, msg, msg_prefix=roles)
            p.refresh_from_db()
            self.assertEqual(p.roles, before)
