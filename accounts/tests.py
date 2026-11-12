"""The accounts app's own tests: sign-in and two-factor, registration, invitations, people and roles, Settings.

The other `tests_*.py` files in this app each cover one subject (see `worklog/internals.md`); this one holds what
belongs to no single subject.
"""

import io
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from accounts import edition
from accounts.kz import normalize_phone, tax_id_checksum_ok, validate_tax_id
from accounts.models import Invitation, Kind, Role, User
from tenants.models import Approval, JoinRequest, Membership, Organization, PortalSettings, Tenant

TMP_MEDIA = tempfile.mkdtemp()


def make_tax_id(prefix11):
    for d in "0123456789":
        if tax_id_checksum_ok(prefix11 + d):
            return prefix11 + d
    raise ValueError


def valid_bin(seed="0105400012"):
    for extra in "0123456789":
        try:
            return make_tax_id(seed + extra)
        except ValueError:
            continue


def valid_iin(seed="8501153001"):
    for extra in "0123456789":
        try:
            return make_tax_id(seed + extra)
        except ValueError:
            continue


def photo():
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), "teal").save(buf, "JPEG")
    return SimpleUploadedFile("me.jpg", buf.getvalue(), content_type="image/jpeg")


def link_path(msg):
    return re.sub(r"^https?://[^/]+", "", re.search(r"https?://\S+", msg.body).group(0))


PROFILE = {
    "full_name": "Aigerim Sarsenova",
    "phone": "8 701 123 45 67",
    "city": "almaty",
    "consent": "on",
    "password1": "Str0ng-PASS-123",
    "password2": "Str0ng-PASS-123",
}


@override_settings(MEDIA_ROOT=TMP_MEDIA, SITE_URL="http://testserver")
class Base(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TMP_MEDIA, ignore_errors=True)

    def staff(self, email, totp=True, **flags):
        u = User.objects.create_user(
            email, "Str0ng-PASS-123", kind=Kind.STAFF, email_verified=True, full_name=email.split("@")[0], **flags
        )
        if totp:
            u.totp_enabled = True
            u.save()
        return u

    def sales_entry(self, email="rep@corp.kz"):
        return User.objects.create_user(
            email, None, kind=Kind.STAFF, is_sales=True, login_enabled=False, full_name="Dana Sales"
        )

    def register(self, email, tax_id, tax_type="bin", org_name="ТОО «Romashka»"):
        return self.client.post(
            reverse("register"),
            {
                **PROFILE,
                "email": email,
                "photo": photo(),
                "organization_name": org_name,
                "tax_id_type": tax_type,
                "tax_id": tax_id,
            },
        )


class KzValidators(TestCase):
    def test_tax_id(self):
        b = valid_bin()
        validate_tax_id(b, "bin")
        self.assertFalse(tax_id_checksum_ok(b[:-1] + str((int(b[-1]) + 1) % 10)))
        validate_tax_id(valid_iin(), "iin")
        with self.assertRaises(ValidationError):
            validate_tax_id(valid_iin(), "bin")

    def test_phone(self):
        self.assertEqual(normalize_phone("8 (701) 123-45-67"), "+77011234567")
        self.assertEqual(normalize_phone("+7 495 123 45 67"), "+74951234567")  # correction 15: any international number
        with self.assertRaises(ValidationError):
            normalize_phone("701 123 45")


class Roles(Base):
    def test_super_admin_has_every_right(self):
        # correction 16: a super admin has every right whatever roles are ticked; asked only in the roles ticked
        sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.assertTrue(sa.has_role(Role.RESOURCE_MANAGER))
        self.assertTrue(sa.has_role(Role.SUPPORT))
        self.assertTrue(sa.has_role(Role.SALES_MANAGER))
        self.assertFalse(sa.holds(Role.RESOURCE_MANAGER))
        self.assertIn(sa, User.objects.with_role(Role.RESOURCE_MANAGER))  # nobody holds it: the super admins
        rm = self.staff("rm@corp.kz", is_resource_manager=True)
        self.assertEqual(list(User.objects.with_role(Role.RESOURCE_MANAGER)), [rm])

    def test_super_admin_roles_and_removal(self):
        """Correction 16: own roles editable (not the super admin role); another's super admin role is taken
        only when every other super admin confirms within 48 hours."""
        from accounts.models import SuperAdminRemoval

        a = self.staff(
            "a@corp.kz", is_super_admin=True, is_resource_manager=True, is_support=True, is_sales=True, is_kam=True
        )
        b = self.staff("b@corp.kz", is_super_admin=True, is_resource_manager=True)
        c = self.staff("c@corp.kz", is_super_admin=True)
        self.client.force_login(a)
        # own roles: the resource manager role can go (b holds it), the super admin role cannot
        self.client.post(
            reverse("person_roles", args=[a.pk]), {"roles": ["is_super_admin", "is_support", "is_sales", "is_kam"]}
        )
        a.refresh_from_db()
        self.assertFalse(a.is_resource_manager)
        r = self.client.post(
            reverse("person_roles", args=[a.pk]), {"roles": ["is_support", "is_sales", "is_kam"]}, follow=True
        )
        self.assertContains(r, "You cannot take the super admin role from yourself.")
        a.refresh_from_db()
        self.assertTrue(a.is_super_admin)
        # the last holder of a role keeps it
        r = self.client.post(
            reverse("person_roles", args=[a.pk]), {"roles": ["is_super_admin", "is_sales", "is_kam"]}, follow=True
        )
        self.assertContains(r, "at least one user must hold it")
        # a asks to take it from b: c must confirm; everybody is told
        mail.outbox.clear()
        self.client.post(reverse("person_roles", args=[b.pk]), {"roles": ["is_resource_manager"]})
        b.refresh_from_db()
        self.assertTrue(b.is_super_admin)
        req = SuperAdminRemoval.objects.get()
        self.assertEqual(req.state, "pending")
        self.assertEqual(sorted(e for m in mail.outbox for e in m.to), ["a@corp.kz", "b@corp.kz", "c@corp.kz"])
        self.assertContains(self.client.get(reverse("staff_users")), "Super admin role: requests")
        # b cannot confirm their own removal; c confirms → done
        self.client.force_login(b)
        self.client.post(reverse("super_admin_removal", args=[req.pk, "confirm"]))
        req.refresh_from_db()
        self.assertEqual(req.state, "pending")
        self.client.force_login(c)
        self.client.post(reverse("super_admin_removal", args=[req.pk, "confirm"]))
        req.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(req.state, "done")
        self.assertFalse(b.is_super_admin)
        self.assertTrue(b.is_resource_manager)
        # a request lapses after 48 hours; a refusal ends one
        User.objects.filter(pk=b.pk).update(is_super_admin=True)  # three super admins again
        self.client.force_login(a)
        self.client.post(reverse("person_roles", args=[c.pk]), {"roles": []})
        req2 = SuperAdminRemoval.objects.get(state="pending")
        self.assertEqual(req2.target, c)
        SuperAdminRemoval.objects.filter(pk=req2.pk).update(created_at=timezone.now() - timezone.timedelta(hours=49))
        self.client.get(reverse("staff_users"))
        req2.refresh_from_db()
        c.refresh_from_db()
        self.assertEqual(req2.state, "expired")
        self.assertTrue(c.is_super_admin)

    def test_directory_sales_rep_cannot_sign_in(self):
        rep = self.sales_entry()
        rep.set_password("Str0ng-PASS-123")
        rep.save()
        r = self.client.post(reverse("login"), {"username": rep.email, "password": "Str0ng-PASS-123"})
        self.assertEqual(r.status_code, 200)  # form error, not a redirect
        self.assertNotIn("_auth_user_id", self.client.session)


class Registration(Base):
    def test_registration_creates_only_the_organization(self):
        tax = valid_bin()
        rm = self.staff("rm@corp.kz", is_resource_manager=True)
        se = self.staff("se@corp.kz", is_support=True)
        rep = self.staff("rep@corp.kz", is_sales=True)
        self.register("Owner@Romashka.kz", tax)
        user = User.objects.get(email="owner@romashka.kz")
        self.assertFalse(user.is_active)
        r = self.client.get(link_path(mail.outbox[-1]))
        user.refresh_from_db()
        org = Organization.objects.get(tax_id=tax)
        self.assertTrue(user.is_org_admin)
        self.assertEqual(user.organization, org)
        self.assertFalse(Tenant.objects.exists())
        # correction 16: the organization waits for approval; nobody of it signs in until then
        self.assertContains(r, "sent for approval")
        self.assertEqual(org.registration, "pending")
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(mail.outbox[-1].to, ["rm@corp.kz"])
        r = self.client.post(reverse("login"), {"username": user.email, "password": PROFILE["password1"]})
        self.assertContains(r, "waiting for approval")
        self.assertNotIn("_auth_user_id", self.client.session)
        # sales rep approval on: the resource manager assigns the platform users, then the sales rep approves
        PortalSettings.objects.update_or_create(pk=1, defaults={"sales_approval_required": True})
        self.client.force_login(rm)
        self.assertContains(self.client.get(reverse("approvals")), "New organizations")
        r = self.client.post(
            reverse("org_registration", args=[org.pk, "approve"]),
            {"reg-kind": "cloud", "reg-resource_manager": rm.pk, "reg-support_engineer": se.pk},
            follow=True,
        )
        self.assertContains(r, "needs its sales rep")
        self.client.post(
            reverse("org_registration", args=[org.pk, "approve"]),
            {
                "reg-kind": "cloud",
                "reg-resource_manager": rm.pk,
                "reg-support_engineer": se.pk,
                "reg-sales_rep": rep.pk,
            },
        )
        org.refresh_from_db()
        self.assertEqual((org.registration, org.sales_rep, org.support_engineer), ("pending", rep, se))
        self.assertEqual(mail.outbox[-1].to, ["rep@corp.kz"])
        self.client.force_login(rep)
        self.client.post(reverse("org_registration", args=[org.pk, "approve"]))
        org.refresh_from_db()
        self.assertEqual(org.registration, "approved")
        self.assertEqual(mail.outbox[-1].to, ["owner@romashka.kz"])
        self.assertIn("order resources", mail.outbox[-1].body)
        self.client.logout()
        self.client.post(reverse("login"), {"username": user.email, "password": PROFILE["password1"]})
        self.assertIn("_auth_user_id", self.client.session)
        # correction 11: two-factor authentication first, for every user
        self.assertRedirects(
            self.client.get(reverse("dashboard")), reverse("totp_setup"), fetch_redirect_response=False
        )
        user.totp_enabled = True
        user.save()
        self.assertContains(self.client.get(reverse("dashboard")), "Order a project")
        self.assertContains(self.client.get(reverse("my_org")), "Order a project")

    def test_corporate_email_domain_makes_a_corporate_organization(self):
        PortalSettings.objects.update_or_create(pk=1, defaults={"corporate_domains": "corp.example"})
        self.register("it@cloud.corp.example", valid_bin(), org_name="Example Cloud")
        self.client.get(link_path(mail.outbox[-1]))
        self.assertEqual(Organization.objects.get().kind, "corp")
        self.client.logout()
        self.register("me@gmail.com", valid_bin("0105400013"), org_name="Gmail Co")
        self.client.get(link_path(mail.outbox[-1]))
        self.assertEqual(Organization.objects.get(name="Gmail Co").kind, "cloud")

    def test_staff_invite_a_corporate_organization(self):
        rm = self.staff("rm@corp.kz", is_resource_manager=True)
        rep = self.staff("rep@corp.kz", is_sales=True)
        self.client.force_login(rm)
        self.client.post(reverse("org_invite"), {"email": "boss@mail.kz", "org_kind": "corp", "sales_rep": rep.pk})
        link = link_path(mail.outbox[-1])
        self.assertIn("corporate", mail.outbox[-1].body)
        self.client.logout()
        r = self.client.get(link)
        self.assertRedirects(
            r, reverse("register") + "?invite=" + link.rstrip("/").rsplit("/", 1)[-1], fetch_redirect_response=False
        )
        r = self.client.get(r["Location"])
        self.assertContains(r, "boss@mail.kz")
        token = link.rstrip("/").rsplit("/", 1)[-1]
        self.client.post(
            reverse("register"),
            {
                **PROFILE,
                "invite": token,
                "email": "someone@else.kz",
                "photo": photo(),
                "organization_name": "Boss LLP",
                "tax_id_type": "bin",
                "tax_id": valid_bin(),
            },
        )
        boss = User.objects.get(email="boss@mail.kz")  # the invited email wins
        org = Organization.objects.get(name="Boss LLP")
        self.assertEqual(
            (org.kind, org.sales_rep, boss.organization, boss.is_org_admin, boss.is_active),
            ("corp", rep, org, True, True),
        )  # corrections-18: a corporate one may keep its sales rep
        self.client.logout()
        self.assertEqual(self.client.get(reverse("register") + "?invite=" + token).status_code, 400)  # used

    def test_known_tax_id_becomes_join_request_to_org_admins(self):
        tax = valid_bin()
        self.register("owner@romashka.kz", tax)
        self.client.get(link_path(mail.outbox[-1]))
        self.client.logout()
        mail.outbox.clear()
        self.register("intruder@mail.kz", tax)
        self.client.get(link_path(mail.outbox[-1]))
        self.assertEqual(Organization.objects.count(), 1)
        jr = JoinRequest.objects.get()
        self.assertIsNone(jr.user.organization)
        self.assertIn("owner@romashka.kz", {e for m in mail.outbox for e in m.to})
        self.client.force_login(User.objects.get(email="owner@romashka.kz"))
        r = self.client.post(reverse("decide_join", args=[jr.pk, "accept"]))
        jr.refresh_from_db()
        self.assertEqual(jr.state, "accepted")
        self.assertEqual(jr.user.organization, jr.organization)
        self.assertRedirects(r, reverse("person", args=[jr.user.pk]), fetch_redirect_response=False)

    def test_staff_email_cannot_register(self):
        self.sales_entry("rep@corp.kz")
        r = self.register("rep@corp.kz", valid_bin())
        self.assertContains(r, "already exists")
        self.assertTrue(User.objects.get(email="rep@corp.kz").is_sales)  # not deleted


def make_inventory():
    """One site with a shared cluster and address pools."""
    from inventory.models import Cluster, DelegatedSubnet, Platform, Segment, Site

    site = Site.objects.create(
        code="dc1", management={"ports": {"vyos": 55522, "ssh": 55523}}
    )  # an installation's own ports
    plat = Platform.objects.create(
        key="dc1.cloud", site=site, name="cloud", kind="shared", segments=["cloud-ix", "corpnet-ix"]
    )
    Cluster.objects.create(platform=plat, name="shared-std-1_dc1", kind="shared", quota_code="ca1")
    Platform.objects.create(key="dc1.portal", site=site, name="portal", kind="ext-managed", segments=["portal-ix"])
    # The portal's exchange network comes first by address but belongs to another front end.
    DelegatedSubnet.objects.create(site=site, segment="portal-ix", cidr="198.51.100.0/26", purpose="inet")
    DelegatedSubnet.objects.create(site=site, segment="cloud-ix", cidr="203.0.113.0/25", purpose="inet")
    Segment.objects.create(site=site, name="cloud-services", vlan=103, cidr="203.0.113.0/29")
    DelegatedSubnet.objects.create(site=site, segment="corpnet-ix", cidr="10.10.0.0/22", purpose="corpnet")


INET_ORDER = {
    "cpu": 4,
    "ram_gb": 16,
    "disk_gb": 100,
    "connections": ["inet"],
    "inet_mbps": 100,
    "inet_main_prefix": 31,
    "inet_extra_prefixes": [29],
}
CORP_ORDER = {"cpu": 8, "ram_gb": 32, "disk_gb": 200, "connections": ["corpnet"], "corpnet_prefixes": [28, 29]}


class Approvals(Base):
    def setUp(self):
        make_inventory()
        self.rm = self.staff("rm@corp.kz", is_resource_manager=True)
        self.sup = self.staff("sup@corp.kz", is_support=True)
        self.rep = self.sales_entry()
        self.org = Organization.objects.create(name="ТОО «Romashka»", tax_id=valid_bin(), kind="corp")

    def tenant(self, kind, order=None):
        return Tenant.objects.create(
            organization=self.org,
            title="P",
            kind=kind,
            order=dict(order or (CORP_ORDER if kind == "corp" else INET_ORDER)),
        )

    def rm_approve(self, t, name, kind=None, **extra):
        data = {
            "rm-name": name,
            "rm-kind": kind or t.kind,
            "rm-placement": "dc1.cloud/ca1",
            "rm-cpu": t.order.get("cpu", 4),
            "rm-ram_gb": t.order.get("ram_gb", 16),
            "rm-disk_gb": t.order.get("disk_gb", 100),
        }
        data.update({f"rm-{k}": v for k, v in extra.items()})
        return self.client.post(reverse("tenant_rm_approve", args=[t.pk]), data)

    def cloud_org(self):
        self.org.kind = "cloud"
        self.org.save()

    def test_cloud_rm_alone_allocates_and_materializes(self):
        self.cloud_org()
        t = self.tenant("cloud")
        self.client.force_login(self.rm)
        self.rm_approve(t, "romashka", sales_rep=self.rep.pk, cpu=6)
        t.refresh_from_db()
        self.org.refresh_from_db()
        self.assertEqual(t.approval, Approval.APPROVED)
        self.assertEqual(t.stage, "poc")
        self.assertEqual(self.org.sales_rep, self.rep)
        self.assertEqual((self.org.short_name, t.group_name), ("romashka", "_romashka_romashka"))
        cidrs = {p.kind: p.cidr for p in t.prefixes.all()}
        self.assertEqual(cidrs, {"inet_extra": "203.0.113.8/29", "inet_main": "203.0.113.16/31"})
        self.assertEqual((t.order["platform"], t.order["frontend"]), ("dc1.cloud", "dc1.cloud"))
        rec = t.current_quota_records()[0]
        self.assertEqual((rec.site, rec.cluster, rec.cpu, rec.ram_gb, rec.disk_gb), ("dc1", "ca1", 6, 16, 100))
        conn = t.current_connectivity()[0]
        self.assertEqual(
            (conn.connection_type, conn.inet_mbps, conn.inet_main_prefix, conn.inet_public_prefixes),
            ("inet", 100, 31, "29"),
        )

    def test_allocation_skips_taken_addresses(self):
        from tenants.models import PrefixAssignment
        from tenants.ordering import ensure_service_reservations

        PrefixAssignment.objects.create(cidr="203.0.113.4/32", site="dc1", kind="reserved", note="portal VM")
        PrefixAssignment.objects.create(cidr="203.0.113.8/29", site="dc1", kind="reserved")
        PrefixAssignment.objects.create(cidr="203.0.113.16/28", site="dc1", kind="services")  # stale
        self.assertEqual(ensure_service_reservations(), 1)
        self.assertEqual(ensure_service_reservations(), 0)
        svc = PrefixAssignment.active().get(kind="services")
        self.assertEqual(svc.cidr, "203.0.113.0/29")
        self.assertIn("cloud-services", svc.note)
        self.assertIn("portal VM", svc.note)
        self.assertFalse(PrefixAssignment.active().filter(cidr="203.0.113.4/32").exists())
        t = self.tenant("cloud", {**INET_ORDER, "inet_extra_prefixes": []})
        self.client.force_login(self.rm)
        self.rm_approve(t, "second")
        self.assertEqual(t.prefixes.get().cidr, "203.0.113.16/31")

    def test_rm_approves_without_sales_rep_when_switch_off(self):
        t = self.tenant("cloud")
        self.client.force_login(self.rm)
        self.rm_approve(t, "norep")
        t.refresh_from_db()
        self.assertEqual(t.approval, Approval.APPROVED)
        self.assertIsNone(t.sales_rep)

    def test_sales_on_without_any_rep_asks_a_super_admin(self):
        """Owner 2026-11-12: a ticked role is always asked - its own person, else anyone with it, else a super admin."""
        PortalSettings.objects.update_or_create(pk=1, defaults={"sales_approval_required": True})
        self.rep.delete()
        sa = self.staff("sa7@corp.kz", is_super_admin=True)
        t = self.tenant("cloud")
        self.client.force_login(self.rm)
        self.rm_approve(t, "lonely")
        t.refresh_from_db()
        self.assertEqual(t.approval, Approval.PENDING)
        self.assertEqual(t.approvers(Role.SALES_MANAGER), [sa])

    def test_sales_on_asks_any_rep_when_the_organization_has_none(self):
        PortalSettings.objects.update_or_create(pk=1, defaults={"sales_approval_required": True})
        rep = self.staff("rep5@corp.kz", is_sales=True)
        t = self.tenant("cloud")
        self.client.force_login(self.rm)
        self.rm_approve(t, "pick")
        t.refresh_from_db()
        self.assertIsNotNone(t.rm_approved_at)  # the rep is not demanded any more
        self.assertEqual((t.approval, t.approvers(Role.SALES_MANAGER)), (Approval.PENDING, [rep]))
        t = self.tenant("corp")
        self.client.force_login(self.rm)
        self.rm_approve(t, "corp0")
        t.refresh_from_db()
        self.assertEqual(t.approval, Approval.APPROVED)
        self.assertEqual(sorted(p.cidr for p in t.prefixes.all()), ["10.10.0.0/28", "10.10.0.16/29"])

    def test_support_on_without_engineers_asks_a_super_admin(self):
        PortalSettings.objects.update_or_create(pk=1, defaults={"support_approval_required": True})
        self.sup.delete()
        sa = self.staff("sa8@corp.kz", is_super_admin=True)
        t = self.tenant("corp")
        self.client.force_login(self.rm)
        self.rm_approve(t, "corp2")
        t.refresh_from_db()
        self.assertEqual((t.approval, t.approvers(Role.SUPPORT)), (Approval.PENDING, [sa]))

    def test_settings_table_shows_who_approves(self):
        """Owner 2026-11-12: the Approvers column lists who holds the role; a box can be ticked only while somebody
        active holds it."""
        sa = self.staff("sa2@corp.kz", is_super_admin=True)
        self.client.force_login(sa)
        r = self.client.get(reverse("portal_settings") + "?tab=platform")
        self.assertContains(r, 'name="sales_approval_required"  disabled')  # the rep is a directory entry
        self.assertContains(r, "sup@corp.kz")
        self.assertNotContains(r, "Approves now")
        self.client.post(
            reverse("portal_settings"),
            {
                "section": "approvals",
                "rm_approval_required": "on",
                "support_approval_required": "on",
                "sales_approval_required": "on",
            },
        )
        conf = PortalSettings.get()
        self.assertEqual((conf.support_approval_required, conf.sales_approval_required), (False, False))  # refused
        self.client.post(
            reverse("portal_settings"),
            {"section": "approvals", "rm_approval_required": "on", "support_approval_required": "on"},
        )
        self.assertTrue(PortalSettings.get().support_approval_required)

    def test_corp_needs_rm_and_support(self):
        """Owner 2026-11-12: with the support engineer's box ticked, a corporate project waits for its approval."""
        PortalSettings.objects.update_or_create(pk=1, defaults={"support_approval_required": True})
        t = self.tenant("corp")
        self.client.force_login(self.rm)
        self.rm_approve(t, "corp1")
        t.refresh_from_db()
        self.assertEqual(t.approval, Approval.PENDING)
        self.client.force_login(self.sup)
        self.client.post(reverse("tenant_support_approve", args=[t.pk]))
        t.refresh_from_db()
        self.assertEqual(t.approval, Approval.APPROVED)

    def test_assigned_support_engineer_approves_corp(self):
        PortalSettings.objects.update_or_create(pk=1, defaults={"support_approval_required": True})
        other = self.staff("sup2@corp.kz", is_support=True)
        self.org.support_engineer = other
        self.org.save()
        t = self.tenant("corp")
        self.client.force_login(self.rm)
        self.rm_approve(t, "corp3")
        t.refresh_from_db()
        self.assertEqual(t.approvers(Role.SUPPORT), [other])
        self.client.force_login(self.sup)  # not the organization's engineer
        self.assertEqual(self.client.post(reverse("tenant_support_approve", args=[t.pk])).status_code, 403)
        self.client.force_login(other)
        self.client.post(reverse("tenant_support_approve", args=[t.pk]))
        t.refresh_from_db()
        self.assertEqual(t.approval, Approval.APPROVED)

    def test_support_engineer_and_super_admin_as_corp_tenant_users(self):
        from tenants.models import Membership

        t = self.tenant("corp")
        cloud = self.tenant("cloud")
        sa = self.staff("sa3@corp.kz", is_super_admin=True)
        self.sup.may_tenant_admin = True  # owner 2026-11-03: eligible to be a project admin
        self.sup.save()
        self.client.force_login(self.rm)
        self.client.post(reverse("tenant_staff_member", args=[t.pk]), {"user": self.sup.pk, "role": "tenant_admin"})
        self.client.post(reverse("tenant_staff_member", args=[t.pk]), {"user": sa.pk})
        self.assertEqual(t.membership_role(self.sup), Membership.Role.TENANT_ADMIN)
        self.assertEqual(t.membership_role(sa), Membership.Role.TENANT_USER)
        self.assertEqual(
            self.client.post(reverse("tenant_staff_member", args=[cloud.pk]), {"user": self.sup.pk}).status_code, 403
        )
        self.client.post(reverse("tenant_staff_member", args=[t.pk]), {"user": sa.pk, "remove": "1"})
        self.assertIsNone(t.membership_role(sa))

    def test_corp_tenant_resource_manager_approves(self):
        other = self.staff("rm2@corp.kz", is_resource_manager=True)
        t = self.tenant("corp")
        t.resource_manager = other
        t.save()
        self.client.force_login(self.rm)
        self.assertEqual(self.rm_approve(t, "corp4").status_code, 403)
        self.client.force_login(other)
        self.rm_approve(t, "corp4")
        t.refresh_from_db()
        self.assertEqual(t.approval, Approval.APPROVED)

    def test_corp_org_has_no_kam_and_an_optional_sales_rep(self):
        kam = self.staff("kam@corp.kz", is_kam=True)
        self.org.kind = "corp"
        self.org.sales_rep, self.org.kam = self.rep, kam
        self.org.save()
        self.org.refresh_from_db()
        self.assertEqual((self.org.sales_rep, self.org.kam), (self.rep, None))  # corrections-18

    def test_super_admin_approves_in_the_roles_they_hold(self):
        sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.org.sales_rep = self.rep
        self.org.save()
        t = self.tenant("cloud")
        self.client.force_login(sa)
        # correction 16: not asked as resource manager (not ticked), but a super admin has every right
        self.assertFalse(t.asks(sa, Role.RESOURCE_MANAGER))
        self.assertTrue(t.can_approve_as(sa, Role.RESOURCE_MANAGER))
        sa.is_resource_manager = True
        sa.save()
        self.assertTrue(t.asks(sa, Role.RESOURCE_MANAGER) or t.effective_resource_manager is not None)
        self.rm_approve(t, "poc1")
        t.refresh_from_db()
        self.assertEqual(t.approval, Approval.APPROVED)

    def test_one_person_in_several_roles_approves_once(self):
        PortalSettings.objects.update_or_create(pk=1, defaults={"support_approval_required": True})
        both = self.staff("both@corp.kz", is_resource_manager=True, is_support=True)
        t = self.tenant("corp")
        self.client.force_login(both)
        self.rm_approve(t, "once")
        t.refresh_from_db()
        self.assertEqual((t.approval, t.support_approved_by), (Approval.APPROVED, both))

    def _pending_after_rm(self):
        """A cloud tenant that still waits for its sales rep after the resource manager (correction 13: no support step)."""
        PortalSettings.objects.update_or_create(pk=1, defaults={"sales_approval_required": True})
        self.cloud_org()
        self.org.sales_rep = self.staff("rep9@corp.kz", is_sales=True)
        self.org.save()
        return self.tenant("cloud")

    def test_reject_releases_prefixes_and_request_can_be_deleted(self):
        t = self._pending_after_rm()
        self.client.force_login(self.rm)
        self.rm_approve(t, "gone")
        self.assertEqual(t.prefixes.filter(released_at__isnull=True).count(), 2)
        self.client.post(reverse("tenant_reject", args=[t.pk]), {"rej-reason": "no capacity"})
        self.assertEqual(t.prefixes.filter(released_at__isnull=True).count(), 0)
        self.client.post(reverse("tenant_delete", args=[t.pk]))
        t.refresh_from_db()
        self.assertEqual(t.status, "archived")  # requests are archived, never deleted

    def test_group_naming_rules(self):
        from adoption.models import Observation

        Observation.objects.create(
            platform_key="dc1.cloud",
            group_id=7,
            group_name="_romashka_taken",
            template={},
            quotas={},
            users=[],
            collected_at=timezone.now(),
        )
        t = self.tenant("cloud")
        self.client.force_login(self.rm)
        for bad, _why in (
            ("1abc", "starting with a letter"),
            ("abc", "4-15"),
            ("with--dash", "4-15"),
            ("dash-", "4-15"),
            ("sixteenletters12", "4-15"),
            ("taken", "already exists"),
        ):
            self.rm_approve(t, bad, sales_rep=self.rep.pk)
            t.refresh_from_db()
            self.assertIsNone(t.rm_approved_at, bad)
        self.rm_approve(t, "fourteenletter")
        t.refresh_from_db()
        self.assertEqual(t.group_name, "_romashka_fourteenletter")  # correction 13
        self.assertEqual(len(t.group_name), 24)
        t2 = self.tenant("cloud")
        self.rm_approve(t2, "fourteenletter")
        t2.refresh_from_db()
        self.assertIsNone(t2.rm_approved_at)  # same short name in the same organization
        t3 = self.tenant("cloud")  # correction 12: "-" allowed; one name with or without it
        self.rm_approve(t3, "fourteen-letter")
        t3.refresh_from_db()
        self.assertIsNone(t3.rm_approved_at)  # "fourteenletter" without the "-"
        self.rm_approve(t3, "ml-lab")
        t3.refresh_from_db()
        self.assertEqual(t3.group_name, "_romashka_mllab")  # correction 13: no "-" in the group
        from tenants.naming import vyos_names

        self.assertEqual(vyos_names(t3, "egw", "dc1"), ("_romashka_mllab_egw_dc1", "egw-romashka-mllab-dc1"))

    def test_correction_12_organization_rules(self):
        from django.utils import timezone

        from tenants.forms import OrganizationForm, TenantMoveForm

        f = OrganizationForm()
        self.assertNotIn("default_ssh_key", f.fields)  # keys: the organization's portal
        self.assertEqual((f.initial.get("kind"), f.initial.get("tax_id_type")), ("cloud", "bin"))
        f = OrganizationForm(data={"name": "", "kind": "", "tax_id_type": "", "tax_id": ""})
        self.assertFalse(f.is_valid())
        self.assertTrue({"name", "kind", "tax_id"} <= set(f.errors))
        gone = Organization.objects.create(name="Old", archived_at=timezone.now())
        other = Organization.objects.create(name="Other", tax_id=valid_bin("0205400012"))
        t = self.tenant("cloud")
        ids = [v for v, _l in TenantMoveForm(tenant=t).fields["organization"].choices]
        self.assertIn(str(other.pk), ids)
        self.assertNotIn(str(gone.pk), ids)  # archived organizations are not offered
        self.org.short_name = "romashka"
        self.org.save()
        self.assertFalse(self.org.short_name_locked)
        Tenant.objects.filter(pk=t.pk).update(one_group_id=77)  # deployed (group tenant-x)
        self.assertTrue(Organization.objects.get(pk=self.org.pk).short_name_locked)
        self.client.force_login(self.rm)
        page = self.client.get(reverse("tenant_list")).content.decode()
        heads = [h for h in ("Organization", "Project", "Type", "Stage", "Status", "Review") if f">{h}<" in page]
        self.assertEqual(
            heads, ["Organization", "Project", "Type", "Stage", "Status", "Review"]
        )  # u01: Review, not Created
        self.assertLess(page.index(">Organization<"), page.index(">Project<"))
        self.assertLess(page.index(">Type<"), page.index(">Stage<"))
        self.assertLess(page.index(">Stage<"), page.index(">Status<"))  # u05 (r231): Status after Stage
        self.assertNotIn(">Platform<", page)

    def test_rejected_request_gives_the_group_name_back(self):
        t = self._pending_after_rm()
        self.client.force_login(self.rm)
        self.rm_approve(t, "labs")
        t.refresh_from_db()
        self.assertEqual(t.group_name, "_romashka_labs")
        self.client.post(reverse("tenant_reject", args=[t.pk]), {"rej-reason": "no"})
        t.refresh_from_db()
        self.assertIsNone(t.group_name)
        t2 = self.tenant("cloud")
        self.rm_approve(t2, "labs")
        t2.refresh_from_db()
        self.assertEqual(t2.group_name, "_romashka_labs")

    def test_stage_changed_by_staff(self):
        t = self.tenant("cloud")
        self.client.force_login(self.rm)
        self.rm_approve(t, "stagey")
        self.client.post(reverse("tenant_stage", args=[t.pk]), {"st-stage": "client"})
        t.refresh_from_db()
        self.assertEqual(t.stage, "client")

    def test_approval_requests_page(self):
        t = self.tenant("corp")
        self.client.force_login(self.rm)
        r = self.client.get(reverse("approvals"))
        self.assertTrue(r.context["items"][0]["mine"])
        self.assertContains(r, ">Review<")  # the RM step needs the form on the tenant page
        self.assertContains(r, "Approval requests (1)")  # menu badge
        self.rm_approve(t, "corpq")
        r = self.client.get(reverse("approvals") + "?mine=1")
        self.assertEqual(r.context["items"], [])  # correction 13: approved; Provision is not the RM's
        self.client.force_login(self.sup)
        r = self.client.get(reverse("approvals") + "?mine=1")
        self.assertEqual([i["kind"] for i in r.context["items"]], ["provision"])  # the support engineer provisions
        self.assertContains(r, reverse("tenant_detail", args=[t.pk]) + "?provision=1")
        self.assertContains(self.client.get(reverse("approvals") + "?tab=approved"), "_romashka_corpq")

    def test_rm_approval_off_approves_as_ordered(self):
        """Owner 2026-11-12: the resource manager's box cleared - no request; the order is approved as ordered."""
        from tenants import autoapprove

        PortalSettings.objects.update_or_create(pk=1, defaults={"rm_approval_required": False})
        t = self.tenant("corp", {**CORP_ORDER, "platform": "dc1.cloud", "cluster": "ca1"})
        t.name = "corp9"
        t.save()
        self.assertEqual(t.required_approvals(), [])
        with mock.patch("tenants.provision.auto", return_value=(None, "")):
            self.assertEqual(autoapprove.run(t), "approved")
        t.refresh_from_db()
        self.assertEqual(t.approval, Approval.APPROVED)
        self.assertTrue(t.current_quota_records())

    def test_rm_approval_off_failure_comes_back_for_a_super_admin(self):
        from tenants import autoapprove

        PortalSettings.objects.update_or_create(pk=1, defaults={"rm_approval_required": False})
        sa = self.staff("sa4@corp.kz", is_super_admin=True)
        t = self.tenant("corp")  # no name and no placement: the form cannot be filled
        why = autoapprove.run(t)
        t.refresh_from_db()
        self.assertNotIn(why, ("approved", "waiting"))
        self.assertEqual((t.approval, t.required_approvals()), (Approval.PENDING, [Role.RESOURCE_MANAGER]))
        self.assertEqual(t.approvers(Role.RESOURCE_MANAGER), [sa])
        self.client.force_login(self.rm)
        self.assertEqual(self.rm_approve(t, "corp9").status_code, 403)
        self.client.force_login(sa)
        self.rm_approve(t, "corp9")
        t.refresh_from_db()
        self.assertEqual(t.approval, Approval.APPROVED)
        self.assertNotIn("auto_rm_failed", t.order)

    def test_a_cleared_box_asks_nobody(self):
        self.cloud_org()
        PortalSettings.objects.update_or_create(pk=1, defaults={"sales_approval_required": False})
        self.org.sales_rep = self.staff("rep6@corp.kz", is_sales=True)
        self.org.save()
        self.assertEqual(self.tenant("cloud").required_approvals(), [Role.RESOURCE_MANAGER])

    def test_permissions(self):
        self.cloud_org()
        t = self.tenant("cloud")
        rep = self.staff("rep3@corp.kz", is_sales=True)
        self.client.force_login(rep)
        self.assertEqual(self.client.get(reverse("tenant_detail", args=[t.pk])).status_code, 403)
        self.org.sales_rep = rep
        self.org.save()
        self.assertEqual(self.client.get(reverse("tenant_detail", args=[t.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("inventory")).status_code, 403)
        self.assertEqual(
            self.client.post(reverse("tenant_stage", args=[t.pk]), {"st-stage": "client"}).status_code, 302
        )


class Dashboard(Base):
    def test_staff_dashboard_with_stats_and_side_menu(self):
        from django.utils import timezone as tz

        from adoption.models import PlatformStats

        make_inventory()
        sa = self.staff("sa@corp.kz", is_super_admin=True)
        PlatformStats.objects.create(
            platform_key="dc1.cloud",
            collected_at=tz.now(),
            vms={"total": 9, "running": 6, "tenant_total": 7, "tenant_running": 5},
            hosts={"total": 3, "on": 2, "error": 1},
        )
        self.client.force_login(sa)
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, 'class="sidenav"')
        self.assertContains(r, "Clusters")
        self.assertContains(r, "6 <small>/ 9</small>", html=False)
        self.assertContains(r, "203.0.113.0/25")
        self.assertNotContains(r, "198.51.100.0/26")  # portal-ix is not a tenant pool
        self.assertContains(r, 'aria-current="page"')

    def test_documentation_is_on_the_public_site(self):
        sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(sa)
        r = self.client.get(reverse("docs"))  # old links lead to the public reference
        self.assertRedirects(r, "https://docs.claugine.com/reference/portal/", fetch_redirect_response=False)
        self.assertRedirects(
            self.client.get(reverse("docs_data")),
            "https://docs.claugine.com/reference/data-model/",
            fetch_redirect_response=False,
        )
        self.assertRedirects(
            self.client.get(reverse("docs_network")),
            "https://docs.claugine.com/reference/network/",
            fetch_redirect_response=False,
        )
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, 'href="https://docs.claugine.com/" target="_blank"')  # Help → Documentation
        self.assertNotContains(r, 'href="/docs/"')

    def test_customer_has_short_menu(self):
        org = Organization.objects.create(name="Org", tax_id=valid_bin())
        u = User.objects.create_user(
            "a@org.kz", "x", kind=Kind.CUSTOMER, organization=org, is_org_admin=True, email_verified=True
        )
        self.client.force_login(u)
        r = self.client.get(reverse("my_org"))
        self.assertContains(r, 'class="sidenav"')
        self.assertNotContains(r, f'href="{reverse("tenant_list")}"')


class ResourceEditing(Base):
    def test_rm_edits_adds_and_removes_records(self):
        from tenants.models import QuotaRecord

        make_inventory()
        rm = self.staff("rm@corp.kz", is_resource_manager=True)
        rep = self.staff("rep@corp.kz", is_sales=True)
        org = Organization.objects.create(name="Org", tax_id=valid_bin())
        t = Tenant.objects.create(
            organization=org,
            name="lab",
            group_name="_org-lab",
            approval=Approval.APPROVED,
            order={"frontend": "dc1.cloud"},
        )
        QuotaRecord.objects.create(tenant=t, site="dc1", cluster="ca1", version=1, cpu=4, ram_gb=16, disk_gb=100)
        url = reverse("tenant_resources", args=[t.pk])
        org.sales_rep = rep
        org.save()
        self.client.force_login(rep)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(rm)
        self.assertContains(self.client.get(url), "dc1/ca1")
        data = {
            "r-TOTAL_FORMS": "2",
            "r-INITIAL_FORMS": "1",
            "r-MIN_NUM_FORMS": "0",
            "r-MAX_NUM_FORMS": "1000",
            "r-0-key": "dc1/ca1",
            "r-0-cpu": "8",
            "r-0-ram_gb": "16",
            "r-0-gpu": "",
            "r-0-disk_gb": "200",
            "r-0-backups_gb": "0",
        }
        self.client.post(url, data)
        r = t.current_quota_records()[0]
        self.assertEqual(
            (r.version, r.cpu, r.disk_gb, r.split), (2, 8, 200, (200, 0, 0))
        )  # no images or files unless asked
        self.client.post(url, {**data, "r-0-remove": "on"})
        self.assertEqual(t.current_quota_records(), [])


class OrganizationDeletion(Base):
    def test_archived_never_deleted_and_back(self):
        rm = self.staff("rm@corp.kz", is_resource_manager=True)
        rep = self.staff("rep@corp.kz", is_sales=True)
        empty = Organization.objects.create(name="Empty", tax_id=valid_bin())
        used = Organization.objects.create(name="Used")
        Tenant.objects.create(organization=used, title="P", approval=Approval.APPROVED)
        self.client.force_login(rep)
        self.assertEqual(self.client.post(reverse("org_delete", args=[empty.pk])).status_code, 403)
        self.client.force_login(rm)
        self.assertContains(self.client.get(reverse("org_detail", args=[empty.pk])), "Archive")
        self.client.post(reverse("org_delete", args=[used.pk]))
        used.refresh_from_db()
        self.assertIsNone(used.archived_at)  # a tenant that is not archived
        self.client.post(reverse("org_delete", args=[empty.pk]))
        empty.refresh_from_db()
        self.assertIsNotNone(empty.archived_at)
        self.assertNotContains(self.client.get(reverse("org_list")), f'data-id="o-{empty.pk}"')
        self.assertContains(self.client.get(reverse("org_list") + "?archived=1"), f'data-id="o-{empty.pk}"')
        self.client.post(reverse("org_unarchive", args=[empty.pk]))
        empty.refresh_from_db()
        self.assertIsNone(empty.archived_at)
        from accounts.models import AuditLog

        self.assertTrue(AuditLog.objects.filter(action="organization_archived", details__name="Empty").exists())


@override_settings(
    FIELD_ENCRYPTION_KEY=__import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
)
class CustomerRoles(Base):
    def setUp(self):
        from inventory.models import Cluster, Host, Platform, ServerProfile, Site

        make_inventory()
        gp = ServerProfile.objects.create(
            code="ga1", cores=64, ram_gb=1536, gpu_model="L40S", gpu_count=4, gpu_vram_gb=48
        )
        # spare_hosts=0 is what both creation paths give a GPU cluster (inventory.ops.cluster_add and the loader),
        # and what the owner confirms real ones carry. Built directly here, it would otherwise inherit the model's
        # default of 1, hold back its only host and offer no bundles at all (owner 2026-11-09).
        gc = Cluster.objects.create(
            platform=Platform.objects.get(key="dc1.cloud"),
            name="shared-gpu-1_dc1",
            kind="shared",
            quota_code="ga1",
            spare_hosts=0,
        )
        gc.hosts.add(Host.objects.create(site=Site.objects.get(code="dc1"), name="g1", profile=gp))
        self.org = Organization.objects.create(name="Org", tax_id=valid_bin())
        self.a = Tenant.objects.create(organization=self.org, name="aaa", title="A", approval=Approval.APPROVED)
        self.b = Tenant.objects.create(organization=self.org, name="bbb", title="B", approval=Approval.APPROVED)
        self.admin = User.objects.create_user(
            "admin@org.kz",
            "Str0ng-PASS-123",
            kind=Kind.CUSTOMER,
            organization=self.org,
            is_org_admin=True,
            email_verified=True,
        )

    def test_staff_manager_can_edit_org_user_roles(self):
        # doc-gaps §4 part 2b: a manager/RM edits an existing org user's roles from the org page
        rm = self.staff("rm5@corp.kz", is_resource_manager=True)
        member = User.objects.create_user(
            "member2@org.kz",
            "Str0ng-PASS-123",
            kind=Kind.CUSTOMER,
            organization=self.org,
            email_verified=True,
            login_enabled=True,
        )
        self.client.force_login(rm)
        html = self.client.get(reverse("org_detail", args=[self.org.pk])).content.decode()
        self.assertIn(f"dlg-roles-{member.pk}", html)  # the roles dialog renders for the member
        self.client.post(reverse("person_roles", args=[member.pk]), {"tenants": [self.a.pk]})
        self.assertEqual(self.a.membership_role(member), "tenant_admin")

    def test_manager_has_full_resource_management(self):
        # doc-gaps §1: manager/KAM/RM can change figures (resource_request) and make network requests (can_request)
        from tenants.network_views import can_request

        rm = self.staff("rm4@corp.kz", is_resource_manager=True)
        self.assertTrue(can_request(self.a, rm))
        self.client.force_login(rm)
        r = self.client.get(reverse("tenant_resource_request", args=[self.a.pk]))
        self.assertNotEqual(r.status_code, 403)  # past the permission gate (302 = no resources yet, not denied)

    def test_staff_manager_sees_lock_and_can_block_on_org_page(self):
        # doc-gaps §4 part 2: Lock/Unlock on the organization page for manager/KAM/RM
        rm = self.staff("rm3@corp.kz", is_resource_manager=True)
        member = User.objects.create_user(
            "member@org.kz",
            "Str0ng-PASS-123",
            kind=Kind.CUSTOMER,
            organization=self.org,
            email_verified=True,
            login_enabled=True,
        )
        self.client.force_login(rm)
        html = self.client.get(reverse("org_detail", args=[self.org.pk])).content.decode()
        self.assertIn("Lock", html)
        self.client.post(reverse("toggle_user_active", args=[member.pk]))
        member.refresh_from_db()
        self.assertFalse(member.is_active)  # blocked by the resource manager

    def test_staff_manager_can_invite_org_users(self):
        # doc-gaps §4: a manager/KAM/RM (here a resource manager) can invite users, not only super admin / org admin
        from django.core import mail

        rm = self.staff("rm2@corp.kz", is_resource_manager=True)
        self.client.force_login(rm)
        mail.outbox = []
        self.client.post(
            reverse("customer_invite"), {"organization": self.org.pk, "email": "invitee@org.kz", "read_only": "on"}
        )
        self.assertIn("invitee@org.kz", " ".join(m.to[0] for m in mail.outbox))
        outsider = User.objects.create_user("x@other.kz", "Str0ng-PASS-123", kind=Kind.CUSTOMER, email_verified=True)
        self.client.force_login(outsider)
        r = self.client.post(
            reverse("customer_invite"), {"organization": self.org.pk, "email": "z@org.kz", "read_only": "on"}
        )
        self.assertEqual(r.status_code, 403)

    def test_invite_with_tenant_roles(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("customer_invite"), {"email": "dev@org.kz", "tenants": [self.a.pk]})
        self.client.post(reverse("customer_invite"), {"email": "ro@org.kz", "read_only": "on"})
        self.assertContains(self.client.get(reverse("org_users")), "ro@org.kz")  # invited, no account yet
        links = {m.to[0]: link_path(m) for m in mail.outbox}
        self.client.logout()
        self.client.post(links["dev@org.kz"], {**PROFILE, "photo": photo()})
        dev = User.objects.get(email="dev@org.kz")
        self.assertEqual(dev.organization, self.org)
        self.assertEqual(self.a.membership_role(dev), "tenant_admin")
        self.assertIsNone(self.b.membership_role(dev))  # a tenant admin sees only their tenants
        self.client.logout()
        self.client.post(links["ro@org.kz"], {**PROFILE, "email": "ro@org.kz", "photo": photo()})
        ro = User.objects.get(email="ro@org.kz")
        self.assertTrue(ro.is_org_readonly)
        self.assertEqual((self.a.membership_role(ro), self.b.membership_role(ro)), ("tenant_user", "tenant_user"))

    def test_tenant_user_sees_only_own_tenant(self):
        u = User.objects.create_user(
            "u@org.kz", "Str0ng-PASS-123", kind=Kind.CUSTOMER, organization=self.org, email_verified=True
        )
        Membership.objects.create(user=u, tenant=self.a, role="tenant_user")
        self.client.force_login(u)
        self.assertEqual(self.client.get(reverse("tenant_detail", args=[self.a.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("tenant_detail", args=[self.b.pk])).status_code, 403)

    def test_org_admin_area(self):
        from accounts.models import audit

        u = User.objects.create_user(
            "u@org.kz", "Str0ng-PASS-123", kind=Kind.CUSTOMER, organization=self.org, email_verified=True
        )
        Membership.objects.create(user=u, tenant=self.a, role="tenant_user")
        audit(self.admin, "tenant_note", self.a, note="hello")
        audit(
            self.admin,
            "other_org_event",
            Tenant.objects.create(
                organization=Organization.objects.create(name="Other", tax_id=valid_bin("0105400099")),
                name="zzzz",
                title="Z",
            ),
        )
        self.client.force_login(self.admin)
        r = self.client.get(reverse("dashboard"))
        for text in ("Users", "Logs", "Tasks", "Virtual machines"):
            self.assertContains(r, text)
        self.assertNotContains(r, "tenant_note")  # activity is seen only in Logs and Tasks
        self.assertContains(self.client.get(reverse("org_users")), "u@org.kz")
        r = self.client.get(reverse("org_logs"))
        self.assertContains(r, "tenant_note")
        self.assertNotContains(r, "other_org_event")
        self.assertContains(self.client.get(reverse("org_logs") + f"?tenant={self.b.pk}"), "Nothing logged.")
        self.assertEqual(self.client.get(reverse("org_tasks")).status_code, 200)
        self.client.force_login(u)
        self.assertEqual(self.client.get(reverse("org_users")).status_code, 403)
        self.assertEqual(self.client.get(reverse("org_logs")).status_code, 403)
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, "aaa")
        self.assertNotContains(r, "bbb")

    def order(self, **extra):
        from datetime import date, timedelta

        data = {
            "name": "gpuresearch",
            "site": "dc1",
            "pool": "dc1.cloud/ca1",
            "cpu": 4,
            "ram_gb": 16,
            "disk_gb": 100,
            "stage": "poc",
            "date_review": (date.today() + timedelta(days=20)).isoformat(),
            "connections": ["inet"],
            "inet_mbps": 100,
            "inet_main_prefix": 31,
            "inet_extra_prefixes": "29 28",
        }
        data.update(extra)
        return self.client.post(reverse("new_tenant"), data)

    def test_igw_types_in_the_order(self):
        """The igw **type** is chosen with the order; its credentials still come at provisioning.

        Correction 13 kept the whole gateway out of the order. The owner reversed that for the type alone on
        2026-11-09: someone ordering the internet is choosing what kind of gateway they want, and the provision
        dialog already read it back from the order. The admin password, the gateway's own user and password and the
        ssh keys stay where correction 13 put them.
        """
        self.client.force_login(self.admin)
        r = self.client.get(reverse("new_tenant"))
        for field in ("admin_password", "igw_username", "igw_password", "igw_ssh_key", "igw_ssh_from"):
            self.assertNotContains(r, f'name="{field}"')
        self.assertContains(r, 'name="igw_type"')
        for value in ("vyos", "onevr", "custom"):  # all three offered, VyOS the default
            self.assertContains(r, f'value="{value}"')
        self.assertContains(r, 'name="date_review"')
        self.order(name="custom1", igw_type="custom", admin_password="Kx7-Pass+2026")
        t = Tenant.objects.get(name="custom1")
        self.assertEqual(t.order["igw_type"], "custom")
        self.order(name="default1")
        self.assertEqual(Tenant.objects.get(name="default1").order["igw_type"], "vyos")
        self.assertEqual((t.admin_password_encrypted, t.igw_username, t.ssh_keys.count()), ("", "", 0))
        self.assertContains(self.order(name="noreview", date_review=""), "This field is required")

    def test_org_admin_orders_tenant(self):
        rm = self.staff("rm@corp.kz", is_resource_manager=True)
        self.client.force_login(self.admin)
        self.assertNotContains(self.client.get(reverse("new_tenant")), 'name="kind"')
        self.order(kind="corp")  # ignored: not a corporate email
        t = Tenant.objects.get(name="gpuresearch")
        self.assertEqual((t.organization, t.approval, t.kind, t.stage), (self.org, Approval.PENDING, "cloud", "poc"))
        self.assertEqual((t.name, t.one_group_name, t.group_name), ("gpuresearch", "_org_gpuresearch", None))
        self.assertEqual(
            (t.igw_username, t.admin_password_encrypted, t.one_admin_name), ("", "", "org-gpuresearch-adm")
        )
        self.assertEqual(t.order["inet_extra_prefixes"], [29, 28])
        self.assertIn(rm.email, mail.outbox[-1].to)
        self.assertIn("100 Mbps", mail.outbox[-1].body)

    def test_order_defaults_and_password_change(self):
        self.org.default_ssh_key = "ssh-ed25519 AAAAorgkey ops@org"
        self.org.save()
        self.client.force_login(self.admin)
        r = self.client.get(reverse("new_tenant"))
        self.assertContains(r, 'value="tenant1"')
        self.assertNotContains(r, "ssh-ed25519 AAAAorgkey ops@org")  # correction 12: no default key filled in
        self.order(name="tenant1")
        t = Tenant.objects.get(name="tenant1")
        self.assertContains(self.client.get(reverse("new_tenant")), 'value="tenant2"')
        url = reverse("tenant_password", args=[t.pk])
        t.set_admin_password("Kx7-Pass+2026")  # as kept at provisioning
        t.save()
        r = self.client.post(url, {"password": "Kx7-Pass+2026", "password2": "Kx7-Pass+2026"})
        self.assertContains(r, "last 5")  # the provisioning's password
        self.client.post(url, {"password": "Zq9-Word+7788", "password2": "Zq9-Word+7788"})
        t.refresh_from_db()
        self.assertEqual(t.get_admin_password(), "Zq9-Word+7788")
        self.assertEqual(t.password_hashes.count(), 2)

    def test_gpu_pool_is_ordered_in_bunches(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse("new_tenant"))
        # owner 2026-11-09 (gpu-bundles): the bundle is the cluster's own setting now, and until staff set one it
        # is the hardware's default - one host's cores and RAM over its own cards, less 10%. The cluster's reserve
        # percentages no longer apply to a GPU cluster at all, so this is 64/4 x 0.9 = 14 and 1536/4 x 0.9 = 344
        # (RAM down to a multiple of 8), where correction 14 gave 15 and 360 off the 5% reserve.
        self.assertContains(r, "1 bundle = 1 GPU + 14 vCPU + 344 GB RAM")
        self.order(name="gpulab", pool="dc1.cloud/ga1", gpu=2, cpu=999, ram_gb=1)
        t = Tenant.objects.get(name="gpulab")
        self.assertEqual(
            {k: t.order[k] for k in ("site", "platform", "cluster", "gpu", "cpu", "ram_gb")},
            {"site": "dc1", "platform": "dc1.cloud", "cluster": "ga1", "gpu": 2, "cpu": 28, "ram_gb": 688},
        )
        self.assertContains(self.order(name="nogpu", pool="dc1.cloud/ga1", gpu=""), "Choose how many GPU bundles")
        self.assertContains(self.order(name="wrongsite", site="dc9"), "Select a valid choice")

    def test_order_validation(self):
        self.client.force_login(self.admin)
        self.assertContains(self.order(inet_mbps=5), "greater than or equal to 10")
        self.assertContains(self.order(inet_extra_prefixes="30"), "is not offered")
        self.assertContains(self.order(connections=[]), "Choose at least one connection")
        self.assertContains(self.order(connections=["corpnet"], corpnet_main_prefix="28"), "Select a valid choice")
        self.assertContains(self.order(name="9lives"), "starting with a letter")
        self.assertContains(self.order(name="a" * 16), "at most 15")  # u02: 15
        self.assertFalse(Tenant.objects.filter(name="gpuresearch").exists())
        self.assertContains(self.order(name=""), "This field is required")
        self.assertContains(self.order(name="abc"), "4-15 characters")
        self.order(connections=["l2"], name="onlyl2")
        self.assertEqual(Tenant.objects.get(name="onlyl2").order["connections"], ["l2"])
        self.order(connections=["inet", "l2"], name="both")
        self.assertEqual(Tenant.objects.get(name="both").order["connections"], ["inet", "l2"])
        self.org.kind = "corp"
        self.org.save()
        self.assertContains(
            self.order(kind="corp", connections=["corpnet"], corpnet_main_prefix=""), "Choose the main prefix"
        )
        # the internet and the corporate network are not combined; L2 alone only for cloud tenants
        self.assertContains(
            self.order(kind="corp", connections=["inet", "corpnet"], corpnet_main_prefix="28", name="both2"),
            "cannot be combined",
        )
        self.assertContains(self.order(kind="corp", connections=["l2"], name="onlyl2corp"), "An L2 channel alone")
        self.assertContains(
            self.order(kind="cloud", connections=["corpnet"], corpnet_main_prefix="28", name="cloudcorp"),
            "for corporate projects",
        )
        self.order(kind="corp", connections=["corpnet", "l2"], corpnet_main_prefix="28", name="corpandl2")
        o = Tenant.objects.get(name="corpandl2").order
        self.assertEqual((o["connections"], o["corpnet_prefixes"]), (["corpnet", "l2"], [28]))
        t = Tenant.objects.get(name="corpandl2")
        self.assertEqual(
            (t.stage, t.date_review),
            ("poc", __import__("datetime").date.today() + __import__("datetime").timedelta(days=20)),
        )  # as ordered

    def test_corpnet_order_base_and_prefixes(self):
        self.org.kind = "corp"
        self.org.save()
        self.client.force_login(self.admin)
        self.order(
            kind="corp",
            connections=["corpnet"],
            name="corpbase",
            corpnet_main_prefix="28",
            corpnet_main_pool="10.10.0.0/22",
            corpnet_extra_prefixes="29",
            corpnet_extra_pools="10.10.0.0/22",
        )
        o = Tenant.objects.get(name="corpbase").order
        self.assertEqual(o["corpnet_prefixes"], [28, 29])
        self.assertEqual(o["corpnet_pools"], ["10.10.0.0/22", "10.10.0.0/22"])

    def test_corporate_organization_may_choose_corp(self):
        self.org.kind = "corp"
        self.org.save()
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("new_tenant")), 'name="kind"')
        self.order(kind="corp", connections=["corpnet"], corpnet_main_prefix="28")
        t = Tenant.objects.get(name="gpuresearch")
        self.assertEqual(t.kind, "corp")
        # no internet, no igw: its port, user and console password are not set; the key is kept with the tenant
        self.assertEqual((t.igw_ssh_port, t.igw_username, t.igw_password_encrypted, t.igw_ssh_from), (None, "", "", ""))
        self.assertEqual(list(t.ssh_keys.values_list("on_igw", flat=True)), [])  # correction 13: keys at provisioning

    def test_cancel_and_delete_own_request(self):
        self.client.force_login(self.admin)
        self.order()
        t = Tenant.objects.get(name="gpuresearch")
        self.assertEqual(self.client.post(reverse("tenant_delete", args=[t.pk])).status_code, 403)  # still pending
        self.client.post(reverse("tenant_cancel", args=[t.pk]))
        t.refresh_from_db()
        self.assertEqual(t.approval, Approval.CANCELLED)
        self.client.post(reverse("tenant_delete", args=[t.pk]))
        t.refresh_from_db()
        self.assertEqual(t.status, "archived")
        self.assertEqual(self.client.post(reverse("tenant_cancel", args=[self.a.pk])).status_code, 403)  # approved


class InvitationsAnd2FA(Base):
    def test_staff_invitation_with_roles_and_2fa(self):
        sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(sa)
        self.client.post(reverse("person_add"), {"email": "ops@corp.kz", "full_name": "Ops One", "city": "astana"})
        ops = User.objects.get(email="ops@corp.kz")
        self.assertFalse(
            ops.login_enabled or ops.is_support or ops.is_resource_manager
        )  # no role before the invitation
        r = self.client.get(reverse("staff_users"))
        self.assertContains(r, "inactive")
        self.client.post(reverse("invite_person", args=[ops.pk]))  # roles are required
        self.assertEqual(len(mail.outbox), 0)
        self.client.post(reverse("invite_person", args=[ops.pk]), {"roles": ["is_resource_manager", "is_support"]})
        self.assertContains(self.client.get(reverse("staff_users")), "invited")
        link = link_path(mail.outbox[-1])
        self.client.logout()
        r = self.client.post(link, {**PROFILE, "photo": photo()})
        self.assertRedirects(r, reverse("totp_setup"), fetch_redirect_response=False)
        u = User.objects.get(email="ops@corp.kz")
        self.assertTrue(u.is_resource_manager and u.is_support and not u.is_super_admin and u.login_enabled)
        self.assertRedirects(
            self.client.get(reverse("tenant_list")), reverse("totp_setup"), fetch_redirect_response=False
        )
        self.client.get(reverse("totp_setup"))
        from accounts.utils import totp_code, totp_device

        self.client.post(reverse("totp_setup"), {"code": totp_code(totp_device(u, confirmed=False))})
        self.assertEqual(self.client.get(reverse("tenant_list")).status_code, 200)
        self.assertEqual(self.client.get(link).status_code, 400)

    def test_tenant_user_invited_to_register(self):
        sa = self.staff("sa@corp.kz", is_super_admin=True)
        org = Organization.objects.create(name="Org")
        u = User.objects.create_user(
            "dev@org.kz", None, kind=Kind.CUSTOMER, organization=org, login_enabled=False, email_verified=True
        )
        self.client.force_login(sa)
        self.assertContains(self.client.get(reverse("staff_users") + "?tab=tenant"), "dev@org.kz")
        self.client.post(reverse("invite_person", args=[u.pk]), {"read_only": "on"})
        link = link_path(mail.outbox[-1])
        self.client.logout()
        self.client.post(link, {**PROFILE, "photo": photo()})
        u.refresh_from_db()
        self.assertTrue(u.login_enabled and u.is_org_readonly)
        # block, unblock, archive
        self.client.force_login(sa)
        self.client.post(reverse("toggle_user_active", args=[u.pk]))
        u.refresh_from_db()
        self.assertFalse(u.is_active)
        self.assertContains(self.client.get(reverse("staff_users") + "?tab=tenant"), "blocked")
        self.client.post(reverse("person_archive", args=[u.pk]))
        u.refresh_from_db()
        self.assertTrue(u.archived_at)
        self.client.get(reverse("dashboard"))  # (the message about it)
        self.assertNotContains(self.client.get(reverse("staff_users") + "?tab=tenant"), "dev@org.kz")
        self.assertContains(self.client.get(reverse("staff_users") + "?tab=tenant&archived=1"), "dev@org.kz")
        self.assertTrue(User.objects.filter(pk=u.pk).exists())  # never deleted

    def test_archive_delete_invite_back(self):
        sa = self.staff("sa@corp.kz", is_super_admin=True)
        org = Organization.objects.create(name="Org")
        self.client.force_login(sa)
        # owner 2026-11-03: users are never deleted, only archived — the never-accepted ghost is archived, kept
        ghost = User.objects.create_user(
            "ghost@org.kz", None, kind=Kind.CUSTOMER, organization=org, login_enabled=False
        )
        self.client.post(reverse("person_archive", args=[ghost.pk]))
        ghost.refresh_from_db()
        self.assertTrue(ghost.archived_at and User.objects.filter(email="ghost@org.kz").exists())
        # had an account: archive drops the roles; invite back gives only the new ones
        u = User.objects.create_user(
            "dev@org.kz",
            "Str0ng-PASS-123",
            kind=Kind.CUSTOMER,
            organization=org,
            is_org_admin=True,
            email_verified=True,
        )
        User.objects.create_user(
            "owner@org.kz",
            "Str0ng-PASS-123",
            kind=Kind.CUSTOMER,
            organization=org,
            is_org_admin=True,
            email_verified=True,
        )  # keeps the org an admin when u is archived
        r = self.client.post(reverse("person_delete", args=[u.pk]), follow=True)
        self.assertTrue(User.objects.filter(pk=u.pk).exists())
        self.client.post(reverse("person_archive", args=[u.pk]))
        u.refresh_from_db()
        self.assertTrue(u.archived_at and not u.is_org_admin and not u.is_active)
        r = self.client.get(reverse("staff_users") + "?tab=tenant&archived=1")
        self.assertContains(r, "Invite back")
        self.client.post(reverse("invite_person", args=[u.pk]), {"read_only": "on"})
        u.refresh_from_db()
        self.assertFalse(u.archived_at)
        self.assertFalse(u.is_org_admin)
        self.assertContains(self.client.get(reverse("staff_users") + "?tab=tenant"), "dev@org.kz")

    def test_platform_user_lock_and_uninvited_roles(self):
        sa = self.staff("sa@corp.kz", is_super_admin=True)
        ops = self.staff("ops@corp.kz", is_support=True)
        self.client.force_login(sa)
        r = self.client.get(reverse("staff_users"))
        self.assertContains(r, "Lock")
        self.client.post(reverse("toggle_user_active", args=[ops.pk]))
        ops.refresh_from_db()
        self.assertTrue(ops.is_active)  # correction 16: the only active support engineer is not locked
        sa.is_support = True
        sa.save()
        self.client.post(reverse("toggle_user_active", args=[ops.pk]))
        ops.refresh_from_db()
        self.assertFalse(ops.is_active)
        self.assertContains(self.client.get(reverse("staff_users")), "Unlock")
        # an invitation revoked before it was accepted leaves no roles
        self.client.post(reverse("person_add"), {"email": "new@corp.kz", "full_name": "New One", "city": "astana"})
        new = User.objects.get(email="new@corp.kz")
        self.client.post(reverse("invite_person", args=[new.pk]), {"roles": ["is_support"]})
        inv = Invitation.objects.filter(user=new).latest("pk")
        self.client.post(reverse("revoke_invitation", args=[inv.pk]))
        new.refresh_from_db()
        self.assertFalse(new.is_support)

    def test_two_factor_rules(self):
        ops = self.staff("ops@corp.kz", is_support=True)
        self.client.force_login(ops)
        self.assertEqual(self.client.get(reverse("totp_disable")).status_code, 403)
        c = User.objects.create_user("c@org.kz", "Str0ng-PASS-123", kind=Kind.CUSTOMER, email_verified=True)
        from accounts.testkit import enrol
        from accounts.utils import totp_code

        dev = enrol(c)
        self.client.force_login(c)
        # correction 11: mandatory for every user — a customer cannot turn it off either
        self.assertEqual(self.client.post(reverse("totp_disable"), {"code": totp_code(dev)}).status_code, 403)
        c.refresh_from_db()
        self.assertTrue(c.totp_enabled)
        User.objects.create_user("n@org.kz", "Str0ng-PASS-123", kind=Kind.CUSTOMER, email_verified=True)
        self.client.logout()
        self.client.post(reverse("login"), {"username": "n@org.kz", "password": "Str0ng-PASS-123"})
        self.assertRedirects(
            self.client.get(reverse("dashboard")), reverse("totp_setup"), fetch_redirect_response=False
        )
        self.assertNotContains(self.client.get(reverse("totp_setup")), "turn off")

    @override_settings(OTP_TOTP_THROTTLE_FACTOR=0)  # django-otp waits after a wrong code; this test retries at once
    def test_sign_in_with_second_factor(self):
        from accounts.testkit import enrol
        from accounts.utils import totp_code

        u = self.staff("rm@corp.kz", is_resource_manager=True)
        dev = enrol(u)
        r = self.client.post(reverse("login"), {"username": "rm@corp.kz", "password": "Str0ng-PASS-123"})
        self.assertRedirects(r, reverse("login_otp"), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)
        r = self.client.post(reverse("login_otp"), {"code": "000000"})
        self.assertContains(r, "Wrong or expired code")
        r = self.client.post(reverse("login_otp"), {"code": totp_code(dev)})
        self.assertRedirects(r, reverse("dashboard"), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_sign_in_without_second_factor(self):
        User.objects.create_user("c@org.kz", "Str0ng-PASS-123", kind=Kind.CUSTOMER, email_verified=True)
        r = self.client.post(reverse("login"), {"username": "c@org.kz", "password": "Str0ng-PASS-123"})
        self.assertRedirects(r, reverse("dashboard"), fetch_redirect_response=False)

    def test_sales_rep_activation(self):
        sa = self.staff("sa@corp.kz", is_super_admin=True)
        rep = self.sales_entry()
        self.client.force_login(sa)
        self.client.post(reverse("sales_rep_invite", args=[rep.pk]))
        link = self.client.session.get("last_invite_link").replace("http://testserver", "")
        self.client.logout()
        self.client.post(link, {**PROFILE, "photo": photo()})
        rep.refresh_from_db()
        self.assertTrue(rep.login_enabled and rep.has_usable_password())
        self.assertEqual(User.objects.filter(email=rep.email).count(), 1)

    def test_bootstrap_superadmin(self):
        from django.core.management import call_command

        out = io.StringIO()
        call_command("bootstrap_superadmin", stdout=out)
        link = re.sub(r"^https?://[^/]+", "", out.getvalue().strip())
        self.client.post(link, {**PROFILE, "email": "boss@corp.kz", "photo": photo()})
        boss = User.objects.get(email="boss@corp.kz")
        self.assertTrue(boss.is_super_admin and boss.is_superuser)
        self.assertEqual(Invitation.objects.get().state, "accepted")


class PublicPage(TestCase):
    def test_home(self):
        from content.models import Article, ServiceComponent

        ServiceComponent.objects.create(name="Compute")
        Article.objects.create(kind="news", title="Hello Almaty", is_published=True)
        r = self.client.get("/")
        self.assertContains(r, "Hello Almaty")
        self.assertContains(r, 'href="https://docs.claugine.com/guide/services/"')

    def test_guides_are_on_the_public_site(self):
        go = lambda page: self.client.get(reverse("guide_page", args=[page]))  # noqa: E731
        self.assertRedirects(go("service"), "https://docs.claugine.com/guide/service/", fetch_redirect_response=False)
        self.assertRedirects(
            self.client.get(reverse("guide")), "https://docs.claugine.com/", fetch_redirect_response=False
        )
        self.client.cookies["django_language"] = "ru"
        self.assertRedirects(
            go("org-admin"), "https://docs.claugine.com/ru/guide/org-admin/", fetch_redirect_response=False
        )
        self.client.cookies["django_language"] = "kk"
        self.assertRedirects(go("staff"), "https://docs.claugine.com/kz/guide/staff/", fetch_redirect_response=False)
        self.assertEqual(go("nope").status_code, 404)


class Capacity(Base):
    def test_logical_and_physical(self):
        from decimal import Decimal

        from inventory import capacity
        from inventory.models import (
            Cluster,
            Host,
            Platform,
            PlatformDatastore,
            ServerProfile,
            Site,
            Storage,
            StorageVolume,
        )
        from tenants.models import QuotaRecord, Tenant

        site = Site.objects.create(code="dc1", number=1)
        prof = ServerProfile.objects.create(
            code="std", sockets=2, cores_per_socket=16, cores=32, ram_gb=512, cpu_mhz=2500
        )
        gprof = ServerProfile.objects.create(
            code="gpu",
            sockets=2,
            cores_per_socket=16,
            cores=32,
            ram_gb=1024,
            cpu_mhz=2000,
            gpu_count=4,
            gpu_model="L40S",
        )
        plat = Platform.objects.create(key="dc1.cloud", site=site, name="cloud", kind="shared")
        c = Cluster.objects.create(
            platform=plat,
            name="std_dc1",
            kind="shared",
            vcpu_ratio=Decimal("10"),
            spare_hosts=1,
            reserved_cpu_pct=5,
            reserved_ram_pct=5,
        )
        c.hosts.set([Host.objects.create(site=site, name=f"h{i}", profile=prof, ip=f"10.0.0.{i}") for i in range(3)])
        g = Cluster.objects.create(platform=plat, name="gpu_dc1", kind="shared", spare_hosts=0)
        g.hosts.set([Host.objects.create(site=site, name=f"g{i}", profile=gprof, ip=f"10.0.1.{i}") for i in range(2)])
        org = Organization.objects.create(name="Org")
        t = Tenant.objects.create(name="t1", organization=org, approval="approved")
        QuotaRecord.objects.create(
            tenant=t,
            site="dc1",
            cluster="std_dc1",
            version=1,
            cpu=200,
            ram_gb=300,
            disk_gb=500,
            images_gb=20,
            files_gb=1,
            backups_gb=100,
        )
        QuotaRecord.objects.create(
            tenant=t, site="dc1", cluster="gpu_dc1", version=1, cpu=16, ram_gb=512, gpu=2, disk_gb=100
        )
        rows = {r["cluster"].name: r for r in capacity.clusters()}
        r = rows["std_dc1"]
        # 3 hosts × 32 cores; one spare host; 5% of the other 64 reserved -> 60.8 cores × 10 = 608 vCPU
        self.assertEqual((r["physical"]["cores"], r["physical"]["spare_cores"], r["logical"]["vcpu"]), (96, 32, 608))
        self.assertEqual(r["physical"]["used_cores"], 20.0)  # 200 vCPU / 10
        self.assertEqual(r["physical"]["ghz"], 240.0)
        self.assertEqual(r["logical"]["ram_gb"], round(1024 * 0.95))
        self.assertEqual(r["oversubscription"], round(200 / 60.8, 2))
        gr = rows["gpu_dc1"]
        self.assertTrue(gr["gpu"])
        self.assertEqual((gr["logical"]["gpus"], gr["logical"]["used_gpus"]), (8, 2))
        vol = StorageVolume.objects.create(
            storage=Storage.objects.create(site=site, name="nfs", kind="nfs"), name="v", size_gb=1000
        )
        ds = PlatformDatastore.objects.create(platform=plat, name="vms_dc1", kind="vms", volume=vol)
        c.datastores.add(ds)
        d = next(x for x in capacity.datastores() if x["ds"].pk == ds.pk)
        self.assertEqual((d["cap_gb"], d["used_gb"]), (900, 479))  # 500 − 20 images − 1 file
        # a second VM datastore of the GPU cluster only (local disks): its own tenants, not the first one's
        ds2 = PlatformDatastore.objects.create(platform=plat, name="vms2_dc1", kind="vms", volume=vol)
        g.datastores.add(ds2)
        rows2 = {x["ds"].name: x for x in capacity.datastores()}
        self.assertEqual(rows2["vms_dc1"]["used_gb"], 479)
        self.assertLessEqual(rows2["vms2_dc1"]["used_gb"], 100)
        self.assertIsNone(rows2["vms2_dc1"]["drift"])
        from django.utils import timezone as tz

        from adoption.models import PlatformStats

        PlatformStats.objects.create(
            platform_key="dc1.cloud",
            collected_at=tz.now(),
            datastores=[
                {
                    "id": 1,
                    "name": "vms2_dc1",
                    "type": "system",
                    "total_mb": 0,
                    "used_mb": 0,
                    "free_mb": 0,
                    "clusters": ["gpu_dc1", "std_dc1"],
                }
            ],
        )
        self.assertEqual(
            next(x for x in capacity.datastores() if x["ds"].name == "vms2_dc1")["drift"],
            {"portal": ["gpu_dc1"], "one": ["gpu_dc1", "std_dc1"]},
        )
        sa = self.staff("sa@corp.kz", is_super_admin=True, is_resource_manager=True)
        self.client.force_login(sa)
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, "200 / 608 vCPU")
        self.assertContains(r, "2 / 8")
        r = self.client.get(reverse("dashboard") + "?board=rm")
        self.assertContains(r, "Shared clusters")
        self.assertNotContains(r, 'data-view-btn="physical"')


class RoleDashboards(Base):
    def test_dashboard_and_act_as(self):
        # u12: the per-role view buttons are gone; a super admin instead "acts as" any non-archived user.
        from tenants.models import Tenant

        sa = self.staff("sa@corp.kz", is_super_admin=True)
        rep = self.staff("rep@corp.kz", is_sales=True, is_kam=True)
        org = Organization.objects.create(name="Romashka", sales_rep=rep, kam=rep)
        Organization.objects.create(name="Other")
        Tenant.objects.create(name="lab", organization=org, approval="approved")
        self.client.force_login(rep)
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, "My organizations")
        self.assertContains(r, "Romashka")
        self.assertNotContains(r, "Other")
        self.assertNotContains(r, 'class="board-switch"')  # no per-role buttons
        self.assertNotContains(r, "Act as")  # a non-super-admin has no act-as control
        # super admin sees the act-as control; acting as rep renders rep's own dashboard
        self.client.force_login(sa)
        self.assertContains(self.client.get(reverse("dashboard")), "Act as")
        self.client.post(reverse("act_as"), {"user": rep.pk})
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, "Acting as")
        self.assertContains(r, "My organizations")
        self.assertContains(r, "Romashka")


class LogsAndTasks(Base):
    def test_logs_filters_tasks_and_cleanup(self):
        from datetime import timedelta

        from django.utils import timezone

        from accounts import tasks
        from accounts.models import AuditLog, Task, audit
        from tenants.models import PortalSettings

        sa = self.staff("sa@corp.kz", is_super_admin=True)
        org = Organization.objects.create(name="Org")
        audit(sa, "organization_edited", org)
        audit(sa, "setting_changed", PortalSettings.get())
        self.client.force_login(sa)
        r = self.client.get(reverse("audit_log") + "?section=organizations")
        self.assertContains(r, "organization_edited")
        self.assertNotContains(r, "setting_changed")
        self.assertContains(self.client.get(reverse("audit_log") + "?q=setting"), "setting_changed")
        t = tasks.start("test", "A test task", lambda task: (task.step("one", "ok", "fine"), {"n": 1})[1], user=sa)
        t.refresh_from_db()
        self.assertEqual(t.state, Task.State.DONE)
        self.assertEqual(t.result, {"n": 1})
        bad = tasks.start("test", "A failing task", lambda task: 1 / 0, user=sa)
        bad.refresh_from_db()
        self.assertEqual(bad.state, Task.State.FAILED)
        r = self.client.get(reverse("tasks"))
        self.assertContains(r, "A test task")
        self.assertContains(r, "ZeroDivisionError")
        Task.objects.filter(pk=t.pk).update(finished_at=timezone.now() - timedelta(days=30))
        AuditLog.objects.filter(action="setting_changed").update(created_at=timezone.now() - timedelta(days=900))
        done = tasks.cleanup(10, 500)
        self.assertEqual((done["tasks"], done["log_entries"]), (1, 1))
        self.assertTrue(Task.objects.filter(pk=bad.pk).exists())


class UnifiedLists(Base):
    def test_sales_reps_and_news_lists(self):
        from content.models import Article, ServiceComponent

        sa = self.staff("sa@corp.kz", is_super_admin=True, is_support=True)
        rep = self.sales_entry()
        Org = Organization.objects.create(name="Org", sales_rep=rep)
        ServiceComponent.objects.create(name="Compute")
        Article.objects.create(kind="news", title="Hello", is_published=True)
        self.client.force_login(sa)
        r = self.client.get(reverse("sales_reps"))
        self.assertContains(r, 'data-open="dlg-rep-new"')
        self.assertContains(r, f'id="dlg-rep-{rep.pk}"')
        self.assertContains(r, Org.name)
        r = self.client.post(
            reverse("sales_rep_edit", args=[rep.pk]), {"full_name": "Dana S", "phone": "", "city": "almaty"}
        )
        self.assertRedirects(r, reverse("sales_reps"), fetch_redirect_response=False)
        r = self.client.get(reverse("manage_articles"))
        self.assertContains(r, 'data-open="dlg-art-new"')
        self.assertContains(r, 'data-open="dlg-comp-new"')
        self.assertContains(r, "olist-detail")


class StaffRolesMatrix(Base):
    def test_platform_users_roles_given_with_the_invitation(self):
        sa = self.staff("sa@corp.kz", is_super_admin=True)
        rep = self.sales_entry()
        self.client.force_login(sa)
        r = self.client.get(reverse("staff_users"))
        self.assertContains(r, '?tab=tenant">Organizations (')  # tabs: Platform, Organizations, each with a count
        self.assertNotContains(r, "Resource managers</a>")  # no tabs per role any more
        sa.refresh_from_db()
        # alone, sa is given the roles nobody holds; is_sales is left with the directory rep, not forced onto sa
        self.assertTrue(sa.is_resource_manager and sa.is_support and sa.is_kam)
        self.assertFalse(sa.is_sales)
        two = self.staff("two@corp.kz", is_super_admin=True)
        url = reverse("person_roles", args=[sa.pk])
        r = self.client.post(url, {"roles": ["is_super_admin", "is_resource_manager"]}, follow=True)
        self.assertContains(r, "at least one user")  # support and KAM would have nobody (sales is on the directory rep)
        self.client.post(
            reverse("person_roles", args=[two.pk]), {"roles": ["is_super_admin", "is_support", "is_sales", "is_kam"]}
        )
        self.client.post(url, {"roles": ["is_super_admin", "is_resource_manager"]})
        sa.refresh_from_db()
        self.assertEqual((sa.is_resource_manager, sa.is_support, sa.is_kam), (True, False, False))
        r = self.client.post(url, {"roles": ["is_resource_manager"]}, follow=True)
        self.assertContains(r, "cannot take the super admin role from yourself")
        r = self.client.post(reverse("person_archive", args=[two.pk]), follow=True)
        self.assertContains(r, "Give")  # two holds support, sales and KAM alone
        self.assertEqual(
            self.client.post(reverse("invite_person", args=[rep.pk]), {"roles": ["is_sales"]}).status_code, 302
        )
        rep.refresh_from_db()
        self.assertTrue(rep.is_sales)


@override_settings(
    FIELD_ENCRYPTION_KEY=__import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
)
class Corrections02(Base):
    """Everything in the database; staff order tenants; logs filters; templates and credentials can be deleted."""

    def test_photo_kept_in_the_database_scaled_down(self):
        u = self.staff("p@corp.kz", is_super_admin=True)
        buf = io.BytesIO()
        Image.new("RGB", (1600, 900), "teal").save(buf, "PNG")
        u.photo = SimpleUploadedFile("big.png", buf.getvalue(), content_type="image/png")
        u.save()
        u.refresh_from_db()
        self.assertTrue(u.photo)
        self.assertEqual(Image.open(io.BytesIO(bytes(u.photo_data))).size, (512, 288))
        self.client.force_login(u)
        r = self.client.get(reverse("user_photo", args=[u.pk]))
        self.assertEqual((r.status_code, r["Content-Type"]), (200, u.photo_type))

    def test_secrets_only_from_the_database(self):
        import os
        import tempfile

        from inventory import credentials as cr

        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "cloud"))
        with open(os.path.join(d, "cloud", "one.yaml"), "w") as f:
            f.write("oneadmin: s3cr3t-1\n")
        from inventory.testing import load_from_secrets

        self.assertIsNone(cr.value("secrets.acme.cloud.one", "oneadmin"))  # files are not read at run time
        load_from_secrets(d)
        shutil.rmtree(d)
        self.assertEqual(cr.value("secrets.acme.cloud.one", "oneadmin"), "s3cr3t-1")  # the copy in the database

    def test_staff_order_a_tenant_for_an_organization(self):
        from tenants.models import Tenant

        make_inventory()
        sa = self.staff("sa@corp.kz", is_super_admin=True, is_resource_manager=True)
        org = Organization.objects.create(name="Org", tax_id=valid_bin(), short_name="orgone")
        owner = User.objects.create_user(
            "owner@org.kz",
            None,
            kind="customer",
            organization=org,
            is_org_admin=True,
            full_name="Owner",
            login_enabled=False,
        )
        self.client.force_login(sa)
        self.assertContains(self.client.get(reverse("tenant_list")), reverse("tenant_new"))
        r = self.client.get(reverse("tenant_new") + f"?org={org.pk}")
        form = r.context["form"]
        data = {
            k: (form.initial.get(k, f.initial))
            for k, f in form.fields.items()
            if form.initial.get(k, f.initial) is not None
        }
        data = {k: (v() if callable(v) else v) for k, v in data.items()}
        data.update(
            {
                "org": org.pk,
                "name": "proj1",
                "connections": ["inet"],
                "igw_ssh_key": "ssh-ed25519 AAAAx me",
                "inet_mbps": 100,
                "inet_main_prefix": 31,
                "inet_extra_prefixes": "",
                "pool": next(iter(form.pools)),
                "admin_password": "Kx7-Pass+2026-Qw",
                "igw_password": "Zt9-Gate+2026-Lp",
                "stage": "poc",
                "date_review": __import__("datetime").date.today().isoformat(),
            }
        )
        r = self.client.post(reverse("tenant_new"), data)
        t = Tenant.objects.filter(organization=org, name="proj1").first()
        self.assertIsNotNone(
            t, (r.status_code, r.get("Location"), r.context and r.context.get("form") and r.context["form"].errors)
        )
        self.assertEqual(
            (t.created_by, t.approval, t.tenant_admin), (sa, "pending", owner)
        )  # the only user (correction 15)

    def test_logs_filters_and_deletes(self):
        from accounts.models import AuditLog, audit
        from inventory.models import ConfigTemplate

        sa = self.staff("sa@corp.kz", is_super_admin=True)
        org = Organization.objects.create(name="Org")
        audit(sa, "organization_edited", org)
        AuditLog.objects.filter(action="organization_edited").update(created_at="2026-01-05T10:00:00Z")
        audit(sa, "something_else", org)
        self.client.force_login(sa)
        url = reverse("audit_log")
        r = self.client.get(url + "?from=2026-01-01&to=2026-01-31")
        self.assertContains(r, "organization_edited")
        self.assertNotContains(r, "something_else")
        r = self.client.get(url + f"?object=organization:{org.pk}&user={sa.pk}")
        self.assertContains(r, "something_else")
        self.assertContains(r, "<th>User</th>", html=False)
        t = ConfigTemplate.objects.filter(role="l2-a").first() or ConfigTemplate.objects.create(
            role="l2-a", name="l2a", body="x"
        )
        self.client.post(reverse("infra_template_delete", args=[t.pk]))  # no L2 channel uses it
        self.assertFalse(ConfigTemplate.objects.filter(role="l2-a").exists())
        self.client.post(
            reverse("infra_template_new"),
            {
                "role": "l2-a",
                "new-name": "L2 a",
                "new-os": "vyos",
                "new-os_version": "",
                "new-description": "",
                "new-body": "set x",
            },
        )
        self.assertTrue(ConfigTemplate.objects.filter(role="l2-a", name="L2 a").exists())


@override_settings(FIELD_ENCRYPTION_KEY="m3Jb0bC1k0qWw8bq3cJmHqk6kVqZ0b9mJxS6m0f4n1A=")
class PortalAppliance(Base):
    @unittest.skipIf(
        edition.standard_build(), "a Standard build keeps Standard's limits whatever the licence says (editions step 5)"
    )
    def test_settings_schedule_backup_and_mail(self):
        import datetime
        import tempfile
        from unittest import mock

        from django.utils import timezone

        from accounts import appliance
        from accounts.utils import site_link
        from tenants.models import PortalSettings

        sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(sa)
        folder = tempfile.mkdtemp()
        url = reverse("portal_settings")
        data = {
            "section": "appliance",
            "mail_mode": "internal",
            "mail_from": "Example Cloud <portal@example.kz>",
            "mail_host": "",
            "mail_port": 587,
            "mail_user": "",
            "backup_local": "on",
            "backup_path": folder,
            "backup_start": "00:45",
            "backup_period_hours": 6,
            "backup_keep_days": 10,
            "backup_password": "",
        }
        r = self.client.post(url, data)
        self.assertEqual(
            PortalSettings.get().backup_path, PortalSettings().backup_path
        )  # refused: backups need a password
        self.assertContains(r, "Required: backups are encrypted.")  # shown at the field
        self.assertTrue(PortalSettings.get().rm_approval_required == PortalSettings().rm_approval_required)
        self.client.post(url, {**data, "backup_password": "Archive-Pass-2026"})
        # the public names are on the Provider tab (corrections-18)
        self.client.post(
            url,
            {
                "section": "publishing",
                "tab": "provider",
                "public_url": "https://portal.example.kz:55543/",
                "test_url": "https://portal-test.example.kz",
            },
        )
        s = PortalSettings.get()
        # release 226: the Test URL is no longer a setting (the test address is the public URL + /test-new-version/)
        self.assertEqual((s.public_url, s.test_url, s.backup_path), ("https://portal.example.kz:55543", "", folder))
        r = self.client.get(url + "?tab=portal")  # u01: the portal nodes are under Portal
        self.assertContains(r, "This portal runs on one node")
        self.assertNotEqual(s.backup_password_encrypted, "Archive-Pass-2026")
        self.assertEqual(s.get_secret("backup_password"), "Archive-Pass-2026")
        appliance.forget()
        self.assertEqual(site_link("/x/"), "https://portal.example.kz:55543/x/")
        r = self.client.get(url + "?tab=mail")
        self.assertContains(r, "E-mail templates")
        self.assertNotContains(r, "Archive-Pass-2026")
        tz = timezone.get_current_timezone()
        now = datetime.datetime(2026, 10, 24, 14, 0, tzinfo=tz)
        self.assertEqual(timezone.localtime(appliance.slots(s, now)).strftime("%H:%M"), "00:45")  # u07: one a day
        s.backup_last_at = datetime.datetime(2026, 10, 24, 13, 0, tzinfo=tz)  # already ran today
        self.assertFalse(appliance.due(s, now))
        s.backup_last_at = datetime.datetime(2026, 10, 23, 13, 0, tzinfo=tz)  # last ran yesterday
        self.assertTrue(appliance.due(s, now))

        def dump(tmp):
            with open(f"{tmp}/database.sql", "w") as fh:
                fh.write("-- dump\n")

        import os

        # owner 2026-11-08: no subfolder - the configured folder itself holds the backups, named
        # claugine_<fqdn>_… , and pruning is scoped to this portal's own fqdn
        sub = folder
        os.makedirs(sub, exist_ok=True)
        _fq = appliance.portal_fqdn(PortalSettings.get())
        old = f"{sub}/{appliance.ARCHIVE_PREFIX}_{_fq}_20260101-0045.tar.enc"
        open(old, "w").close()
        os.utime(old, (0, 0))
        envf = tempfile.mktemp()
        with open(envf, "w") as fh:
            fh.write("FIELD_ENCRYPTION_KEY=test-key\n")
        commitf = tempfile.mktemp()
        with open(commitf, "w") as fh:
            fh.write("0123456789abcdef0123456789abcdef01234567\n")
        with (
            mock.patch("accounts.appliance._dump", side_effect=dump),
            override_settings(ENV_FILE=envf, INSTALL_ID="inst-0123456789abcdef"),
            mock.patch.dict(os.environ, {"PORTAL_COMMIT_FILE": commitf}),
        ):
            result = appliance.backup(PortalSettings.get())
        self.assertTrue(result.startswith("ok:"), result)
        names = os.listdir(sub)
        self.assertEqual(len(names), 1)  # the archive older than 10 days is removed
        work = tempfile.mkdtemp()
        b = f"{sub}/{names[0]}"
        self.assertTrue(os.path.isdir(b) and names[0].startswith("claugine_"))  # a backup is a folder
        # data and the GitHub commit only: the portal itself comes from GitHub (2026-10-30)
        self.assertEqual(sorted(os.listdir(b)), ["COMMIT", "MANIFEST", "README", "database.sql.gz", "portal.env.zip"])
        self.assertEqual(open(f"{b}/COMMIT").read().strip(), "0123456789abcdef0123456789abcdef01234567")
        self.assertIn("commit 0123456789abcdef0123456789abcdef01234567", open(f"{b}/MANIFEST").read())
        # the restore reads the installation id before it can open portal.env.zip (2026-11-10)
        self.assertIn("install_id inst-0123456789abcdef\n", open(f"{b}/MANIFEST").read())
        self.assertEqual(oct(os.stat(b).st_mode & 0o777), "0o750")
        self.assertEqual({oct(os.stat(f"{b}/{x}").st_mode & 0o777) for x in os.listdir(b)}, {"0o640"})
        import gzip

        self.assertEqual(gzip.open(f"{b}/database.sql.gz").read(), b"-- dump\n")
        import pyzipper

        with pyzipper.AESZipFile(f"{b}/portal.env.zip") as zf:
            with self.assertRaises(RuntimeError):  # no password, no key
                zf.read("portal.env")
            zf.setpassword(b"Archive-Pass-2026")
            self.assertEqual(zf.read("portal.env"), b"FIELD_ENCRYPTION_KEY=test-key\n")
        r = self.client.get(url + "?tab=backups")
        self.assertContains(r, "claugine-install.sh --restore --licence-key &lt;licence key&gt;")
        self.assertContains(r, f"--backup ~/{names[0]} --site-url")
        self.assertTrue(PortalSettings.get().backup_last_result.startswith("ok:"))
        self.client.post(url, {"section": "backup_now"})
        self.assertTrue(appliance.due(PortalSettings.get()))
        # mail: an unreachable relay keeps the message in files
        from django.core.mail import EmailMessage

        s = PortalSettings.get()
        s.mail_mode, s.mail_host, s.mail_port = "external", "127.0.0.1", 9
        s.client_mail = True  # x@example.kz is no platform user: sent only with e-mail to clients on
        s.save()
        with override_settings(EMAIL_FILE_PATH=work):
            n = appliance.EmailBackend().send_messages([EmailMessage("s", "b", None, ["x@example.kz"])])
        self.assertEqual(n, 1)
        self.assertTrue(any(f.endswith(".log") for f in os.listdir(work)))
        from accounts.models import AuditLog

        self.assertTrue(AuditLog.objects.filter(action="mail_failed").exists())

    def test_internal_relay_taken_over_and_kept(self):
        import tempfile
        from unittest import mock

        from accounts import appliance
        from tenants.models import PortalSettings

        d = tempfile.mkdtemp()
        key = "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n"
        os.makedirs(f"{d}/keys")
        with open(f"{d}/portal.private", "w") as fh:
            fh.write(key)
        with open(f"{d}/sasl", "w") as fh:
            fh.write("[smtp-relay.example.com]:587 relayuser:Relay-Secret-1\n")
        with open(f"{d}/opendkim.conf", "w") as fh:
            fh.write(
                f"Domain app.example.kz\nSelector portal\nKeyFile {d}/portal.private\nSocket inet:8891@127.0.0.1\n"
            )
        conf = {"relayhost": "[smtp-relay.example.com]:587", "smtp_tls_security_level": "encrypt"}
        with (
            mock.patch.object(appliance, "SASL_FILE", f"{d}/sasl"),
            mock.patch.object(appliance, "DKIM_CONF", f"{d}/opendkim.conf"),
            mock.patch.object(appliance, "_postconf", side_effect=lambda k: conf.get(k, "")),
            mock.patch("shutil.which", return_value="/usr/sbin/postconf"),
        ):
            self.assertTrue(appliance.capture_mail(PortalSettings.get(), lambda m: None))
        s = PortalSettings.get()
        self.assertEqual(
            (s.mail_host, s.mail_port, s.mail_user, s.mail_tls, s.mail_dkim_domain, s.mail_dkim_selector),
            ("smtp-relay.example.com", 587, "relayuser", True, "app.example.kz", "portal"),
        )
        self.assertEqual((s.get_secret("mail_password"), s.get_secret("mail_dkim_key")), ("Relay-Secret-1", key))
        self.assertNotIn("Relay-Secret-1", s.mail_password_encrypted)
        with mock.patch("shutil.which", return_value="/usr/sbin/postconf"):
            self.assertFalse(appliance.capture_mail(s, lambda m: None))  # only once: Settings decide from now on
        # applied back: nothing differs on a VM set up like this, so nothing is written or restarted
        conf.update(
            {
                "inet_interfaces": "loopback-only",
                "smtp_sasl_auth_enable": "yes",
                "smtp_sasl_password_maps": f"hash:{d}/sasl",
                "smtp_sasl_security_options": "noanonymous",
                "smtpd_milters": "inet:127.0.0.1:8891",
                "non_smtpd_milters": "inet:127.0.0.1:8891",
                "milter_default_action": "accept",
                "milter_protocol": "6",
            }
        )
        with open(f"{d}/opendkim.conf", "w") as fh:  # as on the portal VM
            fh.write(
                "Domain app.example.kz\nSelector portal\nKeyFile /etc/opendkim/keys/app.example.kz/portal.private\n"
                "Socket inet:8891@127.0.0.1\nMode s\nCanonicalization relaxed/simple\nOversignHeaders From\nSyslogSuccess yes\n"
            )
        with (
            mock.patch.object(appliance, "SASL_FILE", f"{d}/sasl"),
            mock.patch.object(appliance, "DKIM_CONF", f"{d}/opendkim.conf"),
            mock.patch.object(appliance, "_postconf", side_effect=lambda k: conf.get(k, "")),
            mock.patch("shutil.which", return_value="/usr/sbin/postconf"),
            mock.patch("subprocess.run") as run,
            mock.patch.object(
                appliance, "_read", side_effect=lambda p: key if p.endswith(".private") else open(p).read()
            ),
        ):
            appliance.apply_mail(PortalSettings.get(), lambda m: None)
        self.assertEqual(run.call_count, 0, run.call_args_list)


class TenantLifecycle(Base):
    def test_status_review_and_daily_round(self):
        import datetime

        from accounts.models import AuditLog
        from tenants import lifecycle
        from tenants.models import Tenant

        rm = self.staff("rm@corp.kz", is_resource_manager=True)
        rep = self.staff("rep@corp.kz", is_sales=True)
        org = Organization.objects.create(name="Org", tax_id=valid_bin(), short_name="orgone", sales_rep=rep)
        t = Tenant.objects.create(organization=org, name="poc1", title="P", approval="approved", stage="poc")
        lifecycle.default_review(t)
        t.save()
        self.assertEqual(t.date_review, timezone.localdate() + datetime.timedelta(days=30))
        self.client.force_login(rep)  # the sales rep extends a POC up to 3 months, no further
        self.assertEqual(
            self.client.post(reverse("tenant_status", args=[t.pk]), {"ss-status": "paused"}).status_code, 403
        )
        limit = t.poc_review_limit()
        self.client.post(
            reverse("tenant_review", args=[t.pk]), {"rv-date_review": (limit + datetime.timedelta(days=1)).isoformat()}
        )
        t.refresh_from_db()
        self.assertNotEqual(t.date_review, limit + datetime.timedelta(days=1))
        self.client.post(reverse("tenant_review", args=[t.pk]), {"rv-date_review": limit.isoformat()})
        t.refresh_from_db()
        self.assertEqual(t.date_review, limit)
        self.client.force_login(rm)
        self.client.post(reverse("tenant_status", args=[t.pk]), {"ss-status": "saved"})  # ok → saved: not a step
        t.refresh_from_db()
        self.assertEqual(t.status, "ok")
        for new in ("paused", "suspended", "saved"):
            self.client.post(reverse("tenant_status", args=[t.pk]), {"ss-status": new})
            t.refresh_from_db()
            self.assertEqual(t.status, new)
        self.assertEqual(t.date_review, timezone.localdate() + datetime.timedelta(days=30))
        from tenants.models import PrefixAssignment, QuotaRecord

        PrefixAssignment.objects.create(tenant=t, site="dc1", kind="inet_main", cidr="203.0.113.40/31")
        QuotaRecord.objects.create(tenant=t, site="dc1", cluster="ca1", version=1, cpu=2, ram_gb=4, disk_gb=20)
        self.assertEqual(lifecycle.run_daily(t.date_review - datetime.timedelta(days=1))["archived"], 0)
        self.assertEqual(lifecycle.run_daily(t.date_review)["archived"], 1)  # a month after saving: archived
        t.refresh_from_db()
        self.assertEqual(t.status, "archived")
        # owner 2026-11-11: an archived project releases everything; only its record stays
        self.assertFalse(PrefixAssignment.active().filter(tenant=t).exists())
        self.assertEqual(t.current_quota_records(), [])
        self.assertTrue(Tenant.objects.filter(pk=t.pk).exists())  # never deleted
        c = Tenant.objects.create(
            organization=org,
            name="cli1",
            title="C",
            approval="approved",
            stage="client",
            contract_end=timezone.localdate() + datetime.timedelta(days=30),
        )
        out = lifecycle.run_daily()
        self.assertEqual((out["notices_sent"], out["notices_held"]), (0, 1))  # email to clients is off
        n = AuditLog.objects.get(action="tenant_notice", target_id=str(c.pk))
        self.assertEqual((n.details["what"], n.details["sent"]), ("contract", "held"))
        self.assertEqual(len(mail.outbox), 0)


@override_settings(FIELD_ENCRYPTION_KEY="m3Jb0bC1k0qWw8bq3cJmHqk6kVqZ0b9mJxS6m0f4n1A=")
class Corrections04(Base):
    def test_rename_keeps_references_and_org_history(self):
        from accounts.models import audit
        from inventory import credentials as cr
        from inventory.models import Host, Secret
        from tenants.models import Tenant

        make_inventory()
        sa = self.staff("sa@corp.kz", is_super_admin=True)
        ipmi = Secret(kind="user", name="ipmi.users.standard", username="admin")
        ipmi.put("password", "Ipmi-Secret-6")
        ipmi.save()
        src = Secret(kind="user", name="claugine.source.ssh", username="opsuser")
        src.put("private_key", "-----BEGIN OPENSSH PRIVATE KEY-----\nx\n-----END OPENSSH PRIVATE KEY-----\n")
        src.save()
        from inventory.models import Site

        h = Host.objects.create(site=Site.objects.first(), name="hkvm01", ipmi_user=ipmi.ref)
        self.client.force_login(sa)
        r = self.client.get(reverse("portal_settings") + "?tab=operations")
        self.assertNotContains(r, "Old management system")  # 2026-10-28: removed
        for s, new in ((ipmi, "ipmi-default"), (src, "ssot_old-db")):  # rename: the same secret, a new name
            p = f"s{s.pk}-"
            self.client.post(
                reverse("infra_secret_save", args=[s.pk]),
                {p + "name": new, p + "username": s.username, p + "purpose": ""},
            )
            s.refresh_from_db()
            self.assertEqual(s.name, new)
        self.assertEqual(Secret.objects.filter(kind="user").count(), 2)  # nothing new was made
        self.assertEqual(cr.account(Host.objects.get(pk=h.pk).ipmi_user)[0], "admin")
        self.assertEqual(cr.unresolved(), [])
        self.assertContains(
            self.client.get(reverse("infra_site", args=[h.site.code]) + "?tab=hosts"), "Users: ipmi-default"
        )
        # an organization's history shows its tenants' too
        org = Organization.objects.create(name="Org", tax_id=valid_bin(), short_name="orgone")
        t = Tenant.objects.create(organization=org, name="ten1", title="T", approval="approved")
        audit(sa, "organization_edited", org)
        audit(sa, "tenant_note", t, note="x")
        r = self.client.get(reverse("audit_log") + f"?object=organization:{org.pk}")
        self.assertContains(r, "organization_edited")
        self.assertContains(r, "tenant_note")


class QuotaUsageTag(TestCase):
    """The one place the quota/usage thresholds live (owner 2026-11-09: red at 100, yellow 75-99)."""

    def render(self, q, u):
        from accounts.templatetags.ui import qu

        return qu(q, u)

    def test_quota_leads_and_usage_follows_without_a_slash(self):
        h = self.render(8, 3)
        self.assertIn("<b>8</b>", h)
        self.assertIn('<span class="qu">3</span>', h)
        import re

        self.assertEqual("8 3", re.sub(r"<[^>]+>", "", h).strip())  # the visible text carries no separator

    def test_the_bar_turns_yellow_at_75_and_red_at_100(self):
        self.assertIn('class="ub ok"', self.render(100, 74))
        self.assertIn('class="ub warn"', self.render(100, 75))
        self.assertIn('class="ub warn"', self.render(100, 99))
        self.assertIn('class="ub bad"', self.render(100, 100))
        self.assertIn('class="ub bad"', self.render(100, 140))  # over the quota: still red, never past 100%
        self.assertIn("width:100%", self.render(100, 140))

    def test_no_reading_shows_the_quota_alone(self):
        for empty in (None, ""):
            h = self.render(8, empty)
            self.assertIn("<b>8</b>", h)
            self.assertNotIn("ub ", h)

    def test_the_gpu_column_keeps_its_model_in_front(self):
        from accounts.templatetags.ui import qu

        self.assertIn("L40S <b>4</b>", qu(4, 3, prefix="L40S"))


class Correction08(Base):
    """Resource tracking of an organization, quotas against the platform, refresh from the platforms."""

    def setUp(self):
        make_inventory()
        self.rm = self.staff("rm@corp.kz", is_resource_manager=True)
        self.org = Organization.objects.create(name="Org", tax_id=valid_bin(), short_name="orgone")

    def test_resource_tracking(self):
        from datetime import timedelta

        from django.utils import timezone

        from tenants.models import Connectivity, QuotaRecord

        t = Tenant.objects.create(organization=self.org, name="proj1", title="P", approval="approved")
        t0 = timezone.now() - timedelta(days=10)
        QuotaRecord.objects.create(
            tenant=t,
            site="dc1",
            cluster="ca1",
            version=1,
            cpu=4,
            ram_gb=16,
            disk_gb=100,
            backups_gb=10,
            effective_from=t0,
        )
        Connectivity.objects.create(
            tenant=t,
            site="dc1",
            version=1,
            connection_type="inet",
            inet_mbps=100,
            inet_main_prefix=31,
            inet_public_prefixes="29",
            l2_vlans="101 102",
            effective_from=t0,
        )
        QuotaRecord.objects.create(
            tenant=t,
            site="dc1",
            cluster="ca1",
            version=2,
            cpu=8,
            ram_gb=32,
            gpu=1,
            disk_gb=200,
            backups_gb=10,
            effective_from=t0 + timedelta(days=4),
        )
        from tenants.tracking import history

        rows = history(self.org)
        self.assertEqual(
            [(r["action"], r["cpu"], r["gpu"], r["days"]) for r in rows], [("update", 8, 1, 6), ("create", 4, 0, 4)]
        )  # days alive: until the next change, the latest until today
        self.assertEqual((rows[1]["public_ips"], rows[1]["inet_mbps"], rows[1]["l2"]), (2 + 8, 100, 2))
        self.client.force_login(self.rm)
        r = self.client.get(reverse("org_resources", args=[self.org.pk]))
        self.assertContains(r, "Resource tracking")
        self.assertContains(r, '8 <small class="muted">(1)</small>')
        self.assertContains(
            self.client.get(reverse("org_detail", args=[self.org.pk])), 'id="resources"'
        )  # correction 13: on the page

    def test_quota_against_the_platform(self):
        from django.utils import timezone

        from adoption.models import Observation, PlatformStats
        from tenants.models import QuotaRecord
        from tenants.quota_check import compare

        t = Tenant.objects.create(
            organization=self.org,
            name="proj1",
            title="P",
            approval="approved",
            group_name="_t_orgone_proj1",
            one_group_id=150,
            order={"platform": "dc1.cloud"},
        )
        QuotaRecord.objects.create(
            tenant=t, site="dc1", cluster="ca1", version=1, cpu=4, ram_gb=16, disk_gb=100, backups_gb=10
        )
        self.assertEqual(compare(t), ("", []))  # not read yet
        PlatformStats.objects.create(
            platform_key="dc1.cloud", collected_at=timezone.now(), datastores=[{"id": 3, "type": "backups"}]
        )
        q = {
            "VM_QUOTA": {
                "VM": [
                    {
                        "CLUSTER_IDS": "0",
                        "RUNNING_CPU": "4",
                        "RUNNING_MEMORY": "16384",
                        "RUNNING_PCI_DEV": "0",
                        "SYSTEM_DISK_SIZE": str(100 * 1024),
                    },
                    {"CLUSTER_IDS": "1", "RUNNING_CPU": "-1", "RUNNING_MEMORY": "-1"},
                ]
            },
            "DATASTORE_QUOTA": {"DATASTORE": {"ID": "3", "SIZE": "10240"}},
        }
        o = Observation.objects.create(
            platform_key="dc1.cloud", group_id=150, group_name="_t_orgone_proj1", quotas=q, collected_at=timezone.now()
        )
        self.assertEqual(compare(t), ("ok", []))
        q["VM_QUOTA"]["VM"][0]["RUNNING_CPU"] = "17"
        o.quotas = q
        o.save()
        self.assertEqual(compare(t), ("diff", [("vCPU", 4, 17)]))
        self.client.force_login(self.rm)
        r = self.client.get(reverse("tenant_list") + "?tab=active")
        self.assertNotContains(r, "vCPU: portal 4, platform 17")  # owner 2026-11-11: no quota column in the list
        # ... the platform's consistency check finds it instead (Site → Platforms), and its repair sets the quota
        from inventory.models import PlatformControl
        from tenants import consistency

        archived = Tenant.objects.create(
            organization=self.org,
            name="old1",
            title="Old",
            approval="approved",
            status="archived",
            group_name="_t_orgone_old1",
            order={"platform": "dc1.cloud"},
        )
        groups = {"oneadmin": 0, "users": 1, "_t_orgone_proj1": 150, "_t_orgone_old1": 151, "mystery": 152}

        class FakeOne:
            def _call(self, method, *a):
                if method == "one.grouppool.info":
                    return (
                        "<GROUP_POOL>"
                        + "".join(f"<GROUP><ID>{i}</ID><NAME>{n}</NAME></GROUP>" for n, i in groups.items())
                        + "</GROUP_POOL>"
                    )
                if method == "one.userpool.info":
                    return "<USER_POOL><USER><ID>9</ID><GID>151</GID><NAME>old1-admin</NAME></USER></USER_POOL>"
                if method == "one.vmpool.info":
                    return (
                        "<VM_POOL><VM><ID>7</ID><NAME>x</NAME><GID>151</GID><UID>9</UID><STATE>3</STATE></VM>"
                        "<VM><ID>8</ID><NAME>y</NAME><GID>152</GID><UID>0</UID><STATE>3</STATE></VM></VM_POOL>"
                    )
                return "<POOL/>"

        c, _ = PlatformControl.objects.get_or_create(key="dc1.cloud")
        res = consistency.check(c, one=FakeOne())
        self.assertEqual(res["error"], "")
        self.assertEqual([q["group"] for q in res["quota"]], ["_t_orgone_proj1"])
        self.assertEqual(
            res["leftovers"],
            [
                {
                    "tenant": archived.pk,
                    "group": "_t_orgone_old1",
                    "title": "Old",
                    "counts": {"vms": 1, "users": 1, "group": 1},
                }
            ],
        )
        self.assertEqual([(u["group"], u["objects"]) for u in res["unknown"]], [("mystery", 1)])
        self.assertEqual(len(res["actions"]), 2)  # the unknown group is shown, never repaired
        self.assertFalse(consistency.ok(c))
        with (
            mock.patch("tenants.deploy.apply_quota", return_value="quota set") as ap,
            mock.patch("tenants.undeploy.Undeployer") as und,
            mock.patch("tenants.consistency.check") as again,
            mock.patch("adoption.collector.collect"),
        ):
            und.return_value.live = True
            done = consistency.repair(c.pk)
        ap.assert_called_once()
        self.assertEqual(ap.call_args.args[0], t)
        und.assert_called_once()
        again.assert_called_once()
        self.assertTrue(any("quota set" in d for d in done))

    def test_addresses_against_the_registry(self):
        """Owner 2026-11-11: the consistency check compares the public and corporate addresses used on the platform
        with the address registry."""
        from inventory.models import PlatformControl
        from tenants import consistency
        from tenants.models import PrefixAssignment

        t = Tenant.objects.create(
            organization=self.org,
            name="proj9",
            title="P",
            approval="approved",
            group_name="_t_orgone_proj9",
            one_group_id=190,
            order={"platform": "dc1.cloud"},
        )
        old = Tenant.objects.create(
            organization=self.org,
            name="old9",
            title="O",
            approval="approved",
            status="archived",
            group_name="_t_orgone_old9",
            order={"platform": "dc1.cloud"},
        )
        PrefixAssignment.objects.create(tenant=t, site="dc1", kind="inet_main", cidr="203.0.113.10/31")
        PrefixAssignment.objects.create(tenant=t, site="dc1", kind="inet_extra", cidr="203.0.113.32/29")  # not used
        PrefixAssignment.objects.create(tenant=old, site="dc1", kind="inet_main", cidr="203.0.113.20/31")  # left active

        def vnet(name, group, *ars):
            return (
                f"<VNET><NAME>{name}</NAME><GNAME>{group}</GNAME><AR_POOL>"
                + "".join(f"<AR><IP>{ip}</IP><SIZE>{n}</SIZE></AR>" for ip, n in ars)
                + "</AR_POOL></VNET>"
            )

        pool = (
            "<VNET_POOL>"
            + vnet("proj9-main", "_t_orgone_proj9", ("203.0.113.10", 2))
            + vnet("old9-main", "_t_orgone_old9", ("203.0.113.20", 2))
            + vnet("someone", "_t_orgone_other", ("203.0.113.12", 2))  # not in the registry
            + vnet("private", "_t_orgone_proj9", ("10.9.9.0", 16))  # outside the public pools
            + "</VNET_POOL>"
        )

        class FakeOne:
            def _call(self, method, *a):
                if method == "one.vnpool.info":
                    return pool
                if method == "one.grouppool.info":
                    return "<GROUP_POOL><GROUP><ID>190</ID><NAME>_t_orgone_proj9</NAME></GROUP></GROUP_POOL>"
                return "<POOL/>"

        c, _ = PlatformControl.objects.get_or_create(key="dc1.cloud")
        with mock.patch("tenants.quota_check.compare", return_value=("ok", [])):
            res = consistency.check(c, one=FakeOne())
        self.assertEqual(res["error"], "")
        got = sorted((a["what"], a["cidr"]) for a in res["addresses"])
        self.assertEqual(
            got, [("archived", "203.0.113.20/31"), ("unregistered", "203.0.113.12/31"), ("unused", "203.0.113.32/29")]
        )
        self.assertFalse(consistency.ok(c))
        self.assertEqual(res["actions"], [])  # shown, never repaired
        self.client.force_login(self.staff("sa9@corp.kz", is_super_admin=True))
        h = self.client.get(reverse("infra_site", args=["dc1"]) + "?tab=platforms").content.decode()
        self.assertIn("used on the platform, not in the address registry", h)

    def test_images_and_files_against_the_platform(self):
        """Owner 2026-11-11: the images and files quotas are compared too, datastore by datastore as the deployment sets
        them; VM disks exactly (the old "all the storage on the VM disks" is no longer the same)."""
        from django.utils import timezone

        from adoption.models import Observation, PlatformStats
        from tenants.models import QuotaRecord
        from tenants.quota_check import compare

        t = Tenant.objects.create(
            organization=self.org,
            name="proj2",
            title="P",
            approval="approved",
            group_name="_t_orgone_proj2",
            one_group_id=160,
            order={"platform": "dc1.cloud"},
        )
        QuotaRecord.objects.create(
            tenant=t,
            site="dc1",
            cluster="ca1",
            version=1,
            cpu=4,
            ram_gb=16,
            disk_gb=1100,
            images_gb=1000,
            files_gb=0,
            backups_gb=0,
        )
        PlatformStats.objects.create(
            platform_key="dc1.cloud",
            collected_at=timezone.now(),
            clusters={"0": "shared-std-1_dc1", "1": "other"},
            datastores=[
                {"id": 1, "type": "images", "clusters": ["shared-std-1_dc1"]},
                {"id": 9, "type": "images", "clusters": ["other"]},
                {"id": 2, "type": "files", "clusters": ["shared-std-1_dc1"]},
                {"id": 3, "type": "backups", "clusters": []},
            ],
        )
        q = {
            "VM_QUOTA": {
                "VM": [
                    {
                        "CLUSTER_IDS": "0",
                        "RUNNING_CPU": "4",
                        "RUNNING_MEMORY": "16384",
                        "RUNNING_PCI_DEV": "0",
                        "SYSTEM_DISK_SIZE": str(100 * 1024),
                    }
                ]
            },
            "DATASTORE_QUOTA": {
                "DATASTORE": [
                    {"ID": "1", "SIZE": str(20 * 1024)},
                    {"ID": "9", "SIZE": "-1"},
                    {"ID": "2", "SIZE": "0"},
                    {"ID": "3", "SIZE": "0"},
                ]
            },
        }
        o = Observation.objects.create(
            platform_key="dc1.cloud", group_id=160, group_name="_t_orgone_proj2", quotas=q, collected_at=timezone.now()
        )
        self.assertEqual(compare(t), ("diff", [("images, GB", 1000, "∞")]))  # another cluster's image datastore: open
        q["DATASTORE_QUOTA"]["DATASTORE"][1]["SIZE"] = "0"  # closed, as the deployment sets it (owner 2026-11-11)
        o.quotas = q
        o.save()
        self.assertEqual(compare(t), ("diff", [("images, GB", 1000, 20)]))
        q["DATASTORE_QUOTA"]["DATASTORE"][0]["SIZE"] = str(1000 * 1024)
        q["DATASTORE_QUOTA"]["DATASTORE"][2]["SIZE"] = "-1"
        o.quotas = q
        o.save()
        self.assertEqual(compare(t), ("diff", [("files, GB", 0, "∞")]))
        q["DATASTORE_QUOTA"]["DATASTORE"][2]["SIZE"] = "0"
        q["VM_QUOTA"]["VM"][0]["SYSTEM_DISK_SIZE"] = str(1100 * 1024)  # the old way: all the storage on the VM disks
        o.quotas = q
        o.save()
        self.assertEqual(compare(t), ("diff", [("VM disks, GB", 100, 1100)]))
        q["VM_QUOTA"]["VM"][0]["SYSTEM_DISK_SIZE"] = str(100 * 1024)
        o.quotas = q
        o.save()
        self.assertEqual(compare(t), ("ok", []))

    def test_two_pools_on_a_site(self):
        """Owner 2026-11-11: backups is one figure per site (every pool carries it, the platform gets it once, not the
        sum); images and files are set per pool and summed on the datastores the pools share; vCPU, RAM, GPU and VM
        disks are compared pool by pool."""
        from django.utils import timezone

        from adoption.models import Observation, PlatformStats
        from inventory.models import Cluster, Platform
        from tenants.deploy import quota_for
        from tenants.models import QuotaRecord
        from tenants.quota_check import compare

        Cluster.objects.create(
            platform=Platform.objects.get(key="dc1.cloud"), name="gpu-1_dc1", kind="shared", quota_code="ga1"
        )
        t = Tenant.objects.create(
            organization=self.org,
            name="proj3",
            title="P",
            approval="approved",
            group_name="_t_orgone_proj3",
            one_group_id=170,
            order={"platform": "dc1.cloud"},
        )
        QuotaRecord.objects.create(
            tenant=t,
            site="dc1",
            cluster="ca1",
            version=1,
            cpu=4,
            ram_gb=16,
            disk_gb=110,
            images_gb=10,
            files_gb=1,
            backups_gb=50,
        )
        QuotaRecord.objects.create(
            tenant=t,
            site="dc1",
            cluster="ga1",
            version=1,
            cpu=8,
            ram_gb=64,
            gpu=1,
            disk_gb=205,
            images_gb=5,
            files_gb=0,
            backups_gb=50,
        )
        q = quota_for(
            t,
            Platform.objects.get(key="dc1.cloud"),
            {"shared-std-1_dc1": 0, "gpu-1_dc1": 1},
            [(1, "0", {0, 1}), (2, "2", {0, 1}), (3, "3", set())],
        )
        self.assertEqual(
            {e["ID"]: e["SIZE"] // 1024 for e in q["DATASTORE"]}, {1: 15, 2: 1, 3: 50}
        )  # backups: 50, not 100
        self.assertEqual([(e["CLUSTER_IDS"], e["SYSTEM_DISK_SIZE"] // 1024) for e in q["VM"]], [("0", 99), ("1", 200)])
        PlatformStats.objects.create(
            platform_key="dc1.cloud",
            collected_at=timezone.now(),
            clusters={"0": "shared-std-1_dc1", "1": "gpu-1_dc1"},
            datastores=[
                {"id": 1, "type": "images", "clusters": ["shared-std-1_dc1", "gpu-1_dc1"]},
                {"id": 2, "type": "files", "clusters": ["shared-std-1_dc1", "gpu-1_dc1"]},
                {"id": 3, "type": "backups", "clusters": []},
            ],
        )
        vm = [
            {
                "CLUSTER_IDS": "0",
                "RUNNING_CPU": "4",
                "RUNNING_MEMORY": str(16 * 1024),
                "RUNNING_PCI_DEV": "0",
                "SYSTEM_DISK_SIZE": str(99 * 1024),
            },
            {
                "CLUSTER_IDS": "1",
                "RUNNING_CPU": "8",
                "RUNNING_MEMORY": str(64 * 1024),
                "RUNNING_PCI_DEV": "1",
                "SYSTEM_DISK_SIZE": str(200 * 1024),
            },
        ]
        ds = [{"ID": "1", "SIZE": str(15 * 1024)}, {"ID": "2", "SIZE": "1024"}, {"ID": "3", "SIZE": str(100 * 1024)}]
        o = Observation.objects.create(
            platform_key="dc1.cloud",
            group_id=170,
            group_name="_t_orgone_proj3",
            quotas={"VM_QUOTA": {"VM": vm}, "DATASTORE_QUOTA": {"DATASTORE": ds}},
            collected_at=timezone.now(),
        )
        self.assertEqual(compare(t), ("diff", [("backups, GB", 50, 100)]))  # the old sum is a difference
        ds[2]["SIZE"] = str(50 * 1024)
        vm[0]["SYSTEM_DISK_SIZE"], vm[1]["SYSTEM_DISK_SIZE"] = str(200 * 1024), str(99 * 1024)  # same total, swapped
        o.quotas = {"VM_QUOTA": {"VM": vm}, "DATASTORE_QUOTA": {"DATASTORE": ds}}
        o.save()
        self.assertEqual(compare(t), ("diff", [("ca1: VM disks, GB", 99, 200), ("ga1: VM disks, GB", 200, 99)]))
        vm[0]["SYSTEM_DISK_SIZE"], vm[1]["SYSTEM_DISK_SIZE"] = str(99 * 1024), str(200 * 1024)
        o.quotas = {"VM_QUOTA": {"VM": vm}, "DATASTORE_QUOTA": {"DATASTORE": ds}}
        o.save()
        self.assertEqual(compare(t), ("ok", []))

    def test_usage_next_to_the_quota_and_tenant_tracking(self):
        """Correction 11: the tenant's resources show what is in use (per cluster, from the platform) next to the quota;
        the tenant has its own resource tracking page."""
        from django.utils import timezone

        from adoption.models import Observation, PlatformStats
        from inventory.models import Cluster

        t, p = self._gpu_tenant()
        ca1 = Cluster.objects.filter(platform=p, quota_code="ca1").order_by("pk").first().name
        PlatformStats.objects.create(
            platform_key="dc1.cloud",
            collected_at=timezone.now(),
            clusters={"0": ca1, "5": "h200_dc1"},
            datastores=[{"id": 3, "type": "backups"}, {"id": 1, "type": "images"}],
        )
        q = {
            "VM_QUOTA": {
                "VM": [
                    {
                        "CLUSTER_IDS": "0",
                        "RUNNING_CPU_USED": "3",
                        "RUNNING_MEMORY_USED": "6144",
                        "RUNNING_PCI_DEV_USED": "0",
                        "SYSTEM_DISK_SIZE_USED": "12288",
                    },
                    {
                        "CLUSTER_IDS": "5",
                        "RUNNING_CPU_USED": "24",
                        "RUNNING_MEMORY_USED": str(250 * 1024),
                        "RUNNING_PCI_DEV_USED": "1",
                        "SYSTEM_DISK_SIZE_USED": "0",
                    },
                ]
            },
            "DATASTORE_QUOTA": {"DATASTORE": [{"ID": "3", "SIZE_USED": "2048"}, {"ID": "1", "SIZE_USED": "5120"}]},
        }
        Observation.objects.create(
            platform_key="dc1.cloud", group_id=161, group_name="tenant-gpu1", quotas=q, collected_at=timezone.now()
        )
        self.client.force_login(self.rm)
        h = self.client.get(reverse("tenant_detail", args=[t.pk])).content.decode()
        # owner 2026-11-09: the quota leads in bold, what is in use follows it smaller and lighter, no slash
        self.assertIn('<b>8</b> <span class="qu">3</span>', h)  # vCPU: quota 8, in use 3 on the CPU cluster
        self.assertIn('<b>32</b> <span class="qu">6</span>', h)  # RAM, GB
        self.assertIn('GPU <b>1</b> <span class="qu">1</span>', h)  # the GPU bundles on the GPU cluster
        self.assertIn('<b>50</b> <span class="qu">2</span>', h)  # backups, GB (the site's backup datastore)
        self.assertIn('<b>0</b> <span class="qu">5</span>', h)  # images, GB: none set, 5 in use
        self.assertIn('class="ub bad"', h)  # the GPU pool runs at 24/24 vCPU: the fill bar is red
        r = self.client.get(reverse("tenant_tracking", args=[t.pk]))
        self.assertContains(r, "Resource tracking")
        self.assertContains(r, "gpu1")
        self.assertContains(
            self.client.get(reverse("tenant_detail", args=[t.pk])), "Resource change history"
        )  # u09: inline on the page

    def _usage_of_gpu_tenant(self):
        from django.utils import timezone

        from adoption.models import Observation, PlatformStats
        from inventory.models import Cluster

        t, p = self._gpu_tenant()
        ca1 = Cluster.objects.filter(platform=p, quota_code="ca1").order_by("pk").first().name
        PlatformStats.objects.create(
            platform_key="dc1.cloud",
            collected_at=timezone.now(),
            clusters={"0": ca1, "5": "h200_dc1"},
            datastores=[{"id": 3, "type": "backups"}, {"id": 1, "type": "images"}],
        )
        vm = [
            {
                "CLUSTER_IDS": "0",
                "RUNNING_CPU_USED": "6",
                "RUNNING_MEMORY_USED": "20480",
                "SYSTEM_DISK_SIZE_USED": "102400",
            },
            {
                "CLUSTER_IDS": "5",
                "RUNNING_CPU_USED": "24",
                "RUNNING_MEMORY_USED": str(250 * 1024),
                "RUNNING_PCI_DEV_USED": "1",
            },
        ]
        ds = [{"ID": "3", "SIZE_USED": str(40 * 1024)}, {"ID": "1", "SIZE_USED": "0"}]
        Observation.objects.create(
            platform_key="dc1.cloud",
            group_id=161,
            group_name="tenant-gpu1",
            quotas={"VM_QUOTA": {"VM": vm}, "DATASTORE_QUOTA": {"DATASTORE": ds}},
            collected_at=timezone.now(),
        )
        return t

    def test_a_quota_change_never_goes_below_what_is_in_use(self):
        """Owner 2026-11-12: "add check when quota changes to disable values below real usage"."""
        from tenants.models import ResourceChange
        from tenants.quota_check import below_usage, floor

        t = self._usage_of_gpu_tenant()
        rec = next(r for r in t.current_quota_records() if r.cluster == "ca1")
        self.assertEqual(floor(t, rec)["cpu"], 6)
        found = below_usage(t, "dc1", "ca1", {"cpu": 4, "ram_gb": 32, "gpu": 0, "disk_gb": 200})
        self.assertEqual([w for w, _n, _u in found], ["vCPU"])
        found = below_usage(t, "dc1", "ca1", {"cpu": 8, "ram_gb": 16, "gpu": 0, "disk_gb": 50})
        self.assertEqual([w for w, _n, _u in found], ["RAM, GB", "VM disks, GB"])
        self.assertEqual(below_usage(t, "dc1", "ca1", {"cpu": 6, "ram_gb": 20, "gpu": 0, "disk_gb": 200}), [])
        self.client.force_login(self.rm)
        url = reverse("tenant_resource_request", args=[t.pk]) + "?site=dc1&cluster=ca1"
        r = self.client.post(url, {"site": "dc1", "cluster": "ca1", "cpu": 4, "ram_gb": 32, "disk_gb": 200})
        self.assertContains(r, "Not below what is in use now: 6.")
        self.assertFalse(ResourceChange.objects.filter(tenant=t).exists())
        self.assertContains(self.client.get(url), 'min="6"')
        # the GPU pool runs its one GPU: it is not removed under it
        # owner 2026-11-12: a pool with VMs can go, once the VMs' deletion is confirmed (tests below)
        from unittest import mock

        from tenants.deploy import DeployError

        with mock.patch("tenants.poolvms.pool_vms", side_effect=DeployError("no answer")):
            r = self.client.post(reverse("tenant_pool_remove", args=[t.pk]), {"site": "dc1", "cluster": "gh1"})
        self.assertContains(r, "Delete these VMs with the pool")  # the reading saw usage: confirm first
        self.assertFalse(ResourceChange.objects.filter(tenant=t).exists())
        # backups: 40 GB in use on the site
        r = self.client.post(reverse("tenant_backups_request", args=[t.pk]), {"site": "dc1", "backups_gb": 30})
        self.assertContains(r, "in use 40")
        self.assertFalse(ResourceChange.objects.filter(tenant=t).exists())
        self.client.post(reverse("tenant_backups_request", args=[t.pk]), {"site": "dc1", "backups_gb": 60})
        self.assertTrue(ResourceChange.objects.filter(tenant=t, backups_gb=60).exists())

    def test_a_pool_with_vms_is_removed_with_them_once_confirmed(self):
        """Owner 2026-11-12: "resource pool removal - can be allowed even vms exists / if any vm exists warn again and
        ask to confirm vms deletion"."""
        from unittest import mock

        from tenants.models import ResourceChange

        t = self._usage_of_gpu_tenant()
        vms = [{"platform": "dc1.cloud", "id": 7, "name": "train-1"}]
        self.client.force_login(self.rm)
        url = reverse("tenant_pool_remove", args=[t.pk])
        with mock.patch("tenants.poolvms.pool_vms", return_value=vms):
            r = self.client.post(url, {"site": "dc1", "cluster": "gh1"})
            self.assertContains(r, "train-1")
            self.assertFalse(ResourceChange.objects.exists())  # nothing asked before the confirmation
            self.client.post(url, {"site": "dc1", "cluster": "gh1", "delete_vms": "1"})
        ch = ResourceChange.objects.get(tenant=t)
        self.assertEqual((ch.pool["kind"], ch.pool["delete_vms"], ch.pool["vms"]), ("remove", True, ["train-1"]))
        self.assertContains(self.client.get(reverse("tenant_detail", args=[t.pk])), "its VMs are deleted on approval")
        with (
            mock.patch("tenants.poolvms.pool_vms", return_value=vms),
            mock.patch("tenants.poolvms.delete_pool_vms", return_value=["train-1"]) as gone,
            mock.patch("tenants.deploy.apply_quota", return_value="quota set"),
        ):
            self.client.post(reverse("tenant_resource_decide", args=[t.pk, ch.pk, "approve"]))
        ch.refresh_from_db()
        self.assertEqual(ch.state, ResourceChange.State.APPROVED)
        gone.assert_called_once()
        self.assertIn("VMs deleted: train-1", ch.applied)
        self.assertEqual([r.cluster for r in t.current_quota_records()], ["ca1"])

    def test_pool_vms_are_the_groups_vms_on_that_cluster_and_never_its_gateways(self):
        """What a confirmed removal deletes: decided here, so tested against a platform's own answers."""
        import xml.etree.ElementTree as ET
        from unittest import mock

        from inventory.models import Cluster
        from tenants.poolvms import pool_vms

        t, p = self._gpu_tenant()
        cpu = Cluster.objects.filter(platform=p, quota_code="ca1").order_by("pk").first().name

        def vm(i, name, gid, cid):
            return f"<VM><ID>{i}</ID><NAME>{name}</NAME><GID>{gid}</GID><HISTORY_RECORDS><HISTORY><CID>0</CID></HISTORY><HISTORY><CID>{cid}</CID></HISTORY></HISTORY_RECORDS></VM>"

        answers = {
            "one.clusterpool.info": f"<C><CLUSTER><ID>0</ID><NAME>{cpu}</NAME></CLUSTER><CLUSTER><ID>5</ID><NAME>h200_dc1</NAME></CLUSTER></C>",
            "one.vmpool.info": "<P>"
            + vm(1, "train-1", 161, 5)  # the project's VM on the GPU cluster: this one
            + vm(2, "web-1", 161, 0)  # the project's VM on the CPU cluster
            + vm(3, "other", 99, 5)  # another group's VM on the GPU cluster
            + vm(4, "tenant_gpu1-igw_dc1", 161, 5)  # the project's gateway: never
            + "</P>",
        }

        class FakeOne:
            def __init__(self, control):
                pass

            def xml(self, method, *args):
                return ET.fromstring(answers[method])

            def find(self, *args):
                return ET.fromstring("<GROUP><ID>161</ID><NAME>tenant-gpu1</NAME></GROUP>")

        with (
            mock.patch("tenants.poolvms.One", FakeOne),
            mock.patch("inventory.models.PlatformControl.configured", True),
        ):
            self.assertEqual([v["name"] for v in pool_vms(t, "dc1", "gh1")], ["train-1"])
            self.assertEqual([v["name"] for v in pool_vms(t, "dc1", "ca1")], ["web-1"])

    def test_a_pool_without_vms_goes_without_the_question(self):
        from unittest import mock

        from tenants.models import ResourceChange

        t = self._usage_of_gpu_tenant()
        self.client.force_login(self.rm)
        with mock.patch("tenants.poolvms.pool_vms", return_value=[]):
            self.client.post(reverse("tenant_pool_remove", args=[t.pk]), {"site": "dc1", "cluster": "gh1"})
        self.assertFalse(ResourceChange.objects.get(tenant=t).pool["delete_vms"])

    def test_an_approver_cannot_set_a_change_below_the_usage(self):
        from tenants.models import ResourceChange

        t = self._usage_of_gpu_tenant()
        rec = next(r for r in t.current_quota_records() if r.cluster == "ca1")
        ch = ResourceChange.objects.create(
            tenant=t,
            site="dc1",
            cluster="ca1",
            requested_by=self.rm,
            cpu=10,
            ram_gb=32,
            disk_gb=200,
            images_gb=0,
            files_gb=0,
            backups_gb=50,
        )
        self.client.force_login(self.rm)
        r = self.client.post(reverse("tenant_resource_decide", args=[t.pk, ch.pk, "approve"]), {"cpu": 2}, follow=True)
        self.assertContains(r, "vCPU 2, in use 6")
        ch.refresh_from_db()
        self.assertEqual(ch.state, ResourceChange.State.PENDING)
        self.assertEqual(
            t.current_quota_records()[0].cpu if t.current_quota_records()[0].cluster == "ca1" else rec.cpu, 8
        )

    def _gpu_tenant(self, origin="portal"):
        from inventory.models import Cluster, Platform, PlatformControl, Site
        from tenants.models import QuotaRecord

        st = Site.objects.get_or_create(code="dc1")[0]
        p = Platform.objects.get_or_create(key="dc1.cloud", defaults={"site": st, "name": "cloud", "kind": "shared"})[0]
        if not Cluster.objects.filter(platform=p, quota_code="ca1").exists():
            Cluster.objects.create(platform=p, name="cpu_dc1", kind="shared", quota_code="ca1")
        Cluster.objects.create(platform=p, name="h200_dc1", kind="shared", quota_code="gh1")
        PlatformControl.objects.get_or_create(key="dc1.cloud", defaults={"mode": "execution"})
        PlatformControl.objects.filter(key="dc1.cloud").update(mode="execution")
        t = Tenant.objects.create(
            organization=self.org,
            name="gpu1",
            title="G",
            approval="approved",
            origin=origin,
            group_name="tenant-gpu1",
            one_group_id=161,
            order={"platform": "dc1.cloud", "cluster": "ca1"},
        )
        QuotaRecord.objects.create(
            tenant=t, site="dc1", cluster="ca1", version=1, cpu=8, ram_gb=32, disk_gb=200, backups_gb=50
        )
        QuotaRecord.objects.create(
            tenant=t, site="dc1", cluster="gh1", version=1, cpu=24, ram_gb=250, gpu=1, disk_gb=500, backups_gb=0
        )
        return t, p

    def test_quota_for_every_cluster_of_the_records(self):
        """2026-10-27: a tenant with a CPU and a GPU cluster gets both quotas; the platform's other clusters are reset."""
        from tenants.deploy import quota_for
        from tenants.models import split_of

        t, p = self._gpu_tenant()
        from inventory.models import Cluster

        cpu = Cluster.objects.filter(platform=p, quota_code="ca1").first().name
        clusters = {cpu: 0, "h200_dc1": 5, "other_dc1": 7}
        datastores = [(1, "0", {0}), (2, "0", {5}), (3, "3", set()), (4, "2", set())]
        q = quota_for(t, p, clusters, datastores)
        by = {e["CLUSTER_IDS"]: e for e in q["VM"]}
        self.assertEqual((by["0"]["RUNNING_CPU"], by["0"]["RUNNING_MEMORY"], by["0"]["PCI_DEV"]), (8, 32 * 1024, 0))
        self.assertEqual(
            (by["5"]["RUNNING_CPU"], by["5"]["RUNNING_MEMORY"], by["5"]["RUNNING_PCI_DEV"]), (24, 250 * 1024, 1)
        )
        self.assertEqual(by["7"]["RUNNING_CPU"], -1)
        recs = {r.cluster: r for r in t.current_quota_records()}
        ds = {d["ID"]: d["SIZE"] for d in q["DATASTORE"]}
        self.assertEqual((ds[1], ds[2]), (split_of(recs["ca1"])[1] * 1024, split_of(recs["gh1"])[1] * 1024))
        self.assertEqual(ds[3], 50 * 1024)  # backups: the records' sum

    def test_set_quota_on_an_adopted_tenant_changes_only_its_quota(self):
        from inventory.models import Cluster
        from tenants import deploy as dp

        t, p = self._gpu_tenant()
        cpu = Cluster.objects.filter(platform=p, quota_code="ca1").first().name
        calls = []

        class FakeOne:
            def __init__(self, control):
                pass

            def xml(self, method, *a):
                import xml.etree.ElementTree as ET

                if method == "one.clusterpool.info":
                    return ET.fromstring(
                        f"<P><CLUSTER><ID>0</ID><NAME>{cpu}</NAME></CLUSTER>"
                        "<CLUSTER><ID>5</ID><NAME>h200_dc1</NAME></CLUSTER></P>"
                    )
                return ET.fromstring(
                    "<P><DATASTORE><ID>1</ID><TYPE>0</TYPE><CLUSTERS><ID>0</ID><ID>5</ID></CLUSTERS></DATASTORE>"
                    "<DATASTORE><ID>3</ID><TYPE>3</TYPE><CLUSTERS/></DATASTORE></P>"
                )

            def call(self, method, *a):
                calls.append((method, a))

        with mock.patch.object(dp, "One", FakeOne), mock.patch("inventory.models.PlatformControl.configured", True):
            report = dp.apply_quota(t, self.rm)
        self.assertEqual(
            [c[0] for c in calls], ["one.group.quota"]
        )  # no group template change for a tenant-<name> group
        self.assertEqual(calls[0][1][0], 161)
        self.assertIn("24 vCPU", report)
        self.assertIn("8 vCPU", report)

    def test_only_platform_users_get_mail(self):
        from django.core.mail import EmailMessage

        from accounts.appliance import hold_client_mail
        from accounts.models import AuditLog, Kind, User

        User.objects.create_user("ops@provider.example", "Str0ng-PASS-123", kind=Kind.STAFF, email_verified=True)
        User.objects.create_user("adm@client.kz", "Str0ng-PASS-123", kind=Kind.CUSTOMER, email_verified=True)
        both = EmailMessage("s", "b", "p@x", ["Ops@provider.example", "adm@client.kz"])
        client = EmailMessage("s2", "b", "p@x", ["adm@client.kz", "x@y.kz"])
        kept = hold_client_mail([both, client])
        self.assertEqual([m.to for m in kept], [["Ops@provider.example"]])
        self.assertEqual(AuditLog.objects.filter(action="mail_held").count(), 2)
        # a message the owner allowed passes the backend's hold (the marker header is removed)
        from unittest import mock as _mock

        from accounts.appliance import EmailBackend

        allowed = EmailMessage("s3", "b", "p@x", ["adm@client.kz"], headers={"X-Portal-Allow-Client": "1"})
        with (
            _mock.patch("accounts.ha.is_active", return_value=True),
            _mock.patch(
                "django.core.mail.backends.smtp.EmailBackend.send_messages", side_effect=lambda ms: len(ms)
            ) as smtp,
        ):
            EmailBackend().send_messages([allowed, EmailMessage("s4", "b", "p@x", ["x@y.kz"])])
        sent = smtp.call_args[0][0]
        self.assertEqual([m.subject for m in sent], ["s3"])
        self.assertNotIn("X-Portal-Allow-Client", sent[0].extra_headers)


class MailHtmlCopy(TestCase):
    def test_links_keep_their_text(self):
        """Owner 2026-10-29: the platform's URL is seen as it is, also behind a link-rewriting mail filter."""
        from django.core.mail import EmailMessage

        from accounts.appliance import with_html

        m = with_html(
            EmailMessage("s", "Web UI: https://one-dc2.provider.example:55516/?a=1&b=2\n<x>", "p@x", ["a@b.kz"])
        )
        html = m.alternatives[0][0]
        self.assertIn(
            '<a href="https://one-dc2.provider.example:55516/?a=1&amp;b=2" style="color:#0f6b55">https://one-dc2.provider.example:55516/?a=1&amp;b=2</a>',
            html,
        )
        self.assertIn("&lt;x&gt;", html)
        self.assertIn("Example Cloud", html)  # the portal's frame
        from accounts.mailhtml import body_html

        h = body_html(
            "Hello.\n\nTenant: kd\nOrganization: KD\nSales rep: Maxim S <m.s@sales.example>\n\nhttps://my.provider.example/tenants/1/\n\nAsk a@b.kz"
        )
        self.assertIn('<a href="mailto:m.s@sales.example" style="color:#0f6b55">Maxim S</a>', h)
        self.assertIn("<li>Tenant: <strong>kd</strong></li>", h)
        self.assertIn(">Open</a>", h)
        self.assertIn('href="mailto:a@b.kz"', h)
        self.assertEqual(m.body, "Web UI: https://one-dc2.provider.example:55516/?a=1&b=2\n<x>")


class Correction16Refuse(Base):
    def test_refused_registration_is_removed(self):
        rm = self.staff("rm@corp.kz", is_resource_manager=True)
        tax = valid_bin()
        self.register("x@firm.kz", tax)
        self.client.get(link_path(mail.outbox[-1]))
        org = Organization.objects.get(tax_id=tax)
        self.client.force_login(rm)
        self.client.post(reverse("org_registration", args=[org.pk, "reject"]), {"reason": "not a client"})
        self.assertFalse(Organization.objects.filter(tax_id=tax).exists())
        self.assertFalse(User.objects.filter(email="x@firm.kz").exists())
        self.assertEqual(mail.outbox[-1].to, ["x@firm.kz"])
        self.assertIn("not a client", mail.outbox[-1].body)

    def test_settings_lists_people_and_expiry_switch(self):
        sa = self.staff("sa@corp.kz", is_super_admin=True)
        User.objects.filter(pk=self.staff("rm@corp.kz", is_resource_manager=True).pk).update(full_name="Gaisha N")
        self.client.force_login(sa)
        r = self.client.get(reverse("portal_settings") + "?tab=platform")
        self.assertContains(r, 'class="usedby"')  # owner 2026-11-12: the Approvers column opens the list
        self.assertContains(r, "Gaisha N")
        self.client.post(
            reverse("portal_settings"), {"tab": "mail", "section": "notify_clients", "notify_clients": "on"}
        )
        self.assertTrue(PortalSettings.get().notify_clients)

    def test_two_super_admins_removal_needs_two(self):
        """owner u12: with two super admins, removing one needs two to agree; the one being removed cannot confirm
        their own removal, so it waits (the other role changes still apply at once)."""
        from accounts.models import SuperAdminRemoval

        a = self.staff(
            "a@corp.kz", is_super_admin=True, is_resource_manager=True, is_support=True, is_sales=True, is_kam=True
        )
        b = self.staff("b@corp.kz", is_super_admin=True)
        self.client.force_login(a)
        self.client.post(reverse("person_roles", args=[b.pk]), {"roles": ["is_support"]})
        b.refresh_from_db()
        self.assertTrue(b.is_super_admin)  # stays — a second approval is needed and not available
        self.assertTrue(b.is_support)  # the other role change applied at once
        self.assertEqual(SuperAdminRemoval.objects.get().state, "pending")


class ProviderBrand(Base):
    """Owner 2026-10-29 16:00: the provider's name and logo in Settings."""

    def _img(self, fmt, name):
        buf = io.BytesIO()
        Image.new("RGB", (2400, 400), "navy").save(buf, fmt)
        return SimpleUploadedFile(name, buf.getvalue(), content_type=f"image/{fmt.lower()}")

    def test_name_and_logo(self):
        sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(sa)
        self.client.post(
            reverse("portal_settings"),
            {
                "tab": "provider",
                "section": "provider",
                "provider_name": "Acme Cloud",
                "logo": self._img("JPEG", "l.jpg"),
            },
        )
        s = PortalSettings.get()
        self.assertEqual(s.provider_name, "Acme Cloud")
        self.assertTrue(bytes(s.logo).startswith(b"\x89PNG"))
        self.assertEqual(Image.open(io.BytesIO(bytes(s.logo))).size, (1200, 200))
        r = self.client.get(reverse("dashboard"))
        self.assertContains(r, 'class="brand-logo"')
        self.assertContains(r, "Acme Cloud")
        self.client.logout()
        r = self.client.get(reverse("brand_logo"))
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "image/png"))
        # anything but PNG or JPEG is refused
        self.client.force_login(sa)
        r = self.client.post(
            reverse("portal_settings"),
            {
                "tab": "provider",
                "section": "provider",
                "provider_name": "Acme Cloud",
                "logo": SimpleUploadedFile("l.svg", b"<svg/>", content_type="image/svg+xml"),
            },
            follow=True,
        )
        self.assertContains(r, "a PNG or JPEG image")
        # the e-mails carry the name
        from accounts import ha
        from accounts.utils import notify

        with (
            mock.patch.object(ha, "is_active", return_value=True),
            mock.patch(
                "django.core.mail.backends.smtp.EmailBackend.send_messages", side_effect=lambda ms: len(ms)
            ) as smtp,
            override_settings(EMAIL_BACKEND="accounts.appliance.EmailBackend"),
        ):
            notify(
                ["sa@corp.kz"],
                "org_registration_approved",
                {"org": Organization(name="X"), "link": "http://x"},
                "Hello",
            )
        m = smtp.call_args[0][0][0]
        self.assertEqual(m.subject, "[Acme Cloud] Hello")
        self.assertIn("Acme Cloud portal", m.body)
        self.assertIn('src="cid:provider-logo"', m.alternatives[0][0])  # the logo travels with the message
        raw = m.message().as_string()
        self.assertIn("Content-ID: <provider-logo>", raw)
        self.assertIn("multipart/related", raw)


class Corrections18Orgs(Base):
    def test_type_locked_and_corporate_sales_rep(self):
        from tenants.forms import OrganizationForm

        rm = self.staff("rm@corp.kz", is_resource_manager=True)
        se = self.staff("se@corp.kz", is_support=True)
        out = self.staff("rep@gmail.com", is_sales=True)
        inside = self.staff("rep@corp.example", is_sales=True)
        PortalSettings.objects.update_or_create(pk=1, defaults={"corporate_domains": "corp.example"})
        org = Organization.objects.create(
            name="Corp", kind="corp", tax_id=valid_bin(), resource_manager=rm, support_engineer=se
        )
        base = {
            "name": "Corp",
            "kind": "corp",
            "tax_id_type": "bin",
            "tax_id": org.tax_id,
            "resource_manager": rm.pk,
            "support_engineer": se.pk,
        }
        f = OrganizationForm(data={**base, "sales_rep": out.pk}, instance=org)
        self.assertFalse(f.is_valid())
        self.assertIn("sales_rep", f.errors)
        self.assertTrue(OrganizationForm(data={**base, "sales_rep": inside.pk}, instance=org).is_valid())
        self.assertTrue(OrganizationForm(data=base, instance=org).is_valid())  # optional
        Tenant.objects.create(organization=org, name="t1", title="t1", kind="corp")
        f = OrganizationForm(data={**base, "kind": "cloud", "sales_rep": inside.pk}, instance=org)
        self.assertTrue(f.fields["kind"].disabled)
        # a corporate tenant's admin may be a platform user (of a corporate e-mail domain), when flagged eligible
        from tenants.admins import admin_candidates

        self.assertNotIn(inside, admin_candidates(org))  # owner 2026-11-03: not until flagged may_tenant_admin
        inside.may_tenant_admin = True
        inside.save()
        self.assertIn(inside, admin_candidates(org))
        self.assertNotIn(rm, admin_candidates(org))


class Corrections18Settings(Base):
    def test_tabs_timers_and_mail_templates(self):
        from accounts import timers
        from accounts.models import MailTemplate

        sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(sa)
        url = reverse("portal_settings")
        r = self.client.get(url)
        for label in ("Provider", "Platform", "Security", "Appearance", "Mail", "Reports", "Backups", "Portal"):
            self.assertContains(r, f">{label}</a>")
        self.assertNotContains(r, ">Publishing</a>")
        self.assertContains(r, "Corporate email domains")  # under Provider now
        self.assertContains(r, "Public names")
        self.assertNotContains(self.client.get(url + "?tab=security"), "Network access")
        r = self.client.get(url + "?tab=data")  # the old name: Appearance now
        for key in ("portal", "platform", "ipmi", "vyos"):
            self.assertContains(r, f'name="minutes_{key}"')
        self.assertEqual([timers.state(k)["minutes"] for k in timers.KEYS], [10, 30, 240, 480, 1440, 5])
        self.client.post(url, {"tab": "data", "section": "timers", "minutes_ipmi": "120"})
        self.assertEqual(timers.state("ipmi")["minutes"], 120)
        with mock.patch.dict(timers.FUNCS, {"portal": lambda: (True, "all good")}):
            self.assertEqual(timers.run_due().get("portal"), (True, "all good"))
            self.assertFalse(timers.due("portal"))
            self.client.post(url, {"tab": "data", "section": "timers", "refresh": "portal"})
        self.assertEqual(timers.state("portal")["state"], "ok")
        # an edited e-mail template is used until reset
        r = self.client.get(reverse("mail_template", args=["org_assigned.txt"]))
        self.assertContains(r, "You are now the sales rep")
        self.client.post(reverse("mail_template", args=["org_assigned.txt"]), {"body": "{% if %}broken"})
        self.assertFalse(MailTemplate.objects.exists())
        self.client.post(
            reverse("mail_template", args=["org_assigned.txt"]), {"body": "Hi! {{ org.name }} is yours.\n{{ link }}"}
        )
        from accounts.utils import notify

        mail.outbox.clear()
        notify(["sa@corp.kz"], "org_assigned", {"org": Organization(name="Acme"), "link": "http://x"}, "Assigned")
        self.assertIn("Hi! Acme is yours.", mail.outbox[-1].body)
        self.client.post(reverse("mail_template", args=["org_assigned.txt"]), {"reset": "1"})
        self.assertFalse(MailTemplate.objects.exists())
        # Mail and Backups save their own fields
        from tenants.models import PortalSettings

        r = self.client.post(
            url,
            {
                "tab": "mail",
                "section": "appliance",
                "part": "mail",
                "mail_mode": "external",
                "mail_from": "",
                "mail_host": "smtp.example.kz",
                "mail_port": 587,
                "mail_user": "",
            },
        )
        s = PortalSettings.get()
        self.assertEqual((s.mail_mode, s.mail_host), ("external", "smtp.example.kz"))  # no backup password needed here


class SuperAdminEditsUninvited(Base):
    def test_edit_an_uninvited_organization_user(self):
        sa = self.staff("sa@corp.kz", is_super_admin=True)
        org = Organization.objects.create(name="Acme", tax_id=valid_bin())
        u = User.objects.create_user("x@acme.kz", None, kind=Kind.CUSTOMER, organization=org, login_enabled=False)
        ops = self.staff("ops@corp.kz", is_support=True, totp=False)
        User.objects.filter(pk=ops.pk).update(login_enabled=False)
        self.client.force_login(sa)
        self.client.post(
            reverse("person_edit", args=[u.pk]),
            {"email": "boss@acme.kz", "full_name": "Aigerim B", "phone": "+7 701 1234567", "city": "almaty"},
        )
        u.refresh_from_db()
        self.assertEqual(
            (u.email, u.full_name, u.phone, u.city), ("boss@acme.kz", "Aigerim B", "+77011234567", "almaty")
        )
        self.assertEqual(
            self.client.post(
                reverse("person_edit", args=[ops.pk]), {"email": "o@corp.kz", "full_name": "O"}
            ).status_code,
            403,
        )
        # roles of the uninvited user (owner 20:08)
        t = Tenant.objects.create(organization=org, name="t1", title="t1", kind="cloud")
        r = self.client.get(reverse("staff_users") + "?tab=tenant")
        self.assertContains(r, reverse("person_role_set", args=[u.pk]))  # u10: roles edited inline, even while inactive
        self.client.post(reverse("person_role_set", args=[u.pk]), {"role": "org_admin", "on": "1"})
        self.client.post(reverse("person_role_set", args=[u.pk]), {"role": "tenant_admin", "tenants": [t.pk]})
        u.refresh_from_db()
        self.assertTrue(u.is_org_admin)
        self.assertTrue(u.memberships.filter(tenant=t).exists())
        # owner 2026-11-12: the city is typed in; a user made without one has none
        self.assertEqual(
            User.objects.create_user("n@acme.kz", None, kind=Kind.CUSTOMER).city, ""
        )  # 2026-11-12: typed in
        self.client.post(
            reverse("person_edit", args=[u.pk]), {"email": "boss@acme.kz", "full_name": "Aigerim B", "city": ""}
        )
        u.refresh_from_db()
        self.assertEqual(u.city, "almaty")
