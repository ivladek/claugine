"""T36 B1 (owner 2026-11-24/25, rules v2, QS): the model and its migration 0122."""

import importlib

from django.apps import apps
from django.test import TestCase

from accounts.tests import Base
from inventory.models import HostService, LoggedService, OsProfile, ServiceProfile


def mig():
    return importlib.import_module("inventory.migrations.0122_t36_data")


def snapshot():
    return {
        "profiles": sorted(
            (p.key, p.stype, p.usage, tuple(p.services), p.vcpu, p.ram_gb, p.disk_gb, p.vms, str(p.nics), str(p.roles))
            for p in ServiceProfile.objects.all()
        ),
        "logged": sorted((p.key, tuple(sorted(x.name for x in p.logged.all()))) for p in ServiceProfile.objects.all()),
        "functions": sorted(
            (h.key, h.group, h.os, tuple(h.includes), tuple(h.requires), h.version) for h in HostService.objects.all()
        ),
        "os": sorted(OsProfile.objects.values_list("key", "version")),
    }


class Migration0122(TestCase):
    def test_back_then_forward_is_the_same(self):
        before = snapshot()
        mig().back(apps, None)
        self.assertFalse(ServiceProfile.objects.filter(key__in=["ns-secondary", "kvm-mgmt", "egw"]).exists())
        self.assertTrue(ServiceProfile.objects.filter(key="ns").exists())
        self.assertTrue(HostService.objects.filter(key="jump").exists())
        mig().forward(apps, None)
        self.assertEqual(snapshot(), before)

    def test_renamed_profiles_keep_their_vm_count_and_nics(self):
        mig().back(apps, None)
        ns = ServiceProfile.objects.get(key="ns")
        ns.vms, ns.nics = 2, [{"network": "core-cloud"}, {"network": "storage", "optional": True}]
        ns.save()
        mig().forward(apps, None)
        ns.refresh_from_db()
        self.assertEqual((ns.key, ns.vms, ns.nics[1]), ("ns-primary", 2, {"network": "storage", "optional": True}))
        self.assertEqual((ns.vcpu, ns.ram_gb, ns.disk_gb), (1, 2, 10))  # rules v2 sizes (Q16)

    def test_install_steps_follow_the_function_names(self):
        p = ServiceProfile.objects.get(key="admin")
        body = apps.get_model("inventory", "ConfigTemplate").objects.get(role=p.install_template).body
        self.assertIn("- name: admin\n", body)
        self.assertNotIn("- name: jump", body)
        mig().back(apps, None)
        body = apps.get_model("inventory", "ConfigTemplate").objects.get(role=p.install_template).body
        self.assertIn("- name: jump\n", body)


class OfType(TestCase):
    def test_the_types_own_profile(self):
        self.assertEqual(ServiceProfile.of_type("ns").key, "ns-primary")
        self.assertEqual(ServiceProfile.of_type("host").key, "kvm-shared")
        self.assertIsNone(ServiceProfile.of_type("nope"))
        made = ServiceProfile.objects.filter(stype="egw")
        self.assertEqual(ServiceProfile.of_type("egw", made).key, "egw")  # only made ones: the first


def pre():
    """The data before 0122 (its reverse), for the cases a fresh test database never has."""
    mig().back(apps, None)


class ReviewCdb4d89(TestCase):
    def test_462_a_profile_holding_the_new_name_moves_aside(self):
        pre()
        ServiceProfile.objects.create(key="ns-primary", stype="admin", os="base-os", name="mine")  # any other type
        ServiceProfile.objects.create(key="kvm-mgmt", stype="backup", os="base-os", name="also mine")
        before = sorted(ServiceProfile.objects.values_list("pk", "key"))
        mig().forward(apps, None)
        ns = ServiceProfile.objects.get(key="ns-primary")
        self.assertEqual((ns.stype, ns.legacy["t36"]["key"]), ("ns", "ns"))  # the old ns profile, renamed
        self.assertEqual(ServiceProfile.objects.get(key="ns-primary-own").name, "mine")
        self.assertEqual(ServiceProfile.objects.get(key="kvm-mgmt").stype, "host")  # made
        self.assertEqual(ServiceProfile.objects.get(key="kvm-mgmt-own").name, "also mine")
        mig().back(apps, None)
        self.assertEqual(sorted(ServiceProfile.objects.values_list("pk", "key")), before)

    def test_462_an_old_name_of_another_type_is_not_taken_over(self):
        pre()
        ServiceProfile.objects.filter(key="ns").update(key="ns-old")
        ServiceProfile.objects.create(key="ns", stype="backup", os="base-os", name="not a name server")
        mig().forward(apps, None)
        self.assertEqual(ServiceProfile.objects.get(key="ns").name, "not a name server")  # untouched
        self.assertTrue(ServiceProfile.objects.get(key="ns-primary").legacy["t36"].get("made"))

    def test_463_the_reverse_puts_customized_functions_back(self):
        pre()
        HostService.objects.filter(key="opennebula").update(includes=["mysql", "my-extra"], group="system")
        HostService.objects.filter(key="jump").update(includes=["desktop"], name="My jump")
        want = sorted(HostService.objects.values_list("key", "name", "group", "includes", "install"))
        mig().forward(apps, None)
        self.assertEqual(HostService.objects.get(key="one-fe").includes, ["my-extra"])
        mig().back(apps, None)
        self.assertEqual(sorted(HostService.objects.values_list("key", "name", "group", "includes", "install")), want)

    def test_464_the_machines_logged_names_stay(self):
        from inventory.models import Host, Site

        pre()
        h = Host.objects.create(site=Site.objects.create(code="dc7"), name="h7", services=["bind", "common"])
        mig().forward(apps, None)
        h.refresh_from_db()
        self.assertEqual(h.services, ["bind", "common"])


class Layouts(TestCase):
    """Owner 2026-11-25 08:39-08:43 (S1-S6, licence) and commit-465."""

    def setUp(self):
        from inventory.models import Site

        self.s1 = Site.objects.create(code="dc1")
        self.s2 = Site.objects.create(code="dc2")

    def plat(self, site, name, kind):
        from inventory import ops

        return ops.platform_save(None, site, name, {"kind": kind}, new=True)

    def test_simple_site_has_only_its_platform(self):
        from inventory.ops import EditError

        self.plat("dc1", "edge", "simple")
        for kind in ("simple", "mgmt", "shared", "ext-managed"):
            with self.assertRaisesMessage(EditError, "a simple site has no other platform"):
                self.plat("dc1", f"x{kind}", kind)
        self.plat("dc2", "edge", "simple")  # S3: another site may be simple too

    def test_dedicated_site(self):
        from inventory.ops import EditError

        with self.assertRaisesMessage(EditError, "has no mgmt platform"):
            self.plat("dc1", "cloud", "shared")
        self.plat("dc1", "mgmt", "mgmt")
        self.plat("dc1", "cloud", "shared")
        with self.assertRaisesMessage(EditError, "has its mgmt platform already: one per site"):
            self.plat("dc1", "mgmt2", "mgmt")
        with self.assertRaisesMessage(EditError, "a simple platform is a site's only platform"):
            self.plat("dc1", "edge", "simple")
        self.assertEqual(self.s1.__class__.objects.get(code="dc1").layout, "dedicated")

    def test_standard_allows_only_simple(self):
        from unittest import mock

        from inventory.ops import EditError

        with mock.patch("accounts.edition.is_standard", return_value=True):
            with self.assertRaisesMessage(EditError, "allows only simple platforms"):
                self.plat("dc1", "mgmt", "mgmt")
            self.plat("dc1", "edge", "simple")

    def test_465_cluster_kinds_follow_the_platform(self):
        from inventory import ops
        from inventory.ops import EditError

        self.plat("dc1", "edge", "simple")
        self.plat("dc2", "mgmt", "mgmt")
        self.plat("dc2", "cloud", "shared")
        with self.assertRaisesMessage(EditError, "The cluster of a simple platform is of kind simple"):
            ops.cluster_add(None, "dc1", "edge", "c_dc1", "shared", "", [])
        ops.cluster_add(None, "dc1", "edge", "c_dc1", "simple", "", [])
        with self.assertRaisesMessage(EditError, "A simple platform has exactly one cluster"):
            ops.cluster_add(None, "dc1", "edge", "d_dc1", "simple", "", [])
        with self.assertRaisesMessage(EditError, "A simple cluster is only the cluster of a simple platform"):
            ops.cluster_add(None, "dc2", "cloud", "c_dc2", "simple", "", [])
        ops.cluster_add(None, "dc2", "cloud", "c_dc2", "shared", "", [])
        with self.assertRaisesMessage(EditError, "A mgmt cluster is only the cluster of a mgmt platform"):
            ops.cluster_save(None, "dc2", "cloud", "c_dc2", kind="mgmt")
        ops.cluster_add(None, "dc2", "mgmt", "m_dc2", "mgmt", "", [])
        with self.assertRaisesMessage(EditError, "A mgmt platform has exactly one cluster"):
            ops.cluster_add(None, "dc2", "mgmt", "n_dc2", "mgmt", "", [])

    def state(self, site, name, state):
        from inventory import ops

        return ops.platform_save(None, site, name, {"state": state}, new=False)

    def test_468_retiring_never_frees_the_layout(self):
        from inventory.ops import EditError

        self.plat("dc1", "edge", "simple")
        self.state("dc1", "edge", "retired")
        with self.assertRaisesMessage(EditError, "a simple site has no other platform"):
            self.plat("dc1", "mgmt", "mgmt")
        self.state("dc1", "edge", "planned")  # back: its site is still simple
        self.plat("dc2", "mgmt", "mgmt")
        self.state("dc2", "mgmt", "retired")
        with self.assertRaisesMessage(EditError, "a simple platform is a site's only platform"):
            self.plat("dc2", "edge", "simple")
        self.plat("dc2", "mgmt2", "mgmt")  # a dedicated site may replace its retired mgmt platform
        with self.assertRaisesMessage(EditError, "has its mgmt platform already: one per site"):
            self.state("dc2", "mgmt", "planned")

    def test_466_the_form_offers_the_sites_kinds(self):
        from unittest import mock

        from inventory.edit_views import PlatformForm

        def kinds(site, **kw):
            return [c[0] for c in PlatformForm(site=site, new=True, **kw).fields["kind"].choices]

        self.assertEqual(kinds("dc1"), ["simple", "mgmt"])
        with mock.patch("accounts.edition.is_standard", return_value=True):
            self.assertEqual(kinds("dc1"), ["simple"])
        self.plat("dc1", "mgmt", "mgmt")
        self.assertEqual(kinds("dc1"), ["shared", "ext-managed"])
        self.plat("dc2", "edge", "simple")
        self.assertEqual(kinds("dc2"), [])
        self.assertIn("simple", [c[0] for c in PlatformForm(site="dc1").fields["kind"].choices])  # an edit: all


class StandardAddsSimple(Base):
    """commit-466: on Standard, a simple platform is added through the portal."""

    def test_post(self):
        from unittest import mock

        from django.urls import reverse

        from inventory.models import Platform, Site

        Site.objects.create(code="dc1")
        sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(sa)
        data = {
            "new": "1",
            "site": "dc1",
            "name": "edge",
            "kind": "simple",
            "fe_mode": "local",
            "fe_type": "host",
            "fe_node1": "n1",
            "fe_node2": "n2",
            "fe_node3": "n3",
            "fe_hostname": "n",
            "fe_vip": "192.0.2.30",
            **{f"fe_ip{i}": f"192.0.2.3{i}" for i in (1, 2, 3)},
        }
        with mock.patch("accounts.edition.is_standard", return_value=True):
            self.client.post(reverse("inv_platform_save"), data)
        self.assertEqual(Platform.objects.get(key="dc1.edge").kind, "simple")


class RollbackKeys(TestCase):
    def test_467_a_profile_holding_the_temporary_key(self):
        pre()
        ns = ServiceProfile.objects.get(key="ns")
        ServiceProfile.objects.create(key=f"t36-tmp-{ns.pk}", stype="backup", os="base-os", name="in the way")
        before = sorted(ServiceProfile.objects.values_list("pk", "key"))
        mig().forward(apps, None)
        mig().back(apps, None)
        self.assertEqual(sorted(ServiceProfile.objects.values_list("pk", "key")), before)

    def test_467_an_upper_case_original_key(self):
        pre()
        ns = ServiceProfile.objects.get(key="ns")
        ServiceProfile.objects.filter(key="admin").update(key=f"T36-TMP-{ns.pk}")  # restored before ns, in any order
        before = sorted(ServiceProfile.objects.values_list("pk", "key"))
        mig().forward(apps, None)
        mig().back(apps, None)
        self.assertEqual(sorted(ServiceProfile.objects.values_list("pk", "key")), before)


class LayoutKept(TestCase):
    """commit-468: deleting a site's only platform keeps its layout; commit-469: one rule for the form and the save."""

    def setUp(self):
        from inventory.models import Site

        Site.objects.create(code="dc1")

    def plat(self, name, kind, state=None):
        from inventory import ops

        if state:
            return ops.platform_save(None, "dc1", name, {"state": state}, new=False)
        return ops.platform_save(None, "dc1", name, {"kind": kind}, new=True)

    def test_468_delete_keeps_the_layout(self):
        from inventory.models import Platform, Site
        from inventory.ops import EditError

        self.plat("edge", "simple")
        Platform.objects.filter(key="dc1.edge").delete()
        self.assertEqual(Site.objects.get(code="dc1").layout, "simple")
        with self.assertRaisesMessage(EditError, "a simple site has no other platform"):
            self.plat("mgmt", "mgmt")
        self.plat("edge2", "simple")  # its replacement

    def test_469_a_retired_mgmt_with_a_live_shared(self):
        from inventory import ops
        from inventory.models import Site

        self.plat("mgmt", "mgmt")
        self.plat("cloud", "shared")
        self.plat("mgmt", None, state="retired")
        self.assertEqual(ops.new_platform_kinds(Site.objects.get(code="dc1")), ["mgmt"])
        self.plat("mgmt2", "mgmt")  # offered and accepted

    def test_470_a_site_read_before_its_first_platform_keeps_the_layout(self):
        from inventory.models import Site

        stale = Site.objects.get(code="dc1")  # read before the platform is added
        self.plat("edge", "simple")
        stale.city = "Almaty"
        stale.save()  # the whole (stale) row
        s = Site.objects.get(code="dc1")
        self.assertEqual((s.city, s.layout), ("Almaty", "simple"))


class RestoreKeys(TestCase):
    def test_471_a_target_freed_by_staging_stays_reserved(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        a = ServiceProfile.objects.create(key="aa", stype="backup", os="base-os", name="a")
        b = ServiceProfile.objects.create(key="bb", stype="backup", os="base-os", name="b")
        with CaptureQueriesContext(connection) as q:  # a goes back to the upper-case spelling of b's temporary key
            mig()._restore_keys([(a, f"T36-TMP-{b.pk}"), (b, "bb2")])
        self.assertFalse([x for x in q.captured_queries if f"'t36-tmp-{b.pk}'" in x["sql"]])  # skipped
        self.assertEqual(
            sorted(ServiceProfile.objects.filter(pk__in=[a.pk, b.pk]).values_list("key", flat=True)),
            sorted([f"T36-TMP-{b.pk}", "bb2"]),
        )


class SiteSave(TestCase):
    def test_472_a_partly_loaded_site_writes_only_what_it_loaded(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from inventory.models import Site

        Site.objects.create(code="dc1", city="Astana", timezone="Asia/Almaty")
        s = Site.objects.only("city").get(code="dc1")
        Site.objects.filter(code="dc1").update(timezone="Europe/Berlin")  # a change in between
        s.city = "Almaty"
        with CaptureQueriesContext(connection) as q:
            s.save()
        self.assertEqual(len(q.captured_queries), 1)  # no fetch of the deferred fields
        s = Site.objects.get(code="dc1")
        self.assertEqual((s.city, s.timezone), ("Almaty", "Europe/Berlin"))


class B2SettingsAndPlatforms(Base):
    """T36 B2: Settings → Security public ports (N3, mockup m05); OpenNebula per platform (rules v2 B4, mockup m04)."""

    def setUp(self):
        self.client.force_login(self.staff("sa@corp.kz", is_super_admin=True))

    def test_public_ports(self):
        from django.urls import reverse

        from accounts.tests import igw_block
        from tenants.models import PortalSettings

        url = reverse("portal_settings")
        s = PortalSettings.get()
        self.assertEqual((s.public_ssh_port, s.public_rdp_port), (55274, 55275))
        self.assertContains(self.client.get(url + "?tab=security"), 'name="public_ssh_port" value="55274"')
        self.client.post(url, igw_block(public_ssh_port="60022", public_rdp_port="60389"))
        s = PortalSettings.get()
        self.assertEqual((s.public_ssh_port, s.public_rdp_port), (60022, 60389))
        for ssh, rdp in (("60022", "60022"), ("22", "60389"), ("x", "1")):
            r = self.client.post(url, igw_block(public_ssh_port=ssh, public_rdp_port=rdp), follow=True)
            self.assertContains(r, "two different ports")
        s = PortalSettings.get()
        self.assertEqual((s.public_ssh_port, s.public_rdp_port), (60022, 60389))

    def test_platform_opennebula_fields(self):
        from unittest import mock

        from django.urls import reverse

        from inventory.models import Platform, Site
        from tenants.models import PortalSettings

        Site.objects.create(code="dc1")
        body = {
            "new": "1",
            "site": "dc1",
            "name": "mgmt",
            "kind": "mgmt",
            "fe_mode": "local",
            "fe_type": "vm",
            "fe_node1": "n1",
            "fe_node2": "n2",
            "fe_node3": "n3",
            "fe_hostname": "n",
            "fe_vip": "192.0.2.30",
            **{f"fe_ip{i}": f"192.0.2.3{i}" for i in (1, 2, 3)},
        }
        self.client.post(reverse("inv_platform_save"), body)
        p = Platform.objects.get(key="dc1.mgmt")
        self.assertEqual((p.one_version, p.one_ee, p.fe["type"]), (PortalSettings.get().baseline_one, False, "host"))
        with mock.patch("inventory.credentials.ref_choices", return_value=[("sec:1", "repo login")]):
            body.update({"new": "", "one_version": "7.6", "one_ee": "on", "one_login": "sec:1"})
            self.client.post(reverse("inv_platform_save"), body)
        p.refresh_from_db()
        self.assertEqual((p.one_version, p.one_ee, p.one_login), ("7.6", True, "sec:1"))


class OsPage(Base):
    """T36 B2 (mockup m01): Profiles → OS."""

    def setUp(self):
        self.client.force_login(self.staff("sa@corp.kz", is_super_admin=True))

    def test_the_page(self):
        from django.urls import reverse

        r = self.client.get(reverse("infra_os"))
        for text in ("base-os", "vyos", "Ubuntu", "Stream 2026.03", "Installation media", "Check now", "dlg-os-vyos"):
            self.assertContains(r, text)
        common = __import__("inventory.models", fromlist=["x"]).LoggedService.objects.get(name="common")
        self.assertContains(r, f'id="dlg-lg-{common.pk}"')  # its logging rules, editable here
        self.assertContains(r, 'name="next" value="/infrastructure/os/"')

    def test_save_base_os_is_the_baseline(self):
        from unittest import mock

        from django.urls import reverse

        from tenants.models import PortalSettings

        rev = PortalSettings.get().baseline_rev
        with mock.patch("accounts.timers.start") as start:
            r = self.client.post(
                reverse("inv_os_save", args=["base-os"]),
                {
                    "os-base-os-version": "28.04",
                    "os-base-os-min_vcpu": 2,
                    "os-base-os-min_ram_gb": 4,
                    "os-base-os-min_disk_gb": 8,
                },
            )
        self.assertRedirects(r, reverse("infra_os") + "?sel=os-base-os", fetch_redirect_response=False)
        s = PortalSettings.get()
        o = OsProfile.objects.get(key="base-os")
        self.assertEqual((s.baseline_os, s.baseline_rev, o.version, o.min_ram_gb), ("28.04", rev + 1, "28.04", 4))
        start.assert_called_once()
        self.client.post(
            reverse("inv_os_save", args=["base-os"]),
            {
                "os-base-os-version": "28.10",
                "os-base-os-min_vcpu": 1,
                "os-base-os-min_ram_gb": 1,
                "os-base-os-min_disk_gb": 1,
            },
        )
        self.assertEqual(PortalSettings.get().baseline_os, "28.04")  # not an LTS release: refused

    def test_save_vyos(self):
        from django.urls import reverse

        from tenants.models import PortalSettings

        self.client.post(
            reverse("inv_os_save", args=["vyos"]),
            {
                "os-vyos-version": "Stream 2026.09",
                "os-vyos-min_vcpu": 1,
                "os-vyos-min_ram_gb": 1,
                "os-vyos-min_disk_gb": 2,
                "os-vyos-media_url": "https://example.com/vyos.qcow2",
            },
        )
        o = OsProfile.objects.get(key="vyos")
        self.assertEqual((o.version, o.media_url), ("Stream 2026.09", "https://example.com/vyos.qcow2"))
        self.assertEqual(PortalSettings.get().baseline_vyos, "Stream 2026.09")

    def test_logging_defaults_and_back_to_os(self):
        from django.urls import reverse

        from tenants.models import PortalSettings

        r = self.client.post(
            reverse("inv_logging_settings"),
            {
                "log_queue_mb": 120,
                "log_retry_seconds": 20,
                "log_kept_days": 7,
                "log_size_mb": 300,
                "next": "/infrastructure/os/",
            },
        )
        self.assertRedirects(r, "/infrastructure/os/", fetch_redirect_response=False)
        s = PortalSettings.get()
        self.assertEqual((s.log_queue_mb, s.log_kept_days, s.log_size_mb), (120, 7, 300))
        r = self.client.post(
            reverse("inv_logging_settings"),
            {"log_queue_mb": 120, "log_retry_seconds": 20, "next": "https://evil.example/"},
        )
        self.assertRedirects(r, reverse("infra_functions"), fetch_redirect_response=False)


class ReviewB2(Base):
    """commit-473 .. commit-480 (reviews of 67fd889 and 1e168da)."""

    def setUp(self):
        self.client.force_login(self.staff("sa@corp.kz", is_super_admin=True))

    def test_474_the_pair_is_written_together(self):
        """commit-481: the request holds a settings copy read before another save changed the row."""
        from unittest import mock

        from django.urls import reverse

        from accounts.tests import igw_block
        from tenants.models import PortalSettings

        url = reverse("portal_settings")
        data = igw_block(public_ssh_port="60000", public_rdp_port="55275")  # this page: SSH moves to 60000
        stale = PortalSettings.get()  # (55274, 55275), as the request read it
        PortalSettings.objects.filter(pk=stale.pk).update(public_rdp_port=60000)  # another save moved RDP there
        with mock.patch.object(PortalSettings, "get", return_value=stale):
            self.client.post(url, data)
        s = PortalSettings.objects.get(pk=stale.pk)
        self.assertEqual((s.public_ssh_port, s.public_rdp_port), (60000, 55275))  # the pair as submitted, never equal

    def test_475_a_deployed_vm_front_end_keeps_its_type(self):
        from inventory import ops
        from inventory.models import Platform, Site

        st = Site.objects.create(code="dc1")
        p = Platform.objects.create(
            key="dc1.mgmt", site=st, name="mgmt", kind="mgmt", state="deployed", fe={"type": "vm"}
        )
        ops.platform_save(None, "dc1", "mgmt", {"onegate_proxy": True, "fe_type": "vm"})
        p.refresh_from_db()
        self.assertEqual(p.fe["type"], "vm")

    def test_476_a_cleared_version_takes_the_baselines(self):
        from inventory import ops
        from inventory.models import Platform, Site
        from tenants.models import PortalSettings

        st = Site.objects.create(code="dc1")
        Platform.objects.create(key="dc1.mgmt", site=st, name="mgmt", kind="mgmt", one_version="7.6")
        ops.platform_save(None, "dc1", "mgmt", {"onegate_proxy": False})  # omitted: kept
        self.assertEqual(Platform.objects.get(key="dc1.mgmt").one_version, "7.6")
        ops.platform_save(None, "dc1", "mgmt", {"one_version": ""})  # cleared: the baseline's
        self.assertEqual(Platform.objects.get(key="dc1.mgmt").one_version, PortalSettings.get().baseline_one)

    def test_477_support_sees_no_node_details(self):
        from django.urls import reverse

        self.client.force_login(self.staff("sup@corp.kz", is_support=True))
        r = self.client.get(reverse("infra_os"))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'id="hostmedia"')
        self.assertNotContains(r, "baseline_check")

    def test_478_one_version_for_both_pages(self):
        from unittest import mock

        from django.urls import reverse

        from inventory.models import OsProfile
        from tenants.models import PortalSettings

        with mock.patch("accounts.timers.start"):
            self.client.post(
                reverse("infra_baseline"),
                {
                    "section": "baseline",
                    "baseline_os": "28.04",
                    "baseline_frr": "10.6",
                    "baseline_vyos": "Stream 2026.03",
                },
            )
            self.assertEqual(OsProfile.objects.get(key="base-os").version, "28.04")
            rev = PortalSettings.get().baseline_rev
            self.client.post(  # a resource-only edit from the OS page
                reverse("inv_os_save", args=["base-os"]),
                {
                    "os-base-os-version": "28.04",
                    "os-base-os-min_vcpu": 2,
                    "os-base-os-min_ram_gb": 2,
                    "os-base-os-min_disk_gb": 5,
                },
            )
        s = PortalSettings.get()
        self.assertEqual((s.baseline_os, s.baseline_rev), ("28.04", rev))  # no old release back, no new revision
        self.assertContains(self.client.get(reverse("infra_os")), 'value="28.04"')

    def test_479_below_the_os_minimum(self):
        from inventory import services
        from inventory.models import OsProfile

        OsProfile.objects.filter(key="base-os").update(min_ram_gb=4)
        d = {
            "key": "small",
            "name": "s",
            "os": "base-os",
            "stype": "backup",
            "services": ["base-os"],
            "vcpu": 1,
            "ram_gb": 2,
            "disk_gb": 10,
        }
        with self.assertRaisesMessage(services.ServiceError, "Below the minimum of base-os"):
            services.save_profile(None, None, d, new=True)
        self.assertEqual(services.save_profile(None, None, dict(d, ram_gb=4), new=True).ram_gb, 4)

    def test_480_a_profile_without_roles_counts_by_its_os(self):
        from inventory import os_views
        from inventory.models import ServiceProfile

        ServiceProfile.objects.create(key="plain", stype="backup", os="base-os", name="p")
        self.assertIn("plain", [p.key for p in os_views._used_by("base-os")])


class Minimums(TestCase):
    """commit-479: a blank VyOS disk is checked as the 2 GB a new VM gets; an instance is never saved below the minimum."""

    def test_a_blank_vyos_disk(self):
        from inventory import services
        from inventory.models import OsProfile

        OsProfile.objects.filter(key="vyos").update(min_disk_gb=4)
        d = {
            "key": "rr-x",
            "name": "x",
            "os": "vyos",
            "stype": "bgp-rr",
            "services": [],
            "vms": 3,
            "install_template": "rr-ix",
            "configs": [],
            "vcpu": 2,
            "ram_gb": 4,
            "disk_gb": None,
        }
        with self.assertRaisesMessage(services.ServiceError, "Below the minimum of vyos"):
            services.save_profile(None, None, d, new=True)
        self.assertEqual(services.save_profile(None, None, dict(d, disk_gb=4), new=True).disk_gb, 4)

    def test_an_instance(self):
        from inventory import core_types as ct
        from inventory.models import OsProfile

        OsProfile.objects.filter(key="base-os").update(min_ram_gb=4)
        with self.assertRaisesMessage(ct.DefinitionError, "Below the minimum of base-os"):
            ct.check_minimum("ns", {"kind": "vm", "vcpu": 1, "ram_gb": 2, "disk_gb": 10})
        ct.check_minimum("ns", {"kind": "vm", "vcpu": 1, "ram_gb": 4, "disk_gb": 10})
        ct.check_minimum("host", {"kind": "host", "vcpu": None, "ram_gb": None, "disk_gb": None})


class FunctionsPage(Base):
    """T36 B2c (mockup m02): Profiles → Functions; Logging and Baseline folded."""

    def setUp(self):
        self.client.force_login(self.staff("sa@corp.kz", is_super_admin=True))

    def fn(self, key):
        return HostService.objects.get(key=key)

    def edit(self, key, **kw):
        from django.urls import reverse

        f = self.fn(key)
        data = {f"fn{f.pk}-name": f.name, f"fn{f.pk}-notes": ""}
        data.update({f"fn{f.pk}-{k}": v for k, v in kw.items()})
        return self.client.post(reverse("inv_function_save", args=[f.pk]), data, follow=True)

    def test_the_page(self):
        from django.urls import reverse

        r = self.client.get(reverse("infra_functions"))
        for text in ("kvm", "one-fe", "nf-ix", "graylog-datanode", "per platform", "dlg-fn-new", ">Functions</a>"):
            self.assertContains(r, text)
        self.assertNotContains(r, ">Logging</a>")
        self.assertNotContains(r, ">Baseline</a>")
        bind = LoggedService.objects.get(name="bind")
        self.assertContains(r, f'id="dlg-lg-{bind.pk}"')  # ns's logging rules, editable here

    def test_requires_version_and_frr(self):
        from tenants.models import PortalSettings

        self.edit("graylog-server", requires=["mongodb"], version="7.2")
        self.assertEqual((self.fn("graylog-server").requires, self.fn("graylog-server").version), (["mongodb"], "7.2"))
        r = self.edit("mongodb", requires=["graylog-server"])
        self.assertContains(r, "a loop")
        self.assertEqual(self.fn("mongodb").requires, [])
        self.edit("frr", version="10.7")
        self.assertEqual(PortalSettings.get().baseline_frr, "10.7")  # one source: the baseline's FRR

    def test_new_and_delete(self):
        from django.urls import reverse

        self.client.post(
            reverse("inv_function_new"), {"new-key": "haproxy", "new-os": "base-os", "new-name": "HAProxy"}
        )
        f = self.fn("haproxy")
        self.assertEqual((f.group, f.os), ("function", "base-os"))
        r = self.client.post(
            reverse("inv_function_new"), {"new-key": "kvm", "new-os": "base-os", "new-name": "x"}, follow=True
        )
        self.assertContains(r, "exists already")
        self.client.post(reverse("inv_function_delete", args=[f.pk]))
        self.assertFalse(HostService.objects.filter(key="haproxy").exists())
        r = self.client.post(reverse("inv_function_delete", args=[self.fn("kvm").pk]), follow=True)
        self.assertContains(r, "is used by")

    def test_logged_service_of_a_function_and_unlinked(self):
        from django.urls import reverse

        f = self.fn("one-fe")
        nxt = reverse("infra_functions") + f"?sel=fn-{f.pk}"
        r = self.client.post(
            reverse("inv_logged_new"), {"new-name": "oneflow", "new-log_type": "", "new-function": f.pk, "next": nxt}
        )
        self.assertRedirects(r, nxt, fetch_redirect_response=False)
        self.assertEqual(LoggedService.objects.get(name="oneflow").function_id, f.pk)
        orphan = LoggedService.objects.create(name="orphan", kind="base-os")
        self.assertContains(self.client.get(reverse("infra_functions")), f'id="lg-{orphan.pk}"')
        r = self.client.get(reverse("infra_logging") + f"?sel=lg-{orphan.pk}")
        self.assertRedirects(r, reverse("infra_functions") + f"#lg-{orphan.pk}", fetch_redirect_response=False)
        common = LoggedService.objects.get(name="common")
        r = self.client.get(reverse("infra_logging") + "?name=common")
        self.assertRedirects(r, reverse("infra_os") + "?sel=os-base-os", fetch_redirect_response=False)
        self.assertTrue(common.fixed)

    def test_support_reads_only(self):
        from django.urls import reverse

        self.client.force_login(self.staff("sup@corp.kz", is_support=True))
        r = self.client.get(reverse("infra_functions"))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "dlg-fn-new")
        r = self.client.post(reverse("inv_function_new"), {"new-key": "x", "new-os": "base-os", "new-name": "x"})
        self.assertFalse(HostService.objects.filter(key="x").exists())

    def test_487_frr_takes_the_baselines_release_check(self):
        from tenants.models import PortalSettings

        before = PortalSettings.get().baseline_frr
        for bad in ("stable", "10.6.1", "1000.6"):
            r = self.edit("frr", version=bad)
            self.assertContains(r, "FRR: a release as major.minor")
            self.assertEqual(PortalSettings.get().baseline_frr, before)

    def test_488_a_logged_service_only_for_a_base_os_function(self):
        from django.urls import reverse

        for fid in (self.fn("nf-ix").pk, 999999, 0):
            r = self.client.post(
                reverse("inv_logged_new"),
                {"new-name": "rr-x", "new-function": fid, "next": "/infrastructure/functions/"},
                follow=True,
            )
            self.assertContains(r, "a base-os function only")
            self.assertFalse(LoggedService.objects.filter(name="rr-x").exists())
        self.client.post(reverse("inv_logged_new"), {"new-name": "rr-x", "next": "/infrastructure/functions/"})
        self.assertIsNone(LoggedService.objects.get(name="rr-x").function)  # no id: unlinked, as before

    def test_489_the_loop_check_reads_the_graph_under_the_lock(self):
        """commit-490: in order - every function row locked, then the loop check, inside the save's own transaction
        (deeper than the test's); the test database has no row locks between connections, so the order is checked."""
        from unittest import mock

        from django.db import connection
        from django.db.models.query import QuerySet

        from inventory import function_views

        events, outer = [], tuple(connection.savepoint_ids)
        total = HostService.objects.filter(group=HostService.Group.FUNCTION).count()
        real_cycle, real_fetch = function_views._requires_cycle, QuerySet._fetch_all

        def cycle(key, want):
            events.append(("check", tuple(connection.savepoint_ids), None))
            return real_cycle(key, want)

        def fetch(qs):  # a locking query of the functions, recorded once it has run, with the rows it returned
            ran = qs._result_cache is None  # the query runs now (a later call reads its cache)
            real_fetch(qs)
            if ran and qs.model is HostService and qs.query.select_for_update and qs._result_cache is not None:
                events.append(("lock", tuple(connection.savepoint_ids), len(qs._result_cache)))

        with (
            mock.patch.object(function_views, "_requires_cycle", cycle),
            mock.patch.object(QuerySet, "_fetch_all", fetch),
        ):
            self.edit("graylog-server", requires=["mongodb"])
            r = self.edit("mongodb", requires=["graylog-server"])  # the other edit sees the first one's write
        self.assertContains(r, "a loop")
        self.assertEqual([e for e, _d, _n in events], ["lock", "check", "lock", "check"])
        self.assertTrue(all(n == total for e, _d, n in events if e == "lock"))  # every function row
        for (_l, lock, _n), (_c, check, _x) in (events[0:2], events[2:4]):
            # the savepoint the locking query ran in (deeper than the test's) is still open at the check
            self.assertGreater(len(lock), len(outer))
            self.assertEqual(check[: len(lock)], lock)


class ServicesRoles(Base):
    """T36 B2d (mockup m03): the services' roles, where they are used and placed."""

    def setUp(self):
        self.sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(self.sa)

    def post(self, key, **kw):
        from django.urls import reverse

        from inventory import services

        p = ServiceProfile.objects.get(key=key)
        data = {k: ("" if v is None else v) for k, v in services.profile_initial(p).items()}
        data.pop("stype")
        data.pop("os")
        data["logged"] = services.profile_initial(p)["logged"]
        data.update(kw)
        return self.client.post(reverse("inv_sprofile_edit", args=[p.pk]), data, follow=True)

    def test_every_profile_saves_as_it_is(self):
        for p in ServiceProfile.objects.order_by("key"):
            before = (p.roles, p.usage, p.placement)
            r = self.post(p.key)
            p.refresh_from_db()
            self.assertEqual((p.roles, p.usage, p.placement), before, p.key)
            self.assertNotContains(r, 'class="msg error"', msg_prefix=p.key)

    def test_roles_edited_and_checked(self):
        r = self.post("onefe", roles="vm 3 base-os: one-fe")
        self.assertContains(r, "needs mysql too")
        r = self.post("onefe", roles="vm 3 base-os: mysql, nf-ix")
        self.assertContains(r, "is not a function of base-os")
        r = self.post("onefe", roles="vm 3 vyos: nf-ix")
        self.assertContains(r, "the service&#x27;s OS is base-os")
        r = self.post("onefe", roles="vm 3 base-os: mysql, nope")
        self.assertContains(r, "no function nope")
        nics = ServiceProfile.objects.get(key="onefe").roles[0]["nics"]
        r = self.post("onefe", roles="vm 2 base-os: mysql, one-fe")  # commit-493: VMs 3, the roles 2
        self.assertContains(r, "its roles add up to 2")
        self.post("onefe", roles="vm 2 base-os: mysql, one-fe", vms=2, placement="mgmt platform (VMs)")
        p = ServiceProfile.objects.get(key="onefe")
        self.assertEqual(
            p.roles, [{"name": "vm", "count": 2, "os": "base-os", "functions": ["mysql", "one-fe"], "nics": nics}]
        )
        self.assertEqual(p.placement, "mgmt platform (VMs)")

    def test_an_older_form_keeps_the_roles(self):
        from django.urls import reverse

        from inventory import services

        p = ServiceProfile.objects.get(key="onefe")
        roles = p.roles
        data = {k: ("" if v is None else v) for k, v in services.profile_initial(p).items()}
        for f in ("stype", "os", "roles", "usage", "placement"):
            data.pop(f)
        self.client.post(reverse("inv_sprofile_edit", args=[p.pk]), data)
        p.refresh_from_db()
        self.assertEqual(p.roles, roles)

    def test_the_list(self):
        from django.urls import reverse

        r = self.client.get(reverse("infra_services"))
        for text in ("Roles: OS · functions", "Placed on", "vm ×3: base-os · mysql, one-fe", "egw", "l2vlan"):
            self.assertContains(r, text)

    def test_491_a_project_profile_is_not_offered_in_site_services(self):
        from inventory.core_views import add_choices
        from inventory.models import Site

        Site.objects.create(code="dc1")
        ServiceProfile.objects.filter(key="admin").update(usage="project")
        keys = [p.key for p, _w in add_choices("dc1")]
        self.assertNotIn("admin", keys)
        self.assertIn("backup", keys)

    def test_492_a_function_in_a_profiles_services_is_not_deleted(self):
        from django.urls import reverse

        f = HostService.objects.create(key="haproxy", name="HAProxy", group="function", os="base-os", legacy={})
        p = ServiceProfile.objects.get(key="onefe")  # it has roles; its services list names haproxy
        p.services = list(p.services) + ["haproxy"]
        p.save(update_fields=["services"])
        r = self.client.post(reverse("inv_function_delete", args=[f.pk]), follow=True)
        self.assertContains(r, "is used by onefe")
        self.assertTrue(HostService.objects.filter(key="haproxy").exists())

    def test_494_a_renamed_role_keeps_its_nics(self):
        nics = ServiceProfile.objects.get(key="onefe").roles[0]["nics"]
        self.assertTrue(nics)
        self.post("onefe", roles="fe 3 base-os: mysql, one-fe")
        self.assertEqual(
            ServiceProfile.objects.get(key="onefe").roles[0],
            {"name": "fe", "count": 3, "os": "base-os", "functions": ["mysql", "one-fe"], "nics": nics},
        )

    def test_493_a_vm_service_has_at_least_one_vm(self):
        r = self.post("onefe", roles="vm 0 base-os: mysql, one-fe", vms="")
        self.assertContains(r, "each role of a VM service has at least 1 VM")
        r = self.post("onefe", roles="vm 3 base-os: mysql, one-fe\nspare 0 base-os: mysql, one-fe", vms=3)
        self.assertContains(r, "spare - each role of a VM service has at least 1 VM")
        self.assertEqual(ServiceProfile.objects.get(key="onefe").vms, 3)

    def test_493_the_migration_gives_a_blank_count_its_roles(self):
        pre()
        ServiceProfile.objects.filter(key="admin").update(vms=None)
        mig().forward(apps, None)
        p = ServiceProfile.objects.get(key="admin")
        self.assertEqual((p.vms, p.roles[0]["count"]), (3, 3))

    def test_495_the_roles_total_is_bounded(self):
        r = self.post("admin", roles="vm 21 base-os: admin, fail2ban, tools", vms="")
        self.assertContains(r, "a service has 20 at most")
        self.assertEqual(ServiceProfile.objects.get(key="admin").vms, 3)

    def test_496_an_inserted_role_takes_no_nics(self):
        p = ServiceProfile.objects.get(key="cloud-gw")
        p.roles = [dict(p.roles[0], nics=[{"network": "core-vyos"}]), dict(p.roles[1], nics=[{"network": "service"}])]
        p.save(update_fields=["roles"])
        self.post("cloud-gw", roles="mon 1 vyos: nf-egw\negw 1 vyos: nf-egw\nigw 1 vyos: nf-xgw", vms=3)
        roles = {r["name"]: r["nics"] for r in ServiceProfile.objects.get(key="cloud-gw").roles}
        self.assertEqual(roles, {"mon": [], "egw": [{"network": "core-vyos"}], "igw": [{"network": "service"}]})
        self.post("cloud-gw", roles="edge 1 vyos: nf-egw\nigw 1 vyos: nf-xgw\nmon 1 vyos: nf-egw", vms=3)  # one renamed
        roles = {r["name"]: r["nics"] for r in ServiceProfile.objects.get(key="cloud-gw").roles}
        self.assertEqual(roles["edge"], [{"network": "core-vyos"}])

    def test_497_a_blank_count_takes_its_services_layout(self):
        from inventory.models import CoreService, CoreServiceNode, Site

        pre()
        ns = ServiceProfile.objects.get(key="ns")
        ns.vms = None
        ns.save(update_fields=["vms"])
        svc = CoreService.objects.create(site=Site.objects.create(code="dc1"), stype="ns", name="ns", kind="vm")
        for i in (1, 2):  # one VM per DNS address
            CoreServiceNode.objects.create(service=svc, name=f"ns{i}")
        mig().forward(apps, None)
        p = ServiceProfile.objects.get(key="ns-primary")
        self.assertEqual((p.vms, p.roles[0]["count"]), (2, 2))

    def test_497_services_of_different_sizes_defer_the_count(self):
        from inventory.models import CoreService, CoreServiceNode, Site

        pre()
        ServiceProfile.objects.filter(key="ns").update(vms=None)
        for code, n in (("dc1", 2), ("dc2", 1)):
            svc = CoreService.objects.create(site=Site.objects.create(code=code), stype="ns", name="ns", kind="vm")
            for i in range(n):
                CoreServiceNode.objects.create(service=svc, name=f"ns{i}")
        mig().forward(apps, None)
        p = ServiceProfile.objects.get(key="ns-primary")
        self.assertEqual((p.vms, p.roles), (None, []))

    def test_498_an_unlinked_reflector_counts_only_for_its_network_profile(self):
        from inventory.models import CoreService, CoreServiceNode, Network, Site

        pre()
        ServiceProfile.objects.filter(key__in=("rr-ix", "rr-vxlan")).update(vms=None)
        site = Site.objects.create(code="dc1")
        for kind, vlan, n in (("evpn", 201, 2), ("ix", 202, 3)):
            net = Network.objects.create(
                site="dc1", name=kind, kind=kind, interface="bond0", vlan=vlan, subnet=f"10.0.{vlan}.0", prefix=24
            )
            svc = CoreService.objects.create(
                site=site, stype="bgp-rr", name=f"{kind}-rrs", kind="vm", params={"network": net.pk}
            )
            for i in range(n):
                CoreServiceNode.objects.create(service=svc, name=f"{kind}-rr{i}")
        mig().forward(apps, None)
        got = {k: ServiceProfile.objects.get(key=k).vms for k in ("nf-ix", "nf-evpn")}
        self.assertEqual(got, {"nf-ix": 3, "nf-evpn": 2})

    def test_498_a_reflector_counts_only_for_the_first_profile_of_its_template(self):
        from inventory.models import CoreService, CoreServiceNode, Network, Site

        pre()
        rr = ServiceProfile.objects.get(key="rr-ix")
        first = min(ServiceProfile.objects.values_list("pk", flat=True)) - 1  # 0117 can keep an earlier one
        ServiceProfile.objects.create(
            pk=first, key="rr-old", stype="bgp-rr", os="vyos", name="kept by 0117", install_template="rr-ix", vms=2
        )
        ServiceProfile.objects.filter(pk=rr.pk).update(vms=None)
        site = Site.objects.create(code="dc1")
        net = Network.objects.create(
            site="dc1", name="ix", kind="ix", interface="bond0", vlan=202, subnet="10.0.202.0", prefix=24
        )
        svc = CoreService.objects.create(
            site=site, stype="bgp-rr", name="ix-rrs", kind="vm", params={"network": net.pk}
        )
        for i in range(2):
            CoreServiceNode.objects.create(service=svc, name=f"ix-rr{i}")
        mig().forward(apps, None)
        self.assertEqual(ServiceProfile.objects.get(pk=rr.pk).vms, 3)  # its roles' count, not the older one's service

    def test_499_a_gateway_counts_only_for_the_profile_of_its_template(self):
        from inventory.models import CoreService, CoreServiceNode, Site

        pre()
        gw = ServiceProfile.objects.get(key="cloud-gw")
        self.assertEqual(gw.install_template, "igw")
        first = min(ServiceProfile.objects.values_list("pk", flat=True)) - 1  # an earlier one on another template
        ServiceProfile.objects.create(
            pk=first, key="gw-old", stype="cloud-gw", os="vyos", name="on egw", install_template="egw", vms=2
        )
        ServiceProfile.objects.filter(pk=gw.pk).update(vms=None)
        svc = CoreService.objects.create(
            site=Site.objects.create(code="dc1"), stype="cloud-gw", name="cloud-gw", kind="vm"
        )
        for i in range(4):  # not the seeded total
            CoreServiceNode.objects.create(service=svc, name=f"gw{i}")
        mig().forward(apps, None)
        self.assertEqual(ServiceProfile.objects.get(pk=gw.pk).vms, 4)

    def test_494_two_renames_at_once_are_refused_while_they_have_nics(self):
        p = ServiceProfile.objects.get(key="cloud-gw")
        p.roles = [dict(p.roles[0], nics=[{"network": "core-vyos"}]), dict(p.roles[1], nics=[{"network": "service"}])]
        p.save(update_fields=["roles"])
        r = self.post("cloud-gw", roles="edge 1 vyos: nf-egw\ncloud 1 vyos: nf-xgw")
        self.assertContains(r, "Rename one role at a time")
        self.post("cloud-gw", roles="edge 1 vyos: nf-egw\nigw 1 vyos: nf-xgw")
        self.post("cloud-gw", roles="edge 1 vyos: nf-egw\ncloud 1 vyos: nf-xgw")
        roles = {r["name"]: r["nics"] for r in ServiceProfile.objects.get(key="cloud-gw").roles}
        self.assertEqual(roles, {"edge": [{"network": "core-vyos"}], "cloud": [{"network": "service"}]})
