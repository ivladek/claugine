"""Profiles → Logging (owner 2026-11-24 08:54 - 09:36, T33; mockup 17 approved 09:24, with log_type 09:36): one list
of logged services - Name, Type, log_type, Days, Size - with the common Queue and Retry above it; details like
Templates (used by, values, rsyslog.conf, the files it generates); Edit in a dialog (rsyslog.conf, Also rotated, After
rotation, Config changes); "+ new" asks the name and log_type. common/base-os and vyos/vyos are predefined: their name,
type and log_type are fixed and they are never deleted; a row a profile uses is not deleted either. Profiles →
Services picks them (inventory/services.py). The machines' files: inventory/logconf.py.
"""

import os
import shutil
import subprocess
import tempfile

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
from .models import LoggedService
from .network_views import rows_widget

SEP = "¦"  # a config change's fields: its regular expression may hold | and spaces
NAME_RE = r"^[a-z0-9][a-z0-9-]{0,39}$"
LOG_TYPE_RE = r"^[a-z0-9][a-z0-9_-]{0,39}$"


class LoggingError(Exception):
    pass


class SettingsForm(forms.Form):
    """Owner 09:06 (1): "define 2 common option for queue size and retry period - add them as one line above list"."""

    log_queue_mb = forms.IntegerField(label=_l("Queue, MB"), min_value=10, max_value=10000)
    log_retry_seconds = forms.IntegerField(label=_l("Retry, s"), min_value=5, max_value=600)
    # T36 rules v2 C1 (mockup m01): the defaults a new logged service starts from - optional for the older form
    log_kept_days = forms.IntegerField(label=_l("Days"), min_value=1, max_value=366, required=False)
    log_size_mb = forms.IntegerField(label=_l("Size, MB"), min_value=1, max_value=100000, required=False)


class NewForm(forms.Form):
    name = forms.RegexField(label=_l("Name"), regex=NAME_RE, help_text=_l("unique, no spaces"))
    log_type = forms.RegexField(label="log_type", regex=LOG_TYPE_RE, required=False, help_text=_l("empty: the name"))
    # T36 B2 (mockup m02): added from a function's Logging card - the function it belongs to
    function = forms.IntegerField(required=False, widget=forms.HiddenInput)


class EditForm(forms.Form):
    # T36 B2 (mockup m02): the function whose logging rules it is (Profiles → Functions); empty: none yet
    function = forms.TypedChoiceField(label=_l("Function"), required=False, coerce=int, empty_value=None)
    name = forms.RegexField(label=_l("Name"), regex=NAME_RE, required=False, help_text=_l("unique, no spaces"))
    log_type = forms.RegexField(
        label="log_type",
        regex=LOG_TYPE_RE,
        required=False,
        help_text=_l("Sent with every message; the rsyslog.conf must set this one. Empty: the name."),
    )
    days = forms.IntegerField(
        label=_l("Days"),
        min_value=1,
        max_value=366,
        required=False,
        help_text=_l("logs older are deleted (rotation once a day)"),
    )
    size_mb = forms.IntegerField(
        label=_l("Size, MB"),
        min_value=1,
        max_value=100000,
        help_text=_l("over it, the oldest logs are deleted first, even younger than Days"),
    )
    also_rotated = forms.CharField(
        label=_l("Also rotated"),
        required=False,
        max_length=2000,
        help_text=_l(
            "Files its rsyslog.conf does not read or write, rotated with them; paths or patterns under /var/log."
        ),
    )
    after_rotation = forms.CharField(
        label=_l("After rotation, run"),
        required=False,
        max_length=300,
        help_text=_l("Empty: its files are copied and truncated (files rsyslog writes: rsyslog reopens them)."),
    )
    rsyslog_conf = forms.CharField(
        label="rsyslog.conf",
        required=False,
        widget=forms.Textarea(attrs={"rows": 18, "class": "mono conf", "spellcheck": "false"}),
        help_text=_l(
            "Plain rsyslog text, no template. Set $.log_type to the log_type above, then call send_logs (the portal "
            "writes it). Checked when saved."
        ),
    )
    config_changes = forms.CharField(
        label=_l("Config changes"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 2, "class": "mono"}),
        help_text=_l(
            "Settings in the service's own files the portal makes sure of with its logging. Each change must change "
            "nothing a second time. A file that is not on the machine is skipped and reported. The file before a "
            "change is kept as <file>.claugine-prev."
        ),
    )
    config_check = forms.CharField(
        label=_l("Check with"),
        max_length=300,
        required=False,
        help_text=_l("Run after a file changed ({file}: the file); when it fails, the previous file is put back."),
    )
    config_then = forms.ChoiceField(
        label=_l("Then"),
        choices=[("nothing", _l("nothing")), ("reload", _l("reload")), ("restart", _l("restart"))],
        initial="nothing",
        required=False,
    )
    config_command = forms.CharField(label=_l("Command"), max_length=300, required=False)
    notes = forms.CharField(label=_l("Notes"), required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        rows_widget(
            self.fields["config_changes"],
            [
                ("file", _("File"), "/etc/bind/named.options", "16em"),
                ("find", _("Find (regular expression)"), r"\s+versions\s+\d+\s+size\s+\w+;"),
                ("replace", _("Replace with"), ";", "10em"),
            ],
            _("Change"),
            sep=SEP,
        )
        # owner 2026-11-27 07:48 rule / 08:18 "yes": one path per row (as Corporate email domains)
        rows_widget(self.fields["also_rotated"], [("p", _("Path or pattern"), "/var/log/named/*.log")], _("Path"))

    def clean_also_rotated(self):
        out = []
        from .network_views import one_per_row

        rows, bad = one_per_row(self.cleaned_data.get("also_rotated"), commas=False)  # commit-596: a path may hold one
        if bad:
            raise forms.ValidationError(_("One per row: %(l)s") % {"l": bad})
        for p in rows:
            if "'" in p or '"' in p or not p.startswith("/var/log/"):
                raise forms.ValidationError(_l("A path or pattern under /var/log/, without quotes: %(p)s") % {"p": p})
            if p not in out:
                out.append(p)
        return out

    def clean_after_rotation(self):
        v = (self.cleaned_data.get("after_rotation") or "").strip()
        if "\n" in v or "\r" in v:
            raise forms.ValidationError(_l("One line."))
        return v

    def clean_rsyslog_conf(self):
        return (self.cleaned_data.get("rsyslog_conf") or "").replace("\r\n", "\n")

    def clean_config_changes(self):
        out = []
        for line in (self.cleaned_data.get("config_changes") or "").splitlines():
            f = [x.strip() for x in line.split(SEP)]
            if not any(f):
                continue
            f += [""] * (3 - len(f))
            if not f[0] or not f[1]:
                raise forms.ValidationError(_l("Give the file and what to find: %(l)s") % {"l": line.strip()})
            out.append({"file": f[0], "find": f[1], "replace": f[2]})
        return out

    def clean(self):
        d = super().clean()
        if (
            d.get("config_changes")
            and d.get("config_then") in ("reload", "restart")
            and not (d.get("config_command") or "").strip()
        ):
            self.add_error("config_command", _l("Give the command."))
        return d


def config_text(s):
    return "\n".join(
        f" {SEP} ".join([c.get("file") or "", c.get("find") or "", c.get("replace") or ""]).rstrip(f" {SEP}")
        for c in (s.config or {}).get("changes") or []
    )


def initial(s):
    c = s.config or {}
    return {
        "name": s.name,
        "log_type": s.log_type,
        "days": s.days,
        "size_mb": s.size_mb,
        "also_rotated": "\n".join(s.also_rotated or []),
        "after_rotation": s.after_rotation,
        "rsyslog_conf": s.rsyslog_conf,
        "config_changes": config_text(s),
        "config_check": c.get("check") or "",
        "config_then": c.get("then") or "nothing",
        "config_command": c.get("command") or "",
        "notes": s.notes,
        "function": s.function_id,
    }


def function_choices():
    from .models import HostService

    return [("", "—")] + [
        (f.pk, f.key)
        for f in HostService.objects.filter(group=HostService.Group.FUNCTION, os="base-os").order_by("key")
    ]


def rsyslog_check(s):
    """The text read by rsyslogd -N1 with the portal's own files, where the portal's machine has rsyslogd (owner 09:06
    mockup 17: "checked with rsyslogd -N1 when saved"); "" when it accepts it or cannot be run here."""
    exe = shutil.which("rsyslogd")
    if not exe or s.is_vyos or not (s.rsyslog_conf or "").strip():
        return ""
    with tempfile.TemporaryDirectory(prefix="claugine-rsyslog-") as d:
        os.makedirs(os.path.join(d, "rsyslog.d"))
        conf = logconf.RSYSLOG_CONF.format(dir=os.path.join(d, "rsyslog.d")).replace(
            "$WorkDirectory /var/spool/rsyslog", f"$WorkDirectory {d}"
        )
        with open(os.path.join(d, "rsyslog.conf"), "w") as f:
            f.write(conf)
        rules = {"queue_mb": 100, "retry_seconds": 30}
        with open(os.path.join(d, "rsyslog.d", "00-claugine.conf"), "w") as f:
            f.write(logconf.common_conf({"host": "127.0.0.1", "port": 514, "proto": "tcp"}, rules))
        with open(os.path.join(d, "rsyslog.d", "40-service.conf"), "w") as f:
            f.write(logconf.service_conf(s) or "")
        try:
            r = subprocess.run(  # noqa: S603 - the portal's own check of a super admin's text, no shell
                [exe, "-N1", "-f", os.path.join(d, "rsyslog.conf")], capture_output=True, text=True, timeout=20
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        if r.returncode:
            lines = [ln for ln in (r.stderr + r.stdout).splitlines() if "error" in ln.lower()] or [
                (r.stderr + r.stdout).strip()
            ]
            return "; ".join(lines)[-500:].replace(os.path.join(d, "rsyslog.d", "40-service.conf"), "rsyslog.conf")
    return ""


def host_profile_sets():
    """commit-444: the profiles of each KVM host that runs core services (a management node: the host profile and its
    front end's, owner 09:33) - as logconf.logged_of merges them on that machine."""
    from .models import CoreServiceNode, Host, ServiceProfile

    default = ServiceProfile.of_type(ServiceProfile.HOST)
    by_host = {}
    for n in CoreServiceNode.objects.filter(host__isnull=False).select_related("service__profile").order_by("pk"):
        q = n.service.profile or ServiceProfile.of_type(n.service.stype)
        if q is not None and not q.is_vyos:
            by_host.setdefault(n.host_id, {})[q.pk] = q
    # T36 B4a: each host's own host service (kvm-mgmt ... kvm-shared-gpu), else the type's
    own = {
        h.pk: h.service for h in Host.objects.filter(pk__in=by_host, service__isnull=False).select_related("service")
    }
    out = []
    for hid, qs in by_host.items():
        host = own.get(hid) or default
        ps = [host] if host is not None else []
        out.append(ps + [q for q in qs.values() if host is None or q.pk != host.pk])
    return out


def check_sets(p, rows, swap=None):
    """commit-444: LogConfError when a machine made from the profile ``p`` with the logged ``rows`` could not get its
    files - the rows alone (common always), and every host that runs ``p`` with all of its profiles merged (the host
    profile and each core service's on it). ``swap``: an edited logged service, in place of its stored row."""
    common = LoggedService.objects.filter(name=LoggedService.COMMON).first()

    def logged(q):
        return [swap if swap is not None and x.pk == swap.pk else x for x in q.logged.all()]

    def merged(*lists):
        by = {}
        for rows_ in lists:
            for x in rows_:
                if not x.is_vyos:
                    by[x.pk or x.name] = x
        if common is not None and common.pk not in by and not any(x.name == common.name for x in by.values()):
            by[common.pk] = common
        return list(by.values())

    if p.is_vyos:
        return
    sets = [merged(rows)]
    for ps in host_profile_sets():  # commit-444: each host's whole set, with this profile's rows in place
        if p.pk and any(q.pk == p.pk for q in ps):
            sets.append(merged(*[rows if q.pk == p.pk else logged(q) for q in ps]))
    for found in sets:
        logconf.overlaps(found)


def validate(s):
    """LoggingError for a logged service a machine could not use, or that breaks a profile using it (two of its
    services rotating one same file; commit-444: with the host profile where hosts run that profile)."""
    try:
        logconf.check_text(s)
        logconf.check_config(s)
        if not s.is_vyos:
            used = list(s.profiles.all()) if s.pk else []
            for p in used:
                check_sets(p, [s if x.pk == s.pk else x for x in p.logged.all()], swap=s)
            if not used:
                logconf.overlaps([s])
    except logconf.LogConfError as exc:
        raise LoggingError(str(exc)) from exc
    bad = rsyslog_check(s)
    if bad:
        raise LoggingError(_("rsyslogd does not accept the rsyslog.conf: %(e)s") % {"e": bad})


def save(user, pk, d):
    with transaction.atomic():
        s = LoggedService.objects.select_for_update().filter(pk=pk).first()
        if s is None:
            raise LoggingError(_("No such logged service."))
        former = list((s.config or {}).get("former") or [])
        if not s.fixed:
            name = d.get("name") or s.name
            if LoggedService.objects.filter(name=name).exclude(pk=s.pk).exists():
                raise LoggingError(_("A logged service %(n)s exists already.") % {"n": name})
            if name != s.name and s.name not in former:  # commit-445: a machine's marker may still have the old name
                former.append(s.name)
            s.name = name
            s.log_type = d.get("log_type") or ""
        if not s.is_vyos:  # owner 09:06 Q4: VyOS rotates by size only; it has no rsyslog.conf (08:54)
            s.days = d.get("days") or s.days
            s.also_rotated = d.get("also_rotated") or []
            s.after_rotation = d.get("after_rotation") or ""
            s.rsyslog_conf = d.get("rsyslog_conf") or ""
            changes = d.get("config_changes") or []
            then = d.get("config_then") or "nothing"
            s.config = (
                {
                    "changes": changes,
                    "check": (d.get("config_check") or "").strip(),
                    "then": then,
                    "command": (d.get("config_command") or "").strip() if then != "nothing" else "",
                    **({"former": former} if former else {}),
                }
                if changes
                else {}
            )
        s.size_mb = d.get("size_mb") or s.size_mb
        s.notes = d.get("notes") or ""
        if "function" in d and not s.fixed:  # T36 B2: common and vyos are the OS's, never a function's
            from .models import HostService

            fn = d.get("function")
            s.function = (
                HostService.objects.filter(pk=fn, group=HostService.Group.FUNCTION, os="base-os").first()
                if fn
                else None
            )
            if fn and s.function is None:  # commit-488: the same check as a new one
                raise LoggingError(_("That function does not take logged services (a base-os function only)."))
        validate(s)
        s.save()
    audit(user, "logged_service_saved", None, name=s.name, id=s.pk)
    return s


def create(user, d):
    from tenants.models import PortalSettings

    from .models import HostService

    conf = PortalSettings.get()
    fn = d.get("function")
    function = None
    if fn is not None:  # commit-488: as Edit offers them - a base-os function; any other id (0 too) is refused
        function = HostService.objects.filter(pk=fn, group=HostService.Group.FUNCTION, os="base-os").first()
        if function is None:
            raise LoggingError(_("That function does not take logged services (a base-os function only)."))
    d = dict(d, function=function)
    with transaction.atomic():
        if LoggedService.objects.filter(name=d["name"]).exists():
            raise LoggingError(_("A logged service %(n)s exists already.") % {"n": d["name"]})
        s = LoggedService.objects.create(  # T36 (mockup m01): Days and Size start from the common defaults
            name=d["name"],
            kind=LoggedService.Kind.BASE_OS,
            log_type=d.get("log_type") or "",
            days=conf.log_kept_days or 5,
            size_mb=conf.log_size_mb or 200,
            function=d.get("function"),
        )
    audit(user, "logged_service_saved", None, name=s.name, id=s.pk, new=True)
    return s


def delete(user, pk):
    s = LoggedService.objects.filter(pk=pk).first()
    if s is None:
        raise LoggingError(_("No such logged service."))
    if s.fixed:
        raise LoggingError(_("%(n)s is predefined: it is never deleted.") % {"n": s.name})
    used = sorted(p.key for p in s.profiles.all())
    if used:
        raise LoggingError(_("%(n)s is used by %(p)s.") % {"n": s.name, "p": ", ".join(used)})
    name = s.name
    s.delete()
    audit(user, "logged_service_deleted", None, name=name, id=pk)


def decorate(s, rules):
    """What the details and the Edit dialog of a logged service show (Profiles → Logging, OS, Functions)."""
    s.used_by = sorted(s.profiles.all(), key=lambda p: p.key)
    s.shown_log_type = (
        ", ".join(logconf.log_types(s.rsyslog_conf)) if s.name == LoggedService.COMMON else logconf.log_type_of(s)
    )
    try:
        s.generated, s.generated_error = logconf.shown(s, rules), ""
    except logconf.LogConfError as exc:
        s.generated, s.generated_error = [], str(exc)
    s.vyos_max_size = max(1, int(s.size_mb or 0) // (logconf.VYOS_KEEP + 1))
    s.form = EditForm(initial=initial(s), prefix=f"lg{s.pk}")
    s.form.fields["function"].choices = function_choices()
    return s


def _rows():
    from tenants.models import PortalSettings

    rules = logconf.rules_of(PortalSettings.get())
    rows = sorted(
        LoggedService.objects.prefetch_related("profiles"), key=lambda s: (not s.fixed, s.name != "common", s.name)
    )
    return [decorate(s, rules) for s in rows]


def back_to(request, default):
    """T36 B2: back to the page a form came from (Profiles → OS or Functions) - only a page of this portal."""
    nxt = request.POST.get("next") or ""
    return redirect(nxt if nxt.startswith("/infrastructure/") and "//" not in nxt else default)


@staff_required(Role.SUPPORT, Role.RESOURCE_MANAGER)
def logging_view(request):
    """T36 B2 (rules v2 D): the logged services live under Profiles → OS (common, vyos) and Functions (the rest): this
    address leads there - by ?name= (a machine's Logging names its services) or ?sel=lg-<id>; ?all=1 keeps the list."""
    from tenants.models import PortalSettings

    name, sel = request.GET.get("name"), request.GET.get("sel") or ""
    if not request.GET.get("all"):
        s = LoggedService.objects.filter(name=name).first() if name else None
        if s is None and sel.startswith("lg-") and sel[3:].isdigit():
            s = LoggedService.objects.filter(pk=int(sel[3:])).first()
        return redirect(where(s))
    conf = PortalSettings.get()
    return render(
        request,
        "inventory/logging.html",
        {
            "rows": _rows(),
            "rules": conf,
            "settings_form": SettingsForm(initial={f: getattr(conf, f) for f in SettingsForm.base_fields}),
            "new_form": NewForm(prefix="new"),
            "can_edit": request.user.is_super_admin,
            "logcap": logconf.LOGCAP,
        },
    )


def where(s):
    """The page a logged service is shown on (T36 B2): its OS's (common, vyos), its function's, else Functions."""
    if s is None:
        return reverse("infra_os")
    if s.name == LoggedService.COMMON:
        return reverse("infra_os") + "?sel=os-base-os"
    if s.is_vyos:
        return reverse("infra_os") + "?sel=os-vyos"
    if s.function_id:
        return reverse("infra_functions") + f"?sel=fn-{s.function_id}"
    return reverse("infra_functions") + f"#lg-{s.pk}"


def _errors(request, form):
    for f, errs in form.errors.items():
        label = form.fields[f].label if f in form.fields else ""
        messages.error(request, f"{label}: {' '.join(errs)}" if label else " ".join(errs))


@require_POST
@staff_required(super_only=True)
def logging_settings_view(request):
    from tenants.models import PortalSettings

    form = SettingsForm(request.POST)
    if form.is_valid():
        conf = PortalSettings.get()
        values = {f: v for f, v in form.cleaned_data.items() if v is not None}
        for f, v in values.items():
            setattr(conf, f, v)
        conf.save(update_fields=list(values))
        audit(request.user, "logging_rules_saved", None, **values)
        messages.success(request, _("Saved: every machine gets it with its next logging."))
    else:
        _errors(request, form)
    return back_to(request, reverse("infra_functions"))


@require_POST
@staff_required(super_only=True)
def logged_new_view(request):
    form = NewForm(request.POST, prefix="new")
    if form.is_valid():
        try:
            s = create(request.user, form.cleaned_data)
            messages.success(request, _("Logged service added: give its rsyslog.conf in Edit."))
            return back_to(request, where(s))
        except LoggingError as exc:
            messages.error(request, str(exc))
    else:
        _errors(request, form)
    return back_to(request, reverse("infra_functions"))


@require_POST
@staff_required(super_only=True)
def logged_save_view(request, pk):
    form = EditForm(request.POST, prefix=f"lg{pk}")
    form.fields["function"].choices = function_choices()
    if form.is_valid():
        try:
            save(request.user, pk, form.cleaned_data)
            messages.success(request, _("Logged service saved: the machines get it with their next logging."))
        except LoggingError as exc:
            messages.error(request, str(exc))
    else:
        _errors(request, form)
    return back_to(request, where(LoggedService.objects.filter(pk=pk).first()))


@require_POST
@staff_required(super_only=True)
def logged_delete_view(request, pk):
    try:
        delete(request.user, pk)
        messages.success(request, _("Logged service deleted."))
    except LoggingError as exc:
        messages.error(request, str(exc))
        return back_to(request, where(LoggedService.objects.filter(pk=pk).first()))
    return back_to(request, reverse("infra_functions"))


LOGGING_DIRS = ("/etc/rsyslog", "/etc/logrotate", "/etc/systemd/system/logrotate.timer.d/")


def is_logging_file(path):
    """An rsyslog or logrotate file: written from the logged services, never a profile's config (owner 09:13)."""
    return any(str(path or "").startswith(d) for d in LOGGING_DIRS)


def profile_logged(p, chosen_ids):
    """The logged services a profile keeps (owner 09:13): a VyOS profile - vyos only; a Base OS profile - common and
    the base-os rows chosen."""
    if p.is_vyos:
        return list(LoggedService.objects.filter(name=LoggedService.VYOS, kind=LoggedService.Kind.VYOS))
    rows = list(LoggedService.objects.filter(kind=LoggedService.Kind.BASE_OS, pk__in=chosen_ids))
    common = LoggedService.objects.filter(name=LoggedService.COMMON).first()
    if common is not None and common not in rows:
        rows.insert(0, common)
    return rows
