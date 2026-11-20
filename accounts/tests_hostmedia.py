"""The hosts' installation media (owner 2026-11-18, T25 §A4): the newest point release of the base OS from a signed
SHA256SUMS, downloaded, checked and rebuilt only when it is new, the previous one kept; the answers ISOs in seed/;
/hostmedia/ in HAProxy from the site's iLO networks only."""

import hashlib
import os
import shutil
import tempfile
from unittest import mock

from django.test import TestCase, override_settings

from accounts import haproxy, hostmedia
from inventory.models import Host, Site


def sums(*lines):
    return ("\n".join(lines) + "\n").encode()


SHA_A, SHA_B, SHA_C = "a" * 64, "b" * 64, "c" * 64
SUMS = sums(
    f"{SHA_A} *ubuntu-26.04-live-server-amd64.iso",
    f"{SHA_B} *ubuntu-26.04.1-live-server-amd64.iso",
    f"{'d' * 64} *ubuntu-26.04.1-desktop-amd64.iso",
)


def getter(content):
    return lambda url, timeout=60: content if url.endswith("SHA256SUMS") else b"SIG"


class Newest(TestCase):
    def test_the_newest_point_release_of_the_series(self):
        with mock.patch.object(hostmedia.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(
                hostmedia.newest("26.04", get=getter(SUMS)), ("26.04.1", "ubuntu-26.04.1-live-server-amd64.iso", SHA_B)
            )
        self.assertEqual(run.call_args.args[0][:3], ["gpgv", "--keyring", hostmedia.KEYRING])
        self.assertTrue(os.path.exists(hostmedia.KEYRING))

    def test_a_bad_signature_stops_it(self):
        bad = mock.Mock(returncode=1, stderr="BAD signature")
        with mock.patch.object(hostmedia.subprocess, "run", return_value=bad):
            with self.assertRaisesMessage(hostmedia.MediaError, "signature"):
                hostmedia.newest("26.04", get=getter(SUMS))


class Check(TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.ov = override_settings(HOSTMEDIA_DIR=self.dir)
        self.ov.enable()
        self.gpg = mock.patch.object(hostmedia.subprocess, "run", return_value=mock.Mock(returncode=0))
        self.gpg.start()
        self.downloads = []

    def tearDown(self):
        self.gpg.stop()
        self.ov.disable()
        shutil.rmtree(self.dir, ignore_errors=True)

    def download(self, url, path, want):
        self.downloads.append(url)
        with open(path, "wb") as fh:
            fh.write(want.encode())

    def build(self, src, dst):
        shutil.copy(src, dst)

    def run_check(self, content=SUMS):
        return hostmedia.check(
            log=lambda m: None, get=getter(content), download=self.download, build=self.build, replicate=False
        )

    def test_a_damaged_iso_is_made_again(self):
        """commit-156."""
        self.run_check()
        cur = hostmedia.current()
        with open(os.path.join(self.dir, cur["file"]), "wb") as fh:
            fh.write(b"damaged")
        ok, detail = self.run_check()
        self.assertTrue(ok, detail)
        self.assertIn("rebuilt", detail)
        self.assertEqual(len(self.downloads), 2)
        self.assertTrue(hostmedia.has(hostmedia.current()))

    def test_other_bytes_of_the_same_source_are_another_name(self):
        """commit-160."""
        self.run_check()
        first = hostmedia.current()
        with open(os.path.join(self.dir, first["file"]), "wb") as fh:
            fh.write(b"damaged")
        self.build = lambda src, dst: open(dst, "wb").write(b"rebuilt, other bytes")
        self.run_check()
        again = hostmedia.current()
        self.assertNotEqual(again["file"], first["file"])
        self.assertEqual(again["file"][-16:-4], hashlib.sha256(b"rebuilt, other bytes").hexdigest()[:12])

    def test_new_then_current_then_newer(self):
        ok, detail = self.run_check()
        self.assertTrue(ok, detail)
        cur = hostmedia.current()
        self.assertEqual(
            (cur["version"], cur["file"]),
            (
                "26.04.1",
                f"ubuntu-26.04.1-live-server-amd64-claugine-{hashlib.sha256(SHA_B.encode()).hexdigest()[:12]}.iso",
            ),
        )
        self.assertEqual(cur["sha256"], hashlib.sha256(SHA_B.encode()).hexdigest())
        self.assertEqual(self.downloads, ["https://releases.ubuntu.com/26.04/ubuntu-26.04.1-live-server-amd64.iso"])
        ok, detail = self.run_check()
        self.assertEqual((ok, detail, len(self.downloads)), (True, "Ubuntu 26.04.1: current", 1))
        ok, _d = self.run_check(SUMS + sums(f"{SHA_C} *ubuntu-26.04.2-live-server-amd64.iso"))
        st = hostmedia.state()
        self.assertEqual((st["current"]["version"], st["previous"]["version"]), ("26.04.2", "26.04.1"))
        self.run_check(SUMS + sums(f"{'e' * 64} *ubuntu-26.04.3-live-server-amd64.iso"))
        isos = sorted(f for f in os.listdir(self.dir) if f.endswith(".iso"))
        self.assertEqual(
            isos,
            [
                f"ubuntu-26.04.2-live-server-amd64-claugine-{hashlib.sha256(SHA_C.encode()).hexdigest()[:12]}.iso",
                f"ubuntu-26.04.3-live-server-amd64-claugine-{hashlib.sha256(('e' * 64).encode()).hexdigest()[:12]}.iso",
            ],
        )

    def test_a_respin_is_another_file_and_pinned_isos_are_kept(self):
        """commit-142: the same version re-released is a new name; an ISO an unfinished run uses stays."""
        self.run_check()
        first = hostmedia.current()
        with mock.patch.object(hostmedia, "pinned", return_value=[{"file": first["file"], "sha256": first["sha256"]}]):
            self.run_check(sums(f"{'d' * 64} *ubuntu-26.04.1-live-server-amd64.iso"))
            self.run_check(SUMS + sums(f"{SHA_C} *ubuntu-26.04.2-live-server-amd64.iso"))
        isos = sorted(f for f in os.listdir(self.dir) if f.endswith(".iso"))
        self.assertIn(first["file"], isos)  # pinned: kept though neither current nor previous
        self.assertEqual(len(isos), 3)
        self.assertTrue(hostmedia.has(first))

    def test_a_failure_keeps_what_is_here(self):
        self.run_check()

        def broken(src, dst):
            raise hostmedia.MediaError("xorriso: no")

        ok, detail = hostmedia.check(
            log=lambda m: None,
            get=getter(SUMS + sums(f"{SHA_C} *ubuntu-26.04.2-live-server-amd64.iso")),
            download=self.download,
            build=broken,
            replicate=False,
        )
        self.assertFalse(ok)
        self.assertIn("xorriso", detail)
        self.assertEqual(hostmedia.current()["version"], "26.04.1")
        self.assertEqual(hostmedia.state()["error"], "xorriso: no")
        self.assertFalse([f for f in os.listdir(self.dir) if f.startswith(".work-")])

    def test_seeds(self):
        name = hostmedia.seed_name()
        hostmedia.put_seed_here(name, b"ISO")
        with open(os.path.join(self.dir, "seed", name), "rb") as fh:
            self.assertEqual(fh.read(), b"ISO")
        self.assertTrue(hostmedia.drop_seed_here(name))
        self.assertFalse(hostmedia.drop_seed_here(name))
        with self.assertRaises(hostmedia.MediaError):
            hostmedia.put_seed_here("../x.iso", b"")

    def test_the_grub_menu(self):
        self.assertIn("set timeout=0", hostmedia.GRUB)
        self.assertIn("linux\t/casper/vmlinuz autoinstall ---", hostmedia.GRUB)


class Served(TestCase):
    def test_the_site_ilo_networks(self):
        zk1, at1 = Site.objects.create(code="zk1"), Site.objects.create(code="at1")
        Host.objects.create(site=zk1, name="h1", ipmi_host="10.201.201.14")
        Host.objects.create(site=zk1, name="h2", ipmi_host="10.201.201.15")
        Host.objects.create(site=zk1, name="h3", ipmi_host="10.201.202.9")
        Host.objects.create(site=zk1, name="old", ipmi_host="10.201.203.9", state="retired")
        Host.objects.create(site=at1, name="h4", ipmi_host="10.61.201.10")
        self.assertEqual(haproxy.ilo_networks("zk1"), ["10.201.201.0/24", "10.201.202.0/24"])


class PeerAndServing(TestCase):
    """commit-124..126, commit-142."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.ov = override_settings(HOSTMEDIA_DIR=self.dir)
        self.ov.enable()
        with open(os.path.join(self.dir, "u.iso"), "w") as fh:
            fh.write("iso")
        self.sha = hostmedia.sha256(os.path.join(self.dir, "u.iso"))
        hostmedia.write_state({"current": {"version": "26.04.1", "file": "u.iso", "sha256": self.sha}})

    def tearDown(self):
        self.ov.disable()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_the_other_node_is_retried_until_it_has_it(self):
        env = {"SITE": "zk1", "ADDR": "192.0.2.1", "PEER_SITE": "at1", "PEER_ADDR": "192.0.2.2"}
        with (
            mock.patch("accounts.thisnode.env", return_value=env),
            mock.patch("accounts.ha_agent._pair", return_value=object()),
            mock.patch("accounts.ha_agent.peer_request", side_effect=OSError("down")) as req,
        ):
            self.assertTrue(hostmedia.peer_pending(hostmedia.state()))
            hostmedia.hand_over(hostmedia.state())
            self.assertIn("down", hostmedia.state()["peer"]["error"])
            req.side_effect, req.return_value = None, {"result": "checked", "sha256": self.sha}
            hostmedia.hand_over(hostmedia.state())
            self.assertFalse(hostmedia.peer_pending(hostmedia.state()))

    def test_the_site_node_must_have_the_current_iso(self):
        env = {"SITE": "zk1", "ADDR": "192.0.2.1", "PEER_SITE": "at1", "PEER_ADDR": "192.0.2.2"}
        with (
            mock.patch("accounts.thisnode.env", return_value=env),
            mock.patch("accounts.ha_agent._pair", return_value=object()),
            mock.patch("accounts.ha_agent.peer_request", return_value={"have": False}) as req,
        ):
            self.assertEqual(hostmedia.on_site_node("zk1"), (True, ""))
            ok, why = hostmedia.on_site_node("at1")
            self.assertFalse(ok)
            self.assertIn("at1", why)
            self.assertEqual(req.call_args_list[0].args[3], {"file": "u.iso", "sha256": self.sha})
        with (
            mock.patch("accounts.thisnode.env", return_value=env),
            mock.patch("accounts.ha_agent._pair", return_value=object()),
            mock.patch("accounts.ha_agent.peer_request", return_value={"have": True}),
        ):
            self.assertEqual(hostmedia.on_site_node("at1"), (True, ""))

    def test_a_refused_handover_falls_back_to_the_fetch(self):
        """commit-159: the pin is current here, the other node has a newer current one and refuses the handover."""
        env = {"SITE": "zk1", "ADDR": "192.0.2.1", "PEER_SITE": "at1", "PEER_ADDR": "192.0.2.2"}
        paths, fetched = [], []

        def req(peer, method, path, body=None, **_k):
            paths.append(path)
            if path == "/hostmedia-pull":
                raise OSError("a newer installer is here already")
            if path == "/hostmedia-fetch":
                fetched.append(body)
                return {"result": "checked"}
            return {"have": bool(fetched)}

        with (
            mock.patch("accounts.thisnode.env", return_value=env),
            mock.patch("accounts.ha_agent._pair", return_value=object()),
            mock.patch("accounts.ha_agent.peer_request", side_effect=req),
        ):
            self.assertEqual(hostmedia.on_site_node("at1"), (True, ""))
        self.assertEqual(
            paths, ["/hostmedia-have", "/hostmedia-pull", "/hostmedia-have", "/hostmedia-fetch", "/hostmedia-have"]
        )
        self.assertEqual(fetched[0], {"source": "192.0.2.1", "file": "u.iso", "sha256": self.sha})

    def test_a_pinned_iso_is_checked_on_disk(self):
        """commit-142/152: the run's own ISO, hashed on disk - a damaged file or another ISO is not it."""
        env = {"SITE": "zk1", "ADDR": "192.0.2.1"}
        pin = {"file": "u.iso", "sha256": self.sha}
        with mock.patch("accounts.thisnode.env", return_value=env):
            self.assertEqual(hostmedia.on_site_node("zk1", pin), (True, ""))
            self.assertFalse(hostmedia.on_site_node("zk1", {"file": "u.iso", "sha256": "b" * 64})[0])
            with open(os.path.join(self.dir, "u.iso"), "w") as fh:
                fh.write("damaged")
            ok, why = hostmedia.on_site_node("zk1", pin)
            self.assertFalse(ok)
            self.assertIn("not on this node (whole)", why)

    def test_a_late_answer_does_not_overwrite_a_newer_installer(self):
        """commit-153."""
        env = {"SITE": "zk1", "ADDR": "192.0.2.1", "PEER_SITE": "at1", "PEER_ADDR": "192.0.2.2"}
        sent = hostmedia.state()

        def slow(*_a, **_k):  # a newer installer is published while the other node copies
            hostmedia.write_state({"current": {"version": "26.04.2", "file": "v.iso", "sha256": "c" * 64}})
            return {"result": "checked", "sha256": self.sha}

        with (
            mock.patch("accounts.thisnode.env", return_value=env),
            mock.patch("accounts.ha_agent._pair", return_value=object()),
            mock.patch("accounts.ha_agent.peer_request", side_effect=slow),
        ):
            out = hostmedia.hand_over(sent)
        st = hostmedia.state()
        self.assertEqual(st["current"]["file"], "v.iso")
        self.assertNotIn("peer", st)
        self.assertIn("newer installer", out)

    def test_only_ipv4_ilos_and_apache_local(self):
        zk1 = Site.objects.create(code="zk1")
        Host.objects.create(site=zk1, name="h1", ipmi_host="10.201.201.14")
        Host.objects.create(site=zk1, name="h6", ipmi_host="fd00::14")
        Host.objects.create(site=zk1, name="hn", ipmi_host="ilo-h.example.com")
        self.assertEqual(haproxy.ilo_networks("zk1"), ["10.201.201.0/24"])
        self.assertEqual(haproxy.ilo_unserved("zk1"), [("h6", "fd00::14"), ("hn", "ilo-h.example.com")])
        from django.conf import settings

        text = open(os.path.join(settings.BASE_DIR, "deploy", "install.sh")).read()
        block = text[text.index("<Directory $DATA_DIR/hostmedia>") :][:200]
        self.assertIn("Require local", block)
        self.assertNotIn("Require all granted", block)
