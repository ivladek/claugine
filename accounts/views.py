"""Every page of the accounts app: signing in, registration, invitations, people, the profile, Settings.

The biggest module in the app, and deliberately flat: one view per address (`portal/urls.py`), each gated by
`accounts/utils.staff_required` or `customer_required`, each ending in a render or a redirect. Business rules do
not live here — they are in the modules the views call (`people.py`, `policy.py`, `roles.py`, `appliance.py`,
`superadmins.py`). **Settings posts are dispatched by the `section` field**: a handler that does not check its own
section becomes a catch-all and silently rewrites another form's fields.
"""

import re
from datetime import timedelta

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
from .mailtemplates import T
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
            T.VERIFY_EMAIL,
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
                    T.ORG_ASSIGNED,
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
        T.JOIN_REQUEST_OWNER,
        {**ctx, "link": site_link(reverse("my_org"))},
        _("Someone asks to join %(name)s") % {"name": org.name},
    )
    staff = list(users_with_role(Role.RESOURCE_MANAGER))
    if org.sales_rep:
        staff.append(org.sales_rep)
    notify(
        staff,
        T.JOIN_REQUEST_STAFF,
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
        T.TENANT_PENDING,
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
            # core-045 (twenty-third full review): the invitation and its account are read again under their row
            # locks - a revoke, a block or an archive made since the page was opened wins, and two acceptances of one
            # invitation cannot both go through; only the activation's own fields are written. commit-042: the
            # account's row first, then the invitation's - the order an archive and an invitation back take them in
            # (people.archive_now, invite_person_view), so the two never wait for each other
            account = User.objects.select_for_update().filter(pk=inv.user_id).first() if inv.user_id else None
            inv = Invitation.objects.select_for_update().get(pk=inv.pk)
            if not inv.is_usable:
                return render(request, "accounts/link_invalid.html", status=400)
            if inv.purpose == Invitation.Purpose.ACTIVATE:
                user = account
                if user is None or user.archived_at or not user.is_active:
                    return render(request, "accounts/link_invalid.html", status=400)
                for k, v in profile.items():
                    setattr(user, k, v)
                fields = [*profile, "password", "login_enabled", "email_verified", "consent_at"]
                if d.get("photo"):
                    user.photo = d["photo"]
                    fields += ["photo_data", "photo_type"]
                user.set_password(d["password1"])
                user.login_enabled = True
                user.email_verified = True
                user.consent_at = timezone.now()
                user.save(update_fields=fields)
            else:
                User.objects.filter(email__iexact=d["email"], kind=Kind.CUSTOMER, email_verified=False).delete()
                if inv.purpose == Invitation.Purpose.STAFF:
                    roles = set(g.get("roles", []))
                    # core-016 again (sixth full review): the super admin role comes with an invitation only from the
                    # console (manage.py bootstrap_superadmin, root, no one invited by); any other invitation asks
                    # for it below, granted once the super admins agree (owner u12)
                    sa_now = bool(g.get("super_admin")) and inv.invited_by_id is None
                    extra = dict(
                        kind=Kind.STAFF,
                        is_super_admin=sa_now,
                        is_staff=sa_now,
                        is_superuser=sa_now,
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
                if inv.purpose == Invitation.Purpose.STAFF and g.get("super_admin") and not user.is_super_admin:
                    from . import superadmins

                    if inv.invited_by and inv.invited_by.is_super_admin:
                        superadmins.request_add(inv.invited_by, user)
                if inv.purpose == Invitation.Purpose.CUSTOMER:
                    for tid, role in (g.get("tenants") or {}).items():
                        tenant = Tenant.objects.filter(pk=tid, organization=inv.organization).first()
                        if tenant:
                            Membership.objects.get_or_create(user=user, tenant=tenant, defaults={"role": role})
            inv.accepted_at = timezone.now()
            inv.save(update_fields=["accepted_at"])
        audit(user, "invitation_accepted", user, purpose=inv.purpose, invitation=inv.pk)
        if user.totp_enabled:
            # core-001 (eighteenth full review): an account brought back keeps its authenticator (and passkeys); the
            # invitation sets its password, and the session then passes the second factor as at a login - it was
            # signed in at once, with no code asked
            request.session.update(
                {
                    "pre2fa_uid": user.pk,
                    "pre2fa_backend": BACKEND,
                    "pre2fa_next": reverse("dashboard"),
                    "pre2fa_attempts": 0,
                }
            )
            messages.info(request, _("One more step: the code from your authenticator app."))
            return redirect("login_otp")
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
    # core-045: revoked only while still open, in one statement - an acceptance holding the invitation's row finishes
    # first, and then the revoke finds it accepted
    if Invitation.objects.open().filter(pk=inv.pk).update(revoked_at=timezone.now()):
        inv.refresh_from_db()
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
                    # commit-031: agreed, held back by the role check - what holds it
                    "held": superadmins.archive_blocked(req) if superadmins.is_ready(req) else "",
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


def _archive_report(request, err, person):
    """core-016: archiving a super admin may wait for the other super admins."""
    from . import people

    req = getattr(person, "sa_removal", None)
    if err or req is None or req.state == "done":
        people.report(request, err, _("%(email)s is archived.") % {"email": person.email})
        return
    messages.info(
        request,
        _(
            "%(e)s is archived, and loses the super admin role, once %(n)s super admins agree (you count as one), "
            "within %(h)s hours; they are told."
        )
        % {"e": person.email, "n": req.required, "h": req.HOURS},
    )


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
        # core-020 (seventh full review): archiving switched the account off (is_active) - inviting back switches it on
        # again, with sign-in off until the invitation is accepted; a block after this stays (is_active off again)
        person.archived_at, person.login_enabled, person.is_active = None, False, True
        person.save(update_fields=["archived_at", "login_enabled", "is_active"])
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
        # core-016 (fourth full review): the super admin role is never given by an invitation - it is asked for, and
        # granted once the super admins agree (owner u12), as on the roles form; taking it away is asked for too
        sa_wanted = "is_super_admin" in fields
        for f, _l2 in people.STAFF_ROLES:
            if f != "is_super_admin":
                setattr(person, f, f in fields)
        person.save(update_fields=[f for f, _l2 in people.STAFF_ROLES if f != "is_super_admin"])
        from . import superadmins as _sa

        _sa.retry_held(request.user)  # commit-031: an inactive holder counts - an agreed archive may complete now
        if sa_wanted != person.is_super_admin:
            from . import superadmins

            ask = superadmins.request_add if sa_wanted else superadmins.request
            person.sa_removal, err = ask(request.user, person)
            if err:
                messages.error(request, err)
                return _people_back(request)
            _sa_pending_message(request, person)
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
            T.STAFF_INVITATION,
            {"invitation": inv, "link": link, "role": person.role_display, "hours": settings.INVITATION_TTL_HOURS},
            _("Your invitation to the Example Cloud portal"),
        )
    else:
        notify(
            [person.email],
            T.MEMBER_INVITATION,
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
        T.MEMBER_INVITATION,
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
    _archive_report(request, err, person)
    return _people_back(request)


@require_POST
@login_required
def archive_person_view(request, pk):
    from . import people

    person = get_object_or_404(User, pk=pk, archived_at__isnull=True)
    if not _may_archive(request.user, person):
        raise PermissionDenied
    err = people.archive(request.user, person)
    _archive_report(request, err, person)
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
        T.STAFF_INVITATION,
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
def mail_template_view(request, tid):
    """corrections-18: edit one e-mail template (Settings → Mail): its name and its text, or reset the text to the
    portal's own. The template is addressed by its id (owner 2026-11-17 18:52); the name is the people's."""
    from django.http import Http404

    from . import mailtemplates as mt
    from .models import MailTemplate

    name = mt.IDS.get(tid)
    if name is None or name not in mt.names():
        raise Http404
    back = redirect(reverse("portal_settings") + "?tab=mail#mail-templates")
    current = mt.override(name)
    text = current.body if current else mt.default_text(name)
    if request.method == "POST":
        if request.POST.get("reset"):
            MailTemplate.objects.filter(name=name).delete()
            audit(request.user, "mail_template_reset", None, template=tid)
            messages.success(request, _("%(n)s: the portal's own text again.") % {"n": mt.title(tid)})
            return back
        text = (request.POST.get("body") or "").replace("\r\n", "\n")
        err = mt.compile_error(text)
        new_title = " ".join((request.POST.get("title") or "").split())[:80] if "title" in request.POST else None
        if new_title is not None and not new_title:
            err = _("the name is empty")
        elif new_title and any(mt.title(i).lower() == new_title.lower() for i in mt.IDS if i != tid):
            err = _("another template has this name")
        if err:
            messages.error(request, _("Not saved: %(e)s") % {"e": err})
            return back
        if new_title is not None:
            s = PortalSettings.get()
            names = {k: v for k, v in (s.mail_template_names or {}).items() if k != str(tid)}
            if new_title != mt.label(name):
                names[str(tid)] = new_title
            if names != (s.mail_template_names or {}):
                s.mail_template_names = names
                s.save(update_fields=["mail_template_names"])
                audit(request.user, "mail_template_renamed", None, template=tid, title=new_title)
        if text.strip() == mt.default_text(name).strip():
            MailTemplate.objects.filter(name=name).delete()
        else:
            MailTemplate.objects.update_or_create(name=name, defaults={"body": text, "updated_by": request.user})
            audit(request.user, "mail_template_saved", None, template=tid)
        messages.success(request, _("%(n)s saved.") % {"n": mt.title(tid)})
        return back
    return render(
        request,
        "staff/mail_template.html",
        {
            "name": mt.title(tid),
            "what": mt.WHAT.get(name, ""),
            "text": text,
            "edited": current,
            "default": mt.default_text(name),
        },
    )


@require_POST
@staff_required(super_only=True)
def mail_template_preview_view(request, tid):
    """The edited text drawn as the e-mail looks, for the live preview of the Edit dialog (owner 2026-11-17 17:47:
    "Source + live preview"). Nothing is saved."""
    from django.http import Http404, HttpResponse

    from . import mailtemplates as mt

    name = mt.IDS.get(tid)
    if name is None or name not in mt.names():
        raise Http404
    text = (request.POST.get("body") or "").replace("\r\n", "\n")
    err = mt.compile_error(text)
    out = mt.preview(name, text) if not err else ""
    if err or not name.endswith(".html"):
        from html import escape

        out = f'<pre style="white-space:pre-wrap;font:13px monospace;margin:12px">{escape(err or out)}</pre>'
    return HttpResponse(out, content_type="text/html; charset=utf-8")


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
    # owner 2026-11-17 14:05: "settings/platfrom rename to customer / add here all color and logo settings"
    "customer": _l("Customer"),
    "security": _l("Security"),  # corrections-18, u02; the igw ssh ports (owner 2026-11-17)
    "mail": _l("Mail"),
    "reports": _l("Reports"),  # owner 2026-11-17 15:06: "return reports tab"
    "backups": _l("Backups"),
    "portal": _l("Portal"),  # the nodes and updates in one block, then what Appearance had: lists, history, timers
}
OLD_TABS = {
    "platform": "customer",
    "provider": "customer",
    "customers": "customer",
    "branding": "customer",
    "approvals": "customer",
    "appearance": "portal",
    "operations": "portal",
    "data": "portal",
    "publishing": "portal",
    "haproxy": "portal",
    "appliance": "mail",
}  # the addresses of the tabs as they were (2026-11-15: Platform; before it Provider, Appearance, Reports, ...)


def brand_logo_view(request):
    """The provider's logo (PNG; public: the sign-in page and the e-mails show it)."""
    from django.http import Http404, HttpResponse

    obj = PortalSettings.get()
    if not obj.logo:
        raise Http404
    r = HttpResponse(bytes(obj.logo), content_type="image/png")
    r["Cache-Control"] = "public, max-age=31536000" if request.GET.get("v") else "public, max-age=300"
    return r


def brand_icon_view(request, size):
    """The browser icon (public): cut from the uploaded logo by the portal, else Claugine's gear (owner 2026-11-17)."""
    from django.http import Http404, HttpResponse

    from . import branding

    if size not in branding.ICON_SIZES:
        raise Http404
    r = HttpResponse(branding.icon_png(size), content_type="image/png")
    r["Cache-Control"] = "public, max-age=31536000" if request.GET.get("v") else "public, max-age=300"
    return r


def _changed(request, obj, values, *, extra=()):
    """Sets the values that differ and saves only those fields (with updated_by); returns {field: value} changed."""
    changed = {f: v for f, v in values.items() if v != getattr(obj, f)}
    for f, v in changed.items():
        setattr(obj, f, v)
    if changed or extra:
        obj.updated_by = request.user
        obj.save(update_fields=[*changed, *extra, "updated_by"])
    return changed


def _back_to(tab):
    return redirect(reverse("portal_settings") + f"?tab={tab}")


def _save_provider(request, obj):
    """Settings → Customer → Provider (owner 2026-11-17): Name, E-mail (the Let's Encrypt account: "use name foir this
    field just email"), Customer Portal (the public URL), Admin Portal (on the certificate too since 2026-11-23, U13) and the colours.
    Everything is checked first; nothing is saved when one value is wrong."""
    import re as _re

    from django.core.exceptions import ValidationError
    from django.core.validators import URLValidator

    from .branding import DEFAULT
    from .haproxy import EMAIL_RE

    P = request.POST
    errors = []
    name = " ".join((P.get("provider_name") or "").split())[:100] or DEFAULT
    urls = {}
    for field, label in (("public_url", _("Customer Portal")), ("admin_url", _("Admin Portal"))):
        url = (P.get(field) or "").strip().rstrip("/")
        if url:
            try:
                URLValidator(schemes=["https", "http"])(url)
            except ValidationError:
                errors.append(_("%(f)s: not a URL.") % {"f": label})
        urls[field] = url
    le_email = (P.get("le_email") or "").strip()
    if le_email and not EMAIL_RE.match(le_email):
        errors.append(_("E-mail: not an e-mail address."))
    values = {"provider_name": name, **urls}
    if "colour_scheme" in P:  # the colours, in the same block (owner 2026-11-17)
        scheme = P.get("colour_scheme") or ""
        custom = (P.get("colour_custom") or "").strip().lower()
        if scheme not in ("claugine", "logo", "custom") or (scheme == "logo" and not obj.logo):
            errors.append(_("Choose the colours."))
        elif scheme == "custom" and not _re.fullmatch(r"#[0-9a-f]{6}", custom):
            errors.append(_("The custom colour: #rrggbb."))
        else:
            values["colour_scheme"] = scheme
            if scheme == "custom":
                values["colour_custom"] = custom
            if scheme != obj.colour_scheme:
                values["colour_offer"] = False
    if errors:
        for e in errors:
            messages.error(request, e)
        return _back_to("customer")
    extra = []
    if le_email != ((obj.haproxy or {}).get("le_email") or ""):  # HAProxy's Let's Encrypt account, on both VMs
        obj.haproxy = {**(obj.haproxy or {}), "le_email": le_email}
        extra.append("haproxy")
    changed = _changed(request, obj, values, extra=extra)
    changed.pop("colour_offer", None)
    if extra:
        changed["le_email"] = le_email
    if changed:
        if "public_url" in changed or "admin_url" in changed:  # commit-424: either address, at once
            from . import appliance

            appliance.forget()
        audit(request.user, "setting_changed", obj, **changed)
    messages.success(request, _("Settings saved."))
    return _back_to("customer")


def _save_security(request, obj):
    """Settings → Security (owner 2026-11-17): the password policy, one row per rule, and the igw ssh port range, one
    Save. Everything is checked first; nothing is saved when one value is wrong."""
    from tenants.models import Tenant

    from .policy import DEFAULTS, LIMITS, clean_allowed, policy

    P = request.POST
    back = _back_to("security")
    new = {}
    for k in ("length", "lower", "upper", "special", "digits", "history"):
        try:
            v = int(P.get(k))
        except (TypeError, ValueError):
            messages.error(request, _("%(f)s: a whole number.") % {"f": k})
            return back
        lo, hi = LIMITS[k]
        if not lo <= v <= hi:
            messages.error(request, _("%(f)s: from %(lo)s to %(hi)s.") % {"f": k, "lo": lo, "hi": hi})
            return back
        new[k] = v
    new["allowed"], refused = clean_allowed(P.get("allowed", DEFAULTS["allowed"]))
    if refused:
        messages.warning(
            request,
            _("Not taken (they break passwords where they are used): %(c)s")
            % {"c": " ".join("space" if c == " " else c for c in refused)},
        )
    if new["special"] and not new["allowed"]:
        messages.error(request, _("Special symbols are required, but none is allowed."))
        return back
    new["names"] = P.get("names") == "on"
    if new["lower"] + new["upper"] + new["special"] + new["digits"] > new["length"]:
        messages.error(request, _("The minimum counts add up to more than the length."))
        return back
    values = {}
    if "igw_port_min" in P:  # the old Password policy form had no ports
        lo, hi = Tenant.IGW_PORT_LIMITS
        try:
            a, b = int(P.get("igw_port_min") or 0), int(P.get("igw_port_max") or 0)
        except ValueError:
            a = b = 0
        if not (lo <= a < b <= hi):
            messages.error(
                request,
                _("The igw ssh ports: a range within %(lo)s-%(hi)s, the first lower than the last.")
                % {"lo": lo, "hi": hi},
            )
            return back
        values = {"igw_port_min": a, "igw_port_max": b}
    ports = None
    if "public_ssh_port" in P:  # owner 2026-11-24 20:54 (T36 N3): the admin VMs' public SSH and RDP ports
        try:
            ssh, rdp = int(P.get("public_ssh_port") or 0), int(P.get("public_rdp_port") or 0)
        except ValueError:
            ssh = rdp = 0
        if not (1024 <= ssh <= 65535 and 1024 <= rdp <= 65535) or ssh == rdp:
            messages.error(request, _("Public access ports: two different ports from 1024 to 65535."))
            return back
        ports = {"public_ssh_port": ssh, "public_rdp_port": rdp}
    if ports is not None:  # commit-474: the pair checked against the row as it is and written together, under its lock
        from django.db import transaction

        with transaction.atomic():
            row = PortalSettings.objects.select_for_update().get(pk=obj.pk)
            if any(getattr(row, f) != v for f, v in ports.items()):
                PortalSettings.objects.filter(pk=row.pk).update(updated_by=request.user, **ports)
                audit(request.user, "setting_changed", obj, **ports)
        for f, v in ports.items():
            setattr(obj, f, v)
    old = policy()
    if new != (obj.password_policy or {}):
        values["password_policy"] = new
    changed = _changed(request, obj, values)
    detail = {f"password_{k}": v for k, v in new.items() if old.get(k) != v}
    detail.update({k: v for k, v in changed.items() if k != "password_policy"})
    if detail:
        audit(request.user, "setting_changed", obj, **detail)
    messages.success(request, _("Settings saved."))
    return back


def _save_mail(request, obj):
    """Settings → Mail (owner 2026-11-17): the sender address, the DKIM selector, e-mail to organization users and to
    platform users, each node's public address. The nodes' own mail servers send (no relay); the portal makes the DKIM
    key when there is none, signing for the sender address's domain."""
    import ipaddress
    import re as _re
    from email.utils import parseaddr

    from django.core.validators import validate_email

    from . import appliance, maildns

    P = request.POST
    errors = []
    sender = (P.get("mail_from") or "").strip()[:200]
    if sender:
        try:
            validate_email(parseaddr(sender)[1])
        except Exception:  # noqa: BLE001
            errors.append(_("Sender address: an e-mail address, optionally with a name: Name <address>."))
    selector = (P.get("mail_dkim_selector") or "").strip()[:63]
    if selector and not _re.fullmatch(r"[A-Za-z0-9-]+", selector):
        errors.append(_("DKIM selector: letters, figures and dashes."))
    ips = {}
    node_list = maildns.nodes()
    via = (P.get("mail_via") or "").strip()  # owner 2026-11-23 (A): the node every mail leaves from
    if via and via not in [nd["site"] for nd in node_list]:
        errors.append(_("Send mail from: not one of the portal's nodes."))
    for nd in node_list:
        raw = (P.get(f"ip_{nd['site']}") or "").strip()
        if raw:
            try:
                if ipaddress.ip_address(raw).version != 4:
                    raise ValueError
            except ValueError:
                errors.append(_("%(s)s: not an IPv4 address.") % {"s": nd["site"]})
        ips[nd["site"]] = raw
    if errors:
        for e in errors:
            messages.error(request, e)
        return _back_to("mail")
    keep = {k: v for k, v in (obj.mail_public_ips or {}).items() if k not in ips}  # a node not answering keeps its
    values = {
        "mail_from": sender,
        "mail_dkim_selector": selector,
        "client_mail": P.get("client_mail") == "on",
        "staff_mail": P.get("staff_mail") == "on",
        "mail_public_ips": {**keep, **{k: v for k, v in ips.items() if v}},
        "mail_mode": "direct",
        "mail_via": via,
    }
    extra = []
    try:
        if appliance.ensure_dkim_key(obj):  # "Portal makes the key"
            extra.append("mail_dkim_key_encrypted")
    except Exception as exc:  # noqa: BLE001 - no key to encrypt with: the appliance timer makes it later
        messages.warning(request, _("The DKIM key could not be made now: %(e)s") % {"e": exc})
    changed = _changed(request, obj, values, extra=extra)
    if extra:
        changed["mail_dkim_key"] = "made"
    if changed:
        appliance.forget()
        audit(request.user, "setting_changed", obj, **{k: str(v) for k, v in changed.items()})
    messages.success(request, _("Settings saved."))
    return _back_to("mail")


def _mail_test(request, obj):
    from .mailtemplates import T
    from .utils import notify

    n = notify([request.user], T.MAIL_TEST, {"user": request.user}, _("Test message"))
    audit(request.user, "mail_test_sent", obj, to=request.user.email, sent=n)
    if n:
        messages.success(request, _("A test message is sent to %(e)s.") % {"e": request.user.email})
    else:
        messages.warning(
            request, _("Nothing was sent to %(e)s: see Logs (mail_held, mail_failed).") % {"e": request.user.email}
        )
    return _back_to("mail")


def _save_report_redirect_to(request, obj):
    import re as _re

    from . import reports

    to = (request.POST.get("redirect_to") or "").strip()
    if to and not _re.match(r"^[^@\s]+@[^@\s]+\.[a-z]{2,63}$", to, _re.I):
        messages.error(request, _("Redirect e-mail: not an e-mail address."))
        return _back_to("reports")
    conf = reports.config()
    if to != conf["redirect_to"]:
        reports.save({"redirect_to": to, "list": conf["list"]})
        audit(request.user, "setting_changed", obj, reports_redirect_to=to)
    messages.success(request, _("Settings saved."))
    return _back_to("reports")


def _save_report(request, obj):
    """Settings → Reports (owner 2026-11-17): one report's Edit (name, template, day and time, roles), On/Off,
    Redirect on/off, Send now."""
    import re as _re

    from . import reports, tasks

    P, section = request.POST, request.POST.get("section")
    rid = P.get("report") or ""
    conf = reports.config()
    rep = reports.by_id(rid, conf)
    back = redirect(reverse("portal_settings") + f"?tab=reports&report={rid}")
    if rep is None:
        messages.error(request, _("No such report."))
        return _back_to("reports")
    if section == "report_send":
        by = request.user
        tasks.start(
            f"report {rid}",
            f"Report: {rid}",
            lambda task: {"result": reports.send(rid, by=by, how="now")[1]},
            user=request.user,
        )
        audit(request.user, "report_sent_now", obj, report=rid)
        messages.success(request, _("Sending; the result shows in the report's sent history."))
        return back
    new = dict(rep)
    if section == "report_on":
        new["on"] = not rep["on"]
    elif section == "report_redirect":
        new["redirect"] = not rep["redirect"]
    else:
        t = (P.get("time") or "").strip()
        if not _re.match(r"^([01]\d|2[0-3]):[0-5]\d$", t):
            messages.error(request, _("The time: HH:MM."))
            return back
        day = P.get("day") or rep["day"]
        roles = [x for x in P.getlist("roles") if x in reports.ROLE_FLAG]
        if not roles:
            messages.error(request, _("Choose at least one role."))
            return back
        template = reports.template_id(P.get("template"))  # owner 2026-11-17 18:52: kept by id
        if template not in dict(reports.templates()):
            template = reports.template_id(rep.get("template"))
        new.update(
            name=(P.get("name") or "").strip()[:80] or rep["name"],
            day=day if day in dict(reports.DAYS) else rep["day"],
            time=t,
            roles=roles,
            template=template,
        )
    if new != rep:
        lst = [new if r["id"] == rid else r for r in conf["list"]]
        reports.save({"redirect_to": conf["redirect_to"], "list": lst})
        audit(
            request.user, "setting_changed", obj, report=rid, **{k: str(v) for k, v in new.items() if rep.get(k) != v}
        )
    messages.success(request, _("Settings saved."))
    return back


def _save_portal_general(request, obj):
    """Settings → Portal, the block under the nodes (owner 2026-11-17): items per page and how long history is kept,
    one per row, and the timers beside them; one Save. A timer's Refresh is in the same form."""
    from . import timers

    P = request.POST
    values = {}
    for f, lo, hi, dflt in (
        ("list_rows", 5, 100, obj.list_rows),
        ("task_days", 1, 3650, obj.task_days),
        ("log_days", 30, 36500, obj.log_days),
        ("tenant_history_days", 90, 36500, obj.tenant_history_days),
    ):
        try:
            v = int(P.get(f) or dflt)
        except ValueError:
            v = dflt
        values[f] = min(max(v, lo), hi)
    changed = _changed(request, obj, values)
    for key in timers.KEYS:
        raw = P.get(f"minutes_{key}")
        if raw and raw.isdigit() and int(raw) != timers.state(key)["minutes"]:
            timers.set_minutes(key, int(raw))
            changed[f"timer_{key}"] = timers.state(key)["minutes"]
    if changed:
        audit(request.user, "setting_changed", obj, **changed)
    key = P.get("refresh")
    if key in timers.KEYS:
        if timers.start(key, request.user) is None:
            messages.info(request, _("That timer is running already."))
        else:
            audit(request.user, "timer_refreshed", obj, timer=key)
            messages.success(request, _("Started; its result shows here when it is over (Tasks)."))
    else:
        messages.success(request, _("Settings saved."))
    return _back_to("portal")


def _save_customers(request, obj):
    """Settings → Customer → Customers: adding users before they are invited, the corporate email domains, the
    internal domain (owner 2026-11-19: the hosts' FQDN and DNS search domain, T25)."""
    import re as _re

    domains = "\n".join(
        sorted({d.strip().lower().lstrip("@") for d in request.POST.get("corporate_domains", "").split() if d.strip()})
    )
    values = {"users_add": request.POST.get("users_add") == "on", "corporate_domains": domains}
    if "internal_domain" in request.POST:  # a page opened before the field existed leaves the domain as it is
        internal = (request.POST.get("internal_domain") or "").strip().lower().strip(".")
        field = obj._meta.get_field("internal_domain")
        if len(internal) > field.max_length:
            messages.error(request, _("Internal domain: at most %(n)s characters.") % {"n": field.max_length})
            return _back_to("customer")
        if internal and not _re.match(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$", internal):
            messages.error(request, _("Internal domain: a DNS domain, e.g. example.cloud."))
            return _back_to("customer")
        values["internal_domain"] = internal
    if "ntp_external" in request.POST:  # T36 B5d (QB5-2): the external NTP servers of the sites' time servers
        import ipaddress

        ntp = (request.POST.get("ntp_external") or "").replace(",", " ").split()
        if len(ntp) > 4:
            messages.error(request, _("External NTP servers: at most 4."))
            return _back_to("customer")
        for x in ntp:
            try:
                ipaddress.ip_address(x)
            except ValueError:
                if not _re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", x, _re.I):
                    messages.error(
                        request, _("External NTP servers: %(x)s is not an address or a host name.") % {"x": x}
                    )
                    return _back_to("customer")
        if len(set(ntp)) != len(ntp):
            messages.error(request, _("External NTP servers: each once."))
            return _back_to("customer")
        values["ntp_external"] = ntp
    changed = _changed(request, obj, values)
    if changed:
        if "corporate_domains" in changed:
            changed["corporate_domains"] = " ".join(domains.split())
        audit(request.user, "setting_changed", obj, **changed)
    messages.success(request, _("Settings saved."))
    return _back_to("customer")


def _save_logo(request, obj):
    """Settings → Customer → Upload logo / Remove logo. After an upload the portal cuts the browser icon from the logo
    and, unless the portal already follows the logo's colours, offers them (owner 2026-11-17)."""
    from . import branding

    if request.POST.get("op") == "remove":
        if obj.logo:
            obj.logo, obj.logo_updated_at, obj.colour_offer = None, None, False
            obj.updated_by = request.user
            obj.save(update_fields=["logo", "logo_updated_at", "colour_offer", "updated_by"])
            audit(request.user, "setting_changed", obj, logo="removed")
            messages.success(request, _("The logo is removed: Claugine's logo is shown."))
        return _back_to("customer")
    f = request.FILES.get("logo")
    if not f:
        messages.error(request, _("Choose the logo's file."))
        return _back_to("customer")
    if f.size > 5 * 1024 * 1024:
        messages.error(request, _("The logo: at most 5 MB."))
        return _back_to("customer")
    try:
        png = branding.to_png(f)
    except ValueError:
        messages.error(request, _("The logo: a PNG or JPEG image."))
        return _back_to("customer")
    fields = ["logo", "logo_updated_at", "colour_offer", "updated_by"]
    if not obj.colour_scheme:  # the colours stay what they were until the offer is answered
        obj.colour_scheme = "logo" if obj.logo else "claugine"
        fields.append("colour_scheme")
    obj.logo, obj.logo_updated_at = png, timezone.now()
    obj.colour_offer = obj.colour_scheme != "logo"
    obj.updated_by = request.user
    obj.save(update_fields=fields)
    audit(request.user, "setting_changed", obj, logo=f"uploaded ({f.name}, kept as PNG)")
    if not obj.colour_offer:  # else the offer of its colours says it
        messages.success(request, _("The logo is uploaded."))
    return _back_to("customer")


def _save_colours(request, obj):
    """Settings → Customer → Colours: Claugine's, the logo's or a custom colour; also the answer to the offer of the
    logo's colours after an upload (Use / Keep)."""
    import re as _re

    P = request.POST
    answer = P.get("offer")
    if answer in ("use", "keep"):
        values = {"colour_offer": False}
        if answer == "use":
            values["colour_scheme"] = "logo"
        changed = _changed(request, obj, values)
        if "colour_scheme" in changed:
            audit(request.user, "setting_changed", obj, colour_scheme="logo")
        messages.success(request, _("Settings saved."))
        return _back_to("customer")
    scheme = P.get("colour_scheme") or ""
    custom = (P.get("colour_custom") or "").strip().lower()
    if scheme not in ("claugine", "logo", "custom") or (scheme == "logo" and not obj.logo):
        messages.error(request, _("Choose the colours."))
        return _back_to("customer")
    if scheme == "custom" and not _re.fullmatch(r"#[0-9a-f]{6}", custom):
        messages.error(request, _("The custom colour: #rrggbb."))
        return _back_to("customer")
    values = {"colour_scheme": scheme, "colour_offer": False}
    if scheme == "custom":
        values["colour_custom"] = custom
    changed = _changed(request, obj, values)
    changed.pop("colour_offer", None)
    if changed:
        audit(request.user, "setting_changed", obj, **changed)
    messages.success(request, _("Settings saved."))
    return _back_to("customer")


def _backup_rows(local, bucket, last_at, last_result):
    """Settings → Backups, the list under the arrow (owner 2026-11-17): date, time, target, size, status - the backups
    there are, newest first, and the last run when it failed (it left none)."""
    from datetime import datetime as _dt

    from . import appliance

    rows = []

    def when(stamp, at=None):
        if at:
            try:
                t = at if isinstance(at, _dt) else _dt.fromisoformat(str(at))
                t = timezone.localtime(t) if timezone.is_aware(t) else t
                return t.strftime("%d.%m.%Y"), t.strftime("%H:%M"), t.strftime("%Y%m%d-%H%M%S")
            except ValueError:
                pass
        if stamp:
            return f"{stamp[6:8]}.{stamp[4:6]}.{stamp[:4]}", f"{stamp[9:11]}:{stamp[11:13]}", stamp
        return "—", "—", ""

    for item in local or []:
        name, size, at = (list(item) + [None, None])[:3]
        d, t, key = when(appliance.backup_stamp(name), at)
        rows.append({"date": d, "time": t, "key": key, "target": _("folder"), "size": size, "ok": True, "name": name})
    for name in bucket or []:
        d, t, key = when(appliance.backup_stamp(name))
        rows.append({"date": d, "time": t, "key": key, "target": "S3", "size": None, "ok": True, "name": name})
    if last_at and (last_result or "")[:2] != "ok":
        d, t, key = when("", last_at)
        rows.append({"date": d, "time": t, "key": key, "target": "—", "size": None, "ok": False, "name": last_result})
    return sorted(rows, key=lambda r: r["key"], reverse=True)


def _branding_context(obj):
    """Settings → Customer: the logo, the icon cut from it and the colour schemes with their colours."""
    from . import branding

    info = branding.analyse(obj)
    current, _main = branding.scheme(obj)
    logo_main = info["main"] if info else None
    custom = obj.colour_custom or branding.CLAUGINE
    how = {
        "beside": _("the mark beside the name"),
        "above": _("the mark above the name"),
        "": _("the first letter of the provider's name: the logo has no separate mark"),
    }
    return {
        "has_logo": bool(info),
        "logo_src": branding.logo_src(obj),
        "logo_colours": info["colours"] if info else [],
        "icons": [(n, branding.icon_path(n, obj)) for n in (180, 32, 16)],
        "icon_how": how[info["how"]] if info else _("Claugine's gear: no logo uploaded"),
        "current": current,
        "chosen": obj.colour_scheme,
        "offer": obj.colour_offer and bool(info),
        "custom": custom,
        "schemes": [
            ("claugine", _("Claugine"), branding.CLAUGINE, branding.palette(branding.CLAUGINE)),
            ("logo", _("From the logo"), logo_main, branding.palette(logo_main) if logo_main else None),
            ("custom", _("Custom"), custom, branding.palette(custom)),
        ],
    }


def _settings_back(request):
    tab = request.POST.get("tab") or request.GET.get("tab") or ""
    tab = OLD_TABS.get(tab, tab)
    return redirect(reverse("portal_settings") + (f"?tab={tab}" if tab in SETTINGS_TABS else ""))


def _baseline_back():
    return redirect(reverse("infra_os") + "?sel=os-base-os")  # T36 B2: Profiles → OS → base-os


def _baseline_post(request, obj):
    """Profiles → Baseline (owner 2026-11-18, T25; moved from Settings → Platforms 2026-11-23, U1): the releases, the
    OpenNebula repository (public, or private with its secret: Q8), Check now (the timer "baseline") and the
    installation media's Check now."""
    from inventory import baseline

    from . import timers

    section = request.POST.get("section")
    if section == "hostmedia_check":  # T25
        if timers.start("hostmedia", user=request.user) is None:
            messages.info(request, _("The check is already running."))
        else:
            messages.success(request, _("The check started; the result shows here when it is over."))
        return _baseline_back()
    if section == "baseline_check":
        if timers.start("baseline", user=request.user) is None:
            messages.info(request, _("The check is already running; it runs once more when it ends."))
        else:
            messages.success(request, _("The check started; the result shows here when it is over."))
        return _baseline_back()
    new = {k: (request.POST.get(f"baseline_{k}") or "").strip() for k in ("os", "one", "frr")}
    new["one"] = new["one"] or obj.baseline_one  # T36 B2: OpenNebula is per platform; the form no longer has it
    vyos = request.POST.get("baseline_vyos")
    vyos = obj.baseline_vyos if vyos is None else " ".join(vyos.split())  # a form without the field keeps it
    bad = []
    if not baseline.OS_RE.match(new["os"]):
        bad.append(_("Base OS: an Ubuntu LTS release, e.g. 26.04."))
    for k, label in (("one", _("OpenNebula")), ("frr", "FRR")):
        if not baseline.VER_RE.match(new[k]) or len(new[k]) > 8:  # commit-175: the field's length
            bad.append(_("%(what)s: a release as major.minor, e.g. 7.4.") % {"what": label})
    if not baseline.VYOS_RE.match(vyos):
        bad.append(_("VyOS: the release as VyOS names it, e.g. Stream 2026.03."))
    if bad:
        for b in bad:
            messages.error(request, b)
        return _baseline_back()

    more = {"baseline_vyos": vyos}  # T36 B2: the OpenNebula repository and its login are per platform
    from django.db import transaction

    # commit-178/425: the whole form compared with the row as it is and written under its lock (never by a save of the
    # whole row - BASELINE_FIELDS), the releases with their revision
    with transaction.atomic():
        row = PortalSettings.objects.select_for_update().get(pk=obj.pk)
        old = baseline.values(row)
        changed = any(new[k] != old[k] for k in ("os", "one", "frr"))
        saved_more = {k: v for k, v in more.items() if getattr(row, k) != v}
        fields = dict(saved_more)
        if changed:
            fields.update(baseline_os=new["os"], baseline_one=new["one"], baseline_frr=new["frr"])
            if new["os"] != old["os"] or new["one"] != old["one"]:  # commit-177: a new revision, to be checked whole
                fields["baseline_rev"] = row.baseline_rev + 1
        if fields:
            PortalSettings.objects.filter(pk=obj.pk).update(updated_by=request.user, **fields)
            from inventory.models import OsProfile  # commit-478: Profiles → OS shows the same versions

            if "baseline_os" in fields:
                OsProfile.objects.filter(key=OsProfile.BASE_OS).update(version=fields["baseline_os"])
            if "baseline_vyos" in fields:
                OsProfile.objects.filter(key=OsProfile.VYOS).update(version=fields["baseline_vyos"])
    obj.refresh_from_db()
    if saved_more:
        audit(request.user, "setting_changed", obj, **saved_more)
    if changed:
        audit(request.user, "setting_changed", obj, **{f"baseline_{k}": v for k, v in new.items()})
        timers.start("baseline", user=request.user)  # the platforms against the new values at once
        messages.success(request, _("Settings saved. The platforms are checked against the new baseline."))
    elif saved_more:
        messages.success(request, _("Settings saved."))
    return _baseline_back()


def _baseline_context(obj):
    """What Profiles → Baseline shows (owner 2026-11-18, T25; moved there 2026-11-23, U1)."""
    from inventory import baseline

    from . import timers

    base = baseline.values(obj)
    rows = []
    for p in baseline.platforms():
        r = p.baseline or {}
        nodes = r.get("nodes") or []
        rows.append(
            {
                "p": p,
                "r": r,
                "state": baseline.state(p, base),
                "nodes": nodes,
                "fe": sum(1 for n in nodes if n.get("kind") != "host"),
                "hosts": sum(1 for n in nodes if n.get("kind") == "host"),
            }
        )
    from . import hostmedia, thisnode

    n = thisnode.env()
    media = hostmedia.state()
    cur = media.get("current") or {}
    nodes = [(n.get(k + "SITE"), n.get(k + "ADDR")) for k in ("", "PEER_") if n.get(k + "ADDR")]

    from . import haproxy as _hp

    return {
        "obj": obj,
        "media_unserved": [x for site, _a in nodes if site for x in _hp.ilo_unserved(site)],
        "media_peer": media.get("peer") if hostmedia._peer_addr() else None,
        "baseline_rows": rows,
        "baseline_out": [(k, w) for k, kind, w in baseline.blocking(base) if kind == "out"],  # commit-176
        "baseline_stale": [(k, w) for k, kind, w in baseline.blocking(base) if kind == "stale"],
        "baseline_timer": next((t for t in timers.rows() if t["key"] == "baseline"), None),
        "media": media,
        "media_urls": [(site, f"http://{addr}/hostmedia/{cur['file']}") for site, addr in nodes] if cur else [],
        "media_timer": next((t for t in timers.rows() if t["key"] == "hostmedia"), None),
    }


@staff_required(super_only=True)
def baseline_view(request):
    """Profiles → Baseline (owner 2026-11-23 16:01, U1: "Move Baseline tab from Settings to profiles"; mockup p01)."""
    obj = PortalSettings.get()
    if request.method == "POST":
        return _baseline_post(request, obj)
    # T36 B2 (rules v2 D): Baseline folded - base OS and VyOS into Profiles → OS, FRR into Functions → frr,
    # OpenNebula into each platform; the check and the installation media are on OS → base-os
    return redirect(reverse("infra_os") + "?sel=os-base-os")


@staff_required(super_only=True)
def portal_settings_view(request):
    obj = PortalSettings.get()
    bound_appliance = None
    if request.method == "POST" and request.POST.get("section") in (
        "ha_op",
        "auto_update",
        "node_source",
        "add_node",
        "upgrade_enterprise",
        "install_now",
        "rotate_key",
        "update_os",
    ):
        from . import ha_views

        return ha_views.post(request, obj)
    if request.method == "POST" and request.POST.get("section") in ("baseline", "baseline_check", "hostmedia_check"):
        # commit-426: a form of the old Settings → Platforms tab (a page left open across the upgrade) saves nothing -
        # it had no private repository tick and would switch a private repository to public
        messages.error(request, _("This form moved to Profiles → OS: nothing was saved. Open it there."))
        return redirect(reverse("infra_baseline"))
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
        return _save_provider(request, obj)
    if request.method == "POST" and request.POST.get("section") == "customers":
        return _save_customers(request, obj)
    if request.method == "POST" and request.POST.get("section") == "logo":
        return _save_logo(request, obj)
    if request.method == "POST" and request.POST.get("section") == "colours":
        return _save_colours(request, obj)
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
        # owner 2026-11-18 (mockup 70b): "Connect to Front-Ends using" - each platform's SA secret (empty), or an
        # account secret with an ssh key
        from inventory.credentials import ref_choices

        login_ = (P.get("fe_backup_login") or "").strip()
        if login_ and login_ not in {r for r, _label in ref_choices("account")}:
            messages.error(request, _("Choose a secret with an ssh key to connect to the Front-Ends."))
            return _settings_back(request)
        changed = {}
        new = {
            "fe_backup": on,
            "fe_backup_login": login_,
            "fe_backup_local": fe_local,
            "fe_backup_platforms": P.getlist("fe_backup_platforms"),
            "fe_backup_path": path,
            "fe_backup_time": _time(*map(int, t.split(":"))),
            "fe_backup_keep_days": _int("fe_backup_keep_days", 1, 3650, 10),
            "fe_backup_dirs": dirs_,
            "fe_backup_s3": s3_on,
            "fe_backup_s3_use_portal": use_portal,
            "fe_backup_s3_endpoint": (P.get("fe_backup_s3_endpoint") or "").strip(),
            "fe_backup_s3_region": (P.get("fe_backup_s3_region") or "").strip()  # empty: from the endpoint
            if "fe_backup_s3_region" in P
            else obj.fe_backup_s3_region,  # commit-040: not on the page - a stored region is kept
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
            from django.db import transaction

            from inventory.models import Platform

            # commit-039: under the Settings row's lock, which a platform rename takes too (inventory/ops.py) - a
            # selection naming a platform renamed or deleted since the page was read is refused, and only the fields
            # this section changed are written (the whole row put back what a rename had just changed)
            fields = [f"{k}_encrypted" if k.endswith(("password", "secret")) else k for k in changed]
            with transaction.atomic():
                PortalSettings.objects.select_for_update().filter(pk=obj.pk).first()
                known = set(Platform.objects.values_list("key", flat=True))
                gone = [k for k in new["fe_backup_platforms"] if k not in known]
                # commit-080: the chosen login checked again under the lock a secret's replacement takes too - still
                # active, with an ssh private key (a replacement may have moved it on meanwhile)
                if "fe_backup_login" in changed and login_:
                    from inventory.credentials import find
                    from inventory.models import Secret

                    sec = find(login_)
                    if sec is None or sec.state != Secret.State.ACTIVE or not sec.has("private_key"):
                        messages.error(request, _("Choose a secret with an ssh key to connect to the Front-Ends."))
                        return _settings_back(request)
                if "fe_backup_platforms" in changed and gone:
                    messages.error(
                        request,
                        _("The platforms changed meanwhile (%(k)s): reload the page and choose them again.")
                        % {"k": ", ".join(gone)},
                    )
                    return _settings_back(request)
                obj.updated_by = request.user
                obj.save(update_fields=[*fields, "updated_by", "updated_at"])
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
    if request.method == "POST" and request.POST.get("section") == "portal_general":
        return _save_portal_general(request, obj)
    if request.method == "POST" and request.POST.get("section") == "mail":
        return _save_mail(request, obj)
    if request.method == "POST" and request.POST.get("section") == "mail_test":
        return _mail_test(request, obj)
    if request.method == "POST" and request.POST.get("section") in (
        "report",
        "report_on",
        "report_redirect",
        "report_send",
    ):
        return _save_report(request, obj)
    if request.method == "POST" and request.POST.get("section") == "report_redirect_to":
        return _save_report_redirect_to(request, obj)
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
    if request.method == "POST" and request.POST.get("section") in ("passwords", "security"):
        return _save_security(request, obj)
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
                # commit-040: the S3 region is not on the page any more (owner 2026-11-17: "Drop it") - a stored one
                # is kept, never overwritten by a form that does not send it
                if f not in mine or (f == "backup_s3_region" and f not in request.POST):
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
    from . import appliance
    from .policy import LIMITS, policy, rules

    if bound_appliance is not None:  # back where the errors are
        part = request.POST.get("part") or (
            "backups" if any(k.startswith("backup_") for k in bound_appliance.errors) else "mail"
        )
        tab = part
    else:
        tab = request.GET.get("tab") or "customer"
    if tab == "platforms":  # owner 2026-11-23 (U1, Q1): the baseline and the installation media are under Profiles
        return redirect(reverse("infra_baseline"))
    tab = OLD_TABS.get(tab, tab)
    if tab not in SETTINGS_TABS:
        tab = "customer"
    extra_haproxy = {}
    if tab == "customer":  # owner 2026-11-15: "move lets encrypt email to platfrom page"
        from . import haproxy

        extra_haproxy = {"le_email": haproxy.get(obj)["le_email"], "branding": _branding_context(obj)}
    extra = {}
    if tab == "portal":
        from . import ha_views

        extra = ha_views.context(request, obj)
    if tab == "backups":  # u04
        from inventory.credentials import ref_choices

        from . import edition as _edn
        from . import fe_backup

        s3 = _edn.ee("s3")  # S3 is the Enterprise edition's (editions split step 4)
        extra.update(
            fe_platforms=fe_backup.platforms(),
            fe_login_choices=ref_choices("account"),  # owner 2026-11-18: Connect to Front-Ends using
            s3_ui=s3 is not None,
            s3_policy=s3.bucket_policy(obj.backup_s3_bucket or "<bucket>", obj.backup_s3_prefix or "") if s3 else "",
        )
    if tab == "portal":
        from . import timers

        extra["timers"] = timers.rows()
    if tab == "reports":
        from . import reports

        extra.update(
            report_rows=reports.rows(),
            report_conf=reports.config(),
            report_days=reports.DAYS,
            report_roles=reports.ROLES,
            report_templates=reports.templates(),
        )
    if tab == "mail":
        from . import appliance as _ap
        from . import maildns, mailtemplates

        _nodes = maildns.nodes()
        _recs = maildns.records(obj, _nodes)
        extra.update(
            mail_templates=mailtemplates.listing(with_text=True),
            mail_nodes=[{**nd, "ip": (obj.mail_public_ips or {}).get(nd["site"], "")} for nd in _nodes],
            dns_records=_recs,
            dns_missing=maildns.summary(_recs),
            dkim_domain=_ap.dkim_domain(obj),
            dkim_selector=_ap.dkim_selector(obj),
        )
    extra.update(extra_haproxy)
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
            "portal_backup_rows": _backup_rows(
                _backups["local"], _backups["s3"], obj.backup_last_at, obj.backup_last_result
            ),
            "fe_backup_rows": _backup_rows(
                _backups.get("fe_local") or [],
                _backups.get("fe_s3") or [],
                obj.fe_backup_last_at,
                obj.fe_backup_last_result,
            ),
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
            "unsafe_list": list(__import__("accounts.policy", fromlist=["UNSAFE"]).UNSAFE),
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


TOTP_SETUP_KEEP = timedelta(minutes=10)
TOTP_SETUP_TRIES = 5  # commit-372: SQLite only - a locked database is tried again


def _totp_setup_device(user, posted):
    """The pending key the set-up page shows or checks. The posted code checks the key shown last. Opened again within
    TOTP_SETUP_KEEP (a reload, a second tab, a background fetch), the page shows the same key: a new key there left the
    QR code already scanned useless (owner 2026-11-22: "6 digit code ignored"); later, a new key. commit-371: looked
    up and replaced under a lock on the user's row, so two pages opened together show one key, not two. commit-372:
    SQLite (the fallback database) has no row lock; there the second request can find the database locked - it waits
    and runs the whole lookup again, so it reads the key the first one made."""
    import time

    from django.db import OperationalError, connection

    for attempt in range(TOTP_SETUP_TRIES):
        try:
            with transaction.atomic():
                User.objects.select_for_update().filter(pk=user.pk).first()
                device = totp_device(user, confirmed=False)
                fresh = (
                    device is not None and device.created_at and timezone.now() - device.created_at < TOTP_SETUP_KEEP
                )
                if device is None or (not posted and not fresh):
                    device = new_totp_device(user)
                return device
        except OperationalError as exc:
            last = attempt == TOTP_SETUP_TRIES - 1
            if connection.vendor != "sqlite" or "locked" not in str(exc) or last:
                raise
            time.sleep(0.2 * (attempt + 1))


@login_required
def totp_setup_view(request):
    user = request.user
    device = _totp_setup_device(user, request.method == "POST")
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
