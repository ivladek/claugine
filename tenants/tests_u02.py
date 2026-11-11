"""u02 (owner 2026-10-31): an archived organization's users are archived with it; short names editable only while
nothing uses them (an organization without projects, a project not provisioned), at most 15 characters; the
project edit form shows the organization first, with "Move to another"."""
from django.urls import reverse

from accounts.models import Kind, User
from accounts.tests import Base, valid_bin
from tenants import naming
from tenants.forms import OrganizationForm, TenantStaffForm
from tenants.models import Organization, Tenant


class U02(Base):
    def setUp(self):
        self.rm = self.staff("rm@corp.kz", is_resource_manager=True, is_super_admin=True)
        self.org = Organization.objects.create(name='ТОО "ALPHA"', tax_id=valid_bin(), short_name="alpha")

    def test_lengths(self):
        self.assertTrue(naming.ORG_RE.match("a" * 15) and not naming.ORG_RE.match("a" * 16))
        self.assertTrue(naming.TENANT_RE.match("p" * 15) and not naming.TENANT_RE.match("p" * 16))

    def test_archive_takes_the_users(self):
        u = User.objects.create_user("adm@alpha.kz", "Str0ng-PASS-123", kind=Kind.CUSTOMER, organization=self.org,
                                     email_verified=True, is_org_admin=True)
        self.client.force_login(self.rm)
        self.client.post(reverse("org_delete", args=[self.org.pk]))
        self.org.refresh_from_db()
        u.refresh_from_db()
        self.assertIsNotNone(self.org.archived_at)
        self.assertIsNotNone(u.archived_at)
        self.assertFalse(u.is_active or u.login_enabled)

    def test_org_short_name_only_without_projects(self):
        self.assertTrue(self.org.short_name_editable)
        self.assertFalse(OrganizationForm(instance=self.org).fields["short_name"].disabled)
        Tenant.objects.create(organization=self.org, name="proj", title="P", approval="pending")
        self.assertFalse(Organization.objects.get(pk=self.org.pk).short_name_editable)
        self.assertTrue(OrganizationForm(instance=Organization.objects.get(pk=self.org.pk)).fields["short_name"].disabled)

    def test_project_short_name_until_provisioned(self):
        t = Tenant.objects.create(organization=self.org, name="proj", title="P", approval="approved",
                                  group_name=naming.group_name("alpha", "proj"))
        f = TenantStaffForm({"e-name": "newname15chars1", "e-title": "P"}, instance=t, prefix="e")
        self.assertTrue(f.is_valid(), f.errors)
        f.save()
        t.refresh_from_db()
        self.assertEqual((t.name, t.group_name), ("newname15chars1", "_alpha_newname15chars1"))  # the group follows
        self.assertFalse(TenantStaffForm({"e-name": "x" * 16, "e-title": "P"}, instance=t, prefix="e").is_valid())
        Tenant.objects.filter(pk=t.pk).update(one_group_id=301)  # on the platform now
        t.refresh_from_db()
        f = TenantStaffForm(instance=t, prefix="e")
        # u05: the ID (name) is fixed once provisioned; the description (title) can be changed at any time
        self.assertTrue(f.fields["name"].disabled)
        self.assertFalse(f.fields["title"].disabled)

    def test_edit_form_shows_the_organization_first(self):
        t = Tenant.objects.create(organization=self.org, name="proj", title="P", approval="approved")
        self.client.force_login(self.rm)
        page = self.client.get(reverse("tenant_edit_all", args=[t.pk])).content.decode()  # u05: edit is its own page
        self.assertIn("Move to another", page)
        self.assertNotIn(">Move to another organization</legend>", page)
        self.assertLess(page.index('ТОО &quot;ALPHA&quot;'), page.index('name="e-name"'))
