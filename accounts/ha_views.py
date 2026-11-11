"""Settings → Portal: this node, its licence and its updates; Settings → Provider: the public URL.

Editions split, step 3 (2026-11-11): what only a pair has — the operations between the nodes, adding or replacing
the second node — is `ee/ha_views.py` (and its template `ee/templates/staff/_portal_pair.html`), reached through
`edition.ee("ha_views")`. A Standard build shows one node: its licence, the upgrade to Enterprise and its automatic
updates. Every node installs bundles from the control plane (v01.00.00).
"""
import re

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.validators import URLValidator
from django.core.exceptions import ValidationError
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext as _

from .models import audit
from . import ha
from . import thisnode

TEST_PATH = "/test-new-version/"  # owner 2026-10-31: the standby's version under the public URL (HAProxy, Apache)


def test_url():
    from . import appliance
    return appliance.public_url().rstrip("/") + TEST_PATH


def _back(tab="portal"):  # corrections-18: public names under Provider, the nodes under Backups
    return redirect(reverse("portal_settings") + f"?tab={tab}")


def post(request, obj):
    if not request.user.is_super_admin:
        raise PermissionDenied
    if request.POST.get("section") == "auto_update":  # owner 2026-10-31: off by default
        on = request.POST.get("auto_update") == "on"
        if on != obj.auto_update:
            obj.auto_update, obj.updated_by = on, request.user
            if on:  # switched on again: a release that failed before is tried again
                obj.auto_update_state = {**(obj.auto_update_state or {}), "failed_tag": "", "checked_at": ""}
            obj.save()
            audit(request.user, "setting_changed", obj, auto_update=on)
        from . import edition
        stream = request.POST.get("update_stream")
        if stream and not edition.allows("editable_update_settings", obj):  # Standard: the stream is pinned
            messages.error(request, _("The update stream is fixed in the Standard edition."))
            return _back()
        if stream in ("stable", "all") and stream != obj.update_stream:
            obj.update_stream = stream
            obj.save(update_fields=["update_stream"])
            audit(request.user, "setting_changed", obj, update_stream=stream)
            messages.success(request, _("Update stream saved."))
        else:
            messages.success(request, _("Automatic updates on.") if on else _("Automatic updates off."))
        return _back()
    if request.POST.get("section") == "publishing":
        changed = {}
        for f in ("public_url",):  # the test address is the public URL + /test-new-version/ (release 226)
            v = (request.POST.get(f) or "").strip().rstrip("/")
            if v:
                try:
                    URLValidator(schemes=["https", "http"])(v)
                except ValidationError:
                    messages.error(request, _("Not a URL: %(u)s") % {"u": v})
                    return _back("provider")
            if v != getattr(obj, f):
                setattr(obj, f, v)
                changed[f] = v
        if changed:
            obj.updated_by = request.user
            obj.save()
            from . import appliance
            appliance.forget()
            audit(request.user, "setting_changed", obj, **changed)
        messages.success(request, _("Settings saved."))
        return _back("provider")
    if request.POST.get("section") == "upgrade_enterprise":  # 2026-11-10 (phase 6): Standard → Enterprise
        from . import autoupdate, edition, ha_agent
        if not request.user.is_super_admin:
            messages.error(request, _("Only a super admin can do this."))
            return _back()
        if not edition.allows("second_node") or ha_agent.runs_enterprise():
            messages.error(request, _("Nothing to upgrade: the licence is not Enterprise, or this node already runs the "
                                      "Enterprise edition."))
            return _back()
        autoupdate._save(upgrade_requested=True, upgrade_requested_by=request.user.email, upgrade_result="")
        audit(request.user, "ha_upgrade_enterprise_requested", obj)
        messages.success(request, _("Requested. Within a few minutes this node takes a backup and installs the Enterprise "
                                    "edition; the portal is unavailable for a minute while it does."))
        return _back()
    from . import edition
    pair = edition.ee("ha_views")
    if pair is None:  # Standard: one node, nothing to switch, add or point elsewhere
        messages.error(request, _("This needs the Enterprise edition: the Standard edition runs one node."))
        return _back()
    return pair.post(request, obj)


def context(request, obj):
    from . import ha_agent, tasks
    n = thisnode.env()
    nodes = ha_agent.nodes_status() if (n.get("ADDR") and settings.HA_TOKEN) else []
    me = next((s for label, _a, s in nodes if label == "this node" and "error" not in s), None)
    other = next((s for label, _a, s in nodes if label == "other node" and "error" not in s), None)
    for _s in (me, other):
        _mark_bundle(_s)
    busy = tasks.running_count()
    from . import checkin
    from django.conf import settings as _dj
    _st = obj.auto_update_state or {}
    from .edition import effective as _ed_effective   # Standard pins the stream: show what is in force
    _lic = {"configured": checkin.configured(), "url": getattr(_dj, "CHECKIN_URL", ""),
            "install_id": getattr(_dj, "INSTALL_ID", ""), "state": _st.get("lic_state", ""),
            "grace_until": _st.get("grace_until", ""), "notices": _st.get("notices") or [],
            "checked_at": _st.get("checked_at", ""), "stream": _ed_effective(obj, "update_stream")}
    from . import autoupdate as _au, edition
    from . import ha_agent as _hag
    _ust = obj.auto_update_state or {}
    _enterprise_code = _hag.runs_enterprise()
    _upgrade = {"available": edition.allows("second_node") and not _enterprise_code and request.user.is_super_admin
                             and ha.is_active(),
                "requested": bool(_ust.get("upgrade_requested")), "result": _ust.get("upgrade_result", ""),
                "at": _ust.get("upgrade_at", "")}
    from . import edition as _ed
    pair = _ed.ee("ha_views")
    extra = pair.context(request, obj, n, nodes, me, other) if pair else {}
    return {"pair_ui": bool(pair and nodes), **extra, "upgrade": _upgrade, "auto_update": obj.auto_update, "auto_update_state": obj.auto_update_state or {},
            "update_stream": edition.effective(obj, "update_stream"), "checkin_configured": checkin.configured(), "licence": _lic,
            "update_locked": not edition.allows("editable_update_settings", obj), "update_window": _au.window_text(obj),
            "ha_node": n, "ha_role": ha.role(), "ha_nodes": nodes, "ha_busy": busy,
            "ha_running": any((s.get("op") or {}).get("state") == "running" for _l, _a, s in nodes if "error" not in s)}


BUNDLE_RE = re.compile(r"^v\d+\.\d+\.\d+$")


def _mark_bundle(s):
    """A node reports its release (vNN.NN.NN) as its commit: show that as its release."""
    if s and BUNDLE_RE.match(s.get("commit") or ""):
        s["bundle"] = True
        s["tag"] = s["commit"]
