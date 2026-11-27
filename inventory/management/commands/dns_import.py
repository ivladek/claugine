"""Infrastructure → DNS: the one import of the present zones (owner 2026-11-21 12:42 - by Claude, once, while
building; T34 B5). Each file <zone>.zone in the folder is the canonical form of a zone of the present primary
(named-compilezone -s full -o <zone>.zone <zone> <its file>, read only there); what the portal does not make by itself
is added. Writes the portal's database: run on the active node only, after the owner's yes."""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from inventory import dns


class Command(BaseCommand):
    help = "Add the records of canonical zone files that Infrastructure → DNS does not make by itself."

    def add_arguments(self, parser):
        parser.add_argument("folder")

    def handle(self, folder, **opts):
        files = sorted(Path(folder).glob("*.zone"))
        if not files:
            raise CommandError(f"no *.zone files in {folder}")
        for f in files:
            zone, added, skipped = dns.import_zone(f.name[: -len(".zone")], f.read_text())
            self.stdout.write(f"{zone.name}: {added} added, {skipped} made by the portal or skipped")
