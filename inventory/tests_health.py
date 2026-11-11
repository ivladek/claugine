"""Hosts' health from the IPMI (2026-10-29) and the tenants' igw VMs found by address."""
from django.test import SimpleTestCase

from inventory import hosthealth, redfish
from inventory.models import Host, ServerProfile

ADAPTER = {"Model": "P42046-001 / Mellanox MCX631102AS-ADAT", "Ports": {"@odata.id": "{a}/Ports"}}


def port(i, mac, link="LinkUp", health="OK"):
    return {"Id": str(i), "PortId": str(i + 1), "LinkStatus": link, "Status": {"Health": health, "State": "Enabled"},
            "CurrentSpeedGbps": 25.0, "Ethernet": {"LLDPTransmit": {"ChassisId": mac}}}


def ilo(down=None, gpu_health="OK", boot_health="OK"):
    """A fake HPE iLO 6: two dual-port cards in slots 3 and 6, an OCP card without links, 2 CPUs and a GPU."""
    a3, a6, ocp = "/redfish/v1/Chassis/1/NetworkAdapters/A3", "/redfish/v1/Chassis/1/NetworkAdapters/A6", "/redfish/v1/Chassis/1/NetworkAdapters/O"
    data = {
        "/redfish/v1/Systems/1": {"BiosVersion": "A55 v3.16", "PowerState": "On", "Status": {"HealthRollup": "OK"},
                                  "MemorySummary": {"Status": {"HealthRollup": "OK"}}},
        "/redfish/v1/Managers/1": {"Model": "iLO 6", "FirmwareVersion": "iLO 6 v1.77"},
        "/redfish/v1/Systems/1/Processors": {"Members": [{"@odata.id": f"/p/{i}"} for i in (1, 2, 3)]},
        "/p/1": {"Id": "1", "ProcessorType": "CPU", "Status": {"Health": "OK", "State": "Enabled"}},
        "/p/2": {"Id": "2", "ProcessorType": "CPU", "Status": {"Health": "OK", "State": "Enabled"}},
        "/p/3": {"Id": "3", "ProcessorType": "GPU", "Model": "L40S", "Status": {"Health": gpu_health, "State": "Enabled"}},
        "/redfish/v1/Systems/1/PCIDevices": {"Members": [{"@odata.id": "/d/3"}, {"@odata.id": "/d/6"}, {"@odata.id": "/d/22"}]},
        "/d/22": {"DeviceType": "NIC", "Name": "Mellanox ConnectX-6 LX OCP3.0", "StructuredName": "OCP.Slot.22.1"},
        "/d/3": {"DeviceType": "NIC", "Name": "Mellanox Network Adapter - A0:88:C2:FE:FF:3C", "StructuredName": "NIC.Slot.3.1"},
        "/d/6": {"DeviceType": "NIC", "Name": "Mellanox Network Adapter - A0:88:C2:FE:FF:30", "StructuredName": "NIC.Slot.6.1"},
        "/redfish/v1/Chassis/1/NetworkAdapters": {"Members": [{"@odata.id": a6}, {"@odata.id": a3}, {"@odata.id": ocp}]},
        a3: {**ADAPTER, "Ports": {"@odata.id": a3 + "/Ports"}}, a6: {**ADAPTER, "Ports": {"@odata.id": a6 + "/Ports"}},
        ocp: {"Model": "P42043-001 / Mellanox MCX631432AS-ADAI Ethernet 10/25Gb 2-port SFP28 OCP3 Adapter",
              "Ports": {"@odata.id": ocp + "/Ports"}},  # no slot, no MAC in the PCI device: the OCP slot left
        a3 + "/Ports": {"Members": [{"@odata.id": a3 + "/Ports/0"}, {"@odata.id": a3 + "/Ports/1"}]},
        a6 + "/Ports": {"Members": [{"@odata.id": a6 + "/Ports/0"}, {"@odata.id": a6 + "/Ports/1"}]},
        ocp + "/Ports": {"Members": [{"@odata.id": ocp + "/Ports/0"}, {"@odata.id": ocp + "/Ports/1"}]},
        # a card's second port names the next MAC: only the first one matches the PCI device
        a3 + "/Ports/0": port(0, "A0:88:C2:FE:FF:3C"), a3 + "/Ports/1": port(1, "A0:88:C2:FE:FF:3D"),
        a6 + "/Ports/0": port(0, "A0:88:C2:FE:FF:30"), a6 + "/Ports/1": port(1, "A0:88:C2:FE:FF:31"),
        ocp + "/Ports/0": port(0, "5C:25:73:3B:BD:4C", link="LinkDown"), ocp + "/Ports/1": port(1, "5C:25:73:3B:BD:4D"),
        "/redfish/v1/Systems/1/Storage": {"Members": [{"@odata.id": "/s/1"}]},
        "/s/1": {"Name": "HPE NS204i-u Gen11 Boot Controller", "Status": {"HealthRollup": "OK"}, "Volumes": {"@odata.id": "/s/1/v"}},
        "/s/1/v": {"Members": [{"@odata.id": "/s/1/v/1"}]},
        "/s/1/v/1": {"CapacityBytes": 480036519936, "Status": {"Health": boot_health, "State": "Enabled"}},
    }
    if down:
        data[down]["LinkStatus"] = "LinkDown"
    c = redfish.Client.__new__(redfish.Client)
    c.get = lambda path: data[path]
    return c


def profile(fabrics=2):
    cards = [{"slot": "3", "name": "ens3", "ports": 2, "links": ["ens3f0np0", "ens3f1np1"]},
             {"slot": "6", "name": "ens6", "ports": 2, "links": ["ens6f0np0", "ens6f1np1"]}]
    assign = {"nic-data1": "ens3f0np0", "nic-data2": "ens3f1np1", "nic-storage1": "ens6f0np0", "nic-storage2": "ens6f1np1"}
    return ServerProfile(code="ga1", nics={"cards": cards, "assign": assign, "fabrics": fabrics})


class HostHealth(SimpleTestCase):
    def test_healthy_host_and_versions(self):
        h = Host(name="h1", profile=profile())
        data = ilo().health()
        self.assertEqual(sorted(data["ports"]), ["22", "3", "6"])  # slots from the cards' MACs, the OCP card by elimination
        self.assertEqual(data["ports"]["22"]["0"]["link"], "LinkDown")  # not in the profile: no problem
        self.assertEqual(sorted(data["ports"]["6"]), ["0", "1"])
        r = hosthealth.evaluate(h, data)
        self.assertEqual((r["ok"], r["problems"], r["bios"], r["ipmi_fw"]), (True, [], "A55 v3.16", "iLO 6 v1.77"))
        self.assertEqual([p["alias"] for p in r["ports"]], ["nic-data1", "nic-data2", "nic-storage1", "nic-storage2"])
        self.assertEqual([g["id"] for g in r["gpus"]], ["3"])

    def test_problems(self):
        h = Host(name="h1", profile=profile())
        r = hosthealth.evaluate(h, ilo(down="/redfish/v1/Chassis/1/NetworkAdapters/A6/Ports/1", gpu_health="Critical",
                                       boot_health="Warning").health())
        self.assertFalse(r["ok"])
        self.assertEqual(r["problems"], ["boot device: Warning", "nic-storage2 (ens6f1np1): link LinkDown", "GPU 3: Critical"])

    def test_one_fabric_uses_two_ports(self):
        h = Host(name="h1", profile=profile(fabrics=1))
        r = hosthealth.evaluate(h, ilo(down="/redfish/v1/Chassis/1/NetworkAdapters/A6/Ports/1").health())
        self.assertTrue(r["ok"], r["problems"])  # gi2: only nic-data1/2; the storage port being down does not count
        self.assertEqual(len(r["ports"]), 2)

    def test_no_profile_is_a_problem(self):
        r = hosthealth.evaluate(Host(name="h1"), ilo().health())
        self.assertIn("network: no server profile with its NIC aliases", r["problems"])


class IgwDetect(SimpleTestCase):
    def test_type_from_image(self):
        from tenants.igwdetect import type_of
        self.assertEqual(type_of(["vyos-2026.03-cloud"]), "vyos")
        self.assertEqual(type_of(["Service Virtual Router"]), "onevr")
        self.assertEqual(type_of(["ubuntu-24.04"]), "custom")
