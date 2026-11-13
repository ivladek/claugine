"""The nightly round (`claugine-cleanup.timer`, 03:30): drop finished tasks, log entries and replaced record versions
older than the history period in Settings.

It is the only thing that removes from `AuditLog`, which is otherwise append-only.
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "The nightly round: finished tasks, log entries and replaced tenant record versions older than the history "
        "settings; saved tenants archived a month after saving; notices of contract and POC ends."
    )

    def handle(self, *args, **opts):
        from accounts import ha

        if not ha.is_active():
            self.stdout.write(f"this portal node is {ha.role()}, not active: nothing to do")
            return
        from accounts.tasks import cleanup
        from tenants import lifecycle
        from tenants.models import PortalSettings

        s = PortalSettings.get()
        done = cleanup(s.task_days or 10, s.log_days or 500)
        done["tenant_versions"] = lifecycle.cleanup_history(s.tenant_history_days or 730)
        done.update(lifecycle.run_daily())
        self.stdout.write(", ".join(f"{k} {v}" for k, v in done.items()))
