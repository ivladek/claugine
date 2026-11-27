"""Core services of a site: the standard layout from the platform's VMs, the form, reading (ssh mocked), the tab."""

import re
from unittest import mock

from cryptography.fernet import Fernet
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Kind, User


def site_with_vms():
    from inventory.models import Cluster, ConfigTemplate, Host, Network, Platform, PlatformControl, Secret, Site

    st = Site.objects.create(code="dc1", management={"ports": {"vyos": 55522, "ssh": 55523}})
    mgmt = Platform.objects.create(key="dc1.mgmt", site=st, name="mgmt", kind="mgmt", sa="secret:1")
    cl = Cluster.objects.create(platform=mgmt, name="mgmt_dc1", kind="mgmt")
    for i in (1, 2, 3):
        cl.hosts.add(Host.objects.create(site=st, name=f"hdc1kvm20{i}", ip=f"10.0.100.20{i}"))
    Network.objects.create(
        site="dc1",
        name="cloud-core",
        kind="core",
        interface="bond0",
        vlan=101,
        subnet="10.0.101.0",
        prefix=24,
        gateway="10.0.101.254",
    )
    Network.objects.create(
        site="dc1", name="data-nfs", kind="storage", interface="bond1", vlan=111, subnet="10.0.111.0", prefix=24
    )
    Network.objects.create(
        site="dc1", name="cloud-ix", kind="ix", interface="bond0", vlan=10, subnet="10.0.10.0", prefix=24
    )
    Network.objects.create(
        site="dc1",
        name="cloud-vyos",
        kind="core",
        interface="bond0",
        vlan=20,
        subnet="10.0.20.0",
        prefix=24,
        gateway="10.0.20.1",
    )
    assert ConfigTemplate.objects.filter(role="rr-ix").exists()  # made by a migration
    s = Secret.objects.create(kind="user", name="sa", username="opsuser")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    pem = (
        ed25519.Ed25519PrivateKey.generate()
        .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption())
        .decode()
    )
    s.put("private_key", pem)
    s.save()
    Platform.objects.filter(pk=mgmt.pk).update(sa=s.ref)
    vm = lambda name, ip, nets=("cloud-core_dc1",), vcpu=1, ram=1.0, disk=5: {  # noqa: E731
        "name": name,
        "id": 1,
        "vcpu": vcpu,
        "ram_gb": ram,
        "disks_gb": [disk],
        "nics": [[n, ip if i == 0 else "10.0.111.9"] for i, n in enumerate(nets)],
        "state": "3",
        "lcm_state": "3",
    }
    PlatformControl.objects.create(
        key="dc1.mgmt",
        vms=[
            vm("cloud-ns1_dc1", "10.0.101.11"),
            vm("cloud-ns2_dc1", "10.0.101.12"),
            vm("cloud-one1_dc1", "10.0.101.31", ("cloud-core_dc1", "data-nfs_dc1"), 8, 32.0, 60),
            vm("cloud-one2_dc1", "10.0.101.32", ("cloud-core_dc1", "data-nfs_dc1"), 8, 16.0, 60),
            vm("cloud-ix-rr1_dc1", "10.0.10.241", ("cloud-ix_dc1",), 2, 4.0, 2),
            vm("cloud-gitlab1_dc1", "10.0.101.50"),
            vm("tenant_x-egw_dc1", "10.0.10.30"),
            {
                "name": "cloud-igw_dc1",
                "id": 9,
                "vcpu": 1,
                "ram_gb": 2.0,
                "disks_gb": [2],
                "state": "3",
                "lcm_state": "3",
                "nics": [
                    ["cloud-services_dc1", "203.0.113.2"],
                    ["cloud-core_dc1", "10.0.101.254"],
                    ["cloud-ix_dc1", "10.0.10.8"],
                ],
            },
        ],
    )
    return st, s


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode())
class CoreServices(TestCase):
    def setUp(self):
        self.site, self.secret = site_with_vms()
        self.sa = User.objects.create_user(
            "sa@corp.kz",
            "Str0ng-PASS-123",
            kind=Kind.STAFF,
            email_verified=True,
            is_super_admin=True,
            totp_enabled=True,
        )
        self.client.force_login(self.sa)
        s = self.client.session
        s["otp_verified"] = True
        s.save()

    def test_standard_layout_from_the_platform_vms(self):
        from inventory import core_services as cs
        from inventory.models import CoreService

        made = cs.seed("dc1")
        self.assertEqual(
            sorted(made), ["cloud-ix-rrs", "ns"]
        )  # only ns, logs, admin, backup and the RRs (correction 06)
        self.assertEqual({s.name: s.stype for s in CoreService.objects.all()}, {"cloud-ix-rrs": "bgp-rr", "ns": "ns"})
        ns = CoreService.objects.get(name="ns")
        self.assertEqual(
            (ns.kind, ns.vcpu, float(ns.ram_gb), ns.disk_gb, ns.nic1_network.name, ns.nic1_gateway, ns.serves),
            ("vm", 1, 1.0, 5, "cloud-core", None, None),
        )  # the subnet's gateway; a core service
        self.assertEqual([n.ips for n in ns.nodes.all()], [["10.0.101.11"], ["10.0.101.12"]])
        self.assertTrue(ns.description.startswith("Name servers"))
        self.assertEqual(
            [(n.name, n.address, n.ssh_port, n.secret) for n in ns.nodes.all()],
            [
                ("cloud-ns1_dc1", "10.0.101.11", 22, self.secret.ref),
                ("cloud-ns2_dc1", "10.0.101.12", 22, self.secret.ref),
            ],
        )
        rr = CoreService.objects.get(name="cloud-ix-rrs")
        self.assertEqual(rr.params["network"], rr.service_network().pk)
        self.assertEqual(
            (rr.template.role, rr.nodes.get().ssh_port, rr.ssh_port, rr.ssh_secret),
            ("rr-ix", 55522, 55522, self.secret.ref),
        )
        self.assertEqual([n.name for n, _gw in rr.nic_list()], ["cloud-vyos", "cloud-ix"])  # management first (VyOS)
        self.assertEqual(rr.nodes.get().ips, ["", "10.0.10.241"])  # its management address: assigned when made
        self.assertEqual(
            (rr.profile.key, ns.profile.key), ("nf-ix", "ns-primary")
        )  # commit-412: linked as a save links
        self.assertEqual(cs.seed("dc1"), [])  # existing services are never changed

    def test_418_the_command_names_what_it_skipped(self):
        from io import StringIO

        from django.core.management import call_command

        from inventory.models import ServiceProfile

        ServiceProfile.objects.filter(key="nf-ix").update(install_template="other")
        out = StringIO()
        call_command("core_services", "seed", "dc1", stdout=out)
        text = out.getvalue()
        self.assertIn("made: ns", text)
        self.assertIn("skipped: cloud-ix-rrs: no service profile of bgp-rr names the template rr-ix", text)

    def test_412_no_profile_for_the_template_nothing_made(self):
        from inventory import core_services as cs
        from inventory.models import ConfigTemplate, CoreService, ServiceProfile

        ServiceProfile.objects.filter(key="nf-ix").update(install_template="other")  # no profile names rr-ix
        ConfigTemplate.objects.filter(role="rr-ix").delete()  # and its template row is missing too
        skipped = []
        made = cs.seed("dc1", skipped=skipped)
        self.assertEqual(made, ["ns"])
        self.assertFalse(CoreService.objects.filter(stype="bgp-rr").exists())
        self.assertIn("cloud-ix-rrs: no service profile of bgp-rr names the template rr-ix", skipped[0])

    @override_settings(DEPLOY_ASYNC=False)
    def test_read_is_read_only_and_flags_a_different_size(self):
        from inventory import core_services as cs
        from inventory.models import CoreService, CoreServiceNode

        with mock.patch("inventory.core_services.seed_type", return_value="core-vm"):  # all of them, as before 06
            cs.seed("dc1")
        out = (
            "##host\ncloud-one1\n##os\nUbuntu 24.04.3 LTS\n##cpu\n8\n##mem\n32092\n##disk\n59G\n"
            "##addr\neth0 10.0.101.31/24\neth1 10.0.111.9/24\n"
        )
        calls = []

        def run(host, port, user, key, remote, **kw):
            calls.append((host, port, user, remote))
            if host == "10.0.101.32":
                from inventory.sshexec import SshError

                raise SshError("10.0.101.32:22: Connection refused")
            return out

        with (
            mock.patch("inventory.core_services.read_platform_vms", side_effect=lambda c: c.vms),
            mock.patch("inventory.sshexec.run", side_effect=run),
        ):
            r = self.client.post(reverse("core_read", args=["dc1", CoreService.objects.get(name="shared-fe").pk]))
        self.assertEqual(r.status_code, 302)
        self.assertTrue(all("vbash" not in c[3] and "set " not in c[3] for c in calls))  # nothing is changed
        n1 = CoreServiceNode.objects.get(name="cloud-one1_dc1")
        self.assertEqual(
            (n1.facts["hostname"], n1.facts["vcpu"], n1.facts["ram_gb"], n1.facts["disk_gb"]), ("cloud-one1", 8, 31, 59)
        )
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
        img = ServiceImage.objects.create(
            control=PlatformControl.objects.get(key="dc1.mgmt"),
            purpose="base",
            kind="image",
            name="Ubuntu 24.04",
            customization="ssh",
        )
        r = self.client.post(
            reverse("core_new", args=["dc1"]),
            {
                "name": "graylog",
                "description": "Log collector",
                "kind": "vm",
                "state": "deployed",
                "platform": str(mgmt.pk),
                "serves": "",
                "nodes": "cloud-onelog1_dc1 | onelog1 | 10.0.101.18 | 10.0.111.18",
                "ssh_port": 22,
                "ssh_secret": self.secret.ref,
                "service_secret": self.secret.ref,
                "image": str(img.pk),
                "vcpu": 12,
                "ram_gb": "48",
                "disk_gb": 20,
                "nic1_network": str(core.pk),
                "more_nics": "data-nfs",
                "routes": "1 10.0.0.0/8 10.0.101.1 20\n10.9.0.0/16 10.0.101.1",
                "firewall": "9000 tcp 10.0.0.0/8\n514 udp",
            },
        )
        self.assertEqual(r.status_code, 302)
        g = CoreService.objects.get(name="graylog")
        n = g.nodes.get()
        self.assertEqual(
            (n.address, n.hostname, n.ips, n.ssh_port, n.secret),
            ("10.0.101.18", "onelog1", ["10.0.101.18", "10.0.111.18"], 22, self.secret.ref),
        )
        self.assertEqual([x.name for x, _g in g.nic_list()], ["cloud-core", "data-nfs"])
        self.assertEqual(
            g.routes,
            [
                {"nic": 1, "subnet": "10.0.0.0/8", "via": "10.0.101.1", "metric": 20},
                {"nic": 1, "subnet": "10.9.0.0/16", "via": "10.0.101.1"},
            ],
        )
        self.assertEqual(
            g.firewall, [{"port": "9000", "proto": "tcp", "source": "10.0.0.0/8"}, {"port": "514", "proto": "udp"}]
        )
        self.assertEqual(
            (g.image, g.platform.key, g.ram_gb, g.service_secret, g.serves),
            (img, "dc1.mgmt", 48, self.secret.ref, None),
        )
        self.assertTrue(AuditLog.objects.filter(action="core_service_saved").exists())
        page = self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=core")
        self.assertContains(page, "core (common)")
        self.assertNotContains(page, "Deployed VMs")  # correction 03: one VMs table with status and details
        self.assertContains(page, 'data-for="cn-')  # T34 B4: a VM is selected; its details below the VMs
        self.assertContains(page, "data-cs-form")
        # from scratch needs a NIC 1 subnet; a route needs its NIC; a row needs a name; no more addresses than NICs
        self.client.post(
            reverse("core_save", args=["dc1", g.pk]),
            {"name": "graylog", "kind": "vm", "state": "deployed", "ssh_port": 22, "nodes": "a | b | c"},
        )
        self.client.post(
            reverse("core_save", args=["dc1", g.pk]),
            {
                "name": "graylog",
                "kind": "vm",
                "state": "deployed",
                "ssh_port": 22,
                "nic1_network": str(core.pk),
                "platform": str(mgmt.pk),
                "nodes": "cloud-onelog1_dc1 | | 10.0.101.19",
                "routes": "2 10.0.0.0/8 10.0.101.1",
            },
        )
        self.client.post(
            reverse("core_save", args=["dc1", g.pk]),
            {
                "name": "graylog",
                "kind": "vm",
                "state": "deployed",
                "ssh_port": 22,
                "nic1_network": str(core.pk),
                "platform": str(mgmt.pk),
                "nodes": "cloud-onelog1_dc1 | | 10.0.101.19 | 10.0.111.19",
            },
        )
        self.assertEqual(CoreService.objects.get(pk=g.pk).nodes.get().ips, ["10.0.101.18", "10.0.111.18"])
        # empty address: assigned when the VM is made; the login address stays as it is
        self.client.post(
            reverse("core_save", args=["dc1", g.pk]),
            {
                "name": "graylog",
                "kind": "vm",
                "state": "deployed",
                "ssh_port": 22,
                "nic1_network": str(core.pk),
                "platform": str(mgmt.pk),
                "more_nics": "data-nfs",
                "nodes": "cloud-onelog1_dc1 | onelog1 | auto | 10.0.111.18",
                "image": str(img.pk),
            },
        )
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

        c = (
            PlatformControl.objects.get(key="dc1.cloud")
            if PlatformControl.objects.filter(key="dc1.cloud").exists()
            else PlatformControl.objects.create(key="dc1.cloud")
        )
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
        self.client.post(
            reverse("service_image_save", args=[c.key, rows["vyos"].pk]),
            {"name": "VyOS 2026.06", "customization": "context"},
        )
        self.assertEqual(ServiceImage.objects.get(pk=rows["vyos"].pk).name, "VyOS 2026.06")
        self.client.post(
            reverse("service_image_add", args=[c.key]),
            {"purpose": "base-os", "kind": "image", "name": "Debian 13", "customization": "ssh"},
        )
        self.assertEqual(ServiceImage.objects.get(name="Debian 13").customization, "ssh")

    def test_rename_a_platform(self):
        from accounts.models import AuditLog
        from inventory.models import Platform, PlatformControl
        from tenants.models import PortalSettings, Tenant

        p = Platform.objects.get(key="dc1.mgmt")
        PlatformControl.objects.get_or_create(key="dc1.mgmt")
        t = Tenant.objects.create(name="t1", order={"platform": "dc1.mgmt"}) if hasattr(Tenant, "order") else None
        PortalSettings.objects.filter(pk=PortalSettings.get().pk).update(fe_backup_platforms=["dc1.mgmt", "dc2.x"])
        r = self.client.post(
            reverse("inv_platform_save"),
            {
                "site": "dc1",
                "name": "mgmt",
                "rename": "mgmt2",
                "kind": p.kind,
                "fe_mode": p.fe_mode or "local",
                **{f"fe_node{i}": f"one{i}" for i in (1, 2, 3)},  # owner 2026-11-15: always three front-end nodes
                **{f"fe_ip{i}": f"192.0.2.3{i}" for i in (1, 2, 3)},
                "fe_hostname": "one",
                "fe_vip": "192.0.2.30",
            },
        )
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Platform.objects.filter(key="dc1.mgmt2", name="mgmt2").exists())
        self.assertTrue(PlatformControl.objects.filter(key="dc1.mgmt2").exists())
        self.assertFalse(PlatformControl.objects.filter(key="dc1.mgmt").exists())
        if t is not None:
            t.refresh_from_db()
            self.assertEqual(t.order["platform"], "dc1.mgmt2")
        log = AuditLog.objects.get(action="platform_renamed")
        # core-042: the platforms chosen for backups follow the rename (in its audit entry)
        self.assertEqual(PortalSettings.get().fe_backup_platforms, ["dc1.mgmt2", "dc2.x"])
        self.assertEqual(log.details["backup_selection"], "1")

    def test_front_end_nodes_table_and_details(self):
        """Owner 2026-11-15: the Edit dialog's table (VIP, then the 3 nodes, name and IP; the IPs can be corrected on a
        deployed platform) and the details' nodes with their state, last refresh and errors; OneGate from VMs."""
        from django.utils import timezone

        from adoption.models import PlatformStats
        from inventory.models import Platform, PlatformControl

        Platform.objects.filter(key="dc1.mgmt").update(
            state="deployed", fe_vip="192.0.2.30", fe={"type": "vm", "hostname": "sdc1one", "nodes": ["a1", "a2", "a3"]}
        )
        p = Platform.objects.get(key="dc1.mgmt")
        self.assertTrue(p.fe_locked)
        data = {"site": "dc1", "name": "mgmt", "state": "deployed", "fe_ip1": "192.0.2.31", "fe_ip2": "192.0.2.32"}
        self.client.post(reverse("inv_platform_save"), {**data, "fe_ip3": "192.0.2.31"})  # the same IP twice: refused
        p.refresh_from_db()
        self.assertNotIn("node_ips", p.fe)
        self.client.post(reverse("inv_platform_save"), {**data, "fe_ip3": "192.0.2.33"})
        p.refresh_from_db()
        self.assertFalse(p.onegate_proxy)
        self.assertEqual(p.onegate_url, "http://192.0.2.30:5030")  # cleared: the VIP, internal (owner 2026-11-15)
        self.client.post(reverse("inv_platform_save"), {**data, "fe_ip3": "192.0.2.33", "onegate_proxy": "on"})
        p.refresh_from_db()
        self.assertTrue(p.onegate_proxy)  # can be set on a deployed platform
        self.assertEqual(
            (p.fe["nodes"], p.fe["node_ips"]), (["a1", "a2", "a3"], ["192.0.2.31", "192.0.2.32", "192.0.2.33"])
        )
        PlatformControl.objects.update_or_create(
            key="dc1.mgmt", defaults={"vip_answered": True, "vip_checked_at": timezone.now()}
        )
        PlatformStats.objects.update_or_create(
            platform_key="dc1.mgmt",
            defaults={
                "collected_at": timezone.now(),
                "frontends": [
                    {"id": "0", "name": "a1", "addr": "192.0.2.31", "state": "follower"},
                    {"id": "1", "name": "a2.example.com", "addr": "192.0.2.32", "state": "leader"},
                    {"id": "2", "name": "a3", "addr": "192.0.2.33", "state": "unreachable"},
                ],
            },
        )
        h = self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=platforms").content.decode()
        self.assertIn('name="fe_ip2"', h)  # the dialog: name and IP per node
        self.assertRegex(h, r'name="fe_ip2"[^>]*value="192.0.2.32"|value="192.0.2.32"[^>]*name="fe_ip2"')
        self.assertNotRegex(h, r'<input[^>]*name="fe_ip2"[^>]* disabled')
        self.assertIn("http://169.254.16.9:5030", h)  # OneGate from VMs
        self.assertRegex(h, r'fe-dot ok"[^>]*></span>VIP')
        self.assertRegex(h, r'fe-dot ok"[^>]*></span>leader')
        self.assertRegex(h, r'fe-dot bad"[^>]*></span>unreachable')
        self.assertIn("its XML-RPC does not answer", h)
        self.assertNotIn("oneadmin password", h)  # owner 2026-11-15: a Copy password button instead
        # mockup 39a (2026-11-16): the gateway cluster is chosen in a dialog from the details' top row
        self.assertIn("Gateway cluster…", h)
        self.assertIn('name="gateway_cluster"', h)
        details = h[h.index('class="olist-detail"') : h.index("<dialog")]
        self.assertNotIn('name="gateway_cluster"', details)


class Checks(TestCase):
    def test_evaluate_units_ports_ntp_bgp_and_addresses(self):
        import json

        from inventory import core_services as cs

        checks = {"units": ["named", "mysql"], "ports": [53, 2633], "ntp": True, "bgp": "ipv4"}
        bgp = {"ipv4Unicast": {"peers": {"10.0.10.1": {"state": "Established"}, "10.0.10.9": {"state": "Active"}}}}
        out = "##units\nactive\ninactive\n##ports\n0.0.0.0:53\n[::]:22\n##ntp\nyes\n##bgp\n" + json.dumps(bgp) + "\n"
        facts = {
            "addresses": ["eth0 10.0.101.11/24"],
            "vm": {"nics": [["cloud-core_dc1", "10.0.101.11"], ["x", "10.9.9.9"]]},
        }
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
        a = CoreServiceNode.objects.create(
            service=svc, name="cloud-one1_dc9", address="10.0.101.31", facts={"addresses": ["eth0 10.0.101.31/24"]}
        )
        CoreServiceNode.objects.create(
            service=svc, name="cloud-one2_dc9", address="10.0.101.32", facts={"addresses": ["eth0 10.0.101.32/24"]}
        )
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
        Network.objects.create(
            site="dc1",
            name="cloud-services",
            kind="core",
            interface="bond0",
            vlan=103,
            subnet="203.0.113.0",
            prefix=29,
            gateway="203.0.113.1",
        )
        Network.objects.filter(site="dc1", name="cloud-ix").update(
            local_asn=4200101002, rrs=["10.0.10.241", "10.0.10.242"]
        )
        self.egw = CoreService.objects.create(
            site=self.site, name="cloud-egw", kind="vm", template=ConfigTemplate.objects.get(role="egw"), ssh_port=55522
        )
        self.node = CoreServiceNode.objects.create(
            service=self.egw,
            name="cloud-egw_dc1",
            hostname="rdc1egw",
            address="10.0.10.9",
            facts={"vm": {"nics": [["cloud-ix_dc1", "10.0.10.9"], ["cloud-services_dc1", "203.0.113.1"]]}},
        )

    def test_egw_from_the_tenant_template_plus_differences(self):
        from inventory import nfconfig

        base = nfconfig.build_gateway("egw", self.node)
        self.assertIn("set protocols bgp parameters router-id 10.0.10.9", base)
        self.assertIn("set firewall group network-group CLIENT network 203.0.113.0/29", base)
        self.assertIn("set protocols bgp neighbor 10.0.10.242 peer-group cloud-int", base)
        self.assertIn("set interfaces ethernet eth0 vrf MGMT", base)
        running = "\n".join(
            [
                "set protocols bgp parameters router-id '10.0.10.9'",
                "set firewall ipv4 name IX-to-GW description 'IX->GW: BGP and SSH'",
                "set firewall group network-group SSHFROM network '10.0.10.8/31'",
                "set system host-name 'rdc1egw'",
                "set protocols bgp peer-group cloud-int password 'x'",
            ]
        )
        self.egw.config_diff = running
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

        igw = CoreService.objects.create(
            site=self.site, name="cloud-igw", kind="vm", template=ConfigTemplate.objects.get(role="igw"), ssh_port=55522
        )
        n = CoreServiceNode.objects.create(
            service=igw,
            name="cloud-igw_dc1",
            address="10.0.101.254",
            facts={"vm": {"nics": [["cloud-services_dc1", "203.0.113.2"], ["cloud-core_dc1", "10.0.101.254"]]}},
        )
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
        self.assertEqual(
            self.client.get(reverse("core_config", args=["dc1", igw.pk, n.pk])).status_code, 302
        )  # staff only


class FakeWorld:
    """Enough OpenNebula for a redeploy: VMs with NICs, power off / resume, rename, NIC detach / attach."""

    def __init__(self):
        from tenants.fakeone import World

        self.w = World(
            {"one.clusterpool.info": "<CLUSTER_POOL><CLUSTER><ID>0</ID><NAME>mgmt_dc1</NAME></CLUSTER></CLUSTER_POOL>"},
            vnets=[
                {
                    "id": 1,
                    "name": "cloud-ix_dc1",
                    "ars": [{"TYPE": "IP4", "IP": "10.0.10.240", "SIZE": 10}],
                    "mtu": 1500,
                },
                {"id": 8, "name": "cloud-vyos_dc1", "ars": [{"TYPE": "IP4", "IP": "10.0.20.100", "SIZE": 20}]},
            ],
            vms=[{"id": 51, "name": "cloud-ix-rr1_dc1", "nics": [{"network_id": 1, "ip": "10.0.10.241"}]}],
        )

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
        from inventory.models import CoreService, PlatformControl, ServiceImage

        self.site, self.secret = site_with_vms()
        PlatformControl.objects.filter(key="dc1.mgmt").update(mode="execution")
        ServiceImage.objects.create(
            control=PlatformControl.objects.get(key="dc1.mgmt"),
            role=ServiceImage.VYOS,
            purpose="vyos",
            kind="image",
            name="VyOS Router",
            one_id=171,
            customization="context",
        )
        cs.seed("dc1")
        self.svc = CoreService.objects.get(name="cloud-ix-rrs")
        self.assertEqual(self.svc.template.role, "rr-ix")
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
            node.facts = {
                "checks": [
                    {"check": "BGP (ipv4) peers established", "ok": bgp_ok, "detail": "25/25" if bgp_ok else "0/25"}
                ]
            }
            node.facts_error = ""
            node.save()
            return True

        with (
            mock.patch("adoption.collector.client_for", return_value=self.fake),
            mock.patch.object(cd, "_config", return_value="set system host-name x\\n"),
            mock.patch("inventory.nfconfig.is_complete", return_value=True),
            mock.patch("inventory.core_services.read_node", side_effect=read_node),
            mock.patch.object(cd, "MGMT_TIMEOUT", 0),
            mock.patch.object(cd, "BGP_TIMEOUT", 0),
        ):
            return tasks.start(
                "core VM redeploy", "test", lambda t: cd.redeploy(t, self.node, None, 25, force, keep_old)
            )

    def test_a_route_reflector_is_built_for_its_defined_address(self):
        """commit-047: its own address is the defined one on the service network, not its place in the list; a
        definition whose address is no route reflector of the network is not redeployed."""
        from inventory import core_deploy as cd
        from inventory.models import Network

        Network.objects.filter(site="dc1", name="cloud-ix").update(rrs=["10.0.10.240", "10.0.10.241", "10.0.10.242"])
        self.node.refresh_from_db()
        self.assertEqual(self.node.ips[-1], "10.0.10.241")
        text = cd._config(self.node, "10.0.20.100", False)
        self.assertIn("set protocols bgp parameters router-id 10.0.10.241", text)
        self.assertNotIn("neighbor 10.0.10.241 ", text)
        Network.objects.filter(site="dc1", name="cloud-ix").update(rrs=["10.0.10.242", "10.0.10.243"])
        node = type(self.node).objects.get(pk=self.node.pk)
        with mock.patch("adoption.collector.client_for", return_value=self.fake):
            p = cd.plan(node)
        chk = next(
            c for c in p["checks"] if c["check"] == "the defined address is one of the network's route reflectors"
        )
        self.assertFalse(chk["ok"])
        self.assertFalse(p["definition_ok"])
        # commit-047: the retired address is not rendered as another reflector's, so even a forced redeploy refuses
        text = cd._config(node, "10.0.20.100", False)
        self.assertNotIn("router-id 10.0.10.242", text)
        self.assertNotIn("router-id 10.0.10.243", text)
        self.assertFalse(p["complete"])
        from accounts import tasks

        vms = set(self.fake.w.vms)
        with mock.patch("adoption.collector.client_for", return_value=self.fake):
            t = tasks.start("core VM redeploy", "test", lambda t: cd.redeploy(t, node, None, 25, True, True))
        self.assertEqual(t.state, "failed")
        self.assertIn("values missing", t.error)
        self.assertEqual(set(self.fake.w.vms), vms)

    def test_redeploy_keeps_the_old_vm_and_moves_the_address(self):
        t = self.run_redeploy(bgp_ok=True)
        self.assertEqual(t.state, "done", t.error)
        w = self.fake.w
        old, new = w.vms[51], w.vms[t.result["new_id"]]
        # correction 09: the new VM takes the definition's name (no underscore) and its host name is exactly that name;
        # the old one is kept as <new name>-old
        self.assertEqual(
            (old["name"], old["state"], old["nics"]), ("cloud-ix-rr1-dc1-old", "8", [])
        )  # kept, powered off
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
        self.assertEqual(
            (vms["cloud-ix-rr1-dc1-old"]["status"], vms["cloud-ix-rr1-dc1"]["init_script"]), ("poweroff", False)
        )
        self.node.refresh_from_db()
        self.assertEqual((self.node.address, self.node.ssh_port), ("10.0.20.100", 55522))
        self.assertEqual((self.node.name, self.node.hostname), ("cloud-ix-rr1-dc1", "cloud-ix-rr1-dc1"))
        self.assertEqual(self.node.deployed_vm_id, t.result["new_id"])  # made by the portal: compliant
        from accounts import tasks
        from inventory import core_deploy as cd

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

        def page():
            return self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=core").content.decode()

        from inventory.models import Network, PeerGroup

        net = Network.objects.get(site="dc1", name="cloud-ix")
        PeerGroup.objects.update_or_create(
            network=net, name="rrs", defaults={"session": "ibgp", "password": "secret:999#rrs"}
        )
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
        self.assertEqual(
            (self.fake.w.vms[51]["name"], self.fake.w.vms[51]["state"]), ("cloud-ix-rr1_dc1", "3")
        )  # untouched

    def test_430_the_expected_template_follows_the_network_kind(self):
        """commit-430: any exchange (whatever its name) - rr-ix; EVPN - rr-vxlan; the redeploy check as the definition."""
        from inventory import core_deploy as cd
        from inventory import core_types as ct
        from inventory.models import Network

        for name, kind, want in (
            ("partner-ix", "ix", "rr-ix"),
            ("corpnet-ix", "ix", "rr-ix"),
            ("dc-fabric", "evpn", "rr-vxlan"),
        ):
            net = Network(site="dc1", name=name, kind=kind)
            self.assertEqual((cd.expected_role(net), ct.rr_role(net)), (want, want), name)
        self.assertEqual(cd.expected_role(Network(site="dc1", name="cloud-core", kind="core-cloud")), "")
        self.assertEqual(cd.expected_role(None), "")

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

    def test_setting_the_old_vm_aside_stopped_half_way_is_undone(self):
        """core-017 again (seventh full review): a failure while the old router is set aside (its NIC detach, the wait
        after it, the rename) is undone too - back under its name, with its NIC, running; nothing new is made."""
        from adoption.collector import OneError
        from inventory import core_deploy as cd

        real_wait = cd._wait
        for fails in ("one.vm.detachnic", "wait", "one.vm.rename"):
            with self.subTest(fails=fails):
                self.fake = FakeWorld()
                real, state = self.fake._call, {"failed": False, "detached": False}

                def call(method, *args, fails=fails, real=real, state=state):
                    if method == "one.vm.detachnic":
                        state["detached"] = True
                    if method == fails and not state["failed"]:
                        state["failed"] = True
                        if method == "one.vm.rename":
                            real(method, *args)  # renamed, then the answer was an error
                        raise OneError(f"{method}: no answer")
                    return real(method, *args)

                def wait(one, vm_id, want, timeout=300, fails=fails, state=state):
                    if fails == "wait" and state["detached"] and not state["failed"]:
                        state["failed"] = True
                        raise cd.RedeployError(f"VM #{vm_id} is not {want}")
                    return real_wait(one, vm_id, want, timeout)

                self.fake._call = call
                with mock.patch.object(cd, "_wait", wait):
                    t = self.run_redeploy(bgp_ok=True)
                self.assertEqual(t.state, "failed")
                old = self.fake.w.vms[51]
                self.assertEqual((old["name"], old["state"]), ("cloud-ix-rr1_dc1", "3"))
                self.assertEqual([n["ip"] for n in old["nics"] if n["vid"] == 1], ["10.0.10.241"])
                self.assertEqual(list(self.fake.w.vms), [51])  # nothing new was made

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
        self.assertNotIn("eth1 mtu", nfconfig.build("rr-ix", site="dc1", index=0))

    def test_zone_mgmt_names_the_vrf(self):
        """VyOS 2026.03 matches a zone's interface in a VRF only with the VRF a member too (T8761): without it the
        management address answered nothing (portal-ix RRs, 2026-10-27)."""
        from inventory import nfconfig
        from inventory.models import ConfigTemplate

        for role in ("rr-ix", "egw", "cgw"):
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

        m92 = importlib.import_module("inventory.migrations.0092_templates_rrs_explicit")

        def at(body, role="rr-inet"):  # the bodies as 0061 made them (0063 and 0092 came later)
            for o, n in m92.CHANGES.get(role, []):
                body = body.replace(n, o, 1)
            return body.replace(T.MGMT_FW, m.NEW_FW)

        old = {"rr-vxlan": T.RR_VXLAN, "l2-a": T.L2, "l2-b": T.L2, "l2-br": T.L2BR}
        for role, new in old.items():  # the bodies before: the input filter in place of the zones
            extra = {"rr-vxlan": at(T.RR_VXLAN_ZONES, "rr-vxlan"), "l2-a": T.L2_ZONES, "l2-b": T.L2_ZONES}.get(role, "")
            new = at(new, role)
            before = new.replace(extra + m.NEW_FW, m.OLD_FILTER)
            self.assertNotEqual(before, new)
            self.assertEqual(m.unify(role, before), new, role)
        before = (
            at(T.RR_IX)
            .replace(m.NEW_FW, m.OLD_ZONE)
            .replace("set firewall zone IX from GW", m.LOCAL + "set firewall zone IX from GW")
        )
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
        again = subprocess.run(
            ["openssl", "passwd", "-6", "-salt", salt, "-stdin"], input="Sa-Pass-123", capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(h, again)  # the super admin's password

    def test_site_time_zone_is_an_iana_one_and_every_vyos_template_sets_it(self):
        """Correction 09 (2026-10-27 19:25): the site's time zone is checked on input (IANA) and every VyOS template sets it."""
        from inventory import nf_templates as T
        from inventory import nfconfig
        from inventory.edit_views import SiteForm
        from inventory.models import Site, site_tz

        self.assertEqual(
            (site_tz("almaty"), site_tz("Asia/Almaty"), site_tz(""), site_tz("Europe/Berlin")),
            ("Asia/Almaty", "Asia/Almaty", "Asia/Almaty", "Europe/Berlin"),
        )
        f = SiteForm(initial={"timezone": "almaty"})
        self.assertEqual(f.initial["timezone"], "Asia/Almaty")
        self.assertIn(("Asia/Almaty", "Asia/Almaty"), f.fields["timezone"].choices)
        self.assertNotIn("almaty", [v for v, _l in f.fields["timezone"].choices])
        self.assertFalse(f.fields["timezone"].valid_value("almaty"))
        self.assertFalse(
            SiteForm({"code": "dc7", "country": "KZ", "city": "A", "dc": "x", "timezone": "almaty"}).is_valid()
        )
        self.assertTrue(f.fields["timezone"].valid_value("Asia/Almaty"))
        for role, _t, os_, _v, body, _d in T.BUILTIN:
            if os_ == "vyos":
                self.assertEqual(body.count("set system time-zone {{ time_zone }}"), 1, role)
        Site.objects.filter(code="dc1").update(timezone="Asia/Aqtobe")
        from inventory.models import Network

        Network.objects.filter(site="dc1", name="cloud-ix").update(rrs=["10.0.10.241"])
        self.assertIn("set system time-zone Asia/Aqtobe", nfconfig.build("rr-ix", site="dc1", index=0))

    def test_time_and_logs_go_out_through_management(self):
        """2026-10-27: NTP to the site's servers and syslog to its collector leave through zone MGMT, replies come back."""
        from inventory import nf_templates as T
        from inventory import nfconfig

        out = nfconfig._render(
            T.MGMT_FW,
            {
                "ssh_port": 55522,
                "ntp": ["10.0.100.201", "10.0.100.202"],
                "syslog": {"host": "10.0.101.18", "port": 15142, "proto": "tcp"},
            },
        )
        for line in (
            "set firewall group address-group NTP address 10.0.100.202",
            "set firewall ipv4 name GW-to-MGMT rule 20 destination port 123",
            "set firewall ipv4 name GW-to-MGMT rule 20 protocol udp",
            "set firewall ipv4 name GW-to-MGMT rule 30 destination address 10.0.101.18",
            "set firewall ipv4 name GW-to-MGMT rule 30 destination port 15142",
            "set firewall ipv4 name GW-to-MGMT rule 30 protocol tcp",
            "set firewall ipv4 name MGMT-to-GW rule 2 state established",
            "set firewall ipv4 name MGMT-to-GW rule 3 state related",
        ):
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
        for t in ConfigTemplate.objects.filter(
            role__in=["egw", "cgw", "igw", "rr-ix", "rr-vxlan", "l2-a", "l2-b", "l2-br"]
        ):
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
        for line in (
            'set firewall group network-group SSHFROM description "SSH: allowed IP"',
            "set firewall ipv4 name EXT-to-GW rule 10 destination group port-group SSHPORT",
            'set firewall group port-group SSHPORT description "GW SSH port"',
            "set service ssh listen-address 1.2.3.4",
        ):
            self.assertTrue(nfconfig.SITE_IGW_NO_SSH.search(line), line)
        self.assertFalse(
            nfconfig.SITE_IGW_NO_SSH.search('set firewall group port-group SSHPORT description "SSH port (management)"')
        )
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
                return (
                    xml.replace("</VM>", f"<USER_TEMPLATE><AUTOSTART>{at}</AUTOSTART></USER_TEMPLATE></VM>")
                    if at
                    else xml
                )
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
        self.fake.w.vms[51]["sched"] = {
            "0": {
                "ACTION": "resume",
                "TIME": "1761292800",
                "REPEAT": "3",
                "DAYS": "1",
                "END_TYPE": "0",
                "END_VALUE": "-1",
            }
        }

        def spy(method, *args):
            v = (
                self.fake.w.vms.get(int(args[0]))
                if method in ("one.vm.scheddelete", "one.vm.schedadd", "one.vm.info")
                else None
            )
            if method == "one.vm.scheddelete":
                calls.append(("remove", int(args[0]), str(args[1]), v["state"]))
                v["sched"].pop(str(args[1]))
                return int(args[0])
            if method == "one.vm.schedadd":
                calls.append(("add", int(args[0]), args[1], v["state"]))
                return int(args[0])
            if method == "one.vm.info" and v is not None and v.get("sched"):
                sa = "".join(
                    "<SCHED_ACTION><ID>{}</ID>{}<DONE>-1</DONE></SCHED_ACTION>".format(
                        i, "".join(f"<{k}>{x}</{k}>" for k, x in a.items())
                    )
                    for i, a in v["sched"].items()
                )
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
        Site.objects.filter(code="dc1").update(
            dns_servers=["10.0.0.53"],
            ntp_servers=["10.0.0.123"],
            log_collectors={"vyos": {"host": "log1.example.kz", "proto": "tcp", "port": 15142}},
        )
        with (
            mock.patch("socket.gethostbyname", return_value="10.0.9.18"),
            mock.patch("inventory.timesrv.servers", return_value=["10.0.0.123"]),  # T36 B5d: the site's time servers
        ):
            text = nfconfig.build("rr-ix", site="dc1", index=0)
        self.assertNotIn("name-server", text)
        self.assertIn("set service ntp vrf MGMT", text)  # NTP stays, in VRF MGMT
        self.assertIn("set service ntp server 10.0.0.123", text)
        self.assertIn("set system syslog remote 10.0.9.18 port 15142", text)
        self.assertIn("set system syslog remote 10.0.9.18 vrf MGMT", text)
        # owner 2026-11-24 (T33, the vyos logged service): octet-counted over TCP; /var/log/messages by size, Size / 10
        self.assertIn("set system syslog remote 10.0.9.18 format octet-counted", text)
        self.assertIn("set system logs logrotate messages max-size 10", text)
        self.assertIn("set system logs logrotate messages rotate 9", text)
        from inventory.models import LoggedService

        LoggedService.objects.filter(name="vyos").update(size_mb=500)
        with mock.patch("socket.gethostbyname", return_value="10.0.9.18"):
            self.assertIn("logrotate messages max-size 50", nfconfig.build("rr-ix", site="dc1", index=0))
        LoggedService.objects.filter(name="vyos").update(size_mb=100)
        self.assertNotIn("set system syslog vrf", text)
        self.assertNotIn("log1.example.kz", text)
        with mock.patch("socket.gethostbyname", side_effect=OSError("no such name")):
            self.assertFalse(nfconfig.is_complete(nfconfig.build("rr-ix", site="dc1", index=0)))  # never by name

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
        self.assertEqual(
            (w.vms[51]["name"], w.vms[51]["state"], [n["ip"] for n in w.vms[51]["nics"]]),
            ("cloud-ix-rr1_dc1", "3", ["10.0.10.241"]),
        )
        self.assertTrue(all(v["state"] == "6" for i, v in w.vms.items() if i != 51))  # the new VM is gone
        self.node.refresh_from_db()
        self.assertEqual(self.node.address, "10.0.10.241")


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode())
class SettingsAndTypes(TestCase):
    """Correction 06, first stage: password and S3 secrets, backup datastores, image types, network types."""

    setUp = CoreServices.setUp

    def test_password_and_s3_secrets(self):
        from inventory.models import Secret

        self.client.post(
            reverse("infra_secret_add", args=["password"]),
            {"new-name": "dc1.restic", "new-password": "Restic-Pass-2026"},
        )
        pw = Secret.objects.get(kind="password", name="dc1.restic")
        self.assertEqual(pw.get("password"), "Restic-Pass-2026")
        self.client.post(reverse("infra_secret_add", args=["s3"]), {"new-name": "dc1.s3", "new-s3_key": "AKIA123"})
        self.assertFalse(Secret.objects.filter(kind="s3").exists())  # the secret key is required
        self.client.post(
            reverse("infra_secret_add", args=["s3"]),
            {"new-name": "dc1.s3", "new-s3_key": "AKIA123", "new-s3_secret": "s3-secret-value"},
        )
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
        priv = k.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()
        ).decode()
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
        self.client.post(
            reverse("infra_secret_add", args=["key"]),
            {"new-key": "-----BEGIN OPENSSH PRIVATE KEY-----\nxx\n-----END OPENSSH PRIVATE KEY-----"},
        )
        self.assertEqual(Secret.objects.filter(kind="key").count(), n)  # not a readable private key: nothing kept

    def test_an_s3_backup_datastore_needs_no_volume(self):
        from inventory.models import PlatformDatastore, Secret

        pw = Secret.objects.create(kind=Secret.Kind.PASSWORD, name="dc1.restic")
        s3 = Secret.objects.create(kind=Secret.Kind.S3, name="dc1.s3")
        url = reverse("inv_ds_new", args=["dc1", "mgmt"])
        base = {
            "name": "mgmt-bkp",
            "kind": "backups",
            "backend": "s3",
            "restic_ip": "10.0.1.60",
            "restic_secret": pw.ref,
        }
        self.client.post(url, base)
        self.assertFalse(PlatformDatastore.objects.exists())  # S3: endpoint, bucket and secret
        self.client.post(
            url, {**base, "s3_endpoint": "s3.example.kz", "s3_bucket": "dc1-mgmt-backup", "s3_secret": s3.ref}
        )
        ds = PlatformDatastore.objects.get(name="mgmt-bkp")
        self.assertEqual(
            (ds.volume, ds.backend, ds.s3_endpoint, ds.s3_bucket, ds.s3_secret),
            (None, "s3", "s3.example.kz", "dc1-mgmt-backup", s3.ref),
        )

    def test_network_types_one_of_each_core_type_per_site(self):
        from inventory.models import Network

        Network.objects.filter(site="dc1", name="cloud-vyos").update(kind="core-vyos")  # as the migration sets it
        url = reverse("inv_net_new", args=["dc1"])
        base = {
            "interface": "bond0",
            "vlan": 230,
            "mtu": 1500,
            "subnet": "10.0.230.0",
            "prefix": 24,
            "state": "deployed",
        }
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
        from inventory.models import (
            Network,
            Platform,
            PlatformControl,
            PlatformDatastore,
            Secret,
            ServiceImage,
            Site,
            Storage,
            StorageVolume,
        )

        self.setUp_base()
        Site.objects.filter(code="dc1").update(primary=True)  # T36 B3: the only site is the primary one
        Network.objects.filter(site="dc1", name="cloud-vyos").update(kind="core-vyos")
        Network.objects.filter(site="dc1", name="cloud-core").update(kind="core-cloud")
        Network.objects.filter(site="dc1", name="data-nfs").update(kind="nfs")
        Network.objects.create(
            site="dc1",
            name="cloud-services",
            kind="core-services",
            interface="bond0",
            vlan=103,
            subnet="203.0.113.0",
            prefix=28,
        )
        Network.objects.filter(site="dc1", name="cloud-ix").update(
            rrs=["10.0.10.241", "10.0.10.242", "10.0.10.243"], announced=["203.0.113.0/24"]
        )
        c = PlatformControl.objects.get(key="dc1.mgmt")
        ServiceImage.objects.create(
            control=c, role="vyos", purpose="vyos", kind="image", name="VyOS", one_id=171, customization="context"
        )
        ServiceImage.objects.create(
            control=c, purpose="base-os", kind="image", name="Ubuntu 26.04", customization="ssh"
        )
        Site.objects.filter(code="dc1").update(
            dns_servers=["203.0.113.3", "203.0.113.4"],
            management={"hosts": ["203.0.113.8/29"], "ports": {"vyos": 55522, "ssh": 55523}},
            log_collectors={"vyos": {"host": "203.0.113.5", "proto": "tcp", "port": 15142}},
        )
        st = Storage.objects.create(site=Site.objects.get(code="dc1"), name="nas1", kind="nfs", targets=["10.0.111.2"])
        self.vol = StorageVolume.objects.create(storage=st, name="logs-one", device="/export/logs", size_gb=500)
        self.pw = Secret.objects.create(kind="password", name="dc1.restic")
        self.ds = PlatformDatastore.objects.create(
            platform=Platform.objects.get(key="dc1.mgmt"),
            name="mgmt-bkp",
            kind="backups",
            volume=self.vol,
            backend="iscsi",
            restic_ip="203.0.113.6",
            restic_secret=self.pw.ref,
        )

    def add(self, stype, **data):
        return self.client.post(
            reverse("core_new", args=["dc1"]), {"stype": stype, "state": "deployed", **data}, follow=True
        )

    def test_s3c_add_service_page(self):
        """T34 S3c (ui-19): the profile first, then what its type needs; a route reflector's networks are those its
        profile's template serves; nothing is deployed."""
        from inventory.models import CoreService, Network, ServiceProfile

        ix = Network.objects.get(site="dc1", name="cloud-ix")
        evpn = Network.objects.create(
            site="dc1",
            name="vxlan",
            kind="evpn",
            interface="bond0",
            vlan=901,
            subnet="10.0.21.0",
            prefix=24,
            rrs=["10.0.21.241", "10.0.21.242", "10.0.21.243"],
        )
        url = reverse("core_add", args=["dc1"])
        page = self.client.get(url).content.decode()
        self.assertIn('name="profile"', page)
        self.assertIn(f'value="{ServiceProfile.objects.get(key="nf-ix").pk}"', page)  # U4: by its id
        self.assertNotIn('name="network"', page)  # the type's form comes after the profile
        vx = ServiceProfile.objects.get(key="nf-evpn").pk
        page = self.client.get(url + f"?profile={vx}").content.decode()
        net = page[page.index('name="network"') :]
        net = net[: net.index("</select>")]  # the network's choices (the profiles' ids are values too: U4)
        self.assertIn(f'value="{evpn.pk}"', net)
        self.assertNotIn(f'value="{ix.pk}"', net)  # an IX network is rr-ix's
        self.assertIn("3 VMs", page)
        r = self.client.post(url, {"profile": vx, "stype": "bgp-rr", "network": evpn.pk, "vcpu": 2, "ram_gb": 4})
        rr = CoreService.objects.get(stype="bgp-rr")
        self.assertRedirects(
            r, reverse("infra_site", args=["dc1"]) + f"?tab=core&sel=cs-{rr.pk}", fetch_redirect_response=False
        )
        self.assertEqual((rr.profile.key, rr.template.role, rr.nodes.count()), ("nf-evpn", "rr-vxlan", 3))
        self.assertFalse(any(n.deployed_vm_id for n in rr.nodes.all()))  # defined, not deployed
        # T36 B3a: anything added starts planned, each VM too
        self.assertEqual((rr.state, {n.state for n in rr.nodes.all()}), ("planned", {"planned"}))
        ixp = ServiceProfile.objects.get(key="nf-ix").pk
        r = self.client.post(url, {"profile": ixp, "stype": "bgp-rr", "network": evpn.pk, "vcpu": 2, "ram_gb": 4})
        self.assertEqual(CoreService.objects.filter(stype="bgp-rr").count(), 1)  # not a network rr-ix serves
        page = self.client.get(url).content.decode()
        self.assertIn("nf-evpn - Route reflector, EVPN (no network of the site without route reflectors", page)

    def test_419_the_chosen_profile_counts_the_vms(self):
        from inventory.models import CoreService, Network, ServiceProfile

        evpn = Network.objects.create(
            site="dc1",
            name="vxlan",
            kind="evpn",
            interface="bond0",
            vlan=902,
            subnet="10.0.22.0",
            prefix=24,
            rrs=["10.0.22.241", "10.0.22.242", "10.0.22.243"],
        )
        ServiceProfile.objects.create(
            key="rr-vxlan-two", name="RR EVPN, two", stype="bgp-rr", services=[], vms=2, install_template="rr-vxlan"
        )  # the same template as rr-vxlan (3 VMs, the default by key order)
        r = self.client.post(
            reverse("core_add", args=["dc1"]),
            {
                "profile": ServiceProfile.objects.get(key="rr-vxlan-two").pk,
                "stype": "bgp-rr",
                "network": evpn.pk,
                "vcpu": 2,
                "ram_gb": 4,
            },
        )
        self.assertEqual(r.status_code, 302)
        rr = CoreService.objects.get(stype="bgp-rr")
        self.assertEqual((rr.profile.key, rr.nodes.count()), ("rr-vxlan-two", 2))

    def test_421_a_profile_with_another_template_is_not_offered(self):
        from inventory.models import ServiceProfile

        ServiceProfile.objects.filter(key="cloud-gw").update(install_template="egw")  # saved before e720681
        page = self.client.get(reverse("core_add", args=["dc1"])).content.decode()
        self.assertIn("it names egw; a cloud-gw service is made with igw: edit the profile", page)
        opt = page[page.index(f'<option value="{ServiceProfile.objects.get(key="cloud-gw").pk}"') :]
        self.assertIn("disabled", opt[: opt.index(">")])
        gw = ServiceProfile.objects.get(key="cloud-gw").pk
        r = self.client.get(reverse("core_add", args=["dc1"]) + f"?profile={gw}", follow=True)
        self.assertContains(r, "Profile cloud-gw cannot be added here.")

    def test_u9_u11_list_columns_profile_and_platform(self):
        """Owner 2026-11-23 16:01 (U9-U11, mockup p05): the list's columns; the profile one value; the platform hosting
        the VMs in the Edit dialog, read only once a VM is on a platform (Q7 "cannot")."""
        from inventory import core_types as ct
        from inventory.models import Cluster, CoreService, Platform, PlatformControl, ServiceImage, ServiceProfile, Site

        Site.objects.filter(code="dc1").update(  # as commit-318's test: ns on core-cloud
            dns_servers=["10.0.101.3", "10.0.101.4"],
            management={"hosts": ["10.0.101.8/29"], "ports": {"vyos": 55522, "ssh": 55523}},
            log_collectors={"core": {"host": "10.0.101.5", "proto": "tcp", "port": 15142}},
        )
        vx = ServiceProfile.objects.get(key="ns-primary")
        r = self.add("ns", vcpu=1, ram_gb=2, disk_gb=10)
        ns = CoreService.objects.get(stype="ns")
        page = self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=core").content.decode()
        head = page[page.index("<thead>") : page.index("</thead>")]
        self.assertEqual(
            re.findall(r"<th[^>]*>([^<]+)</th>", head),
            ["Name", "Profile", "Platform", "VMs", "vCPU", "RAM, GB", "Disk, GB", "State"],
        )
        row = page[page.index(f'data-id="cs-{ns.pk}"') :]
        row = row[: row.index("</tr>")]
        self.assertIn(f"<td>{vx.key}</td>", row)  # U10: its Name only, no type tag
        dlg = page[page.index(f'id="dlg-cs-{ns.pk}"') :]
        self.assertIn('name="host_platform"', dlg[: dlg.index("</dialog>")])
        shared = Platform.objects.create(
            key="dc1.cloud", site=Site.objects.get(code="dc1"), name="cloud", kind="shared"
        )
        with self.assertRaisesMessage(ct.DefinitionError, "shared platform"):  # T36 rules v2 B5
            ct.definition("dc1", "ns", {"vcpu": 1, "ram_gb": 1, ct.HOST_PLATFORM: str(shared.pk)}, existing=ns)
        other = Platform.objects.create(key="dc1.mgmt2", site=Site.objects.get(code="dc1"), name="mgmt2", kind="mgmt")
        Cluster.objects.create(platform=other, name="mgmt2_dc1", kind="mgmt")
        c = PlatformControl.objects.create(key="dc1.mgmt2")
        ServiceImage.objects.create(
            control=c, purpose="base-os", kind="image", name="Ubuntu 26.04", customization="ssh"
        )
        d = ct.definition("dc1", "ns", {"vcpu": 1, "ram_gb": 1, ct.HOST_PLATFORM: str(other.pk)}, existing=ns)
        self.assertEqual(d["platform"], other)
        with self.assertRaises(ct.DefinitionError):
            ct.definition("dc1", "ns", {"vcpu": 1, "ram_gb": 1, ct.HOST_PLATFORM: "999999"}, existing=ns)
        from django.utils import timezone

        f = ct.FORMS["ns"](site="dc1", svc=ns, initial=ct.initial(ns))
        self.assertTrue(f.fields[ct.HOST_PLATFORM].disabled)  # commit-428: the platforms not read yet - kept
        PlatformControl.objects.filter(key__startswith="dc1.").update(vms_at=timezone.now(), vms=[])
        f = ct.FORMS["ns"](site="dc1", svc=ns, initial=ct.initial(ns))
        self.assertFalse(f.fields[ct.HOST_PLATFORM].disabled)  # read, none of its VMs there: it may move
        n = ns.nodes.first()
        PlatformControl.objects.filter(key="dc1.mgmt").update(vms=[{"name": n.name, "nics": []}])  # found, not made
        f = ct.FORMS["ns"](site="dc1", svc=ns, initial=ct.initial(ns))
        self.assertTrue(f.fields[ct.HOST_PLATFORM].disabled)
        data = {**ct.initial(ns), ct.HOST_PLATFORM: str(other.pk)}
        f = ct.FORMS["ns"](data, site="dc1", svc=ns, initial=ct.initial(ns))
        f.is_valid()
        self.assertEqual(f.cleaned_data.get(ct.HOST_PLATFORM), str(ns.platform_id))  # a post cannot move it
        PlatformControl.objects.filter(key="dc1.mgmt").update(vms=[{"name": f"{n.name}-old", "nics": []}])
        self.assertTrue(ct.platform_fixed(ns))  # a kept -old VM too
        # commit-428: a move is checked against the platforms read again - a failed read or a VM found keeps it
        PlatformControl.objects.filter(key="dc1.mgmt").update(vms=[])
        move = {**ct.initial(ns), ct.HOST_PLATFORM: str(other.pk)}
        with mock.patch("inventory.core_services.refresh_vms", return_value=[("dc1.mgmt", "unreachable")]):
            f = ct.FORMS["ns"](move, site="dc1", svc=ns, initial=ct.initial(ns))
            self.assertFalse(f.is_valid())
            self.assertIn("could not be read", str(f.errors))

        def appears(site):
            PlatformControl.objects.filter(key="dc1.mgmt").update(vms=[{"name": n.name, "nics": []}])
            return []

        with mock.patch("inventory.core_services.refresh_vms", side_effect=appears):
            f = ct.FORMS["ns"](move, site="dc1", svc=ns, initial=ct.initial(ns))
            self.assertFalse(f.is_valid())
            self.assertIn("cannot change", str(f.errors))
        PlatformControl.objects.filter(key="dc1.mgmt").update(vms=[])
        with mock.patch("inventory.core_services.refresh_vms", return_value=[]):
            f = ct.FORMS["ns"](move, site="dc1", svc=ns, initial=ct.initial(ns))
            f.is_valid()
            self.assertEqual(f.cleaned_data.get(ct.HOST_PLATFORM), str(other.pk))  # read now, nothing there: it moves
        PlatformControl.objects.filter(key="dc1.mgmt").update(vms=[])
        n.deployed_vm_id = 4107
        n.save()
        f = ct.FORMS["ns"](site="dc1", svc=ns)
        self.assertTrue(f.fields[ct.HOST_PLATFORM].disabled)
        del r

    def test_443_a_found_service_on_a_retired_platform_can_be_edited(self):
        """commit-443: a service found on its platform (locked), that platform since retired - an unrelated edit saves
        and the platform stays."""
        from django.utils import timezone

        from inventory import core_types as ct
        from inventory.models import CoreService, PlatformControl, Site

        Site.objects.filter(code="dc1").update(
            dns_servers=["10.0.101.3", "10.0.101.4"],
            management={"hosts": ["10.0.101.8/29"], "ports": {"vyos": 55522, "ssh": 55523}},
            log_collectors={"core": {"host": "10.0.101.5", "proto": "tcp", "port": 15142}},
        )
        self.add("ns", vcpu=1, ram_gb=2, disk_gb=10)
        ns = CoreService.objects.get(stype="ns")
        n = ns.nodes.first()
        PlatformControl.objects.filter(key__startswith="dc1.").update(vms_at=timezone.now(), vms=[])
        PlatformControl.objects.filter(key="dc1.mgmt").update(vms=[{"name": n.name, "nics": []}])  # found there
        plat = ns.platform
        plat.state = "retired"
        plat.save()
        ns.refresh_from_db()
        data = {**ct.initial(ns), ct.HOST_PLATFORM: str(plat.pk), "vcpu": 2}
        f = ct.FORMS["ns"](data, site="dc1", svc=ns, initial=ct.initial(ns))
        self.assertTrue(f.fields[ct.HOST_PLATFORM].disabled)
        self.assertEqual([v for v, _l in f.fields[ct.HOST_PLATFORM].choices], [str(plat.pk)])
        f.is_valid()
        self.assertNotIn(ct.HOST_PLATFORM, f.errors)
        self.assertEqual(f.cleaned_data.get(ct.HOST_PLATFORM), str(plat.pk))

    def test_435_a_retired_portal_platform_is_never_the_default(self):
        """commit-435: the default of a portal-gw is an active portal platform - the label, the move check, the save."""
        from inventory import core_types as ct
        from inventory.models import Platform, Site

        st = Site.objects.get(code="dc1")
        Platform.objects.create(key="dc1.a-old", site=st, name="a-old", kind="ext-managed", state="retired")
        live = Platform.objects.create(key="dc1.portal", site=st, name="portal", kind="ext-managed")
        self.assertEqual(ct.portal_platform("dc1"), live)
        f = ct.FORMS["portal-gw"](site="dc1")
        self.assertEqual(f.fields[ct.HOST_PLATFORM].choices[0][1], "the site's: dc1.portal")
        self.assertEqual(f._default_platform(), live)
        self.assertNotIn("dc1.a-old", [label for _v, label in f.fields[ct.HOST_PLATFORM].choices])
        # commit-439: the portal platform's image looked up (and a missing one noted) once
        with mock.patch.object(ct, "image_of", wraps=ct.image_of) as looked:
            try:
                ct.definition("dc1", "portal-gw", {"vcpu": 2, "ram_gb": 2})
            except ct.DefinitionError:
                pass
        self.assertEqual([c.args for c in looked.call_args_list].count((live, "vyos")), 1)
        # commit-437: the management platforms retired - the blank choice still resolves to the portal platform
        Platform.objects.filter(site=st, kind="mgmt").update(state="retired")
        try:
            d = ct.definition("dc1", "portal-gw", {"vcpu": 2, "ram_gb": 2})
            self.assertEqual(d["platform"], live)
        except ct.DefinitionError as exc:
            self.assertNotIn("has no management platform", str(exc))

    def test_s3c_the_tab_links_the_page(self):
        page = self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=core").content.decode()
        self.assertIn(reverse("core_add", args=["dc1"]), page)
        self.assertNotIn('id="dlg-cs-new"', page)

    def test_bgp_rr_from_its_network(self):
        from inventory.models import CoreService, Network

        ix = Network.objects.get(site="dc1", name="cloud-ix")
        self.add("bgp-rr", network=ix.pk, vcpu=2, ram_gb=4)
        s = CoreService.objects.get(stype="bgp-rr")
        self.assertEqual(
            (s.name, s.template.role, s.platform.key, s.image.name, s.ssh_port),
            ("cloud-ix-rrs", "rr-ix", "dc1.mgmt", "VyOS", 55522),
        )
        self.assertEqual([n.name for n, _g in s.nic_list()], ["cloud-vyos", "cloud-ix"])
        self.assertEqual(
            [(n.name, n.hostname, n.ips) for n in s.nodes.order_by("pk")],
            [(f"cloud-ix-rr{i}-dc1", f"cloud-ix-rr{i}-dc1", ["", f"10.0.10.24{i}"]) for i in (1, 2, 3)],
        )
        self.add("bgp-rr", network=ix.pk, vcpu=2, ram_gb=4)  # one per network: not offered again
        self.assertEqual(CoreService.objects.filter(stype="bgp-rr").count(), 1)

    def test_s3b_the_profile_counts_the_vms(self):
        """T34 S3b (owner 2026-11-23 Q24): the number of VMs is the service profile's."""
        from inventory import core_types as ct
        from inventory.models import CoreService, Network, ServiceProfile, Site

        ix = Network.objects.get(site="dc1", name="cloud-ix")
        ServiceProfile.objects.filter(key="nf-ix").update(vms=2)
        self.add("bgp-rr", network=ix.pk, vcpu=2, ram_gb=4)
        rr = CoreService.objects.get(stype="bgp-rr")
        self.assertEqual(rr.profile.key, "nf-ix")
        self.assertEqual([n.ips[1] for n in rr.nodes.order_by("pk")], ["10.0.10.241", "10.0.10.242"])  # first two
        ServiceProfile.objects.filter(key="nf-ix").update(vms=4)  # the rrs range has three
        with self.assertRaisesMessage(ct.DefinitionError, "wants 4 VMs; only 3 can be defined here"):
            ct.definition("dc1", "bgp-rr", {"network": ix.pk}, existing=CoreService.objects.get(pk=rr.pk))
        Site.objects.filter(code="dc1").update(
            dns_servers=["10.0.101.3", "10.0.101.4"],
            management={"hosts": ["10.0.101.8/29"], "ports": {"vyos": 55522, "ssh": 55523}},
            log_collectors={"core": {"host": "10.0.101.5", "proto": "tcp", "port": 15142}},
        )
        ServiceProfile.objects.filter(key="ns-primary").update(vms=1)
        self.assertEqual(len(ct.definition("dc1", "ns", {})["nodes"]), 1)
        ServiceProfile.objects.filter(key="admin").update(vms=2)
        d = ct.definition("dc1", "admin", {"first_ip": "10.0.101.9", "data_volume": self.vol.pk, "instances": 5})
        self.assertEqual(len(d["nodes"]), 2)  # an old "instances" value is no longer read
        ServiceProfile.objects.filter(key="onefe").update(vms=2)  # commit-416: a front end is three nodes
        with self.assertRaisesMessage(
            ct.DefinitionError, "A onefe service has 3 VMs; the service profile onefe says 2"
        ):
            ct._vm_count("onefe", {"nodes": [{}, {}, {}]}, None)
        ServiceProfile.objects.filter(key="onefe").update(vms=None)  # commit-416: a blank count checks the layout too
        with self.assertRaisesMessage(ct.DefinitionError, "A onefe service has 3 VMs; only 2 can be defined here"):
            ct._vm_count("onefe", {"nodes": [{}, {}]}, None)
        ServiceProfile.objects.filter(key="onefe").update(vms=3)  # and three are defined
        with self.assertRaisesMessage(ct.DefinitionError, "A onefe service has 3 VMs; only 2 can be defined here"):
            ct._vm_count("onefe", {"nodes": [{}, {}]}, None)
        ServiceProfile.objects.filter(key="log-collector").update(vms=2)  # the logs layout makes one VM
        with self.assertRaisesMessage(
            ct.DefinitionError, "A logs service has 1 VMs; the service profile log-collector says 2"
        ):
            ct.definition("dc1", "logs", {"volume": self.vol.pk})

    def test_415_a_lower_count_with_running_vms_is_refused(self):
        from inventory import core_types as ct
        from inventory.models import CoreService, Network, PlatformControl, ServiceProfile

        ix = Network.objects.get(site="dc1", name="cloud-ix")
        self.add("bgp-rr", network=ix.pk, vcpu=2, ram_gb=4)
        rr = CoreService.objects.get(stype="bgp-rr")
        third = rr.nodes.order_by("pk")[2]
        c = PlatformControl.objects.get(key="dc1.mgmt")
        c.vms = list(c.vms or []) + [{"name": third.name, "nics": [], "id": 900}]
        c.save()
        ServiceProfile.objects.filter(key="nf-ix").update(vms=2)
        form = ct.FORMS["bgp-rr"]({"network": str(ix.pk), "vcpu": 2, "ram_gb": 4}, site="dc1", svc=rr)
        self.assertTrue(form.is_valid(), form.errors)
        with self.assertRaisesMessage(ct.DefinitionError, f"these still run, remove them first: {third.name}"):
            ct.save("dc1", "bgp-rr", form, svc=rr)
        self.assertEqual(rr.nodes.count(), 3)  # nothing removed
        c.vms = [v for v in c.vms if v["name"] != third.name]  # not in the last read: the platform is asked live
        c.save()
        from inventory import core_deploy as cd

        c.mode = "adoption"  # commit-417: a read - allowed in adoption mode
        c.save()
        one = mock.patch("tenants.deploy.One", return_value=object())  # the client, as made for any read
        one.start()
        self.addCleanup(one.stop)
        with mock.patch.object(cd, "_vm", side_effect=lambda one, name: object() if name == third.name else None):
            with self.assertRaisesMessage(ct.DefinitionError, f"remove them first: {third.name}"):  # made since
                ct.save("dc1", "bgp-rr", form, svc=rr)
        with mock.patch.object(cd, "_vm", side_effect=OSError("connection refused")):
            with self.assertRaisesMessage(ct.DefinitionError, "cannot be asked (connection refused)"):
                ct.save("dc1", "bgp-rr", form, svc=rr)
        self.assertEqual(rr.nodes.count(), 3)
        with mock.patch.object(cd, "_vm", return_value=None):
            ct.save("dc1", "bgp-rr", form, svc=rr)  # not on the platform: its record goes
        self.assertEqual(rr.nodes.count(), 2)

    def test_411_a_route_reflector_follows_its_template(self):
        from inventory import core_types as ct
        from inventory.models import CoreService, Network, ServiceProfile

        ix = Network.objects.get(site="dc1", name="cloud-ix")
        evpn = Network.objects.create(
            site="dc1",
            name="vxlan",
            kind="evpn",
            interface="bond0",
            vlan=900,
            subnet="10.0.20.0",
            prefix=24,
            rrs=["10.0.20.241", "10.0.20.242", "10.0.20.243"],
        )
        self.add("bgp-rr", network=ix.pk, vcpu=2, ram_gb=4)
        rr = CoreService.objects.get(stype="bgp-rr")
        self.assertEqual(rr.profile.key, "nf-ix")
        form = ct.FORMS["bgp-rr"]({"network": str(evpn.pk), "vcpu": 2, "ram_gb": 4}, site="dc1", svc=rr)
        self.assertTrue(form.is_valid(), form.errors)
        ct.save("dc1", "bgp-rr", form, svc=rr)
        rr.refresh_from_db()
        self.assertEqual((rr.template.role, rr.profile.key), ("rr-vxlan", "nf-evpn"))  # moved: its profile too
        ServiceProfile.objects.filter(key="nf-ix").update(install_template="other")  # no profile names rr-ix
        form = ct.FORMS["bgp-rr"]({"network": str(ix.pk), "vcpu": 2, "ram_gb": 4}, site="dc1", svc=rr)
        self.assertTrue(form.is_valid(), form.errors)
        with self.assertRaisesMessage(ct.DefinitionError, "No service profile of bgp-rr names the template rr-ix"):
            ct.save("dc1", "bgp-rr", form, svc=rr)
        rr.refresh_from_db()
        self.assertEqual((rr.template.role, rr.profile.key), ("rr-vxlan", "nf-evpn"))  # refused: nothing saved

    def test_ns_admin_logs_backup(self):
        import ipaddress

        from inventory.models import CoreService, Network, Site

        r = self.add("ns", vcpu=1, ram_gb=2, disk_gb=10)  # commit-319: the site's DNS servers outside core-cloud
        self.assertFalse(CoreService.objects.filter(stype="ns").exists())
        self.assertContains(r, "site settings: DNS servers")
        # commit-318: ns, logs and admin on core-cloud (owner 18:44, Q19) - their addresses from that network
        Site.objects.filter(code="dc1").update(
            dns_servers=["10.0.101.3", "10.0.101.4"],
            management={"hosts": ["10.0.101.8/29"], "ports": {"vyos": 55522, "ssh": 55523}},
            log_collectors={"core": {"host": "10.0.101.5", "proto": "tcp", "port": 15142}},
        )
        core = Network.objects.get(site="dc1", name="cloud-core").net

        def on_nic1(svc):  # every saved NIC 1 address inside NIC 1's network, and no note says otherwise
            net = svc.nic_list()[0][0].net
            return all(ipaddress.ip_address(n.ips[0]) in net for n in svc.nodes.all())

        self.add("ns", vcpu=1, ram_gb=2, disk_gb=10)
        ns = CoreService.objects.get(stype="ns")
        self.assertEqual(
            [(n.name, n.hostname, n.ips) for n in ns.nodes.order_by("pk")],
            [("core-ns1_dc1", "sdc1ns1", ["10.0.101.3"])],  # commit-513: the default profile's slot - the 1st server
        )
        self.assertEqual((ns.template, ns.image.name, ns.ssh_secret), (None, "Ubuntu 26.04", self.secret.ref))
        self.add("ns", vcpu=1, ram_gb=2, disk_gb=10)
        self.assertEqual(CoreService.objects.filter(stype="ns").count(), 1)  # its slot is taken
        self.add("admin", vcpu=2, ram_gb=8, disk_gb=30, instances=3, first_ip="10.0.101.2", data_volume=self.vol.pk)
        self.assertFalse(CoreService.objects.filter(stype="admin").exists())  # .2 is not a management host
        self.add(
            "admin",
            vcpu=2,
            ram_gb=8,
            disk_gb=30,
            instances=3,
            first_ip="10.0.101.9",
            data_volume=self.vol.pk,
            home_volume=self.vol.pk,
            home_subdir="/admins/admin1",
        )
        adm = CoreService.objects.get(stype="admin")
        self.assertEqual(
            [(n.name, n.hostname, n.ips) for n in adm.nodes.order_by("pk")],
            [(f"core-admin{i}_dc1", f"sdc1admin{i}", [f"10.0.101.{8 + i}", ""]) for i in (1, 2, 3)],
        )
        self.assertEqual(
            [n.name for n, _g in adm.nic_list()], ["cloud-core", "data-nfs"]
        )  # owner 18:44 (T34 Q19): NIC 1 core-cloud; the volume's storage network
        self.assertEqual([n.name for n, _g in ns.nic_list()], ["cloud-core"])
        self.assertEqual(adm.ssh_port, 55274)  # T36 commit-473: Settings → Security's public SSH port, not the site's
        self.add("logs", vcpu=6, ram_gb=48, disk_gb=20, volume=self.vol.pk)
        logs = CoreService.objects.get(stype="logs")
        self.assertEqual(
            [(n.name, n.hostname, n.ips) for n in logs.nodes.all()],
            [("core-logs1_dc1", "sdc1logs", ["10.0.101.5", ""])],
        )
        self.assertEqual([n.name for n, _g in logs.nic_list()], ["cloud-core", "data-nfs"])
        for svc in (ns, adm, logs):
            self.assertEqual(svc.nic_list()[0][0].net, core)
            self.assertTrue(on_nic1(svc), svc.name)
        from inventory import core_types as ct

        for svc in (ns, adm, logs):  # the calculated definition notes no address outside NIC 1
            notes = ct.definition("dc1", svc.stype, svc.params or {}, existing=svc)["notes"]
            self.assertFalse([x for x in notes if "is not in" in x], notes)
        r = self.add("backup", vcpu=2, ram_gb=8, disk_gb=10, datastore=self.ds.pk)  # commit-319: 203.0.113.6
        self.assertFalse(CoreService.objects.filter(stype="backup").exists())
        self.assertContains(r, "restic VM address")
        type(self.ds).objects.filter(pk=self.ds.pk).update(restic_ip="10.0.101.6")  # Q20: on cloud-core
        self.add("backup", vcpu=2, ram_gb=8, disk_gb=10, datastore=self.ds.pk)
        b = CoreService.objects.get(stype="backup")
        self.assertEqual((b.name, b.service_secret, b.serves.key), ("mgmt-backup", self.pw.ref, "dc1.mgmt"))
        self.assertEqual(
            [(n.name, n.hostname, n.ips) for n in b.nodes.all()],
            [("core-mgmt-backup1_dc1", "sdc1mgmtbackup", ["10.0.101.6", ""])],
        )  # NIC 1: the datastore's restic VM
        self.assertEqual(b.nic_list()[0][0].net, core)  # owner 19:03-19:04 (Q20): no VM but egw/igw on core-services
        self.assertTrue(on_nic1(b))
        page = self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=core").content.decode()
        self.assertIn("core-admin3_dc1", page)
        self.assertIn("Calculated definition", page)

    def test_commit_328_the_logs_collector_resolved_by_its_own_name(self):
        from inventory.models import CoreService, Site

        Site.objects.filter(code="dc1").update(
            log_collectors={
                "core": {"host": "logs.dc1.example", "proto": "tcp", "port": 15142},
                "vyos": {"host": "203.0.113.99", "proto": "udp", "port": 514},
            }
        )
        with mock.patch("socket.gethostbyname", return_value="10.0.101.5") as gh:
            self.add("logs", vcpu=6, ram_gb=48, disk_gb=20, volume=self.vol.pk)
        gh.assert_called_with("logs.dc1.example")
        logs = CoreService.objects.get(stype="logs")
        self.assertEqual(logs.nodes.get().ips, ["10.0.101.5", ""])
        logs.delete()
        with mock.patch("socket.gethostbyname", return_value="203.0.113.5"):  # resolved outside core-cloud: refused
            r = self.add("logs", vcpu=6, ram_gb=48, disk_gb=20, volume=self.vol.pk)
        self.assertFalse(CoreService.objects.filter(stype="logs").exists())
        self.assertContains(r, "site settings: log collector")
        with mock.patch("socket.gethostbyname", return_value="10.0.101.5"):
            self.add("logs", vcpu=6, ram_gb=48, disk_gb=20, volume=self.vol.pk)
        logs = CoreService.objects.get(stype="logs")
        for err in (OSError("no such name"), UnicodeError("label too long")):  # commit-334, commit-335
            with mock.patch("socket.gethostbyname", side_effect=err):
                r = self.client.post(
                    reverse("core_save", args=["dc1", logs.pk]),
                    {
                        "stype": "logs",
                        "state": "deployed",
                        "vcpu": 6,
                        "ram_gb": 48,
                        "disk_gb": 20,
                        "volume": self.vol.pk,
                    },
                    follow=True,
                )
            self.assertContains(r, "cannot be resolved")
            self.assertEqual(logs.nodes.get().ips, ["10.0.101.5", ""])  # the stored address kept

    def test_gateways_and_existing_vms_keep_their_names(self):
        """Correction 11: cloud-gw — one service, its egw and igw; cloud-egw / cloud-igw are not made any more."""
        from inventory.models import CoreService, CoreServiceNode, Network, NetworkRange

        ix = Network.objects.get(site="dc1", name="cloud-ix")
        Network.objects.filter(site="dc1", name="cloud-core").update(
            gateway="10.0.101.1"
        )  # the core router, not the igw
        self.add("cloud-gw", vcpu=2, ram_gb=4)
        self.assertFalse(CoreService.objects.filter(stype="cloud-gw").exists())  # no cloud-gw range on the exchange yet
        NetworkRange.objects.create(network=ix, name="cloud-gw", start="10.0.10.9", length=1)
        self.add("cloud-egw", vcpu=2, ram_gb=4)
        self.assertFalse(CoreService.objects.filter(stype="cloud-egw").exists())
        self.add(
            "cloud-gw",
            vcpu=2,
            ram_gb=4,
            aliases="203.0.113.3 203.0.113.4",
            config_diff="set nat destination rule 111 destination address '203.0.113.2'",
        )
        gw = CoreService.objects.get(stype="cloud-gw")
        egw, igw = gw.nodes.order_by("pk")
        self.assertEqual(
            (egw.name, egw.hostname, egw.ips, egw.role),
            ("cloud-egw-dc1", "cloud-egw-dc1", ["", "10.0.10.9", "203.0.113.1"], "egw"),
        )
        self.assertEqual(
            (igw.name, igw.ips, igw.role), ("cloud-igw-dc1", ["", "203.0.113.2", "10.0.101.254"], "igw")
        )  # the last address of core-cloud
        self.assertEqual([n.name for n, _g in egw.nic_list()], ["cloud-vyos", "cloud-ix", "cloud-services"])
        self.assertEqual([n.name for n, _g in igw.nic_list()], ["cloud-vyos", "cloud-services", "cloud-core"])
        self.assertEqual(igw.params["aliases"], ["203.0.113.3", "203.0.113.4"])
        self.assertEqual((egw.params["checks"], igw.params["checks"]), ({"bgp": "ipv4"}, {}))
        from inventory import core_deploy as cd

        e = cd.config_of(egw)
        self.assertIn(
            "set protocols bgp parameters router-id 10.0.10.9", e
        )  # the exchange address of the cloud-gw range
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

        self.w = World(
            {"one.clusterpool.info": "<CLUSTER_POOL><CLUSTER><ID>0</ID><NAME>mgmt_dc1</NAME></CLUSTER></CLUSTER_POOL>"},
            vnets=[
                {"id": 1, "name": "cloud-ix_dc1", "ars": [{"TYPE": "IP4", "IP": "10.0.10.1", "SIZE": 250}]},
                {"id": 3, "name": "cloud-services_dc1", "ars": [{"TYPE": "IP4", "IP": "203.0.113.1", "SIZE": 14}]},
                {"id": 4, "name": "cloud-core_dc1", "ars": [{"TYPE": "IP4", "IP": "10.0.101.1", "SIZE": 254}]},
                {"id": 8, "name": "cloud-vyos_dc1", "ars": [{"TYPE": "IP4", "IP": "10.0.20.100", "SIZE": 20}]},
            ],
            vms=[
                {
                    "id": 163,
                    "name": "cloud-egw_dc1",
                    "nics": [{"network_id": 1, "ip": "10.0.10.9"}, {"network_id": 3, "ip": "203.0.113.1"}],
                },
                {
                    "id": 162,
                    "name": "cloud-igw_dc1",
                    "nics": [
                        {"network_id": 3, "ip": "203.0.113.2"},
                        {"network_id": 4, "ip": "10.0.101.254"},
                        {"network_id": 1, "ip": "10.0.10.8"},
                    ],
                },
            ],
        )
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
        NetworkRange.objects.create(
            network=Network.objects.get(site="dc1", name="cloud-ix"), name="cloud-gw", start="10.0.10.9", length=1
        )
        self.add(
            "cloud-gw",
            vcpu=2,
            ram_gb=4,
            aliases="203.0.113.3 203.0.113.4",
            config_diff="set nat destination rule 111 destination address '203.0.113.2'",
        )
        self.egw, self.igw = CoreService.objects.get(stype="cloud-gw").nodes.order_by("pk")
        self.fake = GwWorld()

    def run_redeploy(self, node, ok=True, keep_old=True, how="redeploy"):
        from accounts import tasks
        from inventory import core_deploy as cd

        def read_node(n, site):  # as the real one: the facts made again, the redeploy record kept
            n.facts = {
                "checks": [{"check": "BGP (ipv4) peers established", "ok": ok, "detail": "3/3" if ok else "0/3"}],
                **({"redeploy": n.facts["redeploy"]} if "redeploy" in (n.facts or {}) else {}),
            }
            n.facts_error = "" if ok or n.role == "egw" else "ssh: timed out"
            n.save()
            return True

        run = {
            "redeploy": lambda t: cd.redeploy(t, node, None, None, False, keep_old),
            "rollback": lambda t: cd.rollback(t, node),
            "plan": lambda t: cd.plan(node),
        }[how]
        with (
            mock.patch("adoption.collector.client_for", return_value=self.fake),
            mock.patch("inventory.nfconfig.is_complete", return_value=True),
            mock.patch("inventory.core_services.read_node", side_effect=read_node),
            mock.patch.object(cd, "MGMT_TIMEOUT", 0),
            mock.patch.object(cd, "BGP_TIMEOUT", 0),
        ):
            return tasks.start("core VM redeploy", "test", run)

    def nics(self, vm_id):
        return [(n["vid"], n["ip"], n["parent"]) for n in self.fake.w.vms[vm_id]["nics"]]

    def test_plan_reads_only(self):
        t = self.run_redeploy(self.igw, how="plan")
        self.assertEqual(t.state, "done", t.error)
        p = t.result
        self.assertEqual(
            (p["old_id"], p["new_name"], p["aliases"]), (162, "cloud-igw-dc1", ["203.0.113.3", "203.0.113.4"])
        )
        self.assertEqual(
            [(n["net"], n["ip"]) for n in p["new_nics"]],
            [
                ("cloud-vyos_dc1", "10.0.20.100"),
                ("cloud-services_dc1", "203.0.113.2"),
                ("cloud-core_dc1", "10.0.101.254"),
            ],
        )
        self.assertEqual(
            sorted(k["ip"] for k in p["kept"]), ["10.0.101.254", "203.0.113.2", "203.0.113.3", "203.0.113.4"]
        )
        self.assertTrue(p["definition_ok"], p["checks"])
        self.assertFalse(
            [
                m
                for m, _a in self.fake.w.calls
                if m not in ("one.vmpool.info", "one.vm.info", "one.vnpool.info", "one.vn.info", "one.clusterpool.info")
            ]
        )
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
        self.assertEqual(
            self.nics(new),
            [
                (8, "10.0.20.100", ""),
                (3, "203.0.113.2", ""),
                (4, "10.0.101.254", ""),
                (3, "203.0.113.3", "VMNIC1"),
                (3, "203.0.113.4", "VMNIC1"),
            ],
        )
        self.assertIn('AUTOSTART = "always"', w.vms[new]["template"])
        self.assertIn('SCHED_REQUIREMENTS = "HYPERVISOR=kvm"', w.vms[new]["template"])
        self.assertIn('MEMORY_RESIZE_MODE = "BALLOONING"', w.vms[new]["template"])
        self.assertEqual(w.vms[162]["state"], "6")  # not kept: removed once the new one works
        self.igw.refresh_from_db()
        self.assertEqual(
            (self.igw.name, self.igw.address, self.igw.deployed_vm_id), ("cloud-igw-dc1", "10.0.20.100", new)
        )

    def test_egw_checks_bgp_and_a_failure_gives_everything_back(self):
        t = self.run_redeploy(self.egw, ok=False)
        self.assertEqual(t.state, "failed")
        self.assertIn("rolled back", t.error)
        w = self.fake.w
        self.assertEqual((w.vms[163]["name"], w.vms[163]["state"]), ("cloud-egw_dc1", "3"))
        self.assertEqual(sorted(self.nics(163)), [(1, "10.0.10.9", ""), (3, "203.0.113.1", "")])
        self.assertTrue(all(v["state"] == "6" for i, v in w.vms.items() if i not in (162, 163)))
        self.egw.refresh_from_db()
        self.assertEqual(
            self.egw.name, "cloud-egw-dc1"
        )  # its name in the portal; the VM is found by the earlier one too

    def test_setting_the_old_igw_aside_stopped_half_way_is_undone(self):
        """core-017 again (seventh full review): a failure while the old igw is set aside (after one NIC or alias is
        detached, or at the rename) is undone - back under its name with every address and alias, running - and its
        "Roll back" record was written before the first change."""
        from adoption.collector import OneError

        for at in (1, 3, "rename"):
            with self.subTest(at=at):
                self.fake = GwWorld()
                shape = lambda vm: sorted((v, ip, bool(par)) for v, ip, par in self.nics(vm))  # noqa: E731
                before = shape(162)
                real, seen = self.fake._call, {"detach": 0, "failed": False}

                def call(method, *args, at=at, real=real, seen=seen):
                    if method == "one.vm.detachnic":
                        seen["detach"] += 1
                        if seen["detach"] == at and not seen["failed"]:
                            seen["failed"] = True
                            raise OneError("detach: no answer")
                    if method == "one.vm.rename" and at == "rename" and not seen["failed"]:
                        seen["failed"] = True
                        real(method, *args)  # renamed, then the answer was an error
                        raise OneError("rename: no answer")
                    return real(method, *args)

                self.fake._call = call
                t = self.run_redeploy(self.igw)
                self.assertEqual(t.state, "failed")
                w = self.fake.w
                self.assertEqual((w.vms[162]["name"], w.vms[162]["state"]), ("cloud-igw_dc1", "3"))
                self.assertEqual(shape(162), before)  # every address back, the aliases on a NIC again
                self.assertNotIn("one.vm.allocate", [m for m, _a in w.calls])
                self.igw.refresh_from_db()
                self.assertNotIn("redeploy", self.igw.facts or {})  # rolled back: the record is gone again

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
        self.assertEqual(
            sorted((n["ip"], n["parent"]) for n in nics if n["alias"]),
            [("203.0.113.3", cs["name"]), ("203.0.113.4", cs["name"])],
        )
        self.assertEqual(w.vms[t.result["old_id"]]["name"], "cloud-igw_dc1")
        self.assertFalse([i for i, v in w.vms.items() if v["name"] == "cloud-igw-dc1" and v["state"] != "6"])


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode(), DEPLOY_ASYNC=False)
class PortalGw(TestCase):
    """Correction 12: portal-gw — the two egws of a portal platform, VRRP on its public overlays, cloud-igw template."""

    setUp_base = CoreServices.setUp
    setUp_types = CoreTypes.setUp
    add = CoreTypes.add

    def setUp(self):
        from inventory.models import Cluster, DelegatedSubnet, Network, Platform, PlatformControl, ServiceImage, Site

        self.setUp_types()
        site = Site.objects.get(code="dc1")
        self.portal = Platform.objects.create(
            key="dc1.portal",
            site=site,
            name="portal",
            kind="ext-managed",
            sa=Platform.objects.get(key="dc1.mgmt").sa,
            segments=["portal-ix"],
        )
        Cluster.objects.create(platform=self.portal, name="shared-intel-1_dc1", kind="ext-managed")
        c = PlatformControl.objects.create(key="dc1.portal", mode="execution")
        ServiceImage.objects.create(
            control=c, role="vyos", purpose="vyos", kind="image", name="VyOS", one_id=5, customization="context"
        )
        Network.objects.create(
            site="dc1",
            name="portal-ix",
            kind="ix",
            interface="bond0",
            vlan=109,
            subnet="10.0.109.0",
            prefix=24,
            rrs=["10.0.109.241", "10.0.109.242"],
            local_asn=4200101092,
        )
        DelegatedSubnet.objects.create(site=site, segment="portal-ix", cidr="198.51.100.0/25", purpose="inet")

    def test_definition_vrrp_and_plan(self):
        from inventory import core_deploy as cd
        from inventory.models import CoreService, Network

        self.add("portal-gw", vcpu=2, ram_gb=4)
        svc = CoreService.objects.get(stype="portal-gw")
        self.assertEqual(svc.platform.key, "dc1.portal")  # on the portal platform, not the management one
        e1, e2 = svc.nodes.order_by("pk")
        self.assertEqual(
            (e1.name, e1.ips, e2.name, e2.ips),
            ("portal-egw1-dc1", ["", "", "198.51.100.2"], "portal-egw2-dc1", ["", "", "198.51.100.3"]),
        )
        pub = Network.objects.get(site="dc1", name="portal-public01")
        self.assertEqual((pub.kind, pub.cidr, pub.gateway), ("public", "198.51.100.0/25", "198.51.100.1"))
        self.assertEqual([n.name for n, _g in e1.nic_list()], ["cloud-vyos", "portal-ix", "portal-public01"])
        c1, c2 = cd.config_of(e1), cd.config_of(e2)
        for line in (
            "set high-availability vrrp group PUB01 interface eth2",
            "set high-availability vrrp group PUB01 vrid 1",
            "set high-availability vrrp group PUB01 address 198.51.100.1/25",
            "set firewall ipv4 name CLIENT-to-GW rule 20 protocol vrrp",
            "set protocols bgp neighbor 10.0.109.241 peer-group cloud-int",
            "set firewall group network-group CLIENT network 198.51.100.0/25",
        ):
            self.assertIn(line, c1)
        self.assertIn("set high-availability vrrp group PUB01 priority 200", c1)
        self.assertIn("set high-availability vrrp group PUB01 priority 100", c2)
        # the plan reads the portal platform: no cloud-vyos there yet (must), portal-ix free address, the egw's public
        # address added as an address range by the redeploy, the VIP
        from tenants.fakeone import World

        w = World(
            {
                "one.clusterpool.info": "<CLUSTER_POOL><CLUSTER><ID>0</ID><NAME>shared-intel-1_dc1</NAME></CLUSTER></CLUSTER_POOL>",
                "one.vntemplatepool.info": "<VNTEMPLATE_POOL><VNTEMPLATE><ID>0</ID><NAME>portal-overlays_dc1</NAME></VNTEMPLATE></VNTEMPLATE_POOL>",
            },
            vnets=[
                {"id": 1, "name": "portal-ix_dc1", "ars": [{"TYPE": "IP4", "IP": "10.0.109.10", "SIZE": 20}]},
                {
                    "id": 0,
                    "name": "portal-public01_dc1",
                    "ars": [
                        {"TYPE": "IP4", "IP": "198.51.100.1", "SIZE": 1},
                        {"TYPE": "IP4", "IP": "198.51.100.8", "SIZE": 100},
                    ],
                },
            ],
            vms=[
                {
                    "id": 0,
                    "name": "portal-egw1_dc1",
                    "nics": [{"network_id": 1, "ip": "10.0.109.10"}, {"network_id": 0, "ip": "198.51.100.1"}],
                }
            ],
        )
        with (
            mock.patch("adoption.collector.client_for", return_value=w),
            mock.patch("inventory.nfconfig.is_complete", return_value=True),
        ):
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
        f = TemplateForm(
            {"name": boot.name, "os": "vyos", "os_version": "2026.03", "description": "", "body": boot.body},
            instance=boot,
        )
        self.assertTrue(f.is_valid(), f.errors)  # a bash script is valid as a template body (nothing to render)
        self.assertEqual(f.cleaned_data["body"], boot.body)
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
        r = self.client.get(reverse("infra_templates") + "?preview=vyos-postconfig-bootup&site=dc1")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("A script of the VyOS image, kept as it is", html)
        self.assertIn("ONE_TIME_INIT_SCRIPT_BASE64", html)


class NodeIpsFromTheCollection(TestCase):
    """Owner 2026-11-15: "prod must store fe nodes ip" - empty node IPs from the collected zone servers, by name."""

    def test_fill(self):
        from inventory.nodeips import fill

        got = [
            {"name": "sdc1one1.example.com", "addr": "192.0.2.31"},
            {"name": "sdc1one2", "addr": "192.0.2.32"},
            {"name": "other", "addr": "192.0.2.99"},
        ]
        fe, changed = fill({"nodes": ["sdc1one1", "sdc1one2", "sdc1one3"]}, got)
        self.assertTrue(changed)
        self.assertEqual(fe["node_ips"], ["192.0.2.31", "192.0.2.32", ""])
        fe, changed = fill({"nodes": ["sdc1one1", "sdc1one2"], "node_ips": ["192.0.2.41", ""]}, got)
        self.assertEqual(fe["node_ips"], ["192.0.2.41", "192.0.2.32"])  # a stored IP stays as it was set
        self.assertEqual(
            fill({"nodes": ["x"], "node_ips": ["192.0.2.5"]}, got), ({"nodes": ["x"], "node_ips": ["192.0.2.5"]}, False)
        )


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode())
class CoreConfigCheck(TestCase):
    """owner 2026-11-17: the VyOS timer compares the route reflectors and the sites' egw/igw with their templates."""

    def test_a_route_reflector_is_compared_and_the_result_kept(self):
        from inventory import core_services as cs
        from inventory.models import CoreService
        from inventory.sshexec import SshError
        from tenants import configcheck

        site_with_vms()
        cs.seed("dc1")
        node = CoreService.objects.get(name="cloud-ix-rrs").nodes.get()
        self.assertIn(node, configcheck.core_nodes())
        expected = "set protocols bgp neighbor 10.0.10.242 peer-group cloud-rr\nset firewall group address-group CLOUD-RR address 10.0.10.241\n"
        actual = "set protocols bgp neighbor 10.0.10.242 peer-group 'cloud-rr'\nset protocols bgp listen range 10.0.10.240/30 peer-group 'cloud-rr'\n"
        with (
            mock.patch("inventory.core_deploy.config_of", return_value=expected),
            mock.patch("inventory.credentials.account", return_value=("ops", {"key": "K"})),
            mock.patch("inventory.sshexec.run", return_value=actual) as run,
        ):
            r = configcheck.check_core(node)
        self.assertEqual(run.call_args.args[:2], (node.address, node.ssh_port))
        self.assertEqual(r["status"], "drift")
        self.assertEqual(r["missing"], ["set firewall group address-group CLOUD-RR address 10.0.10.241"])
        self.assertEqual(r["extra"], ["set protocols bgp listen range 10.0.10.240/30 peer-group cloud-rr"])
        node.refresh_from_db()
        self.assertEqual(node.facts["config"]["status"], "drift")
        with mock.patch("inventory.sshexec.run", side_effect=SshError("timed out")):
            cs.read_node(node, "dc1")  # a later read of the node keeps the last comparison
        node.refresh_from_db()
        self.assertEqual(node.facts["config"]["status"], "drift")

    def test_a_node_read_during_the_comparison_keeps_its_result(self):
        """commit-053: a read of the node that started before the comparison saved does not erase it."""
        from inventory import core_services as cs
        from inventory.models import CoreService, CoreServiceNode
        from inventory.sshexec import SshError
        from tenants import configcheck

        site_with_vms()
        cs.seed("dc1")
        node = CoreService.objects.get(name="cloud-ix-rrs").nodes.get()
        stale = CoreServiceNode.objects.get(pk=node.pk)  # the read's copy, loaded before the comparison

        def ssh(*a, **k):  # the comparison saves while the read waits for the router
            with (
                mock.patch("inventory.core_deploy.config_of", return_value="set a b\n"),
                mock.patch("inventory.credentials.account", return_value=("ops", {"key": "K"})),
                mock.patch("inventory.sshexec.run", return_value="set a b\n"),
            ):
                configcheck.check_core(node)
            raise SshError("timed out")

        with mock.patch("inventory.sshexec.run", side_effect=ssh):
            cs.read_node(stale, "dc1")
        node.refresh_from_db()
        self.assertEqual(node.facts["config"]["status"], "ok")
        self.assertEqual(node.facts_error, "timed out")

    def test_credentials_are_hidden_by_their_place_in_the_command(self):
        """commit-050: a password the portal does not know is hidden too."""
        from tenants.configcheck import compare, mask

        _missing, extra = compare(
            "set protocols bgp peer-group cloud-rr password 'Known-1'\n",
            "set protocols bgp peer-group cloud-rr password 'Unknown-Live-9'\n",
        )
        out = mask(extra, ["Known-1"])
        self.assertEqual(out, ["set protocols bgp peer-group cloud-rr password «secret»"])
        _missing, extra = compare(  # commit-050: a quoted value with spaces goes whole
            "set protocols bgp peer-group cloud-rr password 'Known-1'\n",
            "set protocols bgp peer-group cloud-rr password 'two words and more'\n",
        )
        self.assertEqual(mask(extra, []), ["set protocols bgp peer-group cloud-rr password «secret»"])
        _missing, extra = compare(  # commit-050: a password holding a login field's name is still a password, all of it
            "set protocols bgp peer-group cloud-rr password 'Known-1'\n",
            "set protocols bgp peer-group cloud-rr password 'my secret plaintext-password tail'\n",
        )
        self.assertEqual(mask(extra, []), ["set protocols bgp peer-group cloud-rr password «secret»"])
        _missing, extra = compare(  # commit-050: even a value that begins like a mask
            "set protocols bgp peer-group cloud-rr password 'Known-1'\n",
            "set protocols bgp peer-group cloud-rr password '«x»Live-7'\n",
        )
        self.assertEqual(mask(extra, []), ["set protocols bgp peer-group cloud-rr password «secret»"])
        self.assertEqual(
            mask(["set system login user vyos authentication encrypted-password $6$abc$def"], []),
            ["set system login user vyos authentication encrypted-password «hash»"],
        )
        self.assertEqual(
            mask(
                [
                    "set interfaces wireguard wg9 peer src preshared-key abc",
                    "set service ssh disable-password-authentication",
                ],
                [],
            ),
            [
                "set interfaces wireguard wg9 peer src preshared-key «secret»",
                "set service ssh disable-password-authentication",
            ],
        )

    def test_what_the_template_deletes_must_not_be_on_the_router(self):
        """commit-052: a main-table default route the template deletes is reported, the management VRF's is not."""
        from tenants.configcheck import compare

        expected = "delete protocols static route 0.0.0.0/0\nset vrf name MGMT protocols static route 0.0.0.0/0 next-hop 10.0.20.1\nset service ssh port 55522\n"
        ok = "set vrf name MGMT protocols static route 0.0.0.0/0 next-hop '10.0.20.1'\nset service ssh port '55522'\n"
        self.assertEqual(compare(expected, ok), ([], []))
        _m, extra = compare(expected, ok + "set protocols static route 0.0.0.0/0 next-hop '10.0.10.1'\n")
        self.assertEqual(extra, ["set protocols static route 0.0.0.0/0 next-hop 10.0.10.1"])
