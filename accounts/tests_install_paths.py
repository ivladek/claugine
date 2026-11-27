"""enterprise-001 (twentieth full review): /var/lib/claugine-portal belongs to the web process (www-data), so the
installer - run as root, also by claugine-add-node on both nodes - makes and removes what is inside it as www-data. As
root, `install -d -o www-data` on a path the web process had replaced with a link followed the link and gave a root
folder to www-data."""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

from django.test import SimpleTestCase

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def install_sh():
    with open(os.path.join(HERE, "deploy", "install.sh"), encoding="utf-8") as fh:
        return fh.read()


class DataDirByTheWebUser(SimpleTestCase):
    def test_nothing_inside_the_data_folder_is_touched_as_root(self):
        changing = re.compile(r"\b(install|rm|chown|chmod|mkdir|mv|cp|ln|touch)\b")
        lines = [ln.strip() for ln in install_sh().splitlines()]
        inside = [ln for ln in lines if '"$DATA_DIR/' in ln and changing.search(ln) and not ln.startswith("#")]
        self.assertTrue(inside)
        for ln in inside:
            self.assertTrue(ln.startswith("as_web "), ln)
        self.assertIn('as_web() { runuser -u www-data -- "$@"; }', install_sh())

    @unittest.skipUnless(
        os.geteuid() == 0
        and shutil.which("runuser")
        and subprocess.run(["id", "www-data"], capture_output=True).returncode == 0,
        "needs root, runuser and a www-data user",
    )
    def test_a_link_left_in_the_data_folder_reaches_no_root_folder(self):
        s = install_sh()
        start = s.index("as_web() {")
        block = s[start : s.index("\n", s.index("|| {", start))]  # as_web and the media/mail line
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        os.chmod(base, 0o755)
        data = os.path.join(base, "data")
        os.mkdir(data, 0o750)
        shutil.chown(data, "www-data", "www-data")
        target = os.path.join(base, "root-only")
        os.mkdir(target, 0o700)
        os.symlink(target, os.path.join(data, "media"))  # what the web process could leave there
        shutil.chown(os.path.join(data, "media"), "www-data", "www-data", follow_symlinks=False)
        r = subprocess.run(["bash", "-c", f"set -e\nDATA_DIR={data}\n{block}"], capture_output=True, text=True)
        st = os.stat(target)
        self.assertEqual((st.st_uid, st.st_mode & 0o777), (0, 0o700), r.stderr)  # untouched
        self.assertNotEqual(r.returncode, 0)  # www-data cannot make a folder through the link: the install stops
        # with no link there, the folders are made, www-data's
        os.unlink(os.path.join(data, "media"))
        r = subprocess.run(["bash", "-c", f"set -e\nDATA_DIR={data}\n{block}"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        import pwd

        web = pwd.getpwnam("www-data").pw_uid
        for name in ("media", "mail"):
            self.assertEqual(os.stat(os.path.join(data, name)).st_uid, web)
