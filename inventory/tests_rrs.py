"""Owner 2026-11-17: the route reflectors of a network are …241, …242 and …243 — the data and the stored templates are
moved there by inventory 0091 / 0092."""

import importlib
import io
from contextlib import redirect_stdout

from django.apps import apps
from django.test import TestCase

from inventory.models import ConfigTemplate, Network, NetworkRange, PeerGroup


def _migration(name):
    return importlib.import_module(f"inventory.migrations.{name}")


class RouteReflectorData(TestCase):
    def _net(self, name, subnet, rrs, hosts, start=240, length=4, kind="ix"):
        n = Network.objects.create(
            site="dc1",
            name=name,
            kind=kind,
            interface="bond0",
            vlan=10 + Network.objects.count(),
            subnet=subnet,
            prefix=24,
            rrs=rrs,
        )
        base = subnet.rsplit(".", 1)[0]
        r = NetworkRange.objects.create(network=n, name="rrs", start=f"{base}.{start}", length=length)
        PeerGroup.objects.create(network=n, name="rrs", hosts=hosts)
        return n, r

    def _forward(self):
        out = io.StringIO()
        with redirect_stdout(out):
            _migration("0091_rrs_explicit").forward(apps, None)
        return out.getvalue()

    def test_default_range_and_lists_become_the_three_route_reflectors(self):
        ix, _r = self._net(
            "cloud-ix",
            "192.0.2.0",
            ["192.0.2.240", "192.0.2.241", "192.0.2.242", "192.0.2.243"],
            ["192.0.2.240", "192.0.2.241", "192.0.2.242", "192.0.2.243"],
        )
        vx, _r = self._net(
            "cloud-vxlan",
            "198.51.100.0",
            ["198.51.100.241", "198.51.100.242", "198.51.100.243"],
            ["198.51.100.240/30"],
            kind="evpn",
        )
        other, _r = self._net("corpnet-ix", "203.0.113.0", ["203.0.113.5"], ["203.0.113.5"], start=4, length=2)
        out = self._forward()
        for n, base in ((ix, "192.0.2"), (vx, "198.51.100")):
            n.refresh_from_db()
            three = [f"{base}.241", f"{base}.242", f"{base}.243"]
            self.assertEqual(n.rrs, three)
            r = n.ranges.get(name="rrs")
            self.assertEqual((r.start, r.length), (f"{base}.241", 3))
            g = n.peer_groups.get(name="rrs")
            self.assertEqual((g.hosts, g.range_id), (three, r.pk))
        other.refresh_from_db()
        r = other.ranges.get(name="rrs")
        self.assertEqual((r.start, r.length, other.rrs), ("203.0.113.4", 2, ["203.0.113.5"]))  # not the default: left
        self.assertIn("corpnet-ix_dc1: range rrs is 203.0.113.4 + 2: left as it is", out)
        again = self._forward()  # twice: nothing more to do
        self.assertNotIn("→", again)
        self.assertNotIn("dropped", again)


class RouteReflectorTemplates(TestCase):
    def test_stored_templates_list_the_route_reflectors_one_by_one(self):
        from inventory import nf_templates as T

        m = _migration("0092_templates_rrs_explicit")
        bodies = {"egw": T.EGW, "cgw": T.CGW, "rr-inet": T.RR_IX, "rr-corpnet": T.RR_IX, "rr-vxlan": T.RR_VXLAN}
        ConfigTemplate.objects.filter(role__in=bodies).delete()
        for role, body in bodies.items():
            for o, n in m.CHANGES[role]:
                self.assertIn(n, body, role)
                body = body.replace(n, o, 1)  # the body as the portal wrote it before
            ConfigTemplate.objects.create(role=role, name=role, os="vyos", os_version="2026.03", body=body)
        edited = ConfigTemplate.objects.get(role="cgw")
        edited.body = edited.body.replace(
            "network-group CORPNET-RR network {{ ix.rr_net }}", "network-group CORPNET-RR network 10.0.0.0/8"
        )
        edited.save()
        out = io.StringIO()
        with redirect_stdout(out):
            m.forward(apps, None)
        for role, body in bodies.items():
            stored = ConfigTemplate.objects.get(role=role).body
            if role == "cgw":
                self.assertIn("network-group CORPNET-RR network 10.0.0.0/8", stored)  # edited by hand: left
            else:
                self.assertEqual(stored, body, role)
                self.assertNotIn("rr_net", stored)
                self.assertNotIn("rr_hosts", stored)
        self.assertIn("cgw template NOT changed", out.getvalue())
        m.backward(apps, None)
        self.assertIn("network-group CLOUD-RR network {{ ix.rr_net }}", ConfigTemplate.objects.get(role="egw").body)

    def test_new_networks_get_the_three_route_reflectors_as_the_range(self):
        from inventory.networks import DEFAULT_RANGES

        self.assertEqual(dict((n, (o, ln)) for n, o, ln in DEFAULT_RANGES["ix"])["rrs"], (241, 3))
        self.assertEqual(DEFAULT_RANGES["evpn"], [("rrs", 241, 3)])


class RouteReflectorAddresses(TestCase):
    def test_without_a_list_the_range_is_the_route_reflectors(self):
        from inventory.core_types import rr_ips
        from inventory.networks import ensure_default_ranges

        n = Network.objects.create(
            site="dc1", name="cloud-ix", kind="ix", interface="bond0", vlan=10, subnet="192.0.2.0", prefix=24
        )
        ensure_default_ranges(n)
        self.assertEqual(rr_ips(n), ["192.0.2.241", "192.0.2.242", "192.0.2.243"])
