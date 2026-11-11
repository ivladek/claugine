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
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend
from django.utils import timezone

ARCHIVE_PREFIX = "claugine"  # owner 2026-11-08: a backup is a folder claugine_<fqdn>_YYYYmmdd-HHMM
LOCAL_SUBDIR = ""            # owner 2026-11-08: backups go straight into the configured folder, with no subfolder
# backups made before 2026-11-08 (claugine-portal_… and the older claugine-portal-…). They are still listed and
# still restorable, so a rollback to before the rename keeps working. Note the portal's and the platforms'
# backups now share one folder, so the match must be exact: "claugine" alone would also catch claugine-platform_….
LEGACY_PREFIXES = ("claugine-portal",)
_CACHE = {"at": 0.0, "url": "", "host": "", "test_url": ""}


def portal_fqdn(s=None):
    """u07: the portal's host name, for the backup folder name (e.g. portal.example.com)."""
    from urllib.parse import urlsplit
    s = s or _settings()
    url = s.public_url or getattr(settings, "SITE_URL", "") or ""
    return urlsplit(url).hostname or (url.split("//")[-1].split("/")[0] if url else "") or "portal"


def portal_backup_name(s=None, when=None):
    """claugine_<fqdn>_YYYYmmdd-HHMM (owner 2026-11-08; was claugine-portal_… before)."""
    return f"{ARCHIVE_PREFIX}_{portal_fqdn(s)}_{(when or timezone.localtime()).strftime('%Y%m%d-%H%M')}"


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
        except Exception:  # noqa: BLE001 - before migrations, no database
            url, test = "", ""
        _CACHE.update(at=now, url=url, test_url=test, host=urlsplit(url).hostname or "" if url else "")
    return _CACHE["url"] or settings.SITE_URL


def forget():
    _CACHE["at"] = 0.0


class PublicHostMiddleware:
    """Accept the public URL's host (Settings) besides ALLOWED_HOSTS, and its origin for CSRF."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        for url in (public_url(), _CACHE.get("test_url", "")):
            p = urlsplit(url.rstrip("/"))
            if not p.scheme:
                continue
            host = p.hostname or ""
            if host and host not in settings.ALLOWED_HOSTS and "*" not in settings.ALLOWED_HOSTS:
                settings.ALLOWED_HOSTS.append(host)
            origin = f"{p.scheme}://{p.netloc}"
            if origin not in settings.CSRF_TRUSTED_ORIGINS:
                settings.CSRF_TRUSTED_ORIGINS.append(origin)
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
        if not s.client_mail:  # owner 2026-10-29: only platform users get e-mail until it is unblocked in Settings
            email_messages = hold_client_mail([m for m in email_messages if m not in allowed]) + allowed
            if not email_messages:
                return 0
        sender = s.mail_from or settings.DEFAULT_FROM_EMAIL
        email_messages = [with_html(m) for m in email_messages]
        for m in email_messages:
            _branded(m)
            _inline_logo(m)
            if not m.from_email or m.from_email == settings.DEFAULT_FROM_EMAIL:
                m.from_email = sender
        if s.mail_mode == "external" and s.mail_host:
            smtp = SmtpBackend(host=s.mail_host, port=s.mail_port, username=s.mail_user or None,
                               password=s.get_secret("mail_password") or None, use_tls=s.mail_tls, timeout=20,
                               fail_silently=False)
        else:
            smtp = SmtpBackend(host="127.0.0.1", port=25, use_tls=False, timeout=10, fail_silently=False)
        try:
            return smtp.send_messages(email_messages) or 0
        except (OSError, Exception) as exc:  # noqa: BLE001 - kept in files instead
            from accounts.models import audit
            audit(None, "mail_failed", None, relay=s.mail_mode, error=f"{type(exc).__name__}: {exc}"[:300],
                  messages=len(email_messages))
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
    pat = re.compile(r'src="[^"]*/brand/logo\.png(?:\?v=\d+)?"')
    if not pat.search(html):
        return m
    from email.mime.image import MIMEImage
    from tenants.models import PortalSettings
    logo = PortalSettings.get().logo
    if not logo:
        return m
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
    n = EmailMultiAlternatives(m.subject, m.body, m.from_email, m.to, bcc=m.bcc, cc=m.cc, reply_to=m.reply_to,
                               headers=m.extra_headers, attachments=m.attachments)
    n.attach_alternative(html, "text/html")
    return n


def platform_addresses():
    from accounts.models import Kind, User
    return {e.lower() for e in User.objects.filter(kind=Kind.STAFF, is_active=True).values_list("email", flat=True) if e}


def hold_client_mail(email_messages):
    """The messages with their recipients cut to platform users; what was cut is kept in files (var/mail-held) and
    logged, never sent."""
    import copy
    from email.utils import parseaddr
    from django.core.mail.backends.filebased import EmailBackend as FileBackend
    from accounts.models import audit
    staff = platform_addresses()
    keep, held = [], []
    for m in email_messages:
        def mine(lst):
            return [a for a in lst or [] if parseaddr(a)[1].lower() in staff]
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
DKIM_SOCKET = "inet:8891@127.0.0.1"     # OpenDKIM's notation of its listening socket
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
    """Once: the internal relay as it is set up on this VM (upstream relay and its account, DKIM key) goes into
    Settings, so that a restored VM gets the same. Values are never printed. Root only."""
    import re
    if s.mail_mode != "internal" or s.mail_host or not shutil.which("postconf"):
        return False
    m = re.match(r"^\[?([^\]\s:]+)\]?(?::(\d+))?$", _postconf("relayhost"))
    if not m:
        return False
    s.mail_host, s.mail_port = m.group(1), int(m.group(2) or 25)
    s.mail_tls = _postconf("smtp_tls_security_level") in ("encrypt", "verify", "secure")
    for line in _read(SASL_FILE).splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].strip("[]").split("]")[0].startswith(m.group(1)) and ":" in parts[1]:
            user, password = parts[1].split(":", 1)
            s.mail_user = user
            s.set_secret("mail_password", password.strip())
            break
    conf = _dkim_conf()
    key = _read(conf.get("KeyFile", "")) if conf.get("KeyFile") else ""
    if key and "PRIVATE KEY" in key:
        s.mail_dkim_domain, s.mail_dkim_selector = conf.get("Domain", ""), conf.get("Selector", "")
        s.set_secret("mail_dkim_key", key)
    if not s.mail_from and settings.DEFAULT_FROM_EMAIL and "localhost" not in settings.DEFAULT_FROM_EMAIL:
        s.mail_from = settings.DEFAULT_FROM_EMAIL[:200]
    s.save()
    from accounts.models import audit
    audit(None, "mail_settings_captured", s, relay=f"{s.mail_host}:{s.mail_port}", dkim=s.mail_dkim_selector or "-")
    log(f"mail relay settings taken from this VM: {s.mail_host}:{s.mail_port}")
    return True


def mail_name():
    """direct delivery: this node's mail name (the name of the PTR record of its public address), from node.env."""
    from accounts import thisnode
    return thisnode.env().get("MAIL_NAME", "")


def apply_mail(s, log, dry=False):
    """Keep this VM's postfix (loopback only) in line with Settings and sign with DKIM (OpenDKIM). internal: relays
    through the SMTP server with its account; direct: delivers to the recipients' mail servers itself, under this
    node's mail name. Changes only what differs. Root only."""
    if s.mail_mode not in ("internal", "direct") or not shutil.which("postconf"):
        return
    if s.mail_mode == "internal" and not s.mail_host:
        return
    changed = []
    if s.mail_mode == "direct":
        # the portal's mail carries its sender (portal@<domain>) as the envelope sender; the VM's own mail (cron, root)
        # stays on the VM under its mail name instead of going out to <user>@<sender domain>
        want = {"inet_interfaces": "loopback-only", "inet_protocols": "ipv4", "relayhost": "",
                "myorigin": "$myhostname", "mydestination": "$myhostname, localhost",
                "smtp_tls_security_level": "may", "smtp_sasl_auth_enable": "no"}
        if mail_name():
            want["myhostname"] = mail_name()
    else:
        want = {"inet_interfaces": "loopback-only", "relayhost": f"[{s.mail_host}]:{s.mail_port}",
                "smtp_tls_security_level": "encrypt" if s.mail_tls else "may"}
    if s.mail_mode == "direct" and _read(SASL_FILE).strip():  # no relay account any more: not kept on disk
        if dry:
            changed.append("relay account removed")
        else:
            with open(os.open(SASL_FILE, os.O_WRONLY | os.O_TRUNC), "w", encoding="utf-8") as fh:
                fh.write("")
            subprocess.run(["postmap", SASL_FILE], check=False)
            changed.append("relay account removed")
    if s.mail_user and s.mail_mode == "internal":
        want.update(smtp_sasl_auth_enable="yes", smtp_sasl_password_maps=f"hash:{SASL_FILE}",
                    smtp_sasl_security_options="noanonymous")
        line = f"[{s.mail_host}]:{s.mail_port} {s.mail_user}:{s.get_secret('mail_password')}\n"
        if _read(SASL_FILE) != line and dry:
            changed.append("relay account")
        elif _read(SASL_FILE) != line:
            with open(os.open(SASL_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
                fh.write(line)
            subprocess.run(["postmap", SASL_FILE], check=True)
            changed.append("relay account")
    key = s.get_secret("mail_dkim_key") if s.has_secret("mail_dkim_key") else ""
    if key and s.mail_dkim_domain and s.mail_dkim_selector and os.path.exists(DKIM_CONF):
        want.update(smtpd_milters=POSTFIX_MILTER, non_smtpd_milters=POSTFIX_MILTER, milter_default_action="accept",
                    milter_protocol="6")
        folder = f"/etc/opendkim/keys/{s.mail_dkim_domain}"
        path = f"{folder}/{s.mail_dkim_selector}.private"
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
        lines = {"Domain": s.mail_dkim_domain, "Selector": s.mail_dkim_selector, "KeyFile": path, "Socket": DKIM_SOCKET,
                 "Mode": "s", "Canonicalization": "relaxed/simple", "OversignHeaders": "From", "SyslogSuccess": "yes"}
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
    if not (_eff(s, "backup_local") or _eff(s, "backup_s3")):  # no destination switched on
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
    cmd = ["mysqldump", "--single-transaction", "--set-gtid-purged=OFF", "--routines", "--no-tablespaces",
           "--default-character-set=utf8mb4", "-h", db.get("HOST") or "localhost", "-u", db["USER"], db["NAME"]]
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
    for root, dirs, files in os.walk(path):
        for x in [root] + [os.path.join(root, f) for f in files]:
            os.chmod(x, DIR_MODE if os.path.isdir(x) else FILE_MODE)
            if owner and os.geteuid() == 0:
                try:
                    os.chown(x, *owner)
                except OSError:
                    pass  # e.g. an NFS share that maps root: the modes still hold


def make_backup(s, folder):
    """One backup as a folder <folder>/claugine-portal-YYYYmmdd-HHMM/:

        database.sql.gz           the database
        portal.env.zip            the database connection and FIELD_ENCRYPTION_KEY (the key to the stored secrets),
                                  in a zip protected by the archive password (AES-256)
        COMMIT                    the release of the portal that made it (a record; the restore installs the current
                                  release and migrates the backup forward)
        README, MANIFEST

    Made in a hidden folder and renamed when complete. Returns its path."""
    import gzip
    password = s.get_secret("backup_password")
    if not password:
        raise RuntimeError("no backup password set (Settings → Portal appliance)")
    final = os.path.join(folder, portal_backup_name(s))  # u07: claugine-portal_<fqdn>_YYYYmmdd-HHMM
    if os.path.exists(final):
        final += timezone.localtime().strftime("%S")
    work = os.path.join(folder, "." + os.path.basename(final) + ".partial")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, mode=0o700)
    try:
        tmp = tempfile.mkdtemp(prefix="claugine-dump-")
        try:
            dump = _dump(tmp) or os.path.join(tmp, "database.sql")
            with open(dump, "rb") as src, gzip.open(os.path.join(work, "database.sql.gz"), "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        env_file = getattr(settings, "ENV_FILE", "") or os.environ.get("PORTAL_ENV_FILE", "/etc/claugine-portal/portal.env")
        if os.path.isfile(env_file):
            import pyzipper
            with pyzipper.AESZipFile(os.path.join(work, "portal.env.zip"), "w", compression=pyzipper.ZIP_DEFLATED,
                                     encryption=pyzipper.WZ_AES) as zf:
                zf.setpassword(password.encode())
                zf.setencryption(pyzipper.WZ_AES, nbits=256)
                zf.write(env_file, "portal.env")
        commit = installed_commit()
        with open(os.path.join(work, "COMMIT"), "w", encoding="utf-8") as fh:
            fh.write((commit or "unknown") + "\n")
        url = s.public_url or settings.SITE_URL
        with open(os.path.join(work, "README"), "w", encoding="utf-8") as fh:
            fh.write("Claugine portal backup: the database (database.sql.gz), portal.env in a zip protected by the archive\n"
                     "password, and the commit of the portal that made it (COMMIT). The portal itself is not in the\n"
                     "backup: the restore installs the release the licence is entitled to and loads this over it.\n\n"
                     "Restore on a freshly installed Ubuntu 26.04 VM: copy this folder there, download the installation\n"
                     "script (https://docs.claugine.com/operations/backup-restore/) and run\n"
                     f"    bash claugine-install.sh --restore --backup <this folder> --licence-key CLG-... --site-url {url}\n"
                     "(an Enterprise installation: add --second <user>@<the other VM>). It asks once for the archive\n"
                     "password and keeps this installation's identity: its installation id is in MANIFEST.\n")
        with open(os.path.join(work, "MANIFEST"), "w", encoding="utf-8") as fh:
            fh.write(f"claugine portal backup\ncreated {timezone.now().isoformat()}\nhost {socket.getfqdn()}\nurl {url}\n"
                     f"commit {commit or 'unknown'}\n")
            # The installation id, in clear: it is not a secret (the licence key is), and claugine-install.sh --restore
            # needs it BEFORE it can open portal.env.zip, to bind the licence to this installation and not a new one.
            if getattr(settings, "INSTALL_ID", ""):
                fh.write(f"install_id {settings.INSTALL_ID}\n")
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
    return c if re.fullmatch(r"[0-9a-f]{40}", c) else ""


def _size(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    return sum(os.path.getsize(os.path.join(r, f)) for r, _d, fs in os.walk(path) for f in fs)


def _ours(s):
    """The folder-name prefixes of *this* portal's own backups: claugine_<our fqdn>_… and the pre-rename forms."""
    fq = portal_fqdn(s)
    return tuple([f"{ARCHIVE_PREFIX}_{fq}_"]
                 + [f"{p}_{fq}_" for p in LEGACY_PREFIXES]   # claugine-portal_<fqdn>_…
                 + [f"{p}-" for p in LEGACY_PREFIXES])       # the oldest dated form, claugine-portal-YYYYmmdd-HHMM


def _is_archive(name, s=None):
    """A portal backup: a folder, or an archive file of the earlier formats (zip, tar.enc).

    With `s`, only **this** portal's own backups (owner 2026-11-08): one folder may now hold the backups of several
    portals and of the platforms, and `prune_local()` deletes whatever this matches — so pruning is scoped to
    `claugine_<our fqdn>_*` and never touches another portal's, nor the platforms' `platform_*`.

    Matched on the full prefix **with its separator**, never on a bare prefix: `claugine` alone would also catch
    `claugine-platform_…` and `claugine-platforms`, and `claugine-portal` alone would catch the pre-rename folders
    `claugine-portal/` and `claugine-platforms/` that sit beside a configured folder. Every real backup name has a
    separator after its prefix (`claugine_<fqdn>_…`, `claugine-portal_<fqdn>_…`, `claugine-portal-<date>`), so
    requiring one costs nothing and keeps a future unscoped `prune_local()` from deleting a folder rather than a
    backup (2026-11-09: nothing destructive takes this branch today, which is exactly why it is worth closing now).
    """
    if name.endswith(".partial"):
        return False
    if s is not None:
        return name.startswith(_ours(s))
    any_prefix = tuple([f"{ARCHIVE_PREFIX}_"]
                       + [f"{p}_" for p in LEGACY_PREFIXES]
                       + [f"{p}-" for p in LEGACY_PREFIXES])
    return name.startswith(any_prefix)


def prune_local(folder, days, s=None):
    cut = time.time() - days * 86400
    removed = 0
    for name in os.listdir(folder):
        p = os.path.join(folder, name)
        if _is_archive(name, s) and os.path.getmtime(p) < cut:
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
            removed += 1
    return removed


def backup(s=None):
    """Make one backup now (as root). u07: one run a day. The local folder (if its switch is on) holds the backup in a
    claugine-portal/ subfolder, and if S3 is on that same folder is copied to the bucket (one dir per backup, no root
    folder). If local is off, the backup is staged in a temp folder, uploaded to S3, then discarded. Returns the
    result line; kept in Settings."""
    from accounts.models import audit
    s = s or _settings()
    from accounts.edition import effective as _eff  # Standard: local folder only, fixed retention
    do_local = bool(_eff(s, "backup_local") and (_eff(s, "backup_path") or "").strip())
    do_s3 = bool(_eff(s, "backup_s3")) and _s3() is not None  # S3 is the Enterprise edition's (ee/s3.py)
    started = timezone.now()
    tmp = tempfile.mkdtemp(prefix="claugine-backup-")
    os.chmod(tmp, 0o700)
    try:
        if not do_local and not do_s3:
            raise RuntimeError("no destination: switch on a local folder and/or an S3 bucket in Settings → Backups")
        if do_local:  # the dated folder is created straight in the configured folder (owner 2026-11-08)
            base = local_backup_dir(s)
            made_base = not os.path.isdir(base)  # we only own what we create
            os.makedirs(base, mode=DIR_MODE, exist_ok=True)
            path = make_backup(s, base)
            # owner 2026-11-08: scoped to this portal's own backups — the folder also holds the platforms' and
            # may hold another portal's. The bucket keeps its own (its lifecycle rules).
            prune_local(base, _eff(s, "backup_keep_days"), s)
            # owner 2026-11-08: the folder is shared with the platforms' backups now, so set the folder itself and
            # only this run's own backup — never walk the whole folder and restamp someone else's files.
            _own = _owner(base, s)
            if made_base:
                _fix_one(base, _own)   # the configured folder is the operator's unless we had to create it
            _fix_modes(path, _own)     # every dir and file of this run's backup
        else:
            path = make_backup(s, tmp)  # no local copy this run: staged only to upload to S3
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
        from accounts.models import Kind, User
        from accounts.utils import notify
        admins = list(User.objects.filter(kind=Kind.STAFF, is_super_admin=True, is_active=True, login_enabled=True))
        if admins:
            notify(admins, "backup_failed", {"text": text, "subject": subject}, subject)
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
    r = subprocess.run(["certbot", "update_account", "--email", email, "--no-eff-email", "--non-interactive"],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        log(f"Let's Encrypt account: not updated: {(r.stdout + r.stderr).strip()[-200:]}")
        return
    os.makedirs(os.path.dirname(LE_STATE), exist_ok=True)
    with open(LE_STATE, "w", encoding="utf-8") as fh:
        fh.write(email + "\n")
    log("Let's Encrypt account: e-mail updated")


def tick(log=print, force=False, dry=False):
    """What the appliance timer does every 5 minutes (as root): keep the mail relay in line and back up when due."""
    s = _settings()
    if os.geteuid() == 0:
        try:
            if not dry and capture_mail(s, log):
                s = _settings()
            apply_mail(s, log, dry=dry)
        except Exception as exc:  # noqa: BLE001
            log(f"mail relay: {exc}")
        try:
            apply_le_email(log, dry=dry)
        except Exception as exc:  # noqa: BLE001 - the next round tries again
            log(f"Let's Encrypt account: {exc}")
    if dry:
        log(f"backup due: {force or due(s)}; mail relay {s.mail_host or '-'}:{s.mail_port}, account {'set' if s.mail_user else '-'}, "
            f"DKIM {'set' if s.has_secret('mail_dkim_key') else '-'}")
        return
    from accounts import ha
    if not ha.is_active():  # only the active portal node backs up
        if force:
            log(f"no backup: this portal node is {ha.role()}, not active")
        return
    if force or due(s):  # u07: one backup a day (local folder and/or S3, written by the same run)
        log(backup(s))
    if os.geteuid() == 0:  # u04: the platforms' front ends, daily or when asked (the active node only)
        from . import fe_backup
        try:
            fe_backup.tick(log)
        except Exception as exc:  # noqa: BLE001 - the next round tries again
            log(f"platform backups: {exc}")
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
    newest = next((n for n, _size, _at in last_local(s, 1) if not n.endswith((".zip", ".tar.enc"))), "")
    if not newest:  # the folder may not be readable by the web server: the last backup's result names it
        m = re.match(r"^ok: (\S+) \(", s.backup_last_result or "")
        newest = os.path.basename(m.group(1)) if m and not m.group(1).endswith((".zip", ".tar.enc")) else ""
    newest = newest or f"{ARCHIVE_PREFIX}_{portal_fqdn(s)}_YYYYmmdd-HHMM"
    url = s.public_url or settings.SITE_URL
    from accounts.edition import effective as _eff
    _path = (_eff(s, "backup_path") or "").strip()  # the folder actually in use (Standard pins it)
    if _path:  # u05: from the local folder if there is one
        first = (_("Copy the backup folder to a freshly installed Ubuntu 26.04 VM:"),
                 f"scp -r {local_backup_dir(s).rstrip('/')}/{newest} <user>@<new VM>:")
    else:  # u05: S3 only — from the bucket (any S3 client, e.g. the AWS CLI)
        pre = "/".join(x for x in ((s.backup_s3_prefix or "").strip("/"), newest) if x)
        first = (_("Copy the backup folder from the S3 bucket to a freshly installed Ubuntu 26.04 VM (any S3 client):"),
                 f"aws s3 cp --recursive s3://{s.backup_s3_bucket}/{pre}/ ~/{newest}/ --endpoint-url {s.backup_s3_endpoint}")
    return [first,
            (_("On the new VM, as the user you copied it to: download the installer"),
             "mkdir -p ~/claugine && cd ~/claugine && curl -fsSLO "
             "https://raw.githubusercontent.com/ivladek/claugine/main/deploy/claugine-install.sh"),
            (_("Restore: installs the current release the licence is entitled to, asks for the archive password once, "
               "loads the backup and migrates it forward:"),
             f"bash claugine-install.sh --restore --licence-key <licence key> --backup ~/{newest} --site-url {url}")]


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
    for name in sorted(names, reverse=True)[:n]:
        p = os.path.join(base, name)
        try:
            out.append((name, _size(p), datetime.fromtimestamp(os.path.getmtime(p), tz=timezone.get_current_timezone())))
        except OSError:
            pass
    return out


def s3_backups(s, n=10):
    """Best-effort: the backup folders in the S3 bucket (newest first), or [] when S3 is off or the bucket can't be
    listed. A listing problem must never blank the Settings page."""
    from accounts.edition import effective as _eff   # Standard pins S3 off: never list a bucket it may not use
    s3 = _s3()
    if s3 is None or not _eff(s, "backup_s3") or not (getattr(s, "backup_s3_bucket", "") or "").strip():
        return []
    try:
        # only the portal's own backup folders (claugine-portal[_-]…) — not the platforms' "platforms/" prefix
        return [x for x in s3.list_objects(s3.conf_of(s)) if _is_archive(x, s)][:n]
    except Exception:  # noqa: BLE001 - best-effort display only
        return []


# --- the Settings form --------------------------------------------------------------------------------------------

def settings_form(*args, **kwargs):
    from django import forms
    from django.utils.translation import gettext_lazy as _l

    class ApplianceForm(forms.Form):
        mail_mode = forms.ChoiceField(label=_l("Mail relay"), choices=[("internal", _l("internal: this VM's relay (postfix, loopback only)")),
                                                                       ("direct", _l("direct: this VM delivers to the recipients' mail servers")),
                                                                       ("external", _l("external SMTP server"))],
                                      help_text=_l("direct: each portal node sends from its own public address under its mail name "
                                                   "(MAIL_NAME in node.env, the name of its PTR record); the sender domain needs SPF "
                                                   "for these addresses and the DKIM key below."))
        mail_from = forms.CharField(label=_l("Sender address"), required=False, max_length=200,
                                    help_text=_l("portal@example.kz or Example Cloud <portal@example.kz>"))
        mail_host = forms.CharField(label=_l("SMTP server"), required=False, max_length=200,
                                    help_text=_l("internal: where this VM's relay sends (e.g. smtp-relay.brevo.com); external: where the portal sends."))
        mail_port = forms.IntegerField(label=_l("SMTP port"), min_value=1, max_value=65535, initial=587)
        mail_tls = forms.BooleanField(label=_l("STARTTLS"), required=False)
        mail_user = forms.CharField(label=_l("SMTP user"), required=False, max_length=200)
        mail_password = forms.CharField(label=_l("SMTP password"), required=False, strip=False,
                                        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
                                        )
        mail_dkim_domain = forms.CharField(label=_l("DKIM domain"), required=False, max_length=200)
        mail_dkim_selector = forms.RegexField(label=_l("DKIM selector"), required=False, max_length=63, regex=r"^[A-Za-z0-9-]+$")
        mail_dkim_key = forms.CharField(label=_l("DKIM private key"), required=False, strip=False,
                                        widget=forms.Textarea(attrs={"rows": 3, "class": "mono", "autocomplete": "off", "spellcheck": "false"}),
                                        help_text=_l("PEM. Internal relay or direct delivery."))
        backup_local = forms.BooleanField(label=_l("to a local folder"), required=False)  # u08: the local destination's switch
        backup_path = forms.CharField(label=_l("Local folder"), required=False, max_length=300,
                                      help_text=_l("Each backup is a folder claugine_<name>_<date> created here."))
        backup_owner = forms.CharField(label=_l("Owner (user:group)"), required=False, max_length=64,
                                       help_text=_l("The local backups (portal and platforms) are set to this owner, e.g. portal:portal. Empty: the local folder's own owner."))
        backup_start = forms.TimeField(label=_l("Backup at"), help_text=_l("Once a day, e.g. 00:45."),  # u07: one backup a day
                                       widget=forms.TimeInput(format="%H:%M"))
        backup_keep_days = forms.IntegerField(label=_l("Keep, days"), min_value=1, max_value=3650)
        backup_password = forms.CharField(label=_l("Archive password"), required=False, strip=False,
                                          widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
                                          help_text=_l("At least 12 characters. Keep it apart: it is needed to restore."))

        def __init__(self, *a, has_backup_password=False, settings_obj=None, **kw):
            super().__init__(*a, **kw)
            s3 = _s3()
            if s3 is not None:  # the bucket's fields (Enterprise, ee/s3.py)
                self.fields.update(s3.form_fields())
            self.has_backup_password = has_backup_password
            self.settings_obj = settings_obj
            from django.utils.translation import gettext as _g
            for f in [x for x in ("mail_password", "backup_password", "mail_dkim_key", "backup_s3_secret") if x in self.fields]:  # a kept value shows as set
                set_ = settings_obj is not None and settings_obj.has_secret(f)
                self.fields[f].widget.attrs.update({"placeholder": _g("●●●●●●●● set — empty keeps it") if set_ else _g("not set"),
                                                    "data-secret": "set" if set_ else "empty"})
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
            if d.get("backup_local") and not d.get("backup_path") and "backup_path" not in self.errors:  # u08: local on needs a folder
                self.add_error("backup_path", _l("Required for a local copy."))
            if not (d.get("backup_local") or d.get("backup_s3")):  # u08: at least one destination switched on
                self.add_error("backup_local", _l("Switch on a local folder and/or an S3 bucket.") if _s3() is not None
                               else _l("Switch on the local folder."))
            pw = d.get("backup_password") or ""
            if pw and len(pw) < 12:
                self.add_error("backup_password", _l("At least 12 characters."))
            if (d.get("backup_local") or d.get("backup_s3")) and not pw and not self.has_backup_password:  # u08: only when a destination is on
                self.add_error("backup_password", _l("Required: backups are encrypted."))
            return d

    return ApplianceForm(*args, **kwargs)


FORM_FIELDS = ("mail_mode", "mail_from", "mail_host", "mail_port", "mail_tls", "mail_user", "mail_dkim_domain",
               "mail_dkim_selector",
               "backup_local", "backup_path", "backup_owner", "backup_start", "backup_keep_days",
               "backup_s3", "backup_s3_endpoint", "backup_s3_region",
               "backup_s3_bucket", "backup_s3_access_key", "backup_s3_style")


def form_initial(s):
    return {f: getattr(s, f) for f in FORM_FIELDS}


def _s3():
    """ee/s3.py — backups to an S3 bucket — or None in a build without ee/ (Standard: a local folder only)."""
    from accounts import edition
    return edition.ee("s3")
