"""The portal appliance: what would otherwise be set by hand on the portal VM is kept in Settings (the database).

    public URL      links in emails; its host is accepted as a host name of the portal (no restart)
    mail            internal: the VM's own relay (postfix, loopback only, set up by the installer and kept in line
                    with the settings by the appliance timer); external: an SMTP server
    backups         an encrypted archive of the database and portal.env (manage.py appliance, run as root every
                    5 minutes by claugine-appliance.timer): to a local folder or to a remote host with rsync over ssh

Restore on a fresh Ubuntu 26.04 VM: `claugine-install.sh --restore` installs the release the licence is entitled to
now, from the control plane, and loads one backup over it, migrating it forward. Backups carry data only, never the
portal itself (2026-10-30).
"""

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend
from django.middleware.csrf import CsrfViewMiddleware
from django.utils import timezone

ARCHIVE_PREFIX = "claugine"  # a backup is a folder claugine_<fqdn>_<edition>_YYYYmmdd-HHMM (owner 2026-11-08, -16)
LOCAL_SUBDIR = ""  # owner 2026-11-08: backups go straight into the configured folder, with no subfolder
_CACHE = {"at": 0.0, "url": "", "host": "", "test_url": "", "admin_url": ""}


def portal_fqdn(s=None):
    """u07: the portal's host name, for the backup folder name (e.g. portal.example.com)."""
    from urllib.parse import urlsplit

    s = s or _settings()
    url = s.public_url or getattr(settings, "SITE_URL", "") or ""
    return urlsplit(url).hostname or (url.split("//")[-1].split("/")[0] if url else "") or "portal"


def portal_backup_name(s=None, when=None):
    """claugine_<fqdn>_<edition>_YYYYmmdd-HHMM: owner 2026-11-08 (was claugine-portal_… before); the edition since
    2026-11-16 (core-024, owner: "edition can be added to archive name"), so a person sees which installation a backup
    restores to. Older backups have no edition in the name; the check is the `edition` file inside the archive."""
    from accounts import edition

    s = s or _settings()
    stamp = (when or timezone.localtime()).strftime("%Y%m%d-%H%M")
    return f"{ARCHIVE_PREFIX}_{portal_fqdn(s)}_{edition.of_installation(s)}_{stamp}"


#: The stamp that ends every backup's name: YYYYmmdd-HHMM, with seconds when two backups share a minute.
_STAMP = re.compile(r"(\d{8}-\d{4}(?:\d{2})?)$")


def backup_stamp(name):
    """The time stamp a backup's name ends with, the order backups are listed in: by name, a Standard installation's
    backups would sort after its later Enterprise ones, and the older names without an edition before both."""
    m = _STAMP.search(name or "")
    return m.group(1) if m else ""


def _settings():
    from tenants.models import PortalSettings

    return PortalSettings.get()


# --- public URL ---------------------------------------------------------------------------------------------------


def public_url():
    """The portal's public URL: Settings, else the installer's (SITE_URL)."""
    now = time.monotonic()
    if now - _CACHE["at"] > 30:
        try:
            st = _settings()
            url, test = (st.public_url or "").rstrip("/"), (st.test_url or "").rstrip("/")
            admin = (st.admin_url or "").rstrip("/")  # owner 2026-11-23 16:39 (U13): the Admin Portal's name too
        except Exception:  # noqa: BLE001 - before migrations, no database
            url, test, admin = "", "", ""
        _CACHE.update(at=now, url=url, test_url=test, admin_url=admin, host=urlsplit(url).hostname or "" if url else "")
    return _CACHE["url"] or settings.SITE_URL


def forget():
    _CACHE["at"] = 0.0


class LiveCsrfViewMiddleware(CsrfViewMiddleware):
    """commit-424: Django's CSRF middleware with its trusted origins read from the settings on every request - Django
    keeps them on the middleware instance once read, so an address PublicHostMiddleware takes out (a portal URL changed
    or cleared in Settings) would stay trusted in that worker until it restarts."""

    @property
    def csrf_trusted_origins_hosts(self):
        return [urlsplit(origin).netloc.lstrip("*") for origin in settings.CSRF_TRUSTED_ORIGINS]

    @property
    def allowed_origins_exact(self):
        return {origin for origin in settings.CSRF_TRUSTED_ORIGINS if "*" not in origin}

    @property
    def allowed_origin_subdomains(self):
        allowed = defaultdict(list)
        for parsed in (urlsplit(origin) for origin in settings.CSRF_TRUSTED_ORIGINS if "*" in origin):
            allowed[parsed.scheme].append(parsed.netloc.lstrip("*"))
        return allowed


# commit-424: what PublicHostMiddleware added to this worker's ALLOWED_HOSTS / CSRF_TRUSTED_ORIGINS - a former address
# (a URL changed or cleared in Settings) is taken out again; the static configuration is never touched
_ADDED = {"hosts": set(), "origins": set()}


class PublicHostMiddleware:
    """Accept the public URL's host (Settings) besides ALLOWED_HOSTS, and its origin for CSRF; the Admin Portal's URL
    too while it is on the certificate (owner 2026-11-23 16:39, U13). Only the current addresses: one set before is
    no longer accepted once Settings changes it (commit-424)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from .haproxy import ADMIN_NAME_ON_CERT

        public_url()  # refreshes the cache every 30 s
        admin = _CACHE.get("admin_url", "") if ADMIN_NAME_ON_CERT else ""
        hosts, origins = set(), set()
        for url in (public_url(), _CACHE.get("test_url", ""), admin):
            p = urlsplit(url.rstrip("/"))
            if not p.scheme:
                continue
            if p.hostname:
                hosts.add(p.hostname)
            origins.add(f"{p.scheme}://{p.netloc}")
        for key, live, want in (
            ("hosts", settings.ALLOWED_HOSTS, hosts),
            ("origins", settings.CSRF_TRUSTED_ORIGINS, origins),
        ):
            for x in list(_ADDED[key] - want):  # a former address: out
                while x in live:
                    live.remove(x)
                _ADDED[key].discard(x)
            for x in want:
                if x not in live and not (key == "hosts" and "*" in live):
                    live.append(x)
                    _ADDED[key].add(x)
        return self.get_response(request)


# --- mail ---------------------------------------------------------------------------------------------------------


class EmailBackend(BaseEmailBackend):
    """Sends with the mail settings of the portal; when the relay cannot be reached the messages are kept in files
    (EMAIL_FILE_PATH) and the failure is logged, so nothing is lost."""

    def send_messages(self, email_messages):
        from django.core.mail.backends.filebased import EmailBackend as FileBackend
        from django.core.mail.backends.smtp import EmailBackend as SmtpBackend

        if not email_messages:
            return 0
        from accounts import ha

        if not ha.is_active():  # a new version under test (or a standby) never mails users: kept in files only
            return FileBackend(file_path=settings.EMAIL_FILE_PATH).send_messages(email_messages)
        s = _settings()
        from accounts.utils import ALLOW_CLIENT_HEADER

        allowed = [m for m in email_messages if (m.extra_headers or {}).pop(ALLOW_CLIENT_HEADER, None)]
        # owner 2026-10-29: only platform users get e-mail until it is unblocked in Settings; 2026-11-17: "allow emails
        # to platform users" may be switched off too. A message a client was allowed to get by the owner's word keeps
        # its client recipients; its platform recipients follow the switch.
        if not s.client_mail or not getattr(s, "staff_mail", True):
            keep_staff = getattr(s, "staff_mail", True)
            email_messages = hold_mail(
                [m for m in email_messages if m not in allowed], clients=s.client_mail, staff=keep_staff
            ) + (hold_mail(allowed, clients=True, staff=keep_staff) if allowed else [])
            if not email_messages:
                return 0
        sender = s.mail_from or settings.DEFAULT_FROM_EMAIL
        email_messages = [with_html(m) for m in email_messages]
        for m in email_messages:
            _branded(m)
            _inline_logo(m)
            if not m.from_email or m.from_email == settings.DEFAULT_FROM_EMAIL:
                m.from_email = sender
        # owner 2026-11-17: "the only option - use internal server": the node's own postfix sends, straight to the
        # recipients' servers, signed with DKIM (apply_mail)
        smtp = SmtpBackend(host="127.0.0.1", port=25, use_tls=False, timeout=10, fail_silently=False)
        try:
            return smtp.send_messages(email_messages) or 0
        except (OSError, Exception) as exc:  # noqa: BLE001 - kept in files instead
            from accounts.models import audit

            audit(
                None,
                "mail_failed",
                None,
                relay=s.mail_mode,
                error=f"{type(exc).__name__}: {exc}"[:300],
                messages=len(email_messages),
            )
            path = getattr(settings, "EMAIL_FILE_PATH", "") or str(settings.BASE_DIR / "var" / "mail")
            for m in email_messages:
                m.portal_kept = True  # not sent: kept in files (the caller can tell, e.g. the provisioning e-mails)
            return FileBackend(file_path=path, fail_silently=True).send_messages(email_messages) or 0


URL_RE = __import__("re").compile(r"(https?://[^\s<>\"']+)")


def _branded(m):
    """The provider's name from the settings in place of the default one (owner 2026-10-29: Settings → Provider)."""
    from .branding import brand

    m.subject, m.body = brand(m.subject), brand(m.body)
    alts = []
    for a in getattr(m, "alternatives", None) or []:
        content = brand(a[0]) if isinstance(a[0], str) else a[0]
        alts.append(a._replace(content=content) if hasattr(a, "_replace") else (content, a[1]))
    if alts:
        m.alternatives = alts
    return m


LOGO_CID = "provider-logo"


def _inline_logo(m):
    """The logo inside the message (owner 2026-10-29 16:11: logo in the e-mails) — mail programs such as Outlook do
    not load pictures from the internet by default; one sent with the message is shown at once."""
    import re

    alts = getattr(m, "alternatives", None) or []
    if not alts or getattr(m, "portal_logo", False):
        return m
    html = alts[0][0] if isinstance(alts[0][0], str) else ""
    from django.templatetags.static import static

    from . import branding

    default = static(branding.DEFAULT_LOGO)  # Claugine's while no logo is uploaded (owner 2026-11-17)
    pat = re.compile(r'src="[^"]*(?:/brand/logo\.png|' + re.escape(default) + r')(?:\?v=\d+)?"')
    if not pat.search(html):
        return m
    from email.mime.image import MIMEImage

    from tenants.models import PortalSettings

    logo = PortalSettings.get().logo
    if not logo:
        with open(branding.static_file(branding.DEFAULT_LOGO), "rb") as fh:
            logo = fh.read()
    html = pat.sub(f'src="cid:{LOGO_CID}"', html)
    a = alts[0]
    m.alternatives = [a._replace(content=html) if hasattr(a, "_replace") else (html, a[1])] + list(alts[1:])
    img = MIMEImage(bytes(logo), "png")
    img.add_header("Content-ID", f"<{LOGO_CID}>")
    img.add_header("Content-Disposition", "inline", filename="logo.png")
    m.attach(img)
    m.mixed_subtype = "related"
    m.portal_logo = True
    return m


def with_html(m):
    """A plain-text message with an HTML copy of the same text, its addresses as links whose text is the address
    (owner 2026-10-29: mail filters such as Outlook Safe Links rewrite the addresses of a plain-text message, so the
    reader saw a long safelinks address instead of the platform's URL; in HTML only the link target is rewritten)."""
    from django.core.mail import EmailMultiAlternatives

    if getattr(m, "alternatives", None) or getattr(m, "content_subtype", "plain") != "plain":
        return m
    from .mailhtml import framed  # owner 2026-10-29: every e-mail in HTML, in the portal's frame

    html = framed(m.body or "", m.subject or "")
    n = EmailMultiAlternatives(
        m.subject,
        m.body,
        m.from_email,
        m.to,
        bcc=m.bcc,
        cc=m.cc,
        reply_to=m.reply_to,
        headers=m.extra_headers,
        attachments=m.attachments,
    )
    n.attach_alternative(html, "text/html")
    return n


def platform_addresses():
    from accounts.models import Kind, User

    return {
        e.lower() for e in User.objects.filter(kind=Kind.STAFF, is_active=True).values_list("email", flat=True) if e
    }


def hold_client_mail(email_messages):
    """The messages with their recipients cut to platform users (e-mail to clients off)."""
    return hold_mail(email_messages, clients=False, staff=True)


def hold_mail(email_messages, clients, staff):
    """The messages with their recipients cut to those allowed (clients: anybody not a platform user; staff: platform
    users); what was cut is kept in files (var/mail-held) and logged, never sent."""
    import copy
    from email.utils import parseaddr

    from django.core.mail.backends.filebased import EmailBackend as FileBackend

    from accounts.models import audit

    platform, staff_ok = platform_addresses(), staff
    keep, held = [], []
    for m in email_messages:

        def mine(lst):
            return [a for a in lst or [] if (staff_ok if parseaddr(a)[1].lower() in platform else clients)]

        to, cc, bcc = mine(m.to), mine(m.cc), mine(m.bcc)
        cut = [a for a in list(m.to or []) + list(m.cc or []) + list(m.bcc or []) if a not in to + cc + bcc]
        if cut:
            h = copy.copy(m)
            h.to, h.cc, h.bcc = cut, [], []
            held.append(h)
            audit(None, "mail_held", None, to=", ".join(cut)[:300], subject=(m.subject or "")[:200])
        if to or cc or bcc:
            m.to, m.cc, m.bcc = to, cc, bcc
            keep.append(m)
    if held:
        path = str(settings.BASE_DIR / "var" / "mail-held")
        try:
            FileBackend(file_path=path, fail_silently=True).send_messages(held)
        except Exception:  # noqa: BLE001 - the audit entry stays
            pass
    return keep


def relay_reachable():
    try:
        with socket.create_connection(("127.0.0.1", 25), timeout=2):
            return True
    except OSError:
        return False


SASL_FILE = "/etc/postfix/sasl_passwd"
DKIM_CONF = "/etc/opendkim.conf"
DKIM_SOCKET = "inet:8891@127.0.0.1"  # OpenDKIM's notation of its listening socket
POSTFIX_MILTER = "inet:127.0.0.1:8891"  # postfix's notation of the same socket


def _postconf(key):
    r = subprocess.run(["postconf", "-h", key], capture_output=True, text=True, check=False)
    return r.stdout.strip()


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _dkim_conf():
    out = {}
    for line in _read(DKIM_CONF).splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and not parts[0].startswith("#"):
            out[parts[0]] = parts[1].strip()
    return out


def capture_mail(s, log):
    """Once: the DKIM key this VM already signs with goes into Settings, with its selector (and the domain, used while
    there is no sender address), so that its DNS record stays right and a restored VM gets the same. The relay of the
    older versions is not taken any more (owner 2026-11-17: the nodes' own servers only). Values are never printed.
    Root only."""
    if not shutil.which("postconf") or s.has_secret("mail_dkim_key"):
        return False
    conf = _dkim_conf()
    key = _read(conf.get("KeyFile", "")) if conf.get("KeyFile") else ""
    if not (key and "PRIVATE KEY" in key):
        return False
    s.mail_dkim_selector = s.mail_dkim_selector or conf.get("Selector", "")
    s.mail_dkim_domain = s.mail_dkim_domain or conf.get("Domain", "")
    s.set_secret("mail_dkim_key", key)
    fields = ["mail_dkim_selector", "mail_dkim_domain", "mail_dkim_key_encrypted"]
    if not s.mail_from and settings.DEFAULT_FROM_EMAIL and "localhost" not in settings.DEFAULT_FROM_EMAIL:
        s.mail_from = settings.DEFAULT_FROM_EMAIL[:200]
        fields.append("mail_from")
    s.save(update_fields=fields)
    from accounts.models import audit

    audit(None, "mail_settings_captured", s, dkim=s.mail_dkim_selector or "-")
    log("the DKIM key of this VM taken into Settings")
    return True


def dkim_domain(s):
    """The domain the mail is signed for: the sender address's (owner 2026-11-17: "Sender's domain"); without a sender
    address, the one kept from before."""
    from email.utils import parseaddr

    addr = parseaddr(s.mail_from or "")[1]
    return addr.rsplit("@", 1)[1].lower() if "@" in addr else (s.mail_dkim_domain or "")


def dkim_selector(s):
    return s.mail_dkim_selector or "claugine"


def ensure_dkim_key(s):
    """The DKIM key, made by the portal when there is none (owner 2026-11-17: "Portal makes the key"); True when made.
    The caller saves."""
    if s.has_secret("mail_dkim_key"):
        return False
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    s.set_secret("mail_dkim_key", pem)
    return True


def dkim_public(s):
    """The public part of the DKIM key as the DNS record carries it (p=...), or ""."""
    import base64

    from cryptography.hazmat.primitives import serialization

    if not s.has_secret("mail_dkim_key"):
        return ""
    try:
        key = serialization.load_pem_private_key(s.get_secret("mail_dkim_key").encode(), password=None)
    except (ValueError, TypeError):
        return ""
    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(der).decode()


def mail_name():
    """direct delivery: this node's mail name (the name of the PTR record of its public address), from node.env."""
    from accounts import thisnode

    return thisnode.env().get("MAIL_NAME", "")


def mail_peer():
    from accounts import thisnode

    return thisnode.env().get("PEER_ADDR", "")


def peer_takes_mail(addr, timeout=3):
    """commit-406: the other node's postfix answers on its internal address (it listens there only as the chosen
    node)."""
    try:
        with socket.create_connection((addr, 25), timeout=timeout):
            return True
    except OSError:
        return False


def mail_role(s):
    """Owner 2026-11-23 (A), Settings → Mail "Send mail from": "" (each node sends itself: also when the chosen site is
    neither node, or there is no other node), "relay" (the other node is chosen: hand every mail to it) or "sender"
    (this node is chosen: also take the other node's mail)."""
    from accounts import thisnode

    n = thisnode.env()
    via = (getattr(s, "mail_via", "") or "").strip()
    if not via or not n.get("PEER_ADDR") or not n.get("ADDR"):
        return ""
    if via == n.get("PEER_SITE"):
        return "relay"
    if via == n.get("SITE"):
        return "sender"
    return ""


def apply_mail(s, log, dry=False):
    """Keep this VM's postfix (loopback only) in line with Settings and sign with DKIM (OpenDKIM): it delivers to the
    recipients' mail servers itself, under this node's mail name, signing for the sender address's domain. Changes only
    what differs. Root only."""
    if not shutil.which("postconf"):
        return
    changed = []
    # owner 2026-11-17: direct delivery only (the relay and the external SMTP server are gone)
    # the portal's mail carries its sender (portal@<domain>) as the envelope sender; the VM's own mail (cron, root)
    # stays on the VM under its mail name instead of going out to <user>@<sender domain>
    want = {
        "inet_interfaces": "loopback-only",
        "inet_protocols": "ipv4",
        "relayhost": "",
        "mynetworks": "127.0.0.0/8",
        "myorigin": "$myhostname",
        "mydestination": "$myhostname, localhost",
        "smtp_tls_security_level": "may",
        "smtp_sasl_auth_enable": "no",
    }
    if mail_name():
        want["myhostname"] = mail_name()
    role = mail_role(s)
    relay = f"[{mail_peer()}]:25"
    if role == "relay" and _postconf("relayhost") != relay and not peer_takes_mail(mail_peer()):
        # commit-406: turned to relaying only once the chosen node takes mail on its internal address (it applies the
        # setting on its own timer); until then this node keeps sending itself and tries again next round. Once
        # relaying it stays: while the chosen node is down the mail waits in the queue.
        log("mail relay: the chosen node does not take mail yet - this node keeps sending itself")
        # commit-407: a node that was the chosen one keeps taking (and signing) the other node's mail meanwhile
        from accounts import thisnode

        addr = thisnode.env().get("ADDR", "")
        role = "sender" if addr and addr in _postconf("inet_interfaces") else ""
    if role == "relay":  # owner 2026-11-23 (A): the other node sends - this one hands every mail to it
        want["relayhost"] = relay
    elif role == "sender":  # and this one takes it from the other node only, on its internal address
        from accounts import thisnode

        n = thisnode.env()
        want["inet_interfaces"] = f"127.0.0.1, {n['ADDR']}"
        want["mynetworks"] = f"127.0.0.0/8, {n['PEER_ADDR']}/32"
    if _read(SASL_FILE).strip():  # no relay account any more: not kept on disk
        if dry:
            changed.append("relay account removed")
        else:
            with open(os.open(SASL_FILE, os.O_WRONLY | os.O_TRUNC), "w", encoding="utf-8") as fh:
                fh.write("")
            subprocess.run(["postmap", SASL_FILE], check=False)
            changed.append("relay account removed")
    key = s.get_secret("mail_dkim_key") if s.has_secret("mail_dkim_key") else ""
    domain, selector = dkim_domain(s), dkim_selector(s)
    if role == "relay":  # signed where it leaves (the other node), not twice
        want.update(smtpd_milters="", non_smtpd_milters="")
    elif key and domain and selector and os.path.exists(DKIM_CONF):
        want.update(
            smtpd_milters=POSTFIX_MILTER,
            non_smtpd_milters=POSTFIX_MILTER,
            milter_default_action="accept",
            milter_protocol="6",
        )
        folder = f"/etc/opendkim/keys/{domain}"
        path = f"{folder}/{selector}.private"
        dkim_changed = False
        if _read(path) != key and dry:
            changed.append("DKIM key")
        elif _read(path) != key:
            os.makedirs(folder, mode=0o750, exist_ok=True)
            with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
                fh.write(key)
            shutil.chown(folder, "opendkim", "opendkim")
            shutil.chown(path, "opendkim", "opendkim")
            dkim_changed = True
        conf = _dkim_conf()
        lines = {
            "Domain": domain,
            "Selector": selector,
            "KeyFile": path,
            "Socket": DKIM_SOCKET,
            "Mode": "s",
            "Canonicalization": "relaxed/simple",
            "OversignHeaders": "From",
            "SyslogSuccess": "yes",
            # commit-405: OpenDKIM signs only what comes from its internal hosts - on the chosen node the other node's
            # mail too
            "InternalHosts": "127.0.0.1" + (f",{mail_peer()}" if role == "sender" else ""),
        }
        if any(conf.get(k) != v for k, v in lines.items()) and dry:
            changed.append("opendkim.conf: " + ", ".join(k for k, v in lines.items() if conf.get(k) != v))
        elif any(conf.get(k) != v for k, v in lines.items()):
            text = [x for x in _read(DKIM_CONF).splitlines() if not (x.split() and x.split()[0] in lines)]
            text += [f"{k:<24}{v}" for k, v in lines.items()]
            with open(DKIM_CONF, "w", encoding="utf-8") as fh:
                fh.write("\n".join(text) + "\n")
            dkim_changed = True
        if dkim_changed:
            subprocess.run(["systemctl", "restart", "opendkim"], check=False)
            changed.append("DKIM")
    diff = [f"{k}={v}" for k, v in want.items() if _postconf(k) != v]
    if dry:
        log("dry run, would change: " + ("; ".join(changed + [d.split("=")[0] for d in diff]) or "nothing"))
        return
    if diff:
        subprocess.run(["postconf", "-e", *diff], check=True)
        changed.append(", ".join(d.split("=")[0] for d in diff))
    if changed:
        subprocess.run(["systemctl", "restart", "postfix"], check=False)
        log(f"mail relay: {'; '.join(changed)}")


# --- backups ------------------------------------------------------------------------------------------------------


def slots(s, now, start=None):
    """u07: the day's scheduled backup time at or before `now` (local). One backup a day, at the block's start time."""
    local = timezone.localtime(now)
    from accounts.edition import effective as _eff  # Standard pins the backup time

    at = start or _eff(s, "backup_start")
    if isinstance(at, str):  # a new settings row keeps the default as text until it is read back
        at = datetime.strptime(at[:5], "%H:%M").time()
    anchor = local.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    if anchor > local:
        anchor -= timedelta(days=1)
    return anchor


def due(s, now=None):
    """u07: one backup a day — due when today's slot has passed and today's backup has not run yet, or on request.
    The local folder and the S3 bucket are written by the same run (local is the source; see backup())."""
    from accounts.edition import effective as _eff

    if not (_eff(s, "backup_local") or s3_allowed(s)):  # no destination switched on (and allowed: integration-006)
        return False
    now = now or timezone.now()
    if s.backup_requested_at and (s.backup_last_at is None or s.backup_requested_at > s.backup_last_at):
        return True
    return s.backup_last_at is None or s.backup_last_at < slots(s, now)


def _dump(tmp):
    db = settings.DATABASES["default"]
    out = os.path.join(tmp, "database.sql")
    if db["ENGINE"].endswith("sqlite3"):
        import sqlite3

        con = sqlite3.connect(str(db["NAME"]))
        with open(out, "w", encoding="utf-8") as fh:
            for line in con.iterdump():
                fh.write(line + "\n")
        return out
    # --set-gtid-purged=OFF: with GTID on (the replication between the nodes) mysqldump would otherwise lock all
    # tables first (FLUSH TABLES), a right the portal's database user does not have — the backups failed (2026-10-29)
    cmd = [
        "mysqldump",
        "--single-transaction",
        "--set-gtid-purged=OFF",
        "--routines",
        "--no-tablespaces",
        "--default-character-set=utf8mb4",
        "-h",
        db.get("HOST") or "localhost",
        "-u",
        db["USER"],
        db["NAME"],
    ]
    if db.get("PORT"):
        cmd[1:1] = ["-P", str(db["PORT"])]
    env = {**os.environ, "MYSQL_PWD": db.get("PASSWORD") or ""}
    with open(out, "wb") as fh:
        subprocess.run(cmd, stdout=fh, stderr=subprocess.PIPE, env=env, check=True, timeout=3600)
    return out


DIR_MODE, FILE_MODE = 0o750, 0o640


def _resolve_owner(spec):
    """'user:group' / 'user' / 'uid:gid' → (uid, gid), or None."""
    import grp
    import pwd

    spec = (spec or "").strip()
    if not spec:
        return None
    u, _, g = spec.partition(":")
    try:
        uid = int(u) if u.isdigit() else pwd.getpwnam(u).pw_uid
    except (KeyError, ValueError):
        return None
    gid = None
    if g:
        try:
            gid = int(g) if g.isdigit() else grp.getgrnam(g).gr_gid
        except (KeyError, ValueError):
            gid = None
    if gid is None:
        try:
            gid = pwd.getpwuid(uid).pw_gid
        except KeyError:
            gid = uid
    return uid, gid


def _owner(folder, s=None):
    """(uid, gid) the local backups are set to (owner 2026-11-02): Settings → backups owner when set, else the owner of
    the backups folder — its parent, the configured local folder — so the claugine-*/ subfolder and the dated dirs are
    not left as nobody (NFS creates them so when the portal runs as root)."""
    if s is not None:
        o = _resolve_owner(getattr(s, "backup_owner", ""))
        if o:
            return o
    # owner 2026-11-08: the backups now sit in the configured folder itself, so inherit from **that** folder first
    # and from its parent only as a fallback. Prefer a real owner over root: when the portal had to create the
    # folder it owns it as root, and inheriting that would quietly take the backups away from their owner.
    found = []
    for p in (folder, os.path.dirname(folder.rstrip("/"))):
        try:
            st = os.stat(p)
        except OSError:
            continue
        found.append((st.st_uid, st.st_gid))
    for uid, gid in found:
        if uid != 0:
            return uid, gid
    return found[0] if found else None


def _fix_one(path, owner):
    """The mode and owner of a single file or folder, without touching anything beside it."""
    try:
        os.chmod(path, DIR_MODE if os.path.isdir(path) else FILE_MODE)
        if owner and os.geteuid() == 0:
            os.chown(path, *owner)
    except OSError:
        pass  # e.g. an NFS share that maps root


def _fix_modes(path, owner):
    """Folders 750, files 640, owned like the backup folder (when the portal may give them away)."""
    for root, _dirs, files in os.walk(path):
        for x in [root] + [os.path.join(root, f) for f in files]:
            os.chmod(x, DIR_MODE if os.path.isdir(x) else FILE_MODE)
            if owner and os.geteuid() == 0:
                try:
                    os.chown(x, *owner)
                except OSError:
                    pass  # e.g. an NFS share that maps root: the modes still hold


CAPTURE_LOCK = ""  # tests: a path; else <the HA agent's state folder>/backup-capture.lock (root's)


class capture_lock:
    """core-040 (nineteenth full review): a backup's database dump and its copy of portal.env (the key to the stored
    secrets) are one capture. Key rotation holds this lock from writing the new key to removing the old one, and a
    backup holds it while it captures, so a backup is wholly before or wholly after a rotation - never a dump made
    with the old key next to a portal.env that no longer has it. Root's lock (the agent's folder): only root makes
    backups and rotates keys; elsewhere (a test run as another user) it is not taken."""

    def __enter__(self):
        import fcntl

        from . import ha_agent

        path = CAPTURE_LOCK or os.path.join(ha_agent.STATE_DIR, "backup-capture.lock")
        try:
            os.makedirs(os.path.dirname(path), mode=0o750, exist_ok=True)
            self.fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except PermissionError:
            self.fd = None
            return self
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        except BaseException:  # commit-082: __exit__ is not called when __enter__ fails - the descriptor goes here
            os.close(self.fd)
            self.fd = None
            raise
        return self

    def __exit__(self, *exc):
        import fcntl

        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
        return False


#: tests: a folder; else the HA agent's state folder (root's)
RUN_LOCK_DIR = ""


class run_lock(capture_lock):
    """commit-062 (twenty-fifth full review's commit reviews): one run of a kind at a time on this node - the
    appliance timer and the HA agent's backup before an update can each start a portal backup, and two runs picking
    one name into an empty S3 folder wrote the same keys (the failing one then deleted the other's archive). The second
    waits; then the name is taken (the local folder, or the bucket for an S3-only run) and it takes its seconds."""

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        import fcntl

        from . import ha_agent

        path = os.path.join(RUN_LOCK_DIR or ha_agent.STATE_DIR, f"{self.name}.lock")
        try:
            os.makedirs(os.path.dirname(path), mode=0o750, exist_ok=True)
            self.fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except PermissionError:  # not root (a test run as another user): not taken, as capture_lock
            self.fd = None
            return self
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        except BaseException:  # commit-082: __exit__ is not called when __enter__ fails - the descriptor goes here
            os.close(self.fd)
            self.fd = None
            raise
        return self


def env_keys(text):
    """The FIELD_ENCRYPTION_KEY keys of a portal.env's text, read as portal/settings.py reads them: the first
    assignment, trimmed, comma-separated, empty items dropped (inventory/crypto.py)."""
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            if key.strip() == "FIELD_ENCRYPTION_KEY":
                return [k.strip() for k in value.strip().split(",") if k.strip()]
    return []


#: commit-072: a Fernet key as both restore paths accept it (deploy/restore.sh and deploy/claugine-install.sh,
#: fernet_keys_ok): 32 bytes in Base64, either alphabet (Fernet reads both)
FERNET_KEY = re.compile(r"^[A-Za-z0-9+/_-]{43}=$")


#: a Fernet token as it sits in a dump: version byte 0x80 then the timestamp - "gAAAAA" in URL-safe Base64
FERNET_TOKEN = re.compile(rb"gAAAAA[A-Za-z0-9_-]{50,}={0,2}")


def _check_env(path, dump):
    """The copy of portal.env a backup carries (core-048, commit-064, commit-071): readable, with a FIELD_ENCRYPTION_KEY
    of keys in the form both restore paths accept, which open every stored secret in the dump itself (commit-064: the
    snapshot the archive carries, not the live database) that the running portal opens. Else the backup fails before
    the archive is made, published or the older ones pruned."""
    from cryptography.fernet import Fernet, InvalidToken, MultiFernet

    from inventory import crypto

    try:
        with open(path, encoding="utf-8") as fh:
            keys = env_keys(fh.read())
    except OSError as exc:
        raise RuntimeError(f"portal.env cannot be read: {exc.strerror}; the backup would lack the key") from None
    if not keys:
        raise RuntimeError("portal.env has no FIELD_ENCRYPTION_KEY: the backup would lack the key")
    try:
        if not all(FERNET_KEY.match(k) for k in keys):
            raise ValueError
        carried = MultiFernet([Fernet(k.encode()) for k in keys])
    except (ValueError, TypeError):
        raise RuntimeError("portal.env: FIELD_ENCRYPTION_KEY is not a valid key") from None
    running = crypto._fernet()
    missed = 0
    with open(dump, "rb") as fh:
        for line in fh:
            for token in FERNET_TOKEN.findall(line):
                try:
                    running.decrypt(token)
                except InvalidToken:
                    continue  # not a secret of this portal, or unreadable here too: not this backup's to judge
                try:
                    carried.decrypt(token)
                except InvalidToken:
                    missed += 1
    if missed:
        raise RuntimeError(
            f"portal.env: its FIELD_ENCRYPTION_KEY does not open {missed} stored secret(s) in the dump that the portal "
            "opens; the backup would not restore them"
        )


def make_backup(s, folder, taken=None):
    """One backup as a folder <folder>/claugine_<fqdn>_<edition>_YYYYmmdd-HHMM/ of two files (owner 2026-11-12: one
    archive and one script per backup, no MANIFEST or README; accounts/backup_archive.py):

        <name>.7z    AES-256 with the archive password, names encrypted: database.sql, portal.env (the database
                     connection and FIELD_ENCRYPTION_KEY, the key to the stored secrets) and edition (core-024:
                     standard or enterprise; the installer and restore.sh refuse a restore onto the other edition)
        <name>.sh    the restore: checks the archive, downloads the public installer and runs its --restore; its
                     comment header says when, from where and by which release the backup was made

    Made in a hidden folder and renamed when complete. Returns its path."""
    from . import backup_archive, edition

    password = s.get_secret("backup_password")
    if not password:
        raise RuntimeError("no backup password set (Settings → Portal appliance)")
    final = os.path.join(folder, portal_backup_name(s))  # u07: claugine_<fqdn>_<edition>_YYYYmmdd-HHMM
    # commit-062: `taken(name)` - the bucket has that name already (an S3-only run stages in a fresh folder, so the
    # local check alone would give a second run of the same minute the first one's keys)
    if os.path.exists(final) or (taken is not None and taken(os.path.basename(final))):
        final += timezone.localtime().strftime("%S")
    name = os.path.basename(final)
    work = os.path.join(folder, "." + name + ".partial")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, mode=0o700)
    try:
        content = os.path.join(work, "content")
        os.makedirs(content, mode=0o700)
        env_file = getattr(settings, "ENV_FILE", "") or os.environ.get(
            "PORTAL_ENV_FILE", "/etc/claugine-portal/portal.env"
        )
        with capture_lock():  # core-040: the dump and the key that reads it, from the same side of a key rotation
            dump = _dump(content) or os.path.join(content, "database.sql")
            if dump != os.path.join(content, "database.sql"):
                shutil.move(dump, os.path.join(content, "database.sql"))
            # core-048 (twenty-fifth full review): a backup without the key to the stored secrets restores a
            # database whose credentials cannot be read - it fails here, before anything is published or pruned
            # commit-064, commit-071: the copy the archive carries is the one checked, against the database now
            try:
                shutil.copyfile(env_file, os.path.join(content, "portal.env"))
            except OSError as exc:
                raise RuntimeError(f"portal.env ({env_file}) cannot be read: {exc.strerror}") from None
            _check_env(os.path.join(content, "portal.env"), os.path.join(content, "database.sql"))
        commit = installed_commit()
        url = s.public_url or settings.SITE_URL
        # core-024: the edition goes inside the encrypted archive, where the installer and restore.sh read it to
        # refuse a restore onto the other edition; the script header only repeats it for a person
        which = edition.of_installation(s)
        with open(os.path.join(content, "edition"), "w", encoding="utf-8") as fh:
            fh.write(which + "\n")
        info = [
            f"made     {timezone.localtime().isoformat(timespec='seconds')} by {socket.getfqdn()}",
            f"portal   {url}",
            f"edition  {which} (restores only to an installation of the same edition)",
            f"release  {commit or 'unknown'} (a record: the restore installs the release the licence is entitled to "
            "and migrates this data forward)",
            "inside   database.sql (the database), edition, portal.env (the database connection and the key to the "
            "stored secrets, with this installation's id: the restore keeps it and the licence binding)",
        ]
        archive = os.path.join(work, name + backup_archive.ARCHIVE)
        backup_archive.make(content, archive, password)
        shutil.rmtree(content)
        backup_archive.write_script(work, name, "portal", archive, url=url, info=info)
        _fix_modes(work, _owner(folder, s))
        os.rename(work, final)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return final


def installed_commit():
    """The installed release (/opt/claugine-portal/COMMIT: vNN.NN.NN, written by the updater and the installer), or ""."""
    path = os.environ.get("PORTAL_COMMIT_FILE") or "/opt/claugine-portal/COMMIT"
    try:
        with open(path, encoding="utf-8") as fh:
            c = fh.read().strip()
    except OSError:
        return ""
    return c if re.fullmatch(r"[0-9a-f]{40}|v\d{2}\.\d{2}\.\d{2}", c) else ""


def _size(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    return sum(os.path.getsize(os.path.join(r, f)) for r, _d, fs in os.walk(path) for f in fs)


def _ours(s):
    """The folder-name prefix of *this* portal's own backups: claugine_<our fqdn>_…"""
    return (f"{ARCHIVE_PREFIX}_{portal_fqdn(s)}_",)


def _is_archive(name, s=None):
    """A portal backup folder.

    With `s`, only **this** portal's own backups (owner 2026-11-08): one folder may now hold the backups of several
    portals and of the platforms, and `prune_local()` deletes whatever this matches — so pruning is scoped to
    `claugine_<our fqdn>_*` and never touches another portal's, nor the platforms' `platform_*`.

    Matched on the full prefix **with its separator**, never on a bare prefix: `claugine` alone would also catch
    `claugine-platform_…` or any other folder beside the backups, and a future unscoped `prune_local()` must never
    delete a folder rather than a backup.
    """
    if name.endswith(".partial"):
        return False
    if s is not None:
        return name.startswith(_ours(s))
    return name.startswith(f"{ARCHIVE_PREFIX}_")


def prune_local(folder, days, s=None):
    cut = time.time() - days * 86400
    removed = 0
    for name in os.listdir(folder):
        p = os.path.join(folder, name)
        if _is_archive(name, s) and os.path.getmtime(p) < cut:
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
            removed += 1
    return removed


def backup(s=None, operation=None):
    """One portal backup at a time on this node (commit-062): see _backup. Owner 2026-11-18 (option A): no backup
    starts while the maintenance gate is closed - an operation that closed it itself (an update, the key change) names
    itself in `operation` and is let through; the gate counts a running backup (backups_running), so a switch over
    waits for it."""
    with run_lock("portal-backup"):
        held = gate_held(operation)
        if held:
            return f"skipped: {held} is running; the backup waits until it ends"
        return _backup(s)


#: the run locks of the backups (backup(), fe_backup.run()), counted by the maintenance gate (option A)
BACKUP_RUN_LOCKS = ("portal-backup", "platform-backup")


def gate_held(operation=None):
    """The operation holding the maintenance gate when a backup may not start now, else "" (owner 2026-11-18, option
    A). An operation that closed the gate itself passes its name and is let through."""
    from django.db import transaction

    from . import maintenance

    # commit-087: read under the gate's row lock, the one maintenance.close() counts the running work under - the
    # caller holds its run lock already, so either the closing count sees that lock (and the maintenance waits) or this
    # read sees the closed gate (and the backup waits); a plain read could see the gate open while it was closing
    with transaction.atomic():
        g = maintenance._row(lock=True)
        closed, op = maintenance._closed(g), g.op
    # an operation's two halves (update-portal and update-portal-finish) hold the same gate
    if not closed or (
        operation and (op == operation or op.startswith(operation + "-") or operation.startswith(op + "-"))
    ):
        return ""
    return op or "a maintenance"


def backups_running():
    """How many backup runs (portal, platforms) hold their run lock on this node now - counted by the maintenance gate
    (accounts.tasks.running_count), so a switch over or an update waits for them (owner 2026-11-18, option A). Only
    root reads the locks (the agent); elsewhere 0."""
    import fcntl

    from . import ha_agent

    n = 0
    for name in BACKUP_RUN_LOCKS:
        path = os.path.join(RUN_LOCK_DIR or ha_agent.STATE_DIR, f"{name}.lock")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:  # no such lock yet, or not root's to read
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            n += 1  # held: a backup runs
        finally:
            os.close(fd)
    return n


def _backup(s=None):
    """Make one backup now (as root). u07: one run a day. The local folder (if its switch is on) holds the backup in a
    claugine-portal/ subfolder, and if S3 is on that same folder is copied to the bucket (one dir per backup, no root
    folder). If local is off, the backup is staged in a temp folder, uploaded to S3, then discarded. Returns the
    result line; kept in Settings."""
    from accounts.models import audit

    s = s or _settings()
    from accounts.edition import effective as _eff  # Standard: local folder only, fixed retention

    do_local = bool(_eff(s, "backup_local") and (_eff(s, "backup_path") or "").strip())
    do_s3 = s3_allowed(s)  # integration-006: the licence's s3_backups too (S3 is the Enterprise edition's, ee/s3.py)
    started = timezone.now()
    tmp = tempfile.mkdtemp(prefix="claugine-backup-")
    os.chmod(tmp, 0o700)
    try:
        if not do_local and not do_s3:
            raise RuntimeError("no destination: switch on a local folder and/or an S3 bucket in Settings → Backups")
        taken = None
        # commit-062: a name the bucket has already gets its seconds, as one in the local folder does - asked only for
        # an S3-only run (commit-070: a local backup never waits for, or fails on, the bucket; upload_folder refuses to
        # write over a folder that is there in any case)
        if do_s3 and not do_local:

            def taken(name):
                s3 = _s3()
                return s3.folder_exists(s3.conf_of(s), name)

        if do_local:  # the dated folder is created straight in the configured folder (owner 2026-11-08)
            base = local_backup_dir(s)
            made_base = not os.path.isdir(base)  # we only own what we create
            os.makedirs(base, mode=DIR_MODE, exist_ok=True)
            path = make_backup(s, base, taken)
            # owner 2026-11-08: scoped to this portal's own backups — the folder also holds the platforms' and
            # may hold another portal's. The bucket keeps its own (its lifecycle rules).
            prune_local(base, _eff(s, "backup_keep_days"), s)
            # owner 2026-11-08: the folder is shared with the platforms' backups now, so set the folder itself and
            # only this run's own backup — never walk the whole folder and restamp someone else's files.
            _own = _owner(base, s)
            if made_base:
                _fix_one(base, _own)  # the configured folder is the operator's unless we had to create it
            _fix_modes(path, _own)  # every dir and file of this run's backup
        else:
            path = make_backup(s, tmp, taken)  # no local copy this run: staged only to upload to S3
        size = _size(path)
        where = [path] if do_local else []
        if do_s3:  # u07: one dir per backup, directly under the bucket (no root prefix folder)
            s3 = _s3()
            try:
                where.append(s3.upload_folder(s3.conf_of(s), path, ""))
            except Exception as exc:  # noqa: BLE001 - a local copy may be there; the run counts as failed
                raise RuntimeError(f"S3: {exc}" + (f" (kept in {path})" if do_local else "")) from exc
        result = f"ok: {' + '.join(where)} ({size // 1024} KiB, {int((timezone.now() - started).total_seconds())} s)"
        ok = True
    except Exception as exc:  # noqa: BLE001 - kept as the result
        err = exc.stderr.decode(errors="replace").strip() if isinstance(getattr(exc, "stderr", None), bytes) else ""
        result = f"failed: {type(exc).__name__}: {exc} {err}".strip()[:1000]
        ok = False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    from tenants.models import PortalSettings

    PortalSettings.objects.filter(pk=s.pk).update(backup_last_at=started, backup_last_result=result)
    audit(None, "portal_backup", s, ok=ok, result=result[:300])
    if not ok:  # u06: e-mail only on errors
        from django.utils.translation import gettext as _g

        notify_backup_failure(_g("Portal backup failed"), result)
    return result


def notify_backup_failure(subject, text):
    """u06: the super admins are e-mailed when a backup fails (success is silent)."""
    try:
        from accounts.mailtemplates import T
        from accounts.models import Kind, User
        from accounts.utils import notify

        admins = list(User.objects.filter(kind=Kind.STAFF, is_super_admin=True, is_active=True, login_enabled=True))
        if admins:
            notify(admins, T.BACKUP_FAILED, {"text": text, "subject": subject}, subject)
    except Exception:  # noqa: BLE001 - never let notification break a backup
        pass


LE_STATE = "/var/lib/claugine-ha/le_email"  # the Let's Encrypt account e-mail this VM has (u01)


def apply_le_email(log=print, dry=False):
    """u01: Settings → Portal → Let's Encrypt e-mail onto this VM's Let's Encrypt account (each portal VM has its own,
    from claugine-lb certificate). Only the account's contact changes; the certificates stay as they are."""
    from . import haproxy

    email = haproxy.get().get("le_email", "")
    if not email or not shutil.which("certbot") or not os.path.isdir("/etc/letsencrypt/accounts"):
        return
    try:
        with open(LE_STATE, encoding="utf-8") as fh:
            if fh.read().strip() == email:
                return
    except OSError:
        pass
    if dry:
        log("Let's Encrypt account: e-mail to update")
        return
    r = subprocess.run(
        ["certbot", "update_account", "--email", email, "--no-eff-email", "--non-interactive"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if r.returncode != 0:
        log(f"Let's Encrypt account: not updated: {(r.stdout + r.stderr).strip()[-200:]}")
        return
    os.makedirs(os.path.dirname(LE_STATE), exist_ok=True)
    with open(LE_STATE, "w", encoding="utf-8") as fh:
        fh.write(email + "\n")
    log("Let's Encrypt account: e-mail updated")


LB_TOOL = "/usr/local/sbin/claugine-lb"
LB_ARGS = "/etc/claugine-portal/lb.args"  # written by claugine-lb: the options applied last
LB_STATE = "/etc/claugine-portal/lb.state"  # the last automatic run: {at, cmd, ok, sig, script, out}
LB_SCRIPT = "/opt/claugine-portal/app/deploy/install-lb.sh"  # what claugine-lb runs: the installed release's
LB_RETRY = 6 * 3600  # a run that failed is not repeated for the same settings before this (Let's Encrypt limits)


LB_LIVE = "/etc/letsencrypt/live"  # certbot's certificates, one folder per certificate (lineage)
LB_CERTS = {"--public-name": "claugine-portal", "--one-name": "claugine-one"}  # install-lb.sh CERT_NAME, ONE_CERT


def _lb_names(args):
    """The names of each certificate in install-lb.sh's options (commit-187: each certificate on its own)."""
    out, it = {c: set() for c in LB_CERTS.values()}, iter(args)
    for a in it:
        if a in LB_CERTS:
            out[LB_CERTS[a]] |= {n.lower() for n in next(it, "").split(",") if n}
    return out


def _cert_names(cert):
    """The DNS names of the certificate certbot keeps and renews (its lineage's current certificate - certbot renews
    for these, commit-189); None without one."""
    from cryptography import x509

    try:
        with open(os.path.join(LB_LIVE, cert, "cert.pem"), "rb") as fh:
            c = x509.load_pem_x509_certificate(fh.read())
        san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except (OSError, ValueError, x509.ExtensionNotFound):
        return None
    return {n.lower() for n in san.get_values_for_type(x509.DNSName)}


def _lb_stale(want):
    """commit-187: the certificates whose names are not the names wanted now - a name removed (an Admin Portal
    URL cleared) is not renewed with the others, even when HAProxy applied the removal before."""
    out = []
    for cert, names in _lb_names(want).items():
        have = _cert_names(cert)
        if names and have is not None and have != names:
            out.append(cert)
    return out


def lb_state():
    try:
        with open(LB_STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def apply_lb(log=print, dry=False):
    """HAProxy follows Settings → Portal and Platform by itself (2026-11-14, decision 0010: no "claugine-lb apply" on
    the console). Every node, every 5 minutes, as root: when the options built from the settings and node.env differ
    from those applied last (lb.args), or when the release brought another install-lb.sh than the one applied last
    (its SHA-256 in lb.state; v01.00.35 changed what HAProxy serves without changing the options, and nothing was
    applied), it runs claugine-lb apply - or claugine-lb certificate when a name is added or removed and the Let's Encrypt e-mail
    is set. A failed run is kept in lb.state and not repeated for the same settings and script for 6 hours. Returns
    what it did, or ""."""
    import hashlib

    from . import haproxy, thisnode

    if not os.path.isfile(LB_TOOL):
        return ""
    conf = haproxy.get()
    try:
        want = haproxy.args_for(conf, thisnode.read())
    except haproxy.Incomplete:
        return ""  # Settings → Portal and Platform not complete for this node: nothing to apply
    have = [x for x in _read(LB_ARGS).splitlines() if x]
    try:
        with open(LB_SCRIPT, "rb") as fh:
            script = hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        script = ""
    st = lb_state()
    stale = _lb_stale(want) if conf.get("le_email") else []
    sig = hashlib.sha256(("\n".join(want) + "\n" + script).encode()).hexdigest()
    if have == want and st.get("script", "") == script and st.get("ok", True):
        recent = st.get("cmd") == "certificate" and st.get("sig") == sig
        if not stale or (recent and time.time() - float(st.get("at") or 0) < LB_RETRY):
            return ""  # applied, and it worked (commit-011: a failed run goes on to the back-off below and is retried;
            # commit-187: a certificate requested for these names is not requested again within the back-off)
    if st.get("sig") == sig and not st.get("ok") and time.time() - float(st.get("at") or 0) < LB_RETRY:
        return ""
    # a certificate's names changed - added or removed, compared with what was applied and with the issued certificate
    # (commit-187): the certificates are requested again for the names wanted now
    changed = _lb_names(want) != _lb_names(have) or stale
    cmd = "certificate" if changed and conf.get("le_email") else "apply"
    if dry:
        log(f"HAProxy: claugine-lb {cmd} would run (the settings or install-lb.sh changed)")
        return cmd
    r = subprocess.run([LB_TOOL, cmd], capture_output=True, text=True, timeout=1200, check=False)
    ok = r.returncode == 0
    state = {
        "at": time.time(),
        "cmd": cmd,
        "ok": ok,
        "sig": sig,
        "script": script,
        "out": (r.stdout + r.stderr)[-1500:],
    }
    with open(LB_STATE + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.chmod(LB_STATE + ".tmp", 0o644)
    os.replace(LB_STATE + ".tmp", LB_STATE)
    msg = f"HAProxy: claugine-lb {cmd}: {'ok' if ok else 'failed'}"
    log(msg)
    return msg


def tick(log=print, force=False, dry=False, operation=None):
    """What the appliance timer does every 5 minutes (as root): keep the mail relay in line and back up when due."""
    s = _settings()
    if os.geteuid() == 0:
        try:
            if not dry and capture_mail(s, log):
                s = _settings()
            from accounts import ha

            if not dry and ha.is_active() and ensure_dkim_key(s):  # owner 2026-11-17: "Portal makes the key"
                s.save(update_fields=["mail_dkim_key_encrypted"])
                log("DKIM key made")
            apply_mail(s, log, dry=dry)
        except Exception as exc:  # noqa: BLE001
            log(f"mail relay: {exc}")
        try:
            apply_le_email(log, dry=dry)
        except Exception as exc:  # noqa: BLE001 - the next round tries again
            log(f"Let's Encrypt account: {exc}")
        try:
            apply_lb(log, dry=dry)
        except Exception as exc:  # noqa: BLE001 - the next round tries again
            log(f"HAProxy: {exc}")
        if not dry:  # v01.01.00: a pair recovers from a failed switch over by itself (ee/ha_agent.recover_tick)
            from accounts import ha_agent

            try:
                r = ha_agent._pair().recover_tick(log) if ha_agent._pair() else ""
                if r:
                    log(r)
            except Exception as exc:  # noqa: BLE001 - the next round tries again
                log(f"pair recovery: {exc}")
    if dry:
        log(
            f"backup due: {force or due(s)}; mail relay {s.mail_host or '-'}:{s.mail_port}, account {'set' if s.mail_user else '-'}, "
            f"DKIM {'set' if s.has_secret('mail_dkim_key') else '-'}"
        )
        return
    from accounts import ha

    if not ha.is_active():  # only the active portal node backs up
        if force:
            log(f"no backup: this portal node is {ha.role()}, not active")
        return
    if force or due(s):  # u07: one backup a day (local folder and/or S3, written by the same run)
        log(backup(s, operation=operation))
    if os.geteuid() == 0:  # u04: the platforms' front ends, daily or when asked (the active node only)
        from . import fe_backup

        try:
            fe_backup.tick(log)
        except Exception as exc:  # noqa: BLE001 - the next round tries again
            log(f"platform backups: {exc}")
    if os.geteuid() == 0:  # core-033: approved network changes that never started, once nothing blocks them
        from tenants import netops

        try:
            r = netops.start_waiting(log)
            if r:
                log(f"network changes started: {r}")
        except Exception as exc:  # noqa: BLE001 - the next round tries again
            log(f"network changes: {exc}")
    if os.geteuid() == 0:  # owner 2026-10-31: new releases, when switched on (the active node only)
        from . import autoupdate

        try:
            r = autoupdate.tick(log)
            if r not in ("off", "checked recently"):
                log(f"auto-update: {r}")
        except Exception as exc:  # noqa: BLE001 - the next round tries again
            log(f"auto-update: {exc}")


def restore_steps(s):
    """The restore, step by step, with the newest backup's name and the public URL filled in."""
    from django.utils.translation import gettext as _

    newest = next((n for n, _size, _at in last_local(s, 1)), "")
    if not newest:  # the folder may not be readable by the web server: the last backup's result names it
        m = re.match(r"^ok: (\S+) \(", s.backup_last_result or "")
        newest = os.path.basename(m.group(1)) if m else ""
    newest = newest or portal_backup_name(s).rsplit("_", 1)[0] + "_YYYYmmdd-HHMM"
    from accounts.edition import effective as _eff

    _path = (_eff(s, "backup_path") or "").strip()  # the folder actually in use (Standard pins it)
    if _path:  # u05: from the local folder if there is one
        first = (
            _("Copy the backup's two files (the archive and its script) to a freshly installed Ubuntu 26.04 VM:"),
            f"scp {local_backup_dir(s).rstrip('/')}/{newest}/{newest}.* <user>@<new VM>:",
        )
    else:  # u05: S3 only — from the bucket (any S3 client, e.g. the AWS CLI)
        pre = "/".join(x for x in ((s.backup_s3_prefix or "").strip("/"), newest) if x)
        first = (
            _("Copy the backup's two files from the S3 bucket to a freshly installed Ubuntu 26.04 VM (any S3 client):"),
            f"aws s3 cp --recursive s3://{s.backup_s3_bucket}/{pre}/ ~/ --endpoint-url {s.backup_s3_endpoint}",
        )
    return [
        first,
        (
            _(
                "On the new VM, as the user you copied them to (passwordless sudo, not root): the script checks the "
                "archive, downloads the installer, installs the release the licence is entitled to, asks for the "
                "archive password once, loads the backup and migrates it forward (Enterprise: add --second "
                "<user>@<the other VM>):"
            ),
            f"bash {newest}.sh --licence-key <licence key>",
        ),
    ]


def local_backup_dir(s):
    """u07: the portal backups live under <local folder>/claugine-portal/."""
    from accounts.edition import effective as _eff

    base = (_eff(s, "backup_path") or "").strip()
    return os.path.join(base, LOCAL_SUBDIR) if LOCAL_SUBDIR else base


def last_local(s, n=5):
    """The newest local backups [(name, size, time)] (when the folder is readable)."""
    base = local_backup_dir(s)
    try:
        names = [x for x in os.listdir(base) if _is_archive(x, s)]
    except OSError:
        return []
    out = []
    for name in sorted(names, key=backup_stamp, reverse=True)[:n]:
        p = os.path.join(base, name)
        try:
            out.append(
                (name, _size(p), datetime.fromtimestamp(os.path.getmtime(p), tz=timezone.get_current_timezone()))
            )
        except OSError:
            pass
    return out


def s3_allowed(s):
    """integration-006 (tenth full review): S3 for the portal's own backups - switched on in Settings, allowed by the
    licence (edition.allows("s3_backups"), as the platform backups check) and the Enterprise code present. The stored
    setting is kept either way."""
    from accounts import edition

    return bool(edition.effective(s, "backup_s3")) and edition.allows("s3_backups", s) and _s3() is not None


def s3_backups(s, n=10):
    """Best-effort: the backup folders in the S3 bucket (newest first), or [] when S3 is off or the bucket can't be
    listed. A listing problem must never blank the Settings page."""

    s3 = _s3()
    if not s3_allowed(s) or not (getattr(s, "backup_s3_bucket", "") or "").strip():
        return []
    try:
        # only the portal's own backup folders (claugine-portal[_-]…) — not the platforms' "platforms/" prefix
        ours = [x for x in s3.list_objects(s3.conf_of(s), prefix=_ours(s)[0]) if _is_archive(x, s)]  # commit-063
        return sorted(ours, key=backup_stamp, reverse=True)[:n]
    except Exception:  # noqa: BLE001 - best-effort display only
        return []


# --- the Settings form --------------------------------------------------------------------------------------------


def settings_form(*args, **kwargs):
    from django import forms
    from django.utils.translation import gettext_lazy as _l

    class ApplianceForm(forms.Form):
        mail_mode = forms.ChoiceField(
            label=_l("Mail relay"),
            choices=[
                ("internal", _l("internal: this VM's relay (postfix, loopback only)")),
                ("direct", _l("direct: this VM delivers to the recipients' mail servers")),
                ("external", _l("external SMTP server")),
            ],
            help_text=_l(
                "direct: each portal node sends from its own public address under its mail name "
                "(MAIL_NAME in node.env, the name of its PTR record); the sender domain needs SPF "
                "for these addresses and the DKIM key below."
            ),
        )
        mail_from = forms.CharField(
            label=_l("Sender address"),
            required=False,
            max_length=200,
            help_text=_l("portal@example.kz or Example Cloud <portal@example.kz>"),
        )
        mail_host = forms.CharField(
            label=_l("SMTP server"),
            required=False,
            max_length=200,
            help_text=_l(
                "internal: where this VM's relay sends (e.g. smtp-relay.brevo.com); external: where the portal sends."
            ),
        )
        mail_port = forms.IntegerField(label=_l("SMTP port"), min_value=1, max_value=65535, initial=587)
        mail_tls = forms.BooleanField(label=_l("STARTTLS"), required=False)
        mail_user = forms.CharField(label=_l("SMTP user"), required=False, max_length=200)
        mail_password = forms.CharField(
            label=_l("SMTP password"),
            required=False,
            strip=False,
            widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        )
        mail_dkim_domain = forms.CharField(label=_l("DKIM domain"), required=False, max_length=200)
        mail_dkim_selector = forms.RegexField(
            label=_l("DKIM selector"), required=False, max_length=63, regex=r"^[A-Za-z0-9-]+$"
        )
        mail_dkim_key = forms.CharField(
            label=_l("DKIM private key"),
            required=False,
            strip=False,
            widget=forms.Textarea(attrs={"rows": 3, "class": "mono", "autocomplete": "off", "spellcheck": "false"}),
            help_text=_l("PEM. Internal relay or direct delivery."),
        )
        backup_local = forms.BooleanField(
            label=_l("to a local folder"), required=False
        )  # u08: the local destination's switch
        backup_path = forms.CharField(
            label=_l("Local folder"),
            required=False,
            max_length=300,
            help_text=_l("Each backup is a folder claugine_<name>_<edition>_<date> created here."),
        )
        backup_owner = forms.CharField(
            label=_l("Owner (user:group)"),
            required=False,
            max_length=64,
            help_text=_l(
                "The local backups (portal and platforms) are set to this owner, e.g. portal:portal. Empty: the local folder's own owner."
            ),
        )
        backup_start = forms.TimeField(
            label=_l("Backup at"),
            help_text=_l("Once a day, e.g. 00:45."),  # u07: one backup a day
            widget=forms.TimeInput(format="%H:%M"),
        )
        backup_keep_days = forms.IntegerField(label=_l("Keep, days"), min_value=1, max_value=3650)
        backup_password = forms.CharField(
            label=_l("Archive password"),
            required=False,
            strip=False,
            widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
            help_text=_l("At least 12 characters. Keep it apart: it is needed to restore."),
        )

        def __init__(self, *a, has_backup_password=False, settings_obj=None, **kw):
            super().__init__(*a, **kw)
            s3 = _s3()
            if s3 is not None:  # the bucket's fields (Enterprise, ee/s3.py)
                self.fields.update(s3.form_fields())
            if (
                "backup_s3_region" in self.fields
            ):  # owner 2026-11-17 16:33 "ok bring it back": optional, empty is us-east-1
                self.fields["backup_s3_region"].widget.attrs.update(
                    {
                        "title": _l("Region: empty - taken from the endpoint (s3.<region>.<host>), else us-east-1"),
                        "aria-label": _l("Region"),
                    }
                )
                if self.initial.get("backup_s3_region") == "us-east-1":
                    self.initial["backup_s3_region"] = ""
            for f, ph in (  # owner 2026-11-17: the S3 fields share a row, so they say what they are
                ("backup_s3_endpoint", "https://s3.example.com"),
                ("backup_s3_bucket", "bucket"),
                ("backup_s3_access_key", "access key"),
                ("backup_s3_region", _l("region")),
                ("backup_owner", "user:group"),
            ):
                if f in self.fields:
                    self.fields[f].widget.attrs.setdefault("placeholder", ph)
            self.has_backup_password = has_backup_password
            self.settings_obj = settings_obj
            from django.utils.translation import gettext as _g

            for f in [
                x for x in ("mail_password", "backup_password", "mail_dkim_key", "backup_s3_secret") if x in self.fields
            ]:  # a kept value shows as set
                set_ = settings_obj is not None and settings_obj.has_secret(f)
                self.fields[f].widget.attrs.update(
                    {
                        "placeholder": _g("●●●●●●●● set — empty keeps it") if set_ else _g("not set"),
                        "data-secret": "set" if set_ else "empty",
                    }
                )
            # owner 2026-11-08 (editions): a setting the edition pins is shown with the value in force and cannot be
            # edited here. Django keeps a disabled field's initial value and ignores what was submitted for it, so
            # this is the enforcement as well as the look of it; the rest of the code still reads every one of these
            # through accounts.edition.effective(), which is what makes a stored value harmless.
            from accounts import edition as _ed

            pinned = _ed.fixed(settings_obj)
            locked = set(_ed.locked(settings_obj))
            if s3 is not None and not _ed.allows("s3_backups", settings_obj):
                locked |= set(s3.LOCKED_WITHOUT_FEATURE)
            why = _g("Fixed in the Standard edition.")
            for name in sorted(locked & set(self.fields)):
                fld = self.fields[name]
                fld.disabled = True
                fld.help_text = f"{fld.help_text} {why}" if fld.help_text else why
                if name in pinned:
                    self.initial[name] = _ed.effective(settings_obj, name)

        def clean(self):
            d = super().clean()
            if d.get("mail_mode") == "external" and not d.get("mail_host"):
                self.add_error("mail_host", _l("Required for an external server."))
            if d.get("mail_from"):
                from email.utils import parseaddr

                from django.core.validators import validate_email

                try:
                    validate_email(parseaddr(d["mail_from"])[1])
                except forms.ValidationError:
                    self.add_error("mail_from", _l("An email address, optionally with a name: Name <address>."))
            if d.get("mail_dkim_key") and "PRIVATE KEY" not in d["mail_dkim_key"]:
                self.add_error("mail_dkim_key", _l("A PEM private key."))
            s3 = _s3()
            if s3 is not None:
                s3.clean_form(self, d)
            if d.get("backup_path") and not d["backup_path"].startswith("/"):
                self.add_error("backup_path", _l("An absolute path."))
            if (
                d.get("backup_local") and not d.get("backup_path") and "backup_path" not in self.errors
            ):  # u08: local on needs a folder
                self.add_error("backup_path", _l("Required for a local copy."))
            if not (d.get("backup_local") or d.get("backup_s3")):  # u08: at least one destination switched on
                self.add_error(
                    "backup_local",
                    _l("Switch on a local folder and/or an S3 bucket.")
                    if _s3() is not None
                    else _l("Switch on the local folder."),
                )
            pw = d.get("backup_password") or ""
            if pw and len(pw) < 12:
                self.add_error("backup_password", _l("At least 12 characters."))
            if (
                (d.get("backup_local") or d.get("backup_s3")) and not pw and not self.has_backup_password
            ):  # u08: only when a destination is on
                self.add_error("backup_password", _l("Required: backups are encrypted."))
            return d

    return ApplianceForm(*args, **kwargs)


FORM_FIELDS = (
    "mail_mode",
    "mail_from",
    "mail_host",
    "mail_port",
    "mail_tls",
    "mail_user",
    "mail_dkim_domain",
    "mail_dkim_selector",
    "backup_local",
    "backup_path",
    "backup_owner",
    "backup_start",
    "backup_keep_days",
    "backup_s3",
    "backup_s3_endpoint",
    "backup_s3_region",
    "backup_s3_bucket",
    "backup_s3_access_key",
    "backup_s3_style",
)


def form_initial(s):
    return {f: getattr(s, f) for f in FORM_FIELDS}


def _s3():
    """ee/s3.py — backups to an S3 bucket — or None in a build without ee/ (Standard: a local folder only)."""
    from accounts import edition

    return edition.ee("s3")
