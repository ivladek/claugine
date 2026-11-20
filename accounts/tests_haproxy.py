"""Settings → HAProxy (owner 2026-10-31): the installation's names and front ends in the portal's
settings; claugine-lb apply | certificate build install-lb.sh's options from them (manage.py haproxy_args)."""

import io
import os
import tempfile
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from accounts import haproxy
from accounts.tests import Base, igw_block, provider_block
from inventory.models import Site
from tenants.models import PortalSettings

STORED = {"public_names": "my.example.com,portal.example.com", "le_email": "admin@example.com"}
NODE = {"SITE": "dc1", "ADDR": "10.81.101.25", "PEER_ADDR": "10.82.101.25", "STATUS_PORTS": "55587"}


class HAProxySettings(Base):
    def setUp(self):
        from inventory.models import DelegatedSubnet, Platform

        dc1 = Site.objects.get_or_create(code="dc1")[0]
        Site.objects.get_or_create(code="dc2")
        # owner 2026-11-15: the site's front end is its shared platform with a public FQDN; the public networks come
        # from its platform's ix segments (u01)
        Platform.objects.create(
            key="dc1.cloud",
            site=dc1,
            name="cloud",
            kind="shared",
            fe_vip="10.81.101.30",
            segments=["cloud-ix", "corpnet-ix"],
            public_url="https://one-dc1.example.com",  # owner 2026-11-15: HAProxy takes the name from the platform
        )
        DelegatedSubnet.objects.create(site=dc1, segment="cloud-ix", cidr="198.18.93.0/25", purpose="inet")
        DelegatedSubnet.objects.create(site=dc1, segment="corpnet-ix", cidr="10.81.104.0/22", purpose="corpnet")
        s = PortalSettings.get()  # u02: the portal's main name is the host of the Public URL (Settings → Provider)
        s.public_url = "https://my.example.com"
        s.save()

    def test_defaults_are_nothing_of_an_installation(self):
        from inventory.models import Platform

        PortalSettings.objects.update(public_url="")
        Platform.objects.update(public_url="")  # no platform with a public FQDN: no front end
        conf = haproxy.get(PortalSettings.get())
        # owner 2026-11-15: every service on 443 by path; no public ports are kept
        self.assertNotIn("ports", conf)
        self.assertEqual((conf["public_names"], conf["le_email"], conf["sites"]), ("", "", {}))
        with self.assertRaises(haproxy.Incomplete):  # nothing to build HAProxy from yet
            haproxy.args_for(conf, NODE)

    def test_the_front_ends_follow_the_inventory(self):
        self.assertEqual(  # the front end and its name follow the inventory
            haproxy.get()["sites"],
            {
                "dc1": {
                    "one_names": "one-dc1.example.com",
                    "one_addr": "10.81.101.30",
                    "platform": "dc1.cloud",
                }
            },
        )  # dc2: no shared platform, no front end through HAProxy
        from inventory.models import Platform

        Platform.objects.filter(key="dc1.cloud").update(public_url="")  # no public FQDN: not published
        self.assertEqual(haproxy.get()["sites"], {})

    def test_only_shared_platforms_are_published(self):
        """commit-010, owner 2026-11-15: "publish implemented only for shared platfrom - it is by design"; the first
        shared platform (by key) with a public FQDN is the site's front end."""
        from inventory.models import Platform

        dc1, dc2 = Site.objects.get(code="dc1"), Site.objects.get(code="dc2")
        Platform.objects.create(
            key="dc2.mgmt",
            site=dc2,
            name="mgmt",
            kind="mgmt",
            fe_vip="10.82.101.40",
            public_url="https://m.example.com",
        )
        Platform.objects.create(
            key="dc1.zz", site=dc1, name="zz", kind="shared", fe_vip="10.81.101.50", public_url="https://z.example.com"
        )
        sites = haproxy.get()["sites"]
        self.assertEqual(list(sites), ["dc1"])
        self.assertEqual(sites["dc1"]["platform"], "dc1.cloud")
        # commit-012: a platform saved without its VIP ("0.0.0.0") never displaces the published one
        Platform.objects.create(
            key="dc1.aa", site=dc1, name="aa", kind="shared", fe_vip="0.0.0.0", public_url="https://a.example.com"
        )
        self.assertEqual(haproxy.get()["sites"]["dc1"]["platform"], "dc1.cloud")

    def test_options_for_each_vm(self):
        s = PortalSettings.get()
        s.haproxy = STORED  # other names stored before: not on the certificate while it is locked (commit-196)
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
                "my.example.com",
                "--one-name",
                "one-dc1.example.com",
                "--one-addr",
                "10.81.101.30",
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

    def test_the_admin_portal_name_stays_off_until_unlocked(self):
        """Owner 2026-11-19 19:01: only the Customer Portal name until the owner unlocks the Admin Portal's."""
        s = PortalSettings.get()
        s.admin_url = "https://admin.example.com/"
        s.haproxy = STORED
        self.assertFalse(haproxy.ADMIN_NAME_ON_CERT)
        # commit-196: neither the admin name nor a former name stored, though both stay stored
        s.haproxy = {"public_names": "admin.example.com,portal.example.com", "le_email": "admin@example.com"}
        conf = haproxy.get(s)
        self.assertEqual((conf["public_names"], conf["other_names"]), ("my.example.com", ""))
        self.assertEqual(s.haproxy["public_names"], "admin.example.com,portal.example.com")

    @mock.patch.object(haproxy, "ADMIN_NAME_ON_CERT", True)
    def test_the_admin_portal_name_is_on_the_certificate(self):
        """Owner 2026-11-19 16:25, once unlocked: the certificate carries the Customer Portal and Admin Portal names."""
        s = PortalSettings.get()
        s.admin_url = "https://admin.example.com/"
        s.haproxy = STORED
        s.save()
        conf = haproxy.get(s)
        self.assertEqual(conf["public_names"], "my.example.com,admin.example.com,portal.example.com")
        self.assertEqual(conf["other_names"], "portal.example.com")
        s.admin_url = "https://my.example.com"  # the same name: once
        self.assertEqual(haproxy.get(s)["public_names"], "my.example.com,portal.example.com")
        s.admin_url = "https://10.81.101.25"  # not a host name: not on the certificate
        self.assertEqual(haproxy.get(s)["public_names"], "my.example.com,portal.example.com")
        s.haproxy = {"public_names": "admin.example.com", "le_email": ""}  # stored before: not repeated
        s.admin_url = "https://Admin.Example.com"
        self.assertEqual(haproxy.get(s)["public_names"], "my.example.com,admin.example.com")

    def test_settings_tab_and_the_command(self):
        """Owner 2026-11-15: "move lets encrypt email to platfrom page", "drop HA proxy block"."""
        sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(sa)
        r = self.client.get(reverse("portal_settings") + "?tab=portal")
        self.assertNotContains(r, 'name="other_names"')
        self.assertNotContains(r, 'name="le_email"')
        r = self.client.get(reverse("portal_settings") + "?tab=customer")
        self.assertContains(r, 'name="le_email"')
        self.assertNotContains(r, "one_addr_")
        r = self.client.post(reverse("portal_settings"), provider_block(le_email="x"), follow=True)
        self.assertContains(r, "not an e-mail address")  # nothing saved
        self.assertEqual(PortalSettings.get().haproxy, {})
        r = self.client.post(reverse("portal_settings"), provider_block(le_email="admin@example.com"))
        self.assertRedirects(r, reverse("portal_settings") + "?tab=customer", fetch_redirect_response=False)
        self.assertEqual(PortalSettings.get().haproxy, {"le_email": "admin@example.com"})
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
        self.client.post(reverse("portal_settings"), igw_block(igw_port_min="9000", igw_port_max="9100"))
        self.assertEqual(Tenant.igw_ports(), (50000, 59999))  # refused: below 10000
        self.client.post(reverse("portal_settings"), igw_block(igw_port_min="20000", igw_port_max="20099"))
        self.assertEqual(Tenant.igw_ports(), (20000, 20099))
        self.assertTrue(all(20000 <= Tenant.random_igw_port() <= 20099 for _i in range(50)))
        self.assertTrue(20000 <= _igw_port_of("tenant-x") <= 20099)
        self.assertContains(
            self.client.get(reverse("portal_settings") + "?tab=security"), 'name="igw_port_min" value="20000"'
        )


class PlatformPublicFqdnAndNodes(Base):
    """Owner 2026-11-15: the platform keeps its public FQDN only ("keep them in platform settings - public fqdn only");
    a front end "can and must have only 3 nodes always" - three fields, no rows to add or remove."""

    def setUp(self):
        Site.objects.get_or_create(code="dc1")
        self.client.force_login(self.staff("sa@corp.kz", is_super_admin=True))

    def save(self, **extra):
        from inventory.models import Platform

        body = {"new": "1", "site": "dc1", "name": "cloud", "kind": "shared", "fe_mode": "local", "fe_type": "vm"}
        body.update({"fe_node1": "one1", "fe_node2": "one2", "fe_node3": "one3", "public_fqdn": "One-DC1.example.com"})
        body.update({"fe_hostname": "one", "fe_vip": "192.0.2.30", "fe_ip1": "192.0.2.31", "fe_ip2": "192.0.2.32"})
        body.update({"fe_ip3": "192.0.2.33"})
        self.client.post(reverse("inv_platform_save"), {**body, **extra})
        return Platform.objects.filter(key="dc1.cloud").first()

    def test_vip_and_nodes_always_defined(self):
        """Owner 2026-11-15: "each platfrom always has vip and fe nodes defined - hostnames and ips" (commit-012)."""
        for missing in ("fe_vip", "fe_hostname", "fe_ip2", "fe_node3"):
            self.assertIsNone(self.save(**{missing: ""}), missing)
        self.assertIsNone(self.save(fe_vip="0.0.0.0"))
        self.assertIsNone(self.save(fe_ip3="192.0.2.30"))  # the VIP is not a node's IP
        p = self.save()
        self.assertEqual(
            (p.fe_vip, p.fe["hostname"], p.fe["node_ips"]),
            ("192.0.2.30", "one", ["192.0.2.31", "192.0.2.32", "192.0.2.33"]),
        )

    def test_three_nodes_and_the_fqdn(self):
        p = self.save()
        self.assertEqual(
            (p.fe["nodes"], p.public_url, p.public_fqdn),
            (["one1", "one2", "one3"], "https://one-dc1.example.com", "one-dc1.example.com"),
        )
        r = self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=platforms")
        for f in ("fe_node1", "fe_node2", "fe_node3", "public_fqdn"):
            self.assertContains(r, f'name="{f}"')
        for f in ("fe_nodes", "fireedge_port", "grpc_port", "xmlrpc_port", "vm_url", "onegate_port", "oneflow_port"):
            self.assertNotContains(r, f'name="{f}"')

    def test_fewer_or_repeated_nodes_are_refused(self):
        self.assertIsNone(self.save(fe_node3=""))
        self.assertIsNone(self.save(fe_node3="one1"))

    def test_not_a_name_is_refused(self):
        self.assertIsNone(self.save(public_fqdn="https://one.example.com:55516"))


class EndpointsFromAnyAddress(SimpleTestCase):
    """2026-11-17 (owner: "Any address, like XML-RPC"; "no any restriction for endpoints now must be"): OneFlow was
    published to the sites' public networks only, and the front ends' names are proxied by Cloudflare, which hides the
    client's address - so it answered 403 to everyone. No endpoint is restricted by address now; each asks for its own
    login. The rules are taken from deploy/install-lb.sh itself and, where HAProxy is installed, run."""

    def script(self):
        import os

        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "deploy", "install-lb.sh"), encoding="utf-8") as fh:
            return fh.read()

    def rules(self):
        import re
        import subprocess

        src = self.script()
        start = src.index("one_443_rules() {")
        end = src.index("one_443_backend() {")
        on443 = re.search(r"^on443\(\) .*$", src, re.M).group(0)
        script = "\n".join(
            [
                on443,
                src[start:end],
                "ONE_NAME=one-dc1.example.com ONE_ADDR=192.0.2.30",
                "FIREEDGE_PUBLISH=443 XMLRPC_PUBLISH=443 ONEFLOW_PUBLISH=443",
                "one_443_rules",
            ]
        )
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout

    def test_no_endpoint_is_restricted_by_address(self):
        src = self.script()
        for gone in ("--local-nets", "LOCAL_NETS", "deny_status 403", "tcp-request connection reject"):
            self.assertNotIn(gone, src)
        out = self.rules()
        self.assertIn("use_backend one_oneflow_443 if one_host one_flow", out)
        self.assertIn("use_backend one_xmlrpc_443 if one_host one_xmlrpc", out)
        self.assertNotIn(" src ", out)

    def test_haproxy_serves_oneflow_and_xmlrpc_to_any_address(self):
        import http.client
        import os
        import shutil
        import socket
        import subprocess
        import tempfile
        import threading
        import time
        from http.server import BaseHTTPRequestHandler, HTTPServer

        if not shutil.which("haproxy"):
            self.skipTest("HAProxy is not installed here")

        class Fe(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(self.path.encode())

            def log_message(self, *a):
                pass

        fe = HTTPServer(("127.0.0.1", 0), Fe)
        threading.Thread(target=fe.serve_forever, daemon=True).start()
        self.addCleanup(fe.shutdown)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        rules = self.rules().replace("\n", "\n    ")
        cfg = f"""global
    log stdout format raw local0 emerg
defaults
    mode http
    timeout connect 5s
    timeout client 5s
    timeout server 5s
frontend https
    bind 127.0.0.1:{port}
    {rules}
backend one_oneflow_443
    http-request replace-path ^/oneflow/?(.*)$ /\\1
    server fe 127.0.0.1:{fe.server_port}
backend one_xmlrpc_443
    server fe 127.0.0.1:{fe.server_port}
backend one_fireedge_443
    server fe 127.0.0.1:{fe.server_port}
backend one_none
    http-request deny deny_status 404
"""
        path = os.path.join(tempfile.mkdtemp(), "haproxy.cfg")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(cfg)
        check = subprocess.run(["haproxy", "-c", "-f", path], capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        proc = subprocess.Popen(["haproxy", "-db", "-f", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.terminate)
        got = {}
        for url in ("/oneflow/service_template", "/RPC2"):
            for _ in range(50):
                try:
                    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    c.request("GET", url, headers={"Host": "one-dc1.example.com"})
                    r = c.getresponse()
                    got[url] = (r.status, r.read().decode())
                    c.close()
                    break
                except ConnectionRefusedError:
                    time.sleep(0.1)
        self.assertEqual(got["/oneflow/service_template"], (200, "/service_template"))  # the prefix taken off
        self.assertEqual(got["/RPC2"], (200, "/RPC2"))
