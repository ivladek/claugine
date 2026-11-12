"""owner u12: a super admin may act as another non-archived user (the whole interface renders as them)."""

from django.test import TestCase
from django.urls import reverse

from accounts.models import Kind, User

TOTP = dict(totp_secret="JBSWY3DPEHPK3PXP", totp_enabled=True, email_verified=True)


class ActAs(TestCase):
    def setUp(self):
        self.sa = User.objects.create_user(
            "sa@corp.kz",
            "Str0ng-PASS-123",
            kind=Kind.STAFF,
            is_super_admin=True,
            is_staff=True,
            is_superuser=True,
            **TOTP,
        )
        self.eng = User.objects.create_user("eng@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_support=True, **TOTP)

    def test_super_admin_acts_as_and_stops(self):
        self.client.force_login(self.sa)
        self.client.post(reverse("act_as"), {"user": self.eng.pk})
        self.assertEqual(self.client.session.get("act_as"), self.eng.pk)
        r = self.client.get(reverse("dashboard"))  # interface now renders as the other user
        self.assertContains(r, "Acting as")
        self.client.post(reverse("act_as"), {"user": "0"})  # stop
        self.assertIsNone(self.client.session.get("act_as"))

    def test_non_super_admin_cannot_impersonate(self):
        self.client.force_login(self.eng)
        r = self.client.post(reverse("act_as"), {"user": self.sa.pk})
        self.assertEqual(r.status_code, 403)
        self.assertIsNone(self.client.session.get("act_as"))

    def test_session_act_as_ignored_for_non_super_admin(self):
        self.client.force_login(self.eng)
        s = self.client.session
        s["act_as"] = self.sa.pk
        s.save()
        r = self.client.get(reverse("dashboard"))
        self.assertNotContains(r, "Acting as")

    def test_impersonation_is_read_only(self):
        # owner u12: while acting as a user, state-changing requests are no-ops (the view never runs)
        from tenants.models import Organization

        self.client.force_login(self.sa)
        self.client.post(reverse("act_as"), {"user": self.eng.pk})
        before = Organization.objects.count()
        r = self.client.post(
            reverse("org_new"), {"name": "Nope", "kind": "cloud", "tax_id_type": "bin", "tax_id": "123456789012"}
        )
        self.assertEqual(Organization.objects.count(), before)  # nothing created
        self.assertEqual(r.status_code, 302)  # bounced, not executed
        # ending impersonation (a POST to act-as) still works
        self.client.post(reverse("act_as"), {"user": "0"})
        self.assertIsNone(self.client.session.get("act_as"))

    def test_archived_target_not_impersonated(self):
        from django.utils import timezone

        self.eng.archived_at = timezone.now()
        self.eng.save(update_fields=["archived_at"])
        self.client.force_login(self.sa)
        self.client.post(reverse("act_as"), {"user": self.eng.pk})
        self.assertIsNone(self.client.session.get("act_as"))
