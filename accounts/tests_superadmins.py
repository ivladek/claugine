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


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class NoBypass(TestCase):
    """core-016 (fourth full review): neither an invitation nor the technical admin gives or takes the super admin
    role without the super admins' agreement."""

    def setUp(self):
        from accounts.testkit import otp, staff

        self.sa1 = staff(
            "sa1@corp.kz", is_super_admin=True, is_staff=True, is_superuser=True, totp_enabled=True, login_enabled=True
        )
        self.sa2 = staff("sa2@corp.kz", is_super_admin=True, totp_enabled=True, login_enabled=True)
        self.client.force_login(self.sa1)
        otp(self.client)
        self.new = User.objects.create_user("new@corp.kz", None, kind=Kind.STAFF, login_enabled=False)

    def test_an_invitation_asks_for_the_super_admin_role(self):
        from django.urls import reverse

        self.client.post(reverse("invite_person", args=[self.new.pk]), {"roles": ["is_super_admin", "is_support"]})
        self.new.refresh_from_db()
        self.assertTrue(self.new.is_support)  # the other roles at once
        self.assertFalse(self.new.is_super_admin or self.new.is_staff or self.new.is_superuser)
        req = superadmins.pending_for(self.new)
        self.assertEqual((req.op, req.required, req.requested_by_id), ("add", 2, self.sa1.pk))
        superadmins.confirm(self.sa2, req)
        self.new.refresh_from_db()
        self.assertTrue(self.new.is_super_admin)

    def test_the_technical_admin_cannot_change_people(self):
        """core-016 (fourth, fifth and ninth full reviews): people are shown in the technical admin, never changed,
        added or deleted there - not their roles, not who may sign in, not their e-mail or two-factor state (a
        password reset to a new address with no second factor took over another super admin's account)."""
        url = f"/django-admin/accounts/user/{self.sa2.pk}/change/"
        self.assertEqual(self.client.get(url).status_code, 200)  # shown
        r = self.client.post(url, {"email": "mine@evil.example", "totp_enabled": "", "is_super_admin": "on"})
        self.assertEqual(r.status_code, 403)
        self.sa2.refresh_from_db()
        self.assertEqual((self.sa2.email, self.sa2.totp_enabled, self.sa2.is_super_admin), ("sa2@corp.kz", True, True))
        r = self.client.post(f"/django-admin/accounts/user/{self.new.pk}/change/", {"is_super_admin": "on"})
        self.assertEqual(r.status_code, 403)
        self.new.refresh_from_db()
        self.assertFalse(self.new.is_super_admin or self.new.is_staff or self.new.is_superuser)
        self.assertEqual(self.client.get("/django-admin/accounts/user/add/").status_code, 403)
        r = self.client.post(f"/django-admin/accounts/user/{self.sa2.pk}/delete/", {"post": "yes"})
        self.assertEqual(r.status_code, 403)  # never deleted here
        self.assertTrue(User.objects.filter(pk=self.sa2.pk).exists())


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ArchiveNeedsAgreement(TestCase):
    """core-016 again (fifth full review): archiving a super admin takes the role, so it waits for the super admins'
    agreement like any removal; then the person is archived."""

    def mk(self, email, **kw):
        return User.objects.create_user(
            email, "Str0ng-PASS-123", kind=Kind.STAFF, login_enabled=True, is_active=True, **TOTP, **kw
        )

    def test_archiving_another_super_admin_waits(self):
        everything = dict(is_resource_manager=True, is_support=True, is_sales=True, is_kam=True)
        sa1 = self.mk("sa1@corp.kz", is_super_admin=True, **everything)
        sa2 = self.mk("sa2@corp.kz", is_super_admin=True)
        sa3 = self.mk("sa3@corp.kz", is_super_admin=True)
        self.assertEqual(people.archive(sa1, sa3), "")
        sa3.refresh_from_db()
        self.assertTrue(sa3.is_super_admin and sa3.is_active and sa3.archived_at is None)  # nothing yet
        req = superadmins.pending_for(sa3)
        self.assertEqual((req.op, req.archive, req.required), ("remove", True, 2))
        superadmins.confirm(sa2, req)
        sa3.refresh_from_db()
        self.assertFalse(sa3.is_super_admin or sa3.is_active)
        self.assertIsNotNone(sa3.archived_at)

    def test_the_page_says_it_waits(self):
        from django.urls import reverse

        from accounts.testkit import otp

        everything = dict(is_resource_manager=True, is_support=True, is_sales=True, is_kam=True)
        sa1 = self.mk("sa1@corp.kz", is_super_admin=True, **everything)
        self.mk("sa2@corp.kz", is_super_admin=True)
        sa3 = self.mk("sa3@corp.kz", is_super_admin=True)
        self.client.force_login(sa1)
        otp(self.client)
        r = self.client.post(reverse("person_archive", args=[sa3.pk]), follow=True)
        self.assertContains(r, "once 2 super admins agree")
        sa3.refresh_from_db()
        self.assertIsNone(sa3.archived_at)

    def test_a_staff_user_without_the_role_is_archived_at_once(self):
        everything = dict(is_resource_manager=True, is_support=True, is_sales=True, is_kam=True)
        sa1 = self.mk("sa1@corp.kz", is_super_admin=True, **everything)
        ops = self.mk("ops@corp.kz", is_support=True)
        self.assertEqual(people.archive(sa1, ops), "")
        ops.refresh_from_db()
        self.assertIsNotNone(ops.archived_at)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ArchiveRequestIsItsOwn(TestCase):
    """commit-028 and commit-029: an archive request completes only while the others hold every staff role, never
    joins (or is joined by) another request for the role, and says "archive" wherever it is shown."""

    EVERYTHING = dict(is_resource_manager=True, is_support=True, is_sales=True, is_kam=True)

    def mk(self, email, **kw):
        return User.objects.create_user(
            email, "Str0ng-PASS-123", kind=Kind.STAFF, login_enabled=True, is_active=True, **TOTP, **kw
        )

    def setUp(self):
        self.sa1 = self.mk("sa1@corp.kz", is_super_admin=True, **self.EVERYTHING)
        self.sa2 = self.mk("sa2@corp.kz", is_super_admin=True)
        self.sa3 = self.mk("sa3@corp.kz", is_super_admin=True, **self.EVERYTHING)

    def test_a_role_given_up_meanwhile_keeps_it_pending(self):
        self.assertEqual(people.archive(self.sa1, self.sa3), "")
        req = superadmins.pending_for(self.sa3)
        # sa1 gives up the support role: sa3 would be the only one left holding it
        self.assertEqual(
            people.set_staff_roles(self.sa1, self.sa1, ["is_super_admin", "is_resource_manager", "is_sales", "is_kam"]),
            [],
        )
        err = superadmins.confirm(self.sa2, req)
        self.assertIn("is not archived yet", err)
        self.sa3.refresh_from_db()
        req.refresh_from_db()
        self.assertTrue(self.sa3.is_super_admin and self.sa3.archived_at is None)
        self.assertEqual(req.state, "pending")

    def test_never_joined_to_another_request(self):
        _req, err = superadmins.request(self.sa1, self.sa3)  # a removal of the role is pending
        self.assertEqual(err, "")
        self.assertIn("is pending: decide it first", people.archive(self.sa1, self.sa3))
        self.assertFalse(superadmins.pending_for(self.sa3).archive)
        other = self.mk("sa4@corp.kz", is_super_admin=True)
        self.assertEqual(people.archive(self.sa1, other), "")  # an archive is pending for sa4
        _req, err = superadmins.request(self.sa1, other)
        self.assertIn("is pending: decide it first", err)

    def test_the_box_and_the_e_mail_say_archive(self):
        from django.core import mail
        from django.urls import reverse

        from accounts.testkit import otp

        mail.outbox = []
        self.assertEqual(people.archive(self.sa1, self.sa3), "")
        self.assertTrue(any("asks to archive" in m.body for m in mail.outbox))
        self.assertTrue(any("archive requested" in m.subject for m in mail.outbox))
        self.client.force_login(self.sa2)
        otp(self.client)
        r = self.client.get(reverse("staff_users") + "?tab=platform")
        self.assertContains(r, ">archive<")

    def test_a_held_archive_completes_once_the_role_is_covered_again(self):
        """commit-031: the agreement stands; giving the role back (or asking again) completes it."""
        from django.urls import reverse

        from accounts.testkit import otp

        held_roles = ["is_super_admin", "is_resource_manager", "is_sales", "is_kam"]
        self.assertEqual(people.archive(self.sa1, self.sa3), "")
        req = superadmins.pending_for(self.sa3)
        self.assertEqual(people.set_staff_roles(self.sa1, self.sa1, held_roles), [])
        self.assertIn("is not archived yet", superadmins.confirm(self.sa2, req))
        self.client.force_login(self.sa2)
        otp(self.client)
        r = self.client.get(reverse("staff_users") + "?tab=platform")
        self.assertContains(r, "it completes by itself once this is done")
        self.assertNotContains(r, f'action="{reverse("super_admin_removal", args=[req.pk, "confirm"])}"')
        self.assertEqual(people.set_staff_roles(self.sa1, self.sa1, [*held_roles, "is_support"]), [])  # given back
        self.sa3.refresh_from_db()
        req.refresh_from_db()
        self.assertEqual(req.state, "done")
        self.assertIsNotNone(self.sa3.archived_at)
        self.assertFalse(self.sa3.is_super_admin)

    def test_asking_again_tries_again(self):
        held_roles = ["is_super_admin", "is_resource_manager", "is_sales", "is_kam"]
        self.assertEqual(people.archive(self.sa1, self.sa3), "")
        req = superadmins.pending_for(self.sa3)
        people.set_staff_roles(self.sa1, self.sa1, held_roles)
        superadmins.confirm(self.sa2, req)
        User.objects.filter(pk=self.sa1.pk).update(is_support=True)  # covered again, by whatever way
        self.assertEqual(people.archive(self.sa1, self.sa3), "")
        self.sa3.refresh_from_db()
        self.assertIsNotNone(self.sa3.archived_at)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class DecidedMeanwhile(TestCase):
    """core-022 (ninth full review): a request declined (or lapsed) while a confirmation was on its way stays so -
    the confirmation's completion works on the request read again under its lock, and never overwrites a decision."""

    def mk(self, email, **kw):
        return User.objects.create_user(
            email, "Str0ng-PASS-123", kind=Kind.STAFF, login_enabled=True, is_active=True, **TOTP, **kw
        )

    def setUp(self):
        self.sa1 = self.mk("sa1@corp.kz", is_super_admin=True)
        self.sa2 = self.mk("sa2@corp.kz", is_super_admin=True)
        self.sa3 = self.mk("sa3@corp.kz", is_super_admin=True)
        self.new = self.mk("new@corp.kz")
        self.req, _err = superadmins.request_add(self.sa1, self.new)  # 3 super admins: 2 must agree

    def test_a_decline_committed_before_the_completion_stands(self):
        from accounts.models import SuperAdminRemoval

        stale = SuperAdminRemoval.objects.get(pk=self.req.pk)  # the confirming worker read it pending
        stale.confirmed = [self.sa2.pk]  # ... and had recorded the confirmation that makes it ready
        SuperAdminRemoval.objects.filter(pk=stale.pk).update(confirmed=[self.sa2.pk])
        self.assertEqual(superadmins.decline(self.sa3, SuperAdminRemoval.objects.get(pk=stale.pk)), "")
        self.assertFalse(superadmins.complete_if_ready(stale, self.sa2))  # its stale object still says pending
        self.new.refresh_from_db()
        self.assertFalse(self.new.is_super_admin)
        self.assertEqual(SuperAdminRemoval.objects.get(pk=stale.pk).state, "declined")

    def test_a_confirmation_on_a_declined_request_is_refused(self):
        from accounts.models import SuperAdminRemoval

        stale = SuperAdminRemoval.objects.get(pk=self.req.pk)
        superadmins.decline(self.sa3, SuperAdminRemoval.objects.get(pk=stale.pk))
        self.assertEqual(superadmins.confirm(self.sa2, stale), "This request is closed.")
        self.new.refresh_from_db()
        self.assertFalse(self.new.is_super_admin)
        self.assertEqual(SuperAdminRemoval.objects.get(pk=stale.pk).state, "declined")

    def test_a_decline_after_completion_is_refused(self):
        from accounts.models import SuperAdminRemoval

        stale = SuperAdminRemoval.objects.get(pk=self.req.pk)
        self.assertEqual(superadmins.confirm(self.sa2, SuperAdminRemoval.objects.get(pk=stale.pk)), "")
        self.assertEqual(superadmins.decline(self.sa3, stale), "This request is closed.")
        self.assertEqual(SuperAdminRemoval.objects.get(pk=stale.pk).state, "done")
