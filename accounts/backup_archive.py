"""One archive and one script per backup (owner 2026-11-12), the same for the portal's backups and the platforms':

    <name>/
        <name>.7z     the data only, in a 7z archive encrypted with AES-256 and the archive password; the file
                      names are encrypted too (-mhe=on)
        <name>.sh     the restore: it checks the archive against the SHA-256 written in it, then
                      - a portal backup: downloads the public installer (claugine repo, deploy/claugine-install.sh) and
                        runs its --restore with this archive;
                      - a platform backup: only unpacks the archive (asks for the password) and prints how to put a
                        node back.
                      Owner 2026-11-12: no MANIFEST or README - what the backup is (when, where from, which release)
                      is the script's comment header.

The script is written last, so a folder without it is an incomplete backup. The archive is made by Ubuntu's 7-Zip
(package 7zip, installed with the portal); the password goes to 7-Zip on stdin, never on a command line.
"""

import hashlib
import os
import shlex
import shutil
import subprocess

INSTALLER_URL = "https://raw.githubusercontent.com/ivladek/claugine/main/deploy/claugine-install.sh"
ARCHIVE = ".7z"
SCRIPT = ".sh"


class ArchiveError(RuntimeError):
    pass


def seven_zip():
    found = shutil.which("7z")
    if not found:
        raise ArchiveError("7z is missing: install the package 7zip")
    return found


def make(src, out, password, level=5):
    """Every file under `src` into the 7z archive `out` (AES-256, names encrypted)."""
    if not password:
        raise ArchiveError("no archive password")
    r = subprocess.run(
        [seven_zip(), "a", "-t7z", "-mhe=on", f"-mx={level}", "-p", "-bso0", "-bsp0", "-y", os.path.abspath(out), "."],
        cwd=src,
        input=(password + "\n").encode(),
        capture_output=True,
        timeout=6 * 3600,
    )
    if r.returncode != 0 or not os.path.isfile(out):
        raise ArchiveError(f"7z could not make the archive: {r.stderr.decode(errors='replace').strip()[-300:]}")
    check(out, password)


def check(path, password):
    """The archive opens with the password and every file in it is intact (7z t)."""
    r = subprocess.run(
        [seven_zip(), "t", "-bso0", "-bsp0", path],
        input=(password + "\n").encode(),
        capture_output=True,
        timeout=6 * 3600,
    )
    if r.returncode != 0:
        raise ArchiveError(f"7z test of {os.path.basename(path)} failed: {r.stderr.decode(errors='replace')[-300:]}")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_HEAD = """#!/usr/bin/env bash
# {name}.sh - {what}
# A backup is two files: this script and {name}.7z
# (7z, AES-256, file names encrypted; the archive password from Settings -> Backups).
{info}
{usage}
set -euo pipefail
NAME={q_name}
SHA256={sha}
HERE="$(cd "$(dirname "$0")" && pwd)"
ARCHIVE="$HERE/$NAME.7z"
[[ -f "$ARCHIVE" ]] || {{ echo "error: $NAME.7z is not next to this script" >&2; exit 1; }}
echo "== Checking $NAME.7z ({size} bytes)"
echo "$SHA256  $ARCHIVE" | sha256sum -c --quiet - \\
  || {{ echo "error: $NAME.7z does not match this script: damaged, or another backup's archive" >&2; exit 1; }}
"""

_PORTAL = """mkdir -p "$HOME/claugine"
echo "== Downloading the installer"
curl -fsSL -o "$HOME/claugine/claugine-install.sh" {q_installer}
exec bash "$HOME/claugine/claugine-install.sh" --restore --backup "$ARCHIVE" --site-url {q_url} "$@"
"""

_UNPACK = """command -v 7z >/dev/null || {{ echo "== Installing 7-Zip"; sudo apt-get install -y -q 7zip >/dev/null; }}
OUT="${{1:-$HERE/$NAME}}"
[[ ! -e "$OUT" ]] || {{ echo "error: $OUT already exists" >&2; exit 1; }}
umask 077
mkdir -p "$OUT"
echo "== Unpacking into $OUT (7-Zip asks for the archive password)"
7z x -bso0 -bsp0 -o"$OUT" "$ARCHIVE" || {{ rm -rf "$OUT"; echo "error: wrong password, or the archive is damaged" >&2; exit 1; }}
echo "== Done: $OUT"
cat <<'AFTER'
{after}
AFTER
"""


def write_script(folder, name, kind, archive_path, url="", info=(), after=""):
    """<folder>/<name>.sh for the archive; `kind` "portal" (restore through the public installer) or "platform"
    (unpack only). Owner 2026-11-12: no MANIFEST or README - what they said is here: `info`, lines about the backup
    (its comment header), and `after`, what the platform script prints once unpacked. Written last: returns its path."""
    q = shlex.quote
    if "\nAFTER\n" in "\n" + after + "\n":
        raise ArchiveError("the text after unpacking contains the here-document's end marker")
    info_text = "\n".join("# " + line if line else "#" for line in ["", *info, ""])
    if kind == "portal":
        what = f"restore of the Claugine portal {url}"
        usage = (
            "# On a freshly installed Ubuntu 26.04 VM, with both files copied there, as the ordinary user with\n"
            "# passwordless sudo (not root):\n"
            f"#     bash {name}.sh --licence-key CLG-...      (Enterprise: add --second <user>@<the other VM>)\n"
            "# It checks the archive, downloads the public installer and runs its --restore with this archive: the\n"
            "# installer asks for the archive password once, before anything is written."
        )
        body = _PORTAL.format(q_installer=q(INSTALLER_URL), q_url=q(url))
    else:
        what = "a backup of an OpenNebula platform's front-end nodes"
        usage = (
            "# Unpack it anywhere (Ubuntu or any Linux with 7-Zip):   bash "
            f"{name}.sh [<folder>]     (default: ./{name})\n"
            "# It checks the archive, asks for the archive password, unpacks, and prints how to put a node back."
        )
        body = _UNPACK.format(after=after)
    text = (
        _HEAD.format(
            name=name,
            what=what,
            usage=usage,
            info=info_text,
            q_name=q(name),
            sha=sha256(archive_path),
            size=os.path.getsize(archive_path),
        )
        + body
    )
    path = os.path.join(folder, name + SCRIPT)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def complete(folder):
    """A backup folder of this format is complete when its script is there (written last)."""
    name = os.path.basename(folder.rstrip("/"))
    return os.path.isfile(os.path.join(folder, name + SCRIPT)) and os.path.isfile(os.path.join(folder, name + ARCHIVE))
