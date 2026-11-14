"""Countries (owner 2026-11-12): an organization has a country; Settings → Provider sets the home country and the
allowed ones; the tax ID follows the country (KZ IIN/BIN, AE TRN, else free) and is required in any case."""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
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

    def test_settings(self):
        sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(sa)
        r = self.client.get(reverse("portal_settings") + "?tab=provider")
        self.assertContains(r, 'name="allowed_countries"')
        self.client.post(
            reverse("portal_settings"),
            {"tab": "provider", "section": "countries", "home_country": "KZ", "allowed_countries": ["AE", "kz", "XX"]},
        )
        conf = PortalSettings.get()
        self.assertEqual((conf.home_country, conf.allowed_countries), ("KZ", ["AE"]))
        # owner 2026-11-12: a two-list picker - the allowed ones on the right (posted), the rest on the left
        page = self.client.get(reverse("portal_settings") + "?tab=provider").content.decode()
        self.assertIn("data-duallist", page)
        to = page[page.index("data-dl-to") : page.index("</select>", page.index("data-dl-to"))]
        frm = page[page.index("data-dl-from") : page.index("</select>", page.index("data-dl-from"))]
        self.assertIn('value="AE"', to)
        self.assertNotIn('value="AE"', frm)
        self.assertNotIn('value="KZ"', frm)  # the home country is in neither list
        self.client.post(reverse("portal_settings"), {"tab": "provider", "section": "countries", "home_country": "ZZ"})
        self.assertEqual(PortalSettings.get().home_country, "KZ")
        self.assertFalse(User.objects.filter(email="x").exists())


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
