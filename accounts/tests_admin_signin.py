"""core-001: /django-admin/ signs nobody in by itself; the portal's sign-in, with its second factor, is the way in."""

from django.test import TestCase
from django.urls import reverse

from accounts.testkit import staff


class AdminSignIn(TestCase):
    def setUp(self):
        self.sa = staff(is_super_admin=True, is_staff=True, is_superuser=True)

    def test_the_password_alone_opens_nothing(self):
        r = self.client.post(
            "/django-admin/login/", {"username": self.sa.email, "password": "Str0ng-PASS-123", "next": "/django-admin/"}
        )
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith(reverse("login")))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)  # still signed out

    def test_the_admin_sends_to_the_portal_sign_in(self):
        r = self.client.get("/django-admin/")
        self.assertEqual(r.status_code, 302)
        r = self.client.get(r["Location"])
        self.assertTrue(r["Location"].startswith(reverse("login")))

    def test_a_portal_session_opens_the_admin(self):
        self.client.force_login(self.sa)
        self.assertEqual(self.client.get("/django-admin/").status_code, 200)
