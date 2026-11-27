"""owner 2026-11-04 (migration to licensed updates): the portal's pull check-in to cp.example.com.

Once per round the installed portal POSTs its licence key, install id, running version, chosen update stream and usage
to the control plane, and gets back what to run and the licence state:

    POST  {CHECKIN_URL}/api/checkin
      { licence_key, install_id, current_version, stream, usage:{…} }
    → { state, grace_until, target_version, channel_effective, notices:[...], edition, features, delivery }

install_id is the installation's FQDN, the host of its site URL (owner 2026-11-11: "use fqdn of installation instead
of inst in installation id").

state is active | grace | expired | pending | conflict | retired (the portal itself never stops; only updates are
governed: they run on active and grace only). retired means this install_id was released from its licence: terminal for this node, and the
check-in has no side effects, so the replacement node can activate while this one keeps calling. An unknown
licence (HTTP 401) or a malformed request (400) is NOT a state: it is treated like the control plane being unreachable,
so the portal keeps its current state. Pull only — there is no inbound channel. The contract is shared with
claugine-web and changes only by agreement (project doc software-delivery.md).
"""

import json
import urllib.error
import urllib.request

from django.conf import settings


def configured():
    """True when the installer has planted the licence key, install id and check-in URL (licensed-update mode)."""
    return bool(
        getattr(settings, "CHECKIN_URL", "")
        and getattr(settings, "LICENCE_KEY", "")
        and getattr(settings, "INSTALL_ID", "")
    )


def usage():
    """Best-effort {sites, hosts, cores, tenants} for the usage report; missing counts are skipped by the server."""
    out = {}
    try:
        from tenants.models import GONE, Tenant

        # core-018: is_deleted is a property (status in GONE), not a field - the count is filtered on the status
        out["tenants"] = Tenant.objects.exclude(status__in=GONE).count()
    except Exception:  # noqa: BLE001 - usage is soft; a missing count is skipped
        pass
    try:
        from inventory.models import Host, Site

        out["sites"] = Site.objects.count()
        out["hosts"] = Host.objects.count()
    except Exception:  # noqa: BLE001
        pass
    return out


def check_in(current_version, stream, use=None, timeout=20):
    """POST the check-in. Returns (ok, data).

    ok=True  → the control plane answered (HTTP 200); data has state/grace_until/target_version/notices/usage_flags.
    ok=False → treat like an outage and keep the current state: a network error, a timeout, or an explicit 401
               (unknown licence) / 400 (bad request), none of which are a licence state.
    """
    url = getattr(settings, "CHECKIN_URL", "")
    if not url:
        return False, {"error": "not configured"}
    payload = {
        "licence_key": getattr(settings, "LICENCE_KEY", ""),
        "install_id": getattr(settings, "INSTALL_ID", ""),
        "current_version": current_version or "",
        "stream": stream or "stable",
        "usage": use if use is not None else usage(),
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url + "/api/checkin",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - our own control plane, https in prod
            return True, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        # 401 unknown_licence / 400 bad_request → not a state; keep current state (handled by the caller)
        return False, {"http": e.code}
    except Exception as e:  # noqa: BLE001 - network/timeout/parse → outage
        return False, {"error": str(e)[:200]}


def apply_countries(data):
    """claugine-web 2026-11-27 (job client-countries, the contract's "Countries"): the client's home and allowed
    countries come from cp.example.com with every answer that names the edition -
    ``countries: {home: "KZ", allowed: ["KZ", "AE"]}`` (ISO 3166-1 alpha-2). Kept in PortalSettings (home_country,
    allowed_countries) for the forms; an answer without it, or one that does not read (an unknown code, no home), changes
    nothing. Returns True when the settings changed."""
    c = (data or {}).get("countries")
    if not isinstance(c, dict):
        return False
    from accounts.taxid import countries_choices

    known = {code for code, _n in countries_choices()}
    home = str(c.get("home") or "").upper()
    allowed = c.get("allowed")
    if home not in known or not isinstance(allowed, list):
        return False
    codes = [str(x).upper() for x in allowed]
    if any(x not in known for x in codes):
        return False
    rest = []
    for x in codes:
        if x != home and x not in rest:
            rest.append(x)
    from tenants.models import PortalSettings

    s = PortalSettings.get()
    if (s.home_country, list(s.allowed_countries or [])) == (home, rest):
        return False
    s.home_country, s.allowed_countries = home, rest
    s.save(update_fields=["home_country", "allowed_countries"])
    from accounts.models import audit

    audit(None, "setting_changed", s, home_country=home, allowed_countries=",".join(rest), by="cp.example.com")
    return True
