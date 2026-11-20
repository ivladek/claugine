"""u10 (owner 2026-11-02): site IP Subnets — define in the tab, type auto-detected, immutable, deletable only if unused."""

from django.test import TestCase
from django.urls import reverse

from accounts.models import Kind, User
from inventory.models import IPSubnet, Network, Site


class IPSubnets(TestCase):
    def setUp(self):
        self.sa = User.objects.create_user(
            "sa@corp.kz",
            "Str0ng-PASS-123",
            kind=Kind.STAFF,
            email_verified=True,
            is_super_admin=True,
            totp_enabled=True,
        )
        self.client.force_login(self.sa)
        self.site = Site.objects.create(code="dc1")

    def add(self, **data):
        return self.client.post(reverse("ipsubnet_add", args=["dc1"]), data)

    def test_type_auto_detected(self):
        self.add(subnet="185.12.0.0", prefix="25", max_prefix="28", routing="bgp")  # public → inet
        self.add(subnet="10.20.0.0", prefix="24", max_prefix="26", routing="bgp")  # RFC1918 → corp
        pub = IPSubnet.objects.get(subnet="185.12.0.0")
        priv = IPSubnet.objects.get(subnet="10.20.0.0")
        self.assertEqual(pub.purpose, "inet")
        self.assertEqual(priv.purpose, "corpnet")
        self.assertEqual(pub.prefix_choices(), [31, 29, 28])  # 31, 29 … down to max /28, /30 skipped

    def test_static_needs_gateway(self):
        self.add(subnet="203.0.113.0", prefix="24", max_prefix="28", routing="static")
        self.assertEqual(IPSubnet.objects.filter(subnet="203.0.113.0").count(), 0)  # rejected: no gateway
        self.add(subnet="203.0.113.0", prefix="24", max_prefix="28", routing="static", static_gateway="203.0.113.1")
        self.assertEqual(IPSubnet.objects.get(subnet="203.0.113.0").static_gateway, "203.0.113.1")

    def test_max_not_larger_than_subnet(self):
        self.add(subnet="192.0.2.0", prefix="26", max_prefix="24", routing="bgp")  # /24 bigger than the /26 subnet
        self.assertEqual(IPSubnet.objects.filter(subnet="192.0.2.0").count(), 0)

    def test_max_defaults_to_ceiling_when_blank(self):
        self.add(subnet="185.12.8.0", prefix="24", max_prefix="", routing="bgp")  # left empty → defaults to the ceiling
        s = IPSubnet.objects.get(subnet="185.12.8.0")
        self.assertEqual(s.max_prefix, 26)  # owner 2026-11-03: biggest block = subnet + 2 (/24 → /26)

    def test_ceiling_rule_and_edit_max_prefix(self):
        from inventory.registry_views import max_prefix_choices

        self.add(subnet="203.0.113.0", prefix="24", max_prefix="26", routing="bgp")
        s = IPSubnet.objects.get(subnet="203.0.113.0")
        self.assertEqual(s.max_prefix_ceiling, 26)  # /24 → /26
        self.assertEqual(max_prefix_choices(s), [26, 27, 28, 29, 30, 31])  # no projects: ceiling … /31
        self.client.post(
            reverse("ipsubnet_edit", args=[s.pk]), {f"se{s.pk}-max_prefix": "28", f"se{s.pk}-routing": "bgp"}
        )
        s.refresh_from_db()
        self.assertEqual(s.max_prefix, 28)
        # below the ceiling (a bigger block than allowed) is not an offered choice → rejected
        self.client.post(
            reverse("ipsubnet_edit", args=[s.pk]), {f"se{s.pk}-max_prefix": "25", f"se{s.pk}-routing": "bgp"}
        )
        s.refresh_from_db()
        self.assertEqual(s.max_prefix, 28)

    def test_ix_subnet_edits_only_max_prefix(self):
        # owner 2026-11-03: an IX (BGP) subnet's edit form exposes only the max allowed prefix
        from inventory.registry_views import IPSubnetEditForm

        self.add(subnet="80.173.171.128", prefix="25", max_prefix="27", routing="bgp")
        s = IPSubnet.objects.get(subnet="80.173.171.128")
        f = IPSubnetEditForm(subnet=s)
        self.assertEqual(list(f.fields), ["max_prefix"])
        # a static subnet keeps routing and static gateway editable
        self.add(subnet="203.0.113.0", prefix="24", max_prefix="28", routing="static", static_gateway="203.0.113.1")
        s2 = IPSubnet.objects.get(subnet="203.0.113.0")
        self.assertEqual(set(IPSubnetEditForm(subnet=s2).fields), {"max_prefix", "routing", "static_gateway"})

    def test_ceiling_capped_at_24(self):
        self.add(subnet="100.64.0.0", prefix="16", max_prefix="24", routing="bgp")
        self.assertEqual(IPSubnet.objects.get(subnet="100.64.0.0").max_prefix_ceiling, 24)  # /16 → capped /24

    def test_delete_only_when_unused(self):
        self.add(subnet="198.51.100.0", prefix="25", max_prefix="28", routing="bgp")
        s = IPSubnet.objects.get(subnet="198.51.100.0")
        net = Network.objects.create(
            site="dc1", name="cloud-ix", kind="ix", interface="eth1", vlan=10, subnet="198.51.100.0", prefix=25
        )
        s.network = net
        s.save()
        self.client.post(reverse("ipsubnet_delete", args=[s.pk]))
        self.assertTrue(IPSubnet.objects.filter(pk=s.pk).exists())  # carried by a network: not deleted
        s.network = None
        s.save()
        self.client.post(reverse("ipsubnet_delete", args=[s.pk]))
        self.assertFalse(IPSubnet.objects.filter(pk=s.pk).exists())


class ManualAssignments(TestCase):
    """core-002 again and core-034 (fifteenth full review): the address registry's manual assignments and releases."""

    def setUp(self):
        self.sup = User.objects.create_user(
            "se@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, email_verified=True, is_support=True, totp_enabled=True
        )
        self.client.force_login(self.sup)
        self.site = Site.objects.create(code="dc1")
        self.url = reverse("infra_site", args=["dc1"]) + "?tab=subnets"

    def test_an_assignment_made_meanwhile_is_not_overlapped(self):
        """The form checked overlaps and the insert came later, with no lock: one written between them overlapped.
        The write is under the site's address lock and checked again there."""
        from unittest import mock

        from tenants.models import PrefixAssignment

        lock = mock.MagicMock()

        def meanwhile(*a, **kw):  # another assignment committed after the form's check
            PrefixAssignment.objects.create(cidr="192.0.2.0/29", site="dc1", kind="reserved", source="manual")
            lock(*a)

        with mock.patch("tenants.ordering.lock_site_addresses", side_effect=meanwhile):
            r = self.client.post(self.url, {"cidr": "192.0.2.0/28", "site": "dc1", "kind": "reserved"}, follow=True)
        lock.assert_called_once_with("dc1")  # the automatic allocator's lock
        self.assertContains(r, "Overlaps a subnet assigned meanwhile")
        self.assertFalse(PrefixAssignment.objects.filter(cidr="192.0.2.0/28").exists())
        self.client.post(self.url, {"cidr": "198.51.100.0/29", "site": "dc1", "kind": "reserved"})
        self.assertTrue(PrefixAssignment.active().filter(cidr="198.51.100.0/29").exists())

    def test_only_a_manual_assignment_is_released_here(self):
        """The page offered Release for manual assignments only, but the endpoint released any - a portal prefix
        posted directly was freed while deployed."""
        from tenants.models import PrefixAssignment

        portal = PrefixAssignment.objects.create(cidr="203.0.113.0/31", site="dc1", kind="inet_main", source="portal")
        manual = PrefixAssignment.objects.create(cidr="203.0.113.8/29", site="dc1", kind="reserved", source="manual")
        r = self.client.post(reverse("prefix_release", args=[portal.pk]), {"next": self.url}, follow=True)
        self.assertContains(r, "it is released by its network change")
        self.assertTrue(PrefixAssignment.active().filter(pk=portal.pk).exists())
        self.client.post(reverse("prefix_release", args=[manual.pk]), {"next": self.url})
        self.assertFalse(PrefixAssignment.active().filter(pk=manual.pk).exists())
