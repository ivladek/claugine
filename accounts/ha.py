"""This node's role and what it allows — the switch between the editions' implementations.

Every caller imports this module (`from accounts import ha`) and nothing else, so the editions split moves code
behind it without touching fifteen importers (Phase 4, worklog/tasks/T16):

    ee/ha.py           the pair (active / standby / maintenance / new): Enterprise builds only
    accounts/single.py one node (active / maintenance): the public Standard implementation

Step 1 (v00.16.12) made this the switch with the pair code still public (`accounts/pair.py`). Step 2 (2026-11-11)
moved that to `ee/ha.py` and made `single` the fallback, after production took the Enterprise releases: a public
snapshot now has one node only. `single.role()` answers `maintenance`, not `active`, for a pair role it does not
know on a node with a peer — so a Standard build that lands on half of a pair never claims to be active.

The functions here are thin on purpose: `role()` is looked up through this module, so `mock.patch("accounts.ha.role")`
in a test still governs `is_active()` and `forbid_writes()`, as it did before the switch existed.
"""
from . import edition, single

IMPL = edition.ee("ha") or single
ROLES = IMPL.ROLES
STATUS_PATHS = IMPL.STATUS_PATHS
RoleMiddleware = IMPL.RoleMiddleware
ReadOnlyHere = single.ReadOnlyHere       # one class in every build, so `except ha.ReadOnlyHere` always catches it


def role():
    return IMPL.role()


def is_active():
    return role() == "active"


def has_peer():
    return IMPL.has_peer()


def forbid_writes(what):
    """Changing a platform, a router or an iLO is refused on a node that is not active (a new version under test)."""
    r = role()
    if r != "active":
        raise ReadOnlyHere(f"{what}: refused on this portal node ({r}): only the active portal changes the platforms")


def status_view(request, name):
    return single.status_response(role(), name)
