"""GPU pools are changed in bundles (owner 2026-11-14, target-ai): a pool whose vCPU and RAM differ from standard
bundles (2 GPUs with 48 vCPU and 768 GB, a bundle being 24 vCPU and 350 GB) keeps them when its bundle count stays,
so a storage-only change is not refused; fewer bundles must still carry the cores and memory in use."""

from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from tenants import quota_check
from tenants.forms import PoolOrderForm, ResourceChangeForm

H200 = {"cores": 24, "ram_gb": 350, "model": "NVIDIA H200 NVL", "vram_gb": 142, "per_host": 8, "free": 0}


def pool_rec(gpu=2, cpu=48, ram_gb=768, disk_gb=5189):
    return SimpleNamespace(
        pk=7,
        site="at1",
        cluster="gi2",
        gpu=gpu,
        cpu=cpu,
        ram_gb=ram_gb,
        disk_gb=disk_gb,
        images_gb=0,
        files_gb=0,
        backups_gb=0,
    )


class GpuBundleFloor(SimpleTestCase):
    def floor(self, rec, used):
        tenant = SimpleNamespace(current_quota_records=lambda: [rec])
        use = {rec.pk: used, f"site:{rec.site}": {"images_gb": 0, "files_gb": 0, "backups_gb": 0}}
        with mock.patch.object(quota_check, "usage", return_value=use):
            return quota_check.floor(tenant, rec, H200)

    def test_the_pools_own_bundles_are_not_refused(self):
        """768 GB in use needs 3 standard bundles, but the pool's own 2 keep its 768 GB: the floor is 2, not 3."""
        f = self.floor(pool_rec(), {"cpu": 48, "ram_gb": 768, "gpu": 2, "vm_disk_gb": 4340})
        self.assertEqual(f["gpu"], 2)

    def test_fewer_bundles_must_carry_the_use(self):
        """A pool of 4 bundles using 768 GB cannot go below 3: 2 bundles would be 700 GB."""
        f = self.floor(pool_rec(gpu=4, cpu=96, ram_gb=1400), {"cpu": 48, "ram_gb": 768, "gpu": 2, "vm_disk_gb": 0})
        self.assertEqual(f["gpu"], 3)

    def test_the_gpus_in_use_stay_the_least(self):
        f = self.floor(pool_rec(gpu=4, cpu=96, ram_gb=1400), {"cpu": 0, "ram_gb": 0, "gpu": 3, "vm_disk_gb": 0})
        self.assertEqual(f["gpu"], 3)


class GpuBundleValues(SimpleTestCase):
    def values(self, data, rec, free=0):
        pool = {"gpu": dict(H200, free=free)}
        form = ResourceChangeForm(data, pool=pool, floor={"gpu": 2, "disk_gb": 4340}, have=rec.gpu)
        self.assertTrue(form.is_valid(), form.errors)
        return form.values(rec)

    def test_storage_only_keeps_vcpu_and_ram(self):
        v = self.values({"gpu": 2, "disk_gb": 5000}, pool_rec())
        self.assertEqual((v["gpu"], v["cpu"], v["ram_gb"], v["disk_gb"]), (2, 48, 768, 5000))

    def test_other_bundles_are_counted_per_bundle(self):
        v = self.values({"gpu": 3, "disk_gb": 5000}, pool_rec(), free=1)
        self.assertEqual((v["gpu"], v["cpu"], v["ram_gb"]), (3, 72, 1050))

    def test_the_form_takes_the_pools_own_bundle_count(self):
        """Before the fix the field's floor was 3 for this pool: "2" was refused even with nothing else changed."""
        form = ResourceChangeForm({"gpu": 2, "disk_gb": 5000}, pool={"gpu": H200}, floor={"gpu": 2, "disk_gb": 4340})
        self.assertTrue(form.is_valid(), form.errors)
        form = ResourceChangeForm({"gpu": 2, "disk_gb": 5000}, pool={"gpu": H200}, floor={"gpu": 3, "disk_gb": 4340})
        self.assertFalse(form.is_valid())


class GpuBundleList(SimpleTestCase):
    """Owner 2026-11-15: bundles are chosen only from a list, no more than the pool has plus the cluster's free ones."""

    def choices(self, have, free, floor=None):
        form = ResourceChangeForm(pool={"gpu": dict(H200, free=free)}, floor=floor, have=have)
        return [v for v, _ in form.fields["gpu"].choices]

    def test_the_list_is_the_pools_bundles_and_the_free_ones(self):
        self.assertEqual(self.choices(2, 3), [1, 2, 3, 4, 5])

    def test_the_list_starts_at_the_bundles_in_use(self):
        self.assertEqual(self.choices(4, 1, floor={"gpu": 3}), [3, 4, 5])

    def test_more_than_free_is_refused(self):
        form = ResourceChangeForm({"gpu": 4, "disk_gb": 100}, pool={"gpu": dict(H200, free=1)}, have=2)
        self.assertFalse(form.is_valid())
        self.assertIn("gpu", form.errors)

    def test_the_field_is_marked_for_the_page(self):
        form = ResourceChangeForm(pool={"gpu": dict(H200, free=1)}, have=2)
        self.assertIn("data-pool-bundles", str(form["gpu"]))
        self.assertIn("data-pool-disk", str(form["disk_gb"]))
        self.assertIn("<select", str(form["gpu"]))

    def test_a_cpu_pool_has_no_bundles(self):
        form = ResourceChangeForm(pool={"gpu": None})
        self.assertNotIn("gpu", form.fields)


class PoolOrderBundles(SimpleTestCase):
    """Ordering a GPU pool for a project: no more bundles than the cluster has free; the storage follows the bundles
    when the bundle defines it (owner 2026-11-15)."""

    def form(self, data, storage_gb=0):
        pool = {
            "key": "at1:gi2",
            "site": "at1",
            "quota_code": "gi2",
            "label": "gi2",
            "gpu": dict(H200, free=2, storage_gb=storage_gb),
        }
        tenant = SimpleNamespace(order={"site": "at1"}, current_quota_records=lambda: [])
        with (
            mock.patch("tenants.ordering.resource_pools", return_value=[pool]),
            mock.patch("tenants.ordering.inet_base_subnets", return_value=([], {})),
            mock.patch("tenants.ordering.corpnet_base_subnets", return_value=([], {})),
            mock.patch("tenants.ordering.prefix_lengths_for", return_value=[28]),
            mock.patch("tenants.ordering.connections", return_value=[]),
        ):
            form = PoolOrderForm(dict({"pool": "at1:gi2", "igw_type": "vyos"}, **data), tenant=tenant)
            form.is_valid()
        return form

    def test_more_than_free_is_refused(self):
        self.assertIn("gpu", self.form({"gpu": 3, "disk_gb": 100}).errors)

    def test_the_free_bundles_are_taken(self):
        self.assertNotIn("gpu", self.form({"gpu": 2, "disk_gb": 100}).errors)

    def test_the_storage_follows_the_bundles(self):
        form = self.form({"gpu": 2}, storage_gb=500)
        self.assertNotIn("disk_gb", form.errors)
        self.assertEqual(form.cleaned_data["disk_gb"], 1000)

    def test_the_fields_are_marked_for_the_page(self):
        form = self.form({})
        self.assertIn("data-pool-select", str(form["pool"]))
        self.assertIn("data-pool-bundles", str(form["gpu"]))
