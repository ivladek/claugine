"""Load the inventory files (the current revision) into the portal's records.

    manage.py load_inventory --once     the first time only (the installer runs this)
    manage.py load_inventory            again: adds and updates records by name, removes nothing

After the load the portal's records are the source; the files are no longer read.
"""
from django.core.management.base import BaseCommand

from inventory.models import InventoryLoad, Revision


class Command(BaseCommand):
    help = "Load the current inventory revision into the portal's records (once, or again on request)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="do nothing if it was loaded before")
        parser.add_argument("--revision", type=int, help="load this revision instead of the current one")

    def handle(self, *args, **opts):
        if opts["once"] and InventoryLoad.objects.exists():
            self.stdout.write("inventory already loaded; the portal's records are the source")
            return
        rev = Revision.objects.filter(pk=opts["revision"]).first() if opts.get("revision") else Revision.current()
        if rev is None:
            self.stdout.write("no inventory revision to load")
            return
        from inventory.loader import load_revision
        done = load_revision(rev)
        from tenants.ordering import ensure_service_reservations
        added = ensure_service_reservations()
        self.stdout.write(f"revision #{rev.pk} loaded: " + ", ".join(f"{k} {v}" for k, v in done.counts.items()))
        self.stdout.write(f"service blocks added to the address registry: {added}")
        for line in done.report:
            self.stdout.write(f"  {line}")
