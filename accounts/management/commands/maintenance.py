"""manage.py maintenance close <op> | open <op> - the maintenance gate for the root scripts (claugine-add-node.sh)."""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Close or open the maintenance gate (accounts.maintenance)."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["close", "open"])
        parser.add_argument("op")

    def handle(self, action, op, **_kw):
        from accounts import maintenance

        if action == "close":
            try:
                maintenance.close(op)
            except RuntimeError as exc:
                raise CommandError(str(exc)) from exc
            self.stdout.write("closed")
        else:
            maintenance.open_(op)
            self.stdout.write("open")
