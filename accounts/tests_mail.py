"""owner 2026-11-04: outbound e-mail must take its identity only from settings (SITE_URL / mail_from), never from the
node's internal OS FQDN (*.portal.internal). The Message-ID in particular used socket.getfqdn()."""
from urllib.parse import urlsplit

from django.conf import settings
from django.core.mail import EmailMessage
from django.test import TestCase


class MessageIdFromSettings(TestCase):
    def test_dns_name_is_the_site_url_host(self):
        """AccountsConfig.ready() points Django's Message-ID domain at the public host from settings."""
        import django.core.mail.utils as mail_utils
        host = urlsplit(settings.SITE_URL).hostname
        self.assertTrue(host)
        self.assertEqual(mail_utils.DNS_NAME.get_fqdn(), host)

    def test_message_id_uses_settings_host_not_internal_fqdn(self):
        host = urlsplit(settings.SITE_URL).hostname
        mid = EmailMessage("s", "b", "portal@portal.example.com", ["x@example.com"]).message()["Message-ID"]
        self.assertTrue(mid.rstrip(">").endswith("@" + host), mid)
        self.assertNotIn("portal.internal", mid)
        # no bare OS hostname either
        import socket
        self.assertNotIn(socket.gethostname(), mid)
