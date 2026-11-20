"""Automatic updates of the portal (owner 2026-10-31): Settings → Portal → Updates.

Off by default. When on, the active node checks in with the licence control plane (cp.example.com) every 10 minutes
(from the appliance timer, as root); the check-in names the release this installation may take, and the node
installs it as a bundle - one node by itself (update-single), a pair as the "Update portal" button does
(update-portal, owner 2026-11-15): a backup, the standby installs the release, the standby takes over (the database
changes run there), then - its second half, on that node - the former active node installs it and the two switch
back.

When a step fails, nothing more is done for that release (a switch over that fails before the other node took over
leaves this node serving), the super admins get an e-mail, and auto-update waits for a newer release. The state (last check, release, result) is kept
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
        return None  # an unreadable pin must never stop updates
    if not (0 <= day <= 6 and 0 <= hh <= 23 and 0 <= mm <= 59):
        return None  # nor may a pin out of range: a window no one can reach would freeze the installation
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
    """Merge into the cached update state. Reads and writes that one column only (2026-11-11): after an update this
    process still runs the old code, and the new release may have dropped columns its model still lists (v01.00.06
    dropped one, and the "installed on both nodes" line was never written)."""
    from tenants.models import PortalSettings

    row = PortalSettings.objects.filter(pk=1)
    if not row.exists():
        PortalSettings.get()  # a new installation: the settings row is made once
    cur = row.values_list("auto_update_state", flat=True).first()
    new = {**(cur or {}), **state}
    row.update(auto_update_state=new)
    return new


STATE_KEYS = ("result", "at", "failed_tag", "upgrade_result", "finish_failed_tag")


def _unfinished_pair(current_tag):
    """The release the other node runs when it is not this node's (integration-002: an update whose second half
    stopped leaves the active node on the new release and the standby on the old one, and the check said "up to
    date"). Also this node's own release when the other node runs it but does not follow this node (full review
    2026-11-16, integration-002 again: a finish that installed the release and then failed its check or catch-up
    leaves both on one release and the standby not replicating). "" when there is no other node, it does not answer,
    or it runs the same release and replicates from this node."""
    from accounts import ha, ha_agent, thisnode

    peer = thisnode.read().get("PEER_ADDR", "")
    if not peer or not current_tag or ha.role() != "active":
        return ""
    try:
        p = ha_agent.peer_request(peer, "GET", "/status", timeout=10)
    except Exception:  # noqa: BLE001
        return ""
    other = p.get("commit") if isinstance(p, dict) and p.get("role") == "standby" else ""
    if not isinstance(other, str) or not other:
        return ""
    if other != current_tag:
        return other
    db = p.get("db") if isinstance(p.get("db"), dict) else None
    if db is None or db.get("error"):
        return ""  # what it replicates is not known: not taken for an unfinished update
    rep = db.get("replica") or {}
    me = thisnode.read().get("ADDR", "")
    if rep.get("io") != "Yes" or rep.get("sql") != "Yes" or (me and rep.get("source") not in ("", None, me)):
        return other  # the same release, not following this node: the finish copies the database and switches
    return ""


def _finish_pair(s, state, current_tag, behind, asked, force, now, log):
    """The other node gets this node's release and the two switch back (update-portal-finish): when a super admin
    asks (Update portal), or by itself once, in the update window, with automatic updates on."""
    from accounts import ha_agent

    if ha_agent.read_op().get("state") == "running":  # integration-002: the finish itself, marked before it starts
        return "another operation is running"
    if not asked:
        if not s.auto_update:
            return f"the other node runs {behind}, this node {current_tag}: Update portal finishes the update"
        if state.get("finish_failed_tag") == current_tag:
            return f"finishing {current_tag} on the other node failed before: Update portal tries again"
        if not force and not in_window(s):
            return f"finishing {current_tag} on the other node waits for the update window ({window_text(s)})"
    from accounts.tasks import running_count

    if running_count():
        return "tasks are running: next round"
    log(f"auto-update: the other node runs {behind}: it gets {current_tag} and the two switch back")
    try:
        op = ha_agent.run_op("update-portal-finish", by="settings" if asked else "auto-update", ref=current_tag)
    except ha_agent.AgentError as exc:
        return str(exc)
    if op.get("state") != "done":  # its failure e-mail is sent by the operation itself (after_failure)
        record(finish_failed_tag=current_tag, at=now.isoformat())
        return f"{current_tag}: finishing on the other node failed"
    record(finish_failed_tag="", at=now.isoformat())
    return f"{current_tag}: installed on the other node too; the two switched back"


def record(notify=None, forward=True, **state):
    """The update's state (and its e-mail) where the database is writable (commit-013): here while this node is
    active; after a switch over this node is a standby with a read-only database, so the other node - active now -
    writes it (its agent's POST /update-state). forward=False (that endpoint): only here, never passed on - two nodes
    neither of which is active must not hand it to each other (commit-014). A courtesy: a node that cannot be
    reached loses only this line."""
    from accounts import ha

    if ha.role() == "active":
        _save(**state)
        if notify:
            _notify(*notify)
        return "here"
    if not forward:
        return "not recorded: this node is not active"
    from accounts import ha_agent, thisnode

    peer = thisnode.read().get("PEER_ADDR", "")
    try:
        ha_agent.peer_request(
            peer, "POST", "/update-state", {"state": state, "notify": list(notify) if notify else []}, timeout=30
        )
        return "on the other node"
    except Exception:  # noqa: BLE001
        return "not recorded"


def _notify(subject, text):
    from accounts.mailtemplates import T
    from accounts.models import Kind, User
    from accounts.utils import notify

    admins = list(User.objects.filter(kind=Kind.STAFF, is_super_admin=True, is_active=True))
    if admins:
        notify(admins, T.AUTO_UPDATE_FAILED, {"text": text}, subject)


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
    # 2026-11-14 (decision 0010, no "claugine-portal-update by hand"): Settings → Portal → "Install now" asks for one
    # round now, whatever the window and even with automatic updates off; asked once, cleared at once
    asked = bool(state.get("install_requested"))
    if asked:
        _save(install_requested=False)
        force = True
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
    _save(
        checked_at=now.isoformat(),
        source="control-plane",
        lic_state=lic_state,
        grace_until=data.get("grace_until") or "",
        notices=list(data.get("notices") or []),
        usage_flags=list(data.get("usage_flags") or []),
        latest=data.get("target_version") or "",
        # claugine-web 2026-11-16 (job critical-releases): the target is a release an admin marked critical
        critical=bool(data.get("critical")) and bool(data.get("target_version")),
        **said,
    )
    if lic_state == "retired":
        # claugine-web, 2026-11-09: this installation id was released from its licence. Terminal for this node
        # - the key is already free for a new install_id, and the check-in has no side effects, so we simply
        # stop acting on it. The portal itself keeps running, as with every other state.
        return "licence retired: this installation was released; it receives no more updates"
    # owner 2026-11-11: "expired is a state" - the licence's "to" date has passed (and its grace, if any). The
    # portal keeps running in every state; only updates stop. A state this portal does not know stops them too:
    # updating on a licence it cannot read is the wrong way to fail.
    # claugine-web 2026-11-16 (owner 08:28: "we can allow customer to update becuase critical security issue
    # discovered"): a target the control plane marks critical is installed whatever the state - as any update
    # (the setting, the window, one node at a time). The control plane offers one only to a running portal.
    critical = bool(data.get("critical")) and bool(data.get("target_version"))
    if lic_state not in ("active", "grace") and not critical:
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
        _save(
            upgrade_requested=False,
            upgrade_available=not ok,
            upgrade_result=(
                f"{ref}: the Enterprise edition from now on" if ok else f"{ref}: failed: {op.get('error', '')}"
            )[:500],
            upgrade_at=now.isoformat(),
            at=now.isoformat(),
        )
        if not ok:
            _notify(
                "The upgrade to Enterprise failed", f"{ref}: {op.get('error', '')}\nThe node runs what it ran before."
            )
        return f"upgrade to Enterprise: {'done' if ok else 'failed'}"
    tag = data.get("target_version") or ""
    behind = _unfinished_pair(current_tag)
    if not behind and current_tag and state.get("finish_failed_tag") == current_tag:
        # commit-023: a finish that failed after the install (the catch-up timed out and it rolled back) leaves both
        # nodes on this release and replicating - nothing to see but the failure it recorded; Update portal runs the
        # finish again, and only a finish that completes clears the record
        behind = current_tag
    if behind and (not tag or _vkey(tag) <= _vkey(current_tag)):  # integration-002: a newer release finishes it too
        return _finish_pair(s, state, current_tag, behind, asked, force, now, log)
    if not tag or tag == current_tag:
        return f"up to date ({current_tag or 'unknown'})"
    if _vkey(tag) <= _vkey(current_tag):  # never step back: HA migrations are forward-only
        return f"target {tag} not newer than {current_tag}: skipped (no downgrade)"
    if tag == state.get("failed_tag"):
        return f"{tag} failed before: waiting for a newer release"

    if not s.auto_update and not asked:  # checked in, state cached; installing is what the setting turns off
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
        try:
            op = ha_agent.run_op("update-single", by="auto-update", ref=tag)
        except ha_agent.AgentError as exc:  # integration-001: another operation took the lock first
            return str(exc)
        if op.get("state") != "done":
            _save(failed_tag=tag, result=f"{tag}: failed: {op.get('error', '')}"[:500], at=now.isoformat())
            _notify(f"Auto-update to {tag} failed", f"This node could not install {tag}: {op.get('error', '')}")
            return f"{tag}: failed"
        _save(result=f"{tag}: installed", at=now.isoformat(), failed_tag="", upgrade_result="")
        return f"{tag}: installed"
    log(f"auto-update: {tag} onto both nodes, one at a time")
    try:
        op = ha_agent.run_op("update-portal", by="auto-update", ref=tag)
    except ha_agent.AgentError as exc:  # integration-001: another operation took the lock first
        return str(exc)
    # commit-013: after the switch over this node is a standby (read only): record() writes where it can
    if op.get("state") != "done":
        record(
            notify=(
                f"Auto-update to {tag} stopped",
                f"{op.get('error', '')}\nSteps: "
                + "; ".join(f"{x['name']}: {x['status']}" for x in op.get("steps", [])),
            ),
            failed_tag=tag,
            result=f"{tag}: failed: {op.get('error', '')}"[:500],
            at=now.isoformat(),
        )
        return f"{tag}: failed"
    # the first half is done: the other node serves {tag}; it now updates this one and switches back (its own run)
    record(
        result=f"{tag}: installed; the other node switches back", at=now.isoformat(), failed_tag="", upgrade_result=""
    )
    return f"{tag}: the other node serves it and finishes the update"
