"""The answers file of a host deployment (owner 2026-11-18, T25 §B2): Ubuntu's autoinstall user-data rendered from the
portal's records only, delivered as a small ISO labelled CIDATA (08:40 "lets create only ISOs") beside the installer
ISO, from the node of the host's site (accounts/hostmedia.py), deleted when the host is deployed.

    disk      the profile's boot device only: GPT, EFI 512 MiB (/boot/efi), /boot 1 GiB (ext4), the rest one LVM
              volume group vg_data with the disk layout's volumes (lv_root, lv_lib, ...; inventory.models.DiskLayout)
    network   every port found by its MAC (read from the iLO; the predicted name when no MAC is known) and named by
              its alias (nic-data1 ...); the profile's bonds (802.3ad, fast, encap3+4, the cards' MTU); each host
              network as a VLAN on its bond and a bridge with the host's address (the same host part as its core
              address); the default route on the network with a gateway; the site's DNS and search domain
    users     the SA user (sudo, key only) and oneadmin (home /var/lib/one, the SA's key and the platform's key);
              no passwords, no private keys, no repository token in the file
    system    the site's time zone, en_US.UTF-8, the hosts' sysctl files (KVM tunables, IPv6 off, bridge netfilter)
              and NIC ring buffers' udev rule as they are on the hosts, fwupd off
    offline   12:58 "install from ISO must not required any connectivity to internet. just setup from isos only":
              no geoip, no installer refresh, no third-party drivers, the offline fallback; the full upgrade is the
              run's own step after the install (§B4.6)

The layout of the hosts as they are (read 2026-11-18 on a provider host, netplan 50-cloud-init.yaml) is what this writes:
VLAN interfaces vlan-<bridge's last word>, bridges named as the network's bridge.
"""

import base64
import ipaddress
import json
import os
import re
import subprocess
import tempfile

from .models import DiskLayout, mount_point

BOND_PARAMS = {
    "mode": "802.3ad",
    "mii-monitor-interval": "100",
    "lacp-rate": "fast",
    "transmit-hash-policy": "encap3+4",
}
# the files the hosts have (read 2026-11-18 on a provider host): /etc/sysctl.d and the NIC ring buffers' udev rule
FILES = {
    "/etc/sysctl.d/91-kvm-host.conf": """net.core.rmem_max = 134217728
net.core.wmem_max = 134217728
net.core.netdev_max_backlog = 250000
net.core.netdev_budget = 600
net.core.netdev_budget_usecs = 8000
net.ipv4.tcp_rmem = 4096 87380 67108864
net.ipv4.tcp_wmem = 4096 65536 67108864
net.ipv4.tcp_congestion_control = bbr
net.ipv4.tcp_mtu_probing = 1
net.ipv4.tcp_max_syn_backlog = 8192
net.ipv4.tcp_low_latency = 1
""",
    "/etc/sysctl.d/92-disable-ipv6.conf": """net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
""",
    "/etc/sysctl.d/bridge-nf-call.conf": """net.bridge.bridge-nf-call-arptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.bridge.bridge-nf-call-iptables = 1
""",
    "/etc/udev/rules.d/50-network-buffer.rules": (
        'ACTION=="add", SUBSYSTEM=="net", KERNEL=="ens*", RUN+="/usr/sbin/ethtool -G $name rx 8192 tx 8192"\n'
        # commit-135: the ports renamed to their aliases (nic-data1 ...) as well
        'ACTION=="add|move", SUBSYSTEM=="net", KERNEL=="nic-*", RUN+="/usr/sbin/ethtool -G $name rx 8192 tx 8192"\n'
    ),
    # commit-136: the bridge netfilter sysctls exist only with br_netfilter loaded
    "/etc/modules-load.d/br_netfilter.conf": "br_netfilter\n",
}
RING_MIN = 8192


class AnswersError(Exception):
    pass


def storage(boot_dev, layout):
    """curtin's storage config: the boot device only, the layout's logical volumes ("the rest" last)."""
    if not boot_dev:
        raise AnswersError("the profile names no boot device")
    if layout.problems():
        raise AnswersError(f"disk layout {layout.code}: {'; '.join(layout.problems())}")
    cfg = [
        {
            "type": "disk",
            "id": "disk0",
            "match": {"path": boot_dev},
            "ptable": "gpt",
            "wipe": "superblock-recursive",
            "preserve": False,
            "grub_device": False,
        },
        {
            "type": "partition",
            "id": "efi",
            "device": "disk0",
            "size": "512M",
            "flag": "boot",
            "grub_device": True,
            "number": 1,
        },
        {"type": "format", "id": "efi-fs", "volume": "efi", "fstype": "fat32"},
        {"type": "partition", "id": "boot", "device": "disk0", "size": "1G", "number": 2},
        {"type": "format", "id": "boot-fs", "volume": "boot", "fstype": "ext4"},
        {"type": "partition", "id": "pv", "device": "disk0", "size": -1, "number": 3},
        {"type": "lvm_volgroup", "id": "vg", "name": "vg_data", "devices": ["pv"]},
    ]
    mounts = [("efi-fs", "/boot/efi"), ("boot-fs", "/boot")]
    swap_mounts = []
    vols = sorted(layout.volumes or [], key=lambda v: v.get("size_gib") is None)
    for v in vols:
        path = mount_point(v["path"]) if v.get("fs") != "swap" else ""  # commit-150: one spelling, emitted and sorted
        lv = DiskLayout.lv_name(path or "swap")
        part = {"type": "lvm_partition", "id": lv, "name": lv, "volgroup": "vg"}
        if v.get("size_gib") is not None:
            part["size"] = f"{int(v['size_gib'])}G"
        cfg.append(part)
        cfg.append({"type": "format", "id": f"{lv}-fs", "volume": lv, "fstype": v.get("fs") or "xfs"})
        if v.get("fs") != "swap":
            mounts.append((f"{lv}-fs", path))
        else:  # commit-134: a swap volume is used (curtin puts it in fstab), and no swap file besides it
            swap_mounts.append({"type": "mount", "id": f"mnt-{lv}-fs", "device": f"{lv}-fs"})
    for fs, path in sorted(mounts, key=lambda m: m[1].count("/") if m[1] != "/" else 0):
        cfg.append({"type": "mount", "id": f"mnt-{fs}", "device": fs, "path": path})
    cfg += swap_mounts
    if swap_mounts:
        return {"config": cfg, "swap": {"size": 0}}
    return {"config": cfg}  # the installer's swap file, as the hosts have it (/swap.img)


def _vlan_name(bridge):
    return "vlan-" + bridge.split("-")[-1]


def host_address(core_ip, core_cidr, cidr):
    """The host's address on another network: the same host part as its core address ("10.201.100.13" on
    10.201.100.0/24 → 10.201.111.13 on 10.201.111.0/24)."""
    core = ipaddress.ip_network(core_cidr, strict=False)
    net = ipaddress.ip_network(cidr, strict=False)
    host = int(ipaddress.ip_address(core_ip)) - int(core.network_address)
    addr = ipaddress.ip_address(int(net.network_address) + host)
    if addr not in net or addr in (net.network_address, net.broadcast_address):
        raise AnswersError(f"{core_ip}: no host address on {cidr}")
    return f"{addr}/{net.prefixlen}"


def network(ports, bonds, nets, core_ip, dns, search):
    """netplan v2. ports {alias: {"mac" or "name", "mtu"}}; bonds [{name, aliases, mtu}]; nets [{name, interface,
    vlan, mtu, cidr, gateway, bridge}] with the core network (the one holding core_ip) among them."""
    core = next(
        (n for n in nets if ipaddress.ip_address(core_ip) in ipaddress.ip_network(n["cidr"], strict=False)), None
    )
    if core is None:
        raise AnswersError(f"{core_ip} is on none of the host's networks")
    eth = {}
    for alias, p in sorted(ports.items()):
        match = {"macaddress": p["mac"].lower()} if p.get("mac") else {"name": p["name"]}
        eth[alias] = {
            "match": match,
            "set-name": alias,
            "dhcp4": False,
            "dhcp6": False,
            "mtu": int(p["mtu"]),
            "link-local": [],
        }
    bnd = {}
    for b in bonds:
        bnd[b["name"]] = {
            "interfaces": list(b["aliases"]),
            "mtu": int(b["mtu"]),
            "parameters": dict(BOND_PARAMS),
            "dhcp4": False,
            "dhcp6": False,
            "link-local": [],
        }
    vlans, bridges, owner = {}, {}, {}
    for n in nets:  # commit-133: two networks giving one VLAN or bridge name stop it, naming both
        bridge = n.get("bridge") or n["name"]
        for key in (_vlan_name(bridge), bridge):
            if key in owner and owner[key] != n["name"]:
                raise AnswersError(f"networks {owner[key]} and {n['name']} would both be {key} on the host")
            owner[key] = n["name"]
    for n in nets:
        if n["interface"] not in bnd:
            raise AnswersError(f"network {n['name']}: no bond {n['interface']} on this profile")
        bridge = n.get("bridge") or n["name"]
        v = _vlan_name(bridge)
        vlans[v] = {
            "id": int(n["vlan"]),
            "link": n["interface"],
            "mtu": int(n["mtu"]),
            "dhcp4": False,
            "dhcp6": False,
            "link-local": [],
        }
        br = {
            "interfaces": [v],
            "addresses": [host_address(core_ip, core["cidr"], n["cidr"])],
            "mtu": int(n["mtu"]),
            "dhcp4": False,
            "dhcp6": False,
            "link-local": [],
        }
        if n is core:
            br["nameservers"] = {"addresses": list(dns), "search": list(search)}
            if n.get("gateway"):
                br["routes"] = [{"to": "default", "via": n["gateway"], "metric": 50}]
        bridges[bridge] = br
    return {"version": 2, "renderer": "networkd", "ethernets": eth, "bonds": bnd, "vlans": vlans, "bridges": bridges}


def target_files(files=None):
    """late-commands writing each file into /target (base64: no quoting)."""
    out = []
    for path, text in (FILES if files is None else files).items():
        b64 = base64.b64encode(text.encode()).decode()
        out.append(f"mkdir -p /target{os.path.dirname(path)} && echo {b64} | base64 -d > /target{path}")
    return out


def user_data(host_name, fqdn, boot_dev, layout, net, tz, sa_user, sa_keys, oneadmin_keys, marker=""):
    """The whole user-data (#cloud-config, autoinstall v1) as JSON - valid YAML, nothing to quote."""
    if not sa_user or not sa_keys:
        raise AnswersError("no SA user with a public key")
    cfg = {
        "autoinstall": {
            "version": 1,
            "interactive-sections": [],
            "refresh-installer": {"update": False},
            "locale": "en_US.UTF-8",
            "keyboard": {"layout": "us"},
            "timezone": tz,
            "network": net,
            "storage": storage(boot_dev, layout),
            "apt": {"geoip": False, "fallback": "offline-install"},
            "drivers": {"install": False},
            "codecs": {"install": False},
            "oem": {"install": False},
            "source": {"search_drivers": False},
            "ssh": {"install-server": True, "allow-pw": False},
            "late-commands": [
                "curtin in-target -- systemctl mask fwupd.service fwupd-refresh.timer || true",
            ]
            # commit-135/136: the hosts' files are in the installed system before its first boot, so udev, the module
            # loader and systemd-sysctl read them on that boot (cloud-init's write_files comes after them)
            + target_files()
            # commit-141: the run that made this answers file - its installation, not an older OS, is waited for
            + ([f"echo {marker} > /target/etc/claugine-deploy"] if re.match(r"^[0-9a-f]{8,64}$", marker or "") else []),
            "user-data": {
                "hostname": host_name,
                "fqdn": fqdn,
                "prefer_fqdn_over_hostname": False,
                "disable_root": True,
                "ssh_pwauth": False,
                "users": [
                    {
                        "name": sa_user,
                        "groups": ["sudo"],
                        "shell": "/bin/bash",
                        "lock_passwd": True,
                        "sudo": "ALL=(ALL) NOPASSWD:ALL",
                        "ssh_authorized_keys": list(sa_keys),
                    },
                    {
                        "name": "oneadmin",
                        "homedir": "/var/lib/one",
                        "shell": "/bin/bash",
                        "lock_passwd": True,
                        "ssh_authorized_keys": list(oneadmin_keys),
                    },
                ],
            },
            "shutdown": "reboot",
        }
    }
    return "#cloud-config\n" + json.dumps(cfg, indent=1) + "\n"


def meta_data(host_name):
    return f"instance-id: {host_name}\nlocal-hostname: {host_name}\n"


def make_iso(user, meta, out):
    """The CIDATA ISO (xorriso -as mkisofs: ISO 9660 + Joliet + Rock Ridge, volume label CIDATA)."""
    with tempfile.TemporaryDirectory() as d:
        for name, text in (("user-data", user), ("meta-data", meta)):
            with open(os.path.join(d, name), "w") as fh:
                fh.write(text)
        r = subprocess.run(
            ["xorriso", "-as", "mkisofs", "-quiet", "-volid", "CIDATA", "-joliet", "-rock", "-output", out, d],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if r.returncode != 0:
            raise AnswersError(f"xorriso: {r.stderr.strip()[-300:]}")
    with open(out, "rb") as fh:
        return fh.read()
