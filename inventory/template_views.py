"""Profiles → Templates (owner 2026-11-21 13:10, T34; was Infrastructure → Templates): sources of network function
configurations, with a preview; and (owner 2026-11-22 16:59-17:46, T34 S1) the templates of the service profiles'
config files and installations - scope "service", each with its own key and kind, no preview, used by the service
profiles only."""

import re

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from accounts.models import Role, audit
from accounts.utils import staff_required

from . import nfconfig
from .models import ConfigTemplate, ServiceProfile, Site

SERVICE_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
# commit-378: the target of a template follows its kind - network roles VyOS or FRR (as before), the service
# templates Ubuntu (the service images are Ubuntu)
NETWORK_OS = ("vyos", "frr")
SERVICE_OS = ("ubuntu",)


def allowed_os(kind):
    return SERVICE_OS if kind in ConfigTemplate.SERVICE_KINDS else NETWORK_OS


class TemplateForm(forms.ModelForm):
    class Meta:
        model = ConfigTemplate
        fields = ["name", "kind", "os", "os_version", "description", "body"]
        widgets = {
            "body": forms.Textarea(attrs={"rows": 24, "class": "mono", "spellcheck": "false"}),
            "description": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *a, service=None, **kw):
        """service: True - a service template (its kind among config file / installation steps / script), False - a
        network role's (no kind to choose), None - the new-template form (any kind: the role or key decides)."""
        super().__init__(*a, **kw)
        if service is None and self.instance.pk:
            service = self.instance.kind in ConfigTemplate.SERVICE_KINDS
        f = self.fields["kind"]
        os_f = self.fields["os"]
        if service is False:
            del self.fields["kind"]
        elif service:
            f.choices = [(v, ln) for v, ln in ConfigTemplate.Kind.choices if v in ConfigTemplate.SERVICE_KINDS]
        else:
            f.required = False  # not posted: a network role's template, as before
            # commit-378: a new template's target defaults by its kind (Ubuntu for a service template)
            os_f.required = False
            os_f.choices = [("", _l("by the kind"))] + list(ConfigTemplate.Os.choices)
            os_f.initial = ""
        if service is not None:
            ok = allowed_os("config" if service else "")
            os_f.choices = [(v, ln) for v, ln in ConfigTemplate.Os.choices if v in ok]

    def clean_kind(self):
        return self.cleaned_data.get("kind") or ConfigTemplate.Kind.NETWORK

    def clean(self):
        d = super().clean()
        kind = d.get("kind") or (self.instance.kind if self.instance.pk else ConfigTemplate.Kind.NETWORK)
        ok = allowed_os(kind)
        if not d.get("os"):
            d["os"] = ok[0]
            self.instance.os = ok[0]
        elif d["os"] not in ok:
            self.add_error(
                "os",
                _l("The target of this kind of template: %(o)s.")
                % {"o": ", ".join(dict(ConfigTemplate.Os.choices)[x] for x in ok)},
            )
        body = d.get("body")
        if body is not None:
            err = body_error(kind, body, self.instance.role or "template")
            if err:
                self.add_error("body", err)
        return d

    def clean_body(self):
        return self.cleaned_data["body"].replace("\r\n", "\n")


def body_error(kind, body, where):
    """commit-383: a body checked as it is used - a service profile's installation steps parsed and a service template
    rendered as the deployment does (recipes, with the example values), a network template with nfconfig's engine;
    "" when it is good."""
    from . import recipes

    try:
        if kind == ConfigTemplate.Kind.STEPS:
            recipes.parse_steps(body)
        elif kind in ConfigTemplate.SERVICE_KINDS:
            recipes._template(body, where)
        else:
            nfconfig.ENGINE.from_string(body)
    except Exception as exc:  # noqa: BLE001 - shown to the editor
        return _l("Template error: %(e)s") % {"e": exc}
    return ""


def _preview(request, tpl, reveal=False):
    """Rendered configuration for the preview chosen in the query: a tenant, or a site and RR index."""
    from tenants.models import Tenant

    if tpl.scope not in ("tenant", "site"):  # commit-376: the image scripts and the service templates
        return None, _("This template has no preview.")
    try:
        if tpl.scope == "tenant":
            pk = request.GET.get("tenant") or request.POST.get("tenant")
            if not pk:
                return None, ""
            t = get_object_or_404(Tenant, pk=pk)
            channel = None
            if tpl.role.startswith("l2"):
                channel = t.l2_channels.exclude(state="removed").first()
                if channel is None:
                    return None, _("This project has no L2 channel.")
            return nfconfig.build(tpl.role, tenant=t, reveal=reveal, channel=channel), ""
        site = request.GET.get("site") or request.POST.get("site")
        if not site:
            return None, ""
        index = int(request.GET.get("index") or request.POST.get("index") or 0)
        return nfconfig.build(tpl.role, site=site, index=index, reveal=reveal), ""
    except nfconfig.BuildError as exc:
        return None, str(exc)


@staff_required(Role.RESOURCE_MANAGER, Role.SUPPORT)
def templates_view(request):
    from tenants.models import Tenant

    templates = list(ConfigTemplate.objects.select_related("updated_by"))
    can_edit = request.user.is_super_admin
    pick = request.GET.get("preview", "")
    for t in templates:
        t.form = TemplateForm(instance=t, prefix=f"t{t.pk}") if can_edit else None
        t.profiles = profile_rows(t.role)  # owner 2026-11-22 17:08 (T34): the service profiles using it, last card
        t.users = template_users(t.role, t)
        t.variables = nfconfig.VARIABLES.get(t.role, "")
        shown = t.role == pick and t.scope not in ("image", "service")
        t.rendered, t.error = _preview(request, t) if shown else (None, "")
    return render(
        request,
        "inventory/templates.html",
        {
            "templates": templates,
            "can_edit": can_edit,
            "pick": pick,
            "common_vars": nfconfig.VARIABLES["common"],
            "tenants": Tenant.objects.filter(approval="approved").exclude(group_name="").order_by("group_name"),
            "sites": Site.objects.all(),
            "q": request.GET,
            "missing_roles": [(v, ln) for v, ln in ConfigTemplate.Role.choices if v not in {t.role for t in templates}],
            "new_form": TemplateForm(prefix="new") if can_edit else None,
            "service_kinds": [(v, ln) for v, ln in ConfigTemplate.Kind.choices if v in ConfigTemplate.SERVICE_KINDS],
        },
    )


def profile_rows(role):
    """The service profiles using the template, each with its type of service and instances (the sites' services of
    its type, or the KVM hosts)."""
    from .models import CoreService, Host

    rows = []
    for p in ServiceProfile.using(role):
        if p.stype == ServiceProfile.HOST:
            n = Host.objects.exclude(state="retired").count()
            inst = [_("%(n)s KVM hosts") % {"n": n}] if n else []
        else:
            svcs = CoreService.objects.filter(stype=p.stype).select_related("site").order_by("site__code", "name")
            inst = [f"{x.site.code} {x.name}" for x in svcs]
        rows.append({"profile": p, "instances": ", ".join(inst)})
    return rows


def template_users(role, tpl=None):
    """What needs the template of a role now (a deleted template stops building those configurations). A service
    template is used by the service profiles only (owner 2026-11-22 17:08, T34)."""
    from .models import CoreService

    profiles = [_("service profile %(p)s") % {"p": p.key} for p in ServiceProfile.using(role)]
    if tpl is None:
        tpl = ConfigTemplate.objects.filter(role=role).first()
    # commit-377: a site service made from the template (deleting it would clear the service's template)
    services = (
        [
            _("site service %(s)s") % {"s": f"{x.site.code} {x.name}"}
            for x in CoreService.objects.filter(template=tpl).select_related("site").order_by("site__code", "name")
        ]
        if tpl is not None
        else []
    )
    if tpl is not None and tpl.scope == "service":
        return profiles + services
    return _network_users(role) + profiles + services


def _network_users(role):
    """What builds its configuration from a network role's template."""
    from tenants.models import L2Channel, PrefixAssignment, Tenant

    live = Tenant.objects.exclude(status__in=["saved", "archived", "deleting"]).exclude(one_group_id=None)
    if role in ("egw", "igw"):
        n = sum(1 for t in live if "inet" in ((t.order or {}).get("connections") or []))
        return [_("%(n)s deployed projects with the internet") % {"n": n}] if n else []
    if role == "cgw":
        n = PrefixAssignment.active().filter(kind="corpnet", tenant__in=live).values("tenant").distinct().count()
        return [_("%(n)s deployed projects with the corporate network") % {"n": n}] if n else []
    if role in ("l2-a", "l2-b", "l2-br"):
        n = (
            L2Channel.objects.exclude(state=L2Channel.State.REMOVED)
            .filter(mode="bridge" if role == "l2-br" else "tunnel")
            .count()
        )
        return [_("%(n)s L2 channels") % {"n": n}] if n else []
    if role in ConfigTemplate.IMAGE_ROLES:  # the image is built from them by hand for now (the rebuild comes later)
        from .models import ServiceImage

        n = ServiceImage.objects.filter(role=ServiceImage.VYOS).count()
        return [_("%(n)s VyOS service images") % {"n": n}] if n else []
    sites = list(Site.objects.values_list("code", flat=True))
    return [_("sites %(s)s") % {"s": ", ".join(sites)}] if sites else []


@require_POST
@staff_required(super_only=True)
def template_new_view(request):
    """A template for a role that has none, or a service template with its own key (T34 S1)."""
    role = request.POST.get("role", "")
    kind = request.POST.get("new-kind", ConfigTemplate.Kind.NETWORK)
    if kind in ConfigTemplate.SERVICE_KINDS:
        role = request.POST.get("key", "").strip().lower()
        if not SERVICE_KEY.match(role) or role in ConfigTemplate.Role.values:
            messages.error(
                request,
                _("The key of a service template: 2-40 lowercase letters, digits and hyphens, not a network role."),
            )
            return redirect("infra_templates")
        if ConfigTemplate.objects.filter(role=role).exists():
            messages.error(request, _("A template with the key %(k)s exists.") % {"k": role})
            return redirect("infra_templates")
    elif kind != ConfigTemplate.Kind.NETWORK or (
        role not in ConfigTemplate.Role.values or ConfigTemplate.objects.filter(role=role).exists()
    ):
        messages.error(request, _("Choose a role that has no template yet."))
        return redirect("infra_templates")
    form = TemplateForm(request.POST, prefix="new")
    if not form.is_valid():
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
        return redirect("infra_templates")
    t = form.save(commit=False)
    t.role, t.updated_by = role, request.user
    t.save()
    audit(request.user, "config_template_saved", t, role=t.role, created=True)
    messages.success(request, _("Template %(n)s saved.") % {"n": t.name})
    return redirect(f"{reverse('infra_templates')}?sel=tpl-{t.pk}")


@require_POST
@staff_required(super_only=True)
def template_delete_view(request, pk):
    tpl = get_object_or_404(ConfigTemplate, pk=pk)
    users = template_users(tpl.role)
    if users:
        messages.error(
            request, _("%(n)s is used (%(u)s) and cannot be deleted.") % {"n": tpl.name, "u": "; ".join(users)}
        )
        return redirect(f"{reverse('infra_templates')}?sel=tpl-{pk}")
    audit(request.user, "config_template_deleted", tpl, role=tpl.role, name=tpl.name)
    tpl.delete()
    messages.success(request, _("Template deleted."))
    return redirect("infra_templates")


@require_POST
@staff_required(super_only=True)
def template_save_view(request, pk):
    tpl = get_object_or_404(ConfigTemplate, pk=pk)
    form = TemplateForm(request.POST, instance=tpl, prefix=f"t{pk}")
    if form.is_valid():
        t = form.save(commit=False)
        t.updated_by = request.user
        t.save()
        audit(request.user, "config_template_saved", t, role=t.role)
        messages.success(request, _("Template %(n)s saved.") % {"n": t.name})
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
    return redirect(f"{reverse('infra_templates')}?sel=tpl-{pk}")


@require_POST
@staff_required(super_only=True)
def template_reveal_view(request, pk):
    """The preview with secrets: for the super admin, logged."""
    tpl = get_object_or_404(ConfigTemplate, pk=pk)
    text, error = _preview(request, tpl, reveal=True)
    audit(
        request.user,
        "config_revealed",
        tpl,
        role=tpl.role,
        tenant=request.POST.get("tenant", ""),
        site=request.POST.get("site", ""),
    )
    return render(
        request,
        "inventory/config_view.html",
        {
            "title": tpl.name,
            "text": text,
            "error": error,
            "back": f"{reverse('infra_templates')}?sel=tpl-{pk}",
            "revealed": True,
        },
    )


@staff_required(Role.SUPPORT, Role.RESOURCE_MANAGER)
def tenant_config_view(request, pk, role):
    """Configuration of one network function of a tenant (masked; revealed for the super admin on POST)."""
    from tenants.models import Tenant

    t = get_object_or_404(Tenant, pk=pk)
    reveal = request.method == "POST" and request.user.is_super_admin
    channel = None
    if role.startswith("l2"):
        channel = (
            t.l2_channels.filter(pk=request.GET.get("channel") or request.POST.get("channel") or 0).first()
            or t.l2_channels.first()
        )
    try:
        text, error = nfconfig.build(role, tenant=t, reveal=reveal, channel=channel), ""
    except nfconfig.BuildError as exc:
        text, error = None, str(exc)
    if reveal:
        audit(request.user, "config_revealed", t, role=role)
    return render(
        request,
        "inventory/config_view.html",
        {
            "title": f"{t.group_name} · {role}",
            "text": text,
            "error": error,
            "revealed": reveal,
            "back": reverse("tenant_detail", args=[pk]) + "#deployment",
            "can_reveal": request.user.is_super_admin,
            "channel": channel,
            "script": nfconfig.init_script(text, f"{t.group_name} {role}") if text else "",
        },
    )
