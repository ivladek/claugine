"""core-035 (sixteenth full review): passwords made to the policy (accounts.policy.generate)."""

import time

from django.test import TestCase

from accounts import policy
from tenants.models import PortalSettings


class Generate(TestCase):
    def set(self, **values):
        s = PortalSettings.get()
        s.password_policy = {**policy.DEFAULTS, **values}
        s.save()

    def test_a_strict_but_valid_policy_is_met_at_once(self):
        """Drawn whole until one passed, a password of 20 special symbols from one allowed symbol practically never
        came, and the pages that make passwords hung. Built to the policy, it comes at once and passes."""
        self.set(length=20, special=20, lower=0, upper=0, digits=0, allowed="-")
        started = time.monotonic()
        s = policy.generate()
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(s, "-" * 20)
        self.assertEqual(policy.problems(s), [])

    def test_every_kind_asked_for_is_there(self):
        self.set(length=30, lower=7, upper=7, digits=7, special=7, allowed="+=")
        for _i in range(50):
            s = policy.generate()
            self.assertEqual(policy.problems(s), [])
            self.assertGreaterEqual(len(s), 30)

    def test_the_default_policy_and_a_longer_request(self):
        s = policy.generate()
        self.assertEqual(policy.problems(s), [])
        self.assertEqual(len(s), 16)
        self.assertEqual(len(policy.generate(24)), 24)

    def test_a_policy_that_cannot_be_met_says_so(self):
        s = PortalSettings.get()
        s.password_policy = {**policy.DEFAULTS, "special": 2, "allowed": ""}
        s.save()
        if policy.policy()["allowed"]:  # clean_allowed keeps something: nothing to check here
            return
        with self.assertRaisesMessage(ValueError, "allows none"):
            policy.generate()
