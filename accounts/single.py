"""One node: the roles an installation has when there is no pair, and the page HAProxy polls.

The public counterpart of `ee/ha.py` (owner 2026-11-09, Phase 4). **The Standard edition is a complete one-node
portal, not an Enterprise build with pieces cut out** — so everything a single node genuinely needs lives here and
is public: HAProxy still polls `/active`, an operator can still take the portal down for maintenance, and the
timers still ask whether this node is the one that runs them.

What is *not* here is everything that only means something with a second node: the `standby` and `new` roles, the
node-to-node agent, and Settings → Publishing. Those are `ee/`, and **`ee/ha.py` extends this module rather than
replacing it** — so the status endpoint, the middleware and the maintenance page have exactly one implementation,
and a fix to the page a client sees cannot drift between the editions.

Read through `accounts.edition.ee("ha") or accounts.single`: with no `ee/` package the answer is this module.
"""

from . import thisnode

ROLES = ("active", "maintenance")
STATUS_PATHS = {f"/{r}" for r in ROLES} | {f"/{r}/" for r in ROLES}


def role():
    """This node's role: `active`, `maintenance`, or — for a word this build does not know — one or the other
    depending on whether node.env names a **peer**.

    This is the line that caused the 2026-11-09 incident, so the reasoning is written out.

    A role this build does not recognise means one of two very different situations:

    * **No PEER_ADDR.** One node, restored from an Enterprise backup or otherwise carrying `ROLE=standby` from a
      former life. Refusing to serve would take the only portal there is off the air, for a word. So: **active**.
    * **PEER_ADDR set.** This machine is half of a pair whose other half may be serving, and this build cannot tell
      which half it is. Claiming `active` here is the worst answer available: HAProxy polls `/active` to find the
      node to send traffic to, so a standby that says "active" gives it two, and this one's database is read only.
      So: **maintenance** — serve nothing, and let the node that does understand its role serve.

    What actually happened: a build without `ee/` landed on the standby, this function read `ROLE=standby`, did not
    know the word, and answered `active`. Both nodes then answered `/active` with 200.
    """
    env = thisnode.env()
    named = env.get("ROLE")
    if named in ROLES:
        return named
    if not named:
        return "active"  # no role named at all: a plain single node
    return "maintenance" if env.get("PEER_ADDR") else "active"


def is_active():
    return role() == "active"


def has_peer():
    """Always False: this module is what runs when there is no second node."""
    return False


class ReadOnlyHere(Exception):
    pass


def forbid_writes(what):
    """Refuse a change to a platform, a router or an iLO on a node that is not active."""
    r = role()
    if r != "active":
        raise ReadOnlyHere(f"{what}: refused on this portal node ({r}): only the active portal changes the platforms")


def status_response(r, name):
    """The body HAProxy reads. Takes the role rather than calling role(), so `ee/ha.py` — whose role set is wider —
    reuses this instead of copying it."""
    from django.http import HttpResponse

    resp = HttpResponse(f"{r}\n", content_type="text/plain", status=200 if r == name else 503)
    resp["Cache-Control"] = "no-store"
    return resp


def status_view(request, name):
    """What HAProxy polls: 200 when this node has the role it asked about, 503 otherwise."""
    return status_response(role(), name)


class RoleMiddleware:
    """maintenance: nothing but the status page."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path in STATUS_PATHS:
            return self.get_response(request)
        if role() == "maintenance":
            return page(request, 503, "maintenance")
        return self.get_response(request)


def page(request, status, kind):
    """The maintenance page — shared with `ee/ha.py`, so a client sees one page whatever the edition."""
    from django.shortcuts import render

    try:
        return render(request, "ha/unavailable.html", {"kind": kind, "status": status}, status=status)
    except Exception:  # noqa: BLE001 - no template or database: plain text
        from django.http import HttpResponse

        return HttpResponse(
            "The portal is under maintenance."
            if kind == "maintenance"
            else "This is a new portal version under test: only super admins can sign in.",
            status=status,
            content_type="text/plain",
        )
