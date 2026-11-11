"""The accounts app: people, sessions, roles, and the portal's own machinery.
"""
from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"

    def ready(self):
        # owner 2026-11-04: outbound e-mail must never carry the node's internal *.portal.internal OS FQDN. Django stamps
        # the Message-ID from socket.getfqdn() (the internal name); use the public host from settings (SITE_URL, e.g.
        # portal.example.com) instead, so it aligns with the From domain and the example.com identity.
        from urllib.parse import urlsplit
        from django.conf import settings
        import django.core.mail.utils as mail_utils
        host = urlsplit(getattr(settings, "SITE_URL", "") or "").hostname
        if host:
            mail_utils.DNS_NAME._fqdn = host
