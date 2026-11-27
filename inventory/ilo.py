"""The iLO steps of a host deployment (owner 2026-11-18, T25 §B1; HPE iLO 6/7 only, 08:50 "Now only ILO 6/7"), on top
of inventory/redfish.py's client. Every step reads back what it wrote; waits poll with a limit, never a fixed sleep.

    power         state; On / ForceOff and wait for it (the owner's rule on a powered-on host is the run's, B0.7)
    media         the two virtual drives: slot 1 takes Floppy/USBStick, slot 2 CD/DVD on iLO 6 (read 2026-11-18);
                  insert by URL (write protected), eject; the boot override "CD, once"
    BIOS          the real Bios.ResetBios (the run's reset to factory), then one pending change over that factory
                  state: the profile's settings, every NIC's network boot off and the site's time zone - nothing else
                  (owner 2026-11-19 17:55: the workload profile and the hardware keep the rest)
    storage       the controllers other than the boot controller: every volume deleted, one made over all the drives,
                  RAID5 for 3 or more, RAID1 for 2; one drive is never used (owner 12:40-12:49)
    the iLO       hostname m<host>, the site's domain, DNS, NTP, time zone, remote syslog, the profile's services (SSDP
                  always off, SNMP left as it is); **its IPv4 address, mask and gateway are never written** (owner 13:00
                  "dont clear ILO IP/mask/gateway to not lost connectivity"): no body sent here may carry them, and they
                  are read before and after every iLO write and must be the same
"""

import re
import time

from .redfish import RedfishError, WriteRefused, _messages

SYS = "/redfish/v1/Systems/1"
MGR = "/redfish/v1/Managers/1"
ETH = f"{MGR}/EthernetInterfaces/1"
NIC_BOOT = re.compile(r"(NicBoot\d*|Slot\d+NicBoot\d+|FlexLom\d*NicBoot\d*|NicBoot)$")
# what an iLO write must never carry (B1.4)
FORBIDDEN = (
    "IPv4Addresses",
    "IPv4StaticAddresses",
    "StaticAddresses",
    "SubnetMask",
    "Gateway",
    "DHCPv4",
    "DHCPEnabled",
)


class IloError(RedfishError):
    pass


def _wait(fn, want, timeout, poll=5, sleep=time.sleep, what=""):
    """Poll fn() until it returns want (or a value want(...) accepts); IloError after timeout seconds."""
    waited = 0
    while True:
        got = fn()
        if want(got) if callable(want) else got == want:
            return got
        if waited >= timeout:
            raise IloError(f"{what or 'waiting'}: still {got!r} after {timeout} s")
        sleep(poll)
        waited += poll


# --- power ------------------------------------------------------------------------------------------------------


def power_state(c):
    return str(c.get(SYS).get("PowerState") or "")


def power(c, on, timeout=600, sleep=time.sleep):
    """On or ForceOff, then wait for the state. Nothing when it is there already."""
    want = "On" if on else "Off"
    if power_state(c) == want:
        return want
    c.post(f"{SYS}/Actions/ComputerSystem.Reset", {"ResetType": "On" if on else "ForceOff"})
    return _wait(lambda: power_state(c), want, timeout, sleep=sleep, what=f"power {want}")


def boot_progress(c):
    """The host's boot progress (BootProgress.LastState: OSBootStarted, OSRunning, ...), "" when not reported."""
    return str((c.get(SYS).get("BootProgress") or {}).get("LastState") or "")


# --- virtual media ----------------------------------------------------------------------------------------------


def media_slots(c):
    """[{id, path, types, image, inserted}] of the iLO's virtual drives."""
    out = []
    for m in c.get(f"{MGR}/VirtualMedia").get("Members") or []:
        v = c.get(m["@odata.id"])
        out.append(
            {
                "id": str(v.get("Id") or ""),
                "path": v.get("@odata.id") or m["@odata.id"],
                "types": [str(t) for t in v.get("MediaTypes") or []],
                "image": str(v.get("Image") or ""),
                "inserted": bool(v.get("Inserted")),
            }
        )
    return out


def slot_for(c, kind):
    """The drive for "cd" (CD/DVD) or "usb" (Floppy/USBStick: the answers ISO, as iLO 6 offers it)."""
    want = {"cd": ("CD", "DVD"), "usb": ("USBStick", "Floppy")}[kind]
    for s in media_slots(c):
        if any(t in s["types"] for t in want):
            return s
    raise IloError(f"no virtual drive for {kind}")


def eject_all(c):
    for s in media_slots(c):
        if s["inserted"] or s["image"]:
            c.post(f"{s['path']}/Actions/VirtualMedia.EjectMedia", {})
    left = [s["id"] for s in media_slots(c) if s["inserted"]]
    if left:
        raise IloError(f"virtual drive(s) {', '.join(left)} still hold an image")


def insert(c, kind, url):
    s = slot_for(c, kind)
    if s["inserted"]:
        c.post(f"{s['path']}/Actions/VirtualMedia.EjectMedia", {})
    c.post(f"{s['path']}/Actions/VirtualMedia.InsertMedia", {"Image": url, "WriteProtected": True, "Inserted": True})
    got = slot_for(c, kind)
    if got["image"] != url or not got["inserted"]:
        raise IloError(f"virtual drive {got['id']}: the image is {got['image'] or 'none'}, not {url}")
    return got["id"]


def boot_cd_once(c):
    c.patch(SYS, {"Boot": {"BootSourceOverrideTarget": "Cd", "BootSourceOverrideEnabled": "Once"}})
    b = c.get(SYS).get("Boot") or {}
    if b.get("BootSourceOverrideTarget") != "Cd" or b.get("BootSourceOverrideEnabled") != "Once":
        raise IloError("the boot override did not take")


# --- BIOS -------------------------------------------------------------------------------------------------------


def bios_current(c):
    return dict(c.get(f"{SYS}/Bios").get("Attributes") or {})


def tz_attr(offset):
    """ "+05:00" → "UtcP5", "-03:30" → "UtcM330", "+00:00" → "Utc0" (the BIOS TimeZone values)."""
    m = re.match(r"^([+-])(\d{2}):(\d{2})$", offset or "")
    if not m:
        return ""
    h, mins = int(m.group(2)), m.group(3)
    if not h and mins == "00":
        return "Utc0"
    return f"Utc{'P' if m.group(1) == '+' else 'M'}{h}{'' if mins == '00' else mins}"


def bios_target(current, profile_bios, tz=""):
    """What the run sets and checks, over the BIOS's factory state (the reset before): every NIC's network boot off,
    the profile's settings and the time zone; only attributes the server has. Everything else - the settings the
    workload profile governs, the values the hardware reports (TPM, uncore frequency) - is the BIOS's own (owner
    2026-11-19 17:55, a test host against a reference host)."""
    t = dict(profile_bios or {})
    # owner 2026-11-19 17:57 "pxe boot disable alway for any NIC": over whatever a profile says, like the time zone
    t.update({k: "Disabled" for k in current if NIC_BOOT.search(k)})
    if tz:
        t["TimeZone"] = tz
    return {k: v for k, v in t.items() if k in current}


def bios_staged(c):
    """The BIOS settings already pending (Bios/Settings), {} when none. commit-130: a failed read stops the step - it
    is never taken as "nothing pending"."""
    try:
        return dict(c.get(f"{SYS}/Bios/Settings").get("Attributes") or {})
    except RedfishError as e:
        raise IloError(f"the pending BIOS settings could not be read: {e}") from e


def bios_pending(c, target, current=None, whole=False):
    """One pending change that makes the next POST give exactly the target (commit-130): every attribute whose running
    value or already-pending value differs from it - or, with whole, every attribute of the target (after a factory
    reset the iLO may still show the values from before it: 2026-11-20, a redeployed test host); then the pending
    settings are read back and must hold the whole target."""
    current = bios_current(c) if current is None else current
    staged = bios_staged(c)
    change = (
        dict(target)
        if whole
        else {k: v for k, v in target.items() if current.get(k) != v or (k in staged and staged[k] != v)}
    )
    if change:
        c.patch(f"{SYS}/Bios/Settings", {"Attributes": change})
    after = bios_staged(c)
    # commit-219, core-064: with whole (after a factory reset) the running values may be stale, so every target
    # attribute must be in the pending settings with its value - no fallback to the running one
    wrong = sorted(k for k, v in target.items() if (after.get(k) if whole else after.get(k, current.get(k))) != v)
    if wrong:
        raise IloError("the pending BIOS settings do not hold the target: " + ", ".join(wrong[:20]))
    return change


def bios_differences(c, target):
    cur = bios_current(c)
    return {k: (cur.get(k), v) for k, v in target.items() if cur.get(k) != v}


def bios_reset(c):
    """The real BIOS factory reset (the run's "Reset to factory"); applied at the next POST."""
    return _messages(c.post(f"{SYS}/Bios/Actions/Bios.ResetBios", {}))


# --- storage ----------------------------------------------------------------------------------------------------


def controllers(c):
    """[{id, path, name, boot, drives: [paths], volumes: [{path, raid, size}]}] of the host's storage."""
    out = []
    for m in c.get(f"{SYS}/Storage").get("Members") or []:
        st = c.get(m["@odata.id"])
        names = " ".join(
            [str(st.get("Name") or "")]
            + [str(x.get("Name") or x.get("Model") or "") for x in st.get("StorageControllers") or []]
        )
        vols = []
        link = (st.get("Volumes") or {}).get("@odata.id")
        for v in (c.get(link).get("Members") or []) if link else []:
            vv = c.get(v["@odata.id"])
            vols.append(
                {
                    "path": vv.get("@odata.id") or v["@odata.id"],
                    "raid": str(vv.get("RAIDType") or ""),
                    "size": int(vv.get("CapacityBytes") or 0),
                    # commit-129: the drives the volume is made of
                    "drives": sorted(d.get("@odata.id") for d in (vv.get("Links") or {}).get("Drives") or []),
                    # commit-138: how the OS will know the volume (NAA → /dev/disk/by-id/wwn-0x...)
                    "ids": [
                        str(i.get("DurableName") or "").lower()
                        for i in vv.get("Identifiers") or []
                        if i.get("DurableName")
                    ],
                }
            )
        out.append(
            {
                "id": str(st.get("Id") or ""),
                "path": st.get("@odata.id") or m["@odata.id"],
                "name": names.strip(),
                "boot": "ns204" in names.lower() or "boot" in names.lower(),
                "drives": [d["@odata.id"] for d in st.get("Drives") or []],
                "volumes": vols,
                "vol_link": link or f"{st.get('@odata.id') or m['@odata.id']}/Volumes",
            }
        )
    return out


def raid_for(n):
    """RAID5 for 3 drives or more, RAID1 for 2, nothing for one (owner 12:46-12:49 "we dont use any single drive ever")."""
    return "RAID5" if n >= 3 else ("RAID1" if n == 2 else "")


def boot_controller(c, ctls, boot_gb):
    """The controller holding the profile's boot volume (commit-128): exactly one controller iLO names as a boot
    controller, and it must hold a volume within 6% of the boot device's size. Anything else - no controller named,
    several, or the named one without that volume - is an IloError: nothing is deleted then. The size alone never
    tells the boot controller."""
    if not boot_gb:
        raise IloError("the profile names no boot device size: the boot controller cannot be told")
    want = int(boot_gb) * 10**9
    near = [x for x in ctls if any(abs(v["size"] - want) <= want * 0.06 for v in x["volumes"])]
    named = [x for x in ctls if x["boot"]]
    if not named:
        seen = "; ".join(f"{x['id']} '{x['name']}'" for x in ctls) or "none"
        raise IloError(
            f"iLO names no boot controller (it lists: {seen}): the boot volume is not known for certain, nothing is "
            "deleted"
        )
    if len(named) != 1 or named[0] not in near:
        raise IloError(
            f"iLO names {', '.join(x['id'] for x in named)} as boot, but the boot volume ({boot_gb} GB) is on "
            f"{', '.join(x['id'] for x in near) or 'no controller'}: nothing is deleted"
        )
    return named[0]


def _storage_ready(c):
    """The controllers, or None while iLO has not read the storage: iLO 6/7 list none while the server is off and answer
    ResourceNotReadyRetry during POST (seen on a test host, 2026-11-19)."""
    try:
        if not c.get(f"{SYS}/Storage").get("Members"):
            return None
        ctls = controllers(c)
        # owner 2026-11-20 13:50: a controller listed without its name yet is a listing still being built (seen while
        # POST applied pending BIOS settings) - not ready
        if not ctls or any(not x["name"] for x in ctls):
            return None
        return ctls
    except RedfishError as exc:
        if "NotReady" in str(exc):
            return None
        raise


def discover_storage(c, log=print, timeout=900, sleep=time.sleep):
    """The storage as iLO reads it during POST: when it lists none (the server off, or not discovered yet), the server
    starts once into the BIOS setup - no OS boots - and the controllers are waited for. Returns them; IloError when they
    do not come. The BIOS settings pending are applied by this start-up as by any."""
    started = power_state(c) == "Off"
    if started:
        log("storage: iLO reads it during POST only - starting into the BIOS setup to read it")
        c.patch(SYS, {"Boot": {"BootSourceOverrideEnabled": "Once", "BootSourceOverrideTarget": "BiosSetup"}})
        power(c, on=True, sleep=sleep)
    # owner 2026-11-20 13:50, commit-220 / core-065: the listing is taken only once POST is over - POST may reset again
    # on the way (pending BIOS settings) and a listing before that can be half built or a named subset
    got = _wait(lambda: boot_progress(c), lambda x: x in POST_OVER, timeout, sleep=sleep, what="the end of POST")
    if started and got != "SetupEntered":
        # commit-221 / core-066: started into the BIOS setup, it must be there - an OS starting instead may be using
        # the volumes the RAID work would delete
        raise IloError(f"storage: the server started its OS ({got}) instead of the BIOS setup - nothing is deleted")
    return _wait(lambda: _storage_ready(c), lambda x: bool(x), timeout, sleep=sleep, what="storage read by iLO")


POST_OVER = ("SetupEntered", "OSBootStarted", "OSRunning")


def _volumes_of(c, ctl):
    return [v["path"] for x in controllers(c) if x["id"] == ctl["id"] for v in x["volumes"]]


def local_storage(c, boot_gb, log=print, timeout=600, sleep=time.sleep, ctls=None):
    """Every controller but the boot one: its volumes deleted, one volume over all its drives (commit-129: each delete
    and the new volume waited for, polled; a controller that keeps the change for the next POST is "pending", and B4's
    readback must then find it). Returns [(controller, raid, drives, "done" | "pending" | "")]. ctls: the listing
    discover_storage checked (commit-220, core-065: no second, unchecked read)."""
    ctls = controllers(c) if ctls is None else ctls
    boot = boot_controller(c, ctls, boot_gb)
    done = []
    for ctl in ctls:
        if ctl["id"] == boot["id"] or not ctl["drives"]:
            continue
        raid = raid_for(len(ctl["drives"]))
        for v in ctl["volumes"]:
            c.delete(v["path"])
        state = ""
        # commit-129: nothing is created until the old volumes are gone (IloError stops the run, naming the controller)
        _wait(lambda ctl=ctl: _volumes_of(c, ctl), [], timeout, sleep=sleep, what=f"{ctl['id']}: volumes deleted")
        if not raid:
            log(f"{ctl['name'] or ctl['id']}: one drive, not used")
            done.append((ctl["id"], "", 1, state))
            continue
        c.post(ctl["vol_link"], {"RAIDType": raid, "Links": {"Drives": [{"@odata.id": d} for d in ctl["drives"]]}})

        def made(ctl=ctl, raid=raid):  # commit-129: the new volume over exactly the controller's drives
            return [
                v
                for x in controllers(c)
                if x["id"] == ctl["id"]
                for v in x["volumes"]
                if v["raid"] == raid and v.get("drives") == sorted(ctl["drives"])
            ]

        try:
            _wait(lambda: bool(made()), True, timeout, sleep=sleep, what=f"{ctl['id']}: {raid} made")
            state = state or "done"
        except IloError:
            state = "pending"
            log(f"{ctl['id']}: the {raid} volume is not listed yet - checked again after the next POST")
        done.append((ctl["id"], raid, len(ctl["drives"]), state))
    return done


# --- the iLO itself ---------------------------------------------------------------------------------------------


def ilo_address(c):
    """The iLO's own IPv4 address, mask and gateway (B1.4: never written; compared before and after)."""
    a = (c.get(ETH).get("IPv4Addresses") or [{}])[0]
    return (a.get("Address"), a.get("SubnetMask"), a.get("Gateway"))


def _no_address(body, where=""):
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in FORBIDDEN:
                    raise IloError(f"refused: an iLO write {where} carries {k}")
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(body)
    return body


class _Guarded:
    """The client with every write followed by a read of the iLO's address, mask and gateway (commit-132): the first
    difference stops the sequence."""

    def __init__(self, c, before):
        self.c, self.before = c, before
        # commit-244: every path written; commit-245/247: one the iLO refused (4xx) changed nothing and is left out -
        # a 5xx or no answer at all may come after it was applied, so those are kept
        self.writes = []

    def __getattr__(self, name):
        return getattr(self.c, name)

    def post(self, path, body):  # commit-132: every write checked as a PATCH is
        return self._checked(self.c.post, path, body)

    def delete(self, path):
        return self._checked(lambda p, _b: self.c.delete(p), path, None)

    def patch(self, path, body):
        return self._checked(self.c.patch, path, body)

    def _checked(self, write, path, body):
        refused = rejected = False
        try:
            return write(path, body)
        except WriteRefused:  # commit-186: nothing was sent, so nothing to read back - the refusal itself goes up
            refused = True
            raise
        except RedfishError as exc:
            # commit-245: the iLO refused the request (4xx): not applied; commit-247: a 5xx may come after it was
            status = getattr(exc, "status", None)
            rejected = status is not None and 400 <= status < 500
            raise
        finally:
            if not refused and not rejected:
                self.writes.append(path)
            after = self.before if refused else ilo_address(self.c)
            if after != self.before:
                raise IloError(f"the iLO's address changed: {self.before} → {after} (after {path})")


LLDP_PORTS = f"{MGR}/DedicatedNetworkPorts"


def lldp_ports(c):
    """{path: LLDPEnabled} of the iLO's dedicated network ports (iLO 6: DedicatedNetworkPorts/N Ethernet.LLDPEnabled);
    None when the iLO has no such list (commit-232: an unreadable state is told apart from "off")."""
    from .redfish import RedfishError

    try:
        members = c.get(LLDP_PORTS).get("Members") or []
    except RedfishError:
        return None
    return {m["@odata.id"]: bool((c.get(m["@odata.id"]).get("Ethernet") or {}).get("LLDPEnabled")) for m in members}


def ensure_lldp(c):
    """commit-232: LLDP on every dedicated iLO port, set where off and read back; "" when on, else what is wrong."""
    for path, on in (lldp_ports(c) or {}).items():
        if not on:
            c.patch(path, {"Ethernet": {"LLDPEnabled": True}})
    after = lldp_ports(c)
    if after is None:
        return "LLDP not readable"
    return "" if after and all(after.values()) else "LLDP off"


def ilo_name(host_name):
    """The iLO's name: "m" and the host's name without its leading "h" - hdc1ci1kvm001 → mdc1ci1kvm001, as
    the existing hosts have it (owner 2026-11-20, the hosts compared)."""
    return "m" + (host_name[1:] if host_name.startswith("h") else host_name)


def ilo_settings(c, host_name, domain, services, ntp=None, dns=None, tz_index=None, syslog=None, log=print):
    """The iLO's settings written in full (B1.4b), never its address. Returns True when the iLO must be restarted: a
    network setting, a service or a site setting changed, or the iLO said a reset is required (commit-131)."""
    from .redfish import Client

    before = ilo_address(c)
    g = _Guarded(c, before)
    eth = c.get(ETH)
    changed = False
    name = ilo_name(host_name)
    hpe = (eth.get("Oem") or {}).get("Hpe") or {}
    if eth.get("HostName") != name or hpe.get("DomainName", "") != (domain or ""):
        g.patch(
            ETH,
            _no_address({"HostName": name, "Oem": {"Hpe": {"HostName": name, "DomainName": domain or ""}}}, "(name)"),
        )
        changed = True
    proto = c.get(f"{MGR}/NetworkProtocol")
    want = {"SSH": bool(services.get("ssh")), "IPMI": bool(services.get("ipmi_lan")), "SSDP": False}
    body = {
        k: {"ProtocolEnabled": v} for k, v in want.items() if bool((proto.get(k) or {}).get("ProtocolEnabled")) != v
    }
    if body:
        g.patch(f"{MGR}/NetworkProtocol", _no_address(body, "(services)"))
        changed = True
    # commit-232 (owner 2026-11-20 08:36 "check lldp too - on ipmi and host / both must be on"): LLDP on every
    # dedicated iLO port
    for path, on in (lldp_ports(c) or {}).items():
        if not on:
            g.patch(path, {"Ethernet": {"LLDPEnabled": True}})
    site_before = _site_view(c)
    messages = Client.apply(g, ntp=ntp, dns=dns, timezone=tz_index)  # each write guarded too
    if syslog is not None:  # owner 2026-11-19 14:22 "syslog set error is minor problem": a warning, the run goes on
        written = len(g.writes)
        try:
            messages = list(messages or []) + list(Client.apply(g, syslog=syslog) or [])
        except WriteRefused:  # commit-186: this node may not write - the step fails, not a warning
            raise
        except RedfishError as exc:
            log(f"iLO remote syslog not set (the deployment goes on): {exc}")
        if len(g.writes) > written:  # commit-244: a syslog write is a change, whatever a later read sees
            changed = True
    if any("reset" in str(m).lower() for m in messages or []) or _site_changed(site_before, _site_view(c)):
        changed = True
    if ilo_address(c) != before:
        raise IloError(f"the iLO's address changed: {before} → {ilo_address(c)}")
    return changed


_SYSLOG_AT = 5  # the syslog's place in _site_view's tuple


def _site_changed(before, after):
    """Did apply() change what the site sets on the iLO? An unreadable view is a change (commit-131). commit-241: the
    syslog is compared only when both reads saw every subscription - an unread one (even the collector's own) is not
    a change apply() made, so it never restarts the iLO; the readback still reports it as drift."""
    if before is None or after is None:
        return True
    for i, (a, b) in enumerate(zip(before, after, strict=True)):
        if i == _SYSLOG_AT and (a is None or b is None):
            continue
        if a != b:
            return True
    return False


def _site_view(c):
    """What the site settings set on the iLO, to tell whether apply() changed any (commit-131)."""
    from .redfish import Client

    try:
        s = Client.settings(c)
    except Exception:  # noqa: BLE001 - not readable: taken as changed by the comparison
        return None
    return (
        tuple(s.get("ntp") or ()),
        tuple(s.get("dns") or ()),
        s.get("tz_offset"),
        s.get("tz_index"),  # commit-131: another zone of the same offset is a change too
        s.get("timezone"),
        # commit-241: None when not every subscription was read - see _site_changed
        None if (s.get("syslog") or {}).get("incomplete") else tuple(sorted((s.get("syslog") or {}).items())),
        bool(s.get("dhcp_ntp")),  # commit-131: the DHCP flags alone change what the iLO uses
        bool(s.get("dhcp_dns")),
    )


def redfish_drift(have, want):
    from .redfish import syslog_drift

    return syslog_drift(have, want)


def ilo_settings_differ(c, host_name, services, ntp=None, dns=None, syslog=None, tz_index=None):
    """What the iLO does not hold of the settings written (commit-131: read back, after a restart too); [] when all."""
    from .redfish import Client

    s = Client.settings(c)
    out = []
    if s.get("hostname") != ilo_name(host_name):
        out.append(f"host name {s.get('hostname')!r}")
    if ntp is not None and ([x for x in ntp if x] != s.get("ntp") or s.get("dhcp_ntp")):
        out.append(f"NTP {s.get('ntp')}" + (" (from DHCP)" if s.get("dhcp_ntp") else ""))
    if dns is not None and ([x for x in dns if x][:3] != s.get("dns") or s.get("dhcp_dns")):
        out.append(f"DNS {s.get('dns')}" + (" (from DHCP)" if s.get("dhcp_dns") else ""))
    if syslog is not None and redfish_drift(s.get("syslog"), syslog):
        out.append(f"syslog {s.get('syslog')}")
    idx = s.get("tz_index")
    if idx is None:  # an iLO that gives the zone by name only: its index from the zone list
        idx = next((z.get("index") for z in s.get("zones") or [] if z.get("name") == s.get("timezone")), None)
    if tz_index is not None and idx != tz_index:  # commit-131
        out.append(f"time zone {s.get('timezone') or s.get('tz_index')!r}")
    lldp = lldp_ports(c)
    if lldp is None:
        out.append("LLDP not readable")
    elif not lldp or not all(lldp.values()):
        out.append("LLDP off")
    proto = c.get(f"{MGR}/NetworkProtocol")
    for k, v in (("SSH", bool(services.get("ssh"))), ("IPMI", bool(services.get("ipmi_lan"))), ("SSDP", False)):
        if bool((proto.get(k) or {}).get("ProtocolEnabled")) != v:
            out.append(f"{k} {'off' if v else 'on'}")
    return out


def ilo_restart(c, timeout=300, sleep=time.sleep):
    c.post(f"{MGR}/Actions/Manager.Reset", {"ResetType": "GracefulRestart"})
    sleep(20)

    def up():
        try:
            return str((c.get(MGR).get("Status") or {}).get("State") or "")
        except RedfishError:
            return ""

    return _wait(up, "Enabled", timeout, sleep=sleep, what="iLO restart")


def snapshot(c):
    """What the run saves before it changes anything (B1.1): the iLO's settings, the BIOS and the storage."""
    out = {}
    for key, fn in (
        ("system", lambda: c.get(SYS)),
        ("manager", lambda: c.get(MGR)),
        ("ethernet", lambda: c.get(ETH)),
        ("network_protocol", lambda: c.get(f"{MGR}/NetworkProtocol")),
        ("datetime", lambda: c.get(f"{MGR}/DateTime")),
        ("bios", lambda: bios_current(c)),
        ("media", lambda: media_slots(c)),
        ("storage", lambda: controllers(c)),
    ):
        try:
            out[key] = fn()
        except RedfishError as exc:
            out[key] = {"error": str(exc)}
    return out
