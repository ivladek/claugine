"""Provisioning an approved tenant (correction 13, 2026-10-29).

The flow: an organization's user (org portal) or a resource manager (cloud portal) orders resources → the resource
manager (and the sales rep, when Settings ask for it) approve → the organization's admins, the tenant's admins and the
support engineer see the Provision button. Nothing is made on the platform before somebody presses it. The dialog asks:

- the OpenNebula admin: <org>-<user name> (user name default: the tenant name) and its password (a generated default);
  tenants that already have an admin keep its name;
- with the internet: the igw type; for a VyOS igw its user, password and optionally a key from the organization's keys;
- who gets the credentials: any of the organization's users (activated or not).

The deployment runs as before (tenants.deploy); when it is done the chosen people get the credentials by e-mail.
"""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from accounts.models import Role, User, audit

from . import naming, passwords
from .forms import clean_ssh_key
from .models import Approval, Connectivity, Membership, Tenant, TenantSshKey

IGW_USER_RE = r"^[a-z_][a-z0-9_-]{0,31}$"


def provisioned(tenant):
    """Made on the platform already: a finished deployment, or an OpenNebula group not made by a portal provisioning.
    A provisioning that failed may be run again: it keeps what it made and goes on (its group is linked by then)."""
    deps = tenant.deployments.exclude(state="plan")
    if deps.filter(state="done").exists():
        return True
    if deps.exists():
        return False
    return tenant.one_group_id is not None


def failed_provisioning(tenant):
    """The last provisioning when it failed and nothing finished since, else None."""
    last = tenant.deployments.exclude(state="plan").order_by("-pk").first()
    return last if last is not None and last.state == "failed" else None


def can_provision(tenant, user):
    """The organization's admins, the tenant's admins, the support engineer and the super admin, once it is approved."""
    if not user.is_authenticated or tenant.approval != Approval.APPROVED or tenant.is_deleted or provisioned(tenant):
        return False
    if tenant.deployments.filter(state="running").exists():
        return False
    if user.is_super_admin:
        return True
    if user.is_staff_member:
        return tenant.can_approve_as(user, Role.SUPPORT) or user.has_role(Role.SUPPORT)
    return tenant.membership_role(user) in (Membership.Role.ORG_ADMIN, Membership.Role.TENANT_ADMIN)


def org_keys(tenant):
    """[(value, label, key)] the keys of the organization: its tenants' keys and its default key."""
    org = tenant.organization
    out, seen = [], set()
    if org is None:
        return out
    if org.default_ssh_key:
        out.append(("default", _("default key of the organization"), org.default_ssh_key))
        seen.add(" ".join(org.default_ssh_key.split()[:2]))
    for k in TenantSshKey.objects.filter(tenant__organization=org).select_related("tenant").order_by("name"):
        sig = " ".join(k.key.split()[:2])
        if sig in seen:
            continue
        seen.add(sig)
        out.append((str(k.pk), f"{k.name} ({k.tenant.name or k.tenant.pk})", k.key))
    return out


def org_people(tenant):
    org = tenant.organization
    return list(User.objects.filter(organization=org).order_by("-is_org_admin", "full_name", "email")) if org else []


def has_inet(tenant):
    from .ordering import connections

    return "inet" in connections(tenant.order or {})


def admin_fixed(tenant):
    """The admin's name is fixed: chosen before, or a group of the earlier naming (_t_…, tenant-…)."""
    g = tenant.group_name or ""
    return bool(tenant.one_admin_user) or g.startswith((naming.LEGACY_PREFIX, "tenant-"))


class ProvisionForm(forms.Form):
    admin_user = forms.CharField(label=_l("OpenNebula admin user name"), max_length=32, required=False)
    admin_password = forms.CharField(label=_l("OpenNebula admin password"), max_length=128)
    igw_type = forms.ChoiceField(
        label=_l("igw"), widget=forms.RadioSelect, choices=Connectivity.Gateway.choices, required=False, initial="vyos"
    )
    igw_username = forms.RegexField(label=_l("igw user"), regex=IGW_USER_RE, required=False)
    igw_password = forms.CharField(label=_l("igw password"), max_length=128, required=False)
    igw_key = forms.ChoiceField(label=_l("igw ssh key (optional)"), required=False)
    igw_cpu = forms.IntegerField(label=_l("igw vCPU"), min_value=1, max_value=16, initial=1, required=False)
    igw_ram_gb = forms.IntegerField(label=_l("igw RAM, GB"), min_value=1, max_value=64, initial=1, required=False)
    tenant_admin = forms.ChoiceField(
        label=_l("Project admin"), help_text=_l("Gets the credentials by e-mail when the project is ready.")
    )

    def __init__(self, *args, tenant=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tenant, self.user = tenant, user
        from accounts.policy import PolicyPasswordInput

        for f in ("admin_password", "igw_password"):
            self.fields[f].widget = PolicyPasswordInput(
                attrs={"class": "mono", "autocomplete": "new-password"}, render_value=True
            )
        org = tenant.organization
        self.org_short = org.short_name if org else ""
        self.fixed_admin = admin_fixed(tenant)
        if self.fixed_admin:
            self.fields["admin_user"].disabled = True
            self.initial["admin_user"] = tenant.one_admin_name
            self.fields["admin_user"].help_text = _("Kept: the project's admin has this name already.")
        else:  # owner 2026-10-31: by the rule <org>-<project>-adm, not chosen
            self.fields["admin_user"].disabled = True
            self.initial["admin_user"] = naming.admin_name(self.org_short, tenant.name or "")
            self.fields["admin_user"].help_text = _("By the rule <organization>-<project>-adm.")
        self.initial.setdefault("admin_password", passwords.generate())
        self.initial.setdefault("igw_password", passwords.generate())
        self.initial.setdefault(
            "igw_username", tenant.igw_username or f"{(tenant.name or 'tenant').replace('-', '')}-igw-admin"[:32]
        )
        self.keys = org_keys(tenant)
        self.fields["igw_key"].choices = [("", "—")] + [(v, label) for v, label, _k in self.keys]
        from .admins import choices as admin_choices
        from .admins import proposed

        self.people = org_people(tenant)
        self.fields["tenant_admin"].choices = [("", "—")] + admin_choices(org)
        if not self.is_bound:
            pick = tenant.tenant_admin or proposed(tenant)[0]
            if pick is not None:
                self.initial.setdefault("tenant_admin", str(pick.pk))
        self.inet = has_inet(tenant)
        if not self.inet:
            for f in ("igw_type", "igw_username", "igw_password", "igw_key", "igw_cpu", "igw_ram_gb"):
                del self.fields[f]

    def clean_admin_user(self):
        if self.fixed_admin:
            return self.tenant.one_admin_name
        name = naming.admin_name(self.org_short, self.tenant.name or "")
        if len(name) > 64:
            raise forms.ValidationError(_("Too long."))
        others = Tenant.objects.exclude(pk=self.tenant.pk)
        if others.filter(one_admin_user=name).exists() or any(
            t.one_admin_name == name for t in others.filter(organization=self.tenant.organization, one_admin_user="")
        ):
            raise forms.ValidationError(_("Another project has the admin %(n)s.") % {"n": name})
        return name

    def _password(self, field):
        p = self.cleaned_data.get(field) or ""
        if not p:
            return p
        words = passwords.words_for(
            self.tenant.name or "",
            self.tenant.organization,
            self.user,
            extra=[self.data.get(self.add_prefix("igw_username"), "")],
        )
        bad = passwords.problems(p, words)
        if bad:
            raise forms.ValidationError(_("The password needs: %(what)s.") % {"what": "; ".join(bad)})
        return p

    def clean_admin_password(self):
        return self._password("admin_password")

    def clean_igw_password(self):
        return self._password("igw_password")

    def clean(self):
        d = super().clean()
        if self.inet:
            d["igw_type"] = d.get("igw_type") or "vyos"
            if d["igw_type"] == "vyos":
                if not d.get("igw_username") and "igw_username" not in self.errors:
                    self.add_error("igw_username", _("Give the igw user."))
                if not d.get("igw_password") and "igw_password" not in self.errors:
                    self.add_error("igw_password", _("Give the igw password."))
        d["key_text"] = ""
        if d.get("igw_key"):
            d["key_text"] = clean_ssh_key(next((k for v, _l2, k in self.keys if v == d["igw_key"]), ""))
        return d

    def clean_tenant_admin(self):
        from .admins import admin_candidates

        u = admin_candidates(self.tenant.organization).filter(pk=self.cleaned_data.get("tenant_admin") or 0).first()
        if u is None:
            raise forms.ValidationError(_("Choose a user of the organization."))
        return u


def apply(tenant, form, user):
    """Keep what the dialog chose on the tenant (passwords encrypted). Returns the recipients' ids."""
    d = form.cleaned_data
    tenant.one_admin_user = d["admin_user"]
    tenant.set_admin_password(d["admin_password"])
    fields = ["one_admin_user", "admin_password_encrypted"]
    if form.inet:
        kind = d["igw_type"]
        site = (tenant.order or {}).get("site") or ""
        tenant.order = {**(tenant.order or {}), "igw_type": kind}
        cur = next((c for c in Connectivity.current_for(tenant) if c.site == site), None)
        if cur is not None and cur.igw_type != kind:
            vals = cur.values()
            vals["igw_type"] = kind
            Connectivity.objects.create(
                tenant=tenant, site=site, version=Connectivity.next_version(tenant, site), created_by=user, **vals
            )
        if kind == "vyos":
            tenant.igw_username = d["igw_username"]
            tenant.set_igw_password(d["igw_password"])
        else:
            tenant.igw_username, tenant.igw_password_encrypted = "", ""
        tenant.igw_cpu = d.get("igw_cpu") or tenant.igw_cpu or 1  # owner 2026-10-29 18:58
        tenant.igw_ram_gb = d.get("igw_ram_gb") or tenant.igw_ram_gb or 1
        fields += ["igw_cpu", "igw_ram_gb"]
        tenant.igw_ssh_key = d.get("key_text") or ""
        tenant.igw_ssh_port = tenant.igw_ssh_port or Tenant.random_igw_port()
        tenant.igw_ssh_from = tenant.igw_ssh_from or "0.0.0.0/0"
        fields += ["order", "igw_username", "igw_password_encrypted", "igw_ssh_key", "igw_ssh_port", "igw_ssh_from"]
        if d.get("key_text") and not tenant.ssh_keys.filter(key=d["key_text"]).exists():
            from .sshkey_views import _name_for

            TenantSshKey.objects.create(
                tenant=tenant, name=_name_for(tenant, d["key_text"]), key=d["key_text"], on_igw=True, created_by=user
            )
    tenant.save(update_fields=fields)
    from .admins import proposed, set_tenant_admin

    admin = d.get("tenant_admin")
    if admin is None:  # owner 2026-11-03: every project must have a project admin — pick one from the organization
        admin = proposed(tenant)[0]
    if admin is None:  # the organization has no eligible users (it always has at least its org admin)
        return []
    set_tenant_admin(tenant, admin, user, why="provisioning")
    return [admin.pk]


def auto(tenant, user=None):
    """corrections-18: provision as soon as every approval is there — with the defaults the Provision dialog offered
    (the admin <org>-<tenant name>, generated passwords, the igw type of the order, its user and a generated password,
    the tenant admin). Returns (deployment or None, why not)."""
    from types import SimpleNamespace

    from . import deploy as dp

    if tenant.approval != Approval.APPROVED or tenant.is_deleted or provisioned(tenant):
        return None, "nothing to provision"
    if tenant.deployments.filter(state="running").exists():
        return None, "a provisioning is running"
    why = dp.blocked_reason(tenant)
    if why:
        audit(user, "tenant_provision_waiting", tenant, why=why)
        return None, why
    form = ProvisionForm(tenant=tenant, user=user, prefix="pv")
    data = {}
    for name, f in form.fields.items():
        v = form.initial.get(name, f.initial)
        data[form.add_prefix(name)] = v if v is not None else ""
    if "igw_type" in form.fields:
        data[form.add_prefix("igw_type")] = (tenant.order or {}).get("igw_type") or "vyos"
    bound = ProvisionForm(data, tenant=tenant, user=user, prefix="pv")
    valid = bound.is_valid()
    errors = {k: v for k, v in bound.errors.items() if k != "tenant_admin"}
    if errors:
        why = "; ".join(f"{k}: {' '.join(v)}" for k, v in errors.items())[:300]
        audit(user, "tenant_provision_waiting", tenant, why=why)
        return None, why
    d = dict(bound.cleaned_data)
    d.setdefault("tenant_admin", None)  # none yet: provisioned anyway, the credentials wait (Resend)
    d.setdefault("key_text", "")
    del valid
    who = apply(tenant, SimpleNamespace(cleaned_data=d, inet=bound.inet), user)
    audit(
        user,
        "tenant_provision_started",
        tenant,
        admin=tenant.one_admin_user,
        automatic=True,
        igw=(tenant.order or {}).get("igw_type", "") if bound.inet else "",
        send_to=len(who),
        tenant_admin=tenant.tenant_admin.email if tenant.tenant_admin_id else "none",
    )
    dep = dp.start(tenant, user, notify=who)
    return dep, ""


@require_POST
def provision_view(request, pk):
    tenant = get_object_or_404(Tenant, pk=pk)
    user = request.user
    if not can_provision(tenant, user):
        raise PermissionDenied
    from . import deploy as dp

    why = dp.blocked_reason(tenant)
    if why:
        messages.error(request, _("Cannot provision now: %(why)s.") % {"why": why})
        return redirect("tenant_detail", pk=pk)
    form = ProvisionForm(request.POST, prefix="pv", tenant=tenant, user=user)
    if not form.is_valid():
        for name, errs in form.errors.items():
            label = form.fields[name].label if name in form.fields else ""
            for e in errs:
                messages.error(request, f"{label}: {e}" if label else e)
        return redirect(reverse("tenant_detail", args=[pk]) + "?provision=1")
    who = apply(tenant, form, user)
    audit(
        user,
        "tenant_provision_started",
        tenant,
        admin=tenant.one_admin_user,
        igw=(tenant.order or {}).get("igw_type", "") if form.inet else "",
        send_to=len(who),
    )
    dep = dp.start(tenant, user, notify=who)
    if dep.state == "failed":
        messages.error(request, _("Provisioning failed: %(e)s") % {"e": dep.error})
    else:
        messages.info(
            request, _("Provisioning started; the progress is in Tasks. The credentials are sent when it is done.")
        )
    return redirect("tenant_detail", pk=pk)


def send_credentials(dep):
    """After a finished provisioning (correction 15): the full e-mail with the credentials to the tenant admin and a
    notification to every platform user connected to the tenant."""
    from .notices import send_ready

    # owner 2026-10-29 15:17: the provisioning e-mail reaches the client also while client mail is held
    return send_ready(dep.tenant, by=dep.created_by, allow_client=True)


def can_resend(tenant, user):
    """Correction 15: the credentials e-mail again to the tenant admin — staff who provision (support engineer,
    resource manager, super admin), once the tenant is made."""
    if not user.is_authenticated or not user.is_staff_member or tenant.is_deleted or tenant.tenant_admin_id is None:
        return False
    if not (tenant.one_group_id or tenant.deployments.filter(state="done").exists()):
        return False
    return user.is_super_admin or user.has_role(Role.SUPPORT) or user.has_role(Role.RESOURCE_MANAGER)


@require_POST
@login_required
def resend_view(request, pk):
    tenant = get_object_or_404(Tenant, pk=pk)
    if not can_resend(tenant, request.user):
        raise PermissionDenied
    from .notices import send_ready

    n = send_ready(tenant, by=request.user, to_staff=False, allow_client=True, to_org_admins=False)
    audit(request.user, "tenant_credentials_resent", tenant, to=tenant.tenant_admin.email, sent=n)
    if n:
        messages.success(request, _("The credentials are sent to %(e)s.") % {"e": tenant.tenant_admin.email})
    else:
        from .models import PortalSettings

        why = (
            ""
            if PortalSettings.get().client_mail
            else " " + _("E-mail to clients is off (Settings): the message is held.")
        )
        messages.warning(request, _("Nothing was sent to %(e)s.") % {"e": tenant.tenant_admin.email} + why)
    return redirect("tenant_detail", pk=pk)


__all__ = [
    "ProvisionForm",
    "auto",
    "can_provision",
    "provision_view",
    "send_credentials",
    "provisioned",
    "failed_provisioning",
    "can_resend",
    "resend_view",
]
