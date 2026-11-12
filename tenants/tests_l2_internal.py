"""owner 2026-11-04 (u11): the one "Add L2 channel" dialog. Endpoint 1 is always a project subnet (site -> subnet);
endpoint 2 is either an external VLAN (external channel, resource-manager approval) or a project subnet on a
different site (internal channel, no approval, one per site-pair, two different sites required)."""

from django.test import TestCase
from django.urls import reverse

from accounts.models import Kind, User
from tenants.models import L2Channel, NetworkRequest, Organization, QuotaRecord, Tenant


class InternalL2(TestCase):
    def setUp(self):
        from accounts.tests import valid_bin

        self.sa = User.objects.create_user(
            "sa@corp.kz",
            "Str0ng-PASS-123",
            kind=Kind.STAFF,
            is_super_admin=True,
            is_staff=True,
            is_superuser=True,
            email_verified=True,
            totp_secret="JBSWY3DPEHPK3PXP",
            totp_enabled=True,
        )
        self.org = Organization.objects.create(name="Acme", tax_id=valid_bin(), short_name="acme")
        self.t = Tenant.objects.create(
            organization=self.org,
            name="lab",
            title="lab",
            kind="cloud",
            approval="approved",
            group_name="_acme_lab",
            order={"platform": "zk1.cloud", "site": "dc1"},
        )
        self.client.force_login(self.sa)

    def _quota(self, site, cluster):
        QuotaRecord.objects.create(
            tenant=self.t, site=site, cluster=cluster, version=1, cpu=4, ram_gb=8, gpu=0, disk_gb=50, backups_gb=0
        )

    def test_internal_rules(self):
        url = reverse("tenant_l2_add", args=[self.t.pk])
        base = {"ep2": "subnet", "network_a": "na", "network_b": "nb"}
        self._quota("dc1", "ca1")
        self.client.post(url, {**base, "site_a": "dc1", "site_b": "dc2"})
        self.assertEqual(L2Channel.objects.filter(tenant=self.t).count(), 0)  # needs resources on two sites
        self._quota("dc2", "ca9")
        self.client.post(url, {**base, "site_a": "dc1", "site_b": "dc1"})
        self.assertEqual(L2Channel.objects.filter(tenant=self.t).count(), 0)  # two different sites required
        self.client.post(url, {**base, "site_a": "dc1", "site_b": "dc2"})
        ch = L2Channel.objects.get(tenant=self.t)
        self.assertEqual((ch.kind, ch.site, ch.site_b, ch.state), ("internal", "dc1", "dc2", "planned"))
        self.assertEqual((ch.endpoint_1(), ch.endpoint_2()), ("dc1 · na", "dc2 · nb"))
        # one internal channel per pair of sites (order of sites does not matter)
        self.client.post(url, {**base, "site_a": "dc2", "site_b": "dc1", "network_a": "x", "network_b": "y"})
        self.assertEqual(L2Channel.objects.filter(tenant=self.t, kind="internal").count(), 1)

    def test_internal_alias_url(self):
        """the old internal-only endpoint still resolves to the unified view."""
        self._quota("dc1", "ca1")
        self._quota("dc2", "ca9")
        self.client.post(
            reverse("tenant_l2_internal", args=[self.t.pk]),
            {"ep2": "subnet", "site_a": "dc1", "site_b": "dc2", "network_a": "na", "network_b": "nb"},
        )
        self.assertEqual(L2Channel.objects.filter(tenant=self.t, kind="internal").count(), 1)

    def test_external_endpoint2_opens_request(self):
        """endpoint 2 = an external VLAN: no channel yet, an L2 request for the resource manager, site from endpoint 1."""
        self._quota("dc1", "ca1")
        self.client.post(
            reverse("tenant_l2_add", args=[self.t.pk]),
            {"ep2": "vlan", "name": "s2s", "site_a": "dc1", "network_a": "lab-net", "note": "peer"},
        )
        self.assertEqual(L2Channel.objects.filter(tenant=self.t).count(), 0)
        req = NetworkRequest.objects.get(tenant=self.t, kind="l2")
        self.assertEqual((req.action, req.site, req.l2_name, req.tenant_network), ("add", "dc1", "s2s", "lab-net"))

    def test_main_network_cannot_be_extended(self):
        self._quota("dc1", "ca1")
        self.client.post(
            reverse("tenant_l2_add", args=[self.t.pk]), {"ep2": "vlan", "site_a": "dc1", "network_a": "lab-main_dc1"}
        )
        self.assertFalse(NetworkRequest.objects.filter(tenant=self.t, kind="l2").exists())

    def test_external_endpoints_unchanged(self):
        ch = L2Channel.objects.create(
            tenant=self.t, site="dc1", vlan=42, tenant_network="lab-net", vni=L2Channel.next_vni()
        )
        self.assertEqual((ch.kind, ch.endpoint_1(), ch.endpoint_2()), ("external", "VLAN 42", "lab-net"))
