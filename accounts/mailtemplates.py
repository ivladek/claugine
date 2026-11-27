"""corrections-18: the e-mail templates in Settings → Mail.

Each e-mail of the portal is one HTML template, templates/emails/<name>.html (owner 2026-11-17 17:19-17:41: "one
template must have one name", "base format is HTML"); the e-mail's plain-text part is made from the rendered HTML
(accounts.mailhtml.text_of). The password reset's subject line is the one text template. An edited template is kept in
the database (MailTemplate) and used instead of the file until it is reset. It is a Django template: variables are
written {{ name }}; a template that does not compile is not saved.
"""

import os

from django.conf import settings
from django.utils.translation import gettext_lazy as _l

WHAT = {
    "tenant_ready_admin.html": _l("Project provisioned: access details to the project admin and organization admins"),
    "tenant_ready_staff.html": _l("Project provisioned: notice to its platform users"),
    "tenant_pending.html": _l("A project waits for approval (approvers)"),
    "tenant_rejected.html": _l("Project request not approved"),
    "auto_update_failed.html": _l("Automatic update stopped (super admins)"),
    "backup_failed.html": _l("A backup failed (super admins)"),
    "mail_test.html": _l("Test message (Settings → Mail → Send test)"),
    "report.html": _l("Report: the weekly report of a role (super admin, support, manager / KAM, resources)"),
    "tenant_move_requested.html": _l("A move to another organization waits for approval"),
    "tenant_deletion_requested.html": _l("A project deletion waits for approval"),
    "tenant_contract_ends.html": _l("Expiration notice: the contract ends"),
    "tenant_poc_ends.html": _l("Expiration notice: the POC ends"),
    "tenant_credentials.html": _l("Credentials (older form)"),
    "network_requested.html": _l("A network change waits for approval"),
    "org_registration_pending.html": _l("A new organization waits for approval"),
    "org_registration_approved.html": _l("Organization registered: sign in and order"),
    "org_registration_rejected.html": _l("Organization registration not approved"),
    "org_assigned.html": _l("An organization assigned to a sales rep"),
    "organization_invitation.html": _l("Invitation to register an organization"),
    "member_invitation.html": _l("Invitation of an organization user"),
    "staff_invitation.html": _l("Invitation of a platform user"),
    "join_request_owner.html": _l("Request to join an organization (its admins)"),
    "join_request_staff.html": _l("Request to join an organization (staff)"),
    "join_decided.html": _l("Join request decided"),
    "verify_email.html": _l("Confirm the e-mail address"),
    "super_admin_removal_requested.html": _l("Super admin role: removal requested"),
    "super_admin_removal_closed.html": _l("Super admin role: removal closed"),
    "registration_report.html": _l("Daily report on registration requests (super admins)"),
    "password_reset.html": _l("Password reset"),
    "password_reset_subject.txt": _l("Password reset: subject"),
    "analysis_export.html": _l("Analysis → Usage sent by e-mail (the person who asked)"),
}
SUBJECTS = ("password_reset_subject.txt",)  # a subject line: text, not an e-mail of its own

# Owner 2026-11-17 18:52: "all contributors of email template must reference by id to it, not by name" - each template
# has a fixed id (never renumbered, never reused); the code sends by it (T.<NAME>); its name is the people's, editable.
IDS = {
    1: "auto_update_failed.html",
    2: "backup_failed.html",
    3: "join_decided.html",
    4: "join_request_owner.html",
    5: "join_request_staff.html",
    6: "mail_test.html",
    7: "member_invitation.html",
    8: "network_requested.html",
    9: "org_assigned.html",
    10: "org_registration_approved.html",
    11: "org_registration_pending.html",
    12: "org_registration_rejected.html",
    13: "organization_invitation.html",
    14: "password_reset.html",
    15: "password_reset_subject.txt",
    16: "registration_report.html",
    17: "report.html",
    18: "staff_invitation.html",
    19: "super_admin_removal_closed.html",
    20: "super_admin_removal_requested.html",
    21: "tenant_contract_ends.html",
    22: "tenant_credentials.html",
    23: "tenant_deletion_requested.html",
    24: "tenant_move_requested.html",
    25: "tenant_pending.html",
    26: "tenant_poc_ends.html",
    27: "tenant_ready_admin.html",
    28: "tenant_ready_staff.html",
    29: "tenant_rejected.html",
    30: "verify_email.html",
    31: "analysis_export.html",  # Enterprise (ee/templates/emails)
}
BY_FILE = {f: i for i, f in IDS.items()}


class T:
    """The templates' ids, for the code that sends them."""

    AUTO_UPDATE_FAILED = 1
    BACKUP_FAILED = 2
    JOIN_DECIDED = 3
    JOIN_REQUEST_OWNER = 4
    JOIN_REQUEST_STAFF = 5
    MAIL_TEST = 6
    MEMBER_INVITATION = 7
    NETWORK_REQUESTED = 8
    ORG_ASSIGNED = 9
    ORG_REGISTRATION_APPROVED = 10
    ORG_REGISTRATION_PENDING = 11
    ORG_REGISTRATION_REJECTED = 12
    ORGANIZATION_INVITATION = 13
    PASSWORD_RESET = 14
    PASSWORD_RESET_SUBJECT = 15
    REGISTRATION_REPORT = 16
    REPORT = 17
    STAFF_INVITATION = 18
    SUPER_ADMIN_REMOVAL_CLOSED = 19
    SUPER_ADMIN_REMOVAL_REQUESTED = 20
    TENANT_CONTRACT_ENDS = 21
    TENANT_CREDENTIALS = 22
    TENANT_DELETION_REQUESTED = 23
    TENANT_MOVE_REQUESTED = 24
    TENANT_PENDING = 25
    TENANT_POC_ENDS = 26
    TENANT_READY_ADMIN = 27
    TENANT_READY_STAFF = 28
    TENANT_REJECTED = 29
    VERIFY_EMAIL = 30
    ANALYSIS_EXPORT = 31


def file_of(tid):
    """The file of the template with this id; a name is refused (the code sends by id)."""
    if isinstance(tid, bool) or not isinstance(tid, int):
        raise TypeError(f"an e-mail template is referenced by its id (T.<NAME>), not by {tid!r}")
    return IDS[tid]


def id_of(value):
    """A stored reference (an id, or a name kept from before the ids) as the id; None when unknown."""
    if isinstance(value, int) and value in IDS:
        return value
    if isinstance(value, str):
        if value.isdigit() and int(value) in IDS:
            return int(value)
        for f, i in BY_FILE.items():
            if f.rsplit(".", 1)[0] == value or f == value:
                return i
    return None


def title(tid, s=None):
    """The template's name as people see and edit it (Settings → Mail); by default its file's name."""
    if s is None:
        from tenants.models import PortalSettings

        s = PortalSettings.get()
    return ((getattr(s, "mail_template_names", None) or {}).get(str(tid)) or "").strip() or label(IDS[tid])


def folder():
    return os.path.join(settings.BASE_DIR, "templates", "emails")


def folders():
    """templates/emails and, in the Enterprise edition, ee/templates/emails."""
    out = [folder()]
    ee = os.path.join(settings.BASE_DIR, "ee", "templates", "emails")
    if os.path.isdir(ee):
        out.append(ee)
    return out


def _path(name):
    for d in folders():
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return os.path.join(folder(), name)


def names():
    """The file name of each template: one per e-mail (.html) and the subject line(s) (.txt)."""
    return sorted(n for n in os.listdir(folder()) if not n.startswith("_") and (n.endswith(".html") or n in SUBJECTS))


def label(name):
    """The name the list shows: the e-mail's name, without the file's extension (one template, one name)."""
    return name.rsplit(".", 1)[0]


def default_text(name):
    with open(_path(name), encoding="utf-8") as f:
        return f.read()


def override(name):
    from .models import MailTemplate

    return MailTemplate.objects.filter(name=name).first()


def listing(with_text=False):
    """Every template with its description and its edited version; with_text: also the text in force (Settings →
    Mail shows it in the list's details, owner 2026-11-17)."""
    from tenants.models import PortalSettings

    from .models import MailTemplate

    s = PortalSettings.get()
    edited = {m.name: m for m in MailTemplate.objects.all()}
    out = []
    for n in names():
        tid = BY_FILE[n]
        row = {
            "id": tid,
            "name": n,
            "label": title(tid, s),
            "what": WHAT.get(n, ""),
            "edited": edited.get(n),
            "html": n.endswith(".html"),
        }
        if with_text:
            row["text"] = edited[n].body if n in edited else default_text(n)
            row["preview"] = preview(n, row["text"])
        out.append(row)
    return out


def compile_error(text):
    """ "" when the text is a valid Django template, else the error."""
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


def _preview_engine():
    """The portal's template engine, with a variable that has no value shown as ‹its name›."""
    from django.template import Engine

    e = Engine.get_default()
    return Engine(
        dirs=e.dirs,
        app_dirs=e.app_dirs,
        loaders=None if e.app_dirs else e.loaders,
        string_if_invalid="‹%s›",
        libraries=e.libraries,
        builtins=e.builtins[len(Engine.default_builtins) :],
        autoescape=True,
    )


def preview(name, text):
    """The template drawn as the e-mail looks (owner 2026-11-17 17:39 "show html templates as normal page"): its
    variables as ‹name›; the subject line as its text."""
    from django.template import Context

    try:
        out = _preview_engine().from_string(text).render(Context({}, autoescape=name.endswith(".html")))
    except Exception as exc:  # noqa: BLE001 - shown in place of the page
        return f"{type(exc).__name__}: {exc}"
    if name.endswith(".html"):
        from .mailhtml import linkify

        out = linkify(out)
    return out
