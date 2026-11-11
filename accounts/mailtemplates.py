"""corrections-18: the e-mail templates in Settings → Mail.

Each e-mail of the portal is rendered from templates/emails/<name> (text; some also have an .html version). An edited
template is kept in the database (MailTemplate) and used instead of the file until it is reset. The text is a Django
template: variables are written {{ name }}; a template that does not compile is not saved.
"""
import os

from django.conf import settings
from django.utils.translation import gettext_lazy as _l

WHAT = {
    "tenant_ready_admin.txt": _l("Project provisioned: access details to the project admin and organization admins (text)"),
    "tenant_ready_admin.html": _l("Project provisioned: access details (HTML)"),
    "tenant_ready_staff.txt": _l("Project provisioned: notice to its platform users (text)"),
    "tenant_ready_staff.html": _l("Project provisioned: notice to its platform users (HTML)"),
    "tenant_pending.txt": _l("A project waits for approval (approvers)"),
    "tenant_rejected.txt": _l("Project request not approved"),
    "auto_update_failed.txt": _l("Automatic update stopped (super admins)"),
    "report_resources.txt": _l("Report: usage and changes (resource managers)"),
    "report_resources.html": _l("Report: usage and changes (HTML)"),
    "report_sales.txt": _l("Report: organizations of a sales rep"),
    "report_sales.html": _l("Report: organizations of a sales rep (HTML)"),
    "report_kam.txt": _l("Report: organizations of a key account manager"),
    "report_kam.html": _l("Report: organizations of a key account manager (HTML)"),
    "report_support.txt": _l("Report: internal organizations (support engineers)"),
    "report_support.html": _l("Report: internal organizations (HTML)"),
    "report_statistics.txt": _l("Report: portal statistics (super admins)"),
    "report_statistics.html": _l("Report: portal statistics (HTML)"),
    "tenant_move_requested.txt": _l("A move to another organization waits for approval"),
    "tenant_deletion_requested.txt": _l("A project deletion waits for approval"),
    "tenant_contract_ends.txt": _l("Expiration notice: the contract ends"),
    "tenant_poc_ends.txt": _l("Expiration notice: the POC ends"),
    "tenant_credentials.txt": _l("Credentials (older form)"),
    "network_requested.txt": _l("A network change waits for approval"),
    "org_registration_pending.txt": _l("A new organization waits for approval"),
    "org_registration_approved.txt": _l("Organization registered: sign in and order"),
    "org_registration_rejected.txt": _l("Organization registration not approved"),
    "org_assigned.txt": _l("An organization assigned to a sales rep"),
    "organization_invitation.txt": _l("Invitation to register an organization"),
    "member_invitation.txt": _l("Invitation of an organization user"),
    "staff_invitation.txt": _l("Invitation of a platform user"),
    "join_request_owner.txt": _l("Request to join an organization (its admins)"),
    "join_request_staff.txt": _l("Request to join an organization (staff)"),
    "join_decided.txt": _l("Join request decided"),
    "verify_email.txt": _l("Confirm the e-mail address"),
    "super_admin_removal_requested.txt": _l("Super admin role: removal requested"),
    "super_admin_removal_closed.txt": _l("Super admin role: removal closed"),
    "registration_report.txt": _l("Daily report on registration requests (super admins)"),
    "registration_report.html": _l("Daily report on registration requests (HTML)"),
    "password_reset.txt": _l("Password reset"),
    "password_reset_subject.txt": _l("Password reset: subject"),
}


def folder():
    return os.path.join(settings.BASE_DIR, "templates", "emails")


def names():
    return sorted(n for n in os.listdir(folder()) if not n.startswith("_") and n.endswith((".txt", ".html")))


def default_text(name):
    with open(os.path.join(folder(), name), encoding="utf-8") as f:
        return f.read()


def override(name):
    from .models import MailTemplate
    return MailTemplate.objects.filter(name=name).first()


def listing():
    from .models import MailTemplate
    edited = {m.name: m for m in MailTemplate.objects.all()}
    return [{"name": n, "what": WHAT.get(n, ""), "edited": edited.get(n)} for n in names()]


def compile_error(text):
    """"" when the text is a valid Django template, else the error."""
    from django.template import Engine, TemplateSyntaxError
    try:
        Engine.get_default().from_string(text)
    except TemplateSyntaxError as exc:
        return str(exc)
    return ""


def render(name, ctx):
    """The template `name` (emails/<name>) rendered: the edited one when there is one, else the file. Raises
    TemplateDoesNotExist when neither exists."""
    from django.template import Context, Engine
    from django.template.loader import get_template
    m = override(name)
    if m is not None:
        return Engine.get_default().from_string(m.body).render(Context(ctx, autoescape=not name.endswith(".txt")))
    return get_template(f"emails/{name}").render(ctx)
