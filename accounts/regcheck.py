"""Owner 2026-10-29 20:30: registration requests — no collection of attackers' requests, a check of what they enter and
a daily report to the super admins.

    * A registration that is not confirmed in time (the e-mail link, REGISTRATION_TTL_HOURS) and a join request that
      was declined are deleted for good by the daily run; a refused organization registration is deleted at once
      (tenants.orgreg.reject). Only the log keeps them: e-mail, organization, IIN/BIN, address, the checks' result.
    * Every registration is checked (analyze): a filled hidden field or a form sent within seconds of opening it is a
      robot — nothing is made, the page answers as usual and the log keeps the attempt; markup or code in the fields,
      a throwaway mail service, a name that is no name, a phone of repeated digits, several attempts from one address,
      one IIN/BIN or one phone in a day add to its score. A score of 3 and more marks it suspicious: the staff see the
      reasons on the organization waiting for approval, and the report lists it.
    * The report (timer "registrations", daily, not before 09:00): the last 24 hours and 30 days — started, confirmed,
      approved, refused, not confirmed and deleted, join requests, robots stopped, suspicious ones with their reasons,
      the addresses seen most, the organizations still waiting.
"""
import re
from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _l

SUSPICIOUS = 3  # a score from here up marks a registration suspicious
MIN_SECONDS = 4  # a person does not fill the registration form faster than this
HONEYPOT = "website"  # a field people do not see; robots fill it
TS_FIELD = "form_ts"  # when the form was shown (signed)
TS_SALT = "claugine.register.ts"

FLAGS = {  # key: (weight, what it means)
    "honeypot": (10, _l("a hidden field was filled (a robot)")),
    "too_fast": (10, _l("the form was sent within seconds of opening it (a robot)")),
    "no_form_time": (2, _l("sent without the form being opened")),
    "markup": (5, _l("markup or code in the fields")),
    "disposable_email": (3, _l("a throwaway mail service")),
    "odd_email": (1, _l("an e-mail address of random characters")),
    "odd_name": (2, _l("the full name is not a name")),
    "odd_org_name": (2, _l("the organization's name is not a name")),
    "odd_phone": (2, _l("a phone of repeated or sequential digits")),
    "repeat_ip": (2, _l("several registrations from one address in a day")),
    "repeat_tax_id": (2, _l("the same IIN/BIN tried several times in a day")),
    "repeat_phone": (2, _l("the same phone in several registrations in a day")),
    "known_tax_id": (0, _l("the IIN/BIN belongs to an organization: a join request")),
}

DISPOSABLE = {
    "mailinator.com", "guerrillamail.com", "guerrillamail.net", "guerrillamail.org", "sharklasers.com", "grr.la",
    "10minutemail.com", "10minutemail.net", "temp-mail.org", "tempmail.com", "tempmail.net", "tempmailo.com",
    "temp-mail.io", "tempail.com", "yopmail.com", "yopmail.net", "trashmail.com", "trashmail.de", "getnada.com",
    "nada.email", "dispostable.com", "maildrop.cc", "mohmal.com", "emailondeck.com", "throwawaymail.com",
    "fakeinbox.com", "mailnesia.com", "mintemail.com", "spamgourmet.com", "mytemp.email", "tmpmail.org",
    "tmail.ws", "burnermail.io", "33mail.com", "mail.tm", "mailcatch.com", "inboxkitten.com", "1secmail.com",
    "1secmail.net", "1secmail.org", "emailfake.com", "moakt.com", "dropmail.me", "spambox.us", "mvrht.net",
    "minuteinbox.com", "discard.email", "anonaddy.me", "cock.li", "airmail.cc", "byom.de", "harakirimail.com",
}

MARKUP = re.compile(
    r"<\s*/?\s*[a-z!]|javascript:|on\w+\s*=|\{\{|\{%|\$\{|<\?|https?://|www\.|\.\./|\\x[0-9a-f]{2}|%3c|%27|%22|"
    r"\bunion\s+(all\s+)?select\b|\bselect\b.+\bfrom\b|\b(drop|truncate|alter)\s+table\b|\binsert\s+into\b|"
    r"\bor\s+['\"]?\d+['\"]?\s*=\s*['\"]?\d+|'\s*or\s*'|--\s*$|;\s*--|/\*|\bsleep\s*\(|\bbenchmark\s*\(|"
    r"\bexec(ute)?\s*\(|\beval\s*\(|\$\(|`|\|\s*\w+|&&",
    re.I)
TEXT_FIELDS = ("email", "full_name", "organization_name", "tax_id", "phone", "city")


def client_ip(request):
    """The client's address: HAProxy adds it last to X-Forwarded-For; without it, the connection's."""
    fwd = [p.strip() for p in (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",") if p.strip()]
    return (fwd[-1] if fwd else request.META.get("REMOTE_ADDR")) or ""


def form_stamp():
    from django.core import signing
    return signing.dumps(int(timezone.now().timestamp()), salt=TS_SALT)


def seconds_on_form(post):
    from django.core import signing
    try:
        shown = signing.loads(post.get(TS_FIELD) or "", salt=TS_SALT, max_age=24 * 3600)
    except signing.BadSignature:
        return None
    return timezone.now().timestamp() - shown


def _vowels(word):
    return sum(1 for ch in word.lower() if ch in "aeiouyаеёиоуыэюяіөүұә")


def odd_name(value, min_words=1):
    """Not a name: digits or symbols, no vowels, long consonant runs, one letter repeated, too few words."""
    v = " ".join((value or "").split())
    if not v:
        return False
    if re.search(r"[0-9@#$%^*=_+|~<>{}\[\]\\]", v):
        return True
    words = [w for w in re.split(r"[\s\-'.,\"«»()&]+", v) if w]
    if len(words) < min_words:
        return True
    for w in words:
        letters = re.sub(r"[^A-Za-zА-Яа-яЁёІіӨөҮүҰұӘәҒғҚқҢңҺһ]", "", w)
        if len(letters) >= 4 and _vowels(letters) == 0:
            return True
        if re.search(r"[bcdfghjklmnpqrstvwxz]{5,}", letters.lower()):
            return True
        if len(letters) >= 3 and len(set(letters.lower())) == 1:
            return True
    return False


def odd_email(email):
    local = (email or "").split("@")[0]
    return len(re.findall(r"\d", local)) >= 6 or bool(re.search(r"[bcdfghjklmnpqrstvwxz]{6,}", local.lower()))


def odd_phone(phone):
    digits = re.sub(r"\D", "", phone or "")[-10:]
    if len(digits) < 10:
        return False
    tail = digits[3:]
    return len(set(tail)) <= 2 or tail in "0123456789" or tail in "9876543210"


def _recent(action, hours=24):
    from .models import AuditLog
    return AuditLog.objects.filter(action=action, created_at__gte=timezone.now() - timedelta(hours=hours))


def analyze(post, ip="", seconds=None):
    """What the entered data tells. Returns {"score", "flags": [keys], "ip", "seconds"}."""
    flags = []
    if (post.get(HONEYPOT) or "").strip():
        flags.append("honeypot")
    if seconds is None:
        flags.append("no_form_time")
    elif seconds < MIN_SECONDS:
        flags.append("too_fast")
    if any(MARKUP.search(str(post.get(f) or "")) for f in TEXT_FIELDS):
        flags.append("markup")
    email = (post.get("email") or "").strip().lower()
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    if domain and (domain in DISPOSABLE or any(domain.endswith("." + d) for d in DISPOSABLE)):
        flags.append("disposable_email")
    if email and odd_email(email):
        flags.append("odd_email")
    if odd_name(post.get("full_name"), min_words=2):
        flags.append("odd_name")
    if odd_name(post.get("organization_name")):
        flags.append("odd_org_name")
    if odd_phone(post.get("phone")):
        flags.append("odd_phone")
    started = list(_recent("registration_started").values_list("details", flat=True))
    started += list(_recent("registration_blocked").values_list("details", flat=True))
    if ip and sum(1 for d in started if d.get("ip") == ip) >= 2:
        flags.append("repeat_ip")
    tax_id = (post.get("tax_id") or "").strip()
    if tax_id and sum(1 for d in started if d.get("tax_id") == tax_id) >= 1:
        flags.append("repeat_tax_id")
    phone = re.sub(r"\D", "", post.get("phone") or "")
    if phone and sum(1 for d in started if d.get("phone_hash") == _hash(phone)) >= 1:
        flags.append("repeat_phone")
    if tax_id:
        from tenants.models import Organization
        if Organization.objects.filter(tax_id=tax_id).exists():
            flags.append("known_tax_id")
    score = sum(FLAGS[f][0] for f in flags)
    return {"score": score, "flags": flags, "ip": ip, "seconds": round(seconds, 1) if seconds is not None else None}


def _hash(value):
    import hashlib
    return hashlib.sha256(f"{settings.SECRET_KEY}:{value}".encode()).hexdigest()[:16]


def is_robot(check):
    return any(f in check.get("flags", ()) for f in ("honeypot", "too_fast"))


def log_fields(post, check):
    """What the log keeps of a registration: no password, the phone as its last digits and a hash."""
    phone = re.sub(r"\D", "", post.get("phone") or "")
    return {"email": (post.get("email") or "")[:120], "org": (post.get("organization_name") or "")[:120],
            "tax_id": (post.get("tax_id") or "")[:20], "name": (post.get("full_name") or "")[:120],
            "city": (post.get("city") or "")[:32], "phone_tail": phone[-4:], "phone_hash": _hash(phone) if phone else "",
            "ip": check.get("ip", ""), "score": check.get("score", 0), "flags": ",".join(check.get("flags", [])),
            "seconds": check.get("seconds")}


def labels(flags):
    if isinstance(flags, str):
        flags = [f for f in flags.split(",") if f]
    return [str(FLAGS[f][1]) for f in flags if f in FLAGS]


# --- deleting what was never approved -----------------------------------------------------------------------------

def purge(now=None):
    """Delete the registrations not confirmed in time and the declined join requests (their people too). The log keeps
    each one. Returns {"unconfirmed": n, "join_declined": n}."""
    from django.db import transaction
    from tenants.models import JoinRequest, Registration
    from .models import Kind, audit
    now = now or timezone.now()
    out = {"unconfirmed": 0, "join_declined": 0}
    limit = now - timedelta(hours=settings.REGISTRATION_TTL_HOURS)
    for reg in Registration.objects.filter(created_at__lt=limit, user__email_verified=False,
                                           user__kind=Kind.CUSTOMER).select_related("user"):
        u = reg.user
        chk = reg.reg_check or {}
        with transaction.atomic():
            audit(None, "registration_purged", None, why="not confirmed", email=u.email, org=reg.organization_name,
                  tax_id=reg.tax_id, ip=chk.get("ip", ""), score=chk.get("score", 0), flags=",".join(chk.get("flags", [])),
                  started=reg.created_at.isoformat())
            u.delete()
        out["unconfirmed"] += 1
    for jr in JoinRequest.objects.filter(state=JoinRequest.State.REJECTED, user__kind=Kind.CUSTOMER,
                                         user__organization__isnull=True, user__is_active=False).select_related("user", "organization"):
        u = jr.user
        if u.memberships.exists() or u.is_staff_member:
            continue
        with transaction.atomic():
            audit(None, "registration_purged", None, why="join declined", email=u.email, org=jr.organization.name,
                  tax_id=jr.organization.tax_id or "", decided=jr.decided_at.isoformat() if jr.decided_at else "")
            u.delete()
        out["join_declined"] += 1
    return out


# --- the daily report -----------------------------------------------------------------------------------------------

ACTIONS = {
    "started": "registration_started", "blocked": "registration_blocked", "probed": "registration_suspicious",
    "confirmed": "organization_registration_requested", "approved": "organization_registration_done",
    "refused": "organization_registration_rejected", "join": "join_requested", "join_declined": "join_rejected",
    "purged": "registration_purged",
}


def stats(hours):
    from .models import AuditLog
    since = timezone.now() - timedelta(hours=hours)
    rows = list(AuditLog.objects.filter(action__in=ACTIONS.values(), created_at__gte=since)
                .values("action", "details", "created_at").order_by("created_at"))
    by = {k: [r for r in rows if r["action"] == a] for k, a in ACTIONS.items()}
    out = {k: len(v) for k, v in by.items()}
    out["purged_unconfirmed"] = sum(1 for r in by["purged"] if r["details"].get("why") == "not confirmed")
    out["purged_join"] = out["purged"] - out["purged_unconfirmed"]
    flagged = [r for r in by["started"] + by["blocked"] + by["probed"] if int(r["details"].get("score") or 0) >= SUSPICIOUS]
    out["suspicious"] = len(flagged)
    reasons = Counter(f for r in flagged for f in (r["details"].get("flags") or "").split(",") if f)
    out["reasons"] = [(str(FLAGS[f][1]), n) for f, n in reasons.most_common() if f in FLAGS]
    ips = Counter(r["details"].get("ip") for r in by["started"] + by["blocked"] + by["probed"] if r["details"].get("ip"))
    out["ips"] = [(ip, n) for ip, n in ips.most_common(10) if n >= 2]
    out["unique_ips"] = len(ips)
    out["flagged"] = [{"at": timezone.localtime(r["created_at"]).strftime("%d.%m %H:%M"),
                       "kind": {"registration_blocked": "robot", "registration_suspicious": "invalid"}.get(r["action"], "request"),
                       "email": r["details"].get("email", ""), "org": r["details"].get("org", ""),
                       "tax_id": r["details"].get("tax_id", ""), "ip": r["details"].get("ip", ""),
                       "score": r["details"].get("score", 0), "reasons": "; ".join(labels(r["details"].get("flags") or ""))}
                      for r in flagged][-30:]
    return out


def waiting():
    from tenants.models import Organization
    now = timezone.now()
    return [{"name": o.name, "tax_id": o.tax_id or "", "days": (now - o.created_at).days,
             "score": (o.reg_check or {}).get("score", 0), "reasons": "; ".join(labels((o.reg_check or {}).get("flags", [])))}
            for o in Organization.objects.filter(registration="pending", archived_at__isnull=True).order_by("created_at")]


ROWS = [("started", _l("Registrations started")), ("confirmed", _l("E-mail confirmed (sent for approval)")), ("approved", _l("Approved")),
        ("refused", _l("Refused (deleted)")), ("purged_unconfirmed", _l("Not confirmed in time (deleted)")),
        ("join", _l("Join requests (a known IIN/BIN)")), ("purged_join", _l("Join requests declined (deleted)")),
        ("blocked", _l("Robots stopped")), ("probed", _l("Invalid forms with code in the fields")), ("suspicious", _l("Suspicious")),
        ("unique_ips", _l("Addresses"))]


def send_report(purged=None, to=None):
    """The report to every super admin who signs in, or only to `to` (Settings → Reports: redirected). Returns
    (sent, detail)."""
    from django.urls import reverse
    from .models import User
    from .utils import notify, site_link
    day, month = stats(24), stats(24 * 30)
    supers = list(User.objects.filter(kind="staff", is_super_admin=True, is_active=True, login_enabled=True))
    today = timezone.localdate()
    rows = [(label, day[k], month[k]) for k, label in ROWS]
    n = notify([to] if to else supers, "registration_report", {"day": day, "month": month, "rows": rows, "waiting": waiting(),
                                               "purged": purged or {}, "date": today, "link": site_link(reverse("org_list"))},
               _l("Registrations on %(d)s: %(n)s new, %(s)s suspicious") % {"d": today.strftime("%d.%m.%Y"),
                                                                            "n": day["started"], "s": day["suspicious"]})
    return n, (f"{day['started']} started, {day['blocked']} robots, {day['suspicious']} suspicious; "
               f"sent to {to + ' (redirected)' if to else len(supers)}")


def daily():
    """The timer: delete what was never approved (the report goes with Settings → Reports, u01)."""
    purged = purge()
    return True, f"deleted {purged['unconfirmed']} not confirmed, {purged['join_declined']} declined joins"
