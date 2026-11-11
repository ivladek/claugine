"""The portal appliance timer (claugine-appliance.timer, every 5 minutes, as root): keeps the internal mail relay in
line with Settings and makes the backups that are due (or asked for with "Back up now")."""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Apply the appliance settings and back up the portal when due."

    def add_arguments(self, parser):
        parser.add_argument("--backup-now", action="store_true", help="make a backup now, whatever the schedule")
        parser.add_argument("--dry-run", action="store_true", help="only say what would change")

    def handle(self, *args, **opts):
        from accounts import appliance
        appliance.tick(log=lambda m: self.stdout.write(m), force=opts["backup_now"], dry=opts["dry_run"])
