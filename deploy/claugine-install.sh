#!/usr/bin/env bash
# claugine-install.sh — install, restore or extend a Claugine portal. The one public entry point.
#
# Download it into ~/claugine and run it there, as the user you log in with over ssh — NOT as root:
#
#   mkdir -p ~/claugine && cd ~/claugine
#   curl -fsSLO https://raw.githubusercontent.com/ivladek/claugine/main/deploy/claugine-install.sh
#   bash claugine-install.sh --new --licence-key CLG-… --site-url https://portal.example.com
#
#   --new      a first portal node: fetch the code this licence is entitled to, install it, bind the licence.
#              The installation id is the host of --site-url (v01.00.00), so a re-run after a failed install, or a
#              rebuild of the same portal, is the same installation; --install-id forces another id
#   --restore  the same, then restore one backup over it (asks for the archive password once, before anything
#              is written)
#
#   --init     portal/init (T36 B6, owner 2026-11-25 L1-L5): the portal in its init edition on a VM on the engineer's
#              laptop (one NIC on cloud-core, routes to the IPMI network). It installs the first site's hosts, its one
#              platform (mgmt or simple) and base services, then the production portal (stage 5), moves its data
#              there and locks itself. Needs --licence-key and --portal-name <the production portal's FQDN>; the key
#              and the name are checked with my.claugine.com first, nothing is registered (no installation id, no
#              check-in, no updates). --site-url is optional (default https://<this VM's address>).
#   --portal-name <fqdn>   with --init: the production portal this init prepares
#   --from-init           set by portal/init's stage 5: an Enterprise installation may start with one node, the
#              second is added later from the production portal (owner 2026-11-27 QB6-3)
#   --release <vNN.NN.NN>  with --from-init: the release portal/init runs - the installation stops before anything is
#              installed when my.claugine.com offers another one (stage 6 moves the data between the same release)
#   --ssh-port <port>     the ssh port the second VM must reach this one on (default 22; the site's ssh port)
#
#   --second <user>@<address>   the second node of an Enterprise installation (owner 2026-11-10). Required with an
#              Enterprise licence: an Enterprise portal is two VMs, and **nothing starts until the second VM is ready**.
#              This VM sets the other one up itself, over ssh with a key of its own: run once without the key in
#              place and the script prints the line to add to <user>'s ~/.ssh/authorized_keys there, then stops
#              before changing anything. The other VM needs only a clean Ubuntu 26.04 and that user with
#              passwordless sudo. There is no separate "join": the second node is always provisioned from here.
#   --site <code>, --second-site <code>   the two nodes' site codes (default site1 / site2)
#
# Requirements of the VM (Ubuntu 26.04):
#   * you log in over ssh as an ordinary user who is in sudoers **with NOPASSWD**, so nothing here ever prompts for
#     a password. Every privileged step below is a single command prefixed with `sudo`; the script itself never runs
#     as root, so what it writes under ~/claugine stays yours.
#   * outbound HTTPS to my.claugine.com (and to the Ubuntu and Python package mirrors).
#
# A licence is required: the script asks my.claugine.com to bind this installation and **stops if it will not**.
# Which edition you get is the control plane's answer — never a choice made here. Either way the release comes as a
# bundle from my.claugine.com, authorised by the licence key, whose SHA-256 and signature are verified **before** it is
# unpacked (v01.00.00: nothing is cloned from git). Every call to my.claugine.com is encrypted under a key only this
# installation and my.claugine.com hold (T37, API v2; this script needs Python's cryptography and installs Ubuntu's
# python3-cryptography when it is missing). A dry run stops before the activation, which binds the licence (QSC-4).
#
# --dry-run prints every privileged command instead of running it, and touches nothing.
set -Eeuo pipefail

CHECKIN_URL="https://my.claugine.com"
MODE="" LICENCE_KEY="" SITE_URL="" LE_EMAIL="" BACKUP="" GIVEN_INSTALL_ID="" DRY=0
LB_STATUS_PORT=8087   # the role endpoint (GET /active …) once HAProxy takes 80 and 443 here (as claugine-add-node.sh)
SECOND="" SITE_CODE="" SECOND_SITE="" DEV_SRC=""
PORTAL_NAME="" FROM_INIT=0 SSH_PORT=22 INIT_FOR="" WANT_RELEASE=""
RELEASE_RE='^v[0-9]{2}\.[0-9]{2}\.[0-9]{2}$'   # commit-611: vNN.NN.NN, the form of every release tag
WORK="$HOME/claugine"
SRC=""            # where the code ends up
PW_FILE=""        # the archive password for --restore, readable only by this user, removed on the way out
CH_DIR=""         # the channel module, the server keys and the new channel key for this run (T37), removed on the way out
cleanup() {
  [[ -z "$PW_FILE" ]] || { rm -f "$PW_FILE"; PW_FILE=""; }
  [[ -z "$CH_DIR" ]] || { rm -rf "$CH_DIR"; CH_DIR=""; }
}
trap cleanup EXIT INT TERM

say()  { printf '\n== %s\n' "$*"; }
info() { printf '   %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }
log()  { info "$*"; }   # the shared fetch_bundle below logs with log(); here that is info()

# ---- fetch_bundle: keep this byte-identical to the copy in deploy/claugine-install.sh ----------------------
# It reads CHECKIN_URL, LICENCE_KEY and INSTALL_ID (or INIT_FOR) from the environment so both copies are the same text; each
# script sets them its own way. T37 SC4: the download itself is channel_get <version> <file>, which each script defines
# (the installer: the channel module it carries; the updater: the portal's own key through claugine-manage-root): it
# prints the HTTP status, then the answer (JSON), and exits 0 on an answer, 3 when the bundle failed its SHA-256 or its
# signature (nothing kept), else 2. accounts/tests_bundle_mode.py runs this exact function from *both* files against
# the same fixtures and fails if they differ.
fetch_bundle() {  # <version> <dest dir> - the SHA-256 and the signature are checked BEFORE unpacking
  local ver="$1" dest="$2" tmp out rc=0 code err why
  [[ -n "${CHECKIN_URL:-}" && -n "${LICENCE_KEY:-}" && ( -n "${INSTALL_ID:-}" || -n "${INIT_FOR:-}" ) ]] \
    || die "a release download needs the control-plane URL, the licence key and the installation id"
  tmp="$(mktemp -d /tmp/claugine-bundle.XXXXXX)"; chmod 700 "$tmp"
  out="$(channel_get "$ver" "$tmp/bundle.tgz" 2>"$tmp/err")" || rc=$?
  why="$(tail -c 400 "$tmp/err" 2>/dev/null | tr '\n' ' ')"
  if [[ $rc -eq 3 ]]; then
    rm -rf "$tmp"
    die "the release bundle failed its check: ${why:-no reason given}
Refusing to unpack it; the download has been deleted and nothing was changed."
  fi
  [[ $rc -eq 0 ]] || { rm -rf "$tmp"; die "the release download failed: ${why:-no answer}; nothing was changed."; }
  code="$(printf '%s\n' "$out" | head -1)"
  err="$(printf '%s\n' "$out" | sed -n 2p | python3 -c 'import json,sys; print(json.load(sys.stdin).get("error") or "")' 2>/dev/null || true)"
  if [[ "$err" == not_entitled ]]; then
    rm -rf "$tmp"
    die "${ver} is on hold or not offered to this installation (old releases nobody runs are held by the control
plane). Nothing was changed. Install the release the control plane offers instead (the portal's update does)."
  fi
  if [[ "$err" == withdrawn ]]; then
    rm -rf "$tmp"  # owner 2026-11-18 (u02): a withdrawn release's build is gone; its tags too - no retry helps
    die "${ver} was withdrawn: its build is no longer available. Nothing was changed.
Install the release the control plane offers instead (the portal's update does)."
  fi
  [[ "$code" == 200 && -s "$tmp/bundle.tgz" ]] \
    || { rm -rf "$tmp"; die "the release download failed (HTTP ${code:-none}${err:+ $err}): ${ver}; nothing was changed."; }
  log "SHA-256 and signature verified before unpacking ($(printf '%s\n' "$out" | sed -n 2p | python3 -c 'import json,sys; print(json.load(sys.stdin).get("signed_by") or "?")' 2>/dev/null || echo '?'))"
  tar -xzf "$tmp/bundle.tgz" -C "$dest" --strip-components=1 || { rm -rf "$tmp"; die "the release bundle could not be unpacked"; }
  rm -rf "$tmp"
  [[ -f "$dest/deploy/install.sh" ]] || die "the fetched release has no deploy/install.sh — refusing to go on"
}

# ---- the secure channel (T37 SC3/SC4, API v2; owner 2026-11-27 16:01-16:35) ---------------------------------------
# Every call to my.claugine.com goes through the channel module, encrypted under a key only this installation and
# my.claugine.com hold, and every bundle is signed. This script is downloaded on its own, so it carries the module
# (deploy/server/claugine_channel.py) and the server keys of its release (deploy/server-keys.json) verbatim between
# the markers below - accounts/tests_channel.py keeps both equal to their files. They are written to a private folder
# for this run; the channel key the activation makes is handed to the portal's database (QSC-2) and the folder goes.
channel_init() {
  [[ -z "$CH_DIR" ]] || return 0
  CH_DIR="$(mktemp -d /tmp/claugine-channel.XXXXXX)"; chmod 700 "$CH_DIR"
  cat > "$CH_DIR/claugine_channel.py" <<'CLAUGINE_CHANNEL'
#!/usr/bin/env python3
"""The secure channel with my.claugine.com (API v2; T37, claugine-web T98, contract project/licensing.md at e2ad180;
owner 2026-11-27 16:01-16:35).

Inside HTTPS every call of an installation (and of portal/init) is encrypted and authenticated with a key only it and
my.claugine.com hold, and every bundle is signed:

    the channel key  the node makes an X25519 pair `ik` and agrees K with the newest server channel key it carries
                     (`next`, else `current`): K = HKDF-SHA256(X25519(ik, S), salt = install_id,
                     info = "claugine/v2 channel|<S.id>|<b64 ik.public>"); kid = "k-" + 16 hex of SHA-256(ik.public);
                     per direction Kc / Ks = HKDF(K, info "claugine/v2 c2s" / "claugine/v2 s2c"). K never travels.
    a request        {"v": 2, "install_id", "kid" | "sid"+"epk", "ts", "nonce", "ct"}: ChaCha20-Poly1305 under Kc of
                     the v1 body, the AAD binding path, install id, key, time and nonce
    an answer        {"v": 2, "kid", "ts", "nonce", "ct"} under Ks; ts within 300 s; an answer that does not open is
                     dropped; a clear answer is never trusted - only key_unknown / key_expired lead to a new activation
    a bundle         X-Content-Signature "<g-id>:<b64 Ed25519>" over "claugine/v2 bundle\\n<edition>\\n<version>\\n
                     <sha256 hex>", checked against a signing key the release carries (never fetched at run time)

One implementation for the portal (imported by accounts/channel.py) and for the shell callers: the installer carries
this file verbatim between its CLAUGINE_CHANNEL markers (accounts/tests_channel.py keeps the two equal), activate.sh
and the updater run it with python3. Needs only the standard library and `cryptography`.
"""

import argparse
import base64
import contextlib
import hashlib
import json
import os
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.request

WINDOW = 300  # seconds either side of the other end's clock
CHUNK = 1 << 20  # commit-640/643: a bundle is read in reads of at most this many bytes
PREFIX = "/api/v2"


class ChannelError(Exception):
    """The call did not give a trusted answer (network, an answer that does not open, a bad signature)."""


class BadBundle(ChannelError):
    """SC4: a bundle that came but fails its SHA-256 or its signature, or cannot be kept: nothing is installed."""


class Refused(ChannelError):
    """A clear refusal of my.claugine.com before the key is known: {"error", "message"}; nothing in it is trusted, it
    only says whether to activate again (key_unknown, key_expired) or to retry later. A download's refusal that opens
    under the key (not_entitled, withdrawn: SC4) is raised as one too, so a caller has one way to read either."""

    def __init__(self, status, error, message=""):
        super().__init__(f"HTTP {status} {error}: {message}".strip())
        self.status, self.error, self.message = status, error, message

    @property
    def reactivate(self):
        return self.error in ("key_unknown", "key_expired")


# ---- encodings and ids ----------------------------------------------------------------------------------------------


def b64e(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def b64d(s):
    s = s.strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def key_id(prefix, public, n=8):
    return f"{prefix}-{hashlib.sha256(public).hexdigest()[:n]}"


def kid_of(public):
    return key_id("k", public, 16)


# ---- the server keys a release carries --------------------------------------------------------------------------


def parse_keys(obj):
    """{"channel": {slot: {"id", "public"}}, "signing": {...}} from GET /api/v2/keys' shape; each id is checked against
    its public key. ChannelError when it is not usable (no current channel or signing key)."""
    out = {}
    for kind, prefix in (("channel", "s"), ("signing", "g")):
        out[kind] = {}
        for k in (obj or {}).get(kind) or []:
            pub = b64d(k["public"])
            if len(pub) != 32 or key_id(prefix, pub) != k["id"]:
                raise ChannelError(f"the carried {kind} key {k.get('id')} does not match its public key")
            out[kind][k["slot"]] = {"id": k["id"], "public": pub}
        if "current" not in out[kind]:
            raise ChannelError(f"no current {kind} key carried")
    return out


def load_keys(path):
    with open(path, encoding="utf-8") as fh:
        return parse_keys(json.load(fh))


def newest(keys):
    """The server channel key a new channel key is agreed with: next when carried, else current."""
    ch = keys["channel"]
    return ch.get("next") or ch["current"]


# ---- the crypto -------------------------------------------------------------------------------------------------


def _x25519():
    from cryptography.hazmat.primitives.asymmetric import x25519

    return x25519


def _raw(k):
    from cryptography.hazmat.primitives import serialization as s

    if hasattr(k, "private_bytes"):
        return k.private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption())
    return k.public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)


def new_pair():
    """(private, public) of a new X25519 key pair, raw 32 bytes each."""
    priv = _x25519().X25519PrivateKey.generate()
    return _raw(priv), _raw(priv.public_key())


def public_of(private):
    return _raw(_x25519().X25519PrivateKey.from_private_bytes(private).public_key())


def hkdf(ikm, info, salt=None, n=32):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=n, salt=salt, info=info.encode()).derive(ikm)


def channel_key(ik_private, s_id, s_public, install_id):
    """K agreed with the server channel key S."""
    x = _x25519()
    shared = x.X25519PrivateKey.from_private_bytes(ik_private).exchange(x.X25519PublicKey.from_public_bytes(s_public))
    info = f"claugine/v2 channel|{s_id}|{b64e(public_of(ik_private))}"
    return hkdf(shared, info, salt=install_id.encode())


def directions(k):
    return hkdf(k, "claugine/v2 c2s"), hkdf(k, "claugine/v2 s2c")


def _aead(key):
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    return ChaCha20Poly1305(key)


def request_aad(path, install_id, ident, ts, nonce):
    who = ident["kid"] if "kid" in ident else f"{ident['sid']}|{ident['epk']}"
    return f"claugine/v2 request|{path}|{install_id}|{who}|{ts}|{nonce}".encode()


def answer_aad(path, install_id, kid, req_nonce, ts):
    return f"claugine/v2 answer|{path}|{install_id}|{kid}|{req_nonce}|{ts}".encode()


def seal(path, install_id, plain, k, ident, ts=None, nonce=None):
    """The request envelope of `plain` (the v1 body) under K; ident {"kid"} or {"sid", "epk"}."""
    ts = int(time.time()) if ts is None else int(ts)
    nonce = b64e(nonce if nonce is not None else secrets.token_bytes(12))
    kc, _ks = directions(k)
    data = json.dumps(plain, separators=(",", ":")).encode()
    ct = _aead(kc).encrypt(b64d(nonce), data, request_aad(path, install_id, ident, ts, nonce))
    return {"v": 2, "install_id": install_id, **ident, "ts": ts, "nonce": nonce, "ct": b64e(ct)}


def open_answer(path, install_id, kid, req_nonce, answer, k, now=None):
    """The v1 answer inside an encrypted answer; ChannelError when it does not open or is out of the window."""
    from cryptography.exceptions import InvalidTag

    now = int(time.time()) if now is None else int(now)
    if not isinstance(answer, dict):  # commit-639
        raise ChannelError(f"an answer that is not a JSON object ({type(answer).__name__}): dropped")
    try:
        if answer.get("v") != 2 or answer.get("kid") != kid:
            raise ChannelError(f"an answer for another key ({answer.get('kid')}, not {kid}): dropped")
        ts = int(answer["ts"])
        if abs(now - ts) > WINDOW:
            raise ChannelError(f"an answer {now - ts} s off this node's clock: dropped")
        _kc, ks = directions(k)
        plain = _aead(ks).decrypt(
            b64d(answer["nonce"]), b64d(answer["ct"]), answer_aad(path, install_id, kid, req_nonce, ts)
        )
        data = json.loads(plain.decode())  # commit-639: what opens must be a JSON object too
    except (KeyError, TypeError, ValueError, InvalidTag) as exc:
        raise ChannelError(f"an answer that does not open: dropped ({type(exc).__name__})") from exc
    if not isinstance(data, dict):
        raise ChannelError(f"an answer whose content is not a JSON object ({type(data).__name__}): dropped")
    return data


def bundle_message(edition, version, sha_hex):
    return f"claugine/v2 bundle\n{edition}\n{version}\n{sha_hex}".encode()


def verify_bundle(keys, edition, version, sha_hex, header):
    """The bundle's X-Content-Signature checked against a carried signing key; returns its key id, else ChannelError."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric import ed25519

    gid, _sep, sig = (header or "").strip().partition(":")
    if not gid or not sig:
        raise BadBundle("the bundle carries no signature (X-Content-Signature): not installed")
    key = next((k for k in keys["signing"].values() if k["id"] == gid), None)
    if key is None:
        raise BadBundle(f"the bundle is signed with {gid}, a key this release does not carry: not installed")
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(key["public"]).verify(
            b64d(sig), bundle_message(edition, version, sha_hex)
        )
    except (InvalidSignature, ValueError) as exc:
        raise BadBundle(f"the bundle's signature ({gid}) does not verify: not installed") from exc
    return gid


# ---- the node's key ---------------------------------------------------------------------------------------------


def fresh(keys, install_id):
    """A new channel key agreed with the newest carried server key: {"kid", "k", "priv", "sid", "epk"} (b64)."""
    priv, pub = new_pair()
    s = newest(keys)
    k = channel_key(priv, s["id"], s["public"], install_id)
    return {"kid": kid_of(pub), "k": b64e(k), "priv": b64e(priv), "sid": s["id"], "epk": b64e(pub)}


# ---- HTTP ---------------------------------------------------------------------------------------------------------


def _http(req, timeout):
    """(status, headers, body bytes); ChannelError on no answer at all."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - our own control plane over https
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except Exception as exc:  # noqa: BLE001 - network, timeout, TLS: no answer
        raise ChannelError(f"no answer: {str(exc)[:200]}") from exc


def _answer(path, install_id, kid, nonce, status, body, k):
    try:
        data = json.loads(body.decode() or "{}")
    except ValueError as exc:
        raise ChannelError(f"HTTP {status}: an answer that is not JSON: dropped") from exc
    if not isinstance(data, dict):  # commit-639
        raise ChannelError(f"HTTP {status}: an answer that is not a JSON object: dropped")
    if "ct" not in data and data.get("error"):
        raise Refused(status, str(data.get("error")), str(data.get("message") or "")[:300])
    return status, open_answer(path, install_id, kid, nonce, data, k)


def post(base_url, name, install_id, plain, k, ident, kid, timeout=25):
    """One encrypted call: POST <base>/api/v2/<name>; returns (HTTP status, the v1 answer). Refused / ChannelError."""
    path = f"{PREFIX}/{name}"
    env = seal(path, install_id, plain, k, ident)
    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=json.dumps(env).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    status, _h, body = _http(req, timeout)
    return _answer(path, install_id, kid, env["nonce"], status, body, k)


def call(base_url, name, install_id, plain, key, timeout=25):
    """A call under the node's channel key {"kid", "k"}."""
    return post(base_url, name, install_id, plain, b64d(key["k"]), {"kid": key["kid"]}, key["kid"], timeout)


def activate(base_url, keys, install_id, plain, timeout=25):
    """POST /api/v2/activate with a new channel key; returns (status, answer, key {"kid", "k", "expires"}) - the key
    only when the activation was accepted (2xx and its answer names that kid)."""
    new = fresh(keys, install_id)
    status, ans = post(
        base_url,
        "activate",
        install_id,
        plain,
        b64d(new["k"]),
        {"sid": new["sid"], "epk": new["epk"]},
        new["kid"],
        timeout,
    )
    key = None
    if 200 <= status < 300:
        got = ans.get("key")
        # commit-638: an accepted activation must name the key it was made with - else nothing is taken as activated
        if not isinstance(got, dict) or got.get("kid") != new["kid"]:
            raise ChannelError(
                f"HTTP {status}: the activation was answered without its key ({new['kid']}): not activated"
            )
        try:
            expires = int(got.get("expires") or 0)
        except (TypeError, ValueError) as exc:
            raise ChannelError(f"HTTP {status}: the activation's key has no valid expiry: not activated") from exc
        key = {"kid": new["kid"], "k": new["k"], "expires": expires}
    return status, ans, key


def one_off(base_url, keys, name, portal_name, plain, timeout=25):
    """portal/init's calls: a one-off key, nothing stored (install_id in the envelope is the production name)."""
    new = fresh(keys, portal_name)
    return post(
        base_url, name, portal_name, plain, b64d(new["k"]), {"sid": new["sid"], "epk": new["epk"]}, new["kid"], timeout
    )


def download(base_url, keys, version, install_id, plain, out_path, edition, key=None, timeout=600):
    """GET /api/v2/release/<version> into out_path (0600): the SHA-256 of the bytes, then the signature against a
    carried signing key; nothing is kept that fails. key: the node's {"kid", "k"}, or None for portal/init (one-off).
    Returns the signing key id."""
    path = f"{PREFIX}/release/{version}"
    if key is None:
        new = fresh(keys, install_id)
        k, ident, kid = b64d(new["k"]), {"sid": new["sid"], "epk": new["epk"]}, new["kid"]
    else:
        k, ident, kid = b64d(key["k"]), {"kid": key["kid"]}, key["kid"]
    env = seal(path, install_id, plain, k, ident)
    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        method="GET",
        headers={"X-Claugine-Request": b64e(json.dumps(env).encode())},
    )
    try:  # commit-642: the answer first - a refusal is never hidden by the destination
        r = urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - our own control plane over https
    except urllib.error.HTTPError as e:
        st, ans = _answer(path, install_id, kid, env["nonce"], e.code, e.read(), k)  # a clear refusal raises Refused
        # SC4: a refusal under the key (not_entitled, withdrawn) names its error - the caller says what it means
        raise Refused(st, str(ans.get("error") or "no bundle"), str(ans.get("message") or "")[:300]) from e
    except Exception as exc:  # noqa: BLE001 - network, timeout, TLS: no answer
        raise ChannelError(f"no bundle: {str(exc)[:200]}") from exc
    with r:
        status, headers = r.status, {k2.lower(): v for k2, v in r.headers.items()}
        if not 200 <= status < 300:
            raise ChannelError(f"HTTP {status}: no bundle")
        try:
            fd, tmp = tempfile.mkstemp(prefix=".bundle-", dir=os.path.dirname(os.path.abspath(out_path)))
        except OSError as exc:
            raise BadBundle(f"the bundle cannot be written beside {out_path}: {exc}") from exc
        # commit-640: streamed into the private file, hashed as it comes, in bounded reads; put in place only once its
        # SHA-256 and signature hold, removed otherwise
        try:
            sha = hashlib.sha256()
            try:
                with os.fdopen(fd, "wb") as fh:
                    while True:
                        try:  # commit-644: a read that fails is the transfer's, never the disk's
                            chunk = r.read(CHUNK)
                        except Exception as exc:  # noqa: BLE001 - reset, timeout, TLS
                            raise ChannelError(f"the download broke off: {str(exc)[:200]}") from exc
                        if not chunk:
                            break
                        sha.update(chunk)
                        fh.write(chunk)
            except OSError as exc:  # the staging file's own write or close
                raise BadBundle(f"the bundle could not be written: {exc}") from exc
            got = sha.hexdigest()
            want = (headers.get("x-content-sha256") or "").strip().lower()
            if want and want != got:
                raise BadBundle(f"the bundle's SHA-256 is {got}, not the {want} it was sent with: not installed")
            gid = verify_bundle(keys, edition, version, got, headers.get("x-content-signature"))
            try:
                os.chmod(tmp, 0o600)
                os.replace(tmp, out_path)
            except OSError as exc:
                raise BadBundle(f"the bundle could not be put in place: {exc}") from exc
            return gid
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


# ---- the shell callers --------------------------------------------------------------------------------------------


def _read_key(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write_key(path, key):
    """commit-641: a unique 0600 file beside the key, written and closed, then put in its place; removed on failure."""
    fd, tmp = tempfile.mkstemp(prefix=".channel-key-", dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(key, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def main(argv=None):
    """CLI for the installer, activate.sh and the updater. The v1 body comes on stdin; prints "<HTTP status>" on the
    first line and the opened v1 answer (JSON) after it; a clear refusal prints its status and {"error", "message"}.
    Exit 0 when an answer came (whatever its status), 2 when none did (it says why on stderr), 3 on a bad bundle
    (BadBundle: its SHA-256, its signature, or it could not be kept)."""
    ap = argparse.ArgumentParser(prog="claugine_channel.py")
    ap.add_argument("action", choices=("activate", "call", "init", "download"))
    ap.add_argument("--url", required=True)
    ap.add_argument("--keys", required=True, help="the server keys this release carries (server-keys.json)")
    ap.add_argument("--install-id", default="")
    ap.add_argument("--name", default="checkin", help="call / init: the call after /api/v2/")
    ap.add_argument("--key-file", default="", help="the node's channel key (activate writes it, call reads it)")
    ap.add_argument("--version", default="")
    ap.add_argument("--edition", default="")
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    try:
        keys = load_keys(a.keys)
        plain = json.loads(sys.stdin.read() or "{}")
        if a.action == "activate":
            status, ans, key = activate(a.url, keys, a.install_id, plain)
            if key is not None:
                _write_key(a.key_file, key)
        elif a.action == "call":
            status, ans = call(a.url, a.name, a.install_id, plain, _read_key(a.key_file))
        elif a.action == "init":
            status, ans = one_off(a.url, keys, a.name, a.install_id, plain)
        else:  # download
            key = _read_key(a.key_file) if a.key_file else None
            gid = download(a.url, keys, a.version, a.install_id, plain, a.out, a.edition, key=key)
            status, ans = 200, {"signed_by": gid}
    except Refused as exc:
        print(exc.status)
        print(json.dumps({"error": exc.error, "message": exc.message}))
        return 0
    except ChannelError as exc:
        print(f"claugine_channel: {exc}", file=sys.stderr)
        return 3 if isinstance(exc, BadBundle) else 2
    print(status)
    print(json.dumps(ans))
    return 0


if __name__ == "__main__":
    sys.exit(main())
CLAUGINE_CHANNEL
  cat > "$CH_DIR/server-keys.json" <<'CLAUGINE_KEYS'
{
  "source": "https://my.claugine.com/api/v2/keys",
  "read": "2026-11-27 17:50 +0500 (claugine-web 71cb670)",
  "channel": [
    {
      "id": "s-49892197",
      "public": "rSLmOkiDkOAClO8DQf2X-tK46iVj-rMC4VyWwlmiHHk",
      "slot": "current"
    },
    {
      "id": "s-548d3542",
      "public": "00J1skgrFXFImLi_ZiRl8S9zbN6o1aXUtTgVw81PSWA",
      "slot": "next"
    }
  ],
  "signing": [
    {
      "id": "g-1bcf5aed",
      "public": "yGdZslj3DUI9qf3c2wIOr_oEtloH8Mnvp-MU1HibJZ0",
      "slot": "current"
    },
    {
      "id": "g-c2644ec0",
      "public": "9JRy68UTx63ZtvVfvLLkhgklv2qfgugsLr_KpUZ9bLE",
      "slot": "next"
    }
  ]
}
CLAUGINE_KEYS
}
# ch_body <name> <value> ... : the v1 body as JSON, with the licence key from the environment (never on a command
# line); a value starting with [ or { is taken as JSON
ch_body() {
  CLG_LK="$LICENCE_KEY" python3 -c '
import json, os, sys
a = sys.argv[1:]
d = {"licence_key": os.environ["CLG_LK"]}
for k, v in zip(a[::2], a[1::2]):
    d[k] = json.loads(v) if v[:1] in ("[", "{") else v
print(json.dumps(d))' "$@"
}
# ch <action> [options] < body (never in a pipeline: it sets this shell's variables): CH_CODE (the HTTP status) and CH_ANS (the opened answer, or a clear refusal's
# {"error", "message"}); returns 2 with CH_WHY when no trusted answer came
ch() {
  local out rc=0
  CH_CODE="" CH_ANS="" CH_WHY=""
  out="$(python3 "$CH_DIR/claugine_channel.py" "$@" --url "$CHECKIN_URL" --keys "$CH_DIR/server-keys.json" \
         2>"$CH_DIR/err")" || rc=$?
  CH_WHY="$(tail -c 400 "$CH_DIR/err" | tr '\n' ' ')"
  [[ $rc -eq 0 ]] || return 2
  CH_CODE="$(printf '%s\n' "$out" | head -1)"; CH_ANS="$(printf '%s\n' "$out" | sed -n 2p)"
}
ch_msg() {  # what a refusal says
  printf '%s' "$CH_ANS" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("message") or d.get("detail") or d.get("error") or "")' 2>/dev/null || true
}
channel_get() {  # <version> <out file>: the shared fetch_bundle's download - an installation under its new key,
  local -a who=(--install-id "${INSTALL_ID:-$INIT_FOR}")  # portal/init (no installation) under a one-off key
  [[ -z "${INSTALL_ID:-}" ]] || who+=(--key-file "$CH_DIR/channel.key")
  if [[ -n "${INSTALL_ID:-}" ]]; then ch_body install_id "$INSTALL_ID" version "$1"
  else ch_body portal_name "$INIT_FOR" version "$1"; fi \
    | python3 "$CH_DIR/claugine_channel.py" download --url "$CHECKIN_URL" --keys "$CH_DIR/server-keys.json" \
        "${who[@]}" --version "$1" --edition "$EDITION" --out "$2"
}

# ---- check_backup_edition (core-024; owner 2026-11-08: "restore possible only to the same installation - standard
# or enterprise. standard cant be restore to enterprise and vise versa."). A backup records its edition in the file
# `edition` inside the encrypted archive (accounts/appliance.make_backup); it is read with the password already
# checked, before anything is written, and a mismatch with the licence's edition stops here. A backup made before
# editions were recorded has no such file: it is accepted and said to be a legacy backup. deploy/restore.sh checks
# the same file again. accounts/tests_backups.BackupEdition runs this function.
# commit-065: a portal.env's FIELD_ENCRYPTION_KEY holds Fernet keys, read as portal/settings.py reads it (the first
# assignment, trimmed, comma-separated, empty items dropped): each one 44 characters of Base64, either alphabet
# (commit-072: as accounts/appliance.FERNET_KEY, which a backup is checked with). The same function is in
# deploy/restore.sh and deploy/claugine-install.sh.
fernet_keys_ok() {  # <portal.env>
  local line value k n=0 re='^[A-Za-z0-9+/_-]{43}=$'
  local -a keys
  line="$(grep -m1 -E '^[[:space:]]*FIELD_ENCRYPTION_KEY[[:space:]]*=' "$1" 2>/dev/null)" || return 1
  value="${line#*=}"
  IFS=',' read -ra keys <<<"$value"
  for k in "${keys[@]}"; do
    k="$(sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' <<<"$k")"
    [[ -z "$k" ]] && continue
    [[ "$k" =~ $re ]] || return 1
    n=$((n + 1))
  done
  ((n > 0))
}

check_backup_edition() {  # <backup .7z or folder> <password file> <the licence's edition>
  local b="$1" pw="$2" want="$3" d got="" env=0
  if [[ -f "$b" ]]; then
    d="$(mktemp -d)"; chmod 700 "$d"
    if ! 7z x -bso0 -bsp0 -bse0 -y -o"$d" "$b" edition portal.env < "$pw" >/dev/null 2>&1 && [[ ! -f "$d/edition" ]]; then
      rm -rf "$d"; die "could not read the backup's edition from $b. Nothing has been installed."
    fi
    [[ ! -f "$d/edition" ]] || got="$(head -c 64 "$d/edition" | tr -d '[:space:]')"
    fernet_keys_ok "$d/portal.env" && env=1
    rm -rf "$d"
  elif [[ -f "$b/edition" ]]; then
    got="$(head -c 64 "$b/edition" | tr -d '[:space:]')"
    fernet_keys_ok "$b/portal.env" && env=1
  fi
  if [[ -z "$got" ]]; then
    info "a legacy backup (made before backups recorded their edition): accepted"
    return 0
  fi
  [[ "$got" == standard || "$got" == enterprise ]] || die "the backup names an unknown edition (${got}). Nothing has been installed."
  [[ "$got" == "$want" ]] || die "the backup is of a ${got^} installation and this licence is ${want^}: a backup restores only
to an installation of the same edition. Nothing has been installed."
  # core-048: a backup of this format holds portal.env with the key to the stored secrets; without it the restored
  # secrets could not be read - refused before anything is installed
  ((env)) || die "the backup has no portal.env with a valid FIELD_ENCRYPTION_KEY (the key to the stored secrets): it cannot be
restored with its secrets readable. Nothing has been installed."
  info "the backup is of a ${got^} installation, as this licence"
}

# accounts/tests_bundle_mode.py sources this file to exercise fetch_bundle alone; set only by the test.
[[ "${CLAUGINE_UPDATE_SOURCE_ONLY:-}" == 1 ]] && return 0 || true


# Every privileged step goes through this one function, so there is exactly one place where root is used.
priv() {
  if [[ $DRY -eq 1 ]]; then
    # never echo the licence key, not even in a dry run: this output is what gets pasted into a ticket
    printf '   [dry-run] sudo %s\n' "${*//${LICENCE_KEY:-__nokey__}/CLG-********}"
    return 0
  fi
  sudo -n "$@"
}

# The header above is the usage text: print it up to the first line that is not a comment.
usage() { sed -n '2,/^[^#]/p' "$0" | sed -n 's/^#\( \|$\)//p'; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --new|--restore|--init) [[ -z "$MODE" ]] || die "give only one of --new, --restore, --init"
                         MODE="${1#--}"; shift ;;
    --portal-name)       PORTAL_NAME="$(printf '%s' "$2" | sed -E 's#^[A-Za-z]+://##; s#[:/].*$##; s#\.$##' | tr '[:upper:]' '[:lower:]')"; shift 2 ;;
    --from-init)         FROM_INIT=1; shift ;;
    --release)           WANT_RELEASE="$2"; shift 2 ;;
    --ssh-port)          SSH_PORT="$2"; shift 2 ;;
    --join)              die "--join is gone (2026-11-10): the second node is set up from the first one.
Run on the first VM: claugine-install.sh --new ... --second <user>@<this VM's address>" ;;
    --second)            SECOND="$2"; shift 2 ;;
    --site)              SITE_CODE="$2"; shift 2 ;;
    --second-site)       SECOND_SITE="$2"; shift 2 ;;
    --src)               DEV_SRC="${2%/}"; shift 2 ;;   # development: install this tree instead of fetching one
    --licence-key)       LICENCE_KEY="$2"; shift 2 ;;
    --site-url)          SITE_URL="${2%/}"; shift 2 ;;
    --letsencrypt-email) LE_EMAIL="$2"; shift 2 ;;
    --backup)            BACKUP="$2"; shift 2 ;;
    --install-id)        GIVEN_INSTALL_ID="$2"; shift 2 ;;
    --checkin-url)       CHECKIN_URL="${2%/}"; shift 2 ;;
    --dry-run)           DRY=1; shift ;;
    -h|--help)           usage ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

# ---------------------------------------------------------------- preflight, before anything is changed
say "Checking this machine"
[[ -n "$MODE" ]]        || usage
[[ $EUID -ne 0 ]]       || die "do not run this as root. Log in as your ordinary user; each privileged step uses sudo."
[[ -n "$LICENCE_KEY" ]] || die "--licence-key is required. Register at ${CHECKIN_URL} to get a free Standard licence."
[[ -n "$SITE_URL" || "$MODE" == init ]] || die "--site-url is required, e.g. --site-url https://portal.example.com"
if [[ "$MODE" == init ]]; then
  [[ -n "$PORTAL_NAME" ]] || die "--init needs --portal-name <the production portal's FQDN>, e.g. --portal-name portal.example.com"
  [[ "$PORTAL_NAME" =~ ^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}$ ]] \
    || die "--portal-name ${PORTAL_NAME} is not a fully qualified domain name"
  [[ -z "$SECOND" && -z "$BACKUP" && $FROM_INIT -eq 0 && -z "$GIVEN_INSTALL_ID" ]] \
    || die "--init takes none of --second, --backup, --from-init, --install-id: portal/init is one VM and no installation"
else
  [[ -z "$PORTAL_NAME" ]] || die "--portal-name goes with --init only"
fi
# commit-625: the release portal/init runs, for its stage 5 only
if [[ -n "$WANT_RELEASE" ]]; then
  [[ $FROM_INIT -eq 1 && "$MODE" == new ]] || die "--release goes with --new --from-init only (portal/init's stage 5)"
  [[ "$WANT_RELEASE" =~ $RELEASE_RE ]] || die "--release wants a release of the form vNN.NN.NN"
fi
# commit-612: read as decimal (a leading 0 is not octal), and the normalized number is what the probe uses
[[ "$SSH_PORT" =~ ^[0-9]{1,5}$ ]] || die "--ssh-port wants a port number"
SSH_PORT=$((10#$SSH_PORT))
((SSH_PORT >= 1 && SSH_PORT <= 65535)) || die "--ssh-port wants a port number"
[[ "$MODE" != "restore" || -n "$BACKUP" ]] || die "--restore needs --backup <the backup's .7z archive, or its folder>"
[[ -z "$SECOND" || "$SECOND" == *@* ]] || die "--second wants <user>@<address> of the other VM"

for c in curl tar sha256sum python3; do command -v "$c" >/dev/null || die "$c is not installed"; done
if [[ $DRY -eq 0 ]]; then
  sudo -n true 2>/dev/null || die "sudo asked for a password. This user must be in sudoers with NOPASSWD (see the header)."
fi
if [[ -r /etc/os-release ]]; then
  . /etc/os-release
  [[ "${ID:-}" == "ubuntu" ]] || info "warning: this is built and tested for Ubuntu 26.04; found ${PRETTY_NAME:-unknown}"
fi
[[ -z "$BACKUP" || ! -e "$BACKUP" ]] || BACKUP="$(realpath "$BACKUP")"  # before the cd below
mkdir -p "$WORK"; cd "$WORK"
if [[ $DRY -eq 1 ]]; then info "working in $WORK as $(id -un) - dry run, so sudo was not checked"
else info "working in $WORK as $(id -un), sudo without a password: yes"; fi

# T37 SC3 (owner 2026-11-27 QSC-3): the channel needs Python's cryptography before the first call to my.claugine.com
# and long before the portal's own Python exists - Ubuntu's python3-cryptography, installed here when it is missing
channel_init
CH_PROBE='from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305'
if ! python3 -c "$CH_PROBE" 2>/dev/null; then
  info "installing python3-cryptography: every call to ${CHECKIN_URL} is encrypted, and the installer needs it for that"
  priv env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y -q python3-cryptography >/dev/null 2>&1 \
    || { priv apt-get -o DPkg::Lock::Timeout=600 update -q >/dev/null; priv env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y -q python3-cryptography >/dev/null 2>&1; } \
    || die "python3-cryptography could not be installed (apt-get install python3-cryptography). Nothing else was changed."
  [[ $DRY -eq 1 ]] || python3 -c "$CH_PROBE" 2>/dev/null \
    || die "python3 still cannot load cryptography's X25519, Ed25519 and ChaCha20-Poly1305. Nothing else was changed."
fi

# The archive password is asked **now**, before a single thing is written, so a restore never stops half way.
ARCHIVE_PASSWORD=""
if [[ "$MODE" == "restore" ]]; then
  # Owner 2026-11-12: a backup is <name>.7z (AES-256, names encrypted) and <name>.sh, which runs this script with
  # --backup <name>.7z. A folder of the new format holds those two; older backups are a folder with database.sql.gz
  # and portal.env.zip.
  if [[ -d "$BACKUP" && -f "$BACKUP/$(basename "${BACKUP%/}").7z" ]]; then BACKUP="${BACKUP%/}/$(basename "${BACKUP%/}").7z"; fi
  if [[ -f "$BACKUP" ]]; then
    [[ "$BACKUP" == *.7z ]] || die "$BACKUP is not a backup archive (<name>.7z)"
    BACKUP="$(cd "$(dirname "$BACKUP")" && pwd)/$(basename "$BACKUP")"
    if ! command -v 7z >/dev/null; then
      info "installing 7-Zip to open the backup"
      priv env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y -q 7zip >/dev/null 2>&1 \
        || { priv apt-get -o DPkg::Lock::Timeout=600 update -q >/dev/null; priv env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y -q 7zip >/dev/null 2>&1; }
    fi
  else
    [[ -d "$BACKUP" ]] || die "no such backup: $BACKUP"
    [[ -f "$BACKUP/database.sql.gz" && -f "$BACKUP/portal.env.zip" ]] \
      || die "$BACKUP does not look like a Claugine backup (<name>.7z, or database.sql.gz and portal.env.zip)"
  fi
  if [[ $DRY -eq 0 ]]; then
    for try in 1 2 3; do
      read -rsp "   The backup's archive password: " ARCHIVE_PASSWORD; echo
      [[ -n "$ARCHIVE_PASSWORD" ]] || die "the archive password is needed to open the backup"
      PW_FILE="$(mktemp "$WORK/.archive-password.XXXXXX")"; chmod 600 "$PW_FILE"
      printf '%s' "$ARCHIVE_PASSWORD" > "$PW_FILE"; ARCHIVE_PASSWORD=""
      [[ -f "$BACKUP" ]] || break
      # checked now, before a single thing is written: the whole archive opens with it and is intact
      if 7z t -bso0 -bsp0 -bse0 "$BACKUP" < "$PW_FILE"; then info "the archive opens with this password"; break; fi
      rm -f "$PW_FILE"; PW_FILE=""
      [[ $try -lt 3 ]] || die "the archive does not open with that password (or it is damaged). Nothing was changed."
      info "wrong password, or a damaged archive - try again"
    done
  fi
fi

# commit-609: portal/init goes on a clean VM - an installed portal is refused here, before anything is downloaded or
# installed (install.sh would replace its code and restart it). A portal/init left by an earlier, failed --init run
# (its portal.env says CLAUGINE_MODE=init) is carried on with. activate.sh --init keeps its own guard.
# commit-616: so is a run that failed between install.sh and activate.sh - it left the marker INIT_MARK (root's,
# holding the production portal's name), written after every check passed and before install.sh; the same name
# carries on, anything else is still refused. The marker goes once activate.sh --init has written the mode.
# commit-618: a portal.env with an installation id is a production portal, whatever marker is left - refused first.
INIT_MARK=/etc/claugine-portal/init-attempt
# commit-626: written by a production install or restore as its very last step (after HAProxy and the second node),
# holding the release installed; portal/init's stage 5 takes only this as a finished installation
DONE_MARK=/etc/claugine-portal/install-done
if [[ "$MODE" == init ]]; then
  if sudo -n grep -qE '^CLAUGINE_INSTALL_ID=.+' /etc/claugine-portal/portal.env 2>/dev/null; then
    die "this VM runs an installation of a production portal: portal/init goes on a clean VM. Nothing has been changed on this machine."
  fi
  if sudo -n test -e /etc/claugine-portal/portal.env 2>/dev/null || [[ -e /etc/claugine-portal/portal.env ]] \
     || sudo -n test -d /opt/claugine-portal/app 2>/dev/null || [[ -d /opt/claugine-portal/app ]]; then
    if sudo -n grep -qx 'CLAUGINE_MODE=init' /etc/claugine-portal/portal.env 2>/dev/null; then
      info "portal/init is installed here already (an earlier run): carried on with"
    elif sudo -n grep -qxF "$PORTAL_NAME" "$INIT_MARK" 2>/dev/null; then
      info "an earlier portal/init run for ${PORTAL_NAME} stopped before it finished: carried on with"
    else
      die "this VM already runs a portal: portal/init goes on a clean VM. Nothing has been changed on this machine."
    fi
  fi
fi

# ---------------------------------------------------------------- the second VM, before anything is changed
# Owner 2026-11-10: an Enterprise installation is two VMs and starts only when the second one is ready. This VM
# reaches it with a key of its own (root's, kept for "Replace node" later), so the checks run as root over ssh.
PEER_KEY=/etc/claugine-portal/peer_ssh/id_ed25519
if [[ -n "$SECOND" ]]; then
  say "Checking the second VM ($SECOND)"
  if [[ $DRY -eq 1 ]]; then
    info "[dry-run] would check ssh, sudo, Ubuntu, a clean VM and the link to this one"
  else
    if ! sudo -n test -s "$PEER_KEY"; then
      sudo -n install -d -m 700 -o root -g root /etc/claugine-portal/peer_ssh
      sudo -n ssh-keygen -q -t ed25519 -N "" -C "claugine-primary@$(hostname -s)" -f "$PEER_KEY"
    fi
    S_ADDR="${SECOND#*@}"
    PSSH=(sudo -n ssh -i "$PEER_KEY" -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=10
          -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/etc/claugine-portal/peer_ssh/known_hosts "$SECOND")
    if ! "${PSSH[@]}" true 2>/dev/null; then
      cat >&2 <<MSG
This VM cannot log in to $SECOND yet. Add this line to ~${SECOND%@*}/.ssh/authorized_keys on $S_ADDR,
then run the same command again. Nothing has been changed on either VM.

$(sudo -n cat "$PEER_KEY.pub")
MSG
      exit 1
    fi
    "${PSSH[@]}" sudo -n true 2>/dev/null || die "${SECOND%@*} on $S_ADDR has no passwordless sudo. Nothing was changed."
    s_os="$("${PSSH[@]}" '. /etc/os-release; echo "$ID $VERSION_ID"' 2>/dev/null || true)"
    [[ "$s_os" == ubuntu* ]] || die "$S_ADDR runs ${s_os:-an unknown system}; Ubuntu 26.04 is required. Nothing was changed."
    # A re-run after a failure finds the second VM half set up by this same installation: that is not a foreign
    # portal, so it is carried on with rather than refused.
    my_id="${GIVEN_INSTALL_ID:-$(printf '%s' "$SITE_URL" | sed -E 's#^[A-Za-z]+://##; s#[:/].*$##; s#\.$##' | tr '[:upper:]' '[:lower:]')}"
    s_id="$("${PSSH[@]}" 'sudo -n sed -n "s/^CLAUGINE_INSTALL_ID=//p" /etc/claugine-portal/portal.env 2>/dev/null | tail -1' 2>/dev/null || true)"
    if [[ -z "$my_id" || "$s_id" != "$my_id" ]]; then
      "${PSSH[@]}" 'sudo -n test ! -e /etc/claugine-portal/portal.env && sudo -n test ! -d /opt/claugine-portal/app' \
        || die "$S_ADDR already has a portal: give a clean VM. Nothing was changed."
    fi
    my_addr="$(ip -4 route get "$S_ADDR" 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1)"
    [[ -n "$my_addr" && "$my_addr" != "$S_ADDR" ]] || die "no route from this VM to $S_ADDR. Nothing was changed."
    "${PSSH[@]}" "timeout 5 bash -c '</dev/tcp/$my_addr/$SSH_PORT'" 2>/dev/null \
      || die "$S_ADDR cannot reach this VM ($my_addr, ssh port $SSH_PORT); the two nodes must reach each other. Nothing was changed."
    info "ok: $SECOND logs in, has passwordless sudo, runs $s_os, is clean and reaches this VM ($my_addr)"
  fi
fi

# ---------------------------------------------------------------- the licence decides what is installed
say "Asking ${CHECKIN_URL} about this licence"
if [[ "$MODE" == init ]]; then
  # T36 B6b (owner 2026-11-25 L2, L5): the key and the production portal's name are checked first; nothing is bound,
  # no installation id exists, and portal/init never checks in. The release is the one the key is given (L4), so
  # init and the production portal it installs run the same release and the data move is a plain restore.
  INSTALL_ID=""; INIT_FOR="$PORTAL_NAME"
  if [[ $DRY -eq 1 ]] && ! python3 -c "$CH_PROBE" 2>/dev/null; then
    info "[dry-run] would ask ${CHECKIN_URL} about the key (needs python3-cryptography, not installed in a dry run)"
    info "dry run: nothing was changed."; exit 0
  fi
  ch init --name init/check --install-id "$PORTAL_NAME" < <(ch_body portal_name "$PORTAL_NAME") \
    || die "${CHECKIN_URL} gave no trusted answer: ${CH_WHY}
Nothing has been changed on this machine. Check that this machine can reach ${CHECKIN_URL}."
  init_code="$CH_CODE"; init_ans="$CH_ANS"
  if [[ "$init_code" != 2?? ]]; then
    reason="$(ch_msg)"
    die "the licence or the portal name was refused (HTTP ${init_code}): ${reason:-${init_ans:0:300}}
Nothing has been changed on this machine."
  fi
  read -r INIT_OK EDITION TARGET <<<"$(printf '%s' "$init_ans" | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("yes" if d.get("ok") else "no", d.get("edition") or "-", d.get("target_version") or "-")' 2>/dev/null || echo "no - -")"
  # commit-611: the release names a folder and a URL below - checked before either is used
  [[ "$INIT_OK" == yes && ( "$EDITION" == standard || "$EDITION" == enterprise ) && "$TARGET" =~ $RELEASE_RE ]] \
    || die "${CHECKIN_URL} gave no usable answer for portal/init (${init_ans:0:300}). Nothing has been changed on this machine."
  STATE=active
  info "portal/init for ${PORTAL_NAME} · edition: ${EDITION} · release: ${TARGET} · nothing registered"
fi
if [[ "$MODE" != init ]]; then
  # The installation id is the host of the site URL (owner 2026-11-11), so rebuilding the same portal (new VMs, a
  # restore) is the same installation. --install-id still forces an id.
  FQDN="$(printf '%s' "$SITE_URL" | sed -E 's#^[A-Za-z]+://##; s#[:/].*$##; s#\.$##' | tr '[:upper:]' '[:lower:]')"
  [[ "$FQDN" =~ ^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}$ ]] \
    || die "the installation id is the host of --site-url, and ${FQDN:-that} is not a fully qualified domain name"
  INSTALL_ID="${GIVEN_INSTALL_ID:-$FQDN}"
  info "installation id: ${INSTALL_ID}"
  FP="$( { cat /etc/machine-id 2>/dev/null; hostname; } | sha256sum | cut -c1-32 )"

  if [[ $DRY -eq 1 ]]; then
    # Activation **binds** the licence to this id, first-wins, and a later real run would mint another and be
    # refused with 409. A dry run must therefore not call it (2026-11-09). T37: without the key an activation makes,
    # my.claugine.com answers nothing more (API v2), so a dry run stops here.
    info "[dry-run] would activate ${INSTALL_ID} against ${CHECKIN_URL}, then check in and download the release"
    info "dry run: a dry run binds nothing, and nothing after the activation can be asked without its key."
    info "dry run: nothing was changed."; exit 0
  else
  # 2026-11-14 (claugine-web: a renewal only within the same client): a refusal carries the control plane's reason in
  # "message" (409: the id is registered with another customer's licence, or the licence holds another id) - say it.
  # T37 SC3: POST /api/v2/activate with a new channel key, kept in this run's private folder until the portal has it
  ch activate --install-id "$INSTALL_ID" --key-file "$CH_DIR/channel.key" \
     < <(ch_body install_id "$INSTALL_ID" fingerprints "[\"${FP}\"]") \
    || die "${CHECKIN_URL} gave no trusted answer to the activation: ${CH_WHY}
Nothing has been changed on this machine. Check that this machine can reach ${CHECKIN_URL}."
  if [[ "$CH_CODE" != 2?? ]]; then
    reason="$(ch_msg)"
    die "the licence was refused (HTTP ${CH_CODE}): ${reason:-${CH_ANS:0:300}}
Nothing has been changed on this machine."
  fi
  info "licence bound to ${INSTALL_ID}"
  fi
fi

if [[ "$MODE" != init ]]; then
ch call --name checkin --install-id "$INSTALL_ID" --key-file "$CH_DIR/channel.key" \
   < <(ch_body install_id "$INSTALL_ID" current_version "" stream stable usage '{}') \
  || die "the licence check-in gave no trusted answer: ${CH_WHY}"
[[ "$CH_CODE" == 2?? ]] || die "the licence check-in was refused (HTTP ${CH_CODE}): $(ch_msg)"
ci="$CH_ANS"
read -r EDITION TARGET STATE <<<"$(printf '%s' "$ci" | python3 -c '
import json,sys
d=json.load(sys.stdin)
print(d.get("edition") or "standard", d.get("target_version") or "-", d.get("state") or "active")')"
[[ "$TARGET" != "-" ]] || TARGET=""
# commit-611: a release the control plane names is a folder and a URL below - its form checked first
[[ -z "$TARGET" || "$TARGET" =~ $RELEASE_RE ]] \
  || die "the control plane named a release in an unknown form (${TARGET:0:40}). Nothing has been changed on this machine."
# retired means this installation id was released from its licence (claugine-web, 2026-11-09). Since 2026-11-16 the
# activation above reinstates a released id when the key is a free licence of the same client (claugine-web job
# 2026-11-16_from-claugine-web-reinstate-by-activation); a check-in never does. Retired here means it was not.
if [[ "$STATE" == "retired" ]]; then
  die "installation ${INSTALL_ID} was released on ${CHECKIN_URL} and was not reinstated.
Run this again with a free licence of the same client (its activation reinstates the installation), or ask
${CHECKIN_URL} to reinstate it. Nothing has been installed."
fi
NOTICE="$(printf '%s' "$ci" | python3 -c 'import json,sys; print(" ".join(json.load(sys.stdin).get("notices") or []))' 2>/dev/null || true)"
[[ "$STATE" == "active" || "$STATE" == "grace" ]] \
  || die "this licence is ${STATE} on ${CHECKIN_URL}${NOTICE:+: ${NOTICE}}; nothing was changed. Resolve it there and run this again."
info "edition: ${EDITION} · release: ${TARGET:-the newest} · licence: ${STATE}"
# commit-625: portal/init's stage 5 installs the release portal/init runs, never another one
if [[ -n "$WANT_RELEASE" && "$TARGET" != "$WANT_RELEASE" ]]; then
  die "${CHECKIN_URL} offers ${TARGET:-no release} to this installation, portal/init runs ${WANT_RELEASE}: the data move
needs the same release on both. Nothing has been installed on this machine."
fi
fi
# owner 2026-11-27 QB6-3: the production portal installed by portal/init (stage 5) starts with one node; its second
# node is added later from it (rules v2 A6)
if [[ "$EDITION" == "enterprise" && -z "$SECOND" && "$MODE" != init && $FROM_INIT -eq 0 ]]; then
  die "an Enterprise installation is two VMs: give the second one with --second <user>@<address>.
Nothing has been installed. Run it again with --second."
fi
if [[ "$EDITION" != "enterprise" && -n "$SECOND" ]]; then
  die "a second node needs the Enterprise edition; this licence is ${EDITION}, which runs one node.
Nothing has been installed. Run it again without --second."
fi
# core-024: the backup must be of this licence's edition; read with the password checked above, nothing written yet
if [[ "$MODE" == "restore" && $DRY -eq 0 ]]; then
  check_backup_edition "$BACKUP" "$PW_FILE" "$EDITION"
fi

# ---------------------------------------------------------------- fetch the code this licence is entitled to
if [[ -n "$DEV_SRC" ]]; then
  say "Using the tree given with --src (development)"
  [[ -f "$DEV_SRC/deploy/install.sh" ]] || die "$DEV_SRC has no deploy/install.sh"
  SRC="$(cd "$DEV_SRC" && pwd)"
  info "$SRC"
else
  # v01.00.00 (owner 2026-11-11): both editions come as bundles from the control plane - each licence gets its own
  # edition's bundle; nothing is cloned from git.
  say "Downloading the ${EDITION^} release from ${CHECKIN_URL}"
  [[ -n "$TARGET" ]] || die "the control plane named no release to install; try again shortly"
  SRC="$WORK/src-${TARGET}"
  rm -rf "$SRC"; mkdir -p "$SRC"
  fetch_bundle "$TARGET" "$SRC"
  # A bundle has no git history: the release IS its identity (install.sh records it in /opt/claugine-portal/COMMIT).
  printf '%s\n' "$TARGET" > "$SRC/COMMIT"
fi
[[ -f "$SRC/deploy/install.sh" ]] || die "the fetched release has no deploy/install.sh — refusing to go on"
# commit-610: a release older than portal/init cannot run as one - checked in the unpacked bundle, before install.sh
if [[ "$MODE" == init ]]; then
  if [[ ! -f "$SRC/accounts/initmode.py" ]] || ! grep -q -- '--init) INIT_NAME=' "$SRC/deploy/server/activate.sh" 2>/dev/null; then
    die "release ${TARGET:-of --src} predates portal/init: it cannot run as portal/init. Nothing has been installed.
Ask ${CHECKIN_URL} for a release with portal/init for this key."
  fi
  # commit-616: every check has passed - the marker that lets a retry of this run carry on (removed at the end)
  priv install -d -m 755 /etc/claugine-portal
  priv bash -c 'umask 077; printf "%s\n" "$1" > "$2"' _ "$PORTAL_NAME" "$INIT_MARK"
fi

# ---------------------------------------------------------------- install
inst_args=(--site-url "$SITE_URL")
# owner 2026-11-11: HAProxy in front of every node, the Standard edition's one node too ("an important security
# guard"). It takes 80 and 443 and asks Let's Encrypt for the certificate itself, so the e-mail goes to it, not to
# install.sh; Apache stays behind it on 8080/8443, and the role endpoint gets a port of its own on this VM's address.
SELF_ADDR=""
if [[ -z "$SECOND" ]]; then
  SELF_ADDR="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1)"
  [[ -n "$SELF_ADDR" ]] || SELF_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [[ -n "$SELF_ADDR" ]] || die "cannot tell this VM's own address (no default route): HAProxy needs it"
  if [[ -z "$SITE_URL" ]]; then SITE_URL="https://${SELF_ADDR}"; inst_args=(--site-url "$SITE_URL"); fi
  inst_args+=(--addr "$SELF_ADDR" --lb-addrs "$SELF_ADDR" --status-ports "$LB_STATUS_PORT")
fi

# commit-618: a production install or restore takes the VM over - a marker left by a failed --init goes first
[[ "$MODE" == init ]] || priv rm -f "$INIT_MARK"
[[ "$MODE" == init ]] || priv rm -f "$DONE_MARK"  # commit-626: finished only when this run reaches its end
case "$MODE" in
  new|init)
    say "Installing the portal${INIT_FOR:+ in its init edition (for ${INIT_FOR})}"
    priv bash "$SRC/deploy/install.sh" "${inst_args[@]}"
    ;;
  restore)
    say "Installing the portal, then restoring the backup"
    priv bash "$SRC/deploy/install.sh" "${inst_args[@]}"
    priv bash "$SRC/deploy/restore.sh" --site-url "$SITE_URL" --backup "$BACKUP" --edition "$EDITION" \
         ${PW_FILE:+--password-file "$PW_FILE"}
    info "the backup carries portal.env, so this installation keeps its original install id and its licence binding"
    ;;
esac
# T37 SC3 (owner 2026-11-27 QSC-2): the channel key this run's activation made goes into the portal's database,
# sealed (a restored database's older key is replaced: my.claugine.com now knows only this one); the run's copy goes
# with its private folder. A release without the channel command re-activates by itself at its first check-in.
if [[ "$MODE" != init && -s "$CH_DIR/channel.key" ]]; then
  priv /usr/local/bin/claugine-manage-root channel adopt < "$CH_DIR/channel.key" \
    || info "warning: the portal did not take the channel key; it activates again at its first check-in"
fi

# ---------------------------------------------------------------- bind the licence into portal.env
if [[ "$MODE" != "restore" ]]; then
  say "Writing the licence settings"
  # **Always** hand over the id this run is using, and never let activate.sh bind a second one. Found on the first
  # real-VM test, 2026-11-09: --new activated inst-A at step 2, then activate.sh found no portal.env, minted
  # inst-B, activated *that* and got 409 — leaving the portal configured with an id the licence is not bound to.
  # The check-in then answered `conflict` and updates were stopped, on a brand-new installation.
  if [[ "$MODE" == init ]]; then
    # never an installation id: the portal reads CLAUGINE_MODE=init and never checks in (L5)
    act_args=(--licence-key "$LICENCE_KEY" --checkin-url "$CHECKIN_URL" --init "$PORTAL_NAME" --edition "$EDITION"
              --release "$TARGET")
  else
  act_args=(--licence-key "$LICENCE_KEY" --checkin-url "$CHECKIN_URL" --install-id "$INSTALL_ID" --no-activate)
  fi
  priv bash "$SRC/deploy/server/activate.sh" "${act_args[@]}"
  [[ "$MODE" != init ]] || priv rm -f "$INIT_MARK"  # commit-616: portal.env says init now - the marker's work is done

  # The backup owner is this account, and it is what a pinned `~` path expands against. Found on the first real-VM
  # test, 2026-11-09: left empty, Standard's `~/claugine/backups` resolved against the home of the user the portal
  # *runs* as - www-data - so a fresh installation would have written its backups into /var/www/claugine/backups.
  # The rule is the service account's home, and the installer is the only thing that knows which account that is.
  say "Setting the backup owner to $(id -un):$(id -gn)"
  if [[ $DRY -eq 1 ]]; then
    info "[dry-run] would set backup_owner to $(id -un):$(id -gn) if it is empty"
  else
    {
      echo "from tenants.models import PortalSettings"
      echo "s = PortalSettings.get()"
      echo "if not (s.backup_owner or '').strip():"
      echo "    s.backup_owner = '$(id -un):$(id -gn)'"
      echo "    s.save(update_fields=['backup_owner'])"
      echo "    print('   backup_owner set to', s.backup_owner)"
      echo "else:"
      echo "    print('   backup_owner already set:', s.backup_owner)"
    } > "$WORK/.set-owner.py"
    priv bash -c "cd /opt/claugine-portal/app && /opt/claugine-portal/venv/bin/python manage.py shell < $WORK/.set-owner.py" \
      2>/dev/null | tail -1 || info "could not set the backup owner - set it in Settings, Backups"
    rm -f "$WORK/.set-owner.py"
  fi

  priv systemctl restart claugine-app  # portal.env changed: the application reads it at start

  # One check-in now, rather than leaving it to the timer. The appliance timer is created before the licence is
  # written, so its first tick finds no check-in configured, takes the GitHub branch and caches no edition - and
  # its ten-minute rate limit then leaves the node without one for up to ten minutes more. A Standard installation
  # enforces nothing until the edition is cached, so that window is exactly the limit it is supposed to have
  # (owner 2026-11-09, test VM). force=True is needed because the first tick already stamped checked_at.
  say "Telling the licence server what this installation is"
  if [[ "$MODE" == init ]]; then
    info "portal/init is no installation: no check-in (owner 2026-11-25 L5)"
  elif [[ $DRY -eq 1 ]]; then
    info "[dry-run] would check in once so the edition is in force immediately"
  else
    {
      echo "from accounts import autoupdate"
      echo "print('  ', autoupdate.tick(log=lambda *a: None, force=True))"
    } > "$WORK/.checkin.py"
    priv bash -c "cd /opt/claugine-portal/app && /opt/claugine-portal/venv/bin/python manage.py shell < $WORK/.checkin.py" \
      2>/dev/null | tail -1 || info "the check-in did not run - the timer will do it within ten minutes"
    rm -f "$WORK/.checkin.py"
  fi
fi

# ---------------------------------------------------------------- HAProxy in front of the one node
if [[ -n "$SELF_ADDR" ]]; then
  say "HAProxy in front of this node (${SITE_URL#https://} → Apache on this VM)"
  https_port="$(sudo -n sed -n 's/^HTTPS_PORT=//p' /etc/claugine-portal/node.env 2>/dev/null | tail -1)"
  lb_args=(--apps "$SELF_ADDR" --public-name "$(printf '%s' "$SITE_URL" | sed -E 's#^https?://##; s#[:/].*$##')"
           --status-port "$LB_STATUS_PORT" --app-port "${https_port:-8443}")
  [[ -z "$LE_EMAIL" ]] || lb_args+=(--letsencrypt-email "$LE_EMAIL")
  priv bash "$SRC/deploy/install-lb.sh" "${lb_args[@]}" \
    || die "HAProxy could not be set up (see above). The portal is installed behind it on port ${https_port:-8443}; run this
again once the cause is fixed"
fi

# ---------------------------------------------------------------- the second node, set up from here
if [[ -n "$SECOND" ]]; then
  say "Setting up the second node ($SECOND) from this one"
  an_args=(--target "$SECOND" --src "$SRC")
  [[ -z "$SITE_CODE" ]]   || an_args+=(--site "$SITE_CODE")
  [[ -z "$SECOND_SITE" ]] || an_args+=(--target-site "$SECOND_SITE")
  [[ -z "$LE_EMAIL" ]]    || an_args+=(--letsencrypt-email "$LE_EMAIL")
  priv bash "$SRC/deploy/server/claugine-add-node.sh" "${an_args[@]}" \
    || die "the second node could not be set up (see above). This node is installed; once the cause is fixed run,
on this VM:  sudo claugine-add-node --target $SECOND"
fi

# commit-626: the installation is finished - HAProxy (and the second node) included
if [[ "$MODE" != init ]]; then
  priv bash -c 'head -1 /opt/claugine-portal/COMMIT > "$1" && chmod 644 "$1"' _ "$DONE_MARK"
fi

# ---------------------------------------------------------------- what the operator does next
say "Done"
if [[ $DRY -eq 1 ]]; then info "dry run: nothing was changed."; exit 0; fi
port="$(sudo -n sed -n 's/^HTTP_PORT=//p' /etc/claugine-portal/node.env 2>/dev/null | tail -1)"
code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port:-8080}/" || true)"
info "the portal answers locally with HTTP ${code} (301 to https, or 200, is expected)"
# A cheap self-check for the fault this test found: the id in portal.env must be the one we bound.
written="$(sudo -n sed -n 's/^CLAUGINE_INSTALL_ID=//p' /etc/claugine-portal/portal.env 2>/dev/null | tail -1)"
if [[ -n "$written" && "$written" != "$INSTALL_ID" ]]; then
  printf "\n   WARNING: portal.env says %s but this run bound %s.\n   The portal will check in as an installation the licence does not cover.\n" \
         "$written" "$INSTALL_ID" >&2
fi
info "open ${SITE_URL} and sign in as the first super admin:"
info "  sudo claugine-manage bootstrap_superadmin        prints a one-time link, valid 24 hours"
case "$MODE" in
  init)
    info "portal/init for ${PORTAL_NAME}: Installation → Settings, then Installation → Run (seven stages; the last one locks it)"
    ;;
  new)
    if [[ "$EDITION" == "enterprise" && -z "$SECOND" ]]; then
      info "one node for now (installed by portal/init); add the second one from this portal later"
    elif [[ "$EDITION" == "enterprise" ]]; then
      info "two nodes: this one active, ${SECOND#*@} standby and replicating; HAProxy on both sends ${SITE_URL} to the active one"
    else
      info "the Standard edition runs one node, behind HAProxy on this VM, with its backups and update window fixed - Settings shows them."
    fi
    ;;
esac
