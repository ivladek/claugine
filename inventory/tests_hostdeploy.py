"""A host deployment (T25): the B0 checks from the records, the run's steps in order with a failed one resumed from
where it stopped, the host-side scripts (network checks, NFS, FRR as the hosts have it), the dialog's confirmations."""

import inspect
import os
import re
from unittest import mock

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from accounts.tests import Base
from inventory import hostdeploy
from inventory.models import (
    Cluster,
    DiskLayout,
    Host,
    HostDeployment,
    Network,
    Platform,
    PlatformControl,
    PlatformDatastore,
    Secret,
    ServerProfile,
    Site,
    Storage,
    StorageVolume,
)
from tenants.models import PortalSettings

KEY = override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode())


def pem():
    return (
        ed25519.Ed25519PrivateKey.generate()
        .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption())
        .decode()
    )


def world():
    site = Site.objects.create(
        code="zk1",
        timezone="Asia/Almaty",
        ntp_servers=["10.201.100.201"],
        dns_servers=["10.201.101.11"],
        log_collectors={
            "ipmi": {"host": "onelog1", "port": 15141, "proto": "udp"},
            "core": {"host": "onelog1", "port": 15140, "proto": "tcp"},
        },
    )
    sa = Secret.objects.create(kind="user", name="sa", username="portal")
    sa.put("private_key", pem())
    sa.save()
    one = Secret.objects.create(kind="opennebula", name="cloud", username="oneadmin", public_key="ssh-ed25519 AAAA one")
    one.put("os_private_key", pem())
    one.save()
    p = Platform.objects.create(
        key="zk1.cloud", site=site, name="cloud", kind="shared", sa=sa.ref, fe_secrets=one.ref, fe_vip="10.201.100.10"
    )  # commit-528: a front end the portal asks (its VIP)
    PlatformControl.objects.create(key="zk1.cloud", mode="execution")
    for name, kind, iface, vlan, mtu, subnet, gw, extra, rrs, asn in (
        ("one-core", "core-kvm", "bond0", 100, 1500, "10.201.100.0", "10.201.100.1", {}, [], None),
        (
            "cloud-vxlan",
            "evpn",
            "bond0",
            102,
            9050,
            "10.201.102.0",
            None,
            {"bridge": "vxlan"},
            ["10.201.102.241", "10.201.102.242"],
            4220110200,
        ),
        (
            "portal-vxlan",
            "evpn",
            "bond0",
            108,
            9050,
            "10.201.108.0",
            None,
            {"bridge": "vxlan"},
            ["10.201.108.241"],
            4220110800,
        ),
        ("data-nfs", "nfs", "bond1", 111, 9000, "10.201.111.0", None, {}, [], None),
    ):
        Network.objects.create(
            site="zk1",
            name=name,
            kind=kind,
            interface=iface,
            vlan=vlan,
            mtu=mtu,
            subnet=subnet,
            prefix=24,
            gateway=gw,
            extra=extra,
            rrs=rrs,
            local_asn=asn,
        )
    nfs = Storage.objects.create(
        site=site, name="c8k-svm2", kind="nfs", targets=["10.201.111.241"], fstab="nfs4  _netdev,rw,vers=4.2  0 0"
    )
    c = Cluster.objects.create(platform=p, name="shared-intel-1_zk1", kind="shared")
    for name, kind, dev in (
        ("cloud-vms01_zk1", "vms", "/cloud_vms_1"),
        ("cloud-images01_zk1", "images", "/cloud_images_1"),
        ("cloud-files01_zk1", "files", "/cloud_files_1"),
    ):
        v = StorageVolume.objects.create(storage=nfs, name=dev.strip("/"), device=dev, size_gb=1)
        c.datastores.add(PlatformDatastore.objects.create(platform=p, name=name, kind=kind, volume=v))
    prof = ServerProfile.objects.create(
        code="ci1",
        ipmi_type="ilo6",
        boot={"dev": "/dev/nvme0n1", "size_gb": 480},
        nics={
            "cards": [
                {"slot": "1", "name": "ens1", "ports": 2, "links": ["ens1f0np0", "ens1f1np1"]},
                {"slot": "15", "name": "ens15", "ports": 2, "links": ["ens15f0np0", "ens15f1np1"]},
            ],
            "fabrics": 2,
            "assign": {
                "nic-data1": "ens1f0np0",
                "nic-data2": "ens15f0np0",
                "nic-storage1": "ens1f1np1",
                "nic-storage2": "ens15f1np1",
            },
        },
        layout=DiskLayout.objects.get(code="standard"),
        bios_version="U54 v2.94 (06/25/2026)",
        ilo_version="iLO 6 v1.77",
        bios={"AutoPowerOn": "RestoreLastState"},
    )
    h = Host.objects.create(
        site=site,
        name="kvm014",
        profile=prof,
        ip="10.201.100.14",
        ipmi_host="10.201.201.14",
        ipmi_user="secret:ipmi",
        ipmi_type="ilo6",
    )
    c.hosts.add(h)
    PortalSettings.objects.filter(pk=PortalSettings.get().pk).update(internal_domain="example.cloud")
    return h, c


@KEY
class Checks(TestCase):
    def setUp(self):
        self.h, self.c = world()
        self.node = mock.patch("accounts.hostmedia.node_for", return_value="10.201.101.25")
        self.node.start()

    def tearDown(self):
        self.node.stop()

    def test_ready(self):
        self.assertEqual(hostdeploy.problems(self.h, self.c), [])
        nets = [n.name for n in hostdeploy.host_networks(self.h, self.c.platform)]
        self.assertEqual(nets, ["one-core", "cloud-vxlan", "data-nfs"])  # not the portal's EVPN network
        self.assertEqual(
            [x[:2] for x in hostdeploy.nfs_datastores(self.c)],
            [
                ("cloud-images01_zk1", "10.201.111.241:/cloud_images_1"),
                ("cloud-vms01_zk1", "10.201.111.241:/cloud_vms_1"),
            ],
        )

    def test_what_stops_it(self):
        ServerProfile.objects.filter(code="ci1").update(bios_version="", layout=None)
        PortalSettings.objects.filter(pk=PortalSettings.get().pk).update(internal_domain="")
        Network.objects.filter(name="data-nfs").delete()
        self.h.refresh_from_db()
        why = " | ".join(hostdeploy.problems(self.h, self.c, power="On"))
        for part in ("no boot storage", "BIOS and iLO versions", "internal domain", "no NFS network", "powered on"):
            self.assertIn(part, why)
        self.assertNotIn("powered on", " ".join(hostdeploy.problems(self.h, self.c, confirmed=True, power="On")))

    def test_off_the_baseline(self):
        with mock.patch("inventory.baseline.problem", return_value="Upgrade required: zk1.cloud"):
            self.assertIn("Upgrade required: zk1.cloud", hostdeploy.problems(self.h, self.c))


@KEY
class Engine(TestCase):
    def setUp(self):
        self.h, self.c = world()
        self.dep = HostDeployment.objects.create(host=self.h, cluster=self.c)
        self.calls = []

    def fake_steps(self, fail_at=None):
        def make(k):
            def fn(r):
                self.calls.append(k)
                if k == fail_at:
                    raise hostdeploy.DeployError("boom")
                return f"{k} ok"

            return fn

        return {k: make(k) for k in hostdeploy.STEP_KEYS}

    def test_stops_and_resumes_from_the_failed_step(self):
        with mock.patch.dict(hostdeploy.STEPS, self.fake_steps(fail_at="media")):
            self.assertFalse(hostdeploy.run(self.dep))
        self.dep.refresh_from_db()
        self.assertEqual(self.dep.state, "failed")
        self.assertEqual(self.dep.steps["media"]["status"], "failed")
        self.assertIn("media: FAILED: boom", self.dep.log)
        self.calls.clear()
        HostDeployment.objects.filter(pk=self.dep.pk).update(data={"installer": {"file": "u.iso", "sha256": "a" * 64}})
        self.dep.refresh_from_db()
        with (
            mock.patch.dict(hostdeploy.STEPS, self.fake_steps()),
            mock.patch.object(hostdeploy, "firewall_rules", return_value=[]),
            mock.patch.object(hostdeploy.ilo, "ensure_lldp", return_value=""),
            mock.patch.object(hostdeploy.Run, "c", new_callable=mock.PropertyMock),
            mock.patch("accounts.hostmedia.on_site_node", return_value=(True, "")) as served,
        ):
            self.assertTrue(hostdeploy.run(self.dep))
        served.assert_called_once_with("zk1", {"file": "u.iso", "sha256": "a" * 64})  # commit-142: its own ISO
        self.assertEqual(self.calls[0], "media")  # the done steps are not run again
        self.dep.refresh_from_db()
        self.assertEqual(self.dep.state, "done")

    def test_a_resume_stops_when_its_installer_is_not_served(self):
        """commit-142 (verify): before any step that powers off or erases."""
        with mock.patch.dict(hostdeploy.STEPS, self.fake_steps(fail_at="storage")):
            hostdeploy.run(self.dep)
        self.dep.refresh_from_db()
        HostDeployment.objects.filter(pk=self.dep.pk).update(
            state="running", data={"installer": {"file": "u.iso", "sha256": "a" * 64}}
        )
        self.dep.refresh_from_db()
        self.calls.clear()
        with (
            mock.patch.dict(hostdeploy.STEPS, self.fake_steps()),
            mock.patch("accounts.hostmedia.on_site_node", return_value=(False, "not at at1")),
        ):
            self.assertFalse(hostdeploy.run(self.dep))
        self.assertEqual(self.calls, [])
        self.dep.refresh_from_db()
        self.assertEqual(self.dep.state, "failed")
        self.assertIn("resume refused: not at at1", self.dep.log)


@KEY
class Scripts(TestCase):
    def setUp(self):
        self.h, self.c = world()
        self.r = hostdeploy.Run(HostDeployment.objects.create(host=self.h, cluster=self.c))

    def test_network_script(self):
        text = hostdeploy.network_script(self.r)
        self.assertIn("/proc/net/bonding/bond0", text)
        self.assertIn("ping -c 2 -W 2 -M do -s 1472 10.201.100.1", text)
        self.assertIn("-s 9022 10.201.102.241", text)

    def test_nfs_script(self):
        text = hostdeploy.nfs_script([("100", "10.201.111.241:/cloud_vms_1", "nfs4", "_netdev,rw")])
        self.assertIn("10.201.111.241:/cloud_vms_1   /var/lib/one/datastores/100   nfs4 _netdev,rw 0 0", text)
        self.assertIn("sudo -u oneadmin touch /var/lib/one/datastores/100/.claugine-check", text)

    def frr(self, password="bgp-hosts", value="s3cret", network="cloud-vxlan", r=None):
        from inventory.models import PeerGroup

        evpn = Network.objects.get(site="zk1", name=network)
        PeerGroup.objects.update_or_create(
            network=evpn, name="hosts", defaults={"hosts": [evpn.subnet + "/24"], "password": password}
        )
        with mock.patch("inventory.credentials.value", return_value=value):
            return hostdeploy.frr_conf(r or self.r)

    def test_frr_a_password_with_the_marker_never_leaks(self):
        """commit-198: a password holding « is written as it is and never reaches an error message."""
        conf = self.frr(value="ab«cd»ef")
        self.assertIn("neighbor fabric password ab«cd»ef", conf)
        with self.assertRaises(hostdeploy.DeployError) as cm:
            self.frr(value="")
        self.assertNotIn("ab", str(cm.exception))

    def test_frr_a_password_starting_with_the_missing_marker_is_a_password(self):
        """commit-199: a real value that begins with «missing: is written, not refused."""
        self.assertIn("neighbor fabric password «missing:x", self.frr(value="«missing:x"))

    def test_frr_a_credential_without_value_anywhere_in_the_template_stops(self):
        """commit-199: a customised template using a credential (other than the password line) that has no value."""
        from inventory.models import ConfigTemplate

        t = ConfigTemplate.objects.get(role="host-frr")
        t.body += "! key {{ passwords.int }}\n"
        t.save()
        with self.assertRaises(hostdeploy.DeployError) as cm:
            self.frr(value="")
        self.assertIn("credentials without a value", str(cm.exception))

    def test_frr_a_credential_in_a_conditional_is_one_evaluation(self):
        """commit-199/200 (second review): a branch on a credential is decided once - the masked and the written text
        come from the same evaluation, and a credential without a value in that branch stops the step."""
        from inventory import nfconfig
        from inventory.models import ConfigTemplate

        t = ConfigTemplate.objects.get(role="host-frr")
        t.body += "{% if passwords.int %}! key {{ passwords.int }}{% endif %}\n"
        t.save()
        self.frr(value="s3cret")
        with mock.patch("inventory.credentials.value", return_value="s3cret"):
            masked, revealed, missing = nfconfig.build_host_frr("zk1", "kvm014", "10.201.102.14", "cloud-vxlan")
        self.assertEqual(re.sub(r"«secret:[^»]*»", "X", masked), revealed.replace("s3cret", "X"))
        self.assertIn("! key s3cret", revealed)
        with self.assertRaises(hostdeploy.DeployError) as cm:
            self.frr(value="")
        self.assertIn("credentials without a value", str(cm.exception))

    def test_frr_a_filtered_credential_stops_before_writing(self):
        """commit-201: |lower on the password elsewhere - the step stops, with no value in the message."""
        from inventory.models import ConfigTemplate

        t = ConfigTemplate.objects.get(role="host-frr")
        body = t.body
        for extra in (  # commit-201: any filter in the template, an alias included, stops it before rendering
            "! key {{ passwords.int|lower }}",
            "! key {{ passwords.int|slugify|slice:':8' }}",
            "{% with k=passwords.int %}! key {{ k|upper }}{% endwith %}",
            "{% if passwords.int|length > 3 %}! x{% endif %}",
            "{% filter slugify %}{% filter upper %}! key {{ passwords.int }}{% endfilter %}{% endfilter %}",
        ):
            t.body = body + extra + "\n"
            t.save()
            with self.assertRaises(hostdeploy.DeployError) as cm:
                self.frr(value="S3CRET")
            self.assertIn("not supported there", str(cm.exception))
            self.assertNotIn("3cret", str(cm.exception).lower())

    def test_frr_checked_on_django_s_own_tokens(self):
        """commit-201/203: a stray "{% if" in plain text before each tag hid a filter block from a text scan; Django
        runs the block, and the check - on Django's tokens - finds it, by its line."""
        from inventory.models import ConfigTemplate

        t = ConfigTemplate.objects.get(role="host-frr")
        lines = t.body.count("\n")
        t.body += "! {% if \n{% filter lower %}! key {{ passwords.int }}! {% if \n{% endfilter %}\n"
        t.save()
        with self.assertRaises(hostdeploy.DeployError) as cm:
            self.frr(value="s3cret")
        self.assertIn(f"line {lines + 2}: a tag other than for / if", str(cm.exception))

    def test_frr_the_rejection_names_no_template_text(self):
        """commit-204: a value typed into a rejected expression never reaches the message (the run's log, the audit)."""
        from inventory.models import ConfigTemplate

        t = ConfigTemplate.objects.get(role="host-frr")
        t.body += '! {{ passwords.missing|default:"SuperSecret-123" }}\n{% with k="Other-456" %}{% endwith %}\n'
        t.save()
        with self.assertRaises(hostdeploy.DeployError) as cm:
            self.frr(value="s3cret")
        msg = str(cm.exception)
        self.assertIn("a substitution with a filter or an expression", msg)
        self.assertIn("a tag other than for / if", msg)
        for secret in ("SuperSecret", "Other-456", "default"):
            self.assertNotIn(secret, msg)

    def test_frr_a_comment_is_no_template_code(self):
        """commit-202: a filter written in a comment is not executed, so it does not stop the step."""
        from inventory.models import ConfigTemplate

        t = ConfigTemplate.objects.get(role="host-frr")
        t.body += "{# e.g. {{ passwords.int|lower }} is not supported #}\n"
        t.save()
        self.assertIn("neighbor fabric password s3cret", self.frr(value="s3cret"))

    def test_frr_checked_and_written_from_one_snapshot(self):
        """commit-200: the template is read once; what is checked and what is written are the same rendering."""
        from inventory import nfconfig
        from inventory.models import ConfigTemplate

        real = ConfigTemplate.objects.filter
        calls = []

        def filter_(*a, **k):
            calls.append(k)
            return real(*a, **k)

        with mock.patch.object(ConfigTemplate.objects, "filter", side_effect=filter_):
            conf = self.frr(value="s3cret")
        self.assertEqual([c for c in calls if c.get("role") == "host-frr"], [{"role": "host-frr"}])
        with mock.patch("inventory.credentials.value", return_value="s3cret"):
            masked, revealed, missing = nfconfig.build_host_frr("zk1", "kvm014", "10.201.102.14", "cloud-vxlan")
        self.assertEqual(revealed, conf)
        self.assertEqual(masked.replace("«secret:", "").count("s3cret"), 0)
        self.assertEqual(missing, [])

    def test_frr_from_the_run_s_own_evpn_network(self):
        """commit-197: the template is filled from the EVPN network the run uses (here the portal's)."""
        evpn = {
            "name": "portal-vxlan",
            "kind": Network.Kind.EVPN,
            "asn": 4220110800,
            "rrs": ["10.201.108.241"],
            "cidr": "10.201.108.0/24",
        }
        with (
            mock.patch.object(hostdeploy.Run, "nets", return_value=[evpn]),
            mock.patch.object(hostdeploy, "evpn_address", return_value="10.201.108.14/24"),
        ):
            conf = self.frr(network="portal-vxlan")
        self.assertIn("router bgp 4220110800", conf)
        self.assertIn("neighbor 10.201.108.241 peer-group fabric", conf)
        self.assertNotIn("10.201.102.241", conf)

    def test_frr_as_the_hosts_have_it(self):
        """Owner 2026-11-19 20:24 "fix FRR": the site's host-frr template - the hosts' BGP password included."""
        conf = self.frr()
        for line in (
            "hostname kvm014",
            "router bgp 4220110200",
            "bgp router-id 10.201.102.14",
            "neighbor fabric update-source 10.201.102.14",
            "neighbor fabric password s3cret",
            "neighbor 10.201.102.241 peer-group fabric",
            "advertise-all-vni",
            "timers bgp 3 15",
        ):
            self.assertIn(line, conf)
        self.assertNotIn("«", conf)

    def test_frr_without_the_hosts_password_stops(self):
        with self.assertRaisesMessage(hostdeploy.DeployError, "BGP password"):
            self.frr(password="")

    def test_repos(self):
        text = hostdeploy.repos_script(self.r, {"os": "26.04", "one": "7.4", "frr": "10.6"})
        self.assertIn("https://downloads.opennebula.io/repo/7.4/Ubuntu/26.04 stable opennebula", text)
        self.assertIn("frr-10.6", text)

    def test_repos_private(self):
        """Owner 2026-11-23 (Q8), per platform since T36 B2: Enterprise Edition - the enterprise repository; its key
        from the public one."""
        self.r.p.one_ee = True
        text = hostdeploy.repos_script(self.r, {"os": "26.04", "one": "7.4", "frr": "10.6"})
        self.assertIn("https://enterprise.opennebula.io/repo/7.4/Ubuntu/26.04 stable opennebula", text)
        self.assertIn("https://downloads.opennebula.io/repo/repo2.key", text)

    def test_repos_from_the_platform(self):
        """T36 B2 (rules v2 B4): the version, the repository and its login are the platform's."""
        self.r.p.one_version, self.r.p.one_ee, self.r.p.one_login = "7.6", False, ""
        r = mock.Mock(p=self.r.p, fns=["kvm", "frr"])  # T36 B4a: the host service's functions
        hostdeploy.s_repos(r)
        self.assertIn("https://downloads.opennebula.io/repo/7.6/", r.ssh.call_args[0][0])
        self.assertEqual(r.ssh.call_args[1]["stdin_extra"], "")
        self.r.p.one_ee, self.r.p.one_login = True, "sec:1"
        sec = mock.Mock(has=lambda k: k == "password", get=lambda k: "pw")
        with (
            mock.patch.object(hostdeploy, "_account", return_value=("repo-user", "", "")),
            mock.patch("inventory.credentials.find", return_value=sec),
        ):
            hostdeploy.s_repos(r)
        self.assertIn("https://enterprise.opennebula.io/repo/7.6/", r.ssh.call_args[0][0])
        self.assertIn("login repo-user\npassword pw", r.ssh.call_args[1]["stdin_extra"])


@KEY
class Dialog(Base):
    def setUp(self):
        self.h, self.c = world()
        self.sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(self.sa)

    def test_the_name_must_be_typed(self):
        url = reverse("inv_host_deploy", args=["zk1", self.h.name])
        with mock.patch.object(hostdeploy, "start") as start:
            self.client.post(url, {"cluster": self.c.pk, "confirm_name": "wrong"})
            start.assert_not_called()
            with mock.patch.object(hostdeploy, "problems", return_value=[]):
                self.client.post(url, {"cluster": self.c.pk, "confirm_name": self.h.name, "reset_factory": "on"})
            start.assert_called_once()
            self.assertTrue(start.call_args.kwargs["reset_factory"])

    def test_the_page_shows_deploy(self):
        r = self.client.get(reverse("infra_site", args=["zk1"]) + "?tab=hosts")
        self.assertContains(r, f'data-open="dlg-deploy-{self.h.name}"')
        self.assertContains(r, "Reset to factory")


@KEY
class InternalDomain(Base):
    """Owner 2026-11-19: the hosts' domain is Settings → Customer's internal domain."""

    def setUp(self):
        self.sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(self.sa)

    def test_saved_on_the_customer_tab(self):
        url = reverse("portal_settings")
        self.assertContains(self.client.get(url + "?tab=customer"), 'name="internal_domain"')
        self.assertNotContains(self.client.get(reverse("infra_os")), 'name="internal_domain"')
        self.client.post(url, {"tab": "customer", "section": "customers", "internal_domain": "Example.Cloud."})
        self.assertEqual(PortalSettings.get().internal_domain, "example.cloud")
        r = self.client.post(
            url, {"tab": "customer", "section": "customers", "internal_domain": "not a domain"}, follow=True
        )
        self.assertContains(r, "Internal domain: a DNS domain")
        self.assertEqual(PortalSettings.get().internal_domain, "example.cloud")

    def test_an_old_form_leaves_it_and_long_is_refused(self):
        """commit-147, commit-148."""
        url = reverse("portal_settings")
        PortalSettings.objects.filter(pk=PortalSettings.get().pk).update(internal_domain="example.cloud")
        self.client.post(url, {"tab": "customer", "section": "customers", "corporate_domains": "corp.kz"})
        st = PortalSettings.get()
        self.assertEqual((st.internal_domain, st.corporate_domains), ("example.cloud", "corp.kz"))
        self.assertContains(self.client.get(url + "?tab=customer"), 'maxlength="100"')
        long = "a" * 60 + "." + "b" * 60 + ".cloud"
        r = self.client.post(url, {"tab": "customer", "section": "customers", "internal_domain": long}, follow=True)
        self.assertContains(r, "Internal domain: at most 100 characters.")
        self.assertEqual(PortalSettings.get().internal_domain, "example.cloud")

    def test_migrations_rename_the_released_column(self):
        """commit-146: 0104 adds hosts_domain as it was first committed; 0105 renames it."""
        from django.db.migrations.loader import MigrationLoader

        g = MigrationLoader(None, ignore_no_migrations=True).graph
        add = g.nodes[("tenants", "0104_host_deployment")].operations[0]
        self.assertEqual(add.name, "hosts_domain")
        ren = g.nodes[("tenants", "0105_internal_domain")].operations[0]
        self.assertEqual((ren.old_name, ren.new_name), ("hosts_domain", "internal_domain"))


@KEY
class ReviewFixes(TestCase):
    """commit-137..144 and the core-lm network (owner 2026-11-19)."""

    def setUp(self):
        self.h, self.c = world()

    def test_the_live_migration_network(self):
        Network.objects.create(
            site="zk1",
            name="one-lm",
            kind="core-lm",
            interface="bond0",
            vlan=105,
            mtu=9000,
            subnet="10.201.105.0",
            prefix=24,
        )
        self.assertEqual(Network.kind_for("one-lm"), "core-lm")
        nets = [n.name for n in hostdeploy.host_networks(self.h, self.c.platform)]
        self.assertEqual(nets, ["one-core", "cloud-vxlan", "one-lm", "data-nfs"])

    def test_the_nfs_probe_fails_the_script(self):
        text = hostdeploy.nfs_script([("100", "10.201.111.241:/cloud_vms_1", "nfs4", "_netdev")])
        self.assertIn('|| { echo "oneadmin cannot write in /var/lib/one/datastores/100" >&2; exit 1; }', text)

    def test_local_storage_by_identity(self):
        self.assertIn("/dev/disk/by-id/wwn-0x$id", hostdeploy.LOCAL_FS)
        self.assertIn("nothing is wiped", hostdeploy.LOCAL_FS)
        r = hostdeploy.Run(
            HostDeployment.objects.create(
                host=self.h, cluster=self.c, data={"local": [{"controller": "A", "raid": "RAID5"}]}
            )
        )
        with self.assertRaisesMessage(hostdeploy.DeployError, "not read back"):
            hostdeploy.s_local_fs(r)

    def test_one_claim_at_a_time_and_an_interrupted_run_resumes(self):
        from accounts import tasks

        with (
            mock.patch("accounts.ha.forbid_writes"),
            mock.patch.object(tasks, "start") as tstart,
            self.captureOnCommitCallbacks(execute=True),
        ):
            dep = hostdeploy.start(None, self.h, self.c)
        tstart.assert_called_once()
        with mock.patch("accounts.ha.forbid_writes"), self.assertRaisesMessage(hostdeploy.DeployError, "running"):
            hostdeploy.start(None, self.h, self.c)  # its claim is fresh: still starting
        HostDeployment.objects.filter(pk=dep.pk).update(
            steps={"checks": {"status": "done"}, "media": {"status": "running"}},
            data={"claim": "x", "claim_at": "2000-01-01T00:00:00+00:00"},
        )
        self.assertEqual(hostdeploy.reap_interrupted(), 1)
        dep.refresh_from_db()
        self.assertEqual((dep.state, dep.steps["media"]["status"]), ("failed", "failed"))
        with mock.patch("accounts.ha.forbid_writes"), mock.patch.object(tasks, "start"):
            again = hostdeploy.start(None, self.h, self.c)
        self.assertEqual(again.pk, dep.pk)  # resumed, not a new run
        self.assertFalse(hostdeploy.run(again, claim="not-mine"))

    def test_a_reinstall_stops_while_the_host_has_vms(self):
        import xml.etree.ElementTree as ET

        r = hostdeploy.Run(HostDeployment.objects.create(host=self.h, cluster=self.c))  # commit-137: no saved ID
        one = mock.Mock()
        one.xml.return_value = ET.fromstring("<HOST><VMS><ID>42</ID></VMS></HOST>")
        with (
            mock.patch.object(hostdeploy, "_one", return_value=one),
            mock.patch.object(hostdeploy, "_one_host_id", return_value=7),
            mock.patch.object(hostdeploy.ilo, "power") as power,
            self.assertRaisesMessage(hostdeploy.DeployError, "1 VM(s)"),
        ):
            hostdeploy.s_power_off(r)
        one.call.assert_called_once_with("one.host.status", 7, 1)  # disabled again right before
        power.assert_not_called()


@KEY
class ReviewFixes2(TestCase):
    """commit-138..143 verified open, fixed again (owner 2026-11-19 08:04)."""

    def setUp(self):
        self.h, self.c = world()

    def test_only_the_volume_by_its_identity_and_a_repeat_reuses_it(self):
        """commit-138, commit-140."""
        self.assertNotIn("NAME,SIZE", hostdeploy.LOCAL_FS)  # no disk chosen by its size
        self.assertIn('[ -n "$dev" ] || { echo "the RAID volume', hostdeploy.LOCAL_FS)
        self.assertIn("-S vg_name=vg_one", hostdeploy.LOCAL_FS)
        wipe = hostdeploy.LOCAL_FS.index("wipefs")
        # commit-140 (again): no early exit - a mount there must be lv_one, the owner is always set
        self.assertNotIn("already mounted", hostdeploy.LOCAL_FS)
        self.assertIn("not vg_one/lv_one: nothing is wiped", hostdeploy.LOCAL_FS)
        self.assertLess(hostdeploy.LOCAL_FS.index("not vg_one/lv_one"), wipe)
        self.assertIn('mountpoint -q "$mnt" || mount "$mnt"\nchown oneadmin:oneadmin "$mnt"', hostdeploy.LOCAL_FS)
        # commit-158: vg_one already here must be on this disk alone, checked before anything is wiped
        self.assertIn("not $dev: nothing is wiped", hostdeploy.LOCAL_FS)
        self.assertLess(hostdeploy.LOCAL_FS.index("vgs_all=$(vgs"), wipe)
        self.assertLess(hostdeploy.LOCAL_FS.index("not $dev: nothing is wiped"), wipe)

    def test_the_reaper_leaves_a_run_claimed_again(self):
        """commit-139: only the claim it read, only while running, never with a live task."""
        from accounts import tasks

        dep = HostDeployment.objects.create(
            host=self.h,
            cluster=self.c,
            state="running",
            steps={"media": {"status": "running"}},
            data={"claim": "new", "claim_at": "2000-01-01T00:00:00+00:00"},
        )
        self.assertFalse(hostdeploy.mark_interrupted(dep, claim="old"))
        dep.refresh_from_db()
        self.assertEqual(dep.state, "running")
        with mock.patch.object(tasks, "running", return_value=True):
            self.assertEqual(hostdeploy.reap_interrupted(), 0)
        self.assertEqual(hostdeploy.reap_interrupted(), 1)
        dep.refresh_from_db()
        self.assertEqual(dep.state, "failed")

    def test_a_run_taken_over_stops_before_its_next_step(self):
        """commit-139."""
        dep = HostDeployment.objects.create(host=self.h, cluster=self.c, state="running", data={"claim": "mine"})
        seen = []

        def first(r):
            seen.append("checks")
            HostDeployment.objects.filter(pk=dep.pk).update(data={"claim": "other"})
            return "ok"

        steps = {k: (lambda r, k=k: seen.append(k)) for k in hostdeploy.STEP_KEYS}
        steps["checks"] = first
        with mock.patch.dict(hostdeploy.STEPS, steps):
            self.assertFalse(hostdeploy.run(dep, claim="mine"))
        self.assertEqual(seen, ["checks"])

    def test_an_existing_registration_in_another_cluster_is_refused(self):
        """commit-143."""
        import xml.etree.ElementTree as ET

        r = hostdeploy.Run(HostDeployment.objects.create(host=self.h, cluster=self.c))
        one = mock.Mock()
        one.xml.return_value = ET.fromstring("<HOST><CLUSTER_ID>9</CLUSTER_ID></HOST>")
        with (
            mock.patch.object(hostdeploy, "_one", return_value=one),
            mock.patch.object(hostdeploy, "_one_host_id", return_value=7),
            mock.patch.object(hostdeploy, "_one_cluster_id", return_value=3),
            self.assertRaisesMessage(hostdeploy.DeployError, "in cluster #9"),
        ):
            hostdeploy.s_one_add(r)
        one.call.assert_not_called()

    def test_both_ring_sizes_are_checked(self):
        """commit-135."""
        text = hostdeploy.network_script(hostdeploy.Run(HostDeployment.objects.create(host=self.h, cluster=self.c)))
        self.assertIn("/^RX:/", text)
        self.assertIn("/^TX:/", text)


@KEY
class ReviewFixes3(TestCase):
    """commit-142, commit-143 (owner 2026-11-19 08:26)."""

    def setUp(self):
        self.h, self.c = world()

    def test_unfinished_runs_pin_their_installer(self):
        from accounts import hostmedia

        pin = {"file": "ubuntu-26.04.1-live-server-amd64-claugine-bbbbbbbbbbbb.iso", "sha256": "a" * 64}
        HostDeployment.objects.create(host=self.h, cluster=self.c, state="failed", data={"installer": pin})
        HostDeployment.objects.create(
            host=self.h, cluster=self.c, state="done", data={"installer": {**pin, "file": "x"}}
        )
        self.assertEqual(hostmedia.pinned(), [pin])

    def test_records_read_the_cluster_again(self):
        import xml.etree.ElementTree as ET

        self.c.hosts.remove(self.h)
        r = hostdeploy.Run(
            HostDeployment.objects.create(
                host=self.h, cluster=self.c, data={"one_id": 7, "one_endpoint": "http://10.201.100.10:2633/RPC2"}
            )
        )
        one = mock.Mock()
        one.client.url = "http://10.201.100.10:2633/RPC2"
        one.xml.return_value = ET.fromstring("<HOST><NAME>kvm014</NAME><CLUSTER_ID>9</CLUSTER_ID></HOST>")
        with (
            mock.patch.object(hostdeploy, "_one", return_value=one),
            mock.patch.object(hostdeploy, "_one_cluster_id", return_value=3),
            self.assertRaisesMessage(hostdeploy.DeployError, "in cluster #9"),
        ):
            hostdeploy.s_records(r)
        self.assertFalse(self.c.hosts.filter(pk=self.h.pk).exists())

    def test_the_installer_is_pinned_under_the_lock_even_when_a_check_fails(self):
        """commit-142: chosen and pinned under the state lock, before the other node is asked."""
        from accounts import hostmedia

        r = hostdeploy.Run(HostDeployment.objects.create(host=self.h, cluster=self.c, power_confirmed=True))
        pin = {"version": "26.04.1", "file": "u.iso", "sha256": "a" * 64}
        order = []

        class Lock:
            def __enter__(self):
                order.append("lock")

            def __exit__(self, *a):
                order.append("unlock")

        def site(_site, p):
            order.append(("asked", dict(HostDeployment.objects.get(pk=r.dep.pk).data).get("installer")))
            return False, "not there"

        one = mock.Mock()
        with (
            mock.patch.object(hostmedia, "check", return_value=(True, "current")),
            mock.patch.object(hostmedia, "locked", return_value=Lock()),
            mock.patch.object(hostmedia, "current", return_value=pin),
            mock.patch.object(hostmedia, "has", return_value=True),
            mock.patch.object(hostmedia, "on_site_node", side_effect=site),
            mock.patch.object(hostdeploy.Run, "c", new_callable=mock.PropertyMock),
            mock.patch.object(hostdeploy.ilo, "power_state", return_value="Off"),
            mock.patch.object(hostdeploy, "problems", return_value=[]),
            mock.patch.object(hostdeploy, "_one", return_value=one),
            mock.patch.object(hostdeploy, "_one_host_id", return_value=None),
            self.assertRaisesMessage(hostdeploy.DeployError, "not there"),
        ):
            hostdeploy.s_checks(r)
        self.assertEqual(order[:2], ["lock", "unlock"])
        self.assertEqual(order[2], ("asked", {"version": "26.04.1", "file": "u.iso", "sha256": "a" * 64}))


@KEY
class LocalFsShell(TestCase):
    """commit-158: LOCAL_FS run in bash with the system commands stubbed - what it wipes and when."""

    def run_script(self, vgs, pvs="", readlink=None):
        import os
        import subprocess
        import tempfile

        # commit-162: readlink is the test's own (fixed canonical paths), not the system's; commit-163: cleaned up
        readlink = (
            readlink
            or 'case "$2" in /dev/sdb|/dev/disk/sdb) echo /dev/sdb ;; /dev/sdc) echo /dev/sdc ;; *) exit 1 ;; esac'
        )
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "log")
            stubs = {
                "mountpoint": "exit 1",
                "lsblk": "echo 1000",
                "vgs": vgs,
                "pvs": pvs if pvs.startswith(("exit", "echo")) else f"echo '{pvs}'",
                "readlink": readlink,
                "wipefs": f"echo wipefs >> {log}",
                "pvcreate": f"echo pvcreate >> {log}",
                "vgcreate": f"echo vgcreate >> {log}",
                "lvs": "exit 0",
                "blkid": "echo xfs",
                "install": "exit 0",
                "true": "exit 0",
                "mount": "exit 0",
                "chown": "exit 0",
                "chmod": "exit 0",
            }
            for name, body in stubs.items():
                path = os.path.join(d, name)
                with open(path, "w") as fh:
                    fh.write(f"#!/bin/bash\n{body}\n")
                os.chmod(path, 0o755)
            text = hostdeploy.LOCAL_FS
            a, b = text.index("bootdisk=$(lsblk"), text.index('[ "$(basename "$dev")"')
            text = text[:a] + "bootdisk=sda\ndev=/dev/sdb\n" + text[b:]
            text = text.replace("systemctl daemon-reload", "true").replace("/etc/fstab", os.path.join(d, "fstab"))
            r = subprocess.run(
                ["bash", "-c", "SIZE=1000\n" + text],
                capture_output=True,
                text=True,
                env={"PATH": f"{d}:/usr/bin:/bin"},
            )
            done = open(log).read().split() if os.path.exists(log) else []
        return r.returncode, r.stdout + r.stderr, done

    def test_unresolved_paths_never_count_as_the_same_disk(self):
        """commit-158/162: readlink gives nothing for either - not "the same", nothing wiped or reused."""
        rc, out, done = self.run_script("echo '  vg_one'", pvs="/dev/sdb", readlink="exit 1")
        self.assertEqual(rc, 1)
        self.assertIn("nothing is wiped", out)
        self.assertEqual(done, [])

    def test_a_failed_pv_read_or_two_pvs_wipe_nothing(self):
        """commit-158."""
        rc, out, done = self.run_script("echo '  vg_one'", pvs="echo 'busy' >&2; exit 5")
        self.assertEqual((rc, done), (1, []))
        self.assertIn("LVM could not be read (busy)", out)
        rc, out, done = self.run_script("echo '  vg_one'", pvs="echo '  /dev/sdb'; echo '  /dev/sdc'")
        self.assertEqual((rc, done), (1, []), out)

    def test_warnings_are_not_volumes_and_vg_one_among_others(self):
        """commit-164, commit-158."""
        rc, out, done = self.run_script(
            "echo '  vg_data'; echo '  vg_one'; echo 'WARNING: something' >&2",
            pvs="echo '  /dev/sdb'; echo 'WARNING: devices file is missing' >&2",
        )
        self.assertEqual((rc, done), (0, []), out)

    def test_a_failed_lvm_read_wipes_nothing(self):
        rc, out, done = self.run_script("echo 'locking failed' >&2; exit 5")
        self.assertEqual(rc, 1)
        self.assertIn("LVM could not be read (locking failed): nothing is wiped", out)
        self.assertEqual(done, [])

    def test_vg_one_on_another_disk_wipes_nothing(self):
        rc, out, done = self.run_script("echo '  vg_data'; echo '  vg_one'", pvs="/dev/sdc")
        self.assertEqual(rc, 1)
        self.assertIn("not /dev/sdb: nothing is wiped", out)
        self.assertEqual(done, [])

    def test_no_vg_one_is_made_and_vg_one_here_is_reused(self):
        rc, out, done = self.run_script("echo '  vg_data'")
        self.assertEqual((rc, done), (0, ["wipefs", "pvcreate", "vgcreate"]), out)
        rc, out, done = self.run_script("echo '  vg_one'", pvs="/dev/sdb")
        self.assertEqual((rc, done), (0, []), out)


@KEY
class StepReasons(TestCase):
    """commit-166: a step script's own reason reaches the run's error."""

    def test_a_reason_on_stdout_is_the_error_when_stderr_has_none(self):
        from inventory import sshexec

        done = mock.Mock(returncode=1, stdout="checking\n/dev/sdb is the boot disk: nothing is wiped\n", stderr="")
        with mock.patch.object(sshexec.subprocess, "run", return_value=done):
            with self.assertRaisesMessage(sshexec.SshError, "/dev/sdb is the boot disk: nothing is wiped"):
                sshexec._once("192.0.2.5", 22, "sa", "KEY", "sudo -n bash -s", None, "x", 10, stdout_reason=True)
        failed = mock.Mock(returncode=255, stdout="", stderr="")
        with mock.patch.object(sshexec.subprocess, "run", return_value=failed):
            with self.assertRaisesMessage(sshexec.SshError, "ssh exit 255"):
                sshexec._once("192.0.2.5", 22, "sa", "KEY", "sudo -n bash -s", None, "x", 10)

    def test_a_failed_configuration_read_never_shows_its_output(self):
        """commit-168: no stdout fallback unless asked - a router's configuration may hold secrets."""
        from inventory import sshexec

        cfg = mock.Mock(
            returncode=1,
            stdout="set service ssh\nset system login user vyos authentication plaintext-password s3cret\n",
            stderr="",
        )
        with mock.patch.object(sshexec.subprocess, "run", return_value=cfg):
            with self.assertRaises(sshexec.SshError) as ctx:
                sshexec.run("192.0.2.5", 22, "vyos", "KEY", "vbash -c 'show configuration commands'", site=None)
        self.assertNotIn("s3cret", str(ctx.exception) + ctx.exception.detail)
        self.assertIn("ssh exit 1", str(ctx.exception))

    def test_a_long_reason_is_whole_in_the_log_and_short_in_the_step(self):
        """commit-169."""
        from inventory import sshexec

        reason = "; ".join(f"bridge-{i}: no ping to 10.201.102.{i} at MTU 9000" for i in range(20))
        failed = mock.Mock(returncode=1, stdout="", stderr=reason + "\n")
        with mock.patch.object(sshexec.subprocess, "run", return_value=failed):
            with self.assertRaises(sshexec.SshError) as ctx:
                sshexec._once("192.0.2.5", 22, "sa", "KEY", "sudo -n bash -s", None, "x", 10, stdout_reason=True)
        self.assertLess(len(str(ctx.exception)), 330)
        self.assertIn("bridge-19", ctx.exception.detail)
        h, c = world()
        dep = HostDeployment.objects.create(host=h, cluster=c)

        def boom(r):
            raise ctx.exception

        steps = {k: (lambda r: "ok") for k in hostdeploy.STEP_KEYS}
        steps["checks"] = boom
        with mock.patch.dict(hostdeploy.STEPS, steps):
            self.assertFalse(hostdeploy.run(dep))
        dep.refresh_from_db()
        self.assertIn("bridge-19: no ping", dep.log)
        self.assertNotIn("bridge-19", str(dep.steps["checks"]))

    def test_both_attempts_reach_the_log(self):
        """commit-170: direct and through the jump host, both failed - both reasons in the run's log."""
        from inventory import sshexec

        def once(host, port, user, key, remote, jump, *a, **k):
            e = sshexec.SshError(f"{host}:{port}: " + ("no route to host" if jump is None else "jump refused the key"))
            e.detail, e.connect = str(e), True
            if jump is not None:  # commit-170: a jump-host reason that fills the whole budget
                e.detail = "x" * sshexec.DETAIL + " jump refused the key"
            raise e

        jump = {"host": "192.0.2.1", "port": 22, "user": "j", "key": "K"}
        with (
            mock.patch.object(sshexec, "_once", side_effect=once),
            mock.patch.object(sshexec, "jump_for", return_value=jump),
            self.assertRaises(sshexec.SshError) as ctx,
        ):
            sshexec.run("192.0.2.5", 22, "sa", "KEY", "sudo -n bash -s", site="zk1")
        h, c = world()
        dep = HostDeployment.objects.create(host=h, cluster=c)

        def boom(r):
            raise ctx.exception

        steps = {k: (lambda r: "ok") for k in hostdeploy.STEP_KEYS}
        steps["checks"] = boom
        with mock.patch.dict(hostdeploy.STEPS, steps):
            hostdeploy.run(dep)
        dep.refresh_from_db()
        self.assertIn("direct: 192.0.2.5:22: no route to host", dep.log)
        self.assertIn("via 192.0.2.1: x", dep.log)
        self.assertIn("jump refused the key", dep.log)
        self.assertLessEqual(len(ctx.exception.detail), sshexec.DETAIL)

    def test_the_network_check_lists_every_reason_on_stderr(self):
        import subprocess

        h, c = world()
        text = hostdeploy.network_script(hostdeploy.Run(HostDeployment.objects.create(host=h, cluster=c)))
        self.assertIn('why="$why; ', text)
        tail = text.splitlines()[-1]
        r = subprocess.run(
            ["bash", "-c", 'fail=1; why="; bond0: not LACP; one-core: no address"\n' + tail],
            capture_output=True,
            text=True,
        )
        self.assertEqual((r.returncode, r.stdout, r.stderr), (1, "", "bond0: not LACP; one-core: no address\n"))

    def test_the_scripts_give_their_reasons_on_stderr(self):
        self.assertIn('nothing is wiped" >&2; exit 1;', hostdeploy.LOCAL_FS)
        self.assertNotIn('nothing is wiped"; exit 1;', hostdeploy.LOCAL_FS)
        self.assertEqual(hostdeploy.LOCAL_FS.count("vgs --noheadings"), 1)  # each LVM read once
        self.assertEqual(hostdeploy.LOCAL_FS.count("pvs --noheadings"), 1)


@KEY
class StorageStep(TestCase):
    """commit-180: the storage step powers the host off again when it started it, even when it fails."""

    def setUp(self):
        self.h, self.c = world()

    def run_step(self, _power_before, discover, power_fails=False):
        r = hostdeploy.Run(HostDeployment.objects.create(host=self.h, cluster=self.c))
        calls = []
        with (
            mock.patch.object(hostdeploy.Run, "c", new_callable=mock.PropertyMock),
            mock.patch.object(hostdeploy.ilo, "discover_storage", side_effect=discover),
            mock.patch.object(hostdeploy.ilo, "local_storage", return_value=[]),
            mock.patch.object(
                hostdeploy.ilo, "power", side_effect=lambda c, on, **k: self.power(calls, on, power_fails)
            ),
        ):
            try:
                return hostdeploy.s_storage(r), calls
            except Exception as exc:  # noqa: BLE001
                return exc, calls

    @staticmethod
    def power(calls, on, fails):
        calls.append(on)
        if fails:
            raise hostdeploy.ilo.IloError("power Off: still 'On' after 600 s")

    def test_a_failed_power_off_fails_a_successful_step(self):
        """commit-182."""
        out, calls = self.run_step("On", lambda *a, **k: [], power_fails=True)
        self.assertIsInstance(out, hostdeploy.ilo.IloError)
        self.assertIn("power Off", str(out))

    def test_a_failed_step_keeps_its_error_when_the_power_off_fails_too(self):
        out, calls = self.run_step("Off", hostdeploy.ilo.IloError("storage read by iLO: timeout"), power_fails=True)
        self.assertIn("storage read by iLO", str(out))
        self.assertEqual(calls, [False])

    def test_an_interruption_ends_with_the_host_off(self):
        """commit-183."""
        r = hostdeploy.Run(HostDeployment.objects.create(host=self.h, cluster=self.c))
        calls = []
        with (
            mock.patch.object(hostdeploy.Run, "c", new_callable=mock.PropertyMock),
            mock.patch.object(hostdeploy.ilo, "discover_storage", side_effect=KeyboardInterrupt),
            mock.patch.object(hostdeploy.ilo, "power", side_effect=lambda c, on, **k: calls.append(on)),
            self.assertRaises(KeyboardInterrupt),
        ):
            hostdeploy.s_storage(r)
        self.assertEqual(calls, [False])

    def test_a_timeout_ends_with_the_host_off_and_its_own_error(self):
        out, calls = self.run_step("Off", hostdeploy.ilo.IloError("storage read by iLO: still None after 900 s"))
        self.assertIsInstance(out, hostdeploy.ilo.IloError)
        self.assertIn("storage read by iLO", str(out))
        self.assertEqual(calls, [False])

    def test_the_host_is_off_after_a_successful_step_too(self):
        out, calls = self.run_step(
            "On", lambda *a, **k: []
        )  # left on by the reset's POST: off for the installer's start
        self.assertEqual((out, calls), ("no local storage", [False]))


@KEY
class InstallCheck(TestCase):
    """Owner 2026-11-19 16:30: Ubuntu 26.04's live installer turns cloud-init off after the first boot."""

    DISABLED = (
        '{\n  "status": "disabled",\n  "errors": []\n}\n\nmarker={m}\n'
        "disabled_by=Disabled by Ubuntu live installer after first boot.\n"
        "boot_finished=yes\n"
    )

    def setUp(self):
        self.h, self.c = world()

    def step(self, out):
        d = HostDeployment.objects.create(host=self.h, cluster=self.c, data={"run_token": "656359b12567095b"})
        r = hostdeploy.Run(d, sleep=lambda s: None)
        with (
            mock.patch.object(hostdeploy.Run, "c", new_callable=mock.PropertyMock),
            mock.patch.object(hostdeploy.Run, "ssh", return_value=out),
            mock.patch.object(hostdeploy, "INSTALL_LIMIT", 60),
            mock.patch.object(hostdeploy.ilo, "boot_progress", return_value="OSBootStarted"),
            mock.patch.object(hostdeploy.ilo, "power_state", return_value="On"),
        ):
            try:
                return hostdeploy.s_install(r)
            except hostdeploy.DeployError as exc:
                return exc

    def test_disabled_by_the_installer_is_installed(self):
        out = self.step(self.DISABLED.replace("{m}", "656359b12567095b"))
        self.assertEqual(out, "installed by this run; cloud-init done and turned off by the installer")

    def test_disabled_otherwise_or_by_another_run_is_not(self):
        other = self.DISABLED.replace("{m}", "0000000000000000")
        self.assertIsInstance(self.step(other), hostdeploy.DeployError)
        by_hand = self.DISABLED.replace("{m}", "656359b12567095b").replace("Ubuntu live installer", "someone")
        self.assertIsInstance(self.step(by_hand), hostdeploy.DeployError)
        self.assertIsInstance(self.step("status: running\nmarker=656359b12567095b\n"), hostdeploy.DeployError)
        # commit-188: disabled by the installer while the final stage still runs (no boot-finished yet)
        running = self.DISABLED.replace("{m}", "656359b12567095b").replace("boot_finished=yes\n", "")
        self.assertIsInstance(self.step(running), hostdeploy.DeployError)

    def test_the_probe_ends_well_without_the_completion_file(self):
        """commit-190: the probe's own exit status is 0 when boot-finished is absent, so its output is read."""
        import subprocess

        with mock.patch.object(hostdeploy.Run, "ssh", return_value="") as ssh:
            d = HostDeployment.objects.create(host=self.h, cluster=self.c, data={"run_token": "x"})
            with (
                mock.patch.object(hostdeploy.Run, "c", new_callable=mock.PropertyMock),
                mock.patch.object(hostdeploy, "INSTALL_LIMIT", 0),
                mock.patch.object(hostdeploy.ilo, "boot_progress", return_value=""),
                mock.patch.object(hostdeploy.ilo, "power_state", return_value=""),
                self.assertRaises(hostdeploy.DeployError),
            ):
                hostdeploy.s_install(hostdeploy.Run(d, sleep=lambda s: None))
        import shlex
        import tempfile

        script = ssh.call_args.args[0]
        self.assertIn("/var/lib/cloud/instance/boot-finished", script)
        with tempfile.TemporaryDirectory() as tmp:  # commit-163, 191, 192: absent whatever the runner has, quoted
            absent = shlex.quote(os.path.join(tmp, "boot-finished"))
            script = script.replace("cloud-init status", "false")
            script = script.replace("/var/lib/cloud/instance/boot-finished", absent)
            r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("marker=", r.stdout)
        self.assertNotIn("boot_finished=yes", r.stdout)

    def test_done_is_installed(self):
        out = self.step('{"status": "done"}\nmarker=656359b12567095b\ndisabled_by=\n')
        self.assertEqual(out, "installed by this run; cloud-init done")


@KEY
class ReadbackTarget(TestCase):
    """commit-193: the readback checks what the run staged."""

    def setUp(self):
        self.h, self.c = world()
        ServerProfile.objects.filter(code="ci1").update(bios={"WorkloadProfile": "Virtualization-MaxPerformance"})

    def target(self, data):
        d = HostDeployment.objects.create(host=self.h, cluster=self.c, data=data)
        return hostdeploy._staged_target(hostdeploy.Run(HostDeployment.objects.get(pk=d.pk)))

    def test_the_kept_target_is_checked_as_it_is(self):
        kept = {"WorkloadProfile": "Virtualization-MaxPerformance", "EmbeddedIpxe": "Disabled", "TimeZone": "UtcP5"}
        # the profile changed since: the run is still checked against what it staged, every key of it
        self.assertEqual(self.target({"bios_target": kept, "bios_scope": hostdeploy.BIOS_SCOPE}), kept)

    def legacy(self, current):
        """A run staged by v01.01.39: its factory-wide target, no scope mark (owner 18:37: dropped, staged again)."""
        from inventory import ilo

        d = HostDeployment.objects.create(
            host=self.h, cluster=self.c, data={"bios_target": {"TpmState": "NotPresent", "PcieGlobalAspm": "Disabled"}}
        )
        r = hostdeploy.Run(HostDeployment.objects.get(pk=d.pk))
        staged = {}
        with (
            mock.patch.object(hostdeploy.Run, "c", new_callable=mock.PropertyMock),
            mock.patch.object(ilo, "bios_current", return_value=current),
            mock.patch.object(
                ilo,
                "bios_pending",
                side_effect=lambda c, t, cur: (
                    staged.update({k: v for k, v in t.items() if cur.get(k) != v})
                    or {k: v for k, v in t.items() if cur.get(k) != v}
                ),
            ),
        ):
            try:
                out = hostdeploy._staged_target(r)
            except hostdeploy.DeployError as exc:
                out = exc
        return out, staged, HostDeployment.objects.get(pk=d.pk).data

    def test_an_older_run_already_right_goes_on(self):
        now = {"WorkloadProfile": "Virtualization-MaxPerformance", "TimeZone": "UtcP5", "TpmState": "PresentEnabled"}
        out, staged, data = self.legacy(now)
        self.assertEqual(out, {"WorkloadProfile": "Virtualization-MaxPerformance", "TimeZone": "UtcP5"})
        self.assertEqual(staged, {})
        self.assertEqual(data["bios_scope"], hostdeploy.BIOS_SCOPE)  # the old target is gone
        self.assertNotIn("TpmState", data["bios_target"])

    def test_an_older_run_missing_a_setting_it_set_stops(self):
        """commit-193 (fourth review): a managed setting the old run set, not shown by the BIOS now."""
        ServerProfile.objects.filter(code="ci1").update(bios={"EmbeddedIpxe": "Disabled"})
        d = HostDeployment.objects.create(
            host=self.h, cluster=self.c, data={"bios_target": {"EmbeddedIpxe": "Disabled", "TpmState": "NotPresent"}}
        )
        r = hostdeploy.Run(HostDeployment.objects.get(pk=d.pk))
        from inventory import ilo

        with (
            mock.patch.object(hostdeploy.Run, "c", new_callable=mock.PropertyMock),
            mock.patch.object(ilo, "bios_current", return_value={"TimeZone": "UtcP5"}),
            mock.patch.object(ilo, "bios_pending") as pending,
            self.assertRaisesMessage(hostdeploy.DeployError, "EmbeddedIpxe"),
        ):
            hostdeploy._staged_target(r)
        pending.assert_not_called()
        self.assertNotIn("bios_scope", HostDeployment.objects.get(pk=d.pk).data)

    def test_an_older_run_with_something_to_change_stops(self):
        out, staged, data = self.legacy({"WorkloadProfile": "GeneralPowerEfficientCompute", "TimeZone": "UtcP5"})
        self.assertIsInstance(out, hostdeploy.DeployError)
        self.assertIn("restart the host, then resume", str(out))
        self.assertEqual(staged, {"WorkloadProfile": "Virtualization-MaxPerformance"})
        self.assertEqual(data["bios_scope"], hostdeploy.BIOS_SCOPE)

    def test_a_missing_setting_is_a_difference(self):
        from inventory import ilo

        c = mock.Mock()
        with mock.patch.object(ilo, "bios_current", return_value={"TimeZone": "UtcP5"}):
            self.assertEqual(
                ilo.bios_differences(c, {"TimeZone": "UtcP5", "EmbeddedIpxe": "Disabled"}),
                {"EmbeddedIpxe": (None, "Disabled")},
            )


@KEY
class ReadbackTarget2(TestCase):
    """commit-193 (second review): an empty kept target stays empty; a run that kept none stops."""

    def setUp(self):
        self.h, self.c = world()

    def run_with(self, data):
        d = HostDeployment.objects.create(host=self.h, cluster=self.c, data=data)
        return hostdeploy.Run(HostDeployment.objects.get(pk=d.pk))

    def test_an_empty_target_is_kept_empty(self):
        r = self.run_with({"bios_target": {}, "bios_scope": hostdeploy.BIOS_SCOPE})
        self.assertEqual(hostdeploy._staged_target(r), {})

    def test_no_target_stops(self):
        with self.assertRaisesMessage(hostdeploy.DeployError, "run it again from the BIOS settings step"):
            hostdeploy._staged_target(self.run_with({}))


class Migration0097(TestCase):
    """commit-194: the three settings leave only a profile that still holds exactly 0094's BIOS."""

    def test_only_the_seed(self):
        import importlib

        from django.apps import apps as global_apps

        m = importlib.import_module("inventory.migrations.0097_bios_workload_profile")
        seed = ServerProfile.objects.create(code="s1", bios=dict(m.SEED))
        edited = ServerProfile.objects.create(
            code="e1", bios={"WorkloadProfile": "Custom", "PcieGlobalAspm": "Disabled"}
        )
        m.forward(global_apps, None)
        seed.refresh_from_db()
        edited.refresh_from_db()
        self.assertEqual(set(seed.bios), set(m.SEED) - set(m.REMOVED))
        self.assertEqual(edited.bios, {"WorkloadProfile": "Custom", "PcieGlobalAspm": "Disabled"})


class ManagedBios(TestCase):
    """commit-195: a TimeZone or NicBoot line a profile holds from before is neither shown nor edited."""

    def test_left_out(self):
        from inventory import profiles

        p = ServerProfile.objects.create(
            code="m1", bios={"EmbeddedIpxe": "Disabled", "TimeZone": "Utc0", "Slot1NicBoot1": "NetworkBoot"}
        )
        self.assertEqual(profiles.profile_bios(p), {"EmbeddedIpxe": "Disabled"})
        self.assertEqual(profiles.initial(p)["bios"], "EmbeddedIpxe = Disabled")


class BiosAfterReset(TestCase):  # owner 2026-11-20 12:56
    def test_the_bios_step_stages_the_whole_target_after_this_runs_reset(self):
        for reset in (True, False):
            r = mock.Mock()
            r.dep.reset_factory, r.dep.data = reset, {}
            r.prof.bios = {}
            with (
                mock.patch.object(hostdeploy.ilo, "bios_current", return_value={"A": "1"}),
                mock.patch.object(hostdeploy, "_bios_target", return_value={"A": "1"}),
                mock.patch.object(hostdeploy.ilo, "bios_pending", return_value={}) as pend,
            ):
                hostdeploy.s_bios(r)
            self.assertEqual(pend.call_args.kwargs["whole"], reset)


class StorageListingUsed(TestCase):  # commit-220, core-065
    def test_the_raid_work_uses_the_checked_listing(self):
        r = mock.Mock()
        r.prof.boot = {"size_gb": 480}
        listing = [{"id": "DE009000"}]
        with (
            mock.patch.object(hostdeploy.ilo, "discover_storage", return_value=listing),
            mock.patch.object(hostdeploy.ilo, "local_storage", return_value=[]) as ls,
            mock.patch.object(hostdeploy.ilo, "power"),
        ):
            hostdeploy.s_storage(r)
        self.assertIs(ls.call_args.kwargs["ctls"], listing)


class StorageOsBoot(TestCase):  # commit-221, core-066
    def test_the_raid_work_is_never_reached(self):
        r = mock.Mock()
        r.prof.boot = {"size_gb": 480}
        with (
            mock.patch.object(hostdeploy.ilo, "discover_storage", side_effect=hostdeploy.ilo.IloError("instead")),
            mock.patch.object(hostdeploy.ilo, "local_storage") as ls,
            mock.patch.object(hostdeploy.ilo, "power"),
        ):
            with self.assertRaises(hostdeploy.ilo.IloError):
                hostdeploy.s_storage(r)
        ls.assert_not_called()


class HostAlignment(TestCase):
    """Owner 2026-11-20 15:46 "build the host alignment": a deployed host as the cluster's other hosts."""

    def _r(self, mgmt=("10.61.101.20/30",), fe_ips=("10.201.101.31",)):
        r = mock.Mock()
        r.site.code, r.site.management = "dc1", {"hosts": list(mgmt)}
        r.site.ntp_servers = ["10.201.100.201", "10.201.100.202"]
        r.nets.return_value = [
            {"kind": "core-kvm", "name": "one-core", "bridge": "one-core", "cidr": "10.201.100.0/24"},
            {"kind": "evpn", "name": "cloud-vxlan", "bridge": "vxlan", "cidr": "10.201.102.0/24"},
            {"kind": "core-lm", "name": "one-lm", "bridge": "one-lm", "cidr": "10.201.105.0/24"},
            {"kind": "nfs", "name": "data-nfs", "bridge": "data-nfs", "cidr": "10.201.111.0/24"},
        ]
        r.p.fe_vip, r.p.fe = "10.201.101.30", {"node_ips": list(fe_ips)}
        return r

    def test_the_firewall_rules_come_from_the_records(self):
        rules = hostdeploy.firewall_rules(self._r())
        self.assertEqual(
            rules,
            [
                "allow from 10.61.101.20/30",
                "allow in on one-core from 10.201.100.0/24",
                "allow in on vxlan from 10.201.102.0/24",
                "allow in on one-lm from 10.201.105.0/24",
                "allow in on one-core from 10.201.101.30 to any port 22 proto tcp",
                "allow in on one-core from 10.201.101.30 to any port 9100,9926 proto tcp",
                "allow in on one-core from 10.201.101.31 to any port 22 proto tcp",
                "allow in on one-core from 10.201.101.31 to any port 9100,9926 proto tcp",
            ],
        )
        self.assertFalse([x for x in rules if "data-nfs" in x])

    def test_no_management_hosts_no_firewall(self):
        with self.assertRaisesMessage(hostdeploy.DeployError, "lock the portal out"):
            hostdeploy.firewall_rules(self._r(mgmt=()))

    def test_the_security_script(self):
        s = hostdeploy.security_script(self._r())
        for x in (
            "/var/tmp/one/vmm/kvm/virtiofsd.sh ix,",
            "aa-status --enabled",
            "systemctl enable --now lldpd",
            "IPV6=no",
            'DEFAULT_INPUT_POLICY=\\"DROP\\"'.replace("\\", ""),
            "-A ufw-before-forward -m physdev --physdev-is-bridged -j RETURN",
            "ufw allow from 10.61.101.20/30",
            "ufw --force enable",
        ):
            self.assertIn(x, s)

    def test_the_os_baseline(self):
        for x in (
            "kdump-tools kexec-tools chrony",
            "systemctl mask",
            "fwupd-refresh.timer",
            "snapd.socket",
            "NTP=%s",
            'useradd -u 9869 -g 9869 -c "ONE Service User" -d /var/lib/one -s /bin/bash oneadmin',
        ):
            self.assertIn(x, hostdeploy.OS_BASE)
        self.assertNotIn("disable --now ufw", hostdeploy.OS_BASE)
        self.assertNotIn("disable --now apparmor", hostdeploy.OS_BASE)
        r = self._r()
        hostdeploy.s_os_base(r)
        self.assertTrue(r.ssh.call_args.args[0].startswith("CLAUGINE_NTP='10.201.100.201 10.201.100.202'\n"))
        r.site.ntp_servers = []
        with self.assertRaisesMessage(hostdeploy.DeployError, "no NTP servers"):
            hostdeploy.s_os_base(r)

    def test_oneadmin_comes_before_the_packages_and_lldpd_ufw_apparmor_are_installed(self):
        keys = hostdeploy.STEP_KEYS
        self.assertLess(keys.index("os_base"), keys.index("packages"))
        self.assertNotIn("keys", keys)  # T36 B4b: oneadmin's keys come with Connect
        self.assertLess(hostdeploy.CONNECT_KEYS.index("keys"), hostdeploy.CONNECT_KEYS.index("one_add"))
        self.assertLess(keys.index("security"), keys.index("reboot"))
        for p in ("lldpd", "ufw", "apparmor"):
            self.assertIn(p, hostdeploy.PACKAGES)


class DatastoresRights(TestCase):  # owner 2026-11-20 15:58
    def test_the_datastores_dir_is_oneadmins(self):
        s = hostdeploy.nfs_script([(100, "10.0.0.1:/vms", "nfs4", "rw")])
        self.assertIn("install -d -m 750 -o oneadmin -g oneadmin /var/lib/one/datastores\n", s)
        self.assertIn("install -d -m 750 -o oneadmin -g oneadmin /var/lib/one/datastores/100\n", s)  # made
        after = s.split("mount /var/lib/one/datastores/100")[1]  # and after the first mount
        self.assertIn("chown oneadmin:oneadmin /var/lib/one/datastores/100", after)
        self.assertIn("sudo -u oneadmin chmod 750 /var/lib/one/datastores/100", after)
        self.assertIn('install -d -m 750 -o oneadmin -g oneadmin "$mnt"', hostdeploy.LOCAL_FS)
        self.assertIn('chown oneadmin:oneadmin "$mnt"; chmod 750 "$mnt"', hostdeploy.LOCAL_FS)


class ReservedCpuAndHealth(TestCase):  # owner 2026-11-20 09:41 and 15:46
    def test_reserved_cpu_from_the_ratio(self):
        self.assertEqual(hostdeploy.reserved_cpu(12800, 10), -115200)
        self.assertEqual(hostdeploy.reserved_cpu(12800, 1), 0)
        self.assertEqual(hostdeploy.reserved_cpu(6400, None), 0)

    def test_one_wait_sets_it_and_reads_it_back(self):
        import xml.etree.ElementTree as ET

        r = mock.Mock()
        r.dep.data = {"one_id": 13}
        r.cluster.vcpu_ratio = 10
        one = mock.Mock()
        one.xml.side_effect = [
            ET.fromstring(
                "<HOST><STATE>4</STATE><HOST_SHARE><TOTAL_CPU>12800</TOTAL_CPU><TOTAL_MEM>2</TOTAL_MEM></HOST_SHARE></HOST>"
            ),
            ET.fromstring("<HOST><TEMPLATE><RESERVED_CPU>-115200</RESERVED_CPU></TEMPLATE></HOST>"),
        ]
        with (
            mock.patch.object(hostdeploy, "_one", return_value=one),
            mock.patch.object(hostdeploy, "not_ours", return_value=""),  # commit-539: proven the run's own
        ):
            out = hostdeploy.s_one_wait(r)
        one.call.assert_called_once_with("one.host.update", 13, 'RESERVED_CPU="-115200"', 1)
        self.assertIn("RESERVED_CPU -115200 (10:1)", out)

    def test_the_health_is_read_at_the_end(self):
        r = mock.Mock()
        r.h.name = "h1"
        with mock.patch("inventory.hosthealth.check", return_value={"h1": True}) as check:
            self.assertEqual(hostdeploy.refresh_health(r), "OK")
        check.assert_called_once_with([r.h])
        with mock.patch("inventory.hosthealth.check", side_effect=RuntimeError("no answer")):
            self.assertEqual(hostdeploy.refresh_health(r), "not read")


@KEY
class BatchA(TestCase):  # commit-233, commit-234
    def test_a_mounted_datastore_is_not_prepared_again(self):
        s = hostdeploy.nfs_script([(100, "10.0.0.1:/vms", "nfs4", "rw")])
        self.assertIn(
            "mountpoint -q /var/lib/one/datastores/100 || install -d -m 750 -o oneadmin -g oneadmin "
            "/var/lib/one/datastores/100",
            s,
        )

    def test_no_health_written_on_a_retired_host(self):
        from inventory import hosthealth

        h, _c = world()
        h.state = "retired"
        h.save()
        hosthealth.store(h, {"ok": True})
        h.refresh_from_db()
        self.assertEqual((h.health, h.health_at), ({}, None))
        h.state = "deployed"
        h.save()
        hosthealth.store(h, {"ok": True})
        h.refresh_from_db()
        self.assertEqual(h.health, {"ok": True})


class BaselineChecked(TestCase):  # commit-231
    def test_the_purge_and_the_boot_config_are_checked(self):
        b = hostdeploy.OS_BASE
        self.assertNotIn("chrony >/dev/null 2>&1 || true", b)
        self.assertIn("is still installed", b)
        self.assertIn("crashkernel is still in the boot configuration", b)
        self.assertIn("crashkernel is still on the kernel command line", hostdeploy.NO_CRASHKERNEL)
        self.assertIn("NO_CRASHKERNEL", inspect.getsource(hostdeploy.s_reboot))


class FirewallSafety(TestCase):  # commit-229
    def test_checked_in_b0_and_rolled_back_unless_a_new_ssh_works(self):
        self.assertIn("firewall_rules(r)", inspect.getsource(hostdeploy.s_checks))
        r = HostAlignment._r(HostAlignment())
        s = hostdeploy.security_script(r)
        self.assertLess(s.index("systemd-run --quiet --unit=claugine-ufw-rollback"), s.index("ufw --force enable"))
        r.ssh.side_effect = ["Status: active\nok\n", RuntimeError("timed out")]
        with self.assertRaisesMessage(hostdeploy.DeployError, "ufw goes off again in 3 minutes"):
            hostdeploy.s_security(r)
        self.assertEqual(r.ssh.call_count, 2)  # the second, new connection is what cancels the way back


@KEY
class BaselineOnResume(TestCase):  # commit-230
    def test_an_older_runs_baseline_is_done_again(self):
        h, c = world()
        old = {k: {"status": "done"} for k in ("checks", "os_base", "packages", "upgrade")}
        dep = HostDeployment.objects.create(host=h, cluster=c, steps=dict(old), data={})
        r = hostdeploy.Run(dep)
        hostdeploy.redo_baseline(r)
        dep.refresh_from_db()
        self.assertEqual([dep.steps[k]["status"] for k in ("os_base", "packages", "upgrade")], ["", "", "done"])
        self.assertEqual(dep.data["baseline"], hostdeploy.BASELINE)
        dep.steps["os_base"]["status"] = dep.steps["packages"]["status"] = "done"
        dep.save()
        hostdeploy.redo_baseline(hostdeploy.Run(dep))  # this version's: kept
        dep.refresh_from_db()
        self.assertEqual(dep.steps["os_base"]["status"], "done")


class Round2(TestCase):  # owner 2026-11-20 16:34 (Q7), 16:35 (commit-229, 231, 232, 236, 237)
    def test_umask(self):
        self.assertIn("chfn -o umask=0027 oneadmin", hostdeploy.OS_BASE)
        r = mock.Mock()
        r.h.name = "h1"
        r.ssh.side_effect = ["ok", "0022\n"]
        with mock.patch.object(hostdeploy, "logins", return_value=("u", "k", "pub", "opub", "okey")):
            with self.assertRaisesMessage(hostdeploy.DeployError, "umask 0022, not 0027"):
                hostdeploy.s_keys(r)
            r.ssh.side_effect = ["ok", "0027\n"]
            self.assertIn("umask 0027", hostdeploy.s_keys(r))

    def test_grub_always_regenerated_and_checked(self):
        self.assertIn("update-grub >/dev/null 2>&1  # commit-231", hostdeploy.OS_BASE)
        self.assertIn("crashkernel is still in /boot/grub/grub.cfg", hostdeploy.OS_BASE)
        self.assertGreaterEqual(inspect.getsource(hostdeploy.s_reboot).count("NO_CRASHKERNEL"), 2)

    def test_the_way_back_must_not_have_run(self):
        r = HostAlignment._r(HostAlignment())
        r.ssh.side_effect = ["Status: active\nok\n", "Status: inactive\n"]
        with self.assertRaisesMessage(hostdeploy.DeployError, "ufw is not active - the way back has run"):
            hostdeploy.s_security(r)
        self.assertEqual(r.ssh.call_args_list[1].args[0], hostdeploy.CANCEL_WAY_BACK)

    def test_reboot_redone_with_the_baseline(self):
        self.assertIn("reboot", hostdeploy.BASELINE_STEPS)

    def test_lldp_on_resume(self):
        from inventory import ilo

        c = mock.Mock()
        with mock.patch.object(ilo, "lldp_ports", side_effect=[{"/p": False}, {"/p": True}]):
            self.assertEqual(ilo.ensure_lldp(c), "")
        c.patch.assert_called_once_with("/p", {"Ethernet": {"LLDPEnabled": True}})
        with mock.patch.object(ilo, "lldp_ports", return_value=None):
            self.assertEqual(ilo.ensure_lldp(c), "LLDP not readable")
        self.assertIn("ilo.ensure_lldp", inspect.getsource(hostdeploy.run))
        self.assertIn("firewall_rules(r)", inspect.getsource(hostdeploy.run))


class OneadminHome(TestCase):  # owner 2026-11-20 20:28 (supersedes 16:48)
    def test_the_user_with_its_ids_and_home_and_no_directory_made(self):
        b = hostdeploy.OS_BASE
        self.assertIn("useradd -u 9869 -g 9869", b)
        self.assertIn("-d /var/lib/one", b)
        self.assertNotIn("install -d -m 750 -o oneadmin -g oneadmin /var/lib/one\n", b)
        self.assertNotIn("9869:9869:750", b)
        self.assertNotIn(" -m ", b.split("useradd", 1)[1].split("\n", 1)[0])  # useradd makes no home either


class Round3(TestCase):  # owner 2026-11-20 16:56
    def test_baseline_and_rollback(self):
        self.assertEqual(hostdeploy.BASELINE, 4)
        self.assertNotIn("keys", hostdeploy.BASELINE_STEPS)  # T36 B4b: keys is Connect's
        r = HostAlignment._r(HostAlignment())
        r.ssh.side_effect = ["Status: active\nok\n", "Status: active\n"]
        hostdeploy.s_security(r)
        self.assertEqual(r.ssh.call_args_list[1].args[0], hostdeploy.CANCEL_WAY_BACK)


@KEY
class Round3Resume(TestCase):  # commit-238, commit-239
    def test_keys_done_at_baseline_2_runs_again(self):
        h, c = world()
        steps = {k: {"status": "done"} for k in ("checks", "os_base", "packages", "keys", "reboot", "ilo")}
        dep = HostDeployment.objects.create(host=h, cluster=c, steps=steps, data={"baseline": 2})
        hostdeploy.redo_baseline(hostdeploy.Run(dep))
        dep.refresh_from_db()
        self.assertEqual([dep.steps[k]["status"] for k in ("packages", "reboot", "ilo")], ["", "", "done"])
        self.assertEqual(dep.data["baseline"], hostdeploy.BASELINE)

    def test_an_unreadable_ilo_on_resume_fails_the_run(self):
        from inventory.redfish import RedfishError

        h, c = world()
        steps = {k: {"status": "done"} for k in hostdeploy.STEP_KEYS[: hostdeploy.STEP_KEYS.index("ilo") + 1]}
        dep = HostDeployment.objects.create(
            host=h,
            cluster=c,
            steps=steps,
            state="running",
            data={"baseline": hostdeploy.BASELINE, "installer": {"file": "u.iso", "sha256": "a" * 64}},
        )
        with (
            mock.patch.object(hostdeploy, "firewall_rules", return_value=[]),
            mock.patch("accounts.hostmedia.on_site_node", return_value=(True, "")),
            mock.patch.object(hostdeploy.Run, "c", new_callable=mock.PropertyMock),
            mock.patch.object(hostdeploy.ilo, "ensure_lldp", side_effect=RedfishError("GET /x: HTTP 500")),
        ):
            self.assertFalse(hostdeploy.run(dep))
        dep.refresh_from_db()
        self.assertEqual(dep.state, "failed")
        self.assertIn("resume refused: iLO LLDP not set: GET /x: HTTP 500", dep.log)


class CrashkernelReads(SimpleTestCase):  # commit-240: grep's status, a read error is never "clean"
    def _run(self, script, path, content):
        import os
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "f")
            if content is not None:
                with open(f, "w") as fh:
                    fh.write(content)
            p = subprocess.run(["bash", "-c", script.replace(path, f)], capture_output=True, text=True)
            return p.returncode, p.stderr

    def test_cmdline(self):
        s, path = hostdeploy.NO_CRASHKERNEL, "/proc/cmdline"
        self.assertEqual(self._run(s, path, "ro quiet"), (0, ""))
        rc, err = self._run(s, path, "ro crashkernel=512M")
        self.assertEqual(rc, 1)
        self.assertIn("still on the kernel command line", err)
        rc, err = self._run(s, path, None)
        self.assertEqual(rc, 1)
        self.assertIn("is not readable", err)

    def test_grub_cfg(self):
        b = hostdeploy.OS_BASE
        i = b.index("rc=0; grep -q crashkernel /boot/grub/grub.cfg")
        s, path = "set -e\n" + "\n".join(b[i:].splitlines()[:2]) + "\n", "/boot/grub/grub.cfg"
        self.assertEqual(self._run(s, path, "linux /vmlinuz ro")[0], 0)
        rc, err = self._run(s, path, "linux /vmlinuz crashkernel=1G")
        self.assertEqual(rc, 1)
        self.assertIn("still in", err)
        rc, err = self._run(s, path, None)
        self.assertEqual(rc, 1)
        self.assertIn("is not readable", err)


class PackagesOnlyAfterOneadmin(SimpleTestCase):  # owner 2026-11-20 19:34
    def _script(self):
        r = mock.Mock()
        r.ssh.return_value = "ok"
        r.fns = ["kvm", "frr"]  # T36 B4a: the host service's functions
        hostdeploy.s_packages(r)
        return r.ssh.call_args.args[0]

    def _run(self, uid, gid, home):
        import subprocess

        stubs = (
            f'id() {{ case "$1" in -u) echo {uid};; -g) echo {gid};; *) echo uid={uid};; esac; }}\n'
            f"getent() {{ echo oneadmin:x:{uid}:{gid}::{home}:/bin/bash; }}\n"
            "apt-get() { echo INSTALLED >&2; }\nusermod() { :; }\nsystemctl() { :; }\n"
        )
        p = subprocess.run(["bash", "-c", stubs + self._script()], capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr

    def test_checked_before_the_install(self):
        script = self._script()
        self.assertLess(script.index("9869:9869"), script.index("apt-get install"))
        rc, out, err = self._run(9869, 9869, "/var/lib/one")
        self.assertEqual((rc, out), (0, "ok\n"))
        self.assertIn("INSTALLED", err)
        rc, out, err = self._run(1001, 1001, "/var/lib/one")
        self.assertEqual(rc, 1)
        self.assertNotIn("INSTALLED", err)
        self.assertIn("nothing installed", err)
        rc, out, err = self._run(9869, 9869, "/home/oneadmin")
        self.assertEqual(rc, 1)
        self.assertNotIn("INSTALLED", err)
        self.assertIn("the home of oneadmin is not /var/lib/one", err)


class CancelWayBack(SimpleTestCase):  # 014, 2026-11-20 20:25; 21cfb7a-commit.md (commit-236)
    def _run(
        self,
        timer_active=True,
        load="not-found",
        state="inactive",
        show_fails=False,
        tload=None,
        tstate="inactive",
        tshow_fails=False,
    ):
        import subprocess

        u = "claugine-ufw-rollback"
        tload = tload if tload is not None else ("loaded" if timer_active else "not-found")
        stubs = (
            "systemctl() {\n"
            '  case "$*" in\n'
            f"    'show -p LoadState --value {u}.timer') {'return 1' if tshow_fails else 'echo ' + tload};;\n"
            f"    'show -p ActiveState --value {u}.timer') echo {tstate};;\n"
            f"    'stop {u}.timer') echo stopped-timer >&2;;\n"
            f"    'show -p LoadState --value {u}.service') {'return 1' if show_fails else 'echo ' + load};;\n"
            f"    'show -p ActiveState --value {u}.service') echo {state};;\n"
            f"    'stop {u}.service') echo stopped-service >&2;;\n"
            '    *) echo "unexpected: $*" >&2; return 99;;\n'
            "  esac\n"
            "}\n"
            "ufw() { echo 'Status: active'; }\n"
        )
        p = subprocess.run(["bash", "-c", stubs + hostdeploy.CANCEL_WAY_BACK], capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr

    def test_the_service_not_loaded_yet(self):
        rc, out, err = self._run()
        self.assertEqual((rc, out), (0, "Status: active\n"), err)
        self.assertIn("stopped-timer", err)
        self.assertNotIn("stopped-service", err)

    def test_run_again_after_the_timer_was_stopped(self):  # commit-254: a retry is safe; ufw tells the rest
        rc, out, err = self._run(timer_active=False)
        self.assertEqual((rc, out), (0, "Status: active\n"), err)
        self.assertNotIn("stopped-timer", err)

    def test_the_service_loaded_is_stopped(self):
        rc, _out, err = self._run(load="loaded")
        self.assertEqual(rc, 0, err)
        self.assertIn("stopped-service", err)

    def test_anything_unclear_stops_the_step(self):
        for kw, why in (
            ({"show_fails": True}, "the way back cannot be read"),
            ({"load": ""}, "the way back is \n"),
            ({"load": "masked"}, "the way back is masked"),
            ({"load": "loaded", "state": "activating"}, "the way back is activating"),
            ({"load": "loaded", "state": "active"}, "the way back is active"),
            ({"tshow_fails": True}, "the way back timer cannot be read"),  # commit-255
            ({"tload": ""}, "the way back timer is \n"),
            ({"tload": "masked"}, "the way back timer is masked"),
            ({"tstate": "active"}, "the way back timer is active"),
        ):
            rc, out, err = self._run(**kw)
            self.assertEqual(rc, 1, (kw, err))
            self.assertIn(why, err)
            self.assertNotIn("Status", out)


class WayBackFailed(TestCase):  # commit-254
    def _security(self, exc):
        r = HostAlignment._r(HostAlignment())
        r.ssh.side_effect = ["Status: active\nok\n", exc]
        with self.assertRaises(hostdeploy.DeployError) as cm:
            hostdeploy.s_security(r)
        return str(cm.exception)

    def _err(self, text, connect):
        from inventory.sshexec import SshError

        e = SshError(text)
        if connect is not None:
            e.connect = connect
        return e

    def test_every_failure_unknown_never_a_promise(self):
        for connect in (True, False, None):
            msg = self._security(self._err("10.0.0.1:22: x", connect))
            self.assertIn("it is not known whether the timer was stopped", msg)
            self.assertIn("only if it was not", msg)
            self.assertIn("check claugine-ufw-rollback and `ufw status` on the host before resuming", msg)

    def test_a_lost_direct_session_then_a_jump_host_that_does_not_connect(self):
        from inventory import sshexec

        def once(host, port, user, key, remote, jump, *a, **k):
            # direct: the session ran (the timer may be stopped) and dropped - 255; via the jump host: no connection
            e = sshexec.SshError(f"{host}:{port}: " + ("connection reset" if jump is None else "no route to host"))
            e.detail, e.connect = str(e), True
            raise e

        jump = {"host": "192.0.2.1", "port": 22, "user": "j", "key": "K"}
        with (
            mock.patch.object(sshexec, "_once", side_effect=once),
            mock.patch.object(sshexec, "jump_for", return_value=jump),
            self.assertRaises(sshexec.SshError) as ctx,
        ):
            sshexec.run("192.0.2.5", 22, "sa", "KEY", "sudo -n bash -s", site="zk1")
        msg = hostdeploy.way_back_failed(ctx.exception)
        self.assertIn("it is not known whether the timer was stopped", msg)
        self.assertNotIn("if it never got in", msg)

    def test_through_the_jump_host_its_own_end_counts(self):
        from inventory import sshexec

        def once(host, port, user, key, remote, jump, *a, **k):
            e = sshexec.SshError(
                f"{host}:{port}: " + ("no route to host" if jump is None else "the way back is active")
            )
            e.detail, e.connect = str(e), jump is None  # direct: no connection; via the jump host: the script failed
            raise e

        jump = {"host": "192.0.2.1", "port": 22, "user": "j", "key": "K"}
        with (
            mock.patch.object(sshexec, "_once", side_effect=once),
            mock.patch.object(sshexec, "jump_for", return_value=jump),
            self.assertRaises(sshexec.SshError) as ctx,
        ):
            sshexec.run("192.0.2.5", 22, "sa", "KEY", "sudo -n bash -s", site="zk1")
        self.assertIs(ctx.exception.connect, False)
        self.assertIn("it is not known whether the timer was stopped", hostdeploy.way_back_failed(ctx.exception))


class KeyComments(SimpleTestCase):  # owner 2026-11-20 21:29 "you lost comments of keys - please keep it"
    def _key(self, kind, comment, passphrase=""):
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            f = f"{d}/k"
            subprocess.run(["ssh-keygen", "-q", "-t", kind, "-N", passphrase, "-C", comment, "-f", f], check=True)
            return open(f).read(), open(f + ".pub").read().strip()

    def test_the_comment_read_from_the_private_key(self):
        from inventory.credentials import _public_key, key_comment, with_comment

        for kind in ("ed25519", "rsa", "ecdsa"):
            priv, pub = self._key(kind, "Freedom Cloud SA")
            self.assertEqual(key_comment(priv), "Freedom Cloud SA", kind)
            bare = _public_key(priv)
            self.assertEqual(len(bare.split()), 2)  # what cryptography gives: no comment
            self.assertEqual(with_comment(bare, priv), pub, kind)
        priv, _pub = self._key("ed25519", "x", passphrase="secret")
        self.assertEqual(key_comment(priv), "")  # encrypted: not read
        self.assertEqual(key_comment("not a key"), "")
        self.assertEqual(with_comment("ssh-ed25519 AAAA own", priv), "ssh-ed25519 AAAA own")  # its own kept

    def test_the_keys_step_writes_them_in_the_reference_order(self):
        from inventory.credentials import _public_key

        sa_priv, sa_pub = self._key("ed25519", "Freedom Cloud SA")
        one_priv, one_pub = self._key("ed25519", "one-platform")
        r = mock.Mock()
        r.h.name = "h1"
        r.ssh.side_effect = ["ok", "0027\n"]
        logins = ("sa", sa_priv, _public_key(sa_priv), _public_key(one_priv), one_priv)
        with mock.patch.object(hostdeploy, "logins", return_value=logins):
            hostdeploy.s_keys(r)
        script = r.ssh.call_args_list[0].args[0]
        body = script.split("<<'CLAUGINE_K'\n", 1)[1].split("CLAUGINE_K\n", 1)[0]
        self.assertEqual(body, f"{one_pub}\n{sa_pub}\n")
