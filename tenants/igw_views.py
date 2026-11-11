"""Tenant secrets (the OpenNebula admin's password, the igw login) and the igw's configurations, for the tenant's
admins (and the super admin). Every look at a secret or a configuration is logged."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import Role, audit

from . import igwconfig
from .deploy import DeployError
from .models import IgwApply, IgwConfig, Membership


def _tenant(request, pk):
    from .views import _tenant_for
    t = _tenant_for(request, pk)
    if t.membership_role(request.user) not in (Membership.Role.ORG_ADMIN, Membership.Role.TENANT_ADMIN) \
            and not request.user.is_super_admin:
        raise PermissionDenied
    return t


def _back(pk, anchor="access"):
    return redirect(f"{reverse('tenant_detail', args=[pk])}#{anchor}")


# --- the OpenNebula admin's password --------------------------------------------------------------------------

class KeepPasswordForm(forms.Form):
    password = forms.CharField(label=_l("Current password"), strip=False,
                               widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "off"}))


def check_one_login(tenant, password):
    """Log in to OpenNebula as the tenant's admin with this password (read only: one.user.info). Raises DeployError."""
    from adoption.collector import OneClient, OneError
    from inventory.models import Platform, PlatformControl
    p = Platform.objects.filter(key=(tenant.order or {}).get("platform")).first()
    if p is None:
        raise DeployError("the project has no platform")
    c = PlatformControl.objects.filter(key=p.fe_primary or p.key).first()
    url = (c.endpoint_url or c.xmlrpc_url) if c is not None else ""
    if not url:
        raise DeployError(f"{p.fe_primary or p.key}: no OpenNebula endpoint")
    try:
        OneClient(url, f"{tenant.one_admin_name}:{password}")._call("one.user.info", -1, False)
    except OneError as exc:
        raise DeployError(str(exc)) from exc


@login_required
def password_keep_view(request, pk):
    """Keep the OpenNebula admin's current password in the portal (an imported tenant's, say): checked by logging in
    to OpenNebula with it; OpenNebula is not changed."""
    t = _tenant(request, pk)
    form = KeepPasswordForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        pw = form.cleaned_data["password"]
        try:
            check_one_login(t, pw)
        except DeployError as exc:
            form.add_error("password", _("OpenNebula did not accept this password for %(u)s: %(e)s") % {
                "u": t.one_admin_name, "e": str(exc)[:200]})
        else:
            t.set_admin_password(pw)
            t.save(update_fields=["admin_password_encrypted"])
            audit(request.user, "tenant_password_kept", t)
            messages.success(request, _("The password is kept (checked with OpenNebula)."))
            return _back(pk)
    return render(request, "tenants/password_keep.html", {"tenant": t, "form": form})


# --- the igw login ---------------------------------------------------------------------------------------------

class IgwLoginForm(forms.Form):
    host = forms.CharField(label=_l("igw address"), max_length=100, required=False,
                           help_text=_l("IP address or name the igw is reached at over ssh. Empty: its main address."))
    port = forms.IntegerField(label=_l("ssh port"), min_value=1, max_value=65535)
    username = forms.RegexField(label=_l("User"), regex=r"^[a-z_][a-z0-9_.-]{0,31}$", max_length=32)
    password = forms.CharField(label=_l("Password"), required=False, strip=False,
                               widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
                               help_text=_l("Empty keeps the kept one."))


@login_required
def igw_login_view(request, pk):
    """The tenant's igw login kept in the portal (address, ssh port, user, password). The igw is not changed."""
    t = _tenant(request, pk)
    form = IgwLoginForm(request.POST or None, initial={"host": t.igw_host, "port": t.igw_ssh_port or 22,
                                                        "username": t.igw_username})
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        t.igw_host, t.igw_ssh_port, t.igw_username = d["host"].strip(), d["port"], d["username"]
        fields = ["igw_host", "igw_ssh_port", "igw_username"]
        if d["password"]:
            t.set_igw_password(d["password"])
            fields.append("igw_password_encrypted")
        t.save(update_fields=fields)
        audit(request.user, "igw_login_saved", t, host=t.igw_host, port=t.igw_ssh_port, user=t.igw_username,
              password_changed=bool(d["password"]))
        messages.success(request, _("The igw login is kept. The igw itself is not changed."))
        return _back(pk)
    return render(request, "tenants/igw_login.html", {"tenant": t, "form": form, "main_ip": igwconfig._main_ip(t),
                                                      "has_password": bool(t.igw_password_encrypted)})


# --- the igw's configurations ----------------------------------------------------------------------------------

class ConfigForm(forms.Form):
    name = forms.CharField(label=_l("Name"), max_length=120)
    text = forms.CharField(label=_l("Configuration"), strip=False,
                           widget=forms.Textarea(attrs={"rows": 18, "class": "mono", "spellcheck": "false"}),
                           help_text=_l("The set commands, as `show configuration commands` prints them."))

    def clean_text(self):
        text = self.cleaned_data["text"]
        if not any(x.strip().startswith("set ") for x in text.splitlines()):
            raise forms.ValidationError(_l("No set commands."))
        return text


class ApplyForm(forms.Form):
    method = forms.ChoiceField(label=_l("How"), choices=IgwApply.Method.choices, initial=IgwApply.Method.SSH,
                               widget=forms.RadioSelect)
    timeout_s = forms.IntegerField(label=_l("Roll back unless confirmed within, seconds"), initial=60,
                                   min_value=igwconfig.MIN_TIMEOUT_S, max_value=igwconfig.MAX_TIMEOUT_S,
                                   help_text=_l("Over ssh only. VyOS counts whole minutes: the time is rounded up (60 s = 1 minute)."))


def _to_dialog(pk, which="configs"):
    return redirect(f"{reverse('tenant_detail', args=[pk])}?igw={which}#connectivity")


@login_required
def igw_view(request, pk):
    """Correction 14: the igw's dialogs on the tenant page (Internet → Details, Configs); this view takes their
    actions and goes back to the open dialog."""
    from .deploy import igw_type_of
    t = _tenant(request, pk)
    kind = igw_type_of(t) if t.approval == "approved" else ""
    if not kind:
        messages.info(request, _("The project has no igw."))
        return _back(pk, "connectivity")
    if request.method != "POST":
        return _to_dialog(pk)
    vyos = igwconfig.available(t)
    if vyos:
        igwconfig.expire(t)
    action = request.POST.get("action")
    back = "gw" if action in ("password", "login", "history", "size") else "configs"
    if request.POST.get("dialog") in ("configs", "gw"):  # back to the dialog the form was in
        back = request.POST.get("dialog")
    if not vyos and action not in ("login", "password", "history", "size"):
        raise PermissionDenied
    if action == "size" and not (request.user.is_staff_member and (request.user.is_super_admin or request.user.has_role(Role.SUPPORT)
                                                                  or request.user.has_role(Role.RESOURCE_MANAGER))):
        raise PermissionDenied
    try:
        if action == "networks":  # correction 12: the subnets connected to the igw
            from . import igwnets
            note = igwnets.apply(t, request.POST.getlist("net"), request.user)
            messages.success(request, _("Networks of the igw: %(n)s.") % {"n": note})
        elif action == "read_current":
            c = igwconfig.read_current(t, request.user)
            audit(request.user, "igw_config_read", t, config="current", lines=c.lines)
            messages.success(request, _("The current configuration is read (%(l)s lines).") % {"l": c.lines})
        elif action == "save_config":
            text = request.POST.get("text") or ""
            if not any(x.strip().startswith("set ") for x in text.splitlines()):
                raise DeployError(_("No set commands."))
            base = IgwConfig.objects.filter(pk=request.POST.get("based_on") or 0, tenant=t).first()
            c = igwconfig.save_stored(t, text, request.POST.get("name", "").strip(), request.user, based_on=base)
            audit(request.user, "igw_config_saved", t, config=c.name, lines=c.lines, based_on=base.name if base else "")
            messages.success(request, _("Stored as %(n)s.") % {"n": c.name})
        elif action == "delete_config":
            c = get_object_or_404(IgwConfig, pk=request.POST.get("config"), tenant=t, current=False)
            audit(request.user, "igw_config_deleted", t, config=c.name)
            c.delete()
            messages.success(request, _("Deleted."))
        elif action == "apply":
            c = get_object_or_404(IgwConfig, pk=request.POST.get("config"), tenant=t)
            try:
                timeout = int(request.POST.get("timeout_s") or 60)
            except ValueError:
                timeout = 60
            a = igwconfig.start(t, c, IgwApply.Method.SSH, timeout, request.user)
            messages.success(request, _("Applying %(n)s: %(r)s") % {"n": c.name, "r": a.result or a.get_state_display()})
        elif action == "commands":
            text = request.POST.get("commands") or ""
            rollback = request.POST.get("rollback") == "on"
            try:
                timeout = int(request.POST.get("timeout_s") or 60) if rollback else 0
            except ValueError:
                timeout = 60
            a = igwconfig.run_commands(t, text, timeout, request.user)
            (messages.error if a.state == a.State.FAILED else messages.success)(request, a.result)
        elif action == "confirm":
            a = get_object_or_404(IgwApply, pk=request.POST.get("apply"), tenant=t)
            igwconfig.confirm(a, request.user)
            messages.success(request, _("Confirmed and saved on the igw."))
        elif action == "password":
            pw = request.POST.get("password") or ""
            from . import passwords
            bad = passwords.problems(pw, passwords.words_for(t.name or "", t.organization, request.user,
                                                             extra=[t.igw_username]))
            if not pw or bad:
                raise DeployError(_("The password needs: %(what)s.") % {"what": "; ".join(bad) or _("a value")})
            if request.POST.get("on_igw") == "on":
                igwconfig.set_password(t, pw, request.user)
            t.set_igw_password(pw, by=request.user)
            t.save(update_fields=["igw_password_encrypted"])
            audit(request.user, "igw_password_changed", t, on_igw=request.POST.get("on_igw") == "on")
            messages.success(request, _("The igw password is changed."))
        elif action == "login":  # correction 14: the igw user can change until the igw is made
            import re as _re
            u = (request.POST.get("username") or "").strip()
            if (t.igw_vm or {}).get("id") or any((d.resources or {}).get("igw_id") for d in t.deployments.all()):
                raise DeployError(_("The igw is made: its user cannot change any more."))
            if not _re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", u):
                raise DeployError(_("1-32 lowercase letters, digits, - and _, starting with a letter."))
            t.igw_username = u
            t.save(update_fields=["igw_username"])
            audit(request.user, "igw_login_saved", t, user=u)
            messages.success(request, _("Saved."))
        elif action == "size":  # owner 2026-10-29 18:58: the igw's vCPU and RAM
            from . import igwsize
            try:
                cpu, ram = int(request.POST.get("cpu") or 0), int(request.POST.get("ram_gb") or 0)
            except ValueError:
                cpu = ram = 0
            messages.success(request, igwsize.resize(t, cpu, ram, request.POST.get("restart") == "on", request.user))
        elif action == "history":
            request.session[f"igw_history_{t.pk}"] = True
            audit(request.user, "igw_password_history_shown", t)
        else:
            raise PermissionDenied
    except DeployError as exc:
        messages.error(request, str(exc))
    return _to_dialog(pk, back)


@login_required
def igw_config_view(request, pk, cid=None):
    """A stored configuration, read only (in a dialog: ?partial=1): shown to the tenant's admins, logged."""
    t = _tenant(request, pk)
    c = get_object_or_404(IgwConfig, pk=cid, tenant=t) if cid else None
    if c is None:
        return _to_dialog(pk)
    audit(request.user, "igw_config_viewed", t, config=c.name)
    return render(request, "tenants/_igw_config_show.html", {"tenant": t, "config": c, "text": c.get_text()})


@login_required
def igw_download_view(request, pk):
    """The igw's running configuration as a file (before changing the igw type, say)."""
    from django.http import HttpResponse
    t = _tenant(request, pk)
    if not igwconfig.available(t):
        raise PermissionDenied
    try:
        text = igwconfig.fetch(t)
    except DeployError as exc:
        messages.error(request, str(exc))
        return _to_dialog(pk, "type")
    audit(request.user, "igw_config_downloaded", t)
    resp = HttpResponse(text, content_type="text/plain; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{t.group_name or t.name}-igw-{timezone.localtime().strftime("%Y%m%d-%H%M")}.txt"'
    return resp


def dialog_context(request, tenant):
    """What the Internet dialogs need (tenant page): stored configurations, the current one, recent changes."""
    if not igwconfig.available(tenant):
        return {}
    igwconfig.expire(tenant)
    configs = list(tenant.igw_configs.all()[:50])
    applies = list(tenant.igw_applies.select_related("config")[:10])
    last_cmd = next((a for a in applies if a.method == IgwApply.Method.COMMANDS), None)
    stored = [c for c in configs if not c.current]
    acting = next((a.config_id for a in applies if a.config_id and a.state in (IgwApply.State.CONFIRMED, IgwApply.State.DONE)), None)
    for c in stored:
        c.acting = c.pk == acting
    ctx = {"igw_vyos": True, "igw_stored": stored, "igw_current": next((c for c in configs if c.current), None),
           "igw_full": len(stored) >= IgwConfig.MAX_STORED, "igw_max": IgwConfig.MAX_STORED, "igw_applies": applies,
           "igw_waiting": next((a for a in applies if a.state == IgwApply.State.WAITING), None),
           "igw_last_commands": last_cmd.get_commands() if last_cmd else "",
           "igw_open": request.GET.get("igw", "")}
    if ctx["igw_current"] is not None and request.GET.get("igw") == "configs":
        ctx["igw_current_text"] = ctx["igw_current"].get_text()
    if request.GET.get("igw") == "configs":
        from . import igwnets
        try:
            ctx["igw_nets"] = igwnets.state(tenant)
        except Exception as exc:  # noqa: BLE001 - shown in the dialog
            ctx["igw_nets_error"] = str(exc)[:300]
    if request.session.pop(f"igw_history_{tenant.pk}", False):
        ctx["igw_history"] = [(h.created_at, h.created_by, h.get_password()) for h in tenant.igw_passwords.all()[:20]]
    return ctx
