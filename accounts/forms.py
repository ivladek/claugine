"""The forms of the accounts app: registration, invitation, the profile, passwords and two-factor.

The mixins at the top hold what several forms share, so the password policy (`accounts/policy.py`) and the consent
text are applied in one place rather than per form.
"""

from django import forms
from django.conf import settings
from django.contrib.auth import password_validation
from django.contrib.auth.forms import SetPasswordForm
from django.utils.translation import gettext_lazy as _

from tenants.models import Membership, TaxIdType

from .fields import city_field, phone_field
from .kz import normalize_phone
from .models import Role, User
from .policy import PolicyPasswordInput

ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}


def clean_photo_file(photo, required):
    if not photo:
        if required:
            raise forms.ValidationError(_("Please upload a photo."))
        return photo
    if getattr(photo, "size", 0) > settings.PHOTO_MAX_BYTES:
        raise forms.ValidationError(_("The photo must be 5 MB or smaller."))
    image = getattr(photo, "image", None)
    if image is not None and image.format not in ALLOWED_IMAGE_FORMATS:
        raise forms.ValidationError(_("Use a JPEG, PNG or WebP image."))
    return photo


class ProfileFieldsMixin(forms.Form):
    full_name = forms.CharField(label=_("Full name"), max_length=200)
    phone = phone_field()
    city = city_field()
    photo = forms.ImageField(label=_("Photo"), required=False, help_text=_("JPEG, PNG or WebP, up to 5 MB."))

    photo_required = False  # the photo is optional

    def clean_phone(self):
        return normalize_phone(self.cleaned_data["phone"])

    def clean_photo(self):
        return clean_photo_file(self.cleaned_data.get("photo"), self.photo_required)


class PasswordPairMixin(forms.Form):
    password1 = forms.CharField(label=_("Password"), widget=PolicyPasswordInput())
    password2 = forms.CharField(
        label=_("Repeat password"), widget=forms.PasswordInput(attrs={"autocomplete": "new-password"})
    )

    def check_passwords(self, user):
        p1, p2 = self.cleaned_data.get("password1"), self.cleaned_data.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", _("The passwords do not match."))
            return
        if p1:
            try:
                password_validation.validate_password(p1, user)
            except forms.ValidationError as exc:
                self.add_error("password1", exc)


class ConsentMixin(forms.Form):
    consent = forms.BooleanField(
        label=_(
            "I consent to the processing of my personal data under the Law of the Republic of Kazakhstan "
            '"On Personal Data and Their Protection".'
        ),
    )


def _email_free(email):
    existing = User.objects.filter(email__iexact=email).first()
    if existing and (existing.email_verified or existing.is_staff_member):
        raise forms.ValidationError(_("An account with this email already exists. Sign in or reset your password."))
    return existing


def country_fields(form, country="country", kind="tax_id_type", number="tax_id"):
    """The country select (the portal's allowed countries, its home country first) and the tax ID fields that follow
    it (owner 2026-11-12): static/olist.js changes the kinds and the label with the country (data-taxid)."""
    import json

    from tenants.models import PortalSettings

    from . import taxid

    conf = PortalSettings.get()
    codes = conf.countries()
    current = getattr(getattr(form, "instance", None), "country", "") or ""
    if current and current not in codes:  # an organization of a country no longer offered keeps it
        codes = codes + [current]
    names = dict(taxid.countries_choices())
    form.fields[country].choices = [(c, names.get(c, c)) for c in codes]
    form.fields[country].initial = current or codes[0]
    form.fields[country].required = False  # not posted (an old form, an API call): the home country
    form.country_default = current or codes[0]
    form.fields[country].widget.attrs["data-taxid"] = json.dumps(
        {c: {"label": str(taxid.label(c)), "kinds": [[k, str(v)] for k, v in taxid.kinds(c)]} for c in codes}
    )
    first = current or codes[0]
    form.fields[kind].choices = taxid.kinds(first)
    if not getattr(getattr(form, "instance", None), "pk", None):
        form.initial.setdefault(country, first)
        form.initial.setdefault(kind, taxid.kinds(first)[0][0])
    form.fields[kind].required = False
    form.fields[number].label = taxid.label(first)
    form.fields[number].required = True
    if form.is_bound:  # the posted country decides which kinds are valid
        posted = (form.data.get(form.add_prefix(country)) or "").upper()
        if posted:
            form.fields[kind].choices = taxid.kinds(posted)


def clean_tax_id(form, data, country="country", kind="tax_id_type", number="tax_id"):
    """The tax ID of the chosen country, normalized; required in any case (owner 2026-11-12)."""
    from . import taxid

    data[country] = (data.get(country) or getattr(form, "country_default", "") or "KZ").upper()
    if not data.get(number):
        return
    try:
        data[number], data[kind] = taxid.normalize(data.get(country), data[number], data.get(kind) or None)
    except forms.ValidationError as exc:
        form.add_error(number, exc)


class RegistrationForm(ProfileFieldsMixin, PasswordPairMixin, ConsentMixin):
    email = forms.EmailField(label=_("Email"))
    organization_name = forms.CharField(
        label=_("Organization (legal name)"),
        max_length=300,
        help_text=_("Company name, or your full name if you act as an individual entrepreneur."),
    )
    country = forms.ChoiceField(label=_("Country"))
    tax_id_type = forms.ChoiceField(label=_("Tax ID type"), choices=TaxIdType.choices, required=False)
    tax_id = forms.CharField(label=_("Tax ID"), max_length=40)

    field_order = [
        "email",
        "password1",
        "password2",
        "full_name",
        "phone",
        "city",
        "photo",
        "organization_name",
        "country",
        "tax_id_type",
        "tax_id",
        "consent",
    ]

    def __init__(self, *args, invitation=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.invitation = invitation
        country_fields(self)
        if invitation is not None:
            self.fields["email"].initial = invitation.email
            self.fields["email"].disabled = True

    def clean_email(self):
        email = (self.invitation.email if self.invitation else self.cleaned_data["email"]).lower()
        _email_free(email)
        return email

    def clean(self):
        data = super().clean()
        clean_tax_id(self, data)
        self.check_passwords(User(email=data.get("email", ""), full_name=data.get("full_name", "")))
        return data


class InvitationAcceptForm(ProfileFieldsMixin, PasswordPairMixin, ConsentMixin):
    email = forms.EmailField(label=_("Email"))

    field_order = ["email", "password1", "password2", "full_name", "phone", "city", "photo", "consent"]

    def __init__(self, *args, invitation, **kwargs):
        super().__init__(*args, **kwargs)
        self.invitation = invitation
        existing = invitation.user
        if invitation.email:
            self.fields["email"].initial = invitation.email
            self.fields["email"].disabled = True
        if existing:
            for f in ("full_name", "city"):
                self.fields[f].initial = getattr(existing, f)
            self.fields["phone"].initial = existing.phone_display

    def clean_email(self):
        email = (self.invitation.email or self.cleaned_data["email"]).lower()
        qs = User.objects.filter(email__iexact=email, email_verified=True)
        if self.invitation.user_id:
            qs = qs.exclude(pk=self.invitation.user_id)
        if qs.exists():
            raise forms.ValidationError(_("An account with this email already exists."))
        return email

    def clean(self):
        data = super().clean()
        self.check_passwords(User(email=data.get("email", ""), full_name=data.get("full_name", "")))
        return data


class ProfileForm(ProfileFieldsMixin):
    photo_required = False


def _email_taken(email):
    if (
        User.objects.filter(email__iexact=email, email_verified=True).exists()
        or User.objects.filter(email__iexact=email, is_sales=True).exists()
    ):
        raise forms.ValidationError(
            _("This email already has an account. One email can be either staff or a customer.")
        )


class PersonForm(forms.Form):
    """Add a person to a staff tab: a directory entry that can then be invited to register."""

    email = forms.EmailField(label=_("Email"))
    full_name = forms.CharField(label=_("Full name"), max_length=200)
    phone = phone_field(required=False)
    city = city_field()

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(_("This email already exists in the portal."))
        return email

    def clean_phone(self):
        value = self.cleaned_data.get("phone")
        return normalize_phone(value) if value else ""


class PersonEditForm(forms.Form):
    """Owner 2026-10-29 19:57: a super admin completes a user who has not accepted an invitation yet."""

    email = forms.EmailField(label=_("Email"))
    full_name = forms.CharField(label=_("Full name"), max_length=200)
    phone = phone_field(required=False)
    city = city_field()

    def __init__(self, *args, person=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.person = person
        if person is not None and not self.is_bound:
            self.initial.update(
                email=person.email, full_name=person.full_name, phone=person.phone_display, city=person.city
            )

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exclude(pk=self.person.pk if self.person else None).exists():
            raise forms.ValidationError(_("This email already exists in the portal."))
        return email

    def clean_phone(self):
        from .kz import normalize_intl_phone

        value = self.cleaned_data.get("phone")
        return normalize_intl_phone(value) if value else ""


class SalesRepForm(forms.Form):
    email = forms.EmailField(label=_("Email"))
    full_name = forms.CharField(label=_("Full name"), max_length=200)
    phone = phone_field(required=False)
    city = city_field()

    def __init__(self, *args, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance = instance
        if instance:
            self.fields["email"].disabled = True
            self.initial.update(
                email=instance.email,
                full_name=instance.full_name,
                phone=instance.phone_display,
                city=instance.city or "almaty",
            )

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        if self.instance is None and User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(_("This email already exists in the portal."))
        return email

    def clean_phone(self):
        value = self.cleaned_data.get("phone")
        return normalize_phone(value) if value else ""


class OrganizationInviteForm(forms.Form):
    """Staff invite a person to register a new customer organization of a given type."""

    email = forms.EmailField(label=_("Email"))
    org_kind = forms.ChoiceField(
        label=_("Organization type"),
        widget=forms.RadioSelect,
        initial="cloud",
        choices=[("cloud", _("Cloud client")), ("corp", _("Corporate (internal)"))],
    )
    sales_rep = forms.ModelChoiceField(label=_("Sales rep"), required=False, queryset=User.objects.none())

    def __init__(self, *args, inviter=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.inviter = inviter
        if inviter is not None and inviter.is_sales and not inviter.has_role(Role.RESOURCE_MANAGER):
            del self.fields["sales_rep"]  # a sales rep invites for themselves
        else:
            self.fields["sales_rep"].queryset = User.objects.sales_reps().order_by("full_name", "email")

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        _email_free(email)
        return email

    def grants(self):
        rep = self.cleaned_data.get("sales_rep")
        if "sales_rep" not in self.fields and self.inviter is not None:
            rep = self.inviter
        return {"org_kind": self.cleaned_data["org_kind"], "sales_rep": rep.pk if rep else None}


class CustomerInviteForm(forms.Form):
    email = forms.EmailField(label=_("Email"))
    org_admin = forms.BooleanField(
        label=_("Organization admin"),
        required=False,
        help_text=_("Manages users and requests projects; has full access to every project."),
    )

    def __init__(self, *args, tenants=(), allow_org_admin=True, **kwargs):
        super().__init__(*args, **kwargs)
        if not allow_org_admin:
            del self.fields["org_admin"]
        self.tenant_fields = []
        for t in tenants:
            name = f"tenant_{t.pk}"
            self.fields[name] = forms.ChoiceField(
                label=str(t),
                required=False,
                choices=[("", _("no access"))] + list(Membership.TENANT_ROLES),
            )
            self.tenant_fields.append((t, name))

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        _email_taken(email)
        return email

    def clean(self):
        data = super().clean()
        if not data.get("org_admin") and not any(data.get(n) for _t, n in self.tenant_fields):
            raise forms.ValidationError(
                _("Give the user a role in at least one project, or make them organization admin.")
            )
        return data

    def grants(self):
        d = self.cleaned_data
        return {
            "org_admin": bool(d.get("org_admin")),
            "tenants": {str(t.pk): d[n] for t, n in self.tenant_fields if d.get(n)},
        }


class OTPForm(forms.Form):
    code = forms.CharField(
        label=_("6-digit code"),
        max_length=7,
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "numeric", "autofocus": True}),
    )


class PolicySetPasswordForm(SetPasswordForm):
    """Password reset: the new password with the policy shown under it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["new_password1"].widget = PolicyPasswordInput()
        self.fields["new_password1"].help_text = ""
