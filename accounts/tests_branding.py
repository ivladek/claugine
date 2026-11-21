"""Owner 2026-11-17: Claugine's logo while no logo is uploaded; the browser icon cut by the portal from an uploaded
logo; the portal's colours - Claugine's, the logo's (offered after an upload) or a custom one."""

import io
import re

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image, ImageDraw

from accounts import branding
from accounts.mailtemplates import T
from accounts.tests import Base
from tenants.models import PortalSettings

BLUE = (31, 95, 191)


def wide_logo(bg=None):
    """An invented logo: a square mark, then a word of shorter letters (on a transparent or a coloured band)."""
    im = Image.new("RGBA", (600, 128), (bg + (255,)) if bg else (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    ink = (255, 255, 255, 255) if bg else BLUE + (255,)
    d.rounded_rectangle([8, 8, 120, 120], radius=18, fill=ink)
    for i in range(6):  # the letters: lower than the mark, closer to each other than to it
        x = 170 + i * 66
        d.rectangle([x, 40, x + 50, 100], fill=ink)
    return im


def text_logo():
    """An invented name-only logo: letters of one height, evenly spaced."""
    im = Image.new("RGBA", (520, 128), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for i in range(7):
        x = 10 + i * 72
        d.rectangle([x, 30, x + 56, 100], fill=BLUE + (255,))
    return im


def upload(im, name="logo.png"):
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


class Branding(Base):
    def setUp(self):
        branding._cache.clear()
        self.sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(self.sa)

    def post(self, **data):
        return self.client.post(reverse("portal_settings"), {"tab": "customer", **data})

    def icon(self, size):
        r = self.client.get(reverse("brand_icon", args=[size]) + "?v=1")
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "image/png"))
        return Image.open(io.BytesIO(r.content)).convert("RGBA")

    def test_claugine_by_default(self):
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn('src="/static/brand/claugine-logo.png"', page)  # the header: Claugine's logo
        self.assertIn('rel="icon" sizes="32x32" href="/brand/icon-32.png?v=', page)
        self.assertIn('rel="apple-touch-icon" sizes="180x180"', page)
        self.assertIn("--accent: #510259;", page)  # Claugine's purple
        self.assertNotIn('brand-logo inv"', page)  # a logo on its own band is not inverted on dark pages
        ic = self.icon(32)
        self.assertEqual(ic.size, (32, 32))
        self.assertEqual(ic.getpixel((1, 1)), (83, 2, 91, 255))  # Claugine's gear: its purple at the corner
        self.assertEqual(self.client.get(reverse("brand_icon", args=[64])).status_code, 404)  # the sizes offered only
        page = self.client.get(reverse("portal_settings") + "?tab=customer").content.decode()
        self.assertIn("Claugine's logo: no logo uploaded", page)
        self.assertIn('value="logo" disabled', page)  # From the logo: once a logo is uploaded
        self.assertNotIn('data-open="dlg-logo-remove"', page)

    def test_upload_cuts_the_icon_and_offers_the_colours(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("brand_icon", args=[16])).status_code, 200)  # public, as the logo
        self.client.force_login(self.sa)
        self.post(section="logo", op="upload", logo=upload(wide_logo()))
        s = PortalSettings.get()
        self.assertEqual((s.colour_scheme, s.colour_offer), ("claugine", True))  # no change until it is answered
        page = self.client.get(reverse("portal_settings") + "?tab=customer").content.decode()
        self.assertIn("The logo is uploaded. Its colours:", page)
        self.assertIn('title="#1f5fbf"', page)
        self.assertIn("the mark beside the name", page)
        self.assertIn("--accent: #510259;", page)
        self.assertIn('brand-logo inv"', page)  # transparent, dark ink: inverted on dark pages
        ic = self.icon(180)  # the mark, centred: blue in the middle, transparent at the corner
        self.assertEqual(ic.getpixel((90, 90))[:3], BLUE)
        self.assertEqual(ic.getpixel((1, 1))[3], 0)
        self.post(section="colours", offer="use")
        s = PortalSettings.get()
        self.assertEqual((s.colour_scheme, s.colour_offer), ("logo", False))
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("--accent: #1f5fbf;", page)
        self.assertNotIn("The logo is uploaded. Its colours:", page)

    def test_keep_the_colours(self):
        self.post(section="logo", op="upload", logo=upload(wide_logo()))
        self.post(section="colours", offer="keep")
        s = PortalSettings.get()
        self.assertEqual((s.colour_scheme, s.colour_offer), ("claugine", False))
        self.assertIn("--accent: #510259;", self.client.get(reverse("dashboard")).content.decode())

    def test_a_logo_already_there_gives_the_colours(self):
        """An installation that had its own logo before this change (no choice made) takes the logo's colours."""
        buf = io.BytesIO()
        wide_logo().save(buf, "PNG")
        from django.utils import timezone

        PortalSettings.objects.update_or_create(
            pk=1, defaults={"logo": buf.getvalue(), "logo_updated_at": timezone.now(), "colour_scheme": ""}
        )
        self.assertEqual(branding.scheme(), ("logo", "#1f5fbf"))
        self.assertIn("--accent: #1f5fbf;", self.client.get(reverse("dashboard")).content.decode())

    def test_a_logo_on_its_own_band(self):
        """A white mark on a band of colour: the icon keeps the band (rounded), the colour is the band's, and the
        logo is not inverted on dark pages."""
        self.post(section="logo", op="upload", logo=upload(wide_logo(bg=(81, 2, 89))))
        self.post(section="colours", offer="use")
        info = branding.analyse()
        self.assertEqual((info["how"], info["transparent"]), ("beside", False))
        self.assertEqual(branding.scheme()[0], "logo")
        ic = self.icon(180)
        self.assertEqual(ic.getpixel((90, 90))[:3], (255, 255, 255))  # the mark
        self.assertEqual(ic.getpixel((90, 3))[:3], (81, 2, 89))  # the band around it
        self.assertEqual(ic.getpixel((0, 0))[3], 0)  # rounded corners
        self.assertNotIn('brand-logo inv"', self.client.get(reverse("dashboard")).content.decode())

    def test_a_name_only_logo_gives_the_first_letter(self):
        PortalSettings.objects.update_or_create(pk=1, defaults={"provider_name": "Acme Cloud"})
        self.post(section="logo", op="upload", logo=upload(text_logo()))
        info = branding.analyse()
        self.assertIsNone(info["box"])
        page = self.client.get(reverse("portal_settings") + "?tab=customer").content.decode()
        self.assertIn("the first letter of the provider&#x27;s name", page)
        ic = self.icon(180)
        self.assertEqual(ic.getpixel((12, 90))[:3], BLUE)  # the logo's colour around the letter
        self.assertGreater(sum(1 for p in ic.getdata() if p[:3] == (255, 255, 255)), 500)  # a white letter

    def test_custom_colour(self):
        r = self.post(section="colours", colour_scheme="custom", colour_custom="red")
        self.assertEqual(PortalSettings.get().colour_scheme, "")
        self.assertContains(self.client.get(r.url), "#rrggbb")
        self.post(section="colours", colour_scheme="custom", colour_custom="#FFCC00")
        s = PortalSettings.get()
        self.assertEqual((s.colour_scheme, s.colour_custom), ("custom", "#ffcc00"))
        page = self.client.get(reverse("dashboard")).content.decode()
        light = re.search(r":root \{ --accent: (#\w+); --accent-ink: (#\w+);", page)
        # a yellow is darkened for the light page until white text on it and links on white can be read
        self.assertGreaterEqual(branding.contrast(branding._rgb(light.group(1)), (255, 255, 255)), 4.5)
        self.assertEqual(light.group(2), "#ffffff")
        # From the logo needs a logo
        self.post(section="colours", colour_scheme="logo")
        self.assertEqual(PortalSettings.get().colour_scheme, "custom")

    def test_remove_logo(self):
        self.post(section="logo", op="upload", logo=upload(wide_logo()))
        self.post(section="colours", offer="use")
        page = self.client.get(reverse("portal_settings") + "?tab=customer").content.decode()
        self.assertIn('data-open="dlg-logo-remove"', page)  # a dialog asks first
        self.post(section="logo", op="remove")
        s = PortalSettings.get()
        self.assertIsNone(s.logo)
        self.assertEqual(branding.scheme(), ("claugine", "#510259"))  # the logo's colours go with it
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn('src="/static/brand/claugine-logo.png"', page)
        self.assertEqual(self.icon(32).getpixel((16, 16))[3], 255)  # the gear again

    def test_e_mails_carry_claugine_logo_while_none_is_uploaded(self):
        from unittest import mock

        from django.test import override_settings

        from accounts import ha
        from accounts.utils import notify
        from tenants.models import Organization

        with (
            mock.patch.object(ha, "is_active", return_value=True),
            mock.patch(
                "django.core.mail.backends.smtp.EmailBackend.send_messages", side_effect=lambda ms: len(ms)
            ) as smtp,
            override_settings(EMAIL_BACKEND="accounts.appliance.EmailBackend"),
        ):
            notify(["sa@corp.kz"], T.ORG_REGISTRATION_APPROVED, {"org": Organization(name="X"), "link": "x"}, "Hi")
        m = smtp.call_args[0][0][0]
        self.assertIn('src="cid:provider-logo"', m.alternatives[0][0])
        self.assertIn("Content-ID: <provider-logo>", m.message().as_string())

    def test_palette_contrast(self):
        for c in ("#510259", "#0e151c", "#ffcc00", "#1f5fbf", "#ffffff"):
            p = branding.palette(c)
            self.assertGreaterEqual(branding.contrast(branding._rgb(p["light"]["accent"]), (255, 255, 255)), 4.5, c)
            self.assertGreaterEqual(branding.contrast(branding._rgb(p["dark"]["accent"]), (26, 32, 29)), 4.5, c)


class VersionInTheFooter(Base):
    def test_the_version_is_on_the_bottom_line(self):
        """Owner 2026-11-17 16:15: "move version from top line to the right on bottom line"."""
        from unittest import mock

        self.client.force_login(self.staff("sa@corp.kz", is_super_admin=True))
        with mock.patch("accounts.context._portal_version", return_value="v01.01.26"):
            page = self.client.get(reverse("dashboard")).content.decode()
        head, foot = page[: page.index("</header>")], page[page.index("<footer") :]
        self.assertNotIn("v01.01.26", head)
        self.assertIn('<span class="foot-version" title="Portal version">v01.01.26</span>', foot)
