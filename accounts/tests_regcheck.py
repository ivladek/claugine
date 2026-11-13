"""Owner 2026-10-29 20:30: registration requests — checks, deletion of what was never approved, the daily report."""

from datetime import timedelta
from unittest import mock

from django.core import mail
from django.urls import reverse
from django.utils import timezone

from accounts import regcheck, timers
from accounts.models import AuditLog, Kind, User
from accounts.tests import PROFILE, Base, link_path, photo, valid_bin
from tenants.models import JoinRequest, Organization, Registration


class RegistrationChecks(Base):
    def post(self, email, tax_id, **extra):
        data = {
            **PROFILE,
            "email": email,
            "photo": photo(),
            "organization_name": "ТОО «Romashka»",
            "tax_id_type": "bin",
            "tax_id": tax_id,
            regcheck.TS_FIELD: extra.pop("ts", None) or self.stamp(60),
        }
        data.update(extra)
        return self.client.post(reverse("register"), data)

    def stamp(self, seconds_ago):
        from django.core import signing

        return signing.dumps(int(timezone.now().timestamp()) - seconds_ago, salt=regcheck.TS_SALT)

    def test_analysis_of_entered_data(self):
        ok = regcheck.analyze(
            {
                "email": "a.sarsenova@romashka.kz",
                "full_name": "Aigerim Sarsenova",
                "organization_name": "ТОО «Ромашка»",
                "phone": "+7 701 5830412",
                "tax_id": valid_bin(),
            },
            "198.51.100.7",
            40,
        )
        self.assertEqual((ok["score"], ok["flags"]), (0, []))
        bad = regcheck.analyze(
            {
                "email": "qx88123456@mailinator.com",
                "full_name": "<script>alert(1)</script>",
                "organization_name": "xkcdqwrtz",
                "phone": "+7 777 7777777",
                regcheck.HONEYPOT: "",
            },
            "198.51.100.7",
            2,
        )
        self.assertTrue(
            {"too_fast", "markup", "disposable_email", "odd_email", "odd_name", "odd_org_name", "odd_phone"}
            <= set(bad["flags"]),
            bad,
        )
        self.assertTrue(regcheck.is_robot(bad))
        self.assertIn("markup", regcheck.analyze({"organization_name": "x' OR '1'='1"}, "", 30)["flags"])

    def test_robot_gets_the_usual_page_and_nothing_is_made(self):
        r = self.post("bot@romashka.kz", valid_bin(), **{regcheck.HONEYPOT: "http://spam"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(User.objects.filter(email="bot@romashka.kz").exists())
        self.assertEqual(len(mail.outbox), 0)
        log = AuditLog.objects.get(action="registration_blocked")
        self.assertIn("honeypot", log.details["flags"])
        self.assertNotIn("password1", log.details)
        r = self.post("fast@romashka.kz", valid_bin(), ts=self.stamp(1))
        self.assertFalse(User.objects.filter(email="fast@romashka.kz").exists())

    def test_suspicious_shown_on_the_waiting_organization_and_unconfirmed_deleted(self):
        tax = valid_bin()
        self.post("qx88123456@mailinator.com", tax, organization_name="xkcdqwrtz")
        reg = Registration.objects.get(user__email="qx88123456@mailinator.com")
        self.assertGreaterEqual(reg.reg_check["score"], regcheck.SUSPICIOUS)
        # not confirmed in time: deleted, the log keeps it
        Registration.objects.filter(pk=reg.pk).update(created_at=timezone.now() - timedelta(hours=49))
        self.assertEqual(regcheck.purge()["unconfirmed"], 1)
        self.assertFalse(User.objects.filter(email="qx88123456@mailinator.com").exists())
        log = AuditLog.objects.get(action="registration_purged")
        self.assertEqual((log.details["why"], log.details["tax_id"]), ("not confirmed", tax))
        # a confirmed one: the check goes with the organization to the staff
        self.post("boss@romashka.kz", tax, organization_name="xkcdqwrtz")
        u = User.objects.get(email="boss@romashka.kz")
        self.client.get(link_path(mail.outbox[-1]))
        org = Organization.objects.get(tax_id=tax)
        self.assertTrue(org.is_pending)
        self.assertIn("odd_org_name", org.reg_check["flags"])
        u.refresh_from_db()
        self.assertTrue(u.email_verified)
        sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(sa)
        r = self.client.get(reverse("org_detail", args=[org.pk]))
        from django.utils.html import escape

        self.assertContains(r, escape(str(regcheck.FLAGS["odd_org_name"][1])))

    def test_declined_join_request_is_deleted(self):
        tax = valid_bin()
        org = Organization.objects.create(name="Acme", tax_id=tax)
        admin = User.objects.create_user(
            "adm@acme.kz",
            "Str0ng-PASS-123",
            kind=Kind.CUSTOMER,
            organization=org,
            is_org_admin=True,
            email_verified=True,
        )
        self.post("someone@acme.kz", tax)
        self.client.get(link_path(mail.outbox[0]))
        jr = JoinRequest.objects.get(user__email="someone@acme.kz")
        self.client.force_login(admin)
        self.client.post(reverse("decide_join", args=[jr.pk, "reject"]))
        self.assertFalse(User.objects.filter(email="someone@acme.kz").exists())
        self.assertTrue(AuditLog.objects.filter(action="registration_purged", details__why="join declined").exists())

    def test_registrations_cleanup_only_no_report(self):
        # u10 (owner 2026-11-02): the registrations report was dropped; the daily timer keeps only the clean-up
        self.staff("sa@corp.kz", is_super_admin=True)
        self.staff("rm@corp.kz", is_resource_manager=True)
        self.post("bot@romashka.kz", valid_bin(), **{regcheck.HONEYPOT: "x"})
        self.post("a.sarsenova@romashka.kz", valid_bin())
        mail.outbox.clear()
        ok, detail = regcheck.daily()  # only the clean-up
        self.assertTrue(ok, detail)
        self.assertEqual(len(mail.outbox), 0)
        from accounts import reports

        self.assertNotIn(
            "registrations", [r["id"] for r in reports.config()["list"]]
        )  # no registrations report any more

    def test_timer_once_a_day_from_nine(self):
        tz = timezone.get_current_timezone()
        early = timezone.make_aware(timezone.datetime(2026, 10, 30, 8, 55), tz)
        later = timezone.make_aware(timezone.datetime(2026, 10, 30, 9, 5), tz)
        self.assertFalse(timers.due("registrations", early))
        self.assertTrue(timers.due("registrations", later))
        timers._save("registrations", last_at=later.isoformat(), state="ok")
        self.assertFalse(timers.due("registrations", later + timedelta(hours=10)))
        self.assertTrue(timers.due("registrations", later + timedelta(hours=24)))
        with mock.patch("accounts.regcheck.daily", return_value=(True, "x")) as d:
            timers.run("registrations")
        d.assert_called_once()
