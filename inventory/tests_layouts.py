"""Disk layouts (owner 2026-11-18, T25): the list attached to server profiles, one each; the first one made by the
migration; the checks of a layout and of a layout against a profile's boot device; the provider's part of a profile
editable when it is verified."""

from django import forms
from django.test import TestCase
from django.urls import reverse

from accounts.tests import Base
from inventory import layouts, ops
from inventory.models import DiskLayout, ServerProfile


class Checks(TestCase):
    def test_the_migration_made_the_standard_layout(self):
        lay = DiskLayout.objects.get(code="standard")
        self.assertEqual(
            [(v["path"], v["size_gib"]) for v in lay.volumes],
            [("/", 50), ("/var/lib", 200), ("/var/log", 100), ("/home", None)],
        )
        self.assertEqual(lay.problems(480), [])  # fits the hosts' 480 GB NVMe
        self.assertEqual(
            [DiskLayout.lv_name(v["path"]) for v in lay.volumes], ["lv_root", "lv_lib", "lv_log", "lv_home"]
        )

    def test_problems(self):
        lay = DiskLayout(
            code="x",
            volumes=[
                {"path": "/var", "size_gib": 10, "fs": "xfs"},
                {"path": "/home", "size_gib": None, "fs": "xfs"},
                {"path": "/srv", "size_gib": None, "fs": "xfs"},
            ],
        )
        p = lay.problems()
        self.assertIn("no / volume", p)
        self.assertIn("more than one volume takes the rest", p)
        big = DiskLayout(code="y", volumes=[{"path": "/", "size_gib": 500, "fs": "xfs"}])
        self.assertTrue(any("needs" in x for x in big.problems(480)))
        same = DiskLayout(
            code="z",
            volumes=[
                {"path": "/", "size_gib": 5, "fs": "xfs"},
                {"path": "/var/log", "size_gib": 5, "fs": "xfs"},
                {"path": "/srv/log", "size_gib": 5, "fs": "xfs"},
            ],
        )
        self.assertTrue(any("lv_log" in x for x in same.problems()))

    def test_parse(self):
        self.assertEqual(
            layouts.parse_volumes("/ 50 xfs\n/home -1\nswap 8 swap"),
            [
                {"path": "/", "size_gib": 50, "fs": "xfs"},
                {"path": "/home", "size_gib": None, "fs": "xfs"},
                {"path": "swap", "size_gib": 8, "fs": "swap"},
            ],
        )
        for bad in ("/boot 1 ext4", "var 10 xfs", "/x 10 ntfs", "/x 0 xfs"):
            with self.assertRaises(forms.ValidationError):
                layouts.parse_volumes(bad)

    def test_a_profile_takes_a_layout_that_fits(self):
        p = ServerProfile.objects.create(code="ci1", boot={"size_gb": 240})
        with self.assertRaisesMessage(ops.EditError, "needs"):
            ops.profile_save(None, "ci1", {"layout": DiskLayout.objects.get(code="standard")})
        p.boot = {"size_gb": 480}
        p.save()
        ops.profile_save(None, "ci1", {"layout": DiskLayout.objects.get(code="standard")})
        self.assertEqual(ServerProfile.objects.get(code="ci1").layout.code, "standard")

    def test_a_layout_in_use(self):
        lay = DiskLayout.objects.get(code="standard")
        ServerProfile.objects.create(code="ci1", boot={"size_gb": 480}, layout=lay)
        with self.assertRaisesMessage(layouts.LayoutError, "used by ci1"):
            layouts.delete(None, lay.pk)
        with self.assertRaisesMessage(layouts.LayoutError, "ci1: needs"):
            layouts.save(None, "standard", "", [{"path": "/", "size_gib": 600, "fs": "xfs"}], pk=lay.pk)


class Pages(Base):
    def setUp(self):
        self.sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(self.sa)

    def test_list_add_delete(self):
        r = self.client.get(reverse("infra_layouts"))
        self.assertContains(r, "standard")
        self.assertContains(r, "/var/lib")
        self.client.post(
            reverse("inv_layout_new"),
            {"code": "big", "title": "GPU", "volumes": "/ 100 xfs\n/var/lib 300 xfs\n/home -1 xfs"},
        )
        self.assertEqual(
            DiskLayout.objects.get(code="big").volumes[1], {"path": "/var/lib", "size_gib": 300, "fs": "xfs"}
        )
        big = DiskLayout.objects.get(code="big").pk
        self.client.post(  # owner 2026-11-24 08:54 (mockup 13): its Name can change, it is found by its id
            reverse("inv_layout_edit", args=[big]),
            {"code": "gpu.big", "title": "GPU hosts", "volumes": "/ 100 xfs\n/home -1 xfs"},
        )
        self.assertEqual(
            (DiskLayout.objects.get(pk=big).code, DiskLayout.objects.get(pk=big).title), ("gpu.big", "GPU hosts")
        )
        r = self.client.post(
            reverse("inv_layout_edit", args=[big]), {"code": "standard", "volumes": "/ 100 xfs"}, follow=True
        )
        self.assertContains(r, "Boot storage standard already exists.")
        self.client.post(reverse("inv_layout_delete", args=[big]))
        self.assertFalse(DiskLayout.objects.filter(pk=big).exists())
        r = self.client.get(reverse("infra_profiles"))
        self.assertContains(r, "<h1>Profiles</h1>")
        self.assertContains(r, ">Functions</a>")  # T36 B2: Servers, Boot storage, OS, Functions, Services, Templates
        self.assertNotContains(r, ">Logging</a>")
        self.assertContains(r, ">Boot storage</a>")
        self.assertContains(r, "<th>Boot storage</th>")


class ProfileHosts(Base):
    """Owner 2026-11-22 18:11 (T34): today's server profile details, then the BIOS settings and the hosts using the
    profile at the bottom."""

    def test_hosts_and_bios_at_the_bottom(self):
        from inventory.models import Cluster, Host, Platform, Site

        self.client.force_login(self.staff("sa@corp.kz", is_super_admin=True))
        st = Site.objects.create(code="dc1")
        p = ServerProfile.objects.create(code="ci1", boot={"size_gb": 240})
        h1 = Host.objects.create(site=st, name="kvm1", ip="10.0.0.1", profile=p)
        h2 = Host.objects.create(site=st, name="kvm2", ip="10.0.0.2", profile=p)
        p.check_result = {f"id:{h1.pk}": [], f"id:{h2.pk}": ["RAM 512"]}
        p.save(update_fields=["check_result"])
        Host.objects.create(site=st, name="kvm3", ip="10.0.0.3", profile=p, state="retired")
        pl = Platform.objects.create(key="dc1.cloud", site=st, name="cloud", kind="cloud", sa="secret:1")
        Cluster.objects.create(platform=pl, name="c1", kind="cloud").hosts.add(h1)
        r = self.client.get(reverse("infra_profiles"))
        html = r.content.decode()
        d = html[html.index('data-for="pr-ci1"') :]
        self.assertLess(d.index("Port aliases and bonds"), d.index("BIOS settings"))
        self.assertLess(d.index("BIOS settings"), d.index("using this profile"))
        hosts = d[d.index("using this profile") :]
        self.assertIn("?tab=hosts&amp;sel=h-kvm1", hosts)
        self.assertIn("RAM 512", hosts)  # kvm2 differs
        self.assertIn(">c1<", hosts)  # kvm1's cluster
        self.assertNotIn("kvm3", hosts)  # retired

    def test_same_name_at_two_sites(self):
        """commit-373: two hosts with one name at two sites keep their own check results."""
        from unittest import mock

        from inventory import profiles as pr
        from inventory.models import Host, Site

        self.client.force_login(self.staff("sa@corp.kz", is_super_admin=True))
        a, b = Site.objects.create(code="dc1"), Site.objects.create(code="dc2")
        p = ServerProfile.objects.create(code="ci1", boot={"size_gb": 240})
        ha = Host.objects.create(site=a, name="kvm1", ip="10.0.0.1", profile=p)
        hb = Host.objects.create(site=b, name="kvm1", ip="10.1.0.1", profile=p)

        def read(h):
            return ({"ram_gb": 1}, []) if h.pk == ha.pk else ({}, ["ssh refused"])

        with (
            mock.patch("inventory.profiles.read", side_effect=read),
            mock.patch("inventory.profiles.compare", return_value=[]),
        ):
            out = pr.check(p)
        self.assertEqual(out, {ha: [], hb: ["not read: ssh refused"]})
        p.refresh_from_db()
        self.assertEqual(set(p.check_result), {f"id:{ha.pk}", f"id:{hb.pk}"})
        d = self.client.get(reverse("infra_profiles")).content.decode()
        hosts = d[d.index('data-for="pr-ci1"') :]
        hosts = hosts[hosts.index("using this profile") :]
        self.assertIn("not read: ssh refused", hosts)  # dc2's kvm1 keeps its failure
        self.assertIn("kvm1 (dc2): not read", d)  # the Last check line names the site
        # commit-373: a result stored by name only proves no host - not even the one current host of that name
        ServerProfile.objects.filter(pk=p.pk).update(check_result={"kvm1": ["RAM 512"]})
        p.refresh_from_db()
        self.assertEqual(pr.host_results(p, [ha, hb]), {})
        self.assertEqual(pr.host_results(p, [ha]), {})
        # commit-374: its difference stays on the profile, which is "different", never "same"
        self.assertEqual(pr.unclaimed(p, [ha, hb]), [("kvm1", ["RAM 512"])])
        self.assertEqual(pr.check_state(p, [ha, hb]), "different")
        d = self.client.get(reverse("infra_profiles")).content.decode()
        self.assertIn("earlier result, no current host: kvm1: RAM 512", d)
        self.assertNotIn("all hosts the same", d)
        # a clean name-only result: the check is not complete, not "same"
        ServerProfile.objects.filter(pk=p.pk).update(check_result={"kvm1": []})
        p.refresh_from_db()
        self.assertEqual(pr.check_state(p, [ha, hb]), "incomplete")
        d = self.client.get(reverse("infra_profiles")).content.decode()
        self.assertIn("not complete", d)
        self.assertIn("not every host checked", d)
        self.assertNotIn("all hosts the same", d)
        # commit-373: a host retired after the check gives its result to no other host of the same name
        ServerProfile.objects.filter(pk=p.pk).update(check_result={f"id:{ha.pk}": ["RAM 512"], f"id:{hb.pk}": []})
        Host.objects.filter(pk=ha.pk).update(state="retired")
        p.refresh_from_db()
        hb.refresh_from_db()
        self.assertEqual(pr.host_results(p, [hb]), {hb: []})
        self.assertEqual(pr.unclaimed(p, [hb]), [("kvm1 (dc1, retired)", ["RAM 512"])])
        self.assertEqual(pr.check_state(p, [hb]), "different")


class Fixes(TestCase):
    """commit-118..121 (review of d61016f)."""

    def test_mount_points_one_spelling_and_never_under_boot(self):
        self.assertEqual(layouts.parse_volumes("/home/ -1 xfs\n/ 50")[0]["path"], "/home")
        for bad in ("/boot/ 1 ext4", "/boot/x 1 xfs", "/a/../boot 1 xfs", "//boot 1 xfs"):
            with self.assertRaises(forms.ValidationError):
                layouts.parse_volumes(bad)
        lay = DiskLayout(
            code="x",
            volumes=[
                {"path": "/", "size_gib": 5, "fs": "xfs"},
                {"path": "/home", "size_gib": 5, "fs": "xfs"},
                {"path": "/home/", "size_gib": 5, "fs": "xfs"},
                {"path": "/boot/", "size_gib": 1, "fs": "xfs"},
            ],
        )
        p = " | ".join(lay.problems())
        self.assertIn("mount point given twice: /home", p)
        self.assertIn("not a mount point a volume may have: /boot/", p)

    def test_a_profile_from_a_host_takes_a_layout_that_fits(self):
        from inventory.profiles import _fitting_layout

        self.assertEqual(_fitting_layout(480).code, "standard")
        self.assertIsNone(_fitting_layout(240))
        self.assertIsNone(_fitting_layout(0))

    def test_the_code_new_and_the_edit_address(self):
        self.assertTrue(layouts.LayoutForm({"code": "new", "volumes": "/ 50"}).is_valid())  # by id: any name
        self.assertTrue(reverse("inv_layout_edit", args=[7]).endswith("/7/edit/"))

    def test_saves_lock_the_row(self):
        from unittest import mock

        with mock.patch(
            "django.db.models.query.QuerySet.select_for_update", autospec=True, side_effect=lambda qs, *a, **k: qs
        ) as lock:
            layouts.save(
                None,
                "standard",
                "x",
                [{"path": "/", "size_gib": 50, "fs": "xfs"}, {"path": "/home", "size_gib": None, "fs": "xfs"}],
                pk=DiskLayout.objects.get(code="standard").pk,
            )
            self.assertTrue(lock.called)
            lock.reset_mock()
            ServerProfile.objects.create(code="ci9", boot={"size_gb": 480})
            ops.profile_save(None, "ci9", {"layout": DiskLayout.objects.get(code="standard")})
            self.assertTrue(lock.called)


class Fixes2(TestCase):
    """commit-120, commit-149 (owner 2026-11-19 08:04)."""

    def test_a_boot_storage_deleted_meanwhile_is_an_edit_error(self):
        gone = DiskLayout(pk=987654, code="gone", volumes=[{"path": "/", "size_gib": None, "fs": "xfs"}])
        with self.assertRaisesMessage(ops.EditError, "no longer exists"):
            ops.profile_save(None, "p1", {"layout": gone, "boot": {"size_gb": 480}}, new=True)

    def test_a_discovered_profile_takes_its_boot_storage_under_the_lock(self):
        from unittest import mock

        from inventory import profiles

        real = DiskLayout.objects.select_for_update
        with mock.patch.object(DiskLayout.objects, "select_for_update", side_effect=real) as sfu:
            self.assertEqual(profiles._fitting_layout(960).code, "standard")
        sfu.assert_called_once()
        self.assertTrue(hasattr(profiles.profile_from, "__wrapped__"))  # in one transaction with the profile's save


class S4Pages(TestCase):
    """T34 S4 (UI rules R1, R7, R8; ui-11, ui-12, ui-17): the list titles carry the count and a ? to the
    Documentation, the intro paragraphs went there; Boot storage lists the server profiles using it last; the logging
    rules' Save is on top."""

    def setUp(self):
        from accounts.models import Kind, User

        sa = User.objects.create_user(
            "sa@corp.kz",
            "Str0ng-PASS-123",
            kind=Kind.STAFF,
            email_verified=True,
            is_super_admin=True,
            totp_enabled=True,
        )
        self.client.force_login(sa)
        s = self.client.session
        s["otp_verified"] = True
        s.save()

    def test_titles_help_and_save_on_top(self):
        from django.urls import reverse

        from inventory.models import DiskLayout

        std = DiskLayout.objects.create(code="std", volumes=[{"path": "/", "size_gib": None, "fs": "ext4"}])
        servers = self.client.get(reverse("infra_profiles")).content.decode()
        self.assertIn("0 Server profiles", servers)
        self.assertIn('class="qhelp"', servers)
        self.assertNotIn("Server models of the hosts:", servers)
        boot = self.client.get(reverse("infra_layouts")).content.decode()
        from inventory.models import DiskLayout as DL

        n = DL.objects.count()  # with the seeded ones
        self.assertIn(f"{n} Boot storage" + ("" if n == 1 else "s"), boot)
        self.assertIn("Not used: it can be deleted.", boot)
        d = boot[boot.index(f'data-for="dl-{std.pk}"') :]
        d = d[: d.index("</section>\n  </div>")]
        self.assertEqual(d.count('<section class="card'), 2)  # commit-422: volumes, then server profiles - each a card
        self.assertLess(d.index(">Volumes<"), d.index(">Server profiles<"))
        logging = self.client.get(reverse("infra_os")).content.decode()  # T36 B2: the common line is on OS
        self.assertLess(logging.index('name="log_queue_mb"'), logging.index(">Save</button>"))  # one line, Save last
