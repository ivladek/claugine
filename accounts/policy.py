"""Password policy (Administration → Settings): one set of rules for every password the portal takes.

Portal users' passwords, the passwords of tenants' OpenNebula admins and of their routers, and the secrets typed
into Secrets follow it. Defaults:

    length            10 (any number 10-50)
    small letters     at least 2
    capital letters   at least 2
    special symbols   at least 1, only from the allowed list (default - _ . + = ^ ~)
    figures           at least 1
    last passwords    5 may not be used again (3-1000)
    personal data     no part (4 characters or more; 6 for figures) of the person's name, email, phone or IIN, the
                      tenant's name, the organization's names or BIN (on/off)

Any special symbol can be allowed except these (UNSAFE, never taken): the space, quotes (' " `), ? / \\ and |.

Under every new-password field the rules are shown and checked while typing (static/olist.js).
"""
import json
import re
import secrets
import string

from django import forms
from django.core.exceptions import ValidationError
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext as _

DEFAULTS = {"length": 10, "lower": 2, "upper": 2, "special": 1, "digits": 1,
            "allowed": "-_.+=^~", "history": 5, "names": True}
UNSAFE = " '\"`?/\\|"
PART = 4          # personal data: parts of this length (letters) ...
PART_DIGITS = 6   # ... or of figures only (phone, IIN/BIN) may not appear
LIMITS = {"length": (10, 50), "history": (3, 1000), "lower": (0, 20), "upper": (0, 20), "special": (0, 20), "digits": (0, 20)}


def policy():
    try:
        from tenants.models import PortalSettings
        saved = PortalSettings.get().password_policy or {}
    except Exception:  # noqa: BLE001 - before the table exists (migrations)
        saved = {}
    p = {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}
    for k, (lo, hi) in LIMITS.items():
        try:
            p[k] = min(max(int(p[k]), lo), hi)
        except (TypeError, ValueError):
            p[k] = DEFAULTS[k]
    p["names"] = bool(p["names"])
    p["allowed"] = clean_allowed(p["allowed"])[0]
    return p


def clean_allowed(text):
    """(allowed special symbols, refused ones) of what was typed: punctuation only, never the UNSAFE ones."""
    text = str(text or "")
    ok = "".join(dict.fromkeys(c for c in text if c in string.punctuation and c not in UNSAFE))
    refused = "".join(dict.fromkeys(c for c in text if c not in ok))
    return ok, refused


def specials(p=None):
    p = p or policy()
    return p["allowed"]


def _shown(c):
    return _("space") if c == " " else c


def rules(p=None):
    """[(key, text)] of the rules, as shown under a password field."""
    p = p or policy()
    out = [("length", _("at least %(n)s characters") % {"n": p["length"]})]
    for key, label in (("lower", _("small letters")), ("upper", _("capital letters")), ("digits", _("figures")),
                       ("special", _("special symbols"))):
        if p[key]:
            out.append((key, _("%(label)s: at least %(n)s") % {"label": label, "n": p[key]}
                        + (f" ({' '.join(specials(p))})" if key == "special" else "")))
    out.append(("forbidden", _("only letters, figures and these symbols: %(c)s") % {"c": " ".join(specials(p)) or "—"}))
    if p["names"]:
        out.append(("names", _("no part of the personal data of the user, the project or the organization")))
    if p["history"]:
        out.append(("history", _("none of the last %(n)s passwords") % {"n": p["history"]}))
    return out


def problems(password, words=(), history_hashes=(), p=None):
    """What is wrong with a password (empty: it is fine)."""
    from django.contrib.auth.hashers import check_password
    p = p or policy()
    s = password or ""
    out = []
    if len(s) < p["length"]:
        out.append(_("at least %(n)s characters") % {"n": p["length"]})
    if len(s) > 128:
        out.append(_("at most 128 characters"))
    counts = {"lower": len(re.findall(r"[a-z]", s)), "upper": len(re.findall(r"[A-Z]", s)),
              "digits": len(re.findall(r"[0-9]", s)), "special": sum(1 for c in s if c in specials(p))}
    names = {"lower": _("small letters"), "upper": _("capital letters"), "digits": _("figures"),
             "special": _("special symbols")}
    for key in ("lower", "upper", "digits", "special"):
        if counts[key] < p[key]:
            out.append(_("%(label)s: at least %(n)s") % {"label": names[key], "n": p[key]})
    bad = sorted({c for c in s if not (c.isascii() and c.isalnum()) and c not in specials(p)})
    if bad:
        out.append(_("not allowed: %(c)s") % {"c": " ".join(_shown(c) for c in bad)})
    if p["names"]:
        hits = personal_hits(s, words)
        if hits:
            out.append(_("no part of the personal data of the user, the project or the organization (%(w)s)")
                       % {"w": ", ".join(hits)})
    if p["history"] and any(h and check_password(s, h) for h in list(history_hashes)[:p["history"]]):
        out.append(_("none of the last %(n)s passwords") % {"n": p["history"]})
    return out


def personal_hits(password, words):
    """The personal data a password contains a part of: a run of PART characters (PART_DIGITS for figures only) of
    any word, case-insensitive."""
    low = (password or "").lower()
    hits = set()
    for w in words:
        w = (w or "").lower()
        n = PART_DIGITS if w.isdigit() else PART
        if len(w) >= n and any(w[i:i + n] in low for i in range(len(w) - n + 1)):
            hits.add(w)
    return sorted(hits)


def personal_words(*texts):
    """Words of personal data, lower case: runs of letters and figures (4 or more), of letters only (4 or more) and of
    figures only (6 or more). An email counts by its part before @."""
    out = set()
    for t in texts:
        t = str(t or "")
        if "@" in t:
            t = t.split("@")[0]
        for x in re.findall(r"[^\W_]{4,}", t) + re.findall(r"[^\W\d_]{4,}", t) + re.findall(r"\d{6,}", t):
            if not x.isdigit() or len(x) >= PART_DIGITS:
                out.add(x.lower())
    return out


def words_for_user(user):
    if user is None:
        return set()
    org = getattr(user, "organization", None)
    return personal_words(getattr(user, "email", ""), getattr(user, "full_name", ""), getattr(user, "phone", ""),
                          getattr(user, "iin", ""), getattr(org, "short_name", "") if org else "",
                          getattr(org, "name", "") if org else "", getattr(org, "tax_id", "") if org else "")


def generate(length=None):
    """A random password that follows the policy."""
    p = policy()
    n = max(length or 16, p["length"])
    alphabet = "".join(c for c in string.ascii_letters + string.digits + specials(p))
    while True:
        s = "".join(secrets.choice(alphabet) for _ in range(n))
        if not problems(s, p=p):
            return s


class PolicyValidator:
    """Django password validator: the policy, the user's names and the user's last passwords."""

    def validate(self, password, user=None):
        bad = problems(password, words_for_user(user), getattr(user, "password_history", None) or [])
        if bad:
            raise ValidationError(_("The password needs: %(what)s.") % {"what": "; ".join(bad)}, code="password_policy")

    def get_help_text(self):
        return "; ".join(t for _k, t in rules())


def policy_list(scope=""):
    """The rules as a list checked while typing: under one field, or (scope="form") for every password of the form."""
    p = policy()
    data = json.dumps({**p, "specials": specials(p)})
    items = format_html_join("", '<li data-rule="{}">{}</li>', rules(p))
    return format_html('<ul class="pw-policy" data-policy="{}"{}>{}</ul>', data,
                       format_html(' data-scope="{}"', scope) if scope else "", items)


class PolicyPasswordInput(forms.PasswordInput):
    """A new-password field with the rules of the policy under it, checked while typing."""

    def __init__(self, attrs=None, render_value=False):
        super().__init__({"autocomplete": "new-password", **(attrs or {})}, render_value=render_value)

    def render(self, name, value, attrs=None, renderer=None):
        return format_html("{}{}", super().render(name, value, attrs, renderer), policy_list())
