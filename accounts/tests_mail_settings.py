"""Owner 2026-11-17, Settings → Mail: the nodes' own mail servers only ("the only option - use internal server"), the
DKIM key made by the portal ("Portal makes the key") for the sender address's domain, each node's public address ("A
field per node") and the DNS records from them, e-mail to organization users and to platform users each allowed or
held, the expiration notices following them; and "check that implemented mail sending logic followed settings"."""

import struct
from unittest import mock

from django.conf import settings
from django.core import mail
from django.core.mail import EmailMessage
from django.test import override_settings
from django.urls import reverse

from accounts import appliance, maildns
from accounts.models import AuditLog, Kind, User
from accounts.tests import Base
from tenants.models import PortalSettings

NODES = [{"site": "dc1", "mail_name": "mail1.example.com"}, {"site": "dc2", "mail_name": "mail2.example.com"}]


def smtp_patch():
    return mock.patch("django.core.mail.backends.smtp.EmailBackend.send_messages", side_effect=lambda ms: len(ms))


@override_settings(FIELD_ENCRYPTION_KEY="m3Jb0bC1k0qWw8bq3cJmHqk6kVqZ0b9mJxS6m0f4n1A=")
class MailSettings(Base):
    def setUp(self):
        self.sa = self.staff("sa@corp.kz", is_super_admin=True)
        self.client.force_login(self.sa)

    def post(self, **over):
        data = {
            "tab": "mail",
            "section": "mail",
            "mail_from": "Example Cloud <portal@example.com>",
            "mail_dkim_selector": "claugine",
            "client_mail": "on",
            "staff_mail": "on",
            "ip_dc1": "203.0.113.10",
            "ip_dc2": "198.51.100.10",
        }
        data.update(over)
        with mock.patch.object(maildns, "nodes", return_value=NODES):
            return self.client.post(reverse("portal_settings"), {k: v for k, v in data.items() if v is not None})

    def test_save_makes_the_key_and_the_records(self):
        PortalSettings.objects.update_or_create(pk=1, defaults={"mail_mode": "internal", "mail_host": "relay.example"})
        self.post()
        s = PortalSettings.get()
        self.assertEqual(s.mail_mode, "direct")  # the only way now
        self.assertTrue(s.has_secret("mail_dkim_key"))  # "Portal makes the key"
        self.assertEqual(appliance.dkim_domain(s), "example.com")  # "Sender's domain"
        self.assertEqual(s.mail_public_ips, {"dc1": "203.0.113.10", "dc2": "198.51.100.10"})
        key = s.get_secret("mail_dkim_key")
        self.post(mail_from="portal@example.com")
        self.assertEqual(PortalSettings.get().get_secret("mail_dkim_key"), key)  # made once, kept
        recs = {(r["type"], r["name"]): r["value"] for r in maildns.records(PortalSettings.get(), NODES, check=False)}
        self.assertEqual(recs[("A", "mail1.example.com")], "203.0.113.10")

        self.assertEqual(recs[("TXT", "example.com")], "v=spf1 ip4:203.0.113.10 ip4:198.51.100.10 -all")
        self.assertTrue(recs[("TXT", "claugine._domainkey.example.com")].startswith("v=DKIM1; k=rsa; p=MIIB"))
        self.assertEqual(recs[("TXT", "_dmarc.example.com")], "v=DMARC1; p=quarantine; rua=mailto:portal@example.com")
        self.assertEqual(recs[("PTR", "10.113.0.203.in-addr.arpa")], "mail1.example.com")
        self.assertEqual(recs[("PTR", "10.100.51.198.in-addr.arpa")], "mail2.example.com")
        # wrong values: nothing saved
        self.post(mail_from="not an address", ip_dc1="300.1.1.1", mail_dkim_selector="a b")
        self.assertEqual(PortalSettings.get().mail_public_ips["dc1"], "203.0.113.10")

    def test_send_mail_from_a_node(self):
        """Owner 2026-11-23 (A): one of the nodes, or each node itself; anything else is refused."""
        with mock.patch.object(maildns, "records", return_value=[]):
            self.post(mail_via="dc2")
            self.assertEqual(PortalSettings.get().mail_via, "dc2")
            self.post(mail_via="dc7")
            self.assertEqual(PortalSettings.get().mail_via, "dc2")  # not a node: refused, kept
            self.post(mail_via="")
            self.assertEqual(PortalSettings.get().mail_via, "")
            with mock.patch.object(maildns, "nodes", return_value=NODES):
                page = self.client.get(reverse("portal_settings") + "?tab=mail").content.decode()
        self.assertIn('name="mail_via"', page)

    def test_page(self):
        self.post()
        recs = [
            {
                "zone": "example.com",
                "name": "example.com",
                "type": "TXT",
                "value": "v=spf1 -all",
                "state": "missing",
                "now": [],
            }
        ]
        with (
            mock.patch.object(maildns, "nodes", return_value=NODES),
            mock.patch.object(maildns, "records", return_value=recs),
        ):
            page = self.client.get(reverse("portal_settings") + "?tab=mail").content.decode()
        for name in ("mail_from", "mail_dkim_selector", "ip_dc1", "ip_dc2", "client_mail", "staff_mail"):
            self.assertIn(f'name="{name}"', page)
        for gone in ("mail_host", "mail_user", "mail_password", "mail_mode", "notify_clients", "mail_dkim_key"):
            self.assertNotIn(f'name="{gone}"', page)
        self.assertIn("<summary>DNS records (1 missing, 0 other value)</summary>", page)  # under an arrow
        # owner 2026-11-17 17:29: one row for the senders' addresses, without the mail names; no Zone column
        self.assertEqual(page.count("Mail sender public IPs"), 1)
        self.assertNotIn("mail1.example.com", page)
        self.assertNotIn(">Zone<", page)
        self.assertIn("This node&#x27;s mail server", page.replace("This node's", "This node&#x27;s"))
        self.assertIn('data-key="mailtpl"', page)  # the templates: a standard list, the e-mail in the details
        self.assertIn('<iframe class="mail-preview"', page)
        self.assertIn("data-tpl-edit data-preview=", page)

    def test_send_test(self):
        mail.outbox.clear()
        self.client.post(reverse("portal_settings"), {"tab": "mail", "section": "mail_test"})
        self.assertEqual(mail.outbox[-1].to, ["sa@corp.kz"])
        self.assertTrue(AuditLog.objects.filter(action="mail_test_sent").exists())


class MailFollowsSettings(Base):
    """The sending honours the switches: e-mail to organization users, e-mail to platform users; a message the owner
    allowed to a client keeps that client; the sender address is the one set."""

    def setUp(self):
        User.objects.create_user("ops@provider.example", "Str0ng-PASS-123", kind=Kind.STAFF, email_verified=True)
        User.objects.create_user("adm@client.kz", "Str0ng-PASS-123", kind=Kind.CUSTOMER, email_verified=True)

    def send(self, *msgs):
        with mock.patch("accounts.ha.is_active", return_value=True), smtp_patch() as smtp:
            appliance.EmailBackend().send_messages(list(msgs))
        return smtp.call_args[0][0] if smtp.called else []

    def msg(self, *to, **kw):
        return EmailMessage("s", "b", settings.DEFAULT_FROM_EMAIL, list(to), **kw)

    def test_both_on(self):
        PortalSettings.objects.update_or_create(
            pk=1, defaults={"client_mail": True, "staff_mail": True, "mail_from": "portal@example.com"}
        )
        sent = self.send(self.msg("ops@provider.example", "adm@client.kz"))
        self.assertEqual(sent[0].to, ["ops@provider.example", "adm@client.kz"])
        self.assertEqual(sent[0].from_email, "portal@example.com")

    def test_platform_users_off(self):
        PortalSettings.objects.update_or_create(pk=1, defaults={"client_mail": True, "staff_mail": False})
        sent = self.send(self.msg("ops@provider.example", "adm@client.kz"), self.msg("Ops@provider.example"))
        self.assertEqual([m.to for m in sent], [["adm@client.kz"]])
        self.assertEqual(AuditLog.objects.filter(action="mail_held").count(), 2)

    def test_organization_users_off(self):
        PortalSettings.objects.update_or_create(pk=1, defaults={"client_mail": False, "staff_mail": True})
        allowed = self.msg("adm@client.kz", "ops@provider.example", headers={"X-Portal-Allow-Client": "1"})
        sent = self.send(self.msg("ops@provider.example", "adm@client.kz"), allowed)
        self.assertEqual([m.to for m in sent], [["ops@provider.example"], ["adm@client.kz", "ops@provider.example"]])

    def test_both_off(self):
        PortalSettings.objects.update_or_create(pk=1, defaults={"client_mail": False, "staff_mail": False})
        allowed = self.msg("adm@client.kz", "ops@provider.example", headers={"X-Portal-Allow-Client": "1"})
        sent = self.send(self.msg("ops@provider.example", "adm@client.kz"), allowed)
        self.assertEqual([m.to for m in sent], [["adm@client.kz"]])  # the owner's allowed client only

    def test_expiration_notices_follow_mail_to_organization_users(self):
        """ "remove send expiration notices - filtered by settings above": e-mail to organization users decides; the
        old switch no longer does."""
        from accounts.tests import valid_bin
        from tenants import lifecycle
        from tenants.models import Organization, Tenant

        org = Organization.objects.create(name="Org", tax_id=valid_bin(), short_name="orgone")
        t = Tenant.objects.create(organization=org, name="p1", title="P", approval="approved")
        due = [(t, {"what": "contract", "days": 30, "date": "2026-12-17"})]
        admins = mock.patch.object(
            type(org), "admins", new_callable=mock.PropertyMock, return_value=[User.objects.get(email="adm@client.kz")]
        )
        PortalSettings.objects.update_or_create(pk=1, defaults={"client_mail": True, "notify_clients": False})
        with (
            mock.patch.object(lifecycle, "notices", return_value=due),
            admins,
            mock.patch("accounts.utils.notify") as n,
        ):
            out = lifecycle.run_daily()
        self.assertEqual((out["notices_sent"], n.call_count), (1, 1))
        PortalSettings.objects.filter(pk=1).update(client_mail=False, notify_clients=True)
        with (
            mock.patch.object(lifecycle, "notices", return_value=due),
            admins,
            mock.patch("accounts.utils.notify") as n,
        ):
            out = lifecycle.run_daily()
        self.assertEqual((out["notices_sent"], out["notices_held"], n.call_count), (0, 1, 0))


class Resolver(Base):
    def test_txt_and_a_answers(self):
        """The resolver of maildns reads A and TXT answers (a canned reply; no DNS in the tests)."""

        def reply(name, qtype, rdata_list):
            q = b"".join(bytes([len(x)]) + x.encode() for x in name.split(".")) + b"\0" + struct.pack(">HH", qtype, 1)
            body = b""
            for rd in rdata_list:
                body += b"\xc0\x0c" + struct.pack(">HHIH", qtype, 1, 60, len(rd)) + rd
            return lambda qid: struct.pack(">HHHHHH", qid, 0x8180, 1, len(rdata_list), 0, 0) + q + body

        class Sock:
            def __init__(self, make):
                self.make = make

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def settimeout(self, t):
                pass

            def sendto(self, data, addr):
                self.qid = struct.unpack(">H", data[:2])[0]

            def recvfrom(self, n):
                return self.make(self.qid), None

        txt = b"\x0ev=spf1 ip4:1.2" + b"\x0c.3.4 -all xx"
        with mock.patch("socket.socket", return_value=Sock(reply("example.com", 16, [txt]))):
            self.assertEqual(maildns.query("example.com", "TXT"), ["v=spf1 ip4:1.2.3.4 -all xx"])
        with mock.patch("socket.socket", return_value=Sock(reply("mail1.example.com", 1, [bytes([203, 0, 113, 10])]))):
            self.assertEqual(maildns.query("mail1.example.com", "A"), ["203.0.113.10"])


class RecordState(Base):
    """2026-11-17, production: "all records are valid" - a record is right when it does the job."""

    def test_states(self):
        spf = {"type": "TXT", "spf": True, "value": "v=spf1 ip4:192.0.2.5 ip4:198.51.100.5 -all"}
        self.assertEqual(maildns.state(spf, ["v=spf1 ip4:198.51.100.5 ip4:192.0.2.5 mx ~all"]), "ok")
        self.assertEqual(maildns.state(spf, ["v=spf1 ip4:192.0.2.2 ip4:198.51.100.2 -all"]), "other")
        self.assertEqual(maildns.state(spf, ["google-site-verification=x"]), "missing")
        # commit-041: the order of the mechanisms and one record only
        self.assertEqual(maildns.state(spf, ["v=spf1 -all ip4:192.0.2.5 ip4:198.51.100.5"]), "other")
        self.assertEqual(maildns.state(spf, ["v=spf1 ip4:192.0.2.5 ip4:198.51.100.5 -all", "v=spf1 mx -all"]), "other")
        self.assertEqual(maildns.state(spf, ["v=spf1 include:_spf.example.com -all"]), "other")  # check by hand
        self.assertEqual(maildns.state(spf, ["v=spf1 +ip4:192.0.2.5 ip4:198.51.100.5 ~all"]), "ok")
        self.assertEqual(maildns.state(spf, ["v=spf1ip4:192.0.2.5"]), "missing")  # not an SPF record
        # the first matching mechanism decides, with its qualifier
        self.assertEqual(maildns.state(spf, ["v=spf1 -ip4:192.0.2.5 ip4:192.0.2.5 ip4:198.51.100.5 -all"]), "other")
        self.assertEqual(maildns.state(spf, ["v=spf1 ip4:192.0.2.0/24 ip4:198.51.100.0/24 -all"]), "ok")
        self.assertEqual(
            maildns.state(spf, ["v=spf1 mx ip4:192.0.2.5 ip4:198.51.100.5 -all"]), "other"
        )  # not looked up here: check by hand
        self.assertEqual(
            maildns.state(spf, ["v=spf1 include:nonexistent.invalid ip4:192.0.2.5 ip4:198.51.100.5 -all"]), "other"
        )  # an include of a domain without SPF ends the evaluation with an error (permerror) first
        self.assertEqual(
            maildns.state(spf, ["v=spf1 ip4:192.0.2.5 ip4:198.51.100.5 include:x.example -all"]), "ok"
        )  # after both addresses are decided
        self.assertEqual(
            maildns.state(spf, ["v=spf1 -mx ip4:192.0.2.5 ip4:198.51.100.5 -all"]), "other"
        )  # might reject
        self.assertEqual(maildns.state(spf, ["v=spf1 ip4:192.0.2.5 ip4:198.51.100.5 redirect=_spf.example.com"]), "ok")
        self.assertEqual(maildns.state(spf, ["v=spf1 ip4:192.0.2.5"]), "other")  # the second node: no match
        dkim = {"type": "TXT", "value": "v=DKIM1; k=rsa; p=MIIBkey"}
        self.assertEqual(maildns.state(dkim, ["v=DKIM1; h=sha256; k=rsa; p=MIIB" + "key"]), "ok")  # extra tag, split
        self.assertEqual(maildns.state(dkim, ["v=DKIM1; k=rsa; p=OTHER"]), "other")
        dmarc = {"type": "TXT", "dmarc": True, "value": "v=DMARC1; p=quarantine"}
        self.assertEqual(maildns.state(dmarc, ["v=DMARC1; p=none"]), "ok")  # the policy is the provider's choice
        self.assertEqual(maildns.state(dmarc, []), "missing")
        a = {"type": "A", "value": "192.0.2.5"}
        self.assertEqual(maildns.state(a, ["192.0.2.2"]), "other")
        self.assertEqual(maildns.state({"type": "PTR", "value": "mail1.example.com"}, ["mail1.example.com."]), "ok")

    def test_detected_value_only_for_other_value(self):
        """Owner 2026-11-17 18:19: "under dns records show detected values in case if other value detexcted"; 18:37: in
        the Now column, the values DNS has instead of the words "other value"."""

        recs = [
            {
                "name": "_dmarc.example.com",
                "type": "TXT",
                "value": "v=DMARC1; p=quarantine",
                "state": "ok",
                "now": ["v=DMARC1; p=none"],
            },
            {
                "name": "example.com",
                "type": "TXT",
                "value": "v=spf1 ip4:192.0.2.5 -all",
                "state": "other",
                "now": ["v=spf1 mx -all"],
            },
            {"name": "mail1.example.com", "type": "A", "value": "192.0.2.5", "state": "missing", "now": []},
        ]
        self.client.force_login(self.staff("sa2@corp.kz", is_super_admin=True))
        from accounts.testkit import otp

        otp(self.client)
        with (
            mock.patch.object(maildns, "nodes", return_value=NODES),
            mock.patch.object(maildns, "records", return_value=recs),
        ):
            page = self.client.get(reverse("portal_settings") + "?tab=mail").content.decode()
        # 18:37: "in column NOW don show other vcalue text but show real other detected values"
        self.assertIn("<small>v=spf1 mx -all</small></span>", page)
        self.assertNotIn("DNS has:", page)
        self.assertNotIn('<span class="st warn">other value</span>', page)
        self.assertNotIn("p=none", page)


class SplitHorizon(Base):
    """Owner 2026-11-23 11:48: the nodes' DNS holds a local copy of the sender's zone without the TXT records - when it
    has no answer, the check asks external DNS."""

    def test_external_dns_when_the_local_one_has_no_answer(self):
        s = PortalSettings.get()
        s.mail_from = "portal@example.com"
        s.mail_public_ips = {"dc1": "203.0.113.10"}
        s.save()
        spf = "v=spf1 ip4:203.0.113.10 -all"
        asked = []

        def fake(name, rtype, timeout=1.5, servers=None):
            asked.append((name, rtype, servers))
            if rtype == "PTR":
                return ["mail1.example.com"] if servers is None else []  # the local DNS knows it
            if servers is None:
                return []  # the local copy of the zone has none of the others
            return {("A", "mail1.example.com"): ["203.0.113.10"], ("TXT", "example.com"): [spf]}.get((rtype, name), [])

        with mock.patch.object(maildns, "query", side_effect=fake):
            recs = {(r["type"], r["name"]): r for r in maildns.records(s, NODES[:1])}
        a, txt, ptr = (
            recs[("A", "mail1.example.com")],
            recs[("TXT", "example.com")],
            recs[("PTR", "10.113.0.203.in-addr.arpa")],
        )
        self.assertEqual((a["state"], a["external"]), ("ok", True))
        self.assertEqual((txt["state"], txt["external"]), ("ok", True))
        self.assertEqual((ptr["state"], ptr["external"]), ("ok", False))
        self.assertNotIn(("10.113.0.203.in-addr.arpa", "PTR", maildns.EXTERNAL), asked)  # local answered: not asked
        dmarc = recs[("TXT", "_dmarc.example.com")]
        self.assertEqual((dmarc["state"], dmarc["external"]), ("missing", False))  # neither has it

    def test_404_an_unrelated_local_txt_does_not_stop_the_external_check(self):
        """commit-404: the local copy of the zone has a site-verification TXT only; public DNS has the SPF."""
        s = PortalSettings.get()
        s.mail_from = "portal@example.com"
        s.mail_public_ips = {"dc1": "203.0.113.10"}
        s.save()

        def fake(name, rtype, timeout=1.5, servers=None):
            if (rtype, name) != ("TXT", "example.com"):
                return []
            return ["google-site-verification=abc"] if servers is None else ["v=spf1 ip4:203.0.113.10 -all"]

        with mock.patch.object(maildns, "query", side_effect=fake):
            spf = {(r["type"], r["name"]): r for r in maildns.records(s, NODES[:1])}[("TXT", "example.com")]
        self.assertEqual((spf["state"], spf["external"]), ("ok", True))

        def other(name, rtype, timeout=1.5, servers=None):  # a local SPF with another value: the local one stands
            if (rtype, name) != ("TXT", "example.com"):
                return []
            return ["v=spf1 ip4:192.0.2.1 -all"] if servers is None else ["v=spf1 ip4:203.0.113.10 -all"]

        with mock.patch.object(maildns, "query", side_effect=other):
            spf = {(r["type"], r["name"]): r for r in maildns.records(s, NODES[:1])}[("TXT", "example.com")]
        self.assertEqual((spf["state"], spf["external"]), ("other", False))
