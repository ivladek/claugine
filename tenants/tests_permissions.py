"""owner u12: per-role rights — Manager, KAM, Org admin, Project admin."""
from django.test import TestCase

from accounts.models import Kind, User
from tenants import permissions as P
from tenants.models import Membership, Organization, Tenant

TOTP = dict(totp_secret="JBSWY3DPEHPK3PXP", totp_enabled=True, email_verified=True)


class RoleRights(TestCase):
    def setUp(self):
        self.sa = User.objects.create_user("sa@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_super_admin=True, **TOTP)
        self.rm = User.objects.create_user("rm@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_resource_manager=True, **TOTP)
        self.mgr = User.objects.create_user("mgr@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_sales=True, **TOTP)
        self.mgr2 = User.objects.create_user("mgr2@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_sales=True, **TOTP)
        self.kam = User.objects.create_user("kam@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, is_kam=True, **TOTP)
        self.org = Organization.objects.create(name="Acme", short_name="acme", sales_rep=self.mgr, kam=self.kam)
        self.other = Organization.objects.create(name="Other", short_name="other", sales_rep=self.mgr2)
        self.t = Tenant.objects.create(organization=self.org, name="lab", title="lab", kind="cloud", approval="approved",
                                       group_name="_acme_lab")
        self.oadmin = User.objects.create_user("oa@acme.kz", "Str0ng-PASS-123", kind=Kind.CUSTOMER, organization=self.org,
                                               is_org_admin=True, **TOTP)
        self.padmin = User.objects.create_user("pa@acme.kz", "Str0ng-PASS-123", kind=Kind.CUSTOMER, organization=self.org, **TOTP)
        Membership.objects.create(user=self.padmin, tenant=self.t, role=Membership.Role.TENANT_ADMIN)

    def test_manager(self):
        self.assertTrue(P.can_edit_org(self.mgr, self.org))
        self.assertFalse(P.can_edit_org(self.mgr, self.other))       # only their own org
        self.assertTrue(P.can_add_org(self.mgr))
        self.assertTrue(P.can_create_project(self.mgr, self.org))
        self.assertTrue(P.can_manage_org_users(self.mgr, self.org))
        self.assertTrue(P.can_manage_project(self.mgr, self.t))

    def test_kam(self):
        self.assertFalse(P.can_edit_org(self.kam, self.org))         # org read-only
        self.assertFalse(P.can_add_org(self.kam))
        self.assertFalse(P.can_create_project(self.kam, self.org))   # no project creation
        self.assertTrue(P.can_manage_org_users(self.kam, self.org))  # but can manage users
        self.assertFalse(P.can_archive_org_users(self.kam, self.org))
        self.assertTrue(P.can_manage_project(self.kam, self.t))      # full resource mgmt in its projects

    def test_org_admin(self):
        self.assertTrue(P.can_manage_org_users(self.oadmin, self.org))
        self.assertTrue(P.can_archive_org_users(self.oadmin, self.org))
        self.assertTrue(P.can_create_project(self.oadmin, self.org))
        self.assertTrue(P.can_assign_project_admin(self.oadmin, self.org))
        self.assertTrue(P.can_manage_project(self.oadmin, self.t))
        self.assertFalse(P.can_edit_org(self.oadmin, self.org))      # not org details

    def test_project_admin(self):
        self.assertTrue(P.can_manage_project(self.padmin, self.t))   # resources in the assigned project
        self.assertFalse(P.can_create_project(self.padmin, self.org))
        self.assertFalse(P.can_manage_org_users(self.padmin, self.org))
        other_t = Tenant.objects.create(organization=self.org, name="lab2", title="lab2", kind="cloud",
                                        approval="approved", group_name="_acme_lab2")
        self.assertFalse(P.can_manage_project(self.padmin, other_t))  # only the projects assigned to them

    def test_project_deletion_manager_not_kam(self):
        self.assertTrue(self.t.can_request_deletion(self.mgr))    # the org's manager may delete its projects
        self.assertFalse(self.t.can_request_deletion(self.kam))   # the KAM may not
        self.assertFalse(self.t.can_request_deletion(self.mgr2))  # not the manager of this org
        self.assertTrue(self.t.can_request_deletion(self.oadmin))   # org admin can
        self.assertFalse(self.t.can_request_deletion(self.padmin))  # project admin may not delete the project
        self.assertTrue(self.t.can_request_deletion(self.rm))       # resource manager can

    def test_rm_and_super_full(self):
        for u in (self.sa, self.rm):
            self.assertTrue(P.can_edit_org(u, self.other))
            self.assertTrue(P.can_create_project(u, self.other))
            self.assertTrue(P.can_manage_project(u, self.t))
