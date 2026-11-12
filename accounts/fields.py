"""Phone and city fields of every form (owner 2026-11-12: "make city and phone fields not list boxed. allow enter
text and phone number, just check that phone in correct format, offer + as first symbol always")."""

from django import forms
from django.utils.translation import gettext_lazy as _


class PhoneInput(forms.TextInput):
    """A phone in international format; static/olist.js puts the + first (data-intl-phone)."""

    input_type = "tel"

    def __init__(self, attrs=None):
        base = {"inputmode": "tel", "autocomplete": "tel", "placeholder": "+7 701 2345678", "data-intl-phone": "1"}
        super().__init__({**base, **(attrs or {})})


def phone_field(label=None, required=True, help_text=None):
    return forms.CharField(
        label=label or _("Mobile number"),
        max_length=24,
        required=required,
        widget=PhoneInput(),
        help_text=help_text if help_text is not None else _("International format, starting with +."),
    )


def city_field(label=None, required=True):
    return forms.CharField(label=label or _("City"), max_length=100, required=required)
