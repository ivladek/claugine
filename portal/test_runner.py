"""The test runner: Django's, with one test-only convenience.

Two-factor authentication is mandatory for every user (correction 11). Tests that sign a user in with
``Client.force_login`` stand for a user who has already enrolled: the user gets TOTP enabled first. Nothing of this
runs outside the tests.
"""
from django.test import Client
from django.test.runner import DiscoverRunner

_force_login = Client.force_login


def _enrolled_force_login(self, user, backend=None):
    if not getattr(user, "totp_enabled", True):
        import pyotp
        user.totp_secret = user.totp_secret or pyotp.random_base32()
        user.totp_enabled = True
        user.save(update_fields=["totp_secret", "totp_enabled"])
    return _force_login(self, user, backend)


class Runner(DiscoverRunner):
    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        Client.force_login = _enrolled_force_login

    def teardown_test_environment(self, **kwargs):
        Client.force_login = _force_login
        super().teardown_test_environment(**kwargs)
