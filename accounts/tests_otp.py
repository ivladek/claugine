"""Two-factor on django-otp (owner 2026-11-11): keys moved from User.totp_secret keep working, a code is accepted
once, and the version under test on the read-only standby verifies without writing."""

import base64
import hashlib
import hmac
import importlib
import struct
import time
from unittest import mock

from django.apps import apps
from django.test import TestCase

from accounts.models import Kind, User


def rfc6238(secret_b32, at):
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8))
    mac = hmac.new(key, struct.pack(">Q", int(at // 30)), hashlib.sha1).digest()
    o = mac[-1] & 0x0F
    return f"{(struct.unpack('>I', mac[o : o + 4])[0] & 0x7FFFFFFF) % 1000000:06d}"


class Otp(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("u@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, email_verified=True)

    def test_a_moved_key_gives_the_codes_the_phone_already_shows(self):
        from django_otp.plugins.otp_totp.models import TOTPDevice

        mig = importlib.import_module("accounts.migrations.0018_totp_to_django_otp")

        class FakeUser:
            pk, totp_secret, totp_enabled, totp_last_counter = self.u.pk, "JBSWY3DPEHPK3PXP", True, 0

        class FakeApps:
            def get_model(self, app, name):
                if name == "User":
                    m = mock.Mock()
                    m.objects.exclude.return_value = [FakeUser()]
                    return m
                return apps.get_model(app, name)

        mig.forward(FakeApps(), None)
        d = TOTPDevice.objects.get(user=self.u)
        from accounts.utils import totp_code

        self.assertEqual(totp_code(d), rfc6238("JBSWY3DPEHPK3PXP", time.time()))
        self.assertTrue(d.confirmed)

    def test_a_code_is_accepted_once(self):
        from accounts.testkit import enrol
        from accounts.utils import totp_code, verify_totp

        d = enrol(self.u)
        code = totp_code(d)
        self.assertTrue(verify_totp(self.u, code))
        self.assertFalse(verify_totp(self.u, code))

    def test_the_read_only_standby_verifies_without_writing(self):
        from accounts.testkit import enrol
        from accounts.utils import totp_code, verify_totp

        d = enrol(self.u)
        code = totp_code(d)
        with (
            mock.patch("accounts.testmount.read_only", return_value=True),
            mock.patch("django_otp.plugins.otp_totp.models.TOTPDevice.save", side_effect=AssertionError("written")),
        ):
            self.assertTrue(verify_totp(self.u, code))
            self.assertFalse(verify_totp(self.u, code))  # kept in memory there
