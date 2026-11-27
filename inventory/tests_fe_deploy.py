"""T34 B6: a platform's front end (onefe) made again - one node from the zone's leader, every node from the latest
backup (inventory/fe_deploy.py)."""

import contextlib
import gzip
import io
import os
import subprocess
import tarfile
import tempfile
from unittest import mock

from cryptography.fernet import Fernet
from django.test import TestCase, override_settings
from django.utils import timezone

from inventory.tests_base_deploy import BaseWorld, _key

IPS = ["10.0.101.31", "10.0.101.32", "10.0.101.33"]


class FeWorld(BaseWorld):
    """The three front-end VMs on cloud-core and data-nfs; oneadmin's user template (commit-352's marker)."""

    user_tpl = None

    def _call(self, method, *args):
        if method == "one.user.info":
            tpl = "".join(f"<{k}>{v}</{k}>" for k, v in (self.user_tpl or {}).items())
            return f"<USER><ID>0</ID><NAME>oneadmin</NAME><TEMPLATE>{tpl}</TEMPLATE></USER>"
        if method == "one.user.update":
            import re

            self.user_tpl = dict(self.user_tpl or {})
            for k, v in re.findall(r'(\w+) = "([^"]*)"', args[1]):
                self.user_tpl[k] = v
            self.log.append((method, args))
            return 0
        return super()._call(method, *args)

    def __init__(self):
        from tenants.fakeone import World

        super().__init__()
        self.w = World(
            {
                "one.clusterpool.info": "<CLUSTER_POOL><CLUSTER><ID>0</ID><NAME>mgmt_dc1</NAME></CLUSTER></CLUSTER_POOL>",
                "one.system.config": "<OPENNEBULA_CONFIGURATION><FEDERATION><ZONE_ID>0</ZONE_ID></FEDERATION>"
                "</OPENNEBULA_CONFIGURATION>",
                "one.zone.info": "<ZONE><ID>0</ID><NAME>mgmt-dc1</NAME></ZONE>",
            },
            vnets=[
                {"id": 4, "name": "cloud-core_dc1", "ars": [{"TYPE": "IP4", "IP": "10.0.101.1", "SIZE": 250}]},
                {"id": 5, "name": "data-nfs_dc1", "ars": [{"TYPE": "IP4", "IP": "10.0.111.1", "SIZE": 250}]},
            ],
            vms=[
                {
                    "id": 80 + i,
                    "name": f"core-cloud-one{i + 1}_dc1",
                    "nics": [{"network_id": 4, "ip": ip}, {"network_id": 5, "ip": f"10.0.111.{31 + i}"}],
                }
                for i, ip in enumerate(IPS)
            ],
        )


def _tgz(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


CONFIG = _tgz({"etc/one/oned.conf": "FEDERATION = [\n  SERVER_ID = 0\n]\n", "etc/ufw/user.rules": "*filter\n"})


class Zone:
    """The front ends' XML-RPC: each node's Raft state by its address; the leader answers the zone."""

    def __init__(self, leader=IPS[0]):
        self.states = {
            ip: {"state": "follower", "server_id": i, "term": 3, "commit": 100, "index": 100}
            for i, ip in enumerate(IPS)
        }
        self.states[leader]["state"] = "leader"
        self.down = set()
        self.world = None  # set: the old zone answers (one1 leader) while its old VMs run under their names

    def raft(self, addr, session):
        if self.world is not None:  # the lowest old VM running under its name leads

            def up(j):
                vm = self.world.vms.get(80 + j)
                return bool(vm) and vm["state"] == "3" and vm["name"] == f"core-cloud-one{j + 1}_dc1"

            i = IPS.index(addr)
            if up(i):
                return {
                    "state": "leader" if i == min(j for j in range(3) if up(j)) else "follower",
                    "server_id": i,
                    "term": 2,
                    "commit": 50,
                    "index": 50,
                }
            return None
        return None if addr in self.down else dict(self.states[addr])

    def client(self, addr, session):
        zone = self

        class C:
            def _call(self, method, *args):
                if method == "one.system.config":
                    return (
                        "<OPENNEBULA_CONFIGURATION><FEDERATION><ZONE_ID>100</ZONE_ID></FEDERATION>"
                        "</OPENNEBULA_CONFIGURATION>"
                    )
                if method == "one.zone.info":
                    srv = "".join(
                        f"<SERVER><ID>{i}</ID><NAME>sdc1one{i + 1}</NAME><ENDPOINT>http://{ip}:2633/RPC2</ENDPOINT>"
                        "</SERVER>"
                        for i, ip in enumerate(IPS)
                    )
                    return f"<ZONE><ID>100</ID><SERVER_POOL>{srv}</SERVER_POOL></ZONE>"
                raise AssertionError(method)

        assert zone.states[addr]["state"] == "leader"
        return C()

    def leader(self, ip):
        for k, v in self.states.items():
            v["state"] = "leader" if k == ip else "follower"


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode(), DEPLOY_ASYNC=False)
class FeDeploy(TestCase):
    def setUp(self):
        from inventory.models import (
            Cluster,
            CoreService,
            CoreServiceNode,
            Network,
            Platform,
            PlatformControl,
            Secret,
            ServiceImage,
            Site,
        )

        self.site = Site.objects.create(
            code="dc1",
            timezone="Asia/Almaty",
            management={"hosts": ["10.0.0.0/16"], "ports": {"vyos": 55522, "ssh": 55523, "rdp": 55524}},
        )
        from inventory import hostservice
        from inventory.models import Host

        # T36 B5d (QB5-2): the site's time server (a host of the mgmt service, it has the time function)
        Host.objects.create(
            site=self.site,
            name="kca201",
            ip="10.0.100.201",
            service=hostservice.of_slot("mgmt"),
            time_served_at=timezone.now(),
            time_confirmed_by="peer",
        )
        sa = Secret.objects.create(kind="user", name="sa", username="portal")
        sa.put("private_key", _key())
        sa.save()
        self.mgmt = Platform.objects.create(key="dc1.mgmt", site=self.site, name="mgmt", kind="mgmt", sa=sa.ref)
        self.cloud = Platform.objects.create(
            key="dc1.cloud",
            site=self.site,
            name="cloud",
            kind="shared",
            sa=sa.ref,
            fe_vip="10.0.101.30",
            fe={"type": "vm", "nodes": ["sdc1one1", "sdc1one2", "sdc1one3"], "node_ips": IPS},
        )
        Cluster.objects.create(platform=self.mgmt, name="mgmt_dc1", kind="mgmt")
        control = PlatformControl.objects.create(key="dc1.mgmt", mode="execution")
        PlatformControl.objects.create(key="dc1.cloud", mode="execution")
        ServiceImage.objects.create(
            control=control, purpose="base-os", kind="image", name="Ubuntu 26.04", one_id=49, customization="ssh"
        )
        core = Network.objects.create(
            site="dc1",
            name="cloud-core",
            kind="core-cloud",
            interface="bond0",
            vlan=101,
            subnet="10.0.101.0",
            prefix=24,
        )
        nfs = Network.objects.create(
            site="dc1", name="data-nfs", kind="nfs", interface="bond1", vlan=111, subnet="10.0.111.0", prefix=24
        )
        self.svc = CoreService.objects.create(
            site=self.site,
            name="cloud-fe",
            stype="onefe",
            kind="vm",
            platform=self.mgmt,
            serves=self.cloud,
            ssh_port=22,
            ssh_secret=sa.ref,
            vcpu=8,
            ram_gb=32,
            disk_gb=100,
            nic1_network=core,
            more_nics=[{"network": nfs.pk, "gateway": ""}],
            params={"platform": self.cloud.pk},
        )
        self.nodes = [
            CoreServiceNode.objects.create(
                service=self.svc,
                name=f"core-cloud-one{i + 1}_dc1",
                hostname=f"sdc1one{i + 1}",
                address=ip,
                ips=[ip, f"10.0.111.{31 + i}"],
                ssh_port=22,
                secret=sa.ref,
            )
            for i, ip in enumerate(IPS)
        ]
        self.fake = FeWorld()
        self.zone = Zone()

    # --- the parts -----------------------------------------------------------------------------------------------

    def test_offered_for_a_front_end_with_its_platform(self):
        from inventory import base_deploy as bd

        ok, why = bd.offer(self.nodes[2])
        self.assertTrue(ok, why)
        self.svc.serves = self.mgmt
        self.svc.save()
        ok, why = bd.offer(self.nodes[2])
        self.assertFalse(ok)
        self.assertTrue(any("platform it serves" in w for w in why), why)

    def test_the_scripts_are_bash_and_set_the_server_id(self):
        from inventory import fe_deploy as fe

        s = fe.install_script(2, ["opennebula", "opennebula-fireedge"], fe._fallback(self.nodes, self.svc))
        for text in (s, fe.start_script(["opennebula", "opennebula-flow"])):
            r = subprocess.run(["bash", "-n"], input=text, text=True, capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("\\12/", s)
        self.assertIn("onedb restore -f", s)
        self.assertIn("ufw allow proto tcp from 10.0.101.32 to any port 2633,2634", s)
        self.assertIn("--exclude=etc/fstab", s)

    def test_the_database_statements_from_oned_conf(self):
        from inventory import fe_deploy as fe

        with tempfile.TemporaryDirectory() as d:
            conf = os.path.join(d, "oned.conf")
            with open(conf, "w") as fh:
                fh.write(
                    'LOG = [ X = 1 ]\nDB = [ BACKEND = "mysql",\n SERVER = "localhost",\n PORT = 0,\n'
                    ' USER = "oneadmin",\n PASSWD = "a]b[c\'d",\n DB_NAME = "opennebula",\n CONNECTIONS = 25 ]\n'
                )
            r = subprocess.run(
                ["python3", "-"], input=fe.DB_SQL.replace("/etc/one/oned.conf", conf), text=True, capture_output=True
            )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("CREATE DATABASE IF NOT EXISTS `opennebula`;", r.stdout)
        self.assertIn("IDENTIFIED BY 'a]b[c\\'d';", r.stdout)

    def test_backups_carry_the_extra_paths_a_node_has(self):
        from accounts.fe_backup import EXTRA, tar_paths

        text = tar_paths(["/etc/one"])
        self.assertTrue(text.startswith("'/etc/one' $(for p in"))
        for p in EXTRA:
            self.assertIn(f"'{p}'", text)

    # --- one node, from the leader -------------------------------------------------------------------------------

    def ssh_world(self, sync=True, on_start=None):
        self.ssh, self.pushed = [], []
        zone = self.zone

        def run(node, script, timeout=600):
            self.ssh.append((node.name, script))
            if "list-unit-files" in script:
                return "opennebula.service\nopennebula-fireedge.service\nbogus.service\n"
            if "systemctl stop opennebula\n" == script:
                zone.down.add(fe_ip(node))
                others = [ip for ip in IPS if ip != fe_ip(node)]
                zone.leader(others[0])
            if "systemctl start" in script:
                zone.down.discard(fe_ip(node))
                if on_start:
                    on_start(node)
                elif sync:
                    zone.states[fe_ip(node)]["state"] = zone.states[fe_ip(node)]["state"]
            if "RULES" in script:
                return "RULES carried\n"
            return ""

        def fetch(node, remote, out, timeout=3600):
            with open(out, "wb") as fh:
                fh.write(CONFIG if "tar -czf" in remote else gzip.compress(b"-- dump\n"))

        def push(node, path, name):
            self.pushed.append((node.name, name))

        def node_ssh(n):
            def r(script, timeout=300):
                if "firstboot.ok" in script and "elif" in script:
                    return "OK\n"
                return ""

            return r

        if not sync:
            zone.states[IPS[2]]["commit"] = 1
        stack = contextlib.ExitStack()
        for p in (
            mock.patch.multiple(
                "inventory.fe_deploy",
                raft=zone.raft,
                _client=zone.client,
                _run=run,
                _fetch=fetch,
                _push=push,
                session_of=mock.DEFAULT,
                SYNC_TIMEOUT=0,
                LEADER_TIMEOUT=0,
            ),
            mock.patch("inventory.hostlogging.node_ssh", side_effect=node_ssh),
            mock.patch("adoption.collector.client_for", return_value=self.fake),
        ):
            got = stack.enter_context(p)
            if isinstance(got, dict):
                got["session_of"].return_value = "oneadmin:x"
        return stack

    def redeploy(self, node, **kw):
        from accounts import tasks
        from inventory import core_deploy as cd

        with self.ssh_world(**kw):
            return tasks.start("core VM redeploy", "test", lambda t: cd.redeploy(t, node, None, None, False, True))

    def test_a_follower_from_the_leader(self):
        t = self.redeploy(self.nodes[2])
        self.assertEqual(t.state, "done", t.error)
        w = self.fake.w
        self.assertEqual(w.vms[82]["name"], "core-cloud-one3_dc1-old")
        new_id = t.result["new_id"]
        self.assertEqual(w.vms[new_id]["name"], "core-cloud-one3_dc1")
        self.assertEqual(
            self.pushed, [("core-cloud-one3_dc1", "config.tar.gz"), ("core-cloud-one3_dc1", "onedb.sql.gz")]
        )
        install = next(s for n, s in self.ssh if "onedb restore" in s)
        self.assertIn("\\12/", install)  # server 2
        self.assertIn("systemctl enable opennebula opennebula-fireedge", install)  # the leader's units, no others
        self.assertNotIn("bogus", install)
        reset = [(n, s) for n, s in self.ssh if "server-reset" in s]
        self.assertEqual(
            reset, [("core-cloud-one1_dc1", "sudo -u oneadmin -H onezone server-reset 100 2 >/dev/null\n")]
        )
        self.assertNotIn("leader", [s["name"] for s in t.steps])

    def test_the_leader_moves_first(self):
        t = self.redeploy(self.nodes[0])
        self.assertEqual(t.state, "done", t.error)
        self.assertIn("the leadership moved to core-cloud-one2_dc1", [s["detail"] for s in t.steps])
        reset = [n for n, s in self.ssh if "server-reset" in s]
        self.assertEqual(reset, ["core-cloud-one2_dc1"])

    def test_402_a_first_deployment_found_later_changes_nothing(self):
        """commit-402: Deploy (first) - the first lookup has no VM, the front end's own lookup finds it: no leadership
        move, no VM change."""
        from accounts import tasks
        from inventory import base_deploy as bd

        real, seen = bd.plan, []

        def plan(n):
            p = real(n)
            seen.append(p["old_id"])
            return dict(p, old_id=None) if len(seen) == 1 else p

        node = self.nodes[2]  # a follower
        keep = ("name", "state", "nics")
        before = {k: self.fake.w.vms[82].get(k) for k in keep}
        with self.ssh_world(), mock.patch.object(bd, "plan", side_effect=plan):
            t = tasks.start("core VM redeploy", "test", lambda t: bd.redeploy(t, node, None, True, first=True))
        self.assertEqual(t.state, "failed")
        self.assertIn("is on the platform now", t.error)
        self.assertEqual(len(seen), 2)
        self.assertFalse([s for _n, s in self.ssh if "systemctl stop opennebula" in s])
        self.assertEqual({k: self.fake.w.vms[82].get(k) for k in keep}, before)

    def test_402_a_first_deployment_never_moves_the_leadership(self):
        """commit-402: Deploy (first) of the node the zone says leads - it has a VM after all: refused, nothing done."""
        from accounts import tasks
        from inventory import base_deploy as bd

        real = bd.plan
        with self.ssh_world(), mock.patch.object(bd, "plan", side_effect=lambda n: dict(real(n), old_id=None)):
            t = tasks.start("core VM redeploy", "test", lambda t: bd.redeploy(t, self.nodes[0], None, True, first=True))
        self.assertEqual(t.state, "failed")
        self.assertIn("leads the zone", t.error)
        self.assertFalse([s for _n, s in self.ssh if "opennebula" in s or "server-reset" in s])

    def test_402_found_by_the_last_lookup_no_zone_change(self):
        """commit-402: a follower; the two early lookups miss its VM, _redeploy's finds it - no reset, no stop."""
        from accounts import tasks
        from inventory import base_deploy as bd

        real, seen = bd.plan, []

        def plan(n):
            p = real(n)
            seen.append(p["old_id"])
            return dict(p, old_id=None) if len(seen) <= 2 else p

        keep = ("name", "state", "nics")
        before = {k: self.fake.w.vms[82].get(k) for k in keep}
        with self.ssh_world(), mock.patch.object(bd, "plan", side_effect=plan):
            t = tasks.start("core VM redeploy", "test", lambda t: bd.redeploy(t, self.nodes[2], None, True, first=True))
        self.assertEqual(t.state, "failed")
        self.assertIn("is on the platform now", t.error)
        self.assertEqual(len(seen), 3)
        self.assertFalse([s for _n, s in self.ssh if "server-reset" in s or "systemctl stop opennebula" in s])
        self.assertEqual({k: self.fake.w.vms[82].get(k) for k in keep}, before)

    def test_refused_unless_the_two_others_serve(self):
        self.zone.down.add(IPS[1])
        t = self.redeploy(self.nodes[2])
        self.assertEqual(t.state, "failed")
        self.assertIn("the two other nodes must serve", t.error)
        self.assertEqual(self.fake.w.vms[82]["name"], "core-cloud-one3_dc1")
        self.assertEqual(self.fake.w.vms[82]["state"], "3")

    def test_a_node_that_does_not_catch_up_is_rolled_back(self):
        t = self.redeploy(self.nodes[2], sync=False)
        self.assertEqual(t.state, "failed")
        self.assertIn("has not caught up", t.error)
        w = self.fake.w
        self.assertEqual(w.vms[82]["name"], "core-cloud-one3_dc1")
        self.assertEqual(w.vms[82]["state"], "3")
        self.assertEqual([v["state"] for k, v in w.vms.items() if k > 82], ["6"])
        resets = [s for n, s in self.ssh if "server-reset" in s]
        self.assertEqual(len(resets), 2)  # before the new node started, and for the old one back

    # --- every node, from the backup -----------------------------------------------------------------------------

    def restore(self, fail_on=None):
        from accounts import tasks
        from inventory import fe_deploy as fe
        from tenants.models import PortalSettings

        s = PortalSettings.get()
        s.set_secret("fe_backup_password", "pw")
        s.save()
        for ip in IPS:
            self.zone.down.add(ip)
            self.zone.states[ip]["state"] = "candidate"

        def extract(path, password, dest):
            self.assertEqual(
                path, "/b/platform_cloud_10.0.101.30_20261121-0300/platform_cloud_10.0.101.30_20261121-0300.7z"
            )
            for i, ip in enumerate(IPS):
                d = os.path.join(dest, f"{i}-sdc1one{i + 1}-{ip}_{'leader' if i == 1 else 'follower'}")
                os.makedirs(d)
                with open(os.path.join(d, "config.tar.gz"), "wb") as fh:
                    fh.write(CONFIG)
                if i == 1:
                    with open(os.path.join(d, "onedb-zone.sql.gz"), "wb") as fh:
                        fh.write(gzip.compress(b"-- dump\n"))

        started = []

        def on_start(node):
            if fail_on and node.name == fail_on:
                self.zone.world = self.fake.w  # from here the fake zone is the old VMs'
                raise fe.RedeployError("the units did not start")
            started.append(node.name)
            if len(started) == 3:
                self.zone.leader(IPS[1])

        with (
            self.ssh_world(on_start=on_start),
            mock.patch("accounts.backup_archive.extract", side_effect=extract),
            mock.patch.object(fe, "latest_backup", return_value="platform_cloud_10.0.101.30_20261121-0300"),
            mock.patch.object(fe, "_folder", return_value=("/b", s)),
        ):
            return tasks.start("core FE restore", "test", lambda t: fe.restore_all(t, self.svc)), started

    def test_restore_all_from_the_latest_backup(self):
        t, started = self.restore()
        self.assertEqual(t.state, "done", t.error)
        self.svc.refresh_from_db()
        self.assertEqual(self.svc.recovery, {})  # commit-350: cleared once restored
        self.assertEqual(started, ["core-cloud-one2_dc1", "core-cloud-one1_dc1", "core-cloud-one3_dc1"])
        w = self.fake.w
        for i in range(3):
            self.assertEqual(w.vms[80 + i]["name"], f"core-cloud-one{i + 1}_dc1-old")
            self.assertEqual(w.vms[80 + i]["state"], "8")
        self.assertEqual(t.result["leader"], "core-cloud-one2_dc1")
        installs = [s for n, s in self.ssh if "onedb restore" in s]
        self.assertEqual(len(installs), 3)
        self.assertFalse([s for n, s in self.ssh if "server-reset" in s])

    def test_restore_all_rolls_every_node_back(self):
        t, started = self.restore(fail_on="core-cloud-one3_dc1")
        self.assertEqual(t.state, "failed")
        self.assertIn("RedeployError: rolled back:", t.error)
        w = self.fake.w
        for i in range(3):
            self.assertEqual(w.vms[80 + i]["name"], f"core-cloud-one{i + 1}_dc1")
            self.assertEqual(w.vms[80 + i]["state"], "3")
        self.assertEqual({v["state"] for k, v in w.vms.items() if k > 82}, {"6"})
        # commit-345: every new VM gone before any old one runs again
        acts = self.fake.actions
        first_resume = acts.index("resume")
        self.assertEqual(acts[first_resume:].count("terminate-hard"), 0)
        self.assertEqual(acts.count("terminate-hard"), 3)
        self.assertIn("leader core-cloud-one1_dc1", [s["detail"] for s in t.steps if s["name"] == "rollback: zone"][0])
        self.svc.refresh_from_db()
        self.assertEqual(self.svc.recovery, {})  # commit-350: cleared once rolled back

    def test_restore_rollback_removes_a_new_vm_without_an_old_one(self):
        """commit-346"""
        self.fake._call("one.vm.action", "terminate-hard", 80)  # one1 has no VM: its addresses are free
        t, _started = self.restore(fail_on="core-cloud-one3_dc1")
        self.assertEqual(t.state, "failed")
        self.assertIn("RedeployError: rolled back:", t.error)
        self.assertEqual({v["state"] for k, v in self.fake.w.vms.items() if k > 82}, {"6"})
        self.nodes[0].refresh_from_db()
        self.assertIsNone(self.nodes[0].deployed_vm_id)
        self.assertEqual(self.nodes[0].address, IPS[0])

    def test_restore_keeps_the_old_vms_off_when_a_new_one_stays(self):
        """commit-345: a new VM that cannot be removed - no old VM starts; the task says it is not complete"""
        from inventory import fe_deploy as fe

        real = fe._fenced

        def fenced(one, vm_id, timeout=None):
            if vm_id == min(k for k in self.fake.w.vms if k > 82):
                raise fe.RedeployError("still there")
            return real(one, vm_id, timeout)

        with mock.patch.object(fe, "_fenced", side_effect=fenced):
            t, _started = self.restore(fail_on="core-cloud-one3_dc1")
        self.assertIn("RedeployError: not completely rolled back", t.error)
        self.assertNotIn("resume", self.fake.actions)
        for i in range(3):
            self.assertEqual(self.fake.w.vms[80 + i]["state"], "8")
        # commit-350: the record stays on the service; every node action waits for Retry the rollback
        self.svc.refresh_from_db()
        rec = self.svc.recovery
        self.assertEqual(rec["phase"], "incomplete")
        self.assertEqual(sorted(r["old"] for r in rec["nodes"].values()), [80, 81, 82])
        self.assertTrue(all(r["new"] for r in rec["nodes"].values()))
        self.assertTrue(any("still there" in x for x in rec["problems"]))
        t2 = self.redeploy(self.nodes[2])
        self.assertIn("not rolled back completely", t2.error)
        self.assertIn("not rolled back completely", "; ".join(fe.restore_offer(self.svc)[1]))
        from accounts import tasks
        from inventory import core_deploy

        t3 = tasks.start("core VM redeploy", "x", lambda tk: core_deploy.finish(tk, self.nodes[0]))
        self.assertIn("not rolled back completely", t3.error)
        # the retry: every new VM confirmed gone, then the old ones back, the old zone answers, the record cleared
        with self.ssh_world():
            self.zone.world = self.fake.w
            t4 = tasks.start("core FE restore", "retry", lambda tk: fe.retry_rollback(tk, self.svc))
        self.assertEqual(t4.state, "done", t4.error)
        for i in range(3):
            self.assertEqual(self.fake.w.vms[80 + i]["name"], f"core-cloud-one{i + 1}_dc1")
            self.assertEqual(self.fake.w.vms[80 + i]["state"], "3")
        self.assertEqual({v["state"] for k, v in self.fake.w.vms.items() if k > 82}, {"6"})
        self.svc.refresh_from_db()
        self.assertEqual(self.svc.recovery, {})

    def test_restore_keeps_the_old_vms_off_when_removal_is_not_confirmed(self):
        """commit-345: the platform accepted the termination but its status call fails - not proof: old VMs stay off"""
        from adoption.collector import OneError
        from inventory import fe_deploy as fe

        real = self.fake._call
        state = {"terminated": set()}

        def call(method, *args):
            if method == "one.vm.action" and args[0] == "terminate-hard":
                state["terminated"].add(int(args[1]))
                out = real(method, *args)
                self.fake.w.vms[int(args[1])]["state"] = "3"  # still shutting down
                return out
            if method == "one.vm.info" and int(args[0]) in state["terminated"]:
                raise OneError("[one.vm.info] connection refused")
            return real(method, *args)

        def restore_with_flaky_status():
            self.fake._call = call
            return self.restore(fail_on="core-cloud-one3_dc1")

        with mock.patch.object(fe, "FENCE_TIMEOUT", 0):
            t, _started = restore_with_flaky_status()
        self.assertIn("RedeployError: not completely rolled back", t.error)
        self.assertIn("not confirmed gone", t.error)
        self.assertNotIn("resume", self.fake.actions)

    def test_only_a_structured_no_exists_answer_counts_as_gone(self):
        """commit-345: OpenNebula's NO_EXISTS code for this VM's one.vm.info - not a fault quoting the same words"""
        import xmlrpc.client

        from adoption.collector import OneClient, OneError
        from inventory import fe_deploy as fe
        from tenants.deploy import DeployError

        def one_raising(make):
            class One:
                def xml(self, *a):
                    try:
                        make()
                    except OneError as exc:
                        raise DeployError(str(exc)) from exc

            return One()

        cl = OneClient("http://192.0.2.1:2633/RPC2", "s")
        words = "[one.vm.info] Error getting virtual machine [612]."
        with mock.patch.object(cl, "proxy") as proxy:
            proxy.one.vm.info.return_value = [False, words, 0x0400]
            fe._fenced(one_raising(lambda: cl._call("one.vm.info", 612)), 612, timeout=0)  # gone
            proxy.one.vm.info.side_effect = xmlrpc.client.Fault(1, words)
            with self.assertRaises(fe.RedeployError):
                fe._fenced(one_raising(lambda: cl._call("one.vm.info", 612)), 612, timeout=0)
            proxy.one.vm.info.side_effect = None
            proxy.one.vm.info.return_value = [False, words, 0x2000]  # INTERNAL: not proof
            with self.assertRaises(fe.RedeployError):
                fe._fenced(one_raising(lambda: cl._call("one.vm.info", 612)), 612, timeout=0)

    def lost_reply(self, also=None):
        """commit-351: the platform makes one3's VM but its answer never comes back."""
        real = self.fake._call

        def call(method, *args):
            if method == "one.vm.allocate" and 'NAME = "core-cloud-one3_dc1"' in args[0]:
                real(method, *args)
                self.zone.world = self.fake.w
                raise RuntimeError("[one.vm.allocate] the connection was reset")
            if also:
                also(method)
            return real(method, *args)

        self.fake._call = call

    def test_a_new_vm_whose_id_never_came_back_is_found_by_its_tag(self):
        """commit-351"""
        self.lost_reply()
        t, _started = self.restore()
        self.assertIn("RedeployError: rolled back:", t.error)
        w = self.fake.w
        lost = max(w.vms)
        self.assertIn('CLAUGINE_RESTORE = "', w.vms[lost]["template"])
        self.assertEqual(w.vms[lost]["state"], "6")  # removed though the record never had its id
        self.assertEqual({v["state"] for k, v in w.vms.items() if k > 82}, {"6"})
        for i in range(3):
            self.assertEqual((w.vms[80 + i]["name"], w.vms[80 + i]["state"]), (f"core-cloud-one{i + 1}_dc1", "3"))
        self.svc.refresh_from_db()
        self.assertEqual(self.svc.recovery, {})

    def test_an_allocation_still_in_progress_keeps_the_record_pending(self):
        """commit-351: the allocate timed out, the tagged VM appears only after the first rollback read the pool"""
        from accounts import tasks
        from inventory import fe_deploy as fe

        real = self.fake._call
        late = {}

        def call(method, *args):
            if method == "one.vm.allocate" and 'NAME = "core-cloud-one3_dc1"' in args[0]:
                late["template"] = args[0]  # OpenNebula is still making it
                self.zone.world = self.fake.w
                raise RuntimeError("[one.vm.allocate] timed out")
            return real(method, *args)

        self.fake._call = call
        t, _started = self.restore()
        self.assertIn("RedeployError: not completely rolled back", t.error)
        self.assertNotIn("resume", self.fake.actions)
        self.svc.refresh_from_db()
        self.assertEqual(self.svc.recovery["phase"], "incomplete")
        self.assertIn("did not answer the creation", t.error)
        with self.ssh_world():  # still nothing on the platform: no time settles it
            self.zone.world = self.fake.w
            t1 = tasks.start("core FE restore", "retry", lambda tk: fe.retry_rollback(tk, self.svc))
        self.assertIn("Confirm no VM was made", t1.error)
        self.assertNotIn("resume", self.fake.actions)
        vid = real("one.vm.allocate", late["template"], False)  # it appears now
        with self.ssh_world():
            self.zone.world = self.fake.w
            t2 = tasks.start("core FE restore", "retry", lambda tk: fe.retry_rollback(tk, self.svc))
        self.assertEqual(t2.state, "done", t2.error)
        self.assertEqual(self.fake.w.vms[vid]["state"], "6")  # found by its tag and removed
        for i in range(3):
            self.assertEqual(self.fake.w.vms[80 + i]["state"], "3")

    def test_an_operator_confirms_no_vm_was_made(self):
        """commit-351 (owner 11:50): only an operator settles an unanswered creation; then the retry rolls back"""
        from accounts import tasks
        from inventory import fe_deploy as fe

        real = self.fake._call

        def call(method, *args):
            if method == "one.vm.allocate" and 'NAME = "core-cloud-one3_dc1"' in args[0]:
                self.zone.world = self.fake.w
                raise RuntimeError("[one.vm.allocate] timed out")  # never made
            return real(method, *args)

        self.fake._call = call
        t, _s = self.restore()
        self.assertIn("did not answer the creation", t.error)
        [(pk, name, tag)] = fe.Recovery(self.svc).unresolved()
        self.assertEqual((pk, name), (self.nodes[2].pk, "core-cloud-one3_dc1"))
        self.assertFalse(fe.confirm_absent(self.svc, pk, tag=tag[:-1] + "x"))  # commit-359: another allocation's tag
        self.assertTrue(fe.confirm_absent(self.svc, pk, tag=tag))
        self.assertFalse(fe.confirm_absent(self.svc, pk, tag=tag))  # nothing left to confirm
        with self.ssh_world():
            self.zone.world = self.fake.w
            t2 = tasks.start("core FE restore", "retry", lambda tk: fe.retry_rollback(tk, self.svc))
        self.assertEqual(t2.state, "done", t2.error)
        for i in range(3):
            self.assertEqual(self.fake.w.vms[80 + i]["state"], "3")

    def test_a_refused_creation_settles_by_itself(self):
        """commit-351: OpenNebula's own refusal (an error code) says no VM was made"""
        from adoption.collector import OneError
        from inventory import fe_deploy as fe

        real = self.fake._call

        def call(method, *args):
            if method == "one.vm.allocate" and 'NAME = "core-cloud-one3_dc1"' in args[0]:
                self.zone.world = self.fake.w
                raise OneError("[one.vm.allocate] quota exceeded", method=method, code=0x0800)
            return real(method, *args)

        self.fake._call = call
        t, _s = self.restore()
        self.assertIn("RedeployError: rolled back:", t.error)
        self.assertEqual(fe.Recovery(self.svc).rec, {})

    def test_another_installation_under_the_key_is_not_touched(self):
        """commit-352 (owner 11:50): the zone answers with another name - nothing is read as absent, nothing acted on"""
        from accounts import tasks
        from inventory import fe_deploy as fe

        def fenced(one, vm_id, timeout=None):
            raise fe.RedeployError("still there")

        with mock.patch.object(fe, "_fenced", side_effect=fenced):
            t, _s = self.restore(fail_on="core-cloud-one3_dc1")
        self.assertIn("not completely rolled back", t.error)
        self.fake.w.pools["one.zone.info"] = "<ZONE><ID>0</ID><NAME>elsewhere</NAME></ZONE>"
        acts = len(self.fake.actions)
        with self.ssh_world():
            t2 = tasks.start("core FE restore", "retry", lambda tk: fe.retry_rollback(tk, self.svc))
        self.assertIn("not the installation the restore began on", t2.error)
        self.assertEqual(len(self.fake.actions), acts)

    def test_a_vm_found_by_its_tag_is_remembered_by_its_id(self):
        """commit-360: removed, then another rollback step fails - the next retry checks it by id, asks nothing"""
        from accounts import tasks
        from inventory import base_deploy as bd
        from inventory import fe_deploy as fe

        self.lost_reply()
        with mock.patch.object(bd, "_rollback", side_effect=bd.RedeployError("the platform stopped")):
            t, _s = self.restore()
        self.assertIn("not completely rolled back", t.error)
        lost = max(self.fake.w.vms)
        node = fe.Recovery(self.svc).rec["nodes"][str(self.nodes[2].pk)]
        self.assertEqual((node["new"], self.fake.w.vms[lost]["state"]), (lost, "6"))
        self.assertEqual(fe.Recovery(self.svc).unresolved(), [])
        with self.ssh_world():
            self.zone.world = self.fake.w
            t2 = tasks.start("core FE restore", "retry", lambda tk: fe.retry_rollback(tk, self.svc))
        self.assertEqual(t2.state, "done", t2.error)

    def test_the_installation_marker_is_written_once_and_checked(self):
        """commit-352 (owner 12:28): CLAUGINE_INSTALLATION in oneadmin's template - another installation at the same
        URL with the same zone lacks it (or has another): the rollback does nothing there"""
        from accounts import tasks
        from inventory import fe_deploy as fe

        def fenced(one, vm_id, timeout=None):
            raise fe.RedeployError("still there")

        with mock.patch.object(fe, "_fenced", side_effect=fenced):
            t, _s = self.restore(fail_on="core-cloud-one3_dc1")
        self.assertIn("not completely rolled back", t.error)
        marker = self.fake.user_tpl["CLAUGINE_INSTALLATION"]
        self.assertEqual(fe.Recovery(self.svc).rec["identity"]["installation"], marker)
        self.assertEqual(len([c for c in self.fake.log if c[0] == "one.user.update"]), 1)
        self.fake.user_tpl = {"CLAUGINE_INSTALLATION": "another"}  # same URL, same zone, another installation
        acts = len(self.fake.actions)
        with self.ssh_world():
            t2 = tasks.start("core FE restore", "retry", lambda tk: fe.retry_rollback(tk, self.svc))
        self.assertIn("not the installation the restore began on", t2.error)
        self.assertEqual(len(self.fake.actions), acts)
        self.fake.user_tpl = {}  # a marker that is gone is not read as one: nothing is written by a retry either
        with self.ssh_world():
            t3 = tasks.start("core FE restore", "retry", lambda tk: fe.retry_rollback(tk, self.svc))
        self.assertIn("nothing is done", t3.error)
        self.assertEqual(len([c for c in self.fake.log if c[0] == "one.user.update"]), 1)

    def test_two_restores_on_one_installation_share_its_marker(self):
        """commit-364: two platform records reaching one endpoint (a shared VIP) take the same locks; the marker is
        created once - the second finds the first's"""
        from inventory import fe_deploy as fe
        from inventory.models import Platform, PlatformControl
        from tenants.deploy import One

        Platform.objects.filter(pk=self.mgmt.pk).update(fe_vip="10.0.100.30")
        other = Platform.objects.create(
            key="dc1.mgmt2", site=self.site, name="mgmt2", kind="mgmt", fe_vip="10.0.100.30"
        )
        c1 = PlatformControl.objects.get(key="dc1.mgmt")
        c2 = PlatformControl.objects.create(key=other.key, mode="execution")
        PlatformControl.objects.create(key="dc1.cloud2", mode="execution")  # elsewhere: not locked
        self.assertEqual({c.pk for c in fe.same_endpoint(c1)}, {c1.pk, c2.pk})
        self.assertEqual({c.pk for c in fe.same_endpoint(c2)}, {c1.pk, c2.pk})
        with mock.patch("adoption.collector.client_for", return_value=self.fake):
            first = fe.installation_marker(One(c1), create=True, control=c1)
            second = fe.installation_marker(One(c2), create=True, control=c2)  # the other record, same installation
        self.assertEqual(first, second)
        self.assertEqual(len([x for x in self.fake.log if x[0] == "one.user.update"]), 1)

    def test_the_marker_is_read_and_written_only_under_the_endpoints_lock(self):
        """commit-365: installation_marker takes same_endpoint's locks before its first read and holds them (inside one
        transaction) through the write and the read-back; without the lock this test fails"""
        from django.db import transaction

        from inventory import fe_deploy as fe
        from inventory.models import PlatformControl
        from tenants.deploy import One

        c1 = PlatformControl.objects.get(key="dc1.mgmt")
        order = []
        real_lock, real_call = fe.same_endpoint, self.fake._call

        def lock(control):
            order.append(("lock", len(transaction.get_connection().savepoint_ids)))
            return real_lock(control)

        def call(method, *args):
            if method in ("one.user.info", "one.user.update"):
                order.append((method, len(transaction.get_connection().savepoint_ids)))
            return real_call(method, *args)

        self.fake._call = call
        with (
            mock.patch("adoption.collector.client_for", return_value=self.fake),
            mock.patch.object(fe, "same_endpoint", side_effect=lock),
        ):
            fe.installation_marker(One(c1), create=True, control=c1)
        self.assertEqual([m for m, _a in order], ["lock", "one.user.info", "one.user.update", "one.user.info"])
        # the lock, the reads and the write in the same transaction block - the marker's own (one level deeper than
        # the test's), so the lock is held until the read-back
        base = len(transaction.get_connection().savepoint_ids)
        self.assertEqual({a for _m, a in order}, {base + 1})
        with (
            mock.patch("adoption.collector.client_for", return_value=self.fake),
            mock.patch.object(fe, "same_endpoint", side_effect=AssertionError("no lock for a read")),
        ):
            self.assertTrue(fe.installation_marker(One(c1), create=False))  # reading it alone takes no lock

    def test_a_recorded_id_that_is_another_vm_is_not_touched(self):
        """commit-352: the platform key now points elsewhere - the id answers with a VM that is not this restore's"""
        from accounts import tasks
        from inventory import fe_deploy as fe
        from inventory.models import CoreService

        CoreService.objects.filter(pk=self.svc.pk).update(
            recovery={
                "id": "abc",
                "platform": "dc1.mgmt",
                "identity": {"url": "", "zone_id": 0, "zone": "mgmt-dc1", "installation": "m1"},
                "phase": "incomplete",
                "backup": "b",
                "problems": [],
                "nodes": {
                    str(self.nodes[0].pk): {
                        "name": "core-cloud-one1_dc1",
                        "before": [IPS[0], None],
                        "old": 81,
                        "off": True,
                        "intent": "abc-1",
                        "new": 82,
                    }
                },
            }
        )
        self.fake.user_tpl = {"CLAUGINE_INSTALLATION": "m1"}  # the same installation
        with self.ssh_world():
            t = tasks.start("core FE restore", "retry", lambda tk: fe.retry_rollback(tk, self.svc))
        self.assertIn("not this restore's VM", t.error)
        self.assertEqual(self.fake.w.vms[82]["state"], "3")  # an unrelated VM with that id: untouched
        self.assertNotIn("resume", getattr(self.fake, "actions", []))

    def test_a_platform_that_cannot_list_its_vms_keeps_the_record_pending(self):
        """commit-351: not knowing whether a tagged VM exists is not proof that none does"""
        state = {"lost": False}

        def also(method):
            if method == "one.vm.allocate":
                return
            if method == "one.vmpool.info" and self.zone.world is not None:
                raise RuntimeError("[one.vmpool.info] timeout")

        self.lost_reply(also)
        t, _started = self.restore()
        self.assertIn("RedeployError: not completely rolled back", t.error)
        self.assertIn("could not be read", t.error)
        self.assertNotIn("resume", self.fake.actions)
        self.svc.refresh_from_db()
        self.assertEqual(self.svc.recovery["phase"], "incomplete")
        self.assertFalse(state["lost"])

    def test_the_rollback_works_only_on_the_records_platform(self):
        """commit-352: a record whose platform is gone keeps everything off and pending"""
        from accounts import tasks
        from inventory import fe_deploy as fe
        from inventory.models import CoreService

        CoreService.objects.filter(pk=self.svc.pk).update(
            recovery={
                "id": "x",
                "platform": "dc1.gone",
                "phase": "incomplete",
                "backup": "b",
                "problems": [],
                "nodes": {
                    str(self.nodes[0].pk): {
                        "name": "one1",
                        "before": [IPS[0], None],
                        "old": 80,
                        "off": True,
                        "intent": None,
                        "new": 81,
                    }
                },
            }
        )
        with self.ssh_world():
            t = tasks.start("core FE restore", "retry", lambda tk: fe.retry_rollback(tk, self.svc))
        self.assertIn("dc1.gone", t.error)
        self.assertNotIn("terminate-hard", getattr(self.fake, "actions", []))
        self.svc.refresh_from_db()
        self.assertEqual(self.svc.recovery["phase"], "incomplete")

    # --- one at a time, the leader's oned, readiness (commit-344, 347, 348, 349) ----------------------------------

    def test_one_operation_at_a_time_on_a_front_end(self):
        from accounts.models import Task
        from inventory import core_views

        other = Task.objects.create(
            kind="core VM redeploy",
            title="Redeploy one1",
            target_type="coreservicenode",
            target_id=str(self.nodes[0].pk),
            state=Task.State.RUNNING,
        )
        self.assertEqual(core_views._running_node(self.nodes[2]), other)
        t = self.redeploy(self.nodes[2])
        self.assertEqual(t.state, "failed")
        self.assertIn("one at a time", t.error)
        self.assertEqual(self.fake.w.vms[82]["name"], "core-cloud-one3_dc1")

    def test_the_leader_is_not_stopped_before_the_read_only_steps(self):
        from inventory import fe_deploy as fe

        with mock.patch.object(fe, "_units_of", side_effect=fe.RedeployError("ssh failed")):
            t = self.redeploy(self.nodes[0])
        self.assertEqual(t.state, "failed")
        self.assertFalse([s for n, s in self.ssh if "systemctl stop" in s])

    def test_the_leader_gets_oned_back_after_a_later_failure(self):
        from inventory import fe_deploy as fe

        with mock.patch.object(fe, "_from_leader", side_effect=fe.RedeployError("no dump")):
            t = self.redeploy(self.nodes[0])
        self.assertEqual(t.state, "failed")
        self.assertIn(("core-cloud-one1_dc1", "systemctl start opennebula\n"), self.ssh)
        self.assertIn("oned started again", [s["detail"] for s in t.steps if s["name"] == "leader"][0])

    def test_not_ready_without_a_leader_answering(self):
        def gone(node):
            self.zone.down.add(IPS[0])

        t = self.redeploy(self.nodes[2], on_start=gone)
        self.assertEqual(t.state, "failed")
        self.assertIn("no leader answers with its commit", t.error)

    def test_not_ready_without_a_valid_follower_commit(self):
        from inventory import fe_deploy as fe

        self.zone.states[IPS[2]]["commit"] = -1
        with mock.patch.multiple(fe, raft=self.zone.raft, SYNC_TIMEOUT=0):
            with self.assertRaises(fe.RedeployError):
                fe._wait_sync(self.nodes[2], self.nodes, "s", 0)
            self.zone.states[IPS[2]]["commit"] = 100
            self.assertIn("commit 100 of 100", fe._wait_sync(self.nodes[2], self.nodes, "s", 0))

    def test_a_unit_that_does_not_start_fails_the_start(self):
        from inventory import fe_deploy as fe

        with tempfile.TemporaryDirectory() as d:
            bin_ = os.path.join(d, "bin")
            os.makedirs(bin_)
            with open(os.path.join(bin_, "systemctl"), "w") as fh:
                fh.write('#!/bin/sh\n[ "$1" = is-active ] && [ "$3" = opennebula-flow ] && exit 3\nexit 0\n')
            os.chmod(os.path.join(bin_, "systemctl"), 0o755)
            work = os.path.join(d, "work")
            os.makedirs(work)
            env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"}
            r = subprocess.run(
                ["bash", "-s"],
                input=fe.start_script(["opennebula", "opennebula-flow"], work, 2),
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("opennebula-flow is not active", r.stdout)
            self.assertTrue(os.path.isdir(work))  # kept: the node is not ready
            r = subprocess.run(
                ["bash", "-s"], input=fe.start_script(["opennebula"], work, 2), text=True, capture_output=True, env=env
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(os.path.exists(work))

    # --- the page ------------------------------------------------------------------------------------------------

    def test_the_page_offers_restore_on_the_service_and_the_view_checks_the_backup(self):
        from django.urls import reverse

        from accounts.models import Kind, User
        from inventory import fe_deploy as fe

        u = User.objects.create_user(
            "root@corp.kz",
            "Str0ng-PASS-123",
            kind=Kind.STAFF,
            is_super_admin=True,
            email_verified=True,
            totp_enabled=True,
        )
        self.client.force_login(u)
        ses = self.client.session
        ses["otp_verified"] = True
        ses.save()
        name = "platform_cloud_10.0.101.30_20261121-0300"
        with (
            mock.patch.object(fe, "latest_backup", return_value=name),
            mock.patch("adoption.collector.client_for", return_value=self.fake),
        ):
            h = self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=core").content.decode()
            self.assertIn(f'data-open="dlg-fere-{self.svc.pk}"', h)
            self.assertIn(name, h)
            url = reverse("core_fe_restore", args=["dc1", self.svc.pk])
            with mock.patch("accounts.tasks.start") as start:
                self.client.post(url, {"confirm": "cloud-fe", "backup": "platform_cloud_10.0.101.30_older"})
                self.client.post(url, {"confirm": "wrong", "backup": name})
                start.assert_not_called()
                self.client.post(url, {"confirm": "cloud-fe", "backup": name})
                self.assertEqual(start.call_args.args[0], "core FE restore")
        for n in self.nodes:
            n.refresh_from_db()
            self.assertEqual(n.logging_claim, {})
        # commit-350: with a record pending, the page offers only Retry the rollback; node actions are refused
        from inventory.models import CoreService

        CoreService.objects.filter(pk=self.svc.pk).update(
            recovery={"phase": "incomplete", "backup": name, "problems": ["new VM #600 not removed"], "nodes": {}}
        )
        with mock.patch("adoption.collector.client_for", return_value=self.fake):
            h = self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=core").content.decode()
            self.assertIn(f'data-open="dlg-ferr-{self.svc.pk}"', h)
            self.assertNotIn(f'data-open="dlg-fere-{self.svc.pk}"', h)
            self.assertIn("new VM #600 not removed", h)
            with mock.patch("accounts.tasks.start") as start:
                n = self.nodes[2]
                self.client.post(reverse("core_rollback", args=["dc1", self.svc.pk, n.pk]))
                start.assert_not_called()
                # commit-353: a retry while another request has just claimed the service is refused
                from django.utils import timezone

                from inventory.models import CoreServiceNode

                CoreServiceNode.objects.filter(pk=self.nodes[1].pk).update(
                    logging_claim={"token": "other", "at": timezone.now().isoformat(), "by": "restore"}
                )
                self.client.post(reverse("core_fe_retry", args=["dc1", self.svc.pk]), {"confirm": "cloud-fe"})
                start.assert_not_called()
                CoreServiceNode.objects.filter(pk=self.nodes[1].pk).update(logging_claim={})
                self.client.post(reverse("core_fe_retry", args=["dc1", self.svc.pk]), {"confirm": "cloud-fe"})
                self.assertEqual(start.call_args.args[0], "core FE restore")
            # commit-352: neither edited nor deleted while the record is pending
            before = CoreService.objects.get(pk=self.svc.pk).updated_at
            self.client.post(reverse("core_save", args=["dc1", self.svc.pk]), {"platform": str(self.cloud.pk)})
            self.assertEqual(CoreService.objects.get(pk=self.svc.pk).updated_at, before)
            self.client.post(reverse("core_delete", args=["dc1", self.svc.pk]))
            self.assertTrue(CoreService.objects.filter(pk=self.svc.pk).exists())
            CoreService.objects.filter(pk=self.svc.pk).update(recovery={})
            self.client.post(reverse("core_delete", args=["dc1", self.svc.pk]))
            self.assertFalse(CoreService.objects.filter(pk=self.svc.pk).exists())  # free again


def fe_ip(node):
    return node.ips[0]
