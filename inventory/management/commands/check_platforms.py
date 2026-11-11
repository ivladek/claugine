"""Run the portal's timers that are due — portal, platforms, IPMI, VyOS (Settings → Data → Timers).

Despite the name it is the portal's clock, not only the platform check: `claugine-health.timer` calls it every five
minutes and `accounts/timers.py` decides from the database what is actually due. `--force` runs every timer now.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = ("Run the portal's timers that are due (corrections-18: portal, platforms, IPMI, VyOS — Settings → Data); "
            "--force: every one now.")

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Run every timer now.")

    def handle(self, *args, **opts):
        from accounts import ha, timers
        if not ha.is_active():
            self.stdout.write(f"this portal node is {ha.role()}, not active: nothing to do")
            return
        done = {k: timers.run(k) for k in timers.KEYS} if opts["force"] else timers.run_due()
        for key, (ok, detail) in done.items():
            self.stdout.write(f"{key}: {'ok' if ok else 'PROBLEM'} — {detail}")
        if not done:
            self.stdout.write("nothing due")
