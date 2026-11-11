"""u01 (owner 2026-10-31): Settings → Reports — the e-mail reports, their schedule, recipients and the redirect;
review dates highlighted (a week for a POC, 3 months for a client)."""
from datetime import timedelta

from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from accounts import reports
from accounts.tests import Base, valid_bin
from tenants.models import Organization, PortalSettings, Tenant


def at(day, hh, mm=0):
    return timezone.make_aware(timezone.datetime(2026, 11, day, hh, mm), timezone.get_current_timezone())


class Reports(Base):
    def setUp(self):
        self.sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.rep = self.staff("rep@corp.kz", is_sales=True)
        self.other_rep = self.staff("rep2@corp.kz", is_sales=True)
        self.org = Organization.objects.create(name="ТОО «ALPHA»", tax_id=valid_bin(), sales_rep=self.rep)
        today = timezone.localdate()
        self.poc = Tenant.objects.create(organization=self.org, name="alphapoc", title="P", approval="approved", stage="poc",
                                         date_review=today + timedelta(days=5))
        self.client_t = Tenant.objects.create(organization=self.org, name="alphaprod", title="C", approval="approved",
                                              stage="client", date_review=today + timedelta(days=200))

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
        self.assertNotContains(r, "Refresh from the platforms")  # u01: no Refresh button

    def test_schedule(self):
        item = {"on": True, "period": "daily", "time": "09:00", "weekday": 0, "redirect": False}
        conf = {"redirect_to": "", "items": {"sales": item}, "sent": {}}
        self.assertFalse(reports.due("sales", conf, at(2, 8, 55)))
        self.assertTrue(reports.due("sales", conf, at(2, 9, 1)))
        self.assertFalse(reports.due("sales", conf, at(2, 10, 30)))  # u02: never sent: not a slot passed long before
        conf["sent"]["sales"] = at(2, 9, 1).isoformat()
        self.assertFalse(reports.due("sales", conf, at(2, 18)))
        self.assertTrue(reports.due("sales", conf, at(3, 9, 2)))
        weekly = {**item, "period": "weekly", "weekday": 0}  # Mondays; 2 Nov 2026 is a Monday
        conf = {"redirect_to": "", "items": {"sales": weekly}, "sent": {"sales": at(2, 9, 5).isoformat()}}
        self.assertFalse(reports.due("sales", conf, at(5, 9, 5)))
        self.assertTrue(reports.due("sales", conf, at(9, 9, 5)))
        self.assertFalse(reports.due("sales", {**conf, "sent": {}}, at(5, 9, 5)))  # set up on a Thursday: Monday next
        conf["items"]["sales"]["on"] = False
        self.assertFalse(reports.due("sales", conf, at(9, 9, 5)))

    def test_sales_report_per_person_and_the_redirect(self):
        mail.outbox.clear()
        n, detail = reports.send("sales")
        self.assertEqual(n, 1)  # only the rep with organizations
        m = mail.outbox[-1]
        self.assertEqual(m.to, ["rep@corp.kz"])
        self.assertIn("ТОО «ALPHA»", m.body)
        self.assertIn("alphapoc", m.body)
        self.assertIn("the review date is close", m.body)
        PortalSettings.get()
        reports.save({"redirect_to": "reader@example.com",
                      "items": {**reports.config()["items"], "sales": {**reports.config()["items"]["sales"], "redirect": True}}})
        mail.outbox.clear()
        reports.send("sales")
        m = mail.outbox[-1]
        self.assertEqual(m.to, ["reader@example.com"])  # instead of the rep
        self.assertIn("for rep", m.subject)
        reports.save({"redirect_to": "", "items": reports.config()["items"]})
        mail.outbox.clear()
        n, detail = reports.send("sales")
        self.assertEqual((n, len(mail.outbox)), (0, 0))  # redirected, but nowhere to: nothing sent
        self.assertIn("not sent", detail)

    def test_statistics_and_resources(self):
        self.staff("rm@corp.kz", is_resource_manager=True)
        mail.outbox.clear()
        reports.send("statistics")
        self.assertEqual(mail.outbox[-1].to, ["sa@corp.kz"])
        self.assertIn("Review dates come or close", mail.outbox[-1].body)
        reports.send("resources")
        self.assertEqual(mail.outbox[-1].to, ["rm@corp.kz"])
        self.assertIn("Projects by cluster", mail.outbox[-1].body)  # u05: matrix + org counts, nothing after usage
        self.assertIn("Resource manager ·", mail.outbox[-1].subject)  # u05: role name in the subject

    @override_settings(DEPLOY_ASYNC=False)
    def test_settings_tab(self):
        self.client.force_login(self.sa)
        url = reverse("portal_settings")
        r = self.client.get(url + "?tab=reports")
        for key in reports.KEYS:
            self.assertContains(r, f'name="on_{key}"')
        post = {"tab": "reports", "section": "reports", "redirect_to": "reader@example.com"}
        for key in reports.KEYS:
            post.update({f"on_{key}": "on", f"redirect_{key}": "on", f"period_{key}": "weekly", f"weekday_{key}": "4",
                         f"time_{key}": "10:30"})
        self.client.post(url, post)
        c = reports.config()
        self.assertEqual(c["redirect_to"], "reader@example.com")
        self.assertEqual(c["items"]["kam"], {"on": True, "period": "weekly", "weekday": 4, "time": "10:30", "redirect": True})
        mail.outbox.clear()
        self.client.post(url, {**post, "send_now": "sales"})
        self.assertEqual(mail.outbox[-1].to, ["reader@example.com"])
        self.assertIn("sales", reports.config()["sent"])
        self.client.post(url, {**post, "time_kam": "25:00"})
        self.assertEqual(reports.config()["items"]["kam"]["time"], "10:30")  # refused
