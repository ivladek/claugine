"""Profiles → Functions (owner 2026-11-24, T36 rules v2 C2 and D; mockup m02 approved 21:11): a function is one Linux
service (or one VyOS purpose) - its OS (fixed once made), the functions it requires, its version where it has one,
its parts (includes), its installation, its logging rules (the logged services linked to it) and the services built
with it (Profiles → Services, their roles). Versions: frr's is the baseline's FRR release (PortalSettings.baseline_frr,
one source, as the OS versions); one-fe's is each platform's OpenNebula (the platform form). The logging rules are
the logged services of Profiles → Logging, edited here; a logged service no function has is listed at the end, to be
linked to one. HostService.group "function" rows are the functions; "part" rows only come with a function."""

from django import forms
from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from accounts.models import Role, audit
from accounts.utils import staff_required

from . import logconf
from .logging_views import NewForm as LoggedNewForm
from .logging_views import decorate
from .models import ConfigTemplate, HostService, LoggedService, OsProfile, ServiceProfile

KEY_RE = r"^[a-z][a-z0-9-]{0,39}$"
VERSION_RE = r"^[0-9A-Za-z][0-9A-Za-z .\-]{0,31}$"
FRR, ONE_FE = "frr", "one-fe"


class FunctionError(Exception):
    pass


class NewForm(forms.Form):
    key = forms.RegexField(label=_l("Name"), regex=KEY_RE, help_text=_l("small letters, digits and -; fixed once made"))
    os = forms.ChoiceField(label=_l("OS"), choices=[(k, k) for k in OsProfile.KEYS])
    name = forms.CharField(label=_l("What it is"), max_length=100)


class EditForm(forms.Form):
    name = forms.CharField(label=_l("What it is"), max_length=100)
    version = forms.RegexField(label=_l("Version"), regex=VERSION_RE, required=False, max_length=32)
    requires = forms.MultipleChoiceField(
        label=_l("Requires"),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text=_l("functions of the same OS a service must have with it"),
    )
    notes = forms.CharField(label=_l("Notes"), required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, fn=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["requires"].choices = [
            (f.key, f.key)
            for f in HostService.objects.filter(group=HostService.Group.FUNCTION, os=fn.os).exclude(pk=fn.pk)
        ]
        if fn.key == ONE_FE:  # rules v2 B4: OpenNebula is each platform's
            del self.fields["version"]


def used_by(key):
    """The services a role of which has this function (Profiles → Services)."""
    return [
        p
        for p in ServiceProfile.objects.order_by("key")
        if any(key in ((r or {}).get("functions") or []) for r in p.roles or [])
    ]


def version_of(fn, conf):
    if fn.key == FRR:
        return conf.baseline_frr
    if fn.key == ONE_FE:
        return _("per platform")
    return fn.version


def _requires_cycle(key, want):
    """True when a function in want requires key, directly or through others."""
    by = {f.key: list(f.requires or []) for f in HostService.objects.filter(group=HostService.Group.FUNCTION)}
    by[key] = list(want)
    seen, stack = set(), list(want)
    while stack:
        k = stack.pop()
        if k == key:
            return True
        if k in seen:
            continue
        seen.add(k)
        stack.extend(by.get(k) or [])
    return False


def rows():
    from tenants.models import PortalSettings

    conf = PortalSettings.get()
    rules = logconf.rules_of(conf)
    templates = set(ConfigTemplate.objects.values_list("role", flat=True))
    out = list(HostService.objects.filter(group=HostService.Group.FUNCTION).prefetch_related("logging_rules__profiles"))
    out.sort(key=lambda f: (OsProfile.KEYS.index(f.os) if f.os in OsProfile.KEYS else 9, f.key))
    for f in out:
        f.shown_version = version_of(f, conf)
        f.used_by = used_by(f.key)
        f.install_template = (
            f.vyos_template
            if f.os == OsProfile.VYOS
            else (f"{f.key}-install" if f"{f.key}-install" in templates else "")
        )
        f.parts = [k for k in f.includes or []]
        f.logged = [decorate(s, rules) for s in sorted(f.logging_rules.all(), key=lambda s: s.name)]
        f.form = EditForm(
            initial={
                "name": f.name,
                "version": f.shown_version if f.key != ONE_FE else "",
                "requires": f.requires or [],
                "notes": f.notes,
            },
            fn=f,
            prefix=f"fn{f.pk}",
        )
    return out


def unlinked():
    from tenants.models import PortalSettings

    rules = logconf.rules_of(PortalSettings.get())
    return [
        decorate(s, rules)
        for s in LoggedService.objects.filter(function__isnull=True)
        .exclude(name__in=(LoggedService.COMMON, "vyos"))
        .prefetch_related("profiles")
        .order_by("name")
    ]


@staff_required(Role.SUPPORT, Role.RESOURCE_MANAGER)
def functions_view(request):
    return render(
        request,
        "inventory/functions.html",
        {
            "rows": rows(),
            "unlinked": unlinked(),
            "new_form": NewForm(prefix="new"),
            "logged_new_form": LoggedNewForm(prefix="new"),
            "can_edit": request.user.is_super_admin,
            "logcap": logconf.LOGCAP,
            "next": reverse("infra_functions"),
        },
    )


def create(user, d):
    with transaction.atomic():
        if HostService.objects.filter(key=d["key"]).exists():
            raise FunctionError(_("%(k)s exists already (a function or a part).") % {"k": d["key"]})
        f = HostService.objects.create(
            key=d["key"], name=d["name"], os=d["os"], group=HostService.Group.FUNCTION, legacy={}
        )
    audit(user, "function_saved", None, key=f.key, new=True)
    return f


def save(user, f, d):
    from tenants.models import PortalSettings

    from . import baseline

    want = list(d.get("requires") or [])
    version = (d.get("version") or "").strip()
    if f.key == FRR and version and (not baseline.VER_RE.match(version) or len(version) > 8):
        # commit-487: the baseline's FRR release - its check and its field's length (commit-175)
        raise FunctionError(_("FRR: a release as major.minor, e.g. 10.6."))
    with transaction.atomic():
        # commit-489: every function row locked and the graph read again inside - two edits cannot make a loop
        list(HostService.objects.select_for_update().filter(group=HostService.Group.FUNCTION).order_by("pk"))
        if _requires_cycle(f.key, want):
            raise FunctionError(_("Requires: a loop - a function it requires requires it."))
        f = HostService.objects.get(pk=f.pk)
        f.name, f.requires, f.notes = d["name"], want, d.get("notes") or ""
        if f.key == FRR and version:  # one source: the baseline's FRR release (new hosts install it)
            PortalSettings.objects.filter(pk=PortalSettings.get().pk).update(baseline_frr=version, updated_by=user)
            f.version = version
        elif f.key != ONE_FE and "version" in d:
            f.version = d.get("version") or ""
        f.save()
    audit(user, "function_saved", None, key=f.key, requires=want, version=f.version)
    return f


def delete(user, f):
    users = used_by(f.key)  # its roles; commit-492: and its services list, which the machines' checks still read
    users += [p for p in ServiceProfile.objects.order_by("key") if f.key in (p.services or []) and p not in users]
    if users:
        raise FunctionError(
            _("%(k)s is used by %(s)s: take it out of their roles first.")
            % {"k": f.key, "s": ", ".join(p.key for p in users)}
        )
    if any(f.key in (o.requires or []) for o in HostService.objects.filter(group=HostService.Group.FUNCTION)):
        raise FunctionError(_("Another function requires %(k)s.") % {"k": f.key})
    key = f.key
    LoggedService.objects.filter(function=f).update(function=None)  # its logging rules stay, unlinked
    f.delete()
    audit(user, "function_deleted", None, key=key)


def _errors(request, form):
    for name, errs in form.errors.items():
        label = form.fields[name].label if name in form.fields else ""
        messages.error(request, f"{label}: {' '.join(errs)}" if label else " ".join(errs))


@require_POST
@staff_required(super_only=True)
def function_new_view(request):
    form = NewForm(request.POST, prefix="new")
    if form.is_valid():
        try:
            f = create(request.user, form.cleaned_data)
            messages.success(request, _("Function added."))
            return redirect(reverse("infra_functions") + f"?sel=fn-{f.pk}")
        except FunctionError as exc:
            messages.error(request, str(exc))
    else:
        _errors(request, form)
    return redirect(reverse("infra_functions"))


@require_POST
@staff_required(super_only=True)
def function_save_view(request, pk):
    f = get_object_or_404(HostService, pk=pk, group=HostService.Group.FUNCTION)
    form = EditForm(request.POST, fn=f, prefix=f"fn{f.pk}")
    if form.is_valid():
        try:
            save(request.user, f, form.cleaned_data)
            messages.success(request, _("Function saved."))
        except FunctionError as exc:
            messages.error(request, str(exc))
    else:
        _errors(request, form)
    return redirect(reverse("infra_functions") + f"?sel=fn-{f.pk}")


@require_POST
@staff_required(super_only=True)
def function_delete_view(request, pk):
    f = get_object_or_404(HostService, pk=pk, group=HostService.Group.FUNCTION)
    try:
        delete(request.user, f)
        messages.success(request, _("Function deleted."))
    except FunctionError as exc:
        messages.error(request, str(exc))
        return redirect(reverse("infra_functions") + f"?sel=fn-{f.pk}")
    return redirect(reverse("infra_functions"))
