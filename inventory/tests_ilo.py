"""The iLO steps of a host deployment (T25 §B1) against a small fake iLO 6: power and its wait, the two virtual drives
by type, the boot override, the BIOS target as one pending change (defaults + NIC boot off + the profile + the time
zone), local storage (RAID5 / RAID1, never one drive, the boot controller untouched), and the iLO's settings that never
carry its address."""

import copy
from unittest import mock

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

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
            raise RedfishError(f"GET {path}: HTTP 404")
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
        c.d[ilo.ETH]["HostName"] = "mh"
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
        c.d[ilo.ETH]["HostName"] = "mh"
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

    def test_storage_is_read_after_a_start_into_the_bios_setup(self):
        """2026-11-19 on a test host: iLO 6 lists no storage while the server is off, and "not ready" during POST."""
        from inventory.redfish import RedfishError

        c = FakeIlo()
        c.d[SYS]["PowerState"] = "Off"
        storage = c.d[f"{SYS}/Storage"]
        real_get = c.get
        polls = []

        def get(path):
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
        c.get = lambda path: {"Members": []} if path == f"{SYS}/Storage" else real_get(path)
        with self.assertRaisesMessage(ilo.IloError, "storage read by iLO"):
            ilo.discover_storage(c, log=lambda m: None, timeout=20, sleep=no_sleep)

    def test_syslog_and_ntp_where_ilo_6_keeps_them(self):
        """2026-11-19 on a test host: iLO 6 refuses syslog on Managers/1 (it lives under NetworkProtocol) and holds
        two NTP servers."""
        from inventory.redfish import Client

        c = FakeIlo()
        c.d[f"{MGR}/DateTime"].setdefault("TimeZone", {})
        Client.apply(c, ntp=["192.0.2.1", "192.0.2.2", "192.0.2.3"], syslog={"host": "log.example.com", "port": 15141})
        patched = {p: b for m, p, b in c.calls if m == "PATCH"}
        self.assertNotIn(MGR, patched)
        self.assertEqual(
            patched[f"{MGR}/NetworkProtocol"]["Oem"]["Hpe"],
            {"RemoteSyslogEnabled": True, "RemoteSyslogServer": "log.example.com", "RemoteSyslogPort": 15141},
        )
        self.assertEqual(patched[f"{MGR}/DateTime"]["StaticNTPServers"], ["192.0.2.1", "192.0.2.2"])
        c.d[f"{MGR}/NetworkProtocol"]["Oem"] = {
            "Hpe": {"RemoteSyslogEnabled": True, "RemoteSyslogServer": "log.example.com", "RemoteSyslogPort": 15141}
        }
        self.assertEqual(Client.settings(c)["syslog"], {"host": "log.example.com", "port": 15141})

    def test_a_refused_syslog_is_a_warning(self):
        """owner 2026-11-19 14:22-14:23: "syslog set error is minor problem" - the run goes on."""
        from inventory.redfish import RedfishError

        c = FakeIlo()
        c.d[ilo.ETH]["HostName"] = "mh"
        c.d[f"{MGR}/NetworkProtocol"]["SSDP"] = {"ProtocolEnabled": False}
        real = c.patch

        def patch(path, body):
            if "RemoteSyslogServer" in str(body):
                raise RedfishError(f"PATCH {path}: HTTP 400 iLO.2.37.PropertyNotWritableOrUnknown")
            return real(path, body)

        c.patch = patch
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
        c.d[ilo.ETH]["HostName"] = "mh"
        c.d[f"{MGR}/NetworkProtocol"]["SSDP"] = {"ProtocolEnabled": False}
        self.assertFalse(ilo.ilo_settings(c, "h", "", {"ssh": True}))
        c2 = FakeIlo()
        c2.d[ilo.ETH]["HostName"] = "mh"
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
        from inventory.models import Site

        Site.objects.create(code="dc1", ntp_servers=["192.0.2.1", "192.0.2.2", "192.0.2.3"])
        sa = Base.staff(self, "sa@corp.kz", is_super_admin=True)
        self.client.force_login(sa)
        r = self.client.get(reverse("infra_site_ipmi", args=["dc1"]))
        self.assertEqual(r.context["desired"]["ntp"], ["192.0.2.1", "192.0.2.2"])


class SyslogRefusals(SimpleTestCase):
    """commit-186: an iLO that refuses syslog is a warning; this portal node refusing the write stops the step."""

    def _run(self, exc):
        c = FakeIlo()
        real = c.patch

        def patch(path, body):
            if "RemoteSyslogServer" in str(body):
                raise exc
            return real(path, body)

        c.patch = patch
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
        real_patch, real_get = c.patch, c.get
        state = {"refused": False}

        def patch(path, body):
            if "RemoteSyslogServer" in str(body):
                state["refused"] = True
                raise WriteRefused("refused on this portal node (standby)")
            return real_patch(path, body)

        def get(path):
            if state["refused"]:
                raise RedfishError("GET: timed out")
            return real_get(path)

        c.patch, c.get = patch, get
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
