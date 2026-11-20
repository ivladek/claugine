"""core-009 (full review 2026-11-16): a project's deletion covers every zone it has pools in - the plan, the
deletion and the check - and the federation's ACLs, users and group go only once every zone is clean."""

import types
import xml.etree.ElementTree as ET
from unittest import mock

from django.test import SimpleTestCase, TestCase

from tenants import undeploy
from tenants.deploy import DeployError

POOL_TAGS = {m: (t, d) for _k, m, t, d, _x in undeploy.POOLS}


class Zone:
    """One zone's front end: VMs and pools of (id, name, gid); the federation's group, users and ACLs on the primary."""

    def __init__(self, vms=(), images=(), federation=False, down=False):
        self.vms, self.images, self.federation, self.down = list(vms), list(images), federation, down
        self.gone, self.calls = set(), []

    def _items(self, method):
        if method == "one.vmpool.info":
            return "VM", [x for x in self.vms if ("vm", x[0]) not in self.gone]
        if method == "one.imagepool.info":
            return "IMAGE", [x for x in self.images if ("image", x[0]) not in self.gone]
        if method in POOL_TAGS:
            return POOL_TAGS[method][0], []
        raise AssertionError(method)

    def xml(self, method, *args):
        if self.down:
            raise DeployError(f"{method}: connection refused")
        if method == "one.grouppool.info":
            return ET.fromstring("<P><GROUP><ID>300</ID><NAME>grp</NAME></GROUP></P>" if self.federation else "<P/>")
        if method == "one.userpool.info":
            return ET.fromstring("<P/>")
        if method == "one.acl.info":
            return ET.fromstring("<P/>")
        tag, items = self._items(method)
        body = "".join(f"<{tag}><ID>{i}</ID><NAME>{n}</NAME><GID>{g}</GID><UID>0</UID></{tag}>" for i, n, g in items)
        return ET.fromstring(f"<P>{body}</P>")

    def find(self, method, tag, name, *args):
        return next((e for e in self.xml(method, *args).findall(tag) if e.findtext("NAME") == name), None)

    def call(self, method, *args):
        self.calls.append((method, args))
        if method == "one.vm.action":
            self.gone.add(("vm", args[1]))
        elif method == "one.image.delete":
            self.gone.add(("image", args[0]))


def undeployer(home, others):
    u = undeploy.Undeployer.__new__(undeploy.Undeployer)
    u.tenant = types.SimpleNamespace(group_name="grp", tenant_org=None)
    u.user, u.dry, u.deletion, u.steps, u.live = None, False, None, [], True
    u.one, u.gone, u.zone_ones = home, home, others
    u.others = []
    return u


@mock.patch("accounts.models.audit", lambda *a, **k: None)
@mock.patch.object(undeploy, "names_of", lambda t: __import__("re").compile("^$"))
@mock.patch.object(undeploy, "POLL_SECONDS", 0)
class Zones(SimpleTestCase):
    def test_every_zone_is_planned_deleted_and_checked(self):
        home = Zone(vms=[(1, "a", 300), (2, "other", 7)], federation=True)
        dc2 = Zone(vms=[(11, "b", 300)], images=[(21, "img-b", 300), (22, "shared", 0)])
        u = undeployer(home, {"dc2.cloud": dc2})
        plan = u.inventory()
        self.assertEqual([v["id"] for v in plan["vms"]], [1])
        self.assertEqual(
            (
                [v["id"] for v in plan["zones"]["dc2.cloud"]["vms"]],
                [i["id"] for i in plan["zones"]["dc2.cloud"]["images"]],
            ),
            ([11], [21]),
        )
        u.vms_step(plan)
        u.pools_step(plan)
        self.assertEqual(u.verify_step(plan), "nothing of the project is left")
        self.assertIn(("one.vm.action", ("terminate-hard", 11)), dc2.calls)
        self.assertIn(("one.image.delete", (21,)), dc2.calls)
        self.assertNotIn(("one.image.delete", (22,)), dc2.calls)

    def test_a_zone_that_cannot_be_read_stops_the_plan(self):
        u = undeployer(Zone(federation=True), {"dc2.cloud": Zone(down=True)})
        with self.assertRaisesRegex(DeployError, "dc2.cloud"):
            u.inventory()

    def test_what_an_old_plan_missed_in_another_zone_stops_before_the_access_goes(self):
        home = Zone(vms=[(1, "a", 300)], federation=True)
        dc2 = Zone(vms=[(11, "b", 300)])
        u = undeployer(home, {"dc2.cloud": dc2})
        plan = {"vms": [{"id": 1, "name": "a"}], "group": {"id": 300, "name": "grp"}}  # made before core-009
        u.vms_step(plan)
        with self.assertRaisesRegex(DeployError, r"dc2.cloud: vms #11"):
            u.verify_step(plan)

    def test_a_read_only_zone_is_refused_before_anything_is_read(self):
        u = undeploy.Undeployer.__new__(undeploy.Undeployer)
        p = types.SimpleNamespace(key="dc2.cloud")
        u.others = [(p, types.SimpleNamespace(configured=True, mode="adoption"))]
        with mock.patch.object(undeploy, "One") as one, self.assertRaisesRegex(DeployError, "read-only"):
            u.connect()
        one.assert_not_called()

    def test_the_plan_shows_the_other_zones(self):
        from tenants.models import TenantDeletion

        d = TenantDeletion(
            plan={"vms": [{"id": 1, "name": "a"}], "zones": {"dc2.cloud": {"vms": [{"id": 11, "name": "b"}]}}}
        )
        vms = next(r for r in d.plan_rows if r["key"] == "vms")["items"]
        self.assertEqual([v["name"] for v in vms], ["a", "dc2.cloud · b"])
        self.assertEqual(d.plan_total, 2)


class DeletionPlatforms(TestCase):
    """commit-022: the zones a deletion covers come from every pool the project ever had - retired clusters too -
    and a pool no platform can be found for stops it."""

    def setUp(self):
        from inventory.models import Cluster, Platform, Site
        from tenants.models import Organization, QuotaRecord, Tenant

        def site(code):
            return Site.objects.get_or_create(code=code)[0]

        self.home = Platform.objects.create(key="zz1.cloud", site=site("zz1"), name="cloud")
        self.far = Platform.objects.create(key="zz2.cloud", site=site("zz2"), name="cloud")
        Cluster.objects.create(platform=self.home, name="cpu_zz1", kind="shared", quota_code="za1")
        self.gone = Cluster.objects.create(platform=self.far, name="gpu_zz2", kind="shared", quota_code="zg1")
        org = Organization.objects.create(name="Acme", short_name="acme")
        self.t = Tenant.objects.create(organization=org, name="lab", group_name="tenant-lab")
        for s, c in (("zz1", "za1"), ("zz2", "zg1")):
            QuotaRecord.objects.create(tenant=self.t, site=s, cluster=c, version=1, source="test")

    def test_a_retired_clusters_zone_is_still_covered(self):
        self.gone.state = "retired"
        self.gone.save()
        others, unplaced = undeploy.deletion_platforms(self.t, self.home)
        self.assertEqual(([p.key for p in others], unplaced), (["zz2.cloud"], []))

    def test_an_unplaced_pool_in_the_home_site(self):
        """commit-022 (review of b87a926): covered only when home is the site's one platform."""
        from inventory.models import Platform
        from tenants.models import QuotaRecord

        QuotaRecord.objects.create(tenant=self.t, site="zz1", cluster="zx9", version=1, source="test")
        self.assertEqual(undeploy.deletion_platforms(self.t, self.home)[1], [])  # home is all of zz1
        Platform.objects.create(key="zz1.edge", site=self.home.site, name="edge")
        self.assertEqual(undeploy.deletion_platforms(self.t, self.home)[1], [("zz1", "zx9")])

    def test_a_pool_no_platform_has_stops_the_deletion(self):
        from tenants.models import QuotaRecord

        QuotaRecord.objects.create(tenant=self.t, site="zz3", cluster="zx1", version=1, source="test")
        others, unplaced = undeploy.deletion_platforms(self.t, self.home)
        self.assertEqual(unplaced, [("zz3", "zx1")])
        u = undeploy.Undeployer.__new__(undeploy.Undeployer)
        u.others, u.unplaced = [], unplaced
        with self.assertRaisesRegex(DeployError, "zz3/zx1"):
            u.connect()


class PlatformNotReachable(TestCase):
    """core-013 (third full review): a deployed project whose platform has no control or no credentials is not deleted
    from the portal alone - nothing is released."""

    def setUp(self):
        from inventory.models import Platform, Site
        from tenants.models import Organization, Tenant

        site = Site.objects.get_or_create(code="zz1")[0]
        self.p = Platform.objects.create(key="zz1.cloud", site=site, name="cloud")
        org = Organization.objects.create(name="Acme", short_name="acme")
        self.t = Tenant.objects.create(
            organization=org, name="lab", group_name="tenant-lab", one_group_id=161, order={"platform": "zz1.cloud"}
        )

    def test_no_control_or_no_credentials_blocks(self):
        from inventory.models import PlatformControl
        from tenants.models import TenantDeletion

        u = undeploy.Undeployer(self.t)
        self.assertIn("not configured here", u.blocked)
        with self.assertRaisesRegex(DeployError, "not configured here"):
            undeploy.archive(self.t)
        d = TenantDeletion.objects.create(tenant=self.t, state="running")  # core-025: claimed by its approval
        undeploy.make_plan(d)
        self.assertIn("not configured here", d.plan_error)
        d = undeploy.execute(d.pk)
        self.assertEqual(d.state, "failed")
        self.t.refresh_from_db()
        self.assertEqual(self.t.one_group_id, 161)  # the portal step did not run
        PlatformControl.objects.create(key="zz1.cloud", mode="execution")  # a control, but no credentials
        self.assertTrue(undeploy.Undeployer(self.t).blocked)

    def test_read_only_platform_or_never_deployed_stay_portal_only(self):
        from inventory.models import PlatformControl

        PlatformControl.objects.create(key="zz1.cloud", mode="adoption")
        self.assertEqual(undeploy.Undeployer(self.t).blocked, "")
        PlatformControl.objects.all().delete()
        self.t.one_group_id = None
        self.assertEqual(undeploy.Undeployer(self.t).blocked, "")
