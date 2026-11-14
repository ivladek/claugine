"""Editions (owner 2026-11-08): **Standard** (free, restricted) and **Enterprise** (everything).

The edition is decided by the licence control plane (cp.example.com) and arrives in the check-in answer; it is
cached by ``accounts.autoupdate`` in ``PortalSettings.auto_update_state`` and is **never** decided locally.

The rule that protects a live installation
------------------------------------------
An answer with **no** ``edition`` field means **no edition enforcement at all**: the portal behaves exactly as it did
before editions existed. Only an explicit ``edition: "standard"`` applies the Standard restrictions.

That matters because a running installation checks in against a control plane that may not know about editions yet.
If "not said" meant "Standard", the first release carrying this module would take away that installation's second
node and its S3 backups. It must not. For the same reason an outage never changes anything: nothing is written, so
the last cached answer stands.

What Standard restricts (the job's "restricted features" list, nothing else): one portal node; backups fixed to a
local folder, daily, keep 3 days; updates fixed to the stable channel, weekly; no S3 backups; stable channel only.
"""

STANDARD = "standard"
ENTERPRISE = "enterprise"

#: Every gated feature. Enterprise has all of them; Standard has none of them.
FEATURES = (
    "second_node",
    "s3_backups",
    "editable_backup_settings",
    "editable_update_settings",
    "preview_stream",
    "analysis",  # owner 2026-11-13: Analysis → Summary and Usage (ee/analysis.py)
)

#: What Standard fixes and an operator cannot change. Applied on start and after a restore.
STANDARD_FIXED = {
    "backup_local": True,
    "backup_path": "~/claugine/backups",
    "backup_start": "03:06",  # once a day
    "backup_keep_days": 3,
    "backup_s3": False,
    "update_stream": "stable",
    "update_weekday": 6,  # Monday=0 … Sunday=6
    "update_time": "07:02",  # once a week
}


def normalise(value):
    """ "standard" / "enterprise", or "" for anything else (including None and an unknown name)."""
    name = (value or "").strip().lower()
    return name if name in (STANDARD, ENTERPRISE) else ""


def features_from(state):
    """{feature: bool} from a cached check-in answer (a plain dict). Pure, so it is unit-testable on its own.

    The edition sets the baseline — Standard: nothing, Enterprise or not said: everything — and an explicit
    ``features`` map from the control plane overrides it key by key, so what an edition includes can change there
    without a portal release. ``nodes`` (1 or 2) is accepted as a synonym for ``second_node``.
    """
    state = state if isinstance(state, dict) else {}
    standard = normalise(state.get("edition")) == STANDARD
    out = {f: not standard for f in FEATURES}
    sent = state.get("features")
    if isinstance(sent, dict):
        for f in FEATURES:
            if f in sent:
                out[f] = bool(sent[f])
        if "nodes" in sent and "second_node" not in sent:
            try:
                out["second_node"] = int(sent["nodes"]) >= 2
            except (TypeError, ValueError):
                pass
    return out


def _state(s=None):
    if s is None:
        from accounts.appliance import _settings

        s = _settings()
    return getattr(s, "auto_update_state", None) or {}


def current(s=None):
    """The edition this installation runs, or "" when the control plane has not said."""
    return normalise(_state(s).get("edition"))


def is_standard(s=None):
    """True only when the control plane explicitly said Standard."""
    return current(s) == STANDARD


#: What only the Enterprise build carries the code for (editions split, step 5, 2026-11-11). second_node is not
#: here: a Standard build with an Enterprise licence must still see it, to offer the upgrade to the Enterprise
#: releases, which is what brings that code.
BUILD_FEATURES = ("s3_backups", "editable_backup_settings", "editable_update_settings", "preview_stream", "analysis")


def standard_build():
    """True in a build without ee/ — the public Standard snapshot. It keeps Standard's limits whatever the control
    plane answers: the code that would lift them is not in it."""
    return ee("ha") is None


def features(s=None):
    out = features_from(_state(s))
    if standard_build():
        out.update({f: False for f in BUILD_FEATURES})
    return out


def allows(name, s=None):
    """Whether a gated feature is permitted here. An unknown feature name is permitted."""
    return features(s).get(name, True)


def fixed(s=None):
    """The values Standard pins, or {} when the edition does not pin anything. A Standard build pins them always
    (step 5): an installation that has not been told its edition, or holds an Enterprise licence before its upgrade,
    runs the Standard code and keeps its limits until the Enterprise release is installed."""
    return dict(STANDARD_FIXED) if (is_standard(s) or standard_build()) else {}


def locked(s=None):
    """The setting names an operator cannot change here (empty unless the edition pins some)."""
    return set(fixed(s))


def home_of(s):
    """The home directory a pinned ``~`` path belongs to: the **backup owner's**, not the running user's.

    Backups run as root, so a plain ``os.path.expanduser("~")`` would give ``/root`` — not the service account whose
    home the backups are meant to live in, and not the user the files are chowned to (owner 2026-11-08).
    """
    import os
    import pwd

    who = (getattr(s, "backup_owner", "") or "").split(":")[0].strip()
    if who:
        try:
            return pwd.getpwuid(int(who)).pw_dir if who.isdigit() else pwd.getpwnam(who).pw_dir
        except (KeyError, ValueError, OSError):
            pass
    return os.path.expanduser("~")


def effective(s, name):
    """The value in force for a setting: Standard pins some, so there the stored value is ignored.

    Read settings through this wherever they are **used**, not only where they are edited. A Standard installation
    must still obey its limits when the stored value says otherwise — after a restore from an Enterprise backup, a
    direct database edit, or an upgrade that left Enterprise values behind. A pinned ``backup_path`` beginning with
    ``~`` is expanded against the backup owner's home (see ``home_of``), since it is used as a filesystem path.
    """
    fx = fixed(s)
    value = fx[name] if name in fx else getattr(s, name, None)
    if name == "backup_path" and isinstance(value, str) and value.startswith("~"):
        import os

        value = os.path.join(home_of(s), value[1:].lstrip("/"))
    return value


# --- where the Enterprise code is, when this build has any -----------------------------------------------------


def ee(name):
    """The Enterprise implementation of `name` (``ee.<name>``), or None when this build has none.

    **Two different absences, and they must not be confused** (owner 2026-11-09, Phase 4):

    * The public build has **no `ee/` package at all**. That is a normal, expected state - the Standard edition is a
      complete one-node portal, not an Enterprise build with pieces removed - so this returns None and the caller
      falls back to its public counterpart (`accounts/single.py`).
    * An `ee/` package that **is** present but whose module fails to import is a **broken Enterprise build**, and
      the error is raised, not swallowed. Silently falling back would turn a typo in `ee/ha.py` into an Enterprise
      installation quietly losing its second node - the worst possible failure, because nothing would say so.

    The check is `find_spec("ee")`, not a try/except around the import of `ee.<name>`, precisely so the two cases
    stay apart.
    """
    import importlib
    import importlib.util

    try:
        spec = importlib.util.find_spec("ee")
    except (ImportError, ValueError):
        return None
    # A directory named ee/ without __init__.py is not an Enterprise package: it is what an install over an
    # Enterprise build leaves behind (ee/__pycache__/ survived install.sh's rsync - found 2026-11-11 on the test
    # pair, where it made every status page answer 500). Python sees it as a namespace package; treat it as absent.
    if spec is None or spec.origin in (None, "namespace") or not str(spec.origin).endswith("__init__.py"):
        return None
    return importlib.import_module(f"ee.{name}")
