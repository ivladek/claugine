"""owner u12: granting/removing the super admin role needs approvals by count — 1→1, 2→2, 3→2, 4+→3 (asker counts)."""

from django.test import TestCase, override_settings

from accounts import people, superadmins
from accounts.models import Kind, User

TOTP = dict(totp_enabled=True, email_verified=True)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class SAChange(TestCase):
    def mk(self, email, **kw):
        return User.objects.create_user(
            email, "Str0ng-PASS-123", kind=Kind.STAFF, login_enabled=True, is_active=True, **TOTP, **kw
        )

    def test_thresholds(self):
        self.assertEqual([superadmins.required_for(n) for n in (1, 2, 3, 4, 5)], [1, 2, 2, 3, 3])

    def test_add_one_sa_is_immediate(self):
        sa = self.mk("sa1@corp.kz", is_super_admin=True)
        new = self.mk("new@corp.kz")
        people.set_staff_roles(sa, new, ["is_super_admin"])
        new.refresh_from_db()
        self.assertTrue(new.is_super_admin)  # required 1 → applied at once

    def test_add_two_sa_needs_one_more(self):
        sa1 = self.mk("sa1@corp.kz", is_super_admin=True)
        sa2 = self.mk("sa2@corp.kz", is_super_admin=True)
        new = self.mk("new@corp.kz")
        people.set_staff_roles(sa1, new, ["is_super_admin"])
        new.refresh_from_db()
        self.assertFalse(new.is_super_admin)  # pending (required 2)
        req = superadmins.pending_for(new)
        self.assertEqual((req.op, req.required), ("add", 2))
        superadmins.confirm(sa2, req)
        new.refresh_from_db()
        self.assertTrue(new.is_super_admin)

    def test_remove_three_sa_needs_one_confirm(self):
        sa1 = self.mk("sa1@corp.kz", is_super_admin=True)
        sa2 = self.mk("sa2@corp.kz", is_super_admin=True)
        sa3 = self.mk("sa3@corp.kz", is_super_admin=True)
        people.set_staff_roles(sa1, sa3, [])  # ask to remove sa3
        sa3.refresh_from_db()
        self.assertTrue(sa3.is_super_admin)  # still SA until confirmed
        req = superadmins.pending_for(sa3)
        self.assertEqual((req.op, req.required), ("remove", 2))
        superadmins.confirm(sa2, req)
        sa3.refresh_from_db()
        self.assertFalse(sa3.is_super_admin)

    def test_remove_two_sa_cannot_complete(self):
        sa1 = self.mk("sa1@corp.kz", is_super_admin=True)
        sa2 = self.mk("sa2@corp.kz", is_super_admin=True)
        people.set_staff_roles(sa1, sa2, [])  # remove sa2: required 2, but only sa1 (the asker) can agree
        sa2.refresh_from_db()
        self.assertTrue(sa2.is_super_admin)  # stays — the one removed cannot confirm their own removal
        self.assertIsNotNone(superadmins.pending_for(sa2))

    def test_decline_ends_it(self):
        sa1 = self.mk("sa1@corp.kz", is_super_admin=True)
        sa2 = self.mk("sa2@corp.kz", is_super_admin=True)
        new = self.mk("new@corp.kz")
        people.set_staff_roles(sa1, new, ["is_super_admin"])
        req = superadmins.pending_for(new)
        superadmins.decline(sa2, req)
        new.refresh_from_db()
        self.assertFalse(new.is_super_admin)
        self.assertIsNone(superadmins.pending_for(new))

    def test_declining_a_grant_reads_request_not_removal(self):
        # owner 2026-11-05 (doc-gaps u11 §5): decline/lapse of a GRANT must not say "removal"
        from django.core import mail

        sa1 = self.mk("sa1@corp.kz", is_super_admin=True)
        self.mk("sa2@corp.kz", is_super_admin=True)
        sa3 = self.mk("sa3@corp.kz", is_super_admin=True)
        new = self.mk("new@corp.kz")
        req, err = superadmins.request_add(sa1, new)
        self.assertEqual((err, req.op, req.required), ("", "add", 2))  # 3 SAs → required 2, stays pending
        mail.outbox = []
        superadmins.decline(sa3, req)
        subjects = " | ".join(m.subject for m in mail.outbox)
        self.assertIn("request declined", subjects)
        self.assertNotIn("removal declined", subjects)
