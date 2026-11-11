"""Correction 16: a new organization registered on the organizations' portal.

The person registering gives the organization's legal name, its BIN/IIN, the city (in Kazakhstan) and their own full
name, e-mail and phone (they become its organization admin), confirms the e-mail and so sends the request. Nobody of
the organization signs in until it is approved:
    1. the resource manager approves and assigns the organization's platform users: the sales rep (cloud
       organizations) and the support engineer are required, the key account manager is optional;
    2. the sales rep approves too when sales rep approval is on in Settings → Approvals (cloud organizations);
    3. then the organization admin gets an e-mail: they can sign in and order resources.
A refused registration is removed (the organization and its users never existed for the portal; the log keeps it).
A super admin may do every step; they are asked only in the roles ticked for them.
"""
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from accounts.models import Role, User, audit


def sales_required(org):
    from .models import OrgKind, PortalSettings
    return org.kind == OrgKind.CLOUD and PortalSettings.get().sales_approval_required


def steps(org):
    out = [{"key": "rm", "label": _("Resource manager"), "done": bool(org.reg_rm_at), "by": org.reg_rm_by,
            "at": org.reg_rm_at}]
    if sales_required(org):
        out.append({"key": "sales", "label": _("Sales rep"), "done": bool(org.reg_sales_at), "by": org.reg_sales_by,
                    "at": org.reg_sales_at})
    return out


def next_step(org):
    return next((s["key"] for s in steps(org) if not s["done"]), None) if org.is_pending else None


def _rm_holders():
    return list(User.objects.filter(kind="staff", is_active=True, login_enabled=True, is_resource_manager=True))


def _signs_in(u):
    return u is not None and u.is_active and u.login_enabled


def can(user, org, step):
    """May act on the step (a super admin: always)."""
    if not (user.is_authenticated and user.is_staff_member and org.is_pending):
        return False
    if user.is_super_admin:
        return True
    if step == "rm":
        return user.has_role(Role.RESOURCE_MANAGER)
    return step == "sales" and org.sales_rep_id == user.pk


def asked(org, step):
    """Who is asked (and told) for the step."""
    if step == "rm":
        return _rm_holders() or list(User.objects.filter(kind="staff", is_super_admin=True, is_active=True,
                                                         login_enabled=True))
    if _signs_in(org.sales_rep):
        return [org.sales_rep]
    supers = User.objects.filter(kind="staff", is_super_admin=True, is_active=True, login_enabled=True)
    return list(supers.filter(is_sales=True)) or list(supers)


def asks(user, org):
    step = next_step(org)
    return bool(step) and can(user, org, step) and user in asked(org, step)


def pending():
    from .models import Organization
    return list(Organization.objects.filter(registration="pending", archived_at__isnull=True)
                .select_related("sales_rep", "resource_manager", "support_engineer", "kam").order_by("created_at"))


def pending_rows(user):
    out = []
    for org in pending():
        step = next_step(org)
        admin = org.users.filter(is_org_admin=True).first()
        out.append({"org": org, "step": step, "steps": steps(org), "admin": admin, "people": asked(org, step) if step else [],
                    "mine": asks(user, org), "can": bool(step) and can(user, org, step)})
    return out


def pending_count(user):
    if not (user.is_authenticated and user.is_staff_member):
        return 0
    return sum(1 for org in pending() if asks(user, org))


def _tell_step(org):
    from accounts.utils import notify, site_link
    step = next_step(org)
    if not step:
        return
    admin = org.users.filter(is_org_admin=True).first()
    from accounts.regcheck import labels
    notify(asked(org, step), "org_registration_pending",
           {"org": org, "admin": admin, "step": step, "link": site_link(reverse("org_detail", args=[org.pk])),
            "checks": labels((org.reg_check or {}).get("flags", []))},
           (_("Organization registration waiting for your approval: %(n)s") if step == "sales"
            else _("New organization waiting for approval: %(n)s")) % {"n": org.name})


def registered(org, user):
    """The e-mail of a new organization's admin is confirmed: the request goes to the resource managers."""
    audit(user, "organization_registration_requested", org, tax_id=org.tax_id, city=org.city, admin=user.email)
    _tell_step(org)


def approve(actor, org, step, data=None):
    """Approve a step (the resource manager's with the platform users in `data`). Returns an error or ""."""
    if next_step(org) != step or not can(actor, org, step):
        return _("This step is not waiting for you.")
    now = timezone.now()
    with transaction.atomic():
        if step == "rm":
            for f in ("kind", "resource_manager", "support_engineer", "sales_rep", "kam"):
                if data is not None and f in data:
                    setattr(org, f, data[f])
            org.reg_rm_by, org.reg_rm_at = actor, now
            # one person in several roles approves once: the organization's sales rep approving as resource manager
            if sales_required(org) and org.sales_rep_id == actor.pk:
                org.reg_sales_by, org.reg_sales_at = actor, now
        else:
            org.reg_sales_by, org.reg_sales_at = actor, now
        if next_step(org) is None:
            org.registration = "approved"
        org.save()
    audit(actor, "organization_registration_approved", org, step=step,
          sales_rep=org.sales_rep.email if org.sales_rep else "", support_engineer=org.support_engineer.email if org.support_engineer else "",
          resource_manager=org.resource_manager.email if org.resource_manager else "", kam=org.kam.email if org.kam else "",
          done="yes" if org.registration == "approved" else "no")
    if org.registration == "approved":
        _welcome(org, actor)
    else:
        _tell_step(org)
    return ""


def _welcome(org, actor):
    from accounts.utils import notify, site_link
    admins = list(org.users.filter(is_org_admin=True, is_active=True))
    notify(admins, "org_registration_approved", {"org": org, "link": site_link(reverse("login"))},
           _("%(n)s is registered: you can order resources") % {"n": org.name},
           allow_client=True)  # client onboarding: always sent, even when e-mail to clients is off
    audit(actor, "organization_registration_done", org, to=", ".join(u.email for u in admins))


def reject(actor, org, reason):
    """Refuse: the registrant is told, the organization and its users are removed (the log keeps the record)."""
    from accounts.utils import notify
    step = next_step(org)
    if not step or not can(actor, org, step):
        return _("This step is not waiting for you.")
    users = list(org.users.all())
    notify([u for u in users if u.is_active], "org_registration_rejected", {"org_name": org.name, "reason": reason},
           _("Your organization's registration was not approved"))
    chk = org.reg_check or {}
    info = {"name": org.name, "tax_id": org.tax_id or "", "city": org.city, "users": ", ".join(u.email for u in users),
            "reason": reason[:500], "ip": chk.get("ip", ""), "score": chk.get("score", 0), "flags": ",".join(chk.get("flags", []))}
    with transaction.atomic():
        from accounts.models import Invitation
        for u in users:
            Invitation.objects.filter(user=u).delete()
            u.delete()
        org.join_requests.all().delete()
        org.delete()
    audit(actor, "organization_registration_rejected", None, **info)
    return ""
