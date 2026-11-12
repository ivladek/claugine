"""owner 2026-11-03 (u11): the orderer picks a base IX subnet per public prefix; "auto" lets the portal choose."""

from django.test import TestCase

from accounts.tests import make_inventory
from tenants.ordering import inet_base_subnets, plan_prefixes


class BaseSubnet(TestCase):
    def setUp(self):
        make_inventory()  # dc1.cloud reaches cloud-ix 203.0.113.0/25 (inet); 203.0.113.0/29 is a service block

    def test_base_subnets_listed_per_platform(self):
        choices, meta = inet_base_subnets()
        self.assertIn(("203.0.113.0/25", "cloud-ix · 203.0.113.0/25"), choices)
        self.assertIn("dc1.cloud", meta["203.0.113.0/25"]["platforms"])
        # the portal-ix pool belongs to an ext-managed front end, not an orderable cluster
        self.assertNotIn("198.51.100.0/26", meta)

    def test_auto_still_allocates(self):
        order = {"connections": ["inet"], "inet_main_prefix": 31, "site": "dc1"}
        plan = plan_prefixes(order, "dc1.cloud")
        self.assertEqual(plan[0][0], "inet_main")
        self.assertTrue(plan[0][1].startswith("203.0.113."))  # from the cloud-ix pool

    def test_chosen_base_subnet_is_used(self):
        order = {
            "connections": ["inet"],
            "inet_main_prefix": 30,
            "inet_main_pool": "203.0.113.0/25",
            "inet_extra_prefixes": [29],
            "inet_extra_pools": ["203.0.113.0/25"],
            "site": "dc1",
        }
        plan = dict(plan_prefixes(order, "dc1.cloud"))
        import ipaddress

        base = ipaddress.ip_network("203.0.113.0/25")
        self.assertTrue(ipaddress.ip_network(plan["inet_main"]).subnet_of(base))
        self.assertTrue(ipaddress.ip_network(plan["inet_extra"]).subnet_of(base))

    def test_request_pool_choices(self):
        # the customer "+ subnet" dialog offers the tenant's platform INET subnets, with "auto" added in the template
        from tenants.models import Organization, Tenant
        from tenants.network_views import _pool_choices

        org = Organization.objects.create(name="Acme", short_name="acme")
        t = Tenant.objects.create(
            organization=org,
            name="lab",
            title="lab",
            kind="cloud",
            approval="approved",
            order={"platform": "dc1.cloud", "site": "dc1"},
        )
        self.assertEqual(_pool_choices(t, "inet"), [("203.0.113.0/25", "cloud-ix · 203.0.113.0/25")])

    def test_rm_pool_overrides_main(self):
        # the resource manager's choice at approval (inet_pool) wins over the main prefix's ordered base subnet
        order = {"connections": ["inet"], "inet_main_prefix": 31, "inet_main_pool": "", "site": "dc1"}
        plan = plan_prefixes(order, "dc1.cloud", inet_pool="203.0.113.0/25")
        self.assertTrue(plan[0][1].startswith("203.0.113."))


class CorpnetBaseSubnet(TestCase):
    def setUp(self):
        make_inventory()  # dc1.cloud reaches corpnet-ix 10.10.0.0/22 (corpnet)

    def test_corpnet_base_subnets_listed(self):
        from tenants.ordering import corpnet_base_subnets

        choices, meta = corpnet_base_subnets()
        self.assertIn(("10.10.0.0/22", "corpnet-ix · 10.10.0.0/22"), choices)
        self.assertIn("dc1.cloud", meta["10.10.0.0/22"]["platforms"])
        self.assertNotIn("203.0.113.0/25", meta)  # that is an inet base, not corpnet

    def test_corpnet_chosen_base_used(self):
        import ipaddress

        order = {
            "connections": ["corpnet"],
            "corpnet_prefixes": [28, 29],
            "corpnet_pools": ["10.10.0.0/22", ""],
            "site": "dc1",
        }
        base = ipaddress.ip_network("10.10.0.0/22")
        for kind, cidr in plan_prefixes(order, "dc1.cloud"):
            self.assertEqual(kind, "corpnet")
            self.assertTrue(ipaddress.ip_network(cidr).subnet_of(base))

    def test_corpnet_without_pools_still_allocates(self):
        order = {"connections": ["corpnet"], "corpnet_prefixes": [28], "site": "dc1"}
        plan = plan_prefixes(order, "dc1.cloud")
        self.assertEqual(plan[0][0], "corpnet")
        self.assertTrue(plan[0][1].startswith("10.10."))


class MaxPrefixCap(TestCase):
    def setUp(self):
        make_inventory()  # dc1.cloud · cloud-ix 203.0.113.0/25 (inet)

    def test_max_prefix_caps_the_block(self):
        from inventory.models import IPSubnet, Site
        from tenants.ordering import AllocationError

        # the IP Subnets tab caps this /25 at /28 (largest block a project may be given)
        IPSubnet.objects.create(site=Site.objects.get(code="dc1"), subnet="203.0.113.0", prefix=25, max_prefix=28)
        order = {"connections": ["inet"], "inet_main_prefix": 26, "inet_main_pool": "203.0.113.0/25", "site": "dc1"}
        with self.assertRaises(AllocationError):
            plan_prefixes(order, "dc1.cloud")  # /26 is bigger than the /28 cap; it is the only inet pool
        order["inet_main_prefix"] = 28
        plan = plan_prefixes(order, "dc1.cloud")
        self.assertEqual(plan[0][0], "inet_main")  # /28 is within the cap

    def test_no_ipsubnet_means_no_cap(self):
        # make_inventory seeds no IPSubnet rows, so allocation behaves exactly as before (backward-compatible)
        order = {"connections": ["inet"], "inet_main_prefix": 26, "inet_main_pool": "203.0.113.0/25", "site": "dc1"}
        plan = plan_prefixes(order, "dc1.cloud")
        self.assertEqual(plan[0][0], "inet_main")
