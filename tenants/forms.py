"""The forms of the tenants app: ordering a project, approving it, editing it, and every network change.

A form here decides what a client may *ask* for; what actually happens is decided at approval and carried out by
`provision.py`, `deploy.py` and `netops.py`. Choices that depend on the infrastructure (sites, base subnets,
prefixes) are built from the portal's records, never typed.
"""

from datetime import date

from django import forms
from django.utils.translation import gettext_lazy as _

from accounts.fields import city_field, phone_field
from accounts.models import Role, User

from . import naming
from .models import Connectivity, Organization, OrgKind, Tenant, TenantKind, TenantStage
from .ordering import (
    DEFAULT_RESOURCES,
    MAX_EXTRA_PREFIXES,
    MBPS_DEFAULT,
    MBPS_MAX,
    MBPS_MIN,
    PREFIX_LENGTHS,
    AllocationError,
    connections,
    plan_prefixes,
    pools,
)


def sales_rep_queryset():
    return User.objects.sales_reps().order_by("full_name", "email")


def corp_domain_ok(user):
    """corrections-18: a corporate organization's sales rep has an e-mail of a corporate domain (Settings)."""
    from .models import PortalSettings

    s = PortalSettings.get()
    return user is None or not s.corporate_domain_list or s.is_corporate_email(user.email)


def check_org_platform_users(form, data, kind):
    """The platform users of an organization by its type (corrections-18): a cloud one needs its sales rep; a corporate
    one may have one, of a corporate e-mail domain, and no KAM."""
    if kind == OrgKind.CORP:
        if data.get("kam"):
            form.add_error("kam", _("A corporate organization has no key account manager."))
        rep = data.get("sales_rep")
        if rep is not None and not corp_domain_ok(rep):
            form.add_error(
                "sales_rep",
                _("A corporate organization's sales rep needs a corporate e-mail domain (Settings → Provider)."),
            )
    elif "sales_rep" in form.fields and not data.get("sales_rep") and "sales_rep" not in form.errors:
        form.add_error("sales_rep", _("A cloud organization needs its sales rep."))


def cluster_choices():
    """Shared clusters customers can get resources on: (platform key/quota code, label)."""
    from inventory.models import Cluster

    out = []
    for c in Cluster.objects.select_related("platform", "platform__site"):
        if c.orderable:
            p = c.platform
            fe = f" · front end {p.frontend}" if p.frontend != p.key else ""
            out.append((f"{p.key}/{c.order_code}", f"{p.key} · {c.name}{fe}"))
    return sorted(out)


class RMApprovalForm(forms.ModelForm):
    """The resource manager names the tenant, places it on a cluster and confirms the resources."""

    placement = forms.ChoiceField(label=_("Site and cluster"))
    cpu = forms.IntegerField(label=_("vCPU"), min_value=1, max_value=4096)
    ram_gb = forms.IntegerField(label=_("RAM, GB"), min_value=1, max_value=65536)
    gpu = forms.IntegerField(label=_("GPUs"), min_value=0, max_value=1000, required=False)
    disk_gb = forms.IntegerField(label=_("Storage, GB"), min_value=1, max_value=1048576)
    backups_gb = forms.IntegerField(label=_("Backups, GB"), min_value=0, max_value=1048576, required=False)
    inet_pool = forms.ChoiceField(label=_("Internet address pool"), required=False)
    sales_rep = forms.ModelChoiceField(
        label=_("Sales rep of the organization"), queryset=User.objects.none(), required=False
    )
    tenant_admin = forms.ModelChoiceField(
        label=_("Project admin"),
        queryset=User.objects.none(),
        help_text=_("A user of the organization: gets the project's credentials."),
    )

    class Meta:
        model = Tenant
        fields = ["name", "kind"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        o = self.instance.order or {}
        self.fields["name"].required = True
        org = self.instance.organization if self.instance.organization_id else None
        if org and org.short_name:
            self.fields["name"].help_text = _(
                "4-15 lowercase letters, digits and -, starting with a letter. "
                "Project ID: _%(org)s_<name> (without -). It cannot be changed after approval."
            ) % {"org": org.short_name}
        if self.instance.pk and self.instance.gateways_deployed:  # correction 12
            self.fields["name"].disabled = True
            self.fields["name"].help_text = _("Fixed: the project's gateways are deployed.")
        if org is not None and not org.is_corp and not org.sales_rep_id and self.instance.kind != TenantKind.CORP:
            self.fields["sales_rep"].required = True  # owner 2026-10-29: the platform users are required
        from .admins import admin_candidates
        from .admins import label as admin_label

        self.fields["tenant_admin"].queryset = admin_candidates(org)
        self.fields["tenant_admin"].label_from_instance = admin_label
        self.fields["tenant_admin"].required = False  # kept / proposed unless another is chosen
        if self.instance.tenant_admin_id:
            self.initial.setdefault("tenant_admin", self.instance.tenant_admin_id)
        elif self.instance.pk:
            from .admins import proposed

            pick = proposed(self.instance)[0]
            if pick is not None:
                self.initial.setdefault("tenant_admin", pick.pk)
        self.fields["placement"].choices = [("", "—")] + cluster_choices()
        if o.get("platform") and o.get("cluster"):
            self.initial.setdefault("placement", f"{o['platform']}/{o['cluster']}")
        for f in ("cpu", "ram_gb", "disk_gb", "backups_gb"):
            self.initial.setdefault(f, o.get(f, DEFAULT_RESOURCES[f]))
        self.initial.setdefault("gpu", o.get("gpu", 0))
        if "inet" in connections(o):
            from inventory.models import DelegatedSubnet

            choices = []
            for pkey in sorted({c[0].split("/")[0] for c in cluster_choices()}):
                choices += [
                    (d.cidr, f"{pkey} · {d.segment} · {d.cidr}") for d in pools(pkey, DelegatedSubnet.Purpose.INET)
                ]
            self.fields["inet_pool"].choices = [("", _("first free (cloud pools first)"))] + choices
            if (
                choices
            ):  # owner 2026-11-03 (u11): default to what was ordered for the main prefix, else first BGP subnet
                ordered = o.get("inet_main_pool") or ""
                self.initial.setdefault(
                    "inet_pool", ordered if any(ordered == c[0] for c in choices) else ""
                )  # owner 2026-11-05: else auto (first free), never an off-platform pool
        else:
            del self.fields["inet_pool"]
        reps = sales_rep_queryset()
        self.fields["sales_rep"].queryset = reps
        org = self.instance.organization
        if (org and org.sales_rep_id) or not reps.exists():
            del self.fields["sales_rep"]
        if not (org and org.is_corp):
            self.fields["kind"].choices = [c for c in self.fields["kind"].choices if c[0] != TenantKind.CORP]

    def clean(self):
        data = super().clean()
        # owner 2026-11-12: with sales approval on and no rep of the organization, any sales rep is asked - the rep
        # can be picked here, not demanded
        name = data.get("name")
        if name and not self.instance.legacy_names:
            org = self.instance.organization if self.instance.organization_id else None
            if not naming.TENANT_RE.match(name):
                self.add_error(
                    "name", _("4-15 characters: lowercase latin letters, digits and -, starting with a letter.")
                )
            elif org is None or not org.short_name:
                self.add_error("name", _("Link the project to an organization with a short name first."))
            elif naming.taken(name, org, exclude_pk=self.instance.pk):
                self.add_error("name", _("This organization already has a project with this name."))
            else:
                group = naming.group_name(org.short_name, name)
                if (
                    Tenant.objects.filter(group_name=group).exclude(pk=self.instance.pk).exists()
                    or group in naming.observed_group_names()
                ):
                    self.add_error("name", _("OpenNebula group %(g)s already exists.") % {"g": group})
                else:
                    self.group_name = group
        placement = data.get("placement") or ""
        if "/" in placement:
            pkey, cluster = placement.split("/", 1)
            order = {**(self.instance.order or {}), "platform": pkey, "cluster": cluster}
            pool = data.get("inet_pool") or None
            if pool and "inet" in connections(order):
                if not any(pool == d.cidr for d in pools(pkey, "inet")):
                    self.add_error("inet_pool", _("This pool is not on an exchange network of %(p)s.") % {"p": pkey})
            try:
                self.planned_prefixes = plan_prefixes(order, pkey, pool)
            except AllocationError as exc:
                self.add_error("placement", str(exc))
        return data

    def order_update(self):
        from inventory.models import Platform

        pkey, cluster = self.cleaned_data["placement"].split("/", 1)
        plat = Platform.objects.select_related("site").get(key=pkey)
        from .models import fit_split

        o = self.instance.order or {}
        vals = {
            f: self.cleaned_data[f] if self.cleaned_data.get(f) is not None else DEFAULT_RESOURCES[f]
            for f in ("cpu", "ram_gb", "disk_gb", "backups_gb")
        }
        images, files = fit_split(vals["disk_gb"], o.get("images_gb"), o.get("files_gb"))
        return {
            "platform": pkey,
            "frontend": plat.frontend,
            "site": plat.site.code,
            "cluster": cluster,
            **vals,
            "images_gb": images,
            "files_gb": files,
            "gpu": self.cleaned_data.get("gpu") or 0,
        }


class TenantStaffForm(forms.ModelForm):
    """Details staff keep on a tenant. Not here: the type (fixed), the stage, status and review date (own actions),
    the organization (Move). The short name (u02) only while nothing is provisioned: its OpenNebula group follows it."""

    class Meta:
        model = Tenant
        fields = [
            "name",
            "title",
            "resource_manager",
            "support_engineer",
            "notes",
        ]  # u05: ID, Description, RM, support, notes
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["resource_manager"].queryset = User.objects.filter(
            kind="staff", is_resource_manager=True, is_active=True
        )
        self.fields["support_engineer"].queryset = User.objects.filter(kind="staff", is_support=True, is_active=True)
        self.fields["name"].label = _("ID")  # u05: renamed from "short name"
        self.fields["name"].help_text = _(
            "4-15 lowercase latin letters, digits and -, starting with a letter; the project ID follows it."
        )
        self.fields["title"].label = _("Description")  # u05: renamed from "project title"
        from .provision import provisioned

        # u05: only the ID is fixed once provisioned; the description can be changed at any time
        self.locked = bool(self.instance.pk and (provisioned(self.instance) or self.instance.legacy_names))
        if self.locked:
            self.fields["name"].disabled = True
            self.fields["name"].help_text = _("Fixed: the project's resources are provisioned.")
        self.group_name = None

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip().lower()
        if self.locked or name == (self.instance.name or ""):
            return self.instance.name
        org = self.instance.organization if self.instance.organization_id else None
        if not naming.TENANT_RE.match(name):
            raise forms.ValidationError(
                _("4-15 characters: lowercase latin letters, digits and -, starting with a letter.")
            )
        if org is None or not org.short_name:
            raise forms.ValidationError(_("Link the project to an organization with a short name first."))
        if naming.taken(name, org, exclude_pk=self.instance.pk):
            raise forms.ValidationError(_("This organization already has a project with this name."))
        if self.instance.group_name:  # fixed on approval: it follows the new name (nothing is on the platform yet)
            group = naming.group_name(org.short_name, name)
            if (
                Tenant.objects.filter(group_name=group).exclude(pk=self.instance.pk).exists()
                or group in naming.observed_group_names()
            ):
                raise forms.ValidationError(_("OpenNebula group %(g)s already exists.") % {"g": group})
            self.group_name = group
        return name

    def save(self, commit=True):
        t = super().save(commit=False)
        if self.group_name:
            t.group_name = self.group_name
        if commit:
            t.save()
        return t


class StatusForm(forms.Form):
    """A status change by hand (the lifecycle of quota.md) with the date the new status is reviewed."""

    status = forms.ChoiceField(label=_("New status"))
    date_review = forms.DateField(
        label=_("Review date"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("Paused: at most 2 weeks; suspended: at most 1 month; saved: archived on this date."),
    )
    note = forms.CharField(label=_("Comment"), required=False, max_length=500)

    def __init__(self, *args, tenant=None, keep_option=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.tenant = tenant
        self.fields["status"].choices = tenant.status_choices() if tenant is not None else []
        if keep_option:  # correction 15: in the one Edit dialog the status stays unless one is chosen
            self.fields["status"].choices = [("", _("— unchanged (%(s)s) —") % {"s": tenant.get_status_display()})] + [
                c for c in self.fields["status"].choices if c[0] != tenant.status
            ]
            self.fields["status"].required = False

    def clean(self):
        from datetime import timedelta

        from .models import STATUS_PERIOD

        d = super().clean()
        new, review = d.get("status"), d.get("date_review")
        period = STATUS_PERIOD.get(new)
        today = date.today()
        if period:
            limit = today + timedelta(days=period)
            if review is None:
                d["date_review"] = limit
            elif not today <= review <= limit:
                self.add_error("date_review", _("From today to %(d)s.") % {"d": limit.strftime("%d.%m.%Y")})
        return d


class ReviewForm(forms.ModelForm):
    class Meta:
        model = Tenant
        fields = ["date_review"]
        widgets = {"date_review": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["date_review"].required = True

    def clean_date_review(self):
        d = self.cleaned_data["date_review"]
        t, u = self.instance, self.user
        if d < date.today():
            raise forms.ValidationError(_("A date from today on."))
        if u is not None and not (u.is_super_admin or u.has_role(Role.RESOURCE_MANAGER)) and d > t.poc_review_limit():
            raise forms.ValidationError(
                _("A POC can be extended up to 3 months after it was created (%(d)s).")
                % {"d": t.poc_review_limit().strftime("%d.%m.%Y")}
            )
        return d


class RejectForm(forms.Form):
    reason = forms.CharField(label=_("Reason"), widget=forms.Textarea(attrs={"rows": 3}), max_length=2000)


class OrgRegistrationForm(forms.ModelForm):
    """Correction 16: the resource manager approves a registered organization and assigns its platform users."""

    class Meta:
        model = Organization
        fields = ["kind", "resource_manager", "support_engineer", "sales_rep", "kam"]

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["sales_rep"].queryset = sales_rep_queryset()
        self.fields["kam"].queryset = User.objects.kams()
        self.fields["resource_manager"].queryset = User.objects.filter(
            kind="staff", is_resource_manager=True, is_active=True
        )
        self.fields["support_engineer"].queryset = User.objects.filter(kind="staff", is_support=True, is_active=True)
        for f in ("resource_manager", "support_engineer"):
            self.fields[f].required = True
            self.fields[f].help_text = ""
        self.fields["sales_rep"].help_text = _(
            "Required for cloud organizations; optional for corporate ones (a corporate e-mail domain)."
        )
        self.fields["kam"].help_text = _("Optional; cloud organizations only.")
        if actor is not None and actor.holds(Role.RESOURCE_MANAGER) and not self.instance.resource_manager_id:
            self.initial["resource_manager"] = actor.pk

    def clean(self):
        data = super().clean()
        if data.get("kind") == OrgKind.CORP:
            data["kam"] = None
        check_org_platform_users(self, data, data.get("kind"))
        return data


class OrganizationForm(forms.ModelForm):
    class Meta:
        model = Organization
        # u05: tax id first; the organization keeps only its sales rep and KAM (resource manager and support
        # engineer are per-project now, on the project edit form)
        fields = [
            "country",
            "tax_id_type",
            "tax_id",
            "name",
            "kind",
            "short_name",
            "address",
            "website",
            "sales_rep",
            "kam",
            "notes",
        ]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    country = forms.ChoiceField(label=_("Country"))
    tax_id_type = forms.ChoiceField(label=_("Tax ID type"), required=False)

    def __init__(self, *args, can_set_rep=True, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.actor = actor
        if actor is not None:  # u12: only RM/super may set or reassign the org's manager/KAM; a manager cannot
            from .permissions import is_rm

            can_set_rep = is_rm(actor)
        # correction 12 (production): legal name, type (cloud) and tax ID are required; owner 2026-11-12: the country
        # decides the tax ID (accounts/taxid.py)
        for f in ("name", "kind", "tax_id"):
            self.fields[f].required = True
        from accounts.forms import country_fields

        country_fields(self)
        if not self.instance.pk:
            self.initial.setdefault("kind", OrgKind.CLOUD)
        if "website" in self.fields:  # "example.com" is taken as https://example.com
            self.fields["website"] = forms.URLField(
                label=self.fields["website"].label, required=False, assume_scheme="https"
            )
            self.fields["website"].widget.attrs["placeholder"] = "https://example.com"
        self.fields["sales_rep"].queryset = sales_rep_queryset()
        self.fields["sales_rep"].help_text = _("Cloud organizations only.")
        self.fields["kam"].queryset = User.objects.kams()
        self.fields["kam"].help_text = _("Optional; cloud organizations only.")
        if not can_set_rep:
            del self.fields["sales_rep"]
            del self.fields["kam"]
        if not self.instance.pk:
            self._add_user_fields()
        if self.instance.pk and self.instance.kind_locked:  # corrections-18
            self.fields["kind"].disabled = True
            self.fields["kind"].help_text = _("Fixed: the organization has projects.")
        if "sales_rep" in self.fields:
            self.fields["sales_rep"].help_text = _(
                "Required for cloud organizations; optional for corporate ones (a corporate e-mail domain)."
            )
        if self.instance.pk and not self.instance.short_name_editable:  # u02: only without projects
            self.fields["short_name"].disabled = True
            self.fields["short_name"].help_text = (
                _("Fixed: project groups already use it.")
                if self.instance.short_name_locked
                else _("Fixed: the organization has projects.")
            )
        else:
            self.fields["short_name"].help_text = _(
                "3–15 lowercase latin/digits; unique; generated from the legal name if left empty."
            )
        if (
            self.instance.pk
        ):  # u10: identity is fixed once the organization exists — only short name, team and notes change
            for _f in ("country", "tax_id_type", "tax_id", "name", "kind"):
                self.fields[_f].disabled = True
        if self.instance.pk and self.instance.is_corp:  # u11: assign a platform user as the corp org's admin
            from .admins import admin_candidates
            from .admins import label as admin_label

            self._org_admin_cands = list(admin_candidates(self.instance, capability="may_org_admin"))
            self.fields["org_admin"] = forms.ChoiceField(
                label=_("Organization admin"),
                required=False,
                choices=[("", "—")] + [(str(u.pk), admin_label(u)) for u in self._org_admin_cands],
                help_text=_("A corporate organization can have a platform user as its admin."),
            )
            cur = self.instance.admins.first()
            if cur:
                self.initial["org_admin"] = str(cur.pk)

    def save(self, commit=True):
        org = super().save(commit=False)
        if self.actor is not None and org.pk is None and org.sales_rep_id is None:  # u12: a manager who creates
            from .permissions import is_rm  # an org becomes its manager

            if self.actor.is_sales and not is_rm(self.actor):
                org.sales_rep = self.actor
        if commit:
            org.save()
            self.save_m2m()
        return org

    def save_org_admin(self, org, by=None):
        """u11: make the chosen user (an org user or, for a corporate org, a platform user) the organization admin."""
        pk = self.cleaned_data.get("org_admin")
        if not pk:
            return None
        from accounts.models import audit

        u = next((c for c in getattr(self, "_org_admin_cands", []) if str(c.pk) == str(pk)), None)
        if u is None:
            return None
        fields = []
        if u.organization_id != org.pk:
            u.organization = org
            fields.append("organization")
        if not u.is_org_admin:
            u.is_org_admin = True
            fields.append("is_org_admin")
        if fields:
            u.save(update_fields=fields)
            audit(by, "org_admin_set", org, user=u.email)
        return u

    def clean_tax_id(self):
        return (self.cleaned_data.get("tax_id") or "").strip() or None

    def _clean_user(self, data):
        import re as _re

        from accounts.kz import normalize_intl_phone

        pick = data.get("org_user")
        if pick:
            u = User.objects.filter(pk=pick, kind="customer", organization__isnull=True).first()
            if u is None:
                self.add_error("org_user", _("Choose a user without an organization."))
            elif not (u.full_name and u.phone):
                self.add_error(
                    "org_user",
                    _("%(e)s has no name or phone yet: complete the user first, or add a new one.") % {"e": u.email},
                )
            self.org_user = u
            return
        name = " ".join((data.get("user_name") or "").split())
        if len(name) < 2 or _re.search(r"[0-9@<>]", name):
            self.add_error("user_name", _("Enter the user's full name."))
        email = (data.get("user_email") or "").strip().lower()
        if not email:
            self.add_error("user_email", _("Enter the user's email."))
        elif User.objects.filter(email__iexact=email).exists():
            self.add_error(
                "user_email",
                _("This email already exists in the portal: choose that user above if it has no organization."),
            )
        try:
            phone = normalize_intl_phone(data.get("user_phone") or "")
        except forms.ValidationError as exc:
            self.add_error("user_phone", exc)
            phone = ""
        data.update(user_name=name, user_email=email, user_phone=phone, user_city=data.get("user_city") or "")
        self.org_user = None

    def save_user(self, org, by):
        """Attach the chosen user, or create the new one (not activated: staff invite it from the organization's users)."""
        from accounts.models import Kind, audit

        d = self.cleaned_data
        if getattr(self, "no_user", False):
            return None
        u = getattr(self, "org_user", None)
        if u is not None:
            u.organization, u.is_org_admin = org, True
            u.save(update_fields=["organization", "is_org_admin"])
            audit(by, "user_organization_changed", u, now=org.name, org_admin=True, why="new organization")
            return u
        u = User.objects.create_user(
            d["user_email"],
            None,
            kind=Kind.CUSTOMER,
            full_name=d["user_name"],
            phone=d["user_phone"],
            city=d.get("user_city") or "",
            organization=org,
            is_org_admin=True,
            login_enabled=False,
        )
        audit(by, "customer_created", u, org=org.name, org_admin=True)
        return u

    def _add_user_fields(self):
        """Correction 14: a new organization gets its user at once — one of the users without an organization, or a new
        one (name, email and a phone in international format)."""
        free = User.objects.filter(kind="customer", organization__isnull=True, is_active=True).order_by(
            "full_name", "email"
        )
        self.fields["org_user"] = forms.ChoiceField(
            label=_("Organization user"),
            required=False,
            choices=[("", _("— a new user (below) —"))]
            + [(str(u.pk), f"{u.full_name or '—'} <{u.email}>") for u in free],
            help_text=_("Becomes the organization's admin."),
        )
        self.fields["user_name"] = forms.CharField(
            label=_("New user: full name"),
            max_length=200,
            required=False,
            widget=forms.TextInput(attrs={"placeholder": _("Full Name")}),
        )
        self.fields["user_email"] = forms.EmailField(
            label=_("New user: email"),
            required=False,
            widget=forms.EmailInput(attrs={"placeholder": "name@company.kz"}),
        )
        self.fields["user_phone"] = phone_field(label=_("New user: mobile number"), required=False)
        self.fields["user_city"] = city_field(label=_("New user: city"), required=False)

    def clean(self):
        data = super().clean()
        if "org_user" in self.fields:
            nothing = not any(
                (data.get(f) or "").strip() for f in ("org_user", "user_name", "user_email", "user_phone")
            )
            if data.get("kind") == OrgKind.CORP and nothing:
                self.org_user, self.no_user = (
                    None,
                    True,
                )  # corrections-18: platform users serve a corporate organization
            else:
                self._clean_user(data)
        check_org_platform_users(self, data, data.get("kind"))
        if not self.instance.pk:  # the identity is fixed once the organization exists (u10)
            from accounts.forms import clean_tax_id

            clean_tax_id(self, data)
        return data


class TenantMoveForm(forms.Form):
    """corrections-18: the organization to move the tenant to — a list (IIN/BIN first, by name); the move waits for the
    resource manager's and the target sales rep's approval (tenants.moves)."""

    # u03: not "required" in the page: it is hidden until "Move to another" (a hidden required select stopped the form)
    organization = forms.ChoiceField(label=_("Organization"), required=False)

    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tenant = tenant
        qs = Organization.objects.filter(archived_at__isnull=True).order_by("name")
        if tenant is not None:
            qs = qs.exclude(pk=tenant.organization_id)
        self.orgs = {str(o.pk): o for o in qs}
        self.fields["organization"].choices = [("", "—")] + [(str(o.pk), f"{o.tax_id or '—'} · {o.name}") for o in qs]

    def clean_organization(self):
        o = self.orgs.get(self.cleaned_data.get("organization") or "")
        if o is None:
            raise forms.ValidationError(_("Choose an organization."))
        return o


class MergeForm(forms.Form):
    target = forms.ModelChoiceField(label=_("Merge into"), queryset=Organization.objects.none())

    def __init__(self, *args, source=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["target"].queryset = (
            Organization.objects.exclude(pk=source.pk).filter(archived_at__isnull=True).order_by("name")
        )


class StageForm(forms.ModelForm):
    class Meta:
        model = Tenant
        fields = ["stage"]


def _lengths(text, field_label, allowed=PREFIX_LENGTHS):
    out = []
    for part in (text or "").replace(",", " ").replace("/", " ").split():
        try:
            n = int(part)
        except ValueError:
            raise forms.ValidationError(
                _("%(f)s: %(v)s is not a prefix length.") % {"f": field_label, "v": part}
            ) from None
        if n not in allowed:
            raise forms.ValidationError(
                _("%(f)s: /%(n)s is not offered; choose from %(list)s.")
                % {"f": field_label, "n": n, "list": " ".join(f"/{x}" for x in allowed)}
            )
        out.append(n)
    if len(out) > MAX_EXTRA_PREFIXES:
        raise forms.ValidationError(_("At most %(n)s prefixes.") % {"n": MAX_EXTRA_PREFIXES})
    return out


SSH_KEY_RE = (
    r"^(ssh-(rsa|ed25519|dss)|ecdsa-sha2-nistp(256|384|521)|sk-(ssh-ed25519|ecdsa-sha2-nistp256)@openssh\.com) "
    r"[A-Za-z0-9+/]+={0,3}( \S.*)?$"
)


def clean_ssh_key(value):
    import re

    key = " ".join((value or "").split())
    if key and not re.match(SSH_KEY_RE, key):
        raise forms.ValidationError(_("This does not look like an ssh public key (e.g. ssh-ed25519 AAAA…)."))
    return key


class OrderForm(forms.Form):
    """What an organization admin asks for when ordering a tenant: resources and connections only (correction 13);
    the admin, the igw and the keys are set when the tenant is provisioned (ProvisionForm)."""

    PREFIX_CHOICES = [(n, f"/{n} · {2 ** (32 - n)} " + str(_("addresses"))) for n in PREFIX_LENGTHS]
    CONNECTION_CHOICES = [
        ("inet", _("Internet")),
        ("corpnet", _("Corporate network")),
        ("l2", _("External L2 channel")),
    ]

    name = forms.CharField(
        label=_("Project name"),
        max_length=naming.TENANT_MAX,
        help_text=_("4-15 lowercase latin letters and digits, starting with a letter. It cannot be changed later."),
    )
    kind = forms.ChoiceField(
        label=_("Project type"),
        widget=forms.RadioSelect,
        initial=TenantKind.CLOUD,
        choices=TenantKind.choices,
        required=False,
    )
    stage = forms.ChoiceField(label=_("Stage"), choices=TenantStage.choices, initial=TenantStage.POC)
    date_review = forms.DateField(
        label=_("Review date"),
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("The day the stage (a POC) is reviewed."),
    )
    site = forms.ChoiceField(label=_("Site"))
    pool = forms.ChoiceField(label=_("Resource pool"))
    gpu = forms.IntegerField(
        label=_("GPU bundles"),
        min_value=1,
        max_value=64,
        initial=1,
        required=False,
        help_text=_(
            "GPU clusters are ordered in bundles: a bundle is one GPU with its share of the host's CPU and RAM."
        ),
    )
    cpu = forms.IntegerField(
        label=_("vCPU"), min_value=1, max_value=1024, initial=DEFAULT_RESOURCES["cpu"], required=False
    )
    ram_gb = forms.IntegerField(
        label=_("RAM, GB"), min_value=1, max_value=8192, initial=DEFAULT_RESOURCES["ram_gb"], required=False
    )
    disk_gb = forms.IntegerField(
        label=_("Storage, GB"),
        min_value=10,
        max_value=1048576,
        initial=DEFAULT_RESOURCES["disk_gb"],
        help_text=_(
            "VM disks, images and files together; all of it is VM disks until you "
            "set images or files on the project page."
        ),
    )
    backups_gb = forms.IntegerField(
        label=_("Backups, GB"),
        min_value=0,
        max_value=1048576,
        required=False,
        initial=DEFAULT_RESOURCES["backups_gb"],
        help_text=_("0 = no backups."),
    )
    connections = forms.MultipleChoiceField(
        label=_("Connections"),
        widget=forms.CheckboxSelectMultiple,
        initial=["inet"],
        choices=CONNECTION_CHOICES,
        required=False,
        help_text=_(
            "At least one. Internet and the corporate network can be combined; an external L2 channel "
            "(a VLAN from your site brought into the project, number assigned by support) can be added to "
            "either or ordered alone."
        ),
    )
    inet_mbps = forms.IntegerField(
        label=_("Internet bandwidth, Mbps"),
        min_value=MBPS_MIN,
        max_value=MBPS_MAX,
        initial=MBPS_DEFAULT,
        required=False,
        widget=forms.NumberInput(attrs={"step": 1, "min": MBPS_MIN, "max": MBPS_MAX}),
    )
    # owner 2026-11-09: chosen when the internet is ordered, not only at provisioning. The provision dialog already
    # reads it back from the order (tenants/views.py), so filling it here is all that was missing.
    igw_type = forms.ChoiceField(
        label=_("Internet gateway"),
        required=False,
        choices=Connectivity.Gateway.choices,
        initial=Connectivity.Gateway.VYOS,
    )
    inet_main_prefix = forms.TypedChoiceField(
        label=_("Main public prefix"), coerce=int, required=False, choices=PREFIX_CHOICES, initial=31
    )
    inet_main_pool = forms.ChoiceField(label=_("Base subnet"), required=False)
    inet_extra_prefixes = forms.CharField(
        label=_("Additional public prefixes"),
        required=False,
        help_text=_("Optional. Prefix lengths separated by spaces, e.g. 29 28."),
    )
    inet_extra_pools = forms.CharField(required=False, widget=forms.HiddenInput)
    corpnet_main_prefix = forms.TypedChoiceField(
        label=_("Main corporate prefix"), coerce=int, required=False, choices=PREFIX_CHOICES, initial=31
    )
    corpnet_main_pool = forms.ChoiceField(label=_("Base subnet"), required=False)
    corpnet_extra_prefixes = forms.CharField(required=False, widget=forms.HiddenInput)
    corpnet_extra_pools = forms.CharField(required=False, widget=forms.HiddenInput)
    note = forms.CharField(
        label=_("Comment"), required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 3})
    )
    tenant_admin = forms.ChoiceField(
        label=_("Project admin"),
        required=False,
        help_text=_("A user of the organization: gets the project's credentials when it is ready."),
    )

    def clean_tenant_admin(self):
        from .admins import admin_candidates

        users = admin_candidates(self.organization)
        pick = self.cleaned_data.get("tenant_admin") or (
            self.user.pk
            if self.user is not None
            and getattr(self.user, "organization_id", None) == getattr(self.organization, "pk", -1)
            else 0
        )
        u = users.filter(pk=pick or 0).first()
        if u is None:
            raise forms.ValidationError(_("Choose a user of the organization."))
        return u

    def clean_date_review(self):
        from datetime import timedelta

        from django.utils import timezone

        d = self.cleaned_data["date_review"]
        today = timezone.localdate()
        if d < today:
            raise forms.ValidationError(_("A date from today on."))
        staff = self.user is not None and (self.user.is_super_admin or self.user.has_role(Role.RESOURCE_MANAGER))
        if (
            not staff
            and self.cleaned_data.get("stage", TenantStage.POC) == TenantStage.POC
            and d > today + timedelta(days=Tenant.POC_MAX_DAYS)
        ):
            raise forms.ValidationError(
                _("A POC is reviewed within 3 months (%(d)s).")
                % {"d": (today + timedelta(days=Tenant.POC_MAX_DAYS)).strftime("%d.%m.%Y")}
            )
        return d

    def __init__(self, *args, corporate_allowed=False, organization=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.corporate_allowed = corporate_allowed
        self.organization = organization
        self.user = user
        if not self.is_bound:
            from datetime import timedelta

            from django.utils import timezone

            self.initial.setdefault("date_review", timezone.localdate() + timedelta(days=Tenant.POC_DAYS))
            if organization is not None:
                taken = naming.org_tenant_names(organization)
                n = 1
                while f"tenant{n}" in taken:
                    n += 1
                self.initial.setdefault("name", f"tenant{n}")
        from .admins import choices as admin_choices

        self.fields["tenant_admin"].choices = [("", "—")] + admin_choices(organization)
        if user is not None and getattr(user, "organization_id", None) == getattr(organization, "pk", -1):
            self.initial.setdefault("tenant_admin", str(user.pk))
        elif len(self.fields["tenant_admin"].choices) == 2:
            self.initial.setdefault("tenant_admin", self.fields["tenant_admin"].choices[1][0])
        from .ordering import inet_base_subnets, prefix_lengths_for, resource_pools

        # u10: the main-prefix dropdown offers 31 → MAX, MAX taken from the sites' IP subnets (most permissive
        # across sites here; the chosen site's own MAX is enforced in clean()).
        self._inet_lengths = prefix_lengths_for("inet")
        self.fields["inet_main_prefix"].choices = [
            (n, f"/{n} · {2 ** (32 - n)} " + str(_("addresses"))) for n in self._inet_lengths
        ]
        # u11: a base IX subnet can be picked per public prefix; "auto" lets the portal choose (cloud pools first).
        self._base_choices, self._base_meta = inet_base_subnets()
        self.fields["inet_main_pool"].choices = [("", _("auto"))] + self._base_choices
        from .ordering import corpnet_base_subnets

        self._corpnet_lengths = prefix_lengths_for("corpnet")
        self.fields["corpnet_main_prefix"].choices = [
            (n, f"/{n} · {2 ** (32 - n)} " + str(_("addresses"))) for n in self._corpnet_lengths
        ]
        self._corp_base_choices, self._corp_base_meta = corpnet_base_subnets()
        self.fields["corpnet_main_pool"].choices = [("", _("auto"))] + self._corp_base_choices
        self.pools = {p["key"]: p for p in resource_pools()}
        sites = []
        for p in self.pools.values():
            if p["site"] not in [s for s, _l in sites]:
                sites.append((p["site"], f"{p['site']} · {p['site_city']}" if p["site_city"] else p["site"]))
        self.fields["site"].choices = sites
        self.fields["pool"].choices = [(k, p["label"]) for k, p in self.pools.items()]
        if sites:
            self.fields["site"].initial = sites[0][0]
        if organization is not None and organization.short_name:
            self.fields["name"].help_text = _(
                "4-15 lowercase latin letters, digits and -, starting with a letter. "
                "Your project ID will be _%(org)s_<name> (without -); it cannot be changed "
                "later."
            ) % {"org": organization.short_name}
        if not corporate_allowed:
            del self.fields["kind"]
            self.fields["connections"].choices = [c for c in self.CONNECTION_CHOICES if c[0] != "corpnet"]

    def clean_name(self):
        v = (self.cleaned_data.get("name") or "").strip().lower()
        if not naming.TENANT_RE.match(v):
            raise forms.ValidationError(
                _("4-15 characters: lowercase latin letters, digits and -, starting with a letter.")
            )
        if self.organization is not None and naming.taken(v, self.organization):
            raise forms.ValidationError(_("Your organization already has a project with this name."))
        return v

    def short_name(self):
        return self.cleaned_data["name"]

    def clean(self):
        d = super().clean()
        self._resources(d)
        conns = set(d.get("connections") or [])
        if not conns and "connections" not in self.errors:
            self.add_error("connections", _("Choose at least one connection."))
        kind = d.get("kind") or TenantKind.CLOUD
        if "corpnet" in conns and not self.corporate_allowed:
            self.add_error("connections", _("Only corporate organizations can order the corporate network."))
        elif "corpnet" in conns and kind != TenantKind.CORP:
            self.add_error("connections", _("The corporate network is for corporate projects."))
        if "inet" in conns and "corpnet" in conns:
            self.add_error(
                "connections", _("The internet and the corporate network cannot be combined in one project.")
            )
        if conns == {"l2"} and kind == TenantKind.CORP:
            self.add_error(
                "connections",
                _("An L2 channel alone is for cloud projects; add the internet or the corporate network."),
            )
        from .ordering import prefix_lengths_for

        site = d.get("site")
        inet_allowed = prefix_lengths_for("inet", site=site)
        if "inet" in conns:
            if d.get("inet_mbps") is None:
                self.add_error("inet_mbps", _("Enter the bandwidth."))
            if not d.get("inet_main_prefix"):
                self.add_error("inet_main_prefix", _("Choose the main prefix."))
            elif d["inet_main_prefix"] not in inet_allowed:
                self.add_error(
                    "inet_main_prefix",
                    _("/%(n)s is not offered; choose from %(list)s.")
                    % {"n": d["inet_main_prefix"], "list": " ".join(f"/{x}" for x in inet_allowed)},
                )
            try:
                d["inet_extra_list"] = _lengths(
                    d.get("inet_extra_prefixes"), _("Additional public prefixes"), inet_allowed
                )
            except forms.ValidationError as exc:
                self.add_error("inet_extra_prefixes", exc)
            self._clean_base_subnets(d)
        if "corpnet" in conns:
            corp_allowed = prefix_lengths_for("corpnet", site=site)
            if not d.get("corpnet_main_prefix"):
                self.add_error("corpnet_main_prefix", _("Choose the main prefix."))
            elif d["corpnet_main_prefix"] not in corp_allowed:
                self.add_error(
                    "corpnet_main_prefix",
                    _("/%(n)s is not offered; choose from %(list)s.")
                    % {"n": d["corpnet_main_prefix"], "list": " ".join(f"/{x}" for x in corp_allowed)},
                )
            try:
                corp_extra = _lengths(d.get("corpnet_extra_prefixes"), _("Additional corporate prefixes"), corp_allowed)
            except forms.ValidationError as exc:
                self.add_error("corpnet_extra_prefixes", exc)
                corp_extra = []
            d["corpnet_list"] = ([d["corpnet_main_prefix"]] if d.get("corpnet_main_prefix") else []) + corp_extra
            self._clean_corp_base_subnets(d, corp_extra)
        return d

    def _clean_base_subnets(self, d):
        """The main prefix and each additional prefix may name a base IX subnet; "auto" (empty) lets the portal pick.
        A named subnet must be one the chosen platform reaches."""
        pkey = (d.get("pool") or "").split("/", 1)[0]

        def ok(cidr):
            meta = self._base_meta.get(cidr)
            return bool(meta) and (not pkey or pkey in meta["platforms"])

        main = d.get("inet_main_pool") or ""
        if main and not ok(main):
            self.add_error("inet_main_pool", _("This base subnet is not on an exchange network of the chosen cluster."))
            main = ""
        d["inet_main_pool"] = main
        tokens = (d.get("inet_extra_pools") or "").split()
        extras = d.get("inet_extra_list") or []
        pools_out = []
        for i in range(len(extras)):
            cidr = tokens[i] if i < len(tokens) else ""
            if cidr in ("", "auto"):
                pools_out.append("")
            elif ok(cidr):
                pools_out.append(cidr)
            else:
                self.add_error(
                    "inet_extra_prefixes",
                    _("Base subnet %(c)s is not on an exchange network of the chosen cluster.") % {"c": cidr},
                )
                pools_out.append("")
        d["inet_extra_pool_list"] = pools_out

    def _clean_corp_base_subnets(self, d, corp_extra):
        """Validate the corpnet base subnets (main + extras) against the chosen cluster's platform; build the parallel
        corpnet_pool_list (one base cidr or "" per corpnet prefix, main first). owner 2026-11-05."""
        pkey = (d.get("pool") or "").split("/", 1)[0]

        def ok(cidr):
            meta = self._corp_base_meta.get(cidr)
            return bool(meta) and (not pkey or pkey in meta["platforms"])

        main = d.get("corpnet_main_pool") or ""
        if main and not ok(main):
            self.add_error(
                "corpnet_main_pool", _("This base subnet is not on an exchange network of the chosen cluster.")
            )
            main = ""
        d["corpnet_main_pool"] = main
        tokens = (d.get("corpnet_extra_pools") or "").split()
        pools_out = []
        for i in range(len(corp_extra)):
            cidr = tokens[i] if i < len(tokens) else ""
            if cidr in ("", "auto"):
                pools_out.append("")
            elif ok(cidr):
                pools_out.append(cidr)
            else:
                self.add_error(
                    "corpnet_extra_prefixes",
                    _("Base subnet %(c)s is not on an exchange network of the chosen cluster.") % {"c": cidr},
                )
                pools_out.append("")
        d["corpnet_pool_list"] = ([main] if d.get("corpnet_main_prefix") else []) + pools_out

    def clean_pool(self):
        key = self.cleaned_data.get("pool")
        pool = self.pools.get(key)
        site = self.data.get(self.add_prefix("site"))
        if pool is None or (site and pool["site"] != site):
            raise forms.ValidationError(_("Choose a resource pool of the selected site."))
        return key

    def _resources(self, d):
        """vCPU, RAM and GPUs: free for a CPU pool, in whole bundles for a GPU pool.

        On a GPU pool the operator chooses bundles and nothing else: vCPU and RAM follow from the bundle, and the
        number is capped by the cards the cluster still has free (owner 2026-11-09).
        """
        pool = self.pools.get(d.get("pool"))
        if pool and pool["gpu"]:
            n = d.get("gpu") or 0
            if n < 1:
                self.add_error("gpu", _("Choose how many GPU bundles."))
                return
            free = pool["gpu"].get("free")
            if free is not None and n > free:
                self.add_error("gpu", _("Only %(free)s bundles are free on this cluster.") % {"free": free})
                return
            d["cpu"], d["ram_gb"] = n * pool["gpu"]["cores"], n * pool["gpu"]["ram_gb"]
        else:
            d["gpu"] = 0
            for f in ("cpu", "ram_gb"):
                if not d.get(f):
                    self.add_error(f, _("This field is required."))

    def tenant_kind(self):
        return self.cleaned_data.get("kind") or TenantKind.CLOUD

    def order(self):
        d = self.cleaned_data
        conns = [c for c in ("inet", "corpnet", "l2") if c in (d.get("connections") or [])]
        from inventory.models import Platform

        pkey, quota = d["pool"].split("/", 1)
        plat = Platform.objects.select_related("site").get(key=pkey)
        o = {
            "platform": pkey,
            "frontend": plat.frontend,
            "site": plat.site.code,
            "cluster": quota,
            "cpu": d["cpu"],
            "ram_gb": d["ram_gb"],
            "gpu": d.get("gpu") or 0,
            "disk_gb": d["disk_gb"],
            "images_gb": __import__("tenants.models", fromlist=["default_split"]).default_split(d["disk_gb"])[0],
            "files_gb": __import__("tenants.models", fromlist=["default_split"]).default_split(d["disk_gb"])[1],
            "backups_gb": d["backups_gb"] if d.get("backups_gb") is not None else DEFAULT_RESOURCES["backups_gb"],
            "connections": conns,
            "note": d.get("note") or "",
        }
        if "inet" in conns:
            o.update(
                inet_mbps=d["inet_mbps"],
                inet_main_prefix=d["inet_main_prefix"],
                igw_type=d.get("igw_type") or Connectivity.Gateway.VYOS,
                inet_extra_prefixes=d.get("inet_extra_list") or [],
                inet_main_pool=d.get("inet_main_pool") or "",
                inet_extra_pools=d.get("inet_extra_pool_list") or [],
            )
        if "corpnet" in conns:
            o["corpnet_prefixes"] = d.get("corpnet_list") or []
            o["corpnet_pools"] = d.get("corpnet_pool_list") or []
        return o


class ResourceRowForm(forms.Form):
    """One quota record of a tenant, edited by a resource manager or the super admin."""

    key = forms.CharField(widget=forms.HiddenInput, required=False)  # "<site>/<quota code>" of an existing record
    placement = forms.ChoiceField(label=_("Site and cluster"), required=False)
    cpu = forms.IntegerField(label=_("vCPU"), min_value=0, max_value=100000)
    ram_gb = forms.IntegerField(label=_("RAM, GB"), min_value=0, max_value=10000000)
    gpu = forms.IntegerField(label=_("GPU"), min_value=0, max_value=1000, required=False)
    disk_gb = forms.IntegerField(label=_("Storage, GB"), min_value=0, max_value=100000000)
    backups_gb = forms.IntegerField(label=_("Backups, GB"), min_value=0, max_value=100000000, required=False)
    remove = forms.BooleanField(label=_("Remove"), required=False)

    FIELDS = ("cpu", "ram_gb", "gpu", "disk_gb", "backups_gb")

    def __init__(self, *args, placements=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["placement"].choices = [("", "—")] + list(placements)

    def clean(self):
        d = super().clean()
        if not d.get("key") and not d.get("placement") and self.has_changed():
            self.add_error("placement", _("Choose where the new resources go."))
        return d


def tenant_placements(tenant):
    """Clusters a tenant may get resources on: orderable clusters of its front end (all, if it has none yet)."""
    from inventory.models import Cluster

    rows = [(c, c.platform) for c in Cluster.objects.select_related("platform", "platform__site") if c.orderable]
    fe = (tenant.order or {}).get("frontend")
    if not fe:
        keys = {(r.site, r.cluster) for r in tenant.current_quota_records()}
        fe = next((p.frontend for c, p in rows if (p.site.code, c.order_code) in keys), None)
    return sorted(
        (f"{p.site.code}/{c.order_code}", f"{p.site.code} · {c.name}") for c, p in rows if not fe or p.frontend == fe
    )


class SshKeyForm(forms.Form):
    default_ssh_key = forms.CharField(
        label=_("Default ssh public key"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "class": "mono", "placeholder": "ssh-ed25519 AAAA… you@host"}),
        help_text=_("Offered for the igw of every new project; you can still give another key in each order."),
    )

    def clean_default_ssh_key(self):
        return clean_ssh_key(self.cleaned_data.get("default_ssh_key"))


class TenantPasswordForm(forms.Form):
    password = forms.CharField(label=_("New password"))
    password2 = forms.CharField(label=_("Repeat it"), widget=forms.PasswordInput(render_value=False))

    def __init__(self, *args, tenant=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tenant, self.user = tenant, user
        from accounts.policy import PolicyPasswordInput

        self.fields["password"].widget = PolicyPasswordInput()

    def clean(self):
        from . import passwords

        d = super().clean()
        p = d.get("password") or ""
        if p and p != d.get("password2"):
            self.add_error("password2", _("The passwords differ."))
            return d
        t = self.tenant
        words = passwords.words_for(t.name, t.organization, self.user, extra=[t.igw_username])
        bad = passwords.problems(p, words, t.password_history())
        if p and bad:
            self.add_error("password", _("The password needs: %(what)s.") % {"what": "; ".join(bad)})
        return d


class ResourceChangeForm(forms.Form):
    """A tenant admin asks for other resources on the tenant's resource pool."""

    gpu = forms.IntegerField(
        label=_("GPU bundles"),
        min_value=1,
        max_value=64,
        required=False,
        help_text=_(
            "GPU clusters are ordered in bundles: a bundle is one GPU with its share of the host's CPU and RAM."
        ),
    )
    cpu = forms.IntegerField(label=_("vCPU"), min_value=1, max_value=1024, required=False)
    ram_gb = forms.IntegerField(label=_("RAM, GB"), min_value=1, max_value=8192, required=False)
    disk_gb = forms.IntegerField(
        label=_("Storage, GB"),
        min_value=10,
        max_value=1048576,
        help_text=_("VM disks, images and files together; the split stays as it is when it fits."),
    )
    note = forms.CharField(
        label=_("Comment"), required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args, pool=None, floor=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.pool = pool or {}
        if self.pool.get("gpu"):
            del self.fields["cpu"], self.fields["ram_gb"]
            self.fields["gpu"].required = True
        else:
            del self.fields["gpu"]
            self.fields["cpu"].required = self.fields["ram_gb"].required = True
        # owner 2026-11-12: nothing below what the project uses now (tenants/quota_check.floor)
        self.floor = {k: v for k, v in (floor or {}).items() if v and k in self.fields}
        for k, v in self.floor.items():
            f = self.fields[k]
            f.widget.attrs["min"] = max(v, f.min_value or 0)
            hint = _("In use now: %(n)s.") % {"n": v}
            f.help_text = f"{f.help_text} {hint}".strip() if f.help_text else hint

    def clean(self):
        d = super().clean()
        for k, v in self.floor.items():
            if d.get(k) is not None and d[k] < v:
                self.add_error(k, _("Not below what is in use now: %(n)s.") % {"n": v})
        return d

    def values(self, rec=None):
        from .models import fit_split

        d = self.cleaned_data
        g = self.pool.get("gpu")
        if g and rec is not None and rec.gpu and d["gpu"] == rec.gpu:
            # the same bundles: the pool keeps its vCPU and RAM as they are, which may differ from standard bundles
            # (target-ai, 2026-11-14: 2 GPUs with 768 GB; a storage-only change must not recount RAM as 2 x 350 GB)
            cpu, ram, gpu = rec.cpu, rec.ram_gb, rec.gpu
        elif g:
            cpu, ram, gpu = d["gpu"] * g["cores"], d["gpu"] * g["ram_gb"], d["gpu"]
        else:
            cpu, ram, gpu = d["cpu"], d["ram_gb"], 0
        images, files = fit_split(d["disk_gb"], getattr(rec, "images_gb", None), getattr(rec, "files_gb", None))
        # backups are not here: they are one number for the whole site (owner 2026-11-09), asked for once on the
        # site's first pool, so this change carries the record's current value through unchanged.
        return {
            "cpu": cpu,
            "ram_gb": ram,
            "gpu": gpu,
            "disk_gb": d["disk_gb"],
            "images_gb": images,
            "files_gb": files,
            "backups_gb": getattr(rec, "backups_gb", None) or 0,
        }


class BackupsChangeForm(forms.Form):
    """The backups quota of one **site**: one number shared by every resource pool the project has there.

    OpenNebula holds it as the group's backup datastore quota, which is one figure per platform - so a per-pool
    field let two pools of one site disagree, and whichever deployed last won (owner 2026-11-09).
    """

    backups_gb = forms.IntegerField(
        label=_("Backups, GB"), min_value=0, max_value=1048576, help_text=_("For every resource pool of this site.")
    )
    note = forms.CharField(
        label=_("Comment"), required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 2})
    )


class OrgUserForm(forms.Form):
    """Correction 15: resource managers and sales reps add the users of an organization themselves (an invitation is not
    enough): name, e-mail and a phone in the standard format (+7 701 2345678). Made without a portal login."""

    full_name = forms.CharField(label=_("Full name"), max_length=200)
    email = forms.EmailField(label=_("Email"))
    phone = phone_field()
    city = city_field()
    is_org_admin = forms.BooleanField(label=_("Organization admin"), required=False)

    def __init__(self, *args, user=None, org=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user, self.org = user, org
        if user is not None:
            self.fields["email"].disabled = True
            self.initial.update(
                full_name=user.full_name,
                email=user.email,
                phone=user.phone_display,
                city=user.city or "",
                is_org_admin=user.is_org_admin,
            )

    def clean_full_name(self):
        import re as _re

        name = " ".join((self.cleaned_data.get("full_name") or "").split())
        if len(name) < 2 or _re.search(r"[0-9@<>]", name):
            raise forms.ValidationError(_("Enter the user's full name."))
        return name

    def clean_email(self):
        if self.user is not None:
            return self.user.email
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(_("This email already exists in the portal."))
        return email

    def clean_phone(self):
        from accounts.kz import normalize_intl_phone

        return normalize_intl_phone(self.cleaned_data.get("phone") or "")

    def save(self, by):
        from accounts.models import Kind, audit

        d = self.cleaned_data
        if self.user is None:
            u = User.objects.create_user(
                d["email"],
                None,
                kind=Kind.CUSTOMER,
                full_name=d["full_name"],
                phone=d["phone"],
                city=d["city"],
                organization=self.org,
                is_org_admin=d["is_org_admin"],
                login_enabled=False,
            )
            audit(by, "customer_created", u, org=self.org.name, org_admin=u.is_org_admin)
            from .admins import fill_missing  # its tenants without an admin get one

            fill_missing(self.org, by)
            return u
        u = self.user
        changed = {k: d[k] for k in ("full_name", "phone", "city", "is_org_admin") if getattr(u, k) != d[k]}
        for k, v in changed.items():
            setattr(u, k, v)
        if changed:
            u.save(update_fields=list(changed))
            audit(by, "customer_changed", u, **{k: (v if k != "phone" else "changed") for k, v in changed.items()})
        return u


class PoolOrderForm(forms.Form):
    """Correction 15: order another resource pool — another cluster on a site the tenant has (vCPU, RAM or GPU bundles,
    storage; backups are the site's), or a cluster on a new site (with its backups and the connectivity of the tenant's
    type)."""

    pool = forms.ChoiceField(label=_("Resource pool"))
    gpu = forms.IntegerField(label=_("GPU bundles"), min_value=1, max_value=64, required=False)
    cpu = forms.IntegerField(label=_("vCPU"), min_value=1, max_value=1024, required=False)
    ram_gb = forms.IntegerField(label=_("RAM, GB"), min_value=1, max_value=8192, required=False)
    disk_gb = forms.IntegerField(label=_("Storage, GB"), min_value=10, max_value=1048576, initial=100)
    backups_gb = forms.IntegerField(
        label=_("Backups, GB"),
        min_value=0,
        max_value=1048576,
        required=False,
        initial=0,
        help_text=_("A new site only: backups are one number per site."),
    )
    inet_mbps = forms.IntegerField(
        label=_("Internet bandwidth, Mbps"),
        min_value=MBPS_MIN,
        max_value=MBPS_MAX,
        initial=MBPS_DEFAULT,
        required=False,
    )
    # owner 2026-11-09: a new site is ordered like a new project - speed, gateway type, base subnet and prefix.
    # The base subnet and the gateway type were missing here, so a new site could only ever take an auto subnet
    # and the project's gateway type, with no way to say otherwise.
    igw_type = forms.ChoiceField(
        label=_("Internet gateway"),
        required=False,
        choices=Connectivity.Gateway.choices,
        initial=Connectivity.Gateway.VYOS,
    )
    inet_main_pool = forms.ChoiceField(label=_("Main public subnet"), required=False)
    inet_main_prefix = forms.TypedChoiceField(
        label=_("Main public prefix"),
        coerce=int,
        required=False,
        initial=31,
        choices=[(n, f"/{n}") for n in PREFIX_LENGTHS],
    )
    corpnet_main_pool = forms.ChoiceField(label=_("Corporate base subnet"), required=False)
    corpnet_prefixes = forms.CharField(
        label=_("Corporate prefixes"), required=False, help_text=_("Prefix lengths separated by spaces, e.g. 28 29.")
    )
    note = forms.CharField(
        label=_("Comment"), required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        from .ordering import connections, corpnet_base_subnets, inet_base_subnets, prefix_lengths_for, resource_pools

        self.tenant = tenant
        self.fields["inet_main_prefix"].choices = [(n, f"/{n}") for n in prefix_lengths_for("inet")]
        self._base_choices, self._base_meta = inet_base_subnets()
        self.fields["inet_main_pool"].choices = [("", _("auto"))] + self._base_choices
        self._corp_base_choices, self._corp_base_meta = corpnet_base_subnets()
        self.fields["corpnet_main_pool"].choices = [("", _("auto"))] + self._corp_base_choices
        self.fields["igw_type"].initial = (tenant.order or {}).get("igw_type") or Connectivity.Gateway.VYOS
        have = {(r.site, r.cluster) for r in tenant.current_quota_records()}
        self.sites = {r.site for r in tenant.current_quota_records()} | {(tenant.order or {}).get("site")} - {None}
        self.pools = {p["key"]: p for p in resource_pools() if (p["site"], p["quota_code"]) not in have}
        self.fields["pool"].choices = [("", "—")] + [
            (k, f"{p['site']} · {p['label']}" + ("" if p["site"] in self.sites else " · " + str(_("new site"))))
            for k, p in self.pools.items()
        ]
        self.conns = connections(tenant.order or {})

    def clean(self):
        d = super().clean()
        p = self.pools.get(d.get("pool") or "")
        if p is None:
            self.add_error("pool", _("Choose a resource pool the project does not have yet."))
            return d
        d["p"], d["new_site"] = p, p["site"] not in self.sites
        if p.get("gpu"):
            if not d.get("gpu"):
                self.add_error("gpu", _("Enter the GPU bundles."))
        elif not d.get("cpu") or not d.get("ram_gb"):
            self.add_error("cpu", _("Enter vCPU and RAM."))
        if d["new_site"]:
            from .ordering import prefix_lengths_for

            site = p["site"]
            if "inet" in self.conns:
                if d.get("inet_mbps") is None or not d.get("inet_main_prefix"):
                    self.add_error("inet_mbps", _("A new site needs its internet: bandwidth and main prefix."))
                else:
                    inet_allowed = prefix_lengths_for("inet", site=site)
                    if d["inet_main_prefix"] not in inet_allowed:
                        self.add_error(
                            "inet_main_prefix",
                            _("/%(n)s is not offered; choose from %(list)s.")
                            % {"n": d["inet_main_prefix"], "list": " ".join(f"/{x}" for x in inet_allowed)},
                        )
            if "corpnet" in self.conns:
                try:
                    d["corpnet_list"] = _lengths(
                        d.get("corpnet_prefixes"), _("Corporate prefixes"), prefix_lengths_for("corpnet", site=site)
                    )
                    if not d["corpnet_list"]:
                        self.add_error("corpnet_prefixes", _("List at least one prefix."))
                except forms.ValidationError as exc:
                    self.add_error("corpnet_prefixes", exc)
        return d

    def change_values(self):
        from .models import fit_split

        d, p = self.cleaned_data, self.cleaned_data["p"]
        g = p.get("gpu")
        cpu, ram, gpu = (d["gpu"] * g["cores"], d["gpu"] * g["ram_gb"], d["gpu"]) if g else (d["cpu"], d["ram_gb"], 0)
        images, files = fit_split(d["disk_gb"], None, None)
        backups = d.get("backups_gb") or 0
        if not d["new_site"]:  # the site's backups stay as they are
            backups = next((r.backups_gb or 0 for r in self.tenant.current_quota_records() if r.site == p["site"]), 0)
        pool = {
            "kind": "new_site" if d["new_site"] else "add",
            "cluster_name": p["cluster"],
            "platform": p["key"].split("/")[0],
        }
        if d["new_site"]:
            pool["connectivity"] = {
                "types": [c for c in self.conns if c in ("inet", "corpnet", "l2")],
                "inet_mbps": d.get("inet_mbps"),
                "inet_main_prefix": d.get("inet_main_prefix"),
                "inet_main_pool": d.get("inet_main_pool") or "",
                "corpnet_main_pool": d.get("corpnet_main_pool") or "",
                "igw_type": d.get("igw_type") or "",
                "corpnet_prefixes": " ".join(str(x) for x in d.get("corpnet_list") or []),
            }
        return {
            "site": p["site"],
            "cluster": p["quota_code"],
            "cpu": cpu,
            "ram_gb": ram,
            "gpu": gpu,
            "disk_gb": d["disk_gb"],
            "images_gb": images,
            "files_gb": files,
            "backups_gb": backups,
            "note": d.get("note") or "",
            "pool": pool,
        }
