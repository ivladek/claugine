"""Encrypt every stored secret again with the first key of FIELD_ENCRYPTION_KEY (inventory/crypto.py).

manage.py rotate_keys [--dry-run]
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Encrypt every stored secret again with the first key of FIELD_ENCRYPTION_KEY."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="count what would change; change nothing")

    def handle(self, *args, **opts):
        from accounts.models import audit
        from inventory.crypto import rotate

        report = rotate(dry=opts["dry_run"])
        if not opts["dry_run"] and report:
            audit(None, "secrets_rotated", None, report=report)
        for k, v in report.items():
            self.stdout.write(f"{k}: {v}")
        self.stdout.write("nothing to rotate" if not report else ("would change" if opts["dry_run"] else "rotated"))
