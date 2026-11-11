"""Infrastructure → Secrets: users, BGP, OpenNebula and public keys (inventory.models.Secret).

Each secret is new (not used), used, replaced (its contributors were moved to another secret, the old one may still
be accepted somewhere) or archived (kept for the record; shown on demand). Values are stored encrypted; lists show
whether a value is kept, never the value; the super admin can reveal one (logged). Replace and archive: secret_ops.
"""
import re

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from accounts.models import Role, audit
from accounts.utils import staff_required

from . import credentials as cr
from . import secret_ops
from .models import Network, Secret, Site

TABS = ("user", "bgp", "opennebula", "key", "password", "s3")
NAME_RE = r"^[A-Za-z0-9_@-]+(\.[A-Za-z0-9_@-]+)*$"


def _secret_input(help_text=""):
    return forms.CharField(required=False, strip=False, help_text=help_text,
                           widget=forms.PasswordInput(attrs={"autocomplete": "new-password", "class": "mono"}, render_value=False))


def _key_input(help_text=""):
    return forms.CharField(required=False, strip=False, help_text=help_text,
                           widget=forms.Textarea(attrs={"rows": 4, "class": "mono", "spellcheck": "false", "autocomplete": "off"}))


class _Base(forms.Form):
    kind = None

    def __init__(self, *args, instance=None, **kwargs):
        self.instance = instance
        super().__init__(*args, **kwargs)
        for f in Secret.SECRET_FIELDS:  # a kept value is shown as set, never as its content
            if f in self.fields:
                set_ = instance is not None and instance.has(f)
                self.fields[f].widget.attrs.update(
                    {"placeholder": _("●●●●●●●● set — empty keeps it") if set_ else _("not set"),
                     "data-secret": "set" if set_ else "empty"})

    def _unique(self, name):
        qs = Secret.objects.filter(kind=self.kind, name=name)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(_("A secret of this type is already named %(n)s.") % {"n": name})
        return name

    def _keep_or_new(self, field, cleaned, required, min_len=0, label=""):
        v = cleaned.get(field) or ""
        if v and "key" not in field:
            from accounts.policy import specials
            bad = sorted({c for c in v if not (c.isascii() and c.isalnum()) and c not in specials()})
            if bad:
                self.add_error(field, _("Not allowed: %(c)s (Settings → Password policy).") % {"c": " ".join(bad)})
        if v and min_len and len(v) < min_len:
            self.add_error(field, _("At least %(n)s characters.") % {"n": min_len})
        if required and not v and not (self.instance is not None and self.instance.has(field)):
            self.add_error(field, _("Required."))
        return v


class UserForm(_Base):
    kind = Secret.Kind.USER
    name = forms.RegexField(label=_l("Name"), regex=NAME_RE, max_length=200, help_text=_l("Unique among users, e.g. cloud.admins.sa.ops"))
    username = forms.CharField(label=_l("User name"), max_length=100)
    password = _secret_input(_l("Optional when a private key is given."))
    private_key = _key_input(_l("ssh private key (OpenSSH). Optional when a password is given."))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from accounts.policy import PolicyPasswordInput  # the rules of Settings under the field (correction 11)
        self.fields["password"].widget = PolicyPasswordInput(attrs={"class": "mono"})
    purpose = forms.CharField(label=_l("Purpose"), required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def clean_name(self):
        return self._unique(self.cleaned_data["name"])

    def clean(self):
        d = super().clean()
        pw, key = d.get("password") or "", (d.get("private_key") or "").strip()
        if pw:
            from accounts.policy import problems
            bad = problems(pw)
            if bad:
                self.add_error("password", _("The password needs: %(what)s.") % {"what": "; ".join(bad)})
        if key and not cr._public_key(key):
            self.add_error("private_key", _("Not an OpenSSH private key (without a passphrase)."))
        has_pw = bool(pw) or (self.instance is not None and self.instance.has("password"))
        has_key = bool(key) or (self.instance is not None and self.instance.has("private_key"))
        if not (has_pw or has_key):
            self.add_error("password", _("Give a password or a private key."))
        return d

    def apply(self, s):
        d = self.cleaned_data
        s.name, s.username, s.purpose = d["name"], d["username"], d.get("purpose") or ""
        if d.get("password"):
            s.put("password", d["password"])
        key = (d.get("private_key") or "").strip()
        if key:
            s.put("private_key", key + "\n")
            s.public_key = cr._public_key(key)


class BgpForm(_Base):
    kind = Secret.Kind.BGP
    name = forms.RegexField(label=_l("Name"), regex=NAME_RE, max_length=200, required=False,
                            help_text=_l("Unique among BGP secrets. Empty: the site network, e.g. dc2.cloud-ix."))
    net_type = forms.ChoiceField(label=_l("Type"), choices=Secret.NET_TYPES)
    network = forms.ChoiceField(label=_l("Site network"), required=False,
                                help_text=_l("The network whose BGP peer groups use these passwords by default."))
    rrs = _secret_input(_l("Route reflectors."))
    igw = _secret_input(_l("Internal peers (igw): project gateways or KVM hosts."))
    egw = _secret_input(_l("External peers (egw): ix networks only."))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        nets = Network.objects.filter(kind__in=["ix", "evpn"]).order_by("site", "name")
        self.fields["network"].choices = [("", "—")] + [(f"{n.site}.{n.name}", f"{n.site}.{n.name} ({n.kind})") for n in nets]

    def clean(self):
        d = super().clean()
        d["name"] = (d.get("name") or d.get("network") or "").strip()
        if not d["name"]:
            self.add_error("name", _("Give a name or choose the site network."))
        else:
            try:
                self._unique(d["name"])
            except forms.ValidationError as exc:
                self.add_error("name", exc)
        for f in ("rrs", "igw"):
            self._keep_or_new(f, d, True)
        self._keep_or_new("egw", d, d.get("net_type") == "ix")
        return d

    def apply(self, s):
        d = self.cleaned_data
        s.name, s.net_type = d["name"], d["net_type"]
        if d.get("network"):
            s.site, s.network = d["network"].split(".", 1)
        elif s.pk is None:
            s.site = s.network = ""
        for f in ("rrs", "igw", "egw"):
            if d.get(f):
                s.put(f, d[f])


class OneForm(_Base):
    kind = Secret.Kind.ONE
    name = forms.RegexField(label=_l("Name"), regex=NAME_RE, max_length=200, help_text=_l("Unique, e.g. cloud.one"))
    os_password = _secret_input(_l("OS user oneadmin (uid 9869): at least 30 characters."))
    os_private_key = _key_input(_l("OS user oneadmin: ssh private key."))
    db_name = forms.CharField(label=_l("Database"), max_length=64, initial="onedb")
    db_user = forms.CharField(label=_l("Database user"), max_length=64, initial="onedbuser")
    db_password = _secret_input(_l("At least 30 characters."))
    one_password = _secret_input(_l("OpenNebula super admin oneadmin (uid 0): at least 15 characters."))

    def clean_name(self):
        return self._unique(self.cleaned_data["name"])

    def clean(self):
        d = super().clean()
        self._keep_or_new("os_password", d, True, 30)
        self._keep_or_new("os_private_key", d, True)
        self._keep_or_new("db_password", d, True, 30)
        self._keep_or_new("one_password", d, True, 15)
        key = (d.get("os_private_key") or "").strip()
        if key and not cr._public_key(key):
            self.add_error("os_private_key", _("Not an OpenSSH private key (without a passphrase)."))
        return d

    def apply(self, s):
        d = self.cleaned_data
        s.name, s.db_name, s.db_user = d["name"], d["db_name"], d["db_user"]
        s.username = Secret.OS_USER
        for f in ("os_password", "db_password", "one_password"):
            if d.get(f):
                s.put(f, d[f])
        key = (d.get("os_private_key") or "").strip()
        if key:
            s.put("os_private_key", key + "\n")
            s.public_key = cr._public_key(key)


class KeyForm(_Base):
    kind = Secret.Kind.KEY
    key = forms.CharField(label=_l("Key"), widget=forms.Textarea(attrs={"rows": 4, "class": "mono", "spellcheck": "false"}),
                          help_text=_l("One OpenSSH key, public (type, key and comment) or private (without a passphrase): "
                                       "from a private key the public one is made, and the private key is kept encrypted. "
                                       "It cannot be changed after adding."))
    name = forms.RegexField(label=_l("Name"), regex=r"^[^\s].{0,199}$", required=False, help_text=_l("Empty: the key's comment."))

    def clean(self):
        d = super().clean()
        raw = (d.get("key") or "").strip()
        d["private"] = ""
        if "PRIVATE KEY" in raw:  # owner 2026-10-29: a private key gives its public key
            pub = cr._public_key(raw)
            if not pub:
                self.add_error("key", _("Not an OpenSSH private key (without a passphrase)."))
                return d
            d["private"], raw = raw, pub
        parts = raw.split()
        if len(parts) < 2 or not re.match(r"^(ssh-(ed25519|rsa|dss)|ecdsa-sha2-nistp\d+|sk-\S+)$", parts[0]):
            self.add_error("key", _("Not an OpenSSH public key."))
            return d
        d["key_type"], d["key_b64"], d["comment"] = parts[0], parts[1], " ".join(parts[2:])
        name = (d.get("name") or d["comment"] or parts[1][-12:]).strip()
        try:
            d["name"] = self._unique(name)
        except forms.ValidationError as exc:
            self.add_error("name", exc)
        if Secret.objects.filter(kind=self.kind, public_key__startswith=f"{parts[0]} {parts[1]}").exists():
            self.add_error("key", _("This key is already kept."))
        return d

    def apply(self, s):
        d = self.cleaned_data
        s.name, s.key_type, s.comment = d["name"], d["key_type"], d["comment"]
        s.public_key = f"{d['key_type']} {d['key_b64']}" + (f" {d['comment']}" if d["comment"] else "")
        if d.get("private"):
            s.put("private_key", d["private"] + "\n")


class PasswordForm(_Base):
    kind = Secret.Kind.PASSWORD
    name = forms.RegexField(label=_l("Name"), regex=NAME_RE, max_length=200, help_text=_l("Unique among passwords, e.g. dc1.restic.mgmt"))
    password = _secret_input(_l("e.g. the password of a restic repository."))
    purpose = forms.CharField(label=_l("Purpose"), required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def clean_name(self):
        return self._unique(self.cleaned_data["name"])

    def clean(self):
        d = super().clean()
        self._keep_or_new("password", d, True)
        return d

    def apply(self, s):
        d = self.cleaned_data
        s.name, s.purpose = d["name"], d.get("purpose") or ""
        if d.get("password"):
            s.put("password", d["password"])


class S3Form(_Base):
    kind = Secret.Kind.S3
    name = forms.RegexField(label=_l("Name"), regex=NAME_RE, max_length=200, help_text=_l("Unique among S3 secrets, e.g. dc1.s3.backup"))
    s3_key = _secret_input(_l("Access key."))
    s3_secret = _secret_input(_l("Secret key."))
    purpose = forms.CharField(label=_l("Purpose"), required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["s3_key"].label, self.fields["s3_secret"].label = _l("Key"), _l("Secret")

    def clean_name(self):
        return self._unique(self.cleaned_data["name"])

    def clean(self):
        d = super().clean()
        if d.get("s3_key") and any(c.isspace() for c in d["s3_key"]):
            self.add_error("s3_key", _("No spaces."))
        if not self.errors.get("s3_key"):
            if not d.get("s3_key") and not (self.instance is not None and self.instance.has("s3_key")):
                self.add_error("s3_key", _("Required."))
        if not d.get("s3_secret") and not (self.instance is not None and self.instance.has("s3_secret")):
            self.add_error("s3_secret", _("Required."))
        return d

    def apply(self, s):
        d = self.cleaned_data
        s.name, s.purpose = d["name"], d.get("purpose") or ""
        for f in ("s3_key", "s3_secret"):
            if d.get(f):
                s.put(f, d[f])


FORMS = {Secret.Kind.USER: UserForm, Secret.Kind.BGP: BgpForm, Secret.Kind.ONE: OneForm, Secret.Kind.KEY: KeyForm,
         Secret.Kind.PASSWORD: PasswordForm, Secret.Kind.S3: S3Form}


def _initial(s):
    if s.kind == Secret.Kind.USER:
        return {"name": s.name, "username": s.username, "purpose": s.purpose}
    if s.kind == Secret.Kind.BGP:
        return {"name": s.name, "net_type": s.net_type, "network": f"{s.site}.{s.network}" if s.site and s.network else ""}
    if s.kind == Secret.Kind.ONE:
        return {"name": s.name, "db_name": s.db_name, "db_user": s.db_user}
    if s.kind in (Secret.Kind.PASSWORD, Secret.Kind.S3):
        return {"name": s.name, "purpose": s.purpose}
    return {}


def _url(tab, sel=None, archived=False):
    return reverse("infra_passwords") + f"?tab={tab}" + ("&archived=1" if archived else "") + (f"&sel=s-{sel}" if sel else "")


@staff_required(Role.RESOURCE_MANAGER, Role.SUPPORT)
def secrets_view(request):
    tab = request.GET.get("tab", "user")
    tab = {"active": "user", "keys": "key"}.get(tab, tab)
    tab = tab if tab in TABS else "user"
    show_archived = request.GET.get("archived") == "1"
    can_edit = request.user.is_super_admin
    used = cr.contributors()
    qs = Secret.objects.filter(kind=tab)
    if not show_archived:
        qs = qs.exclude(state=Secret.State.ARCHIVED)
    rows = list(qs.select_related("replaced_by", "updated_by"))
    for s in rows:
        s.used_by = used.get(s.pk, [])
        s.status = cr.status_of(s, used)
        s.form = FORMS[s.kind](prefix=f"s{s.pk}", instance=s, initial=_initial(s)) if can_edit and s.kind != Secret.Kind.KEY else None
        s.alternatives = [x for x in Secret.objects.filter(kind=s.kind, state=Secret.State.ACTIVE).exclude(pk=s.pk)] if can_edit else []
        s.checks = (s.extra or {}).get("checks") or {}
        s.blockers = secret_ops.archive_blockers(s) if s.state != Secret.State.ARCHIVED else []
    counts = {k: Secret.objects.filter(kind=k).exclude(state=Secret.State.ARCHIVED).count() for k in TABS}
    return render(request, "inventory/secrets.html", {
        "tab": tab, "rows": rows, "counts": counts, "can_edit": can_edit, "show_archived": show_archived,
        "new_form": FORMS[tab](prefix="new", initial={"db_name": "onedb", "db_user": "onedbuser"}) if can_edit else None,
        "unresolved": cr.unresolved(), "tabs": [(k, Secret.Kind(k).label) for k in TABS],
    })


@require_POST
@staff_required(super_only=True)
def secret_save_view(request, kind=None, pk=None):
    s = get_object_or_404(Secret, pk=pk) if pk else Secret(kind=kind)
    if s.kind not in FORMS or (pk and (s.kind == Secret.Kind.KEY or s.state == Secret.State.ARCHIVED)):
        raise PermissionDenied
    form = FORMS[s.kind](request.POST, prefix=f"s{pk}" if pk else "new", instance=s if pk else None,
                         initial=_initial(s) if pk else None)
    if not form.is_valid():
        for field, errs in form.errors.items():
            for e in errs:
                messages.error(request, f"{form.fields[field].label}: {e}" if field in form.fields and field != "__all__" else e)
        return redirect(_url(s.kind, pk))
    form.apply(s)
    s.updated_by = request.user
    s.save()
    audit(request.user, "secret_saved", s, kind=s.kind, name=s.name, created=not pk,
          changed=",".join(f for f in Secret.SECRET_FIELDS if form.cleaned_data.get(f)))
    messages.success(request, _("Secret %(n)s saved.") % {"n": s.name})
    return redirect(_url(s.kind, s.pk))


@require_POST
@staff_required(super_only=True)
def secret_replace_view(request, pk):
    old = get_object_or_404(Secret, pk=pk)
    new = get_object_or_404(Secret, pk=request.POST.get("new") or 0)
    try:
        task = secret_ops.replace(old, new, request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(_url(old.kind, pk))
    messages.success(request, _("%(o)s replaced by %(n)s; the systems are checked (Tasks).") % {"o": old.name, "n": new.name})
    if task.state == "done":
        old.refresh_from_db()
    return redirect(_url(old.kind, pk))


@require_POST
@staff_required(super_only=True)
def secret_check_view(request, pk):
    from accounts import tasks
    s = get_object_or_404(Secret, pk=pk, state=Secret.State.REPLACED)
    tasks.start("secret check", f"{s.get_kind_display()} {s.name}", lambda task: secret_ops.run_checks(task, s),
                user=request.user, target=s)
    messages.info(request, _("Checking the systems (Tasks)."))
    return redirect(_url(s.kind, pk))


@require_POST
@staff_required(super_only=True)
def secret_archive_view(request, pk):
    s = get_object_or_404(Secret, pk=pk)
    if s.state == Secret.State.ARCHIVED:
        return redirect(_url(s.kind, pk, archived=True))
    why = secret_ops.archive_blockers(s)
    if why:
        messages.error(request, _("Cannot archive: %(why)s.") % {"why": "; ".join(why)})
        return redirect(_url(s.kind, pk))
    secret_ops.archive(s, request.user)
    messages.success(request, _("Secret %(n)s archived.") % {"n": s.name})
    return redirect(_url(s.kind, archived=True))


REVEAL_FIELDS = {
    Secret.Kind.USER: (("password", _l("Password")), ("private_key", _l("Private key"))),
    Secret.Kind.BGP: (("rrs", "rrs"), ("igw", "igw"), ("egw", "egw")),
    Secret.Kind.ONE: (("os_password", _l("OS user oneadmin: password")), ("os_private_key", _l("OS user oneadmin: private key")),
                      ("db_password", _l("Database password")), ("one_password", _l("OpenNebula oneadmin: password"))),
    Secret.Kind.PASSWORD: (("password", _l("Password")),),
    Secret.Kind.S3: (("s3_key", _l("Key")), ("s3_secret", _l("Secret"))),
}


@require_POST
@staff_required(super_only=True)
def secret_reveal_view(request, pk):
    s = get_object_or_404(Secret, pk=pk)
    fields = [(label, s.get(f)) for f, label in REVEAL_FIELDS.get(s.kind, ()) if s.has(f)]
    audit(request.user, "secret_revealed", s, kind=s.kind, name=s.name)
    return render(request, "inventory/credential_reveal.html", {"s": s, "fields": fields, "back": _url(s.kind, pk)})


@require_POST
@staff_required(super_only=True)
def secret_values_view(request, pk):
    """The values of a secret for its edit dialog (the "Show values" switch), as the dialog's field names. Logged."""
    from django.http import JsonResponse
    s = get_object_or_404(Secret, pk=pk)
    out = {f"s{s.pk}-{f}": s.get(f) for f, _label in REVEAL_FIELDS.get(s.kind, ()) if s.has(f)}
    audit(request.user, "secret_revealed", s, kind=s.kind, name=s.name, where="edit dialog")
    return JsonResponse(out)


@staff_required(Role.RESOURCE_MANAGER, Role.SUPPORT)
def old_url_view(request, tab="user"):
    return redirect(_url(tab))
