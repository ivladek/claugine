"""Minimal Redfish client for HPE iLO 6/7 (the BMCs of the hosts): read hardware, read and set NTP/DNS/time zone.

The iLOs use self-signed certificates on the management network, so the certificate is not verified.
Credentials come from Infrastructure → Credentials (the host's ``ipmi.user`` reference).
"""
import base64
import json
import re
import ssl
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

TIMEOUT = 8
GPU_WORDS = ("nvidia", "instinct", "radeon")
NOT_GPU = ("connectx", "mellanox", "bluefield", "ethernet", "network", "nvme", "infiniband")


class RedfishError(Exception):
    pass


class Client:
    def __init__(self, host, user, password, timeout=TIMEOUT, port=443):
        self.base = f"https://{host}" + (f":{port}" if port and int(port) != 443 else "")  # Redfish: always https
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE
        self.timeout = timeout

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Authorization": self.auth, "Content-Type": "application/json",
                                              "OData-Version": "4.0"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            try:
                info = json.loads(exc.read() or b"{}")
                msg = "; ".join(m.get("MessageId", "") for m in info.get("error", {}).get("@Message.ExtendedInfo", []))
            except ValueError:
                msg = ""
            raise RedfishError(f"{method} {path}: HTTP {exc.code} {msg}".strip()) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise RedfishError(f"{self.base}: {getattr(exc, 'reason', exc)}") from exc

    def get(self, path):
        return self._req("GET", path)

    def patch(self, path, body):
        from accounts import ha
        try:
            ha.forbid_writes(f"{self.base} settings")
        except ha.ReadOnlyHere as exc:
            raise RedfishError(str(exc)) from exc
        return self._req("PATCH", path, body)

    # --- reading ------------------------------------------------------------------------

    def manager(self):
        m = self.get("/redfish/v1/Managers/1")
        model = str(m.get("Model") or "")
        kind = model.lower().replace(" ", "")  # "iLO 6" -> "ilo6"
        return {"type": kind, "model": model, "firmware": m.get("FirmwareVersion", "")}

    def settings(self):
        """NTP, DNS and time zone as set on the iLO."""
        dt = self.get("/redfish/v1/Managers/1/DateTime")
        eth = self.get("/redfish/v1/Managers/1/EthernetInterfaces/1")
        hpe = (eth.get("Oem") or {}).get("Hpe") or {}
        dhcp = hpe.get("DHCPv4") or {}
        tz = dt.get("TimeZone") or {}
        syslog = {}
        try:
            m = (self.get("/redfish/v1/Managers/1").get("Oem") or {}).get("Hpe") or {}
            if m.get("RemoteSyslogEnabled") and m.get("RemoteSyslogServer"):
                syslog = {"host": m.get("RemoteSyslogServer"), "port": int(m.get("RemoteSyslogPort") or 514)}
        except RedfishError:
            pass
        return {"ntp": [x for x in dt.get("StaticNTPServers") or [] if x], "syslog": syslog,
                "timezone": tz.get("Name", ""), "tz_offset": tz.get("UtcOffset", ""),
                "zones": [{"index": z.get("Index"), "name": z.get("Name"), "offset": z.get("UtcOffset"),
                           "dst": "," in str(z.get("Value") or "")} for z in dt.get("TimeZoneList") or []],
                "dns": [x for x in (hpe.get("IPv4") or {}).get("DNSServers") or [] if x and x != "0.0.0.0"],
                "dhcp_ntp": bool(dhcp.get("UseNTPServers")), "dhcp_dns": bool(dhcp.get("UseDNSServers")),
                "hostname": eth.get("HostName", "")}

    def hardware(self):
        s = self.get("/redfish/v1/Systems/1")
        ps = s.get("ProcessorSummary") or {}
        cores = 0
        try:
            p = self.get("/redfish/v1/Systems/1/Processors/1")
            cores = int(p.get("TotalCores") or 0)
            cpu_vendor = str(p.get("Manufacturer") or "")
        except RedfishError:
            cpu_vendor = ""
        devices = self._members("/redfish/v1/Systems/1/PCIDevices")  # HPE: names and slots of the PCI devices
        gpus = [str(d.get("Name")) for d in devices if _is_gpu(str(d.get("Name") or ""))]
        try:
            coll = {} if gpus else self.get("/redfish/v1/Chassis/1/PCIeDevices")
            for m in coll.get("Members") or []:
                d = self.get(m["@odata.id"])
                name = " ".join(str(d.get(k) or "") for k in ("Name", "Model")).strip()
                if _is_gpu(name):
                    gpus.append(str(d.get("Model") or d.get("Name")))
        except RedfishError:
            pass
        gpu_model, gpu_count = (Counter(gpus).most_common(1)[0] if gpus else ("", 0))
        vendor, model = str(s.get("Manufacturer") or ""), str(s.get("Model") or "")
        if vendor and model.lower().startswith(vendor.lower() + " "):  # HPE ProLiant Compute DL380a Gen12
            model = model[len(vendor):].strip()
        return {"vendor": vendor, "model": model, "serial": str(s.get("SerialNumber") or "").strip(),
                "cpu_vendor": cpu_vendor, "cpu_model": str(ps.get("Model") or "").strip(),
                "sockets": int(ps.get("Count") or 0), "cores": cores,
                "ram_gb": int(round(float((s.get("MemorySummary") or {}).get("TotalSystemMemoryGiB") or 0))),
                "gpu_model": gpu_model, "gpu_quantity": gpu_count, "cards": self._cards(devices), "boot": self._boot()}

    def _members(self, path):
        try:
            return [self.get(m["@odata.id"]) for m in self.get(path).get("Members") or []]
        except (RedfishError, KeyError):
            return []

    def _cards(self, devices):
        """Network cards: slot (HPE PCIDevices), model, ports and speed (NetworkAdapters), the Linux names predicted."""
        pci = [d for d in devices if str(d.get("DeviceType") or "") == "NIC"]
        adapters = []
        for a in self._members("/redfish/v1/Chassis/1/NetworkAdapters"):
            ports = []
            link = (a.get("Ports") or a.get("NetworkPorts") or {}).get("@odata.id")
            if link:
                ports = self._members(link)
            adapters.append({"model": a.get("Model") or a.get("Name") or "", "part": a.get("PartNumber") or "",
                             "ports": _port_count(a, ports), "speed": nominal_speed(max(
                                 [float(p.get("CurrentSpeedGbps") or 0) for p in ports] or [0])),
                             "slot": str((((a.get("Location") or {}).get("PartLocation") or {}).get("LocationOrdinalValue")) or "")})
        return nic_cards(pci, adapters)

    def _boot(self, with_health=False):
        """The boot volume (HPE NS204i boot controller, else the smallest volume): size in GB (10⁹ bytes); with_health:
        its health and its controller's."""
        best = None
        for st in self._members("/redfish/v1/Systems/1/Storage"):
            names = " ".join([str(st.get("Name") or "")] + [str(c.get("Name") or c.get("Model") or "")
                                                            for c in st.get("StorageControllers") or []]).lower()
            link = (st.get("Volumes") or {}).get("@odata.id")
            for v in self._members(link) if link else []:
                size = int(v.get("CapacityBytes") or 0)
                if not size:
                    continue
                boot = "ns204" in names or "boot" in names
                key = (not boot, size)
                if best is None or key < best[0]:
                    best = (key, {"size_gb": round(size / 1e9), "controller": (st.get("StorageControllers") or [{}])[0].get("Name")
                                  or st.get("Name") or "", "raid": str(v.get("RAIDType") or "")})
                    if with_health:
                        vs, cs = v.get("Status") or {}, st.get("Status") or {}
                        best[1].update(health=str(vs.get("Health") or vs.get("HealthRollup") or ""),
                                       controller_health=str(cs.get("HealthRollup") or cs.get("Health") or ""),
                                       state=str(vs.get("State") or ""))
        return best[1] if best else {}


    def health(self):
        """What the IPMI says about the parts a host needs (2026-10-29): the boot volume, every network port (link and
        health, by PCI slot and port), CPUs and GPUs (Processors: HPE lists the GPUs there), memory; BIOS and IPMI
        firmware versions. Read only."""
        s = self.get("/redfish/v1/Systems/1")
        m = self.get("/redfish/v1/Managers/1")

        def health_of(d):
            st = (d or {}).get("Status") or {}
            return st.get("HealthRollup") or st.get("Health") or ""
        procs = []
        try:
            members = self.get("/redfish/v1/Systems/1/Processors").get("Members") or []
        except RedfishError:
            members = []
        for mem in members:  # one by one: a member that does not answer does not hide the others
            try:
                p = self.get(mem["@odata.id"])
            except (RedfishError, KeyError):
                continue
            st = p.get("Status") or {}
            if st.get("State") == "Absent":
                continue
            procs.append({"id": str(p.get("Id") or ""), "type": str(p.get("ProcessorType") or "CPU"),
                          "model": str(p.get("Model") or ""), "health": str(st.get("Health") or ""),
                          "state": str(st.get("State") or "")})
        agg = ((s.get("Oem") or {}).get("Hpe") or {}).get("AggregateHealthStatus") or {}
        memory = health_of(s.get("MemorySummary")) or ((agg.get("Memory") or {}).get("Status") or {}).get("Health") or ""
        # network ports by PCI slot: a card's first port names the card's MAC (LLDP chassis id), which the PCI device's
        # name carries too; cards without (OCP, some ConnectX-7) take the NIC slots left, OCP with OCP, by model words
        mac_re = re.compile(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")
        nics = []
        for d in self._members("/redfish/v1/Systems/1/PCIDevices"):
            if str(d.get("DeviceType") or "") == "NIC" and _slot(d):
                mm = mac_re.search(str(d.get("Name") or ""))
                nics.append({"slot": _slot(d), "mac": mm.group(1).upper() if mm else "", "name": str(d.get("Name") or ""),
                             "ocp": str(d.get("StructuredName") or "").startswith("OCP") or "ocp" in str(d.get("Name") or "").lower()})
        adapters = []
        for a in self._members("/redfish/v1/Chassis/1/NetworkAdapters"):
            link = (a.get("Ports") or a.get("NetworkPorts") or {}).get("@odata.id")
            plist = self._members(link) if link else []
            macs = {str((((p.get("Ethernet") or {}).get("LLDPTransmit") or {}).get("ChassisId")) or "").upper() for p in plist}
            adapters.append({"a": a, "ports": plist, "macs": macs - {""},
                             "slot": str((((a.get("Location") or {}).get("PartLocation") or {}).get("LocationOrdinalValue")) or "")})
        left = list(nics)
        for ad in adapters:  # by MAC, then by the adapter's own location
            n = next((n for n in left if n["mac"] and n["mac"] in ad["macs"]), None) or \
                next((n for n in left if ad["slot"] and n["slot"] == ad["slot"]), None)
            if n is not None:
                ad["slot"] = n["slot"]
                left.remove(n)
            elif ad["slot"]:
                ad["fixed"] = True

        def words(x):
            return {w for w in re.split(r"[^a-z0-9]+", str(x).lower()) if len(w) > 1} - {"network", "adapter", "nic", "ethernet"}
        for ad in adapters:
            if (ad["slot"] and (ad.get("fixed") or ad["slot"] in {n["slot"] for n in nics} - {n["slot"] for n in left})) or not left:
                continue
            model = str(ad["a"].get("Model") or "")
            ocp = "ocp" in model.lower()
            n = max(left, key=lambda n: ((n["ocp"] == ocp), len(words(n["name"]) & words(model)), -left.index(n)))
            ad["slot"] = n["slot"]
            left.remove(n)
        ports = {}
        for ad in adapters:
            plist = ad["ports"]
            ids = [str(p.get("Id") or "") for p in plist]
            one_based = bool(ids) and all(i.isdigit() for i in ids) and min(int(i) for i in ids) == 1
            slot = ad["slot"] or "?"
            for k, p in enumerate(plist):
                pid, iid = str(p.get("PortId") or ""), str(p.get("Id") or "")
                idx = int(iid) - (1 if one_based else 0) if iid.isdigit() else (int(pid) - 1 if pid.isdigit() else k)
                ports.setdefault(slot, {})[str(idx)] = {
                    "link": str(p.get("LinkStatus") or ""), "health": str((p.get("Status") or {}).get("Health") or ""),
                    "speed": p.get("CurrentSpeedGbps"), "model": str(ad["a"].get("Model") or "")}
        return {"bios": str(s.get("BiosVersion") or ""), "ipmi_fw": str(m.get("FirmwareVersion") or ""),
                "ipmi_model": str(m.get("Model") or ""), "power": str(s.get("PowerState") or ""),
                "system": health_of(s), "memory": memory, "processors": procs, "ports": ports,
                "cpu_summary": health_of(s.get("ProcessorSummary")),
                "boot": self._boot(with_health=True)}

    # --- writing ------------------------------------------------------------------------

    def apply(self, ntp=None, dns=None, timezone=None, syslog=None):
        """Set static NTP, DNS, the time zone (an iLO zone index) and remote syslog ({host, port}; the iLO sends UDP).
        Returns the iLO messages (a reset may be required)."""
        out = []
        if syslog is not None:
            body = {"RemoteSyslogEnabled": bool(syslog.get("host"))}
            if syslog.get("host"):
                body.update(RemoteSyslogServer=syslog["host"], RemoteSyslogPort=int(syslog.get("port") or 514))
            out += _messages(self.patch("/redfish/v1/Managers/1", {"Oem": {"Hpe": body}}))
        oem = {}
        if ntp is not None or dns is not None:
            oem["DHCPv4"] = {}
            if ntp is not None:
                oem["DHCPv4"]["UseNTPServers"] = False
            if dns is not None:
                oem["DHCPv4"]["UseDNSServers"] = False
                oem["IPv4"] = {"DNSServers": (list(dns) + ["0.0.0.0"] * 3)[:3]}
            r = self.patch("/redfish/v1/Managers/1/EthernetInterfaces/1", {"Oem": {"Hpe": oem}})
            out += _messages(r)
        body = {}
        if ntp is not None:
            body["StaticNTPServers"] = (list(ntp) + [""] * 2)[:max(2, len(ntp))]
        if timezone is not None:
            body["TimeZone"] = {"Index": timezone}
        if body:
            out += _messages(self.patch("/redfish/v1/Managers/1/DateTime", body))
        return out


def _messages(resp):
    info = (resp or {}).get("error", {}).get("@Message.ExtendedInfo") or (resp or {}).get("@Message.ExtendedInfo") or []
    return [m.get("MessageId", "") for m in info if m.get("MessageId")]


# --- hosts of a site -------------------------------------------------------------------------

def host_credentials(host):
    """(user, password) for a Host from its ipmi.user reference (Credentials first, then the secrets files)."""
    from .credentials import find
    ref = host.ipmi_user
    if not ref:
        raise RedfishError(f"{host.name}: no IPMI user in the inventory")
    s = find(ref)
    if s is None or not s.has("password"):
        raise RedfishError(f"{host.name}: secret {ref} is not defined")
    return s.username or s.name.rsplit(".", 1)[-1], s.get("password")


def for_hosts(hosts, fn, workers=12):
    """Run fn(client, host) for every host with an IPMI address, in parallel: {host name: result or {"error": ...}}."""
    def one(h):
        try:
            if not h.ipmi_host:
                raise RedfishError("no IPMI address")
            user, password = host_credentials(h)
            return h.name, fn(Client(h.ipmi_host, user, password, port=h.ipmi_port or 443), h)
        except RedfishError as exc:
            return h.name, {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - shown per host
            return h.name, {"error": f"{type(exc).__name__}: {exc}"}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return dict(ex.map(one, list(hosts)))


def utc_offset(tz_name):
    """'+05:00' for an IANA zone now."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    off = datetime.now(ZoneInfo(tz_name)).utcoffset()
    minutes = int(off.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def zone_for(zones, offset):
    """The iLO zone index for an offset: a zone without daylight saving first."""
    same = [z for z in zones if z.get("offset") == offset]
    same.sort(key=lambda z: (z.get("dst"), z.get("index") or 0))
    return same[0]["index"] if same else None


def _is_gpu(name):
    low = name.lower()
    return any(w in low for w in GPU_WORDS) and not any(w in low for w in NOT_GPU)


SPEEDS = (1, 10, 25, 40, 50, 100, 200, 400, 800)


def nominal_speed(gbps):
    """424.8 → 400: the nominal Ethernet speed nearest below the reported one."""
    if not gbps:
        return 0
    return max([s for s in SPEEDS if s <= gbps * 1.1] or [int(gbps)])


def _port_count(a, ports):
    for c in a.get("Controllers") or []:
        n = (c.get("ControllerCapabilities") or {}).get("NetworkPortCount")
        if n:
            return int(n)
    return len(ports)


def card_model(model):
    """'P42046-001 / Mellanox MCX631102AS-ADAT Ethernet …' → ('Mellanox', 'MCX631102AS-ADAT Ethernet …')."""
    m = str(model or "").split(" / ", 1)[-1].strip()
    for v in ("Mellanox", "NVIDIA", "Intel", "Broadcom", "HPE"):
        if m.lower().startswith(v.lower() + " "):
            return ("Mellanox" if "MCX" in m.upper() else v), m[len(v):].strip()
    vendor = "Mellanox" if ("MCX" in m.upper() or "CONNECTX" in m.upper()) else ("Broadcom" if m.upper().startswith("BCM") else "")
    return vendor, m


def is_mellanox(text):
    t = str(text or "").lower()
    return any(w in t for w in ("mellanox", "connectx", "mcx", "nvidia", "mlx5"))


def linux_names(base, ports, mellanox):
    """Predictable names (systemd): <base>f<function>[np<port>]; a one-function card has no f0 (ens3np0)."""
    if int(ports or 0) <= 1:
        return [base + ("np0" if mellanox else "")]
    return [f"{base}f{i}" + (f"np{i}" if mellanox else "") for i in range(int(ports))]


def slot_base(slot, domain=0):
    """The Linux name of the card in PCI slot `slot`: ens1; a card in PCI domain 1 is enP1s2."""
    return f"enP{domain}s{slot}" if domain else f"ens{slot}"


def _domain(uefi_path):
    """PciRoot(0x103)/… → 1: HPE puts the PCI segment in the high byte of the root's UID."""
    m = re.match(r"PciRoot\((0x[0-9a-fA-F]+|\d+)\)", str(uefi_path or ""))
    return (int(m.group(1), 0) >> 8) if m else 0


def _slot(d):
    m = re.match(r"^(?:NIC|OCP|PCI)\.Slot\.(\d+)\.", str(d.get("StructuredName") or ""))
    if m:
        return m.group(1)
    m = re.search(r"(\d+)\s*$", str(d.get("LocationString") or d.get("DeviceLocation") or ""))
    return m.group(1) if m else ""


def nic_cards(pci, adapters):
    """Pair the NIC slots (HPE PCIDevices) with the network adapters (model, ports, speed): an adapter that names its
    slot goes there; else OCP with OCP, then the one whose model shares the most words with the slot's device name.
    Adapters of one model are alike, so which of them lands in which slot does not change the result."""
    left = list(adapters)
    out = []
    def words(s):
        return {w for w in re.split(r"[^a-z0-9]+", str(s).lower()) if len(w) > 1} - {"network", "adapter", "nic", "ethernet"}
    for d in sorted(pci, key=lambda d: int(_slot(d) or 0)):
        slot, name = _slot(d), re.sub(r"\b([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b", "", str(d.get("Name") or ""))
        ocp = str(d.get("StructuredName") or "").startswith("OCP") or "ocp" in name.lower()
        pick = next((a for a in left if a.get("slot") and a["slot"] == slot), None)
        if pick is None and left:
            def score(a):
                m = str(a.get("model") or "").lower()
                nums = {x for x in re.findall(r"\d{2,}", name)} & {x for x in re.findall(r"\d{2,}", m)}
                return (("ocp" in m) == ocp, len(words(name) & words(m)) + len(nums), -left.index(a))
            pick = max(left, key=score)
        if pick is not None:
            left.remove(pick)
        vendor, model = card_model(pick["model"] if pick else name)
        ports = int((pick or {}).get("ports") or 0) or 1
        mlx = is_mellanox(name + " " + model)
        base = slot_base(slot, _domain(d.get("UEFIDevicePath"))) if slot else ""
        tr = next((t for t in ("OSFP", "QSFP56", "QSFP28", "QSFP+", "SFP56", "SFP28", "SFP+") if t in model.upper()), "")
        if not tr and "BASE-T" in model.upper():
            tr = "RJ45"
        out.append({"slot": slot, "bus": "ocp3" if ocp else "pci", "name": base, "vendor": vendor, "model": model,
                    "ports": ports, "speed": int((pick or {}).get("speed") or 0), "transceiver": tr,
                    "links": linux_names(base, ports, mlx) if base else [], "mellanox": mlx})
    for a in left:  # adapters with no slot found
        vendor, model = card_model(a["model"])
        out.append({"slot": a.get("slot") or "", "bus": "", "name": "", "vendor": vendor, "model": model,
                    "ports": int(a.get("ports") or 0), "speed": int(a.get("speed") or 0), "transceiver": "", "links": []})
    return out


def cpu_label(model, vendor=""):
    """'AMD EPYC 9354 32-Core Processor' → 'EPYC 9354'; 'INTEL(R) XEON(R) GOLD 6548N' → 'Xeon Gold 6548N'."""
    s = re.sub(r"\((r|tm)\)", " ", str(model or ""), flags=re.I)
    s = re.sub(r"\b\d+-core\b|\bprocessor\b|\bcpu\b|@.*$", " ", s, flags=re.I)
    s = re.sub(r"\b(amd|intel|genuineintel|authenticamd)\b", " ", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    if s.isupper() and "XEON" in s:
        s = " ".join(w.capitalize() if w.isalpha() else w for w in s.split())
    return s
