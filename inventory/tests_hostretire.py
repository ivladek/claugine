"""Retiring a host (owner 2026-11-20 10:15-10:17; inventory/hostretire.py)."""

import xml.etree.ElementTree as ET
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from accounts.tests import Base
from inventory import hostdeploy, hostretire
from inventory.models import HostDeployment
from inventory.tests_hostdeploy import KEY, world

CL = "shared-intel-1_zk1"


def PlatformDatastore_exists(platform, name):
    from inventory.models import PlatformDatastore

    return PlatformDatastore.objects.filter(platform=platform, name=name).exists()


class FakeOne:
    """Hosts {id: {name, cluster, state, max_cpu, cpu, max_mem, mem}} and VMs {id: {host, state, lcm, cpu, mem}}."""

    def __init__(self, hosts, vms, fail=(), ds=None, images=None):
        self.hosts, self.vms, self.fail, self.calls = hosts, vms, set(fail), []
        self.ds, self.images = ds or {100: "cloud-vms0"}, images or {}  # {id: name}, {id: (name, ds id)}

    def _vmx(self, i, v):
        disks = "".join(f"<DISK><DATASTORE_ID>{d}</DATASTORE_ID></DISK>" for d in v.get("disks", ()))
        name = self.hosts.get(v["host"], {}).get("name", "")
        return (
            f"<VM><ID>{i}</ID><NAME>vm{i}</NAME><STATE>{v['state']}</STATE><LCM_STATE>{v['lcm']}</LCM_STATE>"
            f"<TEMPLATE><CPU>{v['cpu']}</CPU><MEMORY>{v['mem']}</MEMORY>{disks}</TEMPLATE><HISTORY_RECORDS><HISTORY>"
            f"<HID>{v['host']}</HID><HOSTNAME>{name}</HOSTNAME><DS_ID>{v.get('sys', 100)}</DS_ID></HISTORY>"
            "</HISTORY_RECORDS></VM>"
        )

    def _host(self, i, h):
        vms = "".join(f"<ID>{v}</ID>" for v, x in self.vms.items() if x["host"] == i and x["state"] != "6")
        return (
            f"<HOST><ID>{i}</ID><NAME>{h['name']}</NAME><CLUSTER>{h['cluster']}</CLUSTER><STATE>{h['state']}</STATE>"
            f"<HOST_SHARE><MAX_CPU>{h['max_cpu']}</MAX_CPU><CPU_USAGE>{h['cpu']}</CPU_USAGE>"
            f"<MAX_MEM>{h['max_mem']}</MAX_MEM><MEM_USAGE>{h['mem']}</MEM_USAGE></HOST_SHARE><VMS>{vms}</VMS></HOST>"
        )

    def xml(self, method, *args):
        if method == "one.hostpool.info":
            return ET.fromstring(
                "<HOST_POOL>" + "".join(self._host(i, h) for i, h in self.hosts.items()) + "</HOST_POOL>"
            )
        if method == "one.host.info":
            return ET.fromstring(self._host(args[0], self.hosts[args[0]]))
        if method == "one.vm.info":
            return ET.fromstring(self._vmx(args[0], self.vms[args[0]]))
        if method == "one.vmpool.info":
            return ET.fromstring(
                "<VM_POOL>" + "".join(self._vmx(i, v) for i, v in self.vms.items() if v["state"] != "6") + "</VM_POOL>"
            )
        if method == "one.datastorepool.info":
            return ET.fromstring(
                "<DATASTORE_POOL>"
                + "".join(
                    f"<DATASTORE><ID>{i}</ID><NAME>{n}</NAME><TYPE>1</TYPE></DATASTORE>" for i, n in self.ds.items()
                )
                + "</DATASTORE_POOL>"
            )
        if method == "one.imagepool.info":
            return ET.fromstring(
                "<IMAGE_POOL>"
                + "".join(
                    f"<IMAGE><ID>{i}</ID><NAME>{n}</NAME><DATASTORE_ID>{d}</DATASTORE_ID><DATASTORE>{self.ds[d]}</DATASTORE>"
                    "</IMAGE>"
                    for i, (n, d) in self.images.items()
                )
                + "</IMAGE_POOL>"
            )
        raise AssertionError(method)

    def find(self, pool_method, tag, name, *args):
        for el in self.xml(pool_method).findall(tag):
            if el.findtext("NAME") == name:
                return el
        return None

    def call(self, method, *args):
        self.calls.append((method, *args))
        if method == "one.host.status":
            self.hosts[args[0]]["state"] = "4" if args[1] == 1 else "2"
        elif method == "one.vm.migrate":
            vid, to = args[0], args[1]
            if vid not in self.fail:
                self.vms[vid]["host"] = to
        elif method == "one.host.delete":
            del self.hosts[args[0]]
        elif method == "one.vm.action":
            self.vms[args[1]]["state"] = "6"
        elif method == "one.image.delete":
            del self.images[args[0]]
        elif method == "one.datastore.delete":
            del self.ds[args[0]]
        return 0


class FakeIlo:
    def __init__(self, graceful=True):
        self.on, self.graceful, self.posts = True, graceful, []

    def get(self, path):
        return {"PowerState": "On" if self.on else "Off"}

    def post(self, path, body):
        self.posts.append(body["ResetType"])
        if body["ResetType"] == "ForceOff" or (body["ResetType"] == "GracefulShutdown" and self.graceful):
            self.on = False
        return {}


def hosts(free_mem=100 * 1048576):
    return {
        12: {
            "name": "kvm014",
            "cluster": CL,
            "state": "2",
            "max_cpu": 128000,
            "cpu": 400,
            "max_mem": 2 * 10**9,
            "mem": 0,
        },
        8: {
            "name": "kvm010",
            "cluster": CL,
            "state": "2",
            "max_cpu": 102400,
            "cpu": 0,
            "max_mem": free_mem,
            "mem": 0,
        },
        9: {
            "name": "kvm011",
            "cluster": "other",
            "state": "2",
            "max_cpu": 102400,
            "cpu": 0,
            "max_mem": 10**12,
            "mem": 0,
        },
    }


@KEY
class Retire(TestCase):
    def setUp(self):
        self.h, self.c = world()
        self.h.state = "deployed"
        self.h.save()
        self.c.hosts.add(self.h)

    def go(self, one, c=None):
        d = HostDeployment.objects.create(host=self.h, cluster=self.c, kind=HostDeployment.Kind.RETIRE)
        c = c or FakeIlo()
        with (
            mock.patch.object(hostretire, "_one", return_value=one),
            mock.patch.object(hostdeploy.Run, "c", new_callable=mock.PropertyMock, return_value=c),
        ):
            ok = hostretire.run(d, sleep=lambda s: None)
        d.refresh_from_db()
        return ok, d, c

    def test_all_steps(self):
        one = FakeOne(
            hosts(),
            {
                101: {"host": 12, "state": "3", "lcm": "3", "cpu": 2, "mem": 4096},
                102: {"host": 12, "state": "8", "lcm": "0", "cpu": 1, "mem": 1024},
            },
        )
        ok, d, c = self.go(one)
        self.assertTrue(ok, d.log)
        self.assertEqual(d.state, "done")
        migrates = [x for x in one.calls if x[0] == "one.vm.migrate"]
        self.assertEqual(
            migrates, [("one.vm.migrate", 101, 8, True, True, -1, 0), ("one.vm.migrate", 102, 8, False, True, -1, 0)]
        )
        self.assertIn(("one.host.status", 12, 1), one.calls)
        self.assertNotIn(12, one.hosts)  # deleted from OpenNebula
        self.assertEqual(c.posts, ["GracefulShutdown"])
        self.h.refresh_from_db()
        self.assertEqual(self.h.state, "retired")

    def test_a_vm_that_does_not_move_stops_the_run(self):
        one = FakeOne(
            hosts(),
            {
                101: {"host": 12, "state": "3", "lcm": "3", "cpu": 2, "mem": 4096},
                102: {"host": 12, "state": "3", "lcm": "3", "cpu": 1, "mem": 1024},
            },
            fail={101},
        )
        ok, d, c = self.go(one)
        self.assertFalse(ok)
        self.assertEqual(d.steps["migrate"]["status"], "failed")
        self.assertIn("VM #101 did not move", d.steps["migrate"]["detail"])
        self.assertIn(12, one.hosts)  # not deleted
        self.assertEqual(c.posts, [])  # not powered off
        self.assertFalse([x for x in one.calls if x[0] == "one.vm.migrate" and x[1] == 102])  # the next one not tried

    def test_not_enough_room_stops_before_anything(self):
        one = FakeOne(hosts(free_mem=1024 * 1024), {101: {"host": 12, "state": "3", "lcm": "3", "cpu": 2, "mem": 4096}})
        ok, d, _c = self.go(one)
        self.assertFalse(ok)
        self.assertEqual(d.steps["checks"]["status"], "failed")
        self.assertEqual(one.calls, [])

    def test_a_vm_neither_running_nor_off_stops_the_checks(self):
        one = FakeOne(hosts(), {101: {"host": 12, "state": "5", "lcm": "0", "cpu": 1, "mem": 1024}})
        ok, d, _c = self.go(one)
        self.assertFalse(ok)
        self.assertIn("neither running nor powered off", d.steps["checks"]["detail"])

    def test_each_vm_must_fit_on_one_host(self):  # core-053
        hs = hosts()
        hs[8]["max_mem"] = 8 * 1048576
        hs[10] = {**hs[8], "name": "kvm012"}
        one = FakeOne(hs, {101: {"host": 12, "state": "3", "lcm": "3", "cpu": 1, "mem": 12 * 1024}})
        ok, d, _c = self.go(one)
        self.assertFalse(ok)
        self.assertIn("fits on no single enabled host", d.steps["checks"]["detail"])
        self.assertEqual(one.calls, [])  # not disabled

    def test_the_vms_are_placed_one_after_another(self):  # core-053: the room counts down
        hs = hosts()
        hs[8]["max_mem"] = 10 * 1048576
        vms = {v: {"host": 12, "state": "3", "lcm": "3", "cpu": 1, "mem": 6 * 1024} for v in (101, 102)}
        ok, d, _c = self.go(FakeOne(hs, vms))
        self.assertFalse(ok)
        self.assertIn("VM #102", d.steps["checks"]["detail"])

    def test_vms_found_late_are_moved_on_resume(self):  # core-055
        one = FakeOne(hosts(), {})
        real = hostretire.s_migrate

        def late(r):
            out = real(r)
            one.vms[103] = {"host": 12, "state": "3", "lcm": "3", "cpu": 1, "mem": 1024}
            return out

        with mock.patch.dict(hostretire.STEPS, {"migrate": late}):
            ok, d, _c = self.go(one)
        self.assertFalse(ok)
        self.assertEqual((d.steps["empty"]["status"], d.steps["migrate"]["status"]), ("failed", "pending"))
        d.state = HostDeployment.State.RUNNING
        d.save()
        with (
            mock.patch.object(hostretire, "_one", return_value=one),
            mock.patch.object(hostdeploy.Run, "c", new_callable=mock.PropertyMock, return_value=FakeIlo()),
        ):
            self.assertTrue(hostretire.run(d, sleep=lambda s: None))
        self.assertIn(("one.vm.migrate", 103, 8, True, True, -1, 0), one.calls)

    def test_a_host_registered_again_is_not_powered_off(self):  # core-051
        one = FakeOne(hosts(), {})
        real = hostretire.s_one_delete

        def back(r):
            out = real(r)
            one.hosts[40] = {**hosts()[12]}  # the server added to OpenNebula again
            return out

        with mock.patch.dict(hostretire.STEPS, {"one_delete": back}):
            ok, d, c = self.go(one)
        self.assertFalse(ok)
        self.assertIn("again (#40)", d.steps["power_off"]["detail"])
        self.assertEqual(c.posts, [])

    def _local(self):  # commit-207: the host's local storage in the portal, its datastore 120 in OpenNebula
        from inventory.models import PlatformDatastore, Storage, StorageVolume

        st = Storage.objects.create(site=self.h.site, name="local-kvm014", kind="local", host=self.h)
        vol = StorageVolume.objects.create(storage=st, name="local-1", mount="/var/lib/one/datastores")
        PlatformDatastore.objects.create(platform=self.c.platform, name="images-local", kind="images", volume=vol)
        return st

    def _with_local(self):
        return FakeOne(
            hosts(),
            {
                101: {"host": 12, "state": "3", "lcm": "3", "cpu": 1, "mem": 1024, "disks": [120]},  # deleted
                102: {"host": 8, "state": "3", "lcm": "3", "cpu": 1, "mem": 1024, "disks": [120]},  # elsewhere: deleted
                103: {"host": 12, "state": "3", "lcm": "3", "cpu": 1, "mem": 1024, "disks": [101]},  # moved
            },
            ds={100: "cloud-vms0", 101: "cloud-imag", 120: "images-local"},
            images={7: ("ubuntu-local", 120), 8: ("ubuntu", 101)},
        )

    def go_wipe(self, one, wipe):
        d = HostDeployment.objects.create(
            host=self.h, cluster=self.c, kind=HostDeployment.Kind.RETIRE, data={"wipe": wipe}
        )
        c = FakeIlo()
        with (
            mock.patch.object(hostretire, "_one", return_value=one),
            mock.patch.object(hostdeploy.Run, "c", new_callable=mock.PropertyMock, return_value=c),
        ):
            ok = hostretire.run(d, sleep=lambda s: None)
        d.refresh_from_db()
        return ok, d, c

    def test_the_local_storage_is_listed_and_deleted_when_confirmed(self):
        st = self._local()
        one = self._with_local()
        local = hostretire.local_items(one, self.h, self.c)
        self.assertEqual([x[0] for x in local["datastores"]], [120])
        self.assertEqual([x[0] for x in local["images"]], [7])
        self.assertEqual([x[0] for x in local["vms"]], [101, 102])
        ok, d, c = self.go_wipe(one, local["key"])
        self.assertTrue(ok, d.log)
        self.assertEqual((one.vms[101]["state"], one.vms[102]["state"]), ("6", "6"))
        self.assertEqual(one.vms[103]["host"], 8)  # moved, not deleted
        self.assertNotIn(7, one.images)
        self.assertIn(8, one.images)
        self.assertEqual(set(one.ds), {100, 101})
        self.assertFalse([x for x in one.calls if x[0] == "one.vm.migrate" and x[1] in (101, 102)])
        self.assertFalse(PlatformDatastore_exists(self.c.platform, "images-local"))
        st.refresh_from_db()
        self.assertEqual(st.state, "retired")
        self.assertEqual(c.posts, ["GracefulShutdown"])

    def test_nothing_is_deleted_without_the_confirmation(self):
        self._local()
        one = self._with_local()
        ok, d, _c = self.go_wipe(one, None)
        self.assertFalse(ok)
        self.assertIn("not what was confirmed", d.steps["checks"]["detail"])
        self.assertEqual(one.calls, [])

    def test_an_image_added_after_the_confirmation_stops_the_deletion(self):
        self._local()
        one = self._with_local()
        key = hostretire.local_items(one, self.h, self.c)["key"]
        real = hostretire.s_migrate

        def more(r):
            out = real(r)
            one.images[9] = ("new", 120)
            return out

        with mock.patch.dict(hostretire.STEPS, {"migrate": more}):
            ok, d, _c = self.go_wipe(one, key)
        self.assertFalse(ok)
        self.assertIn("not confirmed: image #9", d.steps["wipe_images"]["detail"])
        self.assertIn(7, one.images)  # nothing deleted
        self.assertIn(120, one.ds)

    def test_storage_used_by_another_platform_stops_the_run(self):  # commit-207
        from inventory.models import Platform, PlatformDatastore

        st = self._local()
        other = Platform.objects.create(key="other.cloud", site=self.h.site, name="other", kind="shared")
        PlatformDatastore.objects.create(platform=other, name="vms-x", kind="vms", volume=st.volumes.first())
        one = self._with_local()
        key = hostretire.local_items(one, self.h, self.c)["key"]
        ok, d, _c = self.go_wipe(one, key)
        self.assertFalse(ok)
        self.assertIn("other platforms: other.cloud/vms-x", d.steps["checks"]["detail"])
        self.assertEqual(one.calls, [])

    def test_a_vm_moved_off_the_local_storage_is_not_deleted(self):  # commit-211
        self._local()
        one = self._with_local()
        key = hostretire.local_items(one, self.h, self.c)["key"]
        real = hostretire.s_disable

        def moved(r):
            out = real(r)
            one.vms[102]["disks"] = [101]  # its disk moved to the shared datastore after the confirmation
            return out

        with mock.patch.dict(hostretire.STEPS, {"disable": moved}):
            ok, d, _c = self.go_wipe(one, key)
        self.assertFalse(ok)
        self.assertIn("VM #102 no longer uses", d.steps["wipe_vms"]["detail"])
        self.assertFalse([x for x in one.calls if x[0] == "one.vm.action"])  # none deleted, not even #101

    def test_the_power_is_not_cut_when_the_host_came_back_during_the_shutdown(self):  # core-056
        one = FakeOne(hosts(), {})

        class Back(FakeIlo):
            def post(self, path, body):
                super().post(path, body)
                one.hosts[40] = {**hosts()[12]}  # registered again while it shuts down

        ok, d, c = self.go(one, Back(graceful=False))
        self.assertFalse(ok)
        self.assertIn("the power is not cut", d.steps["power_off"]["detail"])
        self.assertEqual(c.posts, ["GracefulShutdown"])

    def test_resume_stops_on_storage_of_another_platform(self):  # core-059
        from inventory.models import Platform, PlatformDatastore

        st = self._local()
        one = self._with_local()
        key = hostretire.local_items(one, self.h, self.c)["key"]
        real = hostretire.s_disable

        def broke(r):
            real(r)
            raise RuntimeError("stop here")

        with mock.patch.dict(hostretire.STEPS, {"disable": broke}):
            ok, d, _c = self.go_wipe(one, key)
        self.assertFalse(ok)
        other = Platform.objects.create(key="other.cloud", site=self.h.site, name="other", kind="shared")
        PlatformDatastore.objects.create(platform=other, name="vms-x", kind="vms", volume=st.volumes.first())
        d.state = HostDeployment.State.RUNNING
        d.save()
        with mock.patch.object(hostretire, "_one", return_value=one):
            self.assertFalse(hostretire.run(d, sleep=lambda s: None))
        d.refresh_from_db()
        self.assertIn("other platforms", d.steps["disable"]["detail"])
        self.assertFalse([x for x in one.calls if x[0] == "one.vm.action"])

    def test_a_later_vm_moved_while_an_earlier_is_deleted_is_kept(self):  # commit-211, core-057
        self._local()
        one = self._with_local()
        key = hostretire.local_items(one, self.h, self.c)["key"]
        call = one.call

        def moving(method, *args):
            out = call(method, *args)
            if method == "one.vm.action" and args[1] == 101:
                one.vms[102]["disks"] = [101]  # moved to shared storage while #101 is deleted
            return out

        one.call = moving
        ok, d, _c = self.go_wipe(one, key)
        self.assertFalse(ok)
        self.assertIn("VM #102 no longer uses", d.steps["wipe_vms"]["detail"])
        self.assertEqual([x[2] for x in one.calls if x[0] == "one.vm.action"], [101])

    def test_a_datastore_added_during_the_shutdown_keeps_the_power(self):  # commit-207, core-059
        from inventory.models import PlatformDatastore

        one = FakeOne(hosts(), {})  # OpenNebula has datastore 100 "cloud-vms0"
        st = self._local()
        PlatformDatastore.objects.filter(volume__storage=st).update(name="not-in-opennebula")

        class Slow(FakeIlo):
            def post(self, path, body):
                super().post(path, body)
                if body["ResetType"] == "GracefulShutdown":  # placed on the host's storage during the wait
                    PlatformDatastore.objects.filter(volume__storage=st).update(name="cloud-vms0")

        ok, d, c = self.go(one, Slow(graceful=False))
        self.assertFalse(ok)
        self.assertIn("the power is not cut", d.steps["power_off"]["detail"])
        self.assertEqual(c.posts, ["GracefulShutdown"])

    def test_one_read_decides_each_deletion(self):  # commit-211, core-057: no second read before terminate-hard
        self._local()
        one = self._with_local()
        key = hostretire.local_items(one, self.h, self.c)["key"]
        xml, seq = one.xml, []

        def track(method, *args):
            if method == "one.vm.info":
                seq.append(("info", args[0]))
            return xml(method, *args)

        call = one.call

        def track_call(method, *args):
            if method == "one.vm.action":
                seq.append(("terminate", args[1]))
            return call(method, *args)

        one.xml, one.call = track, track_call
        ok, d, _c = self.go_wipe(one, key)
        self.assertTrue(ok, d.log)
        for vid in (101, 102):
            i = seq.index(("terminate", vid))
            self.assertEqual(seq[i - 1], ("info", vid))
            self.assertNotEqual(seq[i - 2], ("info", vid))  # the read before it is another VM's, not a second look

    def test_an_off_host_back_in_opennebula_is_not_retired(self):  # commit-216, core-062
        one = FakeOne(hosts(), {})
        c = FakeIlo()
        c.on = False
        one.hosts[40] = one.hosts.pop(12)  # deleted by an earlier run, registered again while off
        d = HostDeployment.objects.create(
            host=self.h,
            cluster=self.c,
            kind=HostDeployment.Kind.RETIRE,
            data={"one_id": 12},
            steps={k: {"status": "done"} for k in hostretire.STEP_KEYS[:-2]},
        )
        with (
            mock.patch.object(hostretire, "_one", return_value=one),
            mock.patch.object(hostdeploy.Run, "c", new_callable=mock.PropertyMock, return_value=c),
        ):
            self.assertFalse(hostretire.run(d, sleep=lambda s: None))
        d.refresh_from_db()
        self.assertIn("again (#40)", d.steps["power_off"]["detail"])
        self.h.refresh_from_db()
        self.assertEqual(self.h.state, "deployed")

    def test_the_power_is_cut_when_the_shutdown_does_not_end(self):
        one = FakeOne(hosts(), {})
        ok, d, c = self.go(one, FakeIlo(graceful=False))
        self.assertTrue(ok, d.log)
        self.assertEqual(c.posts, ["GracefulShutdown", "ForceOff"])


@KEY
class Abandon(TestCase):  # core-052
    def setUp(self):
        self.h, self.c = world()

    def test_only_a_failed_run_with_no_worker_is_abandoned(self):
        d = HostDeployment.objects.create(
            host=self.h, cluster=self.c, kind=HostDeployment.Kind.RETIRE, state=HostDeployment.State.FAILED
        )
        with mock.patch("accounts.tasks.running", return_value=object()):
            with self.assertRaisesMessage(hostdeploy.DeployError, "nothing abandoned"):
                hostdeploy.abandon(None, self.h)
        d.refresh_from_db()
        self.assertEqual(d.state, "failed")
        with mock.patch("accounts.tasks.running", return_value=None):
            self.assertEqual(hostdeploy.abandon(None, self.h).pk, d.pk)
        d.refresh_from_db()
        self.assertEqual(d.state, "abandoned")
        HostDeployment.objects.filter(pk=d.pk).update(state=HostDeployment.State.RUNNING)
        with mock.patch("accounts.tasks.running", return_value=None):
            self.assertIsNone(hostdeploy.abandon(None, self.h))  # a resumed run is not touched
        d.refresh_from_db()
        self.assertEqual(d.state, "running")

    def test_a_live_worker_refuses_a_new_run(self):
        with mock.patch("accounts.ha.forbid_writes"), mock.patch("accounts.tasks.running", return_value=object()):
            with self.assertRaisesMessage(hostdeploy.DeployError, "running"):
                hostretire.start(None, self.h, self.c)
        self.assertFalse(HostDeployment.objects.filter(host=self.h).exists())


@KEY
class NewConfirmation(TestCase):  # core-058
    def setUp(self):
        self.h, self.c = world()

    def test_a_different_confirmation_starts_a_new_run(self):
        old = HostDeployment.objects.create(
            host=self.h,
            cluster=self.c,
            kind=HostDeployment.Kind.RETIRE,
            state=HostDeployment.State.FAILED,
            data={"wipe": "datastores:120;images:7;vms:101,102"},
            steps={"checks": {"status": "done"}},
        )
        with (
            mock.patch("accounts.ha.forbid_writes"),
            mock.patch("accounts.tasks.running", return_value=None),
            mock.patch("accounts.tasks.start"),
        ):
            same = hostretire.start(None, self.h, self.c, wipe="datastores:120;images:7;vms:101,102")
            self.assertEqual(same.pk, old.pk)  # the same confirmation resumes it
            HostDeployment.objects.filter(pk=old.pk).update(state=HostDeployment.State.FAILED)
            new = hostretire.start(None, self.h, self.c, wipe="datastores:120;images:7;vms:101")
        self.assertNotEqual(new.pk, old.pk)
        self.assertEqual(new.steps, {})
        old.refresh_from_db()
        self.assertEqual(old.state, "abandoned")

    def test_a_refused_confirmation_leaves_an_interrupted_run_failed(self):  # commit-217, core-063
        old = HostDeployment.objects.create(
            host=self.h,
            cluster=self.c,
            kind=HostDeployment.Kind.RETIRE,
            state=HostDeployment.State.RUNNING,  # its worker ended with the portal's process
            data={"wipe": "datastores:120;images:7;vms:101", "claim": "gone", "claim_at": "2026-01-01T00:00:00+00:00"},
            steps={"checks": {"status": "done"}, "disable": {"status": "done"}, "wipe_vms": {"status": "running"}},
        )
        with (
            mock.patch("accounts.ha.forbid_writes"),
            mock.patch("accounts.tasks.running", return_value=None),
            mock.patch("accounts.tasks.start") as tstart,
        ):
            with self.assertRaisesMessage(hostdeploy.DeployError, "use Resume"):
                hostretire.start(None, self.h, self.c, wipe=hostretire.local_items_none())
        old.refresh_from_db()
        self.assertEqual((old.state, old.steps["wipe_vms"]["status"]), ("failed", "failed"))  # Resume shown
        tstart.assert_not_called()

    def test_a_run_that_has_deleted_is_kept_for_its_resume(self):  # core-060, commit-213
        old = HostDeployment.objects.create(
            host=self.h,
            cluster=self.c,
            kind=HostDeployment.Kind.RETIRE,
            state=HostDeployment.State.FAILED,
            data={"wipe": "datastores:120;images:7;vms:101"},
            steps={"checks": {"status": "done"}, "disable": {"status": "done"}, "wipe_vms": {"status": "failed"}},
        )
        with (
            mock.patch("accounts.ha.forbid_writes"),
            mock.patch("accounts.tasks.running", return_value=None),
            mock.patch("accounts.tasks.start"),
        ):
            with self.assertRaisesMessage(hostdeploy.DeployError, "use Resume"):
                hostretire.start(None, self.h, self.c, wipe=hostretire.local_items_none())
            old.refresh_from_db()
            self.assertEqual((old.state, old.data["wipe"]), ("failed", "datastores:120;images:7;vms:101"))
            again = hostretire.start(None, self.h, self.c)  # its own Resume
        self.assertEqual(again.pk, old.pk)
        self.assertEqual(again.data["wipe"], "datastores:120;images:7;vms:101")


@KEY
class RetireView(Base):
    def setUp(self):
        super().setUp()
        self.h, self.c = world()
        self.h.state = "deployed"
        self.h.save()
        self.c.hosts.add(self.h)
        self.client.force_login(Base.staff(self, "sa@corp.kz", is_super_admin=True))
        self.url = reverse("inv_host_retire", args=["zk1", self.h.name])

    def test_the_name_must_be_typed(self):
        none = hostretire.local_items_none()
        with mock.patch.object(hostretire, "start") as start:
            self.client.post(self.url, {"confirm_name": "wrong", "wipe": none})
            start.assert_not_called()
            self.client.post(self.url, {"confirm_name": self.h.name, "wipe": none})
            start.assert_called_once()

    def test_the_host_page_shows_what_a_run_was_confirmed_to_delete(self):  # commit-213
        HostDeployment.objects.create(
            host=self.h,
            cluster=self.c,
            kind=HostDeployment.Kind.RETIRE,
            state=HostDeployment.State.FAILED,
            data={
                "local": {
                    "datastores": [[120, "images-local", "1"]],
                    "images": [[7, "ubuntu-local", "x"]],
                    "vms": [[101, "web-1", "kvm014"]],
                }
            },
            steps={"wipe_vms": {"status": "failed"}},
        )
        page = self.client.get(reverse("infra_site", args=["zk1"]) + "?tab=hosts").content.decode()
        for x in ("Confirmed to delete", "web-1", "ubuntu-local", "images-local"):
            self.assertIn(x, page)

    def test_the_page_lists_the_local_storage_and_needs_the_deletion_confirmed(self):  # commit-207
        local = {
            "datastores": [(120, "images-local", "1")],
            "images": [(7, "ubuntu-local", "images-local")],
            "vms": [(101, "web-1", "kvm014")],
            "key": "datastores:120;images:7;vms:101",
        }
        with mock.patch.object(hostretire, "_one_of"), mock.patch.object(hostretire, "local_items", return_value=local):
            page = self.client.get(self.url).content.decode()
        for x in ("images-local", "ubuntu-local", "web-1", 'name="confirm_delete"', local["key"]):
            self.assertIn(x, page)
        with mock.patch.object(hostretire, "start") as start:
            self.client.post(self.url, {"confirm_name": self.h.name, "wipe": local["key"]})
            start.assert_not_called()
            self.client.post(self.url, {"confirm_name": self.h.name, "wipe": local["key"], "confirm_delete": "on"})
            self.assertEqual(start.call_args.kwargs["wipe"], local["key"])


class LogExtra(TestCase):  # owner 2026-11-20 10:59
    def test_the_log_shows_only_what_the_steps_do_not(self):
        from inventory.deploy_views import log_extra

        d = HostDeployment(
            steps={
                "checks": {"status": "done", "detail": "OpenNebula host #12"},
                "migrate": {"status": "failed", "detail": "VM #101 did not move"},
            },
            log="10:00:00 checks: started\n10:00:01 checks: OpenNebula host #12\n10:00:02 migrate: started\n"
            "10:00:03 migrate: VM #101 to host #8 (live)\n10:00:09 migrate: FAILED: VM #101 did not move\n",
        )
        self.assertEqual(log_extra(d), "10:00:03 migrate: VM #101 to host #8 (live)")

    def test_a_long_error_stays_in_the_log(self):  # commit-215, core-061
        from inventory.deploy_views import log_extra

        err = "ssh: " + "x" * 300
        d = HostDeployment(steps={"os": {"status": "failed", "detail": err}}, log=f"10:00:00 os: FAILED: {err}\n")
        self.assertIn(err, log_extra(d))
