"""Backups is one figure per site, not per resource pool (owner 2026-11-09).

OpenNebula holds it as the group's backup datastore quota, which is one number per platform, so a per-pool field
let two pools of one site disagree and whichever deployed last won. The figure is now asked for once, on the
site's first pool, and an approval writes it to every pool of that site.
"""

from django.urls import reverse
from django.utils import timezone

from accounts.tests import Base, valid_bin
from tenants.models import Organization, QuotaRecord, ResourceChange, Tenant


class SiteWideBackups(Base):
    def setUp(self):
        self.sa = self.staff("sa@corp.kz", is_super_admin=True, is_resource_manager=True)
        self.org = Organization.objects.create(name='ТОО "ALPHA"', tax_id=valid_bin(), short_name="alpha")
        self.t = Tenant.objects.create(organization=self.org, name="proj", title="P", approval="approved", stage="poc")
        for site, cluster in (("zk1", "ca1"), ("zk1", "gh1"), ("at1", "ca2")):
            QuotaRecord.objects.create(
                tenant=self.t,
                site=site,
                cluster=cluster,
                version=1,
                cpu=8,
                ram_gb=32,
                gpu=0,
                disk_gb=200,
                images_gb=20,
                files_gb=1,
                backups_gb=50,
                effective_from=timezone.now(),
            )
        self.client.force_login(self.sa)

    def backups_of(self, site):
        return sorted((r.cluster, r.backups_gb) for r in self.t.current_quota_records() if r.site == site)

    def test_the_button_sits_on_the_first_pool_of_each_site(self):
        h = self.client.get(reverse("tenant_detail", args=[self.t.pk])).content.decode()
        url = reverse("tenant_backups_request", args=[self.t.pk])
        self.assertEqual(h.count(url), 2)  # zk1 and at1 - not three, the second zk1 pool carries none
        self.assertEqual(h.count("<span></span>"), 1)  # that pool keeps the empty slot so Remove stays put

    def test_the_storage_split_reads_as_a_sum(self):
        """Owner 2026-11-23 16:27 (U12, mockup p06): "=" after Storage, "+" after vms and after images, in each values row
        only; the storage bar spans the seven cells."""
        h = self.client.get(reverse("tenant_detail", args=[self.t.pk])).content.decode()
        pools = len(self.t.current_quota_records())
        self.assertEqual(h.count('<td class="op">=</td>'), pools)
        self.assertEqual(h.count('<td class="op">+</td>'), 2 * pools)
        self.assertEqual(h.count('<th class="op"></th>'), 3)
        self.assertEqual(h.count('<td colspan="7">'), pools)

    def test_a_pending_change_keeps_the_columns(self):
        """commit-423: the pending change row has the three operator cells too - each value under its own header."""
        import re

        self.client.post(reverse("tenant_backups_request", args=[self.t.pk]), {"site": "zk1", "backups_gb": 300})
        ch = ResourceChange.objects.get(tenant=self.t)
        ResourceChange.objects.filter(pk=ch.pk).update(disk_gb=900, images_gb=31, files_gb=42)  # vms: 827
        h = self.client.get(reverse("tenant_detail", args=[self.t.pk])).content.decode()
        head = h[h.index('id="resources"') :]
        head = head[head.index("<thead>") : head.index("</thead>")]
        cols = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<th[^>]*>.*?</th>", head)]
        row = h[h.index("color-mix(in srgb, var(--warn) 8%") :]
        row = row[: row.index("</tr>")]
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>.*?</td>", row, re.S)]
        cells = [""] + cells  # the first cell spans Site and Cluster
        for value, header in (
            ("900", "Storage, GB"),
            ("827", "vms"),
            ("31", "images"),
            ("42", "files"),
            ("300", "Backups, GB"),
        ):
            self.assertEqual(cols[cells.index(value)], header, value)

    def test_a_request_is_one_change_for_the_whole_site(self):
        r = self.client.post(reverse("tenant_backups_request", args=[self.t.pk]), {"site": "zk1", "backups_gb": 300})
        self.assertEqual(r.status_code, 302)
        ch = ResourceChange.objects.get(tenant=self.t)
        self.assertEqual((ch.site, ch.backups_gb, ch.pool.get("kind")), ("zk1", 300, "backups"))
        self.assertEqual((ch.cpu, ch.disk_gb), (8, 200))  # the pool's own numbers ride along unchanged

    def test_approval_writes_the_figure_to_every_pool_of_that_site(self):
        self.client.post(reverse("tenant_backups_request", args=[self.t.pk]), {"site": "zk1", "backups_gb": 300})
        ch = ResourceChange.objects.get(tenant=self.t)
        r = self.client.post(reverse("tenant_resource_decide", args=[self.t.pk, ch.pk, "approve"]), {"backups_gb": 300})
        self.assertEqual(r.status_code, 302, r.content[:300])
        ch.refresh_from_db()
        self.assertEqual(ch.state, "approved")
        self.assertEqual(self.backups_of("zk1"), [("ca1", 300), ("gh1", 300)])  # both pools move together
        self.assertEqual(self.backups_of("at1"), [("ca2", 50)])  # the other site is untouched
        for cluster in ("ca1", "gh1"):  # append-only: the old versions are still there
            self.assertEqual(QuotaRecord.objects.filter(tenant=self.t, cluster=cluster).count(), 2)

    def test_the_approver_may_change_the_figure_but_nothing_else(self):
        self.client.post(reverse("tenant_backups_request", args=[self.t.pk]), {"site": "zk1", "backups_gb": 300})
        ch = ResourceChange.objects.get(tenant=self.t)
        dlg = self.client.get(reverse("tenant_detail", args=[self.t.pk])).content.decode()
        dlg = dlg[dlg.index(f'id="dlg-rc-{ch.pk}"') :]
        dlg = dlg[: dlg.index("</dialog>")]
        self.assertIn('name="backups_gb"', dlg)
        self.assertNotIn('name="cpu"', dlg)  # a backups change is about backups and nothing else
        self.client.post(
            reverse("tenant_resource_decide", args=[self.t.pk, ch.pk, "approve"]), {"backups_gb": 120, "cpu": 999}
        )
        self.assertEqual(self.backups_of("zk1"), [("ca1", 120), ("gh1", 120)])
        self.assertTrue(all(r.cpu == 8 for r in self.t.current_quota_records()))  # the posted cpu is ignored

    def test_the_same_figure_is_not_a_change(self):
        r = self.client.post(reverse("tenant_backups_request", args=[self.t.pk]), {"site": "zk1", "backups_gb": 50})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(ResourceChange.objects.filter(tenant=self.t).exists())

    def test_a_site_the_project_has_no_pools_on_is_refused(self):
        r = self.client.post(
            reverse("tenant_backups_request", args=[self.t.pk]), {"site": "nowhere", "backups_gb": 300}
        )
        self.assertEqual(r.status_code, 302)
        self.assertFalse(ResourceChange.objects.filter(tenant=self.t).exists())
