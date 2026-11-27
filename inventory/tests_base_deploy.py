"""T34 B4: core services as instances of their service profile - onefe, the instance's secrets, and the redeploy of a
base-OS VM from its profile (inventory/base_deploy.py)."""

import base64
import subprocess
from unittest import mock

from cryptography.fernet import Fernet
from django.test import TestCase, override_settings
from django.utils import timezone

from inventory.tests_core import FakeWorld


class BaseWorld(FakeWorld):
    """cloud-core and data-nfs, the admin VM on both, another VM holding an address."""

    def __init__(self):
        from tenants.fakeone import World

        self.w = World(
            {"one.clusterpool.info": "<CLUSTER_POOL><CLUSTER><ID>0</ID><NAME>mgmt_dc1</NAME></CLUSTER></CLUSTER_POOL>"},
            vnets=[
                {"id": 4, "name": "cloud-core_dc1", "ars": [{"TYPE": "IP4", "IP": "10.0.101.1", "SIZE": 250}]},
                {"id": 5, "name": "data-nfs_dc1", "ars": [{"TYPE": "IP4", "IP": "10.0.111.1", "SIZE": 250}]},
            ],
            vms=[
                {
                    "id": 70,
                    "name": "cloud-admin1_dc1",
                    "nics": [{"network_id": 4, "ip": "10.0.101.21"}, {"network_id": 5, "ip": "10.0.111.208"}],
                },
                {"id": 71, "name": "someone", "nics": [{"network_id": 4, "ip": "10.0.101.99"}]},
            ],
        )
        self.sched_fail_after = None  # commit-316: the platform stops after this many schedadd calls
        self.log = []  # commit-317: every call, as made

    def _call(self, method, *args):
        from tenants.fakeone import _vectors

        self.log.append((method, args))
        v = self.w.vms.get(int(args[0])) if method.startswith("one.vm.sched") or method == "one.vm.info" else None
        if method == "one.vm.schedadd":
            if self.sched_fail_after is not None:
                if self.sched_fail_after <= 0:
                    raise RuntimeError("[one.vm.schedadd] the platform stopped")
                self.sched_fail_after -= 1
            sched = v.setdefault("sched", [])
            sched.append(
                {"ID": str(max([int(s["ID"]) for s in sched] + [-1]) + 1), **_vectors(args[1], "SCHED_ACTION")[0]}
            )
            return int(args[0])
        if method == "one.vm.scheddelete":
            v["sched"] = [s for s in v.get("sched", []) if s["ID"] != str(args[1])]
            return int(args[0])
        out = super()._call(method, *args)
        if method == "one.vm.info" and v and v.get("sched"):
            xml = "".join(
                "<SCHED_ACTION>" + "".join(f"<{k}>{val}</{k}>" for k, val in s.items()) + "</SCHED_ACTION>"
                for s in v["sched"]
            )
            out = out.replace("</TEMPLATE>", xml + "</TEMPLATE>", 1)
        return out


def _key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    return (
        ed25519.Ed25519PrivateKey.generate()
        .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption())
        .decode()
    )


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode(), DEPLOY_ASYNC=False)
class BaseDeploy(TestCase):
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
            Storage,
            StorageVolume,
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
        self.rdp = Secret.objects.create(kind="password", name="rdp")
        self.rdp.put("password", "it's secret")
        self.rdp.save()
        self.mgmt = Platform.objects.create(key="dc1.mgmt", site=self.site, name="mgmt", kind="mgmt", sa=sa.ref)
        Cluster.objects.create(platform=self.mgmt, name="mgmt_dc1", kind="mgmt")
        self.control = PlatformControl.objects.create(key="dc1.mgmt", mode="execution")
        self.image = ServiceImage.objects.create(
            control=self.control, purpose="base-os", kind="image", name="Ubuntu 26.04", one_id=49, customization="ssh"
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
        st = Storage.objects.create(site=self.site, name="svm1", kind="nfs", targets=["10.0.111.240"])
        self.vol = StorageVolume.objects.create(storage=st, name="mgmt-one", device="/mgmt_one")
        self.svc = CoreService.objects.create(
            site=self.site,
            name="admin",
            stype="admin",
            kind="vm",
            platform=self.mgmt,
            ssh_port=55523,
            ssh_secret=sa.ref,
            vcpu=2,
            ram_gb=8,
            disk_gb=30,
            nic1_network=core,
            more_nics=[{"network": nfs.pk, "gateway": ""}],
            params={"data_volume": self.vol.pk, "secrets": {"rdp": self.rdp.ref}},
        )
        self.node = CoreServiceNode.objects.create(
            service=self.svc,
            name="cloud-admin1_dc1",
            hostname="sdc1admin1",
            address="10.0.101.21",
            ips=["10.0.101.21", "10.0.111.208"],
            ssh_port=55523,
            secret=sa.ref,
        )
        self.fake = BaseWorld()

    # --- what an instance gives ---------------------------------------------------------------------------------

    def test_references_of_the_seeded_admin_profile(self):
        from inventory import base_deploy as bd
        from inventory.recipes import references

        prof = bd.profile_of(self.svc)
        used, keys = references(prof, bd.templates_of(prof))
        self.assertEqual(used["secret"], {"rdp"})
        self.assertTrue({"ssh_port", "rdp_port", "hostname"} <= used["node"])
        self.assertIn("admins", used["site"])  # T36 B5c (QB5-5): fail2ban ignores the admin VMs only
        self.assertEqual(keys, {"sa"})

    def test_offered_only_with_every_value(self):
        from inventory import base_deploy as bd

        ok, why = bd.offer(self.node)
        self.assertTrue(ok, why)
        self.svc.params = {"data_volume": self.vol.pk}
        self.svc.save()
        ok, why = bd.offer(self.node)
        self.assertFalse(ok)
        self.assertTrue(any("rdp" in w for w in why), why)
        self.image.one_id = None
        self.image.save()
        self.assertTrue(any("base-os image" in w for w in bd.offer(self.node)[1]))

    def test_395_the_nics_as_the_profiles_networks(self):
        from inventory import base_deploy as bd
        from inventory.models import ServiceProfile

        self.assertEqual(bd.nic_mismatch(self.svc), [])  # admin: core-cloud, storage - its nfs volume's network
        p = ServiceProfile.objects.get(stype="admin")
        p.nics = [{"network": "nfs"}, {"network": "core-cloud"}]
        p.save()
        ok, why = bd.offer(self.node)
        self.assertFalse(ok)
        self.assertTrue(any("NIC 1 is on core-cloud" in w for w in why), why)
        p.nics = [{"network": "core-cloud"}]
        p.save()
        self.assertTrue(any("NIC 2 (nfs) is not in the profile" in w for w in bd.offer(self.node)[1]))
        p.nics = []  # the profile names none: not compared
        p.save()
        self.assertEqual(bd.nic_mismatch(self.svc), [])

    def test_398_storage_and_optional_rows(self):
        from inventory import base_deploy as bd
        from inventory.models import Network, PlatformDatastore, ServiceProfile, Storage, StorageVolume

        core = self.svc.nic1_network
        nfs = Network.objects.get(site="dc1", name="data-nfs")
        iscsi = Network.objects.create(
            site="dc1", name="data-iscsi", kind="iscsi", interface="bond1", vlan=112, subnet="10.0.112.0", prefix=24
        )
        lun = StorageVolume.objects.create(
            storage=Storage.objects.create(site=self.site, name="svm2", kind="iscsi", targets=["10.0.112.240"]),
            name="backup-lun",
        )
        ds = PlatformDatastore.objects.create(platform=self.mgmt, name="backups", kind="backups", volume=lun)
        seeded = {p.stype: p.nics for p in ServiceProfile.objects.all()}
        self.assertEqual(seeded["onefe"], [{"network": "core-cloud"}, {"network": "nfs", "optional": True}])
        self.assertEqual(seeded["backup"], [{"network": "core-cloud"}, {"network": "storage", "optional": True}])
        self.assertEqual(seeded["logs"], [{"network": "core-cloud"}, {"network": "storage"}])
        nfs_vol = {"data_volume": self.vol.pk}
        lun_vol = {"data_volume": lun.pk}
        cases = [  # (type, params, the instance's NICs, agrees)
            ("onefe", {}, [core, nfs], True),  # the site has NFS
            ("onefe", {}, [core], True),  # it has none
            ("onefe", {}, [core, iscsi], False),
            ("backup", {"datastore": ds.pk}, [core, iscsi], True),  # its LUN
            ("backup", {"datastore": ds.pk}, [core], True),  # the optional row passed over
            ("backup", {"datastore": ds.pk}, [core, nfs], False),  # commit-399: not its volume's network
            ("backup", {"datastore": ds.pk}, [nfs], False),
            ("admin", nfs_vol, [core, nfs], True),
            ("admin", nfs_vol, [core, iscsi], False),  # commit-399: an NFS volume, an iSCSI NIC
            ("admin", lun_vol, [core, iscsi], True),
            ("admin", lun_vol, [core, nfs], False),  # commit-399: the reverse
            ("admin", nfs_vol, [core], False),  # storage is required
            ("admin", nfs_vol, [core, nfs, iscsi], False),  # one too many
            ("ns", {}, [core], True),
        ]
        for stype, params, nets, ok in cases:
            self.svc.stype, self.svc.params = stype, params
            got = bd.nic_mismatch(self.svc, [(n, "") for n in nets])
            self.assertEqual(not got, ok, (stype, params, [n.name for n in nets], got))
        ds.backend = "s3"  # S3: no storage NIC
        ds.save()
        self.svc.stype, self.svc.params = "backup", {"datastore": ds.pk}
        self.assertEqual(bd.nic_mismatch(self.svc, [(core, "")]), [])
        self.assertTrue(bd.nic_mismatch(self.svc, [(core, ""), (iscsi, "")]))

    def test_400_an_optional_row_passed_over_for_a_required_one(self):
        from inventory import base_deploy as bd
        from inventory.models import Network, ServiceProfile

        core, nfs = self.svc.nic1_network, Network.objects.get(site="dc1", name="data-nfs")
        ServiceProfile.objects.filter(stype="admin").update(
            nics=[{"network": "core-cloud"}, {"network": "nfs", "optional": True}, {"network": "nfs"}]
        )
        self.assertEqual(bd.nic_mismatch(self.svc, [(core, ""), (nfs, "")]), [])
        self.assertEqual(bd.nic_mismatch(self.svc, [(core, ""), (nfs, ""), (nfs, "")]), [])
        self.assertTrue(bd.nic_mismatch(self.svc, [(core, "")]))

    def test_401_a_long_run_of_optional_rows(self):
        from inventory import base_deploy as bd

        rows = [("core-cloud", False), ("storage", False)] + [("nfs", True)] * 2000
        self.assertTrue(bd._matches(["core-cloud", "nfs"], rows, "nfs"))
        self.assertTrue(bd._matches(["core-cloud", "nfs", "nfs", "nfs"], rows, "nfs"))
        self.assertFalse(bd._matches(["core-cloud", "iscsi"], rows, "nfs"))
        self.assertFalse(bd._matches(["core-cloud"] + ["nfs"] * 2003, rows, "nfs"))

    def test_398_the_migration_keeps_edited_rows(self):
        from importlib import import_module

        from django.apps import apps

        from inventory.models import ServiceProfile

        m = import_module("inventory.migrations.0115_profile_nic_rows")
        ServiceProfile.objects.filter(stype="logs").update(nics=[{"network": "core-cloud"}])  # edited by hand
        m.backwards(apps, None)
        got = {p.stype: p.nics for p in ServiceProfile.objects.all()}
        self.assertEqual(got["onefe"], [{"network": "core-cloud"}])
        self.assertEqual(got["backup"], [{"network": "core-cloud"}, {"network": "iscsi"}])
        self.assertEqual(got["logs"], [{"network": "core-cloud"}])
        m.forwards(apps, None)
        got = {p.stype: p.nics for p in ServiceProfile.objects.all()}
        self.assertEqual(got["onefe"], m.ROWS["onefe"])
        self.assertEqual(got["logs"], [{"network": "core-cloud"}])

    def test_name_servers_wait_for_dns_and_onefe_for_the_platform_it_serves(self):
        from inventory import base_deploy as bd

        self.svc.stype = "ns"
        self.svc.save()
        ok, why = bd.offer(self.node)
        self.assertFalse(ok)
        self.assertTrue(any("DNS" in w or w.startswith("dns.") or "internal domain" in w for w in why), why)
        self.svc.stype = "onefe"  # T34 B6: offered with the platform it serves (tests_fe_deploy)
        self.svc.save()
        ok, why = bd.offer(self.node)
        self.assertFalse(ok)
        self.assertIn(bd._("no platform served"), why)

    def test_the_script_masks_secrets_unless_revealed_and_is_bash(self):
        from inventory import base_deploy as bd

        self.svc.firewall = [{"port": "8443", "proto": "tcp", "source": "10.0.0.0/8"}]
        self.svc.save()
        shown = bd.script_of(self.node)
        self.assertIn("••••", shown)
        self.assertNotIn("it's secret", shown)
        real = bd.script_of(self.node, reveal=True)
        self.assertIn("'it'\"'\"'s secret'", real)  # one shell word
        self.assertIn("grdctl --system rdp set-port 55275", real)  # T36 B5c (N3): Settings → Security's RDP port
        self.assertIn("Port 55274", real)  # T36 commit-473: Settings → Security's public SSH port
        self.assertIn("ufw allow from 10.0.0.0/8 to any port 8443 proto tcp", real)
        self.assertTrue(real.rstrip().endswith('step "done"'))
        boot = bd.first_boot(real)
        for text in (real, boot):
            r = subprocess.run(["bash", "-n"], input=text, text=True, capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("systemd-run --unit claugine-firstboot", boot)
        self.assertIn("firstboot.ok ] && exit 0", boot)

    # --- the redeploy --------------------------------------------------------------------------------------------

    def run_redeploy(self, checks_ok=True, how="redeploy", status=None, timeout=0):
        from accounts import tasks
        from inventory import base_deploy as bd
        from inventory import core_deploy as cd

        self.ssh = []
        answers = list(status or ["OK\n"])

        def node_ssh(n):
            def run(script, timeout=300):
                self.ssh.append(script)
                if "firstboot.ok" in script and "elif" in script:
                    a = answers.pop(0) if len(answers) > 1 else answers[0]
                    if isinstance(a, Exception):
                        raise a
                    return a
                return "" if checks_ok else "FAIL systemctl is-active --quiet gdm3\n"

            return run

        fn = {
            "redeploy": lambda t: cd.redeploy(t, self.node, None, None, False, True),
            "first": lambda t: bd.redeploy(t, self.node, None, True, first=True),  # commit-402
            "rollback": lambda t: cd.rollback(t, self.node),
            "finish": lambda t: cd.finish(t, self.node),
            "clean": lambda t: cd.clean_context(t, self.node),
        }[how]
        with (
            mock.patch("adoption.collector.client_for", return_value=self.fake),
            mock.patch("inventory.hostlogging.node_ssh", side_effect=node_ssh),
            mock.patch.object(bd, "FIRSTBOOT_TIMEOUT", timeout),
        ):
            return tasks.start("core VM redeploy", "test", fn)

    def nics(self, vm_id):
        return [(n["vid"], n["ip"]) for n in self.fake.w.vms[vm_id]["nics"]]

    def test_redeploy_makes_the_vm_from_its_profile(self):
        t = self.run_redeploy()
        self.assertEqual(t.state, "done", t.error)
        w = self.fake.w
        new_id = t.result["new_id"]
        self.assertEqual(w.vms[70]["name"], "cloud-admin1_dc1-old")
        self.assertEqual(w.vms[70]["state"], "8")
        self.assertEqual(self.nics(70), [])  # both addresses given up
        self.assertEqual(w.vms[new_id]["name"], "cloud-admin1_dc1")
        self.assertEqual(self.nics(new_id), [(4, "10.0.101.21"), (5, "10.0.111.208")])
        tpl = w.vms[new_id]["template"]
        self.assertIn('IMAGE_ID = "49"', tpl)
        self.assertIn('SIZE = "30720"', tpl)
        self.assertIn('SET_HOSTNAME = "sdc1admin1"', tpl)
        script = base64.b64decode(
            next(ln for ln in tpl.splitlines() if "START_SCRIPT_BASE64" in ln).split('"')[1]
        ).decode()
        self.assertIn("grdctl --system rdp set-credentials portal", script)
        self.assertEqual(w.vms[new_id]["context"].get("START_SCRIPT_BASE64"), "")  # removed after ready
        self.node.refresh_from_db()
        self.assertEqual(self.node.deployed_vm_id, new_id)
        self.assertEqual(self.node.facts["redeploy"]["old_id"], 70)
        self.assertTrue(any("FAIL" in s for s in self.ssh))  # the profile's checks ran

    def test_473_a_redeploy_takes_the_public_ssh_port_of_settings(self):
        from tenants.models import PortalSettings

        PortalSettings.objects.filter(pk=PortalSettings.get().pk).update(public_ssh_port=60022)
        from inventory import base_deploy as bd

        self.assertIn("Port 60022", bd.script_of(self.node, reveal=True))
        t = self.run_redeploy()
        self.assertEqual(t.state, "done", t.error)
        self.node.refresh_from_db()
        self.svc.refresh_from_db()
        self.assertEqual((self.node.ssh_port, self.svc.ssh_port), (60022, 60022))

    def test_482_a_rollback_by_hand_puts_the_old_ports_back(self):
        from tenants.models import PortalSettings

        node_port, svc_port = self.node.ssh_port, self.svc.ssh_port
        PortalSettings.objects.filter(pk=PortalSettings.get().pk).update(public_ssh_port=60022)
        t = self.run_redeploy()
        self.assertEqual(t.state, "done", t.error)
        self.node.refresh_from_db()
        self.assertEqual(self.node.ssh_port, 60022)
        t = self.run_redeploy(how="rollback")
        self.assertEqual(t.state, "done", t.error)
        self.node.refresh_from_db()
        self.svc.refresh_from_db()
        self.assertEqual((self.node.ssh_port, self.svc.ssh_port), (node_port, svc_port))

    def test_484_a_service_port_saved_after_the_redeploy_is_kept(self):
        from inventory.models import CoreService
        from tenants.models import PortalSettings

        node_port = self.node.ssh_port
        PortalSettings.objects.filter(pk=PortalSettings.get().pk).update(public_ssh_port=60022)
        self.assertEqual(self.run_redeploy().state, "done")
        CoreService.objects.filter(pk=self.svc.pk).update(ssh_port=60044)  # a service edit in between
        t = self.run_redeploy(how="rollback")
        self.assertEqual(t.state, "done", t.error)
        self.node.refresh_from_db()
        self.svc.refresh_from_db()
        self.assertEqual((self.node.ssh_port, self.svc.ssh_port), (node_port, 60044))

    def test_484_a_redeploy_that_kept_the_service_port_restores_none(self):
        from inventory.models import CoreService
        from tenants.models import PortalSettings

        PortalSettings.objects.filter(pk=PortalSettings.get().pk).update(public_ssh_port=self.svc.ssh_port)
        self.assertEqual(self.run_redeploy().state, "done")  # the port unchanged
        self.assertNotIn("service_ssh_port_new", self.node.__class__.objects.get(pk=self.node.pk).facts["redeploy"])
        CoreService.objects.filter(pk=self.svc.pk).update(ssh_port=60044)
        self.assertEqual(self.run_redeploy(how="rollback").state, "done")
        self.svc.refresh_from_db()
        self.assertEqual(self.svc.ssh_port, 60044)

    def test_485_a_failed_record_write_leaves_the_service_port(self):
        from unittest import mock

        from inventory.models import CoreServiceNode
        from tenants.models import PortalSettings

        svc_port = self.svc.ssh_port
        PortalSettings.objects.filter(pk=PortalSettings.get().pk).update(public_ssh_port=60022)
        save = CoreServiceNode.save

        def failing(n, *a, **kw):  # the record of the service's new port cannot be written
            if kw.get("update_fields") == ["facts"] and "service_ssh_port_new" in (n.facts or {}).get("redeploy", {}):
                raise RuntimeError("database gone")
            return save(n, *a, **kw)

        with mock.patch.object(CoreServiceNode, "save", failing):
            t = self.run_redeploy()
        self.assertEqual(t.state, "failed")
        self.svc.refresh_from_db()
        self.assertEqual(self.svc.ssh_port, svc_port)  # one transaction: nothing written

    def test_486_an_edit_during_the_redeploy_is_what_a_rollback_restores(self):
        from unittest import mock

        from inventory import base_deploy as bd
        from inventory.models import CoreService
        from tenants.models import PortalSettings

        PortalSettings.objects.filter(pk=PortalSettings.get().pk).update(public_ssh_port=60022)
        wait = bd._wait

        def wait_and_edit(*a, **kw):  # the service is edited while the new VM boots
            CoreService.objects.filter(pk=self.svc.pk).update(ssh_port=60044)
            return wait(*a, **kw)

        with mock.patch.object(bd, "_wait", side_effect=wait_and_edit):
            self.assertEqual(self.run_redeploy().state, "done")
        self.svc.refresh_from_db()
        self.assertEqual(self.svc.ssh_port, 60022)
        self.assertEqual(self.run_redeploy(how="rollback").state, "done")
        self.svc.refresh_from_db()
        self.assertEqual(self.svc.ssh_port, 60044)  # the value this redeploy replaced

    def test_483_a_settings_change_during_the_redeploy(self):
        from unittest import mock

        from inventory import base_deploy as bd
        from tenants.models import PortalSettings

        PortalSettings.objects.filter(pk=PortalSettings.get().pk).update(public_ssh_port=60022)
        wait = bd._wait

        def wait_and_change(*a, **kw):  # while the new VM boots, Security is saved again
            PortalSettings.objects.filter(pk=PortalSettings.get().pk).update(public_ssh_port=60033)
            return wait(*a, **kw)

        with mock.patch.object(bd, "_wait", side_effect=wait_and_change):
            t = self.run_redeploy()
        self.assertEqual(t.state, "done", t.error)
        self.node.refresh_from_db()
        self.assertEqual(self.node.ssh_port, 60022)  # the port the recipe set, not the later one
        tpl = self.fake.w.vms[t.result["new_id"]]["template"]
        script = base64.b64decode(next(ln for ln in tpl.splitlines() if "START_SCRIPT_BASE64" in ln).split('"')[1])
        self.assertIn(b"Port 60022", script)

    def test_473_a_failed_redeploy_keeps_the_old_port(self):
        from tenants.models import PortalSettings

        port = self.node.ssh_port
        PortalSettings.objects.filter(pk=PortalSettings.get().pk).update(public_ssh_port=60022)
        t = self.run_redeploy(checks_ok=False)
        self.assertEqual(t.state, "failed")
        self.node.refresh_from_db()
        self.assertEqual(self.node.ssh_port, port)

    def test_failed_checks_roll_back_by_themselves(self):
        t = self.run_redeploy(checks_ok=False)
        self.assertEqual(t.state, "failed")
        self.assertIn("checks failed", t.error)
        w = self.fake.w
        self.assertEqual(w.vms[70]["name"], "cloud-admin1_dc1")
        self.assertEqual(w.vms[70]["state"], "3")
        self.assertEqual(sorted(self.nics(70)), [(4, "10.0.101.21"), (5, "10.0.111.208")])
        self.assertEqual([v["state"] for k, v in w.vms.items() if k > 71], ["6"])
        self.node.refresh_from_db()
        self.assertIsNone(self.node.deployed_vm_id)

    def test_rollback_and_finish_by_hand(self):
        t = self.run_redeploy()
        self.assertEqual(t.state, "done", t.error)
        new_id = t.result["new_id"]
        t = self.run_redeploy(how="rollback")
        self.assertEqual(t.state, "done", t.error)
        self.assertEqual(self.fake.w.vms[new_id]["state"], "6")
        self.assertEqual(sorted(self.nics(70)), [(4, "10.0.101.21"), (5, "10.0.111.208")])
        t = self.run_redeploy()
        self.assertEqual(t.state, "done", t.error)
        t = self.run_redeploy(how="finish")
        self.assertEqual(t.state, "done", t.error)
        self.assertEqual(self.fake.w.vms[70]["state"], "6")

    def test_an_address_held_by_another_vm_stops_it_before_anything_changes(self):
        self.node.ips = ["10.0.101.99", "10.0.111.208"]
        self.node.save()
        t = self.run_redeploy()
        self.assertEqual(t.state, "failed")
        self.assertIn("held by another VM", t.error)
        self.assertEqual(self.fake.w.vms[70]["name"], "cloud-admin1_dc1")
        self.assertEqual(self.fake.w.vms[70]["state"], "3")

    # --- the instance's secrets, onefe ---------------------------------------------------------------------------

    def test_the_admin_form_asks_the_profile_secrets(self):
        from inventory import core_types as ct

        f = ct.FORMS["admin"](site="dc1", svc=self.svc, initial=ct.initial(self.svc))
        self.assertIn("secret__rdp", f.fields)
        self.assertEqual(f.initial["secret__rdp"], self.rdp.ref)
        data = {k: v for k, v in ct.initial(self.svc).items() if v is not None}
        data.update(instances=1, first_ip="10.0.101.21", data_volume=str(self.vol.pk), vcpu=2, ram_gb=8, disk_gb=30)
        f = ct.FORMS["admin"](data, site="dc1", svc=self.svc)
        self.assertTrue(f.is_valid(), f.errors)
        self.assertEqual(f.params()["secrets"], {"rdp": self.rdp.ref})
        self.assertNotIn("secret__rdp", f.params())

    def test_the_secret_is_a_contributor_and_follows_a_replacement(self):
        from inventory import credentials
        from inventory.models import Secret

        self.assertIn("core service dc1.admin: rdp", credentials.contributors()[self.rdp.pk])
        new = Secret.objects.create(kind="password", name="rdp2")
        credentials.repoint(self.rdp, new)
        self.svc.refresh_from_db()
        self.assertEqual(self.svc.params["secrets"], {"rdp": new.ref})

    def test_onefe_is_the_front_end_of_a_platform(self):
        from inventory import core_types as ct
        from inventory.models import Platform

        cloud = Platform.objects.create(
            key="dc1.cloud",
            site=self.site,
            name="cloud",
            kind="shared",
            fe_secrets="secret:9",
            fe={
                "type": "vm",
                "nodes": ["sdc1one1", "sdc1one2", "sdc1one3"],
                "node_ips": ["10.0.101.31", "10.0.101.32", "10.0.101.33"],
            },
        )
        Platform.objects.create(key="dc1.mgmt2", site=self.site, name="mgmt2", kind="mgmt", fe={"type": "host"})
        f = ct.FORMS["onefe"](site="dc1")
        self.assertEqual([c[0] for c in f.fields["platform"].choices], ["", str(cloud.pk)])
        self.assertEqual(ct.SIZES["onefe"], (8, 32, 100))
        d = ct.definition("dc1", "onefe", {"platform": cloud.pk})
        self.assertEqual(d["name"], "cloud-fe")
        self.assertEqual(d["serves"], cloud)
        self.assertEqual([n["hostname"] for n in d["nodes"]], ["sdc1one1", "sdc1one2", "sdc1one3"])
        self.assertEqual(d["nodes"][0]["ips"], ["10.0.101.31", ""])
        self.assertEqual([n.name for n, _gw in d["nics"]], ["cloud-core", "data-nfs"])
        cloud.fe = {"type": "vm", "nodes": ["sdc1one1"], "node_ips": [""]}
        cloud.save()
        with self.assertRaises(ct.DefinitionError):
            ct.definition("dc1", "onefe", {"platform": cloud.pk})


class Migration0108(TestCase):
    def test_the_backup_datastore_logs_in_with_its_own_initiator_name(self):
        from inventory.base_deploy import templates_of
        from inventory.models import ServiceProfile
        from inventory.recipes import parse_steps

        p = ServiceProfile.objects.get(key="backup")  # T34 S2: a template; T36 B5a: the backup-ds function's
        t = templates_of(p)
        steps = {
            s["name"]: s for u in p.units() if u.install_template_id for s in parse_steps(t[u.install_template_id].body)
        }
        cmds = steps["backup-ds"]["commands"]
        self.assertIn("{{ storage.initiator|sh }}", cmds[0])
        self.assertIn("restart iscsid", cmds[0])
        self.assertIn("--login", cmds[1])


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode(), DEPLOY_ASYNC=False)
class BaseDeployFixes(BaseDeploy):
    """commit-310 .. commit-313 (review of 5923fc6)."""

    def test_commit_310_no_answer_over_ssh_is_retried(self):
        from inventory.sshexec import SshError

        t = self.run_redeploy(status=[SshError("connection refused"), SshError("timed out"), "OK\n"], timeout=60)
        self.assertEqual(t.state, "done", t.error)
        self.assertEqual(sum("firstboot.ok" in s for s in self.ssh), 3)

    def test_commit_310_the_guests_failure_ends_it(self):
        t = self.run_redeploy(status=["FAILED exit 3 at == jump: commands\n"])
        self.assertEqual(t.state, "failed")
        self.assertIn("exit 3 at == jump: commands", t.error)
        self.assertIn("/var/log/claugine-firstboot.log", t.error)
        self.assertEqual(self.fake.w.vms[70]["name"], "cloud-admin1_dc1")

    def test_commit_311_rollback_by_hand_restores_address_and_portal_mark(self):
        self.node.deployed_vm_id = 70  # made by the portal before
        self.node.ips = ["10.0.101.50", "10.0.111.208"]  # the definition's NIC 1 address changed since
        self.node.save()
        t = self.run_redeploy()
        self.assertEqual(t.state, "done", t.error)
        self.node.refresh_from_db()
        rec = self.node.facts["redeploy"]
        self.assertEqual((rec["address"], rec["deployed_vm_id"], rec["autostart"]), ("10.0.101.21", 70, ""))
        self.assertIn("schedule", rec)
        self.assertEqual(self.node.address, "10.0.101.50")
        self.assertEqual(sorted(self.nics(70)), [(4, "10.0.101.21")])  # .21 was not taken
        t = self.run_redeploy(how="rollback")
        self.assertEqual(t.state, "done", t.error)
        self.node.refresh_from_db()
        self.assertEqual((self.node.address, self.node.deployed_vm_id), ("10.0.101.21", 70))
        self.assertEqual(sorted(self.nics(70)), [(4, "10.0.101.21"), (5, "10.0.111.208")])
        self.assertNotIn("redeploy", self.node.facts)

    def test_commit_312_only_the_exit_code_and_step_leave_the_vm(self):
        import tempfile
        from pathlib import Path

        from inventory import base_deploy as bd

        with tempfile.TemporaryDirectory() as d:
            st, log = Path(d) / "st", Path(d) / "log"
            st.mkdir()
            step = st / "firstboot.step"
            # the recipe's step function writes the step file; its output (even marker-like) goes only to the log
            recipe = bd.script_of(self.node, reveal=True)
            head = next(ln for ln in recipe.splitlines() if ln.startswith("step() {"))
            subprocess.run(
                ["bash", "-c", head + '\nstep "jump: commands"\necho "== jump: password=SECRET-123"'],
                env={"CLAUGINE_STEP_FILE": str(step), "PATH": "/usr/bin:/bin"},
                stdout=open(log, "w"),
                check=True,
            )
            (st / "firstboot.failed").write_text("3\n")
            script = bd.STATUS.replace(bd.STATE, str(st)).replace(bd.LOG, str(log))
            out = subprocess.run(["bash", "-c", script], text=True, capture_output=True).stdout
            self.assertIn("SECRET-123", log.read_text())  # kept on the VM
        self.assertEqual(out.strip(), "FAILED exit 3 at jump: commands")
        self.assertNotIn("SECRET", out)
        self.assertIn(f"export CLAUGINE_STEP_FILE={bd.STEP}", bd.first_boot(recipe))

    def test_commit_313_each_kind_clears_only_its_own_key(self):
        from inventory import core_deploy as cd
        from tenants.deploy import One

        w = self.fake.w
        w.vms[70]["context"] = {"START_SCRIPT_BASE64": "b3du"}  # the VM's own start script
        with mock.patch("adoption.collector.client_for", return_value=self.fake):
            one = One(self.control)
            self.assertFalse(cd.drop_init_script(one, 70))  # a VyOS cleanup: its own key only
            self.assertEqual(w.vms[70]["context"]["START_SCRIPT_BASE64"], "b3du")
        t = self.run_redeploy(how="clean")
        self.assertEqual(t.state, "failed")
        self.assertIn("not made by the portal", t.error)
        self.assertEqual(w.vms[70]["context"]["START_SCRIPT_BASE64"], "b3du")

    def test_commit_314_an_interrupted_rollback_by_hand_is_completed_by_a_retry(self):
        from inventory.models import CoreServiceNode

        self.node.deployed_vm_id = 70
        self.node.ips = ["10.0.101.50", "10.0.111.208"]
        self.node.save()
        t = self.run_redeploy()
        self.assertEqual(t.state, "done", t.error)
        save = CoreServiceNode.save

        def stop(node, *args, **kwargs):  # the worker stops at the portal's write after the platform is back
            if "deployed_vm_id" in (kwargs.get("update_fields") or []):
                raise RuntimeError("worker stopped")
            return save(node, *args, **kwargs)

        with mock.patch.object(CoreServiceNode, "save", stop):
            t = self.run_redeploy(how="rollback")
        self.assertEqual(t.state, "failed")
        self.assertEqual(self.fake.w.vms[70]["name"], "cloud-admin1_dc1")  # the old VM is back on the platform
        self.node.refresh_from_db()
        self.assertIn("redeploy", self.node.facts)  # the record is kept for the retry
        t = self.run_redeploy(how="rollback")
        self.assertEqual(t.state, "done", t.error)
        self.node.refresh_from_db()
        self.assertEqual((self.node.address, self.node.deployed_vm_id), ("10.0.101.21", 70))
        self.assertNotIn("redeploy", self.node.facts)

    def _interrupted_after_rename(self):
        """A redeploy, then a rollback by hand that stopped right after the old VM got its name back: the new VM
        gone, the old one powered off without the NIC it gave up."""
        self.node.deployed_vm_id = 70
        self.node.ips = ["10.0.101.50", "10.0.111.208"]
        self.node.save()
        t = self.run_redeploy()
        self.assertEqual(t.state, "done", t.error)
        w = self.fake.w
        w._call("one.vm.action", "terminate-hard", t.result["new_id"])
        w.vms[70]["name"] = "cloud-admin1_dc1"
        self.assertEqual((w.vms[70]["state"], sorted(self.nics(70))), ("8", [(4, "10.0.101.21")]))

    def test_commit_315_a_retry_finishes_the_platform_steps_before_the_portal(self):
        self._interrupted_after_rename()
        t = self.run_redeploy(how="rollback")
        self.assertEqual(t.state, "done", t.error)
        w = self.fake.w
        self.assertEqual(w.vms[70]["state"], "3")
        self.assertEqual(sorted(self.nics(70)), [(4, "10.0.101.21"), (5, "10.0.111.208")])
        self.node.refresh_from_db()
        self.assertEqual((self.node.address, self.node.deployed_vm_id), ("10.0.101.21", 70))
        self.assertNotIn("redeploy", self.node.facts)

    def test_commit_315_the_record_stays_until_the_vm_is_back(self):
        from inventory import base_deploy as bd

        self._interrupted_after_rename()
        with mock.patch.object(bd, "put_schedule"), mock.patch.object(bd, "_nics", return_value=[]):
            t = self.run_redeploy(how="rollback")  # the NICs cannot be seen: not back in service
        self.assertEqual(t.state, "failed")
        self.assertIn("Roll back again", t.error)
        self.node.refresh_from_db()
        self.assertIn("redeploy", self.node.facts)
        t = self.run_redeploy()  # no new redeploy over an unfinished rollback
        self.assertEqual(t.state, "failed")
        self.assertIn("has not ended", t.error)

    def test_commit_314_the_page_offers_the_retry(self):
        from django.urls import reverse

        from accounts.models import Kind, User
        from inventory import core_services as cs

        self._interrupted_after_rename()
        with mock.patch("adoption.collector.client_for", return_value=self.fake):
            cs.read_platform_vms(self.control)
        sa = User.objects.create_user(
            "sa@corp.kz",
            "Str0ng-PASS-123",
            kind=Kind.STAFF,
            email_verified=True,
            is_super_admin=True,
            totp_enabled=True,
        )
        self.client.force_login(sa)
        s = self.client.session
        s["otp_verified"] = True
        s.save()
        page = self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=core").content.decode()
        rollback = reverse("core_rollback", args=["dc1", self.svc.pk, self.node.pk])
        finish = reverse("core_finish", args=["dc1", self.svc.pk, self.node.pk])
        self.assertIn(rollback, page)
        self.assertNotIn(finish, page)  # Remove the old VM only with a -old VM
        self.assertNotIn(f'data-open="dlg-cnre-{self.node.pk}"', page)  # no Redeploy meanwhile

    def _sa_page(self):
        from django.urls import reverse

        from accounts.models import Kind, User

        sa = User.objects.create_user(
            "sa3@corp.kz",
            "Str0ng-PASS-123",
            kind=Kind.STAFF,
            email_verified=True,
            is_super_admin=True,
            totp_enabled=True,
        )
        self.client.force_login(sa)
        s = self.client.session
        s["otp_verified"] = True
        s.save()
        return self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=core").content.decode()

    def test_s3a_site_services_list_check_and_deploy(self):
        """T34 S3a (ui-18): the tab is Services; the list shows the profile, what it serves and the platform; the
        service's Check is linked; a VM not on the platform is deployed (no old VM to keep)."""
        from django.urls import reverse

        page = self._sa_page()
        self.assertTrue('href="?tab=core">Services</a>' in page and ">Core services</a>" not in page)
        for col in ("<th>Name</th>", "<th>Profile</th>", "<th>Platform</th>"):  # U9: Serves in the details
            self.assertIn(col, page)
        self.assertIn("<td>dc1.mgmt</td>", page)
        self.assertIn(reverse("core_check", args=["dc1", self.svc.pk]), page)
        dlg = page[page.index(f'<dialog id="dlg-cnre-{self.node.pk}"') :]
        dlg = dlg[: dlg.index("</dialog>")]
        self.assertTrue("<h2>Deploy" in dlg and "the VM is made for the first time" in dlg, dlg[:200])
        self.assertTrue("kept as" not in dlg and "a failure removes the new VM" in dlg, dlg[:200])

    def test_402_a_first_deployment_stops_when_the_vm_is_there(self):
        """commit-402: the page offered Deploy from a stale read; the live plan finds the VM - nothing changes."""
        w = self.fake.w
        before = (w.vms[70]["name"], self.nics(70), len(w.vms))
        t = self.run_redeploy(how="first")
        self.assertEqual(t.state, "failed")
        self.assertIn("is on the platform now (#70)", t.error)
        self.assertEqual((w.vms[70]["name"], self.nics(70), len(w.vms)), before)
        self.assertEqual(self.ssh, [])

    def test_402_the_deploy_form_says_first(self):
        from django.urls import reverse

        page = self._sa_page()
        dlg = page[page.index(f'<dialog id="dlg-cnre-{self.node.pk}"') :]
        self.assertIn('name="first" value="1"', dlg[: dlg.index("</dialog>")])
        with mock.patch("accounts.tasks.start") as start:
            self.client.post(
                reverse("core_redeploy", args=["dc1", self.svc.pk, self.node.pk]),
                {"confirm": self.node.name, "keep_old": "1", "first": "1"},
            )
        start.assert_called_once()
        self.assertTrue(start.call_args[0][1].startswith("Deploy "), start.call_args)

    def test_403_support_sees_check_only(self):
        from django.urls import reverse

        from accounts.models import Kind, Role, User

        u = User.objects.create_user(
            "sup@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, email_verified=True, totp_enabled=True
        )
        from accounts.models import STAFF_ROLE_FIELDS

        setattr(u, STAFF_ROLE_FIELDS[Role.SUPPORT], True)
        u.save()
        self.client.force_login(u)
        s = self.client.session
        s["otp_verified"] = True
        s.save()
        page = self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=core").content.decode()
        self.assertIn(reverse("core_check", args=["dc1", self.svc.pk]), page)
        self.assertNotIn(f'data-open="dlg-cs-{self.svc.pk}"', page)  # no Edit
        self.assertNotIn(reverse("core_delete", args=["dc1", self.svc.pk]), page)

    def test_s3a_serves_text(self):
        from inventory.core_views import serves_text
        from inventory.models import CoreService, Network

        ix = Network.objects.create(
            site="dc1", name="inet", kind="ix", interface="bond0", vlan=900, subnet="10.9.0.0", prefix=24
        )
        rr = CoreService(site=self.site, name="rr-inet", stype="bgp-rr", params={"network": ix.pk})
        self.assertEqual(serves_text(rr), "IX inet")
        self.assertEqual(serves_text(self.svc), "")
        fe = CoreService(site=self.site, name="cloud", stype="onefe", serves=self.mgmt)
        self.assertEqual(serves_text(fe), "platform dc1.mgmt")

    def test_commit_316_a_partly_restored_schedule_is_completed(self):
        w = self.fake.w
        w.vms[70]["sched"] = [
            {"ID": "0", "ACTION": "resume", "TIME": "1700000000", "REPEAT": "3", "DAYS": "1", "END_TYPE": "0"},
            {"ID": "1", "ACTION": "poweroff", "TIME": "1700003600", "END_TYPE": "0"},
        ]
        t = self.run_redeploy()
        self.assertEqual(t.state, "done", t.error)
        self.assertEqual(w.vms[70].get("sched"), [])  # taken off the stepped-aside VM
        self.fake.sched_fail_after = 1
        t = self.run_redeploy(how="rollback")
        self.assertEqual(t.state, "failed")
        self.assertEqual([s["ACTION"] for s in w.vms[70]["sched"]], ["resume"])
        self.node.refresh_from_db()
        self.assertIn("redeploy", self.node.facts)
        self.fake.sched_fail_after = None
        t = self.run_redeploy(how="rollback")
        self.assertEqual(t.state, "done", t.error)
        self.assertEqual(sorted(s["ACTION"] for s in w.vms[70]["sched"]), ["poweroff", "resume"])  # no duplicate
        self.node.refresh_from_db()
        self.assertNotIn("redeploy", self.node.facts)

    def test_commit_317_one_read_when_the_schedule_needs_nothing(self):
        t = self.run_redeploy()
        self.assertEqual(t.state, "done", t.error)
        calls = self.fake.log
        start = len(calls)
        t = self.run_redeploy(how="rollback")
        self.assertEqual(t.state, "done", t.error)
        mine = [(m, a) for m, a in calls[start:] if a and (str(a[0]) == "70" or (a[0] == "resume" and a[1] == 70))]
        resumed = max(i for i, (m, a) in enumerate(mine) if m == "one.vm.action" and a[0] == "resume")
        after = [m for m, a in mine[resumed + 1 :]]
        self.assertNotIn("one.vm.schedadd", after)
        # one read while waiting for it to run, one for the checks - no second read for an unchanged schedule
        self.assertEqual(after.count("one.vm.info"), 2)
