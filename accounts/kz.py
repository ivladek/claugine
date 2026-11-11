"""Kazakhstan-specific reference data and validators."""
import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

# Cities of republican significance and regional centres, plus large towns.
CITIES = [
    ("astana", "Astana"),
    ("almaty", "Almaty"),
    ("shymkent", "Shymkent"),
    ("aktau", "Aktau"),
    ("aktobe", "Aktobe"),
    ("atyrau", "Atyrau"),
    ("balkhash", "Balkhash"),
    ("ekibastuz", "Ekibastuz"),
    ("karaganda", "Karaganda"),
    ("kokshetau", "Kokshetau"),
    ("konaev", "Konaev"),
    ("kostanay", "Kostanay"),
    ("kyzylorda", "Kyzylorda"),
    ("oral", "Oral (Uralsk)"),
    ("oskemen", "Oskemen (Ust-Kamenogorsk)"),
    ("pavlodar", "Pavlodar"),
    ("petropavl", "Petropavl"),
    ("rudny", "Rudny"),
    ("semey", "Semey"),
    ("taldykorgan", "Taldykorgan"),
    ("taraz", "Taraz"),
    ("temirtau", "Temirtau"),
    ("turkistan", "Turkistan"),
    ("zhanaozen", "Zhanaozen"),
    ("zhezkazgan", "Zhezkazgan"),
    ("other", _("Other locality")),
]

_W1 = list(range(1, 12))
_W2 = [3, 4, 5, 6, 7, 8, 9, 10, 11, 1, 2]


def tax_id_checksum_ok(value: str) -> bool:
    """Check digit of a 12-digit IIN or BIN (same algorithm for both)."""
    if not re.fullmatch(r"\d{12}", value or ""):
        return False
    digits = [int(c) for c in value]
    check = sum(d * w for d, w in zip(digits, _W1)) % 11
    if check == 10:
        check = sum(d * w for d, w in zip(digits, _W2)) % 11
        if check == 10:
            return False
    return check == digits[11]


def guess_tax_id_type(value: str) -> str:
    """IIN: 5th digit is part of the birth date (month 0/1); BIN: 5th digit is 4-6."""
    return "bin" if value[4] in "456" else "iin"


def validate_tax_id(value, kind=None):
    if not re.fullmatch(r"\d{12}", value or ""):
        raise ValidationError(_("IIN/BIN must be exactly 12 digits."))
    if not tax_id_checksum_ok(value):
        raise ValidationError(_("IIN/BIN check digit is wrong. Please check the number."))
    detected = guess_tax_id_type(value)
    if kind == "iin":
        month = int(value[2:4])
        if detected != "iin" or not 1 <= month <= 12:
            raise ValidationError(_("This does not look like an IIN (individual identification number)."))
    if kind == "bin" and detected != "bin":
        raise ValidationError(_("This does not look like a BIN (business identification number)."))


def normalize_phone(value: str) -> str:
    """A mobile number kept as +<digits> (owner 2026-10-29: one standard for every phone, see normalize_intl_phone)."""
    return normalize_intl_phone(value)


def to_international(value: str) -> str:
    """Any written phone number -> +<country><number>, or "" if it is not one.

    8XXXXXXXXXX and 7XXXXXXXXXX (11 digits) and 7XXXXXXXXX (10 digits, a Kazakhstan mobile without the
    country code) become +7...; numbers written with a leading + keep their country code.
    """
    raw = (value or "").strip()
    if not raw or raw.startswith("@") or not re.search(r"\d", raw):
        return ""
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+"):
        out = digits
    elif len(digits) == 11 and digits[0] in "78":
        out = "7" + digits[1:]
    elif len(digits) == 10 and digits[0] == "7":
        out = "7" + digits
    else:
        return ""
    return "+" + out if 10 <= len(out) <= 15 else ""


def normalize_intl_phone(value: str) -> str:
    """A phone number in international format, written as +7 701 2345678 (owner 2026-10-29: the standard for adding
    and editing any phone): spaces, dashes and brackets allowed; a Kazakhstan number may also start with 8. Returns
    the number kept: +<8-15 digits>, or raises ValidationError."""
    v = re.sub(r"[\s\-().]", "", (value or "").strip())
    if re.fullmatch(r"8\d{10}", v):
        v = "+7" + v[1:]
    if not re.fullmatch(r"\+\d{8,15}", v):
        raise ValidationError(_("Enter the number in international format, starting with +, e.g. +7 701 2345678."))
    return v


def format_phone(value: str) -> str:
    """The standard way to show a phone: +7 701 2345678 (the country code, 3 digits, the rest)."""
    v = re.sub(r"[\s\-().]", "", value or "")
    if re.fullmatch(r"\+7\d{10}", v):
        return f"+7 {v[2:5]} {v[5:]}"
    return value or ""