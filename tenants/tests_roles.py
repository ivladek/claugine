"""Owner 2026-11-03: org-user role rules — each organization keeps at least one org admin, each project keeps at
least one project admin, a user may hold both roles at once, and credentials go only to project admins."""

from django.test import TestCase
from django.urls import reverse

from accounts import people
from accounts.models import Kind, User
from tenants.admins import fill_missing, proposed, set_tenant_admin
from tenants.models import Membership, Organization, Tenant


class RoleRules(TestCase):
    def setUp(self):
        from accounts.tests import valid_bin

        self.org = Organization.objects.create(name="Romashka", tax_id=valid_bin(), short_name="romashka")
        self.oa = User.objects.create_user(
            "oa@romashka.kz",
            "Str0ng-PASS-123",
            kind=Kind.CUSTOMER,
            organization=self.org,
            is_org_admin=True,
            email_verified=True,
            login_enabled=True,
        )
        self.u = User.objects.create_user(
            "u@romashka.kz",
            "Str0ng-PASS-123",
            kind=Kind.CUSTOMER,
            organization=self.org,
            email_verified=True,
            login_enabled=True,
        )

    def _tenant(self, name):
        return Tenant.objects.create(organization=self.org, name=name, title=name, kind="cloud", approval="approved")

    def test_last_org_admin_cannot_be_dropped(self):
        err = people.set_customer_roles(None, self.oa, org_admin=False, read_only=False, tenant_admin_of=[])
        self.assertTrue(err)
        self.oa.refresh_from_db()
        self.assertTrue(self.oa.is_org_admin)  # unchanged: the organization keeps its only admin
        self.u.is_org_admin = True
        self.u.save()
        self.assertEqual(people.set_customer_roles(None, self.oa, False, False, []), "")  # a second admin exists now
        self.oa.refresh_from_db()
        self.assertFalse(self.oa.is_org_admin)

    def test_user_can_hold_both_roles(self):
        t = self._tenant("lab")
        self.assertEqual(people.set_customer_roles(None, self.oa, True, False, [t]), "")  # org admin AND project admin
        self.oa.refresh_from_db()
        self.assertTrue(self.oa.is_org_admin)
        self.assertTrue(Membership.objects.filter(user=self.oa, tenant=t, role="tenant_admin").exists())

    def test_fill_missing_gives_every_project_an_admin(self):
        t1, t2 = self._tenant("lab"), self._tenant("ml")
        self.assertIsNone(t1.tenant_admin)
        names = fill_missing(self.org)
        t1.refresh_from_db()
        t2.refresh_from_db()
        self.assertEqual(t1.tenant_admin, self.oa)  # the org admin is chosen
        self.assertEqual(t2.tenant_admin, self.oa)
        self.assertCountEqual(names, ["lab", "ml"])

    def test_proposed_skips_inactive_admin(self):
        t = self._tenant("lab")
        set_tenant_admin(t, self.u)
        self.u.is_active = False
        self.u.save()
        who, _why = proposed(t, exclude=self.u)
        self.assertEqual(who, self.oa)  # not the inactive u

    def test_archive_reassigns_project_admin(self):
        t = self._tenant("lab")
        set_tenant_admin(t, self.u)
        t.refresh_from_db()
        self.assertEqual(t.tenant_admin, self.u)
        self.assertEqual(people.archive(self.oa, self.u), "")  # u archived; its projects handed on
        t.refresh_from_db()
        self.assertEqual(t.tenant_admin, self.oa)  # now the org admin
        self.u.refresh_from_db()
        self.assertIsNotNone(self.u.archived_at)

    def test_users_page_role_checkbox_columns(self):
        sa = User.objects.create_user(
            "sa@corp.kz",
            "Str0ng-PASS-123",
            kind=Kind.STAFF,
            is_super_admin=True,
            is_staff=True,
            is_superuser=True,
            email_verified=True,
        )
        self.client.force_login(sa)
        r = self.client.get(reverse("staff_users") + "?tab=tenant")
        self.assertContains(r, ">Organization<")
        self.assertContains(r, ">Projects<")
        self.assertContains(r, 'class="role-col"')  # u10: a checkbox column per role
        self.assertContains(r, "Org admin")  # a role column header
        self.assertContains(r, "oa@romashka.kz")

    def test_roles_can_be_assigned_to_inactive_user_and_reach_reports(self):
        from accounts import reports
        from accounts.models import Kind

        # a platform user who was never invited (no login) but holds the support role
        sup = User.objects.create_user(
            "sup@corp.kz", None, kind=Kind.STAFF, is_support=True, login_enabled=False, email_verified=True
        )
        self.assertIn(sup, reports._staff(is_support=True))  # reports reach them even though not invited
        # and a blocked user does not receive them
        sup.is_active = False
        sup.save()
        self.assertNotIn(sup, reports._staff(is_support=True))

    def test_role_removable_from_active_user_when_an_inactive_user_holds_it(self):
        """owner 2026-11-03: a role may be removed from an active user as long as any user (even a not-yet-invited
        one) still holds it; it is blocked only when that would leave nobody holding it."""
        from accounts import people

        sa = User.objects.create_user(
            "sa@corp.kz",
            "Str0ng-PASS-123",
            kind=Kind.STAFF,
            is_super_admin=True,
            is_staff=True,
            is_superuser=True,
            email_verified=True,
            login_enabled=True,
        )
        active_sup = User.objects.create_user(
            "sup-a@corp.kz",
            "Str0ng-PASS-123",
            kind=Kind.STAFF,
            is_support=True,
            login_enabled=True,
            email_verified=True,
        )
        inactive_sup = User.objects.create_user(
            "sup-i@corp.kz", None, kind=Kind.STAFF, is_support=True, login_enabled=False, email_verified=True
        )  # never invited
        # allowed: support still held by the inactive user
        self.assertEqual(people.set_staff_roles(sa, active_sup, []), [])
        active_sup.refresh_from_db()
        self.assertFalse(active_sup.is_support)
        # blocked: removing it from the sole remaining holder leaves the role with nobody
        errs = people.set_staff_roles(sa, inactive_sup, [])
        self.assertTrue(errs)
        inactive_sup.refresh_from_db()
        self.assertTrue(inactive_sup.is_support)  # unchanged

    def test_platform_user_with_org_role_shows_as_org_user(self):
        """owner 2026-11-03: a platform (staff) user who is a corporate project's admin appears in the org-users
        list too, read-only (platform flag)."""
        from accounts import people
        from accounts.tests import valid_bin

        corp = Organization.objects.create(
            name='ТОО "CORP"', tax_id=valid_bin("0205400012"), short_name="corp", kind="corp"
        )
        sup = User.objects.create_user(
            "sup2@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_support=True, email_verified=True, login_enabled=True
        )
        t = Tenant.objects.create(organization=corp, name="lab", title="lab", kind="corp", approval="approved")
        Membership.objects.create(tenant=t, user=sup, role=Membership.Role.TENANT_ADMIN)
        rows = people.customer_rows(corp)
        pr = [r for r in rows if r.get("platform")]
        self.assertEqual([r["u"].pk for r in pr], [sup.pk])
        on = {k: v for k, _l, v in pr[0]["roles"]}
        self.assertTrue(on["tenant_admin"])
        self.assertFalse(on["org_admin"])
        self.assertEqual([x.pk for x in pr[0]["info"]["admin_of"]], [t.pk])
        self.assertEqual(pr[0]["rowid"], f"u-{sup.pk}-o{corp.pk}")  # unique id even across orgs

    def test_platform_user_not_shown_for_non_created_project(self):
        """owner 2026-11-03: a platform user's project-admin membership on a not-yet-created (pending) project must
        not list them as its project admin in the org users."""
        from accounts import people
        from accounts.tests import valid_bin

        corp = Organization.objects.create(
            name='ТОО "PEND"', tax_id=valid_bin("0205400012"), short_name="pend", kind="corp"
        )
        sup = User.objects.create_user(
            "sup4@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_support=True, email_verified=True, login_enabled=True
        )
        t = Tenant.objects.create(organization=corp, name="draft", title="draft", kind="corp", approval="pending")
        Membership.objects.create(tenant=t, user=sup, role=Membership.Role.TENANT_ADMIN)
        self.assertEqual([r for r in people.customer_rows(corp) if r.get("platform")], [])  # pending → hidden
        t.approval = "approved"
        t.save()
        self.assertEqual([r["u"].pk for r in people.customer_rows(corp) if r.get("platform")], [sup.pk])  # now shown

    def test_platform_org_role_eligibility_toggle(self):
        """owner 2026-11-03: the org-admin/project-admin eligibility flags toggle straight from the platform list,
        without the 'every role keeps a holder' guard."""
        sa = User.objects.create_user(
            "sa3@corp.kz",
            "Str0ng-PASS-123",
            kind=Kind.STAFF,
            is_super_admin=True,
            is_staff=True,
            is_superuser=True,
            email_verified=True,
        )
        stf = User.objects.create_user(
            "stf@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_support=True, email_verified=True, login_enabled=True
        )
        self.client.force_login(sa)
        self.client.post(reverse("person_role_set", args=[stf.pk]), {"role": "may_org_admin", "on": "1"})
        self.client.post(reverse("person_role_set", args=[stf.pk]), {"role": "may_tenant_admin", "on": "1"})
        stf.refresh_from_db()
        self.assertTrue(stf.may_org_admin and stf.may_tenant_admin and stf.is_support)  # support role untouched
        self.client.post(reverse("person_role_set", args=[stf.pk]), {"role": "may_org_admin"})  # off
        stf.refresh_from_db()
        self.assertFalse(stf.may_org_admin)
        self.assertTrue(stf.may_tenant_admin)

    def test_kam_and_sales_fields_offer_only_role_holders(self):
        """owner 2026-11-03: only a user who holds the role can be assigned to it — a stale assignee who lost the
        role (or was blocked) is not offered on the org edit form."""
        from tenants.forms import OrganizationForm

        kam = User.objects.create_user(
            "kam@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_kam=True, email_verified=True, login_enabled=True
        )
        ex_kam = User.objects.create_user(
            "exk@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_kam=False, email_verified=True, login_enabled=True
        )
        self.org.kam = ex_kam  # stale: org points at a user who is not a KAM
        self.org.save()
        f = OrganizationForm(instance=self.org)
        pks = set(f.fields["kam"].queryset.values_list("pk", flat=True))
        self.assertIn(kam.pk, pks)
        self.assertNotIn(ex_kam.pk, pks)  # strict: the non-KAM assignee is not selectable

    def test_inline_role_toggle_and_read_only_exclusive(self):
        sa = User.objects.create_user(
            "sa2@corp.kz",
            "Str0ng-PASS-123",
            kind=Kind.STAFF,
            is_super_admin=True,
            is_staff=True,
            is_superuser=True,
            email_verified=True,
        )
        self.client.force_login(sa)
        t = self._tenant("lab")
        # project admin via the picker (tenants list)
        self.client.post(reverse("person_role_set", args=[self.u.pk]), {"role": "tenant_admin", "tenants": [t.pk]})
        self.assertTrue(Membership.objects.filter(user=self.u, tenant=t, role="tenant_admin").exists())
        # read-only clears any other org role
        self.client.post(reverse("person_role_set", args=[self.u.pk]), {"role": "read_only", "on": "1"})
        self.u.refresh_from_db()
        self.assertTrue(self.u.is_org_readonly)
        self.assertFalse(Membership.objects.filter(user=self.u, role="tenant_admin").exists())
        # turning on another role clears read-only
        self.client.post(reverse("person_role_set", args=[self.u.pk]), {"role": "org_admin", "on": "1"})
        self.u.refresh_from_db()
        self.assertTrue(self.u.is_org_admin and not self.u.is_org_readonly)

    def test_credentials_recipients_are_project_admins_only(self):
        from tenants.notices import _project_admins

        t = self._tenant("lab")
        set_tenant_admin(t, self.u)  # project admin = u (a plain org user)
        t.refresh_from_db()
        self.assertEqual([x.pk for x in _project_admins(t)], [self.u.pk])  # the org admin oa is NOT a recipient
        # a second project admin is included; an org admin who is also a project admin is included
        Membership.objects.create(user=self.oa, tenant=t, role="tenant_admin")
        self.assertCountEqual([x.pk for x in _project_admins(t)], [self.u.pk, self.oa.pk])
