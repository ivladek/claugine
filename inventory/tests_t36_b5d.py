"""T36 B5d (owner 2026-11-26 14:05 "go ahead"; QB5-2, QB5-3 of 11:29). Time: the installation's external NTP servers
(Settings → Customer); a site's time servers are its hosts with the function time (chrony from that list), every
other system of the site syncs with them. GPU: passthrough on a host whose service has gpu, checked after the reboot."""

import importlib
from unittest import mock

from django.apps import apps
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.tests import Base
from inventory import hostdeploy, hostservice, recipes, timesrv
from inventory.models import ConfigTemplate, Host, HostService, ItemState, ServiceProfile, Site
from inventory.tests_hostdeploy import KEY, world
from tenants.models import PortalSettings


def m0133():
    return importlib.import_module("inventory.migrations.0133_t36_time")


def ext(*xs):
    PortalSettings.objects.filter(pk=PortalSettings.get().pk).update(ntp_external=list(xs))


class TimeServers(TestCase):
    def setUp(self):
        self.site = Site.objects.create(code="dc1", management={"hosts": ["10.0.0.0/16", "10.9.9.9"]})

    def host(self, name, ip, slot, **kw):
        kw.setdefault("time_served_at", timezone.now())  # commit-577: seen serving time
        kw.setdefault("time_confirmed_by", "peer")
        return Host.objects.create(site=self.site, name=name, ip=ip, service=hostservice.of_slot(slot), **kw)

    def test_the_hosts_with_the_time_function_by_name(self):
        self.host("m3", "10.0.100.3", "mgmt")
        self.host("m1", "10.0.100.1", "mgmt")
        self.host("s1", "10.0.100.11", "simple-fe")
        self.host("x1", "10.0.100.21", "shared")  # no time
        self.host("m0", "10.0.100.9", "mgmt", state=ItemState.RETIRED)
        self.host("m9", "", "mgmt")  # no core address
        Site.objects.create(code="dc2")
        self.assertEqual(timesrv.servers(self.site), ["10.0.100.1", "10.0.100.3", "10.0.100.11"])
        self.assertEqual(self.site.time_servers, timesrv.servers(self.site))
        self.assertEqual(timesrv.servers(Site.objects.get(code="dc2")), [])
        self.assertEqual(timesrv.servers(None), [])
        for i in range(4, 8):
            self.host(f"m{i}", f"10.0.100.{i}", "mgmt")
        self.assertEqual(len(timesrv.servers(self.site)), 4)  # timesyncd, chrony and the routers take four

    def test_577_only_hosts_in_service(self):
        self.host("m1", "10.0.100.1", "mgmt", state=ItemState.PLANNED)  # its Deploy may not have installed chrony yet
        self.host("m2", "10.0.100.2", "simple-fe", state=ItemState.PLANNED)
        self.assertEqual(timesrv.servers(self.site), [])
        Host.objects.filter(name="m2").update(state=ItemState.NON_COMPLIANT)  # still serving
        Host.objects.filter(name="m1").update(state=ItemState.DEPLOYED)
        self.assertEqual(timesrv.servers(self.site), ["10.0.100.1", "10.0.100.2"])
        # an FE host deployed before B5d (0126 gave it its service, nothing checked it): not a time server yet
        self.host("m3", "10.0.100.3", "mgmt", time_served_at=None)
        self.assertEqual(timesrv.servers(self.site), ["10.0.100.1", "10.0.100.2"])

    def test_the_typed_list_is_not_read(self):
        Site.objects.filter(pk=self.site.pk).update(ntp_servers=["192.0.2.1"])
        self.assertEqual(timesrv.servers(Site.objects.get(pk=self.site.pk)), [])

    def test_chrony_conf(self):
        allow = timesrv.allowed(self.site, ["10.0.100.0/24", "10.0.0.0/16"])
        self.assertEqual(allow, ["10.0.0.0/16", "10.9.9.9/32", "10.0.100.0/24"])
        conf = timesrv.chrony_conf(["0.pool.ntp.org", "192.0.2.5"], allow)
        self.assertIn("server 0.pool.ntp.org iburst\nserver 192.0.2.5 iburst\n", conf)
        self.assertIn("allow 10.0.0.0/16\nallow 10.9.9.9/32\nallow 10.0.100.0/24\n", conf)
        self.assertNotIn("pool ", conf)  # no Ubuntu pool
        self.assertNotIn("sourcedir", conf)

    def test_external_list(self):
        self.assertEqual(timesrv.external(), [])
        ext("a.example", "b.example", "c.example", "d.example", "e.example")
        self.assertEqual(timesrv.external(), ["a.example", "b.example", "c.example", "d.example"])


@KEY
class HostChecks(TestCase):
    def setUp(self):
        self.h, self.c = world()
        self.node = mock.patch("accounts.hostmedia.node_for", return_value="10.201.101.25")
        self.node.start()

    def tearDown(self):
        self.node.stop()

    def test_a_host_needs_its_site_time_servers(self):
        self.assertEqual(hostdeploy.problems(self.h, self.c), [])
        Host.objects.filter(name="kca201").update(state=ItemState.RETIRED)
        self.assertIn(
            "site zk1 has no time servers yet: deploy the FE hosts of its mgmt or simple platform first",
            hostdeploy.problems(self.h, self.c),
        )

    def test_a_time_server_needs_the_external_list(self):
        p = hostdeploy.problems(self.h, self.c, fns=["kvm", "frr", "time"])
        self.assertIn("Settings → Customer: no external NTP servers (the site's time servers sync with them)", p)
        ext("0.pool.ntp.org")
        self.assertEqual(hostdeploy.problems(self.h, self.c, fns=["kvm", "frr", "time"]), [])

    def test_gpus_only_with_the_gpu_function(self):
        prof = self.h.profile
        prof.gpu_count = 2
        prof.save()
        h = Host.objects.get(pk=self.h.pk)
        self.assertEqual(hostservice.pick(h, self.c.platform).host_slot, "shared-gpu")
        self.assertIn("gpu", hostservice.of_slot("shared-gpu").functions())
        self.assertEqual(hostdeploy.problems(h, self.c), [])  # no more "GPU hosts are not deployed yet"
        p = hostdeploy.problems(h, self.c, fns=["kvm", "frr"])
        self.assertIn("server profile ci1 has 2 GPUs, host service kvm-shared-gpu has no gpu function", p)
        prof.gpu_count = 0
        prof.save()
        h = Host.objects.get(pk=self.h.pk)
        p = hostdeploy.problems(h, self.c, fns=["kvm", "frr", "gpu"])
        self.assertIn("server profile ci1 has no GPUs, host service kvm-shared has the gpu function", p)


class Steps(TestCase):
    def r(self, fns, gpus=0):
        r = mock.Mock()
        r.fns = fns
        r.site, _ = Site.objects.get_or_create(code="dc1", defaults={"management": {"hosts": ["10.61.0.0/16"]}})
        r.nets.return_value = [
            {"kind": "core-kvm", "name": "one-core", "bridge": "one-core", "cidr": "10.61.100.0/24"},
            {"kind": "core-lm", "name": "one-lm", "bridge": "one-lm", "cidr": "10.61.105.0/24"},
            {"kind": "nfs", "name": "data-nfs", "bridge": "data-nfs", "cidr": "10.61.111.0/24"},
        ]
        r.prof.gpu_count, r.prof.code = gpus, "ga1"
        r.service_key.return_value = "kvm-shared-gpu" if "gpu" in fns else "kvm-mgmt"
        r.h = Host.objects.create(site=r.site, name=f"h{Host.objects.count()}", ip="10.61.100.9", service=None)
        return r

    def test_a_time_server_runs_chrony_from_the_external_list(self):
        r = self.r(["kvm", "mysql", "one-fe", "time"])
        with self.assertRaisesMessage(hostdeploy.DeployError, "no external NTP servers"):
            hostdeploy.s_os_base(r)
        r.ssh.assert_not_called()
        ext("0.pool.ntp.org", "192.0.2.5")
        r.ssh.return_value = "NTP_OK serving time on 10.61.100.9 (stratum 3)\n"
        said = hostdeploy.s_os_base(r)
        script = r.ssh.call_args_list[0].args[0]
        self.assertTrue(script.startswith("CLAUGINE_TIME=chrony\nCLAUGINE_CHRONY="))
        for x in ("server 0.pool.ntp.org iburst", "allow 10.61.0.0/16", "allow 10.61.100.0/24", "allow 10.61.105.0/24"):
            self.assertIn(x, script)
        self.assertNotIn("allow 10.61.111.0/24", script)  # the NFS network asks no time
        self.assertIn("chrony, the site's time server", said)
        # commit-577: checked on itself, then asked - no other host of the site in service: by itself, from its address
        calls = [c.args[0] for c in r.ssh.call_args_list]
        self.assertEqual(calls[1:], [timesrv.CHECK, timesrv.probe_script("10.61.100.9", "10.61.100.9")])
        self.assertIsNotNone(Host.objects.get(pk=r.h.pk).time_served_at)
        self.assertEqual(Host.objects.get(pk=r.h.pk).time_confirmed_by, timesrv.SELF)
        self.assertIn("serving time (answered itself (no other host of the site in service yet))", said)
        r.ssh.side_effect = [None, None, "NTP_FAIL no NTP answer from 10.61.100.9 to 10.61.100.9: timed out\n"]
        with self.assertRaisesMessage(hostdeploy.DeployError, "no NTP answer from 10.61.100.9"):
            hostdeploy.s_os_base(r)
        self.assertIsNone(Host.objects.get(pk=r.h.pk).time_served_at)
        r.ssh.side_effect = [None, RuntimeError("chrony has no selected source")]
        with self.assertRaisesMessage(hostdeploy.DeployError, "chrony does not serve time yet"):
            hostdeploy.s_os_base(r)
        self.assertIsNone(Host.objects.get(pk=r.h.pk).time_served_at)

    def test_577_585_the_site_hosts_ask_across_the_network(self):
        from inventory.models import Cluster, Platform

        ext("0.pool.ntp.org")
        r = self.r(["kvm", "mysql", "one-fe", "time"])
        p = Platform.objects.create(key="dc1.mgmt", site=r.site, name="mgmt", kind="mgmt")
        c = Cluster.objects.create(platform=p, name="mgmt_dc1", kind="mgmt")
        c.hosts.add(r.h)
        for n in ("a2", "a3", "a4", "a5"):
            c.hosts.add(Host.objects.create(site=r.site, name=n, ip=f"10.61.100.{int(n[1]) * 10}"))
        Host.objects.create(site=r.site, name="a0", ip="10.61.100.99")  # in no cluster: no login to it
        self.assertEqual([x.name for x in timesrv.peers(r.h)], ["a2", "a3", "a4", "a5"])  # every one (commit-585)
        asked = []

        def peer(h, script, timeout=None, deadline=None):
            asked.append((h.name, script))
            if h.name in ("a2", "a3"):  # commit-585: it cannot run the request - skipped, the next is asked
                raise RuntimeError("ssh: connection refused")
            return "NTP_OK serving time on 10.61.100.9 (stratum 3)\n"

        with mock.patch.object(timesrv, "_peer_ssh", side_effect=peer):
            said = hostdeploy.s_os_base(r)
        self.assertEqual([a for a, _s in asked], ["a2", "a3", "a4"])
        self.assertEqual(asked[2][1], timesrv.probe_script("10.61.100.9", "10.61.100.40"))
        self.assertIn("serving time (answered a4)", said)
        self.assertEqual(Host.objects.get(pk=r.h.pk).time_confirmed_by, timesrv.PEER)
        self.assertNotIn(timesrv.probe_script("10.61.100.9", "10.61.100.9"), [c.args[0] for c in r.ssh.call_args_list])
        # a peer that ran it and got no time fails the server
        with mock.patch.object(timesrv, "_peer_ssh", return_value="NTP_FAIL no NTP answer: timed out\n"):
            with self.assertRaisesMessage(hostdeploy.DeployError, "a2 gets no time from it: no NTP answer"):
                hostdeploy.s_os_base(r)
        self.assertIsNone(Host.objects.get(pk=r.h.pk).time_served_at)
        # no peer could run it: inconclusive - the mark is left as it was, never asked by itself
        timesrv.mark(r.h, True, timesrv.address_of(r.h), timesrv.PEER)
        with mock.patch.object(timesrv, "_peer_ssh", side_effect=RuntimeError("timed out")):
            with self.assertRaisesMessage(hostdeploy.DeployError, "no host of the site could run the check - a2:"):
                hostdeploy.s_os_base(r)
        self.assertIsNotNone(Host.objects.get(pk=r.h.pk).time_served_at)
        # a peer whose output has no result line did not run it either
        with mock.patch.object(timesrv, "_peer_ssh", return_value="python3: not found\n"):
            with self.assertRaisesMessage(timesrv.Inconclusive, "a5: it did not run the request"):
                timesrv.confirm(r.h, "10.61.100.9", lambda s: None)

    def test_585_the_deadline(self):
        from inventory.models import Cluster, Platform

        r = self.r(["kvm", "time"])
        c = Cluster.objects.create(
            platform=Platform.objects.create(key="dc1.mgmt", site=r.site, name="mgmt", kind="mgmt"),
            name="m",
            kind="mgmt",
        )
        c.hosts.add(r.h)
        for n in ("b1", "b2", "b3"):
            c.hosts.add(Host.objects.create(site=r.site, name=n, ip=f"10.61.100.{20 + int(n[1])}"))
        # commit-586: each peer gets at most PEER_TIMEOUT, within what is left; the deadline is read after each too
        t = iter([0, 10, 40, 100, 190])  # start; b1 at 10 s (back at 40); b2 at 100 s (back at 190, past 180)
        with mock.patch.object(timesrv, "_peer_ssh", side_effect=RuntimeError("timed out")) as ssh:
            with self.assertRaisesMessage(timesrv.Inconclusive, "the deadline passed"):
                timesrv.confirm(r.h, "10.61.100.9", lambda s: None, clock=lambda: next(t))
        self.assertEqual([c.kwargs["timeout"] for c in ssh.call_args_list], [30, 30])  # b3 never asked
        t = iter([0, 165, 170, 179.5])  # b1 gets the 15 s left; then less than a second: stop
        with mock.patch.object(timesrv, "_peer_ssh", side_effect=RuntimeError("timed out")) as ssh:
            with self.assertRaisesMessage(timesrv.Inconclusive, "the deadline passed"):
                timesrv.confirm(r.h, "10.61.100.9", lambda s: None, clock=lambda: next(t))
        self.assertEqual([c.kwargs["timeout"] for c in ssh.call_args_list], [15])

    def test_577_a_self_confirmed_server_counts_only_alone(self):
        from inventory.models import Cluster, Platform

        r = self.r(["kvm", "time"])
        Host.objects.filter(pk=r.h.pk).update(service=hostservice.of_slot("mgmt"))
        c = Cluster.objects.create(
            platform=Platform.objects.create(key="dc1.mgmt", site=r.site, name="mgmt", kind="mgmt"),
            name="m",
            kind="mgmt",
        )
        c.hosts.add(r.h)
        timesrv.mark(r.h, True, timesrv.address_of(r.h), timesrv.SELF)
        self.assertEqual(timesrv.servers(r.site), ["10.61.100.9"])  # the site's only host
        other = Host.objects.create(site=r.site, name="z2", ip="10.61.100.22")
        self.assertEqual(timesrv.servers(r.site), ["10.61.100.9"])  # not in a cluster: not a client that asks
        c.hosts.add(other)
        self.assertEqual(timesrv.servers(r.site), [])  # a client is in service: a peer must have got its answer
        timesrv.mark(r.h, True, timesrv.address_of(r.h), timesrv.PEER)
        self.assertEqual(timesrv.servers(r.site), ["10.61.100.9"])

    def test_584_a_discarded_result_is_not_a_success(self):
        from inventory import ops

        ext("0.pool.ntp.org")
        r = self.r(["kvm", "time"])
        sa = Base.staff(self, "sa8@corp.kz", is_super_admin=True)

        def ssh(script, timeout=0):
            if "CLAUGINE_PY" in script:  # the address is edited while the check runs
                ops.host_save(sa, "dc1", r.h.name, None, "10.61.100.11", new=False)
                return "NTP_OK serving time on 10.61.100.9 (stratum 3)\n"
            return ""

        r.ssh.side_effect = ssh
        with self.assertRaisesMessage(hostdeploy.DeployError, "but the result is not kept: the address changed"):
            hostdeploy.s_os_base(r)
        self.assertIsNone(Host.objects.get(pk=r.h.pk).time_served_at)

    def test_580_a_result_for_an_older_address_revision_is_not_kept(self):
        from inventory import ops

        r = self.r(["kvm"])
        sa = Base.staff(self, "sa9@corp.kz", is_super_admin=True)
        addr = timesrv.address_of(r.h)
        ops.host_save(sa, "dc1", r.h.name, None, "10.61.100.10", new=False)  # A -> B
        ops.host_save(sa, "dc1", r.h.name, None, "10.61.100.9", new=False)  # B -> A again
        self.assertEqual(timesrv.address_of(r.h), ("10.61.100.9", addr[1] + 2))
        timesrv.mark(r.h, True, addr)  # the check that began before the edits
        self.assertIsNone(Host.objects.get(pk=r.h.pk).time_served_at)
        timesrv.mark(r.h, True, timesrv.address_of(r.h))
        self.assertIsNotNone(Host.objects.get(pk=r.h.pk).time_served_at)

    def test_any_other_host_syncs_with_the_site_time_servers(self):
        r = self.r(["kvm", "frr"])
        Host.objects.create(
            site=r.site,
            name="m1",
            ip="10.61.100.201",
            service=hostservice.of_slot("mgmt"),
            time_served_at=timezone.now(),
            time_confirmed_by="peer",
        )
        r.ssh.side_effect = lambda script, timeout=0: "NTP_OK serving time\n" if "CLAUGINE_PY" in script else ""
        said = hostdeploy.s_os_base(r)
        # commit-577: this host asked the time server from its own core address before it was given it
        self.assertEqual(r.ssh.call_args_list[0].args[0], timesrv.probe_script("10.61.100.201", "10.61.100.9"))
        self.assertTrue(r.ssh.call_args.args[0].startswith("CLAUGINE_TIME=timesyncd\nCLAUGINE_NTP=10.61.100.201\n"))
        self.assertIn("timesyncd (10.61.100.201)", said)
        self.assertIsNone(Host.objects.get(pk=r.h.pk).time_served_at)  # a client is never a time server

    def test_577_the_first_client_asks_before_it_is_given_them(self):
        r = self.r(["kvm", "frr"])
        now = timezone.now()
        mk = dict(service=hostservice.of_slot("mgmt"), time_served_at=now)
        s1 = Host.objects.create(site=r.site, name="m1", ip="10.61.100.201", time_confirmed_by="self", **mk)
        s2 = Host.objects.create(site=r.site, name="m2", ip="10.61.100.202", time_confirmed_by="self", **mk)
        self.assertEqual(timesrv.servers(r.site), ["10.61.100.201", "10.61.100.202"])  # alone: no client yet

        def ssh(script, timeout=0):
            if "CLAUGINE_PY" not in script:
                return ""
            return "NTP_OK serving time\n" if "10.61.100.201" in script else "NTP_FAIL no NTP answer: timed out\n"

        r.ssh.side_effect = ssh
        said = hostdeploy.s_os_base(r)
        self.assertIn("timesyncd (10.61.100.201)", said)  # only the one that answered it
        self.assertEqual(Host.objects.get(pk=s1.pk).time_confirmed_by, timesrv.PEER)  # confirmed by this client
        self.assertIsNone(Host.objects.get(pk=s2.pk).time_served_at)  # never answered a client: no longer one
        r.ssh.side_effect = lambda script, timeout=0: (
            "NTP_FAIL no NTP answer: timed out\n" if "CLAUGINE_PY" in script else ""
        )
        with self.assertRaisesMessage(
            hostdeploy.DeployError, "none of the site's time servers answers this host - m1:"
        ):
            hostdeploy.s_os_base(r)
        self.assertIsNotNone(Host.objects.get(pk=s1.pk).time_served_at)  # peer-confirmed: left out for this host only
        r.ssh.side_effect = lambda script, timeout=0: "python3: not found\n"
        with self.assertRaisesMessage(hostdeploy.DeployError, "the time request did not run on this host"):
            hostdeploy.s_os_base(r)

    def test_the_baseline_script_keeps_chrony_only_on_a_time_server(self):
        b = hostdeploy.OS_BASE
        self.assertIn('[ "$CLAUGINE_TIME" = chrony ] || purge="$purge chrony"', b)
        self.assertIn("/etc/chrony/chrony.conf", b)
        self.assertIn("systemd-timesyncd is still running", b)
        self.assertEqual(
            hostdeploy.BASELINE, 6
        )  # commit-581: a resumed older run makes its OS baseline (and check) again

    def test_gpu_step(self):
        r = self.r(["kvm", "frr"])
        self.assertEqual(hostdeploy.s_gpu(r), "no gpu in its service (kvm-mgmt)")
        r = self.r(["kvm", "frr"], gpus=2)
        with self.assertRaisesMessage(hostdeploy.DeployError, "has 2 GPUs and host service"):
            hostdeploy.s_gpu(r)
        r = self.r(["kvm", "frr", "gpu"])
        with self.assertRaisesMessage(hostdeploy.DeployError, "no GPUs"):
            hostdeploy.s_gpu(r)
        r = self.r(["kvm", "frr", "gpu"], gpus=4)
        r.ssh.return_value = "gpus=4 ids=10de:233b iommu=intel_iommu=on\n"
        self.assertEqual(
            hostdeploy.s_gpu(r), "passthrough set (gpus=4 ids=10de:233b iommu=intel_iommu=on); checked after the reboot"
        )
        script = r.ssh.call_args.args[0]
        self.assertTrue(script.startswith("CLAUGINE_GPUS=4\n"))
        for x in (
            "intel_iommu=on",
            "amd_iommu=on",
            "iommu=pt kvm.ignore_msrs=1",
            "options vfio-pci ids=$ids",
            "blacklist nouveau",
            "install nvidia /bin/false",
            "vfio_iommu_type1",
            'SUBSYSTEM=="vfio", OWNER="oneadmin", GROUP="oneadmin", MODE="0600"',
            "update-initramfs -u",
            "/var/run/reboot-required",
        ):
            self.assertIn(x, script)
        self.assertNotIn("pcie_acs_override", script)
        # commit-578: a changed file marks the boot updates pending; the mark goes only after all three
        self.assertLess(script.index('touch "$pending"'), script.index('mv -f "$tmp" "$1"'))
        self.assertIn('if [ -e "$pending" ]; then', script)
        self.assertLess(script.index("udevadm control --reload"), script.index('rm -f "$pending"'))
        self.assertNotIn("changed=1", script)
        self.assertNotIn("10de:2331", script)  # the ids are read from the host's own cards

    def test_gpu_checked_after_the_reboot(self):
        r = self.r(["kvm", "gpu"], gpus=2)
        r.ssh.side_effect = lambda s, **kw: "yes\n" if "reboot-required" in s else "gpus=2 on vfio-pci\n"
        r.sleep = lambda s: None
        with mock.patch.object(hostdeploy, "network_script", return_value="true\n"):
            said = hostdeploy.s_reboot(r)
        self.assertIn("gpus=2 on vfio-pci", said)
        checks = [c.args[0] for c in r.ssh.call_args_list if "vfio-pci, not" in c.args[0] or "is on ${drv" in c.args[0]]
        self.assertTrue(checks and checks[-1].startswith("CLAUGINE_GPUS=2\n"))
        r = self.r(["kvm"])
        r.ssh.return_value = "no\n"
        with mock.patch.object(hostdeploy, "network_script", return_value="true\n"):
            self.assertEqual(hostdeploy.s_reboot(r), "no reboot needed; network checked")


class CheckTimeServers(TestCase):
    """commit-577: the FE hosts built before B5d become time servers only by Check time servers."""

    def setUp(self):
        from inventory.models import Cluster, Platform

        self.site = Site.objects.create(code="dc1")
        self.p = Platform.objects.create(key="dc1.mgmt", site=self.site, name="mgmt", kind="mgmt")
        self.c = Cluster.objects.create(platform=self.p, name="mgmt_dc1", kind="mgmt")
        self.hs = []
        for i in (1, 2):
            h = Host.objects.create(
                site=self.site, name=f"m{i}", ip=f"10.0.100.{i}", service=hostservice.of_slot("mgmt")
            )
            self.c.hosts.add(h)
            self.hs.append(h)
        x = Host.objects.create(site=self.site, name="p1", ip="10.0.100.9", service=hostservice.of_slot("mgmt"))
        Host.objects.filter(pk=x.pk).update(state=ItemState.PLANNED)
        self.c.hosts.add(x)

    def test_each_host_passing_is_a_time_server_and_one_failing_is_not(self):
        self.assertEqual([h.name for h in timesrv.time_candidates(self.p)], ["m1", "m2"])  # not the planned one
        self.assertEqual(timesrv.servers(self.site), [])

        def ssh(h, platform, text):
            self.assertEqual(text, timesrv.CHECK)
            if h.name == "m2":
                raise RuntimeError("chrony is not running")

        asked = []

        def peer(
            p, script, timeout=None, deadline=None
        ):  # the other host asks across the network, from its own core address
            asked.append((p.name, script))
            return f"NTP_OK serving time (stratum 2) - asked by {p.name}\n"

        out = timesrv.check_platform(self.p, ssh=ssh, ssh_peer=peer)
        self.assertEqual(out, {"m1": "serving time, answered m2", "m2": "FAILED: chrony is not running"})
        self.assertEqual(asked, [("m2", timesrv.probe_script("10.0.100.1", "10.0.100.2"))])
        self.assertEqual(timesrv.servers(self.site), ["10.0.100.1"])
        Host.objects.filter(name="m2").update(time_served_at=timezone.now(), time_confirmed_by="peer")
        timesrv.check_platform(self.p, ssh=ssh, ssh_peer=peer)
        self.assertEqual(timesrv.servers(self.site), ["10.0.100.1"])  # failing again: no longer one
        # commit-577: chrony fine on m1, but the site's host gets no answer (its bridge drops NTP): not a time server
        dropped = timesrv.check_platform(
            self.p, ssh=lambda h, p, t: None, ssh_peer=mock.Mock(return_value="NTP_FAIL no NTP answer: timed out\n")
        )
        self.assertIn("m2 gets no time from it", dropped["m1"])
        self.assertEqual(timesrv.servers(self.site), [])

        # commit-580: the address edited while the check runs - its result is not kept for the new address
        def edited(h, platform, text):
            from inventory import ops

            if h.name == "m1":
                ops.host_save(self.sa, "dc1", "m1", None, "10.0.100.7", new=False)

        self.sa = Base.staff(self, "sa2@corp.kz", is_super_admin=True)
        timesrv.check_platform(self.p, ssh=edited, ssh_peer=peer)
        self.assertIsNone(Host.objects.get(name="m1").time_served_at)
        self.assertEqual(timesrv.servers(self.site), ["10.0.100.2"])  # m2 passed this time; m1's result not kept
        with mock.patch.object(timesrv, "check_platform", return_value=out):
            with self.assertRaisesMessage(timesrv.CheckFailed, "1 of 2 time server(s) of dc1.mgmt"):
                timesrv.check_task(self.p)

    def probe(self, answer, wrong_endpoint=False):
        """PROBE run as on the host, against a stand-in NTP server on 127.0.0.1; (exit code, output)."""
        import socket
        import subprocess
        import sys
        import threading

        so = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        so.bind(("127.0.0.1", 0))
        other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        other.bind(("127.0.0.1", 0))

        def run():
            data, addr = so.recvfrom(512)
            self.assertEqual(data[0], 0x23)  # version 4, client
            if answer is not None:
                (other if wrong_endpoint else so).sendto(answer(data[40:48]), addr)
            so.close()
            other.close()

        threading.Thread(target=run, daemon=True).start()
        p = subprocess.run(
            [sys.executable, "-", "127.0.0.1", str(so.getsockname()[1]), "127.0.0.1"],
            input=timesrv.PROBE,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return p.returncode, p.stdout + p.stderr

    def test_the_probe(self):
        """commit-582: only an answer to this request, from the address asked, synchronised."""

        def server(first=0x24, stratum=2, echo=True):
            return lambda tx: bytes([first, stratum]) + 22 * b"\0" + (tx if echo else 8 * b"\1") + 16 * b"\0"

        rc, out = self.probe(server())
        self.assertEqual(rc, 0, out)
        self.assertEqual(timesrv.outcome(out), (True, "serving time on 127.0.0.1 (stratum 2)"))
        rc, out = self.probe(server(echo=False))
        self.assertEqual(timesrv.outcome(out), (False, "127.0.0.1 gave an answer that is not to this request"))
        for bad in (server(first=0xE4), server(stratum=16), server(first=0x23)):
            rc, out = self.probe(bad)
            self.assertEqual(rc, 0)  # commit-585: it ran - its result is in its line, not its exit
            self.assertFalse(timesrv.outcome(out)[0])
            self.assertIn("serves no synchronised time", out)
        rc, out = self.probe(server(), wrong_endpoint=True)  # from another port: dropped, no answer
        self.assertIn("NTP_FAIL no NTP answer from 127.0.0.1 to 127.0.0.1", out)
        rc, out = self.probe(None)
        self.assertIn("NTP_FAIL no NTP answer from 127.0.0.1 to 127.0.0.1", out)
        self.assertIsNone(timesrv.outcome("Traceback ...\nOSError: cannot assign requested address\n"))

    def test_an_address_edit_clears_the_confirmation(self):
        from inventory import ops

        sa = Base.staff(self, "sa3@corp.kz", is_super_admin=True)
        Host.objects.filter(name="m1").update(time_served_at=timezone.now(), time_confirmed_by="peer")
        ops.host_save(sa, "dc1", "m1", None, "10.0.100.1", new=False)  # the same address: kept
        self.assertIsNotNone(Host.objects.get(name="m1").time_served_at)
        ops.host_save(sa, "dc1", "m1", None, "10.0.100.8", new=False)
        self.assertIsNone(Host.objects.get(name="m1").time_served_at)

    def test_the_check_script(self):
        for x in ("systemctl is-active --quiet chrony", "ss -Hlun | grep -q ':123 '", "chronyc -n sources"):
            self.assertIn(x, timesrv.CHECK)
        self.assertEqual(
            timesrv.probe_script("10.0.100.1", "10.0.100.2"),
            "python3 - 10.0.100.1 123 10.0.100.2 <<'CLAUGINE_PY'\n" + timesrv.PROBE + "CLAUGINE_PY\n",
        )
        self.assertIn("so.bind((src, 0))", timesrv.PROBE)  # from the client's core address (commit-583)

    def test_the_action_on_the_cluster(self):
        sa = Base.staff(self, "sa@corp.kz", is_super_admin=True)
        self.client.force_login(sa)
        page = self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=clusters").content.decode()
        self.assertIn(reverse("inv_cluster_time_check", args=["dc1", self.c.pk]), page)
        with mock.patch("accounts.tasks.start") as start:
            self.client.post(reverse("inv_cluster_time_check", args=["dc1", self.c.pk]))
        self.assertEqual(start.call_args.args[0], "time check")

    def test_a_new_deploy_run_unmarks_the_host(self):
        src = open(hostdeploy.__file__).read()
        self.assertIn('Host.objects.filter(pk=h.pk).update(time_served_at=None, time_confirmed_by="")', src)


class Review584to589(TestCase):
    """The new host's check of its time servers (commit-584, 588, 589) and the peers' deadline over ssh (commit-587)."""

    def setUp(self):
        self.site = Site.objects.create(code="dc1")

    def server(self, n, by="peer"):
        return Host.objects.create(
            site=self.site,
            name=f"m{n}",
            ip=f"10.0.100.{n}",
            service=hostservice.of_slot("mgmt"),
            time_served_at=timezone.now(),
            time_confirmed_by=by,
        )

    def test_584_an_address_edited_during_the_probe_is_not_given(self):
        for by in ("peer", "self"):
            h = self.server(1, by)

            def ssh(script, h=h):
                Host.objects.filter(pk=h.pk).update(ip_rev=h.ip_rev + 1)  # an edit away and back meanwhile
                return "NTP_OK serving time\n"

            ok, bad = timesrv.client_check(self.site, "10.0.100.50", ssh)
            self.assertEqual(ok, [], by)
            self.assertEqual(bad, ["m1: its address changed during the check"], by)
            h.delete()

    def test_584_one_chosen_earlier_whose_address_moves_later_is_not_given(self):
        m1, _m2 = self.server(1), self.server(2)

        def ssh(script):
            if "10.0.100.2 " in script:  # while m2 is asked, m1's address is edited away and back
                Host.objects.filter(pk=m1.pk).update(ip_rev=m1.ip_rev + 2)
                return "NTP_FAIL timed out\n"
            return "NTP_OK serving time\n"

        ok, bad = timesrv.client_check(self.site, "10.0.100.50", ssh)
        self.assertEqual(ok, [])
        self.assertEqual(bad, ["m2: timed out", "m1: its address changed during the check"])

    def test_587_the_timeout_is_read_right_before_ssh_starts(self):
        import subprocess
        import time

        from inventory import sshexec

        clock = [1000.0]
        seen = []

        def write(d, name, text):
            clock[0] += 4  # writing the keys takes 4 s
            return f"{d}/{name}"

        def run(cmd, **kw):
            seen.append(kw["timeout"])
            return subprocess.CompletedProcess(cmd, 0, "NTP_OK serving time\n", "")

        with (
            mock.patch.object(sshexec, "_write", side_effect=write),
            mock.patch.object(sshexec.subprocess, "run", side_effect=run),
            mock.patch.object(time, "monotonic", side_effect=lambda: clock[0]),
        ):
            sshexec.run("192.0.2.5", 22, "sa", "KEY", "x", timeout=30, deadline=1010.0)
        self.assertEqual(seen, [6])  # 10 s were left when it was asked, 6 when ssh started

    def test_587_confirm_gives_each_peer_an_absolute_deadline(self):
        import time

        from inventory.models import Cluster, Platform

        h = self.server(1, "")
        c = Cluster.objects.create(
            platform=Platform.objects.create(key="dc1.mgmt", site=self.site, name="mgmt", kind="mgmt"),
            name="m",
            kind="mgmt",
        )
        c.hosts.add(h, Host.objects.create(site=self.site, name="p1", ip="10.0.100.31"))
        with mock.patch.object(timesrv, "_peer_ssh", return_value="NTP_OK serving time\n") as ssh:
            before = time.monotonic()
            timesrv.confirm(h, "10.0.100.1", lambda s: None)
        kw = ssh.call_args.kwargs
        self.assertEqual(kw["timeout"], timesrv.PEER_TIMEOUT)
        self.assertGreaterEqual(kw["deadline"], before + timesrv.PEER_TIMEOUT)
        self.assertLessEqual(kw["deadline"], time.monotonic() + timesrv.PEER_TIMEOUT)

    def test_587_a_peer_deadline_never_passes_the_search_end(self):
        from inventory.models import Cluster, Platform

        h = self.server(1, "")
        c = Cluster.objects.create(
            platform=Platform.objects.create(key="dc1.mgmt", site=self.site, name="mgmt", kind="mgmt"),
            name="m",
            kind="mgmt",
        )
        c.hosts.add(h, Host.objects.create(site=self.site, name="p1", ip="10.0.100.31"))
        t = iter([1000.0, 1175.0, 1177.0])  # start (end 1180); p1 chosen at 1175 with 5 s left; back at 1177
        with mock.patch.object(timesrv, "_peer_ssh", return_value="NTP_OK serving time\n") as ssh:
            timesrv.confirm(h, "10.0.100.1", lambda s: None, clock=lambda: next(t))
        self.assertEqual((ssh.call_args.kwargs["timeout"], ssh.call_args.kwargs["deadline"]), (5, 1180.0))

    def test_590_a_server_removed_during_the_check_is_left_out(self):
        m1, _m2 = self.server(1), self.server(2)

        def ssh(script):
            if "10.0.100.1 " in script:
                Host.objects.filter(pk=m1.pk).delete()
            return "NTP_OK serving time\n"

        ok, bad = timesrv.client_check(self.site, "10.0.100.50", ssh)
        self.assertEqual(ok, ["10.0.100.2"])
        self.assertEqual(bad, ["m1: removed during the check"])
        self.assertIsNone(timesrv.address_of(m1))
        self.assertFalse(timesrv.mark(m1, True, None, "peer"))

    def test_590_removed_after_being_chosen(self):
        m1, _m2 = self.server(1), self.server(2)

        def ssh(script):
            if "10.0.100.2 " in script:
                Host.objects.filter(pk=m1.pk).delete()
            return "NTP_OK serving time\n"

        ok, bad = timesrv.client_check(self.site, "10.0.100.50", ssh)
        self.assertEqual((ok, bad), (["10.0.100.2"], ["m1: removed during the check"]))

    def test_591_a_stale_one_of_four_is_replaced_by_the_next(self):
        servers = [self.server(n) for n in range(1, 7)]
        asked = []

        def ssh(script):
            asked.append(script)
            if "10.0.100.4 " in script:  # while m4 is asked, m1 moves
                Host.objects.filter(pk=servers[0].pk).update(ip_rev=5)
            return "NTP_OK serving time\n"

        ok, bad = timesrv.client_check(self.site, "10.0.100.50", ssh)
        self.assertEqual(ok, ["10.0.100.2", "10.0.100.3", "10.0.100.4", "10.0.100.5"])
        self.assertEqual(bad, ["m1: its address changed during the check"])
        self.assertEqual(len(asked), 5)  # m5 asked for m1's place; m6 not

    def test_591_the_read_that_stops_is_the_last(self):
        servers = [self.server(n) for n in range(1, 7)]
        reads = []
        from inventory import models

        orig = models.Host.objects.filter

        def counting(*a, **k):
            if "pk__in" in k:
                reads.append(1)
            return orig(*a, **k)

        with mock.patch.object(models.Host.objects, "filter", side_effect=counting):
            ok, bad = timesrv.client_check(self.site, "10.0.100.50", lambda script: "NTP_OK serving time\n")
        self.assertEqual(ok, ["10.0.100.1", "10.0.100.2", "10.0.100.3", "10.0.100.4"])
        self.assertEqual(len(reads), 1)  # one read decided the stop: no second one after it to drop a server
        self.assertEqual(len(servers), 6)  # m5, m6 never needed

    def test_591_every_chosen_stale_the_next_ones_are_asked(self):
        servers = [self.server(n) for n in range(1, 7)]

        def ssh(script):
            if "10.0.100.4 " in script:
                Host.objects.filter(pk__in=[x.pk for x in servers[:4]]).update(ip_rev=9)
            return "NTP_OK serving time\n"

        ok, bad = timesrv.client_check(self.site, "10.0.100.50", ssh)
        self.assertEqual(ok, ["10.0.100.5", "10.0.100.6"])
        self.assertEqual(len(bad), 4)

    def test_588_a_failure_never_erases_a_peer_confirmation_made_meanwhile(self):
        h = self.server(1, "self")

        def ssh(script):
            Host.objects.filter(pk=h.pk).update(time_confirmed_by="peer")  # another check confirms it meanwhile
            return "NTP_FAIL no NTP answer: timed out\n"

        ok, bad = timesrv.client_check(self.site, "10.0.100.50", ssh)
        self.assertEqual(ok, [])
        self.assertIsNotNone(Host.objects.get(pk=h.pk).time_served_at)  # kept
        Host.objects.filter(pk=h.pk).update(time_confirmed_by="self")
        timesrv.client_check(self.site, "10.0.100.50", lambda script: "NTP_FAIL no NTP answer: timed out\n")
        self.assertIsNone(Host.objects.get(pk=h.pk).time_served_at)  # still self-confirmed: no longer one

    def test_589_the_first_four_that_answer(self):
        for n in range(1, 7):
            self.server(n)
        asked = []

        def ssh(script):
            asked.append(script)
            return "NTP_FAIL timed out\n" if "10.0.100.2 " in script else "NTP_OK serving time\n"

        ok, bad = timesrv.client_check(self.site, "10.0.100.50", ssh)
        self.assertEqual(ok, ["10.0.100.1", "10.0.100.3", "10.0.100.4", "10.0.100.5"])
        self.assertEqual(len(asked), 5)  # m6 not asked
        self.assertEqual(bad, ["m2: timed out"])

    def test_587_one_deadline_for_the_direct_and_the_jump_host_attempts(self):
        import time

        from inventory import sshexec

        given = []

        def once(host, port, user, key, remote, jump, stdin, timeout, *a, **k):
            given.append((jump is not None, timeout))
            if jump is None:
                e = sshexec.SshError("no answer")
                e.connect = True
                clock[0] += 20  # the direct attempt took 20 s
                raise e
            return "NTP_OK serving time\n"

        clock = [1000.0]
        jump = {"host": "192.0.2.1", "port": 22, "user": "j", "key": "K"}
        with (
            mock.patch.object(sshexec, "_once", side_effect=once),
            mock.patch.object(sshexec, "jump_for", return_value=jump),
            mock.patch.object(time, "monotonic", side_effect=lambda: clock[0]),
        ):
            sshexec.run("192.0.2.5", 22, "sa", "KEY", "x", site="zk1", timeout=30, deadline=1030.0)
            self.assertEqual(given, [(False, 30), (True, 10)])  # the jump host gets only the 10 s left
            given.clear()
            clock[0] = 1029.5
            with self.assertRaisesMessage(sshexec.SshError, "the deadline passed"):
                sshexec.run("192.0.2.5", 22, "sa", "KEY", "x", site="zk1", timeout=30, deadline=1030.0)
            self.assertEqual(given, [])

    def test_587_a_result_after_the_deadline_is_not_used(self):
        from inventory.models import Cluster, Platform

        h = self.server(1, "")
        c = Cluster.objects.create(
            platform=Platform.objects.create(key="dc1.mgmt", site=self.site, name="mgmt", kind="mgmt"),
            name="m",
            kind="mgmt",
        )
        c.hosts.add(h, Host.objects.create(site=self.site, name="p1", ip="10.0.100.31"))
        t = iter([0, 170, 185])  # start; p1 asked at 170 s with 10 s left; its answer comes at 185 s
        with mock.patch.object(timesrv, "_peer_ssh", return_value="NTP_OK serving time\n"):
            with self.assertRaisesMessage(timesrv.Inconclusive, "p1: its result came after the deadline"):
                timesrv.confirm(h, "10.0.100.1", lambda s: None, clock=lambda: next(t))


class OtherSystems(TestCase):
    def test_ilo_vyos_and_vms_take_the_site_time_servers(self):
        from inventory import base_deploy, hw_views, nfconfig

        s = Site.objects.create(code="dc1", ntp_servers=["198.51.100.1"])
        for i in (1, 2, 3):
            Host.objects.create(
                site=s,
                name=f"m{i}",
                ip=f"10.0.100.{i}",
                service=hostservice.of_slot("mgmt"),
                time_served_at=timezone.now(),
                time_confirmed_by="peer",
            )
        h = Host.objects.create(site=s, name="x1", ip="10.0.100.21")
        self.assertEqual(hw_views.site_settings_of(h)["ntp"], ["10.0.100.1", "10.0.100.2"])  # an iLO holds two
        self.assertEqual(nfconfig._time_servers(s), ["10.0.100.1", "10.0.100.2", "10.0.100.3"])
        self.assertIn('"ntp": servers(site)', open(base_deploy.__file__).read())
        self.assertIn("ntp", recipes.sample_values()["site"])


class Migration0133(TestCase):
    def base(self):
        b = HostService.objects.get(key="base-os")
        return b, ConfigTemplate.objects.get(pk=b.install_template_id)

    def test_base_os_gets_its_time_step(self):
        b, t = self.base()
        self.assertTrue(t.body.rstrip().endswith("- systemd-timesyncd"))
        self.assertIn("- name: base-os time", t.body)
        c = next(c for c in b.configs if c["path"] == m0133().PATH)
        self.assertEqual(c["then"], "restart systemd-timesyncd")
        ct = ConfigTemplate.objects.get(pk=c["template"])  # by id
        self.assertEqual((ct.kind, ct.role), ("config", "base-os-timesyncd"))
        p = ServiceProfile.objects.filter(os="base-os").exclude(stype="host").order_by("key").first()
        out = recipes.render_script(p, ConfigTemplate.objects.in_bulk(), recipes.sample_values(p))
        self.assertIn("NTP=10.61.100.201 10.61.100.202 10.61.100.203", out)
        self.assertIn('step "base-os time: packages"', out)
        self.assertIn("systemctl restart systemd-timesyncd", out)

    def test_back_then_forward_and_a_second_forward(self):
        b, t = self.base()
        body, configs = t.body, list(b.configs)
        m0133().backward(apps, None)
        b2, t2 = self.base()
        self.assertNotIn("base-os time", t2.body)
        self.assertFalse([c for c in b2.configs if c["path"] == m0133().PATH])
        self.assertFalse(ConfigTemplate.objects.filter(role="base-os-timesyncd").exists())
        m0133().forward(apps, None)
        m0133().forward(apps, None)  # nothing more
        b3, t3 = self.base()
        self.assertEqual(t3.body, body)
        self.assertEqual([c["path"] for c in b3.configs], [c["path"] for c in configs])
        self.assertEqual(ConfigTemplate.objects.filter(role="base-os-timesyncd").count(), 1)

    def test_579_steps_that_cannot_take_text_appended_are_left(self):
        from io import StringIO

        m0133().backward(apps, None)
        b, t = self.base()
        for body in ("[{name: base-os, packages: [curl]}]\n", "- name: base-os\n  packages: [curl]\n...\n"):
            ConfigTemplate.objects.filter(pk=t.pk).update(body=body)
            self.assertIsNotNone(recipes.parse_steps(body))  # a valid recipe
            with mock.patch("sys.stdout", new_callable=StringIO) as out:
                m0133().forward(apps, None)
            self.assertEqual(ConfigTemplate.objects.get(pk=t.pk).body, body)  # left as it is
            self.assertIn("left to add by hand", out.getvalue())
            self.assertEqual(HostService.objects.get(pk=b.pk).configs, b.configs)
            self.assertFalse(ConfigTemplate.objects.filter(role="base-os-timesyncd").exists())
        self.assertIsNone(m0133()._appended("- a: [\n"))  # unreadable

    def test_a_site_whose_list_differs_is_named(self):
        from io import StringIO

        m0133().backward(apps, None)
        Site.objects.create(code="dc1", ntp_servers=["198.51.100.1"])
        with mock.patch("sys.stdout", new_callable=StringIO) as out:
            m0133().forward(apps, None)
        self.assertIn(
            "site dc1: NTP servers 198.51.100.1 are no longer read; its time servers: none yet", out.getvalue()
        )


class SettingsAndSite(TestCase):
    def setUp(self):
        self.sa = Base.staff(self, "sa@corp.kz", is_super_admin=True)
        self.client.force_login(self.sa)

    def post(self, value):
        return self.client.post(
            reverse("portal_settings"), {"tab": "customer", "section": "customers", "ntp_external": value}
        )

    def test_the_external_ntp_servers(self):
        self.post("0.pool.ntp.org\r\n192.0.2.5")
        self.assertEqual(PortalSettings.get().ntp_external, ["0.pool.ntp.org", "192.0.2.5"])
        for bad in (
            "a.example\nb.example\nc.example\nd.example\ne.example",
            "not_a_host",
            "a.example\na.example",
            "a.example b.example",  # commit-595: one per row
            "a.example,b.example",
        ):
            self.post(bad)
            self.assertEqual(PortalSettings.get().ntp_external, ["0.pool.ntp.org", "192.0.2.5"], bad)
        page = self.client.get(reverse("portal_settings") + "?tab=customer").content.decode()
        self.assertIn('name="ntp_external"', page)  # owner 2026-11-27 07:40: a list like the domains
        self.assertIn(">0.pool.ntp.org&#10;192.0.2.5</textarea>", page)
        self.assertNotIn("up to 4, separated by spaces", page)
        self.post("1.pool.ntp.org\r\n198.51.100.7\r\n")  # one per line, as the list sends it (commit-594: a new pair)
        self.assertEqual(PortalSettings.get().ntp_external, ["1.pool.ntp.org", "198.51.100.7"])
        self.post("")
        self.assertEqual(PortalSettings.get().ntp_external, [])

    def test_the_site_shows_its_time_servers_and_takes_no_ntp_list(self):
        from inventory.edit_views import SiteForm

        self.assertNotIn("ntp_servers", SiteForm().fields)
        s = Site.objects.create(code="dc1")
        Host.objects.create(
            site=s, name="m1", ip="10.0.100.1", service=hostservice.of_slot("mgmt"), time_served_at=timezone.now()
        )
        page = self.client.get(reverse("inventory")).content.decode()
        self.assertIn("Time servers", page)
        self.assertIn("10.0.100.1", page)
