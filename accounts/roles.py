"""Staff roles: who holds which role, kept complete.

Every role (super admin, resource manager, support engineer, sales manager, key account manager) always has
at least one activated holder. Only activated people (can sign in, password set) hold roles that count; with
a single activated person, that person (the super admin) holds all of them. Several super admins can split
the roles between them. A user holding several roles approves once for all of them (tenants.views).
"""

from django.db import transaction
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l

ROLE_COLUMNS = [
    ("is_super_admin", _l("Super admin")),
    ("is_resource_manager", _l("Resource manager")),
    ("is_support", _l("Support engineer")),
    ("is_sales", _l("Sales manager")),
    ("is_kam", _l("Key account manager")),
]
FIELDS = [f for f, _l2 in ROLE_COLUMNS]


def activated(u):
    return bool(u.is_active and u.login_enabled and u.has_usable_password())


def staff():
    from .models import Kind, User

    return list(User.objects.filter(kind=Kind.STAFF).order_by("-is_super_admin", "full_name", "email"))


def matrix():
    """Rows for the Users → Roles table: {u, active, cells: [(field, on, locked)]}, and whether all is forced."""
    people = staff()
    active = [u for u in people if activated(u)]
    single = len(active) == 1
    rows = []
    for u in people:
        a = activated(u)
        rows.append(
            {
                "u": u,
                "active": a,
                "cells": [(f, (True if single and a else getattr(u, f)), not a or single) for f in FIELDS],
            }
        )
    return rows, single


def missing(people=None, require_activated=True):
    """Roles without a holder. A holder must be activated (can sign in) by default; with require_activated=False a
    user still on the books — not blocked, not archived, even one not yet invited — counts too (owner 2026-11-03: a
    role may be freed from an active user as long as any user, including an inactive one, still holds it)."""
    people = people if people is not None else staff()

    def holds(u, f):
        if not getattr(u, f):
            return False
        return activated(u) if require_activated else (u.is_active and u.archived_at is None)

    return [f for f in FIELDS if not any(holds(u, f) for u in people)]


def ensure_complete(actor=None):
    """Keep every role held by someone. Only a role that NO user holds at all — not even an inactive, not-yet-invited
    directory holder — is given to the activated super admins (or, on a fresh install with a single activated person,
    to that person). owner 2026-11-03: a role already held by any user, including a directory sales rep or KAM, is
    left alone and never forced onto an active user (that was assigning directory roles to support engineers)."""
    from .models import audit

    people = staff()
    active = [u for u in people if activated(u)]
    if not active:
        return []
    miss = missing(people, require_activated=False)  # roles nobody holds at all
    if not miss:
        return []
    targets = active if len(active) == 1 else ([u for u in active if u.is_super_admin] or active[:1])
    changed = []
    with transaction.atomic():
        for f in miss:
            for u in targets:
                if not getattr(u, f):
                    setattr(u, f, True)
                    u.save(update_fields=[f])
                    changed.append((u, [f]))
    for u, fields in changed:
        audit(actor, "staff_roles_completed", u, roles=",".join(fields))
    return changed


def save(actor, posted):
    """Apply {user id: set of fields} for the activated people. Returns a list of errors (nothing saved then)."""
    from .models import audit

    people = staff()
    active = [u for u in people if activated(u)]
    if len(active) == 1:
        ensure_complete(actor)
        return []
    new = {}
    for u in people:
        new[u.pk] = {f: (f in posted.get(u.pk, set())) if activated(u) else getattr(u, f) for f in FIELDS}
    errors = []
    # owner 2026-11-03: an inactive (not-yet-invited) holder keeps a role too, so a role may be freed from an active
    # user as long as any user still holds it.
    holders = [u for u in people if u.is_active and u.archived_at is None]
    for f, label in ROLE_COLUMNS:
        if not any(new[u.pk][f] for u in holders):
            errors.append(_("%(r)s: at least one user must hold it.") % {"r": label})
    if not new[actor.pk]["is_super_admin"]:
        errors.append(_("You cannot take the super admin role from yourself."))
    if errors:
        return errors
    with transaction.atomic():
        for u in active:
            changed = [f for f in FIELDS if getattr(u, f) != new[u.pk][f]]
            if not changed:
                continue
            for f in changed:
                setattr(u, f, new[u.pk][f])
            if "is_super_admin" in changed:
                u.is_staff = u.is_superuser = u.is_super_admin
                changed += ["is_staff", "is_superuser"]
            u.save(update_fields=changed)
            audit(actor, "staff_roles_changed", u, roles=",".join(u.staff_role_labels) or "-")
    return []
