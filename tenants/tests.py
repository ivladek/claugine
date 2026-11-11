"""The tenants app: ordering, approval, provisioning, the project page, and the records behind them.

The other `tests_*.py` files cover one subject each (see `worklog/internals.md`); this one holds the rest.
"""
import os
import shutil
import tempfile
from inventory.testing import secrets_loaded
from unittest import mock

from cryptography.fernet import Fernet
from django.test import TestCase, override_settings
from django.utils import timezone

from tenants import one_acl


POOLS = {
    "one.zonepool.info": "<ZONE_POOL><ZONE><ID>0</ID><NAME>one_dc1</NAME><TEMPLATE><ENDPOINT>http://10.0.0.30:2633/RPC2</ENDPOINT></TEMPLATE></ZONE></ZONE_POOL>",
    "one.clusterpool.info": "<CLUSTER_POOL><CLUSTER><ID>0</ID><NAME>default</NAME></CLUSTER><CLUSTER><ID>5</ID><NAME>shared-std-1_dc1</NAME></CLUSTER></CLUSTER_POOL>",
    "one.datastorepool.info": ("<DATASTORE_POOL>"
                               "<DATASTORE><ID>100</ID><NAME>vms</NAME><TYPE>1</TYPE><CLUSTERS><ID>5</ID></CLUSTERS></DATASTORE>"
                               "<DATASTORE><ID>101</ID><NAME>images</NAME><TYPE>0</TYPE><CLUSTERS><ID>5</ID></CLUSTERS></DATASTORE>"
                               "<DATASTORE><ID>102</ID><NAME>files</NAME><TYPE>2</TYPE><CLUSTERS><ID>5</ID></CLUSTERS></DATASTORE>"
                               "<DATASTORE><ID>108</ID><NAME>backups</NAME><TYPE>3</TYPE><CLUSTERS><ID>5</ID></CLUSTERS></DATASTORE>"
                               "</DATASTORE_POOL>"),
    "one.vntemplatepool.info": "<VNTEMPLATE_POOL><VNTEMPLATE><ID>0</ID><NAME>overlays_dc1</NAME></VNTEMPLATE></VNTEMPLATE_POOL>",
    "one.grouppool.info": "<GROUP_POOL><GROUP><ID>217</ID><NAME>shared</NAME></GROUP></GROUP_POOL>",
    "one.userpool.info": "<USER_POOL></USER_POOL>",
    "one.imagepool.info": "<IMAGE_POOL><IMAGE><ID>110</ID><NAME>VyOS Router 2026.03</NAME></IMAGE></IMAGE_POOL>",
    "one.vnpool.info": ("<VNET_POOL><VNET><ID>0</ID><NAME>cloud-ix_dc1</NAME></VNET>"
                        "<VNET><ID>3</ID><NAME>cloud-vyos_dc1</NAME></VNET></VNET_POOL>"),
    "one.vmpool.info": "<VM_POOL></VM_POOL>",
    "one.acl.info": "<ACL_POOL></ACL_POOL>",
    "one.vn.info": ("<VNET><ID>0</ID><AR_POOL><AR><TYPE>IP4</TYPE><IP>10.0.10.10</IP><SIZE>20</SIZE>"
                    "<LEASES><LEASE><IP>10.0.10.10</IP></LEASE></LEASES></AR></AR_POOL></VNET>"),
    "one.group.info": "<GROUP><ID>300</ID><ADMINS></ADMINS></GROUP>",
}



FULL_SITE = {"country": "KZ", "city": "Almaty", "dc": "1", "timezone": "Asia/Almaty", "ntp_servers": "10.0.0.1",
             "dns_servers": "10.0.0.2", "bootstrap_dns": "10.0.0.3", "mgmt_hosts": "10.0.0.21",
             "ssh_ports": "VyOS VMs | 55522\nCore services | 55523",
             "log_collectors": "IPMI | 10.0.0.18 | udp | 15141\nVyOS VMs | 10.0.0.18 | tcp | 15142\nCore services | 10.0.0.18 | tcp | 15140"}
# every site setting is required (2026-10-27)

class FakeClient:
    def __init__(self):
        self.calls = []
        self.ids = {"one.group.allocate": 300, "one.user.allocate": 400, "one.vn.allocate": 500, "one.vm.allocate": 600}

    def _call(self, method, *args):
        self.calls.append((method, args))
        if method in POOLS:
            return POOLS[method]
        if method in self.ids:
            self.ids[method] += 1
            return self.ids[method] - 1
        return 0


class AclEncoding(TestCase):
    def test_encode(self):
        self.assertEqual(one_acl.encode("@105 VM+NET/@105 USE+MANAGE #0"),
                         ("200000069", "5200000069", "3", "100000000"))
        self.assertEqual(one_acl.encode("#181 USER/* CREATE *"), ("1000000b5", "10400000000", "8", "400000000"))


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode())
class DeploymentBase(TestCase):
    def setUp(self):
        from accounts.tests import make_inventory, valid_bin
        from inventory.models import Cluster, Platform, PlatformControl
        from tenants import ordering
        from tenants.models import Organization, PrefixAssignment, Tenant
        make_inventory()
        Platform.objects.filter(key="dc1.cloud").update(fe_vip="10.0.0.30", fe_secrets="secrets.acme.cloud.one")
        Cluster.objects.filter(quota_code="ca1").update(name="shared-std-1_dc1")
        PlatformControl.objects.create(key="dc1.cloud", mode="execution")
        Platform.objects.filter(key="dc1.cloud").update(sa="secrets.acme.cloud.admins.sa", shared_owner="shared")
        from inventory.networks import import_site
        import_site("dc1", {"cloud-services": {"interface": "bond0", "vlan": 103, "subnet": "198.51.100.64", "prefix": 29,
                                               "dns": ["203.0.113.2", "203.0.113.3"]},
                            "cloud-vyos": {"interface": "bond0", "vlan": 20, "subnet": "10.0.20.0", "prefix": 24,
                                           "gw": "10.0.20.1", "ranges": {"cloud": {"start": "10.0.20.10", "len": 20}}}})
        from inventory.models import Site
        Site.objects.filter(code="dc1").update(management={"hosts": ["10.0.0.21", "10.0.0.22/31"], "ports": {"vyos": 55522, "ssh": 55523}}, timezone="almaty")
        Platform.objects.filter(key="dc1.cloud").update(segments=["cloud-ix", "corpnet-ix", "cloud-services"])
        self.secrets = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.secrets, "cloud"))
        with open(os.path.join(self.secrets, "cloud", "one.yaml"), "w") as f:
            f.write("oneadmin: pw-oneadmin-77\n")
        with open(os.path.join(self.secrets, "cloud", "admins.yaml"), "w") as f:
            f.write("sa:\n  opsuser:\n    uid: 1000\n    password: sapw\n    key: ssh-ed25519 AAAAsa opsuser\n")
        org = Organization.objects.create(name="Romashka", tax_id=valid_bin(), short_name="romashka")
        self.t = Tenant.objects.create(
            organization=org, name="gpulab", title="gpulab", kind="cloud", approval="approved", group_name="_t_romashka_gpulab",
            igw_username="gpulab-igw-admin", igw_ssh_key="ssh-ed25519 AAAAuser me@host",
            order={"platform": "dc1.cloud", "site": "dc1", "cluster": "ca1", "cpu": 4, "ram_gb": 16, "gpu": 0, "disk_gb": 100,
                   "files_gb": 1, "backups_gb": 0, "connections": ["inet"], "inet_mbps": 100, "inet_main_prefix": 31})
        self.t.set_admin_password("Kx7-Pass+2026")
        self.t.save()
        PrefixAssignment.objects.create(tenant=self.t, site="dc1", kind="inet_main", cidr="203.0.113.16/31")
        ordering.materialize(self.t, None)

    def tearDown(self):
        shutil.rmtree(self.secrets, ignore_errors=True)

    def _run(self, dry):
        from tenants import deploy
        fake = FakeClient()
        with secrets_loaded(self.secrets), \
                mock.patch("adoption.collector.client_for", return_value=fake), \
                mock.patch("tenants.deploy.crypt_sha512", return_value="$6$x$y"):
            dep = deploy.deploy(self.t, None, dry=dry)
        return dep, fake


class ProvisionFlow(DeploymentBase):
    """Correction 13: an approved tenant is made only when somebody presses Provision; the dialog sets the admin, the
    igw and who gets the credentials. corrections-18: provisioned by itself after the approvals (the dialog stays for a
    provisioning that could not start or failed)."""

    def test_automatic_provisioning_with_defaults(self):
        from django.core import mail
        from accounts.models import Kind, User
        from tenants.models import PrefixAssignment, Tenant
        from tenants import ordering
        from tenants.provision import auto
        t = Tenant.objects.create(
            organization=self.t.organization, name="auto-lab", title="auto-lab", kind="cloud", approval="approved",
            group_name="_romashka_autolab", order={**self.t.order, "inet_main_prefix": 31})
        PrefixAssignment.objects.create(tenant=t, site="dc1", kind="inet_main", cidr="203.0.113.22/31")
        ordering.materialize(t, None)
        adm = User.objects.create_user("boss@romashka.kz", "Str0ng-PASS-123", kind=Kind.CUSTOMER,
                                       organization=t.organization, is_org_admin=True, email_verified=True)
        fake = FakeClient()
        mail.outbox.clear()
        with secrets_loaded(self.secrets), override_settings(DEPLOY_ASYNC=False), \
                mock.patch("adoption.collector.client_for", return_value=fake), \
                mock.patch("tenants.deploy.crypt_sha512", return_value="$6$x$y"):
            dep, why = auto(t, None)
        self.assertEqual(why, "")
        self.assertEqual(dep.state, "done", (dep.error, dep.steps))
        t.refresh_from_db()
        self.assertEqual((t.one_admin_user, t.tenant_admin), ("romashka-autolab-adm", adm))
        self.assertTrue(t.get_admin_password() and t.get_igw_password())
        self.assertIn("boss@romashka.kz", [x for m in mail.outbox if "access details" in m.subject for x in m.to])
        self.assertEqual(auto(t, None), (None, "nothing to provision"))

    def test_provision_dialog_deploys_and_sends_the_credentials(self):
        from django.core import mail
        from django.urls import reverse
        from accounts.models import Kind, User
        from tenants.models import Tenant
        t = Tenant.objects.create(
            organization=self.t.organization, name="ml-lab", title="ml-lab", kind="cloud", approval="approved",
            group_name="_romashka_mllab",
            order={**self.t.order, "inet_main_prefix": 31})
        from tenants.models import PrefixAssignment
        from tenants import ordering
        PrefixAssignment.objects.create(tenant=t, site="dc1", kind="inet_main", cidr="203.0.113.20/31")
        ordering.materialize(t, None)
        org = t.organization
        org.default_ssh_key = "ssh-ed25519 AAAAorgkey ops@org"
        org.save()
        adm = User.objects.create_user("adm@romashka.kz", "Str0ng-PASS-123", kind=Kind.CUSTOMER, organization=org,
                                       is_org_admin=True, email_verified=True)
        new = User.objects.create_user("new@romashka.kz", "Str0ng-PASS-123", kind=Kind.CUSTOMER, organization=org,
                                       email_verified=True, login_enabled=False)  # not activated: gets them too
        self.client.force_login(adm)
        r = self.client.get(reverse("tenant_detail", args=[t.pk]))
        self.assertContains(r, 'data-open="dlg-t-provision"')
        self.assertContains(r, 'value="romashka-mllab-adm"')  # owner 2026-10-31: <org>-<project>-adm, not chosen
        self.assertEqual(t.one_admin_name, "romashka-mllab-adm")
        self.assertFalse(t.deployments.exists())  # nothing made before Provision
        Tenant.objects.filter(pk=self.t.pk).update(one_admin_user="romashka-mllab-adm")
        data = {"pv-admin_user": "ops", "pv-admin_password": "Kx7-Pass+2026", "pv-igw_type": "vyos",
                "pv-igw_username": "netadmin", "pv-igw_password": "Qz8-Gate+2026", "pv-igw_key": "default",
                "pv-tenant_admin": str(new.pk)}
        self.client.post(reverse("tenant_provision", args=[t.pk]), data)
        t.refresh_from_db()
        self.assertFalse(t.deployments.exists())  # romashka-mllab-adm is another project's admin
        Tenant.objects.filter(pk=self.t.pk).update(one_admin_user="romashka-gpulab-admin")
        data["pv-admin_user"] = "ignored"  # the name follows the rule
        fake = FakeClient()
        with secrets_loaded(self.secrets), override_settings(DEPLOY_ASYNC=False), \
                mock.patch("adoption.collector.client_for", return_value=fake), \
                mock.patch("tenants.deploy.crypt_sha512", return_value="$6$x$y"):
            self.client.post(reverse("tenant_provision", args=[t.pk]), data)
        t.refresh_from_db()
        dep = t.deployments.get()
        self.assertEqual(dep.state, "done", (dep.error, dep.steps))
        self.assertEqual((t.one_admin_user, t.igw_username, t.get_igw_password()), ("romashka-mllab-adm", "netadmin", "Qz8-Gate+2026"))
        self.assertEqual((t.igw_ssh_key, list(t.ssh_keys.values_list("on_igw", flat=True))),
                         ("ssh-ed25519 AAAAorgkey ops@org", [True]))
        self.assertIn(("one.user.allocate", "romashka-mllab-adm"), [(m, a[0]) for m, a in fake.calls if m == "one.user.allocate"])
        creds = [m for m in mail.outbox if "access details" in m.subject]  # correction 15: to the project admin
        # owner 2026-11-03: credentials go to the project admin only, not to the organization's admin
        self.assertEqual(sorted(x for m in creds for x in m.to), ["new@romashka.kz"])
        self.assertIn("Kx7-Pass+2026", creds[0].body)
        self.assertIn("romashka-mllab-adm", creds[0].body)
        self.assertIn("ssh netadmin@203.0.113.21", creds[0].body)
        self.assertEqual(t.tenant_admin, new)
        self.assertEqual(self.client.post(reverse("tenant_provision", args=[t.pk]), data).status_code, 403)  # once


class ProvisionRetry(DeploymentBase):
    """A provisioning that failed half way (kd-solution: egw made, igw not) can be run again, even after the
    tenants refresh linked its group; the existing admin gets the newly chosen password."""

    def test_failed_provisioning_can_be_run_again(self):
        from django.urls import reverse
        from accounts.models import Kind, User
        from tenants.models import Deployment
        from tenants.provision import can_provision, failed_provisioning, provisioned
        t = self.t
        t.one_group_id, t.one_admin_user, t.one_admin_id = 520, "romashka-gpulab", 247
        t.save()
        self.assertTrue(provisioned(t))  # an imported tenant: a group, no portal provisioning
        Deployment.objects.create(tenant=t, platform_key="dc1.cloud", state="failed", error="igw: no user",
                                  steps=[{"name": "egw", "status": "ok"}, {"name": "igw", "status": "failed"}])
        self.assertFalse(provisioned(t))
        self.assertEqual(failed_provisioning(t).error, "igw: no user")
        su = User.objects.create_user("root@acme.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_super_admin=True,
                                      email_verified=True)
        self.assertTrue(can_provision(t, su))
        self.client.force_login(su)
        r = self.client.get(reverse("tenant_detail", args=[t.pk]))
        self.assertContains(r, 'data-open="dlg-t-provision"')
        self.assertContains(r, "igw: no user")
        r = self.client.get(reverse("approvals"))
        self.assertContains(r, reverse("tenant_detail", args=[t.pk]))
        fake = FakeClient()
        users = POOLS["one.userpool.info"].replace("</USER_POOL>", "<USER><ID>247</ID><NAME>romashka-gpulab</NAME>"
                                                    "<GID>520</GID><GROUPS><ID>520</ID></GROUPS></USER></USER_POOL>")
        from tenants import deploy
        with mock.patch.dict(POOLS, {"one.userpool.info": users}), secrets_loaded(self.secrets), \
                mock.patch("adoption.collector.client_for", return_value=fake), \
                mock.patch("tenants.deploy.crypt_sha512", return_value="$6$x$y"):
            d = deploy.Deployer(t, su, False)
            d.one = d.gone = deploy.One(d.control)
            d.gid = 520
            note = d.admin_step()
        self.assertIn("password set", note)
        self.assertIn(("one.user.passwd", (247, "Kx7-Pass+2026")), fake.calls)
        Deployment.objects.create(tenant=t, platform_key="dc1.cloud", state="done")
        self.assertTrue(provisioned(t))
        self.assertIsNone(failed_provisioning(t))


class TenantDeployment(DeploymentBase):
    def test_igw_console_password_and_random_ssh_port(self):
        from tenants.models import Tenant
        port = Tenant.random_igw_port()
        self.assertTrue(50000 <= port <= 59999)
        self.t.igw_ssh_port = port
        self.t.set_igw_password("Qz8-Gate+2026")
        self.t.save()
        dep, fake = self._run(dry=False)
        igw = [a[0] for m, a in fake.calls if m == "one.vm.allocate"][1]
        self.assertIn(f'SSH_PORT = "{port}"', igw)
        self.assertIn("CRYPTED_PASSWORD_BASE64", igw)
        self.assertIn('SSH_PUBLIC_KEY = "ssh-ed25519 AAAAuser me@host"', igw)
        egw = [a[0] for m, a in fake.calls if m == "one.vm.allocate"][0]
        self.assertIn('SSH_PORT = "55522"', egw)

    def test_prefix_already_in_opennebula_stops_the_run(self):
        busy = POOLS["one.vnpool.info"].replace("</VNET_POOL>", "<VNET><ID>9</ID><NAME>tenant_old-main_dc1</NAME><AR_POOL>"
                                                "<AR><IP>203.0.113.17</IP><SIZE>1</SIZE></AR></AR_POOL></VNET></VNET_POOL>")
        with mock.patch.dict(POOLS, {"one.vnpool.info": busy}):
            dep, fake = self._run(dry=False)
        self.assertEqual(dep.state, "failed")
        self.assertIn("tenant_old-main_dc1", dep.error)
        self.assertEqual([m for m, _a in fake.calls if not m.endswith(".info")], [])

    def test_preview_writes_nothing(self):
        dep, fake = self._run(dry=True)
        self.assertEqual(dep.error, "", dep.steps)
        writes = [m for m, _a in fake.calls if not m.endswith(".info")]
        self.assertEqual(writes, [])
        self.assertEqual((dep.resources["egw_ip"], dep.resources["igw_ip"]), ("203.0.113.16", "203.0.113.17"))
        self.assertEqual(dep.resources["egw_ix_ip"], "10.0.10.11")  # .10 is leased

    def test_deploy_creates_group_user_quota_acls_network_egw_igw(self):
        dep, fake = self._run(dry=False)
        self.assertEqual((dep.state, dep.error), ("done", ""), dep.steps)
        methods = [m for m, _a in fake.calls]
        for m in ("one.group.allocate", "one.group.update", "one.user.allocate", "one.group.addadmin", "one.group.quota",
                  "one.acl.addrule", "one.vn.allocate", "one.vn.chown", "one.vm.allocate", "one.vm.chown"):
            self.assertIn(m, methods)
        args = dict((m, a) for m, a in fake.calls)
        self.assertEqual(args["one.group.allocate"], ("_t_romashka_gpulab",))
        self.assertEqual(args["one.user.allocate"][:3], ("romashka-gpulab-admin", "Kx7-Pass+2026", "core"))
        vn_tpl = args["one.vn.allocate"][0]
        self.assertIn('NAME = "_t_romashka_gpulab-main_dc1"', vn_tpl)
        self.assertIn('GATEWAY = "203.0.113.16"', vn_tpl)
        self.assertIn('DNS = "203.0.113.2 203.0.113.3"', vn_tpl)
        vms = [a[0] for m, a in fake.calls if m == "one.vm.allocate"]
        egw, igw = vms
        self.assertIn('NAME = "_romashka_gpulab_egw_dc1"', egw)  # correction 11: _<org>_<tenant>_<role>_<site>
        self.assertIn('SET_HOSTNAME = "egw-romashka-gpulab-dc1"', egw)  # the host name: <role>-<org>-<tenant>-<site>
        self.assertIn('IP = "10.0.10.11"', egw)
        self.assertIn('USERNAME = "opsuser"', egw)
        self.assertNotIn("sapw", egw)  # only the hash goes to OpenNebula
        self.assertIn('USERNAME = "gpulab-igw-admin"', igw)
        self.assertIn('SSH_PUBLIC_KEY = "ssh-ed25519 AAAAuser me@host"', igw)
        self.assertNotIn("CRYPTED_PASSWORD", igw)
        quota = args["one.group.quota"][1]
        self.assertIn('RUNNING_CPU = "4"', quota)
        self.assertIn('SYSTEM_DISK_SIZE = "102400"', quota)  # 100 GB: all of it for VM disks (no images or files)
        self.assertEqual(len(dep.resources["acls"]), 14)
        self.assertNotIn("ONE_TIME_INIT_SCRIPT_BASE64", egw)  # no cloud-ix data here: no init script
        self.t.refresh_from_db()
        self.assertEqual((self.t.one_group_id, self.t.one_admin_user), (300, "romashka-gpulab-admin"))


    def test_edit_opens_the_pool_of_its_row(self):
        """Owner 2026-11-11: with two resource pools, Edit on any row opened the first one."""
        from accounts.models import Kind, User
        from django.urls import reverse
        from tenants.models import QuotaRecord, ResourceChange
        self._run(dry=False)
        self.t.refresh_from_db()
        QuotaRecord.objects.create(tenant=self.t, site="dc1", cluster="zz2", version=1, cpu=12, ram_gb=48, gpu=0,
                                   disk_gb=300, source="test")
        admin = User.objects.create_user("adm@romashka.kz", "Str0ng-PASS-123", kind=Kind.CUSTOMER,
                                         organization=self.t.organization, is_org_admin=True, email_verified=True)
        self.client.force_login(admin)
        detail = self.client.get(reverse("tenant_detail", args=[self.t.pk])).content.decode()
        self.assertIn("cluster=zz2", detail)  # each row's Edit names its pool
        url = reverse("tenant_resource_request", args=[self.t.pk])
        r = self.client.get(url, {"site": "dc1", "cluster": "zz2"})
        self.assertContains(r, "12 vCPU")
        self.assertContains(r, 'name="cluster" value="zz2"')
        r = self.client.post(url, {"site": "dc1", "cluster": "zz2", "cpu": 16, "ram_gb": 48, "disk_gb": 300})
        self.assertEqual(r.status_code, 302)
        ch = ResourceChange.objects.get(tenant=self.t)
        self.assertEqual((ch.site, ch.cluster, ch.cpu), ("dc1", "zz2", 16))
        r = self.client.get(url, {"site": "dc1", "cluster": "nope"})
        self.assertEqual(r.status_code, 302)  # not someone else's pool: back with a message

    def test_resource_change_applies_quota_with_split(self):
        from accounts.models import Kind, User
        from django.urls import reverse
        from tenants.models import QuotaRecord, ResourceChange
        self._run(dry=False)
        self.t.refresh_from_db()
        admin = User.objects.create_user("adm@romashka.kz", "Str0ng-PASS-123", kind=Kind.CUSTOMER,
                                         organization=self.t.organization, is_org_admin=True, email_verified=True)
        self.client.force_login(admin)
        r = self.client.post(reverse("tenant_resource_request", args=[self.t.pk]),
                             {"cpu": 8, "ram_gb": 32, "disk_gb": 200})
        self.assertEqual(r.status_code, 302)
        ch = ResourceChange.objects.get(tenant=self.t)
        self.assertEqual((ch.cpu, ch.disk_gb, ch.images_gb, ch.files_gb), (8, 200, 0, 0))  # the split carries over
        rm = User.objects.create_user("rm@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_super_admin=True,
                                      email_verified=True, totp_secret="JBSWY3DPEHPK3PXP", totp_enabled=True)
        self.client.force_login(rm)
        fake = FakeClient()
        with secrets_loaded(self.secrets), \
                mock.patch("adoption.collector.client_for", return_value=fake):
            self.assertContains(self.client.get(reverse("tenant_detail", args=[self.t.pk])), f'id="dlg-rc-{ch.pk}"')
            self.assertContains(self.client.get(reverse("approvals")), f'id="dlg-rc-{ch.pk}"')
            r = self.client.post(reverse("tenant_resource_decide", args=[self.t.pk, ch.pk, "approve"]),
                                 {"cpu": 6, "ram_gb": 32, "disk_gb": 200, "backups_gb": 10})  # the approver changes it
        self.assertEqual(r.status_code, 302, r.content[:300])
        ch.refresh_from_db()
        self.assertEqual(ch.state, "approved", ch.applied)
        # owner 2026-11-09: backups is the site's, so a pool change cannot move it - the posted 10 is ignored
        self.assertEqual(ch.changed_on_approval, [("cpu", 8, 6)])
        rec = [q for q in QuotaRecord.current_for(self.t) if q.cluster == "ca1"][0]
        self.assertEqual((rec.cpu, rec.disk_gb, rec.backups_gb), (6, 200, 0))
        quota = dict((m, a) for m, a in fake.calls)["one.group.quota"][1]
        self.assertIn('RUNNING_CPU = "6"', quota)
        # the tenant admin splits the storage without approval: VM disks get the rest
        self.client.force_login(admin)
        fake = FakeClient()
        with secrets_loaded(self.secrets), \
                mock.patch("adoption.collector.client_for", return_value=fake):
            self.client.post(reverse("tenant_storage_split", args=[self.t.pk]),
                             {"site": "dc1", "cluster": "ca1", "images_gb": 60, "files_gb": 150})  # too much
            self.assertEqual([q for q in QuotaRecord.current_for(self.t) if q.cluster == "ca1"][0].split, (200, 0, 0))
            self.client.post(reverse("tenant_storage_split", args=[self.t.pk]),
                             {"site": "dc1", "cluster": "ca1", "images_gb": 60, "files_gb": 5})
        rec = [q for q in QuotaRecord.current_for(self.t) if q.cluster == "ca1"][0]
        self.assertEqual((rec.disk_gb, rec.split, rec.cpu), (200, (135, 60, 5), 6))
        quota = dict((m, a) for m, a in fake.calls)["one.group.quota"][1]
        self.assertIn('SYSTEM_DISK_SIZE = "138240"', quota)  # 135 GB for VM disks
        self.assertIn('SIZE = "61440"', quota)  # 60 GB for images
        self.assertIn('SIZE = "5120"', quota)  # 5 GB for files
        from tenants.models import default_split
        self.assertEqual((default_split(100), default_split(101)), ((0, 0), (20, 1)))


class PasswordPolicy(TestCase):
    def test_rules(self):
        from tenants import passwords
        self.assertEqual(passwords.problems("Kx7-Pass+2026"), [])
        self.assertIn("at least 10 characters", passwords.problems("Kx7-Pa#2"))
        self.assertTrue(any("capital" in p for p in passwords.problems("kx7-pass#2026")))
        self.assertTrue(any("special" in p for p in passwords.problems("Kx7Pass2026abc")))
        self.assertTrue(any(p.startswith("not allowed") and "$" in p for p in passwords.problems("Kx7-Pass$2026")))
        self.assertTrue(any("?" in p for p in passwords.problems("Kx7-Pass?2026")))  # VyOS breaks on it
        self.assertTrue(any("space" in p for p in passwords.problems("Kx7-Pass 2026")))
        self.assertTrue(any("romashka" in p for p in passwords.problems("Romashka-77x", {"romashka"})))
        self.assertTrue(any("not allowed" in p and "@" in p and "#" in p for p in passwords.problems("Kx7-P@ss#2026")))
        words = passwords.words_for("gpulab", None, None)
        self.assertTrue(any("gpulab" in p for p in passwords.problems("Zq9-Pulab+2026", words)))  # a part: "ulab"
        from accounts.models import User
        u = User(email="ivan.i@example.kz", full_name="Ivan Ivanov", phone="+77011234567")
        w = passwords.words_for("", None, u)
        self.assertTrue(passwords.problems("Xy7-Ivano+2026", w))       # "ivan" of the name
        self.assertTrue(passwords.problems("Xy7-Qw+701123", w))        # 6 figures of the phone
        self.assertEqual(passwords.problems("Xy7-Qw+2026zz", w), [])   # "example", "kz" are not personal
        for _i in range(20):
            self.assertEqual(passwords.problems(passwords.generate()), [])

    def test_history_of_five(self):
        from django.contrib.auth.hashers import make_password
        from tenants import passwords
        hist = [make_password(f"Kx7-Pass+{i}000") for i in range(6)]
        self.assertTrue(any("last 5" in p for p in passwords.problems("Kx7-Pass+0000", (), hist)))
        self.assertFalse(any("last 5" in p for p in passwords.problems("Kx7-Pass+5000", (), hist)))  # 6th is forgotten


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode())
class InfraAndNetworks(DeploymentBase):
    """Credentials, configuration templates, network requests and the extra deployment steps."""

    IX_YAML = ("interface: bond0\nvlan: 10\nsubnet: 10.0.10.0\nprefix: 24\nranges:\n  egw:\n    start: 10.0.10.10\n    len: 20\n"
               "bgp:\n  local_asn: 4220101002\n  remote_asn: 4220101001\n  subnets:\n    - 203.0.113.0/25\n"
               "  ibgp:\n    peer_groups:\n      egw:\n        password: secrets.acme.cloud.network.cloud-ix.dc1.egw\n"
               "        hosts:\n          - 10.0.10.10/31\n      rrs:\n        password: secrets.acme.cloud.network.cloud-ix.dc1.rrs\n"
               "        hosts:\n          - 10.0.10.240/30\n  ebgp:\n    peer_groups:\n      ext:\n"
               "        password: secrets.acme.cloud.network.cloud-ix.dc1.ext\n        hosts:\n          - 10.0.10.0/30\n"
               "  rrs:\n    - 10.0.10.241\n    - 10.0.10.242\n")

    def setUp(self):
        super().setUp()
        import yaml
        from inventory.networks import import_site
        import_site("dc1", {"cloud-ix": yaml.safe_load(self.IX_YAML)})
        os.makedirs(os.path.join(self.secrets, "cloud", "network", "cloud-ix"))
        with open(os.path.join(self.secrets, "cloud", "network", "cloud-ix", "dc1.yaml"), "w") as f:
            f.write("egw: Bgp-Egw-Secret-9\nrrs: Bgp-Rrs-Secret-8\next: Bgp-Ext-Secret-7\n")
        with open(os.path.join(self.secrets, "ipmi.yaml"), "w") as f:
            f.write("users:\n  standard:\n    user: admin\n    password: Ipmi-Secret-6\n")
        from accounts.models import Kind, User
        self.sa = User.objects.create_user("sa@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_super_admin=True,
                                           email_verified=True, totp_secret="JBSWY3DPEHPK3PXP", totp_enabled=True)
        self.adm = User.objects.create_user("adm@romashka.kz", "Str0ng-PASS-123", kind=Kind.CUSTOMER,
                                            organization=self.t.organization, is_org_admin=True, email_verified=True)

    def test_secrets_by_type(self):
        from unittest import mock
        from django.urls import reverse
        from inventory import credentials as cr
        from inventory.models import Platform, PlatformControl, Secret
        with override_settings(INVENTORY_SECRETS_DIR=self.secrets):
            cr.load_from_secrets()
        one = Secret.objects.get(kind="opennebula", name="cloud.one")
        bgp = Secret.objects.get(kind="bgp", name="dc1.cloud-ix")
        self.assertEqual((bgp.get("rrs"), bgp.get("egw"), bgp.get("igw")), ("Bgp-Rrs-Secret-8", "Bgp-Ext-Secret-7", "Bgp-Egw-Secret-9"))
        self.assertEqual(cr.value("secrets.acme.cloud.network.cloud-ix.dc1.ext"), "Bgp-Ext-Secret-7")  # older references
        self.assertEqual(Secret.objects.get(kind="user", name="ipmi.users.standard").username, "admin")
        self.assertTrue(one.one_password_encrypted.startswith("gAAAA"))  # Fernet: encrypted, never the value
        one.put("one_password", "From-Portal-1")
        one.save()
        control = PlatformControl.objects.get(key="dc1.cloud")
        self.assertEqual(control.credentials()[1], "oneadmin:From-Portal-1")
        self.assertTrue(control.credential_defined)
        self.client.force_login(self.sa)
        url = reverse("infra_passwords")
        r = self.client.get(url + "?tab=bgp")
        self.assertContains(r, "dc1.cloud-ix")
        self.assertNotContains(r, "Bgp-Egw-Secret-9")
        r = self.client.get(url + "?tab=opennebula")
        self.assertContains(r, "cloud.one")
        self.assertContains(r, "platform dc1.cloud: front end")  # its contributors
        self.assertNotContains(r, "From-Portal-1")
        self.assertContains(self.client.get(url + "?tab=user"), "ipmi.users.standard")
        b = f"s{bgp.pk}-"
        r = self.client.get(url + "?tab=bgp")
        self.assertContains(r, f'name="{b}name"')  # the BGP dialog has a name like the others
        self.assertContains(r, 'data-secret="set"')  # kept values show as set, never their content
        self.client.post(reverse("infra_secret_save", args=[bgp.pk]), {b + "name": "dc1-ix", b + "net_type": "ix",
                                                                       b + "network": "dc1.cloud-ix", b + "rrs": "New-RRS-pw-2026"})
        bgp.refresh_from_db()
        self.assertEqual((bgp.name, bgp.get("rrs")), ("dc1-ix", "New-RRS-pw-2026"))
        self.client.post(reverse("infra_secret_save", args=[bgp.pk]), {b + "name": "dc1-ix", b + "net_type": "ix",
                                                                       b + "network": "dc1.cloud-ix", b + "rrs": "Bad@pass-2026"})
        bgp.refresh_from_db()
        self.assertEqual(bgp.get("rrs"), "New-RRS-pw-2026")  # @ is never allowed
        self.assertEqual(cr.value("secrets.acme.cloud.network.cloud-ix.dc1.ext"), "Bgp-Ext-Secret-7")  # renamed: still found
        vals = self.client.post(reverse("infra_secret_values", args=[bgp.pk])).json()
        self.assertEqual(vals[b + "rrs"], "New-RRS-pw-2026")
        from accounts.models import AuditLog
        self.assertTrue(AuditLog.objects.filter(action="secret_revealed", details__where="edit dialog").exists())
        self.assertEqual(bgp.get("egw"), "Bgp-Ext-Secret-7")  # empty fields keep what is stored
        p = f"s{one.pk}-"
        self.client.post(reverse("infra_secret_save", args=[one.pk]), {p + "name": "cloud.one", p + "db_name": "onedb",
                                                                       p + "db_user": "onedbuser", p + "db_password": "short"})
        one.refresh_from_db()
        self.assertNotEqual(one.get("db_password"), "short")  # at least 30 characters
        r = self.client.post(reverse("infra_secret_reveal", args=[one.pk]))
        self.assertContains(r, "From-Portal-1")
        ipmi = Secret.objects.get(kind="user", name="ipmi.users.standard")
        self.client.post(reverse("infra_secret_archive", args=[ipmi.pk]))  # not used: archived
        ipmi.refresh_from_db()
        self.assertEqual(ipmi.state, "archived")
        self.assertIsNone(cr.find("secrets.acme.ipmi.users.standard"))
        self.assertNotContains(self.client.get(url + "?tab=user"), f'data-id="s-{ipmi.pk}"')
        self.assertContains(self.client.get(url + "?tab=user&archived=1"), "ipmi.users.standard")
        self.client.post(reverse("infra_secret_archive", args=[one.pk]))  # used: refused
        one.refresh_from_db()
        self.assertEqual(one.state, "active")
        new = Secret(kind="opennebula", name="cloud.one2", db_name="onedb", db_user="onedbuser")
        for f, v in (("one_password", "P" * 15), ("os_password", "O" * 30), ("db_password", "D" * 30), ("os_private_key", "k")):
            new.put(f, v)
        new.save()
        with mock.patch("inventory.secret_ops.check_one", return_value=("ok", "")):
            self.client.post(reverse("infra_secret_replace", args=[one.pk]), {"new": new.pk})
        self.assertEqual(Platform.objects.get(key="dc1.cloud").fe_secrets, new.ref)  # by id: secret:<id>
        self.assertEqual(new.ref, f"secret:{new.pk}")
        one.refresh_from_db()
        self.assertEqual((one.state, one.replaced_by_id), ("archived", new.pk))  # every check passed
        self.client.post(reverse("infra_secret_add", args=["key"]), {"new-key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINSHMrKsawuiu5jH6cpkEQ0OQM4JXtkS1uuYH6TIpAa1 first", "new-name": ""})
        self.assertTrue(Secret.objects.filter(kind="key", name="first").exists())
        kept = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINSHMrKsawuiu5jH6cpkEQ0OQM4JXtkS1uuYH6TIpAa1 again"
        self.client.post(reverse("infra_secret_add", args=["key"]), {"new-key": kept, "new-name": ""})
        self.assertFalse(Secret.objects.filter(kind="key", name="again").exists())  # this key is already kept
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBxKq0vYkqF2Jb8m1oZr9cY0aVq0p5nXhWz3Lr7Tt9sD Some One"
        self.client.post(reverse("infra_secret_add", args=["key"]), {"new-key": key, "new-name": ""})
        k = Secret.objects.get(kind="key", name="Some One")
        self.assertEqual((k.key_type, k.comment), ("ssh-ed25519", "Some One"))
        self.assertEqual(self.client.post(reverse("infra_secret_save", args=[k.pk]), {}).status_code, 403)  # not editable
        self.client.force_login(self.adm)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_configurations_from_templates(self):
        import base64
        import re
        from django.urls import reverse
        from inventory import nfconfig
        dep, fake = self._run(dry=False)
        vms = [a[0] for m, a in fake.calls if m == "one.vm.allocate"]
        for vm in vms:  # egw and igw get their configuration as the one-time init script
            enc = re.search(r'ONE_TIME_INIT_SCRIPT_BASE64 = "([^"]+)"', vm)
            self.assertIsNotNone(enc, vm)
            self.assertIn("${conf} commit", base64.b64decode(enc.group(1)).decode())
        self.t.refresh_from_db()
        with secrets_loaded(self.secrets):
            egw = nfconfig.build("egw", tenant=self.t)
            self.assertIn('set firewall group network-group CLIENT network 203.0.113.16/31', egw)
            self.assertIn('set interfaces ethernet eth2 description "_t_romashka_gpulab-main_dc1"', egw)
            self.assertIn('set interfaces ethernet eth0 description "MGMT: cloud-vyos_dc1"', egw)
            self.assertIn("set vrf name MGMT protocols static route 0.0.0.0/0 next-hop 10.0.20.1", egw)
            self.assertIn("set protocols bgp neighbor 10.0.10.241 peer-group cloud-int", egw)
            self.assertIn("set protocols bgp system-as 4220101002", egw)
            self.assertIn("set qos policy shaper LIMIT-OUT bandwidth 100mbit", egw)
            for line in egw.splitlines():  # quotes: descriptions "…", passwords '…', nothing else
                self.assertEqual("'" in line, " password '" in line, line)
                self.assertEqual('"' in line, " description " in line, line)
            for default in ("ipv6 disable-forwarding", "loopback", "host-name", "login user", "eth0 address"):
                self.assertNotIn(default, egw)
            self.assertIn("set system time-zone Asia/Almaty", egw)  # correction 09: from the site settings
            self.assertIn("«secret:", egw)
            self.assertNotIn("Bgp-Egw-Secret-9", egw)
            self.assertIn("Bgp-Egw-Secret-9", nfconfig.build("egw", tenant=self.t, reveal=True))
            from inventory.models import Secret
            bgp = Secret.objects.get(kind="bgp", name="dc1.cloud-ix")
            old_igw = bgp.get("igw")
            bgp.put("igw", "Bgp&Egw<Sec>9")  # a configuration is not HTML: nothing is escaped
            bgp.save()
            self.assertIn("'Bgp&Egw<Sec>9'", nfconfig.build("egw", tenant=self.t, reveal=True))
            bgp.put("igw", old_igw)
            bgp.save()
            igw = nfconfig.build("igw", tenant=self.t)
            self.assertIn('set interfaces ethernet eth0 description "EXT: _t_romashka_gpulab-main_dc1"', igw)
            self.assertNotIn("static route", igw)  # the default route comes from the context
            self.assertIn("delete service ssh port\n", igw)  # the image starts with port 22
            self.assertNotIn("name-server", igw)  # the provider's DNS/NTP are not reachable from a tenant router
            self.assertNotIn("nat source", igw)  # no private networks yet
            script = nfconfig.init_script(nfconfig.build("egw", tenant=self.t, reveal=True), "egw")
            self.assertIn("${conf} set protocols bgp peer-group cloud-int password 'Bgp-Egw-Secret-9' >/dev/null 2>&1 ||", script)
            self.assertIn("${conf} commit", script)
            rr = nfconfig.build("rr-inet", site="dc1", index=0)
            self.assertIn("set protocols bgp neighbor 10.0.10.242 peer-group cloud-rr", rr)
            self.assertIn("set protocols bgp listen range 10.0.10.0/30 peer-group cloud-ext", rr)
            from inventory.models import Network
            px = Network.objects.get(site="dc1", name="cloud-ix")
            px.pk, px.name, px.vlan, px.subnet = None, "portal-ix", 109, "10.0.109.0"
            px.rrs = ["10.0.109.241", "10.0.109.242", "10.0.109.243"]
            px.save()
            prr = nfconfig.build("rr-inet", site="dc1", index=0, segment="portal-ix")  # the ext-managed platform's own names
            self.assertIn("set protocols bgp neighbor 10.0.109.242 peer-group portal-rr", prr)
            self.assertNotIn("peer-group cloud-", prr)
            self.client.force_login(self.sa)
            r = self.client.get(reverse("infra_templates") + f"?preview=egw&tenant={self.t.pk}")
            self.assertContains(r, "peer-group cloud-int password")
            self.assertNotContains(r, "Bgp-Egw-Secret-9")
            self.assertEqual(self.client.get(reverse("tenant_config", args=[self.t.pk, "igw"])).status_code, 200)

    def test_network_requests_and_their_deployment(self):
        from django.urls import reverse
        from tenants.models import L2Channel, NetworkRequest, PrefixAssignment
        self._run(dry=False)
        self.t.refresh_from_db()
        self.client.force_login(self.adm)
        url = reverse("tenant_network_request", args=[self.t.pk])
        self.client.post(url, {"kind": "inet", "prefix_len": 29})
        self.client.post(url, {"kind": "corpnet", "prefix_len": 28})  # not a corporate organization
        self.client.post(url, {"kind": "l2", "l2_name": "lab", "tenant_network_text": "lab-net", "note": "to the lab"})
        self.assertEqual(sorted(NetworkRequest.objects.values_list("kind", flat=True)), ["inet", "l2"])
        inet, l2 = NetworkRequest.objects.get(kind="inet"), NetworkRequest.objects.get(kind="l2")
        decide = lambda r, d, **kw: self.client.post(reverse("tenant_network_decide", args=[self.t.pk, r.pk, d]), kw)  # noqa: E731
        self.assertEqual(decide(inet, "approve").status_code, 403)  # the tenant cannot approve
        self.client.force_login(self.sa)
        with mock.patch("tenants.netops.start") as start:  # the change itself: tenants.tests_netops
            decide(inet, "approve")
            inet.refresh_from_db()
            self.assertEqual(inet.state, "approved")
            extra = PrefixAssignment.active().get(tenant=self.t, kind="inet_extra")
            self.assertEqual(extra.cidr, inet.result)
            decide(l2, "approve")  # correction 13: approved without a VLAN; not made yet
            l2.refresh_from_db()
            self.assertEqual((l2.state, start.call_count), ("approved", 1))
            ch = L2Channel.objects.get(tenant=self.t)
            self.assertEqual((ch.name, ch.vlan, ch.state), ("lab", None, "planned"))
            self.client.post(reverse("tenant_l2_vlan", args=[self.t.pk, ch.pk]), {"vlan": 1721})  # the support engineer
            self.assertEqual(start.call_count, 2)  # VLAN and subnet set: made
        ch = L2Channel.objects.get(tenant=self.t)
        self.assertEqual((l2.state, ch.vni, ch.vlan, ch.state), ("approved", 1721, 1721, "planned"))
        r = self.client.get(reverse("tenant_detail", args=[self.t.pk]))
        self.assertContains(r, extra.cidr)
        self.assertContains(r, "VLAN 1721")


class SitesAndHardware(TestCase):
    def setUp(self):
        from accounts.models import Kind, User
        from accounts.tests import make_inventory
        from inventory.models import Host, ServerProfile, Site
        make_inventory()
        ServerProfile.objects.filter(code="ca1").delete()
        pr = ServerProfile.objects.create(code="ca1", vendor="HPE", chassis="ProLiant DL385 Gen11", cpu_vendor="AMD",
                                          cpu_name="EPYC 9354", sockets=2, cores_per_socket=32, ram_gb=768,
                                          nics={"cards": [{"name": "ens1", "model": "MCX631102AS-ADAT", "speed": 25, "ports": 2,
                                                           "links": ["ens1f0np0", "ens1f1np1"]}]})
        Site.objects.filter(code="dc1").update(ntp_servers=["10.0.0.1"], dns_servers=["10.0.0.2"], timezone="almaty")
        self.h = Host.objects.create(site=Site.objects.get(code="dc1"), name="h1", profile=pr, ip="10.0.0.11",
                                     ipmi_host="10.0.1.11", ipmi_user="secrets.acme.ipmi.users.standard")
        self.sa = User.objects.create_user("sa@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_super_admin=True,
                                           email_verified=True, totp_secret="JBSWY3DPEHPK3PXP", totp_enabled=True)

    def test_site_dns_and_ntp_limits(self):
        from inventory import ops
        from inventory.edit_views import SiteForm
        from inventory.models import Site
        base = {**FULL_SITE, "code": "dc1", "number": str(Site.objects.get(code="dc1").number or 81)}
        self.assertTrue(SiteForm(base).is_valid(), SiteForm(base).errors)
        for f in ("timezone", "ntp_servers", "dns_servers", "bootstrap_dns", "mgmt_hosts", "ssh_ports", "log_collectors"):
            self.assertFalse(SiteForm({**base, f: ""}).is_valid(), f)  # every field required
        self.assertFalse(SiteForm({**base, "ntp_servers": "1.1.1.1 2.2.2.2 3.3.3.3 4.4.4.4 5.5.5.5"}).is_valid())
        self.assertFalse(SiteForm({**base, "bootstrap_dns": "1.1.1.1 2.2.2.2"}).is_valid())  # one green-field DNS server
        # correction 10: the required lines of the lists
        self.assertFalse(SiteForm({**base, "ssh_ports": "VyOS VMs | 55522"}).is_valid())  # no core services port
        self.assertFalse(SiteForm({**base, "log_collectors": "IPMI | 10.0.0.18 | udp | 15141"}).is_valid())
        self.assertFalse(SiteForm({**base, "log_collectors": base["log_collectors"] + "\nIPMI | 10.0.0.9 | udp | 1"}).is_valid())
        f = SiteForm({**base, "ssh_ports": base["ssh_ports"] + "\nBackup | 2222",
                      "log_collectors": base["log_collectors"] + "\nPublic services | logs.example.kz | udp | 514"})
        self.assertTrue(f.is_valid(), f.errors)
        ops.site_save(self.sa, "dc1", f.cleaned_data)
        s = Site.objects.get(code="dc1")
        self.assertEqual(s.management["ports"], {"vyos": 55522, "ssh": 55523})
        self.assertEqual(s.management["ports_extra"], [{"purpose": "Backup", "port": 2222}])
        self.assertEqual(s.log_collectors["core"], {"host": "10.0.0.18", "proto": "tcp", "port": 15140, "label": "Core services"})
        self.assertEqual(s.log_collectors["public-services"]["host"], "logs.example.kz")
        from inventory.views import site_initial
        self.assertTrue(SiteForm({**site_initial(s), "code": "dc1"}).is_valid())  # what the form shows saves again
        f = SiteForm({**base, "ntp_servers": "1.1.1.1, ntp.example.kz", "dns_servers": "8.8.8.8", "bootstrap_dns": "9.9.9.9"})
        self.assertTrue(f.is_valid(), f.errors)
        ops.site_save(self.sa, "dc1", f.cleaned_data)
        s = Site.objects.get(code="dc1")
        self.assertEqual(s.bootstrap_dns, ["9.9.9.9"])
        self.assertEqual(s.dns_servers, ["8.8.8.8"])
        self.assertIn("ntp.example.kz", s.ntp_servers)

    def test_sites_page_shows_everything_of_a_site(self):
        """2026-10-27: the details show all the site's settings; no jump host, no endpoints outside the inventory."""
        from django.urls import reverse
        from inventory.models import Site
        Site.objects.filter(code="dc1").update(timezone="Asia/Almaty", jump={"host": "10.9.9.9"},
                                               log_collectors={"vyos": {"host": "10.0.0.18", "proto": "tcp", "port": 15142}})
        self.client.force_login(self.sa)
        h = self.client.get(reverse("inventory")).content.decode()
        for text in ("Site code", "Country", "Data center", "UTC+05:00", "15142", "not set", "IPMI", "Storage", "Address or network"):
            self.assertIn(text, h)
        self.assertNotIn("Endpoints outside the inventory", h)
        self.assertNotIn("10.9.9.9", h)
        self.assertNotIn("Jump host", h)

    def test_ipmi_check_and_profile_refresh(self):
        from django.urls import reverse
        self.client.force_login(self.sa)
        found = {"h1": {"type": "ilo6", "model": "iLO 6", "firmware": "1.62", "ntp": ["10.0.0.9"], "dns": ["10.0.0.2"],
                        "timezone": "Ekaterinburg, Islamabad, Karachi, Tashkent", "tz_offset": "+05:00", "zones": [], "dhcp_ntp": False, "dhcp_dns": False,
                        "hostname": "h1-ilo"}}
        with mock.patch("inventory.redfish.for_hosts", return_value=found), mock.patch("inventory.ops.site_ipmi_save"):
            r = self.client.post(reverse("infra_site_ipmi", args=["dc1"]), {"action": "check"})
        self.assertContains(r, "ilo6")
        self.assertContains(r, 'name="hosts" value="h1"')  # NTP differs: offered for apply
        from inventory.redfish import zone_for
        zones = [{"index": 7, "offset": "-06:00", "dst": True}, {"index": 57, "offset": "+05:00", "dst": False},
                 {"index": 23, "offset": "+05:00", "dst": False}, {"index": 28, "offset": "+07:00", "dst": False}]
        self.assertEqual(zone_for(zones, "+05:00"), 23)
        same = {"vendor": "HPE", "chassis": "ProLiant DL385 Gen11", "cpu_vendor": "AMD", "cpu_name": "AMD EPYC 9354 32-Core",
                "sockets": 2, "cores_per_socket": 32, "ram_os_gb": 755,
                "nics": {"cards": [{"name": "ens1", "model": "MT2894 Family [ConnectX-6 Lx] MCX631102AS-ADAT", "speed": 25,
                                    "ports": 2, "links": ["ens1f0np0", "ens1f1np1"]}]}}
        with mock.patch("inventory.profiles._os_facts", return_value=same), \
                mock.patch("inventory.profiles._ipmi_facts", return_value={"ram_gb": 768}):
            r = self.client.post(reverse("inv_profile_check", args=["ca1"]), follow=True)
        from accounts.models import Task
        from inventory.models import ServerProfile
        t = Task.objects.filter(kind="profile check").latest("pk")
        self.assertEqual((t.state, t.result["different"]), ("done", 0))
        self.assertFalse(any(ServerProfile.objects.get(code="ca1").check_result.values()))
        self.assertContains(self.client.get(reverse("tasks")), "Check of server profile ca1")
        other = dict(same, cpu_name="Intel Xeon Gold 6430", cpu_vendor="Intel", cores_per_socket=32)
        with mock.patch("inventory.profiles._os_facts", return_value=other), \
                mock.patch("inventory.profiles._ipmi_facts", return_value={"ram_gb": 512}):
            r = self.client.post(reverse("inv_profile_check", args=["ca1"]), follow=True)
            self.assertTrue(any(ServerProfile.objects.get(code="ca1").check_result.values()))
            self.assertContains(r, "different")
            r = self.client.post(reverse("inv_host_add"), {"site": "dc1", "name": "h2", "ip": "10.0.0.12", "profile": "ca1"},
                                 follow=True)
        from inventory.models import Host
        h2 = Host.objects.get(name="h2")
        self.assertEqual(h2.profile.code, "ci1")  # ca1 does not fit: a profile is made from the host
        self.assertEqual((h2.profile.cpu_vendor, h2.profile.ram_gb, h2.profile.reference_host), ("Intel", 512, "dc1.h2"))
        with mock.patch("inventory.profiles._os_facts", return_value=same), \
                mock.patch("inventory.profiles._ipmi_facts", side_effect=Exception("no answer")):
            self.client.post(reverse("inv_host_add"), {"site": "dc1", "name": "h3", "ip": "10.0.0.13"})
        self.assertEqual(Host.objects.get(name="h3").profile.code, "ca1")  # detected
        self.assertIn("IPMI", Host.objects.get(name="h3").facts_error)


class HardwareFromIpmi(SitesAndHardware):
    def test_ipmi_names_os_aliases_and_profile_names(self):
        from inventory import profiles, redfish
        from inventory.models import ServerProfile
        pci = [{"DeviceType": "NIC", "Name": "Mellanox Network Adapter - A0:88:C2:2B:76:DE", "StructuredName": "NIC.Slot.1.1",
                "UEFIDevicePath": "PciRoot(0x1)/Pci(0x1,0x1)/Pci(0x0,0x0)"},
               {"DeviceType": "NIC", "Name": "Mellanox ConnectX-6 LX OCP3.0", "StructuredName": "OCP.Slot.22.1",
                "UEFIDevicePath": "PciRoot(0x3)/Pci(0x1,0x2)/Pci(0x0,0x0)"},
               {"DeviceType": "NIC", "Name": "ConnectX-7 400GE 1P NIC", "StructuredName": "NIC.Slot.3.1",
                "UEFIDevicePath": "PciRoot(0x123)/Pci(0x2,0x0)/Pci(0x0,0x0)"}]
        adapters = [{"model": "P42043-001 / Mellanox MCX631432AS-ADAI Ethernet 10/25Gb 2-port SFP28 OCP3 Adapter", "ports": 2, "speed": 25},
                    {"model": "HPE InfiniBand NDR/Ethernet 400Gb 1-port OSFP PCIe5 x16 MCX75310AAS-NEAT Adapter", "ports": 1,
                     "speed": redfish.nominal_speed(424.8)},
                    {"model": "P42046-001 / Mellanox MCX631102AS-ADAT Ethernet 10/25Gb 2-port SFP28 Adapter", "ports": 2, "speed": 25}]
        cards = {c["slot"]: c for c in redfish.nic_cards(pci, adapters)}
        self.assertEqual(cards["1"]["links"], ["ens1f0np0", "ens1f1np1"])
        self.assertEqual(cards["22"]["links"], ["ens22f0np0", "ens22f1np1"])
        self.assertEqual((cards["22"]["bus"], cards["1"]["model"][:16]), ("ocp3", "MCX631102AS-ADAT"))
        self.assertEqual((cards["3"]["links"], cards["3"]["speed"], cards["3"]["transceiver"]), (["enP1s3np0"], 400, "OSFP"))
        self.assertEqual(redfish.cpu_label("INTEL(R) XEON(R) GOLD 6548N"), "Xeon Gold 6548N")
        # correction 12: the serial number from the IPMI (Systems/1 SerialNumber)
        c = redfish.Client("h", "u", "p")
        sysinfo = {"Manufacturer": "HPE", "Model": "ProLiant DL365 Gen11", "SerialNumber": " CZ2D1F0ABC ",
                   "MemorySummary": {"TotalSystemMemoryGiB": 768}, "ProcessorSummary": {"Count": 2, "Model": "AMD EPYC 9354"}}
        with mock.patch.object(redfish.Client, "get", side_effect=lambda p: sysinfo if p == "/redfish/v1/Systems/1" else {}), \
                mock.patch.object(redfish.Client, "_members", return_value=[]), mock.patch.object(redfish.Client, "_cards", return_value=[]), \
                mock.patch.object(redfish.Client, "_boot", return_value={}):
            self.assertEqual(c.hardware()["serial"], "CZ2D1F0ABC")
        ipmi = {"vendor": "HPE", "chassis": "ProLiant DL365 Gen11", "cpu_vendor": "AMD", "cpu_name": "EPYC 9354", "sockets": 2,
                "cores_per_socket": 32, "ram_gb": 768, "ipmi_read": True, "boot": {"size_gb": 480, "controller": "NS204i"},
                "nics": {"cards": [dict(c, source="ipmi") for c in (cards["1"], cards["22"])]}}
        os_card = profiles._os_card({"pci": "0000:42:00", "slot": "1", "links": ["nic-data1", "nic-data2"], "driver": "mlx5_core",
                                     "fns": {"nic-data1": 0, "nic-data2": 1}, "speed": 25000, "model": "MT2894 Family [ConnectX-6 Lx]"})
        self.assertEqual(os_card["aliases"], {"nic-data1": "ens1f0np0", "nic-data2": "ens1f1np1"})
        os_facts = {"chassis": "ProLiant DL365 Gen11", "cpu_name": "AMD EPYC 9354 32-Core Processor", "nics": {"cards": [os_card]},
                    "boot": {"dev": "/dev/nvme0n1", "size_gb": 480, "volumes": [{"path": "/", "size_gb": 50, "fs": "xfs"}]}}
        with mock.patch("inventory.profiles._ipmi_facts", return_value=ipmi), mock.patch("inventory.profiles._os_facts", return_value=os_facts):
            facts, errors = profiles.read(self.h)
        self.assertEqual(errors, [])
        self.assertEqual((facts["cpu_name"], facts["boot"]["dev"], facts["boot"]["controller"]), ("EPYC 9354", "/dev/nvme0n1", "NS204i"))
        c1 = next(c for c in facts["nics"]["cards"] if c["slot"] == "1")
        self.assertEqual((c1["model"][:16], c1["aliases"]["nic-data2"]), ("MCX631102AS-ADAT", "ens1f1np1"))
        # a host in no cluster that does not answer over ssh: no error, the IPMI is enough
        with mock.patch("inventory.profiles._ipmi_facts", return_value=ipmi), \
                mock.patch("inventory.profiles._os_facts", side_effect=Exception("timeout")):
            facts2, errors2 = profiles.read(self.h)
        self.assertEqual((errors2, facts2["os_unread"]), ([], "timeout"))
        # the profile takes the names from the IPMI; its cards are compared by slot, the aliases with the host's
        p = ServerProfile.objects.get(code="ca1")
        p.nics = {"cards": [{"slot": "1", "name": "ens1", "vendor": "Mellanox", "model": "MCX631102AS-ADAT", "ports": 2, "speed": 25},
                            {"slot": "22", "name": "ens22", "vendor": "Mellanox", "model": "MCX631432AS-ADAI", "ports": 2, "speed": 25}],
                  "fabrics": 2, "assign": {"nic-data1": "ens1f0np0", "nic-data2": "ens22f0np0", "nic-storage1": "ens1f1np1",
                                           "nic-storage2": "ens22f1np1"}}
        p.save()
        changes = profiles.names_from_ipmi(p, [facts])
        p.refresh_from_db()
        self.assertEqual(p.chassis, "ProLiant DL365 Gen11")
        facts3 = dict(facts, nics={"cards": facts["nics"]["cards"] + [dict(cards["3"])]})
        profiles.names_from_ipmi(p, [facts3])
        p.refresh_from_db()
        self.assertEqual([c["slot"] for c in p.cards], ["1", "3", "22"])  # a card every host has is added
        p.nics = dict(p.nics, cards=[c for c in p.cards if c["slot"] != "3"])
        p.save()
        self.assertTrue(any("chassis" in x for x in changes))
        diffs = profiles.compare(p, facts)
        self.assertIn("nic-data2 is ens1f1np1 ≠ ens22f0np0", diffs)
        self.assertFalse([d for d in diffs if d.startswith("card")])


class FakeIgw:
    """A VyOS igw for the igw-configuration tests: running set lines, commit-confirm, confirm."""

    def __init__(self, lines):
        self.lines, self.pending, self.saved, self.calls = list(lines), None, list(lines), []

    def run(self, host, port, user, key, remote, site=None, stdin=None, timeout=60, password=None):
        from tenants.configcheck import SHOW, _norm
        self.calls.append((host, port, user, bool(key), password, remote))
        if remote == SHOW:
            return "\n".join(self.lines) + "\n"
        if remote == "vbash -s":
            if "commit-confirm" in stdin:
                self.pending = list(self.lines)
                for cmd in stdin.splitlines():
                    if cmd.startswith("set ") and _norm(cmd) not in {_norm(x) for x in self.lines}:
                        self.lines.append(cmd)
                    elif cmd.startswith("delete "):
                        node = _norm("set " + cmd[7:])
                        self.lines[:] = [x for x in self.lines if not (_norm(x) == node or _norm(x).startswith(node + " "))]
            elif "confirm" in stdin:
                self.pending, self.saved = None, list(self.lines)
            return ""
        raise RuntimeError(remote)


class IgwConfigurations(DeploymentBase):
    def test_secrets_configs_apply_confirm_rollback_and_fireedge_link(self):
        from datetime import timedelta
        from django.urls import reverse
        from accounts.models import Kind, User
        from inventory.models import Platform
        from tenants.models import IgwApply, IgwConfig, Membership
        admin = User.objects.create_user("adm@romashka.kz", "Str0ng-PASS-123", kind=Kind.CUSTOMER,
                                         organization=self.t.organization, is_org_admin=True, email_verified=True)
        Membership.objects.get_or_create(user=admin, tenant=self.t, defaults={"role": Membership.Role.ORG_ADMIN})
        self.client.force_login(admin)
        # the public FireEdge URL of the shared platform: an icon in the list and a line on the tenant page
        Platform.objects.filter(key="dc1.cloud").update(public_url="https://dc2-one.provider.example")
        r = self.client.get(reverse("tenant_detail", args=[self.t.pk]))
        self.assertContains(r, 'href="https://dc2-one.provider.example"')
        self.assertContains(r, 'data-open="dlg-speed-dc1"')  # owner 2026-10-29 20:20: the Internet rows' actions
        # the igw login is kept (the password encrypted); the OpenNebula password is checked before it is kept
        r = self.client.post(reverse("tenant_igw_login", args=[self.t.pk]),
                             {"host": "203.0.113.17", "port": 52222, "username": "gpulab-igw-admin", "password": "Igw-Pass-2026"})
        self.assertEqual(r.status_code, 302)
        self.t.refresh_from_db()
        self.assertEqual((self.t.igw_host, self.t.igw_ssh_port, self.t.get_igw_password()), ("203.0.113.17", 52222, "Igw-Pass-2026"))
        with mock.patch("tenants.igw_views.check_one_login", side_effect=__import__("tenants.deploy", fromlist=["x"]).DeployError("auth failed")):
            r = self.client.post(reverse("tenant_password_keep", args=[self.t.pk]), {"password": "Wrong-1"})
        self.assertContains(r, "did not accept")
        with mock.patch("tenants.igw_views.check_one_login"):
            self.client.post(reverse("tenant_password_keep", args=[self.t.pk]), {"password": "Kept-Pass+2026"})
        self.t.refresh_from_db()
        self.assertEqual(self.t.get_admin_password(), "Kept-Pass+2026")
        # configurations (correction 14: the Configs dialog): read the current one, store an edited copy, apply it over
        # ssh with commit-confirm, confirm
        igw = FakeIgw(["set system host-name 'gpulab-igw'", "set interfaces ethernet eth0 hw-id '00:11:22:33:44:55'",
                       "set system login user gpulab-igw-admin authentication encrypted-password 'x'",
                       "set protocols static route 10.9.0.0/16 next-hop 10.0.0.1"])
        with override_settings(DEPLOY_ASYNC=False), mock.patch("tenants.igwconfig.available", return_value=True), \
                mock.patch("inventory.sshexec.run", side_effect=igw.run):
            self.client.post(reverse("tenant_igw", args=[self.t.pk]), {"action": "read_current"})
            c = IgwConfig.objects.get(tenant=self.t, current=True)
            self.assertEqual((c.lines, c.source), (4, "read"))
            self.assertNotIn("host-name", c.text_encrypted)  # kept encrypted
            self.assertEqual(igw.calls[0][2:5], ("gpulab-igw-admin", False, "Igw-Pass-2026"))  # the tenant's login
            page = self.client.get(reverse("tenant_detail", args=[self.t.pk]) + "?igw=configs").content.decode()
            self.assertIn('id="dlg-igw-configs"', page)
            self.assertIn("set protocols static route 10.9.0.0/16", page)  # the current one, in the editor
            text = c.get_text().replace("route 10.9.0.0/16 next-hop 10.0.0.1", "route 10.8.0.0/16 next-hop 10.0.0.1")
            text = text.replace("set interfaces ethernet eth0 hw-id '00:11:22:33:44:55'\n", "")
            self.client.post(reverse("tenant_igw", args=[self.t.pk]), {"action": "save_config", "name": "new route", "text": text,
                                                                       "based_on": c.pk})
            n = IgwConfig.objects.get(tenant=self.t, name="new route")
            self.assertEqual((n.based_on_id, n.source, n.current), (c.pk, "edited", False))
            self.assertContains(self.client.get(reverse("tenant_igw_config", args=[self.t.pk, n.pk])), "10.8.0.0/16")  # Show
            self.client.post(reverse("tenant_igw", args=[self.t.pk]), {"action": "apply", "config": n.pk, "timeout_s": 90})
            a = IgwApply.objects.get(tenant=self.t)
            self.assertEqual(a.state, "waiting", a.result)
            self.assertIn("set protocols static route 10.8.0.0/16 next-hop 10.0.0.1", igw.lines)
            self.assertNotIn("set protocols static route 10.9.0.0/16 next-hop 10.0.0.1", igw.lines)
            self.assertIn("set interfaces ethernet eth0 hw-id '00:11:22:33:44:55'", igw.lines)  # hw-id is never deleted
            self.assertEqual(a.before.name, "before applying new route")  # the running one is stored first
            self.assertAlmostEqual((a.deadline - a.created_at).total_seconds(), 120, delta=5)  # 90 s: 2 whole minutes
            self.assertContains(self.client.get(reverse("tenant_detail", args=[self.t.pk]) + "?igw=configs"), "waits for your confirmation")
            self.client.post(reverse("tenant_igw", args=[self.t.pk]), {"action": "confirm", "apply": a.pk})
            a.refresh_from_db()
            n.refresh_from_db()
            self.assertEqual(a.state, "confirmed")
            self.assertIsNotNone(n.applied_at)
            self.assertEqual(igw.saved, igw.lines)
            # commands with commit-confirm, and the stored list is limited to 10
            self.client.post(reverse("tenant_igw", args=[self.t.pk]), {"action": "commands", "commands": "set system domain-name x.kz",
                                                                       "rollback": "on", "timeout_s": 60})
            cmd = IgwApply.objects.filter(tenant=self.t, method="commands").get()
            self.assertEqual((cmd.state, cmd.get_commands()), ("waiting", "set system domain-name x.kz"), cmd.result)
            self.client.post(reverse("tenant_igw", args=[self.t.pk]), {"action": "confirm", "apply": cmd.pk})
            for k in range(10):
                self.client.post(reverse("tenant_igw", args=[self.t.pk]), {"action": "save_config", "name": f"c{k}", "text": text})
            self.assertEqual(IgwConfig.objects.filter(tenant=self.t, current=False).count(), IgwConfig.MAX_STORED)
            # roll back: apply the configuration kept before — refused while 10 are stored (the current one is kept first)
            self.client.post(reverse("tenant_igw", args=[self.t.pk]), {"action": "apply", "config": a.before.pk, "timeout_s": 60})
            self.assertEqual(IgwApply.objects.filter(tenant=self.t).count(), 2)
            IgwConfig.objects.filter(tenant=self.t, name="c0").delete()  # 8 of the 10 fitted
            self.client.post(reverse("tenant_igw", args=[self.t.pk]), {"action": "apply", "config": a.before.pk, "timeout_s": 60})
            b = IgwApply.objects.filter(tenant=self.t).first()
            self.assertEqual(b.state, "waiting")
            IgwApply.objects.filter(pk=b.pk).update(deadline=timezone.now() - timedelta(seconds=1))
            self.client.get(reverse("tenant_detail", args=[self.t.pk]))
            b.refresh_from_db()
            self.assertEqual(b.state, "rolled_back")


class TenantDeletionFlow(DeploymentBase):
    def setUp(self):
        super().setUp()
        from accounts.models import Kind, User
        self.rm = User.objects.create_user("rm@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_resource_manager=True,
                                           email_verified=True, totp_secret="JBSWY3DPEHPK3PXP", totp_enabled=True)
        self.adm = User.objects.create_user("adm@romashka.kz", "Str0ng-PASS-123", kind=Kind.CUSTOMER,
                                            organization=self.t.organization, is_org_admin=True, email_verified=True)

    # a platform that forgets what is deleted: (pool method, tag, [(id, name, gid, uid)])
    WORLD = {
        "one.vmpool.info": ("VM_POOL", "VM", [(601, "_t_romashka_gpulab-egw_dc1", 0, 0), (602, "_t_romashka_gpulab-igw_dc1", 300, 400),
                                              (603, "app1", 300, 400), (604, "owned-by-user", 1, 400), (700, "other", 55, 3)]),
        "one.vnpool.info": ("VNET_POOL", "VNET", [(500, "_t_romashka_gpulab-main_dc1", 300, 0), (0, "cloud-ix_dc1", 0, 0)]),
        "one.imagepool.info": ("IMAGE_POOL", "IMAGE", [(110, "VyOS Router 2026.03", 217, 0), (111, "my-disk", 300, 400),
                                                       (112, "app1-backup", 300, 400)]),
        "one.templatepool.info": ("VMTEMPLATE_POOL", "VMTEMPLATE", [(7, "web", 300, 400), (8, "shared-tpl", 217, 0)]),
        "one.backupjobpool.info": ("BACKUPJOB_POOL", "BACKUPJOB", [(3, "nightly", 300, 400)]),
        "one.vrouterpool.info": ("VROUTER_POOL", "VROUTER", []),
        "one.secgrouppool.info": ("SECURITY_GROUP_POOL", "SECURITY_GROUP", [(0, "default", 0, 0), (101, "web-sg", 300, 400)]),
        "one.vmgrouppool.info": ("VM_GROUP_POOL", "VM_GROUP", []),
    }
    DELETES = {"one.vm.action": "one.vmpool.info", "one.vn.delete": "one.vnpool.info", "one.image.delete": "one.imagepool.info",
               "one.template.delete": "one.templatepool.info", "one.backupjob.delete": "one.backupjobpool.info",
               "one.secgroup.delete": "one.secgrouppool.info"}

    def _fake(self):
        world, deletes = self.WORLD, self.DELETES

        class Live(FakeClient):
            def __init__(self):
                super().__init__()
                self.gone = set()
                self.linger = {}  # (pool, id) → reads it is still listed after its delete (OpenNebula deletes images in the background)

            def _call(self, method, *args):
                if method in world:
                    self.calls.append((method, args))
                    root, tag, items = world[method]
                    for k in [k for k in self.linger if k[0] == method]:
                        self.linger[k] -= 1
                    return f"<{root}>" + "".join(f"<{tag}><ID>{i}</ID><NAME>{n}</NAME><GID>{g}</GID><UID>{u}</UID></{tag}>"
                                                 for i, n, g, u in items
                                                 if (method, i) not in self.gone or self.linger.get((method, i), -1) >= 0) + f"</{root}>"
                if method in deletes:
                    key = (deletes[method], args[1] if method == "one.vm.action" else args[0])
                    if key in self.gone:
                        from adoption.collector import OneError
                        raise OneError(f"[{method}] Error getting object [{key[1]}].")
                    self.gone.add(key)
                if method == "one.grouppool.info":
                    self.calls.append((method, args))
                    return "<GROUP_POOL/>" if ("group", 300) in self.gone else \
                        "<GROUP_POOL><GROUP><ID>300</ID><NAME>_t_romashka_gpulab</NAME></GROUP></GROUP_POOL>"
                if method == "one.group.delete":
                    self.gone.add(("group", args[0]))
                if method == "one.userpool.info":
                    self.calls.append((method, args))
                    users = [(400, "romashka-gpulab-admin", 300, "<ID>300</ID>"), (9, "support", 1, "<ID>1</ID><ID>300</ID>")]
                    return "<USER_POOL>" + "".join(f"<USER><ID>{i}</ID><NAME>{n}</NAME><GID>{g}</GID><GROUPS>{gs}</GROUPS></USER>"
                                                   for i, n, g, gs in users if ("user", i) not in self.gone) + "</USER_POOL>"
                if method == "one.user.delete":
                    self.gone.add(("user", args[0]))
                if method == "one.acl.info":
                    self.calls.append((method, args))
                    rules = [(50, "@300 VM+NET/@300 USE #100"), (51, "#400 USER/* CREATE *"), (52, "@301 ZONE/#100 USE #100"),
                             (53, "#9 USER/#400 USE *")]
                    return "<ACL_POOL>" + "".join(f"<ACL><ID>{i}</ID><STRING>{r}</STRING></ACL>" for i, r in rules
                                                  if ("acl", i) not in self.gone) + "</ACL_POOL>"
                if method == "one.acl.delrule":
                    self.gone.add(("acl", args[0]))
                if method == "one.group.info":
                    return super()._call(method, *args)
                return super()._call(method, *args)
        return Live()

    def test_request_lists_resources_and_approval_deletes_them(self):
        from django.urls import reverse
        from tenants.models import Lifecycle, PrefixAssignment, TenantDeletion
        self._run(dry=False)
        self.t.refresh_from_db()
        fake = self._fake()
        ctx = lambda: (secrets_loaded(self.secrets, DEPLOY_ASYNC=False),  # noqa: E731
                       mock.patch("adoption.collector.client_for", return_value=fake))
        self.client.force_login(self.adm)
        url = reverse("tenant_deletion_request", args=[self.t.pk])
        self.client.post(url, {"confirm": "wrong"})
        self.assertFalse(TenantDeletion.objects.exists())
        a, b = ctx()
        with a, b:
            self.client.post(url, {"confirm": "gpulab", "note": "project finished"})
        d = TenantDeletion.objects.get()
        self.assertEqual(d.state, "pending")
        plan = {k: sorted(i["id"] for i in v) for k, v in d.plan.items() if isinstance(v, list)}
        self.assertEqual(plan["vms"], [601, 602, 603, 604])  # not 700
        self.assertEqual(plan["images"], [111, 112])  # the VyOS image is shared, not the tenant's
        self.assertEqual((plan["templates"], plan["backupjobs"], plan["secgroups"]), ([7], [3], [101]))
        self.assertEqual(plan["networks"], [500])
        self.assertEqual(plan["acls"], [50, 51, 53])
        self.assertEqual((plan["users"], plan["members"]), ([400], [9]))
        self.assertEqual(d.plan["group"]["id"], 300)
        r = self.client.get(reverse("tenant_detail", args=[self.t.pk]))
        self.assertContains(r, "app1-backup")
        decide = reverse("tenant_deletion_decide", args=[self.t.pk, d.pk, "approve"])
        self.assertEqual(self.client.post(decide, {"confirm": "gpulab"}).status_code, 403)  # not the tenant's call
        self.client.force_login(self.rm)
        self.assertContains(self.client.get(reverse("approvals")), "app1-backup")  # the list is on the request
        a, b = ctx()
        with a, b:
            self.client.post(decide, {"confirm": "gpulab"})
        d.refresh_from_db()
        self.assertEqual(d.state, "done", (d.error, d.steps))
        self.assertEqual(d.steps[-2]["name"], "check")
        self.assertEqual({i["status"] for i in d.plan["vms"] + d.plan["images"] + d.plan["acls"]}, {"deleted"})
        from accounts.models import AuditLog
        logged = AuditLog.objects.filter(action="tenant_object_deleted", target_id=str(self.t.pk))
        self.assertEqual(logged.count(), d.plan_total + len(d.plan["members"]))
        calls = fake.calls
        self.assertEqual(sorted(a[1] for m, a in calls if m == "one.vm.action"), [601, 602, 603, 604])
        self.assertEqual([a[0] for m, a in calls if m == "one.vn.delete"], [500])
        self.assertEqual(sorted(a[0] for m, a in calls if m == "one.image.delete"), [111, 112])
        self.assertEqual([a for m, a in calls if m == "one.template.delete"], [(7, False)])
        self.assertEqual([a[0] for m, a in calls if m == "one.backupjob.delete"], [3])
        self.assertEqual(sorted(a[0] for m, a in calls if m == "one.acl.delrule"), [50, 51, 53])
        self.assertEqual([a[0] for m, a in calls if m == "one.user.delete"], [400])
        self.assertEqual([a for m, a in calls if m == "one.user.delgroup"], [(9, 300)])
        self.assertEqual([a[0] for m, a in calls if m == "one.group.delete"], [300])
        self.t.refresh_from_db()
        self.assertEqual(self.t.status, Lifecycle.ARCHIVED)
        self.assertFalse(PrefixAssignment.active().filter(tenant=self.t).exists())
        self.assertEqual(len(list(self.t.current_quota_records())), 0)
        self.client.force_login(self.adm)
        self.assertNotContains(self.client.get(reverse("my_org")), "gpulab")

    def test_the_gateways_by_name_are_the_tenants_and_no_other_tenants(self):
        """2026-10-28: beta-ml's egw (new name, the provider's VM) was not on the list — its main network could not go."""
        from tenants.models import Organization, Tenant
        from tenants.undeploy import names_of
        n = names_of(self.t)
        for name in ("_romashka_gpulab_egw_dc1", "_romashka_gpulab_l2src10_dc1", "_t_romashka_gpulab-main_dc1"):
            self.assertTrue(n.match(name), name)
        for name in ("_romashka_gpulab2_egw_dc1", "_romashka_gpulabx_egw_dc1", "_t_romashka_gpulab2-main_dc1"):
            self.assertFalse(n.match(name), name)
        org = Organization.objects.create(name="Example Cloud", short_name="examplecloud")
        mk = lambda name: Tenant.objects.create(organization=org, name=name, group_name=f"tenant-{name}", origin="adopted")  # noqa: E731
        beta, betaml = mk("beta"), mk("beta-ml")
        n = names_of(betaml)
        for name in ("_examplecloud_betaml_egw_dc2", "tenant_beta-ml-egw_dc2", "tenant_beta-ml-main_dc2",
                     "tenant-beta-ml-lan1_dc2"):
            self.assertTrue(n.match(name), name)
        n = names_of(beta)
        for name in ("_examplecloud_betaml_egw_dc2", "tenant_beta-ml-egw_dc2", "tenant-beta-ml-lan1_dc2"):
            self.assertFalse(n.match(name), name)
        self.assertTrue(n.match("tenant_beta-egw_dc2"))

    def test_images_deleted_in_the_background_are_waited_for(self):
        """2026-10-28: gamma-poc's image was still listed (being deleted) when the check read the platform."""
        from tenants import undeploy
        from tenants.models import TenantDeletion
        self._run(dry=False)
        fake = self._fake()
        fake.linger = {("one.imagepool.info", 111): 3, ("one.imagepool.info", 112): 1}
        d = TenantDeletion.objects.create(tenant=self.t, requested_by=self.rm)
        with secrets_loaded(self.secrets), mock.patch("adoption.collector.client_for", return_value=fake), \
                mock.patch.object(undeploy, "POLL_SECONDS", 0):
            undeploy.make_plan(d)
            d = undeploy.execute(d.pk)
        self.assertEqual(d.state, "done", (d.error, d.steps))
        self.assertEqual(sorted(a[0] for m, a in fake.calls if m == "one.image.delete"), [111, 112])

    def test_an_object_gone_meanwhile_is_not_an_error(self):
        from tenants import undeploy
        from tenants.models import TenantDeletion
        self._run(dry=False)
        fake = self._fake()
        d = TenantDeletion.objects.create(tenant=self.t, requested_by=self.rm)
        with secrets_loaded(self.secrets), mock.patch("adoption.collector.client_for", return_value=fake), \
                mock.patch.object(undeploy, "POLL_SECONDS", 0):
            undeploy.make_plan(d)
            fake.gone.add(("one.imagepool.info", 111))  # deleted after the read, before the call
            fake.linger[("one.imagepool.info", 111)] = 1  # still listed once
            d = undeploy.execute(d.pk)
        self.assertEqual(d.state, "done", (d.error, d.steps))
        self.assertEqual(next(i for i in d.plan["images"] if i["id"] == 111)["status"], "gone")

    def test_check_fails_when_something_is_left(self):
        from tenants import undeploy
        from tenants.models import TenantDeletion
        self._run(dry=False)
        fake = self._fake()
        d = TenantDeletion.objects.create(tenant=self.t, requested_by=self.rm)
        with secrets_loaded(self.secrets), mock.patch("adoption.collector.client_for", return_value=fake):
            undeploy.make_plan(d)
            d.plan["images"] = [i for i in d.plan["images"] if i["id"] != 112]  # a backup made after the request
            d.save()
            d = undeploy.execute(d.pk)
        self.assertEqual(d.state, "failed")
        self.assertIn("images #112 app1-backup", d.error)
        self.assertEqual(d.steps[-1]["name"], "verify")  # stopped before the users and the group
        self.assertFalse(any(m == "one.group.delete" for m, _a in fake.calls))
        self.t.refresh_from_db()
        self.assertNotEqual(self.t.status, "archived")

    def test_staff_delete_now_on_read_only_platform_touches_only_the_portal(self):
        from django.urls import reverse
        from inventory.models import PlatformControl
        from tenants.models import TenantDeletion
        PlatformControl.objects.filter(key="dc1.cloud").update(mode="adoption")
        self.client.force_login(self.rm)
        with override_settings(DEPLOY_ASYNC=False), mock.patch("adoption.collector.client_for") as cf:
            self.client.post(reverse("tenant_delete_now", args=[self.t.pk]), {"confirm": "_t_romashka_gpulab"})
            d = TenantDeletion.objects.get()
            self.assertEqual((d.state, d.steps or []), ("pending", []))  # never at once: an approval first
            self.client.post(reverse("tenant_deletion_decide", args=[self.t.pk, d.pk, "approve"]),
                             {"confirm": "_t_romashka_gpulab"})  # the requester approves their own request
        cf.assert_not_called()
        d = TenantDeletion.objects.get()
        self.assertEqual((d.state, [s["name"] for s in d.steps]), ("done", ["portal"]))
        self.t.refresh_from_db()
        self.assertEqual(self.t.status, "archived")  # never deleted from the portal
        r = self.client.get(reverse("tenant_list"))  # archived tenants on demand only
        self.assertNotContains(r, 'data-id="t-%d"' % self.t.pk)
        self.assertContains(self.client.get(reverse("tenant_list") + "?archived=1"), 'data-id="t-%d"' % self.t.pk)
        from accounts.models import AuditLog
        acts = set(AuditLog.objects.filter(target_type="tenant", target_id=str(self.t.pk)).values_list("action", flat=True))
        self.assertTrue({"tenant_deleted", "tenant_deletion_requested", "tenant_deletion_approved"} <= acts)  # in Logs


class PlatformHealth(TestCase):
    def setUp(self):
        from accounts.tests import make_inventory
        from inventory.models import PlatformControl
        make_inventory()
        self.c = PlatformControl.objects.create(key="dc1.cloud", xmlrpc_url="x")

    def _client(self, images, svr_image=247):
        cl = mock.Mock()
        cl.version.return_value = "7.0.1"
        pools = {
            "one.imagepool.info": "<IMAGE_POOL>" + "".join(
                f"<IMAGE><ID>{i}</ID><NAME>{n}</NAME><STATE>{s}</STATE><DATASTORE>images</DATASTORE></IMAGE>"
                for i, n, s in images) + "</IMAGE_POOL>",
            "one.templatepool.info": ("<VMTEMPLATE_POOL><VMTEMPLATE><ID>77</ID><NAME>Service Virtual Router</NAME>"
                                      f"<TEMPLATE><DISK><IMAGE_ID>{svr_image}</IMAGE_ID></DISK></TEMPLATE></VMTEMPLATE>"
                                      "</VMTEMPLATE_POOL>"),
        }
        cl._call.side_effect = lambda method, *a: pools[method]
        return cl

    def test_image_checked_and_period_respected(self):
        from inventory import health
        good = [(110, "VyOS Router 2026.03", 1), (247, "Service Virtual Router", 1)]
        with mock.patch("adoption.collector.client_for", return_value=self._client(good)), \
                mock.patch("inventory.models.PlatformControl.configured", new_callable=mock.PropertyMock, return_value=True):
            self.assertEqual(health.check_due(), {"dc1.cloud": True})
            self.assertEqual(health.check_due(), {})  # checked a moment ago: not due (30 minutes)
            self.c.refresh_from_db()
            self.assertEqual(self.c.health["image"], {"id": 110, "state": "ready", "datastore": "images"})
        with mock.patch("adoption.collector.client_for", return_value=self._client([(110, "VyOS Router 2026.02", 1)])), \
                mock.patch("inventory.models.PlatformControl.configured", new_callable=mock.PropertyMock, return_value=True):
            self.assertEqual(health.check_due(force=True), {"dc1.cloud": False})
            self.c.refresh_from_db()
            self.assertIn("not found", self.c.health["problems"][0])
        # the native router's image in ERROR (as found on dc2 in the checks): a problem of the platform
        bad = [(110, "VyOS Router 2026.03", 1), (247, "Service Virtual Router", 5)]
        with mock.patch("adoption.collector.client_for", return_value=self._client(bad)), \
                mock.patch("inventory.models.PlatformControl.configured", new_callable=mock.PropertyMock, return_value=True):
            self.assertEqual(health.check_due(force=True), {"dc1.cloud": False})
            self.c.refresh_from_db()
            self.assertEqual(self.c.health["problems"], ["one-router image 'Service Virtual Router' #247 is error"])
        rows = {r.role or r.name: (r.one_id, r.state) for r in self.c.service_images.all()}
        self.assertEqual(rows, {"vyos": (110, "ready"), "one-router-template": (77, "found"), "one-router-image": (247, "error")})


    def test_service_images_and_import_switch(self):
        from django.urls import reverse
        from accounts.models import Kind, User
        sa = User.objects.create_user("sa@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_super_admin=True,
                                      email_verified=True, totp_secret="JBSWY3DPEHPK3PXP", totp_enabled=True)
        self.client.force_login(sa)
        r = self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=platforms")
        self.assertContains(r, "VyOS Router 2026.03")
        self.assertContains(r, "Read projects: on")
        rows = {x.role: x for x in self.c.service_images.all()}
        self.assertEqual(set(rows), {"vyos", "one-router-template", "one-router-image"})
        self.client.post(reverse("service_image_save", args=["dc1.cloud", rows["one-router-template"].pk]), {"name": "SVR 7"})
        self.client.post(reverse("service_image_save", args=["dc1.cloud", rows["one-router-image"].pk]), {"name": "x"})
        self.client.post(reverse("service_image_save", args=["dc1.cloud", rows["vyos"].pk]), {"name": "VyOS 2026.09", "kind": "template"})
        self.c.refresh_from_db()
        self.assertEqual((self.c.nr_template, self.c.nf_image), ("SVR 7", "VyOS 2026.09"))
        names = {x.role: (x.kind, x.name) for x in self.c.service_images.all()}
        self.assertEqual(names, {"vyos": ("image", "VyOS 2026.09"), "one-router-template": ("template", "SVR 7"),
                                 "one-router-image": ("image", "SVR 7")})  # the router image takes the template's name
        self.client.post(reverse("service_image_add", args=["dc1.cloud"]), {"purpose": "base-os", "kind": "image", "name": "Ubuntu 26.04"})
        extra = self.c.service_images.get(role="")
        self.client.post(reverse("service_image_delete", args=["dc1.cloud", rows["vyos"].pk]))  # mandatory: kept
        self.client.post(reverse("service_image_delete", args=["dc1.cloud", extra.pk]))
        self.assertEqual(self.c.service_images.count(), 3)
        self.client.post(reverse("platform_control_collect", args=["dc1.cloud"]))
        self.c.refresh_from_db()
        self.assertFalse(self.c.collect_enabled)


class TenantSshKeys(DeploymentBase):
    def test_upload_several_keys_and_assign_them_to_the_igw(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.urls import reverse
        from accounts.models import Kind, User
        from inventory import nfconfig
        from tenants.models import TenantSshKey
        adm = User.objects.create_user("adm@romashka.kz", "Str0ng-PASS-123", kind=Kind.CUSTOMER,
                                       organization=self.t.organization, is_org_admin=True, email_verified=True)
        first = TenantSshKey.objects.create(tenant=self.t, name="gpulab-igw-admin", key=self.t.igw_ssh_key, on_igw=True)
        self.client.force_login(adm)
        url = reverse("tenant_ssh_keys_add", args=[self.t.pk])
        pub = SimpleUploadedFile("bob.pub", b"ssh-rsa AAAAB3NzaC1yc2EAAAADAQAB bob@host\n")
        self.client.post(url, {"keys": "ssh-ed25519 AAAAalice alice@laptop\nnot a key\nssh-ed25519 AAAAuser me@host",
                               "files": [pub]})
        names = list(self.t.ssh_keys.values_list("name", "on_igw"))
        self.assertEqual(names, [("gpulab-igw-admin", True), ("alice@laptop", False), ("bob@host", False)])  # duplicate skipped
        alice = self.t.ssh_keys.get(name="alice@laptop")
        self.client.post(reverse("tenant_ssh_key_action", args=[self.t.pk, alice.pk, "assign"]))
        self.client.post(reverse("tenant_ssh_key_action", args=[self.t.pk, first.pk, "unassign"]))
        self.t.refresh_from_db()
        self.assertEqual(self.t.igw_ssh_key, "ssh-ed25519 AAAAalice alice@laptop")  # the igw is created with it
        r = self.client.post(reverse("tenant_ssh_key_action", args=[self.t.pk, alice.pk, "unassign"]), follow=True)
        self.assertContains(r, "at least one key")  # ssh is key-only
        self._run(dry=False)
        self.t.refresh_from_db()
        igw = nfconfig.build("igw", tenant=self.t)
        self.assertIn("set system login user gpulab-igw-admin authentication public-keys alice@laptop key AAAAalice", igw)
        self.assertNotIn("public-keys gpulab-igw-admin key", igw)
        self.assertNotIn("disable-password-authentication", igw)  # with a key: key-only, as the image sets it
        self.t.ssh_keys.update(on_igw=False)
        from inventory.models import ConfigTemplate
        ConfigTemplate.objects.filter(role="igw").update(body=ConfigTemplate.objects.get(role="igw").body.replace(
            "{% if not igw_keys %}delete service ssh disable-password-authentication\n{% endif %}", ""))  # as on dc1/dc2
        igw = nfconfig.build("igw", tenant=self.t)
        self.assertEqual(igw.count("delete service ssh disable-password-authentication"), 1)  # no key: the password
        self.assertLess(igw.index("disable-password-authentication"), igw.index("set system time-zone"))
        r = self.client.get(reverse("tenant_detail", args=[self.t.pk]))
        self.assertNotContains(r, 'id="sshkeys"')  # correction 13: keys only on the organization's portal


class OrgKeysPage(DeploymentBase):
    def test_keys_page_adds_to_several_tenants(self):
        from django.urls import reverse
        from accounts.models import Kind, User
        from tenants.models import Tenant
        other = Tenant.objects.create(organization=self.t.organization, name="labs", title="labs", approval="approved")
        adm = User.objects.create_user("adm@romashka.kz", "Str0ng-PASS-123", kind=Kind.CUSTOMER,
                                       organization=self.t.organization, is_org_admin=True, email_verified=True)
        self.client.force_login(adm)
        r = self.client.get(reverse("dashboard"))
        nav = r.content.decode()
        order = [nav.index(f'href="{reverse(n)}"') for n in ("dashboard", "my_org", "org_users", "org_keys", "org_logs")]
        self.assertEqual(order, sorted(order))  # Dashboard, Tenants, Users, Keys, Logs
        self.client.post(reverse("org_keys"), {"section": "add", "tenants": [self.t.pk, other.pk],
                                               "keys": "ssh-ed25519 AAAAteam team@office", "on_igw": "on"})
        self.assertEqual(self.t.ssh_keys.count(), 1)
        self.assertEqual(other.ssh_keys.count(), 1)
        self.assertContains(self.client.get(reverse("org_keys")), "team@office")
        self.client.post(reverse("org_keys"), {"key-default_ssh_key": "ssh-ed25519 AAAAdef d@x"})
        self.t.organization.refresh_from_db()
        self.assertEqual(self.t.organization.default_ssh_key, "ssh-ed25519 AAAAdef d@x")


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode())
class ConfigChecks(InfraAndNetworks):
    test_secrets_by_type = None
    test_configurations_from_templates = None
    test_network_requests_and_their_deployment = None

    def test_routers_compared_with_the_templates(self):
        from django.urls import reverse
        from inventory import nfconfig
        from inventory.credentials import load_from_secrets
        from tenants import configcheck
        with override_settings(INVENTORY_SECRETS_DIR=self.secrets):
            load_from_secrets()
            self._run(dry=False)
            self.t.refresh_from_db()
            egw = nfconfig.build("egw", tenant=self.t, reveal=True)
            igw = nfconfig.build("igw", tenant=self.t, reveal=True)
        self.assertIn("set system login user claugine authentication public-keys claugine key", igw)
        # egw ssh: the whole IX network except its first /29; igw ssh: what the tenant ordered (any by default)
        # egw ssh: only in the management VRF (eth0), from the site's management hosts
        self.assertNotIn("SSHFROM", egw)
        self.assertIn("set firewall group network-group MGMT network 10.0.0.22/31", egw)
        self.assertIn("set service ssh vrf MGMT", egw)
        self.assertIn("set firewall zone MGMT member interface eth0", egw)
        self.assertIn("SSHFROM network 0.0.0.0/0", igw)
        self.t.igw_ssh_from = "198.51.100.7, 203.0.113.0/24"
        with secrets_loaded(self.secrets):
            igw2 = nfconfig.build("igw", tenant=self.t)
        self.assertIn("SSHFROM network 198.51.100.7/32", igw2)
        self.assertIn("SSHFROM network 203.0.113.0/24", igw2)
        self.assertNotIn("0.0.0.0/0", igw2)
        self.t.igw_ssh_from = "0.0.0.0/0"
        self.assertEqual(nfconfig.segment_ssh_from("10.0.10.0/29"), ["10.0.10.0/29"])

        def fake_fetch(host, port, user, key, site=None):
            text = egw if host == "10.0.10.11" else igw
            lines = [l.replace(" description ", " description '").rstrip() for l in text.splitlines() if l.startswith("set ")]
            lines = [l for l in lines if "IX-to-GW rule 12" not in l]  # a rule removed on the router
            return "\n".join(lines + ["set firewall ipv4 name GW-to-IX rule 99 action accept"]) + "\n"
        self.client.force_login(self.sa)
        with secrets_loaded(self.secrets, DEPLOY_ASYNC=False), \
                mock.patch("tenants.configcheck.fetch", side_effect=fake_fetch):
            self.client.post(reverse("tenant_config_check", args=[self.t.pk]))
        c = self.t.config_checks.get()
        eg, ig = c.results["egw"], c.results["igw"]
        self.assertEqual((eg["status"], ig["status"]), ("drift", "drift"))
        self.assertIn("set firewall ipv4 name IX-to-GW rule 12 action accept", eg["missing"])
        self.assertIn("set firewall ipv4 name GW-to-IX rule 99 action accept", eg["extra"])
        self.assertTrue(all("Bgp-Egw-Secret-9" not in l for l in eg["missing"] + eg["extra"]))
        self.assertEqual(ig["user"], "claugine")
        r = self.client.get(reverse("tenant_detail", args=[self.t.pk]))
        self.assertNotContains(r, "rule 99")  # the configuration check section was removed (2026-10-29)
        self.assertNotContains(r, "Bgp-Egw-Secret-9")
        self.assertEqual(configcheck.compare("set a 'b'\nset x y\n", "set a b\nset x y\n"), ([], []))
        first_boot = "set service ssh port 22\nset service ssh disable-password-authentication\n" \
                     "set interfaces ethernet eth0 address '198.19.182.99/31'\n"
        self.assertEqual(configcheck.compare("set service ssh port 22\nset interfaces ethernet eth0 description x\n",
                                             first_boot), (["set interfaces ethernet eth0 description x"], []))


class JobTwoInfrastructure(SitesAndHardware):
    """Log collectors, site settings on the IPMI, profile lists, endpoint delete."""

    def test_log_collectors_profile_lists_and_site_settings(self):
        from unittest import mock
        from django.urls import reverse
        from inventory import ops
        from inventory.edit_views import SiteForm
        from inventory.models import PlatformControl, ServerProfile, Site
        base = {**FULL_SITE, "code": "dc1", "number": str(Site.objects.get(code="dc1").number or 81),
                "log_collectors": "IPMI | logs.example.kz | udp | 514\nVyOS VMs | logs.example.kz | tcp | 6514\n"
                                  "Core services | 10.0.0.19 | udp | 514"}
        f = SiteForm(base)
        self.assertTrue(f.is_valid(), f.errors)
        ops.site_save(self.sa, "dc1", f.cleaned_data)
        s = Site.objects.get(code="dc1")
        self.assertEqual(s.log_collectors["vyos"], {"host": "logs.example.kz", "proto": "tcp", "port": 6514, "label": "VyOS VMs"})
        self.assertEqual(s.log_collectors["ipmi"]["proto"], "udp")
        self.assertEqual(s.log_collectors["core"]["host"], "10.0.0.19")  # each purpose its own collector now
        self.assertFalse(SiteForm({**base, "log_collectors": "VyOS VMs | | tcp | 6514"}).is_valid())  # no address
        self.client.force_login(self.sa)
        # profile: cards and bonds as lists of fields
        r = self.client.post(reverse("inv_profile_edit", args=["ca1"]), {
            "vendor": "HPE", "chassis": "ProLiant DL385 Gen11", "cpu_vendor": "AMD", "cpu_name": "EPYC 9354", "sockets": 2,
            "cores_per_socket": 32, "ram_gb": 768, "boot_volumes": "/ 50 xfs\n/var/log 100 xfs",
            "cards": "1 | ens1 | Mellanox | MCX631102AS-ADAT | 2 | 25 | SFP28\n2 | ens2 | Intel | E810 | 2 | 25 | SFP28",
            "fabrics": "1", "assign": "nic-data1 | ens1f0np0\nnic-data2 | ens2f0"}, follow=True)
        p = ServerProfile.objects.get(code="ca1")
        self.assertEqual([c["name"] for c in p.cards], ["ens1", "ens2"])
        self.assertEqual(p.cards[0]["links"], ["ens1f0np0", "ens1f1np1"])  # kept from the hosts
        self.assertEqual(p.cards[1]["transceiver"], "SFP28")
        self.assertEqual(p.bonds, [{"name": "bond0", "links": ["ens1f0np0", "ens2f0"], "aliases": ["nic-data1", "nic-data2"],
                                    "mtu": 9100, "carries": "data and storage"}])
        r = self.client.post(reverse("inv_profile_edit", args=["ca1"]), {
            "vendor": "HPE", "chassis": "x", "cpu_vendor": "AMD", "cpu_name": "EPYC", "sockets": 2, "cores_per_socket": 32,
            "ram_gb": 768, "cards": "1 | ens1 | Mellanox | M | 2 | 25 | SFP28", "fabrics": "2",
            "assign": "nic-data1 | ens9f0"}, follow=True)
        self.assertContains(r, "is not a port of the cards")
        r = self.client.post(reverse("inv_profile_edit", args=["ca1"]), {
            "vendor": "HPE", "chassis": "x", "cpu_vendor": "AMD", "cpu_name": "EPYC", "sockets": 2, "cores_per_socket": 32,
            "ram_gb": 768, "cards": "1 | ens1 | Mellanox | M | 2 | 25 | SFP28", "fabrics": "1",
            "assign": "nic-data1 | ens1f0np0\nnic-storage1 | ens1f1np1"}, follow=True)
        self.assertContains(r, "no storage bond")
        # verify: everything complete, then only the volumes change
        r = self.client.post(reverse("inv_profile_verify", args=["ca1"]), follow=True)
        self.assertContains(r, "cannot be verified")  # the hosts are not checked yet
        ServerProfile.objects.filter(code="ca1").update(checked_at=timezone.now(), check_result={"h1": []})
        r = self.client.post(reverse("inv_profile_verify", args=["ca1"]), follow=True)
        self.assertContains(r, "is verified")
        self.client.post(reverse("inv_profile_edit", args=["ca1"]), {
            "vendor": "Other", "chassis": "x", "cpu_vendor": "AMD", "cpu_name": "EPYC", "sockets": 1, "cores_per_socket": 8,
            "ram_gb": 64, "boot_volumes": "/ 60 xfs", "cards": "", "fabrics": "1", "assign": "nic-data1 | ens1f1np1\nnic-data2 | ens1f0np0"},
            follow=True)
        p = ServerProfile.objects.get(code="ca1")
        self.assertEqual((p.vendor, p.ram_gb, len(p.cards)), ("HPE", 768, 2))  # kept
        self.assertEqual([v["size_gb"] for v in p.boot_volumes], [60])
        self.assertEqual(p.assign["nic-data1"], "ens1f1np1")  # no active host of a cluster uses it: aliases change
        from inventory.models import Cluster, Platform
        c = Cluster.objects.create(platform=Platform.objects.first(), name="c-test", kind="shared")
        c.hosts.add(self.h)
        self.assertTrue(ServerProfile.objects.get(code="ca1").assign_locked)
        self.client.post(reverse("inv_profile_edit", args=["ca1"]), {
            "vendor": "HPE", "chassis": "x", "cpu_vendor": "AMD", "cpu_name": "EPYC", "sockets": 2, "cores_per_socket": 32,
            "ram_gb": 768, "boot_volumes": "/ 70 xfs", "fabrics": "1", "assign": "nic-data1 | ens1f0np0\nnic-data2 | ens1f1np1"})
        p = ServerProfile.objects.get(code="ca1")
        self.assertEqual((p.assign["nic-data1"], p.boot_volumes[0]["size_gb"]), ("ens1f1np1", 70))
        c.delete()
        # site settings on the IPMI: planned changes, then a task applies them
        now = {"ntp": ["10.0.0.1"], "dns": ["1.1.1.1"], "syslog": {}, "tz_offset": "+05:00", "timezone": "Almaty", "zones": []}
        with mock.patch("inventory.redfish.Client.settings", return_value=now), \
                mock.patch("inventory.redfish.host_credentials", return_value=("u", "p")):
            r = self.client.get(reverse("infra_profile_site_settings", args=["ca1"]))
            self.assertContains(r, "1.1.1.1")
            self.assertContains(r, "logs.example.kz:514")
            with mock.patch("inventory.redfish.Client.apply", return_value=[]) as ap:
                self.client.post(reverse("infra_profile_site_settings", args=["ca1"]), {"hosts": ["h1"]})
                self.assertEqual(ap.call_args.kwargs["dns"], ["10.0.0.2"])
                self.assertEqual(ap.call_args.kwargs["syslog"], {"host": "logs.example.kz", "port": 514})
        from accounts.models import Task
        self.assertEqual(Task.objects.get(kind="site settings").state, "done")


class JobTwoTenants(DeploymentBase):
    def test_syslog_in_router_configs_and_bandwidth_change(self):
        import yaml
        from django.urls import reverse
        from inventory.networks import import_site
        import_site("dc1", {"cloud-ix": yaml.safe_load(InfraAndNetworks.IX_YAML)})
        from inventory import nfconfig
        from inventory.models import Site
        from tenants.models import Connectivity, NetworkRequest
        Site.objects.filter(code="dc1").update(log_collectors={"vyos": {"host": "10.0.9.9", "proto": "udp", "port": 514}})
        egw = nfconfig.build("egw", tenant=self.t, reveal=False)
        self.assertIn("set system syslog remote 10.0.9.9 port 514", egw)
        self.assertIn("set system syslog remote 10.0.9.9 vrf MGMT", egw)  # per remote (VyOS 2026.03)
        self.assertNotIn("set system syslog vrf", egw)
        igw = nfconfig.build("igw", tenant=self.t, reveal=False)
        self.assertNotIn("syslog remote", igw)  # the tenant's router
        from accounts.models import Kind, User
        sa = User.objects.create_user("sa2@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_super_admin=True,
                                      email_verified=True, totp_secret="JBSWY3DPEHPK3PXP", totp_enabled=True)
        self.client.force_login(sa)
        self.client.post(reverse("tenant_network_request", args=[self.t.pk]), {"kind": "inet", "action": "bandwidth", "mbps": "5"})
        self.assertFalse(NetworkRequest.objects.exists())  # 10–1000
        self.client.post(reverse("tenant_network_request", args=[self.t.pk]), {"kind": "inet", "action": "bandwidth", "mbps": "250"})
        req = NetworkRequest.objects.get()
        self.assertEqual((req.mbps, req.target), (250, "100 Mbps"))
        from types import SimpleNamespace
        from tenants import netops
        done = []
        fake = SimpleNamespace(tenant=self.t, site="dc1", req=req, user=sa, base="x", gw=lambda role: f"x_{role}",
                               step=lambda name, fn: done.append((name, fn())), _vm_exists=lambda name: 7,
                               _router=lambda role: f"{role} changed")
        netops.NetChange.bandwidth(fake)
        self.assertEqual([n for n, _r in done], ["record", "egw", "group"])  # correction 17: the group template follows
        self.assertEqual(Connectivity.current_for(self.t)[0].inet_mbps, 250)


class ImportedTenantTakeover(DeploymentBase):
    """An imported tenant: gateways made again on the same networks and addresses; igw from its uploaded configuration."""

    IGW_CONFIG = """set interfaces ethernet eth0 address '203.0.113.21/31'
set interfaces ethernet eth0 hw-id '02:00:aa:bb:cc:01'
set interfaces ethernet eth1 address '10.10.0.1/24'
set nat source rule 10 outbound-interface name 'eth0'
set nat source rule 10 source address '10.10.0.0/24'
set nat source rule 10 translation address 'masquerade'
set protocols static route 0.0.0.0/0 next-hop 203.0.113.20
set service ssh port '22'
set system host-name 'old-igw'
set system login user alice authentication public-keys alice key 'AAAAC3alice'
set system login user alice authentication public-keys alice type 'ssh-ed25519'
"""

    def setUp(self):
        super().setUp()
        from tenants.models import Connectivity, PrefixAssignment, QuotaRecord, Tenant
        self.a = Tenant.objects.create(organization=self.t.organization, name="alpha", title="alpha", kind="cloud",
                                       approval="approved", group_name="tenant-alpha", origin="adopted")
        QuotaRecord.objects.create(tenant=self.a, site="dc1", cluster="ca1", version=1, cpu=4, ram_gb=8, gpu=0, disk_gb=50,
                                   images_gb=0, files_gb=0, backups_gb=0)
        Connectivity.objects.create(tenant=self.a, site="dc1", version=1, connection_type="inet", inet_mbps=100,
                                    inet_main_prefix=31)
        PrefixAssignment.objects.create(tenant=self.a, site="dc1", kind="inet_main", cidr="203.0.113.20/31")

    def _fake(self):
        import re
        import xml.sax.saxutils as su
        gone = set()
        vms = {50: ["tenant_alpha-egw_dc1", [(0, "cloud-ix_dc1", "10.0.10.15"), (7, "tenant_alpha-main_dc1", "203.0.113.20")], 0, 0, "3"],
               51: ["tenant_alpha-igw_dc1", [(7, "tenant_alpha-main_dc1", "203.0.113.21"), (8, "tenant_alpha-lan1_dc1", "10.10.0.1")], 410, 310, "3"]}
        names = {0: "cloud-ix_dc1", 3: "cloud-vyos_dc1", 7: "tenant_alpha-main_dc1", 8: "tenant_alpha-lan1_dc1"}

        class Fake(FakeClient):
            vm_table = vms

            def _call(self, method, *args):
                if method == "one.vmpool.info":
                    self.calls.append((method, args))
                    return "<VM_POOL>" + "".join(f"<VM><ID>{i}</ID><NAME>{v[0]}</NAME></VM>" for i, v in vms.items() if i not in gone) + "</VM_POOL>"
                if method == "one.vm.info":
                    self.calls.append((method, args))
                    i = int(args[0])
                    if i in gone or i not in vms:
                        return f"<VM><ID>{i}</ID><STATE>6</STATE><LCM_STATE>0</LCM_STATE></VM>"
                    name, nics, uid, gid, state = vms[i]
                    n = "".join(f"<NIC><NIC_ID>{k}</NIC_ID><NETWORK_ID>{a}</NETWORK_ID><NETWORK>{su.escape(b)}</NETWORK><IP>{c}</IP></NIC>"
                                for k, (a, b, c) in enumerate(nics) if a is not None)
                    return (f"<VM><ID>{i}</ID><NAME>{su.escape(name)}</NAME><UID>{uid}</UID><GID>{gid}</GID><STATE>{state}</STATE>"
                            f"<LCM_STATE>3</LCM_STATE><TEMPLATE><VCPU>2</VCPU><MEMORY>2048</MEMORY>{n}</TEMPLATE></VM>")
                if method == "one.vm.action":
                    self.calls.append((method, args))
                    i = int(args[1])
                    if args[0] in ("poweroff", "poweroff-hard"):
                        vms[i][4] = "8"
                    elif args[0] == "resume":
                        vms[i][4] = "3"
                    else:
                        gone.add(i)
                    return i
                if method == "one.vm.rename":
                    self.calls.append((method, args))
                    vms[int(args[0])][0] = args[1]
                    return int(args[0])
                if method == "one.vm.detachnic":
                    self.calls.append((method, args))
                    i, k = int(args[0]), int(args[1])
                    vms[i][1][k] = (None, "", "")  # the NIC ids of the others stay
                    return i
                if method == "one.vm.attachnic":
                    self.calls.append((method, args))
                    nid = int(re.search(r'NETWORK_ID = "(\d+)"', args[1]).group(1))
                    ip = re.search(r'IP = "([^"]+)"', args[1]).group(1)
                    vms[int(args[0])][1].append((nid, names.get(nid, ""), ip))
                    return int(args[0])
                if method == "one.vm.allocate":
                    vm_id = super()._call(method, *args)
                    t = args[0]
                    nics = [(int(m.group(1)), names.get(int(m.group(1)), ""), m.group(2)) for m in
                            re.finditer(r'NIC = \[\n  NETWORK_ID = "(-?\d+)",\n  IP = "([^"]*)"', t)]
                    vms[int(vm_id)] = [re.search(r'^NAME = "([^"]+)"', t, re.M).group(1), nics, 0, 0, "3"]
                    return vm_id
                if method == "one.vnpool.info":
                    self.calls.append((method, args))
                    return POOLS[method].replace("</VNET_POOL>", "<VNET><ID>7</ID><NAME>tenant_alpha-main_dc1</NAME></VNET></VNET_POOL>")
                if method == "one.grouppool.info":
                    self.calls.append((method, args))
                    return POOLS[method].replace("</GROUP_POOL>", "<GROUP><ID>310</ID><NAME>tenant-alpha</NAME></GROUP></GROUP_POOL>")
                return super()._call(method, *args)
        return Fake()

    def _go(self, what):
        from tenants import takeover
        fake = self._fake()
        # the router's answer to the read (as a route reflector's: inventory.core_services.READ_SCRIPT + BGP)
        bgp = '##host\negw-romashka-alpha-dc1\n##os\nVyOS 2026.03\n##bgp\n{"ipv4Unicast": {"peers": {"10.0.10.250": {"state": "Established"}}}}\n'
        with secrets_loaded(self.secrets), mock.patch("adoption.collector.client_for", return_value=fake), \
                mock.patch("tenants.deploy.crypt_sha512", return_value="$6$x$y"), \
                mock.patch("tenants.netops._poll", return_value=0), mock.patch("tenants.takeover.Takeover.CHECK_TIMEOUT", 0), \
                mock.patch("inventory.sshexec.run", return_value=bgp) as ssh:
            dep = takeover.start(self.a, what, None)
        self.ssh = ssh
        return dep, fake

    def test_preview_reads_only_and_shows_both_configurations(self):
        from django.urls import reverse
        from accounts.models import Kind, User
        from tenants import takeover
        import yaml
        from inventory.networks import import_site
        import_site("dc1", {"cloud-ix": yaml.safe_load(InfraAndNetworks.IX_YAML)})
        fake = self._fake()
        running = "set protocols bgp system-as '4200101002'\nset protocols bgp peer-group cloud-int password 'Old-Secret-1'\n" \
                  "set system host-name 'old-egw'\n"
        with secrets_loaded(self.secrets), mock.patch("adoption.collector.client_for", return_value=fake), \
                mock.patch("inventory.credentials.account", return_value=("opsuser", {"key": "KEY"})), \
                mock.patch("inventory.sshexec._once", return_value=running) as ssh:
            rows = takeover.preview(self.a)
            sa = User.objects.create_user("sa@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_super_admin=True,
                                          email_verified=True, totp_secret="JBSWY3DPEHPK3PXP", totp_enabled=True)
            self.client.force_login(sa)
            s = self.client.session
            s["otp_verified"] = True
            s.save()
            page = self.client.get(reverse("takeover_preview", args=[self.a.pk]))
        egw = rows[0]
        self.assertEqual((egw["role"], egw["old"]["id"], egw["new_name"]), ("egw", 50, "_romashka_alpha_egw_dc1"))
        ssh.assert_not_called()  # correction 11: the old gateways' configuration is not read
        self.assertIn("set protocols bgp system-as", egw["new"], egw["new_error"])
        self.assertFalse([m for m, _a in fake.calls if m not in ("one.vmpool.info", "one.vm.info", "one.vnpool.info",
                                                                  "one.grouppool.info", "one.clusterpool.info")])
        self.assertContains(page, "Switch: redeploy egw / cgw / L2 gateways")
        self.assertNotContains(page, "Old-Secret-1")

    def test_egw_redeploy_rolls_back_when_the_new_one_is_not_ready(self):
        """Correction 11: the new egw never gets its BGP peers up — it is removed, the old one comes back as it was."""
        from tenants import takeover
        fake = self._fake()
        bgp = '##host\negw\n##bgp\n{"ipv4Unicast": {"peers": {"10.0.10.250": {"state": "Active"}}}}\n'
        with secrets_loaded(self.secrets), mock.patch("adoption.collector.client_for", return_value=fake), \
                mock.patch("tenants.deploy.crypt_sha512", return_value="$6$x$y"), \
                mock.patch("tenants.netops._poll", return_value=0), mock.patch.object(takeover.Takeover, "CHECK_TIMEOUT", 0), \
                mock.patch("inventory.sshexec.run", return_value=bgp):
            dep = takeover.start(self.a, "gateways", None)
        self.assertEqual(dep.state, "failed")
        self.assertIn("not ready", dep.error)
        new_id = next(int(r) for m, a in fake.calls if m == "one.vm.allocate" for r in [600])
        self.assertIn(("one.vm.action", ("terminate-hard", new_id)), fake.calls)
        self.assertNotIn(("one.vm.action", ("terminate-hard", 50)), fake.calls)
        name, nics, _u, _g, state = fake.vm_table[50]
        self.assertEqual((name, state), ("tenant_alpha-egw_dc1", "3"))  # its own name, running
        self.assertIn((7, "tenant_alpha-main_dc1", "203.0.113.20"), nics)  # the main address back
        self.assertEqual([s["status"] for s in dep.steps if s["name"] == "egw rollback"], ["ok"])

    def test_gateways_igw_and_no_deploy(self):
        from django.urls import reverse
        from accounts.models import Kind, User
        dep, fake = self._go("gateways")
        self.assertEqual((dep.state, dep.error), ("done", ""), dep.steps)
        self.a.refresh_from_db()
        self.assertEqual((self.a.order["platform"], self.a.one_group_id), ("dc1.cloud", 310))
        self.assertIn(("one.vm.action", ("terminate-hard", 50)), fake.calls)
        egw = [a[0] for m, a in fake.calls if m == "one.vm.allocate"][0]
        # correction 11: the new standard name and host name; a new exchange address, the main address kept
        self.assertIn('NAME = "_romashka_alpha_egw_dc1"', egw)
        self.assertIn('SET_HOSTNAME = "egw-romashka-alpha-dc1"', egw)
        self.assertIn('AUTOSTART = "always"', egw)  # a service VM (correction 12)
        self.assertIn('SCHED_REQUIREMENTS = "HYPERVISOR=kvm"', egw)
        self.assertIn('MEMORY_RESIZE_MODE = "BALLOONING"', egw)
        self.assertNotIn('IP = "10.0.10.15"', egw)                # not its old exchange address
        self.assertIn('IP = "203.0.113.20"', egw)                 # its old main address
        order = [(m, a[:2]) for m, a in fake.calls if m in ("one.vm.action", "one.vm.detachnic", "one.vm.rename", "one.vm.allocate")]
        self.assertEqual(order[:3], [("one.vm.action", ("poweroff", 50)), ("one.vm.detachnic", (50, 1)),
                                     ("one.vm.rename", (50, "_romashka_alpha_egw_dc1-old"))])  # the old one aside first
        self.assertEqual(order[-1], ("one.vm.action", ("terminate-hard", 50)))  # removed after the check
        remote = self.ssh.call_args[0][4]  # the check, as a route reflector's: the read and BGP over VRF MGMT
        self.assertIn("echo '##addr'", remote)
        self.assertIn("vtysh -c 'show bgp summary json'", remote)
        self.a.refresh_from_db()
        st = self.a.gw_status["egw"]  # stored after the redeploy: the card shows it
        self.assertTrue(st["ok"], st)
        self.assertEqual([c["check"] for c in st["checks"]], ["answers ssh on the management address (VRF MGMT)",
                                                              "BGP (ipv4) peers established"])
        self.assertEqual(self.ssh.call_args[0][1], 55522)
        self.assertIn("egw check", [s["name"] for s in dep.steps])
        self.assertNotIn("one.group.allocate", [m for m, _a in fake.calls])  # the tenant's resources are not touched
        self.assertNotIn(("one.vm.action", ("terminate-hard", 51)), fake.calls)  # the igw stays
        # igw: a configuration that does not fit is refused and the igw marked not adopted
        sa = User.objects.create_user("sa@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_super_admin=True,
                                      email_verified=True, totp_secret="JBSWY3DPEHPK3PXP", totp_enabled=True)
        self.client.force_login(sa)
        with secrets_loaded(self.secrets), mock.patch("adoption.collector.client_for", return_value=self._fake()):
            self.client.post(reverse("takeover_igw_config", args=[self.a.pk]),
                             {"text": self.IGW_CONFIG + "set interfaces openvpn vtun0 local-port 1194\n"})
            self.a.refresh_from_db()
            self.assertEqual(self.a.igw_adoption, "not_adopted")
            self.assertIn("openvpn", self.a.igw_adoption_note)
            self.client.post(reverse("takeover_igw_config", args=[self.a.pk]), {"text": self.IGW_CONFIG})
        self.a.refresh_from_db()
        self.assertEqual(self.a.igw_adoption, "ready")
        self.assertNotIn("AAAAC3alice", self.a.igw_config_encrypted)  # kept encrypted
        dep, fake = self._go("igw")
        self.assertEqual((dep.state, dep.error), ("done", ""), dep.steps)
        igw = [a[0] for m, a in fake.calls if m == "one.vm.allocate"][0]
        self.assertIn('NAME = "_romashka_alpha_igw_dc1"', igw)  # correction 11
        self.assertIn('IP = "203.0.113.21"', igw)
        self.assertIn('IP = "10.10.0.1"', igw)
        self.assertIn('USERNAME = "alice"', igw)
        self.assertIn(("one.vm.chown", (600, 410, 310)), [(m, a) for m, a in fake.calls if m == "one.vm.chown"])
        self.a.refresh_from_db()
        self.assertEqual(self.a.igw_adoption, "adopted")
        r = self.client.get(reverse("tenant_detail", args=[self.a.pk]))
        self.assertNotContains(r, "Imported tenant: switch to the portal")  # section removed (2026-10-29)
        self.assertNotContains(r, reverse("tenant_deploy", args=[self.a.pk]))  # no deploy for imported tenants


class IgwVmFound(DeploymentBase):
    """2026-10-29: the igw VM is the one holding the address after the egw's on the main subnet; its image gives its type."""

    def test_detect_by_address(self):
        from tenants.igwdetect import addresses, detect
        from tenants.models import Connectivity
        self.assertEqual(addresses(self.t), ("203.0.113.16", "203.0.113.17"))
        dep, fake = self._run(dry=False)
        Connectivity.objects.filter(tenant=self.t).update(igw_type="")  # as for the imported tenants
        pool = ("<VM_POOL>"
                "<VM><ID>601</ID><NAME>tenant_x-egw_dc1</NAME><GNAME>oneadmin</GNAME><STATE>3</STATE><LCM_STATE>3</LCM_STATE>"
                "<TEMPLATE><NIC><IP>203.0.113.16</IP></NIC><DISK><IMAGE>vyos-2026.03</IMAGE></DISK></TEMPLATE></VM>"
                "<VM><ID>602</ID><NAME>old-router-name</NAME><GNAME>_t_romashka_gpulab</GNAME><STATE>3</STATE><LCM_STATE>3</LCM_STATE>"
                "<TEMPLATE><VCPU>1</VCPU><MEMORY>1536</MEMORY><NIC><NETWORK>_t_romashka_gpulab-main_dc1</NETWORK><IP>203.0.113.17</IP></NIC>"
                "<NIC><NETWORK>lan</NETWORK><IP>10.0.0.1</IP></NIC><DISK><IMAGE>vyos-1.4-tenant</IMAGE><SIZE>2048</SIZE></DISK></TEMPLATE></VM></VM_POOL>")
        real = fake._call
        fake._call = lambda method, *a: pool if method == "one.vmpool.info" else real(method, *a)
        with secrets_loaded(self.secrets), mock.patch("adoption.collector.client_for", return_value=fake):
            found = detect([self.t])
        t, vm, note = found[0]
        self.assertIsNotNone(vm, note)
        self.assertEqual((vm["ip"], vm["type"], vm["name"], vm["id"]), ("203.0.113.17", "vyos", "old-router-name", 602))
        self.t.refresh_from_db()
        self.assertEqual(self.t.igw_vm["name"], vm["name"])
        self.assertEqual(Connectivity.current_for(self.t)[0].igw_type, "vyos")
        # owner 2026-10-29 20:59: whole GB and the NICs, one per line in the igw details
        self.assertEqual((vm["ram_gb"], vm["disks_gb"], vm["nics"]),
                         (2, [2], [["_t_romashka_gpulab-main_dc1", "203.0.113.17"], ["lan", "10.0.0.1"]]))
        from django.template import Template, Context
        self.assertEqual(Template("{% load ui %}{{ a|gb }} {{ b|gb }} {{ c|gb }}").render(Context({"a": 1.0, "b": 15.6, "c": None})),
                         "1 16 None")


class OrganizationWithUser(TestCase):
    """Correction 14: a new organization gets its user at once — an existing one without an organization, or a new one
    with name, email and a phone in international format."""

    def test_new_organization_needs_its_user(self):
        from django.urls import reverse
        from accounts.models import Kind, User
        from accounts.tests import valid_bin
        from tenants.models import Organization
        rm = User.objects.create_user("rm@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_resource_manager=True,
                                      email_verified=True)
        self.client.force_login(rm)
        rep = User.objects.create_user("rep@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_sales=True, email_verified=True)
        se = User.objects.create_user("se@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_support=True, email_verified=True)
        base = {"name": "ТОО Ромашка", "kind": "cloud", "tax_id_type": "bin", "tax_id": valid_bin()}
        url = reverse("org_new")
        r = self.client.post(url, {**base, "user_name": "Asel Nurlanova", "user_email": "asel@romashka.kz", "user_phone": "+77011234567"})
        self.assertContains(r, "needs its sales rep")  # owner 2026-10-29: the platform users are required (KAM optional)
        self.assertFalse(Organization.objects.exists())
        base.update(sales_rep=rep.pk, resource_manager=rm.pk, support_engineer=se.pk)
        r = self.client.post(url, base)
        self.assertContains(r, "Enter the user&#x27;s full name")
        self.assertFalse(Organization.objects.exists())
        r = self.client.post(url, {**base, "user_name": "Asel Nurlanova", "user_email": "asel@romashka.kz", "user_phone": "701 123 45"})
        self.assertContains(r, "international format")
        from accounts.kz import format_phone, normalize_intl_phone
        self.assertEqual(normalize_intl_phone("8 701 234 56 78"), "+77012345678")  # owner 2026-10-29: the standard
        self.assertEqual(format_phone("+77012345678"), "+7 701 2345678")
        self.client.post(url, {**base, "user_name": "Asel Nurlanova", "user_email": "asel@romashka.kz", "user_phone": "+7 701 123-45-67"})
        org = Organization.objects.get()
        u = User.objects.get(email="asel@romashka.kz")
        self.assertEqual((u.organization, u.is_org_admin, u.phone, u.login_enabled, u.kind), (org, True, "+77011234567", False, "customer"))
        free = User.objects.create_user("free@x.kz", None, kind=Kind.CUSTOMER, full_name="Free User", phone="+491701234567")
        self.client.post(url, {**base, "name": "Other", "tax_id": valid_bin("0205400012"), "org_user": str(free.pk)})
        free.refresh_from_db()
        self.assertEqual((free.organization.name, free.is_org_admin), ("Other", True))


class DialogStaysOpen(TestCase):
    """Owner 2026-10-29 12:51: an action in the igw dialog, posted from the tenant shown under the list, comes back to
    the list with the dialog to open (olist.js hands it to the selected row's details)."""

    def test_keep_dialog(self):
        from accounts.middleware import keep_dialog
        self.assertEqual(keep_dialog("/projects/?sel=t-88", "/projects/88/?igw=configs#connectivity"),
                         "/projects/?sel=t-88&igw=configs")
        self.assertEqual(keep_dialog("/projects/?sel=t-88&igw=details", "/projects/88/?igw=configs"),
                         "/projects/?sel=t-88&igw=configs")
        self.assertEqual(keep_dialog("/projects/?sel=t-88", "/projects/88/#connectivity"), "/projects/?sel=t-88")

    def test_background_form_returns_partial(self):
        """Owner 2026-10-29 13:06: the igw dialogs send their forms in the background (X-Partial): the redirect goes to
        the partial page (its messages first), never to the list."""
        from accounts.middleware import with_partial
        self.assertEqual(with_partial("/projects/88/?igw=configs#connectivity"), "/projects/88/?igw=configs&partial=1")
        self.assertEqual(with_partial("/projects/88/?partial=1"), "/projects/88/?partial=1")


class Correction15(DeploymentBase):
    """Correction 15: organization users made by staff, the tenant admin, one Edit dialog, resource pools, the
    provisioning e-mails."""

    def _staff(self, **roles):
        from accounts.models import Kind, User
        return User.objects.create_user(f"{'-'.join(roles) or 'su'}@provider.example", "Str0ng-PASS-123", kind=Kind.STAFF,
                                        email_verified=True, **(roles or {"is_super_admin": True}))

    def test_org_users_tenant_admin_and_edit_dialog(self):
        from django.urls import reverse
        from accounts.models import User
        from tenants.models import Membership
        rm = self._staff(is_resource_manager=True)
        self.client.force_login(rm)
        org = self.t.organization
        url = reverse("org_user_add", args=[org.pk])
        self.client.post(url, {"ou-full_name": "Султан Сайлаубекулы", "ou-email": "a.user@client2.example",
                               "ou-phone": "+7 701 2345678", "ou-city": "almaty", "ou-is_org_admin": "on"})
        u = User.objects.get(email="a.user@client2.example")
        self.assertEqual(u.city, "almaty")  # owner 2026-10-29 20:24: a city for every user
        self.assertEqual((u.organization, u.is_org_admin, u.phone, u.phone_display, u.login_enabled),
                         (org, True, "+77012345678", "+7 701 2345678", False))
        self.t.refresh_from_db()
        self.assertEqual(self.t.tenant_admin, u)  # its tenant had no admin: the new organization user is (owner 2026-10-29)
        r = self.client.post(url, {"ou-full_name": "X Y", "ou-email": "x@y.kz", "ou-phone": "123"}, follow=True)
        self.assertContains(r, "international format")
        self.assertContains(self.client.get(reverse("org_detail", args=[org.pk])), "+7 701 2345678")
        # u05: the Edit button links to the edit page; the tenant admin and the stage change together there
        page = self.client.get(reverse("tenant_detail", args=[self.t.pk])).content.decode()
        self.assertIn(reverse("tenant_edit_all", args=[self.t.pk]), page)
        self.assertNotIn('id="dlg-t-stage"', page)
        r = self.client.post(reverse("tenant_edit_all", args=[self.t.pk]), {"ta-user": str(u.pk), "st-stage": "client"}, follow=True)
        self.t.refresh_from_db()
        self.assertEqual((self.t.tenant_admin, self.t.stage), (u, "client"))
        self.assertTrue(Membership.objects.filter(user=u, tenant=self.t, role="tenant_admin").exists())
        r = self.client.post(reverse("tenant_edit_all", args=[self.t.pk]), {"st-stage": "client"}, follow=True)
        self.assertContains(r, "Nothing changed")

    def test_a_new_site_carries_the_subnet_and_gateway_type_that_were_ordered(self):
        """Owner 2026-11-09: the pool dialog had no base-subnet selector and no gateway type, unlike the project
        order form, so a new site could only ever get an auto subnet and the project's gateway type."""
        from django.urls import reverse
        from tenants.models import Connectivity, ResourceChange
        su = self._staff()
        self.client.force_login(su)
        page = self.client.get(reverse("tenant_detail", args=[self.t.pk])).content.decode()
        dlg = page[page.index('id="dlg-pool-order"'):]
        dlg = dlg[:dlg.index("</dialog>")]
        for field in ("po-inet_main_pool", "po-igw_type", "po-inet_main_prefix"):
            self.assertIn(field, dlg)          # the dialog offers what the project order form offers

        ch = ResourceChange.objects.create(
            tenant=self.t, site="dc3", cluster="ca8", cpu=4, ram_gb=8, disk_gb=50, requested_by=su,
            pool={"kind": "new_site", "cluster_name": "cpu_dc3",
                  "connectivity": {"types": ["inet"], "inet_mbps": 50, "inet_main_prefix": 29,
                                   "inet_main_pool": "ix-2", "igw_type": "onevr"}})
        self.client.post(reverse("tenant_resource_decide", args=[self.t.pk, ch.pk, "approve"]))
        c = Connectivity.objects.get(tenant=self.t, site="dc3")
        self.assertEqual((c.inet_mbps, c.inet_main_prefix, c.igw_type), (50, 29, "onevr"))

    def test_pools_order_new_site_and_remove(self):
        from django.urls import reverse
        from django.utils import timezone
        from tenants.models import Connectivity, QuotaRecord, ResourceChange
        su = self._staff()
        self.client.force_login(su)
        rec = self.t.current_quota_records()[0]
        # a pool on a new site, approved: its quota record and the connectivity of the site
        ch = ResourceChange.objects.create(tenant=self.t, site="dc2", cluster="ca9", cpu=4, ram_gb=8, disk_gb=50,
                                           requested_by=su, pool={"kind": "new_site", "cluster_name": "cpu_dc2",
                                                                  "connectivity": {"types": ["inet"], "inet_mbps": 50, "inet_main_prefix": 31}})
        self.client.post(reverse("tenant_resource_decide", args=[self.t.pk, ch.pk, "approve"]))
        self.assertEqual(sorted((r.site, r.cluster) for r in self.t.current_quota_records()), [(rec.site, rec.cluster), ("dc2", "ca9")])
        c = Connectivity.objects.get(tenant=self.t, site="dc2")
        self.assertEqual((c.connection_type, c.inet_mbps, c.inet_main_prefix), ("inet", 50, 31))
        # removing a pool: approved, the record is removed; the last one cannot be removed
        r = self.client.post(reverse("tenant_pool_remove", args=[self.t.pk]), {"site": "dc2", "cluster": "ca9"})
        ch = ResourceChange.objects.get(tenant=self.t, state="pending")
        self.assertEqual(ch.pool["kind"], "remove")
        self.client.post(reverse("tenant_resource_decide", args=[self.t.pk, ch.pk, "approve"]))
        self.assertEqual([(r.site, r.cluster) for r in self.t.current_quota_records()], [(rec.site, rec.cluster)])
        self.assertTrue(QuotaRecord.objects.filter(tenant=self.t, site="dc2", removed=True).exists())
        r = self.client.post(reverse("tenant_pool_remove", args=[self.t.pk]), {"site": rec.site, "cluster": rec.cluster}, follow=True)
        self.assertContains(r, "last resource pool")
        page = self.client.get(reverse("tenant_detail", args=[self.t.pk])).content.decode()
        self.assertIn("Resource pools", page)
        self.assertIn('data-open="dlg-pool-order"', page)
        self.assertIn("shared-std-1_dc1", page)  # the cluster's full name, never its code

    def test_ready_mails(self):
        from django.core import mail
        from accounts.models import Kind, User
        from tenants.notices import send_ready
        org = self.t.organization
        admin = User.objects.create_user("adm@romashka.kz", None, kind=Kind.CUSTOMER, organization=org,
                                         full_name="Aigerim S", login_enabled=False)  # no login: still gets them
        rep = User.objects.create_user("rep@provider.example", "Str0ng-PASS-123", kind=Kind.STAFF, is_sales=True,
                                       full_name="Marat O", phone="+77010000001", email_verified=True,
                                       login_enabled=False)  # a directory sales rep: still notified (owner 2026-10-29)
        org.sales_rep = rep
        org.save()
        from tenants.admins import set_tenant_admin
        set_tenant_admin(self.t, admin)
        self.t.igw_ssh_port = 52601
        self.t.set_igw_password("Qz8-Gate+2026")
        self.t.save()
        n = send_ready(self.t)
        self.assertEqual(n, 2)
        a = next(m for m in mail.outbox if m.to == ["adm@romashka.kz"])
        s = next(m for m in mail.outbox if m.to == ["rep@provider.example"])
        self.assertIn("Kx7-Pass+2026", a.body)
        self.assertIn("ssh gpulab-igw-admin@203.0.113.17 -p 52601", a.body)
        self.assertIn("Marat O <rep@provider.example>\n", a.body)  # no phones, no group name in e-mails
        self.assertNotIn("_t_romashka_gpulab", a.body + a.subject)
        self.assertIn("shared-std-1_dc1", a.body)
        self.assertNotIn("Kx7-Pass+2026", s.body)
        self.assertIn("You are its sales rep.", s.body)
        self.assertNotIn("Gateway", s.body + s.alternatives[0][0])  # the gateway only for the tenant admin
        self.assertNotIn("/projects/", a.body + a.alternatives[0][0] + s.body)  # no portal link yet (owner 2026-10-29)
        html = a.alternatives[0][0]  # owner 2026-10-29: HTML e-mails with lists; links show their address
        self.assertIn("<ul", html)
        self.assertIn("<li>Gateway ssh:", html)
        self.assertNotIn("Kx7-Pass+2026", s.alternatives[0][0])
        self.assertIn("The credentials were sent to the project admin: Aigerim S <adm@romashka.kz>", s.body)
        # owner 2026-10-30 21:02: nobody in copy of the tenant admin's e-mail (credentials); one e-mail per platform
        # user even with several roles; a client may get it while client mail is off only when allowed
        mail.outbox.clear()
        org.kam = rep
        org.save()
        send_ready(self.t, by=rep, allow_client=True)
        a = next(m for m in mail.outbox if m.to == ["adm@romashka.kz"])
        self.assertEqual(a.cc, [])
        self.assertEqual(a.bcc, [])
        self.assertEqual(a.extra_headers.get("X-Portal-Allow-Client"), "1")
        reps = [m for m in mail.outbox if m.to == ["rep@provider.example"]]
        self.assertEqual(len(reps), 1)
        self.assertIn("You are its sales rep and key account manager.", reps[0].body)
        # owner 2026-10-29: the platform users are told only after the tenant admin's e-mail went out
        mail.outbox.clear()
        with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=[0, 1, 1]):
            self.assertEqual(send_ready(self.t, by=rep), 0)
        from accounts.models import AuditLog
        self.assertTrue(AuditLog.objects.filter(action="tenant_ready_staff_skipped").exists())
        mail.outbox.clear()
        send_ready(self.t, hide=True, override_to=["review@provider.example"], subject_prefix="[review] ")
        self.assertTrue(all(m.to == ["review@provider.example"] for m in mail.outbox))
        self.assertNotIn("Kx7-Pass+2026", "".join(m.body for m in mail.outbox))


class IgwSize(DeploymentBase):
    """Owner 2026-10-29 18:58: the igw's vCPU and RAM (1 / 1 GB by default) at creation and later."""

    def test_size_kept_and_used(self):
        from unittest import mock as _m
        from tenants import igwsize
        from tenants.provision import ProvisionForm
        t = self.t
        self.assertEqual((t.igw_cpu, t.igw_ram_gb), (1, 1))
        f = ProvisionForm(tenant=t, prefix="pv")
        if "igw_cpu" in f.fields:
            self.assertEqual((f.fields["igw_cpu"].initial, f.fields["igw_ram_gb"].initial), (1, 1))
        with _m.patch("tenants.igwsize._vm", return_value=(None, None)):
            self.assertIn("kept: 2 vCPU, 4 GB", igwsize.resize(t, 2, 4))
        t.refresh_from_db()
        self.assertEqual((t.igw_cpu, t.igw_ram_gb), (2, 4))
        with self.assertRaises(igwsize.DeployError):
            igwsize.resize(t, 0, 4)

    def test_resize_running_needs_a_restart_when_live_fails(self):
        from unittest import mock as _m
        from tenants import igwsize
        import xml.etree.ElementTree as ET

        class One:
            def __init__(self):
                self.calls, self.state = [], "3"

            def xml(self, method, vm_id, _x):
                return ET.fromstring(f"<VM><STATE>{self.state}</STATE><LCM_STATE>{'3' if self.state == '3' else '0'}</LCM_STATE></VM>")

            def call(self, method, *a):
                self.calls.append((method, a[0] if a else None))
                if method == "one.vm.resize" and self.state == "3":
                    raise igwsize.DeployError("[one.vm.resize] wrong state")
                if method == "one.vm.action":
                    self.state = "8" if a[0] == "poweroff" else "3"
        one = One()
        with _m.patch("tenants.igwsize._vm", return_value=(one, 42)):
            with self.assertRaises(igwsize.DeployError):
                igwsize.resize(self.t, 2, 2)
            out = igwsize.resize(self.t, 2, 2, restart=True)
        self.assertIn("powered off, resized and started again", out)
        self.assertEqual([m for m, _a in one.calls][-3:], ["one.vm.action", "one.vm.resize", "one.vm.action"])
        self.t.refresh_from_db()
        self.assertEqual((self.t.igw_vm.get("cpu"), self.t.igw_vm.get("ram_gb")), (2, 2))
