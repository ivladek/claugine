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
            layouts.delete(None, "standard")
        with self.assertRaisesMessage(layouts.LayoutError, "ci1: needs"):
            layouts.save(None, "standard", "", [{"path": "/", "size_gib": 600, "fs": "xfs"}])


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
        self.client.post(
            reverse("inv_layout_edit", args=["big"]), {"title": "GPU hosts", "volumes": "/ 100 xfs\n/home -1 xfs"}
        )
        self.assertEqual(DiskLayout.objects.get(code="big").title, "GPU hosts")
        self.client.post(reverse("inv_layout_delete", args=["big"]))
        self.assertFalse(DiskLayout.objects.filter(code="big").exists())
        r = self.client.get(reverse("infra_profiles"))
        self.assertContains(r, "Hardware profiles")
        self.assertContains(r, ">Boot storage</a>")
        self.assertContains(r, "<th>Boot storage</th>")


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
        self.assertFalse(layouts.LayoutForm({"code": "new", "volumes": "/ 50"}).is_valid())
        self.assertTrue(reverse("inv_layout_edit", args=["new"]).endswith("/new/edit/"))

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
