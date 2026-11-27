"""T36 B3c (owner 2026-11-24 20:39 / 21:11; rules v2 C1, C8): snapd on a base-OS VM of a site service. Read reports it
(core_services.READ_SCRIPT, facts["snapd"]: installed, unmasked or absent) and a VM with it differs from its OS
(base-os: Ubuntu, snapd removed and masked) - non-compliant. It is removed in place, never by a redeploy: Remove snapd
(a Task of kind "core VM redeploy", one at a time with the VM's other operations) purges the snaps and the package,
pins it away and masks its units, as a host's OS step does (hostdeploy.OS_BASE). Then the VM is read again; it is
deployed again only when it is the VM the portal made, its platform was just read and nothing else differs."""

from .models import CoreService

PRESENT = ("installed", "unmasked", "unknown")  # commit-512: unknown - a unit's state could not be read
UNITS = ("snapd.service", "snapd.socket", "snapd.seeded.service", "snapd.apparmor.service")

# what Read prints (##snapd): the package installed, else any of its units still loaded (not masked, wherever its
# file is - commit-505), else - when every unit's LoadState was read as masked or not-found - absent; a query that
# failed or answered anything else: unknown (commit-512), never absent
PROBE = (
    "echo '##snapd'; if dpkg-query -W -f='${Status}' snapd 2>/dev/null | grep -q 'install ok installed'; "
    "then echo installed; else u=; k=; for x in "
    + " ".join(UNITS)
    + '; do s=$(systemctl show -p LoadState --value "$x" 2>/dev/null) || s=; case "$s" in loaded) u=1 ;; '
    "masked|not-found) ;; *) k=1 ;; esac; done; "
    'if [ -n "$u" ]; then echo unmasked; elif [ -n "$k" ]; then echo unknown; else echo absent; fi; fi'
)

REMOVE = (
    r"""set -e
export DEBIAN_FRONTEND=noninteractive
if command -v snap >/dev/null; then
  for s in $(snap list 2>/dev/null | awk 'NR>1 {print $1}' | tac); do snap remove --purge "$s" || true; done
fi
apt-get purge -y -q snapd >/dev/null
if dpkg-query -W -f='${Status}' snapd 2>/dev/null | grep -q 'install ok installed'; then echo "snapd is still installed" >&2; exit 1; fi
printf 'Package: snapd\nPin: release *\nPin-Priority: -1\n' > /etc/apt/preferences.d/no-snap.pref
for u in """
    + " ".join(UNITS)
    + r"""; do
  systemctl stop "$u" >/dev/null 2>&1 || true; systemctl mask "$u" >/dev/null
done
rm -rf /snap /var/snap /var/lib/snapd /var/cache/snapd /root/snap
"""
    + PROBE
    + "\n"
)  # commit-505: what is left, as Read reports it


def found(node):
    """snapd on a base-OS VM, or its state not known, by its last read (non-compliant either way)."""
    return bool(node.base_os) and (node.facts or {}).get("snapd") in PRESENT


def removable(node):
    """commit-517: Remove snapd only where the last read found it - installed or a unit unmasked, never unknown."""
    return bool(node.base_os) and (node.facts or {}).get("snapd") in ("installed", "unmasked")


def remove(task, node, user=None):
    """Remove snapd from the VM in place (as root over ssh), read the VM again and set its state (see the module)."""
    from accounts.models import audit

    from . import core_services as cs
    from .hostlogging import node_ssh

    if not node.base_os:
        raise ValueError(f"{node.name} is not a base-OS VM")
    out = node_ssh(node)(REMOVE, timeout=600)
    if "##snapd\nabsent" not in (out or ""):
        task.step("snapd", "failed", (out or "")[-300:])
        raise RuntimeError(f"{node.name}: snapd was not removed")
    task.step("snapd", "ok", "snaps and the package purged, pinned away, units masked")
    audit(user, "core_vm_snapd_removed", node.service, site=node.service.site.code, vm=node.name)
    site = node.service.site.code
    errors = dict(cs.refresh_vms(site))
    plat = node.service.platform.key if node.service.platform_id else None
    read_ok = cs.read_node(node, site)
    node.refresh_from_db()
    fresh = {plat} if plat and plat not in errors else set()
    why = cs.compliance(node, fresh)
    blocked = []  # commit-503/504: deployed again only on a successful read saying absent and its own platform read
    if not read_ok:
        blocked.append(f"the VM was not read: {node.facts_error}")
    elif (node.facts or {}).get("snapd") != "absent":
        blocked.append(f"the read says snapd is {(node.facts or {}).get('snapd') or 'not reported'}")
    if not fresh:
        blocked.append(f"{plat or 'its platform'} not read: {errors.get(plat, 'no platform')}")
    if node.state == CoreService.State.NON_COMPLIANT and not why and not blocked:
        node.set_state(CoreService.State.DEPLOYED)
        task.step("state", "ok", "deployed: nothing else differs from its service")
    else:
        node.service.sync_state()
        task.step(
            "state",
            "ok" if not blocked else "failed",
            f"{node.state}"
            + (f": {'; '.join(why[:4])}" if why else "")
            + (f"; {'; '.join(blocked)}" if blocked else ""),
        )
    return {"state": node.state, "why": why, "blocked": blocked}
