"""Settings → Reports. u01 (owner 2026-10-31): the schedule, recipients and the redirect; review dates highlighted (a
week for a POC, 3 months for a client). Owner 2026-11-13 (job 2026-11-09_resource-dashboard): four reports, one per
role, replace the six; name and roles editable, one Day list, a sent history; a support engineer, a manager and a
KAM get their own organizations only."""

from datetime import timedelta

from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from accounts import reports
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
        self.assertIn("ТОО «ALPHA» · alphapoc · ", by["rep@corp.kz"].body)
        html = by["rep@corp.kz"].alternatives[0][0]
        self.assertIn(f"/projects/{self.poc.pk}/", html)
        self.assertIn(f"/organizations/{self.org.pk}/", html)
        self.assertNotIn("within a week for a POC", html)
        self.assertIn("Projects: 2 (+0", by["rep@corp.kz"].body)
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
        for text in ("Hosts:", "Platform users:", "Org users:", "192.0.2.0/24 (dc1, ", ": 16/256 (6%)", "v01.00.25"):
            self.assertIn(text, m.body)
        self.assertIn("No route to host", m.body)  # the failed platform backup in detail
        self.assertIn("switches active / standby: 1", m.body)
        self.assertIn("v01.00.25: standby —, active done", m.body)  # owner 2026-11-14: the updates table
        self.assertIn("Platforms: failed", m.body)  # the backup status table
        self.assertIn("?tab=subnets", html)  # the subnets link to their site
        self.assertNotIn("/projects/</a></p>", html)  # no link at the end
        self.assertNotIn("Your organizations only", m.body)
        self.assertIn("874.0\xa0KB", html)

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
        self.assertIn("CORP A · corpa: vCPU 8", body)  # owner 2026-11-14: the projects table, only theirs
        self.assertNotIn("alphapoc", body)
        self.assertIn("Projects near their review date", body)

    @override_settings(DEPLOY_ASYNC=False)
    def test_settings_tab(self):
        self.client.force_login(self.sa)
        url = reverse("portal_settings")
        r = self.client.get(url + "?tab=reports")
        for rep in reports.config()["list"]:
            self.assertContains(r, f'name="name_{rep["id"]}"')
        self.assertContains(r, "Sent history")
        post = {"tab": "reports", "section": "reports", "redirect_to": "reader@example.com"}
        for rep in reports.config()["list"]:
            post.update(
                {
                    f"name_{rep['id']}": rep["name"],
                    f"roles_{rep['id']}": rep["roles"],
                    f"on_{rep['id']}": "on",
                    f"redirect_{rep['id']}": "on",
                    f"day_{rep['id']}": "4",
                    f"time_{rep['id']}": "10:30",
                }
            )
        post["name_sales"] = "Managers weekly"
        post["roles_sales"] = ["sales"]
        self.client.post(url, post)
        c = reports.config()
        self.assertEqual(c["redirect_to"], "reader@example.com")
        sales = reports.by_id("sales")
        self.assertEqual(
            {k: sales[k] for k in ("name", "roles", "day", "time", "on", "redirect")},
            {"name": "Managers weekly", "roles": ["sales"], "day": "4", "time": "10:30", "on": True, "redirect": True},
        )
        mail.outbox.clear()
        r = self.client.post(url, {**post, "send_now": "sales"})
        self.assertRedirects(r, url + "?tab=reports&report=sales", fetch_redirect_response=False)
        self.assertEqual(mail.outbox[-1].to, ["reader@example.com"])
        self.assertIn("sales", reports.config()["sent"])
        r = self.client.get(url + "?tab=reports&report=sales")
        self.assertContains(r, "reader@example.com")
        self.client.post(url, {**post, "time_sales": "25:00"})
        self.assertEqual(reports.by_id("sales")["time"], "10:30")  # refused
        self.client.post(url, {**post, "roles_sales": []})
        self.assertEqual(reports.by_id("sales")["roles"], ["sales"])  # refused: at least one role
