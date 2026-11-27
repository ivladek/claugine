"""The iLO steps of a host deployment (T25 §B1) against a small fake iLO 6: power and its wait, the two virtual drives
by type, the boot override, the BIOS target as one pending change (defaults + NIC boot off + the profile + the time
zone), local storage (RAID5 / RAID1, never one drive, the boot controller untouched), and the iLO's settings that never
carry its address."""

import copy
from unittest import mock

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from inventory import ilo
from inventory.redfish import RedfishError, WriteRefused

SYS, MGR = ilo.SYS, ilo.MGR


class FakeIlo:
    def __init__(self, data_drives=3):
        self.calls = []
        drives = [f"{SYS}/Storage/DE00A000/Drives/{i}" for i in range(data_drives)]
        self.d = {
            SYS: {"PowerState": "On", "Boot": {}, "BootProgress": {"LastState": "OSRunning"}},
            MGR: {"Status": {"State": "Enabled"}},
            ilo.ETH: {
                "HostName": "old",
                "IPv4Addresses": [
                    {"Address": "10.201.201.14", "SubnetMask": "255.255.255.0", "Gateway": "10.201.201.1"}
                ],
                "Oem": {"Hpe": {"DomainName": ""}},
            },
            f"{MGR}/NetworkProtocol": {
                "SSH": {"ProtocolEnabled": True},
                "IPMI": {"ProtocolEnabled": False},
                "SSDP": {"ProtocolEnabled": True},
                "SNMP": {"ProtocolEnabled": True},
            },
            f"{MGR}/DateTime": {},
            "/redfish/v1/EventService/Subscriptions": {"Members": []},
            f"{MGR}/DedicatedNetworkPorts": {"Members": [{"@odata.id": f"{MGR}/DedicatedNetworkPorts/1"}]},
            f"{MGR}/DedicatedNetworkPorts/1": {"Ethernet": {"LLDPEnabled": True}},
            f"{MGR}/VirtualMedia": {
                "Members": [{"@odata.id": f"{MGR}/VirtualMedia/1"}, {"@odata.id": f"{MGR}/VirtualMedia/2"}]
            },
            f"{MGR}/VirtualMedia/1": {"Id": "1", "MediaTypes": ["Floppy", "USBStick"], "Image": "", "Inserted": False},
            f"{MGR}/VirtualMedia/2": {"Id": "2", "MediaTypes": ["CD", "DVD"], "Image": "", "Inserted": False},
            f"{SYS}/Bios": {
                "Attributes": {
                    "AutoPowerOn": "AlwaysPowerOn",
                    "Slot1NicBoot1": "NetworkBoot",
                    "TimeZone": "Utc0",
                    "WorkloadProfile": "GeneralPowerEfficientCompute",
                }
            },
            f"{SYS}/Bios/Settings": {"Attributes": {}},
            f"{SYS}/Bios/oem/hpe/baseconfigs": {
                "BaseConfigs": [
                    {
                        "default": {
                            "AutoPowerOn": "AlwaysPowerOn",
                            "Slot1NicBoot1": "NetworkBoot",
                            "TimeZone": "Unspecified",
                            "WorkloadProfile": "GeneralPowerEfficientCompute",
                        }
                    }
                ]
            },
            f"{SYS}/Storage": {
                "Members": [{"@odata.id": f"{SYS}/Storage/DE009000"}, {"@odata.id": f"{SYS}/Storage/DE00A000"}]
            },
            f"{SYS}/Storage/DE009000": {
                "Id": "DE009000",
                "Name": "HPE NS204i-u Gen11 Boot Controller",
                "Drives": [{"@odata.id": "b1"}, {"@odata.id": "b2"}],
                "Volumes": {"@odata.id": f"{SYS}/Storage/DE009000/Volumes"},
            },
            f"{SYS}/Storage/DE009000/Volumes": {"Members": [{"@odata.id": f"{SYS}/Storage/DE009000/Volumes/1"}]},
            f"{SYS}/Storage/DE009000/Volumes/1": {"RAIDType": "RAID1", "CapacityBytes": 480 * 10**9},
            f"{SYS}/Storage/DE00A000": {
                "Id": "DE00A000",
                "Name": "HPE MR416i-o Gen11",
                "Drives": [{"@odata.id": x} for x in drives],
                "Volumes": {"@odata.id": f"{SYS}/Storage/DE00A000/Volumes"},
            },
            f"{SYS}/Storage/DE00A000/Volumes": {"Members": [{"@odata.id": f"{SYS}/Storage/DE00A000/Volumes/239"}]},
            f"{SYS}/Storage/DE00A000/Volumes/239": {"RAIDType": "RAID0", "CapacityBytes": 10**12},
        }

    def get(self, path):
        if path not in self.d:
            raise RedfishError(f"GET {path}: HTTP 404", status=404)
        return copy.deepcopy(self.d[path])

    def patch(self, path, body):
        self.calls.append(("PATCH", path, body))
        if path == SYS:
            self.d[SYS]["Boot"].update(body.get("Boot") or {})
        elif path == ilo.ETH:
            self.d[path]["HostName"] = body.get("HostName", self.d[path]["HostName"])
        elif path == f"{SYS}/Bios/Settings":
            self.d[path]["Attributes"].update(body["Attributes"])
        elif path == f"{MGR}/NetworkProtocol":
            for k, v in body.items():
                self.d[path][k] = v
        return {}

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        if path.endswith("ComputerSystem.Reset"):
            self.d[SYS]["PowerState"] = "On" if body["ResetType"] == "On" else "Off"
        elif path.endswith("VirtualMedia.InsertMedia"):
            vm = self.d[path.split("/Actions")[0]]
            vm.update(Image=body["Image"], Inserted=True)
        elif path.endswith("VirtualMedia.EjectMedia"):
            vm = self.d[path.split("/Actions")[0]]
            vm.update(Image="", Inserted=False)
        elif path.endswith("/Subscriptions"):
            n = f"{path}/{len(self.d.setdefault(path, {'Members': []})['Members']) + 1}"
            self.d[path]["Members"].append({"@odata.id": n})
            self.d[n] = dict(body)
        elif path.endswith("/Volumes"):
            self.d[path]["Members"].append({"@odata.id": path + "/new"})
            self.d[path + "/new"] = {
                "RAIDType": body["RAIDType"],
                "CapacityBytes": 1,
                "Links": {"Drives": list(body["Links"]["Drives"])},
            }
        return {}

    def delete(self, path):
        self.calls.append(("DELETE", path, None))
        coll = path.rsplit("/", 1)[0]
        self.d[coll]["Members"] = [m for m in self.d[coll]["Members"] if m["@odata.id"] != path]
        del self.d[path]
        return {}


def no_sleep(_s):
    pass


class Steps(SimpleTestCase):
    def test_power(self):
        c = FakeIlo()
        self.assertEqual(ilo.power(c, on=False, sleep=no_sleep), "Off")
        self.assertEqual(c.calls[-1][2], {"ResetType": "ForceOff"})
        n = len(c.calls)
        ilo.power(c, on=False, sleep=no_sleep)
        self.assertEqual(len(c.calls), n)  # already off

    def test_a_wait_has_a_limit(self):
        with self.assertRaisesMessage(ilo.IloError, "still"):
            ilo._wait(lambda: "On", "Off", timeout=10, poll=5, sleep=no_sleep, what="power Off")

    def test_media_by_type_and_the_override(self):
        c = FakeIlo()
        self.assertEqual(ilo.insert(c, "cd", "http://10.201.101.25/hostmedia/u.iso"), "2")
        self.assertEqual(ilo.insert(c, "usb", "http://10.201.101.25/hostmedia/seed/x.iso"), "1")
        ilo.boot_cd_once(c)
        self.assertEqual(c.d[SYS]["Boot"], {"BootSourceOverrideTarget": "Cd", "BootSourceOverrideEnabled": "Once"})
        ilo.eject_all(c)
        self.assertEqual([s["inserted"] for s in ilo.media_slots(c)], [False, False])

    def test_bios_target_and_one_pending_change(self):
        c = FakeIlo()
        t = ilo.bios_target(
            ilo.bios_current(c),
            {"AutoPowerOn": "RestoreLastState", "WorkloadProfile": "Virtualization-MaxPerformance", "NotThere": "x"},
            ilo.tz_attr("+05:00"),
        )
        self.assertEqual(
            t,
            {
                "AutoPowerOn": "RestoreLastState",
                "Slot1NicBoot1": "Disabled",
                "TimeZone": "UtcP5",
                "WorkloadProfile": "Virtualization-MaxPerformance",
            },
        )
        change = ilo.bios_pending(c, t)
        self.assertEqual(c.calls[-1], ("PATCH", f"{SYS}/Bios/Settings", {"Attributes": change}))
        self.assertEqual(set(change), {"AutoPowerOn", "Slot1NicBoot1", "TimeZone", "WorkloadProfile"})
        self.assertEqual(
            [ilo.tz_attr(x) for x in ("-03:30", "+00:00", "+05:45", "x")], ["UtcM330", "Utc0", "UtcP545", ""]
        )

    def test_after_a_factory_reset_the_whole_target_is_staged(self):
        """Owner 2026-11-20 12:56: after the reset the iLO still showed the old values - equal to the target - so
        nothing was staged and the BIOS stayed at factory; with whole, every attribute of the target is staged."""
        c = FakeIlo()
        t = ilo.bios_target(ilo.bios_current(c), {"WorkloadProfile": "Virtualization-MaxPerformance"}, "")
        stale = {**ilo.bios_current(c), **t}  # what the iLO reported: the target from the first deployment
        self.assertEqual(ilo.bios_pending(c, t, stale), {})
        change = ilo.bios_pending(c, t, stale, whole=True)
        self.assertEqual(change, t)
        self.assertEqual(c.calls[-1], ("PATCH", f"{SYS}/Bios/Settings", {"Attributes": t}))

    def test_after_a_factory_reset_a_setting_left_out_of_the_pending_ones_stops_it(self):  # commit-219, core-064
        c = FakeIlo()
        t = ilo.bios_target(ilo.bios_current(c), {"WorkloadProfile": "Virtualization-MaxPerformance"}, "")
        stale = {**ilo.bios_current(c), **t}
        left_out = sorted(t)[0]
        with (
            mock.patch.object(ilo, "bios_staged", side_effect=[{}, {k: v for k, v in t.items() if k != left_out}]),
            self.assertRaisesMessage(ilo.IloError, left_out),
        ):
            ilo.bios_pending(c, t, stale, whole=True)

    def test_the_target_is_the_profile_over_the_bios_own(self):
        """Owner 2026-11-19 17:55: the factory set is not the target - values the hardware reports (TPM), settings the
        workload profile governs (ASPM, NUMA) and the rest stay the BIOS's own; only the profile, the NICs' boot and the
        time zone are set and checked."""
        current = {
            "TpmState": "PresentEnabled",
            "PcieGlobalAspm": "Enabled",
            "SubNumaClustering": "EnableSnc2",
            "Slot15NicBoot1": "NetworkBoot",
            "WorkloadProfile": "GeneralPowerEfficientCompute",
            "TimeZone": "Utc0",
        }
        t = ilo.bios_target(
            current, {"WorkloadProfile": "Virtualization-MaxPerformance", "Slot15NicBoot1": "NetworkBoot"}, "UtcP5"
        )  # owner 17:57: network boot off on every NIC, whatever a profile says
        self.assertEqual(
            t, {"Slot15NicBoot1": "Disabled", "WorkloadProfile": "Virtualization-MaxPerformance", "TimeZone": "UtcP5"}
        )

    def test_local_storage(self):
        for drives, raid in ((3, "RAID5"), (2, "RAID1"), (1, "")):
            c = FakeIlo(drives)
            done = ilo.local_storage(c, 480, log=lambda m: None, sleep=no_sleep)
            self.assertEqual(done, [("DE00A000", raid, drives, "done" if raid else "")])
            self.assertIn(("DELETE", f"{SYS}/Storage/DE00A000/Volumes/239", None), c.calls)
            self.assertFalse(any("DE009000" in p for _m, p, _b in c.calls))  # the boot controller untouched
            made = [b for m, p, b in c.calls if m == "POST" and p.endswith("/Volumes")]
            self.assertEqual([b["RAIDType"] for b in made], [raid] if raid else [])

    def test_the_ilo_settings_never_carry_its_address(self):
        c = FakeIlo()
        changed = ilo.ilo_settings(
            c,
            "kvm014",
            "example.cloud",
            {"ssh": True, "ipmi_lan": False},
            ntp=["10.201.202.15"],
            dns=["10.201.101.11"],
            tz_index=23,
            syslog={"host": "10.201.101.40", "port": 514},
        )
        self.assertTrue(changed)
        self.assertEqual(c.d[ilo.ETH]["HostName"], "mkvm014")
        self.assertEqual(c.d[f"{MGR}/NetworkProtocol"]["SSDP"], {"ProtocolEnabled": False})
        self.assertEqual(c.d[f"{MGR}/NetworkProtocol"]["SNMP"], {"ProtocolEnabled": True})  # kept
        for _m, _p, body in c.calls:
            self.assertNotIn("IPv4Addresses", str(body))
        with self.assertRaisesMessage(ilo.IloError, "carries DHCPv4"):
            ilo._no_address({"Oem": {"Hpe": {"DHCPv4": {"Enabled": True}}}})

    def test_an_address_that_changed_stops_it(self):
        c = FakeIlo()
        real = c.patch

        def patch(path, body):
            if path == ilo.ETH:
                c.d[ilo.ETH]["IPv4Addresses"][0]["Address"] = "10.201.201.99"
            return real(path, body)

        c.patch = patch
        with self.assertRaisesMessage(ilo.IloError, "address changed"):
            ilo.ilo_settings(c, "h", "", {"ssh": True})


class ReviewFixes(SimpleTestCase):
    """commit-128..132 (review of af7a8da)."""

    def test_the_boot_controller_must_be_known(self):
        c = FakeIlo()
        c.d[f"{SYS}/Storage/DE009000"]["Name"] = "Storage controller"  # commit-128: no boot controller named
        with self.assertRaisesMessage(ilo.IloError, "iLO names no boot controller"):
            ilo.local_storage(c, 480, log=lambda m: None, sleep=no_sleep)
        self.assertFalse([m for m, _p, _b in c.calls if m in ("DELETE", "POST")])
        c = FakeIlo()
        c.d[f"{SYS}/Storage/DE00A000/Volumes/239"]["CapacityBytes"] = 480 * 10**9  # two candidates, neither named
        c.d[f"{SYS}/Storage/DE009000"]["Name"] = "Storage controller"
        with self.assertRaisesMessage(ilo.IloError, "nothing is deleted"):
            ilo.local_storage(c, 480, log=lambda m: None, sleep=no_sleep)
        self.assertFalse([m for m, _p, _b in c.calls if m == "DELETE"])
        with self.assertRaisesMessage(ilo.IloError, "boot device size"):
            ilo.local_storage(FakeIlo(), 0, log=lambda m: None, sleep=no_sleep)

    def test_a_volume_not_listed_yet_is_pending(self):
        c = FakeIlo()
        real = c.post

        def post(path, body):
            if path.endswith("/Volumes"):
                c.calls.append(("POST", path, body))
                return {}  # accepted, made at the next POST
            return real(path, body)

        c.post = post
        done = ilo.local_storage(c, 480, timeout=10, log=lambda m: None, sleep=no_sleep)
        self.assertEqual(done, [("DE00A000", "RAID5", 3, "pending")])

    def test_a_named_boot_controller_without_the_boot_volume_stops_it(self):
        """commit-128 (verify): iLO's boot controller has no 480 GB volume, the data controller has one."""
        c = FakeIlo()
        c.d[f"{SYS}/Storage/DE009000/Volumes/1"]["CapacityBytes"] = 240 * 10**9
        c.d[f"{SYS}/Storage/DE00A000/Volumes/239"]["CapacityBytes"] = 480 * 10**9
        with self.assertRaisesMessage(ilo.IloError, "names DE009000 as boot"):
            ilo.local_storage(c, 480, log=lambda m: None, sleep=no_sleep)
        self.assertFalse([m for m, _p, _b in c.calls if m in ("DELETE", "POST")])

    def test_nothing_is_made_until_the_old_volume_is_gone(self):
        """commit-129 (verify)."""
        c = FakeIlo()
        c.delete = lambda path: c.calls.append(("DELETE", path, None)) or {}  # accepted, never done
        with self.assertRaisesMessage(ilo.IloError, "volumes deleted"):
            ilo.local_storage(c, 480, timeout=10, log=lambda m: None, sleep=no_sleep)
        self.assertFalse([m for m, _p, _b in c.calls if m == "POST"])

    def test_a_volume_over_other_drives_is_not_made(self):
        """commit-129 (verify): a RAID5 listed, but not over all the controller's drives."""
        c = FakeIlo()
        real = c.post

        def post(path, body):
            r = real(path, body)
            if path.endswith("/Volumes"):
                c.d[path + "/new"]["Links"]["Drives"] = c.d[path + "/new"]["Links"]["Drives"][:2]
            return r

        c.post = post
        done = ilo.local_storage(c, 480, timeout=10, log=lambda m: None, sleep=no_sleep)
        self.assertEqual(done, [("DE00A000", "RAID5", 3, "pending")])

    def test_unreadable_pending_bios_settings_stop_it(self):
        """commit-130 (verify)."""
        c = FakeIlo()
        del c.d[f"{SYS}/Bios/Settings"]
        with self.assertRaisesMessage(ilo.IloError, "could not be read"):
            ilo.bios_pending(c, {"AutoPowerOn": "RestoreLastState"})

    def test_the_dhcp_flags_alone_ask_for_a_restart_and_are_read_back(self):
        """commit-131 (verify)."""
        c = FakeIlo()
        c.d[ilo.ETH]["HostName"] = ilo.ilo_name("h")
        c.d[f"{MGR}/NetworkProtocol"]["SSDP"] = {"ProtocolEnabled": False}
        c.d[f"{MGR}/DateTime"]["StaticNTPServers"] = ["192.0.2.1", ""]
        c.d[ilo.ETH].setdefault("Oem", {}).setdefault("Hpe", {})["DHCPv4"] = {"UseNTPServers": True}
        real = c.patch

        def patch(path, body):
            if path == ilo.ETH:
                c.d[ilo.ETH]["Oem"]["Hpe"]["DHCPv4"].update(
                    ((body.get("Oem") or {}).get("Hpe") or {}).get("DHCPv4") or {}
                )
            return real(path, body)

        c.patch = patch
        self.assertTrue(ilo.ilo_settings(c, "h", "", {"ssh": True}, ntp=["192.0.2.1"]))
        self.assertEqual(ilo.ilo_settings_differ(c, "h", {"ssh": True}, ntp=["192.0.2.1"]), [])
        c.d[ilo.ETH]["Oem"]["Hpe"]["DHCPv4"]["UseNTPServers"] = True
        self.assertEqual(
            ilo.ilo_settings_differ(c, "h", {"ssh": True}, ntp=["192.0.2.1"]), ["NTP ['192.0.2.1'] (from DHCP)"]
        )

    def test_another_zone_of_the_same_offset_is_a_change_and_read_back(self):
        """commit-131 (verify)."""
        c = FakeIlo()
        c.d[ilo.ETH]["HostName"] = ilo.ilo_name("h")
        c.d[f"{MGR}/NetworkProtocol"]["SSDP"] = {"ProtocolEnabled": False}
        c.d[f"{MGR}/DateTime"]["TimeZone"] = {"Index": 20, "Name": "Asia/Almaty", "UtcOffset": "+05:00"}
        real = c.patch

        def patch(path, body):
            if path == f"{MGR}/DateTime" and "TimeZone" in body:
                c.d[path]["TimeZone"] = {
                    "Index": body["TimeZone"]["Index"],
                    "Name": "Asia/Tashkent",
                    "UtcOffset": "+05:00",
                }
            return real(path, body)

        c.patch = patch
        self.assertTrue(ilo.ilo_settings(c, "h", "", {"ssh": True}, tz_index=21))
        self.assertEqual(ilo.ilo_settings_differ(c, "h", {"ssh": True}, tz_index=21), [])
        self.assertEqual(ilo.ilo_settings_differ(c, "h", {"ssh": True}, tz_index=20), ["time zone 'Asia/Tashkent'"])

    def test_a_half_built_listing_is_waited_out(self):  # owner 2026-11-20 13:50
        c = FakeIlo()
        c.d[SYS]["PowerState"] = "Off"
        real_get, seen, reads = c.get, [], []

        def get(path):
            out = real_get(path)
            if path == SYS and c.d[SYS]["PowerState"] == "On":
                seen.append(1)  # POST: the BIOS setup is entered only after a few looks
                state = "SetupEntered" if len(seen) > 3 else "PCIResourceConfigStarted"
                out = {**out, "BootProgress": {"LastState": state}}
            if path.startswith(f"{SYS}/Storage/") and "/" not in path[len(f"{SYS}/Storage/") :]:
                reads.append(1)
                if len(reads) < 4:
                    out = {**out, "Name": "", "StorageControllers": [{}]}  # listed before its name
            return out

        c.get = get
        ctls = ilo.discover_storage(c, log=lambda m: None, sleep=no_sleep)
        self.assertTrue(all(x["name"] for x in ctls))
        self.assertTrue(any(x["boot"] for x in ctls))
        self.assertGreater(len(seen), 3)
        self.assertGreaterEqual(len(reads), 4)

    def test_a_named_subset_during_post_is_not_taken_with_the_host_on(self):  # commit-220, core-065
        c = FakeIlo()  # on, in POST: iLO lists only the boot controller so far
        full = c.d[f"{SYS}/Storage"]
        c.d[SYS]["BootProgress"] = {"LastState": "PCIResourceConfigStarted"}
        boot_only = {**full, "Members": [m for m in full["Members"] if "DE009000" in m["@odata.id"]]}
        real_get, looks = c.get, []

        def get(path):
            if path == SYS:
                looks.append(1)
                if len(looks) > 3:
                    c.d[SYS]["BootProgress"] = {"LastState": "SetupEntered"}
            if path == f"{SYS}/Storage" and c.d[SYS]["BootProgress"]["LastState"] != "SetupEntered":
                return boot_only
            return real_get(path)

        c.get = get
        ctls = ilo.discover_storage(c, log=lambda m: None, sleep=no_sleep)
        self.assertEqual(sorted(x["id"] for x in ctls), ["DE009000", "DE00A000"])  # the data controller too
        self.assertFalse([b for m, _p, b in c.calls if m == "PATCH"])  # it was on: not started again

    def test_an_os_boot_instead_of_the_bios_setup_stops_it(self):  # commit-221, core-066
        c = FakeIlo()
        c.d[SYS]["PowerState"] = "Off"
        c.d[SYS]["BootProgress"] = {"LastState": "OSRunning"}  # the override did not take: the installed OS starts
        with self.assertRaisesMessage(ilo.IloError, "instead of the BIOS setup"):
            ilo.discover_storage(c, log=lambda m: None, sleep=no_sleep)
        self.assertFalse([m for m, _p, _b in c.calls if m == "DELETE"])

    def test_no_boot_controller_names_what_it_saw(self):  # owner 2026-11-20 13:50
        with self.assertRaisesMessage(ilo.IloError, "it lists: X1 'Some Array'"):
            ilo.boot_controller(None, [{"id": "X1", "name": "Some Array", "boot": False, "volumes": []}], 480)

    def test_storage_is_read_after_a_start_into_the_bios_setup(self):
        """2026-11-19 on a test host: iLO 6 lists no storage while the server is off, and "not ready" during POST."""
        from inventory.redfish import RedfishError

        c = FakeIlo()
        c.d[SYS]["PowerState"] = "Off"
        storage = c.d[f"{SYS}/Storage"]
        real_get = c.get
        polls = []

        def get(path):
            if path == SYS and c.d[SYS]["PowerState"] == "On":
                c.d[SYS]["BootProgress"] = {"LastState": "SetupEntered"}
            if path == f"{SYS}/Storage":
                if c.d[SYS]["PowerState"] == "Off":
                    return {"Members": []}
                polls.append(1)
                if len(polls) < 3:
                    raise RedfishError(f"GET {path}: HTTP 400 iLO.2.37.ResourceNotReadyRetry")
                return storage
            return real_get(path)

        c.get = get
        ctls = ilo.discover_storage(c, log=lambda m: None, sleep=no_sleep)
        self.assertEqual(sorted(x["id"] for x in ctls), ["DE009000", "DE00A000"])
        self.assertIn(
            ("PATCH", SYS, {"Boot": {"BootSourceOverrideEnabled": "Once", "BootSourceOverrideTarget": "BiosSetup"}}),
            c.calls,
        )
        self.assertEqual(c.d[SYS]["PowerState"], "On")
        c2 = FakeIlo()  # listed already (on, discovered): nothing is started
        self.assertTrue(ilo.discover_storage(c2, log=lambda m: None, sleep=no_sleep))
        self.assertFalse([m for m, _p, _b in c2.calls])

    def test_storage_that_never_comes_stops_it(self):
        c = FakeIlo()
        c.d[SYS]["PowerState"] = "Off"
        real_get = c.get
        c.d[SYS]["BootProgress"] = {"LastState": "SetupEntered"}  # in the BIOS setup, but no storage listed
        c.get = lambda path: {"Members": []} if path == f"{SYS}/Storage" else real_get(path)
        with self.assertRaisesMessage(ilo.IloError, "storage read by iLO"):
            ilo.discover_storage(c, log=lambda m: None, timeout=20, sleep=no_sleep)

    def test_syslog_and_ntp_where_ilo_6_keeps_them(self):
        """2026-11-19 on a test host: iLO 6 holds two NTP servers. Owner 2026-11-20 (the hosts compared): remote
        syslog as ONE EventService subscription, the legacy field off (it makes a "legacy_udp" subscription of its
        own); another destination or a second subscription is removed."""
        from inventory.redfish import SUBSCRIPTIONS, Client

        c = FakeIlo()
        c.d[f"{MGR}/DateTime"].setdefault("TimeZone", {})
        c.d[f"{MGR}/NetworkProtocol"]["Oem"] = {"Hpe": {"RemoteSyslogEnabled": True, "RemoteSyslogServer": "old"}}
        c.d[SUBSCRIPTIONS] = {"Members": [{"@odata.id": f"{SUBSCRIPTIONS}/1"}]}
        c.d[f"{SUBSCRIPTIONS}/1"] = {"SubscriptionType": "Syslog", "Destination": "syslog://old:514"}
        Client.apply(c, ntp=["192.0.2.1", "192.0.2.2", "192.0.2.3"], syslog={"host": "log.example.com", "port": 15141})
        patched = {p: b for m, p, b in c.calls if m == "PATCH"}
        self.assertEqual(patched[f"{MGR}/NetworkProtocol"]["Oem"]["Hpe"], {"RemoteSyslogEnabled": False})
        self.assertIn(("DELETE", f"{SUBSCRIPTIONS}/1", None), c.calls)
        posted = [b for m, p, b in c.calls if m == "POST" and p == SUBSCRIPTIONS]
        self.assertEqual(
            posted,
            [
                {
                    "Destination": "syslog://log.example.com:15141",
                    "SubscriptionType": "Syslog",
                    "Protocol": "SyslogUDP",
                    "SyslogFilters": [{"LogFacilities": [], "LowestSeverity": "Emergency"}],
                }
            ],
        )
        self.assertEqual(patched[f"{MGR}/DateTime"]["StaticNTPServers"], ["192.0.2.1", "192.0.2.2"])
        c.d[f"{MGR}/NetworkProtocol"]["Oem"] = {"Hpe": {"RemoteSyslogEnabled": False}}
        self.assertEqual(
            Client.settings(c)["syslog"], {"host": "log.example.com", "port": 15141, "count": 1, "legacy": False}
        )
        calls = len(c.calls)
        Client.apply(c, syslog={"host": "log.example.com", "port": 15141})  # as it should be: nothing written
        self.assertFalse([m for m, _p, _b in c.calls[calls:] if m != "GET"])

    def test_a_refused_syslog_is_a_warning(self):
        """owner 2026-11-19 14:22-14:23: "syslog set error is minor problem" - the run goes on."""
        from inventory.redfish import RedfishError

        c = FakeIlo()
        c.d[ilo.ETH]["HostName"] = ilo.ilo_name("h")
        c.d[f"{MGR}/NetworkProtocol"]["SSDP"] = {"ProtocolEnabled": False}
        real = c.post

        def post(path, body):
            if "syslog://" in str(body):
                raise RedfishError(f"POST {path}: HTTP 400 iLO.2.37.PropertyNotWritableOrUnknown")
            return real(path, body)

        c.post = post
        seen = []
        ilo.ilo_settings(c, "h", "", {"ssh": True}, syslog={"host": "log.example.com", "port": 514}, log=seen.append)
        self.assertTrue(any("remote syslog not set" in m for m in seen))

    def test_a_different_pending_value_is_replaced(self):
        c = FakeIlo()
        c.d[f"{SYS}/Bios/Settings"]["Attributes"] = {"AutoPowerOn": "AlwaysPowerOff"}
        c.d[f"{SYS}/Bios"]["Attributes"]["AutoPowerOn"] = "RestoreLastState"
        change = ilo.bios_pending(c, {"AutoPowerOn": "RestoreLastState"})
        self.assertEqual(change, {"AutoPowerOn": "RestoreLastState"})

    def test_a_pending_value_that_does_not_take_stops_it(self):
        c = FakeIlo()
        c.patch = lambda path, body: {}  # the iLO ignores the change
        with self.assertRaisesMessage(ilo.IloError, "do not hold the target"):
            ilo.bios_pending(c, {"AutoPowerOn": "RestoreLastState"})

    def test_site_settings_alone_ask_for_a_restart(self):
        c = FakeIlo()
        c.d[ilo.ETH]["HostName"] = ilo.ilo_name("h")
        c.d[f"{MGR}/NetworkProtocol"]["SSDP"] = {"ProtocolEnabled": False}
        self.assertFalse(ilo.ilo_settings(c, "h", "", {"ssh": True}))
        c2 = FakeIlo()
        c2.d[ilo.ETH]["HostName"] = ilo.ilo_name("h")
        c2.d[f"{MGR}/NetworkProtocol"]["SSDP"] = {"ProtocolEnabled": False}
        real = c2.patch

        def patch(path, body):
            if path == f"{MGR}/DateTime":
                c2.d[path]["StaticNTPServers"] = body.get("StaticNTPServers")
            return real(path, body)

        c2.patch = patch
        self.assertTrue(ilo.ilo_settings(c2, "h", "", {"ssh": True}, ntp=["192.0.2.1"]))

    def test_the_address_is_checked_after_each_write(self):
        c = FakeIlo()
        real = c.patch

        def patch(path, body):
            r = real(path, body)
            if path == ilo.ETH:
                c.d[ilo.ETH]["IPv4Addresses"][0]["Gateway"] = "10.201.201.254"
            return r

        c.patch = patch
        with self.assertRaisesMessage(ilo.IloError, f"(after {ilo.ETH})"):
            ilo.ilo_settings(c, "h", "", {"ssh": True}, ntp=["192.0.2.1"])
        self.assertFalse([p for m, p, _b in c.calls if p.endswith("NetworkProtocol")])  # stopped at once


class SiteIpmiNtp(TestCase):
    """commit-185: the site's IPMI page shows and applies the two NTP servers an iLO holds."""

    def test_two_of_three(self):
        from accounts.tests import Base
        from inventory import hostservice
        from inventory.models import Host, Site

        s = Site.objects.create(code="dc1", ntp_servers=["198.51.100.9"])  # T36 B5d: the typed list is not read
        for i in (1, 2, 3):  # the site's time servers: its hosts with the time function (kvm-mgmt)
            Host.objects.create(
                site=s,
                name=f"t{i}",
                ip=f"192.0.2.{i}",
                service=hostservice.of_slot("mgmt"),
                time_served_at=timezone.now(),
                time_confirmed_by="peer",
            )
        sa = Base.staff(self, "sa@corp.kz", is_super_admin=True)
        self.client.force_login(sa)
        r = self.client.get(reverse("infra_site_ipmi", args=["dc1"]))
        self.assertEqual(r.context["desired"]["ntp"], ["192.0.2.1", "192.0.2.2"])


class SyslogRefusals(SimpleTestCase):
    """commit-186: an iLO that refuses syslog is a warning; this portal node refusing the write stops the step."""

    def _run(self, exc):
        c = FakeIlo()
        real = c.post

        def post(path, body):
            if "syslog://" in str(body):
                raise exc
            return real(path, body)

        c.post = post
        logged = []
        ilo.ilo_settings(c, "h", "", {"ssh": True}, syslog={"host": "192.0.2.40", "port": 514}, log=logged.append)
        return logged

    def test_the_ilo_refusing_is_a_warning(self):
        logged = self._run(RedfishError("PATCH: HTTP 400 PropertyNotWritableOrUnknown"))
        self.assertTrue(any("remote syslog not set" in m for m in logged))

    def test_this_node_refusing_stops_the_step(self):
        with self.assertRaises(WriteRefused):
            self._run(WriteRefused("refused on this portal node (standby)"))

    def test_the_client_refuses_with_its_own_error(self):
        from unittest import mock

        from accounts import ha
        from inventory.redfish import Client

        with mock.patch.object(ha, "role", return_value="standby"):
            with self.assertRaises(WriteRefused):
                Client("192.0.2.9", "u", "p").patch("/redfish/v1/x", {})


class SyslogRefusalReadback(SimpleTestCase):
    """commit-186: a refused write is not read back, so a failing read cannot turn the refusal into a warning."""

    def test_a_failing_read_after_the_refusal(self):
        c = FakeIlo()
        real_post, real_get = c.post, c.get
        state = {"refused": False}

        def post(path, body):
            if "syslog://" in str(body):
                state["refused"] = True
                raise WriteRefused("refused on this portal node (standby)")
            return real_post(path, body)

        def get(path):
            if state["refused"]:
                raise RedfishError("GET: timed out")
            return real_get(path)

        c.post, c.get = post, get
        with self.assertRaises(WriteRefused):
            ilo.ilo_settings(c, "h", "", {"ssh": True}, syslog={"host": "192.0.2.40"}, log=lambda m: None)


class ProfileBiosAlways(SimpleTestCase):
    """Owner 2026-11-19 17:57: the network boot and the time zone are not profile settings - always set, read-only."""

    def test_refused_in_a_profile(self):
        from django import forms

        from inventory.profiles import parse_bios

        self.assertEqual(parse_bios("EmbeddedIpxe = Disabled"), {"EmbeddedIpxe": "Disabled"})
        for line in ("Slot1NicBoot1 = NetworkBoot", "TimeZone = Utc0", "FlexLom1NicBoot1 = Disabled"):
            with self.assertRaises(forms.ValidationError):
                parse_bios(line)


class GuardedWrites(SimpleTestCase):  # commit-132
    def test_post_and_delete_are_checked_as_patch(self):
        c = FakeIlo()
        g = ilo._Guarded(c, ilo.ilo_address(c))
        real = c.post

        def post(path, body):
            r = real(path, body)
            c.d[ilo.ETH]["IPv4Addresses"][0]["Gateway"] = "10.201.201.254"
            return r

        c.post = post
        with self.assertRaisesMessage(ilo.IloError, "(after /x)"):
            g.post("/x", {})


class SyslogBatchB(SimpleTestCase):  # commit-226, commit-227, commit-228
    def test_destination_and_reads(self):
        from inventory import redfish

        self.assertEqual(redfish.parse_syslog_dest("syslog://log.example.com"), ("log.example.com", 514))
        self.assertEqual(redfish.parse_syslog_dest("syslog://log.example.com:15141"), ("log.example.com", 15141))
        c = FakeIlo()
        c.d[redfish.SUBSCRIPTIONS] = {"Members": [{"@odata.id": "/gone"}, {"@odata.id": "/s/1"}]}
        c.d["/s/1"] = {"SubscriptionType": "Syslog", "Destination": "syslog://a"}
        self.assertEqual(redfish.syslog_subscriptions(c), [("/s/1", "syslog://a", False)])  # a member gone is skipped
        del c.d[redfish.SUBSCRIPTIONS]
        self.assertEqual(redfish.syslog_subscriptions(c), [])
        with self.assertRaises(RedfishError):
            redfish.syslog_subscriptions(c, strict=True)  # before a write: not taken for an empty list

    def test_a_refused_post_leaves_the_old_config(self):
        from inventory.redfish import SUBSCRIPTIONS, Client

        c = FakeIlo()
        c.d[f"{MGR}/NetworkProtocol"]["Oem"] = {"Hpe": {"RemoteSyslogEnabled": True, "RemoteSyslogServer": "old"}}
        c.d[SUBSCRIPTIONS] = {"Members": [{"@odata.id": f"{SUBSCRIPTIONS}/1"}]}
        c.d[f"{SUBSCRIPTIONS}/1"] = {"SubscriptionType": "Syslog", "Destination": "syslog://old:514"}

        def post(path, body):
            raise RedfishError("POST: HTTP 400")

        c.post = post
        with self.assertRaises(RedfishError):
            Client.apply(c, syslog={"host": "new", "port": 514})
        self.assertFalse([m for m, _p, _b in c.calls if m in ("DELETE", "PATCH")])  # nothing removed

    def test_one_drift_rule(self):
        from inventory.redfish import syslog_drift

        want = {"host": "l", "port": 514}
        self.assertFalse(syslog_drift({"host": "l", "port": 514, "count": 1, "legacy": False}, want))
        self.assertTrue(syslog_drift({"host": "l", "port": 514, "count": 1, "legacy": True}, want))
        self.assertTrue(syslog_drift({"host": "l", "port": 514, "count": 2}, want))
        self.assertTrue(syslog_drift({"host": "x", "port": 514}, want))
        self.assertFalse(syslog_drift({}, {}))


class IloLldp(SimpleTestCase):  # commit-232
    def test_lldp_set_and_read_back(self):
        c = FakeIlo()
        c.d[f"{MGR}/DedicatedNetworkPorts/1"]["Ethernet"]["LLDPEnabled"] = False
        ilo.ilo_settings(c, "h", "", {"ssh": True})
        self.assertIn(("PATCH", f"{MGR}/DedicatedNetworkPorts/1", {"Ethernet": {"LLDPEnabled": True}}), c.calls)
        self.assertIn("LLDP off", ilo.ilo_settings_differ(c, "h", {"ssh": True}))  # the fake keeps it off
        del c.d[f"{MGR}/DedicatedNetworkPorts"]
        self.assertIn("LLDP not readable", ilo.ilo_settings_differ(c, "h", {"ssh": True}))


class SyslogRound2(SimpleTestCase):  # commit-226, 227, 235
    def _c(self, subs):
        from inventory.redfish import SUBSCRIPTIONS

        c = FakeIlo()
        c.d[SUBSCRIPTIONS] = {"Members": [{"@odata.id": f"{SUBSCRIPTIONS}/{i}"} for i in range(1, len(subs) + 1)]}
        for i, body in enumerate(subs, 1):
            c.d[f"{SUBSCRIPTIONS}/{i}"] = body
        return c

    def test_only_a_gone_member_is_skipped_before_a_write(self):
        from inventory import redfish

        c = self._c([{"SubscriptionType": "Syslog", "Destination": "syslog://a"}])
        real = c.get
        c.get = lambda p: (_ for _ in ()).throw(RedfishError("GET: timed out")) if p.endswith("/1") else real(p)
        with self.assertRaises(RedfishError):
            redfish.syslog_subscriptions(c, strict=True)
        self.assertEqual(redfish.syslog_subscriptions(c), [])

    def test_the_legacy_one_is_never_kept_and_ports_are_parsed(self):
        from inventory.redfish import SUBSCRIPTIONS, Client

        c = self._c(
            [
                {"SubscriptionType": "Syslog", "Destination": "syslog://l:514", "Context": "legacy_udp"},
                {"SubscriptionType": "Syslog", "Destination": "syslog://l"},
            ]
        )
        c.d[f"{MGR}/NetworkProtocol"]["Oem"] = {"Hpe": {"RemoteSyslogEnabled": True}}
        Client.apply(c, syslog={"host": "l", "port": 514})
        self.assertFalse([b for m, p, b in c.calls if m == "POST"])  # the portless one is kept (514)
        self.assertFalse([p for m, p, _b in c.calls if m == "DELETE"])  # the legacy one goes with its field
        self.assertIn(("PATCH", f"{MGR}/NetworkProtocol", {"Oem": {"Hpe": {"RemoteSyslogEnabled": False}}}), c.calls)
        self.assertTrue(SUBSCRIPTIONS)


class Strict404(SimpleTestCase):  # commit-226
    def test_only_the_status_counts(self):
        from inventory import redfish

        c = FakeIlo()
        c.d[redfish.SUBSCRIPTIONS] = {"Members": [{"@odata.id": "/m/1"}]}
        real = c.get

        def failing(err):
            def get(p):
                if p == "/m/1":
                    raise err
                return real(p)

            return get

        # a 500 whose device message reads like a 404 is not a member gone
        c.get = failing(RedfishError("GET /m/1: HTTP 500 Base.1.0.InternalError: HTTP 404", status=500))
        with self.assertRaises(RedfishError):
            redfish.syslog_subscriptions(c, strict=True)
        c.get = failing(RedfishError("GET /m/1: HTTP 404", status=404))
        self.assertEqual(redfish.syslog_subscriptions(c, strict=True), [])
        c.get = failing(RedfishError("https://ilo: timed out"))  # no HTTP answer at all
        with self.assertRaises(RedfishError):
            redfish.syslog_subscriptions(c, strict=True)

    def test_the_client_keeps_the_status(self):
        import io
        import urllib.error

        from inventory import redfish

        err = urllib.error.HTTPError("https://ilo/x", 404, "Not Found", {}, io.BytesIO(b"{}"))
        cl = redfish.Client.__new__(redfish.Client)
        cl.base, cl.auth, cl.timeout, cl.ctx = "https://ilo", "Basic x", 5, None
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RedfishError) as cm:
                cl.get("/x")
        self.assertEqual(cm.exception.status, 404)


class IncompleteSyslogRead(SimpleTestCase):  # commit-226 (98dc3d8-commit-verify-233cd99.md)
    def test_an_unread_subscription_is_drift(self):
        from inventory import redfish
        from inventory.redfish import Client

        c = FakeIlo()
        want = {"host": "log.example.com", "port": 15141}
        Client.apply(c, syslog=want)
        c.d[f"{MGR}/NetworkProtocol"]["Oem"] = {"Hpe": {"RemoteSyslogEnabled": False}}
        self.assertFalse(redfish.syslog_drift(Client.settings(c)["syslog"], want))
        c.d[redfish.SUBSCRIPTIONS]["Members"].append({"@odata.id": "/m/unread"})
        real = c.get

        def get(p):
            if p == "/m/unread":
                raise RedfishError("GET /m/unread: HTTP 500", status=500)
            return real(p)

        c.get = get
        got = Client.settings(c)["syslog"]
        self.assertEqual(got["incomplete"], 1)
        self.assertTrue(redfish.syslog_drift(got, want))  # the deployment's readback and the site planner
        unread = []
        self.assertEqual(len(redfish.syslog_subscriptions(c, unread=unread)), 1)
        self.assertEqual(unread, ["/m/unread"])
        with self.assertRaises(RedfishError):  # before a write: still refused
            redfish.syslog_subscriptions(c, strict=True)


class SiteViewIgnoresAnUnreadMember(SimpleTestCase):  # commit-241
    def _ilo(self):
        from inventory import redfish
        from inventory.redfish import Client

        c = FakeIlo()
        Client.apply(c, syslog={"host": "log.example.com", "port": 15141})
        c.d[f"{MGR}/NetworkProtocol"]["Oem"] = {"Hpe": {"RemoteSyslogEnabled": False}}
        return c, redfish, Client

    def _flaky(self, c, path):
        real = c.get

        def get(p):  # unreadable on the later reads
            if p == path:
                raise RedfishError(f"GET {path}: HTTP 503", status=503)
            return real(p)

        c.get = get

    def test_another_member_unread(self):
        c, redfish, Client = self._ilo()
        c.d[redfish.SUBSCRIPTIONS]["Members"].append({"@odata.id": "/m/flaky"})
        c.d["/m/flaky"] = {"SubscriptionType": "Event", "Destination": "https://x"}
        before = ilo._site_view(c)
        self._flaky(c, "/m/flaky")
        self.assertEqual(Client.settings(c)["syslog"]["incomplete"], 1)  # still reported
        self.assertFalse(ilo._site_changed(before, ilo._site_view(c)))

    def test_the_collectors_own_subscription_unread(self):
        c, redfish, Client = self._ilo()
        before = ilo._site_view(c)
        own = c.d[redfish.SUBSCRIPTIONS]["Members"][0]["@odata.id"]
        self._flaky(c, own)
        after = ilo._site_view(c)
        self.assertFalse(ilo._site_changed(before, after))  # host/port/count unknown: not a change
        self.assertFalse(ilo._site_changed(after, before))

    def test_a_real_change_still_counts(self):
        c, redfish, Client = self._ilo()
        before = ilo._site_view(c)
        c.d[f"{MGR}/DateTime"]["StaticNTPServers"] = ["192.0.2.9"]
        self.assertTrue(ilo._site_changed(before, ilo._site_view(c)))
        self.assertTrue(ilo._site_changed(None, before))


class ConfirmedSyslogWriteRestarts(SimpleTestCase):  # commit-244
    def test_a_replacement_then_an_incomplete_read(self):
        from inventory import redfish

        c = FakeIlo()
        c.d[f"{MGR}/NetworkProtocol"]["Oem"] = {"Hpe": {"RemoteSyslogEnabled": False}}
        old = {"host": "log.example.com", "port": 15141}
        ilo.ilo_settings(c, "h", "", {"ssh": True}, syslog=old)
        self.assertFalse(ilo.ilo_settings(c, "h", "", {"ssh": True}, syslog=old))  # nothing to write: no restart
        c.d[redfish.SUBSCRIPTIONS]["Members"].append({"@odata.id": "/m/other"})
        c.d["/m/other"] = {"SubscriptionType": "Event", "Destination": "https://x"}
        real_get, real_delete = c.get, c.delete
        state = {"deleted": False}

        def delete(p):  # once the old subscription is gone, an unrelated one stops answering
            state["deleted"] = True
            return real_delete(p)

        def get(p):
            if p == "/m/other" and state["deleted"]:
                raise RedfishError("GET /m/other: HTTP 503", status=503)
            return real_get(p)

        c.get, c.delete = get, delete
        new = {"host": "log2.example.com", "port": 514}
        self.assertTrue(ilo.ilo_settings(c, "h", "", {"ssh": True}, syslog=new))
        self.assertTrue(state["deleted"])


class RefusedSyslogWriteNoRestart(SimpleTestCase):  # commit-245
    def test_a_refused_first_post(self):
        c = FakeIlo()
        c.d[f"{MGR}/NetworkProtocol"]["Oem"] = {"Hpe": {"RemoteSyslogEnabled": False}}
        old = {"host": "log.example.com", "port": 15141}
        ilo.ilo_settings(c, "h", "", {"ssh": True}, syslog=old)
        self.assertFalse(ilo.ilo_settings(c, "h", "", {"ssh": True}, syslog=old))

        def post(path, body):
            raise RedfishError(f"POST {path}: HTTP 400 Base.1.0.PropertyValueNotInList", status=400)

        c.post = post
        logged = []
        new = {"host": "log2.example.com", "port": 514}
        self.assertFalse(ilo.ilo_settings(c, "h", "", {"ssh": True}, syslog=new, log=logged.append))
        self.assertIn("iLO remote syslog not set", logged[0])

    def test_a_write_with_no_answer_still_counts(self):
        g = ilo._Guarded(FakeIlo(), None)
        g.before = ilo.ilo_address(g.c)

        def post(path, body):
            raise RedfishError("https://ilo: timed out")

        g.c.post = post
        with self.assertRaises(RedfishError):
            g.post("/x", {})
        self.assertEqual(g.writes, ["/x"])


class ServerErrorAfterAWriteCounts(SimpleTestCase):  # commit-247
    def test_an_applied_post_answered_500_then_an_incomplete_read(self):
        from inventory import redfish

        c = FakeIlo()
        c.d[f"{MGR}/NetworkProtocol"]["Oem"] = {"Hpe": {"RemoteSyslogEnabled": False}}
        old = {"host": "log.example.com", "port": 15141}
        ilo.ilo_settings(c, "h", "", {"ssh": True}, syslog=old)
        c.d[redfish.SUBSCRIPTIONS]["Members"].append({"@odata.id": "/m/other"})
        c.d["/m/other"] = {"SubscriptionType": "Event", "Destination": "https://x"}
        real_post, real_get = c.post, c.get
        state = {"posted": False}

        def post(path, body):  # applied, then answered with a server error
            real_post(path, body)
            state["posted"] = True
            raise RedfishError(f"POST {path}: HTTP 500", status=500)

        def get(p):
            if p == "/m/other" and state["posted"]:
                raise RedfishError("GET /m/other: HTTP 503", status=503)
            return real_get(p)

        c.post, c.get = post, get
        logged = []
        new = {"host": "log2.example.com", "port": 514}
        self.assertTrue(ilo.ilo_settings(c, "h", "", {"ssh": True}, syslog=new, log=logged.append))
        self.assertIn("iLO remote syslog not set", logged[0])
