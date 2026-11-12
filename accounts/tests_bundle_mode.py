"""Bundle mode: the Enterprise edition's code arrives as a release bundle, not from a git repository.

The contract (claugine-web `project/licensing.md`, confirmed in the `enterprise-bundles` job): the node asks
`GET /api/release/<version>` with the licence key as a bearer token and its installation id, and the answer
carries the archive's SHA-256 in `X-Content-SHA256` (hex) or `Digest: sha-256=<base64>`. **The checksum is
verified before the archive is unpacked**, and an answer carrying neither header is refused.

`deploy/server/claugine-portal-update` and `deploy/claugine-install.sh` each implement this, because the installer
is downloaded on its own by a client and cannot source anything. These tests run the real shell against a real
HTTP server so the two cannot quietly drift apart.
"""

import base64
import hashlib
import os
import subprocess
import tarfile
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from django.test import SimpleTestCase

REPO = Path(__file__).resolve().parent.parent
UPDATER = REPO / "deploy" / "server" / "claugine-portal-update"
INSTALLER = REPO / "deploy" / "claugine-install.sh"
BOTH = {"updater": UPDATER, "installer": INSTALLER}


def _function_text(path):
    """The shared fetch_bundle, as it stands in one of the two scripts."""
    out, taking = [], False
    for line in path.read_text().splitlines(keepends=True):
        if line.startswith("# ---- fetch_bundle:"):
            taking = True
        if taking:
            out.append(line)
            if line.rstrip() == "}":
                break
    return "".join(out)


def _bundle(tmp):
    """A release bundle as GitHub serves one: a .tar.gz with a single top-level directory."""
    inner = Path(tmp) / "claugine-v1"
    (inner / "deploy").mkdir(parents=True)
    (inner / "deploy" / "install.sh").write_text("#!/bin/bash\ntrue\n")
    (inner / "marker.txt").write_text("the payload\n")
    path = Path(tmp) / "bundle.tgz"
    with tarfile.open(path, "w:gz") as t:
        t.add(inner, arcname="claugine-v1")
    data = path.read_bytes()
    return data, hashlib.sha256(data).hexdigest()


class _Serve(BaseHTTPRequestHandler):
    body = b""
    headers_out = {}
    status = 200
    seen = {}

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        type(self).seen = {
            "path": self.path,
            "auth": self.headers.get("Authorization", ""),
            "install": self.headers.get("X-Install-Id", ""),
        }
        self.send_response(type(self).status)
        for k, v in type(self).headers_out.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(type(self).body)))
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *a):  # keep the test output clean
        return


class BundleFetch(SimpleTestCase):
    """`fetch_bundle` from the updater, run for real against a local server."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.body, self.sha = _bundle(self.tmp)
        _Serve.body, _Serve.status, _Serve.headers_out, _Serve.seen = self.body, 200, {}, {}
        self.httpd = HTTPServer(("127.0.0.1", 0), _Serve)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]
        self.penv = Path(self.tmp) / "portal.env"
        self.penv.write_text(
            f"CLAUGINE_CHECKIN_URL=http://127.0.0.1:{self.port}\n"
            "CLAUGINE_LICENCE_KEY=CLG-TEST-KEY\n"
            "CLAUGINE_INSTALL_ID=inst-test\n"
            "SITE_URL=https://portal.example.com\n"
        )

    def tearDown(self):
        self.httpd.shutdown()

    def run_fetch(self, version="v1.0.0", script=UPDATER, licence="CLG-TEST-KEY"):
        """Run the real shell function out of whichever script, against the live fixture server."""
        dest = Path(self.tmp) / f"src-{script.name}"
        if dest.exists():
            for f in sorted(dest.rglob("*"), reverse=True):
                f.rmdir() if f.is_dir() else f.unlink()
        dest.mkdir(exist_ok=True)
        env = {
            "PATH": os.environ["PATH"],
            "HOME": self.tmp,
            "CLAUGINE_UPDATE_SOURCE_ONLY": "1",
            "CLAUGINE_PORTAL_ENV": str(self.penv),
        }
        # set after sourcing: each script initialises its own variables, and claugine-install.sh blanks
        # LICENCE_KEY on the way past. The function's contract is the three variables as they are when it runs.
        setup = f'CHECKIN_URL="http://127.0.0.1:{self.port}"; INSTALL_ID="inst-test"; LICENCE_KEY="{licence or ""}"'
        r = subprocess.run(
            ["bash", "-c", f'source "{script}"; {setup}; fetch_bundle "{version}" "{dest}"'],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        return r, dest

    # ---------------------------------------------------------------- the happy paths
    def test_a_hex_checksum_is_verified_and_the_bundle_unpacked(self):
        _Serve.headers_out = {"X-Content-SHA256": self.sha}
        for name, script in BOTH.items():
            with self.subTest(script=name):
                r, dest = self.run_fetch(script=script)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("checksum verified before unpacking", r.stdout)
                self.assertTrue((dest / "marker.txt").exists())  # --strip-components=1: no claugine-v1/ level
                self.assertTrue((dest / "deploy" / "install.sh").exists())

    def test_the_digest_header_is_accepted_too(self):
        b64 = base64.b64encode(bytes.fromhex(self.sha)).decode()
        _Serve.headers_out = {"Digest": f"sha-256={b64}"}
        for name, script in BOTH.items():
            with self.subTest(script=name):
                r, dest = self.run_fetch(script=script)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertTrue((dest / "marker.txt").exists())

    def test_the_licence_and_installation_identify_the_node(self):
        _Serve.headers_out = {"X-Content-SHA256": self.sha}
        self.run_fetch(version="v00.16.05")
        self.assertEqual(_Serve.seen["path"], "/api/release/v00.16.05")
        self.assertEqual(_Serve.seen["auth"], "Bearer CLG-TEST-KEY")
        self.assertEqual(_Serve.seen["install"], "inst-test")

    # ---------------------------------------------------------------- the refusals, which are the point
    def test_a_wrong_checksum_is_refused_and_nothing_is_unpacked(self):
        _Serve.headers_out = {"X-Content-SHA256": "0" * 64}
        for name, script in BOTH.items():
            with self.subTest(script=name):
                r, dest = self.run_fetch(script=script)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("checksum does not match", r.stderr)
                self.assertFalse(
                    any(dest.iterdir())
                )  # refused before unpacking                    # refused *before* unpacking

    def test_no_checksum_header_at_all_is_refused(self):
        _Serve.headers_out = {}
        for name, script in BOTH.items():
            with self.subTest(script=name):
                r, dest = self.run_fetch(script=script)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("carried no checksum", r.stderr)
                self.assertFalse(any(dest.iterdir()))  # refused before unpacking

    def test_a_refused_download_does_not_unpack_anything(self):
        _Serve.status, _Serve.body = 403, b'{"error":"not_entitled"}'
        for name, script in BOTH.items():
            with self.subTest(script=name):
                r, dest = self.run_fetch(script=script)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("on hold or not offered", r.stderr)  # claugine-web 2026-11-11: old releases are held
                self.assertIn("without --ref", r.stderr)
                self.assertFalse(any(dest.iterdir()))  # refused before unpacking

    def test_another_refusal_names_the_http_status(self):
        _Serve.status, _Serve.body = 500, b"oops"
        for name, script in BOTH.items():
            with self.subTest(script=name):
                r, dest = self.run_fetch(script=script)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("release download failed (HTTP 500)", r.stderr)
                self.assertFalse(any(dest.iterdir()))

    def test_a_bundle_without_an_installer_is_refused(self):
        """A well-formed archive that is not a release must not be installed."""
        inner = Path(self.tmp) / "notarelease"
        inner.mkdir()
        (inner / "readme").write_text("nothing here\n")
        path = Path(self.tmp) / "other.tgz"
        with tarfile.open(path, "w:gz") as t:
            t.add(inner, arcname="notarelease")
        _Serve.body = path.read_bytes()
        _Serve.headers_out = {"X-Content-SHA256": hashlib.sha256(_Serve.body).hexdigest()}
        r, _dest = self.run_fetch()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no deploy/install.sh", r.stderr)

    def test_without_a_licence_it_does_not_even_ask(self):
        for name, script in BOTH.items():
            with self.subTest(script=name):
                _Serve.seen = {}
                r, _dest = self.run_fetch(script=script, licence=None)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("needs the control-plane URL, the licence key", r.stderr)
                self.assertEqual(_Serve.seen, {})  # nothing was requested

    def test_the_two_copies_of_the_function_are_identical(self):
        """The installer is downloaded alone and cannot source anything, so the function exists twice. If the two
        ever drift, a fix to one would silently miss the other - this is what stops that."""
        a, b = _function_text(UPDATER), _function_text(INSTALLER)
        self.assertTrue(a.strip(), "fetch_bundle not found in the updater")
        self.assertEqual(a, b, "fetch_bundle differs between the updater and the installer")
