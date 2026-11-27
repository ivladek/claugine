"""Countries (owner 2026-11-12): an organization has a country; Settings → Provider sets the home country and the
allowed ones; the tax ID follows the country (KZ IIN/BIN, AE TRN, else free) and is required in any case."""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts import taxid
from accounts.forms import RegistrationForm
from accounts.models import User
from accounts.tests import PROFILE, Base, photo, valid_bin
from tenants.models import Organization, PortalSettings, Registration


class TaxIds(TestCase):
    def test_per_country(self):
        b = valid_bin()
        self.assertEqual(taxid.normalize("KZ", b), (b, "bin"))
        from accounts.kz import tax_id_checksum_ok

        bad_month = next(  # 12 digits, a correct check digit, 5th digit 0 (so "IIN"), month 13: neither IIN nor BIN
            f"9913{n:07d}{d}" for n in range(1000) for d in range(10) if tax_id_checksum_ok(f"9913{n:07d}{d}")
        )
        with self.assertRaises(ValidationError):
            taxid.normalize("KZ", bad_month)
        with self.assertRaises(ValidationError):
            taxid.normalize("KZ", "123456789012")
        self.assertEqual(taxid.normalize("AE", "100-1234-5678-9003"), ("100123456789003", "trn"))
        with self.assertRaises(ValidationError):
            taxid.normalize("AE", "12345", "trn")
        # owner 2026-11-12: a UAE company may register with its trade licence number (ADGM 000036750, CN-1234567)
        self.assertEqual(taxid.normalize("AE", "000036750", "lic"), ("000036750", "lic"))
        self.assertEqual(taxid.normalize("AE", "cn-1234567", "lic"), ("CN-1234567", "lic"))
        self.assertEqual(taxid.normalize("AE", "36750"), ("36750", "lic"))  # no kind given: not 15 digits
        with self.assertRaises(ValidationError):
            taxid.normalize("AE", "36750", "trn")
        with self.assertRaises(ValidationError):
            taxid.normalize("AE", "x", "lic")
        self.assertEqual(taxid.normalize("DE", "de 123.456/789"), ("DE123456789", "tin"))
        with self.assertRaises(ValidationError):
            taxid.normalize("DE", "x")
        for c in ("KZ", "AE", "DE"):
            with self.assertRaises(ValidationError):
                taxid.normalize(c, "  ")

    def test_unique_per_country(self):
        Organization.objects.create(name="A", country="AE", tax_id="100123456789003")
        Organization.objects.create(name="B", country="SA", tax_id="100123456789003")  # another country: allowed
        with self.assertRaises(IntegrityError), transaction.atomic():
            Organization.objects.create(name="C", country="AE", tax_id="100123456789003")

    def test_existing_default_to_kazakhstan(self):
        self.assertEqual(Organization.objects.create(name="Old").country, "KZ")
        conf = PortalSettings.get()
        self.assertEqual((conf.home_country, conf.countries()), ("KZ", ["KZ"]))


class Countries(Base):
    def post(self, email, country, tax_id, tax_type=""):
        return self.client.post(
            reverse("register"),
            {
                **PROFILE,
                "email": email,
                "photo": photo(),
                "organization_name": "Emirates LLC",
                "country": country,
                "tax_id_type": tax_type,
                "tax_id": tax_id,
            },
        )

    def allow(self, *codes):
        PortalSettings.objects.update_or_create(pk=1, defaults={"allowed_countries": list(codes)})

    def test_only_allowed_countries(self):
        self.assertEqual([c for c, _n in RegistrationForm().fields["country"].choices], ["KZ"])
        r = self.post("a@emirates.ae", "AE", "100123456789003")
        self.assertFalse(Registration.objects.exists())
        self.assertContains(r, "Select a valid choice")
        self.allow("AE")
        self.assertEqual([c for c, _n in RegistrationForm().fields["country"].choices], ["KZ", "AE"])

    def test_emirates_registration(self):
        self.allow("AE", "DE")
        r = self.post("a@emirates.ae", "AE", "12345", "trn")
        self.assertContains(r, "The TRN has 15 digits")
        self.post("a@emirates.ae", "AE", "100 1234 5678 9003")
        reg = Registration.objects.get()
        self.assertEqual((reg.country, reg.tax_id, reg.tax_id_type), ("AE", "100123456789003", "trn"))
        self.post("c@aleroy.ai", "AE", "36750", "lic")
        reg = Registration.objects.get(user__email="c@aleroy.ai")
        self.assertEqual((reg.country, reg.tax_id, reg.tax_id_type), ("AE", "36750", "lic"))
        r = self.post("b@firma.de", "DE", "")
        self.assertContains(r, "required")

    def test_kazakhstan_by_default(self):
        """An old form without the country: the home country, and the BIN is still checked."""
        r = self.client.post(
            reverse("register"),
            {**PROFILE, "email": "o@r.kz", "photo": photo(), "organization_name": "ТОО", "tax_id": "123456789012"},
        )
        self.assertFalse(Registration.objects.exists())
        self.assertEqual(r.status_code, 200)
        self.client.post(
            reverse("register"),
            {**PROFILE, "email": "o@r.kz", "photo": photo(), "organization_name": "ТОО", "tax_id": valid_bin()},
        )
        self.assertEqual(Registration.objects.get().country, "KZ")

    @override_settings(CHECKIN_URL="https://cp.example.com")
    def test_settings(self):
        """claugine-web 2026-11-27 (job client-countries): Settings → Customer shows the countries read only; a post
        changes nothing."""
        PortalSettings.objects.update_or_create(pk=1, defaults={"home_country": "KZ", "allowed_countries": ["AE"]})
        sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(sa)
        page = self.client.get(reverse("portal_settings") + "?tab=customer").content.decode()
        self.assertNotIn('name="allowed_countries"', page)
        self.assertNotIn('name="home_country"', page)
        self.assertNotIn("data-duallist", page[page.index("<h2>Countries</h2>") :][:2000])
        self.assertIn("Kazakhstan", page)
        self.assertIn("United Arab Emirates", page)
        self.assertIn("Managed in your account on cp.example.com", page)  # read from the settings, never named
        r = self.client.post(
            reverse("portal_settings"),
            {"tab": "customer", "section": "countries", "home_country": "AE", "allowed_countries": ["UZ"]},
            follow=True,
        )
        self.assertContains(r, "managed on cp.example.com")
        conf = PortalSettings.get()
        self.assertEqual((conf.home_country, conf.allowed_countries), ("KZ", ["AE"]))
        self.assertFalse(User.objects.filter(email="x").exists())

    def test_the_check_in_brings_them(self):
        from accounts import checkin

        self.assertTrue(checkin.apply_countries({"countries": {"home": "ae", "allowed": ["AE", "KZ", "UZ", "KZ"]}}))
        conf = PortalSettings.get()
        self.assertEqual(
            (conf.home_country, conf.allowed_countries, conf.countries()), ("AE", ["KZ", "UZ"], ["AE", "KZ", "UZ"])
        )
        self.assertFalse(checkin.apply_countries({"countries": {"home": "AE", "allowed": ["AE", "KZ", "UZ"]}}))  # same
        for bad in (
            {},
            {"countries": None},
            {"countries": {"home": "XX", "allowed": ["XX"]}},
            {"countries": {"home": "KZ", "allowed": ["KZ", "ZZ"]}},
            {"countries": {"home": "KZ"}},
        ):
            self.assertFalse(checkin.apply_countries(bad), bad)
        self.assertEqual(PortalSettings.get().countries(), ["AE", "KZ", "UZ"])

    def test_auto_update_keeps_them_in_every_state(self):
        from unittest import mock

        from accounts import autoupdate, checkin

        for state in ("active", "conflict", "retired"):
            ans = {"state": state, "edition": "standard", "countries": {"home": "UZ", "allowed": ["UZ", "KZ"]}}
            PortalSettings.objects.filter(pk=PortalSettings.get().pk).update(home_country="KZ", allowed_countries=[])
            with (
                mock.patch.object(checkin, "configured", return_value=True),
                mock.patch.object(checkin, "check_in", return_value=(True, ans)),
                mock.patch("accounts.ha.role", return_value="active"),
                mock.patch("accounts.ha_agent.installed_status", return_value={"installed_tag": "v01.01.63"}),
            ):
                autoupdate.tick(log=lambda *a: None, force=True)
            self.assertEqual(PortalSettings.get().countries(), ["UZ", "KZ"], state)


class OrganizationCountry(Base):
    """Owner 2026-11-13: the country is a column of the organizations list (with a header filter) and a line of the
    organization's details, with the kind of its tax ID; it is chosen on creation and never edited."""

    def test_list_and_detail(self):
        sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(sa)
        Organization.objects.create(name="TOO A", country="KZ", tax_id=valid_bin(), tax_id_type="bin")
        ae = Organization.objects.create(name="ALEROY LTD", country="AE", tax_id="105438487800001", tax_id_type="trn")
        page = self.client.get(reverse("org_list")).content.decode()
        self.assertIn('<td title="United Arab Emirates">AE</td>', page)  # owner: the first column, the code
        page = self.client.get(reverse("org_list") + "?country=AE").content.decode()
        self.assertIn("ALEROY LTD", page)
        self.assertNotIn("TOO A", page)
        page = self.client.get(reverse("org_detail", args=[ae.pk])).content.decode()
        # owner: "NAME country tax id (tax id type)", then "site address"
        self.assertIn('<span title="United Arab Emirates">AE</span>', page)
        self.assertIn(">(TRN)</span>", page)
        # not editable after creation: the edit form has no country, tax ID or name
        r = self.client.post(
            reverse("org_edit", args=[ae.pk]),
            {"country": "KZ", "tax_id": "1", "name": "X", "short_name": "aleroy", "kind": "cloud"},
        )
        ae.refresh_from_db()
        self.assertEqual((ae.country, ae.tax_id, ae.name), ("AE", "105438487800001", "ALEROY LTD"))
        self.assertIn(r.status_code, (200, 302))

    def test_address_and_website(self):
        """Owner 2026-11-13: both optional, editable at any time; registration takes them too."""
        sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(sa)
        rep = self.staff("rep@corp.kz", is_sales=True)
        o = Organization.objects.create(
            name="ALEROY LTD", country="AE", tax_id="105438487800001", tax_id_type="trn", sales_rep=rep
        )
        self.client.post(
            reverse("org_edit", args=[o.pk]),
            {
                "short_name": "aleroy",
                "sales_rep": rep.pk,
                "address": "Sky Tower, Al Reem Island, Abu Dhabi",
                "website": "aleroy.ai",
            },
        )
        o.refresh_from_db()
        self.assertEqual((o.address, o.website), ("Sky Tower, Al Reem Island, Abu Dhabi", "https://aleroy.ai"))
        page = self.client.get(reverse("org_detail", args=[o.pk])).content.decode()
        self.assertIn('href="https://aleroy.ai"', page)
        self.client.post(
            reverse("org_edit", args=[o.pk]),
            {"short_name": "aleroy", "sales_rep": rep.pk, "address": "", "website": ""},
        )
        o.refresh_from_db()
        self.assertEqual((o.address, o.website), ("", ""))


class Review598599(Base):
    """commit-598, commit-599 (review of b3a8c64, owner 2026-11-27 10:26 "fix commit-598, commit-599")."""

    def test_598_a_whole_row_save_keeps_the_checked_in_countries(self):
        from accounts import checkin

        old = PortalSettings.get()  # a settings request has read the row ...
        checkin.apply_countries({"countries": {"home": "AE", "allowed": ["AE", "UZ"]}})  # ... a check-in writes ...
        old.client_mail = not old.client_mail
        old.save()  # ... and the request saves the whole row from its older copy
        conf = PortalSettings.get()
        self.assertEqual((conf.home_country, conf.allowed_countries), ("AE", ["UZ"]))
        self.assertEqual(conf.client_mail, old.client_mail)

    def test_598_through_the_settings_page(self):
        from unittest import mock

        from accounts import checkin

        sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(sa)
        real = PortalSettings.get

        once = []

        def get():
            obj = real()
            if not once:  # right after the page's read (once: apply_countries reads the row through get() too)
                once.append(1)
                checkin.apply_countries({"countries": {"home": "UZ", "allowed": ["UZ"]}})
            return obj

        with mock.patch.object(PortalSettings, "get", side_effect=get):
            self.client.post(
                reverse("portal_settings"), {"tab": "customer", "section": "client_mail", "client_mail": "on"}
            )
        self.assertEqual(real().countries(), ["UZ"])

    def test_599_a_new_organization_only_in_an_allowed_country(self):
        from tenants.forms import OrganizationForm
        from tenants.models import Organization

        PortalSettings.objects.update_or_create(pk=1, defaults={"home_country": "AE", "allowed_countries": []})
        f = OrganizationForm()
        self.assertEqual([c for c, _n in f.fields["country"].choices], ["AE"])
        self.assertEqual(f.country_default, "AE")
        f = OrganizationForm({"name": "Acme", "kind": "cloud", "country": "KZ", "tax_id": "123"})
        self.assertFalse(f.is_valid())
        self.assertIn("country", f.errors)
        org = Organization(name="Old", country="KZ")
        org.save()
        f = OrganizationForm(instance=org)
        self.assertEqual([c for c, _n in f.fields["country"].choices], ["AE", "KZ"])  # kept on edit


class Review600(Base):
    """commit-600 (review of 4ac954b, owner 2026-11-27 10:47 "fix commit-600")."""

    @override_settings(CHECKIN_URL="https://[bad")
    def test_a_malformed_check_in_url(self):
        self.client.force_login(self.staff("sa@corp.kz", is_super_admin=True))
        r = self.client.get(reverse("portal_settings") + "?tab=customer")
        self.assertContains(r, "Managed in your account on the licence control plane")
        r = self.client.post(reverse("portal_settings"), {"tab": "customer", "section": "countries"}, follow=True)
        self.assertContains(r, "managed on the licence control plane")


class CustomerLayout(Base):
    """Owner 2026-11-27 11:19 (mockup m07, approved 11:25): Settings → Customer - the customers block without its
    heading, the corporate email domains and the external NTP servers side by side, Countries a block of its own on
    the full width, no hint under Internal domain."""

    def test_the_layout(self):
        self.client.force_login(self.staff("sa@corp.kz", is_super_admin=True))
        page = self.client.get(reverse("portal_settings") + "?tab=customer").content.decode()
        self.assertNotIn("<h2>Customers</h2>", page)
        self.assertNotIn("the hosts' FQDN and DNS search domain", page)
        lists = page[page.index('class="settings-lists"') :]
        lists = lists[: lists.index("</form>")]
        self.assertLess(lists.index('name="corporate_domains"'), lists.index('name="ntp_external"'))
        # commit-607: the section holding the countries is itself a full-width card (from the rendered page)
        chips = page.index('class="country-chips"')
        opening = page[page.rindex("<section", 0, chips) : chips]
        self.assertNotIn("</section>", opening)
        self.assertRegex(opening, r'^<section class="card full">')


class LeftBar(Base):
    """Owner 2026-11-27 11:36: "remove all sections headers from left bar - cloud infrastructure ..."."""

    def test_no_section_headings(self):
        self.client.force_login(self.staff("sa@corp.kz", is_super_admin=True))
        page = self.client.get(reverse("portal_settings")).content.decode()
        bar = page[page.index('<aside class="sidenav"') : page.index("</aside>")]
        self.assertNotIn("sidenav-title", bar)
        for title in ("Cloud", "Infrastructure", "Administration"):
            self.assertNotIn(f">{title}<", bar)
            self.assertIn(f'aria-label="{title}"', bar)  # still named for screen readers
        self.assertIn('href="/staff/settings/"', bar)
