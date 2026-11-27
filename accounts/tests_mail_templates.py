"""Owner 2026-11-17 17:19-17:47: one HTML template per e-mail ("one template must have one name", "base format is
HTML"); the plain-text part made from it ("portal can convert html to text byself"); the template shown as the e-mail
looks with an Edit button, the editor with a live preview."""

import os

from django.core import mail
from django.test import override_settings
from django.urls import reverse

from accounts import mailhtml, mailtemplates
from accounts.mailtemplates import T
from accounts.models import MailTemplate
from accounts.tests import Base


class OneHtmlTemplate(Base):
    def setUp(self):
        self.sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(self.sa)

    def test_one_name_per_e_mail(self):
        names = mailtemplates.names()
        labels = [mailtemplates.label(n) for n in names]
        self.assertEqual(len(labels), len(set(labels)))  # no report.txt beside report.html
        self.assertEqual([n for n in names if not n.endswith(".html")], ["password_reset_subject.txt"])
        self.assertFalse([f for f in os.listdir(mailtemplates.folder()) if f.endswith(".txt") and f[0] != "_"
                          and f != "password_reset_subject.txt"])  # fmt: skip
        for n in names:  # every template compiles and draws as a page, its variables shown by name
            self.assertEqual(mailtemplates.compile_error(mailtemplates.default_text(n)), "", n)
            self.assertIn(n, mailtemplates.WHAT)
            page = mailtemplates.preview(n, mailtemplates.default_text(n))
            self.assertNotIn("Error:", page[:80], n)
        self.assertIn(
            "‹link›",
            mailtemplates.preview("member_invitation.html", mailtemplates.default_text("member_invitation.html")),
        )

    def test_the_text_part_is_made_from_the_html(self):
        from accounts.utils import notify
        from tenants.models import Organization

        mail.outbox.clear()
        notify(
            ["sa@corp.kz"],
            T.ORG_ASSIGNED,
            {"org": Organization(name="Acme & Co"), "link": "https://portal.example.com/o/1/"},
            "Assigned",
        )
        m = mail.outbox[-1]
        html = m.alternatives[0][0]
        self.assertIn("Acme &amp; Co", html)  # escaped in HTML
        self.assertIn('href="https://portal.example.com/o/1/"', html)
        self.assertIn("You are now the sales rep for Acme & Co and all its projects.", m.body)
        self.assertIn("\nhttps://portal.example.com/o/1/\n", m.body)  # the button left out, its address kept
        self.assertNotIn("Open", m.body)
        self.assertNotIn("<", m.body)

    def test_text_of(self):
        html = (
            '<table role="presentation"><tr><td><p>Hello <a href="mailto:a@example.com">Ann</a>,</p>'
            "<ul><li>CPU: <strong>4</strong></li><li>RAM: <strong>8 GB</strong></li></ul>"
            '<table role="presentation"><tr><th>Site</th><th>Name</th></tr><tr><td>dc1</td><td><div>a</div><div>b</div></td></tr></table>'
            '<p><a href="https://x.example/">https://x.example/</a> and <a href="https://y.example/">here</a></p></td></tr></table>'
        )
        self.assertEqual(
            mailhtml.text_of(html),
            "Hello Ann (a@example.com),\n\n- CPU: 4\n- RAM: 8 GB\n\nSite | Name\ndc1 | a b\n\n"
            "https://x.example/ and here (https://y.example/)\n",
        )
        self.assertEqual(
            mailhtml.linkify("<p>see https://z.example/a and b@example.com</p>"),
            '<p>see <a href="https://z.example/a" style="color:#0f6b55">https://z.example/a</a> and '
            '<a href="mailto:b@example.com" style="color:#0f6b55">b@example.com</a></p>',
        )

    def test_conversion_of_an_edited_text_template(self):
        src = "{% load i18n %}{% autoescape off %}Hi {{ who }} <{{ mail }}> & co.\n\nA: {{ a }}\nB: {{ b }}\n\n{{ link }}\n{% endautoescape %}"
        out = mailhtml.html_from_text_template(src)
        self.assertTrue(out.startswith('{% extends "emails/_base.html" %}{% load i18n %}{% block body %}'))
        self.assertIn('Hi <a href="mailto:{{ mail }}" style="color:#0f6b55">{{ who }}</a> &amp; co.', out)
        self.assertIn("<li>A: <strong>{{ a }}</strong></li>", out)
        self.assertIn('<a class="btn" href="{{ link }}"', out)
        self.assertEqual(mailtemplates.compile_error(out), "")

    def test_migration_carries_an_edited_text_template_over(self):
        from importlib import import_module

        from django.apps import apps

        mig = import_module("accounts.migrations.0024_mail_templates_html")
        MailTemplate.objects.create(name="org_assigned.txt", body="Yours: {{ org.name }}\n\n{{ link }}")
        MailTemplate.objects.create(name="report.txt", body="old text of an HTML e-mail")  # made from the HTML now
        mig.forward(apps, None)
        self.assertEqual(
            sorted(MailTemplate.objects.values_list("name", flat=True)),
            ["org_assigned.html", "org_assigned.txt", "report.txt"],
        )
        self.assertIn("<p", MailTemplate.objects.get(name="org_assigned.html").body)
        mig.forward(apps, None)  # once
        self.assertEqual(MailTemplate.objects.filter(name="org_assigned.html").count(), 1)

    def test_page_and_live_preview(self):
        page = self.client.get(reverse("portal_settings") + "?tab=mail").content.decode()
        self.assertIn('<span class="code">member_invitation</span>', page)
        self.assertNotIn("member_invitation.html</span>", page)
        self.assertIn('data-open="dlg-tpl-', page)  # Edit on the details' top row
        url = reverse("mail_template_preview", args=[T.MEMBER_INVITATION])
        r = self.client.post(url, {"body": "<p>Hi {{ who }}</p>"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("<p>Hi ‹who›</p>", r.content.decode())
        r = self.client.post(url, {"body": "{% if %}"})
        self.assertIn("<pre", r.content.decode())  # the error, not a page
        self.assertFalse(MailTemplate.objects.exists())  # nothing saved
        self.assertEqual(self.client.post(reverse("mail_template_preview", args=[999]), {"body": "x"}).status_code, 404)
        self.client.force_login(self.staff("sup@corp.kz", is_support=True))
        self.assertNotEqual(self.client.post(url, {"body": "x"}).status_code, 200)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_password_reset_e_mail(self):
        self.client.logout()
        mail.outbox.clear()
        self.client.post(reverse("password_reset"), {"email": "sa@corp.kz"})
        m = mail.outbox[-1]
        self.assertEqual(m.subject, "[Example Cloud] Password reset")
        html = m.alternatives[0][0]
        self.assertIn('class="btn" href="http', html)
        self.assertIn("/password-reset/", m.body)
        self.assertNotIn("<", m.body)


class TemplatesById(Base):
    """Owner 2026-11-17 18:52-18:57 (Mockups 66, 67): "Edit button for email template can allow to edit template name",
    "all contributors of email template must reference by id to it, not by name", "dont show id", the filter on the
    title row, Save and Cancel after the name."""

    def setUp(self):
        self.client.force_login(self.staff("sa@corp.kz", is_super_admin=True))

    def test_the_code_sends_by_id(self):
        import re as _re
        from pathlib import Path

        from django.conf import settings

        from accounts.utils import notify

        self.assertEqual({v for k, v in vars(T).items() if k.isupper()}, set(mailtemplates.IDS))
        with self.assertRaises(TypeError):
            notify(["sa@corp.kz"], "org_assigned", {}, "x")
        # no sender names a template: every notify() / _tell() call gives a T.<NAME>
        bad = []
        for p in Path(settings.BASE_DIR).rglob("*.py"):
            rel = p.relative_to(settings.BASE_DIR).as_posix()
            if "/tests" in "/" + rel or "/migrations/" in rel or rel.startswith((".", "scratch")):
                continue
            for m in _re.finditer(r"(?<![\w.])(?:notify|_tell)\(\s*[^,]+,\s*\"[a-z_]+\"", p.read_text()):
                bad.append(f"{rel}: {m.group(0)[:60]}")
        self.assertEqual(bad, [])

    def test_rename_keeps_what_is_sent(self):
        from accounts.utils import notify
        from tenants.models import Organization, PortalSettings

        page = self.client.get(reverse("portal_settings") + "?tab=mail").content.decode()
        self.assertIn('data-key="mailtpl" data-search-in-bar', page)  # the filter on the title row
        self.assertIn("<th>Name</th><th>Description</th>", page)  # no Id column
        url = reverse("mail_template", args=[T.ORG_ASSIGNED])
        body = mailtemplates.default_text("org_assigned.html")
        self.client.post(url, {"title": "Sales rep assigned", "body": body})
        self.assertEqual(PortalSettings.get().mail_template_names, {str(T.ORG_ASSIGNED): "Sales rep assigned"})
        self.assertFalse(MailTemplate.objects.exists())  # the text is the portal's own: only the name changed
        page = self.client.get(reverse("portal_settings") + "?tab=mail").content.decode()
        self.assertIn('<span class="code">Sales rep assigned</span>', page)
        self.assertIn('name="title" value="Sales rep assigned"', page)
        mail.outbox.clear()
        notify(["sa@corp.kz"], T.ORG_ASSIGNED, {"org": Organization(name="Acme"), "link": "https://x.example/"}, "S")
        self.assertIn("You are now the sales rep for Acme", mail.outbox[-1].body)
        # another template's name, or no name: nothing saved
        self.client.post(url, {"title": "member_invitation", "body": "<p>changed</p>"})
        self.client.post(url, {"title": "  ", "body": "<p>changed</p>"})
        self.assertEqual(PortalSettings.get().mail_template_names, {str(T.ORG_ASSIGNED): "Sales rep assigned"})
        self.assertFalse(MailTemplate.objects.exists())
        # back to its own name: nothing kept
        self.client.post(url, {"title": "org_assigned", "body": body})
        self.assertEqual(PortalSettings.get().mail_template_names, {})

    def test_a_report_keeps_its_template_by_id(self):
        from accounts import reports

        self.client.post(
            reverse("mail_template", args=[T.REGISTRATION_REPORT]),
            {"title": "Daily registrations", "body": mailtemplates.default_text("registration_report.html")},
        )
        self.assertIn((T.REGISTRATION_REPORT, "Daily registrations"), reports.templates())
        self.assertEqual(reports.template_id("registration_report"), T.REGISTRATION_REPORT)  # a name kept from before
        self.assertEqual(reports.template_id(str(T.REPORT)), T.REPORT)
        self.assertEqual(reports.template_id("gone"), T.REPORT)
