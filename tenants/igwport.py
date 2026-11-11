"""Can an imported tenant's igw configuration (the old system's VyOS, `show configuration commands`) be carried to a
new igw made by the portal? The new VM gets the old one's networks and addresses in the same order, so eth0, eth1, …
stay what they were; what refers to things the new VM will not have makes the configuration not transferable.

    transferable       interfaces ethernet/loopback/bridge/dummy/wireguard/vti/tunnel (GRE) and their addresses,
                       firewall, nat, policy, protocols (static, bgp, ospf), service (ssh, dhcp-server, dns, ntp, lldp,
                       snmp, …), system (host name, users and their keys, name servers, time zone, syslog, …), vpn
                       ipsec/wireguard with keys in the configuration, pki
    not transferable   files of the old VM (/config/…, /home/…): openvpn, certificates or scripts kept on disk;
                       pppoe, wireless and other interfaces a VM does not have; an ethN the old VM has no NIC for;
                       an address on ethN that is not the old NIC's address
    dropped            hw-id lines (new MAC addresses)
"""
import ipaddress
import re
import shlex

PORTABLE_TOP = {"firewall", "nat", "policy", "protocols", "service", "system", "vpn", "pki", "interfaces", "high-availability",
                "load-balancing", "qos", "traffic-policy", "vrf", "container"}
PORTABLE_IFACES = {"ethernet", "loopback", "bridge", "dummy", "wireguard", "vti", "tunnel", "bonding", "input"}
NOT_PORTABLE_IFACES = {"openvpn": "openvpn keeps its keys in files", "pppoe": "PPPoE", "wireless": "wireless",
                       "wwan": "mobile modem", "sstpc": "SSTP client", "l2tpv3": "L2TPv3", "macsec": "MACsec"}


def parse(text):
    """[(words, line)] of the set commands of a configuration (other lines are ignored)."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("set "):
            continue
        try:
            words = shlex.split(line)
        except ValueError:
            words = line.split()
        out.append((words[1:], line))
    return out


def check(text, nics):
    """{"transferable", "problems", "warnings", "commands", "kept"} for a configuration and the old VM's NICs
    [{"iface": "eth0", "ip": "198.19.182.38", "network": "tenant_x-main_dc2"}]."""
    cmds = parse(text)
    problems, warnings, kept = [], [], []
    by_iface = {n["iface"]: n for n in nics}
    if not cmds:
        problems.append("no set commands: upload the output of `show configuration commands`")
    seen_eth = set()
    for words, line in cmds:
        if not words:
            continue
        top = words[0]
        if top not in PORTABLE_TOP:
            problems.append(f"{top}: not known to the portal ({line[:80]})")
            continue
        if any(re.match(r"^/(config|home|opt|root|tmp|var)/", w) for w in words[1:]):
            problems.append(f"refers to a file of the old VM: {line[:100]}")
            continue
        if top == "interfaces" and len(words) > 1:
            kind = words[1]
            if kind in NOT_PORTABLE_IFACES:
                problems.append(f"interfaces {kind}: {NOT_PORTABLE_IFACES[kind]}, not transferable")
                continue
            if kind not in PORTABLE_IFACES:
                problems.append(f"interfaces {kind}: not known to the portal")
                continue
            if kind == "ethernet" and len(words) > 2:
                eth = words[2]
                seen_eth.add(eth)
                if eth not in by_iface:
                    problems.append(f"{eth}: the old VM has no NIC for it")
                    continue
                if len(words) > 3 and words[3] == "hw-id":
                    continue  # the new VM has new MAC addresses
                if len(words) > 4 and words[3] == "address" and words[4] not in ("dhcp", "dhcpv6"):
                    try:
                        ip = str(ipaddress.ip_interface(words[4]).ip)
                    except ValueError:
                        ip = ""
                    if by_iface[eth].get("ip") and ip and ip != by_iface[eth]["ip"]:
                        problems.append(f"{eth}: address {ip} is not the old NIC's {by_iface[eth]['ip']}")
                        continue
        kept.append(line)
    for n in nics:
        if n["iface"] not in seen_eth:
            warnings.append(f"{n['iface']} ({n.get('network') or '?'}, {n.get('ip') or 'no address'}) is not configured")
    return {"transferable": not problems, "problems": problems, "warnings": warnings, "commands": len(cmds),
            "kept": kept}


def login_user(text):
    """(user, "type key") of the first login user with a public key in the configuration, or (None, None)."""
    users = {}
    for words, _line in parse(text):
        if words[:3] == ["system", "login", "user"] and len(words) >= 9 and words[4:6] == ["authentication", "public-keys"]:
            u = users.setdefault(words[3], {})
            u[words[7]] = words[8]
    for name, k in users.items():
        if k.get("key") and k.get("type"):
            return name, f"{k['type']} {k['key']}"
    return None, None
