"""Settings → HAProxy (owner 2026-10-31): the installation's names, front ends and public ports in the portal's
settings; claugine-lb apply | certificate build install-lb.sh's options from them (manage.py haproxy_args)."""

import io
import os
import tempfile

from django.core.management import CommandError, call_command
from django.test import override_settings
from django.urls import reverse

from accounts import haproxy
from accounts.tests import Base
from inventory.models import Site
from tenants.models import PortalSettings

FORM = {
    "section": "haproxy",
    "tab": "haproxy",
    "other_names": "portal.example.com",
    "le_email": "admin@example.com",
    "port_fireedge": "55516",
    "port_grpc": "55534",
    "port_xmlrpc": "55533",
    "port_onegate": "55530",
    "port_oneflow": "55574",
    "one_names_dc1": "one-dc1.example.com",
    "one_addr_dc1": "10.81.101.30",
    "one_names_dc2": "",
    "one_addr_dc2": "",
}
NODE = {"SITE": "dc1", "ADDR": "10.81.101.25", "PEER_ADDR": "10.82.101.25", "STATUS_PORTS": "55587"}


class HAProxySettings(Base):
    def setUp(self):
        from inventory.models import DelegatedSubnet, Platform

        dc1 = Site.objects.get_or_create(code="dc1")[0]
        Site.objects.get_or_create(code="dc2")
        # u01: the front end is chosen by its VIP; the public networks come from its platform's ix segments
        Platform.objects.create(
            key="dc1.cloud",
            site=dc1,
            name="cloud",
            kind="shared",
            fe_vip="10.81.101.30",
            segments=["cloud-ix", "corpnet-ix"],
        )
        DelegatedSubnet.objects.create(site=dc1, segment="cloud-ix", cidr="198.18.93.0/25", purpose="inet")
        DelegatedSubnet.objects.create(site=dc1, segment="corpnet-ix", cidr="10.81.104.0/22", purpose="corpnet")
        s = PortalSettings.get()  # u02: the portal's main name is the host of the Public URL (Settings → Provider)
        s.public_url = "https://my.example.com"
        s.save()

    def test_defaults_are_opennebulas_own_ports_and_nothing_of_an_installation(self):
        PortalSettings.objects.update(public_url="")
        conf = haproxy.get(PortalSettings.get())
        self.assertEqual(
            conf["ports"], {"fireedge": 2616, "grpc": 2634, "xmlrpc": 2633, "onegate": 5030, "oneflow": 2474}
        )
        self.assertEqual((conf["public_names"], conf["le_email"], conf["sites"]), ("", "", {}))
        with self.assertRaises(haproxy.Incomplete):  # nothing to build HAProxy from yet
            haproxy.args_for(conf, NODE)

    def test_validation(self):
        bad = {
            **FORM,
            "other_names": "my example",
            "le_email": "x",
            "port_grpc": "443",
            "port_xmlrpc": "55516",
            "one_addr_dc1": "",
            "one_names_dc2": "one-dc2.example.com",
            "one_addr_dc2": "10.81.101.30",
        }
        _conf, err = haproxy.clean(bad, ["dc1", "dc2"])
        self.assertEqual(set(err), {"other_names", "le_email", "port_grpc", "ports", "one_addr_dc1", "one_addr_dc2"})
        self.assertIn("not the VIP", err["one_addr_dc2"])  # dc1's VIP is not dc2's
        conf, err = haproxy.clean(FORM, ["dc1", "dc2"])
        self.assertEqual(err, {})
        self.assertEqual(
            conf["sites"],
            {"dc1": {"one_names": "one-dc1.example.com", "one_addr": "10.81.101.30", "local_nets": "198.18.93.0/25"}},
        )  # dc2: no front end through HAProxy
        self.assertEqual(conf["public_names"], "my.example.com,portal.example.com")

    def test_options_for_each_vm(self):
        conf, _e = haproxy.clean(FORM, ["dc1", "dc2"])
        s = PortalSettings.get()
        s.haproxy = conf
        s.save()
        conf = haproxy.get(s)
        self.assertEqual(
            haproxy.args_for(conf, NODE),
            [
                "--apps",
                "10.81.101.25,10.82.101.25",
                "--peer-lb",
                "10.82.101.25",
                "--public-name",
                "my.example.com,portal.example.com",
                "--one-name",
                "one-dc1.example.com",
                "--one-addr",
                "10.81.101.30",
                "--local-nets",
                "198.18.93.0/25",
                "--fireedge-port",
                "55516",
                "--grpc-port",
                "55534",
                "--xmlrpc-port",
                "55533",
                "--onegate-port",
                "55530",
                "--oneflow-port",
                "55574",
                "--status-port",
                "55587",
            ],
        )
        # the other site's VM: the same app order, the other peer, no front end of its own
        other = haproxy.args_for(
            conf, {"SITE": "dc2", "ADDR": "10.82.101.25", "PEER_ADDR": "10.81.101.25", "STATUS_PORTS": "55587"}
        )
        self.assertEqual(other[:4], ["--apps", "10.81.101.25,10.82.101.25", "--peer-lb", "10.81.101.25"])
        self.assertNotIn("--one-name", other)
        self.assertEqual(
            haproxy.args_for(conf, NODE, letsencrypt=True)[-2:], ["--letsencrypt-email", "admin@example.com"]
        )
        self.assertEqual(haproxy.args_for(conf, {**NODE, "HTTPS_PORT": "55543"})[-2:], ["--app-port", "55543"])
        conf["le_email"] = ""
        with self.assertRaises(haproxy.Incomplete):
            haproxy.args_for(conf, NODE, letsencrypt=True)

    def test_settings_tab_and_the_command(self):
        sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(sa)
        r = self.client.get(reverse("portal_settings") + "?tab=haproxy")  # the old name: now Settings → Portal
        self.assertContains(r, "my.example.com")  # u02: the main name from Settings → Provider
        self.assertContains(r, 'name="other_names"')
        r = self.client.get(reverse("portal_settings") + "?tab=platform")  # u02: the front ends under Platform
        self.assertContains(r, 'name="one_addr_dc2"')
        self.assertContains(r, '<option value="10.81.101.30" >10.81.101.30 · dc1.cloud</option>', html=False)
        self.assertNotContains(r, 'name="local_nets_dc1"')  # shown, not entered
        r = self.client.post(reverse("portal_settings"), {**FORM, "port_fireedge": "80"})
        self.assertContains(r, "HAProxy&#x27;s own")  # shown again with the error, nothing saved
        self.assertEqual(PortalSettings.get().haproxy, {})
        r = self.client.post(reverse("portal_settings"), FORM)
        self.assertRedirects(r, reverse("portal_settings") + "?tab=platform", fetch_redirect_response=False)
        self.assertEqual(PortalSettings.get().haproxy["ports"]["oneflow"], 55574)
        node = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        node.write("".join(f"{k}={v}\n" for k, v in NODE.items()))
        node.close()
        try:
            with override_settings(NODE_ENV_FILE=node.name):
                from accounts import thisnode

                thisnode.forget()
                out = io.StringIO()
                call_command("haproxy_args", "--letsencrypt", stdout=out)
                lines = out.getvalue().strip().split("\n")
                self.assertEqual(lines[:2], ["--apps", "10.81.101.25,10.82.101.25"])
                self.assertEqual(lines[-2:], ["--letsencrypt-email", "admin@example.com"])
                r = self.client.get(reverse("portal_settings") + "?tab=portal")
                self.assertContains(r, "--one-name one-dc1.example.com")
                PortalSettings.objects.update(haproxy={}, public_url="")
                with self.assertRaises(CommandError):
                    call_command("haproxy_args", stdout=io.StringIO())
        finally:
            os.unlink(node.name)
            thisnode.forget()


class ProjectsAndIgwPorts(Base):
    """Owner 2026-10-31: /tenants/… now /projects/… (old links redirect); the igw ssh port range in Settings → Data."""

    def test_old_addresses_redirect(self):
        r = self.client.get("/tenants/12/?sel=t-12&igw=configs")
        self.assertEqual((r.status_code, r["Location"]), (301, "/projects/12/?sel=t-12&igw=configs"))
        r = self.client.get("/my/tenants/new/")
        self.assertEqual((r.status_code, r["Location"]), (301, "/my/projects/new/"))
        self.assertTrue(reverse("tenant_detail", args=[3]).startswith("/projects/3/"))

    def test_igw_port_range(self):
        import zlib

        from inventory.nfconfig import _igw_port_of
        from tenants.models import Tenant

        self.assertEqual(Tenant.igw_ports(), (50000, 59999))
        self.assertEqual(_igw_port_of("tenant-x"), 50000 + zlib.crc32(b"tenant-x") % 10000)  # as before by default
        sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(sa)
        self.client.post(
            reverse("portal_settings"),
            {"section": "igw_ports", "tab": "appearance", "igw_port_min": "9000", "igw_port_max": "9100"},
        )
        self.assertEqual(Tenant.igw_ports(), (50000, 59999))  # refused: below 10000
        self.client.post(
            reverse("portal_settings"),
            {"section": "igw_ports", "tab": "appearance", "igw_port_min": "20000", "igw_port_max": "20099"},
        )
        self.assertEqual(Tenant.igw_ports(), (20000, 20099))
        self.assertTrue(all(20000 <= Tenant.random_igw_port() <= 20099 for _i in range(50)))
        self.assertTrue(20000 <= _igw_port_of("tenant-x") <= 20099)
        self.assertContains(
            self.client.get(reverse("portal_settings") + "?tab=platform"), 'name="igw_port_min" value="20000"'
        )
