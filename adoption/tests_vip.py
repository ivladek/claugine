"""The platform's VIP is up or not (owner 2026-11-15: "vip availability is binary / VIP is not accessible only means
VIP IP address does not up on a leader"; commit-005): any reply through it, an API error included, means it is up;
only a failed connection means it is down. Errors of single requests stay a separate warning."""

import xmlrpc.client
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from adoption import collector
from adoption.models import PlatformStats


class Proxy:
    """A stand-in for the XML-RPC proxy: every call raises `exc`, or returns `result`."""

    def __init__(self, exc=None, result=(True, "<X/>")):
        self.exc, self.result = exc, result

    def __getattr__(self, name):
        return self

    def __call__(self, *args):
        if self.exc:
            raise self.exc
        return self.result


def client(proxy):
    one = collector.OneClient("http://192.0.2.10:2633/RPC2", "oneadmin:x")
    one.proxy = proxy
    return one


class Answered(TestCase):
    def test_a_failed_connection_is_no_answer(self):
        one = client(Proxy(ConnectionRefusedError(111, "Connection refused")))
        with self.assertRaises(collector.OneError):
            one.version()
        self.assertFalse(one.answered)

    def test_an_api_error_is_an_answer(self):
        one = client(Proxy(xmlrpc.client.Fault(1, "not authorized")))
        with self.assertRaises(collector.OneError):
            one.version()
        self.assertTrue(one.answered)
        one = client(Proxy(result=(False, "[one.datastorepool.info] error")))
        with self.assertRaises(collector.OneError):
            one.version()
        self.assertTrue(one.answered)


class Fake:
    """collect_stats's view of a platform: the front ends answer, the datastores fail."""

    answered = True

    def vms(self):
        return []

    def host_states(self):
        return []

    def address_ranges(self):
        return []

    def datastore_usage(self):
        raise collector.OneError("one.datastorepool.info: fault")

    def clusters(self):
        return {}

    def frontends(self):
        return [{"id": "0", "name": "fe1", "addr": "192.0.2.11", "state": "leader"}]


def setup_platform():
    from inventory.models import Platform, PlatformControl, Site

    site = Site.objects.create(code="dc1")
    Platform.objects.create(key="dc1.cloud", site=site, name="cloud", kind="shared", fe_vip="192.0.2.10")
    return PlatformControl.objects.create(key="dc1.cloud")


def vip():
    from accounts.reports import platforms

    row = {r["platform"]: r for r in platforms()}["dc1.cloud"]
    return row["vip"]["state"], row["error"]


def down():
    """A platform whose VIP is not up: every connection is refused."""
    one = mock.Mock(answered=False)
    one.users.side_effect = one.groups.side_effect = collector.OneError("one.grouppool.info: [Errno 111] refused")
    return one


def auth_fault():
    """The VIP answers, with an API error (wrong credentials)."""
    one = mock.Mock(answered=True)
    one.users.side_effect = one.groups.side_effect = collector.OneError("one.grouppool.info: not authorized")
    return one


class VipState(TestCase):
    def collect(self, control, one):
        with mock.patch.object(collector, "client_for", return_value=one), self.assertRaises(collector.OneError):
            collector.collect(control)
        control.refresh_from_db()

    def test_one_failed_request_leaves_the_vip_up(self):
        control = setup_platform()
        collector.collect_stats(Fake(), control, timezone.now())
        control.refresh_from_db()
        self.assertTrue(control.vip_answered)
        st = PlatformStats.objects.get(platform_key="dc1.cloud")
        self.assertIn("datastorepool", st.error)  # kept, as a warning
        state, error = vip()
        self.assertEqual(state, "up")
        self.assertIn("datastorepool", error)

    def test_a_first_attempt_that_never_connects(self):
        """commit-006: no numbers were ever collected, and the VIP is still recorded as down."""
        control = setup_platform()
        self.collect(control, down())
        self.assertFalse(control.vip_answered)
        self.assertFalse(PlatformStats.objects.exists())  # no row of numbers is made up
        self.assertEqual(vip()[0], "unreachable")

    def test_down_then_an_api_error_is_up_again(self):
        """commit-007: an attempt that stops at an API error still records that the VIP answered."""
        control = setup_platform()
        self.collect(control, down())
        self.assertEqual(vip()[0], "unreachable")
        self.collect(control, auth_fault())
        self.assertTrue(control.vip_answered)
        self.assertEqual(vip()[0], "up")

    def test_the_last_numbers_stay_when_the_vip_goes_down(self):
        control = setup_platform()
        collector.collect_stats(Fake(), control, timezone.now())
        self.collect(control, down())
        self.assertEqual(vip()[0], "unreachable")
        self.assertEqual(PlatformStats.objects.get(platform_key="dc1.cloud").frontends[0]["name"], "fe1")

    def test_a_refresh_stores_empty_node_ips(self):
        """Owner 2026-11-15: "prod must store fe nodes ip" - a refresh fills an empty node IP, never a stored one."""
        from inventory.models import Platform

        control = setup_platform()
        Platform.objects.filter(key="dc1.cloud").update(fe={"nodes": ["fe1", "fe2", "fe3"]})
        collector.collect_stats(Fake(), control, timezone.now())
        self.assertEqual(Platform.objects.get(key="dc1.cloud").fe["node_ips"], ["192.0.2.11", "", ""])
        Platform.objects.filter(key="dc1.cloud").update(fe={"nodes": ["fe1"], "node_ips": ["192.0.2.50"]})
        collector.collect_stats(Fake(), control, timezone.now())
        self.assertEqual(Platform.objects.get(key="dc1.cloud").fe["node_ips"], ["192.0.2.50"])
