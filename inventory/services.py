"""Infrastructure → Services (owner 2026-11-21, T33): the services the platform's machines run - how each is found on a
machine (present when), its log_type, the logs rsyslog sends to the site's log collector and their local rotation -
with the tabs Services, Detected and Logging rules (owner 07:03 "services, roles, rules - make them tabs"; roles gone
07:17: a deployment finds the services itself). The files they make: inventory/logconf.py; the deployment step:
hostdeploy.s_logging.
"""

from django import forms
from django.contrib import messages
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from accounts.models import Role, audit
from accounts.utils import staff_required

from . import logconf
from .models import Host, HostService
from .network_views import rows_widget

SEP = "¦"  # the sources' fields: their regular expressions may hold | and spaces
GROUPS = ["platform", "jump", "system"]
SEND_LABELS = {
    "all": _l("all"),
    "warning": _l("warning and above"),
    "matching": _l("matching"),
    "not_matching": _l("not matching"),
}
ROTATE_LABELS = {
    "copytruncate": _l("copy, then truncate"),
    "reopen": _l("rsyslog reopens the file"),
    "command": _l("run a command after"),
    "own": _l("the service rotates its own"),
}


class ServiceError(Exception):
    pass


class ServiceForm(forms.Form):
    key = forms.RegexField(
        label=_l("Key"),
        regex=logconf.KEY,
        help_text=_l("Names its files (NN-<key>.conf). It cannot be changed later."),
    )
    name = forms.CharField(label=_l("Name"), max_length=100)
    group = forms.ChoiceField(label=_l("Group"), choices=HostService.Group.choices)
    log_type = forms.RegexField(
        label="log_type",
        regex=r"^[a-z0-9][a-z0-9_-]{0,39}$",
        required=False,
        help_text=_l("Sent with every message of its sources. Empty: the key."),
    )
    present_when = forms.CharField(
        label=_l("Present when"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 2, "class": "mono"}),
        help_text=_l(
            "Checked on the machine during its deployment; any one is enough. None: every machine. "
            "package, unit, file, module (loaded), pci (vendor id, e.g. 10de)."
        ),
    )
    sources = forms.CharField(
        label=_l("Log sources"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "class": "mono"}),
        help_text=_l(
            "file: a file rsyslog reads; program: a program's messages to syslog or the journal; contains: messages "
            "holding the text (kept in /var/log/<key>.log). Filter: a regular expression. Entry starts with: for "
            "entries over several lines. None: its messages go with the common sources."
        ),
    )
    rotate_paths = forms.CharField(
        label=_l("Rotated files"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 2, "class": "mono"}),
        help_text=_l("One per line; patterns allowed."),
    )
    rotate_method = forms.ChoiceField(
        label=_l("Rotation"), choices=[(k, v) for k, v in ROTATE_LABELS.items()], initial="copytruncate"
    )
    rotate_command = forms.CharField(label=_l("Command after rotation"), max_length=300, required=False)
    kept_days = forms.IntegerField(
        label=_l("Kept, days"), min_value=1, max_value=366, required=False, help_text=_l("Empty: the default.")
    )
    units = forms.CharField(label=_l("Units"), max_length=500, required=False, help_text=_l("Space separated."))
    notes = forms.CharField(label=_l("Notes"), required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        rows_widget(
            self.fields["present_when"],
            [("kind", _("Check"), "", "8em", logconf.PRESENT_KINDS), ("value", _("Value"), "opennebula")],
            _("Check"),
        )
        rows_widget(
            self.fields["sources"],
            [
                ("kind", _("Read from"), "", "7em", logconf.SOURCE_KINDS),
                ("match", _("Path / program / text"), "/var/log/one/oned.log"),
                ("tag", _("Tag"), "one-oned", "8em"),
                ("send", _("Send"), "", "8em", logconf.SEND),
                ("filter", _("Filter"), ""),
                ("start", _("Entry starts with"), ""),
                ("vm_id", _("vm_id from file name"), "", "5em", ("yes",)),
            ],
            _("Log source"),
            sep=SEP,
        )

    def clean_present_when(self):
        out = []
        for line in (self.cleaned_data.get("present_when") or "").splitlines():
            parts = line.split(None, 1)
            if not parts:
                continue
            if parts[0] not in logconf.PRESENT_KINDS or len(parts) < 2:
                raise forms.ValidationError(_l("Not a check: %(l)s") % {"l": line.strip()})
            out.append({"kind": parts[0], "value": parts[1].strip()})
        return out

    def clean_sources(self):
        out = []
        for line in (self.cleaned_data.get("sources") or "").splitlines():
            f = [x.strip() for x in line.split(SEP)]
            if not any(f):
                continue
            f += [""] * (7 - len(f))
            kind, match, tag, send, flt, start, vm = f[:7]
            send = send or "all"
            if kind not in logconf.SOURCE_KINDS or not match:
                raise forms.ValidationError(_l("Not a log source: %(l)s") % {"l": line.strip()})
            if send not in logconf.SEND:
                raise forms.ValidationError(_l("Not a way to send: %(l)s") % {"l": line.strip()})
            if send in ("matching", "not_matching") and not flt:
                raise forms.ValidationError(_l("Give the filter: %(l)s") % {"l": line.strip()})
            if kind == "file" and not match.startswith("/"):
                raise forms.ValidationError(_l("A file is a full path: %(l)s") % {"l": line.strip()})
            d = {"kind": kind, "match": match, "tag": tag, "send": send}
            if flt:
                d["filter"] = flt
            if start:
                d["start"] = start
            if vm:
                d["vm_id"] = True
            out.append(d)
        return out

    def clean(self):
        d = super().clean()
        if (
            d.get("rotate_method") == "command"
            and d.get("rotate_paths")
            and not (d.get("rotate_command") or "").strip()
        ):
            self.add_error("rotate_command", _l("Give the command."))
        return d


def present_text(s):
    return "\n".join(f"{c.get('kind')} {c.get('value')}" for c in s.present_when or [])


def sources_text(s):
    return "\n".join(
        f" {SEP} ".join(
            [
                src.get("kind") or "",
                src.get("match") or "",
                src.get("tag") or "",
                src.get("send") or "all",
                src.get("filter") or "",
                src.get("start") or "",
                "yes" if src.get("vm_id") else "",
            ]
        ).rstrip(f" {SEP}")
        for src in s.sources or []
    )


def initial(s):
    r = s.rotate or {}
    return {
        "key": s.key,
        "name": s.name,
        "group": s.group,
        "log_type": s.log_type,
        "present_when": present_text(s),
        "sources": sources_text(s),
        "rotate_paths": "\n".join(r.get("paths") or []),
        "rotate_method": r.get("method") or "copytruncate",
        "rotate_command": r.get("command") or "",
        "kept_days": s.kept_days,
        "units": " ".join(s.units or []),
        "notes": s.notes,
    }


def save(user, key, d, new=False):
    with transaction.atomic():
        s = HostService.objects.select_for_update().filter(key=key).first()
        if new and s is not None:
            raise ServiceError(f"Service {key} already exists.")
        if not new and s is None:
            raise ServiceError(f"Service {key} does not exist.")
        s = s or HostService(key=key)
        s.name, s.group, s.log_type = d["name"], d["group"], d.get("log_type") or ""
        s.present_when, s.sources = d.get("present_when") or [], d.get("sources") or []
        paths = [p.strip() for p in (d.get("rotate_paths") or "").splitlines() if p.strip()]
        s.rotate = {"paths": paths, "method": d.get("rotate_method") or "copytruncate"}
        if (d.get("rotate_command") or "").strip():
            s.rotate["command"] = d["rotate_command"].strip()
        s.kept_days = d.get("kept_days") or None
        s.units = [u for u in (d.get("units") or "").split() if u]
        s.notes = d.get("notes") or ""
        try:  # what the machine would get: a service that cannot make its files is not saved
            logconf.service_conf(s)
            logconf.service_rotate(s)
            logconf.detect_script([s])
        except logconf.LogConfError as exc:
            raise ServiceError(str(exc)) from exc
        s.save()
    audit(user, "host_service_saved", s, key=key, new=new)
    return s


def delete(user, key):
    s = HostService.objects.filter(key=key).first()
    if s is None:
        raise ServiceError(f"Service {key} does not exist.")
    s.delete()
    audit(user, "host_service_deleted", None, key=key)


class RulesForm(forms.Form):
    log_kept_days = forms.IntegerField(label=_l("Kept on the machine, days"), min_value=1, max_value=366)
    log_queue_mb = forms.IntegerField(
        label=_l("Queue when the collector is down, MB"),
        min_value=10,
        max_value=10000,
        help_text=_l("On disk, kept over a restart; about 30 minutes. When full, the oldest messages are dropped."),
    )
    log_retry_seconds = forms.IntegerField(label=_l("Retry the collector every, seconds"), min_value=5, max_value=600)
    log_syslog_level = forms.TypedChoiceField(
        label=_l("Other messages sent from level"),
        coerce=int,
        choices=[(3, "err"), (4, "warning"), (5, "notice"), (6, "info")],
    )


def _ordered():
    return sorted(HostService.objects.all(), key=lambda s: (GROUPS.index(s.group), s.name.lower()))


@staff_required(Role.SUPPORT, Role.RESOURCE_MANAGER)
def services_view(request):
    from tenants.models import PortalSettings

    from .models import Site

    tab = request.GET.get("tab", "services")
    tab = tab if tab in ("services", "detected", "rules") else "services"
    services = _ordered()
    by_key = {s.key: s for s in services}
    for s in services:
        s.form = ServiceForm(initial=initial(s), auto_id=f"id_sv_{s.key}_%s")
        s.form.fields["key"].disabled = True
        s.rotate_label = ROTATE_LABELS.get((s.rotate or {}).get("method") or "copytruncate", "")
        for src in s.sources or []:
            src["send_label"] = SEND_LABELS.get(src.get("send") or "all", "")
    hosts = list(Host.objects.exclude(services_at=None).select_related("site").order_by("site__code", "name"))
    for h in hosts:
        h.found = [by_key[k] for k in h.services or [] if k in by_key]
        h.unknown = [k for k in h.services or [] if k not in by_key]
    conf = PortalSettings.get()
    sites = []
    for st in Site.objects.all():
        try:
            c = logconf.collector(st)
        except logconf.LogConfError:
            c = None
        sites.append({"site": st, "core": c, "all": st.log_collectors or {}})
    groups = [(g, label, [s for s in services if s.group == g]) for g, label in HostService.Group.choices]
    return render(
        request,
        "inventory/services.html",
        {
            "tab": tab,
            "groups": groups,
            "services": services,
            "hosts": hosts,
            "sites": sites,
            "rules": conf,
            "rules_form": RulesForm(initial={f: getattr(conf, f) for f in RulesForm.base_fields}),
            "service_form": ServiceForm(auto_id="id_sv_new_%s", initial={"group": "platform"}),
            "can_edit": request.user.is_super_admin,
        },
    )


@require_POST
@staff_required(super_only=True)
def service_save_view(request, key=None):
    new = key is None
    form = ServiceForm(request.POST)
    if not new:
        form.fields["key"].required = False
    if form.is_valid():
        key = key or form.cleaned_data["key"]
        try:
            save(request.user, key, form.cleaned_data, new=new)
            messages.success(request, _("Service saved."))
        except ServiceError as exc:
            messages.error(request, str(exc))
    else:
        for f, errs in form.errors.items():
            messages.error(request, f"{form.fields[f].label if f in form.fields else ''}: {' '.join(errs)}")
    return redirect(reverse("infra_services") + (f"?sel=sv-{key}" if key else ""))


@require_POST
@staff_required(super_only=True)
def service_delete_view(request, key):
    try:
        delete(request.user, key)
        messages.success(request, _("Service deleted."))
    except ServiceError as exc:
        messages.error(request, str(exc))
    return redirect(reverse("infra_services"))


@require_POST
@staff_required(super_only=True)
def rules_save_view(request):
    from tenants.models import PortalSettings

    form = RulesForm(request.POST)
    if form.is_valid():
        conf = PortalSettings.get()
        for f, v in form.cleaned_data.items():
            setattr(conf, f, v)
        conf.save(update_fields=list(form.cleaned_data))
        audit(request.user, "logging_rules_saved", None, **form.cleaned_data)
        messages.success(request, _("Logging rules saved."))
    else:
        for f, errs in form.errors.items():
            messages.error(request, f"{form.fields[f].label if f in form.fields else ''}: {' '.join(errs)}")
    return redirect(reverse("infra_services") + "?tab=rules")
