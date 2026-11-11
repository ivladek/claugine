"""The adoption app: the collection from the platforms and staff regrouping.
"""

import pyotp
from inventory.testing import secrets_loaded
from cryptography.fernet import Fernet
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Kind, User
from adoption.collector import xml_to_dict
from inventory import testing_tree as inv

from tenants.models import Organization, PrefixAssignment, Tenant

FILES = {
    "providers/acme.yaml": "name: ACME\nid: acme\n",
    "server_profiles/std1.yaml": "CPU:\n  sockets: 2\n  cores: 32\nRAM:\n  total_gb: 1536\n",
    "server_profiles/gpu1.yaml": "CPU:\n  sockets: 2\n  cores: 32\nRAM:\n  total_gb: 1536\n"
                                 "gpus:\n  model: L40S\n  quantity: 4\n  hw:\n    vram_gb: 48\n",
    "resources/dc1.yaml": "location:\n  country: KZ\n  city: Atyrau\n  dc: 1\n",
    "resources/dc1/hosts/h1.yaml": "server_profile: server_profiles.std1\nip: 10.0.0.1\n",
    "resources/dc1/hosts/h2.yaml": "server_profile: server_profiles.std1\nip: 10.0.0.2\n",
    "resources/dc1/hosts/g1.yaml": "server_profile: server_profiles.gpu1\nip: 10.0.0.3\n",
    "resources/dc1/hosts/m1.yaml": "server_profile: server_profiles.std1\nip: 10.0.0.9\n",
    "resources/dc1/networks/cloud-ix.yaml": "vlan: 10\nsubnet: 10.82.10.0\nprefix: 24\nranges:\n  egw:\n    start: 10.82.10.10\n    len: 20\n"
                                            "bgp:\n  subnets:\n    - 198.19.182.0/25\n",
    "resources/dc1/networks/corpnet-ix.yaml": "vlan: 104\nsubnet: 10.82.103.0\nprefix: 24\nbgp:\n  subnets:\n    - 10.82.104.0/22\n",
    "platforms/dc1/cloud/platform.yaml": "provider: providers.acme\nkind: shared\nsa: secrets.acme.sa\n",
    "platforms/dc1/cloud/fe.yaml": "name: one_dc1\nmode: local\ntype: vm\nnodes: [a, b, c]\nvip: 10.82.101.30\nsecrets: secrets.acme.one\n",
    "platforms/dc1/cloud/clusters.yaml": (
        "shared-std-1_dc1:\n  kind: shared\n  quota_code: ca1\n  hosts:\n    - resources.dc1.hosts.h1\n    - resources.dc1.hosts.h2\n"
        "shared-gpu-1_dc1:\n  kind: shared\n  quota_code: ga1\n  hosts:\n    - resources.dc1.hosts.g1\n"),
    "platforms/dc1/cloud/networks.yaml": "cloud-ix_dc1:\n  type: vlan\n  network: resources.dc1.networks.cloud-ix\n  ranges:\n    - egw\n",
    "resources/dc1/storages/nas-1.yaml": "type: nfs\nparameters: nfs4 _netdev,rw 0 0\ntargets:\n  - 10.82.111.240\n"
                                         "volumes:\n  cloud-vms-1:\n    dev: /cloud_vms\n    size_gb: 10240\n"
                                         "    clients:\n      - 10.82.111.0/24\n",
    "resources/dc1/storages/local/g1.yaml": "type: local\nparameters: xfs defaults 0 1\nvolumes:\n  local-1:\n"
                                            "    dev: /sda\n    size_gb: 2000\n    mount: /var/lib/one/datastores\n",
    "platforms/dc1/cloud/datastores.yaml": "cloud-vms01_dc1:\n  type: vms\n  clusters:\n    - shared-std-1_dc1\n"
                                           "  volume: resources.dc1.storages.nas-1.volumes.cloud-vms-1\n"
                                           "cloud-backups01_dc1:\n  type: restic\n  volume: resources.dc1.storages.local.g1.volumes.local-1\n",
    "platforms/dc1/mgmt/platform.yaml": "provider: providers.acme\nkind: mgmt\n",
    "platforms/dc1/mgmt/fe.yaml": "name: mgmt_dc1\nmode: local\ntype: host\nnodes: [m1]\nvip: 10.82.100.200\n",
    "platforms/dc1/mgmt/clusters.yaml": "mgmt_dc1:\n  kind: mgmt\n  hosts:\n    - resources.dc1.hosts.m1\n",
}



def adopted_projects():
    """Two projects with tenant-<name> groups: alpha (a CPU and a GPU pool, an internet subnet, in an
    organization) and corp-x (corporate)."""
    from tenants.models import Connectivity, QuotaRecord, Source
    org = Organization.objects.create(name="Alpha LLP")
    alpha = Tenant.objects.create(name="alpha", group_name="tenant-alpha", title="alpha", kind="cloud", stage="client",
                                  approval="approved", organization=org)
    for cluster, cpu, ram, gpu, disk in (("ca1", 8, 32, 0, 240), ("ga1", 16, 384, 1, 200)):
        QuotaRecord.objects.create(tenant=alpha, site="dc1", cluster=cluster, version=1, source=Source.PORTAL,
                                   cpu=cpu, ram_gb=ram, gpu=gpu, disk_gb=disk)
    PrefixAssignment.objects.create(cidr="198.19.182.30/31", site="dc1", kind=PrefixAssignment.Kind.INET_MAIN,
                                    tenant=alpha, source=Source.PORTAL)
    Connectivity.objects.create(tenant=alpha, site="dc1", version=1, source=Source.PORTAL, connection_type="inet",
                                inet_mbps=100, inet_main_prefix=31)
    corp = Tenant.objects.create(name="corp-x", group_name="tenant-corp-x", title="corp-x", kind="corp", stage="poc",
                                 approval="approved", organization=Organization.objects.create(name="Example"))
    QuotaRecord.objects.create(tenant=corp, site="dc1", cluster="ca1", version=1, source=Source.PORTAL, cpu=10,
                               ram_gb=16, disk_gb=210)
    return alpha, corp


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode())
class AdoptionFlow(TestCase):
    def setUp(self):
        tree, *_ = inv.check_and_load(FILES)
        inv.rebuild_index(tree)

    def test_pages(self):
        sa = User.objects.create_user("sa@corp.kz", "x", kind=Kind.STAFF, is_super_admin=True, email_verified=True,
                                      totp_enabled=True, totp_secret=pyotp.random_base32())
        self.client.force_login(sa)
        adopted_projects()  # as such projects are recorded
        self.assertEqual(Tenant.objects.count(), 2)
        for name in ("inventory", "infra_profiles", "tenant_list", "org_list", "sales_reps",
                     "portal_settings", "staff_users", "dashboard"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        site = reverse("infra_site", args=["dc1"])
        for tab in ("hosts", "networks", "storage", "platforms", "clusters", "addresses"):
            self.assertEqual(self.client.get(site + "?tab=" + tab).status_code, 200, tab)
        self.assertRedirects(self.client.get(reverse("registry")), site + "?tab=subnets", fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse("capacity")), site + "?tab=clusters#capacity",
                             fetch_redirect_response=False)
        alpha = Tenant.objects.get(name="alpha")
        self.assertContains(self.client.get(reverse("tenant_detail", args=[alpha.pk])), "198.19.182.30/31")
        self.assertContains(self.client.get(site + "?tab=clusters"), "1 / 4 slots")
        self.assertIsNotNone(alpha.organization)  # grouped
        # manual corporate subnet; overlap refused
        self.client.post(site + "?tab=subnets", {"cidr": "10.82.104.0/28", "site": "dc1", "kind": "corpnet", "tenant": "corp-x"})
        self.client.post(site + "?tab=subnets", {"cidr": "10.82.104.0/29", "site": "dc1", "kind": "reserved"})
        self.assertEqual(PrefixAssignment.active().filter(site="dc1", kind__in=["corpnet", "reserved"]).count(), 1)


class Collector(TestCase):
    def test_xml_to_dict(self):
        import xml.etree.ElementTree as ET
        g = xml_to_dict(ET.fromstring(
            "<GROUP><ID>5</ID><NAME>tenant-a</NAME><TEMPLATE><TENANT><STATUS>ok</STATUS></TENANT>"
            "<QUOTA_DC2_CA1><CPU>10</CPU></QUOTA_DC2_CA1></TEMPLATE><USERS><ID>1</ID><ID>2</ID></USERS></GROUP>"))
        self.assertEqual(g["TEMPLATE"]["QUOTA_DC2_CA1"]["CPU"], "10")
        self.assertEqual(g["USERS"]["ID"], ["1", "2"])


class Grouping(TestCase):
    def setUp(self):
        from accounts.models import Kind, User
        from tenants.models import Tenant
        self.rm = User.objects.create_user("rm@corp.kz", "x", kind=Kind.STAFF, is_resource_manager=True,
                                           totp_secret="JBSWY3DPEHPK3PXP", totp_enabled=True)
        self.rep = User.objects.create_user("rep@corp.kz", None, kind=Kind.STAFF, is_sales=True, login_enabled=False)
        mk = lambda name, legal, **kw: Tenant.objects.create(name=name, group_name=f"tenant-{name}",
                                                              approval="approved", **kw)
        mk("a1", "Example Cloud", kind="corp")
        mk("a2", "ТОО “Example Cloud”")
        mk("a3", "Example CLoud")
        mk("b1", "990640001231 ТОО «Lambda»")
        mk("b2", 'ТОО "Lambda"')
        mk("c1", "Акимат Example области")

    def test_move_by_tax_id_and_merge(self):
        from django.urls import reverse
        from accounts.tests import valid_bin
        from tenants.models import Organization, Tenant
        for names, org in ((("b1", "b2"), "Lambda"), (("c1",), "Acme of Example region")):
            o = Organization.objects.create(name=org)
            Tenant.objects.filter(name__in=names).update(organization=o)
        self.client.force_login(self.rm)
        t = Tenant.objects.get(name="c1")
        tax = valid_bin()
        acme = Organization.objects.create(name="Acme", tax_id=tax)
        # corrections-18: chosen from the list; the resource manager asking approves; no sales rep there: moved
        self.client.post(reverse("tenant_move", args=[t.pk]), {"mv-organization": acme.pk})
        t.refresh_from_db()
        self.assertEqual((t.organization.tax_id, t.organization.name), (tax, "Acme"))
        b = Tenant.objects.get(name="b1")
        rep = User.objects.create_user("rep2@corp.kz", "Str0ng-PASS-123", kind="staff", is_sales=True)
        acme.sales_rep = rep
        acme.save()
        self.client.post(reverse("tenant_move", args=[b.pk]), {"mv-organization": acme.pk})
        b.refresh_from_db()
        self.assertNotEqual(b.organization, acme)  # waits for the target's sales rep
        m = b.moves.get()
        self.client.force_login(rep)
        self.client.post(reverse("tenant_move_decide", args=[b.pk, m.pk, "sales"]))
        b.refresh_from_db()
        self.assertEqual(b.organization, acme)
        self.client.force_login(self.rm)
        src = Tenant.objects.get(name="b2").organization
        self.client.post(reverse("org_merge", args=[src.pk]), {"mg-target": t.organization.pk})
        self.assertEqual(Tenant.objects.get(name="b2").organization, t.organization)
        self.assertEqual(src.tenants.count(), 0)


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode())
class InventoryEditing(TestCase):
    def setUp(self):
        from accounts.models import Kind, User
        tree, *_ = inv.check_and_load(FILES)
        inv.rebuild_index(tree)
        self.sa = User.objects.create_user("sa@corp.kz", "x", kind=Kind.STAFF, is_super_admin=True,
                                           totp_secret="JBSWY3DPEHPK3PXP", totp_enabled=True)
        self.client.force_login(self.sa)

    def test_sites_profiles_hosts_clusters(self):
        from django.urls import reverse
        from inventory import ops
        from inventory.models import Cluster, Host, Platform, ServerProfile, Site
        self.assertEqual(self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=platforms").status_code, 200)
        self.client.post(reverse("inv_site_save"), {"new": "1", "code": "dc2", "country": "KZ", "city": "Almaty", "dc": "2"})
        self.assertFalse(Site.objects.filter(code="dc2").exists())  # the numeric site code is required
        from tenants.tests import FULL_SITE
        self.client.post(reverse("inv_site_save"), {**FULL_SITE, "new": "1", "code": "dc2", "number": 202, "dc": "2"})
        self.assertEqual(Site.objects.get(code="dc2").number, 202)
        with self.assertRaises(ops.EditError):  # fixed once set
            ops.site_save(self.sa, "dc2", {"number": 203, "country": "KZ", "city": "Almaty", "dc": "2"})
        self.client.post(reverse("inv_profile_save"), {
            "code": "std2", "vendor": "HPE", "chassis": "DL385", "cpu_vendor": "AMD", "cpu_name": "EPYC 9354", "sockets": 2,
            "cores_per_socket": 32, "ram_gb": 1024, "boot_dev": "/dev/nvme0n1", "boot_size_gb": 480,
            "boot_volumes": "/ 50 xfs\n/var/log 100 xfs\n/home -1 xfs",
            "cards": "ens1 | Mellanox | MCX631102AS-ADAT | 25 | ens1f0np0 ens1f1np1", "fabrics": "1", "assign": "nic-data1 | ens1f0np0\nnic-data2 | ens1f1np1"})
        pr = ServerProfile.objects.get(code="std2")
        self.assertEqual((pr.cores, pr.model, pr.cards[0]["ports"], pr.boot_volumes[2]["size_gb"], pr.bonds[0]["mtu"]),
                         (64, "HPE DL385", 2, -1, 9100))
        r = self.client.get(reverse("infra_profiles"))
        self.assertContains(r, "MCX631102AS-ADAT")
        from unittest import mock
        with mock.patch("inventory.profiles.collect", return_value=["ssh: no answer"]):
            self.client.post(reverse("inv_host_add"), {"site": "dc1", "name": "h9", "profile": "std2", "ip": "10.0.0.9"})
        h9 = Host.objects.get(name="h9")
        self.assertEqual((h9.state, h9.profile.code), ("planned", "std2"))  # not readable: the chosen profile stays
        self.client.post(reverse("inv_host_edit", args=["dc1", "h9"]), {"name": "h10", "ip": "10.0.0.10", "profile": "std2"})
        self.assertTrue(Host.objects.filter(name="h10", ip="10.0.0.10").exists())  # a planned host can be renamed
        self.client.post(reverse("inv_host_edit", args=["dc1", "h1"]), {"name": "h1x", "ip": "10.0.0.1", "profile": "std1"})
        self.assertTrue(Host.objects.filter(name="h1").exists())  # an active one cannot
        # hosts already in a live cluster are neither offered nor accepted for a new cluster
        from inventory.edit_views import ClusterForm
        offered = [v for v, _l in ClusterForm(site="dc1").fields["hosts"].choices]
        self.assertIn("dc1/h10", offered)
        self.assertNotIn("dc1/h1", offered)
        with self.assertRaises(ops.EditError):
            ops.cluster_add(self.sa, "dc1", "cloud", "shared-dup-1_dc1", "shared", "", ["h1"])
        self.client.post(reverse("inv_cluster_add"), {"platform": "dc1.cloud", "name": "bad", "kind": "shared"})
        self.assertFalse(Cluster.objects.filter(name="bad").exists())  # the name ends with _dc1
        # new cluster on the deployed platform: planned, platform becomes "updated"
        self.client.post(reverse("inv_cluster_add"), {"platform": "dc1.cloud", "name": "shared-new-1_dc1", "kind": "shared",
                                                      "hosts": ["dc1/h10"]})
        c = Cluster.objects.get(name="shared-new-1_dc1")
        self.assertEqual((c.state, c.orderable, Platform.objects.get(key="dc1.cloud").state), ("planned", False, "updated"))
        self.assertEqual(c.order_code, "shared-new-1_dc1")  # no quota code: ordered by its name
        p = Platform.objects.get(key="dc1.cloud")
        self.client.post(reverse("inv_cluster_save", args=["dc1", "cloud", c.name]), {
            "kind": "dedicated", "datastores": [p.datastores.get(name="cloud-vms01_dc1").pk],
            "networks": [p.vnets.get(name="cloud-ix_dc1").pk]})
        c.refresh_from_db()
        self.assertEqual((c.kind, c.datastores.count(), c.networks.count()), ("dedicated", 1, 1))
        self.assertTrue(Cluster.objects.get(name="shared-std-1_dc1").orderable)  # updated platform still serves
        # an active host taken out of an active cluster is retired, a planned one just leaves
        self.client.post(reverse("inv_cluster_host_remove", args=["dc1", "cloud", "shared-std-1_dc1", "h2"]))
        self.assertEqual(Host.objects.get(name="h2").state, "retired")
        self.client.post(reverse("inv_cluster_remove", args=["dc1", "cloud", "shared-new-1_dc1"]))
        self.assertFalse(Cluster.objects.filter(name="shared-new-1_dc1").exists())
        self.client.post(reverse("inv_host_remove", args=["dc1", "h10"]))
        self.assertFalse(Host.objects.filter(name="h10").exists())
        # planned platform: everything in it is planned; can be deleted
        self.client.post(reverse("inv_platform_save"), {"new": "1", "site": "dc2", "name": "cloud", "kind": "shared",
                                                        "fe_mode": "local", "fe_type": "vm", "fe_nodes": "n1\nn2\nn3",
                                                        "sa": "secrets.acme.sa"})
        p2 = Platform.objects.get(key="dc2.cloud")
        self.assertEqual((p2.display_state, p2.fe["nodes"], p2.sa), ("planned", ["n1", "n2", "n3"], "secrets.acme.sa"))
        self.client.post(reverse("inv_platform_delete", args=["dc2", "cloud"]))
        self.assertFalse(Platform.objects.filter(key="dc2.cloud").exists())

    def test_networks_storage_datastores(self):
        from django.urls import reverse
        from inventory.models import (DelegatedSubnet, Host, Network, PlatformDatastore, PlatformNetwork, Segment, Site,
                                      Storage)
        for tab in ("hosts", "networks", "storage", "platforms", "clusters", "addresses"):
            r = self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=" + tab)
            self.assertEqual(r.status_code, 200, tab)
            self.assertNotContains(r, ".yaml")
        self.assertEqual(self.client.get(reverse("infra_profiles")).status_code, 200)
        Site.objects.filter(code="dc1").update(number=81)
        new = reverse("inv_net_new", args=["dc1"])
        self.client.post(new, {"name": "test-ix", "interface": "bond0", "vlan": 77, "mtu": 1500, "subnet": "10.9.5.0",
                               "prefix": 24, "state": "active", "routes": "10.0.0.0/8 10.9.5.1 50"})
        n = Network.objects.get(site="dc1", name="test-ix")
        self.assertEqual((n.kind, n.extra.get("routes")), ("core", [{"network": "10.0.0.0/8", "gw": "10.9.5.1", "metric": 50}]))
        cix = Network.objects.get(site="dc1", name="cloud-ix")
        self.assertEqual(sorted(cix.ranges.values_list("name", flat=True)), ["egw", "ext-gws", "rrs"])  # defaults added
        self.client.post(reverse("inv_net_save", args=["dc1", cix.pk]),
                         {"interface": "bond1", "bridge": "", "vlan": 11, "mtu": 9000, "subnet": "10.82.10.0", "prefix": 24,
                          "gateway": "10.82.10.1", "state": "active", "routes": ""})
        cix.refresh_from_db()
        self.assertEqual((cix.vlan, cix.mtu, cix.interface, cix.gateway), (11, 9000, "bond1", "10.82.10.1"))
        self.client.post(new, {"name": "portal-ix", "interface": "bond0", "vlan": 109, "mtu": 1500, "subnet": "10.81.109.0",
                               "prefix": 24, "state": "active"})
        ix = Network.objects.get(site="dc1", name="portal-ix")
        self.assertEqual((ix.kind, ix.local_asn, ix.remote_asn), ("ix", 4208110902, 4208110901))
        rng = reverse("inv_range_new", args=["dc1", ix.pk])
        self.client.post(rng, {"name": "egw", "start": "10.81.109.10", "length": 230})
        self.client.post(rng, {"name": "bad", "start": "10.81.109.238", "length": 4})  # crosses rrs (…240/30)
        self.client.post(rng, {"name": "out", "start": "10.81.109.254", "length": 5})  # leaves the subnet
        self.assertEqual(sorted(ix.ranges.values_list("name", flat=True)), ["egw", "ext-gws", "rrs"])
        r = self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=networks")
        self.assertContains(r, 'data-items="10.81.109.240 10.81.109.241 10.81.109.242 10.81.109.243"')
        self.assertContains(r, "private AS numbers")
        # u10: the announced prefixes are the site's Internet IP subnets connected to this exchange, not free text
        from inventory.models import IPSubnet
        sub_a = IPSubnet.objects.create(site=Site.objects.get(code="dc1"), subnet="45.132.0.0", prefix=24,
                                        max_prefix=26)  # public → purpose auto-set to inet
        sub_b = IPSubnet.objects.create(site=Site.objects.get(code="dc1"), subnet="185.12.64.0", prefix=25,
                                        max_prefix=27)
        bgp = reverse("inv_net_bgp", args=["dc1", ix.pk])
        base = {"local_asn": ix.local_asn, "remote_asn": ix.remote_asn, "ip_subnets": [sub_a.pk, sub_b.pk],
                "hosts_rrs": "10.81.109.240/30", "hosts_igw": "10.81.109.10/31\n10.81.109.12/30",
                "hosts_egw": "10.81.109.0/30", "password_igw": "Egw-PW-pass-1", "password_rrs": "Rrs-PW-pass-2", "password_egw": "Ext-PW-pass-3"}
        self.client.post(bgp, base)
        self.assertEqual(ix.peer_groups.count(), 0)  # rrs takes addresses only
        self.client.post(bgp, dict(base, hosts_rrs="10.81.109.241\n10.81.109.242"))
        from inventory import networks
        from inventory.credentials import value
        ix = Network.objects.get(site="dc1", name="portal-ix")
        self.assertEqual(networks.missing_bgp(ix), [])
        self.assertEqual(ix.rrs, ["10.81.109.241", "10.81.109.242"])
        self.assertTrue(DelegatedSubnet.objects.filter(segment="portal-ix", cidr="45.132.0.0/24").exists())
        seg = networks.segment("dc1", "portal-ix")
        self.assertEqual(seg["ibgp"]["egw"]["hosts"], ["10.81.109.10/31", "10.81.109.12/30"])
        self.assertEqual((seg["rr_net"], seg["ebgp"]["ext"]["hosts"]), ("10.81.109.241/32", ["10.81.109.0/30"]))
        self.assertEqual(value(ix.peer_groups.get(name="rrs").password), "Rrs-PW-pass-2")
        self.assertEqual(value(ix.peer_groups.get(name="ext").password), "Ext-PW-pass-3")
        r = self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=networks")
        self.assertNotContains(r, "Rrs-PW-pass-2")
        self.client.post(reverse("inv_net_delete", args=["dc1", cix.pk]))
        self.assertTrue(Network.objects.filter(site="dc1", name="cloud-ix").exists())  # used by the platform
        self.client.post(reverse("inv_net_delete", args=["dc1", n.pk]))
        self.assertFalse(Network.objects.filter(site="dc1", name="test-ix").exists())
        self.assertFalse(Segment.objects.filter(name="test-ix").exists())
        self.client.post(new, {"name": "portal-vxlan", "interface": "bond0", "vlan": 108, "mtu": 9050,
                               "subnet": "10.81.108.0", "prefix": 24, "state": "active"})
        ev = Network.objects.get(site="dc1", name="portal-vxlan")
        self.assertEqual((ev.kind, ev.local_asn, ev.remote_asn), ("evpn", 4208110800, None))
        self.assertEqual(list(ev.ranges.values_list("name", flat=True)), ["rrs"])
        # storage: NFS has one target, iSCSI several and a port, local belongs to a host
        snew = reverse("inv_storage_new", args=["dc1"])
        self.client.post(snew, {"name": "san-1", "kind": "nfs", "targets": "10.0.9.1\n10.0.9.2"})
        self.assertFalse(Storage.objects.filter(name="san-1").exists())
        self.client.post(snew, {"name": "san-1", "kind": "iscsi", "targets": "10.0.9.1\n10.0.9.2", "port": 3260,
                                "fstab": "xfs _netdev,nofail 0 2"})
        san = Storage.objects.get(name="san-1")
        self.assertEqual((san.targets, san.port), (["10.0.9.1", "10.0.9.2"], 3260))
        vnew = reverse("inv_volume_new", args=["dc1", san.pk])
        self.client.post(vnew, {"name": "vol-a", "device": "/dev/mapper/mpatha", "size_gb": 100, "clients": "not-an-iqn"})
        self.assertFalse(san.volumes.exists())
        self.client.post(vnew, {"name": "vol-a", "device": "/dev/mapper/mpatha", "size_gb": 100,
                                "clients": "iqn.2004-10.com.ubuntu:01:host1"})
        vol = san.volumes.get(name="vol-a")
        # platform datastores and networks
        from inventory.models import Platform
        p = Platform.objects.get(key="dc1.cloud")
        self.client.post(reverse("inv_ds_new", args=["dc1", "cloud"]), {"name": "cloud-bkp_dc1", "kind": "backups",
                                                                         "volume": vol.pk})
        self.assertFalse(PlatformDatastore.objects.filter(name="cloud-bkp_dc1").exists())  # backups: backend, restic VM, password
        from inventory.models import Secret
        pw = Secret.objects.create(kind=Secret.Kind.PASSWORD, name="dc1.restic")
        self.client.post(reverse("inv_ds_new", args=["dc1", "cloud"]), {"name": "cloud-bkp_dc1", "kind": "backups",
                                                                         "volume": vol.pk, "backend": "iscsi",
                                                                         "restic_ip": "10.0.1.50", "restic_secret": pw.ref})
        ds = PlatformDatastore.objects.get(name="cloud-bkp_dc1")
        self.assertEqual((ds.backend, ds.restic_ip, ds.restic_secret, ds.s3_bucket), ("iscsi", "10.0.1.50", pw.ref, ""))
        self.assertEqual(Platform.objects.get(key="dc1.cloud").state, "updated")
        self.client.post(reverse("inv_volume_delete", args=["dc1", san.pk, vol.pk]))
        self.assertTrue(san.volumes.filter(pk=vol.pk).exists())  # used by a datastore
        self.client.post(reverse("inv_storage_delete", args=["dc1", san.pk]))
        self.assertTrue(Storage.objects.filter(pk=san.pk).exists())
        self.client.post(reverse("inv_ds_delete", args=["dc1", "cloud", ds.pk]))
        self.client.post(reverse("inv_storage_delete", args=["dc1", san.pk]))
        self.assertFalse(Storage.objects.filter(pk=san.pk).exists())
        pix = Network.objects.get(site="dc1", name="portal-ix")
        self.client.post(reverse("inv_vn_new", args=["dc1", "cloud"]), {
            "name": "portal-ix_dc1", "kind": "vlan", "network": pix.pk, "ranges": [pix.ranges.get(name="egw").pk]})
        vn = PlatformNetwork.objects.get(name="portal-ix_dc1")
        self.assertEqual(Platform.objects.get(key="dc1.cloud").segments, ["cloud-ix", "portal-ix"])
        self.client.post(reverse("inv_range_delete", args=["dc1", pix.pk, pix.ranges.get(name="egw").pk]))
        self.assertTrue(pix.ranges.filter(name="egw").exists())  # used by the platform network
        self.client.post(reverse("inv_vn_delete", args=["dc1", "cloud", vn.pk]))
        self.assertEqual(Platform.objects.get(key="dc1.cloud").segments, ["cloud-ix"])
        # host: edit; an unused host is deleted, a used active host is only retired
        self.client.post(reverse("inv_host_edit", args=["dc1", "h1"]),
                         {"name": "h1", "profile": "std1", "ip": "10.0.0.11", "ipmi": "10.0.1.11",
                          "ipmi_user": "secrets.acme.ipmi.users.standard"})
        h1 = Host.objects.get(name="h1")
        self.assertEqual((h1.ip, h1.ipmi_host, h1.ipmi_user), ("10.0.0.11", "10.0.1.11", "secrets.acme.ipmi.users.standard"))
        self.assertContains(self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=hosts"), 'href="https://10.0.1.11/"')
        self.client.post(reverse("inv_host_remove", args=["dc1", "h1"]))
        self.assertEqual(Host.objects.get(name="h1").state, "retired")


@override_settings(FIELD_ENCRYPTION_KEY=__import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode())
class InventorySecrets(TestCase):
    def test_endpoint_from_inventory_and_secrets(self):
        import os
        import shutil
        import tempfile
        from django.test import override_settings
        from inventory.models import Platform, PlatformControl, Site
        from adoption.collector import client_for, ensure_controls
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "cloud"))
        with open(os.path.join(d, "cloud", "one.yaml"), "w") as f:
            f.write("oneadmin: s3cr3t\nonedb: {db: opennebula}\n")
        site = Site.objects.create(code="dc1")
        Platform.objects.create(key="dc1.cloud", site=site, name="cloud", kind="shared", fe_vip="10.0.0.30",
                                fe_secrets="secrets.acme.cloud.one")
        Platform.objects.create(key="dc1.mgmt", site=site, name="mgmt", kind="mgmt", fe_vip="10.0.0.40",
                                fe_secrets="secrets.acme.mgmt.one")
        with secrets_loaded(d):
            ensure_controls()
            c, m = PlatformControl.objects.get(key="dc1.cloud"), PlatformControl.objects.get(key="dc1.mgmt")
            self.assertEqual((c.collect_enabled, m.collect_enabled), (True, False))  # only shared ones by default
            self.assertEqual(c.endpoint_url, "http://10.0.0.30:2633/RPC2")
            self.assertTrue(c.configured)  # provider level "acme" skipped: the tree starts at cloud/
            self.assertEqual(c.credentials(), ("http://10.0.0.30:2633/RPC2", "oneadmin:s3cr3t"))
            self.assertFalse(m.configured)
            with self.assertRaises(Exception) as ctx:
                client_for(m)
            self.assertNotIn("s3cr3t", str(ctx.exception))
        shutil.rmtree(d)
