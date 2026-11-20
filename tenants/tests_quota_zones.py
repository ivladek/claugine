"""core-003 (owner 2026-11-15, "b"): a pool on another zone gets its quota there. "Each ordering and change resource
pool must followed by quota changes / No any quota set for users / We set quota only to group, note that each zone in
opennebula has independent quotas.\""""

import xml.etree.ElementTree as ET
from unittest import mock

from django.test import TestCase

from inventory.models import Cluster, Platform, PlatformControl, Site
from tenants import deploy as dp
from tenants.models import Organization, QuotaRecord, Tenant


class Zones(TestCase):
    def setUp(self):
        self.p1 = self._platform("dc1", "dc1.cloud", mode="primary")
        self.p2 = self._platform("dc2", "dc2.cloud", mode="secondary", primary="dc1.cloud")
        self.p3 = self._platform("dc3", "dc3.solo", mode="local")
        for p, n in ((self.p1, "cpu_dc1"), (self.p2, "cpu_dc2"), (self.p3, "cpu_dc3")):
            Cluster.objects.create(platform=p, name=n, kind="shared", quota_code=f"c{p.site.code[-1]}")
        org = Organization.objects.create(name="ACME", short_name="acme")
        self.t = Tenant.objects.create(
            organization=org,
            name="zones",
            title="Z",
            approval="approved",
            group_name="tenant-zones",
            one_group_id=170,
            order={"platform": "dc1.cloud", "cluster": "c1"},
        )
        self.rec("dc1", "c1")
        self.calls, self.acls = [], {}

    def _platform(self, site, key, mode, primary=""):
        st = Site.objects.create(code=site)
        p = Platform.objects.create(
            key=key,
            site=st,
            name=key.split(".")[1],
            kind="shared",
            fe_mode=mode,
            fe_primary=primary,
            fe_vip=f"192.0.2.{len(site) + int(site[-1])}",
        )
        PlatformControl.objects.create(key=key, mode="execution")
        return p

    def rec(self, site, code, removed=False, version=1):
        return QuotaRecord.objects.create(
            tenant=self.t,
            site=site,
            cluster=code,
            version=version,
            cpu=8,
            ram_gb=32,
            disk_gb=200,
            backups_gb=50,
            removed=removed,
        )

    def fake(self):
        calls, acls = self.calls, self.acls

        class FakeOne:
            def __init__(self, control):
                self.key = control.key
                self.site = control.key.split(".")[0]

            def xml(self, method, *a):
                if method == "one.zonepool.info":  # dc1 is zone 0, dc2 zone 1
                    return ET.fromstring(
                        "<P><ZONE><ID>0</ID><TEMPLATE><ENDPOINT>http://192.0.2.4:2633/RPC2</ENDPOINT></TEMPLATE></ZONE>"
                        "<ZONE><ID>1</ID><TEMPLATE><ENDPOINT>http://192.0.2.5:2633/RPC2</ENDPOINT></TEMPLATE></ZONE></P>"
                    )
                if method == "one.acl.info":
                    return ET.fromstring(
                        "<P>"
                        + "".join(f"<ACL><ID>{i}</ID><STRING>{r}</STRING></ACL>" for i, r in acls.items())
                        + "</P>"
                    )
                if method == "one.clusterpool.info":
                    return ET.fromstring(f"<P><CLUSTER><ID>0</ID><NAME>cpu_{self.site}</NAME></CLUSTER></P>")
                return ET.fromstring(
                    "<P><DATASTORE><ID>1</ID><TYPE>0</TYPE><CLUSTERS><ID>0</ID></CLUSTERS></DATASTORE></P>"
                )

            def call(self, method, *a):
                calls.append((self.key, method, a))

        return FakeOne

    def apply(self):
        with mock.patch.object(dp, "One", self.fake()), mock.patch("inventory.models.PlatformControl.configured", True):
            return dp.apply_quota(self.t, None)

    def test_each_zone_with_a_pool_gets_its_quota(self):
        self.rec("dc2", "c2")
        report = self.apply()
        self.assertEqual(
            [(k, m) for k, m, _a in self.calls], [("dc1.cloud", "one.group.quota"), ("dc2.cloud", "one.group.quota")]
        )
        self.assertTrue(all(a[0] == 170 for _k, _m, a in self.calls))  # the group's, never a user's
        self.assertIn("dc1: quota set: cluster #0 8 vCPU", report)
        self.assertIn("dc2: quota set: cluster #0 8 vCPU", report)

    def test_an_apply_writes_the_platform_under_the_projects_lock(self):
        """core-021 (twenty-second full review): the lock is taken before the current allocation is read, and every
        zone's quota is written inside that one transaction - an older apply cannot land after a newer one. (The tests'
        SQLite does not enforce the row lock; MySQL holds the second apply until the first is done.)"""
        from django.db import connection

        from tenants import models as tm

        self.rec("dc2", "c2")
        base = len(connection.atomic_blocks)
        seen = []
        real_lock, real_for = tm.lock_quotas, dp.quota_for

        def lock(t):
            seen.append(("locked", len(connection.atomic_blocks)))
            return real_lock(t)

        def read(*a, **k):
            seen.append(("read", len(connection.atomic_blocks)))
            return real_for(*a, **k)

        with (
            mock.patch.object(tm, "lock_quotas", side_effect=lock),
            mock.patch.object(dp, "quota_for", side_effect=read),
        ):
            self.apply()
        writes = [k for k, m, _a in self.calls if m == "one.group.quota"]
        self.assertEqual(writes, ["dc1.cloud", "dc2.cloud"])
        self.assertEqual([e for e, _n in seen], ["locked", "read", "read"])
        self.assertTrue(all(n == base + 1 for _e, n in seen), seen)

    def test_a_removed_pools_zone_is_closed(self):
        self.rec("dc2", "c2")
        QuotaRecord.objects.create(tenant=self.t, site="dc2", cluster="c2", version=2, removed=True)
        report = self.apply()
        self.assertIn("dc2: quota closed", report)
        q = self.calls[1][2][1]
        self.assertIn("RUNNING_CPU", q)
        self.assertNotIn('RUNNING_CPU = "8"', q)

    def test_a_zone_outside_the_federation_is_refused_and_said(self):
        self.rec("dc3", "c3")
        with self.assertRaises(dp.DeployError) as e:
            self.apply()
        self.assertIn("dc1: quota set", str(e.exception))  # the home zone was still done
        self.assertIn("dc3: failed: dc3.solo is not in the project's OpenNebula federation", str(e.exception))

    def test_the_pool_order_refuses_a_zone_outside_the_federation(self):
        from tenants.forms import PoolOrderForm

        pools = [
            {"key": "dc2.cloud/c2", "site": "dc2", "quota_code": "c2", "label": "c2", "gpu": None},
            {"key": "dc3.solo/c3", "site": "dc3", "quota_code": "c3", "label": "c3", "gpu": None},
        ]
        with (
            mock.patch("tenants.ordering.resource_pools", return_value=pools),
            mock.patch("tenants.ordering.inet_base_subnets", return_value=([], {})),
            mock.patch("tenants.ordering.corpnet_base_subnets", return_value=([], {})),
            mock.patch("tenants.ordering.prefix_lengths_for", return_value=[28]),
            mock.patch("tenants.ordering.connections", return_value=[]),
        ):
            bad = PoolOrderForm(
                {"pool": "dc3.solo/c3", "cpu": 4, "ram_gb": 8, "disk_gb": 50, "igw_type": "vyos"}, tenant=self.t
            )
            good = PoolOrderForm(
                {"pool": "dc2.cloud/c2", "cpu": 4, "ram_gb": 8, "disk_gb": 50, "igw_type": "vyos"}, tenant=self.t
            )
            self.assertFalse(bad.is_valid())
            self.assertIn("pool", bad.errors)
            good.is_valid()
            self.assertNotIn("pool", good.errors)

    def test_a_pool_no_platform_has_stops_the_change(self):
        """core-003 again (fourth full review): never skipped, so its zone is never left as it was unseen."""
        self.rec("dc9", "c9")
        with self.assertRaisesRegex(dp.DeployError, "dc9/c9: no platform has this pool's cluster"):
            self.apply()
        self.assertEqual(self.calls, [])  # nothing was changed


class ObservedAgain(Zones):
    """Owner 2026-11-18 17:01 (kvaaka: the quota set at 16:34 was compared with the 16:27 read until 16:57): after the
    portal sets a project's quota it reads that project's group again, on each platform the quota was set on."""

    def collector_client(self, fail=()):
        class Client:
            def __init__(self, control):
                self.key = control.key

            def users(self):
                return {}

            def group(self, gid):
                if self.key in fail:
                    raise RuntimeError("no answer")
                return {"ID": gid, "NAME": "tenant-zones", "VM_QUOTA": {"VM": {"RUNNING_CPU": "8"}}}

        return Client

    def test_the_group_is_read_again_after_its_quota_is_set(self):
        from datetime import timedelta

        from django.utils import timezone

        from adoption.models import Observation

        self.rec("dc2", "c2")
        old = timezone.now() - timedelta(minutes=20)
        Observation.objects.create(
            platform_key="dc1.cloud", group_id=170, group_name="tenant-zones", quotas={}, collected_at=old
        )
        with mock.patch("adoption.collector.client_for", side_effect=self.collector_client()):
            self.apply()
        for key in ("dc1.cloud", "dc2.cloud"):
            o = Observation.objects.get(platform_key=key, group_id=170)
            self.assertEqual(o.quotas["VM_QUOTA"]["VM"]["RUNNING_CPU"], "8")
            self.assertGreater(o.collected_at, old)
        self.assertEqual(Observation.objects.count(), 2)  # only this group, nothing else of the platform

    def test_a_quota_set_before_a_later_failure_is_read_again(self):
        """commit-114: the quota written, then the group's template update fails - the platform is read again all
        the same (the change itself still reports the failure)."""
        d = mock.MagicMock()
        d.primary_control.key = d.control.key = "same"
        d._addresses.return_value = (None, None, None, None)
        d._group_template.side_effect = dp.DeployError("template failed")
        with (
            mock.patch.object(Tenant, "legacy_names", new_callable=mock.PropertyMock, return_value=False),
            mock.patch.object(dp, "Deployer", return_value=d),
            mock.patch("adoption.collector.observe_group") as observe,
            self.assertRaises(dp.DeployError),
        ):
            self.apply()
        self.assertIn(("dc1.cloud", "one.group.quota"), [(k, m) for k, m, _a in self.calls])
        self.assertEqual([c.args[0].key for c in observe.call_args_list], ["dc1.cloud"])

    def test_a_failed_read_does_not_fail_the_quota_change(self):
        from adoption.models import Observation

        with mock.patch("adoption.collector.client_for", side_effect=self.collector_client(fail={"dc1.cloud"})):
            report = self.apply()
        self.assertIn("dc1: quota set: cluster #0 8 vCPU", report)
        self.assertFalse(Observation.objects.exists())


class NewerReadsStay(TestCase):
    """commit-113: an observation keeps the time its read started; an older read (a full collection that read the
    group before the quota change) does not overwrite a newer one (the re-read after it)."""

    def test_an_older_read_does_not_overwrite_a_newer_one(self):
        from datetime import timedelta

        from django.utils import timezone

        from adoption import collector
        from adoption.models import Observation

        control = PlatformControl.objects.create(key="dc1.cloud", mode="execution")
        one = mock.MagicMock()
        one.group.return_value = {"VM_QUOTA": {"VM": {"RUNNING_CPU": "25"}}}  # the old quota, read first
        newer = timezone.now() + timedelta(seconds=30)  # the re-read after the quota change started later
        Observation.objects.create(
            platform_key="dc1.cloud",
            group_id=109,
            group_name="tenant-kvaaka",
            quotas={"VM_QUOTA": {"VM": {"RUNNING_CPU": "30"}}},
            collected_at=newer,
        )
        self.assertFalse(collector._observe(one, control, 109, "tenant-kvaaka", {}))
        o = Observation.objects.get(group_id=109)
        self.assertEqual((o.quotas["VM_QUOTA"]["VM"]["RUNNING_CPU"], o.collected_at), ("30", newer))
        Observation.objects.filter(group_id=109).update(collected_at=timezone.now() - timedelta(minutes=30))
        self.assertTrue(collector._observe(one, control, 109, "tenant-kvaaka", {}))  # an older one is replaced
        self.assertEqual(Observation.objects.get(group_id=109).quotas["VM_QUOTA"]["VM"]["RUNNING_CPU"], "25")


class NewerReadsStayWhenBothInsert(TestCase):
    """commit-113 again: no observation stored yet - the older read (a full collection) and the newer re-read insert at
    once; the newer one wins the insert, and the older one does not overwrite it."""

    def test_a_concurrent_newer_insert_survives(self):
        from datetime import timedelta

        from django.utils import timezone

        from adoption import collector
        from adoption.models import Observation

        control = PlatformControl.objects.create(key="dc1.cloud", mode="execution")
        one = mock.MagicMock()
        one.group.return_value = {"VM_QUOTA": {"VM": {"RUNNING_CPU": "25"}}}  # the old quota
        newer = timezone.now() + timedelta(seconds=30)
        from django.db.models.query import QuerySet

        mgr = Observation.objects

        def newer_first(real):
            def insert_then(*a, **kw):  # the re-read commits its row just before this writer's insert or update
                if not Observation.objects.filter(group_id=109).exists():
                    Observation.objects.bulk_create(
                        [
                            Observation(
                                platform_key="dc1.cloud",
                                group_id=109,
                                group_name="tenant-kvaaka",
                                quotas={"VM_QUOTA": {"VM": {"RUNNING_CPU": "30"}}},
                                collected_at=newer,
                            )
                        ]
                    )
                return real(*a, **kw)

            return insert_then

        with (  # the queryset's get_or_create (commit-115: called on the locking queryset) and the older update_or_create
            mock.patch.object(
                QuerySet, "get_or_create", autospec=True, side_effect=newer_first(QuerySet.get_or_create)
            ),
            mock.patch.object(mgr, "update_or_create", side_effect=newer_first(mgr.update_or_create)),
        ):
            collector._observe(one, control, 109, "tenant-kvaaka", {})
        o = Observation.objects.get(group_id=109)
        self.assertEqual((o.quotas["VM_QUOTA"]["VM"]["RUNNING_CPU"], o.collected_at), ("30", newer))


class CleanupDuringARead(TestCase):
    """commit-115: a full collection that no longer lists the group deletes its observation while a re-read is saving -
    the re-read finds or makes its row under the lock; a row removed meanwhile is a group gone, nothing is saved.
    commit-116: only that - another integrity failure, or a row that is there, is raised to the caller."""

    def setUp(self):
        self.control = PlatformControl.objects.create(key="dc1.cloud", mode="execution")
        self.one = mock.MagicMock()
        self.one.group.return_value = {"VM_QUOTA": {}}

    def test_the_row_is_found_or_made_on_a_locking_queryset(self):
        """commit-117: the find-or-make runs on a select_for_update() queryset - the lock the cleanup has to wait for."""
        from django.db.models.query import QuerySet

        from adoption import collector

        seen = []
        real = QuerySet.get_or_create

        def spy(qs, *a, **kw):
            seen.append(qs.query.select_for_update)
            return real(qs, *a, **kw)

        with mock.patch.object(QuerySet, "get_or_create", autospec=True, side_effect=spy):
            self.assertTrue(collector._observe(self.one, self.control, 7, "tenant-x", {}))  # made
            collector._observe(self.one, self.control, 7, "tenant-x", {})  # found
        self.assertEqual(seen, [True, True])

    def test_a_row_removed_meanwhile_is_not_an_error(self):
        from django.db import IntegrityError
        from django.db.models.query import QuerySet

        from adoption import collector
        from adoption.models import Observation

        lost = IntegrityError(
            "UNIQUE constraint failed: adoption_observation.platform_key, adoption_observation.group_id"
        )
        mysql = IntegrityError(
            1062,
            "Duplicate entry 'dc1.cloud-7' for key "
            "'adoption_observation.adoption_observation_platform_key_group_id_b5772772_uniq'",
        )
        for exc in (Observation.DoesNotExist, lost, mysql):  # the find-or-make losing the row to the cleanup
            with mock.patch.object(QuerySet, "get_or_create", side_effect=exc):
                self.assertFalse(collector._observe(self.one, self.control, 7, "tenant-x", {}))
        self.assertFalse(Observation.objects.exists())

    def test_the_databases_own_errors_are_told_apart(self):
        """commit-116: the database's real errors - a duplicate (platform, group) is the lost insert, a duplicate
        primary key is not."""
        from django.db import IntegrityError, transaction
        from django.utils import timezone

        from adoption.collector import _duplicate_key
        from adoption.models import Observation

        o = Observation.objects.create(
            platform_key="dc1.cloud", group_id=7, group_name="x", collected_at=timezone.now()
        )
        for kw, lost in (
            ({"platform_key": "dc1.cloud", "group_id": 7}, True),
            ({"pk": o.pk, "platform_key": "dc1.cloud", "group_id": 8}, False),
        ):
            with self.assertRaises(IntegrityError) as cm, transaction.atomic():
                Observation.objects.create(group_name="x", collected_at=timezone.now(), **kw)
            self.assertEqual(_duplicate_key(cm.exception), lost, cm.exception)

    def test_an_unrelated_failure_of_the_first_insert_is_raised(self):
        """commit-116: no row there and an integrity failure that is not the (platform, group) duplicate - raised, so a
        full collection records it instead of reporting success."""
        from django.db import IntegrityError
        from django.db.models.query import QuerySet

        from adoption import collector

        for exc in (
            IntegrityError("NOT NULL constraint failed: adoption_observation.group_name"),
            IntegrityError(1048, "Column 'group_name' cannot be null"),
            IntegrityError(
                "UNIQUE constraint failed: adoption_observation.id"
            ),  # the primary key, not (platform, group)
            IntegrityError(1062, "Duplicate entry '41' for key 'adoption_observation.PRIMARY'"),
        ):
            with (
                mock.patch.object(QuerySet, "get_or_create", side_effect=exc),
                self.assertRaises(IntegrityError),
            ):
                collector._observe(self.one, self.control, 7, "tenant-x", {})

    def test_another_integrity_failure_is_raised(self):
        """commit-116: an IntegrityError while the row is there is not the cleanup - it reaches the caller."""
        from django.db import IntegrityError
        from django.db.models.query import QuerySet
        from django.utils import timezone

        from adoption import collector
        from adoption.models import Observation

        Observation.objects.create(
            platform_key="dc1.cloud", group_id=7, group_name="tenant-x", collected_at=timezone.now()
        )
        with (
            mock.patch.object(QuerySet, "get_or_create", side_effect=IntegrityError("another constraint")),
            self.assertRaises(IntegrityError),
        ):
            collector._observe(self.one, self.control, 7, "tenant-x", {})


class ZoneAcls(Zones):
    """Owner 2026-11-15: "Check that pool ordering and deletion followed by quota changes and acl create deletion".
    The quota is -1 where there is no pool; the ACLs decide which clusters the group may use."""

    test_each_zone_with_a_pool_gets_its_quota = None
    test_a_removed_pools_zone_is_closed = None
    test_a_zone_outside_the_federation_is_refused_and_said = None
    test_the_pool_order_refuses_a_zone_outside_the_federation = None
    test_a_pool_no_platform_has_stops_the_change = None
    test_an_apply_writes_the_platform_under_the_projects_lock = None

    def apply(self):
        d = mock.MagicMock()
        d.primary_control.key = d.control.key = "same"
        d._addresses.return_value = (None, None, None, None)
        d._group_template.return_value = {}
        with (
            mock.patch.object(Tenant, "legacy_names", new_callable=mock.PropertyMock, return_value=False),
            mock.patch.object(dp, "Deployer", return_value=d),
        ):
            report = super().apply()
        for _k, m, a in self.calls:  # what the primary now holds
            if m == "one.acl.addrule":
                self.acls[max(self.acls, default=99) + 1] = self.added.pop(0)
            if m == "one.acl.delrule":
                self.acls.pop(a[0])
        return report

    def test_a_pool_brings_its_acls_and_takes_them_away(self):
        self.acls[50] = "@170 CLUSTER/#0 USE #0"  # made by the deployment
        self.acls[60] = "@171 CLUSTER/#0 USE #1"  # another project's: never touched
        self.rec("dc2", "c2")
        real = dp.one_acl.encode
        self.added = []

        def enc(r):
            self.added.append(r)
            return real(r)

        with mock.patch.object(dp.one_acl, "encode", side_effect=enc):
            report = self.apply()
        self.assertIn("ACL: 7 added, 0 removed", report)  # the deployment made dc1's cluster rule
        rules = set(self.acls.values())
        for z in (0, 1):
            self.assertTrue(
                {
                    f"@170 CLUSTER/#0 USE #{z}",
                    f"@170 DATASTORE/%0 USE #{z}",
                    f"@170 HOST/%0 MANAGE #{z}",
                    f"@170 ZONE/#{z} USE #{z}",
                }
                <= rules
            )
        self.assertEqual([k for k, m, _a in self.calls if m.startswith("one.acl.")][0], "dc1.cloud")  # on the primary
        QuotaRecord.objects.create(tenant=self.t, site="dc2", cluster="c2", version=2, removed=True)
        self.calls.clear()
        with mock.patch.object(dp.one_acl, "encode", side_effect=enc):
            report = self.apply()
        self.assertIn("ACL: 0 added, 4 removed", report)
        rules = set(self.acls.values())
        self.assertFalse([r for r in rules if r.startswith("@170") and r.endswith("#1")])
        self.assertIn("@170 CLUSTER/#0 USE #0", rules)
        self.assertIn("@171 CLUSTER/#0 USE #1", rules)

    def test_the_last_pool_of_a_zone_whose_cluster_was_retired(self):
        """core-003 again (fourth full review): the cluster of dc2's only pool is retired, then the pool is removed -
        dc2's quota is closed and its ACLs go, as for any other zone."""
        self.acls[50] = "@170 CLUSTER/#0 USE #0"
        self.rec("dc2", "c2")
        real = dp.one_acl.encode
        self.added = []

        def enc(r):
            self.added.append(r)
            return real(r)

        with mock.patch.object(dp.one_acl, "encode", side_effect=enc):
            self.apply()
        Cluster.objects.filter(platform=self.p2).update(state="retired")
        QuotaRecord.objects.create(tenant=self.t, site="dc2", cluster="c2", version=2, removed=True)
        self.calls.clear()
        with mock.patch.object(dp.one_acl, "encode", side_effect=enc):
            report = self.apply()
        self.assertIn("dc2: quota closed", report)
        self.assertIn("ACL: 0 added, 4 removed", report)
        self.assertFalse([r for r in self.acls.values() if r.startswith("@170") and r.endswith("#1")])

    def test_a_new_zone_reached_by_a_change_that_failed_is_closed_again(self):
        """commit-032: the resources editor adds the project's first pool in dc2; dc2 takes its quota and access
        rules, the home zone fails - nothing is saved, and the way back reaches dc2 too: its quota closed, its access
        rules removed (the rollback had removed the only record that named it)."""
        from django.urls import reverse

        from accounts.models import Kind, User
        from accounts.testkit import otp

        self.acls[50] = "@170 CLUSTER/#0 USE #0"
        real = dp.one_acl.encode
        self.added = []

        def enc(r):
            self.added.append(r)
            return real(r)

        home_fails = {"n": 0}
        fake = self.fake()

        class Fails(fake):
            def call(self, method, *a):
                if method == "one.group.quota" and self.key == "dc1.cloud" and home_fails["n"] == 0:
                    home_fails["n"] += 1
                    calls.append((self.key, method, a))
                    from tenants.deploy import DeployError

                    raise DeployError("dc1: the quota was refused")
                out = super().call(method, *a)
                if method == "one.acl.addrule":  # what the primary holds, at once (both passes in one request)
                    acls[max(acls, default=99) + 1] = added.pop(0)
                if method == "one.acl.delrule":
                    acls.pop(a[0])
                return out

        calls, acls, added = self.calls, self.acls, self.added
        rm = User.objects.create_user(
            "rm@corp.kz",
            "Str0ng-PASS-123",
            kind=Kind.STAFF,
            is_resource_manager=True,
            email_verified=True,
            totp_enabled=True,
        )
        self.client.force_login(rm)
        otp(self.client)
        url = reverse("tenant_resources", args=[self.t.pk])
        data = {
            "r-TOTAL_FORMS": "2",
            "r-INITIAL_FORMS": "1",
            "r-MIN_NUM_FORMS": "0",
            "r-MAX_NUM_FORMS": "1000",
            "r-0-key": "dc1/c1",
            "r-0-cpu": "8",
            "r-0-ram_gb": "32",
            "r-0-gpu": "",
            "r-0-disk_gb": "200",
            "r-0-backups_gb": "50",
            "r-1-placement": "dc2/c2",
            "r-1-cpu": "4",
            "r-1-ram_gb": "16",
            "r-1-gpu": "",
            "r-1-disk_gb": "100",
            "r-1-backups_gb": "0",
        }
        d = mock.MagicMock()
        d.primary_control.key = d.control.key = "same"
        d._addresses.return_value = (None, None, None, None)
        d._group_template.return_value = {}
        with (
            mock.patch.object(dp, "One", Fails),
            mock.patch("inventory.models.PlatformControl.configured", True),
            mock.patch.object(dp.one_acl, "encode", side_effect=enc),
            mock.patch.object(Tenant, "legacy_names", new_callable=mock.PropertyMock, return_value=False),
            mock.patch.object(dp, "Deployer", return_value=d),
            mock.patch("tenants.forms.tenant_placements", return_value=[("dc2/c2", "dc2 / c2")]),
        ):
            r = self.client.post(url, data, follow=True)
        self.assertContains(r, "Nothing was saved")
        self.assertEqual([(q.site, q.cluster) for q in self.t.current_quota_records()], [("dc1", "c1")])
        dc2 = [a for k, m, a in calls if k == "dc2.cloud" and m == "one.group.quota"]
        self.assertEqual(len(dc2), 2)  # set by the change, then closed again
        self.assertIn('RUNNING_CPU = "4"', dc2[0][1])
        self.assertNotIn('RUNNING_CPU = "4"', dc2[1][1])
        self.assertTrue([m for _k, m, _a in calls if m == "one.acl.addrule"])  # the change did give dc2 its access
        self.assertFalse([x for x in self.acls.values() if x.startswith("@170") and x.endswith("#1")])  # dc2's gone
