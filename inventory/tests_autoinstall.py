"""The answers file (T25 §B2): rendered from the records only, the way the hosts are today (a provider host's netplan):
the boot device with the disk layout, ports by MAC named by alias, the bonds, VLANs and bridges with the host's
addresses, the default route on the core network; users with keys only; offline; the CIDATA ISO."""

import json
import os
import shutil
import tempfile
import unittest

from django.test import TestCase

from inventory import autoinstall
from inventory.models import DiskLayout

PORTS = {
    "nic-data1": {"mac": "58:A2:E1:AD:D4:30", "mtu": 9100},
    "nic-data2": {"mac": "58:A2:E1:AD:D4:31", "mtu": 9100},
    "nic-storage1": {"name": "ens15f0np0", "mtu": 9000},
    "nic-storage2": {"name": "ens15f1np1", "mtu": 9000},
}
BONDS = [
    {"name": "bond0", "aliases": ["nic-data1", "nic-data2"], "mtu": 9100},
    {"name": "bond1", "aliases": ["nic-storage1", "nic-storage2"], "mtu": 9000},
]
NETS = [
    {
        "name": "one-core",
        "interface": "bond0",
        "vlan": 100,
        "mtu": 1500,
        "cidr": "10.201.100.0/24",
        "gateway": "10.201.100.1",
        "bridge": "one-core",
    },
    {
        "name": "cloud-vxlan",
        "interface": "bond0",
        "vlan": 102,
        "mtu": 9050,
        "cidr": "10.201.102.0/24",
        "gateway": None,
        "bridge": "vxlan",
    },
    {
        "name": "data-nfs",
        "interface": "bond1",
        "vlan": 111,
        "mtu": 9000,
        "cidr": "10.201.111.0/24",
        "gateway": None,
        "bridge": "data-nfs",
    },
]


class Render(TestCase):
    def net(self):
        return autoinstall.network(
            PORTS, BONDS, NETS, "10.201.100.14", ["10.201.101.11", "10.201.101.12"], ["example.cloud"]
        )

    def test_network_as_the_hosts_have_it(self):
        n = self.net()
        self.assertEqual(n["ethernets"]["nic-data1"]["match"], {"macaddress": "58:a2:e1:ad:d4:30"})
        self.assertEqual(n["ethernets"]["nic-storage1"]["match"], {"name": "ens15f0np0"})
        self.assertEqual(n["bonds"]["bond1"]["parameters"]["transmit-hash-policy"], "encap3+4")
        self.assertEqual(
            n["vlans"]["vlan-core"],
            {"id": 100, "link": "bond0", "mtu": 1500, "dhcp4": False, "dhcp6": False, "link-local": []},
        )
        self.assertEqual(sorted(n["vlans"]), ["vlan-core", "vlan-nfs", "vlan-vxlan"])
        self.assertEqual(n["bridges"]["vxlan"]["addresses"], ["10.201.102.14/24"])
        self.assertEqual(n["bridges"]["data-nfs"]["addresses"], ["10.201.111.14/24"])
        self.assertEqual(n["bridges"]["one-core"]["routes"], [{"to": "default", "via": "10.201.100.1", "metric": 50}])
        self.assertEqual(n["bridges"]["one-core"]["nameservers"]["search"], ["example.cloud"])
        self.assertNotIn("routes", n["bridges"]["vxlan"])

    def test_a_network_on_a_bond_the_profile_lacks(self):
        with self.assertRaisesMessage(autoinstall.AnswersError, "no bond bond1"):
            autoinstall.network(PORTS, BONDS[:1], NETS, "10.201.100.14", [], [])

    def test_storage(self):
        lay = DiskLayout.objects.get(code="standard")
        st = autoinstall.storage("/dev/nvme0n1", lay)["config"]
        lvs = [(x["name"], x.get("size")) for x in st if x["type"] == "lvm_partition"]
        self.assertEqual(lvs, [("lv_root", "50G"), ("lv_lib", "200G"), ("lv_log", "100G"), ("lv_home", None)])
        mounts = [x["path"] for x in st if x["type"] == "mount"]
        self.assertEqual(mounts[0], "/")
        self.assertLess(mounts.index("/boot"), mounts.index("/boot/efi"))
        self.assertEqual(st[0]["match"], {"path": "/dev/nvme0n1"})

    def test_user_data(self):
        lay = DiskLayout.objects.get(code="standard")
        text = autoinstall.user_data(
            "kvm014",
            "kvm014.example.cloud",
            "/dev/nvme0n1",
            lay,
            self.net(),
            "Asia/Almaty",
            "sa",
            ["ssh-ed25519 AAAA sa"],
            ["ssh-ed25519 AAAA sa", "ssh-ed25519 BBBB one"],
        )
        self.assertTrue(text.startswith("#cloud-config\n"))
        a = json.loads(text.split("\n", 1)[1])["autoinstall"]
        self.assertEqual(a["apt"], {"geoip": False, "fallback": "offline-install"})
        self.assertEqual(a["refresh-installer"], {"update": False})
        self.assertEqual(a["timezone"], "Asia/Almaty")
        users = {u["name"]: u for u in a["user-data"]["users"]}
        self.assertEqual(users["oneadmin"]["homedir"], "/var/lib/one")
        self.assertTrue(all(u["lock_passwd"] for u in users.values()))
        self.assertNotIn("PRIVATE", text)
        self.assertNotIn('passwd"', text.replace("lock_passwd", ""))
        with self.assertRaises(autoinstall.AnswersError):
            autoinstall.user_data("h", "h", "/dev/nvme0n1", lay, self.net(), "UTC", "sa", [], [])

    def test_host_address(self):
        self.assertEqual(
            autoinstall.host_address("10.201.100.13", "10.201.100.0/24", "10.201.105.0/24"), "10.201.105.13/24"
        )
        with self.assertRaises(autoinstall.AnswersError):
            autoinstall.host_address("10.201.100.255", "10.201.100.0/24", "10.201.105.0/25")

    @unittest.skipUnless(shutil.which("xorriso"), "xorriso is not installed here")
    def test_the_cidata_iso(self):
        d = tempfile.mkdtemp()
        try:
            data = autoinstall.make_iso("#cloud-config\n{}\n", autoinstall.meta_data("h"), os.path.join(d, "x.iso"))
            self.assertIn(b"CIDATA", data[:65536])
        finally:
            shutil.rmtree(d)


class ReviewFixes(TestCase):
    """commit-133..136, commit-141."""

    def test_two_networks_on_one_name_stop_it(self):
        nets = NETS + [dict(NETS[2], name="backup-nfs", vlan=113, cidr="10.201.113.0/24", bridge="backup-nfs")]
        with self.assertRaisesMessage(autoinstall.AnswersError, "data-nfs and backup-nfs would both be vlan-nfs"):
            autoinstall.network(PORTS, BONDS, nets, "10.201.100.14", [], [])

    def test_a_swap_volume_is_used(self):
        lay = DiskLayout(
            code="s",
            volumes=[
                {"path": "/", "size_gib": 50, "fs": "xfs"},
                {"path": "swap", "size_gib": 8, "fs": "swap"},
                {"path": "/home", "size_gib": None, "fs": "xfs"},
            ],
        )
        st = autoinstall.storage("/dev/nvme0n1", lay)
        self.assertIn({"type": "mount", "id": "mnt-lv_swap-fs", "device": "lv_swap-fs"}, st["config"])
        self.assertEqual(st["swap"], {"size": 0})
        self.assertNotIn("swap", autoinstall.storage("/dev/nvme0n1", DiskLayout.objects.get(code="standard")))

    def test_ring_buffers_and_br_netfilter(self):
        self.assertIn('KERNEL=="nic-*"', autoinstall.FILES["/etc/udev/rules.d/50-network-buffer.rules"])
        self.assertEqual(autoinstall.FILES["/etc/modules-load.d/br_netfilter.conf"], "br_netfilter\n")

    def test_the_run_marker(self):
        lay = DiskLayout.objects.get(code="standard")
        net = autoinstall.network(PORTS, BONDS, NETS, "10.201.100.14", [], [])
        text = autoinstall.user_data(
            "h", "h.x", "/dev/nvme0n1", lay, net, "UTC", "sa", ["k"], ["k"], marker="0123abcd4567ef89"
        )
        self.assertIn("echo 0123abcd4567ef89 > /target/etc/claugine-deploy", text)
        text = autoinstall.user_data(
            "h", "h.x", "/dev/nvme0n1", lay, net, "UTC", "sa", ["k"], ["k"], marker="x; rm -rf /"
        )
        self.assertNotIn("claugine-deploy", text)


class ReviewFixes2(TestCase):
    """commit-135, commit-136, commit-150 (owner 2026-11-19 08:04)."""

    def test_the_hosts_files_are_in_the_target_before_its_first_boot(self):
        import base64

        cmds = autoinstall.target_files()
        self.assertEqual(len(cmds), len(autoinstall.FILES))
        for path, text in autoinstall.FILES.items():
            b64 = base64.b64encode(text.encode()).decode()
            self.assertIn(f"echo {b64} | base64 -d > /target{path}", "\n".join(cmds))

    def test_one_spelling_of_each_mount_point(self):
        lay = DiskLayout(
            code="x",
            volumes=[
                {"path": "/var/lib/data", "size_gib": 10, "fs": "xfs"},
                {"path": "/var//lib/", "size_gib": 20, "fs": "xfs"},
                {"path": "/", "size_gib": None, "fs": "xfs"},
            ],
        )
        paths = [
            m["path"] for m in autoinstall.storage("/dev/sda", lay)["config"] if m["type"] == "mount" and "path" in m
        ]
        self.assertIn("/var/lib", paths)
        self.assertNotIn("/var//lib/", paths)
        self.assertLess(paths.index("/var/lib"), paths.index("/var/lib/data"))
