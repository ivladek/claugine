"""Owner 2026-10-29 21:10: the dashboard's numbers come from the Platforms timer — every platform's datastores (management
and portal platforms too) and the platforms' own VMs."""
from unittest import mock

from django.test import TestCase

from inventory.models import PlatformControl


class PlatformTimer(TestCase):
    def test_stats_of_platforms_not_read_for_tenants(self):
        from adoption import collector
        PlatformControl.objects.create(key="dc1.mgmt", collect_enabled=False)
        PlatformControl.objects.create(key="dc1.cloud", collect_enabled=True)
        with mock.patch.object(PlatformControl, "configured", new_callable=mock.PropertyMock, return_value=True), \
                mock.patch("adoption.collector.client_for", return_value=object()), \
                mock.patch("adoption.collector.collect_stats") as stats:
            out = collector.collect_stats_only()
        self.assertEqual(out, {"dc1.mgmt": "ok"})
        self.assertEqual(stats.call_args[0][1].key, "dc1.mgmt")

    def test_platform_timer_reads_everything_the_dashboard_shows(self):
        from accounts import timers
        from inventory.models import Site
        Site.objects.get_or_create(code="zz9")
        with mock.patch("inventory.health.check_due", return_value={"dc1.cloud": True}), \
                mock.patch("adoption.collector.collect_all", return_value={}), \
                mock.patch("adoption.collector.collect_stats_only", return_value={"dc1.mgmt": "ok"}) as st, \
                mock.patch("inventory.core_services.refresh_vms", return_value=[]) as vms:
            ok, detail = timers.FUNCS["platform"]()
        self.assertTrue(ok, detail)
        st.assert_called_once()
        self.assertIn("zz9", [c.args[0] for c in vms.call_args_list])
