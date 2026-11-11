"""A small stateful OpenNebula and VyOS for tests of network changes (not used by the portal itself)."""
import re

from .configcheck import _norm


def _attrs(block):
    return dict(re.findall(r'(\w+) = "((?:[^"\\]|\\.)*)"', block))


def _vectors(text, key):
    return [_attrs(m) for m in re.findall(key + r" = \[\n(.*?)\n\]", text, re.S)]


class World:
    """Networks and VMs of one OpenNebula: enough of the XML-RPC API for deployments and network changes."""

    def __init__(self, base_pools, vnets=(), vms=(), images=None):
        self.pools = dict(base_pools)
        self.calls = []
        self.vnets, self.vms, self.vrouters = {}, {}, {}
        self.next = {"vn": 500, "vm": 600, "group": 300, "user": 400}
        self.macs = 0
        for v in vnets:
            self.add_vnet(**v)
        for v in vms:
            self.vms[v["id"]] = {"name": v["name"], "state": "3", "nics": []}
            for n in v.get("nics", []):
                self._nic(v["id"], n["network_id"], n.get("ip"))
        self.images = images

    def mac(self):
        self.macs += 1
        return f"02:00:00:00:{self.macs // 256:02x}:{self.macs % 256:02x}"

    def add_vnet(self, id, name, ars=(), phydev="", clusters=(0,), mtu=None, gname=""):  # noqa: A002
        self.vnets[id] = {"name": name, "ars": [], "phydev": phydev, "clusters": list(clusters), "mtu": mtu, "gname": gname}
        for a in ars:
            self._ar(id, a.get("TYPE", "IP4"), a.get("IP"), int(a.get("SIZE", 1)))

    def _ar(self, vid, typ, ip, size):
        ars = self.vnets[vid]["ars"]
        ars.append({"id": len(ars), "type": typ, "ip": ip, "size": size, "mac": self.mac(), "leases": {}})

    def _nic(self, vm_id, vid, ip=None, mac=None, name=None, parent=None):
        vn = self.vnets[int(vid)]
        ar = None
        if mac:
            ar = next(a for a in vn["ars"] if a["mac"] == mac)
        elif ip:
            ar = next((a for a in vn["ars"] if a["type"] != "ETHER" and a["ip"] and _in(ip, a)), vn["ars"][0])
        else:
            ar = vn["ars"][0]
        m = mac or self.mac()
        ar["leases"][ip or m] = vm_id
        nics = self.vms[vm_id]["nics"]
        nic_id = max([n["nic_id"] for n in nics] + [-1]) + 1
        nics.append({"nic_id": nic_id, "vid": int(vid), "ip": ip, "mac": m, "name": name or f"NIC{nic_id}",
                     "alias": parent is not None, "parent": parent or ""})

    # --- XML -------------------------------------------------------------------------------------

    def vnet_xml(self, vid, full=True):
        v = self.vnets[vid]
        ars = "".join(
            f"<AR><AR_ID>{a['id']}</AR_ID><TYPE>{a['type']}</TYPE>" + (f"<IP>{a['ip']}</IP>" if a["ip"] else "")
            + f"<MAC>{a['mac']}</MAC><SIZE>{a['size']}</SIZE><LEASES>"
            + "".join(f"<LEASE><IP>{k if '.' in k else ''}</IP><VM>{vm}</VM></LEASE>" for k, vm in a["leases"].items())
            + "</LEASES></AR>" for a in v["ars"]) if full else ""
        cl = "".join(f"<ID>{c}</ID>" for c in v["clusters"])
        tpl = f"<TEMPLATE><GUEST_MTU>{v['mtu']}</GUEST_MTU></TEMPLATE>" if v.get("mtu") else ""
        return (f"<VNET><ID>{vid}</ID><NAME>{v['name']}</NAME><GNAME>{v.get('gname', '')}</GNAME><PHYDEV>{v['phydev']}</PHYDEV><CLUSTERS>{cl}</CLUSTERS>"
                f"<AR_POOL>{ars}</AR_POOL>{tpl}</VNET>")

    def vm_xml(self, vm_id):
        v = self.vms[vm_id]
        tag = lambda n: "NIC_ALIAS" if n.get("alias") else "NIC"  # noqa: E731
        nics = "".join(f"<{tag(n)}><NIC_ID>{n['nic_id']}</NIC_ID><NETWORK_ID>{n['vid']}</NETWORK_ID>"
                       f"<NETWORK>{self.vnets[n['vid']]['name'] if n['vid'] in self.vnets else ''}</NETWORK>"
                       f"<IP>{n['ip'] or ''}</IP><MAC>{n['mac']}</MAC><NAME>{n.get('name', '')}</NAME>"
                       + (f"<PARENT>{n['parent']}</PARENT>" if n.get("alias") else "") + f"</{tag(n)}>"
                       for n in v["nics"])
        ctx = "".join(f"<{k}>{val}</{k}>" for k, val in (v.get("context") or {}).items())
        ctx = f"<CONTEXT>{ctx}</CONTEXT>" if v.get("context") else ""
        gname = f"<GNAME>{v['gname']}</GNAME>" if v.get("gname") else ""
        return (f"<VM><ID>{vm_id}</ID><NAME>{v['name']}</NAME>{gname}<STATE>{v['state']}</STATE><LCM_STATE>3</LCM_STATE>"
                f"<TEMPLATE>{nics}{ctx}</TEMPLATE></VM>")

    # --- calls ------------------------------------------------------------------------------------

    def _call(self, method, *args):
        self.calls.append((method, args))
        if method == "one.vnpool.info":
            return "<VNET_POOL>" + "".join(self.vnet_xml(i, False) for i in self.vnets) + "</VNET_POOL>"
        if method == "one.vn.info":
            return self.vnet_xml(int(args[0]))
        if method == "one.vn.allocate":
            vid = self.next["vn"]
            self.next["vn"] += 1
            t = args[0]
            name = re.search(r'^NAME = "([^"]+)"', t, re.M).group(1)
            self.add_vnet(vid, name, [{"TYPE": a.get("TYPE"), "IP": a.get("IP"), "SIZE": a.get("SIZE", 1)} for a in _vectors(t, "AR")],
                          phydev=(re.search(r'^PHYDEV = "([^"]+)"', t, re.M) or [None, ""])[1], clusters=[args[1]])
            return vid
        if method == "one.vn.delete":
            vid = int(args[0])
            if any(a["leases"] for a in self.vnets[vid]["ars"]):
                raise RuntimeError(f"[one.vn.delete] network {vid} has leases")
            del self.vnets[vid]
            return vid
        if method == "one.vn.add_ar":
            a = _vectors(args[1], "AR")[0]
            self._ar(int(args[0]), a.get("TYPE"), a.get("IP"), int(a.get("SIZE", 1)))
            return int(args[0])
        if method == "one.vn.rm_ar":
            self.vnets[int(args[0])]["ars"] = [a for a in self.vnets[int(args[0])]["ars"] if a["id"] != int(args[1])]
            return int(args[0])
        if method == "one.vmpool.info":
            return "<VM_POOL>" + "".join(self.vm_xml(i) for i, v in self.vms.items() if v["state"] != "6") + "</VM_POOL>"
        if method == "one.vm.info":
            return self.vm_xml(int(args[0]))
        if method == "one.vm.updateconf":  # merge (1) of the CONTEXT section
            v = self.vms[int(args[0])]
            if len(args) < 3 or int(args[2]) != 1:
                raise RuntimeError("[one.vm.updateconf] only merge is modelled")
            v["context"] = {**(v.get("context") or {}), **(_vectors(args[1], "CONTEXT") or [{}])[0]}
            return int(args[0])
        if method == "one.vm.allocate":
            vm_id = self.next["vm"]
            self.next["vm"] += 1
            t = args[0]
            self.vms[vm_id] = {"name": re.search(r'^NAME = "([^"]+)"', t, re.M).group(1), "state": "3", "nics": [],
                               "template": t, "context": (_vectors(t, "CONTEXT") or [{}])[0]}
            for n in _vectors(t, "NIC"):
                if int(n["NETWORK_ID"]) >= 0:
                    self._nic(vm_id, n["NETWORK_ID"], n.get("IP"), n.get("MAC"), n.get("NAME"))
            for n in _vectors(t, "NIC_ALIAS"):
                self._nic(vm_id, n["NETWORK_ID"], n.get("IP"), n.get("MAC"), n.get("NAME"), n["PARENT"])
            return vm_id
        if method == "one.templatepool.info" and "one.templatepool.info" not in self.pools:
            return ('<VMTEMPLATE_POOL><VMTEMPLATE><ID>77</ID><NAME>Service Virtual Router</NAME></VMTEMPLATE>'
                    '</VMTEMPLATE_POOL>')
        if method == "one.template.instantiate":
            vm_id = self.next["vm"]
            self.next["vm"] += 1
            t = args[3]
            self.vms[vm_id] = {"name": args[1], "state": "3", "nics": [], "template": t, "from_template": int(args[0])}
            for n in _vectors(t, "NIC"):
                if int(n["NETWORK_ID"]) >= 0:
                    self._nic(vm_id, n["NETWORK_ID"], n.get("IP"), n.get("MAC"))
            return vm_id
        if method == "one.vrouterpool.info" and "one.vrouterpool.info" not in self.pools:
            return "<VROUTER_POOL>" + "".join(f"<VROUTER><ID>{i}</ID><NAME>{v['name']}</NAME></VROUTER>"
                                              for i, v in self.vrouters.items()) + "</VROUTER_POOL>"
        if method == "one.vrouter.allocate":
            vr_id = 900 + len(self.vrouters)
            t = args[0]
            self.vrouters[vr_id] = {"name": re.search(r'^NAME = "([^"]+)"', t, re.M).group(1), "nics": _vectors(t, "NIC"),
                                    "vms": []}
            return vr_id
        if method == "one.vrouter.instantiate":
            vr = self.vrouters[int(args[0])]
            vm_id = self.next["vm"]
            self.next["vm"] += 1
            self.vms[vm_id] = {"name": args[3], "state": "3", "nics": [], "template": args[5], "from_template": int(args[2]),
                               "vrouter": int(args[0])}
            for n in vr["nics"]:
                if int(n["NETWORK_ID"]) >= 0:
                    self._nic(vm_id, n["NETWORK_ID"], n.get("IP"), n.get("MAC"))
            vr["vms"].append(vm_id)
            return int(args[0])
        if method == "one.vrouter.info":
            vr = self.vrouters[int(args[0])]
            nics = "".join(f"<NIC><NETWORK_ID>{n['NETWORK_ID']}</NETWORK_ID></NIC>" for n in vr["nics"])
            return (f"<VROUTER><ID>{args[0]}</ID><NAME>{vr['name']}</NAME><VMS>"
                    + "".join(f"<ID>{v}</ID>" for v in vr["vms"] if self.vms[v]["state"] != "6")
                    + f"</VMS><TEMPLATE>{nics}</TEMPLATE></VROUTER>")
        if method == "one.vrouter.attachnic":
            vr = self.vrouters[int(args[0])]
            n = _vectors(args[1], "NIC")[0]
            vr["nics"].append(n)
            for v in vr["vms"]:
                self._nic(v, n["NETWORK_ID"], n.get("IP"), n.get("MAC"))
            return int(args[0])
        if method == "one.vrouter.delete":
            vr = self.vrouters.pop(int(args[0]))
            for v in vr["vms"]:
                for n in self.vms[v]["nics"]:
                    self._release(n)
                self.vms[v]["state"] = "6"
            return int(args[0])
        if method == "one.vm.attachnic":
            alias = _vectors(args[1], "NIC_ALIAS")
            n = (alias or _vectors(args[1], "NIC"))[0]
            if alias and not any(m["name"] == n["PARENT"] and not m["alias"] for m in self.vms[int(args[0])]["nics"]):
                raise RuntimeError(f"[one.vm.attachnic] no parent NIC {n['PARENT']}")
            self._nic(int(args[0]), n["NETWORK_ID"], n.get("IP"), n.get("MAC"), n.get("NAME"), n["PARENT"] if alias else None)
            return int(args[0])
        if method == "one.vm.detachnic":
            vm_id, nic_id = int(args[0]), int(args[1])
            nic = next(n for n in self.vms[vm_id]["nics"] if n["nic_id"] == nic_id)
            if any(m["alias"] and m["parent"] == nic["name"] for m in self.vms[vm_id]["nics"]):
                raise RuntimeError(f"[one.vm.detachnic] NIC {nic_id} has aliases")
            self._release(nic)
            self.vms[vm_id]["nics"].remove(nic)
            return vm_id
        if method == "one.vm.action" and args[0] in ("poweroff", "poweroff-hard"):
            self.vms[int(args[1])]["state"] = "8"
            return int(args[1])
        if method == "one.vm.action" and args[0] == "resume":
            self.vms[int(args[1])]["state"] = "3"
            return int(args[1])
        if method == "one.vm.rename":
            self.vms[int(args[0])]["name"] = args[1]
            return int(args[0])
        if method == "one.vm.action" and args[0] in ("terminate-hard", "terminate"):
            vm_id = int(args[1])
            for n in self.vms[vm_id]["nics"]:
                self._release(n)
            self.vms[vm_id]["state"] = "6"
            return vm_id
        if method == "one.imagepool.info" and self.images:
            return self.images
        if method in ("one.group.allocate", "one.user.allocate"):
            k = method.split(".")[1]
            self.next[k] += 1
            return self.next[k] - 1
        if method in self.pools:
            return self.pools[method]
        return 0

    def _release(self, nic):
        vn = self.vnets.get(nic["vid"])
        for a in (vn or {}).get("ars", []):
            a["leases"] = {k: v for k, v in a["leases"].items() if k not in (nic["ip"], nic["mac"])}

    def vm(self, name):
        return next((i for i, v in self.vms.items() if v["name"] == name and v["state"] != "6"), None)

    def vnet(self, name):
        return next((i for i, v in self.vnets.items() if v["name"] == name), None)


def _in(ip, ar):
    import ipaddress
    start = ipaddress.ip_address(ar["ip"])
    return start <= ipaddress.ip_address(ip) < start + ar["size"]


class Routers:
    """VyOS routers by host: running set-lines, interfaces from the World's VM NICs, `vbash -s` scripts applied."""

    def __init__(self, world):
        self.world, self.config, self.scripts = world, {}, []

    def vm_by_ip(self, host):
        for vm_id, v in self.world.vms.items():
            if v["state"] != "6" and any(n["ip"] == host for n in v["nics"]):
                return vm_id
        raise RuntimeError(f"no VM at {host}")

    def run(self, host, port, user, key, remote, site=None, stdin=None, timeout=60):
        from .configcheck import SHOW
        from .routercfg import LINK
        vm_id = self.vm_by_ip(host)
        lines = self.config.setdefault(host, [])
        if remote == SHOW:
            return "\n".join(lines) + "\n"
        if remote == LINK:
            return "".join(f"{i + 2}: eth{i}: <BROADCAST> mtu 1500 qdisc mq state UP\\    link/ether {n['mac']} brd ff:ff:ff:ff:ff:ff\n"
                           for i, n in enumerate(m for m in self.world.vms[vm_id]["nics"] if not m.get("alias")))
        if "echo '##addr'" in remote:  # the read of a router (inventory.core_services.READ_SCRIPT + its checks)
            nics = [n for n in self.world.vms[vm_id]["nics"] if n["ip"]]
            state = "Active" if host in getattr(self, "bgp_down", ()) else "Established"
            return ("##host\n" + self.world.vms[vm_id]["name"].strip("_").replace("_", "-") + "\n##os\nVyOS 2026.03\n##cpu\n2\n"
                    "##mem\n4096\n##disk\n2G\n##addr\n" + "".join(f"eth{i} {n['ip']}/24\n" for i, n in enumerate(nics))
                    + ('##bgp\n{"ipv4Unicast": {"peers": {"10.0.10.250": {"state": "%s"}, "10.0.10.251": {"state": "%s"}}}}\n'
                       % (state, state) if "##bgp" in remote else ""))
        if remote.startswith("vtysh -c 'show bgp summary json'"):  # BGP of a gateway: its peers established
            state = "Active" if host in getattr(self, "bgp_down", ()) else "Established"
            return '{"ipv4Unicast": {"peers": {"10.0.10.250": {"state": "%s"}, "10.0.10.251": {"state": "%s"}}}}\n' % (state, state)
        if remote == "vbash -s":
            self.scripts.append((host, stdin))
            for cmd in stdin.splitlines():
                if cmd.startswith("set "):
                    if _norm(cmd) not in {_norm(x) for x in lines}:
                        lines.append(cmd)
                elif cmd.startswith("delete "):
                    node = _norm("set " + cmd[7:])
                    lines[:] = [x for x in lines if not (_norm(x) == node or _norm(x).startswith(node + " "))]
            return ""
        raise RuntimeError(f"unexpected remote command {remote!r}")
