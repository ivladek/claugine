"""Owner 2026-10-29 18:38: no "Status refresh, seconds" setting; a site page refreshes its statuses only while a task of
the site runs."""
from django.test import TestCase
from django.urls import reverse

from accounts.models import Kind, Task, User
from inventory.models import Site


class RefreshWhileTaskRuns(TestCase):
    def test_refresh_only_while_a_task_runs(self):
        from inventory.core_views import site_task_running
        st = Site.objects.create(code="dc9", city="Almaty")
        sa = User.objects.create_user("sa@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_super_admin=True, is_support=True)
        self.client.force_login(sa)
        self.assertFalse(site_task_running("dc9"))
        r = self.client.get(reverse("infra_site", args=["dc9"]))
        self.assertContains(r, 'data-seconds="0"')
        self.assertNotContains(r, "a task is running")
        t = Task.objects.create(kind="core services read", title="x", target_type="site", target_id=str(st.pk), state="running")
        self.assertTrue(site_task_running("dc9"))
        r = self.client.get(reverse("infra_site", args=["dc9"]))
        self.assertContains(r, 'data-seconds="10"')
        self.assertContains(r, "a task is running")
        t.state = "done"
        t.save()
        self.assertFalse(site_task_running("dc9"))
        r = self.client.get(reverse("portal_settings") + "?tab=operations")
        self.assertNotContains(r, "Status refresh")
