"""core-008 (full review 2026-11-16): an L2 endpoint is one of the project's own networks - never another project's,
nor a provider network of the same name, whatever name the request carries."""

import types
import xml.etree.ElementTree as ET
from unittest import mock

from django.test import SimpleTestCase

from tenants import netops, network_views
from tenants.deploy import DeployError

POOL = """<VNET_POOL>
<VNET><ID>5</ID><NAME>lan1</NAME><GNAME>tenant_other</GNAME></VNET>
<VNET><ID>6</ID><NAME>lan1</NAME><GNAME>tenant_mine</GNAME></VNET>
<VNET><ID>7</ID><NAME>theirs</NAME><GNAME>tenant_other</GNAME></VNET>
<VNET><ID>8</ID><NAME>cloud-s2s_dc1</NAME><GNAME>oneadmin</GNAME></VNET>
</VNET_POOL>"""


class Execution(SimpleTestCase):
    def nc(self, group="tenant_mine"):
        one = types.SimpleNamespace(xml=lambda *a: ET.fromstring(POOL))
        return types.SimpleNamespace(one=one, tenant=types.SimpleNamespace(group_name=group))

    def test_own_network_found_even_after_a_namesake(self):
        v = netops.NetChange._tenant_vnet(self.nc(), "lan1")
        self.assertEqual(v.findtext("ID"), "6")  # not #5, another project's network of the same name

    def test_another_projects_or_a_provider_network_is_refused(self):
        for name in ("theirs", "cloud-s2s_dc1"):
            with self.subTest(name), self.assertRaisesRegex(DeployError, "not this project's network"):
                netops.NetChange._tenant_vnet(self.nc(), name)
        with self.assertRaisesRegex(DeployError, "not found"):
            netops.NetChange._tenant_vnet(self.nc(), "nowhere")
        self.assertIsNone(netops.NetChange._tenant_vnet(self.nc(), "theirs", must=False))

    def test_a_project_without_a_group_owns_nothing(self):
        with self.assertRaises(DeployError):
            netops.NetChange._tenant_vnet(self.nc(group=""), "lan1")


class Request(SimpleTestCase):
    def test_foreign_network(self):
        t = types.SimpleNamespace()
        with mock.patch.object(network_views, "tenant_networks", return_value=[{"name": "lan1"}]) as tn:
            self.assertFalse(network_views.foreign_network(t, "dc1", "lan1"))
            self.assertTrue(network_views.foreign_network(t, "dc1", "theirs"))
            self.assertFalse(network_views.foreign_network(t, "dc1", ""))
            self.assertTrue(tn.call_args.kwargs.get("fresh"))  # a name not in the cached list is read again
        with mock.patch.object(network_views, "tenant_networks", return_value=[]):  # not readable: checked on run
            self.assertFalse(network_views.foreign_network(t, "dc1", "theirs"))
