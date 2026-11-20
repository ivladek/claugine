"""The platform baseline (owner 2026-11-18, T25): Settings → Platforms keeps the base OS, the OpenNebula and the FRR
release; a platform whose front ends or hosts run another OS or OpenNebula release is out of baseline ("upgrade
required"), and while any is, no platform is created (and no host is deployed)."""

from unittest import mock

from django.test import TestCase
from django.urls import reverse

from accounts.tests import Base
from inventory import baseline, ops
from inventory.models import Platform, PlatformControl, Site
from tenants.models import PortalSettings

HOSTPOOL = (
    "<HOST_POOL><HOST><NAME>h1</NAME><TEMPLATE><VERSION><![CDATA[7.4.1]]></VERSION></TEMPLATE></HOST>"
    "<HOST><NAME>h2</NAME><TEMPLATE><VERSION><![CDATA[7.2.0]]></VERSION></TEMPLATE></HOST></HOST_POOL>"
)


class FakeOne:
    def __init__(self, version="7.4.1"):
        self.v = version

    def version(self):
        return self.v

    def frontends(self):
        return [{"id": "0", "name": "fe1", "addr": "192.0.2.31", "state": "leader"}]

    def _call(self, method, *a):
        assert method == "one.hostpool.info"
        return HOSTPOOL


def ssh_answers(answers):
    def run(addr, port, user, key, remote, site=None, timeout=60, **kw):
        a = answers[addr]
        if isinstance(a, Exception):
            raise a
        return a

    return run


def set_base(**kw):
    """As a save in Settings does: a change of the OS or OpenNebula raises the revision (commit-177)."""
    from django.db.models import F

    if "baseline_os" in kw or "baseline_one" in kw:
        kw["baseline_rev"] = F("baseline_rev") + 1
    PortalSettings.objects.filter(pk=PortalSettings.get().pk).update(**kw)


def os_out(ver="26.04", frr="10.6.2-0~ubuntu26.1", one="", onefe="7.4.1-1"):
    return f"os=ubuntu {ver}\nfrr={frr}\none={one}\nonefe={onefe}\n"


class Values(TestCase):
    def test_same_release(self):
        self.assertTrue(baseline.same("7.4.1-1", "7.4"))
        self.assertTrue(baseline.same("26.04", "26.04"))
        self.assertFalse(baseline.same("7.2.0", "7.4"))
        self.assertFalse(baseline.same("24.04", "26.04"))
        self.assertFalse(baseline.same("", "7.4"))

    def test_defaults(self):
        self.assertEqual(baseline.values(), {"os": "26.04", "one": "7.4", "frr": "10.6", "rev": 1})


class Check(TestCase):
    def setUp(self):
        self.site = Site.objects.create(code="dc1")
        self.p = Platform.objects.create(key="dc1.cloud", site=self.site, name="cloud", kind="shared", sa="dc1.sa")
        PlatformControl.objects.create(key="dc1.cloud")

    def check(self, answers, one="7.4.1"):
        with (
            mock.patch("adoption.collector.client_for", return_value=FakeOne(one)),
            mock.patch.object(baseline, "_login", return_value=("sa", "KEY")),
            mock.patch("inventory.sshexec.run", side_effect=ssh_answers(answers)),
        ):
            return baseline.check(self.p)

    def test_out_of_baseline(self):
        r = self.check({"192.0.2.31": os_out(), "h1": os_out(), "h2": os_out("24.04")})
        by = {n["name"]: n for n in r["nodes"]}
        self.assertEqual(by["fe1"]["state"], "ok")
        self.assertEqual(by["h1"]["state"], "ok")
        self.assertEqual(sorted(by["h2"]["wrong"]), ["one", "os"])
        self.assertEqual(r["out"], 1)
        self.p.refresh_from_db()
        self.assertEqual(baseline.state(self.p), "out")
        self.assertEqual(baseline.blocking(), [("dc1.cloud", "out", ["h2: os, one"])])
        with self.assertRaisesMessage(ops.EditError, "Upgrade required"):
            ops.platform_save(None, "dc1", "new", {"kind": "shared"}, new=True)

    def test_a_node_not_read_is_no_difference(self):
        r = self.check({"192.0.2.31": os_out(), "h1": OSError("timed out"), "h2": os_out()}, one="7.4.1")
        by = {n["name"]: n for n in r["nodes"]}
        self.assertEqual(by["h1"]["state"], "unread")
        self.assertIn("timed out", by["h1"]["error"])
        self.assertEqual(by["h2"]["state"], "out")  # its OpenNebula 7.2 from the pool, whatever ssh said
        self.p.refresh_from_db()
        self.assertEqual(baseline.state(self.p), "out")

    def test_in_baseline(self):
        pool = HOSTPOOL.replace("7.2.0", "7.4.1")
        with mock.patch.object(FakeOne, "_call", return_value=pool):
            r = self.check({"192.0.2.31": os_out(), "h1": os_out(), "h2": os_out()})
        self.assertEqual((r["out"], r["unread"]), (0, 0))
        self.p.refresh_from_db()
        self.assertEqual(baseline.state(self.p), "ok")
        self.assertEqual(baseline.problem(), "")

    def test_a_new_os_or_opennebula_blocks_until_checked_and_frr_alone_does_not(self):
        """commit-172, commit-174."""
        self.check({"192.0.2.31": os_out(), "h1": os_out(), "h2": os_out("24.04")})
        set_base(baseline_frr="10.8")  # FRR only: the result still applies
        self.p.refresh_from_db()
        self.assertEqual(baseline.state(self.p), "out")
        self.assertEqual(baseline.blocking(), [("dc1.cloud", "out", ["h2: os, one"])])
        set_base(baseline_one="7.2")
        self.p.refresh_from_db()
        self.assertEqual(baseline.state(self.p), "stale")
        self.assertEqual(baseline.blocking(), [("dc1.cloud", "stale", [])])
        self.assertIn("Not checked yet against this baseline: dc1.cloud", baseline.problem())

    def test_each_front_end_reads_its_own_version(self):
        """commit-173: the endpoint answers 7.4.1, the front end's own package is 7.2 - it is out; none - unread."""
        r = self.check({"192.0.2.31": os_out(onefe="7.2.0-1"), "h1": os_out(), "h2": os_out()}, one="7.4.1")
        fe = next(n for n in r["nodes"] if n["kind"] == "front end")
        self.assertEqual((fe["one"], fe["wrong"]), ("7.2.0-1", ["one"]))
        r = self.check({"192.0.2.31": OSError("timed out"), "h1": os_out(), "h2": os_out()}, one="7.4.1")
        fe = next(n for n in r["nodes"] if n["kind"] == "front end")
        self.assertEqual((fe["one"], fe["state"]), ("7.2.0-1", "out"))  # kept from the read before (commit-171)

    def test_a_confirmed_out_stays_until_a_read_clears_it(self):
        """commit-171."""
        self.check({"192.0.2.31": os_out(), "h1": os_out("24.04"), "h2": os_out()})
        r = self.check({"192.0.2.31": os_out(), "h1": OSError("timed out"), "h2": os_out()})
        h1 = next(n for n in r["nodes"] if n["name"] == "h1")
        self.assertEqual((h1["state"], h1["wrong"], h1.get("kept")), ("out", ["os"], True))
        self.assertTrue(baseline.blocking())
        with mock.patch("adoption.collector.client_for", side_effect=baseline.ET.ParseError("bad answer")):
            r = baseline.check(self.p)
        self.assertEqual((r["error"], r["out"]), ("bad answer", 2))  # the endpoint unread: the nodes before kept
        self.assertTrue(baseline.blocking())
        pool = HOSTPOOL.replace("7.2.0", "7.4.1")
        with mock.patch.object(FakeOne, "_call", return_value=pool):
            r = self.check({"192.0.2.31": os_out(), "h1": os_out(), "h2": os_out()})
        self.assertEqual(r["out"], 0)  # read again and in baseline: cleared
        self.assertEqual(baseline.blocking(), [])

    def test_a_node_opennebula_no_longer_lists_is_dropped(self):
        self.check({"192.0.2.31": os_out(), "h1": os_out("24.04"), "h2": os_out()})
        pool = "<HOST_POOL><HOST><NAME>h2</NAME><TEMPLATE><VERSION>7.4.1</VERSION></TEMPLATE></HOST></HOST_POOL>"
        with mock.patch.object(FakeOne, "_call", return_value=pool):
            r = self.check({"192.0.2.31": os_out(), "h2": os_out()})
        self.assertNotIn("h1", [n["name"] for n in r["nodes"]])
        self.assertEqual(r["out"], 0)


class Revisions(TestCase):
    """commit-171 (again): a failed or partial check against changed values keeps the platform blocked."""

    setUp = Check.setUp
    check = Check.check

    def test_a_failed_first_check_against_new_values_keeps_it_blocked(self):
        pool = HOSTPOOL.replace("7.2.0", "7.4.1")
        with mock.patch.object(FakeOne, "_call", return_value=pool):
            self.check({"192.0.2.31": os_out(), "h1": os_out(), "h2": os_out()})
        self.assertEqual(baseline.blocking(), [])  # complete and in baseline
        set_base(baseline_os="28.04")
        with mock.patch("adoption.collector.client_for", side_effect=baseline.ET.ParseError("no answer")):
            baseline.check(self.p)
        self.p.refresh_from_db()
        self.assertEqual(baseline.state(self.p), "stale")
        self.assertEqual([k for k, kind, _w in baseline.blocking()], ["dc1.cloud"])

    def test_a_partial_check_against_new_values_keeps_it_blocked_and_a_complete_one_clears(self):
        pool = HOSTPOOL.replace("7.2.0", "7.4.1")
        with mock.patch.object(FakeOne, "_call", return_value=pool):
            self.check({"192.0.2.31": os_out(), "h1": os_out(), "h2": os_out()})
            set_base(baseline_frr="10.8")
            set_base(baseline_os="28.04")
            self.check({"192.0.2.31": os_out("28.04"), "h1": OSError("down"), "h2": os_out("28.04")})
            self.p.refresh_from_db()
            self.assertEqual(baseline.state(self.p), "stale")
            self.check({"192.0.2.31": os_out("28.04"), "h1": os_out("28.04"), "h2": os_out("28.04")})
        self.p.refresh_from_db()
        self.assertEqual(baseline.state(self.p), "ok")
        self.assertEqual(baseline.blocking(), [])
        with mock.patch.object(FakeOne, "_call", return_value=pool):  # same values, a node down: no block (as before)
            self.check({"192.0.2.31": os_out("28.04"), "h1": OSError("down"), "h2": os_out("28.04")})
        self.p.refresh_from_db()
        self.assertEqual((baseline.state(self.p), baseline.blocking()), ("unread", []))


class Revision(Base):
    """commit-177: back to earlier values is a new revision - blocked until a complete check of it."""

    def setUp(self):
        Check.setUp(self)
        self.sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(self.sa)

    check = Check.check

    def save(self, os_, one):
        with mock.patch("accounts.timers.start"):
            self.client.post(
                reverse("portal_settings"),
                {
                    "tab": "platforms",
                    "section": "baseline",
                    "baseline_os": os_,
                    "baseline_one": one,
                    "baseline_frr": "10.6",
                },
            )

    def test_back_to_earlier_values_is_checked_again(self):
        pool = HOSTPOOL.replace("7.2.0", "7.4.1")
        with mock.patch.object(FakeOne, "_call", return_value=pool):
            self.check({"192.0.2.31": os_out(), "h1": os_out(), "h2": os_out()})  # A: complete
            self.assertEqual(baseline.blocking(), [])
            self.save("28.04", "7.4")  # B
            self.check({"192.0.2.31": os_out(), "h1": OSError("down"), "h2": os_out()})  # incomplete
            self.save("26.04", "7.4")  # back to A
            self.assertEqual(PortalSettings.get().baseline_rev, 3)
            self.p.refresh_from_db()
            self.assertEqual(baseline.state(self.p), "stale")
            self.check({"192.0.2.31": os_out(), "h1": OSError("down"), "h2": os_out()})  # incomplete again
            self.p.refresh_from_db()
            self.assertEqual(baseline.state(self.p), "stale")
            self.assertTrue(baseline.blocking())
            self.check({"192.0.2.31": os_out(), "h1": os_out(), "h2": os_out()})  # complete
        self.p.refresh_from_db()
        self.assertEqual((baseline.state(self.p), baseline.blocking()), ("ok", []))

    def test_a_whole_row_save_from_an_older_copy_keeps_the_baseline(self):
        """commit-178: another section's save of its old copy never puts back an older baseline or revision."""
        stale = PortalSettings.get()  # loaded before the baseline save
        self.save("28.04", "7.4")
        stale.client_mail = not stale.client_mail
        stale.save()
        st = PortalSettings.get()
        self.assertEqual((st.baseline_os, st.baseline_rev, st.client_mail), ("28.04", 2, stale.client_mail))
        r = self.client.post(
            reverse("portal_settings"), {"tab": "customer", "section": "client_mail", "client_mail": "on"}
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual((PortalSettings.get().baseline_os, PortalSettings.get().baseline_rev), ("28.04", 2))

    def test_an_frr_save_compares_with_the_row_not_its_copy(self):
        """commit-178/179: the FRR request loads the settings (26.04, revision 1), another save then raises the OS to
        28.04 (revision 2), and the FRR request goes on with its form's 26.04 - compared with the locked row, that is a
        change of the OS back to 26.04, so the revision is raised again (3); compared with the request's own copy it
        would look like an FRR-only change and leave 26.04 at revision 2, a revision already counted as checked."""
        stale = PortalSettings.get()  # what the FRR request loaded
        set_base(baseline_os="28.04")  # the save in between: revision 2
        self.assertEqual(PortalSettings.get().baseline_rev, 2)
        with mock.patch("accounts.timers.start"), mock.patch.object(PortalSettings, "get", return_value=stale):
            self.client.post(
                reverse("portal_settings"),
                {
                    "tab": "platforms",
                    "section": "baseline",
                    "baseline_os": "26.04",
                    "baseline_one": "7.4",
                    "baseline_frr": "10.8",
                },
            )
        st = PortalSettings.objects.get(pk=stale.pk)
        self.assertEqual((st.baseline_os, st.baseline_frr, st.baseline_rev), ("26.04", "10.8", 3))

    def test_frr_alone_is_no_new_revision(self):
        self.save("26.04", "7.4")
        self.assertEqual(PortalSettings.get().baseline_rev, 1)
        with mock.patch("accounts.timers.start"):
            self.client.post(
                reverse("portal_settings"),
                {
                    "tab": "platforms",
                    "section": "baseline",
                    "baseline_os": "26.04",
                    "baseline_one": "7.4",
                    "baseline_frr": "10.8",
                },
            )
        self.assertEqual((PortalSettings.get().baseline_rev, PortalSettings.get().baseline_frr), (1, "10.8"))


class SettingsTab(Base):
    def setUp(self):
        self.sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(self.sa)

    def test_tab_and_save(self):
        url = reverse("portal_settings")
        r = self.client.get(url + "?tab=platforms")
        self.assertContains(r, 'name="baseline_os" value="26.04"')
        with mock.patch("accounts.timers.start") as start:
            r = self.client.post(
                url,
                {
                    "tab": "platforms",
                    "section": "baseline",
                    "baseline_os": "28.04",
                    "baseline_one": "7.6",
                    "baseline_frr": "10.6",
                },
            )
        self.assertEqual(r.status_code, 302)
        s = PortalSettings.get()
        self.assertEqual((s.baseline_os, s.baseline_one), ("28.04", "7.6"))
        start.assert_called_once()

    def test_wrong_values_refused(self):
        r = self.client.post(
            reverse("portal_settings"),
            {
                "tab": "platforms",
                "section": "baseline",
                "baseline_os": "26.10",
                "baseline_one": "7",
                "baseline_frr": "10.6",
            },
            follow=True,
        )
        self.assertContains(r, "an Ubuntu LTS release")
        self.assertEqual(PortalSettings.get().baseline_os, "26.04")

    def test_versions_fit_their_fields(self):
        """commit-175."""
        r = self.client.post(
            reverse("portal_settings"),
            {
                "tab": "platforms",
                "section": "baseline",
                "baseline_os": "26.04",
                "baseline_one": "12345678.1",
                "baseline_frr": "10.6",
            },
            follow=True,
        )
        self.assertContains(r, "a release as major.minor")
        self.assertEqual(PortalSettings.get().baseline_one, "7.4")


class TimerState(TestCase):
    """commit-172: "again" is never lost to a concurrent write, and makes the timer due."""

    def test_again_makes_it_due(self):
        from accounts import timers

        timers._save("baseline", state="ok", last_at="2026-11-19T00:00:00+00:00", again=True)
        self.assertTrue(timers.due("baseline"))

    def test_the_baseline_timer_runs_again_when_the_values_changed(self):
        from accounts import timers

        seen = []

        def check_all(base):
            seen.append(dict(base))
            if len(seen) == 1:
                PortalSettings.objects.filter(pk=PortalSettings.get().pk).update(baseline_one="7.6")
            return True, "ok"

        with mock.patch.object(baseline, "check_all", side_effect=check_all):
            timers._baseline()
        self.assertEqual([b["one"] for b in seen], ["7.4", "7.6"])


class TimerAgain(TestCase):
    """commit-172: asked while it runs, the timer runs once more when it ends."""

    def test_again(self):
        from accounts import timers

        calls = []

        def work():
            calls.append(1)
            if len(calls) == 1:
                self.assertIsNone(timers.start("baseline"))  # a save while it runs
            return True, "ok"

        with mock.patch.dict(timers.FUNCS, {"baseline": work}):
            timers.run("baseline")
        self.assertEqual(len(calls), 2)
        self.assertFalse(timers.state("baseline").get("again"))
