"""Read tenant groups from every configured OpenNebula endpoint (read-only).

Run by `claugine-collect.timer` with `--if-due`, which obeys the period in Settings rather than the timer's own
interval, so a missed tick costs minutes and not a day.
"""
from django.core.management.base import BaseCommand

from adoption.collector import collect_all


class Command(BaseCommand):
    help = "Read tenant groups from every configured OpenNebula endpoint (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--if-due", action="store_true",
                            help="Only when the period in Settings (tenants refresh, minutes) has passed since the last read.")

    def handle(self, *args, **opts):
        from accounts import ha
        if not ha.is_active():
            self.stdout.write(f"this portal node is {ha.role()}, not active: nothing to do")
            return
        if opts.get("if_due"):  # corrections-18: the "platform" timer (Settings → Data) reads them
            self.stdout.write("not due: the platforms are read by the platform timer (check_platforms)")
            return
        results = collect_all()
        for key, res in results.items():
            self.stdout.write(f"{key}: {res}")
        if results:
            try:  # the igw VMs (the address after the egw's on the main subnet) and their type (2026-10-29)
                from tenants.igwdetect import detect
                found = detect()
                self.stdout.write(f"igw VMs: {sum(1 for _t, f, _n in found if f)} of {len(found)} found")
            except Exception as exc:  # noqa: BLE001 - the refresh itself is done
                self.stdout.write(f"igw VMs: {type(exc).__name__}: {exc}")
