"""Correction 16: super admins.

A super admin has every right whatever roles are ticked; the roles ticked decide which approvals and notices come
to them (a super admin without the resource manager role is not asked to approve as one). Nobody is bound to being
the first user: every super admin's roles can be edited, also their own, as long as every role keeps an active user.

The super admin role itself:
    - nobody takes it from themselves;
    - another super admin asks to take it away; every super admin is told;
    - it is taken away only when every other super admin (but the one asking and the one concerned) confirms within
      48 hours; one refusal ends the request, and so does the deadline.
"""

from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from .mailtemplates import T
from .models import Kind, SuperAdminRemoval, User, audit


def active_super_admins():
    from .roles import activated

    return [u for u in User.objects.filter(kind=Kind.STAFF, is_super_admin=True) if activated(u)]


def required_for(count):
    """owner u12: how many super admins must agree (the asker counts as one), from the current super-admin count:
    1→1, 2→2, 3→2, 4 or more→3."""
    return 1 if count <= 1 else 2 if count <= 3 else 3


def approvals(req):
    """How many super admins have agreed so far: the asker plus everyone who has confirmed."""
    return 1 + len([p for p in (req.confirmed or [])])


def is_ready(req):
    return approvals(req) >= req.required


def deciders(req):
    """Every super admin who may confirm (the asker and, for a removal, the target cannot)."""
    return [u for u in active_super_admins() if u.pk not in (req.target_id, req.requested_by_id)]


def needed(req):
    """The super admins who may still confirm and have not yet."""
    return [u for u in deciders(req) if u.pk not in (req.confirmed or [])]


def pending():
    expire_due()
    return list(
        SuperAdminRemoval.objects.filter(state=SuperAdminRemoval.State.PENDING).select_related("target", "requested_by")
    )


def pending_for(user):
    return next((r for r in pending() if r.target_id == user.pk), None)


def _tell(req, template, subject, **extra):
    from .utils import notify, site_link

    people = {u.pk: u for u in active_super_admins()}
    people.setdefault(req.target_id, req.target)
    notify(
        list(people.values()),
        template,
        {
            "req": req,
            "link": site_link(reverse("staff_users") + "?tab=platform"),
            "hours": SuperAdminRemoval.HOURS,
            **extra,
        },
        subject,
    )


def _request(actor, target, op, archive=False):
    """Ask to grant or take the super admin role. Returns (request or None, error). archive: the person is archived
    once the role is taken (core-016)."""
    if not actor.is_super_admin:
        return None, _("Only a super admin can do this.")
    if op == SuperAdminRemoval.Op.REMOVE:
        if target.pk == actor.pk:
            return None, _("You cannot take the super admin role from yourself.")
        if not target.is_super_admin:
            return None, _("%(e)s is not a super admin.") % {"e": target.email}
    else:  # add
        if target.is_super_admin:
            return None, _("%(e)s is already a super admin.") % {"e": target.email}
    old = pending_for(target)
    if old is not None:
        if old.archive != archive:  # commit-029: never turned into another request with the agreement it had
            return None, _("A request for the super admin role of %(e)s is pending: decide it first.") % {
                "e": target.email
            }
        return old, ""
    req = SuperAdminRemoval.objects.create(
        target=target,
        requested_by=actor,
        confirmed=[],
        op=op,
        archive=archive,
        required=required_for(len(active_super_admins())),
    )
    verb = "requested" if op == SuperAdminRemoval.Op.REMOVE else "grant_requested"
    audit(
        actor,
        f"super_admin_{verb}",
        target,
        request=req.pk,
        required=req.required,
        deciders=", ".join(u.email for u in deciders(req)),
    )
    subj = (
        _("%(n)s: archive requested (a super admin)")
        if archive
        else _("Super admin role of %(n)s: removal requested")
        if op == SuperAdminRemoval.Op.REMOVE
        else _("Super admin role for %(n)s: requested")
    ) % {"n": target.full_name or target.email}
    _tell(req, T.SUPER_ADMIN_REMOVAL_REQUESTED, subj, deciders=deciders(req))
    complete_if_ready(req, actor)
    return req, ""


def request(actor, target, archive=False):
    """Ask to take the super admin role from `target` (and to archive them once it is taken: core-016)."""
    return _request(actor, target, SuperAdminRemoval.Op.REMOVE, archive=archive)


def request_add(actor, target):
    """owner u12: ask to grant the super admin role to `target` (needs the same confirmations as a removal)."""
    return _request(actor, target, SuperAdminRemoval.Op.ADD)


class _Lost(Exception):
    """The request was decided by someone else meanwhile (core-022)."""


def _lock(req):
    """core-022 (ninth full review): confirm, decline, expiry and completion work on the request row locked (SELECT
    ... FOR UPDATE, inside the caller's transaction) and read again - never on an object read before."""
    fresh = SuperAdminRemoval.objects.select_for_update().get(pk=req.pk)
    for f in ("state", "confirmed", "required", "archive", "finished_at", "finished_by_id"):
        setattr(req, f, getattr(fresh, f))
    return req


def confirm(actor, req):
    if not actor.is_super_admin:
        return _("This request is closed.")
    expire_due()
    with transaction.atomic():
        _lock(req)
        if req.state != SuperAdminRemoval.State.PENDING:
            return _("This request is closed.")
        if actor.pk not in [u.pk for u in deciders(req)]:
            return _("Your confirmation is not needed.")
        if actor.pk not in (req.confirmed or []):
            req.confirmed = list(req.confirmed or []) + [actor.pk]
            req.save(update_fields=["confirmed"])
            audit(actor, "super_admin_removal_confirmed", req.target, request=req.pk)
        complete_if_ready(req, actor)
    if req.state == SuperAdminRemoval.State.PENDING and is_ready(req):
        blocked = archive_blocked(req)
        if blocked:
            return _("Confirmed, but %(e)s is not archived yet: %(why)s") % {"e": req.target.email, "why": blocked}
    return ""


def decline(actor, req):
    if not actor.is_super_admin:
        return _("This request is closed.")
    if actor.pk not in [u.pk for u in deciders(req)] and actor.pk != req.requested_by_id:
        return _("Your confirmation is not needed.")
    with transaction.atomic():
        _lock(req)
        if not _finish(req, SuperAdminRemoval.State.DECLINED, actor):  # core-022: decided meanwhile
            return _("This request is closed.")
    audit(actor, "super_admin_removal_declined", req.target, request=req.pk)
    subj = (
        _("Super admin role of %(n)s: removal declined")
        if req.op == SuperAdminRemoval.Op.REMOVE
        else _("Super admin role for %(n)s: request declined")
    ) % {"n": req.target.full_name or req.target.email}
    _tell(req, T.SUPER_ADMIN_REMOVAL_CLOSED, subj, by=actor)
    return ""


def _finish(req, state, by=None):
    """The request's end - only while it is still pending (core-022: a decision made meanwhile is never overwritten).
    Returns whether it ended here."""
    now = timezone.now()
    n = SuperAdminRemoval.objects.filter(pk=req.pk, state=SuperAdminRemoval.State.PENDING).update(
        state=state, finished_at=now, finished_by=by
    )
    if n:
        req.state, req.finished_at, req.finished_by = state, now, by
    return bool(n)


def archive_blocked(req):
    """commit-028: an archive request completes only while the others still hold every staff role (checked again
    when it completes - a role may have been given up since it was asked for). The reason, or ""."""
    from .people import STAFF_ROLES
    from .roles import missing, staff

    if not req.archive:
        return ""
    left = missing([u for u in staff() if u.pk != req.target_id])
    if not left:
        return ""
    return _("Give %(r)s to someone else first.") % {"r": ", ".join(str(dict(STAFF_ROLES)[f]) for f in left)}


def retry_held(actor=None):
    """commit-031: archive requests the super admins agreed to, held back only by the role check (commit-028), tried
    again - after staff roles are saved, or when the archive is asked for again. The agreement they have stands; the
    deadline and the role check are checked again. Returns how many completed."""
    expire_due()
    done = 0
    for req in SuperAdminRemoval.objects.filter(state=SuperAdminRemoval.State.PENDING, archive=True):
        if is_ready(req) and complete_if_ready(req, actor):
            done += 1
    return done


def complete_if_ready(req, actor=None):
    """Apply the change once enough super admins have agreed (a removal still keeps at least one super admin)."""
    if req.state != SuperAdminRemoval.State.PENDING or not is_ready(req):
        return False
    target = User.objects.get(pk=req.target_id)
    try:
        with transaction.atomic():
            done = _complete(req, target, actor)
    except _Lost:
        return False
    if not done:
        return False
    verb = "removed" if req.op == SuperAdminRemoval.Op.REMOVE else "granted"
    audit(
        actor,
        f"super_admin_{verb}",
        target,
        request=req.pk,
        confirmed=", ".join(u.email for u in User.objects.filter(pk__in=req.confirmed or [])),
    )
    subj = (
        _("Super admin role of %(n)s: removed")
        if req.op == SuperAdminRemoval.Op.REMOVE
        else _("Super admin role for %(n)s: granted")
    ) % {"n": target.full_name or target.email}
    _tell(req, T.SUPER_ADMIN_REMOVAL_CLOSED, subj, by=actor, done=True)
    return True


def _complete(req, target, actor):
    """The change itself, in the caller's transaction, on the request locked and read again (core-022)."""
    _lock(req)
    if req.state != SuperAdminRemoval.State.PENDING or not is_ready(req):
        return False
    if archive_blocked(req):  # commit-028: stays pending until the roles are covered again
        return False
    if req.op == SuperAdminRemoval.Op.REMOVE:
        from .roles import missing

        target.is_super_admin = False
        others = [target if u.pk == target.pk else u for u in User.objects.filter(kind=Kind.STAFF)]
        if "is_super_admin" in missing(others):
            return False  # never leave the portal without a super admin
        target.is_staff = target.is_superuser = False  # the ticked roles stay; only the flag goes
    else:  # grant
        target.is_super_admin = target.is_staff = target.is_superuser = True
    target.save(update_fields=["is_super_admin", "is_staff", "is_superuser"])
    if not _finish(req, SuperAdminRemoval.State.DONE, actor):
        raise _Lost()  # decided meanwhile: the change above is rolled back
    if req.op == SuperAdminRemoval.Op.REMOVE and req.archive:  # core-016: the archive it was asked with
        from .people import archive_now

        archive_now(actor or req.requested_by, target)
    return True


def expire_due():
    """Requests past their 48 hours lapse (the role stays)."""
    now = timezone.now()
    for req in SuperAdminRemoval.objects.filter(state=SuperAdminRemoval.State.PENDING).select_related("target"):
        if now >= req.deadline:
            if not _finish(req, SuperAdminRemoval.State.EXPIRED):  # core-022: decided meanwhile
                continue
            audit(None, "super_admin_removal_expired", req.target, request=req.pk)
            subj = (
                _("Super admin role of %(n)s: removal lapsed")
                if req.op == SuperAdminRemoval.Op.REMOVE
                else _("Super admin role for %(n)s: request lapsed")
            ) % {"n": req.target.full_name or req.target.email}
            _tell(req, T.SUPER_ADMIN_REMOVAL_CLOSED, subj, expired=True)
