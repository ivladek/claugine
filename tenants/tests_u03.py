"""u03 (owner 2026-10-31): the project edit form (compact, contacts at the bottom; a stage change alone is saved),
users added or invited (Settings → Platform decides whether adding is allowed), the users' columns, one copy icon."""
from django.core import mail
from django.urls import reverse

from accounts.models import Kind, User
from accounts.tests import Base, valid_bin
from tenants.models import Organization, PortalSettings, Tenant


class U03(Base):
    def setUp(self):
        self.sa = self.staff("sa@corp.kz", is_super_admin=True, is_resource_manager=True)
        self.org = Organization.objects.create(name='ТОО "ALPHA"', tax_id=valid_bin(), short_name="alpha")
        self.t = Tenant.objects.create(organization=self.org, name="proj", title="P", approval="approved", stage="poc")
        self.client.force_login(self.sa)

    def test_edit_form_order_and_stage_alone(self):
        page = self.client.get(reverse("tenant_edit_all", args=[self.t.pk])).content.decode()  # u05: edit is its own page
        # u10 redesign: org is a read-only context line; later sections are Team then Notes
        order = [page.index(x) for x in (">Team</legend>", ">Notes</legend>")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn('name="mv-organization" required', page)  # hidden until "Move to another": not required
        r = self.client.post(reverse("tenant_edit_all", args=[self.t.pk]), {"st-stage": "client", "e-name": "proj", "e-title": "P",
                                                                         "mv-organization": ""})
        self.assertEqual(r.status_code, 302)
        self.t.refresh_from_db()
        self.assertEqual(self.t.stage, "client")

    def test_add_or_invite(self):
        url = reverse("staff_users")
        # u11: the add/invite buttons were removed from the users lists; the endpoints still serve those flows
        # (platform invitations from here, customer invitations from the organization page).
        mail.outbox.clear()
        self.client.post(reverse("staff_invite_new"), {"email": "new@corp.kz", "full_name": "New Person", "phone": "",
                                                        "city": "astana", "roles": ["is_support"]})
        u = User.objects.get(email="new@corp.kz")
        self.assertTrue(u.is_support)
        self.assertEqual(mail.outbox[-1].to, ["new@corp.kz"])
        self.client.post(reverse("customer_add"), {"organization": self.org.pk, "full_name": "Asel Nurlanova",
                                                    "email": "asel@alpha.kz", "phone": "+7 701 2345678", "city": "astana"})
        c = User.objects.get(email="asel@alpha.kz")
        self.assertEqual((c.kind, c.organization_id, c.login_enabled), (Kind.CUSTOMER, self.org.pk, False))
        r = self.client.get(url + "?tab=tenant").content.decode()
        self.assertLess(r.index(">Organization<"), r.index(">Name<"))  # u10: Organization, Name, Projects, then role columns
        PortalSettings.get()
        PortalSettings.objects.update(users_add=False)  # Settings → Platform: only invited
        self.assertEqual(self.client.post(reverse("person_add"), {"email": "x@corp.kz", "full_name": "X Y", "city": "astana"}).status_code, 403)
        self.client.post(reverse("portal_settings"), {"tab": "platform", "section": "users_add", "users_add": "on"})
        self.assertTrue(PortalSettings.get().users_add)

    def test_copy_icon(self):
        from django.template.loader import render_to_string
        html = render_to_string("ui/copy.html", {"text": "10.0.0.1"})
        self.assertIn("⧉", html)
        self.assertNotIn(">Copy<", html)

    def test_corp_org_admin_picker(self):
        # u11: a corporate org's edit form offers a platform user as its org admin, and saving sets it
        corp = Organization.objects.create(name='ТОО "BETA"', tax_id=valid_bin("0205400012"), short_name="beta", kind="corp")
        plat = self.staff("plat@corp.kz")  # a platform (staff) user
        url = reverse("org_edit", args=[corp.pk])
        page = self.client.get(url).content.decode()
        self.assertIn('name="org_admin"', page)
        self.assertNotIn(f'value="{plat.pk}"', page)  # owner 2026-11-03: not offered until flagged may_org_admin
        plat.may_org_admin = True
        plat.save()
        page = self.client.get(url).content.decode()
        self.assertIn(f'value="{plat.pk}"', page)  # now an eligible candidate
        r = self.client.post(url, {"short_name": "beta", "org_admin": str(plat.pk), "notes": ""})
        self.assertEqual(r.status_code, 302)
        plat.refresh_from_db()
        self.assertTrue(plat.is_org_admin)
        self.assertEqual(plat.organization_id, corp.pk)
        self.assertIn(plat, list(corp.admins))
