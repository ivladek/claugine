"""City typed in, phone in international format with the + first (owner 2026-11-12)."""

import importlib

from django.test import TestCase
from django.urls import reverse

from accounts.models import Kind, User
from accounts.testkit import enrol


class CityAndPhone(TestCase):
    def test_profile_takes_any_city_and_an_international_phone(self):
        u = User.objects.create_user("p@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, email_verified=True)
        enrol(u)
        self.client.force_login(u)
        page = self.client.get(reverse("profile")).content.decode()
        self.assertIn('data-intl-phone="1"', page)
        self.assertIn('type="tel"', page)
        self.assertNotIn('<select name="city"', page)
        r = self.client.post(reverse("profile"), {"full_name": "P", "phone": "+971 50 123 4567", "city": "Dubai"})
        self.assertEqual(r.status_code, 302)
        u.refresh_from_db()
        self.assertEqual((u.city, u.phone, u.get_city_display()), ("Dubai", "+971501234567", "Dubai"))
        r = self.client.post(reverse("profile"), {"full_name": "P", "phone": "123", "city": "Dubai"})
        self.assertContains(r, "international format")

    def test_the_old_city_keys_become_names(self):
        mig = importlib.import_module("accounts.migrations.0020_city_as_text")
        a = User.objects.create_user("a@corp.kz", None, kind=Kind.STAFF)
        b = User.objects.create_user("b@corp.kz", None, kind=Kind.STAFF)
        User.objects.filter(pk=a.pk).update(city="oskemen")
        User.objects.filter(pk=b.pk).update(city="other")
        from django.apps import apps

        mig.forward(apps, None)
        self.assertEqual(
            list(User.objects.filter(pk__in=[a.pk, b.pk]).order_by("pk").values_list("city", flat=True)),
            ["Oskemen (Ust-Kamenogorsk)", ""],
        )
