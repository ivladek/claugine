"""The hosts' installation media on the portal nodes (owner 2026-11-18, T25 §A4): the rebuilt Ubuntu installer ISO and
the per-host answers ISOs, served to the hosts' iLOs.

    08:28 "use portal nodes as distribution point for isos - ubuntu and answers file"
    08:50 "Only one and replicate it to another"
    11:59 "ISOs must be mounted from portal node an related site"
    12:01 "may be we still can ise ha proxy ? / and dont add any new staff to nodes ?"
    12:04 "keep modified ubuntu iso on portal node and just check that no new one availaeble on ubuntu.com / answer
          ISOs deleted after host deployed"

The installer. `check()` (the timer "hostmedia", daily; Check now in Profiles → Baseline; and before every host
deployment) reads releases.ubuntu.com/<base OS>/SHA256SUMS and its signature, checks the signature against the Ubuntu
CD image key shipped with the portal (deploy/hostmedia/ubuntu-cdimage-keyring.gpg, gpgv), and takes the newest point
release of the base OS's live-server ISO. Only when that is newer than the one kept here: download it, check its
SHA-256, rebuild it with xorriso - only /boot/grub/grub.cfg replaced: no menu wait, one entry,
the kernel argument `autoinstall` (subiquity then asks nothing; cloud-init's NoCloud finds the answers ISO by its
CIDATA label); the boot images are replayed as they are - and keep it with its SHA-256 as the current one, the
previous one kept, older ones removed. A failure keeps what is here. With a second node (Enterprise) the new ISO is
then handed to it (ee/ha_agent.py: the other node pulls it from this node's agent and checks its SHA-256): one
download for both.

The answers ISOs live in seed/, each named with a random token (inventory/autoinstall.py makes them; on the node of
the host's site, handed over by the agent when that is the other node), and are deleted when the host is deployed or
its run fails or is abandoned.

Serving. Each node's own Apache serves the folder at /hostmedia/ on its HTTP port (deploy/install.sh), and HAProxy on
port 80 sends /hostmedia/ there only from the iLO addresses of the node's site (install-lb.sh --hostmedia,
accounts/haproxy.py), never to the other node: a host's iLO always takes its media from its own site's node
(`url()`).
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import urllib.request
from datetime import UTC, datetime

from django.conf import settings

RELEASES = "https://releases.ubuntu.com"
ISO_RE = re.compile(r"^([0-9a-f]{64}) \*?(ubuntu-(\d{2}\.\d{2}(?:\.\d+)?)-live-server-amd64\.iso)$")
# commit-142/160: the name carries the rebuilt ISO's own checksum, so other bytes are always another file
OUT = "ubuntu-{ver}-live-server-amd64-claugine-{tag}.iso"
NAME_RE = re.compile(r"^ubuntu-[0-9.]+-live-server-amd64-claugine-[0-9a-f]{12}\.iso$")  # commit-167: always tagged
KEYRING = os.path.join(settings.BASE_DIR, "deploy", "hostmedia", "ubuntu-cdimage-keyring.gpg")
SEED = "seed"
TIMEOUT = 60

GRUB = """set timeout=0
set default=0
set timeout_style=hidden
menuentry "Install Ubuntu Server (Claugine, unattended)" {
	set gfxpayload=keep
	linux	/casper/vmlinuz autoinstall ---
	initrd	/casper/initrd
}
"""


class MediaError(Exception):
    pass


def folder():
    return settings.HOSTMEDIA_DIR


def _state_path():
    return os.path.join(folder(), "state.json")


def state():
    """{"current": {version, file, sha256, source_sha256, at}, "previous": {...}, "checked_at", "error"}."""
    try:
        with open(_state_path()) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def write_state(st):
    os.makedirs(folder(), exist_ok=True)
    tmp = _state_path() + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh, indent=1)
    os.replace(tmp, _state_path())


def _vkey(v):
    return tuple(int(x) for x in v.split("."))


def _get(url, timeout=TIMEOUT):
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 - https only, fixed host
        return r.read()


def newest(series, get=_get, keyring=KEYRING):
    """(version, file name, SHA-256) of the newest live-server ISO of an Ubuntu series, from its signed SHA256SUMS."""
    base = f"{RELEASES}/{series}"
    sums, sig = get(f"{base}/SHA256SUMS"), get(f"{base}/SHA256SUMS.gpg")
    with tempfile.TemporaryDirectory() as d:
        a, b = os.path.join(d, "SHA256SUMS"), os.path.join(d, "SHA256SUMS.gpg")
        with open(a, "wb") as fh:
            fh.write(sums)
        with open(b, "wb") as fh:
            fh.write(sig)
        r = subprocess.run(["gpgv", "--keyring", keyring, b, a], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise MediaError(f"SHA256SUMS of {series}: the signature does not check: {r.stderr.strip()[-200:]}")
    found = []
    for line in sums.decode(errors="replace").splitlines():
        m = ISO_RE.match(line.strip())
        if m and (m.group(3) == series or m.group(3).startswith(series + ".")):
            found.append((m.group(3), m.group(2), m.group(1)))
    if not found:
        raise MediaError(f"no live-server ISO of Ubuntu {series} on {base}")
    return max(found, key=lambda x: _vkey(x[0]))


def _download(url, path, want_sha):
    h = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r, open(path, "wb") as fh:  # noqa: S310
        for chunk in iter(lambda: r.read(1 << 20), b""):
            h.update(chunk)
            fh.write(chunk)
    if h.hexdigest() != want_sha:
        raise MediaError(f"{os.path.basename(path)}: its SHA-256 differs from SHA256SUMS")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rebuild(src, dst):
    """The installer ISO with our boot menu (GRUB above as /boot/grub/grub.cfg, which UEFI and BIOS boot read);
    everything else, the boot images included, as it is (checked 2026-11-18 on 26.04.1: /casper/vmlinuz,
    /casper/initrd; El Torito BIOS and UEFI images kept)."""
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "grub.cfg")
        with open(cfg, "w") as fh:
            fh.write(GRUB)
        cmd = ["xorriso", "-indev", src, "-outdev", dst, "-map", cfg, "/boot/grub/grub.cfg"]
        cmd += ["-boot_image", "any", "replay"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            raise MediaError(f"xorriso: {r.stderr.strip()[-300:]}")


def _now():
    return datetime.now(UTC).isoformat(timespec="seconds")


@contextlib.contextmanager
def locked():
    """commit-153: one writer of the state at a time (the check, a peer's answer), across processes."""
    os.makedirs(folder(), exist_ok=True)
    with open(os.path.join(folder(), ".state.lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def check(log=print, get=_get, download=_download, build=rebuild, replicate=True):
    """Keep the newest point release of the base OS here (see the module). Returns (ok, detail). The state is changed
    under the lock; the handover to the other node (up to two hours) runs after it, and its answer is merged under the
    lock again (commit-153)."""
    with locked():
        ok, detail, send = _check(log, get, download, build)
    if replicate and send is not None:
        detail += "; " + hand_over(send)
    return ok, detail


def _check(log, get, download, build):
    """(ok, detail, state to hand over or None)."""
    from inventory.baseline import values

    series = values()["os"]
    st = state()
    st["checked_at"] = _now()
    try:
        ver, name, src_sha = newest(series, get=get)
    except Exception as exc:  # noqa: BLE001 - kept in the state, what is here stays
        st["error"] = str(exc)[:300]
        write_state(st)
        return False, f"check: {st['error']}", None
    cur = st.get("current") or {}
    st["pinned"] = pinned()
    # commit-156: "current" only when its file is whole (hashed on disk); a damaged one is downloaded and rebuilt
    if cur.get("source_sha256") == src_sha and has(cur):
        st["error"] = ""
        write_state(st)
        # commit-125: the other node's copy is retried until it is the current one
        return True, f"Ubuntu {ver}: current", (dict(st) if peer_pending(st) else None)
    log(f"Ubuntu {ver}: new, downloading {name}")
    os.makedirs(folder(), exist_ok=True)
    work = tempfile.mkdtemp(prefix=".work-", dir=folder())
    try:
        src = os.path.join(work, name)
        download(f"{RELEASES}/{series}/{name}", src, src_sha)
        dst = os.path.join(work, "out.iso")
        build(src, dst)
        out_sha = sha256(dst)
        out_name = OUT.format(ver=ver, tag=out_sha[:12])
        os.replace(dst, os.path.join(folder(), out_name))
    except Exception as exc:  # noqa: BLE001
        st["error"] = str(exc)[:300]
        write_state(st)
        return False, f"Ubuntu {ver}: {st['error']}", None
    finally:
        shutil.rmtree(work, ignore_errors=True)
    new = {
        "version": ver,
        "file": out_name,
        "sha256": out_sha,
        "source_sha256": src_sha,
        "at": _now(),
    }
    if cur and cur.get("file") != out_name:
        st["previous"] = cur
    st["current"], st["error"] = new, ""
    st["pinned"] = pinned()  # commit-142: read again right before pruning, still under the lock s_checks pins under
    keep = {out_name, (st.get("previous") or {}).get("file")} | {x["file"] for x in st["pinned"]}
    for f in os.listdir(folder()):
        if f.endswith(".iso") and f not in keep:
            try:
                os.remove(os.path.join(folder(), f))
            except OSError:
                pass
    write_state(st)
    return True, f"Ubuntu {ver}: rebuilt ({new['sha256'][:12]}…)", dict(st)


def _peer_addr():
    from accounts import ha_agent, thisnode

    return thisnode.env().get("PEER_ADDR", "") if ha_agent._pair() is not None else ""


def peer_pending(st):
    """With a second node: True while its copy is not the current ISO (commit-125)."""
    cur = (st or {}).get("current") or {}
    return bool(_peer_addr()) and bool(cur.get("sha256")) and (st.get("peer") or {}).get("sha256") != cur["sha256"]


def hand_over(st):
    """Enterprise: the other node pulls the current ISO from this node's agent (ee/ha_agent.py); its result is kept in
    the state ("peer": sha256, at, error) and shown in Settings (commit-125). One node: nothing."""
    from accounts import ha_agent, thisnode

    peer = _peer_addr()
    if not peer:
        return "one node"
    rec = {"at": _now(), "sha256": (st.get("peer") or {}).get("sha256", ""), "error": ""}
    try:
        me = thisnode.env().get("ADDR", "")
        r = ha_agent.peer_request(peer, "POST", "/hostmedia-pull", {"source": me, "state": st}, timeout=7200)
        rec["sha256"] = r.get("sha256", "")
        out = f"other node: {r.get('result', 'done')}"
    except Exception as exc:  # noqa: BLE001 - retried on the next round
        rec["error"] = str(exc)[:200]
        out = f"other node: {rec['error']}"
    sent = (st.get("current") or {}).get("sha256")
    with locked():  # commit-153: merged into the state as it is now, only while it still names what was sent
        now = state()
        if (now.get("current") or {}).get("sha256") != sent:
            return out + " (a newer installer is current now: handed over on the next check)"
        now["peer"] = rec
        write_state(now)
    return out


def on_site_node(site, pin=None):
    """commit-142: (True, "") when the portal node of the site has the installer ISO pin ({file, sha256}; the current one
    when None) - its file there, hashed on disk (commit-152) - else (False, why). The other node is asked through its
    agent (and handed the current ISO again when it is the one it lacks)."""
    from accounts import ha_agent, thisnode

    pin = pin or current()
    if not pin:
        return False, "no installer ISO on this node"
    if not has(pin):
        return False, f"the installer ISO {pin.get('file')} is not on this node (whole) any more"
    n = thisnode.env()
    if not n.get("ADDR") or n.get("SITE") == site:
        return True, ""
    peer = node_for(site)
    if not peer:
        return False, f"this installation has no portal node at {site}"

    def theirs():
        try:
            r = ha_agent.peer_request(
                peer, "POST", "/hostmedia-have", {"file": pin["file"], "sha256": pin["sha256"]}, timeout=600
            )
            return bool(r.get("have")), ""
        except Exception as exc:  # noqa: BLE001
            return False, f"error: {exc}"

    got, err = theirs()
    if not got and (current() or {}).get("sha256") == pin["sha256"]:
        hand_over(state())
        got, err = theirs()
    if not got:  # commit-159: still missing (an older ISO, or the handover refused there as older than its current) -
        # the other node fetches that one ISO; its current one stays
        try:
            me = thisnode.env().get("ADDR", "")
            ha_agent.peer_request(
                peer,
                "POST",
                "/hostmedia-fetch",
                {"source": me, "file": pin["file"], "sha256": pin["sha256"]},
                timeout=7200,
            )
        except Exception as exc:  # noqa: BLE001
            err = f"fetch: {exc}"
        got, err2 = theirs()
        err = err2 or err
    if not got:
        return False, f"the portal node at {site} does not have the installer ISO {pin['file']}" + (
            f" ({err[:120]})" if err else ""
        )
    return True, ""


def pinned():
    """commit-142: the installer ISOs a running or failed (resumable) host deployment still uses - kept on both nodes
    until the run is done or abandoned."""
    from inventory.models import HostDeployment

    out = {}
    for data in HostDeployment.objects.filter(
        state__in=[HostDeployment.State.RUNNING, HostDeployment.State.FAILED]
    ).values_list("data", flat=True):
        pin = (data or {}).get("installer") or {}
        if NAME_RE.match(pin.get("file") or "") and re.match(r"^[0-9a-f]{64}$", pin.get("sha256") or ""):
            out[pin["file"]] = {"file": pin["file"], "sha256": pin["sha256"]}
    return sorted(out.values(), key=lambda x: x["file"])


def held(name):
    """commit-159: {file, sha256} of an ISO this node holds whole under that name (current, previous or pinned), else
    {} - what the other node may fetch."""
    st = state()
    for x in (st.get("current"), st.get("previous"), *(st.get("pinned") or [])):
        if x and x.get("file") == name and has(x):
            return {"file": name, "sha256": x["sha256"]}
    return {}


def has(pin):
    """commit-142/152: the ISO pin ({file, sha256}) is here as the current or previous one, its file hashed on disk."""
    name, want = (pin or {}).get("file") or "", (pin or {}).get("sha256") or ""
    st = state()
    known = [
        x for x in (st.get("current"), st.get("previous"), *(st.get("pinned") or [])) if x and x.get("file") == name
    ]
    path = os.path.join(folder(), name)
    return bool(
        name
        and "/" not in name
        and any(x.get("sha256") == want for x in known)
        and os.path.isfile(path)
        and sha256(path) == want
    )


def current():
    """The current installer ISO here: {version, file, sha256, at} or {}, only when its file is here."""
    cur = state().get("current") or {}
    return cur if cur.get("file") and os.path.exists(os.path.join(folder(), cur["file"])) else {}


def node_for(site):
    """The address of the portal node of a site (node.env ADDR / PEER_ADDR and their SITE / PEER_SITE), "" when this
    installation has no node there."""
    from accounts import thisnode

    n = thisnode.env()
    if n.get("SITE") == site:
        return n.get("ADDR", "")
    if n.get("PEER_SITE") == site:
        return n.get("PEER_ADDR", "")
    return ""


def url(site, path):
    """http://<the site's node>/hostmedia/<path>; "" when the site has no node."""
    addr = node_for(site)
    return f"http://{addr}/hostmedia/{path}" if addr else ""


# --- the answers ISOs (seed/) -----------------------------------------------------------------------------------


def seed_name():
    return f"{secrets.token_hex(12)}.iso"


def _seed_path(name):
    if not re.match(r"^[0-9a-f]{24}\.iso$", name or ""):
        raise MediaError(f"not an answers ISO name: {name}")
    return os.path.join(folder(), SEED, name)


def put_seed_here(name, data):
    p = _seed_path(name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, p)


def drop_seed_here(name):
    try:
        os.remove(_seed_path(name))
        return True
    except FileNotFoundError:
        return False


def put_seed(site, name, data):
    """The answers ISO on the node of the host's site: here, or handed to the other node (Enterprise)."""
    import base64

    from accounts import ha_agent, thisnode

    if thisnode.env().get("SITE", "") == site or not thisnode.env().get("ADDR"):
        return put_seed_here(name, data)
    peer = node_for(site)
    if not peer:
        raise MediaError(f"this installation has no portal node at {site}")
    ha_agent.peer_request(peer, "POST", "/hostmedia-seed", {"name": name, "data": base64.b64encode(data).decode()})


def drop_seed(site, name):
    from accounts import ha_agent, thisnode

    if thisnode.env().get("SITE", "") == site or not thisnode.env().get("ADDR"):
        return drop_seed_here(name)
    peer = node_for(site)
    if peer:
        ha_agent.peer_request(peer, "POST", "/hostmedia-seed", {"name": name, "drop": True})
    return True
