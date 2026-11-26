"""Settings → Reports. u01 (owner 2026-10-31): the schedule, recipients and the redirect; review dates highlighted (a
week for a POC, 3 months for a client). Owner 2026-11-13 (job 2026-11-09_resource-dashboard): four reports, one per
role, replace the six; name and roles editable, one Day list, a sent history; a support engineer, a manager and a
KAM get their own organizations only."""

from datetime import timedelta
from unittest import mock

from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from accounts import reports
from accounts.mailtemplates import T
from accounts.models import AuditLog, ReportSent
from accounts.tests import Base, valid_bin
from inventory.models import IPSubnet, Site
from tenants.models import Organization, OrgKind, PortalSettings, PrefixAssignment, QuotaRecord, Tenant


def at(day, hh, mm=0):
    return timezone.make_aware(timezone.datetime(2026, 11, day, hh, mm), timezone.get_current_timezone())


class Reports(Base):
    def setUp(self):
        self.sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.rep = self.staff("rep@corp.kz", is_sales=True)
        self.other_rep = self.staff("rep2@corp.kz", is_sales=True)
        self.org = Organization.objects.create(name="ТОО «ALPHA»", tax_id=valid_bin(), sales_rep=self.rep)
        self.other = Organization.objects.create(
            name="ТОО «BETA»", tax_id=valid_bin("0105400013"), sales_rep=self.other_rep
        )
        today = timezone.localdate()
        self.poc = Tenant.objects.create(
            organization=self.org,
            name="alphapoc",
            title="P",
            approval="approved",
            stage="poc",
            date_review=today + timedelta(days=5),
        )
        self.client_t = Tenant.objects.create(
            organization=self.org,
            name="alphaprod",
            title="C",
            approval="approved",
            stage="client",
            date_review=today + timedelta(days=200),
        )
        Tenant.objects.create(
            organization=self.other,
            name="betapoc",
            title="B",
            approval="approved",
            stage="poc",
            date_review=today + timedelta(days=2),
        )

    def test_review_alert(self):
        self.assertEqual(self.poc.review_alert, "soon")  # a POC: a week
        self.assertEqual(self.client_t.review_alert, "")  # a client: 3 months
        self.client_t.date_review = timezone.localdate() + timedelta(days=60)
        self.assertEqual(self.client_t.review_alert, "soon")
        self.client_t.date_review = timezone.localdate()
        self.assertEqual(self.client_t.review_alert, "due")
        self.client.force_login(self.sa)
        r = self.client.get(reverse("tenant_list"))
        self.assertContains(r, ">Review<")
        self.assertContains(r, 'class="pill warn"')

    def test_four_reports_replace_the_six(self):
        """The old settings ("items") give the four their redirect; nothing else of them is used."""
        PortalSettings.objects.update_or_create(
            pk=1,
            defaults={"reports": {"redirect_to": "r@example.com", "items": {"sales": {"redirect": True}}}},
        )
        c = reports.config()
        self.assertEqual([r["kind"] for r in c["list"]], ["sa", "support", "sales", "resources"])
        self.assertTrue(all(r["redirect"] and r["day"] == "0" for r in c["list"]))
        self.assertEqual(reports.by_id("sales")["roles"], ["sales", "kam"])

    def test_schedule(self):
        rep = {"id": "x", "kind": "sales", "name": "X", "roles": ["sales"], "on": True, "day": "daily", "time": "09:00"}
        conf = {"redirect_to": "", "list": [rep], "sent": {}}
        self.assertFalse(reports.due("x", conf, at(2, 8, 55)))
        self.assertTrue(reports.due("x", conf, at(2, 9, 1)))
        self.assertFalse(reports.due("x", conf, at(2, 10, 30)))  # u02: never sent: not a slot passed long before
        conf["sent"]["x"] = at(2, 9, 1).isoformat()
        self.assertFalse(reports.due("x", conf, at(2, 18)))
        self.assertTrue(reports.due("x", conf, at(3, 9, 2)))
        weekly = {**rep, "day": "0"}  # Mondays; 2 Nov 2026 is a Monday
        conf = {"redirect_to": "", "list": [weekly], "sent": {"x": at(2, 9, 5).isoformat()}}
        self.assertFalse(reports.due("x", conf, at(5, 9, 5)))
        self.assertTrue(reports.due("x", conf, at(9, 9, 5)))
        self.assertFalse(reports.due("x", {**conf, "sent": {}}, at(5, 9, 5)))  # set up on a Thursday: Monday next
        weekly["on"] = False
        self.assertFalse(reports.due("x", conf, at(9, 9, 5)))

    def test_sales_report_own_organizations_and_the_redirect(self):
        kam = self.staff("kam@corp.kz", is_kam=True)
        Organization.objects.filter(pk=self.other.pk).update(kam=kam)
        mail.outbox.clear()
        n, detail = reports.send("sales")
        self.assertEqual(n, 3)  # each rep and the KAM: their own organizations
        by = {m.to[0]: m for m in mail.outbox}
        self.assertIn("alphapoc", by["rep@corp.kz"].body)
        self.assertNotIn("betapoc", by["rep@corp.kz"].body)
        self.assertIn("betapoc", by["kam@corp.kz"].body)
        self.assertIn("Your organizations only", by["rep@corp.kz"].body)
        # owner 2026-11-14: the projects with start and review dates; linked; no explanation of "near"
        self.assertIn("ТОО «ALPHA» (", by["rep@corp.kz"].body)
        self.assertIn("| alphapoc (", by["rep@corp.kz"].body)
        html = by["rep@corp.kz"].alternatives[0][0]
        self.assertIn(f"/projects/{self.poc.pk}/", html)
        self.assertIn(f"/organizations/{self.org.pk}/", html)
        self.assertNotIn("within a week for a POC", html)
        self.assertIn("2 Projects +0", by["rep@corp.kz"].body)
        h = ReportSent.objects.get(report="sales")
        self.assertEqual((h.how, h.sent, h.ok), ("schedule", 3, True))
        conf = reports.config()
        conf["redirect_to"] = "reader@example.com"
        for r in conf["list"]:
            r["redirect"] = True
        reports.save(conf)
        mail.outbox.clear()
        reports.send("sales", by=self.sa, how="now")
        self.assertEqual({tuple(m.to) for m in mail.outbox}, {("reader@example.com",)})  # instead of them
        self.assertTrue(any("for rep" in m.subject for m in mail.outbox))
        self.assertEqual(ReportSent.objects.filter(report="sales", how="now", by=self.sa).count(), 1)
        reports.save({"redirect_to": "", "list": reports.config()["list"]})
        mail.outbox.clear()
        n, detail = reports.send("sales")
        self.assertEqual((n, len(mail.outbox)), (0, 0))  # redirected, but nowhere to: nothing sent
        self.assertIn("not sent", detail)
        self.assertFalse(ReportSent.objects.filter(report="sales").order_by("-at").first().ok)

    def test_super_admin_report(self):
        site = Site.objects.create(code="dc1")
        from inventory.models import Platform

        Platform.objects.create(
            key="dc1.cloud", site=site, name="cloud", kind="shared", fe_vip="192.0.2.10", fe={"hostname": "fe"}
        )
        IPSubnet.objects.create(site=site, subnet="192.0.2.0", prefix=24, max_prefix=28, purpose="inet")
        PrefixAssignment.objects.create(cidr="192.0.2.16/28", site="dc1", kind="inet_main", tenant=self.poc)
        AuditLog.objects.create(
            action="portal_backup", details={"ok": True, "result": "ok: /b/x (874 KiB, 4 s)"}, target_type="x"
        )
        AuditLog.objects.create(
            action="platform_backup",
            details={"ok": False, "result": "failed: 1 errors; dc1.cloud (1000 KiB); dc1.mgmt: No route to host"},
        )
        AuditLog.objects.create(
            action="ha_update_active",
            details={
                "state": "done",
                "by": "auto-update",
                "steps": "backup: ok; this node: install v01.00.25 from GitHub",
            },
        )
        AuditLog.objects.create(action="ha_switchover", details={"state": "done", "operation": "switchover"})
        mail.outbox.clear()
        reports.send("sa")
        m = mail.outbox[-1]
        html = m.alternatives[0][0]
        self.assertEqual(m.to, ["sa@corp.kz"])
        for text in (
            "0 Hosts",
            "3 Platform users",
            "0 Org users",
            "192.0.2.0/24 (",
            "| dc1 | Corporate network | 16 / 256 6%",
            "v01.00.25",
        ):
            self.assertIn(text, m.body)
        self.assertIn("No route to host", m.body)  # the failed platform backup in detail
        self.assertIn("switches active / standby: 1", m.body)
        self.assertIn("| v01.00.25 | — | done", m.body)  # owner 2026-11-14: the updates table
        self.assertIn("Platforms | ", m.body)
        self.assertIn("| failed failed: 1 errors", m.body)  # the backup status table
        self.assertIn("?tab=subnets", html)  # the subnets link to their site
        self.assertNotIn("/projects/</a></p>", html)  # no link at the end
        self.assertNotIn("Your organizations only", m.body)
        self.assertIn("874.0\xa0KB", html)
        # owner 2026-11-15: backups are in the super admin's report only, not the resource manager's
        self.assertIsNone(reports.context("resources", timezone.now(), timezone.now()).get("backup_runs"))
        self.assertIsNotNone(reports.context("sa", timezone.now(), timezone.now()).get("backup_runs"))
        # owner 2026-11-15: the platforms' front ends in the super admin's report, none in the others
        self.assertIn("Site | Name | VIP | Leader | Follower | Follower", m.body)
        self.assertIn("dc1 | cloud | fe ● 192.0.2.10 | — | — | —", m.body)  # not collected yet: unsure, no words
        # owner 2026-11-15: "dont loose any tables": an enabled table is there even when it is empty
        self.assertIn("Datastores in OpenNebula\n\nNone.", m.body)
        self.assertIn(">Datastores<", html)
        # owner 2026-11-15: Totals always first, the Platforms table right after them
        self.assertLess(m.body.index("Totals"), m.body.index("Site | Name | VIP"))
        self.assertLess(html.index(">Totals<"), html.index(">Platforms<"))
        self.assertLess(html.index(">Platforms<"), html.index(">IP subnets<"))
        self.assertIn(">Leader<", html)
        # owner 2026-11-15: the platforms in the super admin's and the support engineer's reports, first; the physical
        # view for both, every cluster and datastore for the support engineer; the logical view for sales and the
        # resource manager, whose report has no portal updates and no backups; "near review" in no report
        now = timezone.now()
        ctx = {k: reports.context(k, now, now) for k in ("sa", "support", "sales", "resources")}
        self.assertEqual([k for k in ctx if ctx[k].get("fe_rows") is not None], ["sa", "support"])
        self.assertEqual([k for k in ctx if ctx[k]["physical"]], ["sa", "support"])
        self.assertTrue(ctx["support"]["all_clusters"])
        # owner 2026-11-15: "show all clusters, not only shared in sa/se report", the Totals tile too
        for k in ("sa", "support"):
            keys = [t["key"] for t in ctx[k]["tiles"]]
            self.assertIn("clusters", keys)
            self.assertNotIn("shared_clusters", keys)
        self.assertIsNone(ctx["resources"].get("portal"))
        self.assertIsNone(ctx["resources"].get("backup_runs"))
        self.assertEqual([k for k in ctx if ctx[k].get("near") is not None], [])

    def test_physical_and_logical_cluster_views(self):
        """Owner 2026-11-15: physical (the hosts' cores, RAM, GPUs and what is used) for the super admin and the support
        engineer; logical (what projects can be given and what is allocated) for sales and the resource manager."""
        from types import SimpleNamespace
        from unittest import mock

        row = {
            "site": "dc1",
            "cluster": SimpleNamespace(name="gpu-1", kind="shared"),
            "alloc": {"tenants": 4, "disk_gb": 0},
            "physical": {
                "cores": 512,
                "ram_gb": 8192,
                "gpus": 16,
                "used_cores": 256.0,
                "used_ram": 2800,
                "used_gpus": 8,
            },
            "logical": {
                "vcpu": 256,
                "ram_gb": 6144,
                "gpus": 16,
                "used_vcpu": 256,
                "used_ram": 2800,
                "used_gpus": 8,
                "cpu_pct": 100,
                "ram_pct": 46,
            },
        }
        with mock.patch("inventory.capacity.clusters", return_value=[row]):
            ph, lg = reports.usage(physical=True)[0], reports.usage()[0]
        self.assertEqual(
            (ph["vcpu"], ph["vcpu_cap"], ph["vcpu_pct"], ph["gpu"], ph["gpu_cap"]), (256.0, 512, 50, 8, 16)
        )
        self.assertEqual((lg["vcpu"], lg["vcpu_cap"], lg["vcpu_pct"], lg["top"]), (256, 256, 100, 100))
        # owner 2026-11-15: each resource coloured as on Analysis → Summary: ga1's 8 of 16 GPUs are not red; a full
        # vCPU (not over) is amber; only more than there is would be red
        self.assertEqual((lg["cpu_lvl"], lg["ram_lvl"], lg["gpu_lvl"]), ("warn", "ok", "ok"))
        self.assertEqual((ph["cpu_lvl"], ph["gpu_lvl"]), ("ok", "ok"))
        row["logical"].update(used_vcpu=300, over={"cpu": True, "ram": False, "gpu": False})
        with mock.patch("inventory.capacity.clusters", return_value=[row]):
            self.assertEqual(reports.usage()[0]["cpu_lvl"], "bad")
        # commit-008: physical is red when the allocation passes the usable capacity (less spare hosts and reserve),
        # as on Analysis → Summary, even below the hardware's total
        row["physical"].update(cpu_bar={"over": True}, ram_bar={"over": False}, gpu_bar={"over": True})
        with mock.patch("inventory.capacity.clusters", return_value=[row]):
            ph = reports.usage(physical=True)[0]
        self.assertEqual((ph["vcpu_pct"], ph["cpu_lvl"], ph["ram_lvl"], ph["gpu_lvl"]), (50, "bad", "ok", "bad"))

    def test_support_engineer_gets_their_own(self):
        se = self.staff("se@corp.kz", is_support=True)
        mine = Organization.objects.create(
            name="CORP A", tax_id=valid_bin("0105400014"), kind=OrgKind.CORP, support_engineer=se
        )
        t = Tenant.objects.create(organization=mine, name="corpa", title="A", approval="approved", stage="client")
        QuotaRecord.objects.create(tenant=t, site="dc1", cluster="shared1", version=1, cpu=8, ram_gb=16, disk_gb=100)
        QuotaRecord.objects.create(
            tenant=self.poc, site="dc1", cluster="shared1", version=1, cpu=4, ram_gb=8, disk_gb=50
        )
        mail.outbox.clear()
        reports.send("support")
        body = mail.outbox[-1].body
        self.assertIn("| corpa (", body)  # owner 2026-11-14: the projects table, only theirs (text made from the HTML)
        self.assertIn("| 8 | 16 |", body)
        self.assertNotIn("alphapoc", body)
        self.assertNotIn("Projects near their review date", body)  # owner 2026-11-15: in no report

    @override_settings(DEPLOY_ASYNC=False)
    def test_settings_tab(self):
        """Owner 2026-11-17: Settings → Reports is a standard list (name, template, day, roles, on, redirect, last
        sent), not edited in the rows; the selected report's details have Edit (a dialog: name, template, day and
        time, roles), On/Off, Redirect on/off and Send now; the redirect address above the list."""
        self.client.force_login(self.sa)
        url = reverse("portal_settings")
        r = self.client.get(url + "?tab=reports")
        page = r.content.decode()
        self.assertIn('data-key="reports"', page)
        for th in ("Name", "Template", "Day", "Roles", "On", "Redirect", "Last sent"):
            self.assertIn(f"<th>{th}</th>", page)
        self.assertNotIn('name="name_sales"', page)  # nothing edited in the rows
        self.assertIn('data-open="dlg-rep-sales"', page)
        self.assertContains(r, "Sent history")
        self.client.post(url, {"tab": "reports", "section": "report_redirect_to", "redirect_to": "reader@example.com"})
        self.assertEqual(reports.config()["redirect_to"], "reader@example.com")
        edit = {
            "tab": "reports",
            "section": "report",
            "report": "sales",
            "name": "Managers weekly",
            "template": str(T.REPORT),
            "day": "4",
            "time": "10:30",
            "roles": ["sales"],
        }
        r = self.client.post(url, edit)
        self.assertRedirects(r, url + "?tab=reports&report=sales", fetch_redirect_response=False)
        self.client.post(url, {"tab": "reports", "section": "report_redirect", "report": "sales"})
        sales = reports.by_id("sales")
        self.assertEqual(
            {k: sales[k] for k in ("name", "roles", "day", "time", "on", "redirect", "template")},
            {
                "name": "Managers weekly",
                "roles": ["sales"],
                "day": "4",
                "time": "10:30",
                "on": True,
                "redirect": True,
                "template": T.REPORT,
            },
        )
        self.client.post(url, {"tab": "reports", "section": "report_on", "report": "sales"})
        self.assertFalse(reports.by_id("sales")["on"])
        mail.outbox.clear()
        r = self.client.post(url, {"tab": "reports", "section": "report_send", "report": "sales"})
        self.assertRedirects(r, url + "?tab=reports&report=sales", fetch_redirect_response=False)
        self.assertEqual(mail.outbox[-1].to, ["reader@example.com"])  # sent now even while off, redirected
        self.assertIn("sales", reports.config()["sent"])
        r = self.client.get(url + "?tab=reports&report=sales")
        self.assertContains(r, "reader@example.com")
        self.client.post(url, {**edit, "time": "25:00"})
        self.assertEqual(reports.by_id("sales")["time"], "10:30")  # refused
        self.client.post(url, {**edit, "roles": []})
        self.assertEqual(reports.by_id("sales")["roles"], ["sales"])  # refused: at least one role
        self.client.post(url, {**edit, "template": "nothing"})
        self.assertEqual(reports.by_id("sales")["template"], T.REPORT)  # only the report templates

    def test_a_report_is_sent_with_its_template(self):
        """Owner 2026-11-17: each report names the mail template it is sent with."""
        conf = reports.config()
        # a name stored before the ids (2026-11-17 18:52) is read as its id
        lst = [{**r, "template": "registration_report"} if r["id"] == "sales" else r for r in conf["list"]]
        reports.save({"redirect_to": "reader@example.com", "list": [{**r, "redirect": True} for r in lst]})
        with mock.patch("accounts.utils.notify", return_value=1) as n:
            reports.send("sales")
        self.assertEqual(n.call_args[0][1], T.REGISTRATION_REPORT)
        self.assertIn((T.REGISTRATION_REPORT, "registration_report"), reports.templates())


class ProjectWording(Base):
    """Owner 2026-11-14: people read "project", never "tenant" - the log's actions, the e-mail template names, the
    empty lists; the stored keys stay."""

    def test_log_and_mail_templates_say_project(self):
        sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(sa)
        AuditLog.objects.create(action="tenant_deployed", target_type="tenant", target_id="999")
        r = self.client.get(reverse("audit_log"))
        self.assertContains(r, "project_deployed")
        self.assertNotContains(r, "tenant_deployed")
        r = self.client.get(reverse("audit_log") + "?q=project_deployed")  # the new word finds the stored one
        self.assertContains(r, "project_deployed")
        r = self.client.get(reverse("portal_settings") + "?tab=mail")
        self.assertContains(r, "project_ready_admin")
        self.assertNotContains(r, ">tenant_ready_admin<")
