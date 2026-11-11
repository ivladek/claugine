"""Infrastructure → Templates: sources of network function configurations, with a preview."""
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
from .models import ConfigTemplate, Site


class TemplateForm(forms.ModelForm):
    class Meta:
        model = ConfigTemplate
        fields = ["name", "os", "os_version", "description", "body"]
        widgets = {"body": forms.Textarea(attrs={"rows": 24, "class": "mono", "spellcheck": "false"}),
                   "description": forms.Textarea(attrs={"rows": 2})}

    def clean_body(self):
        body = self.cleaned_data["body"].replace("\r\n", "\n")
        try:
            nfconfig.ENGINE.from_string(body)
        except Exception as exc:  # noqa: BLE001 - template syntax shown to the editor
            raise forms.ValidationError(_l("Template error: %(e)s") % {"e": exc})
        return body


def _preview(request, tpl, reveal=False):
    """Rendered configuration for the preview chosen in the query: a tenant, or a site and RR index."""
    from tenants.models import Tenant
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
        t.users = template_users(t.role)
        t.variables = nfconfig.VARIABLES.get(t.role, "")
        t.rendered, t.error = (_preview(request, t) if t.role == pick and t.scope != "image" else (None, ""))
    return render(request, "inventory/templates.html", {
        "templates": templates, "can_edit": can_edit, "pick": pick, "common_vars": nfconfig.VARIABLES["common"],
        "tenants": Tenant.objects.filter(approval="approved").exclude(group_name="").order_by("group_name"),
        "sites": Site.objects.all(), "q": request.GET,
        "missing_roles": [(v, l) for v, l in ConfigTemplate.Role.choices if v not in {t.role for t in templates}],
        "new_form": TemplateForm(prefix="new") if can_edit else None,
    })


def template_users(role):
    """What needs the template of a role now (a deleted template stops building those configurations)."""
    from tenants.models import L2Channel, Lifecycle, PrefixAssignment, Tenant
    live = Tenant.objects.exclude(status__in=["saved", "archived", "deleting"]).exclude(one_group_id=None)
    if role in ("egw", "igw"):
        n = sum(1 for t in live if "inet" in ((t.order or {}).get("connections") or []))
        return [_("%(n)s deployed projects with the internet") % {"n": n}] if n else []
    if role == "cgw":
        n = PrefixAssignment.active().filter(kind="corpnet", tenant__in=live).values("tenant").distinct().count()
        return [_("%(n)s deployed projects with the corporate network") % {"n": n}] if n else []
    if role in ("l2-a", "l2-b", "l2-br"):
        n = L2Channel.objects.exclude(state=L2Channel.State.REMOVED).filter(
            mode="bridge" if role == "l2-br" else "tunnel").count()
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
    """A template for a role that has none."""
    role = request.POST.get("role", "")
    if role not in ConfigTemplate.Role.values or ConfigTemplate.objects.filter(role=role).exists():
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
        messages.error(request, _("%(n)s is used (%(u)s) and cannot be deleted.") % {"n": tpl.name, "u": "; ".join(users)})
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
    audit(request.user, "config_revealed", tpl, role=tpl.role, tenant=request.POST.get("tenant", ""),
          site=request.POST.get("site", ""))
    return render(request, "inventory/config_view.html", {"title": tpl.name, "text": text, "error": error,
                                                          "back": f"{reverse('infra_templates')}?sel=tpl-{pk}",
                                                          "revealed": True})


@staff_required(Role.SUPPORT, Role.RESOURCE_MANAGER)
def tenant_config_view(request, pk, role):
    """Configuration of one network function of a tenant (masked; revealed for the super admin on POST)."""
    from tenants.models import Tenant
    t = get_object_or_404(Tenant, pk=pk)
    reveal = request.method == "POST" and request.user.is_super_admin
    channel = None
    if role.startswith("l2"):
        channel = t.l2_channels.filter(pk=request.GET.get("channel") or request.POST.get("channel") or 0).first() \
            or t.l2_channels.first()
    try:
        text, error = nfconfig.build(role, tenant=t, reveal=reveal, channel=channel), ""
    except nfconfig.BuildError as exc:
        text, error = None, str(exc)
    if reveal:
        audit(request.user, "config_revealed", t, role=role)
    return render(request, "inventory/config_view.html", {
        "title": f"{t.group_name} · {role}", "text": text, "error": error, "revealed": reveal,
        "back": reverse("tenant_detail", args=[pk]) + "#deployment", "can_reveal": request.user.is_super_admin,
        "channel": channel, "script": nfconfig.init_script(text, f"{t.group_name} {role}") if text else ""})
