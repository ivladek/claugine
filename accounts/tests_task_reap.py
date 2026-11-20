"""core-007: a task whose process ended (a restart) no longer blocks the updates, the OS updates and the key change."""

import socket
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from accounts import tasks
from accounts.models import Task


class Reap(TestCase):
    def task(self, runner, **kw):
        return Task.objects.create(kind="x", title="t", state="running", runner=runner, **kw)

    def test_a_task_of_an_ended_process_is_failed_and_not_counted(self):
        host = socket.gethostname()
        dead = self.task(f"{host}:999999:12345")  # no such process
        reused = self.task(f"{host}:{__import__('os').getpid()}:1")  # this pid, but another start: another process
        self.assertEqual(tasks.running_count(), 0)
        for t in (dead, reused):
            t.refresh_from_db()
            self.assertEqual(t.state, "failed")
            self.assertIn("interrupted", t.error)

    def test_a_live_task_still_counts(self):
        live = self.task(tasks.me())
        self.assertEqual(tasks.running_count(), 1)
        live.refresh_from_db()
        self.assertEqual(live.state, "running")

    def test_unknown_runners_count_until_stale(self):
        other = self.task("other-node:123:456")
        old = self.task("")
        Task.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(minutes=Task.STALE_MINUTES + 1))
        self.assertEqual(tasks.running_count(), 1)  # the other host's recent one
        other.refresh_from_db()
        old.refresh_from_db()
        self.assertEqual((other.state, old.state), ("running", "failed"))

    def test_a_started_task_records_its_runner(self):
        from django.test import override_settings

        with override_settings(DEPLOY_ASYNC=False):
            t = tasks.start("x", "now", lambda task: {"ok": 1})
        self.assertEqual(t.runner, tasks.me())
        self.assertEqual(t.state, "done")


class OnAStandby(TestCase):
    def test_a_standby_counts_and_writes_nothing(self):
        """commit-020: the standby's /status counts tasks; its database is read only, so nothing is failed there."""
        from unittest import mock

        old = Task.objects.create(kind="x", title="t", state="running", runner="other-node:1:2")
        Task.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(minutes=Task.STALE_MINUTES + 1))
        with mock.patch("accounts.ha.role", return_value="standby"), mock.patch.object(tasks, "reap") as reap:
            self.assertEqual(tasks.running_count(), 1)
        reap.assert_not_called()


class ReapWork(TestCase):
    """core-007 (full review 2026-11-16): deployments, network changes, deletions, configuration checks and igw
    applies left running by an ended process stop counting too (they blocked updates and key changes for good)."""

    def setUp(self):
        from tenants.models import Organization, Tenant

        org = Organization.objects.create(name="Acme", short_name="acme")
        self.t = Tenant.objects.create(organization=org, name="lab", group_name="tenant-lab")

    def make(self, runner):
        from tenants.models import ConfigCheck, Deployment, IgwApply, NetworkRequest, TenantDeletion

        return [
            Deployment.objects.create(tenant=self.t, platform_key="dc1.cloud", state="running", runner=runner),
            NetworkRequest.objects.create(
                tenant=self.t, site="dc1", kind="l2", state="approved", run="running", runner=runner
            ),
            TenantDeletion.objects.create(tenant=self.t, state="running", runner=runner),
            ConfigCheck.objects.create(tenant=self.t, state="running", runner=runner),
            IgwApply.objects.create(tenant=self.t, state="running", runner=runner),
        ]

    def test_work_of_an_ended_process_is_failed(self):
        dead = self.make(f"{socket.gethostname()}:999999:12345")
        self.assertEqual(tasks.running_count(), 0)
        for w in dead:
            w.refresh_from_db()
            state = w.run if hasattr(w, "run") else w.state
            self.assertEqual(state, "failed", type(w).__name__)
            self.assertIn("interrupted", getattr(w, "result", "") if type(w).__name__ == "IgwApply" else w.error)

    def test_live_work_still_counts(self):
        self.make(tasks.me())
        self.assertEqual(tasks.running_count(), 5)

    def test_new_work_carries_its_process(self):
        from tenants.models import ConfigCheck

        self.assertEqual(ConfigCheck.objects.create(tenant=self.t).runner, tasks.me())

    def test_unknown_runners_only_once_stale(self):
        from tenants.models import IgwApply

        self.make("other-node:1:2")
        self.assertEqual(tasks.running_count(), 5)
        IgwApply.objects.create(
            tenant=self.t, state="waiting", deadline=timezone.now() - timedelta(hours=2), runner=tasks.me()
        )
        tasks.reap_work()
        self.assertEqual(IgwApply.objects.filter(state="rolled_back").count(), 1)

    def test_a_retry_claimed_meanwhile_keeps_running(self):
        """core-049 (twenty-fifth full review): a second check read the interrupted deletion before the first marked
        it failed and the supported retry claimed it again; its stale write leaves the retry running and counted.
        commit-057: synthetic runners and liveness throughout, so it does not depend on /proc (macOS)."""
        from tenants.models import TenantDeletion

        dead, retry = "node-a:111:1", "node-a:222:2"
        d = TenantDeletion.objects.create(tenant=self.t, state="running", runner=dead)

        def alive(runner):
            if runner == dead:  # the first check failed it and the retry claimed it, after this check's read
                TenantDeletion.objects.filter(pk=d.pk).update(state="running", runner=retry, error="")
                return False
            return runner == retry

        with mock.patch.object(tasks, "alive", side_effect=alive):
            self.assertEqual(tasks.reap_work(), 0)
            d.refresh_from_db()
            self.assertEqual((d.state, d.runner, d.error), ("running", retry, ""))
            self.assertEqual(tasks.running_count(), 1)
