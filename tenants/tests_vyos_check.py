"""Owner 2026-11-17: the VyOS check covers every provider router — route reflectors, the sites' egw/igw, the projects'
egw, cgw and L2 gateways — and never a project's igw (the client's after its deployment)."""

import xml.etree.ElementTree as ET
from unittest import mock

from cryptography.fernet import Fernet
from django.test import override_settings

from tenants.tests import DeploymentBase

VM = (
    "<VM><ID>{id}</ID><TEMPLATE><NIC><NETWORK>cloud-vyos_dc1</NETWORK><IP>10.0.20.{ip}</IP></NIC>"
    "<NIC><NETWORK>cloud-s2s_dc1</NETWORK><IP>10.0.30.{ip}</IP></NIC></TEMPLATE></VM>"
)


class FakeOne:
    def __init__(self, control):
        self.key = control.key

    def xml(self, method, vm_id, _decrypt):
        return ET.fromstring(VM.format(id=vm_id, ip=vm_id))


@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode())
class VyosCheck(DeploymentBase):
    def _channel(self, **kw):
        from tenants.models import L2Channel

        d = {"tenant": self.t, "site": "dc1", "vni": L2Channel.next_vni(), "state": "active", "vm_a": 7, "vm_b": 8}
        d.update(kw)
        return L2Channel.objects.create(**d)

    def test_the_project_routers_are_egw_cgw_and_l2_gateways_never_the_igw(self):
        from tenants import configcheck

        rows = [
            ("egw", "10.0.20.11", 55522, "sa", "K"),
            ("igw", "203.0.113.9", 22, "claugine", "S"),
            ("cgw", "10.0.20.12", 55522, "sa", "K"),
        ]
        self._channel(mode="tunnel", mgmt_platform="dc1.cloud")
        self._channel(mode="bridge", vm_b=None)
        self._channel(mode="tunnel", state="planned")  # not made yet
        self._channel(mode="tunnel", kind="internal")  # not made by the portal
        with (
            mock.patch.object(
                configcheck, "targets", side_effect=lambda t, igw=True: [r for r in rows if igw or r[0] != "igw"]
            ) as tg,
            mock.patch("tenants.deploy.One", FakeOne),
            mock.patch("inventory.credentials.account", return_value=("sa", {"key": "K"})),
        ):
            got = configcheck.check_targets(self.t)
        tg.assert_called_once_with(self.t, igw=False)
        names = [(g[0], g[1], g[2], g[3]) for g in got]
        self.assertNotIn("igw", [g[1] for g in got])
        vni = sorted(c.vni for c in self.t.l2_channels.filter(state="active", kind="external"))
        self.assertEqual(
            names,
            [
                ("egw", "egw", "10.0.20.11", 55522),
                ("cgw", "cgw", "10.0.20.12", 55522),
                (f"l2-a VNI {vni[0]}", "l2-a", "10.0.20.7", 55522),
                (f"l2-b VNI {vni[0]}", "l2-b", "10.0.20.8", 55522),
                (f"l2-br VNI {vni[1]}", "l2-br", "10.0.20.7", 55522),
            ],
        )

    def test_the_tunnel_keys_of_an_l2_gateway_are_not_compared(self):
        from tenants.configcheck import TUNNEL_KEYS, compare

        expected = (
            "set interfaces wireguard wg1001 private-key '«random key, made with the gateways»'\n"
            "set interfaces wireguard wg1001 peer src public-key '«random key, made with the gateways»'\n"
            "set interfaces wireguard wg1001 peer src preshared-key '«random password, made with the gateways»'\n"
            "set interfaces wireguard wg1001 port 51820\n"
        )
        actual = (
            "set interfaces wireguard wg1001 private-key 'cHJpdmF0ZQ=='\n"
            "set interfaces wireguard wg1001 peer src public-key 'cHVibGlj'\n"
            "set interfaces wireguard wg1001 peer src preshared-key 'cHNr'\n"
            "set interfaces wireguard wg1001 port 51820\n"
        )
        self.assertEqual(compare(expected, actual, ignore=TUNNEL_KEYS), ([], []))
        self.assertNotEqual(compare(expected, actual), ([], []))

    def test_the_timer_counts_core_and_project_routers_and_names_them(self):
        from accounts import timers
        from tenants import configcheck

        node = mock.Mock()
        node.name = "cloud-ix-rr1-dc1"
        self.t.one_group_id = 120
        self.t.save(update_fields=["one_group_id"])

        def run(check):
            check.results = {"egw": {"status": "ok"}, "l2-a VNI 1001": {"status": "unreachable"}}
            return check

        with (
            mock.patch.object(configcheck, "core_nodes", return_value=[node]),
            mock.patch.object(configcheck, "check_core", return_value={"status": "drift"}),
            mock.patch.object(configcheck, "check_targets", return_value=[("egw",)]),
            mock.patch.object(configcheck, "run", side_effect=run),
        ):
            ok, detail = timers._vyos()
        self.assertFalse(ok)
        self.assertEqual(
            detail,
            "3 routers: 1 differ from the template (cloud-ix-rr1-dc1), 1 not reachable (gpulab l2-a VNI 1001)",
        )
        label = str(next(d for k, _l, _m, d in timers.TIMERS if k == "vyos"))
        self.assertIn("route reflectors", label)
        self.assertIn("L2 gateways", label)
        self.assertNotIn("project igw", label)  # the label names what is checked: the sites' igw, no project's

    def test_an_edge_gateway_without_its_platforms_login_uses_the_projects(self):
        """commit-051: as when it was made (tenants.netops _sa_of)."""
        from inventory.models import Platform
        from tenants import configcheck

        Platform.objects.filter(key="dc1.cloud").update(sa="secrets.acme.cloud.admins.sa")
        self._channel(mode="tunnel", mgmt_platform="dc9.edge")  # no such platform here: no login of its own
        with (
            mock.patch.object(configcheck, "targets", return_value=[]),
            mock.patch("tenants.deploy.One", FakeOne),
            mock.patch("inventory.credentials.account", return_value=("opsuser", {"key": "K"})),
        ):
            got = configcheck.check_targets(self.t)
        self.assertEqual([(g[1], g[4], g[5]) for g in got], [("l2-a", "opsuser", "K"), ("l2-b", "opsuser", "K")])

    def test_finding_the_routers_never_looks_up_the_igw_login(self):
        """commit-054: the service key of the igw is not even read (looking it up may make one)."""
        from tenants import configcheck

        dep = mock.Mock(resources={"egw": "e", "egw_mgmt_ip": "10.0.20.11", "igw": "i", "igw_ip": "203.0.113.9"})
        with (
            mock.patch.object(type(self.t), "deployments", mock.PropertyMock(), create=True),
            mock.patch("inventory.credentials.service_key", side_effect=AssertionError("igw key looked up")) as sk,
            mock.patch("tenants.deploy.igw_type_of", return_value="vyos"),
        ):
            self.t.deployments.exclude.return_value.filter.return_value.first.return_value = dep
            got = configcheck.targets(self.t, igw=False)
            self.assertEqual([g[0] for g in got], ["egw"])
            sk.assert_not_called()
