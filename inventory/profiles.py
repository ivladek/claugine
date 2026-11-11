"""Server profiles: what a server model is, how a host reports its hardware, and whether they match.

Reading the hardware of a host (``Host.facts``) asks its IPMI first (Redfish on the iLO): chassis, CPU, RAM, GPUs,
the boot volume and the network cards with their PCI slots, models, ports and speeds; the names of the components
come from there. The Linux names of the ports are predicted from the slots (systemd: ens<slot>f<function>np<port>,
enP<domain>s<slot>… for a card in PCI domain 1). The OS of the host is then read over ssh for what the IPMI does not
know: the boot device and its volumes, and the aliases the ports carry (nic-data1 = ens1f0np0). A host in no cluster
may be unreachable over ssh; that is no error, its IPMI facts are enough.

Hosts of one profile must be the same: the check compares every host with its profile. A host added without a
profile gets the one that fits; a wrong choice is corrected; when none fits, a new profile is made from the host.

Networks of a profile: the ports get aliases, and the aliases make the bonds — two fabrics: bond0 = nic-data1 +
nic-data2 (data: VMs, host to host), bond1 = nic-storage1 + nic-storage2 (storage); one fabric: bond0 = nic-data1 +
nic-data2 carries both. A profile can be edited until it is verified; then only its volumes can change, and the
assignment of the aliases while no active host of a cluster uses the profile.
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from accounts.models import Role, audit
from accounts.utils import staff_required

from .models import ALIASES, ServerProfile

# --- reading the hardware --------------------------------------------------------------------------------

FACTS_SCRIPT = r'''
import glob, json, os, re, subprocess
def rd(p):
    try:
        return open(p).read().strip()
    except OSError:
        return ""
def run(*a):
    try:
        return subprocess.run(a, capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return ""
out = {"vendor": rd("/sys/class/dmi/id/sys_vendor"), "chassis": rd("/sys/class/dmi/id/product_name")}
cpu = {}
for line in run("lscpu").splitlines():
    k, _, v = line.partition(":")
    cpu[k.strip()] = v.strip()
out["cpu_vendor"] = {"AuthenticAMD": "AMD", "GenuineIntel": "Intel"}.get(cpu.get("Vendor ID", ""), cpu.get("Vendor ID", ""))
out["cpu_name"] = cpu.get("Model name", "")
out["sockets"] = int(cpu.get("Socket(s)", "0") or 0)
out["cores_per_socket"] = int(cpu.get("Core(s) per socket", "0") or 0)
try:
    out["cpu_mhz"] = int(float(cpu.get("CPU max MHz", "0") or 0))
except ValueError:
    out["cpu_mhz"] = 0
m = re.search(r"MemTotal:\s+(\d+)", rd("/proc/meminfo"))
out["ram_os_gb"] = round(int(m.group(1)) / 1048576) if m else 0
blk = {}
try:
    blk = json.loads(run("lsblk", "-J", "-b", "-o", "NAME,PATH,SIZE,TYPE,FSTYPE,MOUNTPOINT,PKNAME") or "{}")
except ValueError:
    pass
def walk(nodes, disk=None):
    for n in nodes or []:
        yield n, disk or n
        yield from walk(n.get("children"), disk or n)
root = None
for n, disk in walk(blk.get("blockdevices")):
    if n.get("mountpoint") == "/":
        root = disk
if root:
    vols = []
    for n, disk in walk([root]):
        if n.get("mountpoint") and n.get("mountpoint").startswith("/") and n.get("type") in ("part", "lvm"):
            vols.append({"path": n["mountpoint"], "size_gb": round(int(n.get("size") or 0) / 1073741824), "fs": n.get("fstype") or ""})
    out["boot"] = {"dev": root.get("path") or "/dev/" + root.get("name", ""), "size_gb": round(int(root.get("size") or 0) / 1000000000),
                   "volumes": sorted(vols, key=lambda v: v["path"])}
cards = {}
for p in sorted(glob.glob("/sys/class/net/*/device")):
    name = p.split("/")[4]
    addr = os.path.basename(os.path.realpath(p))
    if not re.match(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.\d$", addr):
        continue
    key = addr.rsplit(".", 1)[0]
    c = cards.setdefault(key, {"pci": key, "links": [], "speed": 0})
    c["links"].append(name)
    c.setdefault("fns", {})[name] = int(addr.rsplit(".", 1)[1])
    try:
        c["speed"] = max(c["speed"], int(rd(f"/sys/class/net/{name}/speed") or 0))
    except ValueError:
        pass
    if "model" not in c:
        info = {}
        for line in run("lspci", "-vmm", "-s", addr).splitlines():
            k, _, v = line.partition(":")
            info[k.strip()] = v.strip()
        c["vendor"], c["model"] = info.get("Vendor", ""), info.get("Device", "")
        c["slot"] = info.get("PhySlot", "") or key
        c["driver"] = os.path.basename(os.path.realpath(f"/sys/class/net/{name}/device/driver"))
        port = re.search(r"Port:\s*(.+)", run("ethtool", name))
        c["transceiver"] = port.group(1).strip() if port else ""
out["nics"] = {"cards": list(cards.values())}
gpus = [l for l in run("lspci", "-mm").splitlines() if re.search(r'"(3D|VGA compatible) controller"', l) and "NVIDIA" in l]
out["gpu_count_os"] = len(gpus)
print(json.dumps(out))
'''


def _card_name(links):
    """ens1f0np0, ens1f1np1 -> ens1 (the card's name as the profiles have it)."""
    first = sorted(links)[0] if links else ""
    m = re.match(r"^(.*?)(f\d+)?(np\d+)?$", first)
    return (m.group(1) if m else first) or first


def _os_card(c):
    """A card as the OS reports it, with the predicted Linux names and the aliases its ports carry."""
    from .redfish import is_mellanox, linux_names, slot_base
    links = c.get("links") or []
    ports = len(links)
    slot = str(c.get("slot") or "")
    try:
        domain = int(str(c.get("pci") or "0").split(":")[0], 16)
    except ValueError:
        domain = 0
    mlx = str(c.get("driver") or "").startswith("mlx") or is_mellanox(c.get("vendor"))
    base = slot_base(slot, domain) if slot.isdigit() else _card_name(links)
    names = linux_names(base, ports, mlx)
    fns = c.get("fns") or {}
    aliases = {}
    for x in links:
        fn = fns.get(x, links.index(x))
        if x not in names and fn < len(names):
            aliases[x] = names[fn]
    speed = c.get("speed") or 0
    return {"slot": slot, "pci": c.get("pci") or "", "name": base, "vendor": c.get("vendor") or "", "chip": c.get("model") or "",
            "ports": ports, "speed": int(speed / 1000) if speed >= 1000 else speed, "transceiver": c.get("transceiver") or "",
            "driver": c.get("driver") or "", "links": names, "aliases": aliases}


def _os_facts(h):
    from . import sshexec
    from .credentials import account
    from .models import Cluster
    ref = next((c.platform.sa for c in Cluster.objects.filter(hosts=h).select_related("platform") if c.platform.sa), "")
    ref = ref or sshexec.default_account(h.site.code)
    user, acc = account(ref)
    if not user or not acc.get("key"):
        raise sshexec.SshError(f"no account with an ssh key ({ref})")
    ports = [22]  # hosts use the standard port; only the jump hosts have their own (site settings)
    last = None
    for port in ports:
        try:
            out = sshexec.run(h.ip, port, user, acc["key"], "python3 -", site=h.site.code, stdin=FACTS_SCRIPT, timeout=60)
            data = json.loads(out.strip().splitlines()[-1])
            data["nics"] = {"cards": [_os_card(c) for c in data.get("nics", {}).get("cards", [])]}
            return data
        except (sshexec.SshError, ValueError, IndexError) as exc:
            last = exc
    raise sshexec.SshError(str(last))


def _ipmi_facts(h):
    from . import redfish
    if not h.ipmi_host:
        return {}
    r = redfish.for_hosts([h], lambda c, _h: c.hardware(), workers=1).get(h.name) or {}
    if "error" in r:
        raise redfish.RedfishError(r["error"])
    cpu_vendor = str(r.get("cpu_vendor") or "")
    low = cpu_vendor.lower()  # the iLOs say "Advanced Micro Devices, Inc." and "Intel(R) Corporation"
    cpu_vendor = "AMD" if ("amd" in low or "advanced micro" in low) else ("Intel" if "intel" in low else cpu_vendor)
    out = {"vendor": r.get("vendor", ""), "chassis": r.get("model", ""), "serial": r.get("serial", ""),
           "ram_gb": r.get("ram_gb") or 0,
           "gpu_model": r.get("gpu_model", ""), "gpu_count": r.get("gpu_quantity") or 0,
           "cpu_vendor": cpu_vendor, "cpu_name": redfish.cpu_label(r.get("cpu_model", "")),
           "sockets": r.get("sockets") or 0, "cores_per_socket": r.get("cores") or 0, "ipmi_read": True}
    if r.get("boot"):
        out["boot"] = dict(r["boot"])
    if r.get("cards"):
        out["nics"] = {"cards": [dict(c, source="ipmi") for c in r["cards"]]}
    return {k: v for k, v in out.items() if v not in ("", None)}


def collect(h):
    """Read the host's hardware (OS over ssh, then IPMI) into h.facts. Returns the errors (empty when all answered)."""
    facts, errors = read(h)
    if facts:
        h.facts, h.facts_at = facts, timezone.now()
    h.facts_error = "; ".join(errors)[:300] if errors else ""
    h.save(update_fields=["facts", "facts_at", "facts_error"])
    return errors


def _merge_cards(ipmi, os_cards):
    """The IPMI's cards (names, models, slots) with what the OS adds: the real interface names, the aliases, the
    transceiver and the driver. Cards are paired by PCI slot; the OS's cards the IPMI did not list are added."""
    out = [dict(c) for c in ipmi]
    by_slot = {c.get("slot"): c for c in out if c.get("slot")}
    for oc in os_cards:
        c = by_slot.get(oc.get("slot")) if oc.get("slot") else None
        if c is None:
            out.append(dict(oc))
            continue
        for k in ("pci", "driver", "chip", "aliases"):
            if oc.get(k):
                c[k] = oc[k]
        for k in ("transceiver", "speed"):
            if not c.get(k) and oc.get(k):
                c[k] = oc[k]
        if oc.get("links") and len(oc["links"]) == int(c.get("ports") or 0):
            c["name"], c["links"] = oc["name"], oc["links"]  # the OS knows the PCI domain for sure
    return out


def _in_cluster(h):
    from .models import Cluster
    return Cluster.objects.filter(hosts=h).exists()


def read(h):
    """(facts, errors) of a host, read now; nothing is saved. The IPMI answers first and names the components; the OS
    adds what only it knows. A host in no cluster that does not answer over ssh is no error."""
    facts, errors = {}, []
    try:
        facts.update(_ipmi_facts(h))
    except Exception as exc:  # noqa: BLE001 - shown on the host
        errors.append(f"IPMI: {exc}"[:200])
    try:
        got = _os_facts(h)
    except Exception as exc:  # noqa: BLE001
        got = None
        if _in_cluster(h) or not facts:
            errors.append(f"ssh: {exc}"[:200])
        else:
            facts["os_unread"] = str(exc)[:200]
    if got:
        ipmi_cards = (facts.get("nics") or {}).get("cards") or []
        for k, v in got.items():
            if k == "nics":
                facts["nics"] = {"cards": _merge_cards(ipmi_cards, (v or {}).get("cards") or [])}
            elif k == "boot":
                facts["boot"] = {**(facts.get("boot") or {}), **{x: y for x, y in (v or {}).items() if y}}
            elif k in ("vendor", "chassis", "cpu_vendor", "cpu_name") and facts.get(k):
                facts[k + "_os"] = v  # the IPMI's names stand
            elif not facts.get(k):
                facts[k] = v
        facts["os_read"] = True
    if facts and not facts.get("ram_gb"):
        facts["ram_gb"] = facts.get("ram_os_gb") or 0
    if facts and (facts.get("gpu_count") is None or (not facts.get("gpu_count") and facts.get("gpu_count_os"))):
        facts["gpu_count"] = facts.get("gpu_count_os") or 0
    return facts, errors


# --- comparing --------------------------------------------------------------------------------------------

def _n(v):
    return re.sub(r"\s+", " ", str(v or "")).strip().lower()


def _cpu(v):
    """A CPU name without the marks and words the OS and the iLO spell differently: (R), (TM), Intel, AMD, CPU, @…"""
    v = re.sub(r"\((r|tm)\)|\b(intel|amd|cpu|processor)\b|@.*$", " ", _n(v))
    return re.sub(r"\s+", " ", v).strip()


def _near(a, b, pct=6):
    a, b = int(a or 0), int(b or 0)
    return abs(a - b) <= max(2, max(a, b) * pct / 100)


def compare(p, f):
    """Differences between a profile and a host's facts: chassis, CPU, cores, RAM, GPUs, boot disk, network cards."""
    if not p or not f:
        return []
    d = []
    ch = lambda x: re.sub(r"\s+", " ", re.sub(r"\b(hpe|hp|compute)\b", " ", _n(x))).strip()  # noqa: E731
    if f.get("chassis") and p.chassis and ch(p.chassis) not in ch(f["chassis"]) and ch(f["chassis"]) not in ch(p.chassis):
        d.append(f"chassis {f['chassis']} ≠ {p.chassis}")
    if f.get("cpu_name") and p.cpu_name and _cpu(p.cpu_name) not in _cpu(f["cpu_name"]):
        d.append(f"CPU {f['cpu_name']} ≠ {p.cpu_name}")
    if f.get("sockets") and int(f["sockets"]) != int(p.sockets or 0):
        d.append(f"sockets {f['sockets']} ≠ {p.sockets}")
    if f.get("cores_per_socket") and int(f["cores_per_socket"]) != int(p.cores_per_socket or 0):
        d.append(f"cores per socket {f['cores_per_socket']} ≠ {p.cores_per_socket}")
    if f.get("ram_gb") and not _near(f["ram_gb"], p.ram_gb):
        d.append(f"RAM {f['ram_gb']} GB ≠ {p.ram_gb} GB")
    if f.get("gpu_count") is not None and int(f.get("gpu_count") or 0) != int(p.gpu_count or 0):
        d.append(f"GPUs {f.get('gpu_count') or 0} ≠ {p.gpu_count}")
    fb, pb = f.get("boot") or {}, p.boot or {}
    if fb.get("size_gb") and pb.get("size_gb") and not _near(fb["size_gb"], pb["size_gb"]):
        d.append(f"boot disk {fb['size_gb']} GB ≠ {pb['size_gb']} GB")
    d += _card_diffs(p.cards, (f.get("nics") or {}).get("cards") or [])
    have = {a: port for c in (f.get("nics") or {}).get("cards") or [] for a, port in (c.get("aliases") or {}).items()}
    for a, port in sorted(p.assign.items()):
        if a in have and have[a] != port:
            d.append(f"{a} is {have[a]} ≠ {port}")
    return d


def _card_key(c):
    return ("slot", str(c["slot"])) if c.get("slot") else ("name", c.get("name") or _card_name(c.get("links") or []))


def _card_diffs(pcards, fcards):
    """Cards are paired by PCI slot (by Linux name when a slot is not known)."""
    d = []
    fc = {_card_key(c): c for c in fcards}
    pc = {_card_key(c): c for c in pcards if c.get("slot") or c.get("name")}
    if not (fc and pc):
        return d
    if any(k[0] == "name" for k in pc) or any(k[0] == "name" for k in fc):  # a side without slots: pair by name
        fc = {("name", c.get("name") or _card_name(c.get("links") or [])): c for c in fcards}
        pc = {("name", c.get("name")): c for c in pcards if c.get("name")}
    label = lambda k, c: (f"slot {k[1]}" if k[0] == "slot" else k[1]) + (f" ({c['name']})" if k[0] == "slot" and c.get("name") else "")  # noqa: E731
    for k in sorted(set(pc) - set(fc)):
        d.append(f"card {label(k, pc[k])} missing")
    for k in sorted(set(fc) - set(pc)):
        d.append(f"card {label(k, fc[k])} not in the profile")
    for k in sorted(set(fc) & set(pc)):
        a, b = fc[k], pc[k]
        name = label(k, b)
        ports_a = int(a.get("ports") or len(a.get("links") or []))
        if b.get("ports") and ports_a and ports_a != int(b["ports"]):
            d.append(f"card {name}: {ports_a} ports ≠ {b['ports']}")
        if a.get("speed") and b.get("speed") and int(a["speed"]) != int(b["speed"]):
            d.append(f"card {name}: speed {a['speed']} ≠ {b['speed']} Gb/s")
        if a.get("model") and b.get("model") and not _same_model(a["model"], b["model"]):
            d.append(f"card {name}: model {a['model']} ≠ {b['model']}")
        if a.get("transceiver") and b.get("transceiver") and _n(a["transceiver"]) not in _n(b["transceiver"]) \
                and _n(b["transceiver"]) not in _n(a["transceiver"]):
            d.append(f"card {name}: transceiver {a['transceiver']} ≠ {b['transceiver']}")
    return d


def _same_model(a, b):
    """Models are the same when they share a part number (MCX631102AS-ADAT) or, without one, a significant word."""
    parts = lambda x: set(re.findall(r"\b[A-Z]{2,}\d{3,}[A-Z0-9-]*\b", str(x).upper()))  # noqa: E731
    pa, pb = parts(a), parts(b)
    if pa and pb:
        return bool(pa & pb)
    return bool(set(_n(a).split()) & set(_n(b).split()) - {"ethernet", "adapter", "controller", "network"})


def verify_problems(p):
    """What keeps a profile from being verified."""
    out = []
    if not p.cards:
        out.append(_("no network cards"))
    if not p.assign_complete:
        out.append(_("aliases not assigned: %(a)s") % {"a": ", ".join(a for a in p.aliases_needed() if not p.assign.get(a))})
    ports = set(profile_ports(p))
    bad = [x for x in p.assign.values() if x not in ports]
    if bad:
        out.append(_("not a port of the cards: %(p)s") % {"p": ", ".join(bad)})
    if p.hosts.exclude(state="retired").exists():
        if not p.checked_at:
            out.append(_("the hosts are not checked yet"))
        elif any((p.check_result or {}).values()):
            out.append(_("the last check found differences"))
    return out


def matching(f):
    return [p for p in ServerProfile.objects.all() if not compare(p, f) and _comparable(p, f)]


def _comparable(p, f):
    """A match needs the essentials known on both sides."""
    return bool((f.get("cpu_name") or f.get("chassis")) and (p.cpu_name or p.chassis))


def _next_code(f):
    kind = "g" if int(f.get("gpu_count") or 0) else "c"
    vendor = _n(f.get("cpu_vendor") or f.get("cpu_vendor_ipmi"))
    v = "a" if "amd" in vendor else ("i" if "intel" in vendor else "x")
    n = 1
    while ServerProfile.objects.filter(code=f"{kind}{v}{n}").exists():
        n += 1
    return f"{kind}{v}{n}"


def profile_from(h, user):
    f = h.facts or {}
    cards = [{"slot": c.get("slot") or "", "bus": c.get("bus") or "", "name": c.get("name") or "", "vendor": c.get("vendor") or "",
              "model": c.get("model") or c.get("chip") or "", "ports": c.get("ports") or len(c.get("links") or []),
              "speed": c.get("speed") or 0, "transceiver": c.get("transceiver") or "", "links": c.get("links") or []}
             for c in (f.get("nics") or {}).get("cards") or []]
    assign = {a: port for c in (f.get("nics") or {}).get("cards") or [] for a, port in (c.get("aliases") or {}).items()
              if a in ALIASES}
    boot = f.get("boot") or {}
    p = ServerProfile(code=_next_code(f), vendor=f.get("vendor") or "", chassis=f.get("chassis") or "",
                      cpu_vendor=f.get("cpu_vendor") or f.get("cpu_vendor_ipmi") or "",
                      cpu_name=f.get("cpu_name") or f.get("cpu_name_ipmi") or "", sockets=f.get("sockets") or 1,
                      cores_per_socket=f.get("cores_per_socket") or 0, cpu_mhz=f.get("cpu_mhz") or 0,
                      ram_gb=f.get("ram_gb") or 0, gpu_model=f.get("gpu_model") or "", gpu_count=f.get("gpu_count") or 0,
                      ipmi_type=h.ipmi_type or "", reference_host=h.key,
                      boot={"dev": boot.get("dev", ""), "size_gb": boot.get("size_gb") or 0,
                            "volumes": [{"name": (v.get("path") or "/").strip("/").replace("/", "_") or "root",
                                         "size_gb": v.get("size_gb") or 0, "fs": v.get("fs") or "", "path": v.get("path") or ""}
                                        for v in boot.get("volumes") or []]} if boot else {},
                      nics={"cards": cards, "fabrics": 1 if assign and not any(a.startswith("nic-storage") for a in assign) else 2,
                            "assign": assign} if cards else {})
    p.save()
    audit(user, "profile_created_from_host", p, code=p.code, host=h.key)
    return p


def place(user, h, chosen=None, allow_create=True):
    """Read the host's hardware and give it the profile that fits. Returns a note for the user."""
    errors = collect(h)
    if not h.facts:
        if chosen:
            h.profile = ServerProfile.objects.filter(code=chosen).first()
            h.save(update_fields=["profile"])
        return _("The hardware could not be read (%(e)s); the profile is not checked.") % {"e": "; ".join(errors)}
    current = ServerProfile.objects.filter(code=chosen).first() if chosen else h.profile
    if current and not compare(current, h.facts):
        if h.profile_id != current.pk:
            h.profile = current
            h.save(update_fields=["profile"])
        return _("Profile %(p)s fits the hardware.") % {"p": current.code}
    fits = matching(h.facts)
    if fits:
        h.profile = fits[0]
        h.save(update_fields=["profile"])
        audit(user, "host_profile_set", h, host=h.key, profile=fits[0].code, was=current.code if current else "")
        if current:
            return _("Profile %(c)s does not fit (%(d)s); %(p)s does and is set.") % {
                "c": current.code, "d": "; ".join(compare(current, h.facts)[:3]), "p": fits[0].code}
        return _("Profile %(p)s detected.") % {"p": fits[0].code}
    if not allow_create:
        return _("No profile fits the hardware: %(d)s.") % {"d": "; ".join(compare(current, h.facts)[:3]) if current else "—"}
    p = profile_from(h, user)
    h.profile = p
    h.save(update_fields=["profile"])
    return _("No profile fitted the hardware: profile %(p)s was made from %(h)s; complete it on Server profiles.") % {
        "p": p.code, "h": h.name}


def check(profile, user=None):
    """Read every host of the profile and compare: {host: [differences] or ["error: …"]}."""
    hosts = list(profile.hosts.exclude(state="retired").select_related("site"))
    from django.db import connection
    if connection.vendor == "sqlite":  # tests: one connection
        got = [(h, read(h)) for h in hosts]
    else:
        with ThreadPoolExecutor(max_workers=8) as ex:
            got = list(zip(hosts, ex.map(read, hosts)))
    out = {}
    for h, (facts, errs) in got:
        if facts:
            h.facts, h.facts_at = facts, timezone.now()
        h.facts_error = "; ".join(errs)[:300] if errs else ""
        h.save(update_fields=["facts", "facts_at", "facts_error"])
    changes = names_from_ipmi(profile, [h.facts for h, (facts, _e) in got if facts])
    for h, (facts, errs) in got:
        out[h.name] = compare(profile, h.facts) if facts else [f"not read: {'; '.join(errs)}"]
    profile.check_result, profile.checked_at = out, timezone.now()
    profile.save(update_fields=["check_result", "checked_at"])
    if changes:
        audit(user, "profile_names_from_ipmi", profile, code=profile.code, changes=changes)
    return out


def _most(values):
    from collections import Counter
    values = [v for v in values if v]
    return Counter(values).most_common(1)[0][0] if values else ""


def names_from_ipmi(p, facts_list):
    """The names of the components as the IPMIs of the hosts give them (the most common one): chassis, CPU, GPU and
    card models; an unverified profile also takes the predicted Linux names of its cards, and its aliases follow.
    Returns the changes made, [] when none."""
    ipmi = [f for f in facts_list if f.get("ipmi_read")]
    if not ipmi:
        return []
    changes = []
    for field, key in (("vendor", "vendor"), ("chassis", "chassis"), ("cpu_vendor", "cpu_vendor"), ("cpu_name", "cpu_name")):
        v = _most(f.get(key) for f in ipmi)
        if v and v != getattr(p, field):
            changes.append(f"{field}: {getattr(p, field)} → {v}")
            setattr(p, field, v)
    if p.gpu_count:
        v = _most(f.get("gpu_model") for f in ipmi if f.get("gpu_count"))
        if v and v != p.gpu_model:
            changes.append(f"gpu_model: {p.gpu_model} → {v}")
            p.gpu_model = v
    cards, renamed = p.cards, {}
    for c in cards:
        if not c.get("slot"):
            continue
        same = [x for f in ipmi for x in (f.get("nics") or {}).get("cards") or [] if str(x.get("slot")) == str(c["slot"])]
        model = _most(x.get("model") for x in same)
        if model and model != c.get("model"):
            changes.append(f"card slot {c['slot']}: {c.get('model')} → {model}")
            c["model"] = model
            c["vendor"] = _most(x.get("vendor") for x in same) or c.get("vendor")
        name = _most(x.get("name") for x in same)
        if name and name != c.get("name") and not p.verified_at:
            old_ports = card_links(c)
            links = _most(tuple(x.get("links") or ()) for x in same if x.get("name") == name)
            changes.append(f"card slot {c['slot']}: {c.get('name')} → {name}")
            c["name"], c["links"] = name, list(links or ())
            renamed.update(zip(old_ports, card_links(c)))
    if not p.verified_at:  # cards every host's IPMI shows in a slot the profile does not have
        have = {str(c.get("slot")) for c in cards if c.get("slot")}
        per_host = [{str(x.get("slot")): x for x in (f.get("nics") or {}).get("cards") or [] if x.get("slot")} for f in ipmi]
        common = set.intersection(*[set(h) for h in per_host]) if per_host else set()
        for slot in sorted(common - have, key=lambda x: int(x) if x.isdigit() else 0):
            x = per_host[0][slot]
            cards.append({k: x.get(k) for k in ("slot", "bus", "name", "vendor", "model", "ports", "speed", "transceiver", "links")})
            changes.append(f"card slot {slot} added: {x.get('model')}")
        cards.sort(key=lambda c: int(c["slot"]) if str(c.get("slot") or "").isdigit() else 999)
    if changes:
        n = dict(p.nics or {})
        n["cards"] = cards
        if renamed and n.get("assign"):
            n["assign"] = {a: renamed.get(port, port) for a, port in n["assign"].items()}
        p.nics = n
        p.save()
    return changes


# --- the page ---------------------------------------------------------------------------------------------

class ProfileForm(forms.Form):
    code = forms.RegexField(label=_l("Code"), regex=r"^[a-z][a-z0-9-]{0,30}$")
    vendor = forms.CharField(label=_l("Vendor"), max_length=60)
    chassis = forms.CharField(label=_l("Model"), max_length=140)
    ipmi_type = forms.CharField(label=_l("IPMI"), max_length=16, required=False, help_text=_l("e.g. hpe, ilo6"))
    cpu_vendor = forms.CharField(label=_l("CPU vendor"), max_length=60)
    cpu_name = forms.CharField(label=_l("CPU model"), max_length=140)
    sockets = forms.IntegerField(label=_l("Sockets"), min_value=1, max_value=16)
    cores_per_socket = forms.IntegerField(label=_l("Cores per socket"), min_value=1, max_value=512)
    cpu_mhz = forms.IntegerField(label=_l("CPU frequency, MHz"), min_value=0, required=False)
    ram_gb = forms.IntegerField(label=_l("RAM, GB"), min_value=1, max_value=65536)
    gpu_model = forms.CharField(label=_l("GPU model"), max_length=100, required=False)
    gpu_count = forms.IntegerField(label=_l("GPUs"), min_value=0, max_value=64, required=False)
    gpu_vram_gb = forms.IntegerField(label=_l("GPU memory, GB"), min_value=0, max_value=4096, required=False)
    boot_dev = forms.CharField(label=_l("Boot device"), max_length=100, required=False, help_text=_l("e.g. /dev/nvme0n1"))
    boot_size_gb = forms.IntegerField(label=_l("Boot device size, GB"), min_value=0, required=False,
                                      help_text=_l("GB of the disk vendor (10⁹ bytes); volumes are in GiB (2³⁰ bytes)."))
    boot_volumes = forms.CharField(label=_l("Boot volumes"), required=False,
                                   widget=forms.Textarea(attrs={"rows": 5, "class": "mono"}),
                                   help_text=_l("Mount point, size in GiB (-1: the rest) and file system of each volume."))
    cards = forms.CharField(label=_l("Network cards"), required=False,
                            widget=forms.Textarea(attrs={"rows": 4, "class": "mono"}),
                            help_text=_l("PCI slot, the name Linux gives the card (estimated: its ports are <name>f0, <name>f1, …), "
                                         "vendor, model, number of ports, port speed in Gb/s and the transceiver type."))
    fabrics = forms.TypedChoiceField(label=_l("Network fabrics"), coerce=int, initial=2, required=False, empty_value=2, choices=(
        (2, _l("Two: data (bond0: nic-data1 + nic-data2) and storage (bond1: nic-storage1 + nic-storage2), 4 ports")),
        (1, _l("One: data and storage traffic together in bond0 (nic-data1 + nic-data2), 2 ports"))))
    assign = forms.CharField(label=_l("Port aliases"), required=False, widget=forms.Textarea(attrs={"rows": 2, "class": "mono"}),
                             help_text=_l("The port of the cards above each alias names; the aliases make the bonds."))

    def __init__(self, *args, ports=(), **kwargs):
        super().__init__(*args, **kwargs)
        from .network_views import rows_widget
        rows_widget(self.fields["boot_volumes"], [("path", _("Mount point"), "/var/log"), ("size", _("Size, GiB"), "100", "6em"),
                                                  ("fs", _("File system"), "", "7em", FILE_SYSTEMS)], _("Volume"))
        rows_widget(self.fields["cards"], [("slot", _("PCI slot"), "1", "5em"), ("name", _("Linux name"), "ens1", "7em"),
                                           ("vendor", _("Vendor"), "Mellanox"), ("model", _("Model"), "ConnectX-6 Lx"),
                                           ("ports", _("Ports"), "2", "4em"), ("speed", _("Gb/s"), "25", "4em"),
                                           ("transceiver", _("Transceiver"), "", "8em", TRANSCEIVERS)], _("Card"), sep="|")
        rows_widget(self.fields["assign"], [("alias", _("Alias"), "", "9em", ALIASES), ("port", _("Port"), "", "", "@dynamic")],
                    _("Alias"), sep="|")
        if self.auto_id:  # the ports are the ports of the cards above
            self.fields["assign"].widget.attrs["data-ports-from"] = self.auto_id % "cards"
        self.fields["assign"].initial = "\n".join(f"{a} | " for a in ALIASES)

    def clean_boot_volumes(self):
        out = []
        for line in (self.cleaned_data.get("boot_volumes") or "").splitlines():
            parts = line.split()
            if not parts:
                continue
            try:
                path, size = parts[0], int(parts[1])
            except (IndexError, ValueError):
                raise forms.ValidationError(_l("Not a volume: %(l)s") % {"l": line.strip()})
            if not path.startswith("/"):
                raise forms.ValidationError(_l("A mount point starts with /: %(l)s") % {"l": line.strip()})
            out.append({"name": path.strip("/").replace("/", "_") or "root", "path": path, "size_gb": size,
                        "fs": parts[2] if len(parts) > 2 else ""})
        return out

    def clean_cards(self):
        out = []
        for line in (self.cleaned_data.get("cards") or "").splitlines():
            if not line.strip():
                continue
            parts = [x.strip() for x in line.split("|")]
            if len(parts) == 5:  # the former format: name | vendor | model | speed | interfaces
                links = parts[4].split()
                parts = ["", parts[0], parts[1], parts[2], str(len(links)), parts[3], ""]
            else:
                links = []
            parts = (parts + [""] * 7)[:7]
            slot, name, vendor, model, ports, speed, transceiver = parts
            try:
                ports, speed = int(ports or 0), int(speed or 0)
            except ValueError:
                raise forms.ValidationError(_l("Ports and speed are numbers: %(l)s") % {"l": line.strip()})
            if not name or ports < 1:
                raise forms.ValidationError(_l("Give the card's Linux name and its ports: %(l)s") % {"l": line.strip()})
            out.append({"slot": slot, "name": name, "vendor": vendor, "model": model, "ports": ports, "speed": speed,
                        "transceiver": transceiver, "links": links})
        return out

    def clean_assign(self):
        out = {}
        for line in (self.cleaned_data.get("assign") or "").splitlines():
            parts = [x.strip() for x in (line.split("|") if "|" in line else line.split())]
            if not any(parts):
                continue
            alias, port = (parts + ["", ""])[:2]
            if alias not in ALIASES:
                raise forms.ValidationError(_l("Not an alias: %(a)s (one of %(l)s)") % {"a": alias, "l": ", ".join(ALIASES)})
            if alias in out:
                raise forms.ValidationError(_l("%(a)s is given twice.") % {"a": alias})
            if port:
                out[alias] = port
        if len(set(out.values())) != len(out):
            raise forms.ValidationError(_l("A port can have one alias only."))
        return out

    def clean(self):
        d = super().clean()
        a, fabrics = d.get("assign") or {}, d.get("fabrics") or 2
        if fabrics == 1 and any(x.startswith("nic-storage") for x in a):
            self.add_error("assign", _l("With one fabric there is no storage bond: nic-storage1 and nic-storage2 are not used."))
        return d


FILE_SYSTEMS = ("xfs", "ext4", "vfat", "swap", "btrfs")
TRANSCEIVERS = ("SFP+", "SFP28", "SFP56", "QSFP+", "QSFP28", "QSFP56", "DAC", "RJ45", "FIBRE")


def card_links(c):
    """The interface names of a card: as read from the host, else estimated from its Linux name (<name>f0, <name>f1…;
    Mellanox cards add np<i>)."""
    if c.get("links"):
        return list(c["links"])
    from .redfish import is_mellanox, linux_names
    if not c.get("name") or not int(c.get("ports") or 0):
        return []
    return linux_names(c["name"], int(c["ports"]), is_mellanox(f"{c.get('vendor')} {c.get('model')}"))


def profile_ports(p):
    return [x for c in (p.cards if p else []) for x in card_links(c)]


def initial(p):
    return {"code": p.code, "vendor": p.vendor, "chassis": p.chassis, "ipmi_type": p.ipmi_type, "cpu_vendor": p.cpu_vendor,
            "cpu_name": p.cpu_name, "sockets": p.sockets, "cores_per_socket": p.cores_per_socket, "cpu_mhz": p.cpu_mhz or None,
            "ram_gb": p.ram_gb, "gpu_model": p.gpu_model, "gpu_count": p.gpu_count, "gpu_vram_gb": p.gpu_vram_gb,
            "boot_dev": (p.boot or {}).get("dev", ""), "boot_size_gb": (p.boot or {}).get("size_gb") or None,
            "boot_volumes": "\n".join(f"{v.get('path') or '/' + v.get('name', '')} {v.get('size_gb')} {v.get('fs', '')}".strip()
                                      for v in p.boot_volumes),
            "cards": "\n".join(" | ".join([str(c.get("slot") or ""), c.get("name", ""), c.get("vendor", ""), c.get("model", ""),
                                           str(c.get("ports") or len(c.get("links") or [])), str(c.get("speed") or ""),
                                           c.get("transceiver") or ""]) for c in p.cards),
            "fabrics": p.fabrics,
            "assign": "\n".join(f"{a} | {p.assign.get(a, '')}" for a in p.aliases_needed())}


VERIFIED_EDITABLE = ("boot_volumes",)
ASSIGN_FIELDS = ("fabrics", "assign")


def lock_form(form, p):
    """A verified profile: only the volumes change, and the aliases while no active host of a cluster uses it."""
    if not p or not p.verified_at:
        return form
    free = VERIFIED_EDITABLE + (() if p.assign_locked else ASSIGN_FIELDS)
    for name, f in form.fields.items():
        if name not in free:
            f.disabled = True
    return form


def context(request):
    profiles = list(ServerProfile.objects.prefetch_related("hosts__site", "hosts__clusters"))
    for p in profiles:
        p.form = lock_form(ProfileForm(initial=initial(p), auto_id=f"id_pr_{p.code}_%s", ports=profile_ports(p)), p)
        from accounts import tasks as tk
        p.checking = tk.running("profile check", p)
        p.form.fields["code"].disabled = True
        p.alias_rows = [(a, p.assign.get(a, "")) for a in p.aliases_needed()]
        p.host_list = [h for h in p.hosts.all() if h.state != "retired"]
        p.bad = {h: d for h, d in (p.check_result or {}).items() if d}
    return {"profiles": profiles, "can_edit": request.user.is_super_admin, "profile_form": ProfileForm(auto_id="id_pr_new_%s"),
            "can_check": request.user.has_role(Role.SUPPORT)}


def _keep_links(cards, prev):
    """Cards keep the interface names read from the hosts while their name and ports stay the same."""
    old = {c.get("name"): c for c in (prev.cards if prev else [])}
    for c in cards:
        o = old.get(c["name"])
        if not c.get("links") and o and o.get("links") and len(o["links"]) == c["ports"]:
            c["links"] = list(o["links"])
    return cards


def _data(d):
    data = {k: d.get(k) for k in ("vendor", "chassis", "ipmi_type", "cpu_vendor", "cpu_name", "sockets", "cores_per_socket",
                                  "ram_gb", "gpu_model")}
    data.update(cpu_mhz=d.get("cpu_mhz") or 0, gpu_count=d.get("gpu_count") or 0, gpu_vram_gb=d.get("gpu_vram_gb") or 0,
                boot={"dev": d.get("boot_dev") or "", "size_gb": d.get("boot_size_gb") or 0, "volumes": d["boot_volumes"]},
                nics={"cards": d["cards"], "fabrics": d.get("fabrics") or 2, "assign": d.get("assign") or {}})
    return data


@require_POST
@staff_required(super_only=True)
def profile_save_view(request, code=None):
    from . import ops
    prof = ServerProfile.objects.filter(code=code).first() if code else None
    form = lock_form(ProfileForm(request.POST, initial=initial(prof) if prof else None, ports=profile_ports(prof)), prof)
    if code:
        form.fields["code"].disabled = True
    if form.is_valid():
        d = form.cleaned_data
        d["cards"] = _keep_links(d["cards"], prof)
        ports = {x for c in d["cards"] for x in card_links(c)}
        bad = [x for x in d["assign"].values() if x not in ports]
        if bad:
            messages.error(request, _("Port aliases: %(p)s is not a port of the cards.") % {"p": ", ".join(bad)})
            return redirect(reverse("infra_profiles") + (f"?sel=pr-{code}" if code else ""))
        data = _data(d)
        if prof and prof.verified_at:  # what a verified profile still lets change
            keep = {"boot": {**(prof.boot or {}), "volumes": data["boot"]["volumes"]}}
            if not prof.assign_locked:
                keep["nics"] = {**(prof.nics or {}), "fabrics": data["nics"]["fabrics"], "assign": data["nics"]["assign"]}
            data = keep
        try:
            ops.profile_save(request.user, code or d["code"], data, new=code is None)
            messages.success(request, _("Server profile saved."))
        except ops.EditError as exc:
            messages.error(request, str(exc))
    else:
        for f, errs in form.errors.items():
            messages.error(request, f"{form.fields[f].label if f in form.fields else ''}: {' '.join(errs)}")
    return redirect(reverse("infra_profiles") + (f"?sel=pr-{code}" if code else ""))


@require_POST
@staff_required(super_only=True)
def profile_verify_view(request, code):
    """Verify: the profile is complete and its hosts match; from now on only the volumes change."""
    p = get_object_or_404(ServerProfile, code=code)
    problems = verify_problems(p)
    if p.verified_at:
        messages.info(request, _("Server profile %(p)s is already verified.") % {"p": code})
    elif problems:
        messages.error(request, _("Server profile %(p)s cannot be verified: %(d)s.") % {"p": code, "d": "; ".join(problems)})
    else:
        p.verified_at, p.verified_by = timezone.now(), request.user.email
        p.save(update_fields=["verified_at", "verified_by"])
        audit(request.user, "profile_verified", p, code=code)
        messages.success(request, _("Server profile %(p)s is verified: only its volumes can be changed now.") % {"p": code})
    return redirect(reverse("infra_profiles") + f"?sel=pr-{code}")


@require_POST
@staff_required(super_only=True)
def profile_delete_view(request, code):
    from . import ops
    try:
        ops.profile_delete(request.user, code)
        messages.success(request, _("Server profile deleted."))
    except ops.EditError as exc:
        messages.error(request, str(exc))
    return redirect("infra_profiles")


@require_POST
@staff_required(Role.SUPPORT)
def profile_check_view(request, code):
    """Check: read every host of the profile (IPMI, then the OS over ssh) and compare — a task, as it takes long."""
    from accounts import tasks
    p = get_object_or_404(ServerProfile, code=code)
    if tasks.running("profile check", p):
        messages.info(request, _("The check of %(p)s is already running: see Tasks.") % {"p": code})
        return redirect(reverse("infra_profiles") + f"?sel=pr-{code}")
    user = request.user

    def run(task):
        out = check(p, user)
        bad = {h: d for h, d in out.items() if d}
        for h, d in out.items():
            task.step(h, "failed" if d else "ok", "; ".join(d[:4]) if d else "same as the profile")
        audit(user, "profile_checked", p, code=code, hosts=len(out), different=len(bad))
        return {"hosts": len(out), "different": len(bad)}
    tasks.start("profile check", _("Check of server profile %(p)s") % {"p": code}, run, user=user, target=p)
    messages.success(request, _("Checking the hosts of %(p)s: the result is on the profile and in Tasks.") % {"p": code})
    return redirect(reverse("infra_profiles") + f"?sel=pr-{code}")
