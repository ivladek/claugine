"""Automatic updates of the portal (owner 2026-10-31): Settings → Portal → Updates.

Off by default. When on, the active node checks in with the licence control plane (cp.example.com) every 10 minutes
(from the appliance timer, as root); the check-in names the release this installation may take, and the node
installs it as a bundle - one node by itself (update-single), a pair the standby first:

    1. update-standby   the standby installs the release (no database changes there)
    2. update-active    only when 1. succeeded: no tasks running (else it waits for the next round), a backup, then
                        this node installs the release with the database changes

When a step fails, nothing more is done for that release (the active node is not touched when the standby failed), the
super admins get an e-mail, and auto-update waits for a newer release. The state (last check, release, result) is kept
in PortalSettings.auto_update_state and shown in Settings.
"""
from datetime import datetime, timedelta

from django.utils import timezone

EVERY = timedelta(minutes=10)

# owner 2026-11-08 (editions): the Standard edition installs releases in **one window a week** (Sunday 07:02), pinned
# in accounts/edition.STANDARD_FIXED. The window is an hour wide because the timer only ticks every ten minutes: a
# one-minute window would be missed five times out of six. No window is pinned for Enterprise (no such setting
# exists yet), and where none is in force nothing changes - updates go on as soon as the control plane offers one.
WINDOW = timedelta(hours=1)
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _window_start(s, now=None):
    """The start of the window that is or was most recently open, or None when no window is pinned."""
    from accounts import edition
    day, at = edition.effective(s, "update_weekday"), edition.effective(s, "update_time")
    if day is None or not at:
        return None
    try:
        hh, mm = (int(x) for x in str(at).split(":")[:2])
        day = int(day)
    except (TypeError, ValueError):
        return None          # an unreadable pin must never stop updates
    if not (0 <= day <= 6 and 0 <= hh <= 23 and 0 <= mm <= 59):
        return None          # nor may a pin out of range: a window no one can reach would freeze the installation
    local = timezone.localtime(now or timezone.now())
    start = local.replace(hour=hh, minute=mm, second=0, microsecond=0) - timedelta(days=(local.weekday() - day) % 7)
    return start - timedelta(days=7) if start > local else start


def in_window(s, now=None):
    """True when an update may be installed now: inside the pinned window, or whenever none is pinned."""
    start = _window_start(s, now)
    if start is None:
        return True
    return timezone.localtime(now or timezone.now()) - start < WINDOW


def window_text(s):
    """The pinned window as an operator reads it ("Sunday 07:02"), or "" when none is pinned."""
    from accounts import edition
    day, at = edition.effective(s, "update_weekday"), edition.effective(s, "update_time")
    if day is None or not at:
        return ""
    try:
        day = int(day)
    except (TypeError, ValueError):
        return ""
    return f"{DAYS[day]} {at}" if 0 <= day <= 6 else ""


def _settings():
    from tenants.models import PortalSettings
    return PortalSettings.get()


def _save(**state):
    from tenants.models import PortalSettings
    s = _settings()
    new = {**(s.auto_update_state or {}), **state}
    PortalSettings.objects.filter(pk=s.pk).update(auto_update_state=new)
    return new


def _notify(subject, text):
    from accounts.models import Kind, User
    from accounts.utils import notify
    admins = list(User.objects.filter(kind=Kind.STAFF, is_super_admin=True, is_active=True))
    if admins:
        notify(admins, "auto_update_failed", {"text": text}, subject)


def _vkey(tag):
    """A comparable tuple from a vMM.mm.pp tag, for the no-downgrade guard."""
    import re
    nums = re.findall(r"\d+", tag or "")
    return tuple(int(x) for x in nums[:4]) or (0,)


def tick(log=print, force=False):
    """One round (the appliance timer, as root). Returns what it did, for the log.

    The target release comes from the licence control plane (cp.example.com): the check-in names it, and the node
    installs it as a bundle. v01.00.00: without a check-in configured there is no update at all (a licence is what
    entitles an installation to releases). A pair installs the standby first, then the active node."""
    from accounts import ha, ha_agent
    s = _settings()
    # owner 2026-11-09 (test VM): auto_update gates the **install**, not the check-in. It used to gate both, and it
    # defaults to off, so a fresh installation never checked in at all: it never learned its edition (so Standard
    # enforced none of its limits - accounts/edition.py reads what the check-in cached), never learned grace,
    # frozen or retired, and never reported the version it runs. The check-in is how this node knows what it is.
    role = ha.role()
    if role != "active":
        return f"not the active node ({role})"
    state = s.auto_update_state or {}
    last = state.get("checked_at")
    now = timezone.now()
    if not force and last and now - datetime.fromisoformat(last) < EVERY and not state.get("upgrade_requested"):
        return "checked recently"
    try:  # the licence check-in must not depend on the agent or the updater being reachable
        gh = ha_agent.installed_status()
    except Exception as exc:
        log(f"auto-update: could not read the installed version ({exc})")
        gh = {}
    current_tag = gh.get("installed_tag") or ""

    from accounts import checkin
    if not checkin.configured():
        _save(checked_at=now.isoformat(), source="")
        return "no licence check-in configured: no updates"
    from accounts.edition import effective as _eff  # Standard is stable-only; the control plane enforces it too
    ok, data = checkin.check_in(current_tag, _eff(s, "update_stream"), checkin.usage())
    if not ok:  # outage / 401 unknown_licence / 400 bad_request → keep the current state, do not update
        _save(checked_at=now.isoformat())
        return f"check-in unreachable ({data.get('http') or data.get('error') or '?'})"
    if data.get("state") != "conflict":  # it answered under the FQDN id: the old one has done its job (v01.00.00)
        checkin.forget_previous_id()
    lic_state = data.get("state") or "active"
    # owner 2026-11-08 (editions): cache what the control plane says about the edition, and **only when it says
    # it**. A control plane that predates editions sends neither field; writing a default here would take the
    # second node and the S3 backups away from a running installation (accounts/edition.py).
    said = {}
    if "edition" in data:
        from accounts import edition as _edition
        said["edition"] = _edition.normalise(data.get("edition"))
    if isinstance(data.get("features"), dict):
        said["features"] = data["features"]
    _save(checked_at=now.isoformat(), source="control-plane", lic_state=lic_state,
          grace_until=data.get("grace_until") or "", notices=list(data.get("notices") or []),
          usage_flags=list(data.get("usage_flags") or []), latest=data.get("target_version") or "",
          **said)
    if lic_state == "retired":
        # claugine-web, 2026-11-09: this installation id was released from its licence. Terminal for this node
        # - the key is already free for a new install_id, and the check-in has no side effects, so we simply
        # stop acting on it. The portal itself keeps running, as with every other state.
        return "licence retired: this installation was released; it receives no more updates"
    # owner 2026-11-11: "expired is a state" - the licence's "to" date has passed (and its grace, if any). The
    # portal keeps running in every state; only updates stop. A state this portal does not know stops them too:
    # updating on a licence it cannot read is the wrong way to fail.
    if lic_state not in ("active", "grace"):
        return f"licence {lic_state}: updates stopped"
    # A Standard installation whose licence became Enterprise runs the Standard code; it installs the Enterprise
    # release when a super admin asks (Settings → Portal): the release the control plane names, or the one it runs.
    # Until then it takes no updates - an ordinary update would bring the Enterprise code unasked.
    if said.get("edition") == "enterprise" and not ha_agent.runs_enterprise():
        if not state.get("upgrade_requested"):
            _save(upgrade_available=True)
            return "Enterprise licence: switch to the Enterprise releases in Settings → Portal"
        if ha_agent.read_op().get("state") == "running":
            return "another operation is running"
        ref = data.get("target_version") or current_tag
        if not ref:
            return "upgrade waits: the installed release is not known yet"
        log(f"auto-update: upgrading to the Enterprise edition ({ref} from the bundle)")
        op = ha_agent.run_op("upgrade-enterprise", by=state.get("upgrade_requested_by") or "settings", ref=ref)
        ok = op.get("state") == "done"
        _save(upgrade_requested=False, upgrade_available=not ok, upgrade_result=(
            f"{ref}: the Enterprise edition from now on" if ok else f"{ref}: failed: {op.get('error', '')}")[:500],
            upgrade_at=now.isoformat(), at=now.isoformat())
        if not ok:
            _notify("The upgrade to Enterprise failed", f"{ref}: {op.get('error', '')}\nThe node runs what it ran before.")
        return f"upgrade to Enterprise: {'done' if ok else 'failed'}"
    tag = data.get("target_version") or ""
    if not tag or tag == current_tag:
        return f"up to date ({current_tag or 'unknown'})"
    if _vkey(tag) <= _vkey(current_tag):  # never step back: HA migrations are forward-only
        return f"target {tag} not newer than {current_tag}: skipped (no downgrade)"
    if tag == state.get("failed_tag"):
        return f"{tag} failed before: waiting for a newer release"

    if not s.auto_update:  # checked in, state cached; installing is what the setting turns off
        return "off (the licence was checked in)"
    # The window is checked here, after the check-in: the licence state and the edition must keep refreshing every
    # ten minutes whatever day it is. "Check now" (force) goes ahead outside the window - a super admin asking
    # explicitly is not the unattended schedule this limits.
    if not force and not in_window(s):
        return f"{tag} waits for the update window ({window_text(s)})"
    if ha_agent.read_op().get("state") == "running":
        return "another operation is running"
    from accounts.tasks import running_count
    if running_count():
        return "tasks are running: next round"
    from accounts import thisnode
    if not thisnode.read().get("PEER_ADDR"):  # 2026-11-10: one node (every Standard installation) - no standby
        log(f"auto-update: {tag} onto this node (it has no other node)")
        op = ha_agent.run_op("update-single", by="auto-update", ref=tag)
        if op.get("state") != "done":
            _save(failed_tag=tag, result=f"{tag}: failed: {op.get('error', '')}"[:500], at=now.isoformat())
            _notify(f"Auto-update to {tag} failed", f"This node could not install {tag}: {op.get('error', '')}")
            return f"{tag}: failed"
        _save(result=f"{tag}: installed", at=now.isoformat(), failed_tag="", upgrade_result="")
        return f"{tag}: installed"
    log(f"auto-update: {tag} onto the standby")
    op = ha_agent.run_op("update-standby", by="auto-update", ref=tag)
    if op.get("state") != "done":
        _save(failed_tag=tag, result=f"{tag}: the standby failed: {op.get('error', '')}"[:500], at=now.isoformat())
        _notify(f"Auto-update to {tag} stopped: the standby failed",
                f"The standby could not install {tag}: {op.get('error', '')}\nThe active node was not touched.")
        return f"{tag}: the standby failed"
    log(f"auto-update: {tag} onto the active node")
    op = ha_agent.run_op("update-active", by="auto-update", ref=tag)
    if op.get("state") != "done":
        _save(failed_tag=tag, result=f"{tag}: the active node failed: {op.get('error', '')}"[:500], at=now.isoformat())
        _notify(f"Auto-update to {tag} stopped: the active node failed",
                f"The standby runs {tag}; the active node could not install it: {op.get('error', '')}")
        return f"{tag}: the active node failed"
    # owner 2026-11-11 ("why is v00.16.13 noted?"): the upgrade's own line is news only until the next release is in
    _save(result=f"{tag}: installed on both nodes", at=now.isoformat(), failed_tag="", upgrade_result="")
    return f"{tag}: installed on both nodes"
