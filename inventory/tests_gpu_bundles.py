"""owner 2026-11-09 (job gpu-bundles): the GPU bundle is a setting of the cluster, not a read-time calculation.

A bundle is one GPU card with a fixed share of its host's cores and RAM. The share is stored so staff can set it;
until they do, the hardware's default stands. The reserve percentages and ``vcpu_ratio`` do not apply to a GPU
cluster: the head-room is already inside the bundle.
"""
from django.test import TestCase

from inventory.models import Cluster, Host, Platform, ServerProfile, Site


_n = [0]


def cluster_with(hosts, spare=0, **capacity):
    """A cluster of (cores, ram_gb, gpus) hosts. `hosts` is a list of those triples.

    Everything is named per call, so one test can build two clusters without colliding on a unique field.
    """
    _n[0] += 1
    k = _n[0]
    st, _ = Site.objects.get_or_create(code="dc1")
    plat = Platform.objects.create(key=f"dc1.one{k}", site=st, name=f"one{k}", kind="cloud", sa="secret:1")
    c = Cluster.objects.create(platform=plat, name=f"gpu{k}_dc1", kind="shared", spare_hosts=spare, **capacity)
    for i, (cores, ram, gpus) in enumerate(hosts):
        p = ServerProfile.objects.create(code=f"p{k}-{i}", sockets=2, cores_per_socket=cores // 2,
                                         ram_gb=ram, gpu_count=gpus, gpu_model="H200", gpu_vram_gb=141)
        p.cores = cores
        p.save(update_fields=["cores"])
        c.hosts.add(Host.objects.create(site=st, name=f"h{k}-{i}", ip=f"10.{k}.0.{10 + i}", profile=p, state="active"))
    return c


class DefaultBundle(TestCase):
    def test_one_host_divided_by_its_own_cards(self):
        """64 cores and 1024 GB over 8 cards, less 10%: 7 vCores and 112 GB (RAM down to a multiple of 8)."""
        c = cluster_with([(64, 1024, 8)])
        self.assertEqual(c.default_bundle(), {"cores": 7, "ram_gb": 112})

    def test_the_smallest_host_decides_on_a_mixed_cluster(self):
        """A GPU is passed through from the machine it sits in, so a bundle has to fit on one host.

        A cluster average would promise more than the dense host can give: here 96 cores over 10 cards is 9.6,
        while the dense host only has 8 cores per card.
        """
        c = cluster_with([(64, 1024, 8), (32, 512, 2)])
        self.assertEqual(c.default_bundle()["cores"], 7)   # 64/8*0.9, not 96/10*0.9

    def test_no_gpu_hosts_means_no_bundle(self):
        self.assertEqual(cluster_with([(64, 512, 0)]).default_bundle(), {"cores": 0, "ram_gb": 0})
        self.assertFalse(cluster_with([(64, 512, 0)]).has_gpu())

    def test_a_cluster_with_no_hosts_does_not_raise(self):
        c = cluster_with([])
        self.assertEqual(c.default_bundle(), {"cores": 0, "ram_gb": 0})
        self.assertEqual(c.bundle(), {"cores": 0, "ram_gb": 0})


class StoredBundleWins(TestCase):
    def test_what_staff_set_is_what_is_used(self):
        c = cluster_with([(64, 1024, 8)])
        c.bundle_cores, c.bundle_ram_gb = 6, 96
        c.save(update_fields=["bundle_cores", "bundle_ram_gb"])
        self.assertEqual(c.bundle(), {"cores": 6, "ram_gb": 96})
        self.assertEqual(c.default_bundle(), {"cores": 7, "ram_gb": 112})  # the suggestion still shows beside it

    def test_unset_falls_back_to_the_hardware(self):
        c = cluster_with([(64, 1024, 8)])
        self.assertEqual(c.bundle(), c.default_bundle())

    def test_the_stored_value_does_not_follow_the_hardware(self):
        """Hardware changes must not move a bundle under a running project; staff see the new default and decide."""
        c = cluster_with([(64, 1024, 8)])
        c.bundle_cores, c.bundle_ram_gb = 7, 112
        c.save(update_fields=["bundle_cores", "bundle_ram_gb"])
        p = ServerProfile.objects.create(code="dense", sockets=2, cores_per_socket=16, ram_gb=512, gpu_count=8)
        p.cores = 32
        p.save(update_fields=["cores"])
        c.hosts.add(Host.objects.create(site=c.platform.site, name="hx", ip="10.0.0.99", profile=p, state="active"))
        self.assertEqual(c.bundle()["cores"], 7)           # unchanged
        self.assertEqual(c.default_bundle()["cores"], 3)   # 32/8*0.9 — the suggestion moved


class AvailableCards(TestCase):
    def test_spare_hosts_are_not_available_to_order(self):
        """A spare exists to absorb a failure, so what it carries cannot be sold (owner 2026-11-09)."""
        c = cluster_with([(64, 1024, 8), (64, 1024, 8), (64, 1024, 8)], spare=1)
        self.assertEqual(c.gpu_cards(), {"total": 24, "spare": 8, "available": 16})

    def test_no_spare_means_everything_is_available(self):
        c = cluster_with([(64, 1024, 8), (64, 1024, 8)], spare=0)
        self.assertEqual(c.gpu_cards()["available"], 16)

    def test_more_spares_than_hosts_does_not_go_negative(self):
        c = cluster_with([(64, 1024, 8)], spare=5)
        self.assertEqual(c.gpu_cards()["available"], 0)

    def test_only_active_hosts_count(self):
        c = cluster_with([(64, 1024, 8), (64, 1024, 8)])
        h = c.hosts.first()
        h.state = "retired"
        h.save(update_fields=["state"])
        self.assertEqual(c.gpu_cards()["total"], 8)


class SavingTheForm(TestCase):
    """Posting the cluster form, which is where the fields the form drops have to be survived.

    `shape_for` removes `vcpu_ratio` and the two reserve percentages for a GPU cluster, so a save path that reads
    `cleaned_data["vcpu_ratio"]` raises KeyError and the page 500s. Found in use on 2026-11-09, not by the suite:
    nothing posted this form for a GPU cluster.
    """

    def setUp(self):
        import pyotp
        from accounts.models import Kind, User
        u = User.objects.create_user("sa@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_super_admin=True,
                                     email_verified=True, totp_enabled=True, totp_secret=pyotp.random_base32())
        self.client.force_login(u)

    def _post(self, c, **extra):
        from django.urls import reverse
        url = reverse("inv_cluster_save", args=[c.platform.site.code, c.platform.key.split(".", 1)[1], c.name])
        body = {"kind": c.kind, "datastores": [], "networks": [], "spare_hosts": c.spare_hosts, **extra}
        return self.client.post(url, body)

    def test_a_gpu_cluster_saves_its_bundle(self):
        c = cluster_with([(64, 1024, 8)])
        r = self._post(c, bundle_cores=6, bundle_ram_gb=96)
        self.assertIn(r.status_code, (200, 302))
        c.refresh_from_db()
        self.assertEqual((c.bundle_cores, c.bundle_ram_gb), (6, 96))
        self.assertEqual(c.bundle(), {"cores": 6, "ram_gb": 96})

    def test_a_cpu_cluster_still_saves_its_ratio_and_reserves(self):
        c = cluster_with([(64, 1024, 0)])
        r = self._post(c, vcpu_ratio=8, reserved_cpu_pct=7, reserved_ram_pct=9)
        self.assertIn(r.status_code, (200, 302))
        c.refresh_from_db()
        self.assertEqual((c.vcpu_ratio, c.reserved_cpu_pct, c.reserved_ram_pct), (8, 7, 9))




class GpuClusterDetails(TestCase):
    """Owner 2026-11-09: the GPU type and the bundle belong on the cluster's details."""

    def test_the_model_is_read_from_the_hosts(self):
        c = cluster_with([(64, 1024, 8)])
        self.assertEqual(c.gpu_model(), "H200")

    def test_a_cluster_with_no_cards_names_no_model(self):
        c = cluster_with([(64, 1024, 0)])
        self.assertEqual(c.gpu_model(), "")

    def test_the_details_show_the_model_and_the_bundle(self):
        import pyotp
        from accounts.models import Kind, User
        from django.urls import reverse
        u = User.objects.create_user("sa3@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_super_admin=True,
                                     email_verified=True, totp_enabled=True, totp_secret=pyotp.random_base32())
        self.client.force_login(u)
        c = cluster_with([(64, 1024, 8)], bundle_cores=6, bundle_ram_gb=96)
        h = self.client.get(reverse("infra_site", args=[c.platform.site.code]), {"tab": "clusters"}).content.decode()
        self.assertIn("H200", h)
        self.assertIn("6 vCores + 96 GB RAM per card", h)


class EdgeAndGatewayAreChosen(TestCase):
    """The edge cluster is a **site** setting and the gateway cluster a **platform** setting (owner 2026-11-09:
    "move edge cluster selection to site settings and gateway cluster selection to the platform - more
    controlled"). A selector cannot hold two values, so "one per site" and "one per platform" need no rule.
    """

    def _save_platform(self, platform, cluster_name):
        from inventory import ops
        site, name = platform.site.code, platform.key.split(".", 1)[1]
        ops.platform_save(None, site, name, {"kind": platform.kind, "gateway_cluster": cluster_name})
        platform.refresh_from_db()
        return platform

    def _save_site(self, site, cluster_name):
        from inventory import ops
        ops.site_save(None, site.code, {"edge_cluster": cluster_name})
        site.refresh_from_db()
        return site

    def test_a_platform_may_have_no_gateway_cluster(self):
        c = cluster_with([(64, 512, 0)])
        self.assertIsNone(c.platform.gateway_cluster)       # and that is a valid, ordinary state
        self._save_platform(c.platform, "")
        self.assertIsNone(c.platform.gateway_cluster)

    def test_the_chosen_cluster_is_the_one_the_routers_use(self):
        c = cluster_with([(64, 512, 0)])
        self._save_platform(c.platform, c.name)
        self.assertEqual(c.platform.gateway_cluster, c)

    def test_any_cluster_of_the_platform_can_be_chosen(self):
        """Owner 2026-11-09: production puts the gateways on mgmt and ext-managed platforms too, so restricting
        this to kind="shared" left the real deployment unrepresentable."""
        c = cluster_with([(64, 512, 0)])
        c.kind = "mgmt"
        c.save(update_fields=["kind"])
        self._save_platform(c.platform, c.name)
        self.assertEqual(c.platform.gateway_cluster, c)

    def test_a_cluster_of_another_platform_is_refused(self):
        from inventory import ops
        a = cluster_with([(64, 512, 0)])
        b = cluster_with([(64, 512, 0)])        # its own platform
        with self.assertRaises(ops.EditError):
            self._save_platform(a.platform, b.name)
        a.platform.refresh_from_db()
        self.assertIsNone(a.platform.gateway_cluster)

    def test_a_gpu_cluster_keeps_the_routers_off_itself(self):
        """The point of the whole feature: the routers stay off the GPU cluster the project sits on."""
        gpu = cluster_with([(64, 1024, 8)])
        shared = cluster_with([(64, 512, 0)])
        shared.platform = gpu.platform
        shared.save(update_fields=["platform"])
        self._save_platform(gpu.platform, shared.name)
        self.assertEqual(gpu.platform.gateway_cluster, shared)

    def test_the_site_holds_one_edge_cluster(self):
        c = cluster_with([(64, 512, 0)])
        site = self._save_site(c.platform.site, c.name)
        self.assertEqual(site.edge_cluster, c)
        site = self._save_site(site, "")
        self.assertIsNone(site.edge_cluster)

    def test_a_cluster_of_another_site_cannot_be_its_edge(self):
        from inventory import ops
        from inventory.models import Site
        a = cluster_with([(64, 512, 0)])
        other = Site.objects.create(code="dc9")
        with self.assertRaises(ops.EditError):
            ops.site_save(None, other.code, {"edge_cluster": a.name})


class TheChoiceLocksOnceDeployed(TestCase):
    """Owner 2026-11-09: "selection cannot be changed if any edge service deployed or any gatway deoloyed"."""

    def test_a_project_deployed_on_the_platform_fixes_its_gateway_cluster(self):
        from inventory import ops
        from tenants.models import Deployment, Organization, Tenant
        from accounts.tests import valid_bin
        a = cluster_with([(64, 512, 0)])
        b = cluster_with([(64, 512, 0)])
        b.platform = a.platform
        b.save(update_fields=["platform"])
        site, name = a.platform.site.code, a.platform.key.split(".", 1)[1]
        ops.platform_save(None, site, name, {"kind": a.platform.kind, "gateway_cluster": a.name})
        org = Organization.objects.create(name="O", tax_id=valid_bin(), short_name="o")
        t = Tenant.objects.create(organization=org, name="p1", title="P", approval="approved")
        Deployment.objects.create(tenant=t, platform_key=a.platform.key, state="done")
        with self.assertRaises(ops.EditError) as caught:
            ops.platform_save(None, site, name, {"kind": a.platform.kind, "gateway_cluster": b.name})
        self.assertIn("cannot be changed", str(caught.exception))
        a.platform.refresh_from_db()
        self.assertEqual(a.platform.gateway_cluster, a)

    def test_saving_the_platform_without_touching_the_choice_still_works(self):
        from inventory import ops
        from tenants.models import Deployment, Organization, Tenant
        from accounts.tests import valid_bin
        a = cluster_with([(64, 512, 0)])
        site, name = a.platform.site.code, a.platform.key.split(".", 1)[1]
        ops.platform_save(None, site, name, {"kind": a.platform.kind, "gateway_cluster": a.name})
        org = Organization.objects.create(name="O", tax_id=valid_bin(), short_name="o")
        t = Tenant.objects.create(organization=org, name="p1", title="P", approval="approved")
        Deployment.objects.create(tenant=t, platform_key=a.platform.key, state="done")
        ops.platform_save(None, site, name, {"kind": a.platform.kind, "gateway_cluster": a.name})  # unchanged
        a.platform.refresh_from_db()
        self.assertEqual(a.platform.gateway_cluster, a)

    def test_deployed_edge_services_fix_the_site_choice(self):
        from inventory import ops
        from inventory.models import CoreService, CoreServiceNode
        a = cluster_with([(64, 512, 0)])
        b = cluster_with([(64, 512, 0)])
        b.platform = a.platform
        b.save(update_fields=["platform"])
        ops.site_save(None, a.platform.site.code, {"edge_cluster": a.name})
        svc = CoreService.objects.create(site=a.platform.site, stype="bgp-rr", name="rrs", state="active")
        CoreServiceNode.objects.create(service=svc, name="rr1", address="10.0.0.9", deployed_vm_id=42)
        with self.assertRaises(ops.EditError) as caught:
            ops.site_save(None, a.platform.site.code, {"edge_cluster": b.name})
        self.assertIn("cannot be changed", str(caught.exception))

    def test_a_service_that_is_only_planned_locks_nothing(self):
        from inventory import ops
        from inventory.models import CoreService, CoreServiceNode
        a = cluster_with([(64, 512, 0)])
        b = cluster_with([(64, 512, 0)])
        b.platform = a.platform
        b.save(update_fields=["platform"])
        ops.site_save(None, a.platform.site.code, {"edge_cluster": a.name})
        svc = CoreService.objects.create(site=a.platform.site, stype="bgp-rr", name="rrs", state="planned")
        CoreServiceNode.objects.create(service=svc, name="rr1", address="10.0.0.9")   # no deployed_vm_id
        ops.site_save(None, a.platform.site.code, {"edge_cluster": b.name})
        a.platform.site.refresh_from_db()
        self.assertEqual(a.platform.site.edge_cluster, b)
