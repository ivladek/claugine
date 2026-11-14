"""Every page of the accounts app: signing in, registration, invitations, people, the profile, Settings.

The biggest module in the app, and deliberately flat: one view per address (`portal/urls.py`), each gated by
`accounts/utils.staff_required` or `customer_required`, each ending in a render or a redirect. Business rules do
not live here — they are in the modules the views call (`people.py`, `policy.py`, `roles.py`, `appliance.py`,
`superadmins.py`). **Settings posts are dispatched by the `section` field**: a handler that does not check its own
section becomes a catch-all and silently rewrites another form's fields.
"""

import re

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from tenants.models import (
    JoinRequest,
    Membership,
    Organization,
    OrgKind,
    PortalSettings,
    Registration,
    Tenant,
)
from tenants.naming import suggest_org_short_name
from tenants.ordering import describe as describe_order

from .forms import (
    InvitationAcceptForm,
    OTPForm,
    PersonForm,
    ProfileForm,
    RegistrationForm,
    SalesRepForm,
)
from .models import AuditLog, Invitation, Kind, Role, User, audit
from .utils import (
    new_totp_device,
    notify,
    site_link,
    staff_required,
    totp_device,
    totp_qr_svg,
    totp_secret,
    totp_uri,
    users_with_role,
    verify_totp,
)

VERIFY_SALT = "claugine.verify-email"
BACKEND = "accounts.backends.PortalBackend"


def _safe_next(request, default="dashboard"):
    nxt = request.POST.get("next") or request.GET.get("next")
    if nxt and url_has_allowed_host_and_scheme(
        nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return nxt
    return reverse(default)


# --- sign in / out ---------------------------------------------------------


def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = AuthenticationForm(request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.get_user()
        nxt = _safe_next(request)
        org = user.organization if user.is_customer and user.organization_id else None
        if org is not None and org.is_pending:  # correction 16: not before the organization is approved
            messages.error(
                request,
                _("The registration of %(n)s is waiting for approval. You will get an e-mail when you can sign in.")
                % {"n": org.name},
            )
            return render(request, "accounts/login.html", {"form": AuthenticationForm(request), "next": nxt})
        if user.totp_enabled:
            request.session.update(
                {"pre2fa_uid": user.pk, "pre2fa_backend": user.backend, "pre2fa_next": nxt, "pre2fa_attempts": 0}
            )
            return redirect("login_otp")
        login(request, user)
        audit(user, "login", user)
        return redirect(nxt)
    return render(request, "accounts/login.html", {"form": form, "next": request.GET.get("next", "")})


def login_otp_view(request):
    uid = request.session.get("pre2fa_uid")
    if not uid:
        return redirect("login")
    user = get_object_or_404(User, pk=uid, is_active=True, login_enabled=True)
    form = OTPForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if verify_totp(user, form.cleaned_data["code"]):
            backend = request.session.get("pre2fa_backend")
            nxt = request.session.get("pre2fa_next") or reverse("dashboard")
            for key in ("pre2fa_uid", "pre2fa_backend", "pre2fa_next", "pre2fa_attempts"):
                request.session.pop(key, None)
            login(request, user, backend=backend)
            audit(user, "login", user, second_factor=True)
            return redirect(nxt)
        attempts = request.session.get("pre2fa_attempts", 0) + 1
        request.session["pre2fa_attempts"] = attempts
        if attempts >= 5:
            request.session.flush()
            messages.error(request, _("Too many wrong codes. Sign in again."))
            return redirect("login")
        form.add_error("code", _("Wrong or expired code."))
    return render(request, "accounts/login_otp.html", {"form": form, "has_passkeys": user.passkeys.exists()})


@require_POST
def logout_view(request):
    logout(request)
    return redirect("home")


# --- customer self-registration ----------------------------------------------


def register_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    invite_token = request.GET.get("invite") or request.POST.get("invite") or ""
    inv = Invitation.objects.by_token(invite_token) if invite_token else None
    if inv is not None and (inv.purpose != Invitation.Purpose.ORGANIZATION or not inv.is_usable):
        return render(request, "accounts/link_invalid.html", status=400)
    form = RegistrationForm(request.POST or None, request.FILES or None, invitation=inv)
    from . import regcheck

    check = None
    if request.method == "POST" and inv is None:
        # owner 2026-10-29 20:30: what is entered is checked; a robot gets the usual answer and nothing is made
        check = regcheck.analyze(request.POST, regcheck.client_ip(request), regcheck.seconds_on_form(request.POST))
        if regcheck.is_robot(check):
            audit(None, "registration_blocked", None, **regcheck.log_fields(request.POST, check))
            return render(request, "accounts/register_done.html", {"email": (request.POST.get("email") or "")[:120]})
    if request.method == "POST" and form.is_valid() and inv is not None:
        return _register_invited(request, inv, form.cleaned_data)
    if request.method == "POST" and not form.is_valid() and check and "markup" in check["flags"]:
        audit(
            None,
            "registration_suspicious",
            None,
            errors=",".join(form.errors),
            **regcheck.log_fields(request.POST, check),
        )
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        with transaction.atomic():
            # A never-confirmed earlier customer attempt with the same email is replaced.
            User.objects.filter(email__iexact=d["email"], kind=Kind.CUSTOMER, email_verified=False).delete()
            user = User.objects.create_user(
                d["email"],
                d["password1"],
                kind=Kind.CUSTOMER,
                full_name=d["full_name"],
                phone=d["phone"],
                city=d["city"],
                photo=d["photo"],
                is_active=False,
                consent_at=timezone.now(),
            )
            Registration.objects.create(
                user=user,
                country=d["country"],
                tax_id=d["tax_id"],
                tax_id_type=d["tax_id_type"],
                organization_name=d["organization_name"],
                address=d.get("address") or "",
                website=d.get("website") or "",
                reg_check=check or {},
            )
        token = signing.dumps({"u": user.pk, "e": user.email}, salt=VERIFY_SALT)
        notify(
            [user],
            "verify_email",
            {
                "user": user,
                "link": site_link(reverse("verify_email", args=[token])),
                "hours": settings.REGISTRATION_TTL_HOURS,
            },
            _("Confirm your email"),
        )
        audit(None, "registration_started", user, **regcheck.log_fields(request.POST, check or {}))
        return render(request, "accounts/register_done.html", {"email": user.email})
    return render(
        request,
        "accounts/register.html",
        {
            "form": form,
            "invitation": inv,
            "invite_token": invite_token,
            "form_ts": regcheck.form_stamp(),
            "honeypot": regcheck.HONEYPOT,
            "ts_field": regcheck.TS_FIELD,
        },
    )


def _register_invited(request, inv, d):
    """Staff invited this person to register an organization: the email is proven by the link."""
    g = inv.grants or {}
    with transaction.atomic():
        User.objects.filter(email__iexact=d["email"], kind=Kind.CUSTOMER, email_verified=False).delete()
        user = User.objects.create_user(
            d["email"],
            d["password1"],
            kind=Kind.CUSTOMER,
            full_name=d["full_name"],
            phone=d["phone"],
            city=d["city"],
            photo=d["photo"],
            email_verified=True,
            consent_at=timezone.now(),
        )
        reg = Registration.objects.create(
            user=user,
            country=d["country"],
            tax_id=d["tax_id"],
            tax_id_type=d["tax_id_type"],
            organization_name=d["organization_name"],
            address=d.get("address") or "",
            website=d.get("website") or "",
        )
        rep = User.objects.filter(pk=g.get("sales_rep"), is_sales=True).first() if g.get("sales_rep") else None
        attach_registration(user, reg, kind=g.get("org_kind"), sales_rep=rep, invited_by=inv.invited_by)
        inv.accepted_at = timezone.now()
        inv.save(update_fields=["accepted_at"])
    audit(user, "invitation_accepted", user, purpose=inv.purpose, invitation=inv.pk)
    login(request, user, backend=BACKEND)
    return redirect("dashboard")


def domain_check_view(request):
    """Whether an email address may choose a corporate tenant (registration form helper)."""
    from django.http import JsonResponse

    return JsonResponse({"corporate": PortalSettings.get().is_corporate_email(request.GET.get("email", ""))})


def verify_email_view(request, token):
    try:
        data = signing.loads(token, salt=VERIFY_SALT, max_age=settings.REGISTRATION_TTL_HOURS * 3600)
    except signing.BadSignature:
        return render(request, "accounts/link_invalid.html", status=400)
    user = User.objects.filter(pk=data.get("u"), email=data.get("e"), kind=Kind.CUSTOMER).first()
    if user is None:
        return render(request, "accounts/link_invalid.html", status=400)
    if user.email_verified:
        messages.info(request, _("Your email is already confirmed. Please sign in."))
        return redirect("login")
    reg = getattr(user, "registration", None)
    org = None
    with transaction.atomic():
        user.email_verified = True
        user.is_active = True
        user.save(update_fields=["email_verified", "is_active"])
        if reg is not None:
            org = attach_registration(user, reg, pending=True)
    if org is not None and org.is_pending and user.organization_id == org.pk:
        # correction 16: a new organization waits for approval; nobody of it signs in until then
        from tenants.orgreg import registered

        registered(org, user)
        return render(request, "accounts/register_pending.html", {"org": org, "email": user.email})
    login(request, user, backend=BACKEND)
    return redirect("dashboard")


def attach_registration(user, reg, kind=None, sales_rep=None, invited_by=None, pending=False):
    """New IIN/BIN: a new organization with this user as its admin (tenants are ordered later).
    Its type is corporate when the admin's email domain is in the corporate list, or when staff invited it so.
    Known IIN/BIN: join request to its admins."""
    org = Organization.objects.filter(country=reg.country, tax_id=reg.tax_id).first()
    if org is None:
        if kind not in (OrgKind.CLOUD, OrgKind.CORP):
            kind = OrgKind.CORP if PortalSettings.get().is_corporate_email(user.email) else OrgKind.CLOUD
        try:
            with transaction.atomic():
                org = Organization.objects.create(
                    country=reg.country,
                    tax_id=reg.tax_id,
                    tax_id_type=reg.tax_id_type,
                    name=reg.organization_name,
                    short_name=suggest_org_short_name(reg.organization_name),
                    kind=kind,
                    sales_rep=sales_rep,
                    city=user.city or "",
                    address=reg.address,
                    website=reg.website,
                    registration="pending" if pending else "approved",
                    reg_check=reg.reg_check or {},
                )
        except IntegrityError:
            org = Organization.objects.get(country=reg.country, tax_id=reg.tax_id)
        else:
            user.organization = org
            user.is_org_admin = True
            user.save(update_fields=["organization", "is_org_admin"])
            reg.delete()
            audit(
                user,
                "organization_registered",
                org,
                tax_id=org.tax_id,
                kind=org.kind,
                invited_by=invited_by.email if invited_by else "",
            )
            if sales_rep is not None and sales_rep.login_enabled:
                notify(
                    [sales_rep],
                    "org_assigned",
                    {"org": org, "link": site_link(reverse("org_detail", args=[org.pk]))},
                    _("Organization assigned to you: %(name)s") % {"name": org.name},
                )
            return org
    # Known IIN/BIN: never a second organization. Its admins decide; staff are told.
    jr = JoinRequest.objects.create(organization=org, user=user)
    reg.delete()
    chk = reg.reg_check or {}
    audit(
        user,
        "join_requested",
        org,
        tax_id=org.tax_id,
        email=user.email,
        ip=chk.get("ip", ""),
        score=chk.get("score", 0),
        flags=",".join(chk.get("flags", [])),
    )
    ctx = {"org": org, "user": user, "join": jr}
    notify(
        list(org.admins),
        "join_request_owner",
        {**ctx, "link": site_link(reverse("my_org"))},
        _("Someone asks to join %(name)s") % {"name": org.name},
    )
    staff = list(users_with_role(Role.RESOURCE_MANAGER))
    if org.sales_rep:
        staff.append(org.sales_rep)
    notify(
        staff,
        "join_request_staff",
        {**ctx, "link": site_link(reverse("org_detail", args=[org.pk]))},
        _("Join request for existing IIN/BIN %(tax)s") % {"tax": org.tax_id},
    )
    return org


def notify_tenant_pending(tenant, user):
    recipients = []
    for role in tenant.missing_approvals():
        recipients += [u for u in tenant.approvers(role) if u not in recipients]
    notify(
        recipients,
        "tenant_pending",
        {
            "tenant": tenant,
            "user": user,
            "link": site_link(reverse("tenant_detail", args=[tenant.pk])),
            "order_summary": describe_order(tenant.order or {}),
        },
        _("Project waiting for approval: %(name)s") % {"name": tenant.title or tenant.name},
    )


# --- invitations -------------------------------------------------------------


def accept_invitation_view(request, token):
    inv = Invitation.objects.by_token(token)
    if inv is None or not inv.is_usable:
        return render(request, "accounts/link_invalid.html", status=400)
    if inv.purpose == Invitation.Purpose.ORGANIZATION:
        if request.user.is_authenticated:
            logout(request)
        return redirect(f"{reverse('register')}?invite={token}")
    if request.user.is_authenticated:
        logout(request)
    form = InvitationAcceptForm(request.POST or None, request.FILES or None, invitation=inv)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        profile = dict(full_name=d["full_name"], phone=d["phone"], city=d["city"])
        g = inv.grants or {}
        with transaction.atomic():
            if inv.purpose == Invitation.Purpose.ACTIVATE:
                user = inv.user
                for k, v in profile.items():
                    setattr(user, k, v)
                if d.get("photo"):
                    user.photo = d["photo"]
                user.set_password(d["password1"])
                user.login_enabled = True
                user.email_verified = True
                user.consent_at = timezone.now()
                user.save()
            else:
                User.objects.filter(email__iexact=d["email"], kind=Kind.CUSTOMER, email_verified=False).delete()
                if inv.purpose == Invitation.Purpose.STAFF:
                    roles = set(g.get("roles", []))
                    extra = dict(
                        kind=Kind.STAFF,
                        is_super_admin=bool(g.get("super_admin")),
                        is_staff=bool(g.get("super_admin")),
                        is_superuser=bool(g.get("super_admin")),
                        is_resource_manager=Role.RESOURCE_MANAGER in roles,
                        is_support=Role.SUPPORT in roles,
                    )
                else:
                    extra = dict(
                        kind=Kind.CUSTOMER,
                        organization=inv.organization,
                        is_org_admin=bool(g.get("org_admin")),
                        is_org_readonly=bool(g.get("read_only")) and not g.get("org_admin"),
                    )
                user = User.objects.create_user(
                    d["email"],
                    d["password1"],
                    photo=d["photo"],
                    email_verified=True,
                    consent_at=timezone.now(),
                    **profile,
                    **extra,
                )
                if inv.purpose == Invitation.Purpose.CUSTOMER:
                    for tid, role in (g.get("tenants") or {}).items():
                        tenant = Tenant.objects.filter(pk=tid, organization=inv.organization).first()
                        if tenant:
                            Membership.objects.get_or_create(user=user, tenant=tenant, defaults={"role": role})
            inv.accepted_at = timezone.now()
            inv.save(update_fields=["accepted_at"])
        audit(user, "invitation_accepted", user, purpose=inv.purpose, invitation=inv.pk)
        login(request, user, backend=BACKEND)
        if user.requires_2fa and not user.totp_enabled:
            messages.info(request, _("One more step: set up two-factor authentication."))
            return redirect("totp_setup")
        return redirect("dashboard")
    return render(request, "accounts/invitation_accept.html", {"form": form, "invitation": inv})


@require_POST
@login_required
def revoke_invitation_view(request, pk):
    inv = get_object_or_404(Invitation, pk=pk)
    user = request.user
    allowed = (
        user.is_super_admin
        or inv.invited_by_id == user.pk
        or (user.is_customer and user.is_org_admin and inv.organization_id == user.organization_id)
    )
    if not allowed:
        raise PermissionDenied
    if inv.is_usable:
        inv.revoked_at = timezone.now()
        inv.save(update_fields=["revoked_at"])
        audit(user, "invitation_revoked", inv, email=inv.email)
        from . import people

        if inv.user_id and inv.user.kind == Kind.STAFF and people.never_accepted(inv.user):
            people.clear_roles(inv.user)  # uninvited: no roles
    return redirect(_safe_next(request))


# --- people: platform users and tenant users ------------------------------------

USER_TABS = [("platform", _l("Platform")), ("tenant", _l("Organizations"))]  # owner 2026-10-29


@staff_required(super_only=True)
def staff_users_view(request):
    """Administration → Users: platform users (staff) and tenant users (customers), with their state and roles."""
    from tenants.models import Organization

    from . import people

    tab = request.GET.get("tab") or "platform"
    tab = "tenant" if tab in ("tenant", "customer") else "platform"
    show_archived = request.GET.get("archived") == "1"
    q = (request.GET.get("q") or "").strip().lower()
    rows = people.staff_rows(show_archived) if tab == "platform" else people.customer_rows(None, show_archived)
    if q:  # filter by name, email or organization

        def _hay(r):
            u = r.get("u")
            org = r["org"].name if r.get("org") else ""
            return " ".join([(u.full_name if u else ""), (u.email if u else r.get("email", "")), org]).lower()

        rows = [r for r in rows if q in _hay(r)]
    counts = {
        "platform": User.objects.filter(kind=Kind.STAFF, archived_at__isnull=True).count(),
        "tenant": (
            User.objects.filter(kind=Kind.CUSTOMER, archived_at__isnull=True).count()
            + len(people.staff_org_role_rows())
        ),
    }  # platform users shown as org users too
    tabs = [(k, label, counts[k]) for k, label in USER_TABS]
    removals = []
    if tab == "platform":
        from . import roles as R
        from . import superadmins

        R.ensure_complete(request.user)
        for req in superadmins.pending():
            ids = [u.pk for u in superadmins.deciders(req)]
            removals.append(
                {
                    "req": req,
                    "deciders": superadmins.deciders(req),
                    "confirmed": req.confirmed or [],
                    "approvals": superadmins.approvals(req),
                    "can_confirm": request.user.pk in ids and request.user.pk not in (req.confirmed or []),
                    "can_decline": request.user.pk in ids or request.user.pk == req.requested_by_id,
                }
            )
    orgs = Organization.objects.order_by("name") if tab == "tenant" else []
    return render(
        request,
        "staff/users.html",
        {
            "tab": tab,
            "tabs": tabs,
            "rows": rows,
            "show_archived": show_archived,
            "q": q,
            "archived_toggle": f"?tab={tab}" + ("" if show_archived else "&archived=1"),
            "staff_roles": people.STAFF_ROLES,
            "customer_roles": people.CUSTOMER_ROLES,
            "platform_columns": people.PLATFORM_ROLE_COLUMNS,
            "customer_columns": people.CUSTOMER_ROLE_COLUMNS,
            "person_form": PersonForm() if tab == "platform" else None,
            "orgs": orgs,
            "users_add": users_add_allowed(),
            "org_user_form": __import__("tenants.forms", fromlist=["OrgUserForm"]).OrgUserForm()
            if tab == "tenant"
            else None,
            "org_tenants": {
                o.pk: list(
                    o.tenants.filter(approval__in=["pending", "approved"]).exclude(
                        status__in=["saved", "archived", "deleting"]
                    )
                )
                for o in orgs
            },
            "tenants": [],
            "last_link": request.session.pop("last_invite_link", None),
            "removals": removals,
        },
    )


def _people_back(request, default="staff_users"):
    return redirect(_safe_next(request, default))


def _may_manage(actor, person):
    """Invite / lock / unlock a user (u12). The super admin manages everybody; for a customer of an organization, its
    manager, KAM or resource manager, or its own org admin."""
    if actor.is_super_admin:
        return True
    if person.kind != Kind.CUSTOMER or not person.organization_id:
        return False
    from tenants.permissions import can_manage_org_users

    return can_manage_org_users(actor, person.organization)


def _may_archive(actor, person):
    """Archive / unarchive a user (u12). The super admin for anyone; for a customer, the org's resource manager or its
    own org admin (a manager or KAM may lock but not archive)."""
    if actor.is_super_admin:
        return True
    if person.kind != Kind.CUSTOMER or not person.organization_id:
        return False
    from tenants.permissions import can_archive_org_users

    return can_archive_org_users(actor, person.organization)


def _sa_pending_message(request, person):
    """After a role change, report a pending or finished super-admin grant/removal (u12). Falls back to 'Roles saved.'"""
    req = getattr(person, "sa_removal", None)
    if req is None:
        messages.success(request, _("Roles saved."))
        return
    add = req.op == "add"
    if req.state == "done":
        messages.success(
            request,
            (_("%(e)s is now a super admin.") if add else _("The super admin role is taken from %(e)s."))
            % {"e": person.email},
        )
    else:
        tmpl = (
            _(
                "The super admin role for %(e)s is granted once %(n)s super admins agree (you count as one), within "
                "%(h)s hours; they are told."
            )
            if add
            else _(
                "The super admin role of %(e)s is taken away once %(n)s super admins agree (you count as one), "
                "within %(h)s hours; they are told."
            )
        )
        messages.info(request, tmpl % {"e": person.email, "n": req.required, "h": req.HOURS})


def _may_set_roles(actor, person):
    """Assign a customer's roles, including project admin (u12). Super admin; for a customer, the org's manager/RM or
    its own org admin (a KAM may manage users but not assign roles)."""
    if actor.is_super_admin:
        return True
    if person.kind != Kind.CUSTOMER or not person.organization_id:
        return False
    from tenants.permissions import can_assign_project_admin

    return can_assign_project_admin(actor, person.organization)


def users_add_allowed():
    """u03: Settings → Platform: users may be added before they are invited (else only invited)."""
    return PortalSettings.get().users_add


@require_POST
@staff_required(super_only=True)
def staff_invite_new_view(request):
    """u03: invite a new platform user at once: the record and the invitation with its roles."""
    form = PersonForm(request.POST)
    if not form.is_valid():
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
        return redirect(f"{reverse('staff_users')}?tab=platform")
    if not set(request.POST.getlist("roles")):
        messages.error(request, _("Choose the roles the user gets with the invitation."))
        return redirect(f"{reverse('staff_users')}?tab=platform")
    d = form.cleaned_data
    with transaction.atomic():
        person = User.objects.create_user(
            d["email"],
            None,
            kind=Kind.STAFF,
            login_enabled=False,
            full_name=d["full_name"],
            phone=d["phone"],
            city=d["city"],
        )
        audit(request.user, "person_added", person, email=person.email, invited=True)
    return invite_person_view(request, person.pk)


@require_POST
@staff_required(super_only=True)
def customer_add_view(request):
    """u03: add a project user to an organization (no sign-in until invited), from Users."""
    from tenants.forms import OrgUserForm
    from tenants.models import Organization

    if not users_add_allowed():
        raise PermissionDenied
    org = get_object_or_404(Organization, pk=request.POST.get("organization") or 0, archived_at__isnull=True)
    form = OrgUserForm(request.POST, org=org)
    if form.is_valid():
        u = form.save(request.user)
        messages.success(request, _("%(n)s is added. Invite them with their roles when ready.") % {"n": u.full_name})
    else:
        for name, errs in form.errors.items():
            for e in errs:
                messages.error(request, f"{form.fields[name].label}: {e}" if name in form.fields else e)
    return redirect(f"{reverse('staff_users')}?tab=tenant")


@require_POST
@staff_required(super_only=True)
def staff_person_add_view(request):
    """A platform user without roles: they get them with the invitation."""
    if not users_add_allowed():
        raise PermissionDenied
    form = PersonForm(request.POST)
    if form.is_valid():
        d = form.cleaned_data
        person = User.objects.create_user(
            d["email"],
            None,
            kind=Kind.STAFF,
            login_enabled=False,
            full_name=d["full_name"],
            phone=d["phone"],
            city=d["city"],
        )
        audit(request.user, "person_added", person, email=person.email)
        messages.success(
            request, _("%(email)s added. Invite them with their roles when ready.") % {"email": person.email}
        )
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
    return redirect(f"{reverse('staff_users')}?tab=platform")


def _customer_grants(request, org):
    tenants = list(org.tenants.filter(pk__in=[int(x) for x in request.POST.getlist("tenants") if x.isdigit()]))
    org_admin = request.POST.get("org_admin") == "on"
    read_only = request.POST.get("read_only") == "on" and not org_admin
    return org_admin, read_only, tenants


@require_POST
@login_required
def invite_person_view(request, pk):
    """Invite a person who is not active yet; platform users get their roles here (and only here)."""
    from . import people

    person = get_object_or_404(User, pk=pk)
    if not _may_manage(request.user, person) or person.pk == request.user.pk:
        raise PermissionDenied
    if person.archived_at:  # invited back: no roles but those given now
        person.archived_at, person.login_enabled = None, False
        person.save(update_fields=["archived_at", "login_enabled"])
        people.clear_roles(person)
        audit(request.user, "user_invited_back", person, email=person.email)
    if people.state(person) in ("active", "blocked"):
        messages.error(request, _("%(email)s has already accepted an invitation.") % {"email": person.email})
        return _people_back(request)
    if person.kind == Kind.STAFF:
        fields = set(request.POST.getlist("roles")) & {f for f, _l2 in people.STAFF_ROLES}
        if not fields:
            messages.error(request, _("Choose the roles the user gets with the invitation."))
            return _people_back(request)
        for f, _l2 in people.STAFF_ROLES:
            setattr(person, f, f in fields)
        person.save(update_fields=[f for f, _l2 in people.STAFF_ROLES])
    elif person.organization_id:
        org_admin, read_only, tenants = _customer_grants(request, person.organization)
        if not (org_admin or read_only or tenants):
            messages.error(request, _("Choose a role: org admin, read only or project admin of some projects."))
            return _people_back(request)
        err = people.set_customer_roles(request.user, person, org_admin, read_only, tenants)
        if err:
            messages.error(request, err)
            return _people_back(request)
    Invitation.objects.open().filter(user=person).update(revoked_at=timezone.now())
    inv, token = Invitation.issue(purpose=Invitation.Purpose.ACTIVATE, user=person, invited_by=request.user)
    link = site_link(reverse("accept_invitation", args=[token]))
    if person.is_staff_member:
        notify(
            [person.email],
            "staff_invitation",
            {"invitation": inv, "link": link, "role": person.role_display, "hours": settings.INVITATION_TTL_HOURS},
            _("Your invitation to the Example Cloud portal"),
        )
    else:
        notify(
            [person.email],
            "member_invitation",
            {
                "invitation": inv,
                "org": person.organization,
                "inviter": request.user,
                "link": link,
                "hours": settings.INVITATION_TTL_HOURS,
            },
            _("%(name)s invites you to the Example Cloud portal")
            % {"name": person.organization.name if person.organization else "Example Cloud"},
            allow_client=True,
        )  # an invitation carries the client's own sign-in link: always sent, even when e-mail to clients is off
    audit(request.user, "person_invited", person, email=person.email, roles=person.role_display)
    if request.user.is_super_admin:
        request.session["last_invite_link"] = link
    messages.success(request, _("Invitation sent to %(email)s.") % {"email": person.email})
    return _people_back(request)


@require_POST
@login_required
def customer_invite_view(request):
    """Invite a new tenant user (no account yet) into an organization with their roles."""
    from tenants.models import Organization
    from tenants.permissions import can_manage_org_users

    actor = request.user
    if actor.is_customer and actor.is_org_admin and actor.organization_id:
        org = actor.organization
    else:  # owner 2026-11-05 (doc-gaps §4): staff who manage the org's users (manager/KAM/RM) may invite too
        org = get_object_or_404(Organization, pk=request.POST.get("organization") or 0)
        if not (actor.is_super_admin or can_manage_org_users(actor, org)):
            raise PermissionDenied
    email = (request.POST.get("email") or "").strip().lower()
    try:
        from .forms import _email_taken

        if not email or "@" not in email:
            raise ValidationError(_("Give the email."))
        _email_taken(email)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return _people_back(request)
    org_admin, read_only, tenants = _customer_grants(request, org)
    if not (org_admin or read_only or tenants):
        messages.error(request, _("Choose a role: org admin, read only or project admin of some projects."))
        return _people_back(request)
    Invitation.objects.open().filter(email=email).update(revoked_at=timezone.now())
    grants = {"org_admin": org_admin, "read_only": read_only, "tenants": {str(t.pk): "tenant_admin" for t in tenants}}
    inv, token = Invitation.issue(
        purpose=Invitation.Purpose.CUSTOMER, email=email, organization=org, grants=grants, invited_by=actor
    )
    link = site_link(reverse("accept_invitation", args=[token]))
    notify(
        [email],
        "member_invitation",
        {"invitation": inv, "org": org, "inviter": actor, "link": link, "hours": settings.INVITATION_TTL_HOURS},
        _("%(name)s invites you to the Example Cloud portal") % {"name": org.name},
        allow_client=True,
    )  # the invitation link is the client's sign-in: always sent
    audit(actor, "customer_invited", org, email=email, grants=inv.summary)
    messages.success(request, _("Invitation sent to %(email)s.") % {"email": email})
    return _people_back(request)


@require_POST
@login_required
def person_roles_view(request, pk):
    """Change the roles of a platform user or a tenant user."""
    from . import people

    person = get_object_or_404(User, pk=pk, archived_at__isnull=True)
    if not _may_set_roles(request.user, person):
        raise PermissionDenied
    if person.kind == Kind.STAFF:
        errors = people.set_staff_roles(request.user, person, request.POST.getlist("roles"))
        for e in errors:
            messages.error(request, e)
        if not errors:
            _sa_pending_message(request, person)
    else:
        if person.pk == request.user.pk:
            raise PermissionDenied
        org_admin, read_only, tenants = _customer_grants(request, person.organization)
        err = people.set_customer_roles(request.user, person, org_admin, read_only, tenants)
        messages.error(request, err) if err else messages.success(request, _("Roles saved."))
    return _people_back(request)


@require_POST
@login_required
def person_role_set_view(request, pk):
    """u10 (owner 2026-11-03): toggle one role straight from the Users table (no dialog). For a customer's project
    admin the posted `tenants` list is the projects chosen in the picker (empty removes the role)."""
    from . import people

    person = get_object_or_404(User, pk=pk, archived_at__isnull=True)
    if not _may_set_roles(request.user, person):
        raise PermissionDenied
    role = request.POST.get("role") or ""
    on = request.POST.get("on") == "1"
    if person.kind == Kind.STAFF:
        if role in people.PLATFORM_ELIGIBILITY:  # org-admin / project-admin eligibility: a plain flag, no holder guard
            setattr(person, role, on)
            person.save(update_fields=[role])
            messages.success(request, _("Roles saved."))
        else:
            fields = {f for f, _l in people.STAFF_ROLES if getattr(person, f)}
            fields.add(role) if on else fields.discard(role)
            errs = people.set_staff_roles(request.user, person, list(fields))
            for e in errs:
                messages.error(request, e)
            if not errs:
                _sa_pending_message(request, person)
    else:
        cur = people.customer_roles(person)
        org_admin, read_only, tenant_admin_of = cur["org_admin"], cur["read_only"], list(cur["admin_of"])
        if role == "org_admin":
            org_admin = on
            read_only = read_only and not on
        elif role == "read_only":
            read_only = on
            if on:
                org_admin, tenant_admin_of = False, []
        elif role == "tenant_admin":
            from tenants.models import Tenant

            ids = [int(x) for x in request.POST.getlist("tenants") if x.isdigit()]
            tenant_admin_of = list(Tenant.objects.filter(pk__in=ids, organization=person.organization))
            if tenant_admin_of:
                read_only = False
        err = people.set_customer_roles(request.user, person, org_admin, read_only, tenant_admin_of)
        messages.error(request, err) if err else messages.success(request, _("Roles saved."))
    return _people_back(request)


@require_POST
@staff_required(super_only=True)
def super_admin_removal_view(request, pk, action):
    """Correction 16: confirm or decline taking the super admin role from a super admin."""
    from . import superadmins
    from .models import SuperAdminRemoval

    req = get_object_or_404(SuperAdminRemoval, pk=pk)
    add = req.op == "add"
    if action == "confirm":
        err = superadmins.confirm(request.user, req)
        req.refresh_from_db()
        if req.state == "done":
            ok = (_("%(e)s is now a super admin.") if add else _("The super admin role is taken from %(e)s.")) % {
                "e": req.target.email
            }
        else:
            ok = _("Confirmed. Waiting for the other super admins.")
    elif action == "decline":
        ok = (
            _("Declined: %(e)s does not get the super admin role.")
            if add
            else _("Declined: %(e)s stays a super admin.")
        ) % {"e": req.target.email}
        err = superadmins.decline(request.user, req)
    else:
        raise PermissionDenied
    if err:
        messages.error(request, err)
    else:
        messages.success(request, ok)
    return redirect(f"{reverse('staff_users')}?tab=platform")


@require_POST
@staff_required(super_only=True)
def person_edit_view(request, pk):
    """Owner 2026-10-29 19:57: complete a user who has not accepted an invitation (inactive or invited): e-mail, name,
    mobile, city. A changed e-mail revokes the open invitation (it went to the old address)."""
    from . import people
    from .forms import PersonEditForm

    person = get_object_or_404(User, pk=pk, archived_at__isnull=True)
    if (
        people.state(person) in ("active", "blocked") or person.kind != Kind.CUSTOMER
    ):  # owner 19:58: organization users only
        raise PermissionDenied
    form = PersonEditForm(request.POST, person=person)
    if form.is_valid():
        d = form.cleaned_data
        changed = [f for f in ("email", "full_name", "phone", "city") if (getattr(person, f) or "") != (d[f] or "")]
        old_email = person.email
        for f in changed:
            setattr(person, f, d[f])
        if changed:
            person.save(update_fields=changed)
            audit(
                request.user,
                "person_edited",
                person,
                fields=",".join(changed),
                old_email=old_email if "email" in changed else "",
            )
        if "email" in changed:
            n = Invitation.objects.open().filter(user=person).update(revoked_at=timezone.now())
            if n:
                messages.warning(
                    request, _("The invitation sent to %(e)s is revoked: invite again.") % {"e": old_email}
                )
        messages.success(request, _("%(e)s is saved.") % {"e": person.email})
    else:
        for name, errs in form.errors.items():
            for e in errs:
                messages.error(request, f"{form.fields[name].label}: {e}" if name in form.fields else e)
    return _people_back(request)


@require_POST
@login_required
def toggle_user_active_view(request, pk):
    from . import people

    person = get_object_or_404(User, pk=pk, archived_at__isnull=True)
    if not _may_manage(request.user, person):
        raise PermissionDenied
    err = people.block(request.user, person)
    people.report(
        request,
        err,
        _("%(email)s is unblocked.") % {"email": person.email}
        if person.is_active
        else _("%(email)s is blocked.") % {"email": person.email},
    )
    return _people_back(request)


@require_POST
@login_required
def delete_person_view(request, pk):
    """Owner 2026-11-03: users are never deleted, only archived — kept for the audit and resource history."""
    from . import people

    person = get_object_or_404(User, pk=pk)
    if not _may_archive(request.user, person):
        raise PermissionDenied
    err = people.archive(request.user, person)
    people.report(request, err, _("%(email)s is archived.") % {"email": person.email})
    return _people_back(request)


@require_POST
@login_required
def archive_person_view(request, pk):
    from . import people

    person = get_object_or_404(User, pk=pk, archived_at__isnull=True)
    if not _may_archive(request.user, person):
        raise PermissionDenied
    err = people.archive(request.user, person)
    people.report(request, err, _("%(email)s is archived.") % {"email": person.email})
    return _people_back(request)


@staff_required(super_only=True)
def staff_invitations_view(request):
    return redirect("staff_users")


@staff_required(super_only=True)
def staff_roles_view(request, pk):
    return redirect(f"{reverse('staff_users')}?tab=platform&sel=u-{pk}")


LOG_SECTIONS = [  # (key, label, target types)
    (
        "tenants",
        _l("Projects"),
        (
            "tenant",
            "tenantsshkey",
            "quotarecord",
            "connectivity",
            "networkrequest",
            "deployment",
            "tenantdeletion",
            "configcheck",
            "resourcechange",
            "l2channel",
            "prefixassignment",
        ),
    ),
    ("organizations", _l("Organizations"), ("organization", "joinrequest")),
    ("people", _l("Users"), ("user", "invitation")),
    ("secrets", _l("Secrets"), ("credential",)),
    (
        "infrastructure",
        _l("Infrastructure"),
        (
            "site",
            "host",
            "serverprofile",
            "storage",
            "storagevolume",
            "network",
            "networkrange",
            "peergroup",
            "platform",
            "platformdatastore",
            "platformnetwork",
            "cluster",
            "platformcontrol",
            "configtemplate",
            "revision",
            "delegatedsubnet",
        ),
    ),
    ("tasks", _l("Tasks"), ("task",)),
    ("settings", _l("Settings"), ("portalsettings",)),
    ("content", _l("Content"), ("article", "servicecomponent")),
]


def log_section(target_type):
    for key, _label, types in LOG_SECTIONS:
        if target_type in types:
            return key
    return "other"


def filtered_logs(qs, g):
    """The log filters of both portals: time range, section, user, object, text. Returns (entries, filters)."""
    import datetime

    section = g.get("section", "")
    sections = {k: types for k, _l2, types in LOG_SECTIONS}
    if section in sections:
        qs = qs.filter(target_type__in=sections[section])
    elif section == "other":
        qs = qs.exclude(target_type__in=[t for types in sections.values() for t in types])
    dates = {}
    for name in ("from", "to"):
        try:
            dates[name] = datetime.date.fromisoformat(g.get(name, ""))
        except ValueError:
            dates[name] = None
    if dates["from"]:
        qs = qs.filter(created_at__date__gte=dates["from"])
    if dates["to"]:
        qs = qs.filter(created_at__date__lte=dates["to"])
    obj = g.get("object", "") or (f"tenant:{g['tenant']}" if g.get("tenant", "").isdigit() else "")
    if ":" in obj:
        ttype, tid = obj.split(":", 1)
        if ttype == "organization" and tid.isdigit():  # an organization's history: its tenants and people too
            from tenants.models import Tenant

            tenants = [str(x) for x in Tenant.objects.filter(organization_id=int(tid)).values_list("pk", flat=True)]
            people = [str(x) for x in User.objects.filter(organization_id=int(tid)).values_list("pk", flat=True)]
            qs = qs.filter(
                Q(target_type="organization", target_id=tid)
                | Q(target_type="tenant", target_id__in=tenants)
                | Q(target_type="user", target_id__in=people)
            )
        else:
            qs = qs.filter(target_type=ttype, target_id=tid)
    who = g.get("user", "") or g.get("who", "")
    if who.isdigit():
        qs = qs.filter(actor_id=int(who))
    q = (g.get("q") or "").strip()
    if q:
        # the log stores "tenant" where people now read "project" (owner 2026-11-14): either word finds it
        alt = re.sub("project", "tenant", q, flags=re.I)
        qs = qs.filter(
            Q(action__icontains=q)
            | Q(action__icontains=alt)
            | Q(target_type__icontains=q)
            | Q(target_type__icontains=alt)
            | Q(target_id=q)
            | Q(details__icontains=q)
        )
    return list(qs.select_related("actor")[:1000]), {
        "section": section,
        "object": obj,
        "user": who,
        "q": q,
        "from": g.get("from", ""),
        "to": g.get("to", ""),
    }


def logs_page(request, qs, objects, people, title_scope=""):
    from tenants.models import Tenant

    entries, f = filtered_logs(qs, request.GET)
    tenants = {
        str(t.pk): t
        for t in Tenant.objects.filter(
            pk__in=[e.target_id for e in entries if e.target_type == "tenant" and e.target_id.isdigit()]
        )
    }
    for e in entries:
        e.section = log_section(e.target_type)
        e.tenant = tenants.get(e.target_id) if e.target_type == "tenant" else None
    return render(
        request,
        "staff/audit.html",
        {
            "entries": entries,
            "sections": [(k, label) for k, label, _t in LOG_SECTIONS] + [("other", _("Other"))],
            "f": f,
            "objects": objects,
            "people": people,
            "days": PortalSettings.get().log_days,
            "scope": title_scope,
        },
    )


@staff_required(super_only=True)
def audit_log_view(request):
    """Administration → Logs: every log entry of the portal, filtered by time, section, user, object and text."""
    from inventory.models import Secret
    from tenants.models import Organization, Tenant

    objects = [
        (
            _("Projects"),
            [(f"tenant:{t.pk}", str(t.group_name or t)) for t in Tenant.objects.order_by("group_name", "name")],
        ),
        (_("Organizations"), [(f"organization:{o.pk}", o.name) for o in Organization.objects.order_by("name")]),
        (
            _("Secrets"),
            [(f"secret:{c.pk}", f"{c.get_kind_display()}: {c.name}") for c in Secret.objects.order_by("kind", "name")],
        ),
    ]
    people = User.objects.filter(pk__in=AuditLog.objects.exclude(actor=None).values("actor").distinct()).order_by(
        "email"
    )
    return logs_page(request, AuditLog.objects.all(), objects, people)


@staff_required(super_only=True)
def tasks_view(request):
    """Administration → Tasks: long work in the background and its state."""
    from .tasks import all_tasks

    days = PortalSettings.get().task_days or 10
    return tasks_page(request, all_tasks(days), days)


def tasks_page(request, rows, days, customer=False, tenants=None):
    """Tasks of both portals: state and tenant filters; customers see the steps without the platform's details."""
    state = request.GET.get("state", "")
    if state in ("running", "done", "failed"):
        rows = [r for r in rows if (r["state"] in ("done", "deployed") if state == "done" else r["state"] == state)]
    tenant = request.GET.get("tenant", "")
    if tenant.isdigit():
        rows = [r for r in rows if r["tenant"] is not None and str(r["tenant"].pk) == tenant]
    return render(
        request,
        "staff/tasks.html",
        {
            "rows": rows,
            "days": days,
            "state": state,
            "tenant": tenant,
            "customer": customer,
            "tenants": tenants,
            "any_running": any(r["state"] == "running" and not r["stale"] for r in rows),
        },
    )


# --- sales rep directory -----------------------------------------------------


@staff_required(Role.RESOURCE_MANAGER)
def sales_reps_view(request):
    reps = User.objects.filter(is_sales=True).order_by("full_name", "email")
    can_edit = request.user.is_super_admin
    rows = [
        (
            r,
            list(Organization.objects.filter(sales_rep=r).order_by("name")),
            SalesRepForm(instance=r, auto_id=f"rep{r.pk}_%s") if can_edit else None,
        )
        for r in reps
    ]
    return render(
        request,
        "staff/sales_reps.html",
        {
            "rows": rows,
            "settings_obj": PortalSettings.get(),
            "can_edit": can_edit,
            "new_form": SalesRepForm(auto_id="repnew_%s") if can_edit else None,
        },
    )


@staff_required(super_only=True)
def sales_rep_edit_view(request, pk=None):
    rep = get_object_or_404(User, pk=pk, is_sales=True) if pk else None
    form = SalesRepForm(request.POST or None, instance=rep)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        if rep is None:
            rep = User.objects.create_user(
                d["email"],
                None,
                kind=Kind.STAFF,
                is_sales=True,
                login_enabled=False,
                full_name=d["full_name"],
                phone=d["phone"],
                city=d["city"],
            )
            audit(request.user, "sales_rep_added", rep, email=rep.email)
        else:
            rep.full_name, rep.phone, rep.city = d["full_name"], d["phone"], d["city"]
            rep.save(update_fields=["full_name", "phone", "city"])
            audit(request.user, "sales_rep_updated", rep)
        messages.success(request, _("Saved."))
        return redirect("sales_reps")
    return render(request, "staff/sales_rep_edit.html", {"form": form, "rep": rep})


@require_POST
@staff_required(super_only=True)
def sales_rep_delete_view(request, pk):
    """A directory entry nobody refers to (no organization, never signed in) can be deleted."""
    rep = get_object_or_404(User, pk=pk, is_sales=True)
    used = Organization.objects.filter(sales_rep=rep).exists()
    if used or rep.login_enabled or rep.last_login:
        messages.error(
            request,
            _("%(email)s is used: it is the sales rep of organizations or has signed in.") % {"email": rep.email},
        )
        return redirect("sales_reps")
    email = rep.email
    try:
        Invitation.objects.filter(user=rep).delete()
        rep.delete()
    except Exception:  # noqa: BLE001 - protected by other references
        messages.error(request, _("%(email)s is still referred to.") % {"email": email})
        return redirect("sales_reps")
    audit(request.user, "sales_rep_deleted", None, email=email)
    messages.success(request, _("%(email)s is deleted.") % {"email": email})
    return redirect("sales_reps")


@require_POST
@staff_required(super_only=True)
def sales_rep_invite_view(request, pk):
    rep = get_object_or_404(User, pk=pk, is_sales=True, login_enabled=False)
    Invitation.objects.open().filter(user=rep).update(revoked_at=timezone.now())
    inv, token = Invitation.issue(purpose=Invitation.Purpose.ACTIVATE, user=rep, invited_by=request.user)
    link = site_link(reverse("accept_invitation", args=[token]))
    notify(
        [rep.email],
        "staff_invitation",
        {"invitation": inv, "link": link, "role": Role.SALES_MANAGER.label, "hours": settings.INVITATION_TTL_HOURS},
        _("Your invitation to the Example Cloud portal"),
    )
    audit(request.user, "sales_rep_invited", rep)
    request.session["last_invite_link"] = link
    messages.success(request, _("Invitation sent to %(email)s.") % {"email": rep.email})
    return redirect("sales_reps")


def _can_sign_in(qs):
    return qs.filter(is_active=True, login_enabled=True)


APPROVAL_ROLES = (
    ("rm", "rm_approval_required", "is_resource_manager"),
    ("sales", "sales_approval_required", "is_sales"),
    ("support", "support_approval_required", "is_support"),
)


def approvers_of(key):
    """The active platform users who can sign in and hold the role of an approval row."""
    flag = dict((k, f) for k, _s, f in APPROVAL_ROLES)[key]
    return list(
        _can_sign_in(User.objects.filter(kind=Kind.STAFF, archived_at__isnull=True, **{flag: True})).order_by(
            "full_name", "email"
        )
    )


def approval_table(conf):
    """Settings → Platform → approvals (owner 2026-11-12): a role's request appears only when its box is ticked, and
    a box can be ticked only while somebody active holds the role. [{key, role, scope, switch, people}]."""
    labels = {
        "rm": (_("Resource manager"), _("all projects")),
        "sales": (_("Sales rep"), _("cloud projects")),
        "support": (_("Support engineer"), _("corporate projects")),
    }
    rows = []
    for key, switch, _flag in APPROVAL_ROLES:
        people = approvers_of(key)
        on = getattr(conf, switch)
        rows.append(
            {
                "key": key,
                "role": labels[key][0],
                "scope": labels[key][1],
                "switch": on,
                "people": people,
                "can_tick": bool(people) or on,  # nobody active: it cannot be switched on (off stays possible)
            }
        )
    return rows


@staff_required(super_only=True)
def mail_template_view(request, name):
    """corrections-18: edit one e-mail template (Settings → Mail), or reset it to the portal's own."""
    from django.http import Http404

    from . import mailtemplates as mt
    from .models import MailTemplate

    if name not in mt.names():
        raise Http404
    current = mt.override(name)
    text = current.body if current else mt.default_text(name)
    if request.method == "POST":
        if request.POST.get("reset"):
            MailTemplate.objects.filter(name=name).delete()
            audit(request.user, "mail_template_reset", None, template=name)
            messages.success(request, _("%(n)s: the portal's own text again.") % {"n": name})
            return redirect(reverse("portal_settings") + "?tab=mail#mail-templates")
        text = (request.POST.get("body") or "").replace("\r\n", "\n")
        err = mt.compile_error(text)
        if err:
            messages.error(request, _("Not saved: %(e)s") % {"e": err})
        elif text.strip() == mt.default_text(name).strip():
            MailTemplate.objects.filter(name=name).delete()
            messages.success(request, _("Saved (the same as the portal's own)."))
            return redirect(reverse("portal_settings") + "?tab=mail#mail-templates")
        else:
            MailTemplate.objects.update_or_create(name=name, defaults={"body": text, "updated_by": request.user})
            audit(request.user, "mail_template_saved", None, template=name)
            messages.success(request, _("%(n)s saved.") % {"n": name})
            return redirect(reverse("portal_settings") + "?tab=mail#mail-templates")
    return render(
        request,
        "staff/mail_template.html",
        {
            "name": name,
            "what": mt.WHAT.get(name, ""),
            "text": text,
            "edited": current,
            "default": mt.default_text(name),
        },
    )


@require_POST
@staff_required(super_only=True)
def portal_settings_values_view(request):
    """The kept values of Settings → Portal appliance for the "Show values" switch. Logged."""
    from django.http import JsonResponse

    obj = PortalSettings.get()
    out = {
        f: obj.get_secret(f)
        for f in ("mail_password", "backup_password", "mail_dkim_key", "backup_s3_secret")
        if obj.has_secret(f)
    }
    audit(request.user, "secret_revealed", obj, where="Settings → Portal appliance", fields=",".join(out))
    return JsonResponse(out)


SETTINGS_TABS = {
    "provider": _l("Provider"),
    "platform": _l("Platform"),
    "security": _l("Security"),  # corrections-18, u02
    "appearance": _l("Appearance"),
    "mail": _l("Mail"),
    "reports": _l("Reports"),
    "backups": _l("Backups"),
    "portal": _l("Portal"),
}  # u01: Data → Appearance, HAProxy → Portal (with the portal nodes and updates)
OLD_TABS = {
    "operations": "appearance",
    "data": "appearance",
    "appliance": "mail",
    "publishing": "portal",
    "haproxy": "portal",
    "approvals": "platform",
}  # u02: Approvals → Platform


def brand_logo_view(request):
    """The provider's logo (PNG; public: the sign-in page and the e-mails show it)."""
    from django.http import Http404, HttpResponse

    obj = PortalSettings.get()
    if not obj.logo:
        raise Http404
    r = HttpResponse(bytes(obj.logo), content_type="image/png")
    r["Cache-Control"] = "public, max-age=31536000" if request.GET.get("v") else "public, max-age=300"
    return r


def _haproxy_context(obj, posted=None, errors=None):
    """Settings → HAProxy: the values (or what was sent, with its errors), the sites and each site's options."""
    from . import haproxy, thisnode

    conf, codes = haproxy.get(obj), haproxy.site_codes()
    codes = codes + [c for c in conf["sites"] if c not in codes]
    val = (lambda k, d="": posted.get(k, d)) if posted is not None else None
    fields = {
        "main_name": haproxy.main_name(obj),
        "other_names": val("other_names") if val else conf["other_names"].replace(",", ", "),
        "le_email": val("le_email") if val else conf["le_email"],
    }
    ports = [
        (k, label, d, opens, val(f"port_{k}") if val else conf["ports"][k]) for k, label, d, opens in haproxy.SERVICES
    ]
    sites = []
    for c in codes:
        s = conf["sites"].get(c) or {}
        addr = val(f"one_addr_{c}") if val else s.get("one_addr", "")
        choices = haproxy.fe_choices(c)
        if addr and addr not in [v for v, _l in choices]:
            choices.append((addr, f"{addr} · " + str(_("not a VIP of this site's platforms"))))
        sites.append(
            {
                "code": c,
                "choices": choices,
                "one_addr": addr,
                "one_names": val(f"one_names_{c}") if val else s.get("one_names", "").replace(",", ", "),
                "nets": haproxy.public_nets(c, addr) or [],
            }
        )
    return {
        "hp": fields,
        "hp_ports": ports,
        "hp_sites": sites,
        "hp_errors": errors or {},
        "hp_preview": haproxy.preview(conf, codes, thisnode.read()) if not errors else [],
        "lb_state": _lb_state_view(),
    }


def _lb_state_view():
    """This VM's last automatic HAProxy run (accounts/appliance.apply_lb), for Settings → Portal and Platform."""
    import datetime as _dt

    from . import appliance

    st = appliance.lb_state()
    if not st.get("at"):
        return None
    return {**st, "when": timezone.localtime(_dt.datetime.fromtimestamp(float(st["at"]), tz=_dt.UTC))}


def _settings_back(request):
    tab = request.POST.get("tab") or request.GET.get("tab") or ""
    tab = OLD_TABS.get(tab, tab)
    return redirect(reverse("portal_settings") + (f"?tab={tab}" if tab in SETTINGS_TABS else ""))


@staff_required(super_only=True)
def portal_settings_view(request):
    obj = PortalSettings.get()
    bound_appliance = None
    if request.method == "POST" and request.POST.get("section") in (
        "publishing",
        "ha_op",
        "auto_update",
        "node_source",
        "add_node",
        "upgrade_enterprise",
        "install_now",
        "rotate_key",
    ):
        from . import ha_views

        return ha_views.post(request, obj)
    if request.method == "POST" and request.POST.get("section") == "client_mail":
        on = request.POST.get("client_mail") == "on"
        if on != obj.client_mail:
            obj.client_mail, obj.updated_by = on, request.user
            obj.save()
            audit(request.user, "setting_changed", obj, client_mail=on)
        messages.success(request, _("Settings saved."))
        return _settings_back(request)
    if request.method == "POST" and request.POST.get("section") == "countries":
        # owner 2026-11-12: "need to have ability to define allowed countries"
        from .taxid import countries_choices

        known = {c for c, _n in countries_choices()}
        home = (request.POST.get("home_country") or "").upper()
        if home not in known:
            messages.error(request, _("Choose the home country."))
            return _settings_back(request)
        allowed = sorted({c.upper() for c in request.POST.getlist("allowed_countries") if c.upper() in known} - {home})
        if (home, allowed) != (obj.home_country, list(obj.allowed_countries or [])):
            obj.home_country, obj.allowed_countries = home, allowed
            obj.updated_by = request.user
            obj.save(update_fields=["home_country", "allowed_countries", "updated_by"])
            audit(request.user, "setting_changed", obj, home_country=home, allowed_countries=",".join(allowed))
            messages.success(request, _("Settings saved."))
        return _settings_back(request)
    if request.method == "POST" and request.POST.get("section") == "provider":
        from .branding import DEFAULT, to_png

        changed = {}
        name = " ".join((request.POST.get("provider_name") or "").split())[:100] or DEFAULT
        if name != obj.provider_name:
            obj.provider_name = changed["provider_name"] = name
        f = request.FILES.get("logo")
        if f:
            if f.size > 5 * 1024 * 1024:
                messages.error(request, _("The logo: at most 5 MB."))
                return _settings_back(request)
            try:
                obj.logo = to_png(f)
            except ValueError:
                messages.error(request, _("The logo: a PNG or JPEG image."))
                return _settings_back(request)
            obj.logo_updated_at = timezone.now()
            changed["logo"] = f"uploaded ({f.name}, kept as PNG)"
        elif request.POST.get("logo_remove") == "on" and obj.logo:
            obj.logo, obj.logo_updated_at = None, None
            changed["logo"] = "removed"
        if changed:
            obj.updated_by = request.user
            obj.save()
            audit(request.user, "setting_changed", obj, **changed)
        messages.success(request, _("Settings saved."))
        return _settings_back(request)
    if request.method == "POST" and request.POST.get("section") == "notify_clients":
        on = request.POST.get("notify_clients") == "on"  # correction 16: the contract and POC end notices
        if on != obj.notify_clients:
            obj.notify_clients, obj.updated_by = on, request.user
            obj.save()
            audit(request.user, "setting_changed", obj, notify_clients=on)
        messages.success(request, _("Settings saved."))
        return _settings_back(request)
    if request.method == "POST" and request.POST.get("section") == "fe_backup":  # u04/u06: the platforms' front ends
        import re as _re
        from datetime import time as _time

        P = request.POST
        on = P.get("fe_backup") == "on"
        t = (P.get("fe_backup_time") or "05:25").strip()
        if not _re.match(r"^([01]\d|2[0-3]):[0-5]\d$", t):
            messages.error(request, _("The time: HH:MM."))
            return _settings_back(request)

        def _int(name, lo, hi, default):
            try:
                return min(max(int(P.get(name) or default), lo), hi)
            except ValueError:
                return default

        dirs_ = "\n".join(d.strip() for d in (P.get("fe_backup_dirs") or "").splitlines() if d.strip())
        if any(not d.startswith("/") for d in dirs_.splitlines()):
            messages.error(request, _("The folders must be absolute paths."))
            return _settings_back(request)
        path = (P.get("fe_backup_path") or "").strip()
        if path and not path.startswith("/"):
            messages.error(request, _("The local folder must be an absolute path."))
            return _settings_back(request)
        fe_local = P.get("fe_backup_local") == "on"  # u08: each destination has its own switch
        s3_on = P.get("fe_backup_s3") == "on"
        use_portal = P.get("fe_backup_s3_use_portal") == "on"  # u07: reuse the portal's S3 settings
        on = fe_local or s3_on  # the block is active if either destination is on
        if fe_local and not path:
            messages.error(request, _("Set a local folder for the platform backups, or switch the local copy off."))
            return _settings_back(request)
        # the portal's archive password serves if the platforms have none of their own
        if on and not (
            P.get("fe_backup_password") or obj.has_secret("fe_backup_password") or obj.has_secret("backup_password")
        ):
            messages.error(request, _("A backup password is required."))
            return _settings_back(request)
        if (
            s3_on
            and not use_portal
            and not ((P.get("fe_backup_s3_endpoint") or "").strip() and (P.get("fe_backup_s3_bucket") or "").strip())
        ):
            messages.error(request, _("Fill the platforms' S3 settings, or tick 'use the Portal's S3 settings'."))
            return _settings_back(request)
        changed = {}
        new = {
            "fe_backup": on,
            "fe_backup_local": fe_local,
            "fe_backup_platforms": P.getlist("fe_backup_platforms"),
            "fe_backup_path": path,
            "fe_backup_time": _time(*map(int, t.split(":"))),
            "fe_backup_keep_days": _int("fe_backup_keep_days", 1, 3650, 10),
            "fe_backup_dirs": dirs_,
            "fe_backup_s3": s3_on,
            "fe_backup_s3_use_portal": use_portal,
            "fe_backup_s3_endpoint": (P.get("fe_backup_s3_endpoint") or "").strip(),
            "fe_backup_s3_region": (P.get("fe_backup_s3_region") or "us-east-1").strip(),
            "fe_backup_s3_bucket": (P.get("fe_backup_s3_bucket") or "").strip(),
            "fe_backup_s3_access_key": (P.get("fe_backup_s3_access_key") or "").strip(),
            "fe_backup_s3_style": P.get("fe_backup_s3_style")
            if P.get("fe_backup_s3_style") in ("path", "virtual")
            else "path",
        }
        from . import edition as _edn

        if _edn.ee("s3") is None:  # Standard: no S3 here — leave whatever is stored alone (editions split step 4)
            new = {k: v for k, v in new.items() if not k.startswith("fe_backup_s3")}
        for k, v in new.items():
            if getattr(obj, k) != v:
                setattr(obj, k, v)
                changed[k] = "list" if isinstance(v, list) else str(v)
        if P.get("fe_backup_password"):
            obj.set_secret("fe_backup_password", P["fe_backup_password"])
            changed["fe_backup_password"] = "set"
        if P.get("fe_backup_s3_secret") and _edn.ee("s3") is not None:
            obj.set_secret("fe_backup_s3_secret", P["fe_backup_s3_secret"])
            changed["fe_backup_s3_secret"] = "set"
        if changed:
            obj.updated_by = request.user
            obj.save()
            audit(
                request.user,
                "setting_changed",
                obj,
                **{k: v for k, v in changed.items() if "secret" not in k and "password" not in k},
            )
        messages.success(request, _("Settings saved."))
        return _settings_back(request)
    if request.method == "POST" and request.POST.get("section") == "fe_backup_now":
        PortalSettings.objects.filter(pk=obj.pk).update(fe_backup_requested_at=timezone.now())
        audit(request.user, "platform_backup_requested", obj)
        messages.success(request, _("The platform backups start within 5 minutes; the result shows here."))
        return _settings_back(request)
    if request.method == "POST" and request.POST.get("section") == "users_add":  # u03
        on = request.POST.get("users_add") == "on"
        if on != obj.users_add:
            obj.users_add, obj.updated_by = on, request.user
            obj.save()
            audit(request.user, "setting_changed", obj, users_add=on)
        messages.success(request, _("Settings saved."))
        return _settings_back(request)
    if request.method == "POST" and request.POST.get("section") == "lists":
        try:
            v = min(max(int(request.POST.get("list_rows") or 10), 5), 100)
        except ValueError:
            v = 10
        if v != obj.list_rows:
            obj.list_rows, obj.updated_by = v, request.user
            obj.save()
            audit(request.user, "setting_changed", obj, list_rows=v)
        messages.success(request, _("Settings saved."))
        return _settings_back(request)
    if request.method == "POST" and request.POST.get("section") == "igw_ports":  # owner 2026-10-31
        from tenants.models import Tenant

        lo, hi = Tenant.IGW_PORT_LIMITS
        try:
            a, b = int(request.POST.get("igw_port_min") or 0), int(request.POST.get("igw_port_max") or 0)
        except ValueError:
            a = b = 0
        if not (lo <= a < b <= hi):
            messages.error(
                request,
                _("The igw ssh ports: a range within %(lo)s-%(hi)s, the first lower than the last.")
                % {"lo": lo, "hi": hi},
            )
            return _settings_back(request)
        if (a, b) != (obj.igw_port_min, obj.igw_port_max):
            obj.igw_port_min, obj.igw_port_max, obj.updated_by = a, b, request.user
            obj.save()
            audit(request.user, "setting_changed", obj, igw_ports=f"{a}-{b}")
        messages.success(request, _("Settings saved."))
        return _settings_back(request)
    if request.method == "POST" and request.POST.get("section") == "reports":  # u01: Settings → Reports
        from . import reports, tasks

        conf, errors = reports.clean(request.POST)
        if errors:
            for k, e in errors.items():
                messages.error(request, f"{k}: {e}")
            return _settings_back(request)
        before = reports.config()
        if {k: before[k] for k in ("redirect_to", "list")} != conf:
            reports.save(conf)
            audit(request.user, "setting_changed", obj, reports=conf)
        key = request.POST.get("send_now")
        if reports.by_id(key):
            by = request.user
            tasks.start(
                f"report {key}",
                f"Report: {key}",
                lambda task: {"result": reports.send(key, by=by, how="now")[1]},
                user=request.user,
            )
            audit(request.user, "report_sent_now", obj, report=key)
            messages.success(request, _("Sending; the result shows in the report's sent history."))
            return redirect(reverse("portal_settings") + f"?tab=reports&report={key}")
        else:
            messages.success(request, _("Settings saved."))
        return _settings_back(request)
    if request.method == "POST" and request.POST.get("section") == "timers":  # corrections-18
        from . import timers

        changed = {}
        for key in timers.KEYS:
            raw = request.POST.get(f"minutes_{key}")
            if raw and raw.isdigit() and int(raw) != timers.state(key)["minutes"]:
                timers.set_minutes(key, int(raw))
                changed[f"timer_{key}"] = timers.state(key)["minutes"]
        if changed:
            audit(request.user, "setting_changed", obj, **changed)
        key = request.POST.get("refresh")
        if key in timers.KEYS:
            if timers.start(key, request.user) is None:
                messages.info(request, _("That timer is running already."))
            else:
                audit(request.user, "timer_refreshed", obj, timer=key)
                messages.success(request, _("Started; its result shows here when it is over (Tasks)."))
        elif changed:
            messages.success(request, _("Settings saved."))
        return _settings_back(request)
    if request.method == "POST" and request.POST.get("section") == "health":
        try:
            minutes = int(request.POST.get("health_interval_minutes") or 30)
        except ValueError:
            minutes = 30
        minutes = min(max(minutes, 5), 1440)
        if minutes != obj.health_interval_minutes:
            obj.health_interval_minutes, obj.updated_by = minutes, request.user
            obj.save()
            audit(request.user, "setting_changed", obj, health_interval_minutes=minutes)
        messages.success(request, _("Settings saved."))
        return _settings_back(request)
    if request.method == "POST" and request.POST.get("section") == "display":
        changed = {}
        for f, lo, hi, dflt in (
            ("task_days", 1, 3650, 10),
            ("log_days", 30, 36500, 500),
            ("tenant_history_days", 90, 36500, 730),
        ):
            try:
                v = int(request.POST.get(f) or dflt)
            except ValueError:
                v = dflt
            v = min(max(v, lo), hi)
            if v != getattr(obj, f):
                setattr(obj, f, v)
                changed[f] = v
        if changed:
            obj.updated_by = request.user
            obj.save()
            audit(request.user, "setting_changed", obj, **changed)
        messages.success(request, _("Settings saved."))
        return _settings_back(request)
    if request.method == "POST" and request.POST.get("section") == "network":
        from inventory.models import Site

        site = (request.POST.get("portal_site") or "").strip()
        if Site.objects.filter(code=site).exists() and site != obj.portal_site:
            obj.portal_site, obj.updated_by = site, request.user
            obj.save()
            audit(request.user, "setting_changed", obj, portal_site=site)
        messages.success(request, _("Settings saved."))
        return _settings_back(request)
    if request.method == "POST" and request.POST.get("section") == "passwords":
        from .policy import DEFAULTS, LIMITS, policy

        new = {}
        for k in ("length", "lower", "upper", "special", "digits", "history"):
            try:
                v = int(request.POST.get(k))
            except (TypeError, ValueError):
                messages.error(request, _("%(f)s: a whole number.") % {"f": k})
                return _settings_back(request)
            lo, hi = LIMITS[k]
            if not lo <= v <= hi:
                messages.error(request, _("%(f)s: from %(lo)s to %(hi)s.") % {"f": k, "lo": lo, "hi": hi})
                return _settings_back(request)
            new[k] = v
        from .policy import clean_allowed

        new["allowed"], refused = clean_allowed(request.POST.get("allowed", DEFAULTS["allowed"]))
        if refused:
            messages.warning(
                request,
                _("Not taken (they break passwords where they are used): %(c)s")
                % {"c": " ".join("space" if c == " " else c for c in refused)},
            )
        if new["special"] and not new["allowed"]:
            messages.error(request, _("Special symbols are required, but none is allowed."))
            return _settings_back(request)
        new["names"] = request.POST.get("names") == "on"
        if new["lower"] + new["upper"] + new["special"] + new["digits"] > new["length"]:
            messages.error(request, _("The minimum counts add up to more than the length."))
            return _settings_back(request)
        old = policy()
        obj.password_policy, obj.updated_by = new, request.user
        obj.save()
        audit(request.user, "setting_changed", obj, **{f"password_{k}": v for k, v in new.items() if old.get(k) != v})
        messages.success(request, _("Password policy saved."))
        return _settings_back(request)
    if request.method == "POST" and request.POST.get("section") == "backup_now":
        obj.backup_requested_at = timezone.now()
        obj.save(update_fields=["backup_requested_at"])
        audit(request.user, "portal_backup_requested", obj)
        messages.success(request, _("A backup is made within 5 minutes; its result shows here."))
        return _settings_back(request)
    if request.method == "POST" and request.POST.get("section") == "appliance":
        from . import appliance

        # corrections-18: Mail and Backups are separate tabs — each saves its own fields; the others keep their values
        part = request.POST.get("part") or ""
        mine = [
            f
            for f in appliance.FORM_FIELDS + ("mail_password", "backup_password", "mail_dkim_key", "backup_s3_secret")
            if (
                f.startswith("backup_")
                if part == "backups"
                else not f.startswith("backup_")
                if part == "mail"
                else True
            )
        ]
        data = request.POST.copy()
        if part:
            for f, v in appliance.form_initial(obj).items():
                if f not in mine:
                    data[f] = "on" if v is True else ("" if v in (None, False) else str(v))
        form = appliance.settings_form(data, has_backup_password=obj.has_secret("backup_password"), settings_obj=obj)
        form.is_valid()
        errs = {k: v for k, v in form.errors.items() if k in mine or k == "__all__"}
        if errs:
            messages.error(request, _("Not saved — see the fields marked below."))
            bound_appliance = form
        else:
            bound_appliance = None
    if request.method == "POST" and request.POST.get("section") == "appliance" and bound_appliance is None:
        d, changed = form.cleaned_data, {}
        for f in [x for x in appliance.FORM_FIELDS if x in mine and x in form.cleaned_data]:
            v = d.get(f)
            v = (
                ""
                if v is None
                and f
                in (
                    "public_url",
                    "mail_from",
                    "mail_host",
                    "mail_user",
                    "backup_path",
                    "mail_dkim_domain",
                    "mail_dkim_selector",
                    "backup_s3_endpoint",
                    "backup_s3_region",
                    "backup_s3_bucket",
                    "backup_s3_prefix",
                    "backup_s3_access_key",
                )
                else v
            )
            if f == "public_url":
                v = (v or "").rstrip("/")
            if v != getattr(obj, f):
                setattr(obj, f, v)
                changed[f] = str(v)
        for f in ("mail_password", "backup_password", "mail_dkim_key", "backup_s3_secret"):
            if f in mine and d.get(f):
                obj.set_secret(f, d[f])
                changed[f] = "changed"
        if changed:
            obj.updated_by = request.user
            obj.save()
            appliance.forget()
            audit(request.user, "setting_changed", obj, **changed)
        messages.success(request, _("Settings saved."))
        return _settings_back(request)
    if request.method == "POST" and request.POST.get("section") == "domains":
        obj.corporate_domains = "\n".join(
            sorted(
                {d.strip().lower().lstrip("@") for d in request.POST.get("corporate_domains", "").split() if d.strip()}
            )
        )
        obj.updated_by = request.user
        obj.save()
        audit(request.user, "setting_changed", obj, corporate_domains=" ".join(obj.corporate_domain_list))
        messages.success(request, _("Corporate domains saved."))
        return _settings_back(request)
    # owner 2026-11-08: this used to be a catch-all ("any POST that nothing else claimed"), so a form whose
    # section was not dispatched above landed here and silently switched every approval off - unchecked
    # checkboxes simply are not submitted. "Set source" did exactly that. It now claims its own section only.
    if request.method == "POST" and bound_appliance is None and request.POST.get("section") == "approvals":
        rm = request.POST.get("rm_approval_required") == "on"
        sales = request.POST.get("sales_approval_required") == "on"
        support = request.POST.get("support_approval_required") == "on"
        # owner 2026-11-12: "sa can select any checkbox here only if any active user in that role"
        for key, field, _flag in APPROVAL_ROLES:
            want = {"rm": rm, "sales": sales, "support": support}[key]
            if want and not getattr(obj, field) and not approvers_of(key):
                messages.error(request, _("Nobody active holds this role: add or activate a user first."))
                return _settings_back(request)
        changed = {}
        if rm != obj.rm_approval_required:
            obj.rm_approval_required = changed["rm_approval_required"] = rm
        if sales != obj.sales_approval_required:
            obj.sales_approval_required = changed["sales_approval_required"] = sales
        if support != obj.support_approval_required:
            obj.support_approval_required = changed["support_approval_required"] = support
        if changed:
            obj.updated_by = request.user
            obj.save()
            audit(request.user, "setting_changed", obj, **changed)
            messages.success(request, _("Settings saved."))
        return _settings_back(request)
    haproxy_posted = haproxy_errors = None
    if request.method == "POST" and request.POST.get("section") == "haproxy":  # owner 2026-10-31: Settings → HAProxy
        from . import haproxy

        # u02: the portal's names under Portal, the front ends' ports and sites under Platform: one setting, two forms
        cur = haproxy.get(obj)
        data = {
            "other_names": cur["other_names"],
            "le_email": cur["le_email"],
            **{f"port_{k}": v for k, v in cur["ports"].items()},
            **{f"{f}_{c}": v.get(f, "") for c, v in cur["sites"].items() for f in ("one_names", "one_addr")},
        }
        data.update(
            {k: v for k, v in request.POST.items() if k not in ("csrfmiddlewaretoken", "section", "tab", "part")}
        )
        hp_tab = "platform" if request.POST.get("part") == "platform" else "portal"
        hp_tab = "platform" if request.POST.get("part") != "portal" and "port_fireedge" in request.POST else hp_tab
        conf, haproxy_errors = haproxy.clean(data, haproxy.site_codes())
        if not haproxy_errors:
            if conf != (obj.haproxy or {}):
                obj.haproxy, obj.updated_by = conf, request.user
                obj.save()
                audit(request.user, "setting_changed", obj, haproxy=conf)
            messages.success(
                request,
                _(
                    "Settings saved. Each portal VM applies them by itself within 5 minutes (new names get their "
                    "certificates then)."
                ),
            )
            return redirect(reverse("portal_settings") + f"?tab={hp_tab}")
        haproxy_posted = data
    from . import appliance
    from .policy import LIMITS, policy, rules

    if haproxy_errors:  # back where the errors are: the front ends' ports and sites are under Platform
        tab = (
            "platform"
            if request.POST.get("part") == "platform" or any(k.startswith(("port", "one_")) for k in haproxy_errors)
            else "portal"
        )
    elif bound_appliance is not None:  # back where the errors are
        part = request.POST.get("part") or (
            "backups" if any(k.startswith("backup_") for k in bound_appliance.errors) else "mail"
        )
        tab = part
    else:
        tab = request.GET.get("tab") or "provider"
    tab = OLD_TABS.get(tab, tab)
    if tab in ("portal", "platform"):
        extra_haproxy = _haproxy_context(obj, haproxy_posted, haproxy_errors)
    else:
        extra_haproxy = {}
    if tab not in SETTINGS_TABS:
        tab = "provider"
    extra = {}
    if tab == "portal":
        from . import ha_views

        extra = ha_views.context(request, obj)
    if tab == "backups":  # u04
        from . import edition as _edn
        from . import fe_backup

        s3 = _edn.ee("s3")  # S3 is the Enterprise edition's (editions split step 4)
        extra.update(
            fe_platforms=fe_backup.platforms(),
            s3_ui=s3 is not None,
            s3_policy=s3.bucket_policy(obj.backup_s3_bucket or "<bucket>", obj.backup_s3_prefix or "") if s3 else "",
        )
    if tab == "appearance":
        from . import timers

        extra["timers"] = timers.rows()
    if tab == "reports":
        from . import reports

        extra.update(
            report_rows=reports.rows(),
            report_conf=reports.config(),
            report_days=reports.DAYS,
            report_roles=reports.ROLES,
        )
    if tab == "mail":
        from . import mailtemplates

        extra["mail_templates"] = mailtemplates.listing()
    extra.update(extra_haproxy)
    if "test_url" not in extra:
        from .ha_views import test_url

        extra["test_url"] = test_url()
    from . import ha_agent as _ha_agent

    _backups = _ha_agent.backups_for_page(obj)
    return render(
        request,
        "staff/settings.html",
        {
            "tab": tab,
            "tabs": [(k, v) for k, v in SETTINGS_TABS.items()],
            "all_countries": __import__("accounts.taxid", fromlist=["countries_choices"]).countries_choices(),
            **extra,
            "appliance_form": bound_appliance
            or appliance.settings_form(
                initial=appliance.form_initial(obj),
                settings_obj=obj,
                has_backup_password=obj.has_secret("backup_password"),
            ),
            "has_backup_password": obj.has_secret("backup_password"),
            "has_mail_password": obj.has_secret("mail_password"),
            "has_dkim_key": obj.has_secret("mail_dkim_key"),
            "backup_due": appliance.slots(obj, timezone.now()),
            "relay_ok": appliance.relay_reachable(),
            "local_backups": _backups["local"],
            "s3_backups": _backups["s3"],
            "fe_local_backups": _backups.get("fe_local") or [],
            "fe_s3_backups": _backups.get("fe_s3") or [],
            "restore_steps": appliance.restore_steps(obj),
            "default_url": settings.SITE_URL,
            "backup_pending": bool(
                obj.backup_requested_at and (obj.backup_last_at is None or obj.backup_requested_at > obj.backup_last_at)
            ),
            "obj": obj,
            "table": approval_table(obj),
            "policy": policy(),
            "limits": LIMITS,
            "rules": rules(),
            "unsafe": " ".join(
                "space" if c == " " else c for c in __import__("accounts.policy", fromlist=["UNSAFE"]).UNSAFE
            ),
            "sites": __import__("inventory.models", fromlist=["Site"]).Site.objects.all(),
        },
    )


# --- profile and 2FA ---------------------------------------------------------


@login_required
def profile_view(request):
    user = request.user
    form = ProfileForm(
        request.POST or None,
        request.FILES or None,
        initial={"full_name": user.full_name, "phone": user.phone_display, "city": user.city},
    )
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        user.full_name, user.phone, user.city = d["full_name"], d["phone"], d["city"]
        fields = ["full_name", "phone", "city"]
        if d.get("photo"):
            user.photo = d["photo"]
            fields += ["photo_data", "photo_type"]
        user.save(update_fields=fields)
        audit(user, "profile_updated", user)
        messages.success(request, _("Profile saved."))
        return redirect("profile")
    return render(request, "accounts/profile.html", {"form": form, "passkeys": user.passkeys.all()})


@login_required
def totp_setup_view(request):
    user = request.user
    device = totp_device(user, confirmed=False)
    if device is None or request.method != "POST":
        device = new_totp_device(user)  # a new key each time the page is opened; the posted code checks this one
    form = OTPForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if verify_totp(user, form.cleaned_data["code"], device=device):
            from django_otp.plugins.otp_totp.models import TOTPDevice

            TOTPDevice.objects.filter(user=user).exclude(pk=device.pk).delete()
            device.confirmed = True
            device.save(update_fields=["confirmed"])
            user.totp_enabled = True
            user.save(update_fields=["totp_enabled"])
            audit(user, "2fa_enabled", user)
            messages.success(request, _("Two-factor authentication is on."))
            return redirect("dashboard")
        form.add_error("code", _("Wrong code. Check the time on your phone and try again."))
    return render(
        request,
        "accounts/totp_setup.html",
        {
            "form": form,
            "qr": totp_qr_svg(totp_uri(user, device)),
            "secret": totp_secret(device),
            "forced": user.requires_2fa and not user.totp_enabled,
        },
    )


@login_required
def totp_disable_view(request):
    user = request.user
    if user.requires_2fa:
        raise PermissionDenied
    form = OTPForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if verify_totp(user, form.cleaned_data["code"]):
            from django_otp.plugins.otp_totp.models import TOTPDevice

            TOTPDevice.objects.filter(user=user).delete()
            user.totp_enabled = False
            user.save(update_fields=["totp_enabled"])
            audit(user, "2fa_disabled", user)
            messages.success(request, _("Two-factor authentication is off."))
            return redirect("profile")
        form.add_error("code", _("Wrong or expired code."))
    return render(request, "accounts/totp_disable.html", {"form": form})


def can_see_person(viewer, target):
    if viewer.pk == target.pk:
        return True
    if viewer.is_staff_member:
        if viewer.is_super_admin or viewer.has_role(Role.RESOURCE_MANAGER) or viewer.has_role(Role.SUPPORT):
            return True
        if viewer.is_sales:
            return bool(target.organization_id) and target.organization.sales_rep_id == viewer.pk
        return False
    if viewer.is_org_admin and viewer.organization_id:
        if target.organization_id == viewer.organization_id:
            return True
        return target.join_requests.filter(
            organization_id=viewer.organization_id, state=JoinRequest.State.PENDING
        ).exists()
    if viewer.is_customer and target.organization_id == viewer.organization_id:
        shared = Membership.objects.filter(user=viewer, tenant__memberships__user=target)
        return shared.exists() or target.is_org_admin
    return False


@login_required
def photo_view(request, pk):
    target = get_object_or_404(User, pk=pk)
    if not can_see_person(request.user, target):
        raise PermissionDenied
    if not target.photo:
        raise Http404
    resp = HttpResponse(bytes(target.photo_data), content_type=target.photo_type or "image/jpeg")
    resp["Cache-Control"] = "private, max-age=300"
    return resp


# --- documentation -----------------------------------------------------------


def docs_view(request, page="portal"):
    """The technical documentation moved to the public documentation (reference): old links lead there."""
    from content.views import REFERENCE_PAGES, docs_url

    return redirect(docs_url(REFERENCE_PAGES[page]))


# --- dashboard ---------------------------------------------------------------


@login_required
def dashboard_view(request):
    user = request.user
    if user.is_customer:
        if user.organization_id:
            from tenants.views import org_dashboard

            return org_dashboard(request)
        pending = user.join_requests.order_by("-created_at").first()
        return render(request, "accounts/waiting_join.html", {"join": pending})
    from . import dashboard as dash

    boards = dash.dashboards_of(user)  # u12: "act as" replaces the old per-role view buttons; show the user's own board
    board = request.GET.get("board") or (boards[0] if boards else "")
    if board not in boards:
        board = boards[0] if boards else ""
    ctx = {
        "viewer": user,
        "boards": [(k, _(label)) for k, label in dash.DASHBOARDS if k in boards],
        "settings_obj": PortalSettings.get(),
    }
    if board:
        ctx.update(dash.build(user, board))
    real = getattr(request, "impersonator", None) or user  # the actual super admin, even while acting as someone
    ctx["can_act_as"] = real.is_super_admin
    if real.is_super_admin:  # u12: pick any non-archived user to act as; the whole interface then renders as them
        ctx["act_as_people"] = (
            User.objects.filter(archived_at__isnull=True).exclude(pk=real.pk).order_by("full_name", "email")
        )
        ctx["act_as_current"] = user.pk if getattr(request, "impersonator", None) else 0
    if board in ("super", "support", "rm"):
        ctx["join_requests"] = JoinRequest.objects.filter(state=JoinRequest.State.PENDING).select_related(
            "organization", "user"
        )[:20]
    return render(request, "accounts/dashboard.html", ctx)


@require_POST
def act_as_view(request):
    """u12: a super admin starts or stops acting as another non-archived user. Selecting themselves (or an empty
    value) stops. The swap itself is done by ActAsMiddleware from the session key set here."""
    real = getattr(request, "impersonator", None) or request.user
    if not (real.is_authenticated and real.is_super_admin):
        raise PermissionDenied
    pk = (request.POST.get("user") or "").strip()
    target = None
    if pk.isdigit() and int(pk) not in (0, real.pk):
        target = User.objects.filter(pk=int(pk), archived_at__isnull=True).first()
        if target is None:
            messages.error(request, _("That user is not available."))
    if target is not None:
        request.session["act_as"] = target.pk
        audit(real, "act_as_started", target, email=target.email)
    elif request.session.pop("act_as", None):
        audit(real, "act_as_stopped", real)
    nxt = request.POST.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts=None):
        return redirect(nxt)
    return redirect("dashboard")


def analysis_view(request, page="summary"):
    """Analysis → Summary / Usage (owner 2026-11-13): the Enterprise edition's (ee/analysis.py), found by name; the
    public build and a Standard licence have no such page."""
    from django.http import Http404

    from . import edition

    impl = edition.ee("analysis")
    if impl is None or not edition.allows("analysis"):
        raise Http404
    if not request.user.is_authenticated:
        from django.contrib.auth.views import redirect_to_login

        return redirect_to_login(request.get_full_path())
    return impl.view(request, page)
