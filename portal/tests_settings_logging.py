"""Logging: requests with a Host the portal does not serve are answered 400 without a traceback (2026-11-12)."""

from django.test import TestCase


class Scanners(TestCase):
    def test_an_unknown_host_is_not_logged(self):
        with self.assertNoLogs("django.security", level="ERROR"):
            r = self.client.get("/login/", HTTP_HOST="203.0.113.5")
        self.assertEqual(r.status_code, 400)
