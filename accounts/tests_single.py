"""owner 2026-11-09 (editions, Phase 4 step 2): the public one-node role layer, and how the Enterprise package is
found. Both are inert until step 3 moves the call sites, so these tests are the only thing exercising them."""

import importlib
import pathlib
import sys
import tempfile
from unittest import mock

from django.test import TestCase
from django.test.client import RequestFactory

from accounts import edition, single, thisnode


class NodeEnv:
    """A node.env on disk, with the cache cleared around it."""

    def __init__(self, **values):
        self.values = values

    def __enter__(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.dir.name) / "node.env"
        self.path.write_text("".join(f"{k}={v}\n" for k, v in self.values.items()), encoding="utf-8")
        self._old = getattr(thisnode, "_test_path", None)
        self._patch = __import__("unittest.mock", fromlist=["mock"]).patch.object(
            thisnode, "path", lambda: str(self.path)
        )
        self._patch.start()
        thisnode.forget()
        return self

    def __exit__(self, *a):
        self._patch.stop()
        thisnode.forget()
        self.dir.cleanup()


class Role(TestCase):
    def test_no_node_env_is_a_single_active_portal(self):
        with NodeEnv():
            self.assertEqual(single.role(), "active")
            self.assertTrue(single.is_active())

    def test_maintenance_is_kept(self):
        """A one-node operator can still take the portal down deliberately."""
        with NodeEnv(ROLE="maintenance"):
            self.assertEqual(single.role(), "maintenance")
            self.assertFalse(single.is_active())

    def test_a_role_this_build_does_not_know_on_a_lone_node_still_serves(self):
        """Restored from an Enterprise backup, node.env says ROLE=standby but names no peer. The only portal there
        is must not go dark because of a word it does not recognise."""
        for unknown in ("standby", "new", "junk", ""):
            with NodeEnv(ROLE=unknown):
                self.assertEqual(single.role(), "active", unknown)
                self.assertTrue(single.is_active(), unknown)

    def test_a_role_it_does_not_know_WITH_a_peer_serves_nothing(self):
        """**The 2026-11-09 incident, as a test.** A build without ee/ landed on the standby of a pair; this
        function read ROLE=standby, did not know the word, and answered "active". Both nodes then answered /active
        with 200, and HAProxy had two active nodes — one of them with a read-only database.

        With a peer named, an unrecognised role means this build cannot tell which half of the pair it is, and the
        only safe answer is to serve nothing."""
        for unknown in ("standby", "new", "junk"):
            with NodeEnv(ROLE=unknown, PEER_ADDR="10.0.1.25", PEER_SITE="dc2"):
                self.assertEqual(single.role(), "maintenance", unknown)
                self.assertFalse(single.is_active(), unknown)

    def test_the_incident_end_to_end_the_standby_must_not_claim_active(self):
        """What HAProxy polls, in exactly production's state: ee/ absent, node.env says standby, peer named."""
        from django.test.client import RequestFactory

        rf = RequestFactory()
        with NodeEnv(SITE="dc2", ADDR="10.0.1.25", PEER_ADDR="10.0.0.25", ROLE="standby"):
            self.assertEqual(
                single.status_view(rf.get("/active"), "active").status_code,
                503,
                "a standby running a build that does not know the word must NOT answer /active with 200",
            )
            self.assertEqual(single.status_view(rf.get("/maintenance"), "maintenance").status_code, 200)

    def test_there_is_never_a_peer(self):
        with NodeEnv(PEER_ADDR="10.0.1.25", PEER_SITE="dc2"):
            self.assertFalse(single.has_peer(), "even with a peer in node.env: this module is the no-pair answer")

    def test_writes_are_forbidden_only_off_active(self):
        with NodeEnv(ROLE="maintenance"):
            with self.assertRaises(single.ReadOnlyHere):
                single.forbid_writes("a platform")
        with NodeEnv():
            single.forbid_writes("a platform")  # active: allowed, no exception


class StatusEndpoint(TestCase):
    """What HAProxy polls. A single node must still answer, or HAProxy takes the only portal out of the pool."""

    def setUp(self):
        self.rf = RequestFactory()

    def test_active_answers_200_and_everything_else_503(self):
        with NodeEnv():
            self.assertEqual(single.status_view(self.rf.get("/active"), "active").status_code, 200)
            self.assertEqual(single.status_view(self.rf.get("/maintenance"), "maintenance").status_code, 503)

    def test_the_answer_is_never_cached(self):
        with NodeEnv():
            self.assertEqual(single.status_view(self.rf.get("/active"), "active")["Cache-Control"], "no-store")

    def test_only_the_roles_a_single_node_has(self):
        self.assertEqual(single.ROLES, ("active", "maintenance"))
        self.assertNotIn("/standby", single.STATUS_PATHS)
        self.assertNotIn("/new", single.STATUS_PATHS)


class Middleware(TestCase):
    def setUp(self):
        self.rf = RequestFactory()
        self.mw = single.RoleMiddleware(lambda r: __import__("django.http", fromlist=["h"]).HttpResponse("served"))

    def test_maintenance_serves_nothing_but_the_status_page(self):
        with NodeEnv(ROLE="maintenance"):
            self.assertEqual(self.mw(self.rf.get("/active")).content, b"served")  # the status path still answers
            self.assertEqual(self.mw(self.rf.get("/dashboard/")).status_code, 503)

    def test_active_serves_everything(self):
        with NodeEnv():
            self.assertEqual(self.mw(self.rf.get("/dashboard/")).content, b"served")


class FindingTheEnterprisePackage(TestCase):
    """The two absences must not be confused: no `ee/` at all is normal; a broken `ee/` is a broken build."""

    def _with_ee(self, body):
        d = tempfile.TemporaryDirectory()
        pkg = pathlib.Path(d.name) / "ee"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "probe.py").write_text(body, encoding="utf-8")
        sys.path.insert(0, d.name)
        importlib.invalidate_caches()
        return d

    def setUp(self):
        # The dev tree has a real ee/ (and step 3, 2026-11-11, gave it modules the agent holds on to): set it aside
        # so a fake one can be imported, and put it back after - a fresh copy would split the agent from its tests.
        self._saved = {n: m for n, m in sys.modules.items() if n == "ee" or n.startswith("ee.")}
        for name in self._saved:
            del sys.modules[name]

    def tearDown(self):
        for name in [n for n in sys.modules if n == "ee" or n.startswith("ee.")]:
            del sys.modules[name]
        sys.modules.update(self._saved)
        importlib.invalidate_caches()

    def test_no_package_is_a_normal_state(self):
        """The public build has no ee/ at all. The dev tree does have one, so absence is simulated — which is the
        only way to test the public build's shape from here until the snapshot is built."""
        import importlib.util

        with mock.patch.object(importlib.util, "find_spec", return_value=None):
            self.assertIsNone(edition.ee("ha"), "no ee/ package must return None, not raise")

    def test_a_present_package_is_imported(self):
        d = self._with_ee("MARK = 'enterprise'\n")
        try:
            self.assertEqual(edition.ee("probe").MARK, "enterprise")
        finally:
            sys.path.remove(d.name)
            d.cleanup()

    def test_a_broken_enterprise_module_raises_rather_than_degrading(self):
        """Swallowing this would turn a typo in ee/ into an Enterprise installation quietly losing its second node,
        with nothing anywhere saying so."""
        d = self._with_ee("import a_module_that_is_not_installed\n")
        try:
            with self.assertRaises(ImportError):
                edition.ee("probe")
        finally:
            sys.path.remove(d.name)
            d.cleanup()
