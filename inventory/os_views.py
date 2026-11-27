"""Profiles → OS (owner 2026-11-24, T36 rules v2 C1 and D; mockup m01 approved 21:11): the logging line common to every
OS (Queue, Retry, Days and Size defaults), the two OS - base-os (Ubuntu) and vyos - with their version, minimum
resources, installation media and logging rules (the logged services common and vyos), and the services built on
each. base-os also carries what Profiles → Baseline showed for it: the platforms checked against the base OS (and each
platform's own OpenNebula) and the hosts' installation media. The version is the baseline's: base-os writes
PortalSettings.baseline_os (a new revision, the platforms checked again), vyos baseline_vyos (a reference value)."""

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

from . import baseline, logconf
from .logging_views import SettingsForm, decorate
from .models import ConfigTemplate, HostService, OsProfile, ServiceProfile


class OsError(Exception):
    pass


class OsForm(forms.Form):
    version = forms.CharField(label=_l("Version"), max_length=32)
    min_vcpu = forms.IntegerField(label=_l("Min vCPU"), min_value=1, max_value=64)
    min_ram_gb = forms.IntegerField(label=_l("Min RAM, GB"), min_value=1, max_value=512)
    min_disk_gb = forms.IntegerField(label=_l("Min disk, GB"), min_value=1, max_value=4096)
    media_url = forms.URLField(
        label=_l("Installation media"),
        required=False,
        max_length=500,
        help_text=_l("The customised VyOS qcow2 image."),
    )

    def __init__(self, *args, os_key="", **kwargs):
        super().__init__(*args, **kwargs)
        self.os_key = os_key
        if os_key != OsProfile.VYOS:
            del self.fields["media_url"]  # base-os: the ISO the portal nodes keep (Installation media)
        if os_key == OsProfile.BASE_OS:  # T36 B5a (QB5-1): what every base-os VM is installed with first
            from .function_views import install_choices
            from .services import configs_field, configs_widget

            self.fields["install_template"] = forms.ChoiceField(
                label=_l("Installation"),
                required=False,
                choices=install_choices(),
                help_text=_l("Every base-os VM runs it first, then its functions' (Profiles → Templates)."),
            )
            self.fields["configs"] = configs_field()
            configs_widget(self.fields["configs"])

    def clean_configs(self):
        from .services import parse_configs

        return parse_configs(self.cleaned_data.get("configs"))

    def clean_install_template(self):
        v = self.cleaned_data.get("install_template")
        return int(v) if v else None

    def clean_version(self):
        v = " ".join((self.cleaned_data.get("version") or "").split())
        if self.os_key == OsProfile.BASE_OS and not baseline.OS_RE.match(v):
            raise forms.ValidationError(_("An Ubuntu LTS release, e.g. 26.04."))
        if self.os_key == OsProfile.VYOS and not baseline.VYOS_RE.match(v):
            raise forms.ValidationError(_("The release as VyOS names it, e.g. Stream 2026.03."))
        return v


def _used_by(key):
    """The services a role of which runs on this OS (Profiles → Services); commit-480: one without roles by its own."""
    return [
        p
        for p in ServiceProfile.objects.order_by("key")
        if any((r or {}).get("os") == key for r in p.roles or []) or (not p.roles and p.os == key)
    ]


def baseline_version(o, conf):
    """commit-478: the version is the baseline's (one source); OsProfile.version follows it."""
    return conf.baseline_os if o.key == OsProfile.BASE_OS else conf.baseline_vyos


def rows():
    from tenants.models import PortalSettings

    conf = PortalSettings.get()
    rules = logconf.rules_of(conf)
    out = list(OsProfile.objects.select_related("logging"))
    for o in out:
        o.version = baseline_version(o, conf)
        o.functions = list(HostService.objects.filter(group=HostService.Group.FUNCTION, os=o.key).order_by("key"))
        o.used_by = _used_by(o.key)
        initial = {f: getattr(o, f) for f in ("version", "min_vcpu", "min_ram_gb", "min_disk_gb", "media_url")}
        o.unit = HostService.objects.filter(key=OsProfile.BASE_OS).first() if o.key == OsProfile.BASE_OS else None
        if o.unit is not None:  # T36 B5a: its installation and config files (the base-os row)
            from .services import configs_text

            initial.update(install_template=str(o.unit.install_template_id or ""), configs=configs_text(o.unit))
            roles = dict(ConfigTemplate.objects.values_list("pk", "role"))
            o.unit_configs = [
                (c, roles.get(c.get("template"), "?")) for c in o.unit.configs or [] if isinstance(c, dict)
            ]
        o.form = OsForm(initial=initial, os_key=o.key, prefix=f"os-{o.key}")
        o.logged = decorate(o.logging, rules) if o.logging_id else None
    return out


@staff_required(Role.SUPPORT, Role.RESOURCE_MANAGER)
def os_view(request):
    from accounts.views import _baseline_context
    from tenants.models import PortalSettings

    conf = PortalSettings.get()
    ctx = _baseline_context(conf)  # base-os: the platforms against the baseline, the installation media
    ctx.update(
        {
            "rows": rows(),
            "rules": conf,
            "settings_form": SettingsForm(initial={f: getattr(conf, f) for f in SettingsForm.base_fields}),
            "can_edit": request.user.is_super_admin,
            "logcap": logconf.LOGCAP,
            "next": reverse("infra_os"),
        }
    )
    return render(request, "inventory/os.html", ctx)


def save(user, o, d):
    """The OS's version and minimum resources; the version is the baseline's (base-os: a new revision, the platforms
    checked again - as Profiles → Baseline did, commit-177/178)."""
    from tenants.models import PortalSettings

    from .function_views import FunctionError, check_install

    field = "baseline_os" if o.key == OsProfile.BASE_OS else "baseline_vyos"
    with transaction.atomic():
        ServiceProfile.install_lock()  # commit-561: first, as Profiles → Functions and the service profiles' saves
        if o.key == OsProfile.BASE_OS and "install_template" in d:  # T36 B5a: checked, then kept with the rest
            unit = HostService.objects.filter(key=OsProfile.BASE_OS).first()
            if unit is not None:
                try:
                    check_install(unit, d.get("install_template") or "", d.get("configs") or [])
                except FunctionError as exc:
                    raise OsError(str(exc)) from exc
                unit.save(update_fields=["install_template", "configs"])
        row = PortalSettings.objects.select_for_update().get(pk=PortalSettings.get().pk)
        o = OsProfile.objects.select_for_update().get(pk=o.pk)
        new_version = d["version"] != getattr(row, field)
        fields = {field: d["version"]} if new_version else {}
        if new_version and o.key == OsProfile.BASE_OS:
            fields["baseline_rev"] = row.baseline_rev + 1
        if fields:
            PortalSettings.objects.filter(pk=row.pk).update(updated_by=user, **fields)
        for f in ("version", "min_vcpu", "min_ram_gb", "min_disk_gb", "media_url"):
            if f in d:
                setattr(o, f, d[f] if d[f] is not None else "")
        o.save()
    audit(
        user,
        "os_saved",
        None,
        os=o.key,
        **{k: v for k, v in d.items() if v not in (None, "") and k != "configs"},
        configs=[c.get("path") for c in d.get("configs") or []],
    )
    return new_version and o.key == OsProfile.BASE_OS


@require_POST
@staff_required(super_only=True)
def os_save_view(request, key):
    o = get_object_or_404(OsProfile, key=key)
    form = OsForm(request.POST, os_key=o.key, prefix=f"os-{o.key}")
    if form.is_valid():
        try:
            reread = save(request.user, o, form.cleaned_data)
        except OsError as exc:
            messages.error(request, str(exc))
            return redirect(reverse("infra_os") + f"?sel=os-{o.key}")
        if reread:
            from accounts import timers

            timers.start("baseline", user=request.user)  # the platforms against the new base OS at once
            messages.success(request, _("Saved. The platforms are checked against the new base OS."))
        else:
            messages.success(request, _("Saved."))
    else:
        for f, errs in form.errors.items():
            label = form.fields[f].label if f in form.fields else ""
            messages.error(request, f"{label}: {' '.join(errs)}" if label else " ".join(errs))
    return redirect(reverse("infra_os") + f"?sel=os-{o.key}")
