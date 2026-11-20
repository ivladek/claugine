"""core-046 / enterprise-022 (twenty-third full review, owner "fix before" 2026-11-17 19:14): the maintenance gate -
closed before the running work is counted, so no work is admitted between the count and the restart."""

import os
import tempfile
from unittest import mock

from django.test import TestCase, TransactionTestCase, override_settings

from . import ha_agent, maintenance, tasks
from .testkit import NodeFile


@override_settings(DEPLOY_ASYNC=False)
class Gate(TestCase):
    def test_closed_gate_refuses_every_starter(self):
        from tenants import configcheck, deploy, lifecycle, netops
        from tenants.models import NetworkRequest, Organization, Tenant

        org = Organization.objects.create(name="Acme")
        t = Tenant.objects.create(organization=org, name="dev", title="dev")
        maintenance.close("update-single")
        self.assertEqual(maintenance.state(), (True, "update-single"))
        with self.assertRaises(maintenance.MaintenanceRunning):
            tasks.start("x", "x", lambda task: "ok")
        with self.assertRaises(maintenance.MaintenanceRunning):
            deploy.new_deployment(t, None, False)
        deploy.new_deployment(t, None, True)  # a dry run (a plan) changes nothing: allowed
        with self.assertRaises(maintenance.MaintenanceRunning):
            configcheck.start(t)
        with self.assertRaises(maintenance.MaintenanceRunning):
            netops.claim(NetworkRequest(pk=1))
        with self.assertRaises(maintenance.MaintenanceRunning):
            lifecycle.claim_archive(t)
        maintenance.open_("update-single")
        self.assertEqual(maintenance.state(), (False, ""))
        self.assertEqual(tasks.start("x", "x", lambda task: "ok").state, "done")

    def test_close_with_work_running_reopens(self):
        with mock.patch.object(tasks, "running_count", return_value=2), self.assertRaises(RuntimeError):
            maintenance.close("update-single")
        self.assertEqual(maintenance.state(), (False, ""))

    def test_a_failed_count_opens_the_gate_again(self):
        """integration-007, commit-056: the count raising (the database away for a moment) leaves no closed gate
        behind, and no write after the failure is needed for that: open_() is never called."""
        from django.db import OperationalError

        with (
            mock.patch.object(tasks, "running_count", side_effect=OperationalError("gone")),
            mock.patch.object(maintenance, "open_", side_effect=OperationalError("still gone")) as reopen,
            self.assertRaises(OperationalError),
        ):
            maintenance.close("switchover")
        reopen.assert_not_called()
        self.assertEqual(maintenance.state(), (False, ""))
        self.assertEqual(tasks.start("x", "x", lambda task: "ok").state, "done")

    def test_a_busy_check_keeps_the_clean_up_of_interrupted_work(self):
        """commit-066: one live task and one interrupted repair - maintenance is refused, the gate stays open, and the
        repair stays marked interrupted (the count's clean-up is not rolled back)."""
        from django.utils import timezone

        from inventory.models import PlatformControl

        c = PlatformControl.objects.create(
            key="dc1.cloud",
            mode="execution",
            consistency={"repair": {"state": "running", "at": timezone.now().isoformat(), "runner": "node-a:1:2"}},
        )
        tasks.Task.objects.create(kind="x", title="t", state="running", runner=tasks.me())
        with (
            mock.patch.object(tasks, "alive", side_effect=lambda r: r != "node-a:1:2"),
            self.assertRaises(RuntimeError),
        ):
            maintenance.close("update-single")
        self.assertEqual(maintenance.state(), (False, ""))
        c.refresh_from_db()
        self.assertEqual(c.consistency["repair"]["state"], "interrupted")

    def test_a_stale_gate_opens_by_itself(self):
        from datetime import timedelta

        from django.utils import timezone

        from .models import MaintenanceGate

        maintenance.close("update-single")
        MaintenanceGate.objects.filter(pk=1).update(since=timezone.now() - timedelta(hours=maintenance.STALE_HOURS + 1))
        self.assertEqual(maintenance.state(), (False, ""))
        tasks.start("x", "x", lambda task: "ok")

    def test_the_page_goes_back_with_the_message(self):
        from django.test import RequestFactory

        mw = maintenance.Middleware(lambda r: None)
        req = RequestFactory().post("/x/", HTTP_REFERER="http://testserver/projects/")
        req.session, req._messages = {}, mock.MagicMock()
        r = mw.process_exception(req, maintenance.MaintenanceRunning("update-single"))
        self.assertEqual((r.status_code, r["Location"]), (302, "http://testserver/projects/"))
        api = RequestFactory().post("/api/x/")
        self.assertEqual(mw.process_exception(api, maintenance.MaintenanceRunning("u")).status_code, 503)
        self.assertIsNone(mw.process_exception(req, ValueError("other")))


class SingleNodeUpdate(TestCase):
    """The single-node update closes the gate before counting and keeps it closed through the backup and the install:
    a deployment submitted meanwhile is refused; the gate opens when the operation ends, done or failed."""

    def run_update(self, during_backup, install=lambda: "installed"):
        state = tempfile.mkdtemp()
        seen = {}

        def backup(_appliance, operation=None):
            seen["backup"] = during_backup()
            seen["backup_operation"] = operation  # option A: the operation's own backup names it
            return "ok"

        def inst(ref, migrate):
            seen["install"] = maintenance.state()
            return install()

        with (
            NodeFile(SITE="dc1", ROLE="active"),
            mock.patch.object(ha_agent, "OP_FILE", os.path.join(state, "op.json")),
            mock.patch.object(ha_agent, "STATE_DIR", state),
            mock.patch.object(ha_agent, "_backup_or_fail", side_effect=backup),
            mock.patch.object(ha_agent, "install_ref", side_effect=inst),
        ):
            data = ha_agent.run_op("update-single", by="sa@corp.kz", ref="v9.9.9")
        return data, seen

    def test_work_submitted_during_the_update_is_refused(self):
        def submit():
            try:
                tasks.start("deploy", "a deployment", lambda task: "ok")
            except maintenance.MaintenanceRunning:
                return "refused"
            return "admitted"

        data, seen = self.run_update(submit)
        self.assertEqual(data["state"], "done", data)
        self.assertEqual(seen["backup"], "refused")
        self.assertEqual(seen["install"], (True, "update-single"))  # closed through the install
        self.assertEqual(seen["backup_operation"], "update-single")
        self.assertEqual(maintenance.state(), (False, ""))  # open again

    def test_the_gate_opens_after_a_failure(self):
        def fail():
            raise RuntimeError("install failed")

        data, _seen = self.run_update(lambda: None, install=fail)
        self.assertEqual(data["state"], "failed", data)
        self.assertEqual(maintenance.state(), (False, ""))

    def test_running_work_stops_it_before_anything(self):
        with mock.patch.object(tasks, "running_count", return_value=1):
            data, seen = self.run_update(lambda: None)
        self.assertEqual(data["state"], "failed", data)
        self.assertNotIn("backup", seen)
        self.assertEqual(maintenance.state(), (False, ""))


class Command(TestCase):
    def test_manage_py_maintenance(self):
        from io import StringIO

        from django.core.management import CommandError, call_command

        out = StringIO()
        call_command("maintenance", "close", "add-node", stdout=out)
        self.assertEqual(out.getvalue().strip(), "closed")
        with self.assertRaises(maintenance.MaintenanceRunning):
            tasks.start("x", "x", lambda task: "ok")
        call_command("maintenance", "open", "add-node", stdout=StringIO())
        with mock.patch.object(tasks, "running_count", return_value=3), self.assertRaises(CommandError):
            call_command("maintenance", "close", "add-node", stdout=StringIO())
        self.assertEqual(maintenance.state(), (False, ""))


@override_settings(DEPLOY_ASYNC=False)
class OutsideATransaction(TransactionTestCase):
    """commit-043: the gate's lock is taken in a transaction also when the starter has none (a TestCase wraps every
    test in one, which hid it; MySQL refuses SELECT ... FOR UPDATE outside a transaction)."""

    def test_every_starter_locks_inside_a_transaction(self):
        from django.db import transaction

        from tenants import deploy, takeover
        from tenants.models import Organization, Tenant

        org = Organization.objects.create(name="Acme")
        t = Tenant.objects.create(organization=org, name="dev", title="dev")
        real, seen = maintenance._row, []

        def row(lock=False):
            if lock:
                seen.append(transaction.get_connection().in_atomic_block)
            return real(lock)

        with mock.patch.object(maintenance, "_row", side_effect=row):
            deploy.new_deployment(t, None, False)
            tasks.start("x", "x", lambda task: "ok")
            with mock.patch.object(takeover, "Takeover") as tk, mock.patch.object(takeover, "ensure_order"):
                tk.return_value.run_igw.side_effect = lambda dep: dep
                takeover.start(t, "igw")
        self.assertEqual(seen, [True, True, True])
        with self.assertRaises(RuntimeError):
            maintenance.admit()  # outside a transaction: refused, not a lock that ends at once


@override_settings(DEPLOY_ASYNC=False)
class ConsistencyRepair(TestCase):
    def run(self, result=None):
        # the repairs' lock files (commit-058) in a folder of the test's own
        with tempfile.TemporaryDirectory() as d, override_settings(MEDIA_ROOT=os.path.join(d, "media")):
            return super().run(result)

    """core-046 (reopened, twenty-fourth full review): the platform's consistency repair (Make consistent) deletes what
    archived projects left - admitted only while no maintenance runs, counted while it runs, reaped when its process
    ended."""

    def setUp(self):
        from inventory.models import PlatformControl

        self.c = PlatformControl.objects.create(key="dc1.cloud", mode="execution", consistency={"leftovers": []})

    def test_a_closed_gate_refuses_a_repair(self):
        from tenants import consistency

        maintenance.close("update-single")
        with mock.patch.object(consistency, "repair") as rep, self.assertRaises(maintenance.MaintenanceRunning):
            consistency.start_repair(self.c)
        rep.assert_not_called()
        self.c.refresh_from_db()
        self.assertNotIn("repair", self.c.consistency)

    def test_a_running_repair_is_counted_and_stops_maintenance(self):
        from tenants import consistency

        def during(pk, user_id=None):
            self.assertEqual(tasks.running_count(), 1)
            with self.assertRaises(RuntimeError):
                maintenance.close("update-single")  # work runs: the update does not start
            consistency.start_repair(self.c)  # a second Make consistent meanwhile starts nothing

        with mock.patch.object(consistency, "repair", side_effect=during) as rep:
            consistency.start_repair(self.c)
        self.assertEqual(rep.call_count, 1)
        self.assertEqual(maintenance.state(), (False, ""))

    def test_an_interrupted_repair_is_reaped(self):
        from django.utils import timezone

        self.c.consistency = {"repair": {"state": "running", "at": timezone.now().isoformat(), "runner": "host:1:2"}}
        self.c.save()
        with mock.patch.object(tasks, "alive", return_value=False):
            self.assertEqual(tasks.reap_repairs(), 1)
        self.c.refresh_from_db()
        self.assertEqual(self.c.consistency["repair"]["state"], "interrupted")
        self.assertEqual(tasks.repairs_running(), [])

    def test_a_repair_that_raises_early_ends_failed(self):
        """core-050 (twenty-fifth full review): repair() raising before its own last write (the database away while it
        loads the platform) ends the mark: not counted, and Make consistent can run again."""
        from django.db import OperationalError

        from tenants import consistency

        with mock.patch.object(consistency, "repair", side_effect=OperationalError("gone")):
            consistency.start_repair(self.c)
        self.c.refresh_from_db()
        self.assertEqual(self.c.consistency["repair"]["state"], "failed")
        self.assertIn("OperationalError", self.c.consistency["repair"]["done"][0])
        self.assertEqual(tasks.running_count(), 0)
        maintenance.close("update-single")  # maintenance is not held
        maintenance.open_("update-single")
        with mock.patch.object(consistency, "repair", return_value=[]) as rep:
            consistency.start_repair(self.c)
        rep.assert_called_once()

    def test_a_repair_whose_thread_ended_is_reaped_by_any_process(self):
        """core-050, commit-058: the failure could not be written either - the repair's process lives (alive() True,
        commit-059: mocked, not /proc), its thread's lock is gone: reaped, whichever process looks."""
        from django.utils import timezone

        rep = {"state": "running", "at": timezone.now().isoformat(), "runner": "node-a:1:2", "attempt": "a1"}
        self.c.consistency = {"repair": rep}
        self.c.save()
        with mock.patch.object(tasks, "alive", return_value=True):
            self.assertEqual(tasks.running_count(), 0)
        self.c.refresh_from_db()
        self.assertEqual(self.c.consistency["repair"]["state"], "interrupted")
        self.assertEqual(self.c.consistency["repair"]["done"], [tasks.REPAIR_ENDED])

    def test_a_running_repair_is_seen_from_another_process(self):
        """commit-058: the lock a running repair holds is seen by another process (here a child holding it, as the
        repair's thread does); once that process lets go, the attempt is over."""
        import subprocess
        import sys

        from django.utils import timezone

        from tenants import consistency

        path = consistency._lock_path("b2")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import fcntl, os, sys; fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600); "
                "fcntl.flock(fd, fcntl.LOCK_EX); print('held', flush=True); sys.stdin.read()",
                path,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(child.kill)
        self.assertEqual(child.stdout.readline().strip(), "held")
        self.c.consistency = {
            "repair": {"state": "running", "at": timezone.now().isoformat(), "runner": "node-a:1:2", "attempt": "b2"}
        }
        self.c.save()
        with mock.patch.object(tasks, "alive", return_value=True):
            self.assertTrue(consistency.attempt_running("b2"))
            self.assertEqual(tasks.running_count(), 1)  # running: kept
            child.stdin.close()
            child.wait(10)
            self.assertFalse(consistency.attempt_running("b2"))
            self.assertEqual(tasks.running_count(), 0)

    def test_a_second_repair_releases_its_lock_once(self):
        """commit-067: a repair asked for while one runs closes its own lock once, after the admission ends; a failed
        admission end closes it once as well."""
        from tenants import consistency

        closes = []
        real = consistency._release

        def release(attempt, fd):
            closes.append(attempt)
            real(attempt, fd)

        def during(pk, user_id=None):
            consistency.start_repair(self.c)  # the second: nothing started

        with (
            mock.patch.object(consistency, "_release", side_effect=release),
            mock.patch.object(consistency, "repair", side_effect=during),
        ):
            consistency.start_repair(self.c)
        self.assertEqual(len(closes), 2)
        self.assertEqual(len(set(closes)), 2)  # each attempt's own, once
        # commit-073: the case of commit-067 itself - another repair already runs, the duplicate branch is taken, and
        # the admission's transaction fails on its way out: the lock released once, the error passed on
        import contextlib

        from django.utils import timezone

        closes.clear()
        self.c.consistency = {
            "repair": {"state": "running", "at": timezone.now().isoformat(), "runner": "node-a:1:2", "attempt": "z9"}
        }
        self.c.save()

        @contextlib.contextmanager
        def failing_exit():
            yield
            raise RuntimeError("commit failed")

        with (
            mock.patch.object(consistency, "_release", side_effect=release),
            mock.patch.object(maintenance, "admitting", side_effect=failing_exit),
            self.assertRaises(RuntimeError) as e,
        ):
            consistency.start_repair(self.c)
        self.assertEqual(str(e.exception), "commit failed")
        self.assertEqual(len(closes), 1)

    def test_a_failed_lock_leaves_nothing(self):
        """commit-068: flock failing closes the descriptor and removes the file."""
        from tenants import consistency

        seen = []

        def flock(fd, op):
            seen.append(fd)
            raise OSError("no locks")

        with mock.patch.object(consistency.fcntl, "flock", side_effect=flock), self.assertRaises(OSError):
            consistency._hold("c3")
        self.assertFalse(os.path.exists(consistency._lock_path("c3")))
        # commit-074: the descriptor is closed too (fstat on it fails)
        with self.assertRaises(OSError):
            os.fstat(seen[0])

    def test_a_finished_repair_leaves_no_lock(self):
        from tenants import consistency

        with mock.patch.object(consistency, "repair", return_value=[]):
            consistency.start_repair(self.c)
        self.assertEqual(os.listdir(os.path.dirname(consistency._lock_path("x"))), [])

    def test_a_refresh_read_before_the_repair_keeps_its_mark(self):
        """commit-045: a platform refresh read the control before the repair started, and saves its check after - the
        repair's "running" mark (what maintenance counts) stays, and a second repair is still refused."""
        from inventory.models import PlatformControl
        from tenants import consistency

        stale = PlatformControl.objects.get(pk=self.c.pk)

        def during(pk, user_id=None):
            with mock.patch.object(consistency, "_active", return_value=[]):
                consistency.check(stale, one=mock.MagicMock())  # the refresh, saving from its old copy
            self.assertEqual(tasks.running_count(), 1)
            self.assertEqual(len(tasks.repairs_running()), 1)

        with mock.patch.object(consistency, "repair", side_effect=during):
            consistency.start_repair(self.c)


class GateOwnership(TestCase):
    def test_a_second_half_that_finds_work_leaves_the_first_halfs_gate(self):
        """commit-056: the closure rolled back - the gate is again the first half's, not opened."""
        maintenance.close("update-portal")
        with mock.patch.object(tasks, "running_count", return_value=1), self.assertRaises(RuntimeError):
            maintenance.close("update-portal-finish")
        self.assertEqual(maintenance.state(), (True, "update-portal"))
        maintenance.open_("update-portal")

    def test_the_first_half_does_not_open_the_second_halfs_gate(self):
        """commit-046: an update's second half took the gate (update-portal-finish) and counted the work; the first
        half's clean-up, arriving after, leaves the gate closed - only its holder opens it."""
        maintenance.close("update-portal")
        maintenance.close("update-portal-finish")  # the second half, on the other node
        maintenance.open_("update-portal")  # the first half ends later
        self.assertEqual(maintenance.state(), (True, "update-portal-finish"))
        with self.assertRaises(maintenance.MaintenanceRunning):
            tasks.start("x", "x", lambda task: "ok")
        maintenance.open_("update-portal-finish")
        self.assertEqual(maintenance.state(), (False, ""))
