"""Print a one-time link that creates the first super admin (valid 24 hours).

Run once at install, when no super admin exists yet, so no password is ever passed on a command line.
"""
from django.conf import settings
from django.core.management.base import BaseCommand
from django.urls import reverse
from django.utils import timezone

from accounts.models import Invitation, User


class Command(BaseCommand):
    help = "Print a one-time link that creates a super admin account (valid 24 hours)."

    def add_arguments(self, parser):
        parser.add_argument("--email", default="", help="Fix the super admin email (optional).")
        parser.add_argument("--force", action="store_true", help="Issue a link even if a super admin exists.")

    def handle(self, *args, email, force, **opts):
        if User.objects.filter(is_super_admin=True, is_active=True).exists() and not force:
            self.stderr.write("A super admin already exists. Use --force to issue another link.")
            return
        Invitation.objects.open().filter(purpose=Invitation.Purpose.STAFF, grants__super_admin=True).update(
            revoked_at=timezone.now())
        _, token = Invitation.issue(purpose=Invitation.Purpose.STAFF, email=email,
                                    grants={"super_admin": True, "roles": []}, ttl_hours=24)
        self.stdout.write(f"{settings.SITE_URL}{reverse('accept_invitation', args=[token])}")
