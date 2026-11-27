"""T36 B4 (owner 2026-11-25 19:22 "go ahead", 19:25 QB4-1..3): the KVM hosts. B4a - each host is made from one host
service (kvm-mgmt, kvm-simple-fe, kvm-simple, kvm-shared, kvm-shared-gpu) found by its slot; the deployment's steps
follow its functions (frr only with frr). B4b - Deploy makes a KVM host without OpenNebula; Connect adds it, checks it
and enables it (QB4-1 "Always two actions", QB4-2 "Enable when checks pass")."""

from unittest import mock

from django.test import TestCase
from django.urls import reverse

from accounts.tests import Base
from inventory import hostdeploy, hostservice
from inventory.models import Cluster, Host, HostDeployment, Network, Platform, ServerProfile, ServiceProfile

from .tests_hostdeploy import KEY, world

EP = "http://10.201.100.10:2633/RPC2"  # the front end world() gives (commit-536: kept with the host id)


def _platform(site, kind, name):
    p = Platform.objects.create(key=f"{site.code}.{name}", site=site, name=name, kind=kind)
    c = Cluster.objects.create(platform=p, name=f"{name}-1", kind=kind if kind != "shared" else "shared")
    return p, c


@KEY
class HostService(TestCase):
    def setUp(self):
        self.h, self.c = world()
        self.site = self.h.site
        self.node = mock.patch("accounts.hostmedia.node_for", return_value="10.201.101.25")
        self.node.start()

    def tearDown(self):
        self.node.stop()

    def host(self, name, cluster):
        h = Host.objects.create(site=self.site, name=name, profile=self.h.profile, ip="10.201.100.50")
        cluster.hosts.add(h)
        return h

    def test_0126_slots(self):
        got = {s: hostservice.of_slot(s).key for s, _l in ServiceProfile.HOST_SLOTS}
        self.assertEqual(
            got,
            {
                "mgmt": "kvm-mgmt",
                "simple-fe": "kvm-simple-fe",
                "simple": "kvm-simple",
                "shared": "kvm-shared",
                "shared-gpu": "kvm-shared-gpu",
            },
        )

    def test_shared_and_gpu(self):
        self.assertEqual(hostservice.pick(self.h, self.c.platform).key, "kvm-shared")
        gpu = ServerProfile.objects.create(code="gpu1", gpu_count=2)
        self.h.profile = gpu
        self.assertEqual(hostservice.pick(self.h, self.c.platform).key, "kvm-shared-gpu")

    def test_mgmt_no_frr_no_evpn(self):
        p, c = _platform(self.site, "mgmt", "mgmt")
        h = self.host("mgmt01", c)
        self.assertEqual(hostservice.pick(h, p).key, "kvm-mgmt")
        self.assertNotIn("frr", hostservice.of_slot("mgmt").functions())
        Network.objects.filter(kind="evpn").delete()
        why = " | ".join(hostdeploy.problems(h, c))
        self.assertNotIn("EVPN", why)
        self.assertIn("EVPN", " | ".join(hostdeploy.problems(self.h, self.c)))  # a shared host still needs it

    def test_simple_first_three_by_name_then_kept(self):
        p, c = _platform(self.site, "simple", "simple")
        hosts = [self.host(n, c) for n in ("s04", "s02", "s01", "s03")]
        for h in hosts:
            hostservice.assign(h, p)
        got = {h.name: Host.objects.get(pk=h.pk).service.key for h in hosts}
        self.assertEqual(
            got, {"s01": "kvm-simple-fe", "s02": "kvm-simple-fe", "s03": "kvm-simple-fe", "s04": "kvm-simple"}
        )
        new = self.host("s00", c)  # first by name, but the front-end slots are taken
        self.assertEqual(hostservice.assign(new, p).key, "kvm-simple")
        Host.objects.filter(name="s02").update(name="s99")  # a renamed front-end host keeps its slot
        self.assertEqual(hostservice.assign(Host.objects.get(name="s99"), p).key, "kvm-simple-fe")

    def test_simple_fills_free_fe_slots_by_name(self):
        p, c = _platform(self.site, "simple", "simple")
        a = self.host("s05", c)
        hostservice.assign(a, p)
        self.host("s01", c)  # not deployed yet: before s09 by name
        b = self.host("s09", c)
        self.assertEqual(hostservice.pick(b, p).key, "kvm-simple-fe")  # s05, s01, s09 take the three
        self.host("s02", c)
        self.assertEqual(hostservice.pick(b, p).key, "kvm-simple")  # s01, s02 come first

    def test_simple_at_most_six(self):
        p, c = _platform(self.site, "simple", "simple")
        hosts = [self.host(f"s{i:02}", c) for i in range(7)]
        with self.assertRaisesMessage(hostservice.HostServiceError, "at most 6"):
            hostservice.pick(hosts[0], p)
        self.assertIn("at most 6", " ".join(hostdeploy.problems(hosts[0], c)))

    def test_no_gpu_host_on_mgmt(self):
        p, c = _platform(self.site, "mgmt", "mgmt")
        h = self.host("mgmt01", c)
        h.profile = ServerProfile.objects.create(code="gpu1", gpu_count=2)
        self.assertIn("no GPU hosts on a mgmt platform", " ".join(hostdeploy.problems(h, c)))

    def test_no_profile_for_a_slot(self):
        ServiceProfile.objects.filter(host_slot="shared").update(host_slot="")
        self.assertIn("no host service for shared", " ".join(hostdeploy.problems(self.h, self.c)))

    def test_steps_follow_functions(self):
        p, c = _platform(self.site, "mgmt", "mgmt")
        h = self.host("mgmt01", c)
        hostservice.assign(h, p)
        r = hostdeploy.Run(HostDeployment.objects.create(host=h, cluster=c))
        with mock.patch.object(hostdeploy.Run, "ssh", return_value="ok") as ssh:
            self.assertEqual(hostdeploy.s_frr(r), "no frr in its service (kvm-mgmt)")
            ssh.assert_not_called()
            self.assertNotIn("frr", hostdeploy.s_packages(r))
            self.assertIn(
                "frr", hostdeploy.packages(hostdeploy.Run(HostDeployment.objects.create(host=self.h, cluster=self.c)))
            )
            self.assertEqual(hostdeploy.s_gpu(r), "no gpu in its service (kvm-mgmt)")

    def test_start_keeps_the_service(self):
        with mock.patch("accounts.tasks.start"), mock.patch("accounts.ha.forbid_writes"):
            hostdeploy.start(None, self.h, self.c)
        self.assertEqual(Host.objects.get(pk=self.h.pk).service.key, "kvm-shared")

    def test_logging_reads_the_hosts_service(self):
        from inventory import logconf

        p, c = _platform(self.site, "mgmt", "mgmt")
        h = self.host("mgmt01", c)
        hostservice.assign(h, p)
        self.assertEqual([x.key for x in logconf._profiles_of(Host.objects.get(pk=h.pk))], ["kvm-mgmt"])
        self.assertEqual([x.key for x in logconf._profiles_of(self.h)], ["kvm-shared"])  # none yet: the type's

    def test_slot_is_one_profiles_and_kept_while_used(self):
        from inventory import services

        p = ServiceProfile.objects.get(host_slot="mgmt")
        q = ServiceProfile.objects.create(key="kvm-other", stype="host", roles=p.roles)
        data = {k: v for k, v in services.profile_initial(q).items() if v is not None}
        f = services.ProfileForm({**data, "host_slot": "mgmt"})
        self.assertTrue(f.is_valid(), f.errors)
        with self.assertRaisesMessage(services.ServiceError, "Host slot mgmt"):
            services.save_profile(None, q.pk, f.cleaned_data)
        pl, c = _platform(self.site, "mgmt", "mgmt")
        hostservice.assign(self.host("mgmt01", c), pl)
        data = {k: v for k, v in services.profile_initial(p).items() if v is not None}
        f = services.ProfileForm({**data, "host_slot": ""})
        self.assertTrue(f.is_valid(), f.errors)
        with self.assertRaisesMessage(services.ServiceError, "cannot change"):
            services.save_profile(None, p.pk, f.cleaned_data)

    def test_522_a_pending_deployment_holds_its_slot(self):
        p, c = _platform(self.site, "simple", "simple")
        for n in ("s01", "s02"):
            hostservice.assign(self.host(n, c), p)
        out = []
        for n in ("s03", "s04"):  # not members yet: their runs hold their places
            h = Host.objects.create(site=self.site, name=n, profile=self.h.profile, ip="10.201.100.60")
            with mock.patch("accounts.tasks.start"), mock.patch("accounts.ha.forbid_writes"):
                hostdeploy.start(None, h, c)
            out.append(Host.objects.get(pk=h.pk).service.key)
        self.assertEqual(out, ["kvm-simple-fe", "kvm-simple"])
        for n in ("s05", "s06"):
            self.host(n, c)
        late = Host.objects.create(site=self.site, name="s07", profile=self.h.profile, ip="10.201.100.61")
        with self.assertRaisesMessage(hostservice.HostServiceError, "at most 6"):
            hostservice.pick(late, p)
        HostDeployment.objects.filter(host__name="s04").update(state=HostDeployment.State.ABANDONED)
        self.assertEqual(hostservice.pick(late, p).key, "kvm-simple")  # an abandoned run frees its place

    def test_523_an_used_slot_cannot_move(self):
        from inventory import services

        ServiceProfile.objects.filter(host_slot="simple").update(host_slot="")
        sp = ServiceProfile.objects.get(host_slot="simple-fe")
        pl, c = _platform(self.site, "simple", "simple")
        hostservice.assign(self.host("s01", c), pl)
        data = {k: v for k, v in services.profile_initial(sp).items() if v is not None}
        f = services.ProfileForm({**data, "host_slot": "simple"})
        self.assertTrue(f.is_valid(), f.errors)
        with self.assertRaisesMessage(services.ServiceError, "cannot change"):
            services.save_profile(None, sp.pk, f.cleaned_data)
        f = services.ProfileForm({**data, "key": "kvm-simple-front"})  # a new name is fine
        self.assertTrue(f.is_valid(), f.errors)
        services.save_profile(None, sp.pk, f.cleaned_data)
        self.assertEqual(ServiceProfile.objects.get(pk=sp.pk).host_slot, "simple-fe")

    def test_524_the_run_keeps_its_functions(self):
        p, c = _platform(self.site, "mgmt", "mgmt")
        h = self.host("mgmt01", c)
        with mock.patch("accounts.tasks.start"), mock.patch("accounts.ha.forbid_writes"):
            dep = hostdeploy.start(None, h, c)
        self.assertNotIn("frr", dep.data["fns"])
        sp = ServiceProfile.objects.get(host_slot="mgmt")
        sp.roles = [{**sp.roles[0], "functions": sp.roles[0]["functions"] + ["frr"]}]
        sp.save()
        r = hostdeploy.Run(HostDeployment.objects.get(pk=dep.pk))
        self.assertNotIn("frr", r.fns)  # the profile changed; the run did not
        with mock.patch.object(hostdeploy.Run, "ssh") as ssh:
            self.assertTrue(hostdeploy.s_frr(r).startswith("no frr"))
            ssh.assert_not_called()

    def test_524_an_older_run_does_its_packages_again(self):
        dep = HostDeployment.objects.create(
            host=self.h,
            cluster=self.c,
            state=HostDeployment.State.FAILED,
            steps={k: {"status": "done"} for k in ("checks", "repos", "packages", "upgrade")}
            | {"tuned": {"status": "failed"}},
        )
        with mock.patch("accounts.tasks.start"), mock.patch("accounts.ha.forbid_writes"):
            got = hostdeploy.start(None, self.h, self.c)
        self.assertEqual(got.pk, dep.pk)
        got.refresh_from_db()
        self.assertIn("frr", got.data["fns"])
        self.assertEqual([k for k in ("repos", "packages", "upgrade") if got.steps[k]["status"] == "done"], ["upgrade"])

    def test_525_no_frr_source_without_frr(self):
        p, c = _platform(self.site, "mgmt", "mgmt")
        h = self.host("mgmt01", c)
        hostservice.assign(h, p)
        base = {"os": "26.04", "one": "7.4", "frr": "10.6"}
        text = hostdeploy.repos_script(hostdeploy.Run(HostDeployment.objects.create(host=h, cluster=c)), base)
        self.assertNotIn("deb.frrouting.org", text)
        self.assertIn("rm -f /etc/apt/sources.list.d/frrouting.list", text)
        shared = hostdeploy.Run(HostDeployment.objects.create(host=self.h, cluster=self.c))
        self.assertIn("deb.frrouting.org/frr/keys.gpg", hostdeploy.repos_script(shared, base))

    def test_522_an_abandoned_host_chooses_again(self):
        p, c = _platform(self.site, "simple", "simple")
        for n in ("s01", "s02"):
            hostservice.assign(self.host(n, c), p)
        first = Host.objects.create(site=self.site, name="s03", profile=self.h.profile, ip="10.201.100.60")
        with mock.patch("accounts.tasks.start"), mock.patch("accounts.ha.forbid_writes"):
            hostdeploy.start(None, first, c)
        self.assertEqual(Host.objects.get(pk=first.pk).service.key, "kvm-simple-fe")
        HostDeployment.objects.filter(host=first).update(state=HostDeployment.State.ABANDONED)
        repl = Host.objects.create(site=self.site, name="s09", profile=self.h.profile, ip="10.201.100.61")
        with mock.patch("accounts.tasks.start"), mock.patch("accounts.ha.forbid_writes"):
            hostdeploy.start(None, repl, c)
        self.assertEqual(Host.objects.get(pk=repl.pk).service.key, "kvm-simple-fe")
        first = Host.objects.get(pk=first.pk)
        with mock.patch("accounts.tasks.start"), mock.patch("accounts.ha.forbid_writes"):
            hostdeploy.start(None, first, c)
        self.assertEqual(Host.objects.get(pk=first.pk).service.key, "kvm-simple")  # never a 4th front end
        fe = Host.objects.filter(service__host_slot="simple-fe", name__in=["s01", "s02", "s03", "s09"]).count()
        self.assertEqual(fe, 3)

    def test_526_the_run_checks_with_its_functions(self):
        Network.objects.filter(kind="evpn").update(rrs=[])
        self.assertNotIn("route reflectors", " ".join(hostdeploy.problems(self.h, self.c, fns=["kvm"])))
        self.assertIn("route reflectors", " ".join(hostdeploy.problems(self.h, self.c, fns=["kvm", "frr"])))
        p, c = _platform(self.site, "mgmt", "mgmt")
        h = self.host("mgmt01", c)  # the profile has no frr now; a run kept it
        self.assertIn("route reflectors", " ".join(hostdeploy.problems(h, c, fns=["kvm", "frr"])))
        r = hostdeploy.Run(HostDeployment.objects.create(host=h, cluster=c, data={"fns": ["kvm", "frr"]}))
        with (
            mock.patch("accounts.hostmedia.check", return_value=(True, "ok")),
            mock.patch("accounts.hostmedia.on_site_node", return_value=(True, "")),
            mock.patch("accounts.hostmedia.current", return_value={}),
            mock.patch("accounts.hostmedia.locked"),
            mock.patch.object(hostdeploy.ilo, "power_state", return_value="Off"),
            mock.patch.object(hostdeploy.Run, "c", new_callable=mock.PropertyMock),
            mock.patch.object(hostdeploy, "problems", return_value=[]) as pr,
            mock.patch.object(hostdeploy, "_one"),
            mock.patch.object(hostdeploy, "_one_host_id", return_value=None),
            mock.patch.object(hostdeploy, "firewall_rules"),
        ):
            hostdeploy.s_checks(r)
        self.assertEqual(pr.call_args.kwargs["fns"], ["kvm", "frr"])

    def test_527_an_older_run_does_frr_and_its_reboot_again(self):
        done = {"status": "done"}
        dep = HostDeployment.objects.create(
            host=self.h,
            cluster=self.c,
            state=HostDeployment.State.FAILED,
            steps={k: done for k in ("checks", "repos", "packages", "frr", "gpu", "keys", "reboot")}
            | {"one_add": {"status": "failed"}},
        )
        with mock.patch("accounts.tasks.start"), mock.patch("accounts.ha.forbid_writes"):
            hostdeploy.start(None, self.h, self.c)
        dep.refresh_from_db()
        again = [k for k, v in dep.steps.items() if v.get("status") == ""]
        self.assertEqual(sorted(again), ["frr", "packages", "reboot", "repos"])

    def test_527_a_kept_snapshot_with_frr_repairs_skipped_steps(self):
        done = {"status": "done"}
        dep = HostDeployment.objects.create(
            host=self.h,
            cluster=self.c,
            state=HostDeployment.State.FAILED,
            data={"fns": ["kvm", "frr"]},
            steps={
                "checks": done,
                "repos": {"status": "done", "detail": "OpenNebula repository"},
                "packages": {"status": "done", "detail": "opennebula-node-kvm, tuned"},
                "frr": {"status": "done", "detail": "no frr in its service (kvm-mgmt)"},
                "keys": done,
                "reboot": done,
                "one_add": {"status": "failed"},
            },
        )
        with mock.patch("accounts.tasks.start"), mock.patch("accounts.ha.forbid_writes"):
            hostdeploy.start(None, self.h, self.c)
        dep.refresh_from_db()
        again = sorted(k for k, v in dep.steps.items() if v.get("status") == "")
        self.assertEqual(again, ["frr", "packages", "reboot", "repos"])
        self.assertEqual(dep.steps["keys"]["status"], "done")
        ok = {  # steps that did FRR: nothing to repair
            "repos": {"status": "done", "detail": "OpenNebula and FRR repositories"},
            "packages": {"status": "done", "detail": "opennebula-node-kvm, frr, tuned"},
            "frr": {"status": "done", "detail": "2 established"},
            "reboot": done,
        }
        dep.steps = ok
        self.assertEqual(hostdeploy.frr_again(dep), [])


@KEY
class Pages(Base):
    def setUp(self):
        self.h, self.c = world()
        self.client.force_login(self.staff("sa@corp.kz", is_super_admin=True))

    def test_services_page_lists_the_hosts_and_the_host_its_service(self):
        Host.objects.filter(name="kca201").delete()  # world()'s time server (T36 B5d): no mgmt host here
        hostservice.assign(self.h, self.c.platform)
        sp = ServiceProfile.objects.get(host_slot="shared")
        r = self.client.get(reverse("infra_services"))
        self.assertContains(r, "kvm014")
        self.assertContains(r, "a shared or external platform&#x27;s hosts")
        self.assertContains(r, "No host yet: made for a mgmt platform")
        r = self.client.get(reverse("infra_site", args=["zk1"]) + "?tab=hosts")
        self.assertContains(r, f"?sel=sp-{sp.pk}")


@KEY
class Connect(TestCase):
    def setUp(self):
        from inventory.models import PlatformControl

        self.h, self.c = world()
        self.assertTrue(PlatformControl.objects.filter(key=self.c.platform.key).exists())
        HostDeployment.objects.create(host=self.h, cluster=self.c, state="done")  # Deploy finished (commit-529)
        Host.objects.filter(pk=self.h.pk).update(state="planned")
        self.h.refresh_from_db()
        self.calls = []
        self.patches = [mock.patch("accounts.tasks.start"), mock.patch("accounts.ha.forbid_writes")]
        for x in self.patches:
            x.start()

    def tearDown(self):
        for x in self.patches:
            x.stop()

    def fake(self, keys, fail_at=None):
        def make(k):
            def fn(r):
                self.calls.append(k)
                if k == fail_at:
                    raise hostdeploy.DeployError("boom")
                if k == "one_add":
                    r.keep(one_id=7, one_endpoint=EP)
                return f"{k} ok"

            return fn

        return {k: make(k) for k in keys}

    def test_deploy_has_no_opennebula_and_connect_has_it(self):
        for k in ("keys", "one_add", "one_wait", "compare", "records"):
            self.assertNotIn(k, hostdeploy.STEP_KEYS)
        self.assertEqual(
            hostdeploy.CONNECT_KEYS, ["ready", "keys", "one_add", "one_wait", "compare", "enable", "records"]
        )
        self.assertEqual(hostdeploy.STEP_KEYS[-2:], ["joined", "cleanup"])

    def test_joined_leaves_the_host_planned_in_its_cluster(self):
        self.c.hosts.remove(self.h)
        Host.objects.filter(pk=self.h.pk).update(state="deployed")  # a reinstall: planned until Connect
        dep = HostDeployment.objects.create(
            host=self.h, cluster=self.c, data={"local": ["x"], "local_dev": "/dev/x 960000000000"}
        )
        with mock.patch.object(hostdeploy, "refresh_health", return_value="OK"):
            got = hostdeploy.s_joined(hostdeploy.Run(dep))
        self.assertIn("planned until Connect", got)
        h = Host.objects.get(pk=self.h.pk)
        self.assertEqual(h.state, "planned")
        self.assertTrue(self.c.hosts.filter(pk=h.pk).exists())
        self.assertTrue(h.storages.filter(name="kvm014-local").exists())

    def test_deploy_checks_need_no_front_end(self):
        from inventory.models import PlatformControl

        PlatformControl.objects.all().delete()
        r = hostdeploy.Run(HostDeployment.objects.create(host=self.h, cluster=self.c, data={"fns": ["kvm", "frr"]}))
        with (
            mock.patch("accounts.hostmedia.check", return_value=(True, "ok")),
            mock.patch("accounts.hostmedia.on_site_node", return_value=(True, "")),
            mock.patch("accounts.hostmedia.current", return_value={}),
            mock.patch("accounts.hostmedia.locked"),
            mock.patch.object(hostdeploy.ilo, "power_state", return_value="Off"),
            mock.patch.object(hostdeploy.Run, "c", new_callable=mock.PropertyMock),
            mock.patch.object(hostdeploy, "problems", return_value=[]),
            mock.patch.object(hostdeploy, "firewall_rules"),
            mock.patch.object(hostdeploy, "_one", side_effect=AssertionError("no front end to ask")),
        ):
            self.assertIn("all checks passed", hostdeploy.s_checks(r))

    def test_connect_enables_and_deploys(self):
        Host.objects.filter(pk=self.h.pk).update(state="planned")
        dep = hostdeploy.start_connect(None, self.h, self.c)
        self.assertEqual(dep.kind, "connect")
        with mock.patch.dict(hostdeploy.STEPS, self.fake(hostdeploy.CONNECT_KEYS)):
            self.assertTrue(hostdeploy.run(HostDeployment.objects.get(pk=dep.pk)))
        self.assertEqual(self.calls, hostdeploy.CONNECT_KEYS)
        self.assertEqual(HostDeployment.objects.get(pk=dep.pk).state, "done")
        self.assertEqual([x[1] for x in hostdeploy.steps_view(dep)], hostdeploy.CONNECT_KEYS)

    def test_a_failed_check_leaves_it_non_compliant(self):
        Host.objects.filter(pk=self.h.pk).update(state="planned")
        dep = hostdeploy.start_connect(None, self.h, self.c)
        with mock.patch.dict(hostdeploy.STEPS, self.fake(hostdeploy.CONNECT_KEYS, fail_at="compare")):
            self.assertFalse(hostdeploy.run(HostDeployment.objects.get(pk=dep.pk)))
        self.assertEqual(Host.objects.get(pk=self.h.pk).state, "non-compliant")
        self.assertNotIn("enable", self.calls)  # never enabled

    def test_a_failure_before_opennebula_changes_nothing(self):
        Host.objects.filter(pk=self.h.pk).update(state="planned")
        dep = hostdeploy.start_connect(None, self.h, self.c)
        with mock.patch.dict(hostdeploy.STEPS, self.fake(hostdeploy.CONNECT_KEYS, fail_at="keys")):
            self.assertFalse(hostdeploy.run(HostDeployment.objects.get(pk=dep.pk)))
        self.assertEqual(Host.objects.get(pk=self.h.pk).state, "planned")
        again = hostdeploy.start_connect(None, self.h, self.c)  # resumed, the same run
        self.assertEqual(again.pk, dep.pk)

    def test_connect_refusals(self):
        from inventory.models import Platform

        def why():
            self.h.refresh_from_db()
            self.c.refresh_from_db()
            return " | ".join(hostdeploy.connect_problems(self.h, self.c))

        self.assertEqual(why(), "")
        later = HostDeployment.objects.create(host=self.h, cluster=self.c, state="abandoned")  # commit-529
        self.assertIn("its deployment is abandoned", why())
        with self.assertRaisesMessage(hostdeploy.DeployError, "deploy it (or Resume) first"):
            hostdeploy.start_connect(None, self.h, self.c)
        later.delete()
        HostDeployment.objects.all().delete()  # never deployed by the portal
        self.assertIn("never deployed", why())
        Host.objects.filter(pk=self.h.pk).update(state="non-compliant")  # installed before B4b: its check
        self.assertEqual(why(), "")
        Host.objects.filter(pk=self.h.pk).update(state="deployed")  # commit-531
        self.assertIn("only a planned or non-compliant host", why())
        Host.objects.filter(pk=self.h.pk).update(state="non-compliant")
        Cluster.objects.filter(pk=self.c.pk).update(state="retired")
        self.assertIn("is retired", why())
        Cluster.objects.filter(pk=self.c.pk).update(state="")
        Platform.objects.filter(pk=self.c.platform_id).update(fe_vip="")  # commit-528: no front end address
        self.c.platform.refresh_from_db()
        self.assertIn("no front end endpoint", why())
        self.c.hosts.remove(self.h)
        self.assertIn("not in", why())

    def test_528_power_off_without_a_front_end(self):
        from inventory.models import Platform

        Platform.objects.filter(pk=self.c.platform_id).update(fe_vip="")
        dep = HostDeployment.objects.create(host=self.h, cluster=self.c)
        r = hostdeploy.Run(dep)
        with (
            mock.patch.object(hostdeploy, "_one", side_effect=AssertionError("no front end to ask")),
            mock.patch.object(hostdeploy.ilo, "power"),
            mock.patch.object(hostdeploy.ilo, "eject_all"),
            mock.patch.object(hostdeploy.Run, "c", new_callable=mock.PropertyMock),
        ):
            self.assertIn("OpenNebula not asked", hostdeploy.s_power_off(r))
        Platform.objects.filter(pk=self.c.platform_id).update(fe_vip="10.201.100.10")
        r = hostdeploy.Run(HostDeployment.objects.get(pk=dep.pk))
        with (
            mock.patch.object(hostdeploy, "_one", side_effect=hostdeploy.DeployError("no answer")),
            mock.patch.object(hostdeploy.ilo, "power") as power,
        ):
            with self.assertRaisesMessage(hostdeploy.DeployError, "no answer"):  # configured: always asked
                hostdeploy.s_power_off(r)
            power.assert_not_called()

    def test_530_a_failure_after_enable_disables_it_again(self):
        dep = hostdeploy.start_connect(None, self.h, self.c)
        one = mock.Mock()
        one.client.url = EP
        one.xml.return_value.findtext.side_effect = lambda k, v="4": "kvm014" if k == "NAME" else v
        with (
            mock.patch.dict(hostdeploy.STEPS, self.fake(hostdeploy.CONNECT_KEYS, fail_at="records")),
            mock.patch.object(hostdeploy, "_one", return_value=one),
        ):
            self.assertFalse(hostdeploy.run(HostDeployment.objects.get(pk=dep.pk), sleep=lambda s: None))
        one.call.assert_called_with("one.host.status", 7, 1)
        dep.refresh_from_db()
        self.assertEqual(dep.steps["enable"]["status"], "")  # enabled again on Resume
        self.assertIn("disabled again", dep.log)
        self.assertEqual(Host.objects.get(pk=self.h.pk).state, "non-compliant")
        with (
            mock.patch.dict(hostdeploy.STEPS, self.fake(hostdeploy.CONNECT_KEYS, fail_at="records")),
            mock.patch.object(hostdeploy, "_one", side_effect=hostdeploy.DeployError("down")),
        ):
            hostdeploy.start_connect(None, self.h, self.c)
            hostdeploy.run(HostDeployment.objects.get(pk=dep.pk), sleep=lambda s: None)
        self.assertIn("disabling it again is NOT confirmed", HostDeployment.objects.get(pk=dep.pk).log)

    def test_530_a_rollback_still_enabled_is_not_confirmed(self):
        one = mock.Mock()
        one.client.url = EP
        one.xml.return_value.findtext.side_effect = lambda k, v="2": (
            "kvm014" if k == "NAME" else v
        )  # OpenNebula keeps it enabled
        dep = HostDeployment.objects.create(
            host=self.h,
            cluster=self.c,
            kind="connect",
            data={"one_id": 7, "one_endpoint": EP},
            steps={"enable": {"status": "done"}},
        )
        r = hostdeploy.Run(dep, sleep=lambda s: None)
        with mock.patch.object(hostdeploy, "_one", return_value=one):
            msg = hostdeploy.disable_again(r)
        self.assertIn("NOT confirmed", msg)
        dep.refresh_from_db()
        self.assertEqual(dep.steps["enable"]["status"], "")
        self.assertIn("NOT confirmed", dep.steps["enable"]["detail"])

    def test_531_a_resume_into_a_retired_cluster_is_refused(self):
        dep = hostdeploy.start_connect(None, self.h, self.c)
        HostDeployment.objects.filter(pk=dep.pk).update(steps={"ready": {"status": "done"}})
        Cluster.objects.filter(pk=self.c.pk).update(state="retired")
        with mock.patch.dict(hostdeploy.STEPS, self.fake(hostdeploy.CONNECT_KEYS)):
            self.assertFalse(hostdeploy.run(HostDeployment.objects.get(pk=dep.pk)))
        self.assertEqual(self.calls, [])
        self.assertIn("connect refused: cluster", HostDeployment.objects.get(pk=dep.pk).log)
        self.assertEqual(Host.objects.get(pk=self.h.pk).state, "planned")

    def test_533_a_refused_resume_disables_an_interrupted_enable(self):
        dep = hostdeploy.start_connect(None, self.h, self.c)
        steps = {k: {"status": "done"} for k in ("ready", "keys", "one_add", "one_wait", "compare")}
        steps["enable"] = {"status": "failed", "detail": "interrupted: the portal's process ended (a restart)"}
        HostDeployment.objects.filter(pk=dep.pk).update(steps=steps, data={**dep.data, "one_id": 7, "one_endpoint": EP})
        Cluster.objects.filter(pk=self.c.pk).update(state="retired")  # after Resume was queued
        one = mock.Mock()
        one.client.url = EP
        one.xml.return_value.findtext.side_effect = lambda k, v="4": "kvm014" if k == "NAME" else v
        with (
            mock.patch.dict(hostdeploy.STEPS, self.fake(hostdeploy.CONNECT_KEYS)),
            mock.patch.object(hostdeploy, "_one", return_value=one),
        ):
            self.assertFalse(hostdeploy.run(HostDeployment.objects.get(pk=dep.pk), sleep=lambda s: None))
        self.assertEqual(self.calls, [])
        one.call.assert_called_once_with("one.host.status", 7, 1)
        log = HostDeployment.objects.get(pk=dep.pk).log
        self.assertIn("connect refused: cluster", log)
        self.assertIn("disabled again (state 4)", log)
        self.assertEqual(Host.objects.get(pk=self.h.pk).state, "non-compliant")
        one.xml.return_value.findtext.side_effect = lambda k, v="2": (
            "kvm014" if k == "NAME" else v
        )  # still enabled: the manual action is said
        HostDeployment.objects.filter(pk=dep.pk).update(state="running")
        with mock.patch.object(hostdeploy, "_one", return_value=one):
            hostdeploy.run(HostDeployment.objects.get(pk=dep.pk), sleep=lambda s: None)
        self.assertIn("NOT confirmed", HostDeployment.objects.get(pk=dep.pk).log)

    def test_534_records_ends_the_run_with_the_host(self):
        one = mock.Mock()
        one.client.url = EP
        info = mock.Mock()
        info.findtext.side_effect = lambda k: {"NAME": "kvm014", "CLUSTER_ID": "2", "STATE": "2"}.get(k)
        one.xml.return_value = info
        dep = HostDeployment.objects.create(
            host=self.h, cluster=self.c, kind="connect", state="running", data={"one_id": 7, "one_endpoint": EP}
        )
        with (
            mock.patch.object(hostdeploy, "_one", return_value=one),
            mock.patch.object(hostdeploy, "_one_cluster_id", return_value=2),
            mock.patch.object(hostdeploy, "refresh_health", return_value="OK"),
        ):
            hostdeploy.s_records(hostdeploy.Run(dep))
        dep.refresh_from_db()  # as a restart right after the step would find it
        self.assertEqual((dep.state, dep.steps["records"]["status"]), ("done", "done"))
        self.assertEqual(Host.objects.get(pk=self.h.pk).state, "deployed")
        self.assertEqual(hostdeploy.mark_interrupted(dep), False)  # nothing left to interrupt

    def test_534_a_stranded_run_is_finished_by_resume(self):
        steps = {k: {"status": "done"} for k in ("ready", "keys", "one_add", "one_wait", "compare", "enable")}
        steps["records"] = {"status": "failed", "detail": "interrupted: the portal's process ended (a restart)"}
        dep = HostDeployment.objects.create(
            host=self.h,
            cluster=self.c,
            kind="connect",
            state="failed",
            steps=steps,
            data={"one_id": 7, "one_endpoint": EP},
        )
        Host.objects.filter(pk=self.h.pk).update(state="deployed")  # saved before the run's end (older code)
        self.h.refresh_from_db()
        got = hostdeploy.start_connect(None, self.h, self.c)
        self.assertEqual((got.pk, got.data.get("finishing")), (dep.pk, True))
        with mock.patch.dict(hostdeploy.STEPS, self.fake(hostdeploy.CONNECT_KEYS)):
            self.assertTrue(hostdeploy.run(HostDeployment.objects.get(pk=dep.pk)))
        self.assertEqual(self.calls, ["records"])
        HostDeployment.objects.filter(pk=dep.pk).update(state="done")
        with self.assertRaisesMessage(hostdeploy.DeployError, "only a planned or non-compliant host"):
            hostdeploy.start_connect(None, self.h, self.c)  # a fresh Connect of a deployed host: refused

    def test_536_never_disables_a_host_it_cannot_prove_its_own(self):
        one = mock.Mock()
        one.client.url = hostdeploy.endpoint_of(self.c.platform)  # the client's own endpoint
        one.xml.return_value.findtext.side_effect = lambda k: "other-host" if k == "NAME" else "2"
        dep = HostDeployment.objects.create(
            host=self.h,
            cluster=self.c,
            kind="connect",
            data={"one_id": 7, "one_endpoint": hostdeploy.endpoint_of(self.c.platform)},
            steps={"enable": {"status": "failed"}},
        )
        with mock.patch.object(hostdeploy, "_one", return_value=one):
            msg = hostdeploy.disable_again(hostdeploy.Run(dep, sleep=lambda s: None))
        one.call.assert_not_called()
        self.assertIn("is 'other-host', not kvm014", msg)
        self.assertIn("disable it by hand", msg)
        one.xml.return_value.findtext.side_effect = lambda k: "kvm014" if k == "NAME" else "4"
        HostDeployment.objects.filter(pk=dep.pk).update(data={"one_id": 7, "one_endpoint": "http://10.9.9.9:2633/RPC2"})
        with mock.patch.object(hostdeploy, "_one", return_value=one):
            msg = hostdeploy.disable_again(hostdeploy.Run(HostDeployment.objects.get(pk=dep.pk), sleep=lambda s: None))
        one.call.assert_not_called()  # the client talks to another front end
        self.assertIn("not http://10.9.9.9:2633/RPC2", msg)
        one.client.url = "http://10.9.9.9:2633/RPC2"  # the very endpoint that added it: acted on
        with mock.patch.object(hostdeploy, "_one", return_value=one):
            msg = hostdeploy.disable_again(hostdeploy.Run(HostDeployment.objects.get(pk=dep.pk), sleep=lambda s: None))
        one.call.assert_called_once_with("one.host.status", 7, 1)
        self.assertIn("disabled again", msg)

    def test_536_534_a_run_without_its_endpoint_is_never_acted_on(self):
        one = mock.Mock()
        one.client.url = EP
        one.xml.return_value.findtext.side_effect = lambda k: "kvm014" if k == "NAME" else "2"
        dep = HostDeployment.objects.create(
            host=self.h, cluster=self.c, kind="connect", data={"one_id": 7}, steps={"enable": {"status": "failed"}}
        )
        with (
            mock.patch.object(hostdeploy, "_one", return_value=one),
            mock.patch.object(hostdeploy, "_one_cluster_id", return_value=2),
        ):
            msg = hostdeploy.disable_again(hostdeploy.Run(dep, sleep=lambda s: None))
            self.assertIn("kept no endpoint for #7: reconcile OpenNebula host #7 by hand", msg)
            with self.assertRaisesMessage(hostdeploy.DeployError, "kept no endpoint"):
                hostdeploy.s_records(hostdeploy.Run(dep))
        one.call.assert_not_called()

    def test_538_a_refused_finishing_run_leaves_its_host_non_compliant(self):
        steps = {k: {"status": "done"} for k in ("ready", "keys", "one_add", "one_wait", "compare", "enable")}
        steps["records"] = {"status": "failed", "detail": "interrupted: the portal's process ended (a restart)"}
        dep = HostDeployment.objects.create(
            host=self.h,
            cluster=self.c,
            kind="connect",
            state="failed",
            steps=steps,
            data={"one_id": 7, "one_endpoint": EP},
        )
        Host.objects.filter(pk=self.h.pk).update(state="deployed")  # an older run, cut off in Records
        self.h.refresh_from_db()
        self.assertEqual(hostdeploy.start_connect(None, self.h, self.c).pk, dep.pk)
        Cluster.objects.filter(pk=self.c.pk).update(state="retired")  # then retired: the Resume is refused
        one = mock.Mock()
        one.client.url = EP
        one.xml.return_value.findtext.side_effect = lambda k: "kvm014" if k == "NAME" else "4"
        with mock.patch.object(hostdeploy, "_one", return_value=one):
            self.assertFalse(hostdeploy.run(HostDeployment.objects.get(pk=dep.pk), sleep=lambda s: None))
        one.call.assert_called_once_with("one.host.status", 7, 1)
        self.assertIn("disabled again (state 4)", HostDeployment.objects.get(pk=dep.pk).log)
        self.assertEqual(Host.objects.get(pk=self.h.pk).state, "non-compliant")
        Host.objects.filter(pk=self.h.pk).update(state="retired")  # a retired host stays retired
        HostDeployment.objects.filter(pk=dep.pk).update(state="running")
        with mock.patch.object(hostdeploy, "_one", return_value=one):
            hostdeploy.run(HostDeployment.objects.get(pk=dep.pk), sleep=lambda s: None)
        self.assertEqual(Host.objects.get(pk=self.h.pk).state, "retired")

    def test_539_one_wait_never_touches_an_unproven_host(self):
        one = mock.Mock()
        one.client.url = EP
        one.xml.return_value.findtext.side_effect = lambda k: "kvm014" if k == "NAME" else "4"
        dep = HostDeployment.objects.create(
            host=self.h, cluster=self.c, kind="connect", data={"one_id": 7}, steps={"one_add": {"status": "done"}}
        )
        with mock.patch.object(hostdeploy, "_one", return_value=one):
            with self.assertRaisesMessage(hostdeploy.DeployError, "kept no endpoint for #7"):
                hostdeploy.s_one_wait(hostdeploy.Run(dep, sleep=lambda s: None))
            HostDeployment.objects.filter(pk=dep.pk).update(
                data={"one_id": 7, "one_endpoint": "http://10.9.9.9:2633/RPC2"}
            )
            with self.assertRaisesMessage(hostdeploy.DeployError, "where #7 was added"):
                hostdeploy.s_one_wait(hostdeploy.Run(HostDeployment.objects.get(pk=dep.pk), sleep=lambda s: None))
        one.call.assert_not_called()  # no RESERVED_CPU written
        self.assertEqual(one.xml.call_count, 0)  # not even polled (the name check is after the endpoint's)

    def test_536_one_add_records_the_clients_endpoint(self):
        one = mock.Mock()
        one.client.url = "http://10.201.100.10:2633/RPC2"
        dep = HostDeployment.objects.create(host=self.h, cluster=self.c, kind="connect")
        with (
            mock.patch.object(hostdeploy, "_one", return_value=one),
            mock.patch.object(hostdeploy, "_one_host_id", return_value=7),
            mock.patch.object(hostdeploy, "_one_cluster_id", return_value=2),
        ):
            one.xml.return_value.findtext.return_value = "2"
            hostdeploy.s_one_add(hostdeploy.Run(dep))
        self.assertEqual(HostDeployment.objects.get(pk=dep.pk).data["one_endpoint"], "http://10.201.100.10:2633/RPC2")

    def test_534_records_never_finishes_on_another_registration(self):
        one = mock.Mock()
        one.client.url = "http://10.9.9.9:2633/RPC2"
        info = mock.Mock()
        info.findtext.side_effect = lambda k: {"NAME": "kvm099", "CLUSTER_ID": "2", "STATE": "2"}.get(k)
        one.xml.return_value = info
        dep = HostDeployment.objects.create(
            host=self.h,
            cluster=self.c,
            kind="connect",
            data={"one_id": 7, "one_endpoint": "http://10.201.100.10:2633/RPC2", "finishing": True},
        )
        Host.objects.filter(pk=self.h.pk).update(state="deployed")
        with (
            mock.patch.object(hostdeploy, "_one", return_value=one),
            mock.patch.object(hostdeploy, "_one_cluster_id", return_value=2),
        ):
            with self.assertRaisesMessage(hostdeploy.DeployError, "where #7 was added"):
                hostdeploy.s_records(hostdeploy.Run(dep))
            one.client.url = "http://10.201.100.10:2633/RPC2"
            with self.assertRaisesMessage(hostdeploy.DeployError, "is 'kvm099', not kvm014"):
                hostdeploy.s_records(hostdeploy.Run(dep))
        self.assertNotEqual(HostDeployment.objects.get(pk=dep.pk).state, "done")

    def test_537_the_rollback_comes_before_the_run_ends_and_resumes_anyway(self):
        dep = hostdeploy.start_connect(None, self.h, self.c)
        steps = {k: {"status": "done"} for k in ("ready", "keys", "one_add", "one_wait", "compare")}
        HostDeployment.objects.filter(pk=dep.pk).update(steps=steps, data={**dep.data, "one_id": 7, "one_endpoint": EP})
        Cluster.objects.filter(pk=self.c.pk).update(state="retired")
        seen = []

        def cut_off(r, wait=60):  # a restart in the middle of the rollback
            row = HostDeployment.objects.get(pk=r.dep.pk)
            seen.append((row.state, row.data.get("rollback")))
            raise KeyboardInterrupt

        with mock.patch.object(hostdeploy, "disable_again", side_effect=cut_off):
            with self.assertRaises(KeyboardInterrupt):
                hostdeploy.run(HostDeployment.objects.get(pk=dep.pk))
        self.assertEqual(seen, [("running", "pending")])  # still running, the rollback pending
        hostdeploy.mark_interrupted(HostDeployment.objects.get(pk=dep.pk))  # the reaper
        self.h.refresh_from_db()
        again = hostdeploy.start_connect(None, self.h, self.c)  # the cluster is retired: resumed for the rollback
        self.assertEqual(again.pk, dep.pk)
        with mock.patch.object(hostdeploy, "disable_again", return_value="OpenNebula host #7 disabled again (state 4)"):
            self.assertFalse(hostdeploy.run(HostDeployment.objects.get(pk=dep.pk)))
        done = HostDeployment.objects.get(pk=dep.pk)
        self.assertEqual((done.state, done.data["rollback"]), ("failed", "OpenNebula host #7 disabled again (state 4)"))
        # still a host in OpenNebula, the run unfinished: Resume stays possible (each time the rollback again)
        self.assertEqual(hostdeploy.start_connect(None, self.h, self.c).pk, dep.pk)

    def test_537_a_restart_before_the_marker_still_resumes_the_rollback(self):
        dep = hostdeploy.start_connect(None, self.h, self.c)
        steps = {k: {"status": "done"} for k in ("ready", "keys", "one_add", "one_wait", "compare")}
        steps["enable"] = {"status": "running"}
        HostDeployment.objects.filter(pk=dep.pk).update(steps=steps, data={**dep.data, "one_id": 7, "one_endpoint": EP})
        hostdeploy.mark_interrupted(HostDeployment.objects.get(pk=dep.pk))  # cut off, no marker ever written
        Cluster.objects.filter(pk=self.c.pk).update(state="retired")
        self.assertNotIn("rollback", HostDeployment.objects.get(pk=dep.pk).data)
        again = hostdeploy.start_connect(None, self.h, self.c)
        self.assertEqual(again.pk, dep.pk)
        with mock.patch.object(
            hostdeploy, "disable_again", return_value="OpenNebula host #7 disabled again (state 4)"
        ) as d:
            self.assertFalse(hostdeploy.run(HostDeployment.objects.get(pk=dep.pk)))
        d.assert_called_once()

    def test_535_the_page_reads_nothing_more(self):
        last = hostdeploy.latest_deploy(self.h)
        mine = list(self.h.clusters.select_related("platform"))
        with self.assertNumQueries(0):
            t = hostdeploy.connect_target(self.h, last=last, clusters=mine)
            self.assertEqual(t, self.c)
            self.assertEqual(
                hostdeploy.connect_problems(self.h, t, last=last, members={c.pk for c in mine}, fe=True), []
            )

    def test_531_enable_and_records_check_the_portal_again(self):
        one = mock.Mock()
        one.client.url = EP
        info = mock.Mock()
        info.findtext.side_effect = lambda k: {"NAME": "kvm014", "CLUSTER_ID": "2", "STATE": "2"}.get(k)
        one.xml.return_value = info
        dep = HostDeployment.objects.create(
            host=self.h, cluster=self.c, kind="connect", data={"one_id": 7, "one_endpoint": EP}
        )
        Cluster.objects.filter(pk=self.c.pk).update(state="retired")
        with (
            mock.patch.object(hostdeploy, "_one", return_value=one),
            mock.patch.object(hostdeploy, "_one_cluster_id", return_value=2),
        ):
            with self.assertRaisesMessage(hostdeploy.DeployError, "is retired"):
                hostdeploy.s_enable(hostdeploy.Run(dep, sleep=lambda s: None))
            one.call.assert_not_called()
            with self.assertRaisesMessage(hostdeploy.DeployError, "is retired"):
                hostdeploy.s_records(hostdeploy.Run(dep))
        self.assertEqual(Host.objects.get(pk=self.h.pk).state, "planned")

    def test_530_enable_checks_the_cluster_and_records_the_state(self):
        one = mock.Mock()
        one.client.url = EP
        info = mock.Mock()
        info.findtext.side_effect = lambda k: {"NAME": "kvm014", "CLUSTER_ID": "9", "STATE": "4"}.get(k)
        one.xml.return_value = info
        dep = HostDeployment.objects.create(
            host=self.h, cluster=self.c, kind="connect", data={"one_id": 7, "one_endpoint": EP}
        )
        with (
            mock.patch.object(hostdeploy, "_one", return_value=one),
            mock.patch.object(hostdeploy, "_one_cluster_id", return_value=2),
        ):
            with self.assertRaisesMessage(hostdeploy.DeployError, "in cluster #9"):
                hostdeploy.s_enable(hostdeploy.Run(dep))
            one.call.assert_not_called()
            info.findtext.side_effect = lambda k: {"NAME": "kvm014", "CLUSTER_ID": "2", "STATE": "4"}.get(k)
            with self.assertRaisesMessage(hostdeploy.DeployError, "is not enabled"):
                hostdeploy.s_records(hostdeploy.Run(dep))

    def test_enable_and_records(self):
        one = mock.Mock()
        one.client.url = EP
        info = mock.Mock()
        info.findtext.side_effect = lambda k: {"NAME": "kvm014", "CLUSTER_ID": "2", "STATE": "2"}.get(k)
        one.xml.return_value = info
        dep = HostDeployment.objects.create(
            host=self.h, cluster=self.c, kind="connect", data={"one_id": 7, "one_endpoint": EP}
        )
        r = hostdeploy.Run(dep, sleep=lambda s: None)
        with (
            mock.patch.object(hostdeploy, "_one", return_value=one),
            mock.patch.object(hostdeploy, "_one_cluster_id", return_value=2),
        ):
            self.assertIn("#7 enabled", hostdeploy.s_enable(r))
        one.call.assert_called_once_with("one.host.status", 7, 0)
        Host.objects.filter(pk=self.h.pk).update(state="non-compliant")
        with (
            mock.patch.object(hostdeploy, "_one", return_value=one),
            mock.patch.object(hostdeploy, "_one_cluster_id", return_value=2),
            mock.patch.object(hostdeploy, "refresh_health", return_value="OK"),
        ):
            self.assertIn(
                "deployed and enabled", hostdeploy.s_records(hostdeploy.Run(HostDeployment.objects.get(pk=dep.pk)))
            )
        self.assertEqual(Host.objects.get(pk=self.h.pk).state, "deployed")


@KEY
class ConnectPages(Base):
    def setUp(self):
        self.h, self.c = world()
        self.client.force_login(self.staff("sa@corp.kz", is_super_admin=True))
        Host.objects.filter(pk=self.h.pk).update(state="planned")
        HostDeployment.objects.create(host=self.h, cluster=self.c, state="done")

    def test_host_and_cluster_buttons(self):
        r = self.client.get(reverse("infra_site", args=["zk1"]) + "?tab=hosts")
        self.assertContains(r, reverse("inv_host_connect", args=["zk1", "kvm014"]))
        r = self.client.get(reverse("infra_site", args=["zk1"]) + "?tab=clusters")
        self.assertContains(r, reverse("inv_cluster_connect", args=["zk1", self.c.pk]))
        self.assertContains(r, "Connect 1 host")
        with mock.patch("accounts.tasks.start"), mock.patch("accounts.ha.forbid_writes"):
            self.client.post(reverse("inv_host_connect", args=["zk1", "kvm014"]))
        self.assertTrue(HostDeployment.objects.filter(host=self.h, kind="connect").exists())

    def test_532_a_retired_older_cluster_is_not_the_target(self):
        old = Cluster.objects.create(platform=self.c.platform, name="aaa-old", kind="shared", state="retired")
        old.hosts.add(self.h)  # sorts first, retired, kept as a membership
        HostDeployment.objects.filter(host=self.h).update(cluster=self.c)
        self.assertEqual(hostdeploy.connect_target(self.h), self.c)
        r = self.client.get(reverse("infra_site", args=["zk1"]) + "?tab=hosts")
        self.assertContains(r, reverse("inv_host_connect", args=["zk1", "kvm014"]))
        with mock.patch("accounts.tasks.start"), mock.patch("accounts.ha.forbid_writes"):
            self.client.post(reverse("inv_host_connect", args=["zk1", "kvm014"]))
        self.assertEqual(HostDeployment.objects.get(host=self.h, kind="connect").cluster, self.c)
        HostDeployment.objects.filter(host=self.h).delete()  # installed before: its one live cluster
        Host.objects.filter(pk=self.h.pk).update(state="non-compliant")
        self.assertEqual(hostdeploy.connect_target(self.h), self.c)
        Cluster.objects.create(platform=self.c.platform, name="zzz-other", kind="shared").hosts.add(self.h)
        self.assertIsNone(hostdeploy.connect_target(self.h))  # two live clusters: none chosen

    def test_cluster_connect_starts_each_waiting_host(self):
        with mock.patch("accounts.tasks.start"), mock.patch("accounts.ha.forbid_writes"):
            r = self.client.post(reverse("inv_cluster_connect", args=["zk1", self.c.pk]), follow=True)
        self.assertContains(r, "Connect started: kvm014")
        self.assertEqual(HostDeployment.objects.filter(kind="connect").count(), 1)


@KEY
class HostsFile(TestCase):
    """B4c (rules v2 B6, QB4-3): the portal's block of /etc/hosts on a mgmt or simple platform's hosts."""

    def setUp(self):
        from inventory import hostsfile

        self.hf = hostsfile
        self.h, self.c = world()
        self.site = self.h.site
        self.p, self.mc = _platform(self.site, "mgmt", "mgmt")
        Platform.objects.filter(pk=self.p.pk).update(fe_vip="10.201.100.30", fe={"hostname": "szk1mgmtfe"})
        self.p.refresh_from_db()
        self.hosts = []
        for i, n in enumerate(("mgmt02", "mgmt01", "mgmt03")):
            h = Host.objects.create(site=self.site, name=n, ip=f"10.201.100.{21 + i}", state="deployed")
            self.mc.hosts.add(h)
            self.hosts.append(h)

    def test_the_block(self):
        text = self.hf.block(self.p)
        self.assertEqual(
            text.splitlines(),
            [
                self.hf.BEGIN,
                "10.201.100.22 mgmt01.example.cloud mgmt01",
                "10.201.100.21 mgmt02.example.cloud mgmt02",
                "10.201.100.23 mgmt03.example.cloud mgmt03",
                "10.201.100.30 szk1mgmtfe.example.cloud szk1mgmtfe",
                self.hf.END,
            ],
        )
        self.assertFalse(self.hf.applies(self.c.platform))  # shared: never
        Host.objects.filter(name="mgmt03").update(state="retired")
        self.assertNotIn("mgmt03", self.hf.block(self.p))

    def test_no_vip_yet_and_no_domain(self):
        from tenants.models import PortalSettings

        Platform.objects.filter(pk=self.p.pk).update(fe_vip="")
        self.p.refresh_from_db()
        self.assertNotIn("szk1mgmtfe", self.hf.block(self.p))
        self.assertIn("VIP or name is not set yet", self.hf.missing_fe(self.p))
        PortalSettings.objects.filter(pk=PortalSettings.get().pk).update(internal_domain="")
        with self.assertRaisesMessage(self.hf.HostsFileError, "internal domain"):
            self.hf.block(self.p)

    def run_script(self, text, getent=None, start=None, stub_flock=None):
        import os
        import shlex
        import shutil
        import subprocess
        import sys
        import tempfile

        # commit-556: the hosts have flock (util-linux); where the tests run it may be missing (macOS) - then the same
        # lock (flock(2) on the descriptor the script opened, its last argument: `flock -w 60 9`) taken by Python
        if stub_flock is None:
            stub_flock = not shutil.which("flock")
        flock = (
            f"flock() {{ {shlex.quote(sys.executable)} -c 'import fcntl, sys; fcntl.flock(int(sys.argv[1]), fcntl.LOCK_EX)' "
            '"${@: -1}"; }\n'
            if stub_flock
            else ""
        )
        d = self.d = getattr(self, "d", None) or tempfile.mkdtemp(dir=os.environ.get("HOME"))
        f = self.f = os.path.join(d, "hosts")
        if start is not None:
            with open(f, "w") as fh:
                fh.write(start)
        # getent as /etc/hosts would answer: the first line naming it in the file
        getent = getent or ("getent() { awk -v n=\"$2\" '{for(i=2;i<=NF;i++) if($i==n){print; exit}}' " + f + "; }")
        sc = (
            self.hf.script(text)
            .replace("/etc/hosts.claugine-prev", f + ".prev")
            .replace("/etc/hosts", f)
            .replace("/run/claugine-hosts.lock", os.path.join(d, "lock"))
        )
        return subprocess.run(["bash", "-c", flock + getent + "\n" + sc], capture_output=True, text=True)

    def test_556_the_python_flock_stands_in_for_the_missing_command(self):
        start = "127.0.0.1 localhost\n"
        p = self.run_script(self.hf.block(self.p), start=start, stub_flock=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("10.201.100.22 mgmt01.example.cloud mgmt01", open(self.f).read())
        p = self.run_script(self.hf.block(self.p), start="127.0.0.1 localhost\n10.9.9.9 mgmt01\n", stub_flock=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(open(self.f).read(), "127.0.0.1 localhost\n10.9.9.9 mgmt01\n")  # put back

    def test_the_script_keeps_the_rest_of_the_file(self):
        start = f"127.0.0.1 localhost\n{self.hf.BEGIN}\n10.0.0.9 old.example old\n{self.hf.END}\n::1 ip6-localhost\n"
        p = self.run_script(self.hf.block(self.p), start=start)
        self.assertEqual(p.returncode, 0, p.stderr)
        got = open(self.f).read()
        self.assertIn("127.0.0.1 localhost\n::1 ip6-localhost\n", got)
        self.assertNotIn("old.example", got)
        self.assertEqual(got.count(self.hf.BEGIN), 1)
        self.assertIn("10.201.100.22 mgmt01.example.cloud mgmt01", got)
        self.assertIn("old.example", open(self.f + ".prev").read())
        self.assertEqual(self.run_script(self.hf.block(self.p)).returncode, 0)  # again: one block
        self.assertEqual(open(self.f).read().count(self.hf.BEGIN), 1)

    def test_541_each_write_renders_the_records_of_that_moment(self):
        stale = Platform.objects.get(pk=self.p.pk)
        Platform.objects.filter(pk=self.p.pk).update(fe_vip="10.201.100.77")
        sent = []
        self.hf.write(self.hosts[0], stale, ssh=sent.append)
        self.assertIn("10.201.100.77 szk1mgmtfe", sent[0])

    def test_544_a_name_on_another_address_puts_the_file_back(self):
        start = "127.0.0.1 localhost\n10.9.9.9 mgmt01\n"  # an older line wins for mgmt01
        p = self.run_script(self.hf.block(self.p), start=start)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("mgmt01=10.9.9.9(not 10.201.100.22)", p.stderr)
        self.assertEqual(open(self.f).read(), start)  # the previous file is back
        p = self.run_script(self.hf.block(self.p), start="127.0.0.1 localhost\n")
        self.assertEqual(p.returncode, 0, p.stderr)  # every FQDN and short name on its own address

    def test_540_a_retired_membership_is_not_the_platform(self):
        moved = self.hosts[1]  # mgmt01
        Cluster.objects.filter(pk=self.mc.pk).update(state="retired")
        live, lc = _platform(self.site, "mgmt", "mgmt2")
        Platform.objects.filter(pk=live.pk).update(fe_vip="10.201.100.40", fe={"hostname": "fe2"})
        lc.hosts.add(self.hosts[2])
        self.c.hosts.add(moved)  # now a shared platform's host; its old mgmt membership kept, retired
        self.assertEqual(self.hf.targets(self.p), [])
        rows = self.hf.rows_of(self.hf.block(self.p))
        self.assertEqual([r[2] for r in rows], ["szk1mgmtfe"])
        self.assertEqual([p.key for p in self.hf.platforms_of(moved)], ["zk1.cloud"])

    def test_545_a_failing_host_fails_the_task(self):
        def fake(h, p, text):
            if h.name == "mgmt02":
                raise RuntimeError("no route")
            return "ok"

        task = mock.Mock(result={})
        with mock.patch.object(self.hf, "_ssh", side_effect=fake):
            with self.assertRaisesMessage(self.hf.RefreshFailed, "not written on 1 of 3 host(s): mgmt02"):
                self.hf.refresh_task(self.p, task=task)
        self.assertEqual(task.result["hosts"]["mgmt01"], "ok, 4 lines")  # every host tried, each result kept
        with mock.patch.object(self.hf, "_ssh", return_value="ok"):
            self.assertEqual(len(self.hf.refresh_task(self.p)["hosts"]), 3)

    def test_540_a_host_moved_after_the_list_was_made_is_skipped(self):
        listed = self.hf.targets(self.p)
        self.assertEqual(len(listed), 3)
        moved = listed[0]
        Cluster.objects.filter(pk=self.mc.pk).update(state="retired")  # then: its cluster retired, the host moved
        self.c.hosts.add(moved)
        calls = []
        with (
            mock.patch.object(self.hf, "targets", return_value=listed),
            mock.patch.object(self.hf, "_ssh", side_effect=lambda h, p, t: calls.append(h.name)),
        ):
            out = self.hf.refresh_all(self.p)
        self.assertEqual(calls, [])
        self.assertTrue(all(v.startswith("skipped") for v in out.values()))

    def test_541_540_every_write_and_edit_takes_the_platforms_lock_first(self):
        from inventory import ops

        order = []
        real = self.hf.lock

        def spy(platforms, site=True):
            order.append(("lock", sorted(p.pk for p in platforms if p is not None)))
            return real(platforms, site=site)

        with (
            mock.patch.object(self.hf, "lock", side_effect=spy),
            mock.patch.object(self.hf, "_ssh", side_effect=lambda h, p, t: order.append(("ssh", h.name))),
        ):
            self.hf.write_target(self.hosts[0].pk, self.p)
        self.assertEqual(order, [("lock", [self.p.pk]), ("ssh", "mgmt02")])
        from inventory.models import HostsFileLock

        self.assertTrue(HostsFileLock.objects.filter(platform=self.p).exists())
        edits = [
            lambda: ops.cluster_remove_host(None, "zk1", "mgmt", self.mc.name, "mgmt03"),
            lambda: ops.host_save(None, "zk1", "mgmt01", None, "10.201.100.28", new=False),
            lambda: ops.host_remove(None, "zk1", "mgmt02"),
            lambda: ops.platform_save(None, "zk1", "mgmt", {}),
            lambda: ops.cluster_remove(None, "zk1", "mgmt", self.mc.name),
        ]
        for edit in edits:
            with mock.patch.object(self.hf, "lock", wraps=real) as lk, mock.patch("accounts.tasks.start"):
                edit()
            self.assertIn(self.p.pk, [p.pk for p in lk.call_args_list[0].args[0] if p is not None])
        self.assertEqual(self.hf.lock([self.c.platform]), None)  # shared: no lock row needed
        self.assertFalse(HostsFileLock.objects.filter(platform=self.c.platform).exists())

    def test_541_deploy_and_retire_take_the_lock(self):
        from inventory import hostretire

        new = Host.objects.create(site=self.site, name="mgmt04", ip="10.201.100.24", state="planned")
        dep = HostDeployment.objects.create(host=new, cluster=self.mc)
        with (
            mock.patch.object(self.hf, "lock") as lk,
            mock.patch.object(self.hf, "write", return_value=5),
            mock.patch.object(self.hf, "refresh"),
            mock.patch.object(hostdeploy, "refresh_health", return_value="OK"),
        ):
            hostdeploy.s_joined(hostdeploy.Run(dep))
        self.assertEqual(lk.call_args_list[0].args[0][0].pk, self.p.pk)
        dep = HostDeployment.objects.create(host=self.hosts[0], cluster=self.mc, kind="retire")
        with mock.patch.object(self.hf, "lock") as lk, mock.patch.object(self.hf, "refresh"):
            hostretire.s_records(hostdeploy.Run(dep))
        self.assertEqual(lk.call_args_list[0].args[0][0].pk, self.p.pk)

    def test_550_551_one_lock_order(self):
        from inventory import ops
        from inventory.models import HostsFileLock, Site

        order = []
        real_site = Site.objects.select_for_update
        real_lock = HostsFileLock.objects.select_for_update
        real_platform = Platform.objects.select_for_update

        def tag(name, real):
            def f(*a, **k):
                order.append(name)
                return real(*a, **k)

            return f

        with (
            mock.patch.object(Site.objects, "select_for_update", side_effect=tag("site", real_site)),
            mock.patch.object(HostsFileLock.objects, "select_for_update", side_effect=tag("hosts-file", real_lock)),
            mock.patch.object(Platform.objects, "select_for_update", side_effect=tag("platform", real_platform)),
            mock.patch("accounts.tasks.start"),
        ):
            self.hf.lock([self.p])
            self.assertEqual(order, ["hosts-file", "site"])  # commit-552: the platform's lock, then the site
            order.clear()
            Cluster.objects.filter(pk=self.mc.pk).delete()  # a mgmt platform has one cluster
            ops.cluster_add(None, "zk1", "mgmt", "mgmt-2_zk1", "mgmt", "", [])
            self.assertEqual(order[:3], ["hosts-file", "site", "platform"])  # as platform_save takes them
            order.clear()
            ops.platform_save(None, "zk1", "mgmt", {})
            self.assertEqual(order[:3], ["hosts-file", "site", "site"])

    def test_552_writers_never_lock_the_site(self):
        from inventory.models import Site

        other, oc = _platform(self.site, "shared", "other")
        with (
            mock.patch.object(Site.objects, "select_for_update", wraps=Site.objects.select_for_update) as site_lock,
            mock.patch.object(self.hf, "_ssh", return_value="ok"),
        ):
            self.hf.write_target(self.hosts[0].pk, self.p)
            self.hf.write(self.hosts[0], self.p, ssh=lambda t: "ok")
        site_lock.assert_not_called()
        calls = []
        with mock.patch.object(
            self.hf, "lock", side_effect=lambda ps, site=True: calls.append([p.pk for p in ps if p])
        ):
            from inventory import ops

            ops.platform_save(None, "zk1", "other", {})  # another platform of the site: never this one's lock
        self.assertNotIn(self.p.pk, [pk for c in calls for pk in c])

    def test_553_a_platform_made_meanwhile_is_not_saved_unlocked(self):
        from inventory import ops

        real = Platform.objects.filter
        calls = {"n": 0}

        def lookup(*a, **k):  # the first lookup sees no platform (its create not committed yet)
            q = real(*a, **k)
            if k.get("key") == "zk1.mgmt" and calls["n"] == 0:
                calls["n"] += 1
                return real(pk=-1)
            return q

        with mock.patch.object(Platform.objects, "filter", side_effect=lookup), mock.patch("accounts.tasks.start"):
            with self.assertRaisesMessage(ops.EditError, "was just created or changed by another request: save again"):
                ops.platform_save(None, "zk1", "mgmt", {"fe_vip": "10.201.100.55"})
        self.assertEqual(Platform.objects.get(pk=self.p.pk).fe_vip, "10.201.100.30")  # nothing saved
        with mock.patch("accounts.tasks.start"):
            ops.platform_save(None, "zk1", "mgmt", {})  # an ordinary save still goes through

    def test_553_the_checked_row_is_saved_and_rename_takes_the_same_order(self):
        from inventory import ops

        other, _oc = _platform(self.site, "shared", "spare")
        Platform.objects.filter(pk=self.p.pk).update(key="zk1.away", name="away")  # renamed away after the check
        Platform.objects.filter(pk=other.pk).update(key="zk1.mgmt", name="mgmt")  # another moved into the key
        with mock.patch("accounts.tasks.start"):
            ops._platform_save(None, "zk1", "mgmt", {"fe_vip": "10.201.100.66"}, pk=self.p.pk)
        self.assertNotEqual(Platform.objects.get(pk=other.pk).fe_vip, "10.201.100.66")  # never the replacement
        Platform.objects.filter(pk=other.pk).update(key="zk1.spare", name="spare")
        Platform.objects.filter(pk=self.p.pk).update(key="zk1.mgmt", name="mgmt")
        calls = []
        with mock.patch.object(
            self.hf, "lock", side_effect=lambda ps, site=True: calls.append([p.pk for p in ps if p])
        ):
            ops.platform_rename(None, "zk1", "mgmt", "mgmt9")
        self.assertEqual(calls, [[self.p.pk]])

    def test_554_a_rename_never_acts_on_a_replacement(self):
        from inventory import ops

        other, _oc = _platform(self.site, "shared", "spare")
        real = Platform.objects.filter
        seen = {"n": 0}

        def lookup(*a, **k):  # the first lookup finds the original; then another takes its key
            if k.get("key") == "zk1.mgmt" and seen["n"] == 0:
                seen["n"] += 1
                q = real(*a, **k)
                list(q)
                Platform.objects.filter(pk=self.p.pk).update(key="zk1.away", name="away")
                Platform.objects.filter(pk=other.pk).update(key="zk1.mgmt", name="mgmt")
                return real(pk=self.p.pk)
            return real(*a, **k)

        with mock.patch.object(Platform.objects, "filter", side_effect=lookup):
            with self.assertRaisesMessage(ops.EditError, "renamed or replaced by another request meanwhile"):
                ops.platform_rename(None, "zk1", "mgmt", "mgmt9")
        self.assertFalse(Platform.objects.filter(key="zk1.mgmt9").exists())  # nothing renamed (all rolled back)

    def test_546_no_temporary_file_is_left(self):
        import glob
        import os

        tmp = os.environ.get("TMPDIR", "/tmp")
        before = set(glob.glob(os.path.join(tmp, "tmp.*")))
        self.run_script(self.hf.block(self.p), start="127.0.0.1 localhost\n")
        self.run_script(self.hf.block(self.p), start="127.0.0.1 localhost\n10.9.9.9 mgmt01\n")  # fails, put back
        self.assertEqual(set(glob.glob(os.path.join(tmp, "tmp.*"))), before)

    def test_547_the_task_keeps_every_hosts_result(self):
        task = mock.Mock(result={})
        with mock.patch.object(self.hf, "_ssh", side_effect=RuntimeError("x" * 3000)):
            with self.assertRaises(self.hf.RefreshFailed) as cm:
                self.hf.refresh_task(self.p, task=task)
        self.assertEqual(sorted(task.result["hosts"]), ["mgmt01", "mgmt02", "mgmt03"])
        self.assertLess(len(str(cm.exception)), 200)  # a short error naming the hosts

    def test_543_connect_refreshes_once_it_is_in_service(self):
        one = mock.Mock()
        one.client.url = EP
        info = mock.Mock()
        info.findtext.side_effect = lambda k: {"NAME": "mgmt01", "CLUSTER_ID": "2", "STATE": "2"}.get(k)
        one.xml.return_value = info
        h = self.hosts[1]
        Host.objects.filter(pk=h.pk).update(state="planned")
        HostDeployment.objects.create(host=h, cluster=self.mc, state="done")
        from inventory.models import PlatformControl

        PlatformControl.objects.create(key=self.p.key, mode="execution")
        dep = HostDeployment.objects.create(
            host=h, cluster=self.mc, kind="connect", state="running", data={"one_id": 7, "one_endpoint": EP}
        )
        with (
            mock.patch.object(hostdeploy, "_one", return_value=one),
            mock.patch.object(hostdeploy, "_one_cluster_id", return_value=2),
            mock.patch.object(hostdeploy, "refresh_health", return_value="OK"),
            mock.patch.object(hostdeploy.Run, "ssh", return_value="ok"),
            mock.patch.object(self.hf, "refresh") as refresh,
        ):
            hostdeploy.s_records(hostdeploy.Run(dep))
        self.assertIn("mgmt01 connected", refresh.call_args.args[2])

    def test_542_inventory_edits_refresh_it(self):
        from inventory import ops

        cases = [
            lambda: ops.cluster_remove_host(None, "zk1", "mgmt", self.mc.name, "mgmt03"),
            lambda: ops.host_remove(None, "zk1", "mgmt02"),
            lambda: ops.cluster_remove(None, "zk1", "mgmt", self.mc.name),
        ]
        for case in cases:
            with mock.patch.object(self.hf, "refresh") as refresh, self.captureOnCommitCallbacks(execute=True):
                case()
            self.assertEqual(refresh.call_args.args[1].pk, self.p.pk)
        planned = Host.objects.create(site=self.site, name="mgmt09", ip="10.201.100.29", state="planned")
        Cluster.objects.filter(pk=self.mc.pk).update(state="")
        self.mc.hosts.add(planned)
        with mock.patch.object(self.hf, "refresh") as refresh:
            ops.host_save(None, "zk1", "mgmt09", None, "10.201.100.29", new=False, rename="mgmt10")
        self.assertIn("mgmt10's address or name changed", refresh.call_args.args[2])

    def test_543_connect_writes_the_block_of_the_moment(self):
        one = mock.Mock()
        one.client.url = EP
        info = mock.Mock()
        info.findtext.side_effect = lambda k: {"NAME": "mgmt01", "CLUSTER_ID": "2", "STATE": "2"}.get(k)
        one.xml.return_value = info
        from inventory.models import PlatformControl

        PlatformControl.objects.create(key=self.p.key, mode="execution")
        h = self.hosts[1]
        Host.objects.filter(pk=h.pk).update(state="planned")
        HostDeployment.objects.create(host=h, cluster=self.mc, state="done")
        Platform.objects.filter(pk=self.p.pk).update(fe_vip="10.201.100.88")  # changed between Deploy and Connect
        dep = HostDeployment.objects.create(
            host=h, cluster=self.mc, kind="connect", state="running", data={"one_id": 7, "one_endpoint": EP}
        )
        sent = []
        with (
            mock.patch.object(hostdeploy, "_one", return_value=one),
            mock.patch.object(hostdeploy, "_one_cluster_id", return_value=2),
            mock.patch.object(hostdeploy, "refresh_health", return_value="OK"),
            mock.patch.object(hostdeploy.Run, "ssh", side_effect=lambda s, **k: sent.append(s) or "ok"),
        ):
            got = hostdeploy.s_records(hostdeploy.Run(dep))
        self.assertIn("/etc/hosts: 4 names", got)
        self.assertIn("10.201.100.88 szk1mgmtfe", sent[0])

    def test_joined_writes_it_and_refreshes_the_others(self):
        new = Host.objects.create(site=self.site, name="mgmt04", ip="10.201.100.24", state="planned")
        dep = HostDeployment.objects.create(host=new, cluster=self.mc)
        r = hostdeploy.Run(dep)
        sent = []
        with (
            mock.patch.object(hostdeploy.Run, "ssh", side_effect=lambda s, **k: sent.append(s) or "ok"),
            mock.patch.object(hostdeploy, "refresh_health", return_value="OK"),
            mock.patch.object(self.hf, "refresh") as refresh,
        ):
            got = hostdeploy.s_joined(r)
        self.assertIn("/etc/hosts: 5 names", got)
        self.assertIn("10.201.100.24 mgmt04.example.cloud mgmt04", sent[0])
        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.kwargs["exclude"], new)
        # a shared platform's host: nothing written
        dep2 = HostDeployment.objects.create(host=self.h, cluster=self.c)
        with (
            mock.patch.object(hostdeploy.Run, "ssh") as ssh,
            mock.patch.object(hostdeploy, "refresh_health", return_value="OK"),
        ):
            self.assertNotIn("/etc/hosts", hostdeploy.s_joined(hostdeploy.Run(dep2)))
        ssh.assert_not_called()

    def test_refresh_all_goes_on_past_a_failing_host(self):
        def fake(h, p, text):
            if h.name == "mgmt02":
                raise RuntimeError("no route")
            return "ok"

        with mock.patch.object(self.hf, "_ssh", side_effect=fake):
            out = self.hf.refresh_all(self.p)
        self.assertEqual(out["mgmt02"], "FAILED: no route")
        self.assertEqual((out["mgmt01"], out["mgmt03"]), ("ok, 4 lines", "ok, 4 lines"))

    def test_the_changes_that_refresh_it(self):
        from inventory import ops

        with mock.patch("accounts.tasks.start") as start, self.captureOnCommitCallbacks(execute=True):
            ops.host_save(None, "zk1", "mgmt01", None, "10.201.100.29", new=False)
        self.assertIn("mgmt01's address or name changed", start.call_args.args[1])
        with mock.patch("accounts.tasks.start") as start, self.captureOnCommitCallbacks(execute=True):
            self.p.fe_vip = "10.201.100.31"
            ops.platform_save(None, "zk1", "mgmt", {})
        self.assertIn("VIP or name changed", start.call_args.args[1])
        with mock.patch("accounts.tasks.start") as start, self.captureOnCommitCallbacks(execute=True):
            self.assertIsNone(self.hf.refresh(None, self.c.platform, "x"))  # shared: never
        start.assert_not_called()

    def test_retire_refreshes_it(self):
        from inventory import hostretire

        dep = HostDeployment.objects.create(host=self.hosts[0], cluster=self.mc, kind="retire")
        with mock.patch.object(self.hf, "refresh") as refresh:
            hostretire.s_records(hostdeploy.Run(dep))
        self.assertIn("mgmt02 retired", refresh.call_args.args[2])


@KEY
class HostsFilePages(Base):
    def setUp(self):
        self.h, self.c = world()
        self.p, self.mc = _platform(self.h.site, "mgmt", "mgmt")
        Platform.objects.filter(pk=self.p.pk).update(fe_vip="10.201.100.30", fe={"hostname": "szk1mgmtfe"})
        self.client.force_login(self.staff("sa@corp.kz", is_super_admin=True))

    def test_button_and_post(self):
        url = reverse("inv_cluster_hosts_file", args=["zk1", self.mc.pk])
        r = self.client.get(reverse("infra_site", args=["zk1"]) + "?tab=clusters")
        self.assertContains(r, url)
        self.assertNotContains(r, reverse("inv_cluster_hosts_file", args=["zk1", self.c.pk]))  # shared: none
        with mock.patch("accounts.tasks.start") as start, self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(url, follow=True)
        self.assertContains(r, "is being written")
        start.assert_called_once()
        r = self.client.post(reverse("inv_cluster_hosts_file", args=["zk1", self.c.pk]), follow=True)
        self.assertContains(r, "only on the hosts of a mgmt or simple platform")
