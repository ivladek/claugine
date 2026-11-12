"""Tax IDs by country (owner 2026-11-12: customers from other countries; "tax id required in any case").

An organization has a country (ISO 3166-1 alpha-2) and a tax ID of that country:

- Kazakhstan: the IIN (an individual or individual entrepreneur) or the BIN (a legal entity), 12 digits with a check
  digit (accounts/kz.py);
- the United Arab Emirates: the TRN, 15 digits;
- any other country: its tax or registration number as written, 3-32 letters and digits (spaces, dots, slashes and
  dashes are dropped).

The countries a portal takes are set in Settings → Provider (home country and allowed countries).
"""

import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from . import kz

# country -> the tax ID kinds it has, as (value, label)
KINDS = {
    "KZ": [("bin", _("BIN (legal entity)")), ("iin", _("IIN (individual / individual entrepreneur)"))],
    "AE": [("trn", _("TRN (tax registration number)"))],
}
OTHER = [("tin", _("Tax or registration number"))]


def kinds(country):
    return KINDS.get((country or "").upper(), OTHER)


def label(country):
    """The name of the tax ID field for a country."""
    return {"KZ": _("IIN/BIN"), "AE": _("TRN")}.get((country or "").upper(), _("Tax or registration number"))


def normalize(country, value, kind=None):
    """The tax ID kept for (country, kind), or ValidationError. Returns (value, kind)."""
    country = (country or "").upper()
    raw = (value or "").strip()
    if not raw:
        raise ValidationError(_("The tax ID is required."))
    if country == "KZ":
        v = re.sub(r"\s", "", raw)
        kz.validate_tax_id(v, kind if kind in ("iin", "bin") else None)
        return v, kind if kind in ("iin", "bin") else kz.guess_tax_id_type(v)
    if country == "AE":
        v = re.sub(r"[\s-]", "", raw)
        if not re.fullmatch(r"\d{15}", v):
            raise ValidationError(_("The TRN has 15 digits."))
        return v, "trn"
    v = re.sub(r"[\s./-]", "", raw).upper()
    if not re.fullmatch(r"[A-Z0-9]{3,32}", v):
        raise ValidationError(_("3-32 letters and digits."))
    return v, "tin"


def countries_choices(allowed=None):
    """[(code, name)] in the current language, the allowed ones only when given."""
    from django_countries import countries

    allowed = {c.upper() for c in allowed} if allowed else None
    return [(code, str(name)) for code, name in countries if allowed is None or code in allowed]


def country_name(code):
    from django_countries import countries

    return str(countries.name(code)) if code else ""
