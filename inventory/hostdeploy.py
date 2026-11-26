"""Deploying a host from scratch (owner 2026-11-18, T25 - worklog/tasks/T25-host-deployment-procedure.md v2): from a
server in the records to a **disabled** host in its OpenNebula cluster. A super admin starts it on the host's page
(Deploy / Reinstall); it runs in the background on the active node, one step after another, each writing its status
and log lines on the run (HostDeployment); a failed run stops at its step and is resumed from it (every step can run
again). Nothing on the host is touched until every check of B0 passes.

    B0  checks       no platform off the baseline; the host's records (site, core address, iLO, profile with a disk
                     layout); in no OpenNebula cluster or one with no VMs; the site's NTP/DNS/time zone/IPMI log
                     collector, the host networks, the NFS datastores; the secrets; the installer ISO; power: on →
                     only with the owner's confirmation (09:09 "ask me once before host power actions"), off → go
    B1  the iLO      snapshot; BIOS/iLO versions = the profile's (a mismatch stops); power off; the iLO's settings
                     (never its address); [reset to factory]; the BIOS target as one pending change; local storage;
                     the two ISOs and "CD once"; the one power-on
    B3  install      ssh as SA answers and cloud-init is done (30 min); both drives ejected
    B4  the OS       the OS baseline as on the hosts in service (oneadmin 9869:9869, its home /var/lib/one 750, its
                     sessions umask 0027; no kdump, timesyncd, no snap); BIOS and RAID read back; bonds, bridges and
                     a ping at each MTU; the repositories; the packages; ovmf held, then the full upgrade; tuned; local
                     storage on /var/lib/one/datastores; the NFS datastores (750 oneadmin:oneadmin); FRR (iBGP EVPN to
                     the route reflectors); oneadmin's keys and its umask checked over ssh; ufw (incoming and routed
                     dropped but from the records' networks; VM traffic on the bridges passes, OpenNebula's security
                     groups filter it; outgoing open), AppArmor and lldpd on; the logging from Infrastructure →
                     Services (the KVM host profile's services, their rsyslog and logrotate files, sent to the site's
                     log collector); a reboot when needed, then the checks again
    B5  records      the host joins its cluster in the records (planned), local storage recorded; the answers ISO
                     deleted.

T36 B4b (owner 2026-11-25 19:25, QB4-1 "Always two actions", QB4-2 "Enable when checks pass"; rules v2 A4): Deploy
makes a KVM host with no oneadmin keys and no OpenNebula; **Connect** (a run of its own, HostDeployment kind
"connect", per host or for a cluster's hosts) brings it into OpenNebula:

    C0  ready        in its cluster, its platform's front end reachable, the secrets, the host answers ssh
    C1  keys         oneadmin's keys and its umask checked over ssh
    C2  OpenNebula   added to the cluster, disabled; monitored (RESERVED_CPU)
    C3  compare      compared with its profile
    C4  enable       enabled in OpenNebula, monitored; the host deployed in the records

A Connect that fails once the host is in OpenNebula leaves it disabled and non-compliant.
"""

import ipaddress
import re
import shlex
import time

from django.db import transaction
from django.utils import timezone

from accounts.models import audit

from . import ilo
from .models import IN_SERVICE, HostDeployment, Network, Storage, StorageVolume

INSTALL_LIMIT = 1800  # B3.2: 30 minutes
PHASES = [
    ("B0", "checks", "Checks"),
    ("B1", "snapshot", "iLO: snapshot"),
    ("B1", "versions", "iLO: BIOS and iLO versions"),
    ("B1", "power_off", "Power off, drives ejected"),
    ("B1", "ilo", "iLO settings"),
    ("B1", "bios_reset", "BIOS reset to factory"),
    ("B1", "bios", "BIOS settings (pending)"),
    ("B1", "storage", "Local storage (RAID)"),
    ("B1", "media", "Installer and answers ISOs"),
    ("B1", "power_on", "Power on"),
    ("B3", "install", "Ubuntu installed"),
    ("B3", "eject", "Drives ejected"),
    ("B4", "os_base", "OS baseline"),
    ("B4", "readback", "BIOS and RAID read back"),
    ("B4", "network", "Network checked"),
    ("B4", "repos", "Repositories"),
    ("B4", "packages", "Packages"),
    ("B4", "upgrade", "ovmf held, full upgrade"),
    ("B4", "tuned", "tuned, libvirt network"),
    ("B4", "local_fs", "Local storage mounted"),
    ("B4", "nfs", "NFS datastores"),
    ("B4", "frr", "FRR (EVPN)"),
    ("B4", "gpu", "GPU passthrough"),
    ("B4", "security", "Firewall, AppArmor, LLDP"),
    ("B4", "logging", "Services and logging"),
    ("B4", "reboot", "Reboot if needed, checks again"),
    ("B5", "joined", "Joined its cluster (records)"),
    ("B5", "cleanup", "Answers ISO deleted"),
]
STEP_KEYS = [k for _p, k, _l in PHASES]
# T36 B4b: Connect, a run of its own (kind "connect")
CONNECT_PHASES = [
    ("C0", "ready", "Ready to connect"),
    ("C1", "keys", "oneadmin's keys"),
    ("C2", "one_add", "Added to OpenNebula, disabled"),
    ("C2", "one_wait", "Monitored by OpenNebula"),
    ("C3", "compare", "Compared with its profile"),
    ("C4", "enable", "Enabled in OpenNebula"),
    ("C4", "records", "Records"),
]
CONNECT_KEYS = [k for _p, k, _l in CONNECT_PHASES]
OVMF = ("ovmf", "ovmf-amdsev", "ovmf-generic", "ovmf-inteltdx")  # owner 11:58: OpenNebula requires them held
# owner 2026-11-21 07:46 "add prometherus to host installation": OpenNebula's exporters (node :9100, libvirt :9926)
PACKAGES = (
    "opennebula-node-kvm",
    "opennebula-prometheus-kvm",
    "frr",
    "nfs-common",
    "tuned",
    "bridge-utils",
    "ipmitool",
    "lldpd",
    "ufw",
    "apparmor",
)
EXPORTERS = ("opennebula-node-exporter", "opennebula-libvirt-exporter")
HOST_NET_KINDS = (Network.Kind.CORE_KVM, Network.Kind.CORE_LM, Network.Kind.NFS)  # core-lm: owner 2026-11-19


class DeployError(Exception):
    pass


# --- what a run needs from the records ----------------------------------------------------------------------------


def _settings():
    from tenants.models import PortalSettings

    return PortalSettings.get()


def host_networks(h, platform):
    """The site networks a host carries: the core (core-kvm) network, the live-migration network (core-lm, owner
    2026-11-19), the platform family's EVPN network and the NFS network - as on the hosts today."""
    from .models import Network as N

    fam = "portal" if platform.kind == "portal" else "cloud"
    nets = [n for n in N.objects.filter(site=h.site.code, state__in=IN_SERVICE).order_by("vlan")]
    out = [n for n in nets if n.kind in HOST_NET_KINDS or (n.kind == N.Kind.EVPN and n.family == fam)]
    out += [n for n in nets if n.kind == N.Kind.CORE and (n.extra or {}).get("hosts")]  # a network marked for hosts
    return out


def _net_rows(nets):
    return [
        {
            "name": n.name,
            "interface": n.interface,
            "vlan": n.vlan,
            "mtu": n.mtu,
            "cidr": n.cidr,
            "gateway": n.gateway,
            "bridge": (n.extra or {}).get("bridge") or n.name,
            "dns": (n.extra or {}).get("dns") or [],
            "rrs": list(n.rrs or []),
            "kind": n.kind,
            "asn": n.local_asn,
        }
        for n in nets
    ]


def _account(ref):
    """(user, private key, public key) of a user secret; the public key worked out from the private one when the
    secret keeps none."""
    from .credentials import _public_key, account, find

    user, acc = account(ref) if ref else (None, {})
    sec = find(ref) if ref else None
    key = acc.get("key") or ""
    pub = (sec.public_key if sec else "") or (_public_key(key) if key.startswith("-----") else "")
    return user or "", key, pub


def logins(p):
    """(SA user, SA private key, SA public key, oneadmin public key, oneadmin private key) of the platform."""
    from .credentials import find
    from .sshexec import default_account

    user, key, pub = _account(p.sa or default_account(p.site.code))
    one = find(p.fe_secrets) if p.fe_secrets else None
    one_pub = (one.public_key if one else "") or ""
    one_key = one.get("os_private_key") if one is not None and one.has("os_private_key") else ""
    return user, key, pub, one_pub, one_key


def nfs_datastores(cluster):
    """[(datastore name, NFS source "host:/export", fstab options)] of the cluster's system and image datastores."""
    out = []
    for d in cluster.datastores.select_related("volume__storage").order_by("name"):
        v = d.volume
        if (
            d.kind not in ("vms", "images") or v is None or v.storage.kind != Storage.Kind.NFS
            if hasattr(Storage, "Kind")
            else False
        ):
            continue
        st = v.storage
        if not st.targets:
            continue
        fs = (st.fstab or "nfs4  defaults  0 0").split()
        out.append((d.name, f"{st.targets[0]}:{v.device}", fs[0], fs[1] if len(fs) > 1 else "defaults"))
    return out


# --- B0: the checks ------------------------------------------------------------------------------------------------


def problems(h, cluster, confirmed=False, power=None, fns=None):
    """What keeps the host from being deployed now (B0). Empty: go. ``fns``: the functions a claimed run keeps
    (commit-526: its checks follow them); none - the host service's as it is now (the dialog, before a run)."""
    from accounts import hostmedia

    from . import baseline, hostservice

    out = []
    if why := baseline.problem():
        out.append(why)
    p = cluster.platform
    prof = h.profile
    try:  # T36 B4a: the host service it is made from on this platform; its steps follow its functions
        svc = hostservice.pick(h, p)
        fns = svc.functions() if fns is None else list(fns)
        if p.kind in ("mgmt", "simple") and prof is not None and prof.gpu_count:
            out.append(f"no GPU hosts on a {p.kind} platform (rules v2 B2)")
    except hostservice.HostServiceError as exc:
        out.append(str(exc))
        fns = [] if fns is None else list(fns)
    if not h.ip:
        out.append("no core address (ssh)")
    if not (h.ipmi_host and h.ipmi_user):
        out.append("no iLO address or IPMI secret")
    if (h.ipmi_type or (prof.ipmi_type if prof else "")).lower() not in ("ilo6", "ilo7", "hpe", ""):
        out.append(f"only HPE iLO 6/7 (owner 08:50), not {h.ipmi_type}")
    if prof is None:
        out.append("no server profile")
    else:
        if prof.layout is None:
            out.append(f"server profile {prof.code} has no boot storage")
        elif why := prof.layout.problems((prof.boot or {}).get("size_gb")):
            out.append(f"boot storage {prof.layout.code}: {'; '.join(why)}")
        if not prof.bios_version or not prof.ilo_version:
            out.append(f"server profile {prof.code}: its BIOS and iLO versions are not set")
        if not (prof.boot or {}).get("dev"):
            out.append(f"server profile {prof.code}: no boot device")
        if not prof.assign_complete:
            out.append(f"server profile {prof.code}: the port aliases are not all assigned")
        if prof.gpu_count:
            out.append("GPU hosts are not deployed yet (their passthrough settings are to come)")
    s = h.site
    for what, ok in (
        ("NTP servers", s.ntp_servers),
        ("DNS servers", s.dns_servers),
        ("time zone", s.timezone),
        ("IPMI log collector", (s.log_collectors or {}).get("ipmi", {}).get("host")),
    ):
        if not ok:
            out.append(f"site {s.code}: no {what}")
    # commit-259: the core services' collector as the logging step needs it (host, port, tcp/udp)
    from . import logconf

    try:
        logconf.collector(s)
    except logconf.LogConfError as exc:
        out.append(str(exc))
    nets = host_networks(h, p)
    kinds = {n.kind for n in nets}
    need = [(Network.Kind.CORE_KVM, "core-kvm"), (Network.Kind.NFS, "NFS")]
    if "frr" in fns:  # T36 B4a: EVPN only for a host service with frr (a mgmt platform's hosts have none)
        need.insert(1, (Network.Kind.EVPN, "EVPN"))
    for k, label in need:
        if k not in kinds:
            out.append(f"site {s.code}: no {label} network for the hosts")
    if h.ip and not any(ipaddress.ip_address(h.ip) in n.net for n in nets):
        out.append(f"{h.ip} is on none of the host networks")
    if "frr" in fns and not any(n.rrs for n in nets if n.kind == Network.Kind.EVPN):
        out.append("the EVPN network lists no route reflectors")
    if not nfs_datastores(cluster):
        out.append(f"cluster {cluster.name}: no NFS datastores")
    user, key, pub, one_pub, one_key = logins(p)
    if not (user and key and pub):
        out.append("the platform's SA secret has no private key")
    if not (one_pub and one_key):
        out.append("the platform's OpenNebula secret has no oneadmin key")
    st = _settings()
    if not st.internal_domain:
        out.append("Settings → Customer: the internal domain is not set")
    if not hostmedia.node_for(s.code):
        out.append(f"this installation has no portal node at {s.code} to serve the ISOs")
    if power == "On" and not confirmed:
        out.append("the host is powered on: confirm that it may be switched off and wiped")
    others = [c for c in h.clusters.exclude(pk=cluster.pk)]
    if others:
        out.append(f"the host is in another cluster: {', '.join(c.name for c in others)}")
    return out


# --- the run -------------------------------------------------------------------------------------------------------


class Run:
    """One run's context: the records, the iLO client, ssh to the host, the log."""

    def __init__(self, dep, sleep=time.sleep):
        self.dep, self.h, self.cluster = dep, dep.host, dep.cluster
        self.p, self.prof, self.site = self.cluster.platform, self.h.profile, self.h.site
        self.sleep = sleep
        self._ilo = None

    # the log and the steps
    def log(self, line):
        stamp = timezone.now().strftime("%H:%M:%S")
        HostDeployment.objects.filter(pk=self.dep.pk).update(log=self.dep.log + f"{stamp} {line}\n")
        self.dep.log += f"{stamp} {line}\n"

    def mark(self, key, status, detail=""):
        self.dep.steps[key] = {
            "status": status,
            "at": timezone.now().isoformat(timespec="seconds"),
            "detail": str(detail)[:500],
        }
        HostDeployment.objects.filter(pk=self.dep.pk).update(steps=self.dep.steps)

    def keep(self, **data):
        self.dep.data.update(data)
        HostDeployment.objects.filter(pk=self.dep.pk).update(data=self.dep.data)

    # the iLO and the host
    @property
    def c(self):
        if self._ilo is None:
            from .redfish import Client, host_credentials

            user, password = host_credentials(self.h)
            self._ilo = Client(self.h.ipmi_host, user, password, timeout=30, port=self.h.ipmi_port or 443)
        return self._ilo

    def ssh(self, script, timeout=1800, stdin_extra=""):
        """A bash script run on the host as root (sudo), its text on stdin; its output."""
        from . import sshexec

        user, key, *_r = logins(self.p)
        return sshexec.run(
            self.h.ip,
            22,
            user,
            key,
            "sudo -n bash -s",
            site=self.site.code,
            stdin=stdin_extra + script,
            timeout=timeout,
            stdout_reason=True,  # commit-168: the run's own scripts give their reasons
        )

    def nets(self):
        return _net_rows(host_networks(self.h, self.p))

    @property
    def fns(self):
        """T36 B4a: the functions of the host's service (frr, gpu, ... decide which steps do anything) - commit-524:
        those the run was claimed with (dep.data["fns"]), so a resumed run never mixes two sets."""
        from . import hostservice

        if "fns" in (self.dep.data or {}):
            return list(self.dep.data["fns"])
        return hostservice.functions(self.h, self.p)

    def service_key(self):
        return self.h.service.key if self.h.service_id else "?"


def _seed_url(r):
    from accounts import hostmedia

    return hostmedia.url(r.site.code, f"{hostmedia.SEED}/{r.dep.data['seed']}")


def _installer_url(r):
    from accounts import hostmedia

    pin = r.dep.data.get("installer") or {}  # commit-142: the ISO checked at B0, never one chosen later
    if not pin:
        raise DeployError("no installer ISO was chosen for this run (B0)")
    ok, why = hostmedia.on_site_node(r.site.code, pin)
    if not ok:
        raise DeployError(why)
    return hostmedia.url(r.site.code, pin["file"])


# B0 ---------------------------------------------------------------------------------------------------------------


def s_checks(r):
    from accounts import hostmedia

    ok, detail = hostmedia.check(log=r.log)  # B0.6 / A4: before every run
    r.log(f"installation media: {detail}")
    power = ilo.power_state(r.c)
    why = problems(r.h, r.cluster, confirmed=r.dep.power_confirmed, power=power, fns=r.fns)  # commit-526
    # commit-142: the installer of this run - chosen, checked whole here and pinned under the state lock, the lock a
    # refresh holds until it has pruned, so no refresh deletes it once chosen; then checked on the node that serves
    # this site's iLOs
    with hostmedia.locked():
        pin = {k: v for k, v in (hostmedia.current() or {}).items() if k in ("version", "file", "sha256")}
        if pin and hostmedia.has(pin):
            r.keep(installer=pin)
    there, missing = hostmedia.on_site_node(r.site.code, pin or None)
    if not there:
        why.append(missing)
    one = _one_or_none(r)  # T36 B4b: a platform whose front end is not there yet has no host to look at
    hid = _one_host_id(one, r.h.name) if one is not None else None
    if hid is not None:
        h = one.xml("one.host.info", hid)
        vms = h.findall("VMS/ID")
        if vms:
            why.append(f"the host is in OpenNebula (#{hid}) with {len(vms)} VM(s)")
        cid = _one_cluster_id(one, r.cluster.name)
        if (h.findtext("CLUSTER_ID") or "") != str(cid):  # commit-143
            why.append(
                f"OpenNebula has the host (#{hid}) in cluster {h.findtext('CLUSTER') or h.findtext('CLUSTER_ID')}, not {r.cluster.name}"
            )
    try:  # commit-229: the firewall's inputs are in the records before anything is wiped
        firewall_rules(r)
    except DeployError as exc:
        why.append(str(exc))
    if why:
        raise DeployError("; ".join(why))
    if hid is not None:  # commit-137: no new VMs from here on; the power-off checks again that it has none
        one.call("one.host.status", hid, 1)
        r.keep(one_id=hid)
    return f"power {power}; all checks passed" + (f"; OpenNebula host #{hid} disabled" if hid is not None else "")


# B1 ---------------------------------------------------------------------------------------------------------------


def s_snapshot(r):
    if not r.dep.snapshot:
        snap = ilo.snapshot(r.c)
        HostDeployment.objects.filter(pk=r.dep.pk).update(snapshot=snap)
        r.dep.snapshot = snap
    return "saved"


def s_versions(r):
    m, s = r.c.get(ilo.MGR), r.c.get(ilo.SYS)
    bios, fw = str(s.get("BiosVersion") or ""), str(m.get("FirmwareVersion") or "")
    model = str(m.get("Model") or "").lower().replace(" ", "")
    if model not in ("ilo6", "ilo7"):
        raise DeployError(f"only iLO 6/7: this is {m.get('Model')}")
    bad = []
    if bios != r.prof.bios_version:
        bad.append(f"BIOS {bios} ≠ {r.prof.bios_version}")
    if fw != r.prof.ilo_version:
        bad.append(f"iLO {fw} ≠ {r.prof.ilo_version}")
    if bad:
        raise DeployError("firmware differs from the profile: " + "; ".join(bad))
    return f"BIOS {bios}, iLO {fw}"


def s_power_off(r):
    # commit-137: right before the power-off (a resumed run too) the host is looked up in OpenNebula by name now,
    # disabled for scheduling, and must have no VMs
    one = _one_or_none(r)  # commit-528: a platform with no front end yet has no host to look at
    hid = _one_host_id(one, r.h.name) if one is not None else None
    if hid is not None:
        one.call("one.host.status", hid, 1)
        r.keep(one_id=hid)
        vms = one.xml("one.host.info", hid).findall("VMS/ID")
        if vms:
            raise DeployError(f"OpenNebula host #{hid} has {len(vms)} VM(s): migrate them first")
    ilo.power(r.c, on=False, sleep=r.sleep)
    ilo.eject_all(r.c)
    return "off, drives empty" + ("" if one is not None else " (no front end yet: OpenNebula not asked)")


def s_ilo(r):
    from . import redfish
    from .hw_views import site_settings_of

    w = site_settings_of(r.h)
    zone = redfish.zone_for(r.c.settings().get("zones") or [], w["offset"])
    changed = ilo.ilo_settings(
        r.c,
        r.h.name,
        _settings().internal_domain,
        r.prof.ilo_services or {},
        ntp=w["ntp"] or None,
        dns=w["dns"] or None,
        tz_index=zone,
        syslog=w["syslog"] or None,
        log=r.log,
    )
    if changed:
        r.log("iLO network or services changed: restarting the iLO")
        ilo.ilo_restart(r.c, sleep=r.sleep)
    # commit-131: read back, after the restart too
    bad = ilo.ilo_settings_differ(
        r.c,
        r.h.name,
        r.prof.ilo_services or {},
        ntp=w["ntp"] or None,
        dns=w["dns"] or None,
        syslog=w["syslog"] or None,
        tz_index=zone,
    )
    minor = [b for b in bad if b.startswith("syslog")]  # owner 2026-11-19 14:22: syslog is minor - a warning
    if minor:
        r.log("iLO: " + "; ".join(minor) + " (minor: the deployment goes on)")
    if bad := [b for b in bad if b not in minor]:
        raise ilo.IloError("the iLO does not hold: " + "; ".join(bad))
    r.keep(lldp=1)
    return "written" + (", iLO restarted" if changed else "")


POST_DONE = ("SystemHardwareInitializationComplete", "SetupEntered", "OSBootStarted", "OSRunning", "BootFailed")


def s_bios_reset(r):
    if not r.dep.reset_factory:
        return "skipped (not asked)"
    ilo.bios_reset(r.c)
    ilo.power(r.c, on=True, sleep=r.sleep)  # one POST applies the reset (§B1, "Reset to factory")
    got = ilo._wait(lambda: ilo.boot_progress(r.c), lambda x: x in POST_DONE, 900, sleep=r.sleep, what="POST")
    ilo.power(r.c, on=False, sleep=r.sleep)
    return f"factory settings applied (POST: {got})"


BIOS_SCOPE = 2  # the kept bios_target holds only the profile, the NICs' boot and the time zone (2026-11-19 17:55)


def _bios_target(r, current=None):
    from . import redfish

    current = ilo.bios_current(r.c) if current is None else current
    return ilo.bios_target(current, r.prof.bios or {}, ilo.tz_attr(redfish.utc_offset(r.site.tz)))


def s_bios(r):
    current = ilo.bios_current(r.c)
    old = r.dep.data.get("bios_target") or {}
    managed = set(r.prof.bios or {}) | {"TimeZone"}
    gone = sorted(k for k in old if (k in managed or ilo.NIC_BOOT.search(k)) and k not in current)
    if gone:  # the run set them; the BIOS does not show them now - never dropped from the check silently
        raise DeployError(f"the BIOS no longer shows settings this run set: {', '.join(gone[:10])}")
    target = _bios_target(r, current)
    # owner 2026-11-20 12:56 ("B"): after this run's factory reset the iLO's reading may still be the one from before
    # it (a redeploy found the old target there, staged nothing and the BIOS stayed at factory) - the whole target is
    # staged then, whatever the reading says
    change = ilo.bios_pending(r.c, target, current, whole=bool(r.dep.reset_factory))
    r.keep(bios_target=target, bios_scope=BIOS_SCOPE)  # commit-193: the readback checks exactly this
    return f"{len(change)} setting(s) pending"


def s_storage(r):
    # iLO 6/7 know the storage only after POST (2026-11-19, a test host): started into the BIOS setup when needed,
    # and off again after, so the installer's start-up below is the next one
    ok = False
    try:
        ctls = ilo.discover_storage(r.c, log=r.log, sleep=r.sleep)
        # commit-220, core-065: the RAID work on the listing discovery checked
        done = ilo.local_storage(r.c, (r.prof.boot or {}).get("size_gb"), log=r.log, sleep=r.sleep, ctls=ctls)
        ok = True
    finally:
        # commit-180/183: off again whatever ended the step - no host is left in the BIOS setup
        if ok:  # commit-182: the installer's start-up (power_on) needs the host off - a failed power-off fails the step
            ilo.power(r.c, on=False, sleep=r.sleep)
        else:  # the step's own error (or interruption) stays the one raised
            try:
                ilo.power(r.c, on=False, sleep=r.sleep)
            except Exception as exc:  # noqa: BLE001 - logged beside the step's own error
                r.log(f"storage: the host could not be powered off again: {exc}")
    r.keep(local=[{"controller": c, "raid": raid, "drives": n, "state": st} for c, raid, n, st in done if raid])
    return (
        ", ".join(
            f"{c}: {raid or 'one drive, not used'} ({n}){' pending' if st == 'pending' else ''}"
            for c, raid, n, st in done
        )
        or "no local storage"
    )


def _ports(r):
    """{alias: {"mac" or "name", "mtu"}} - the MACs from the iLO (NetworkAdapters ports), else the predicted names."""
    macs = r.dep.data.get("macs") or {}
    out = {}
    mtu = {a: b["mtu"] for b in r.prof.bonds for a in b.get("aliases") or []}
    for alias, port in r.prof.assign.items():
        out[alias] = {"mac": macs.get(port, ""), "name": port, "mtu": mtu.get(alias, 1500)}
    return out


def read_macs(c):
    """{predicted Linux name: MAC} of the host's ports, from the iLO (by the card's PCI slot and the port's index)."""
    out = {}
    hw = c.hardware()
    for card in hw.get("cards") or []:
        links = card.get("links") or []
        out.update(dict.fromkeys(links, ""))
    for a in c._members("/redfish/v1/Chassis/1/NetworkAdapters"):
        slot = str((((a.get("Location") or {}).get("PartLocation") or {}).get("LocationOrdinalValue")) or "")
        link = (a.get("Ports") or a.get("NetworkPorts") or {}).get("@odata.id")
        ports = c._members(link) if link else []
        card = next((x for x in hw.get("cards") or [] if str(x.get("slot")) == slot), None)
        if card is None:
            continue
        for i, p in enumerate(sorted(ports, key=lambda p: str(p.get("Id") or ""))):
            mac = ((p.get("Ethernet") or {}).get("AssociatedMACAddresses") or [""])[0]
            if i < len(card.get("links") or []) and mac:
                out[card["links"][i]] = mac.upper()
    return out


def s_media(r):
    from accounts import hostmedia

    from . import autoinstall

    try:
        r.keep(macs=read_macs(r.c))
    except Exception as exc:  # noqa: BLE001 - the predicted names stand
        r.log(f"MACs not read ({exc}); the ports are matched by their predicted names")
    st = _settings()
    user, _key, pub, _one_pub, _one_key = logins(r.p)
    nets = r.nets()
    core = next(n for n in nets if n["kind"] == Network.Kind.CORE_KVM)
    net = autoinstall.network(
        _ports(r), r.prof.bonds, nets, r.h.ip, r.site.dns_servers or core["dns"], [st.internal_domain]
    )
    fqdn = f"{r.h.name}.{st.internal_domain}"
    token = r.dep.data.get("run_token") or __import__("secrets").token_hex(8)
    r.keep(run_token=token)
    text = autoinstall.user_data(
        r.h.name, fqdn, r.prof.boot["dev"], r.prof.layout, net, r.site.tz, user, [pub], marker=token
    )
    import os
    import tempfile

    old = r.dep.data.get("seed")
    if old:
        hostmedia.drop_seed(r.site.code, old)
    name = hostmedia.seed_name()
    with tempfile.TemporaryDirectory() as d:
        data = autoinstall.make_iso(text, autoinstall.meta_data(r.h.name), os.path.join(d, "seed.iso"))
    hostmedia.put_seed(r.site.code, name, data)
    r.keep(seed=name)
    ilo.insert(r.c, "cd", _installer_url(r))
    ilo.insert(r.c, "usb", _seed_url(r))
    ilo.boot_cd_once(r.c)
    return f"installer ISO and answers ISO ({len(data)} bytes) inserted, CD once"


def s_power_on(r):
    ilo.power(r.c, on=True, sleep=r.sleep)
    return "on: POST applies the BIOS and RAID, then the installer boots"


# B3 ---------------------------------------------------------------------------------------------------------------


INSTALLER_DISABLED = "disabled_by=Disabled by Ubuntu live installer after first boot."


def _installer_disabled(out):
    """cloud-init reports "disabled", the file that disables it is the live installer's own, and cloud-init's final
    stage is over (commit-188: the installer writes that file during the final stage - boot-finished is its end)."""
    lines = out.splitlines()
    disabled = '"status": "disabled"' in out or "status: disabled" in out
    return disabled and INSTALLER_DISABLED in lines and "boot_finished=yes" in lines


def s_install(r):
    deadline = INSTALL_LIMIT
    waited = 0
    last = ""
    while waited <= deadline:
        try:
            out = r.ssh(
                "cloud-init status --wait >/dev/null 2>&1; cloud-init status --format json || cloud-init status;"
                " echo; echo marker=$(cat /etc/claugine-deploy 2>/dev/null);"
                " echo disabled_by=$(head -n1 /etc/cloud/cloud-init.disabled 2>/dev/null);"
                " if test -f /var/lib/cloud/instance/boot-finished; then echo boot_finished=yes; fi",  # commit-190: exit 0
                timeout=120,
            )
            # commit-141: this run's installation only - the marker its answers file wrote
            ours = f"marker={r.dep.data.get('run_token')}" in out
            if ours and ('"done"' in out or "status: done" in out):
                return "installed by this run; cloud-init done"
            # 2026-11-19, Ubuntu 26.04: the live installer turns cloud-init off after the first boot - "disabled" by
            # its own marker file is a finished installation (owner 16:30 "fix the install check")
            if ours and _installer_disabled(out):
                return "installed by this run; cloud-init done and turned off by the installer"
            if "marker=" in out and f"marker={r.dep.data.get('run_token')}" not in out:
                last = "an OS that this run did not install answers (the installer did not boot?)"
                r.sleep(30)
                waited += 30
                continue
            last = out.strip()[-200:]
        except Exception as exc:  # noqa: BLE001 - not up yet
            last = str(exc)[-200:]
        r.sleep(30)
        waited += 30
    try:
        last += f"; boot progress {ilo.boot_progress(r.c)}, power {ilo.power_state(r.c)}"
    except Exception:  # noqa: BLE001
        pass
    raise DeployError(f"not installed after {INSTALL_LIMIT // 60} minutes: {last}")


def s_eject(r):
    ilo.eject_all(r.c)
    return "both drives empty"


# B4 ---------------------------------------------------------------------------------------------------------------


# owner 2026-11-20 (the comparison of a cluster's hosts, 08:26-10:13): no kernel dump (kdump-tools, kexec-tools - kexec
# made a reboot power the host off - and their crashkernel), timesyncd with the site's NTP instead of chrony, snapd
# removed, pinned and masked, fwupd masked, and oneadmin (9869, "ONE Service User") before the OpenNebula packages
OS_BASE = r"""set -e
export DEBIAN_FRONTEND=noninteractive
if command -v snap >/dev/null; then snap list 2>/dev/null | awk 'NR>1 {print $1}' | while read -r s; do snap remove --purge "$s" || true; done; fi
apt-get purge -y -q snapd kdump-tools kexec-tools chrony >/dev/null
# commit-231: removed for certain, and no crashkernel left for the next boot
for p in snapd kdump-tools kexec-tools chrony; do
  dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'install ok installed' && { echo "$p is still installed" >&2; exit 1; }
done
printf 'Package: snapd\nPin: release *\nPin-Priority: -1\n' > /etc/apt/preferences.d/no-snap.pref
for u in snapd.service snapd.socket snapd.seeded.service snapd.apparmor.service fwupd.service fwupd-refresh.service fwupd-refresh.timer; do
  systemctl stop "$u" >/dev/null 2>&1 || true; systemctl mask "$u" >/dev/null
done
rm -f /etc/default/grub.d/kdump-tools.cfg
update-grub >/dev/null 2>&1  # commit-231: always - an earlier attempt may have removed the drop-in only
# commit-240: grep's own status - 1 is clean, 0 a match, anything else an unreadable file
rc=0; grep -q crashkernel /boot/grub/grub.cfg || rc=$?
[ "$rc" = 1 ] || { [ "$rc" = 0 ] && echo "crashkernel is still in /boot/grub/grub.cfg" >&2 || echo "/boot/grub/grub.cfg is not readable" >&2; exit 1; }
if grep -rqs crashkernel /etc/default/grub /etc/default/grub.d/; then
  echo "crashkernel is still in the boot configuration: $(grep -rls crashkernel /etc/default/grub /etc/default/grub.d/)" >&2; exit 1
fi
grep -q crashkernel /proc/cmdline && touch /var/run/reboot-required
dpkg -s systemd-timesyncd >/dev/null 2>&1 || apt-get install -y -q systemd-timesyncd >/dev/null
install -d -m 755 /etc/systemd/timesyncd.conf.d
printf '[Time]\nNTP=%s\n' "$CLAUGINE_NTP" > /etc/systemd/timesyncd.conf.d/claugine.conf
systemctl enable systemd-timesyncd >/dev/null 2>&1; systemctl restart systemd-timesyncd; timedatectl set-ntp true
getent group oneadmin >/dev/null || groupadd -g 9869 oneadmin
id oneadmin >/dev/null 2>&1 || useradd -u 9869 -g 9869 -c "ONE Service User" -d /var/lib/one -s /bin/bash oneadmin
# owner 2026-11-20 16:34 (Q7): what the drivers create gets no group write and no access for others - a file created
# 0666 is 640, a directory created 0777 is 750 (a umask only clears bits: a 0600 file stays 0600)
chfn -o umask=0027 oneadmin
[ "$(id -u oneadmin):$(id -g oneadmin)" = "9869:9869" ] || { echo "oneadmin is $(id oneadmin), not 9869:9869" >&2; exit 1; }
# owner 2026-11-20 20:28 "you dont need create /var/lib/one / you just need to create oneadmin user with properly home
# and id": the user's home is /var/lib/one (checked); the directory itself is left to the packages
[ "$(getent passwd oneadmin | cut -d: -f6)" = /var/lib/one ] || { echo "oneadmin's home is not /var/lib/one" >&2; exit 1; }
echo ok
"""


def s_os_base(r):
    ntp = " ".join(x for x in (r.site.ntp_servers or []) if x)
    if not ntp:
        raise DeployError(f"site {r.site.code} has no NTP servers: the hosts take theirs from the site settings")
    r.ssh(f"CLAUGINE_NTP={shlex.quote(ntp)}\n" + OS_BASE, timeout=900)
    return f"no kernel dump; timesyncd ({ntp}); snapd and fwupd masked; oneadmin 9869"


def _staged_target(r):
    """What this run staged (commit-193), every key checked - one missing after POST too, and an empty target kept as
    empty. A run that kept none stops: its BIOS step must run again. A run staged by v01.01.39 or before kept the whole
    factory set: that target is dropped (owner 2026-11-19 18:37 "remove unused settings") and the BIOS settings are
    staged again for this version - nothing to change goes on to the check; anything staged applies at the next
    start-up, so the run stops and says so."""
    if "bios_target" not in r.dep.data:
        raise DeployError("this run kept no BIOS target: run it again from the BIOS settings step")
    if r.dep.data.get("bios_scope") == BIOS_SCOPE:
        return dict(r.dep.data.get("bios_target") or {})
    current = ilo.bios_current(r.c)
    old = r.dep.data.get("bios_target") or {}
    managed = set(r.prof.bios or {}) | {"TimeZone"}
    gone = sorted(k for k in old if (k in managed or ilo.NIC_BOOT.search(k)) and k not in current)
    if gone:  # the run set them; the BIOS does not show them now - never dropped from the check silently
        raise DeployError(f"the BIOS no longer shows settings this run set: {', '.join(gone[:10])}")
    target = _bios_target(r, current)
    change = ilo.bios_pending(r.c, target, current)
    r.keep(bios_target=target, bios_scope=BIOS_SCOPE)
    r.log(f"BIOS target of an older version replaced: {len(target)} setting(s), {len(change)} staged again")
    if change:
        raise DeployError(
            f"BIOS settings staged again for this version ({', '.join(sorted(change)[:10])}): they apply at the next "
            "start-up - restart the host, then resume"
        )
    return target


def s_readback(r):
    target = _staged_target(r)
    diff = ilo.bios_differences(r.c, target)
    if diff:
        raise DeployError(
            "BIOS differs from the target: " + ", ".join(f"{k} {a} ≠ {b}" for k, (a, b) in sorted(diff.items())[:20])
        )
    local = r.dep.data.get("local") or []
    ctls = {c["id"]: c for c in ilo.controllers(r.c)}
    vols = {k: [v["raid"] for v in c["volumes"]] for k, c in ctls.items()}
    bad = [
        f"{x['controller']}: {vols.get(x['controller'])} ≠ [{x['raid']}]"
        for x in local
        if vols.get(x["controller"]) != [x["raid"]]
    ]
    bad += [  # commit-129: the one volume is made of all the controller's drives
        f"{x['controller']}: the volume is not over all its drives"
        for x in local
        if x["controller"] in ctls
        and vols.get(x["controller"]) == [x["raid"]]
        and ctls[x["controller"]]["volumes"][0].get("drives") != sorted(ctls[x["controller"]]["drives"])
    ]
    if bad:
        raise DeployError("RAID differs: " + "; ".join(bad))
    for x in local:  # commit-138: the volume as the OS will find it - its identifiers and its exact size
        v = ctls[x["controller"]]["volumes"][0]
        x["ids"], x["size"] = v.get("ids") or [], v["size"]
    r.keep(local=local)
    return "BIOS and RAID as set"


def _ping_target(n):
    return n.get("gateway") or (n.get("rrs") or [None])[0]


def network_script(r):
    lines = ["set -e", "fail=0", 'why=""']
    for b in r.prof.bonds:
        lines.append(
            f'f=/proc/net/bonding/{b["name"]}; grep -q "802.3ad" $f || {{ why="$why; {b["name"]}: not LACP"; fail=1; }}; '
            f'n=$(grep -c "^MII Status: up" $f || true); [ "$n" -ge 3 ] || {{ why="$why; {b["name"]}: $n of 3 up"; fail=1; }}'
        )
    for n in r.nets():
        br = n["bridge"]
        lines.append(
            f'ip -4 -o addr show dev {shlex.quote(br)} | grep -q inet || {{ why="$why; {br}: no address"; fail=1; }}'
        )
        t = _ping_target(n)
        if t:
            size = int(n["mtu"]) - 28
            lines.append(
                f'ping -c 2 -W 2 -M do -s {size} {shlex.quote(t)} >/dev/null || {{ why="$why; {br}: no ping to {t} at MTU {n["mtu"]}"; fail=1; }}'
            )
    from .autoinstall import RING_MIN

    for alias in sorted(r.prof.assign):  # commit-135: the ring buffers the udev rule sets, on every port
        for d in ("RX", "TX"):  # commit-135: both directions
            v = d.lower()
            lines.append(
                f"{v}=$(ethtool -g {alias} 2>/dev/null | awk '/^Current/ {{c=1}} c && /^{d}:/ {{print $2; exit}}'); "
                f'[ "${{{v}:-0}}" -ge {RING_MIN} ] || {{ why="$why; {alias}: {d} ring ${{{v}:-?}}"; fail=1; }}'
            )
    for key in ("iptables", "ip6tables", "arptables"):  # commit-136: the bridge netfilter settings took effect
        lines.append(
            f'[ "$(sysctl -n net.bridge.bridge-nf-call-{key} 2>/dev/null)" = 1 ] || {{ why="$why; bridge-nf-call-{key} not 1"; fail=1; }}'
        )
    # commit-166: the reasons on stderr, where the run's error is taken from
    lines.append('[ "$fail" = 0 ] && echo ok || {{ echo "${{why#; }}" >&2; exit 1; }}'.format())
    return "\n".join(lines) + "\n"


def s_network(r):
    out = r.ssh(network_script(r), timeout=300)
    return out.strip()[-200:] or "ok"


def repos_script(r, base, repo=None):
    """repo: the repository's address, from the same settings snapshot as its login (commit-427)."""
    from . import baseline

    repo = repo or baseline.repo_url(r.p)  # T36 B2: the platform's (Enterprise Edition or public), no address field
    lines = [
        "set -e",
        "install -d -m 755 /etc/apt/keyrings",
        # the signing key is the same for both repositories and the public one needs no login
        f"curl -fsSL {shlex.quote(baseline.PUBLIC_REPO + '/repo2.key')} | gpg --dearmor --yes -o /etc/apt/keyrings/opennebula.gpg",
        f'echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/opennebula.gpg] {repo}/{base["one"]}/Ubuntu/{base["os"]} stable opennebula" > /etc/apt/sources.list.d/opennebula.list',
    ]
    if "frr" in r.fns:  # commit-525: FRR's key and source only for a host service with frr
        lines += [
            "curl -fsSL https://deb.frrouting.org/frr/keys.gpg | gpg --dearmor --yes -o /etc/apt/keyrings/frrouting.gpg",
            f'echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/frrouting.gpg] https://deb.frrouting.org/frr $(. /etc/os-release; echo $VERSION_CODENAME) frr-{base["frr"]}" > /etc/apt/sources.list.d/frrouting.list',
        ]
    else:  # a host made again without frr: no source left from before
        lines += ["rm -f /etc/apt/sources.list.d/frrouting.list /etc/apt/keyrings/frrouting.gpg"]
    lines += [
        "apt-get update -q >/dev/null",
        "echo ok",
    ]
    return "\n".join(lines) + "\n"


def s_repos(r):
    from urllib.parse import urlsplit

    from . import baseline

    st = _settings()  # commit-427: one snapshot - the releases
    p = r.p  # T36 B2 (rules v2 B4): the repository, its login and the OpenNebula version are the platform's
    repo = baseline.repo_url(p)
    auth = ""
    if p.one_ee and p.one_login:  # Q8: the secret is the private repository's only
        user, _k, _p = _account(p.one_login)
        from .credentials import find

        sec = find(p.one_login)
        pw = sec.get("password") if sec is not None and sec.has("password") else ""
        host = urlsplit(repo).hostname
        # the login goes on stdin, into auth.conf.d (0600), never on a command line
        auth = (
            "install -d -m 755 /etc/apt/auth.conf.d\n"
            f"umask 077; cat > /etc/apt/auth.conf.d/opennebula.conf <<'CLAUGINE_AUTH'\nmachine {host}\nlogin {user}\npassword {pw}\nCLAUGINE_AUTH\n"
        )
    r.ssh(repos_script(r, baseline.of_platform(p, baseline.values(st)), repo=repo), stdin_extra=auth, timeout=600)
    return "OpenNebula and FRR repositories" if "frr" in r.fns else "OpenNebula repository"


def packages(r):
    """The packages of the host: frr only with the frr function (T36 B4a, rules v2 B1)."""
    return [k for k in PACKAGES if k != "frr" or "frr" in r.fns]


def s_packages(r):
    pkgs = packages(r)
    r.ssh(
        # owner 2026-11-20 19:34 "any installation of opennebula packages must be only after user creation": nothing
        # is installed unless oneadmin is there as the OS baseline made it
        "set -e\n"
        '[ "$(id -u oneadmin 2>/dev/null):$(id -g oneadmin 2>/dev/null)" = 9869:9869 ] '
        '|| { echo "oneadmin is not 9869:9869 ($(id oneadmin 2>&1)): nothing installed" >&2; exit 1; }\n'
        '[ "$(getent passwd oneadmin | cut -d: -f6)" = /var/lib/one ] '
        "|| { echo 'the home of oneadmin is not /var/lib/one: nothing installed' >&2; exit 1; }\n"
        f"DEBIAN_FRONTEND=noninteractive apt-get install -y -q {' '.join(pkgs)} >/dev/null\n"
        # oneadmin was made before the packages (owner 2026-11-20 10:06): its groups as on the reference host
        "for g in disk kvm libvirt; do usermod -aG $g oneadmin; done\n"
        f"systemctl enable --now {' '.join(EXPORTERS)} >/dev/null 2>&1\n"
        f"for u in {' '.join(EXPORTERS)}; do systemctl is-active --quiet $u "
        '|| { echo "$u is not running" >&2; exit 1; }; done\n'
        "echo ok\n",
        timeout=1800,
    )
    return ", ".join(pkgs)


def s_upgrade(r):
    out = r.ssh(
        "set -e\n"
        f"apt-mark hold {' '.join(OVMF)} >/dev/null\n"
        "apt-mark showhold\n"
        "DEBIAN_FRONTEND=noninteractive apt-get -y -q -o Dpkg::Options::=--force-confold full-upgrade >/dev/null\n",
        timeout=3600,
    )
    held = set(out.split())
    if not set(OVMF) <= held:
        raise DeployError(f"not held: {', '.join(sorted(set(OVMF) - held))}")
    return "ovmf held; upgraded"


def s_tuned(r):
    r.ssh(
        "tuned-adm profile virtual-host\nvirsh net-destroy default >/dev/null 2>&1 || true\nvirsh net-undefine default >/dev/null 2>&1 || true\ntuned-adm active\n"
    )
    return "virtual-host; libvirt default network removed"


LOCAL_FS = r"""set -e
mnt=/var/lib/one/datastores
lv=$(readlink -f /dev/vg_one/lv_one 2>/dev/null || true)
if mountpoint -q "$mnt"; then  # commit-140: a repeat - the mount must be this run's volume; nothing is wiped then
  [ -n "$lv" ] && [ "$(readlink -f "$(findmnt -no SOURCE "$mnt")")" = "$lv" ] || {
    echo "$mnt is mounted from $(findmnt -no SOURCE "$mnt"), not vg_one/lv_one: nothing is wiped" >&2; exit 1; }
fi
bootdisk=$(lsblk -no PKNAME "$(findmnt -no SOURCE /boot)" | head -1)
[ -n "$bootdisk" ] || { echo "the boot disk is not known: nothing is wiped" >&2; exit 1; }
dev=""
for id in $IDS; do  # commit-138: the RAID volume B1 made, by its NAA identifier - nothing else is ever wiped
  [ -e "/dev/disk/by-id/wwn-0x$id" ] && dev=$(readlink -f "/dev/disk/by-id/wwn-0x$id") && break
done
[ -n "$dev" ] || { echo "the RAID volume (${IDS:-no identifier}) is not a disk here: nothing is wiped" >&2; exit 1; }
[ "$(basename "$dev")" != "$bootdisk" ] || { echo "$dev is the boot disk: nothing is wiped" >&2; exit 1; }
[ "$(lsblk -dnbo SIZE "$dev")" = "$SIZE" ] || { echo "$dev is not $SIZE bytes: nothing is wiped" >&2; exit 1; }
# commit-158: LVM read first - a failed read stops it, never taken for "no vg_one"; when vg_one is here already, its
# only physical volume must be this disk - checked before anything is wiped; commit-140: then nothing is made again.
# commit-158/164: LVM's own output only (its warnings never counted), its exit status checked; matched by bash on the
# text it holds - no input redirection, no tool whose failure could read as "no vg_one"
set -f
lvm_err=$(mktemp)  # commit-166: each LVM read once, its own error kept for the message
trap 'rm -f "$lvm_err"' EXIT
vgs_all=$(vgs --noheadings -o vg_name 2>"$lvm_err") || {
  echo "LVM could not be read ($(tail -n 1 "$lvm_err")): nothing is wiped" >&2; exit 1; }
have_one=no
for vg in $vgs_all; do
  if [ "$vg" = vg_one ]; then have_one=yes; fi
done
if [ "$have_one" = yes ]; then
  pv_all=$(pvs --noheadings -o pv_name -S vg_name=vg_one 2>"$lvm_err") || {
    echo "LVM could not be read ($(tail -n 1 "$lvm_err")): nothing is wiped" >&2; exit 1; }
  disk=$(readlink -f "$dev") || disk=""
  n=0 same=no
  for pv in $pv_all; do
    n=$((n + 1))
    real=$(readlink -f "$pv") || real=""
    if [ -n "$real" ] && [ -n "$disk" ] && [ "$real" = "$disk" ]; then same=yes; fi
  done
  [ "$n" = 1 ] && [ "$same" = yes ] || { echo "vg_one is on ${pv_all:-no volume}, not $dev: nothing is wiped" >&2; exit 1; }
else
  wipefs -a "$dev" >/dev/null
  pvcreate -ff -y "$dev" >/dev/null
  vgcreate vg_one "$dev" >/dev/null
fi
lvs vg_one/lv_one >/dev/null 2>&1 || lvcreate -y -l 100%FREE -n lv_one vg_one >/dev/null
[ "$(blkid -s TYPE -o value /dev/vg_one/lv_one)" = xfs ] || mkfs.xfs -q -f /dev/vg_one/lv_one
install -d -m 750 -o oneadmin -g oneadmin "$mnt"
uuid=$(blkid -s UUID -o value /dev/vg_one/lv_one)
grep -q "$uuid" /etc/fstab || echo "UUID=$uuid $mnt xfs defaults 0 2" >> /etc/fstab
systemctl daemon-reload
mountpoint -q "$mnt" || mount "$mnt"
chown oneadmin:oneadmin "$mnt"; chmod 750 "$mnt"
echo "$dev $SIZE"
"""


def s_local_fs(r):
    local = r.dep.data.get("local") or []
    if not local:
        return "no local storage"
    x = local[0]
    if not x.get("size"):
        raise DeployError("the RAID volume was not read back (B4): nothing is wiped")
    ids = " ".join(i for i in x.get("ids") or [] if re.match(r"^[0-9a-f]{16,40}$", i))
    head = f"IDS={shlex.quote(ids)}\nSIZE={int(x['size'])}\n"
    out = r.ssh(head + LOCAL_FS, timeout=600).strip()
    r.keep(local_dev=out)
    return f"/var/lib/one/datastores on {out}"


def nfs_script(mounts):
    # the datastores' parent as on the reference host (owner 2026-11-20 15:58 "follow rights model"): 750 oneadmin:oneadmin
    lines = ["set -e", "install -d -m 750 -o oneadmin -g oneadmin /var/lib/one/datastores"]
    for ds_id, src, fstype, opts in mounts:
        d = f"/var/lib/one/datastores/{ds_id}"
        # owner 2026-11-20 16:02: 750 oneadmin:oneadmin when the directory is made and again after its first mount
        # commit-233: a datastore already mounted (a resumed run) is not prepared again - its export decides
        lines.append(f"mountpoint -q {d} || install -d -m 750 -o oneadmin -g oneadmin {d}")
        line = f"{src}   {d}   {fstype} {opts} 0 0"
        lines.append(f"grep -q ' {d} ' /etc/fstab || echo {shlex.quote(line)} >> /etc/fstab")
    lines.append("systemctl daemon-reload")
    for ds_id, *_x in mounts:
        d = f"/var/lib/one/datastores/{ds_id}"
        lines.append(f"mountpoint -q {d} || mount {d}")
        # the mounted export's root: chown as root (an export squashing root refuses it, so only when it differs),
        # chmod as its owner
        lines.append(f'[ "$(stat -c %U:%G {d})" = oneadmin:oneadmin ] || chown oneadmin:oneadmin {d}')
        lines.append(f'[ "$(stat -c %a {d})" = 750 ] || sudo -u oneadmin chmod 750 {d}')
        t = f"{d}/.claugine-check-$$"  # commit-144: as oneadmin, a name of its own, a failure stops the script
        lines.append(f'sudo -u oneadmin touch {t} || {{ echo "oneadmin cannot write in {d}" >&2; exit 1; }}')
        lines.append(f"sudo -u oneadmin rm -f {t}")
    lines.append("echo ok")
    return "\n".join(lines) + "\n"


def s_nfs(r):
    one = _one(r)
    ids = {d.findtext("NAME"): d.findtext("ID") for d in one.xml("one.datastorepool.info").findall("DATASTORE")}
    mounts = []
    for name, src, fstype, opts in nfs_datastores(r.cluster):
        if name not in ids:
            raise DeployError(f"datastore {name} is not on the platform")
        mounts.append((ids[name], src, fstype, opts))
    r.ssh(nfs_script(mounts), timeout=600)
    return ", ".join(f"{i} ← {s}" for i, s, *_x in mounts)


def frr_conf(r):
    """The host's FRR: the site's host-frr template (Infrastructure → Templates) with the host's name and its address
    on the EVPN network - the hosts' BGP password included (owner 2026-11-19 20:24 "fix FRR": the route reflectors
    take only sessions that carry it, as a reference host's does). Nothing left undecided, no empty password."""
    from . import nfconfig

    evpn = next(n for n in r.nets() if n["kind"] == Network.Kind.EVPN)
    me = str(ipaddress.ip_interface(evpn_address(r, evpn)).ip)
    if not evpn["asn"]:
        raise DeployError(f"network {evpn['name']}: no local AS")
    try:  # commit-197: the run's EVPN network; commit-200: one snapshot for what is checked and what is written
        masked, conf, missing = nfconfig.build_host_frr(r.site.code, r.h.name, me, evpn["name"])
    except nfconfig.BuildError as exc:
        raise DeployError(f"FRR: {exc}") from exc
    # commit-198/199: every check on the masked text or on the credentials' labels - nothing of a value in a message
    if missing:
        raise DeployError("FRR: credentials without a value (Credentials): " + ", ".join(missing[:5]))
    open_ = sorted({x.split("»")[0] for x in masked.split("«")[1:] if not x.startswith("secret:")})
    if open_:
        raise DeployError("FRR: the host-frr template leaves values undecided: " + ", ".join(open_[:5]))
    if not any(re.match(r"^\s*neighbor \S+ password «secret:", line) for line in masked.splitlines()):
        raise DeployError("FRR: the hosts' BGP password of the EVPN network is not set in the template's output")
    want = {f"neighbor {rr} peer-group fabric" for rr in evpn["rrs"]}
    if not want <= {line.strip() for line in masked.splitlines()}:
        raise DeployError("FRR: the host-frr template's route reflectors are not the EVPN network's")
    return conf


def evpn_address(r, evpn):
    from .autoinstall import host_address

    core = next(n for n in r.nets() if n["kind"] == Network.Kind.CORE_KVM)
    return host_address(r.h.ip, core["cidr"], evpn["cidr"])


def s_frr(r):
    if "frr" not in r.fns:  # T36 B4a (rules v2 B1): a mgmt platform's hosts have no FRR
        return f"no frr in its service ({r.service_key()})"
    conf = frr_conf(r)
    evpn = next(n for n in r.nets() if n["kind"] == Network.Kind.EVPN)
    script = (
        "set -e\n"
        "cat > /etc/frr/frr.conf <<'CLAUGINE_FRR'\n" + conf + "CLAUGINE_FRR\n"
        "chown frr:frr /etc/frr/frr.conf; chmod 640 /etc/frr/frr.conf\n"
        "sed -i 's/^bgpd=no/bgpd=yes/' /etc/frr/daemons\n"
        # owner 2026-11-20 08:53 "patch /etc/frr/daemons to avoid port conflict": staticd's port as on the other hosts
        "sed -i 's/^staticd_options=.*/staticd_options=\"-A 127.0.0.1 -P 2620\"/' /etc/frr/daemons\n"
        "grep -q '^staticd_options=\"-A 127.0.0.1 -P 2620\"' /etc/frr/daemons\n"
        "systemctl enable frr >/dev/null 2>&1; systemctl restart frr\n"
        "for i in $(seq 1 40); do\n"
        '  n=$(vtysh -c \'show bgp summary json\' 2>/dev/null | python3 -c \'import json,sys; d=json.load(sys.stdin); p=[v for af in d.values() if isinstance(af,dict) for v in (af.get("peers") or {}).values()]; print(sum(1 for x in p if x.get("state")=="Established"))\' 2>/dev/null || echo 0)\n'
        f'  [ "$n" -ge {len(evpn["rrs"])} ] && {{ echo "$n established"; exit 0; }}\n'
        "  sleep 3\n"
        "done\n"
        "vtysh -c 'show bgp summary' | tail -8\n"
        f'echo "BGP: $n of {len(evpn["rrs"])} route reflectors established after 2 minutes" >&2; exit 1\n'
    )
    out = r.ssh(script, timeout=300)
    return out.strip()[-120:]


def s_gpu(r):
    if "gpu" in r.fns or r.prof.gpu_count:  # T36 B4a: passthrough (the gpu function) is to come
        raise DeployError("GPU hosts are not deployed yet")
    return f"no gpu in its service ({r.service_key()})"


ONE_SSH_CONFIG = """Host *
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
  LogLevel QUIET
  ConnectTimeout 5
  ServerAliveInterval 90
  ServerAliveCountMax 5
  Port 22
  User oneadmin
  IdentityFile ${HOME}/.ssh/one-platform.key
"""


def s_keys(r):
    from .credentials import with_comment

    _u, sa_key, pub, one_pub, one_key = logins(r.p)
    # owner 2026-11-20 21:29 "you lost comments of keys - please keep it": each key with the comment its private key
    # keeps, in the reference host's order (the platform's key, then the SA's)
    keys = "\n".join(k for k in (with_comment(one_pub, one_key), with_comment(pub, sa_key)) if k) + "\n"
    # the private key travels on stdin only (B4.12, owner 08:50 "all hosts within platfrom use the same key")
    script = (
        "set -e\n"
        "d=/var/lib/one/.ssh; install -d -m 700 -o oneadmin -g oneadmin $d\n"
        "umask 077\n"
        "cat > $d/authorized_keys <<'CLAUGINE_K'\n" + keys + "CLAUGINE_K\n"
        "cat > $d/config <<'CLAUGINE_C'\n" + ONE_SSH_CONFIG + "CLAUGINE_C\n"
        "cat > $d/one-platform.key <<'CLAUGINE_P'\n" + one_key.strip() + "\nCLAUGINE_P\n"
        "chmod 400 $d/one-platform.key; chmod 600 $d/authorized_keys $d/config\n"
        "chown -R oneadmin:oneadmin $d\n"
        "echo ok\n"
    )
    r.ssh(script)
    # owner 2026-11-20 16:34 (Q7): an ssh session as oneadmin (what the drivers run with) has umask 0027 - checked over
    # the host's own sshd and PAM, as a front end's session would get it
    got = r.ssh(
        f"sudo -u oneadmin ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new {shlex.quote(r.h.name)} umask\n",
        timeout=60,
    ).strip()
    if not got.endswith("0027"):
        raise DeployError(f"oneadmin's ssh sessions have umask {got[-4:] or '?'}, not 0027")
    return "authorized keys, ssh config and the platform key; oneadmin umask 0027"


APPARMOR_LIBVIRTD = "/var/tmp/one/vmm/kvm/virtiofsd.sh ix,"  # OpenNebula's local option, as on the reference host


def firewall_rules(r):
    """ufw's rules (owner 2026-11-20 08:36: "allow any traffic from admin hosts / allow opennebula connection from FE
    nodes / allow interhost communications"), all from the records: anything from the site's management hosts; on the
    core, live-migration and EVPN bridges anything from their own networks; ssh and the node exporters (9100, 9926) on
    the core bridge from the front end's nodes and VIP."""
    mgmt = [x for x in (r.site.management or {}).get("hosts") or [] if x]
    if not mgmt:
        raise DeployError(f"site {r.site.code} has no management hosts: the firewall would lock the portal out")
    rules = [f"allow from {m}" for m in mgmt]
    core = None
    for n in r.nets():
        if n["kind"] in (Network.Kind.CORE_KVM, Network.Kind.CORE_LM, Network.Kind.EVPN):
            bridge = n.get("bridge") or n["name"]
            rules.append(f"allow in on {bridge} from {n['cidr']}")
            if n["kind"] == Network.Kind.CORE_KVM:
                core = bridge
    fe = [x for x in [r.p.fe_vip, *((r.p.fe or {}).get("node_ips") or [])] if x and x != "0.0.0.0"]
    if core is None or not fe:
        raise DeployError("the core network's bridge or the front end's addresses are not in the records")
    for ip in fe:
        rules.append(f"allow in on {core} from {ip} to any port 22 proto tcp")
        rules.append(f"allow in on {core} from {ip} to any port 9100,9926 proto tcp")
    return rules


def security_script(r):
    lines = [
        "set -e",
        # owner 2026-11-20 08:36 "app armor must be enabled and has opennebula specific options"
        f"printf '%s\\n' {shlex.quote(APPARMOR_LIBVIRTD)} > /etc/apparmor.d/local/usr.sbin.libvirtd",
        "systemctl enable apparmor >/dev/null 2>&1; systemctl restart apparmor",
        "aa-status --enabled",
        # owner 2026-11-20 08:36 "check lldp too - on ipmi and host / both must be on"
        "systemctl enable --now lldpd >/dev/null 2>&1; systemctl is-active lldpd >/dev/null",
        # ufw as on the reference host: IPv4 only, incoming and routed dropped; bridged VM traffic is OpenNebula's (its
        # security groups), so it passes ufw's forward chain
        "f=/etc/default/ufw",
        "sed -i -e 's/^IPV6=.*/IPV6=no/' -e 's/^DEFAULT_INPUT_POLICY=.*/DEFAULT_INPUT_POLICY=\"DROP\"/'"
        " -e 's/^DEFAULT_OUTPUT_POLICY=.*/DEFAULT_OUTPUT_POLICY=\"ACCEPT\"/'"
        " -e 's/^DEFAULT_FORWARD_POLICY=.*/DEFAULT_FORWARD_POLICY=\"DROP\"/' $f",
        "b=/etc/ufw/before.rules",
        "if ! grep -q -- '-m physdev --physdev-is-bridged -j RETURN' $b; then",
        "  sed -i '/^# End required lines/a -A ufw-before-forward -m physdev --physdev-is-bridged -j RETURN' $b",
        "  sed -i '/^# End required lines/a # OpenNebula KVM: bridged VM traffic passes ufw - OpenNebula security groups filter it' $b",
        "fi",
        "grep -q -- '-A ufw-before-forward -m physdev --physdev-is-bridged -j RETURN' $b",
    ]
    lines += [f"ufw {rule} >/dev/null" for rule in firewall_rules(r)]
    # commit-229: ufw goes on with a timed way back - turned off again in 3 minutes unless the portal, over a NEW ssh
    # connection, cancels it (s_security)
    lines += [
        "systemctl stop claugine-ufw-rollback.timer >/dev/null 2>&1 || true",
        "systemd-run --quiet --unit=claugine-ufw-rollback --on-active=180 /usr/sbin/ufw --force disable",
        "ufw --force enable >/dev/null",
        "ufw status | head -1",
        "echo ok",
    ]
    return "\n".join(lines) + "\n"


# commit-229/236: the way back cancelled from a new ssh connection. The timer first; its service exists only once the
# timer has started it (014, 2026-11-20 20:25: stopping both at once failed with "Unit ... not loaded"), so it is
# stopped only when loaded, and must not be running afterwards - nothing queued can turn ufw off after the check
CANCEL_WAY_BACK = (
    "set -e\n"
    # commit-254: safe to run again (a retry through the jump host after a dropped session): the timer stopped if it
    # is still there; whether the way back ran is told by ufw itself below
    # commit-255: the timer's state read with the query's own exit checked - a failed query is never "gone"
    "tload=$(systemctl show -p LoadState --value claugine-ufw-rollback.timer) "
    "|| { echo 'the way back timer cannot be read' >&2; exit 1; }\n"
    'case "$tload" in\n'
    "  loaded) systemctl stop claugine-ufw-rollback.timer ;;\n"
    "  not-found) ;;\n"
    '  *) echo "the way back timer is $tload" >&2; exit 1 ;;\n'
    "esac\n"
    "tstate=$(systemctl show -p ActiveState --value claugine-ufw-rollback.timer) "
    "|| { echo 'the way back timer cannot be read' >&2; exit 1; }\n"
    '[ "$tstate" = inactive ] || { echo "the way back timer is $tstate" >&2; exit 1; }\n'
    # 21cfb7a-commit.md (commit-236): every answer of systemd read - absent only when it says so; stop waits for a
    # queued or running job to end
    "load=$(systemctl show -p LoadState --value claugine-ufw-rollback.service) || { echo 'the way back cannot be read' >&2; exit 1; }\n"
    'case "$load" in\n'
    "  loaded) systemctl stop claugine-ufw-rollback.service ;;\n"
    "  not-found) ;;\n"
    '  *) echo "the way back is $load" >&2; exit 1 ;;\n'
    "esac\n"
    "state=$(systemctl show -p ActiveState --value claugine-ufw-rollback.service) || { echo 'the way back cannot be read' >&2; exit 1; }\n"
    'case "$state" in\n'
    "  inactive | failed) ;;\n"
    '  *) echo "the way back is $state" >&2; exit 1 ;;\n'
    "esac\n"
    "ufw status | head -1\n"
)


def way_back_failed(exc):
    """commit-254: a failed cancellation never promises what ufw will do - the session may have stopped the timer
    before it was lost (a direct attempt dropped, a retry through the jump host failing to connect)."""
    return (
        f"cancelling the way back did not finish ({exc}): it is not known whether the timer was stopped - ufw goes "
        "off again in 3 minutes only if it was not; check claugine-ufw-rollback and `ufw status` on the host before "
        "resuming"
    )


def s_security(r):
    out = r.ssh(security_script(r), timeout=600)
    if "Status: active" not in out:
        raise DeployError("ufw is not active: " + out.strip()[-200:])
    # commit-229: a new ssh connection from the portal through the firewall now on - only then the way back is cancelled
    try:
        out = r.ssh(CANCEL_WAY_BACK, timeout=60)
    except Exception as exc:  # noqa: BLE001
        raise DeployError(way_back_failed(exc)) from exc
    if "Status: active" not in out:  # commit-236/254: off means the way back ran before it was cancelled
        raise DeployError("ufw is not active - the way back has run: " + out.strip()[-200:])
    return f"ufw on ({len(firewall_rules(r))} rules), AppArmor on with OpenNebula's option, lldpd on"


# commit-240: grep's own status - 1 is clean, 0 a match, anything else an unreadable /proc/cmdline
NO_CRASHKERNEL = (
    "rc=0; grep -q crashkernel /proc/cmdline || rc=$?\n"
    '[ "$rc" = 1 ] && exit 0\n'
    "[ \"$rc\" = 0 ] && echo 'crashkernel is still on the kernel command line' >&2 || echo '/proc/cmdline is not readable' >&2\n"
    "exit 1\n"
)


def s_reboot(r):
    need = r.ssh("[ -f /var/run/reboot-required ] && echo yes || echo no").strip().endswith("yes")
    if not need:
        r.ssh(network_script(r), timeout=300)
        r.ssh(NO_CRASHKERNEL)  # commit-231: on this path too
        return "no reboot needed; network checked"
    r.ssh("systemctl reboot >/dev/null 2>&1 &", timeout=30)
    r.sleep(60)
    waited = 0
    while waited < 900:
        try:
            r.ssh("true", timeout=30)
            break
        except Exception:  # noqa: BLE001 - not back yet
            r.sleep(15)
            waited += 15
    else:
        raise DeployError("the host did not come back after the reboot")
    r.ssh(network_script(r), timeout=300)
    # commit-231: the boot after the OS baseline has no crash kernel
    r.ssh(NO_CRASHKERNEL)
    r.ssh(
        "set -e\nfor d in $(awk '$2 ~ /^\\/var\\/lib\\/one\\/datastores/ {print $2}' /etc/fstab); do mountpoint -q $d || { echo \"$d not mounted\" >&2; exit 1; }; done\necho ok\n"
    )
    if "frr" in r.fns:
        s_frr(r)
        return "rebooted; network, mounts and BGP checked again"
    return "rebooted; network and mounts checked again"


# B5 ---------------------------------------------------------------------------------------------------------------


def _one(r):
    from tenants.deploy import One

    from .models import PlatformControl

    c = PlatformControl.objects.filter(key=r.p.key).first()
    if c is None:
        raise DeployError(f"{r.p.key}: no endpoint")
    return One(c)


def has_front_end(platform):
    """commit-528: the platform has a front end the portal can ask - its control row with an endpoint address. A
    platform without one (its front end not made yet) is deployed without OpenNebula; one with it is always asked."""
    from .models import PlatformControl

    c = PlatformControl.objects.filter(key=platform.key).first()
    return bool(c is not None and c.endpoint_url)


def _one_or_none(r):
    """T36 B4b: OpenNebula of the run's platform, or None when the platform has no front end yet (Deploy needs none;
    Connect does). A configured front end that does not answer fails the step (commit-137 stays)."""
    if not has_front_end(r.p):
        return None
    return _one(r)


def _one_host_id(one, name):
    el = one.find("one.hostpool.info", "HOST", name)
    return int(el.findtext("ID")) if el is not None else None


def _one_cluster_id(one, name):
    el = one.find("one.clusterpool.info", "CLUSTER", name)
    if el is None:
        raise DeployError(f"cluster {name} is not on the platform")
    return int(el.findtext("ID"))


def s_one_add(r):
    one = _one(r)
    hid = _one_host_id(one, r.h.name)
    cid = _one_cluster_id(one, r.cluster.name)
    if hid is None:
        hid = int(one.call("one.host.allocate", r.h.name, "kvm", "kvm", cid))
    else:  # commit-143: a registration kept from before is used only in this cluster
        have = one.xml("one.host.info", hid).findtext("CLUSTER_ID") or ""
        if have != str(cid):
            raise DeployError(f"OpenNebula has the host (#{hid}) in cluster #{have}, not {r.cluster.name} (#{cid})")
    one.call("one.host.status", hid, 1)  # disabled at once (owner 08:50 "leave it disabled and check frist")
    r.keep(one_id=hid, one_endpoint=client_endpoint(one))  # commit-536: the endpoint of the client that added it
    return f"OpenNebula host #{hid} in cluster #{cid}, disabled"


def our_host(r, what="act on"):
    """commit-539: the one guard of every step that acts on the run's OpenNebula host after one_add (one_wait, enable,
    records, the rollback): (the client, the host id) once not_ours() passes through that same client; DeployError
    otherwise, nothing asked of the host."""
    hid = (r.dep.data or {}).get("one_id")
    if hid is None:
        raise DeployError(f"the run has no OpenNebula host: nothing to {what}")
    one = _one(r)
    if why := not_ours(r, one, hid):
        raise DeployError(why)
    return one, hid


def s_one_wait(r):
    one, hid = our_host(r, "wait for")  # commit-539

    def look():
        h = one.xml("one.host.info", hid)
        return (
            h.findtext("STATE"),
            int(h.findtext("HOST_SHARE/TOTAL_CPU") or 0),
            int(h.findtext("HOST_SHARE/TOTAL_MEM") or 0),
        )

    got = ilo._wait(
        look, lambda x: x[0] == "4" and x[1] > 0 and x[2] > 0, 900, poll=15, sleep=r.sleep, what="OpenNebula monitoring"
    )
    # owner 2026-11-20 09:41: RESERVED_CPU = TOTAL_CPU x (1 - the cluster's vCPU:pCPU ratio) - 128 threads at 10:1 give
    # 12800 - 128000 = -115200, so OpenNebula offers 10 vCPUs per thread
    reserved = reserved_cpu(got[1], r.cluster.vcpu_ratio)
    one.call("one.host.update", hid, f'RESERVED_CPU="{reserved}"', 1)
    have = one.xml("one.host.info", hid).findtext("TEMPLATE/RESERVED_CPU")
    if str(have) != str(reserved):
        raise DeployError(f"RESERVED_CPU is {have!r} in OpenNebula, not {reserved}")
    return (
        f"disabled and monitored: {got[1] // 100} CPU, {got[2] // 1048576} GB; RESERVED_CPU {reserved} "
        f"({r.cluster.vcpu_ratio or 1}:1)"
    )


def reserved_cpu(total_cpu, ratio):
    """OpenNebula's RESERVED_CPU for a host: its TOTAL_CPU (100 per thread) times (1 - vCPUs per pCore)."""
    return int(total_cpu) * (1 - int(ratio or 1))


def s_compare(r):
    from . import profiles

    facts, errors = profiles.read(r.h)
    if errors:
        raise DeployError("; ".join(errors))
    d = profiles.compare(r.prof, facts)
    if d:
        raise DeployError("differs from its profile: " + "; ".join(d))
    r.h.facts, r.h.facts_at, r.h.facts_error = facts, timezone.now(), ""
    r.h.save(update_fields=["facts", "facts_at", "facts_error"])
    return "as its profile"


def s_joined(r):
    """T36 B4b: the end of Deploy - the host in its cluster in the records (planned until Connect), its local storage."""
    from . import hostsfile

    with transaction.atomic():
        hostsfile.lock([r.p])  # commit-541: no /etc/hosts write of the platform renders while it joins
        r.h.state = "planned"
        r.h.save(update_fields=["state"])
        r.cluster.hosts.add(r.h)
        local = r.dep.data.get("local") or []
        if local:
            name = f"{r.h.name}-local"
            st, _c = Storage.objects.get_or_create(
                site=r.site, name=name, defaults={"kind": "local", "host": r.h, "state": "deployed"}
            )
            dev = (r.dep.data.get("local_dev") or "").split()
            size = int(dev[1]) // 10**9 if len(dev) > 1 and dev[1].isdigit() else 0
            StorageVolume.objects.update_or_create(
                storage=st,
                name="datastores",
                defaults={"device": "/dev/vg_one/lv_one", "size_gb": size, "mount": "/var/lib/one/datastores"},
            )
    hosts_note = ""
    from . import hostsfile

    if hostsfile.applies(r.p):  # T36 B4c (rules v2 B6, QB4-3): this host's /etc/hosts, then the platform's others
        try:
            n = hostsfile.write(r.h, r.p, ssh=r.ssh)
        except hostsfile.HostsFileError as exc:
            raise DeployError(f"/etc/hosts: {exc}") from exc
        hosts_note = f"; /etc/hosts: {n} names" + (f" ({m})" if (m := hostsfile.missing_fe(r.p)) else "")
        with transaction.atomic():
            hostsfile.refresh(r.dep.started_by, r.p, f"{r.h.name} joined", exclude=r.h)
    health = refresh_health(r)
    return (
        f"in {r.cluster.name}, planned until Connect"
        + hosts_note
        + (", local storage recorded" if r.dep.data.get("local") else "")
        + f"; health read: {health}"
    )


def s_ready(r):
    """C0: what Connect needs (T36 B4b)."""
    from . import hostservice

    r.h.refresh_from_db()
    r.cluster.refresh_from_db()
    why = connect_problems(  # commit-529/531: the same check as the buttons'
        r.h, r.cluster, running=True, finishing=bool((r.dep.data or {}).get("finishing"))
    )
    if not r.h.ip:
        why.append("no core address (ssh)")
    _u, key, pub, one_pub, one_key = logins(r.p)
    if not (key and pub):
        why.append("the platform's SA secret has no private key")
    if not (one_pub and one_key):
        why.append("the platform's OpenNebula secret has no oneadmin key")
    if not r.h.service_id:  # a host in a cluster before T36 B4a: its service now
        try:
            with transaction.atomic():
                hostservice.assign(r.h, r.p)
        except hostservice.HostServiceError as exc:
            why.append(str(exc))
    if why:
        raise DeployError("; ".join(why))
    r.ssh("true", timeout=60)
    return f"in {r.cluster.name}; front end, secrets and ssh ready; service {r.service_key()}"


def s_enable(r):
    """C4 (owner 2026-11-25 19:25 QB4-2 "Enable when checks pass"): every check passed - the host enabled."""
    one, hid = our_host(r, "enable")  # commit-536/539; commit-530: OpenNebula's answer first, then enabled
    have = one.xml("one.host.info", hid).findtext("CLUSTER_ID") or ""
    cid = _one_cluster_id(one, r.cluster.name)
    if have != str(cid):
        raise DeployError(f"OpenNebula has the host (#{hid}) in cluster #{have}, not {r.cluster.name} (#{cid})")
    with transaction.atomic():  # commit-531: never into a cluster retired meanwhile
        _eligible_now(r)
        one.call("one.host.status", hid, 0)
    got = ilo._wait(
        lambda: one.xml("one.host.info", hid).findtext("STATE"),
        lambda x: x == "2",
        300,
        poll=10,
        sleep=r.sleep,
        what="OpenNebula monitoring after enabling",
    )
    return f"OpenNebula host #{hid} enabled (state {got})"


ENABLED = ("1", "2")  # OpenNebula host STATE: MONITORING_MONITORED, MONITORED (enabled and monitored)


DISABLED = ("4", "6")  # OpenNebula host STATE: DISABLED, MONITORING_DISABLED


def disable_again(r, wait=60):
    """commit-530: a Connect that failed once its host is in OpenNebula - the host disabled again (nothing new placed
    on it), confirmed by OpenNebula within ``wait`` seconds; its enable step stays to be done on Resume. What happened,
    for the log and the step."""
    hid = (r.dep.data or {}).get("one_id")
    if hid is None:
        return "not in OpenNebula"
    try:
        one, hid = our_host(r, "disable")  # commit-536/539: never a status change on a host not proven its own
        one.call("one.host.status", hid, 1)
        state = ilo._wait(
            lambda: one.xml("one.host.info", hid).findtext("STATE"),
            lambda x: x in DISABLED,
            wait,
            poll=5,
            sleep=r.sleep,
            what="OpenNebula disabling the host",
        )
        msg = f"OpenNebula host #{hid} disabled again (state {state})"
    except Exception as exc:  # noqa: BLE001 - said in the log and on the step
        msg = f"OpenNebula host #{hid}: disabling it again is NOT confirmed ({exc}) - disable it by hand"
    if (r.dep.steps.get("enable") or {}).get("status") in ("done", "failed", "running"):
        r.mark("enable", "", f"{msg}; Enable runs again on Resume")
    return msg


def client_endpoint(one):
    """commit-536: the endpoint the RPC client itself talks to (never a second read of the settings)."""
    url = getattr(getattr(one, "client", None), "url", "")
    return url if isinstance(url, str) else ""


def not_ours(r, one, hid):
    """commit-536: why OpenNebula's host #hid on this client may not be this run's (the client talks to another
    endpoint than the one that added it, or the host has another name); "" if it is."""
    saved = (r.dep.data or {}).get("one_endpoint")
    now = client_endpoint(one)
    if not saved:  # commit-534/536: no proof of where #hid was added - never acted on (made before endpoints were kept)
        return f"the run kept no endpoint for #{hid}: reconcile OpenNebula host #{hid} by hand, then Abandon this run"
    if saved != now:
        return f"the platform's front end is {now or 'none'} now, not {saved} where #{hid} was added"
    name = one.xml("one.host.info", hid).findtext("NAME")
    if name != r.h.name:
        return f"OpenNebula host #{hid} is {name!r}, not {r.h.name}"
    return ""


def _eligible_now(r, lock=True):
    """commit-531: the portal's own answer at the moment of a change - the cluster and host read again (locked, so a
    retirement in between waits for this step), and Connect still allowed for them."""
    from .models import Cluster, Host

    q = Cluster.objects.select_for_update() if lock else Cluster.objects
    r.cluster = q.get(pk=r.cluster.pk)
    r.h = (Host.objects.select_for_update() if lock else Host.objects).get(pk=r.h.pk)
    why = connect_problems(r.h, r.cluster, running=True, finishing=bool((r.dep.data or {}).get("finishing")))
    if why:
        raise DeployError("; ".join(why))


def s_records(r):
    # commit-143: OpenNebula's own answer, read now - a resumed run never records a cluster the host has left
    one, hid = our_host(r, "record")  # commit-534/539: never finished on another registration
    info = one.xml("one.host.info", hid)
    have = info.findtext("CLUSTER_ID") or ""
    cid = _one_cluster_id(one, r.cluster.name)
    if have != str(cid):
        raise DeployError(f"OpenNebula has the host (#{hid}) in cluster #{have}, not {r.cluster.name} (#{cid})")
    if info.findtext("STATE") not in ENABLED:  # commit-530: deployed only when OpenNebula says enabled
        raise DeployError(f"OpenNebula host #{hid} is not enabled (state {info.findtext('STATE')})")
    from . import hostsfile

    hosts_note = ""
    if hostsfile.applies(r.p):  # commit-543: the block as the records say now, before the host is in service
        try:
            hosts_note = f"; /etc/hosts: {hostsfile.write(r.h, r.p, ssh=r.ssh)} names"
        except hostsfile.HostsFileError as exc:
            raise DeployError(f"/etc/hosts: {exc}") from exc
    health = refresh_health(r)  # read first: what follows is one transaction
    detail = f"host deployed and enabled in its cluster{hosts_note}; health read: {health}"
    with transaction.atomic():
        _eligible_now(r)  # commit-531: under the cluster's and the host's locks
        r.h.state = "deployed"
        r.h.save(update_fields=["state"])
        r.cluster.hosts.add(r.h)
        # commit-534: the step and the run end with the host's state - never a deployed host with an unfinished run
        r.mark("records", "done", detail)
        HostDeployment.objects.filter(pk=r.dep.pk).update(state=HostDeployment.State.DONE, finished_at=timezone.now())
        if hostsfile.applies(r.p):  # commit-543: once in service, the platform's refresh takes it - a change made
            # between the write above and this commit reaches the host too
            hostsfile.refresh(r.dep.started_by, r.p, f"{r.h.name} connected")
    return detail


def refresh_health(r):
    """Owner 2026-11-20 15:46 "refresh health after deploy": the host's IPMI health read again at the end of the run,
    so its page does not show a reading from before (taken while it had no OS). A failing read is shown, not fatal."""
    from . import hosthealth

    try:
        ok = hosthealth.check([r.h]).get(r.h.name)
    except Exception as exc:  # noqa: BLE001 - the deployment is done; the health page says what the read said
        r.log(f"records: the IPMI health could not be read: {exc}")
        return "not read"
    return "OK" if ok else "problems (see Health)"


def s_cleanup(r):
    from accounts import hostmedia

    if r.dep.data.get("seed"):
        hostmedia.drop_seed(r.site.code, r.dep.data["seed"])
    return "answers ISO deleted"


def s_logging(r):
    """Owner 2026-11-21 07:17 / 07:46 (T33): the host's services - since T34 those of the KVM host profile (NVIDIA when
    its server profile has GPUs; nothing looked for on the host, owner 13:10/13:44) - their rsyslog and logrotate files
    written (staged and checked first), rsyslog restarted; the services kept on the host."""
    from . import hostlogging, logconf

    try:  # the same plan as Apply logging (owner 09:21: applied when a host is deployed)
        found, chosen, content, col = hostlogging.plan(r.h, r.ssh)
        script = logconf.install_script(content)
    except logconf.LogConfError as exc:
        raise DeployError(str(exc)) from exc
    r.ssh(script, timeout=300)
    r.keep(services=found)
    hostlogging.mark_applied(r.h)  # commit-266: its own field
    # owner 10:49: the found services' config changes (settings in their own files), as Apply logging makes them
    cfiles, _then, cerror = hostlogging.make_config(chosen, r.ssh)
    if cerror:
        raise DeployError(cerror)
    result = hostlogging.check(r.h, r.ssh)  # what the host's details show (Logging)
    changed = sum(1 for f in cfiles if f["state"] == "changed")
    return (
        f"{len(found)} services ({', '.join(found)}); {len(content)} files; to {col['host']}:{col['port']}; "
        + (f"{changed} config files changed; " if changed else "")
        + f"{result.get('status')}"
    )


SERVICE_GROUPS = ["function", "platform", "jump", "system", "part"]  # T36: functions first, parts last
STEPS = {k: globals()[f"s_{k}"] for k in STEP_KEYS + CONNECT_KEYS}


# commit-230: the OS baseline and packages of the host alignment (owner 2026-11-20 15:46); 3: commit-237/238;
# 4: the exporters (owner 2026-11-21 07:46)
BASELINE = 4
# commit-237: a redone baseline may need its reboot; commit-238: keys holds oneadmin's umask check
BASELINE_STEPS = ("os_base", "packages", "reboot")  # T36 B4b: keys moved to Connect
FNS_STEPS = ("repos", "packages", "frr")  # commit-524/527: what the host service's functions change (frr)


def frr_again(dep):
    """commit-527: a run whose functions have frr, with steps done that left FRR out (by what each recorded: the
    repositories without FRR's, the packages without frr, the FRR step skipped) - those made again, and a done reboot
    with them (its BGP check). Read from the steps themselves, so any run is repaired on its next Resume."""
    if "frr" not in ((dep.data or {}).get("fns") or []):
        return []

    def done(k):
        return (dep.steps.get(k) or {}).get("status") == "done"

    def said(k):
        return str((dep.steps.get(k) or {}).get("detail") or "")

    left_out = []
    if done("repos") and "FRR" not in said("repos"):
        left_out.append("repos")
    if done("packages") and "frr" not in said("packages").split(", "):
        left_out.append("packages")
    if done("frr") and said("frr").startswith("no frr"):
        left_out.append("frr")
    if left_out and done("reboot"):
        left_out.append("reboot")
    for k in left_out:
        dep.steps[k] = {**dep.steps[k], "status": "", "detail": "done without FRR, which the run needs: again"}
    return left_out


def redo_baseline(r):
    """commit-230: a run whose OS baseline or packages were done by an older version (oneadmin, lldpd, ufw, AppArmor
    not there then) does them again on Resume, before any later step; a run done by this version keeps them."""
    dep = r.dep
    if (dep.data or {}).get("baseline") == BASELINE:
        return
    done = [k for k in BASELINE_STEPS if (dep.steps.get(k) or {}).get("status") == "done"]
    if done:
        for k in done:
            dep.steps[k] = {**dep.steps[k], "status": "", "detail": "done by an older version: run again"}
        HostDeployment.objects.filter(pk=dep.pk).update(steps=dep.steps)
        r.log(f"resume: {', '.join(done)} done by an older version - run again (host alignment)")
    r.keep(baseline=BASELINE)


def run(dep, sleep=time.sleep, claim=None):
    """Every step not done yet, in order; stops at the first that fails (resumed later from it). With a claim
    (commit-139) it runs only while the deployment's claim is that one."""
    if claim is not None:
        with transaction.atomic():
            cur = HostDeployment.objects.select_for_update().get(pk=dep.pk)
            if (cur.data or {}).get("claim") != claim or cur.state != HostDeployment.State.RUNNING:
                return False
        dep = cur
    r = Run(dep, sleep=sleep)
    connect = dep.kind == HostDeployment.Kind.CONNECT  # T36 B4b
    if not connect and (dep.steps.get("checks") or {}).get("status") == "done" and not _done(dep, "install"):
        # commit-142: a resumed run, before any step that powers off or erases: its installer still served
        from accounts import hostmedia

        ok, why = hostmedia.on_site_node(r.site.code, (dep.data or {}).get("installer") or None)
        if not ok or not (dep.data or {}).get("installer"):
            r.log(f"resume refused: {why or 'no installer was chosen for this run'}")
            HostDeployment.objects.filter(pk=dep.pk, state=HostDeployment.State.RUNNING).update(
                state=HostDeployment.State.FAILED, finished_at=timezone.now()
            )
            return False
    if not connect and (dep.steps.get("checks") or {}).get("status") == "done" and not _done(dep, "install"):
        from . import logconf

        try:  # commit-229: a resumed run, before anything is wiped: the firewall's inputs as B0 checks them now
            firewall_rules(r)
            try:  # commit-259: and the site's log collector, as B0 checks it
                logconf.collector(r.site)
            except logconf.LogConfError as exc:
                raise DeployError(str(exc)) from exc
        except DeployError as exc:
            r.log(f"resume refused: {exc}")
            HostDeployment.objects.filter(pk=dep.pk, state=HostDeployment.State.RUNNING).update(
                state=HostDeployment.State.FAILED, finished_at=timezone.now()
            )
            return False
    if connect:  # commit-531: every attempt (a Resume too) checks again that Connect is still allowed
        try:
            _eligible_now(r, lock=False)
        except DeployError as exc:
            owned = (dep.data or {}).get("one_id") is not None and not _done(dep, "records")
            if owned:  # commit-537: the marker first, before anything else is written
                r.keep(rollback="pending")
            r.log(f"connect refused: {exc}")
            if owned:
                # commit-533: the run's host is in OpenNebula and may be enabled (an Enable cut off before it was
                # recorded): disabled again and confirmed - or the manual action said - before the attempt ends.
                # commit-537: pending until its outcome is saved, the run still running: a restart here leaves a
                # run the reaper fails with the rollback pending, and Resume does it again whatever the cluster is
                from .models import Host

                msg = disable_again(r)
                r.log(f"connect refused: {msg}")
                r.keep(rollback=msg)
                # commit-538: its own host, deployed by an older run cut off in Records, too - disabled (or not
                # confirmed) it is no longer as defined; a retired host stays retired
                Host.objects.filter(pk=dep.host_id, state__in=["planned", "non-compliant", "deployed"]).update(
                    state="non-compliant"
                )
            HostDeployment.objects.filter(pk=dep.pk, state=HostDeployment.State.RUNNING).update(
                state=HostDeployment.State.FAILED, finished_at=timezone.now()
            )
            return False
        if (dep.data or {}).get("rollback") == "pending":  # allowed again: the steps go on (Enable runs again)
            r.keep(rollback="")
    if not connect:
        redo_baseline(r)
    if not connect and _done(dep, "ilo") and (dep.data or {}).get("lldp") != 1:
        # commit-232: a run whose iLO step was done by an older version - LLDP only, set and read back
        try:  # commit-239: an unreadable iLO fails the run, never leaves it running
            bad = ilo.ensure_lldp(r.c)
        except Exception as exc:  # noqa: BLE001
            bad = f"LLDP not set: {exc}"
        if bad:
            r.log(f"resume refused: iLO {bad}")
            HostDeployment.objects.filter(pk=dep.pk, state=HostDeployment.State.RUNNING).update(
                state=HostDeployment.State.FAILED, finished_at=timezone.now()
            )
            return False
        r.keep(lldp=1)
    for key in CONNECT_KEYS if connect else STEP_KEYS:
        if (dep.steps.get(key) or {}).get("status") == "done":
            continue
        if claim is not None and not _still_mine(dep, claim):  # commit-139: another attempt took the run over
            r.log(f"{key}: not started - this run was taken over")
            return False
        r.mark(key, "running")
        r.log(f"{key}: started")
        try:
            detail = STEPS[key](r) or ""
        except Exception as exc:  # noqa: BLE001 - the run stops here and says why
            r.mark(key, "failed", exc)
            r.log(f"{key}: FAILED: {getattr(exc, 'detail', '') or exc}")  # commit-169: the whole reason in the log
            if connect and _done(dep, "one_add"):  # QB4-2: in OpenNebula, a check failed - disabled, non-compliant
                from .models import Host

                r.keep(rollback="pending")  # commit-537: until its outcome is saved, before the run ends
                msg = disable_again(r)  # commit-530
                r.log(f"{key}: {msg}")
                r.keep(rollback=msg)
                Host.objects.filter(pk=dep.host_id).update(state="non-compliant")
            HostDeployment.objects.filter(pk=dep.pk).update(
                state=HostDeployment.State.FAILED, finished_at=timezone.now()
            )
            audit(
                dep.started_by,
                "host_connect_failed" if connect else "host_deploy_failed",
                dep.host,
                host=dep.host.name,
                step=key,
                error=str(exc)[:300],
            )
            return False
        r.mark(key, "done", detail)
        r.log(f"{key}: {detail}")
        if key in ("checks", "power_on", "install", "one_add"):
            audit(dep.started_by, "host_deploy_phase", dep.host, host=dep.host.name, step=key)
    HostDeployment.objects.filter(pk=dep.pk).update(state=HostDeployment.State.DONE, finished_at=timezone.now())
    audit(
        dep.started_by,
        "host_connected" if connect else "host_deployed",
        dep.host,
        host=dep.host.name,
        cluster=dep.cluster.name,
    )
    return True


def _done(dep, key):
    return (dep.steps.get(key) or {}).get("status") == "done"


def _still_mine(dep, claim):
    cur = HostDeployment.objects.filter(pk=dep.pk).values("state", "data").first() or {}
    return cur.get("state") == HostDeployment.State.RUNNING and (cur.get("data") or {}).get("claim") == claim


CLAIM_GRACE = 120  # seconds: a run claimed this recently whose task is not listed yet is still starting


def _claim_fresh(dep):
    from django.utils.dateparse import parse_datetime

    at = parse_datetime((dep.data or {}).get("claim_at") or "")
    return at is not None and (timezone.now() - at).total_seconds() < CLAIM_GRACE


def mark_interrupted(dep, claim=None):
    """commit-140: a run whose worker ended without recording its end becomes a failed run that Resume continues; the
    step that was running is marked so (it runs again from its start, every step being safe to repeat). commit-139:
    only while it is still running, with the claim read (when given). Returns True when it was marked."""
    steps = dict(dep.steps or {})
    for k, v in steps.items():
        if (v or {}).get("status") == "running":
            steps[k] = {**v, "status": "failed", "detail": "interrupted: the portal's process ended (a restart)"}
    q = HostDeployment.objects.filter(pk=dep.pk, state=HostDeployment.State.RUNNING)
    n = q.update(state=HostDeployment.State.FAILED, steps=steps, finished_at=timezone.now()) if claim is None else 0
    if claim is not None:
        cur = q.select_for_update().first()
        if cur is not None and (cur.data or {}).get("claim") == claim:
            n = q.update(state=HostDeployment.State.FAILED, steps=steps, finished_at=timezone.now())
    if n:
        dep.state, dep.steps = HostDeployment.State.FAILED, steps
    return bool(n)


def reap_interrupted():
    """For tasks.reap: every running deployment with no live task (and no fresh claim) is failed, resumable.
    commit-139: decided and written under the host's row lock - the lock start() takes to claim a run - on the row as
    it is then, and only for the claim seen."""
    from accounts import tasks

    from .models import Host

    n = 0
    for pk, host_id in HostDeployment.objects.filter(state=HostDeployment.State.RUNNING).values_list("pk", "host_id"):
        with transaction.atomic():
            Host.objects.select_for_update().filter(pk=host_id).first()
            dep = (
                HostDeployment.objects.select_for_update()
                .select_related("host")
                .filter(pk=pk, state=HostDeployment.State.RUNNING)
                .first()
            )
            if dep is None or tasks.running("host deploy", dep.host) or _claim_fresh(dep):
                continue
            if mark_interrupted(dep, claim=(dep.data or {}).get("claim")):
                n += 1
    return n


def start(user, h, cluster, reset_factory=False, power_confirmed=False):
    """A new run, or the failed (or interrupted) one resumed - its steps done stay done. commit-139: the host's row is
    locked while the run is claimed (a token on it); the worker starts only after the commit and runs only while the
    claim is its own. Returns the deployment."""
    import secrets as _secrets

    from accounts import ha, tasks

    from . import hostlogging
    from .models import Host

    ha.forbid_writes(f"{h.name} deployment")
    claim = _secrets.token_hex(8)
    with transaction.atomic():
        row = Host.objects.select_for_update().get(pk=h.pk)  # one request at a time per host
        if hostlogging.claim_fresh(row.logging_claim):  # commit-263: Apply logging holds the host
            raise DeployError("Apply logging of this host is running: try again when it ends")
        if tasks.running("host deploy", h):  # core-052: a worker still at work, whatever its run's row says
            raise DeployError("a run of this host (deployment or retirement) is running")
        live = HostDeployment.objects.select_for_update().filter(host=h, state=HostDeployment.State.RUNNING).first()
        if live is not None:
            if _claim_fresh(live):
                raise DeployError("a run of this host (deployment or retirement) is running")
            mark_interrupted(live)  # commit-140: resumable, never abandoned for a new one
        try:  # T36 B4a: the host service it is made from, kept on the host
            from . import hostservice

            h.service = hostservice.assign(row, cluster.platform)
        except hostservice.HostServiceError as exc:
            raise DeployError(str(exc)) from exc
        dep = HostDeployment.objects.filter(
            host=h, state=HostDeployment.State.FAILED, cluster=cluster, kind=HostDeployment.Kind.DEPLOY
        ).first()
        if dep is None:
            HostDeployment.objects.filter(host=h, state=HostDeployment.State.FAILED).update(
                state=HostDeployment.State.ABANDONED, finished_at=timezone.now()
            )
            dep = HostDeployment.objects.create(
                host=h, cluster=cluster, reset_factory=reset_factory, power_confirmed=power_confirmed, started_by=user
            )
        dep.state, dep.finished_at = HostDeployment.State.RUNNING, None
        dep.power_confirmed = dep.power_confirmed or power_confirmed
        dep.data = {**(dep.data or {}), "claim": claim, "claim_at": timezone.now().isoformat()}
        if "fns" not in dep.data:  # commit-524: the run's functions, kept for every step and every resume
            dep.data["fns"] = h.service.functions()
            redo = [k for k in FNS_STEPS if (dep.steps.get(k) or {}).get("status") == "done"]
            if "frr" in dep.data["fns"] and (dep.steps.get("reboot") or {}).get("status") == "done":
                redo.append("reboot")  # commit-527: and its BGP check after the reboot
            for k in redo:  # a run begun before: its repositories and packages made again with this set
                dep.steps[k] = {**dep.steps[k], "status": "", "detail": "done before the run kept its functions: again"}
        frr_again(dep)
        dep.save(update_fields=["state", "finished_at", "power_confirmed", "data", "steps"])
        resumed = bool(dep.steps)

        def go():
            tasks.start(
                "host deploy",
                f"Deploy {h.name} into {cluster.name}",
                lambda t: {"ok": run(dep, claim=claim)},
                user=user,
                target=h,
            )

        transaction.on_commit(go)
    audit(
        user, "host_deploy_started", h, host=h.name, cluster=cluster.name, reset_factory=reset_factory, resumed=resumed
    )
    return dep


def abandon(user, h):
    """The host's failed run given up: its answers ISO deleted; the host stays as it is. core-052: under the host's
    lock, as Resume, and only while the run is still failed and no worker of the host is at work. Returns the run
    abandoned, or None."""
    from accounts import hostmedia, tasks

    from .models import Host

    with transaction.atomic():
        Host.objects.select_for_update().get(pk=h.pk)
        if tasks.running("host deploy", h):
            raise DeployError("a run of this host (deployment or retirement) is running: nothing abandoned")
        dep = HostDeployment.objects.select_for_update().filter(host=h, state=HostDeployment.State.FAILED).first()
        if dep is None:
            return None
        HostDeployment.objects.filter(pk=dep.pk, state=HostDeployment.State.FAILED).update(
            state=HostDeployment.State.ABANDONED, finished_at=timezone.now()
        )
    if dep.data.get("seed"):
        try:
            hostmedia.drop_seed(dep.host.site.code, dep.data["seed"])
        except Exception:  # noqa: BLE001 - the node may be away; the name is random
            pass
    audit(
        user,
        {
            HostDeployment.Kind.DEPLOY: "host_deploy_abandoned",
            HostDeployment.Kind.CONNECT: "host_connect_abandoned",
        }.get(dep.kind, "host_retire_abandoned"),
        h,
        host=h.name,
    )
    return dep


def steps_view(dep):
    """[(phase, key, label, status, at, detail)] for the host's page."""
    out = []
    for phase, key, label in CONNECT_PHASES if dep.kind == HostDeployment.Kind.CONNECT else PHASES:
        s = (dep.steps or {}).get(key) or {}
        out.append((phase, key, label, s.get("status", ""), s.get("at", ""), s.get("detail", "")))
    return out


_ASK = object()  # commit-535: "read it now" (a page passes what it already loaded)


def endpoint_of(platform):
    """The platform's OpenNebula endpoint address, or "" (commit-528, commit-536)."""
    from .models import PlatformControl

    c = PlatformControl.objects.filter(key=platform.key).first()
    return c.endpoint_url if c is not None else ""


def connect_problems(h, cluster, running=False, finishing=False, last=_ASK, members=None, fe=None):
    """What keeps Connect from starting (T36 B4b; commit-529, commit-531): the same check for the host's button, the
    cluster's, start_connect and the run's C0. ``running``: asked by the run itself (its host may be non-compliant
    from its own failed try). ``finishing`` (commit-534): a failed Connect left with its host already deployed (cut
    off before its end was recorded) - Resume only finishes it, so a deployed host is allowed for it."""
    out = []
    if cluster.state == "retired":
        out.append(f"cluster {cluster.name} is retired")
    if h.state not in ("planned", "non-compliant") + (("deployed",) if finishing else ()):
        out.append(f"{h.name} is {h.state}: only a planned or non-compliant host is connected")
    # commit-535: ``members`` (the host's cluster ids), ``fe`` (has a front end) and ``last`` (its latest Deploy) may
    # come from the page's own reads; start_connect and the run read them now
    member = cluster.pk in members if members is not None else cluster.hosts.filter(pk=h.pk).exists()
    if not member:
        out.append(f"{h.name} is not in {cluster.name}: deploy it first")
    if not (fe if fe is not None else has_front_end(cluster.platform)):
        out.append(f"{cluster.platform.key} has no front end endpoint yet")
    # commit-529: proof of an installation - its latest deployment is a finished Deploy into this cluster; a host with
    # none since the split that is non-compliant was installed before (Connect is its check)
    if last is _ASK:
        last = latest_deploy(h)
    if last is not None:
        if last.state != HostDeployment.State.DONE:
            out.append(f"its deployment is {last.get_state_display()}: deploy it (or Resume) first")
        elif last.cluster_id != cluster.pk:
            out.append(f"it was deployed into {last.cluster.name}, not {cluster.name}")
    elif h.state != "non-compliant":
        out.append(f"{h.name} was never deployed: deploy it first")
    return out


def latest_deploy(h):
    return HostDeployment.objects.filter(host=h, kind=HostDeployment.Kind.DEPLOY).order_by("-started_at", "-pk").first()


def connect_target(h, last=_ASK, clusters=None):
    """commit-532: the cluster a host's Connect goes to - the one for the host's button and for its POST: the cluster of
    its latest finished Deploy, else (a host installed before) its one cluster that is not retired; None otherwise.
    commit-535: ``last`` and ``clusters`` (the host's clusters) may come from the page's own reads."""
    if last is _ASK:
        last = latest_deploy(h)
    if clusters is None:
        clusters = list(h.clusters.select_related("platform"))
    if last is not None and last.state == HostDeployment.State.DONE:
        c = next((x for x in clusters if x.pk == last.cluster_id), None)
        return c if c is not None and c.state != "retired" else None
    live = [c for c in clusters if c.state != "retired"]
    return live[0] if len(live) == 1 else None


def start_connect(user, h, cluster):
    """T36 B4b: a new Connect of the host into its cluster, or its failed one resumed. One run of a host at a time, of
    any kind (the same task name and claim as a deployment)."""
    import secrets as _secrets

    from accounts import ha, tasks

    from . import hostlogging
    from .models import Host

    ha.forbid_writes(f"{h.name} connect")
    claim = _secrets.token_hex(8)
    with transaction.atomic():
        row = Host.objects.select_for_update().get(pk=h.pk)
        from .models import Cluster

        cluster = Cluster.objects.select_related("platform").get(pk=cluster.pk)  # as it is now (commit-531)
        if hostlogging.claim_fresh(row.logging_claim):
            raise DeployError("Apply logging of this host is running: try again when it ends")
        if tasks.running("host deploy", h):
            raise DeployError("a run of this host is running")
        live = HostDeployment.objects.select_for_update().filter(host=h, state=HostDeployment.State.RUNNING).first()
        if live is not None:
            if _claim_fresh(live):
                raise DeployError("a run of this host is running")
            mark_interrupted(live)
        dep = HostDeployment.objects.filter(
            host=h, state=HostDeployment.State.FAILED, cluster=cluster, kind=HostDeployment.Kind.CONNECT
        ).first()
        # commit-534: a failed Connect whose host is already deployed, its records not ended - Resume finishes it
        finishing = bool(
            dep is not None
            and row.state == "deployed"
            and (dep.data or {}).get("one_id") is not None
            and not _done(dep, "records")
        )
        # commit-537: a run whose rollback is pending (cut off by a restart) is resumed to do it, whatever the host
        # and the cluster are now - its first act checks again and, refused, disables its host
        # commit-537: also when the marker was never written - any unfinished Connect with a host in OpenNebula
        cleanup = dep is not None and (
            (dep.data or {}).get("rollback") == "pending"
            or ((dep.data or {}).get("one_id") is not None and not _done(dep, "records"))
        )
        why = [] if cleanup else connect_problems(row, cluster, finishing=finishing)
        if why:
            raise DeployError("; ".join(why))
        if dep is None:
            HostDeployment.objects.filter(
                host=h, state=HostDeployment.State.FAILED, kind=HostDeployment.Kind.CONNECT
            ).update(state=HostDeployment.State.ABANDONED, finished_at=timezone.now())
            dep = HostDeployment.objects.create(
                host=h, cluster=cluster, kind=HostDeployment.Kind.CONNECT, started_by=user
            )
        dep.state, dep.finished_at = HostDeployment.State.RUNNING, None
        dep.data = {**(dep.data or {}), "claim": claim, "claim_at": timezone.now().isoformat()}
        if finishing:
            dep.data["finishing"] = True
        dep.save(update_fields=["state", "finished_at", "data"])
        resumed = bool(dep.steps)

        def go():
            tasks.start(
                "host deploy",
                f"Connect {h.name} to {cluster.name}",
                lambda t: {"ok": run(dep, claim=claim)},
                user=user,
                target=h,
            )

        transaction.on_commit(go)
    audit(user, "host_connect_started", h, host=h.name, cluster=cluster.name, resumed=resumed)
    return dep
