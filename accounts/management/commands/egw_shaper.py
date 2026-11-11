"""Correction 17: configure tenants' egw bandwidth as the shaper of the current template (the old limiter goes).

    manage.py egw_shaper --tenant acme --dry-run     what would change on one egw
    manage.py egw_shaper --tenant acme               change it
    manage.py egw_shaper --all [--dry-run]             every deployed tenant with an egw
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Configure the egw shaper of tenants from the current template (correction 17)."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", action="append", default=[], help="tenant name (several allowed)")
        parser.add_argument("--all", action="store_true", help="every deployed tenant with an egw")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **o):
        from tenants.configcheck import targets
        from tenants.models import Tenant
        from tenants.routercfg import egw_shaper
        qs = Tenant.objects.exclude(status__in=["archived", "saved", "deleting"]).exclude(one_group_id__isnull=True)
        if o["tenant"]:
            qs = qs.filter(name__in=o["tenant"])
        elif not o["all"]:
            raise CommandError("--tenant NAME or --all")
        ok = failed = 0
        for t in qs.order_by("name"):
            if not any(r == "egw" for r, *_x in targets(t)):
                continue
            try:
                self.stdout.write(f"{t.name}: {egw_shaper(t, dry=o['dry_run'])}")
                ok += 1
            except Exception as exc:  # noqa: BLE001 - one router's failure does not stop the others
                failed += 1
                self.stdout.write(f"{t.name}: FAILED {type(exc).__name__}: {str(exc)[:300]}")
        self.stdout.write(f"done: {ok} ok, {failed} failed")
