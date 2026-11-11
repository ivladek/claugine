"""Core services of a site: the standard layout from the platform's VMs, the form, reading (ssh mocked), the tab."""
from unittest import mock

from cryptography.fernet import Fernet
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Kind, User


def site_with_vms():
    from inventory.models import (Cluster, ConfigTemplate, Host, Network, Platform, PlatformControl, Secret, Site)
    st = Site.objects.create(code="dc1", management={"ports": {"vyos": 55522, "ssh": 55523}})
    mgmt = Platform.objects.create(key="dc1.mgmt", site=st, name="mgmt", kind="mgmt", sa="secret:1")
    cl = Cluster.objects.create(platform=mgmt, name="mgmt_dc1", kind="mgmt")
    for i in (1, 2, 3):
        cl.hosts.add(Host.objects.create(site=st, name=f"hdc1kvm20{i}", ip=f"10.0.100.20{i}"))
    Network.objects.create(site="dc1", name="cloud-core", kind="core", interface="bond0", vlan=101, subnet="10.0.101.0",
                           prefix=24, gateway="10.0.101.254")
    Network.objects.create(site="dc1", name="data-nfs", kind="storage", interface="bond1", vlan=111, subnet="10.0.111.0",
                           prefix=24)
    Network.objects.create(site="dc1", name="cloud-ix", kind="ix", interface="bond0", vlan=10, subnet="10.0.10.0", prefix=24)
    Network.objects.create(site="dc1", name="cloud-vyos", kind="core", interface="bond0", vlan=20, subnet="10.0.20.0",
                           prefix=24, gateway="10.0.20.1")
    assert ConfigTemplate.objects.filter(role="rr-inet").exists()  # made by a migration
    s = Secret.objects.create(kind="user", name="sa", username="opsuser")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    pem = ed25519.Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                                             serialization.NoEncryption()).decode()
    s.put("private_key", pem)
    s.save()
    Platform.objects.filter(pk=mgmt.pk).update(sa=s.ref)
    vm = lambda name, ip, nets=("cloud-core_dc1",), vcpu=1, ram=1.0, disk=5: {  # noqa: E731
        "name": name, "id": 1, "vcpu": vcpu, "ram_gb": ram, "disks_gb": [disk],
        "nics": [[n, ip if i == 0 else "10.0.111.9"] for i, n in enumerate(nets)], "state": "3", "lcm_state": "3"}
    PlatformControl.objects.create(key="dc1.mgmt", vms=[
        vm("cloud-ns1_dc1", "10.0.101.11"), vm("cloud-ns2_dc1", "10.0.101.12"),
        vm("cloud-one1_dc1", "10.0.101.31", ("cloud-core_dc1", "data-nfs_dc1"), 8, 32.0, 60),
        vm("cloud-one2_dc1", "10.0.101.32", ("cloud-core_dc1", "data-nfs_dc1"), 8, 16.0, 60),
        vm("cloud-ix-rr1_dc1", "10.0.10.241", ("cloud-ix_dc1",), 2, 4.0, 2),
        vm("cloud-gitlab1_dc1", "10.0.101.50"), vm("tenant_x-egw_dc1", "10.0.10.30"),
        {"name": "cloud-igw_dc1", "id": 9, "vcpu": 1, "ram_gb": 2.0, "disks_gb": [2], "state": "3", "lcm_state": "3",
         "nics": [["cloud-services_dc1", "203.0.113.2"], ["cloud-core_dc1", "10.0.101.254"], ["cloud-ix_dc1", "10.0.10.8"]]}])
    return st, s


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode())
class CoreServices(TestCase):
    def setUp(self):
        self.site, self.secret = site_with_vms()
        self.sa = User.objects.create_user("sa@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, email_verified=True,
                                           is_super_admin=True, totp_secret="JBSWY3DPEHPK3PXP", totp_enabled=True)
        self.client.force_login(self.sa)
        s = self.client.session
        s["otp_verified"] = True
        s.save()

    def test_standard_layout_from_the_platform_vms(self):
        from inventory import core_services as cs
        from inventory.models import CoreService
        made = cs.seed("dc1")
        self.assertEqual(sorted(made), ["cloud-ix-rrs", "ns"])  # only ns, logs, admin, backup and the RRs (correction 06)
        self.assertEqual({s.name: s.stype for s in CoreService.objects.all()}, {"cloud-ix-rrs": "bgp-rr", "ns": "ns"})
        ns = CoreService.objects.get(name="ns")
        self.assertEqual((ns.kind, ns.vcpu, float(ns.ram_gb), ns.disk_gb, ns.nic1_network.name, ns.nic1_gateway, ns.serves),
                         ("vm", 1, 1.0, 5, "cloud-core", None, None))  # the subnet's gateway; a core service
        self.assertEqual([n.ips for n in ns.nodes.all()], [["10.0.101.11"], ["10.0.101.12"]])
        self.assertTrue(ns.description.startswith("Name servers"))
        self.assertEqual([(n.name, n.address, n.ssh_port, n.secret) for n in ns.nodes.all()],
                         [("cloud-ns1_dc1", "10.0.101.11", 22, self.secret.ref), ("cloud-ns2_dc1", "10.0.101.12", 22, self.secret.ref)])
        rr = CoreService.objects.get(name="cloud-ix-rrs")
        self.assertEqual(rr.params["network"], rr.service_network().pk)
        self.assertEqual((rr.template.role, rr.nodes.get().ssh_port, rr.ssh_port, rr.ssh_secret),
                         ("rr-inet", 55522, 55522, self.secret.ref))
        self.assertEqual([n.name for n, _gw in rr.nic_list()], ["cloud-vyos", "cloud-ix"])  # management first (VyOS)
        self.assertEqual(rr.nodes.get().ips, ["", "10.0.10.241"])  # its management address: assigned when made
        self.assertEqual(cs.seed("dc1"), [])  # existing services are never changed


    @override_settings(DEPLOY_ASYNC=False)
    def test_read_is_read_only_and_flags_a_different_size(self):
        from inventory import core_services as cs
        from inventory.models import CoreService, CoreServiceNode
        with mock.patch("inventory.core_services.seed_type", return_value="core-vm"):  # all of them, as before 06
            cs.seed("dc1")
        out = ("##host\ncloud-one1\n##os\nUbuntu 24.04.3 LTS\n##cpu\n8\n##mem\n32092\n##disk\n59G\n"
               "##addr\neth0 10.0.101.31/24\neth1 10.0.111.9/24\n")
        calls = []

        def run(host, port, user, key, remote, **kw):
            calls.append((host, port, user, remote))
            if host == "10.0.101.32":
                from inventory.sshexec import SshError
                raise SshError("10.0.101.32:22: Connection refused")
            return out
        with mock.patch("inventory.core_services.read_platform_vms", side_effect=lambda c: c.vms), \
                mock.patch("inventory.sshexec.run", side_effect=run):
            r = self.client.post(reverse("core_read", args=["dc1", CoreService.objects.get(name="shared-fe").pk]))
        self.assertEqual(r.status_code, 302)
        self.assertTrue(all("vbash" not in c[3] and "set " not in c[3] for c in calls))  # nothing is changed
        n1 = CoreServiceNode.objects.get(name="cloud-one1_dc1")
        self.assertEqual((n1.facts["hostname"], n1.facts["vcpu"], n1.facts["ram_gb"], n1.facts["disk_gb"]),
                         ("cloud-one1", 8, 31, 59))
        self.assertIn("eth0 10.0.101.31/24", n1.facts["addresses"])
        self.assertIn("refused", CoreServiceNode.objects.get(name="cloud-one2_dc1").facts_error)
        fe = CoreService.objects.get(name="shared-fe")
        self.assertEqual(cs.mismatches(fe), [("cloud-one2_dc1", "RAM 16.0 GB (service: 32)")])
        page = self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=core")
        self.assertContains(page, "Core services")
        self.assertContains(page, "RAM 16.0 GB (service: 32)")
        self.assertNotContains(page, "in no core service")  # that list is gone (correction 02)

    def test_form_saves_nodes_and_audits(self):
        from accounts.models import AuditLog
        from inventory.models import CoreService, Network, Platform, PlatformControl, ServiceImage
        core = Network.objects.get(site="dc1", name="cloud-core")
        mgmt = Platform.objects.get(key="dc1.mgmt")
        img = ServiceImage.objects.create(control=PlatformControl.objects.get(key="dc1.mgmt"), purpose="base", kind="image",
                                          name="Ubuntu 24.04", customization="ssh")
        r = self.client.post(reverse("core_new", args=["dc1"]), {
            "name": "graylog", "description": "Log collector", "kind": "vm", "state": "active",
            "platform": str(mgmt.pk), "serves": "",
            "nodes": "cloud-onelog1_dc1 | onelog1 | 10.0.101.18 | 10.0.111.18", "ssh_port": 22, "ssh_secret": self.secret.ref,
            "service_secret": self.secret.ref, "image": str(img.pk),
            "vcpu": 12, "ram_gb": "48", "disk_gb": 20, "nic1_network": str(core.pk), "more_nics": "data-nfs",
            "routes": "1 10.0.0.0/8 10.0.101.1 20\n10.9.0.0/16 10.0.101.1", "firewall": "9000 tcp 10.0.0.0/8\n514 udp"})
        self.assertEqual(r.status_code, 302)
        g = CoreService.objects.get(name="graylog")
        n = g.nodes.get()
        self.assertEqual((n.address, n.hostname, n.ips, n.ssh_port, n.secret),
                         ("10.0.101.18", "onelog1", ["10.0.101.18", "10.0.111.18"], 22, self.secret.ref))
        self.assertEqual([x.name for x, _g in g.nic_list()], ["cloud-core", "data-nfs"])
        self.assertEqual(g.routes, [{"nic": 1, "subnet": "10.0.0.0/8", "via": "10.0.101.1", "metric": 20},
                                    {"nic": 1, "subnet": "10.9.0.0/16", "via": "10.0.101.1"}])
        self.assertEqual(g.firewall, [{"port": "9000", "proto": "tcp", "source": "10.0.0.0/8"}, {"port": "514", "proto": "udp"}])
        self.assertEqual((g.image, g.platform.key, g.ram_gb, g.service_secret, g.serves), (img, "dc1.mgmt", 48, self.secret.ref, None))
        self.assertTrue(AuditLog.objects.filter(action="core_service_saved").exists())
        page = self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=core")
        self.assertContains(page, "core (common)")
        self.assertNotContains(page, "Deployed VMs")  # correction 03: one VMs table with status and details
        self.assertContains(page, "dlg-cn-")
        self.assertContains(page, 'data-cs-form')
        # from scratch needs a NIC 1 subnet; a route needs its NIC; a row needs a name; no more addresses than NICs
        self.client.post(reverse("core_save", args=["dc1", g.pk]), {"name": "graylog", "kind": "vm", "state": "active",
                                                                     "ssh_port": 22, "nodes": "a | b | c"})
        self.client.post(reverse("core_save", args=["dc1", g.pk]), {
            "name": "graylog", "kind": "vm", "state": "active", "ssh_port": 22, "nic1_network": str(core.pk), "platform": str(mgmt.pk),
            "nodes": "cloud-onelog1_dc1 | | 10.0.101.19", "routes": "2 10.0.0.0/8 10.0.101.1"})
        self.client.post(reverse("core_save", args=["dc1", g.pk]), {
            "name": "graylog", "kind": "vm", "state": "active", "ssh_port": 22, "nic1_network": str(core.pk), "platform": str(mgmt.pk),
            "nodes": "cloud-onelog1_dc1 | | 10.0.101.19 | 10.0.111.19"})
        self.assertEqual(CoreService.objects.get(pk=g.pk).nodes.get().ips, ["10.0.101.18", "10.0.111.18"])
        # empty address: assigned when the VM is made; the login address stays as it is
        self.client.post(reverse("core_save", args=["dc1", g.pk]), {
            "name": "graylog", "kind": "vm", "state": "active", "ssh_port": 22, "nic1_network": str(core.pk), "platform": str(mgmt.pk),
            "more_nics": "data-nfs", "nodes": "cloud-onelog1_dc1 | onelog1 | auto | 10.0.111.18", "image": str(img.pk)})
        n = CoreService.objects.get(pk=g.pk).nodes.get()
        self.assertEqual((n.ips, n.address), (["", "10.0.111.18"], "10.0.101.18"))
        # the image cannot be deleted while a service uses it
        self.client.post(reverse("service_image_delete", args=["dc1.mgmt", img.pk]))
        self.assertTrue(ServiceImage.objects.filter(pk=img.pk).exists())
        self.client.post(reverse("core_delete", args=["dc1", g.pk]))
        self.assertFalse(CoreService.objects.filter(pk=g.pk).exists())
        self.assertTrue(AuditLog.objects.filter(action="core_service_deleted").exists())
        self.client.post(reverse("service_image_delete", args=["dc1.mgmt", img.pk]))
        self.assertFalse(ServiceImage.objects.filter(pk=img.pk).exists())

    def test_images_customization_and_the_native_router_rows(self):
        from inventory import service_images
        from inventory.models import PlatformControl, ServiceImage
        c = PlatformControl.objects.get(key="dc1.cloud") if PlatformControl.objects.filter(key="dc1.cloud").exists() else \
            PlatformControl.objects.create(key="dc1.cloud")
        c.service_images.all().delete()
        rows = {r.role: r for r in service_images.ensure(c)}
        self.assertEqual({r.customization for r in rows.values()}, {"context"})
        self.client.post(reverse("service_image_delete", args=[c.key, rows["vyos"].pk]))
        self.assertTrue(ServiceImage.objects.filter(pk=rows["vyos"].pk).exists())  # mandatory
        self.client.post(reverse("service_image_delete", args=[c.key, rows["one-router-template"].pk]))
        self.assertEqual(list(c.service_images.values_list("role", flat=True)), ["vyos"])  # template and image together
        service_images.ensure(c)
        self.assertEqual(list(c.service_images.values_list("role", flat=True)), ["vyos"])  # not made again
        c.refresh_from_db()
        self.assertEqual(c.nr_template, "")
        self.client.post(reverse("service_image_save", args=[c.key, rows["vyos"].pk]), {"name": "VyOS 2026.06",
                                                                                         "customization": "context"})
        self.assertEqual(ServiceImage.objects.get(pk=rows["vyos"].pk).name, "VyOS 2026.06")
        self.client.post(reverse("service_image_add", args=[c.key]), {"purpose": "base-os", "kind": "image",
                                                                      "name": "Debian 13", "customization": "ssh"})
        self.assertEqual(ServiceImage.objects.get(name="Debian 13").customization, "ssh")

    def test_rename_a_platform(self):
        from accounts.models import AuditLog
        from inventory.models import Platform, PlatformControl
        from tenants.models import Tenant
        p = Platform.objects.get(key="dc1.mgmt")
        PlatformControl.objects.get_or_create(key="dc1.mgmt")
        t = Tenant.objects.create(name="t1", order={"platform": "dc1.mgmt"}) if hasattr(Tenant, "order") else None
        r = self.client.post(reverse("inv_platform_save"), {"site": "dc1", "name": "mgmt", "rename": "mgmt2",
                                                           "kind": p.kind, "fe_mode": p.fe_mode or "local"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Platform.objects.filter(key="dc1.mgmt2", name="mgmt2").exists())
        self.assertTrue(PlatformControl.objects.filter(key="dc1.mgmt2").exists())
        self.assertFalse(PlatformControl.objects.filter(key="dc1.mgmt").exists())
        if t is not None:
            t.refresh_from_db()
            self.assertEqual(t.order["platform"], "dc1.mgmt2")
        self.assertTrue(AuditLog.objects.filter(action="platform_renamed").exists())


class Checks(TestCase):
    def test_evaluate_units_ports_ntp_bgp_and_addresses(self):
        import json
        from inventory import core_services as cs
        checks = {"units": ["named", "mysql"], "ports": [53, 2633], "ntp": True, "bgp": "ipv4"}
        bgp = {"ipv4Unicast": {"peers": {"10.0.10.1": {"state": "Established"}, "10.0.10.9": {"state": "Active"}}}}
        out = ("##units\nactive\ninactive\n##ports\n0.0.0.0:53\n[::]:22\n##ntp\nyes\n##bgp\n" + json.dumps(bgp) + "\n")
        facts = {"addresses": ["eth0 10.0.101.11/24"], "vm": {"nics": [["cloud-core_dc1", "10.0.101.11"], ["x", "10.9.9.9"]]}}
        res = {c["check"]: (c["ok"], c["detail"]) for c in cs.evaluate(checks, out, facts)}
        self.assertEqual(res["named running"], (True, "active"))
        self.assertEqual(res["mysql running"], (False, "inactive"))
        self.assertEqual(res["port 53 listening"][0], True)
        self.assertEqual(res["port 2633 listening"][0], False)
        self.assertEqual(res["time synchronized (NTP)"], (True, "yes"))
        self.assertEqual(res["BGP (ipv4) peers established"], (False, "1/2; down: 10.0.10.9"))
        self.assertEqual(res["the platform's addresses on the VM"], (False, "missing 10.9.9.9"))
        self.assertIn("vtysh -c 'show bgp l2vpn evpn summary json'", cs.check_script({"bgp": "evpn"}))
        self.assertNotIn("vbash", cs.check_script(checks))  # read only

    def test_gate_and_flow_only_on_the_leader(self):
        from inventory import core_services as cs
        checks = cs.CHECKS["shared-fe"]
        script = cs.check_script(checks)
        self.assertIn("opennebula-gate opennebula-flow", script)
        out = "##units\nactive\nactive\nactive\ninactive\ninactive\n##ports\n0.0.0.0:2633\n0.0.0.0:2616\n"
        follower = {"addresses": ["eth0 10.0.101.32/24"]}
        res = {c["check"]: c for c in cs.evaluate(checks, out, follower, "10.0.101.32")}
        self.assertEqual(res["follower"]["detail"], "10.0.101.30")
        self.assertFalse(any("gate" in k or "5030" in k for k in res))
        self.assertTrue(all(c["ok"] for c in res.values()))
        leader = {"addresses": ["eth0 10.0.101.31/24", "eth0 10.0.101.30/24"]}
        res = {c["check"]: c["ok"] for c in cs.evaluate(checks, out, leader, "10.0.101.31")}
        self.assertTrue(res["leader (holds the floating address)"])
        self.assertFalse(res["opennebula-gate running (leader)"])
        self.assertFalse(res["port 5030 listening (leader)"])

    def test_exactly_one_leader(self):
        from inventory import core_services as cs
        from inventory.models import CoreService, CoreServiceNode, Site
        site = Site.objects.create(code="dc9")
        svc = CoreService.objects.create(site=site, name="shared-fe", kind="vm")
        a = CoreServiceNode.objects.create(service=svc, name="cloud-one1_dc9", address="10.0.101.31",
                                           facts={"addresses": ["eth0 10.0.101.31/24"]})
        CoreServiceNode.objects.create(service=svc, name="cloud-one2_dc9", address="10.0.101.32",
                                       facts={"addresses": ["eth0 10.0.101.32/24"]})
        self.assertEqual(cs.leaders(svc), [])
        a.facts = {"addresses": ["eth0 10.0.101.31/24", "eth0 10.0.101.30/24"]}
        a.save()
        self.assertEqual(cs.leaders(svc), ["cloud-one1_dc9"])
        svc.checks = {"vip": "10.0.101.99"}
        svc.save()
        self.assertEqual(cs.leaders(svc), [])


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode())
class Gateways(TestCase):
    """The site's gateways: the tenants' egw/igw template with the site's context plus the service's differences."""

    def setUp(self):
        from inventory.models import ConfigTemplate, CoreService, CoreServiceNode, Network
        self.site, _secret = site_with_vms()
        Network.objects.create(site="dc1", name="cloud-services", kind="core", interface="bond0", vlan=103,
                               subnet="203.0.113.0", prefix=29, gateway="203.0.113.1")
        Network.objects.filter(site="dc1", name="cloud-ix").update(local_asn=4200101002, rrs=["10.0.10.241", "10.0.10.242"])
        self.egw = CoreService.objects.create(site=self.site, name="cloud-egw", kind="vm",
                                              template=ConfigTemplate.objects.get(role="egw"), ssh_port=55522)
        self.node = CoreServiceNode.objects.create(service=self.egw, name="cloud-egw_dc1", hostname="rdc1egw", address="10.0.10.9",
                                                   facts={"vm": {"nics": [["cloud-ix_dc1", "10.0.10.9"],
                                                                          ["cloud-services_dc1", "203.0.113.1"]]}})

    def test_egw_from_the_tenant_template_plus_differences(self):
        from inventory import nfconfig
        base = nfconfig.build_gateway("egw", self.node)
        self.assertIn("set protocols bgp parameters router-id 10.0.10.9", base)
        self.assertIn("set firewall group network-group CLIENT network 203.0.113.0/29", base)
        self.assertIn("set protocols bgp neighbor 10.0.10.242 peer-group cloud-int", base)
        self.assertIn("set interfaces ethernet eth0 vrf MGMT", base)
        running = "\n".join([
            "set protocols bgp parameters router-id '10.0.10.9'",
            "set firewall ipv4 name IX-to-GW description 'IX->GW: BGP and SSH'",
            "set firewall group network-group SSHFROM network '10.0.10.8/31'",
            "set system host-name 'rdc1egw'",
            "set protocols bgp peer-group cloud-int password 'x'",
        ])
        diff = nfconfig.diff_from_running(base, running)
        self.assertIn("set firewall ipv4 name IX-to-GW description 'IX->GW: BGP and SSH'", diff)
        self.assertIn("set firewall group network-group SSHFROM network '10.0.10.8/31'", diff)
        self.assertNotIn("host-name", diff)
        self.assertNotIn("password", diff)
        self.assertNotIn("delete firewall ipv4 name IX-to-GW description", diff)  # replaced, not deleted
        self.assertNotIn("vrf MGMT", diff)  # the standard's management part stays
        self.assertIn("delete protocols bgp neighbor 10.0.10.241 peer-group cloud-int", diff)
        self.egw.config_diff = diff
        self.egw.save()
        self.node.refresh_from_db()
        out = nfconfig.build_gateway("egw", self.node)
        # 2026-10-27: nothing is carried over from a running VM but for the igw — the egw is the standard one
        self.assertEqual(out, nfconfig.gateway_base("egw", self.node))
        self.assertNotIn("IX->GW: BGP and SSH", out)
        self.assertNotIn("SSHFROM", out)

    def test_igw_gets_the_management_part_and_its_ext_interface(self):
        from inventory import nfconfig
        from inventory.models import ConfigTemplate, CoreService, CoreServiceNode
        igw = CoreService.objects.create(site=self.site, name="cloud-igw", kind="vm",
                                         template=ConfigTemplate.objects.get(role="igw"), ssh_port=55522)
        n = CoreServiceNode.objects.create(service=igw, name="cloud-igw_dc1", address="10.0.101.254",
                                           facts={"vm": {"nics": [["cloud-services_dc1", "203.0.113.2"],
                                                                  ["cloud-core_dc1", "10.0.101.254"]]}})
        out = nfconfig.build_gateway("igw", n)
        self.assertIn("set firewall zone EXT member interface eth1", out)
        self.assertIn("set nat source rule 10 outbound-interface name eth1", out)
        self.assertIn("set interfaces ethernet eth2 address 10.0.101.254/24", out)
        self.assertIn("set interfaces ethernet eth0 vrf MGMT", out)
        self.assertIn("set protocols static route 0.0.0.0/0 next-hop 203.0.113.1 distance 100", out)  # not the MGMT one
        self.assertNotIn("SSHFROM", out)  # ssh only in VRF MGMT on the site igw
        self.assertNotIn("EXT-to-GW rule 10 ", out)
        self.assertIn("set service ssh vrf MGMT", out)
        igw.config_diff = "# a comment\nset interfaces ethernet eth1 address '203.0.113.3/29'"
        igw.save()
        n.refresh_from_db()
        self.assertIn("203.0.113.3/29", nfconfig.build_gateway("igw", n))
        self.assertEqual(nfconfig.diff_from_running("set a b c\n", "set a b c\n", {"eth0": "eth1"}), "")
        self.assertEqual(nfconfig.diff_from_running("", "set interfaces ethernet eth0 offload gro", {"eth0": "eth1"}),
                         "set interfaces ethernet eth1 offload gro\n")
        # the NICs' MTU comes from OpenNebula (the context): never a difference (correction 09)
        self.assertEqual(nfconfig.diff_from_running("", "set interfaces ethernet eth1 mtu 9050"), "")
        self.assertEqual(self.client.get(reverse("core_config", args=["dc1", igw.pk, n.pk])).status_code, 302)  # staff only


class FakeWorld:
    """Enough OpenNebula for a redeploy: VMs with NICs, power off / resume, rename, NIC detach / attach."""

    def __init__(self):
        from tenants.fakeone import World
        self.w = World({"one.clusterpool.info": "<CLUSTER_POOL><CLUSTER><ID>0</ID><NAME>mgmt_dc1</NAME></CLUSTER></CLUSTER_POOL>"},
                       vnets=[{"id": 1, "name": "cloud-ix_dc1", "ars": [{"TYPE": "IP4", "IP": "10.0.10.240", "SIZE": 10}], "mtu": 1500},
                              {"id": 8, "name": "cloud-vyos_dc1", "ars": [{"TYPE": "IP4", "IP": "10.0.20.100", "SIZE": 20}]}],
                       vms=[{"id": 51, "name": "cloud-ix-rr1_dc1", "nics": [{"network_id": 1, "ip": "10.0.10.241"}]}])

    def _call(self, method, *args):
        w = self.w
        for v in w.vms.values():  # the platform's own VMs (group oneadmin), as the page reads them
            v.setdefault("gname", "oneadmin")
        if method == "one.vm.action":
            self.__dict__.setdefault("actions", []).append(args[0])
        if method == "one.vm.action" and args[0] in ("poweroff", "poweroff-hard"):
            w.vms[int(args[1])]["state"] = "8"
            if args[0] == "poweroff" and int(args[1]) in getattr(self, "stubborn", ()):
                self.bounce = int(args[1])  # off for a moment, then the monitoring finds it running again
            elif self.__dict__.get("bounce") == int(args[1]):
                self.bounce = None
            return int(args[1])
        if method == "one.vm.info" and self.__dict__.get("bounce") == int(args[0]):
            self.bounce_seen = self.__dict__.get("bounce_seen", 0) + 1
            if self.bounce_seen > 1:
                w.vms[int(args[0])]["state"] = "3"
        if method == "one.vm.action" and args[0] == "resume":
            w.vms[int(args[1])]["state"] = "3"
            return int(args[1])
        if method == "one.vm.rename":
            w.vms[int(args[0])]["name"] = args[1]
            return int(args[0])
        return w._call(method, *args)


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode(), DEPLOY_ASYNC=False)
class Redeploy(TestCase):
    def setUp(self):
        from inventory import core_services as cs
        from inventory.models import ConfigTemplate, CoreService, PlatformControl, ServiceImage
        self.site, self.secret = site_with_vms()
        PlatformControl.objects.filter(key="dc1.mgmt").update(mode="execution")
        ServiceImage.objects.create(control=PlatformControl.objects.get(key="dc1.mgmt"), role=ServiceImage.VYOS,
                                    purpose="vyos", kind="image", name="VyOS Router", one_id=171,
                                    customization="context")
        cs.seed("dc1")
        self.svc = CoreService.objects.get(name="cloud-ix-rrs")
        self.assertEqual(self.svc.template.role, "rr-inet")
        self.node = self.svc.nodes.get()
        self.node.hostname = "rdc1-cloudix-rr1"
        self.node.save()
        self.fake = FakeWorld()

    def run_redeploy(self, bgp_ok, force=False, keep_old=True):
        from accounts import tasks
        from inventory import core_deploy as cd
        from inventory import core_services as cs

        def read_node(node, site):
            vm = cs.site_vms(site).get(node.name)
            self.seen_vm = vm and vm[1]  # what the checks compare the VM with
            node.facts = {"checks": [{"check": "BGP (ipv4) peers established", "ok": bgp_ok, "detail": "25/25" if bgp_ok else "0/25"}]}
            node.facts_error = ""
            node.save()
            return True
        with mock.patch("adoption.collector.client_for", return_value=self.fake), \
                mock.patch.object(cd, "_config", return_value="set system host-name x\\n"), \
                mock.patch("inventory.nfconfig.is_complete", return_value=True), \
                mock.patch("inventory.core_services.read_node", side_effect=read_node), \
                mock.patch.object(cd, "MGMT_TIMEOUT", 0), mock.patch.object(cd, "BGP_TIMEOUT", 0):
            return tasks.start("core VM redeploy", "test", lambda t: cd.redeploy(t, self.node, None, 25, force, keep_old))

    def test_redeploy_keeps_the_old_vm_and_moves_the_address(self):
        t = self.run_redeploy(bgp_ok=True)
        self.assertEqual(t.state, "done", t.error)
        w = self.fake.w
        old, new = w.vms[51], w.vms[t.result["new_id"]]
        # correction 09: the new VM takes the definition's name (no underscore) and its host name is exactly that name;
        # the old one is kept as <new name>-old
        self.assertEqual((old["name"], old["state"], old["nics"]), ("cloud-ix-rr1-dc1-old", "8", []))  # kept, powered off
        self.assertEqual(new["name"], "cloud-ix-rr1-dc1")
        self.assertEqual([(n["vid"], n["ip"]) for n in new["nics"]], [(8, "10.0.20.100"), (1, "10.0.10.241")])
        self.assertEqual(self.seen_vm["id"], t.result["new_id"])  # the checks saw the new VM, not the old one
        self.assertIn('IMAGE_ID = "171"', new["template"])
        self.assertIn("ONE_TIME_INIT_SCRIPT_BASE64", new["template"])  # made with it, then emptied once it works
        self.assertEqual(new["context"]["ONE_TIME_INIT_SCRIPT_BASE64"], "")
        self.assertEqual(new["context"]["SET_HOSTNAME"], "cloud-ix-rr1-dc1")  # the rest of the context stays
        self.assertIn('AUTOSTART = "always"', new["template"])  # starts again after its platform went down (2026-10-28)
        self.assertIn('SCHED_REQUIREMENTS = "HYPERVISOR=kvm"', new["template"])  # never a cluster (owner, 2026-10-28)
        self.assertNotIn("CLUSTER_ID", new["template"])
        self.assertIn('MEMORY_RESIZE_MODE = "BALLOONING"', new["template"])
        self.assertEqual([s["status"] for s in t.steps if s["name"] == "init script"], ["ok"])
        from inventory.models import PlatformControl
        vms = {v["name"]: v for v in PlatformControl.objects.get(key="dc1.mgmt").vms}  # read again: the page is current
        self.assertEqual((vms["cloud-ix-rr1-dc1-old"]["status"], vms["cloud-ix-rr1-dc1"]["init_script"]), ("poweroff", False))
        self.node.refresh_from_db()
        self.assertEqual((self.node.address, self.node.ssh_port), ("10.0.20.100", 55522))
        self.assertEqual((self.node.name, self.node.hostname), ("cloud-ix-rr1-dc1", "cloud-ix-rr1-dc1"))
        self.assertEqual(self.node.deployed_vm_id, t.result["new_id"])  # made by the portal: compliant
        from inventory import core_deploy as cd
        from accounts import tasks
        with mock.patch("adoption.collector.client_for", return_value=self.fake):
            t2 = tasks.start("core VM redeploy", "finish", lambda task: cd.finish(task, self.node))
        self.assertEqual(t2.state, "done", t2.error)
        self.assertEqual(w.vms[51]["state"], "6")
        self.assertNotIn("cloud-ix-rr1-dc1-old", [v["name"] for v in PlatformControl.objects.get(key="dc1.mgmt").vms])

    def test_page_buttons_follow_the_platform(self):
        """Remove the old VM / Roll back only while the old VM is there; the init script can be removed by hand."""
        from accounts import tasks
        from inventory import core_deploy as cd
        from inventory import core_services as cs
        from inventory.models import PlatformControl
        u = User.objects.create_user("root@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_super_admin=True,
                                     email_verified=True, totp_secret="JBSWY3DPEHPK3PXP", totp_enabled=True)
        self.client.force_login(u)
        ses = self.client.session
        ses["otp_verified"] = True
        ses.save()
        page = lambda: self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=core").content.decode()
        from inventory.models import Network, PeerGroup
        net = Network.objects.get(site="dc1", name="cloud-ix")
        PeerGroup.objects.update_or_create(network=net, name="rrs", defaults={"session": "ibgp", "password": "secret:999#rrs"})
        html = page()
        self.assertNotIn("Remove the old VM</button>", html)
        self.assertIn("BGP peer groups", html)  # the slots of the network's BGP secret, shown on the service
        self.assertIn("The peer groups of the network cloud-ix", html)
        self.assertEqual(self.svc.bgp_secret(), "secret:999")
        t = self.run_redeploy(bgp_ok=True)
        self.assertEqual(t.state, "done", t.error)
        html = page()
        self.assertIn("Remove the old VM</button>", html)
        self.assertIn("compliant", html)
        self.assertIn("10.0.20.100", html)  # NIC 1 IP: the VM's, not how it is assigned
        self.assertNotIn("Remove the init script</button>", html)
        w = self.fake.w
        w.vms[t.result["new_id"]]["context"]["ONE_TIME_INIT_SCRIPT_BASE64"] = "c2VjcmV0"  # as rr1 and rr2 were made
        c = PlatformControl.objects.get(key="dc1.mgmt")
        with mock.patch("adoption.collector.client_for", return_value=self.fake):
            self.assertEqual(cs.read_init_flags(c, {self.node.name}), 1)
            self.assertIn("Remove the init script</button>", page())
            r = self.client.post(reverse("core_clean", args=["dc1", self.svc.pk, self.node.pk]))
            self.assertEqual(r.status_code, 302)
            self.assertEqual(w.vms[t.result["new_id"]]["context"]["ONE_TIME_INIT_SCRIPT_BASE64"], "")
            tasks.start("core VM redeploy", "finish", lambda task: cd.finish(task, self.node))
            html = page()
        self.assertNotIn("Remove the old VM</button>", html)
        self.assertNotIn("Remove the init script</button>", html)
        with mock.patch("adoption.collector.client_for", return_value=self.fake):
            r = self.client.post(reverse("core_refresh", args=["dc1"]), HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(r.json(), {"ok": True, "errors": []})

    def test_a_definition_that_does_not_fit_stops_it(self):
        self.node.ips = ["", "10.0.10.249"]  # not the running VM's address
        self.node.save()
        t = self.run_redeploy(bgp_ok=True)
        self.assertEqual(t.state, "failed")
        self.assertIn("the defined address on the service network is the one the old VM frees", t.error)
        self.assertEqual((self.fake.w.vms[51]["name"], self.fake.w.vms[51]["state"]), ("cloud-ix-rr1_dc1", "3"))  # untouched

    def test_forced_it_goes_on_and_says_so(self):
        self.node.login_hash = ""
        self.node.save()
        self.svc.ssh_port = 22  # not the site's VyOS port: a "must" check fails
        self.svc.save()
        self.assertEqual(self.run_redeploy(bgp_ok=True).state, "failed")
        t = self.run_redeploy(bgp_ok=True, force=True)
        self.assertEqual(t.state, "done", t.error)
        self.assertIn("forced", [s for s in t.steps if s["name"] == "definition"][0]["detail"])

    def test_the_management_nic_of_a_redeployed_vm_is_no_other_nic(self):
        from inventory import core_deploy as cd
        t = self.run_redeploy(bgp_ok=True)
        self.assertEqual(t.state, "done", t.error)
        with mock.patch("adoption.collector.client_for", return_value=self.fake):
            tasks_finish = cd.finish
            from accounts import tasks
            tasks.start("core VM redeploy", "finish", lambda task: tasks_finish(task, self.node))
            p = cd.plan(self.node)
        other = [c for c in p["checks"] if c["check"].startswith("the running VM has no other NICs")][0]
        self.assertTrue(other["ok"], other)
        mg = [c for c in p["checks"] if c["check"] == "management address"][0]
        self.assertIn("the old VM keeps 10.0.20.100 until it is removed", mg["detail"])
        self.assertEqual(p["old_state"], "running")

    def test_not_keeping_the_old_vm_removes_it_once_the_new_one_works(self):
        t = self.run_redeploy(bgp_ok=True, keep_old=False)
        self.assertEqual(t.state, "done", t.error)
        self.assertTrue(t.result["old_removed"])
        self.assertEqual(self.fake.w.vms[51]["state"], "6")  # gone
        self.assertEqual(self.fake.w.vms[t.result["new_id"]]["state"], "3")

    def test_not_keeping_it_still_rolls_back_when_the_new_one_fails(self):
        t = self.run_redeploy(bgp_ok=False, keep_old=False)
        self.assertEqual(t.state, "failed")
        self.assertEqual((self.fake.w.vms[51]["name"], self.fake.w.vms[51]["state"]), ("cloud-ix-rr1_dc1", "3"))

    def test_a_different_mtu_stops_it(self):
        self.fake.w.vnets[1]["mtu"] = 9050  # the platform's network differs from the site's (1500)
        t = self.run_redeploy(bgp_ok=True)
        self.assertEqual(t.state, "failed")
        self.assertIn("MTU of the service network", t.error)

    def test_rr_templates_leave_the_mtu_to_opennebula(self):
        """Correction 09: the NIC's MTU comes from OpenNebula when the VM is deployed; the template sets none."""
        from inventory import nfconfig
        from inventory.models import Network
        Network.objects.filter(site="dc1", name="cloud-ix").update(mtu=9050, rrs=["10.0.10.241"])
        self.assertNotIn("eth1 mtu", nfconfig.build("rr-inet", site="dc1", index=0))

    def test_zone_mgmt_names_the_vrf(self):
        """VyOS 2026.03 matches a zone's interface in a VRF only with the VRF a member too (T8761): without it the
        management address answered nothing (portal-ix RRs, 2026-10-27)."""
        from inventory import nfconfig
        from inventory.models import ConfigTemplate
        for role in ("rr-inet", "rr-corpnet", "egw", "cgw"):
            body = ConfigTemplate.objects.get(role=role).body
            if "zone MGMT member interface eth0" in body:
                self.assertIn("set firewall zone MGMT member vrf MGMT", body, role)
        self.assertIn("set firewall zone MGMT member vrf MGMT", nfconfig.MGMT_PART)

    def test_every_vyos_template_has_the_same_zones_for_management(self):
        """2026-10-27: every VyOS router has zone-based firewall and the same management part (the tenant igw has no
        management NIC; the site igw gets the same part)."""
        from inventory import nf_templates as T
        from inventory import nfconfig
        for role, _t, os_, _v, body, _d in T.BUILTIN:
            if os_ != "vyos" or role == "igw":
                continue
            self.assertIn(T.MGMT_FW, body, role)
            self.assertNotIn("input filter", body, role)
            self.assertEqual(body.count("set firewall zone GW local-zone"), 1, role)
        self.assertIn(T.MGMT_FW, nfconfig.MGMT_PART)
        # an L2 side: WireGuard only with the other side on eth1, VXLAN only with it in the tunnel
        self.assertIn("set firewall zone S2S member interface eth1", T.L2)
        self.assertIn("set firewall zone TUN member interface wg{{ vni }}", T.L2)
        self.assertIn("set firewall zone IX member interface eth1", T.RR_VXLAN)

    def test_migration_0061_makes_the_stored_templates_the_built_in_ones(self):
        import importlib
        m = importlib.import_module("inventory.migrations.0061_zones_everywhere")
        from inventory import nf_templates as T
        at = lambda body: body.replace(T.MGMT_FW, m.NEW_FW)  # noqa: E731 - the bodies as 0061 made them (0063 came later)
        old = {"rr-vxlan": T.RR_VXLAN, "l2-a": T.L2, "l2-b": T.L2, "l2-br": T.L2BR}
        for role, new in old.items():  # the bodies before: the input filter in place of the zones
            extra = {"rr-vxlan": T.RR_VXLAN_ZONES, "l2-a": T.L2_ZONES, "l2-b": T.L2_ZONES}.get(role, "")
            before = at(new).replace(extra + m.NEW_FW, m.OLD_FILTER)
            self.assertNotEqual(before, at(new))
            self.assertEqual(m.unify(role, before), at(new), role)
        before = at(T.RR_IX).replace(m.NEW_FW, m.OLD_ZONE).replace("set firewall zone IX from GW", m.LOCAL + "set firewall zone IX from GW")
        self.assertEqual(m.unify("rr-inet", before), at(T.RR_IX))
        self.assertEqual(m.unify("rr-inet", at(T.RR_IX)), at(T.RR_IX))  # twice: no change

    def test_the_login_user_gets_the_super_admins_password(self):
        """2026-10-27: console login with the super admin's password (the portal knows it), not the running VM's hash."""
        import base64
        from inventory.crypto import encrypt
        from inventory.models import Secret
        sa = Secret.objects.get(name="sa")
        sa.put("password", "Sa-Pass-123")
        sa.save()
        self.node.login_hash = encrypt("$6$old$hashnobodyknows")
        self.node.save()
        t = self.run_redeploy(bgp_ok=True)
        self.assertEqual(t.state, "done", t.error)
        h = base64.b64decode(self.fake.w.vms[t.result["new_id"]]["context"]["CRYPTED_PASSWORD_BASE64"]).decode()
        self.assertTrue(h.startswith("$6$") and "hashnobodyknows" not in h)
        import subprocess
        salt = h.split("$")[2]
        again = subprocess.run(["openssl", "passwd", "-6", "-salt", salt, "-stdin"], input="Sa-Pass-123",
                               capture_output=True, text=True).stdout.strip()
        self.assertEqual(h, again)  # the super admin's password

    def test_site_time_zone_is_an_iana_one_and_every_vyos_template_sets_it(self):
        """Correction 09 (2026-10-27 19:25): the site's time zone is checked on input (IANA) and every VyOS template sets it."""
        from inventory import nfconfig
        from inventory import nf_templates as T
        from inventory.edit_views import SiteForm
        from inventory.models import Site, site_tz
        self.assertEqual((site_tz("almaty"), site_tz("Asia/Almaty"), site_tz(""), site_tz("Europe/Berlin")),
                         ("Asia/Almaty", "Asia/Almaty", "Asia/Almaty", "Europe/Berlin"))
        f = SiteForm(initial={"timezone": "almaty"})
        self.assertEqual(f.initial["timezone"], "Asia/Almaty")
        self.assertIn(("Asia/Almaty", "Asia/Almaty"), f.fields["timezone"].choices)
        self.assertNotIn("almaty", [v for v, _l in f.fields["timezone"].choices])
        self.assertFalse(f.fields["timezone"].valid_value("almaty"))
        self.assertFalse(SiteForm({"code": "dc7", "country": "KZ", "city": "A", "dc": "x", "timezone": "almaty"}).is_valid())
        self.assertTrue(f.fields["timezone"].valid_value("Asia/Almaty"))
        for role, _t, os_, _v, body, _d in T.BUILTIN:
            if os_ == "vyos":
                self.assertEqual(body.count("set system time-zone {{ time_zone }}"), 1, role)
        Site.objects.filter(code="dc1").update(timezone="Asia/Aqtobe")
        from inventory.models import Network
        Network.objects.filter(site="dc1", name="cloud-ix").update(rrs=["10.0.10.241"])
        self.assertIn("set system time-zone Asia/Aqtobe", nfconfig.build("rr-inet", site="dc1", index=0))

    def test_time_and_logs_go_out_through_management(self):
        """2026-10-27: NTP to the site's servers and syslog to its collector leave through zone MGMT, replies come back."""
        from inventory import nfconfig
        from inventory import nf_templates as T
        out = nfconfig._render(T.MGMT_FW, {"ssh_port": 55522, "ntp": ["10.0.100.201", "10.0.100.202"],
                                            "syslog": {"host": "10.0.101.18", "port": 15142, "proto": "tcp"}})
        for line in ("set firewall group address-group NTP address 10.0.100.202",
                     "set firewall ipv4 name GW-to-MGMT rule 20 destination port 123",
                     "set firewall ipv4 name GW-to-MGMT rule 20 protocol udp",
                     "set firewall ipv4 name GW-to-MGMT rule 30 destination address 10.0.101.18",
                     "set firewall ipv4 name GW-to-MGMT rule 30 destination port 15142",
                     "set firewall ipv4 name GW-to-MGMT rule 30 protocol tcp",
                     "set firewall ipv4 name MGMT-to-GW rule 2 state established",
                     "set firewall ipv4 name MGMT-to-GW rule 3 state related"):
            self.assertIn(line, out)
        bare = nfconfig._render(T.MGMT_FW, {"ssh_port": 55522, "ntp": [], "syslog": None})
        self.assertNotIn("rule 20", bare)
        self.assertNotIn("rule 30", bare)
        self.assertNotIn("address-group NTP", bare)
        import importlib
        m = importlib.import_module("inventory.migrations.0063_mgmt_time_and_logs")
        old = importlib.import_module("inventory.migrations.0061_zones_everywhere").NEW_FW
        self.assertEqual(T.RR_VXLAN.replace(T.MGMT_FW, old).replace(m.OLD_FW, m.NEW_FW), T.RR_VXLAN)

    def test_no_syslog_marker_on_the_routers(self):
        """2026-10-28: no "-- MARK --" lines: every VyOS router with the management part disables the syslog marker, the
        stored templates as well (migration 0067, idempotent)."""
        import importlib
        from inventory import nf_templates as T
        from inventory import nfconfig
        from inventory.models import ConfigTemplate
        self.assertIn("set system syslog marker disable", nfconfig._render(T.MGMT_BASE, {"mgmt": {}, "syslog": None}))
        self.assertIn("set system syslog marker disable", nfconfig.MGMT_PART)
        for t in ConfigTemplate.objects.filter(role__in=["egw", "cgw", "igw", "rr-inet", "rr-corpnet", "rr-vxlan", "l2-a", "l2-b", "l2-br"]):
            self.assertEqual(t.body.count("set system syslog marker disable"), 1, t.role)
        m = importlib.import_module("inventory.migrations.0067_syslog_marker")
        body = "x\n" + m.OLD + "y\n"
        once = body.replace(m.OLD, m.OLD + m.LINE)
        self.assertEqual(once.count(m.LINE), 1)

    def test_ssh_only_in_the_management_vrf(self):
        """2026-10-27: ssh only in VRF MGMT, the service networks only for the service: no template opens ssh elsewhere
        (but the tenant's own igw, under the tenant's responsibility); the site igw leaves the tenant igw's ssh out."""
        from inventory import nf_templates as T
        from inventory import nfconfig
        for role, _t, os_, _v, body, _d in T.BUILTIN:
            if os_ != "vyos" or role == "igw":
                continue
            self.assertIn("set service ssh vrf MGMT", body, role)
            self.assertNotIn("listen-address", body, role)
            self.assertNotIn("SSHFROM", body, role)
        for line in ('set firewall group network-group SSHFROM description "SSH: allowed IP"',
                     'set firewall ipv4 name EXT-to-GW rule 10 destination group port-group SSHPORT',
                     'set firewall group port-group SSHPORT description "GW SSH port"', "set service ssh listen-address 1.2.3.4"):
            self.assertTrue(nfconfig.SITE_IGW_NO_SSH.search(line), line)
        self.assertFalse(nfconfig.SITE_IGW_NO_SSH.search('set firewall group port-group SSHPORT description "SSH port (management)"'))
        self.assertFalse(nfconfig.SITE_IGW_NO_SSH.search("set firewall ipv4 name EXT-to-GW rule 1 protocol icmp"))

    def test_nothing_is_taken_from_the_old_vm(self):
        """2026-10-27: no password hash from the old VM; the new VM's address on the service network is the definition's."""
        from inventory.crypto import encrypt
        self.node.login_hash = encrypt("$6$old$hashnobodyknows")
        self.node.save()
        t = self.run_redeploy(bgp_ok=True)  # the test super admin secret has a key, no password
        self.assertEqual(t.state, "done", t.error)
        new = self.fake.w.vms[t.result["new_id"]]
        self.assertNotIn("CRYPTED_PASSWORD_BASE64", new["context"])
        self.assertIn((1, self.node.ips[1]), [(n["vid"], n["ip"]) for n in new["nics"]])

    def test_an_old_vm_that_ignores_the_soft_power_off_is_powered_off_hard(self):
        """2026-10-27: cloud-vxlan-rr1/rr2 dc1 showed "off" for a moment and ran again; now the redeploy checks it stays
        off and powers it off hard otherwise, so "remove the old VM" works."""
        self.fake.stubborn = {51}
        t = self.run_redeploy(bgp_ok=True, keep_old=False)
        self.assertEqual(t.state, "done", t.error)
        self.assertEqual(self.fake.actions[:2], ["poweroff", "poweroff-hard"])
        self.assertIn("hard: it ignored the soft power-off", " ".join(s["detail"] for s in t.steps))
        self.assertEqual(self.fake.w.vms[51]["state"], "6")  # removed

    def test_remove_an_old_vm_running_without_nics(self):
        from accounts import tasks
        from inventory import core_deploy as cd
        t = self.run_redeploy(bgp_ok=True)
        self.assertEqual(t.state, "done", t.error)
        self.fake.w.vms[51]["state"] = "3"  # it came back running (no NICs: detached)
        with mock.patch("adoption.collector.client_for", return_value=self.fake):
            t2 = tasks.start("core VM redeploy", "finish", lambda task: cd.finish(task, self.node))
        self.assertEqual(t2.state, "done", t2.error)
        self.assertEqual(self.fake.w.vms[51]["state"], "6")

    def test_correction_10_pages(self):
        """Correction 10: the status refresh on every tab of a site; the redeploy keeps the old VM only when asked; the
        VM table of a service has no host name column."""
        u = User.objects.create_user("root@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_super_admin=True,
                                     email_verified=True, totp_secret="JBSWY3DPEHPK3PXP", totp_enabled=True)
        self.client.force_login(u)
        ses = self.client.session
        ses["otp_verified"] = True
        ses.save()
        for tab in ("platforms", "hosts", "networks", "core"):
            h = self.client.get(reverse("infra_site", args=["dc1"]) + f"?tab={tab}").content.decode()
            self.assertIn("Refresh status", h, tab)
        with mock.patch("adoption.collector.client_for", return_value=self.fake):
            r = self.client.post(reverse("core_refresh", args=["dc1"]), {"tab": "hosts"})
        self.assertTrue(r["Location"].endswith("?tab=hosts"))
        h = self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=core").content.decode()
        self.assertNotIn("<th>Host name</th><th>NIC 1 IP", h)
        from django.conf import settings
        with open(settings.BASE_DIR / "templates" / "inventory" / "core_plan.html") as f:
            tpl = f.read()
        self.assertIn('name="keep_old" value="1">', tpl)  # the "keep the old VM" box: not checked by default
        self.assertNotIn('name="keep_old" value="1" checked', tpl)

    def _autostart_spy(self):
        calls = []
        real = self.fake._call

        def spy(method, *args):
            if method == "one.vm.update":
                calls.append((int(args[0]), args[1]))
                self.fake.w.vms[int(args[0])].setdefault("user", {})["AUTOSTART"] = args[1].split('"')[1]
                return int(args[0])
            if method == "one.vm.info" and int(args[0]) in self.fake.w.vms:
                xml = real(method, *args)
                at = self.fake.w.vms[int(args[0])].get("user", {}).get("AUTOSTART")
                return xml.replace("</VM>", f"<USER_TEMPLATE><AUTOSTART>{at}</AUTOSTART></USER_TEMPLATE></VM>") if at else xml
            return real(method, *args)
        self.fake._call = spy
        self.fake.w.vms[51]["user"] = {"AUTOSTART": "always"}  # a VM the portal made before
        return calls

    def test_the_old_vm_does_not_come_back_with_its_host(self):
        """2026-10-28: an old VM stepped aside gets AUTOSTART="no" (a host restart would resume it)."""
        calls = self._autostart_spy()
        t = self.run_redeploy(bgp_ok=True)
        self.assertEqual(t.state, "done", t.error)
        self.assertEqual([c[1] for c in calls if c[0] == 51], ['AUTOSTART = "no"'])

    def test_a_rolled_back_old_vm_gets_its_autostart_back(self):
        calls = self._autostart_spy()
        t = self.run_redeploy(bgp_ok=False)
        self.assertEqual(t.state, "failed")
        self.assertIn("rolled back", t.error)
        self.assertEqual([c[1] for c in calls if c[0] == 51], ['AUTOSTART = "no"', 'AUTOSTART = "always"'])

    def _schedule_spy(self):
        """The old VM has an hourly "resume" (as the VMs made before the portal, 2026-10-28)."""
        calls = []
        real = self.fake._call
        self.fake.w.vms[51]["sched"] = {"0": {"ACTION": "resume", "TIME": "1761292800", "REPEAT": "3", "DAYS": "1",
                                              "END_TYPE": "0", "END_VALUE": "-1"}}

        def spy(method, *args):
            v = self.fake.w.vms.get(int(args[0])) if method in ("one.vm.scheddelete", "one.vm.schedadd", "one.vm.info") else None
            if method == "one.vm.scheddelete":
                calls.append(("remove", int(args[0]), str(args[1]), v["state"]))
                v["sched"].pop(str(args[1]))
                return int(args[0])
            if method == "one.vm.schedadd":
                calls.append(("add", int(args[0]), args[1], v["state"]))
                return int(args[0])
            if method == "one.vm.info" and v is not None and v.get("sched"):
                sa = "".join("<SCHED_ACTION><ID>%s</ID>%s<DONE>-1</DONE></SCHED_ACTION>" % (
                    i, "".join(f"<{k}>{x}</{k}>" for k, x in a.items())) for i, a in v["sched"].items())
                return real(method, *args).replace("<TEMPLATE>", "<TEMPLATE>" + sa, 1)
            return real(method, *args)
        self.fake._call = spy
        return calls

    def test_the_old_vm_loses_its_scheduled_resume(self):
        """Removed before the power-off (it would bring the old VM back within the hour); the new VM gets none."""
        calls = self._schedule_spy()
        t = self.run_redeploy(bgp_ok=True)
        self.assertEqual(t.state, "done", t.error)
        self.assertEqual(calls, [("remove", 51, "0", "3")])  # while still running: before the power-off
        self.assertEqual(self.fake.w.vms[51]["sched"], {})
        self.assertNotIn("SCHED_ACTION", self.fake.w.vms[t.result["new_id"]]["template"])
        step = next(s for s in t.steps if s["name"] == "old VM aside")
        self.assertIn("scheduled actions removed: resume (repeat 3)", step["detail"])

    def test_a_rolled_back_old_vm_gets_its_scheduled_actions_back(self):
        calls = self._schedule_spy()
        t = self.run_redeploy(bgp_ok=False)
        self.assertEqual(t.state, "failed")
        self.assertEqual([c[0] for c in calls], ["remove", "add"])
        added = calls[1][2]
        for part in ('ACTION = "resume"', 'REPEAT = "3"', 'DAYS = "1"', 'END_TYPE = "0"', 'TIME = "1761292800"'):
            self.assertIn(part, added)
        self.assertTrue(added.startswith("SCHED_ACTION = ["))
        self.assertNotIn("DONE", added)
        self.assertEqual(calls[1][3], "3")  # put back once it runs again

    def test_rollback_brings_back_the_old_name(self):
        """A redeploy that fails: the old VM comes back under its own name, the node keeps its name and host name."""
        t = self.run_redeploy(bgp_ok=False)
        self.assertEqual(t.state, "failed")
        self.node.refresh_from_db()
        self.assertEqual((self.node.name, self.node.hostname), ("cloud-ix-rr1_dc1", "rdc1-cloudix-rr1"))
        self.assertEqual(self.fake.w.vms[51]["name"], "cloud-ix-rr1_dc1")

    def test_routers_get_no_name_servers_and_the_collector_as_an_ip(self):
        """A router resolves in its default VRF (no route to the name servers): no name servers, syslog by IP in MGMT."""
        from inventory import nfconfig
        from inventory.models import Network, Site
        Network.objects.filter(site="dc1", name="cloud-ix").update(rrs=["10.0.10.241"])
        Site.objects.filter(code="dc1").update(dns_servers=["10.0.0.53"], ntp_servers=["10.0.0.123"], log_collectors={
            "vyos": {"host": "log1.example.kz", "proto": "tcp", "port": 15142}})
        with mock.patch("socket.gethostbyname", return_value="10.0.9.18"):
            text = nfconfig.build("rr-inet", site="dc1", index=0)
        self.assertNotIn("name-server", text)
        self.assertIn("set service ntp vrf MGMT", text)  # NTP stays, in VRF MGMT
        self.assertIn("set system syslog remote 10.0.9.18 port 15142", text)
        self.assertIn("set system syslog remote 10.0.9.18 vrf MGMT", text)
        self.assertNotIn("set system syslog vrf", text)
        self.assertNotIn("log1.example.kz", text)
        with mock.patch("socket.gethostbyname", side_effect=OSError("no such name")):
            self.assertFalse(nfconfig.is_complete(nfconfig.build("rr-inet", site="dc1", index=0)))  # never by name

    def test_the_time_zone_is_the_sites(self):
        from inventory.models import Site
        Site.objects.filter(code="dc1").update(timezone="almaty")
        t = self.run_redeploy(bgp_ok=True)
        self.assertEqual(t.state, "done", t.error)
        self.assertEqual(self.fake.w.vms[t.result["new_id"]]["context"]["TIMEZONE"], "Asia/Almaty")

    def test_failed_check_rolls_back_by_itself(self):
        t = self.run_redeploy(bgp_ok=False)
        self.assertEqual(t.state, "failed")
        self.assertIn("rolled back", t.error)
        w = self.fake.w
        self.assertEqual((w.vms[51]["name"], w.vms[51]["state"], [n["ip"] for n in w.vms[51]["nics"]]),
                         ("cloud-ix-rr1_dc1", "3", ["10.0.10.241"]))
        self.assertTrue(all(v["state"] == "6" for i, v in w.vms.items() if i != 51))  # the new VM is gone
        self.node.refresh_from_db()
        self.assertEqual(self.node.address, "10.0.10.241")


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode())
class SettingsAndTypes(TestCase):
    """Correction 06, first stage: password and S3 secrets, backup datastores, image types, network types."""
    setUp = CoreServices.setUp

    def test_password_and_s3_secrets(self):
        from inventory.models import Secret
        self.client.post(reverse("infra_secret_add", args=["password"]), {"new-name": "dc1.restic", "new-password": "Restic-Pass-2026"})
        pw = Secret.objects.get(kind="password", name="dc1.restic")
        self.assertEqual(pw.get("password"), "Restic-Pass-2026")
        self.client.post(reverse("infra_secret_add", args=["s3"]), {"new-name": "dc1.s3", "new-s3_key": "AKIA123"})
        self.assertFalse(Secret.objects.filter(kind="s3").exists())  # the secret key is required
        self.client.post(reverse("infra_secret_add", args=["s3"]), {"new-name": "dc1.s3", "new-s3_key": "AKIA123",
                                                                    "new-s3_secret": "s3-secret-value"})
        s3 = Secret.objects.get(kind="s3", name="dc1.s3")
        self.assertEqual((s3.get("s3_key"), s3.get("s3_secret")), ("AKIA123", "s3-secret-value"))
        page = self.client.get(reverse("infra_passwords") + "?tab=s3").content.decode()
        self.assertIn("dc1.s3", page)
        self.assertNotIn("s3-secret-value", page)

    def test_keys_public_or_private(self):
        """Owner 2026-10-29: a key secret is given public or private; from a private key the public one is made."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from inventory.models import Secret
        k = ed25519.Ed25519PrivateKey.generate()
        priv = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()).decode()
        pub = k.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
        self.client.post(reverse("infra_secret_add", args=["key"]), {"new-key": priv, "new-name": "ops-private"})
        s = Secret.objects.get(kind="key", name="ops-private")
        self.assertEqual((s.public_key, s.key_type, s.get("private_key").strip()), (pub, "ssh-ed25519", priv.strip()))
        self.client.post(reverse("infra_secret_add", args=["key"]), {"new-key": "ssh-ed25519 AAAAother me@host"})
        p = Secret.objects.get(kind="key", name="me@host")
        self.assertFalse(p.has("private_key"))
        page = self.client.get(reverse("infra_passwords") + "?tab=key").content.decode()
        self.assertIn("Key type", page)
        self.assertNotIn(priv.splitlines()[1], page)
        n = Secret.objects.filter(kind="key").count()
        self.client.post(reverse("infra_secret_add", args=["key"]), {"new-key": "-----BEGIN OPENSSH PRIVATE KEY-----\nxx\n-----END OPENSSH PRIVATE KEY-----"})
        self.assertEqual(Secret.objects.filter(kind="key").count(), n)  # not a readable private key: nothing kept

    def test_an_s3_backup_datastore_needs_no_volume(self):
        from inventory.models import PlatformDatastore, Secret
        pw = Secret.objects.create(kind=Secret.Kind.PASSWORD, name="dc1.restic")
        s3 = Secret.objects.create(kind=Secret.Kind.S3, name="dc1.s3")
        url = reverse("inv_ds_new", args=["dc1", "mgmt"])
        base = {"name": "mgmt-bkp", "kind": "backups", "backend": "s3", "restic_ip": "10.0.1.60", "restic_secret": pw.ref}
        self.client.post(url, base)
        self.assertFalse(PlatformDatastore.objects.exists())  # S3: endpoint, bucket and secret
        self.client.post(url, {**base, "s3_endpoint": "s3.example.kz", "s3_bucket": "dc1-mgmt-backup", "s3_secret": s3.ref})
        ds = PlatformDatastore.objects.get(name="mgmt-bkp")
        self.assertEqual((ds.volume, ds.backend, ds.s3_endpoint, ds.s3_bucket, ds.s3_secret), (None, "s3", "s3.example.kz",
                                                                                             "dc1-mgmt-backup", s3.ref))

    def test_network_types_one_of_each_core_type_per_site(self):
        from inventory.models import Network
        Network.objects.filter(site="dc1", name="cloud-vyos").update(kind="core-vyos")  # as the migration sets it
        url = reverse("inv_net_new", args=["dc1"])
        base = {"interface": "bond0", "vlan": 230, "mtu": 1500, "subnet": "10.0.230.0", "prefix": 24, "state": "active"}
        self.client.post(url, {**base, "name": "vyos-two", "kind": "core-vyos"})
        self.assertFalse(Network.objects.filter(name="vyos-two").exists())  # dc1 has its core-vyos network
        self.client.post(url, {**base, "name": "nfs-two", "kind": "nfs"})
        self.assertEqual(Network.objects.get(name="nfs-two").kind, "nfs")
        self.assertEqual(Network.kind_for("data-iscsi"), "iscsi")


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode())
class CoreTypes(TestCase):
    """Correction 06, second stage: each type asks only what cannot be calculated."""
    setUp_base = CoreServices.setUp

    def setUp(self):
        from inventory.models import (Network, PlatformControl, PlatformDatastore, Platform, Secret, ServiceImage, Site,
                                      Storage, StorageVolume)
        self.setUp_base()
        Network.objects.filter(site="dc1", name="cloud-vyos").update(kind="core-vyos")
        Network.objects.filter(site="dc1", name="cloud-core").update(kind="core-cloud")
        Network.objects.filter(site="dc1", name="data-nfs").update(kind="nfs")
        Network.objects.create(site="dc1", name="cloud-services", kind="core-services", interface="bond0", vlan=103,
                               subnet="203.0.113.0", prefix=28)
        Network.objects.filter(site="dc1", name="cloud-ix").update(rrs=["10.0.10.241", "10.0.10.242", "10.0.10.243"],
                                                                    announced=["203.0.113.0/24"])
        c = PlatformControl.objects.get(key="dc1.mgmt")
        ServiceImage.objects.create(control=c, role="vyos", purpose="vyos", kind="image", name="VyOS", one_id=171, customization="context")
        ServiceImage.objects.create(control=c, purpose="base-os", kind="image", name="Ubuntu 26.04", customization="ssh")
        Site.objects.filter(code="dc1").update(dns_servers=["203.0.113.3", "203.0.113.4"],
                                               management={"hosts": ["203.0.113.8/29"], "ports": {"vyos": 55522, "ssh": 55523}},
                                               log_collectors={"vyos": {"host": "203.0.113.5", "proto": "tcp", "port": 15142}})
        st = Storage.objects.create(site=Site.objects.get(code="dc1"), name="nas1", kind="nfs", targets=["10.0.111.2"])
        self.vol = StorageVolume.objects.create(storage=st, name="logs-one", device="/export/logs", size_gb=500)
        self.pw = Secret.objects.create(kind="password", name="dc1.restic")
        self.ds = PlatformDatastore.objects.create(platform=Platform.objects.get(key="dc1.mgmt"), name="mgmt-bkp", kind="backups",
                                                   volume=self.vol, backend="iscsi", restic_ip="203.0.113.6",
                                                   restic_secret=self.pw.ref)

    def add(self, stype, **data):
        return self.client.post(reverse("core_new", args=["dc1"]), {"stype": stype, "state": "active", **data}, follow=True)

    def test_bgp_rr_from_its_network(self):
        from inventory.models import CoreService, Network
        ix = Network.objects.get(site="dc1", name="cloud-ix")
        self.add("bgp-rr", network=ix.pk, vcpu=2, ram_gb=4)
        s = CoreService.objects.get(stype="bgp-rr")
        self.assertEqual((s.name, s.template.role, s.platform.key, s.image.name, s.ssh_port), ("cloud-ix-rrs", "rr-inet", "dc1.mgmt", "VyOS", 55522))
        self.assertEqual([n.name for n, _g in s.nic_list()], ["cloud-vyos", "cloud-ix"])
        self.assertEqual([(n.name, n.hostname, n.ips) for n in s.nodes.order_by("pk")],
                         [(f"cloud-ix-rr{i}-dc1", f"cloud-ix-rr{i}-dc1", ["", f"10.0.10.24{i}"]) for i in (1, 2, 3)])
        r = self.add("bgp-rr", network=ix.pk, vcpu=2, ram_gb=4)  # one per network: not offered again
        self.assertEqual(CoreService.objects.filter(stype="bgp-rr").count(), 1)

    def test_ns_admin_logs_backup(self):
        from inventory.models import CoreService
        self.add("ns", vcpu=1, ram_gb=1, disk_gb=5)
        ns = CoreService.objects.get(stype="ns")
        self.assertEqual([(n.name, n.hostname, n.ips) for n in ns.nodes.order_by("pk")],
                         [("core-ns1_dc1", "sdc1ns1", ["203.0.113.3"]), ("core-ns2_dc1", "sdc1ns2", ["203.0.113.4"])])
        self.assertEqual((ns.template.role, ns.image.name, ns.ssh_secret), ("core-services-ns", "Ubuntu 26.04", self.secret.ref))
        self.add("ns", vcpu=1, ram_gb=1, disk_gb=5)
        self.assertEqual(CoreService.objects.filter(stype="ns").count(), 1)  # one per site
        self.add("admin", vcpu=2, ram_gb=8, disk_gb=30, instances=3, first_ip="203.0.113.2", data_volume=self.vol.pk)
        self.assertFalse(CoreService.objects.filter(stype="admin").exists())  # .2 is not a management host
        self.add("admin", vcpu=2, ram_gb=8, disk_gb=30, instances=3, first_ip="203.0.113.9", data_volume=self.vol.pk,
                 home_volume=self.vol.pk, home_subdir="/admins/admin1")
        adm = CoreService.objects.get(stype="admin")
        self.assertEqual([(n.name, n.hostname, n.ips) for n in adm.nodes.order_by("pk")],
                         [(f"core-admin{i}_dc1", f"sdc1admin{i}", [f"203.0.113.{8 + i}", ""]) for i in (1, 2, 3)])
        self.assertEqual([n.name for n, _g in adm.nic_list()], ["cloud-services", "data-nfs"])  # the volume's storage network
        self.assertEqual(adm.ssh_port, 55523)
        self.add("logs", vcpu=6, ram_gb=48, disk_gb=20, volume=self.vol.pk)
        logs = CoreService.objects.get(stype="logs")
        self.assertEqual([(n.name, n.hostname, n.ips) for n in logs.nodes.all()], [("core-logs1_dc1", "sdc1logs", ["203.0.113.5", ""])])
        self.add("backup", vcpu=2, ram_gb=8, disk_gb=10, datastore=self.ds.pk)
        b = CoreService.objects.get(stype="backup")
        self.assertEqual((b.name, b.service_secret, b.serves.key), ("mgmt-backup", self.pw.ref, "dc1.mgmt"))
        self.assertEqual([(n.name, n.hostname, n.ips) for n in b.nodes.all()],
                         [("core-mgmt-backup1_dc1", "sdc1mgmtbackup", ["203.0.113.6", ""])])  # NIC 1: the datastore's restic VM
        page = self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=core").content.decode()
        self.assertIn("core-admin3_dc1", page)
        self.assertIn("Calculated definition", page)

    def test_gateways_and_existing_vms_keep_their_names(self):
        """Correction 11: cloud-gw — one service, its egw and igw; cloud-egw / cloud-igw are not made any more."""
        from inventory.models import CoreService, CoreServiceNode, Network, NetworkRange
        ix = Network.objects.get(site="dc1", name="cloud-ix")
        Network.objects.filter(site="dc1", name="cloud-core").update(gateway="10.0.101.1")  # the core router, not the igw
        self.add("cloud-gw", vcpu=2, ram_gb=4)
        self.assertFalse(CoreService.objects.filter(stype="cloud-gw").exists())  # no cloud-gw range on the exchange yet
        NetworkRange.objects.create(network=ix, name="cloud-gw", start="10.0.10.9", length=1)
        self.add("cloud-egw", vcpu=2, ram_gb=4)
        self.assertFalse(CoreService.objects.filter(stype="cloud-egw").exists())
        self.add("cloud-gw", vcpu=2, ram_gb=4, aliases="203.0.113.3 203.0.113.4",
                 config_diff="set nat destination rule 111 destination address '203.0.113.2'")
        gw = CoreService.objects.get(stype="cloud-gw")
        egw, igw = gw.nodes.order_by("pk")
        self.assertEqual((egw.name, egw.hostname, egw.ips, egw.role), ("cloud-egw-dc1", "cloud-egw-dc1", ["", "10.0.10.9", "203.0.113.1"], "egw"))
        self.assertEqual((igw.name, igw.ips, igw.role), ("cloud-igw-dc1", ["", "203.0.113.2", "10.0.101.254"], "igw"))  # the last address of core-cloud
        self.assertEqual([n.name for n, _g in egw.nic_list()], ["cloud-vyos", "cloud-ix", "cloud-services"])
        self.assertEqual([n.name for n, _g in igw.nic_list()], ["cloud-vyos", "cloud-services", "cloud-core"])
        self.assertEqual(igw.params["aliases"], ["203.0.113.3", "203.0.113.4"])
        self.assertEqual((egw.params["checks"], igw.params["checks"]), ({"bgp": "ipv4"}, {}))
        from inventory import core_deploy as cd
        e = cd.config_of(egw)
        self.assertIn("set protocols bgp parameters router-id 10.0.10.9", e)  # the exchange address of the cloud-gw range
        i = cd.config_of(igw)
        self.assertIn("set nat destination rule 111 destination address '203.0.113.2'", i)  # the custom part
        self.assertIn("vrf MGMT", i)
        self.assertNotIn("nat destination", e)
        # a running VM keeps its name; the calculated one is for the next redeploy
        from inventory import core_types as ct
        CoreServiceNode.objects.filter(pk=egw.pk).update(name="cloud-egw_dc1")
        d = ct.definition("dc1", "cloud-gw", gw.params, existing=gw)
        self.assertEqual((d["nodes"][0]["name"], d["nodes"][0]["target_name"]), ("cloud-egw_dc1", "cloud-egw-dc1"))


class GwWorld(FakeWorld):
    """The site's networks and the earlier standard's cloud-egw_dc1 / cloud-igw_dc1 (the igw with aliases and a temporary
    cloud-ix NIC)."""

    def __init__(self):
        from tenants.fakeone import World
        self.w = World({"one.clusterpool.info": "<CLUSTER_POOL><CLUSTER><ID>0</ID><NAME>mgmt_dc1</NAME></CLUSTER></CLUSTER_POOL>"},
                       vnets=[{"id": 1, "name": "cloud-ix_dc1", "ars": [{"TYPE": "IP4", "IP": "10.0.10.1", "SIZE": 250}]},
                              {"id": 3, "name": "cloud-services_dc1", "ars": [{"TYPE": "IP4", "IP": "203.0.113.1", "SIZE": 14}]},
                              {"id": 4, "name": "cloud-core_dc1", "ars": [{"TYPE": "IP4", "IP": "10.0.101.1", "SIZE": 254}]},
                              {"id": 8, "name": "cloud-vyos_dc1", "ars": [{"TYPE": "IP4", "IP": "10.0.20.100", "SIZE": 20}]}],
                       vms=[{"id": 163, "name": "cloud-egw_dc1", "nics": [{"network_id": 1, "ip": "10.0.10.9"},
                                                                         {"network_id": 3, "ip": "203.0.113.1"}]},
                            {"id": 162, "name": "cloud-igw_dc1", "nics": [{"network_id": 3, "ip": "203.0.113.2"},
                                                                         {"network_id": 4, "ip": "10.0.101.254"},
                                                                         {"network_id": 1, "ip": "10.0.10.8"}]}])
        for ip in ("203.0.113.3", "203.0.113.4"):
            self.w._nic(162, 3, ip, parent="NIC0")


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode(), DEPLOY_ASYNC=False)
class GwRedeploy(TestCase):
    """Correction 11: the egw and igw of cloud-gw made again; the old VMs give up only the addresses the new ones take."""
    setUp_base = CoreServices.setUp
    setUp_types = CoreTypes.setUp
    add = CoreTypes.add

    def setUp(self):
        from inventory.models import CoreService, Network, NetworkRange, PlatformControl
        self.setUp_types()
        PlatformControl.objects.filter(key="dc1.mgmt").update(mode="execution")
        NetworkRange.objects.create(network=Network.objects.get(site="dc1", name="cloud-ix"), name="cloud-gw",
                                    start="10.0.10.9", length=1)
        self.add("cloud-gw", vcpu=2, ram_gb=4, aliases="203.0.113.3 203.0.113.4",
                 config_diff="set nat destination rule 111 destination address '203.0.113.2'")
        self.egw, self.igw = CoreService.objects.get(stype="cloud-gw").nodes.order_by("pk")
        self.fake = GwWorld()

    def run_redeploy(self, node, ok=True, keep_old=True, how="redeploy"):
        from accounts import tasks
        from inventory import core_deploy as cd

        def read_node(n, site):  # as the real one: the facts made again, the redeploy record kept
            n.facts = {"checks": [{"check": "BGP (ipv4) peers established", "ok": ok, "detail": "3/3" if ok else "0/3"}],
                       **({"redeploy": n.facts["redeploy"]} if "redeploy" in (n.facts or {}) else {})}
            n.facts_error = "" if ok or n.role == "egw" else "ssh: timed out"
            n.save()
            return True
        run = {"redeploy": lambda t: cd.redeploy(t, node, None, None, False, keep_old),
               "rollback": lambda t: cd.rollback(t, node),
               "plan": lambda t: cd.plan(node)}[how]
        with mock.patch("adoption.collector.client_for", return_value=self.fake), \
                mock.patch("inventory.nfconfig.is_complete", return_value=True), \
                mock.patch("inventory.core_services.read_node", side_effect=read_node), \
                mock.patch.object(cd, "MGMT_TIMEOUT", 0), mock.patch.object(cd, "BGP_TIMEOUT", 0):
            return tasks.start("core VM redeploy", "test", run)

    def nics(self, vm_id):
        return [(n["vid"], n["ip"], n["parent"]) for n in self.fake.w.vms[vm_id]["nics"]]

    def test_plan_reads_only(self):
        t = self.run_redeploy(self.igw, how="plan")
        self.assertEqual(t.state, "done", t.error)
        p = t.result
        self.assertEqual((p["old_id"], p["new_name"], p["aliases"]), (162, "cloud-igw-dc1", ["203.0.113.3", "203.0.113.4"]))
        self.assertEqual([(n["net"], n["ip"]) for n in p["new_nics"]],
                         [("cloud-vyos_dc1", "10.0.20.100"), ("cloud-services_dc1", "203.0.113.2"), ("cloud-core_dc1", "10.0.101.254")])
        self.assertEqual(sorted(k["ip"] for k in p["kept"]), ["10.0.101.254", "203.0.113.2", "203.0.113.3", "203.0.113.4"])
        self.assertTrue(p["definition_ok"], p["checks"])
        self.assertFalse([m for m, _a in self.fake.w.calls if m not in ("one.vmpool.info", "one.vm.info", "one.vnpool.info",
                                                                         "one.vn.info", "one.clusterpool.info")])
        svc = self.igw.service
        with mock.patch("adoption.collector.client_for", return_value=self.fake):
            page = self.client.get(reverse("core_plan", args=["dc1", svc.pk, self.igw.pk]))
            check = self.client.get(reverse("core_check", args=["dc1", svc.pk]))
        self.assertContains(page, "cloud-igw-dc1")
        self.assertContains(page, "203.0.113.4")
        self.assertNotContains(page, "BGP peers expected")
        self.assertEqual(check.status_code, 200)
        self.assertContains(self.client.get(reverse("core_config", args=["dc1", svc.pk, self.igw.pk])), "rule 111")
        site = self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=core")
        self.assertContains(site, "cloud-egw-dc1")
        self.assertContains(site, reverse("core_plan", args=["dc1", svc.pk, self.egw.pk]))  # redeployable

    def test_igw_takes_its_addresses_and_aliases_the_exchange_nic_stays_with_the_old_vm(self):
        t = self.run_redeploy(self.igw, keep_old=False)
        self.assertEqual(t.state, "done", t.error)
        w = self.fake.w
        new = t.result["new_id"]
        self.assertEqual(w.vms[new]["name"], "cloud-igw-dc1")
        self.assertEqual(w.vms[new]["context"]["SET_HOSTNAME"], "cloud-igw-dc1")
        self.assertEqual(self.nics(new), [(8, "10.0.20.100", ""), (3, "203.0.113.2", ""), (4, "10.0.101.254", ""),
                                          (3, "203.0.113.3", "VMNIC1"), (3, "203.0.113.4", "VMNIC1")])
        self.assertIn('AUTOSTART = "always"', w.vms[new]["template"])
        self.assertIn('SCHED_REQUIREMENTS = "HYPERVISOR=kvm"', w.vms[new]["template"])
        self.assertIn('MEMORY_RESIZE_MODE = "BALLOONING"', w.vms[new]["template"])
        self.assertEqual(w.vms[162]["state"], "6")  # not kept: removed once the new one works
        self.igw.refresh_from_db()
        self.assertEqual((self.igw.name, self.igw.address, self.igw.deployed_vm_id), ("cloud-igw-dc1", "10.0.20.100", new))

    def test_egw_checks_bgp_and_a_failure_gives_everything_back(self):
        t = self.run_redeploy(self.egw, ok=False)
        self.assertEqual(t.state, "failed")
        self.assertIn("rolled back", t.error)
        w = self.fake.w
        self.assertEqual((w.vms[163]["name"], w.vms[163]["state"]), ("cloud-egw_dc1", "3"))
        self.assertEqual(sorted(self.nics(163)), [(1, "10.0.10.9", ""), (3, "203.0.113.1", "")])
        self.assertTrue(all(v["state"] == "6" for i, v in w.vms.items() if i not in (162, 163)))
        self.egw.refresh_from_db()
        self.assertEqual(self.egw.name, "cloud-egw-dc1")  # its name in the portal; the VM is found by the earlier one too

    def test_kept_old_igw_rolled_back_by_hand_gets_its_aliases_on_its_nic(self):
        t = self.run_redeploy(self.igw)
        self.assertEqual(t.state, "done", t.error)
        w = self.fake.w
        self.assertEqual((w.vms[162]["name"], w.vms[162]["state"]), ("cloud-igw-dc1-old", "8"))
        self.assertEqual(self.nics(162), [(1, "10.0.10.8", "")])  # the temporary exchange NIC stays with it
        self.igw.refresh_from_db()
        t = self.run_redeploy(self.igw, how="rollback")
        self.assertEqual(t.state, "done", t.error)
        self.assertEqual((w.vms[162]["name"], w.vms[162]["state"]), ("cloud-igw_dc1", "3"))
        nics = w.vms[162]["nics"]
        cs = next(n for n in nics if n["ip"] == "203.0.113.2")
        self.assertEqual(sorted((n["ip"], n["parent"]) for n in nics if n["alias"]),
                         [("203.0.113.3", cs["name"]), ("203.0.113.4", cs["name"])])
        self.assertEqual(w.vms[t.result["old_id"]]["name"], "cloud-igw_dc1")
        self.assertFalse([i for i, v in w.vms.items() if v["name"] == "cloud-igw-dc1" and v["state"] != "6"])


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode(), DEPLOY_ASYNC=False)
class PortalGw(TestCase):
    """Correction 12: portal-gw — the two egws of a portal platform, VRRP on its public overlays, cloud-igw template."""
    setUp_base = CoreServices.setUp
    setUp_types = CoreTypes.setUp
    add = CoreTypes.add

    def setUp(self):
        from inventory.models import (Cluster, DelegatedSubnet, Network, Platform, PlatformControl, ServiceImage, Site)
        self.setUp_types()
        site = Site.objects.get(code="dc1")
        self.portal = Platform.objects.create(key="dc1.portal", site=site, name="portal", kind="ext-managed",
                                              sa=Platform.objects.get(key="dc1.mgmt").sa, segments=["portal-ix"])
        Cluster.objects.create(platform=self.portal, name="shared-intel-1_dc1", kind="ext-managed")
        c = PlatformControl.objects.create(key="dc1.portal", mode="execution")
        ServiceImage.objects.create(control=c, role="vyos", purpose="vyos", kind="image", name="VyOS", one_id=5,
                                    customization="context")
        Network.objects.create(site="dc1", name="portal-ix", kind="ix", interface="bond0", vlan=109, subnet="10.0.109.0",
                               prefix=24, rrs=["10.0.109.241", "10.0.109.242"], local_asn=4200101092)
        DelegatedSubnet.objects.create(site=site, segment="portal-ix", cidr="198.51.100.0/25", purpose="inet")

    def test_definition_vrrp_and_plan(self):
        from inventory import core_deploy as cd
        from inventory.models import CoreService, Network
        self.add("portal-gw", vcpu=2, ram_gb=4)
        svc = CoreService.objects.get(stype="portal-gw")
        self.assertEqual(svc.platform.key, "dc1.portal")  # on the portal platform, not the management one
        e1, e2 = svc.nodes.order_by("pk")
        self.assertEqual((e1.name, e1.ips, e2.name, e2.ips),
                         ("portal-egw1-dc1", ["", "", "198.51.100.2"], "portal-egw2-dc1", ["", "", "198.51.100.3"]))
        pub = Network.objects.get(site="dc1", name="portal-public01")
        self.assertEqual((pub.kind, pub.cidr, pub.gateway), ("public", "198.51.100.0/25", "198.51.100.1"))
        self.assertEqual([n.name for n, _g in e1.nic_list()], ["cloud-vyos", "portal-ix", "portal-public01"])
        c1, c2 = cd.config_of(e1), cd.config_of(e2)
        for line in ("set high-availability vrrp group PUB01 interface eth2", "set high-availability vrrp group PUB01 vrid 1",
                     "set high-availability vrrp group PUB01 address 198.51.100.1/25",
                     "set firewall ipv4 name CLIENT-to-GW rule 20 protocol vrrp",
                     "set protocols bgp neighbor 10.0.109.241 peer-group cloud-int",
                     "set firewall group network-group CLIENT network 198.51.100.0/25"):
            self.assertIn(line, c1)
        self.assertIn("set high-availability vrrp group PUB01 priority 200", c1)
        self.assertIn("set high-availability vrrp group PUB01 priority 100", c2)
        # the plan reads the portal platform: no cloud-vyos there yet (must), portal-ix free address, the egw's public
        # address added as an address range by the redeploy, the VIP
        from tenants.fakeone import World
        w = World({"one.clusterpool.info": "<CLUSTER_POOL><CLUSTER><ID>0</ID><NAME>shared-intel-1_dc1</NAME></CLUSTER></CLUSTER_POOL>",
                   "one.vntemplatepool.info": "<VNTEMPLATE_POOL><VNTEMPLATE><ID>0</ID><NAME>portal-overlays_dc1</NAME></VNTEMPLATE></VNTEMPLATE_POOL>"},
                  vnets=[{"id": 1, "name": "portal-ix_dc1", "ars": [{"TYPE": "IP4", "IP": "10.0.109.10", "SIZE": 20}]},
                         {"id": 0, "name": "portal-public01_dc1", "ars": [{"TYPE": "IP4", "IP": "198.51.100.1", "SIZE": 1},
                                                                          {"TYPE": "IP4", "IP": "198.51.100.8", "SIZE": 100}]}],
                  vms=[{"id": 0, "name": "portal-egw1_dc1", "nics": [{"network_id": 1, "ip": "10.0.109.10"},
                                                                     {"network_id": 0, "ip": "198.51.100.1"}]}])
        with mock.patch("adoption.collector.client_for", return_value=w), \
                mock.patch("inventory.nfconfig.is_complete", return_value=True):
            p = cd.plan(e1)
        checks = {c["check"]: c for c in p["checks"]}
        self.assertFalse(checks["network cloud-vyos_dc1 on dc1.portal"]["ok"])  # Q34: no management network there
        self.assertEqual(checks["address on portal-ix_dc1"]["detail"], "free address 10.0.109.11")
        self.assertEqual(p["portal"]["add_ar"], [{"net_id": 0, "net": "portal-public01_dc1", "ip": "198.51.100.2"}])
        self.assertEqual(p["portal"]["vips"][0]["vip"], "198.51.100.1")
        self.assertEqual(p["kept"], [])  # the old egw keeps its NICs (its .1 becomes the VIP once it is removed)
        self.assertFalse(p["definition_ok"])

    def test_cloud_igw_template(self):
        """The site igw comes from the cloud-igw template (the tenant igw + the management part); its config addon."""
        from inventory import nf_templates as T
        from inventory.models import ConfigTemplate
        t = ConfigTemplate.objects.get(role="cloud-igw")
        self.assertIn("set interfaces ethernet eth0 vrf MGMT", t.body)
        self.assertIn(T.CLOUD_IGW_MGMT, t.body)
        self.assertEqual(t.scope, "site")


class VyosImageScripts(TestCase):
    """The VyOS image's scripts in Templates (2026-10-27): kept as they are, shown without a preview."""

    def test_kept_as_they_are_and_shown_without_preview(self):
        import hashlib
        import importlib
        from inventory.models import ConfigTemplate
        from inventory.template_views import TemplateForm
        m = importlib.import_module("inventory.migrations.0062_vyos_image_scripts")
        prep = ConfigTemplate.objects.get(role="vyos-image-prepare")
        boot = ConfigTemplate.objects.get(role="vyos-postconfig-bootup")
        self.assertEqual(hashlib.md5(prep.body.encode()).hexdigest(), "94f28c8405e17a1195bec2ffbf1b0658")
        self.assertEqual(hashlib.md5(boot.body.encode()).hexdigest(), "6885dd13d03609259fbc6160330444b9")
        self.assertEqual((prep.scope, boot.scope), ("image", "image"))
        self.assertEqual(boot.body, m.BOOTUP)
        f = TemplateForm({"name": boot.name, "os": "vyos", "os_version": "2026.03", "description": "", "body": boot.body},
                         instance=boot)
        self.assertTrue(f.is_valid(), f.errors)  # a bash script is valid as a template body (nothing to render)
        self.assertEqual(f.cleaned_data["body"], boot.body)
        u = User.objects.create_user("root@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_super_admin=True,
                                     email_verified=True, totp_secret="JBSWY3DPEHPK3PXP", totp_enabled=True)
        self.client.force_login(u)
        ses = self.client.session
        ses["otp_verified"] = True
        ses.save()
        r = self.client.get(reverse("infra_templates") + "?preview=vyos-postconfig-bootup&site=dc1")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("A script of the VyOS image, kept as it is", html)
        self.assertIn("ONE_TIME_INIT_SCRIPT_BASE64", html)

