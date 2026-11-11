"""The old system's source (database path, ssh target, key, host keys) moves from portal.env and files into the
database: PortalSettings and the encrypted credential claugine.source.ssh. Runs once; empty settings do nothing."""
from django.conf import settings
from django.db import migrations
from django.utils import timezone


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, TypeError):
        return ""


def forward(apps, schema_editor):
    PortalSettings = apps.get_model("tenants", "PortalSettings")
    Credential = apps.get_model("inventory", "Credential")
    path = getattr(settings, "SOURCE_SQLITE_PATH", "") or ""
    target = getattr(settings, "SOURCE_SSH_TARGET", "") or ""
    if path or target:
        s, _c = PortalSettings.objects.get_or_create(pk=1)
        if not s.source_sqlite_path and not s.source_ssh_target:
            s.source_sqlite_path, s.source_ssh_target = path, target
            s.save(update_fields=["source_sqlite_path", "source_ssh_target"])
    key = _read(getattr(settings, "SOURCE_SSH_KEY", ""))
    known = _read(getattr(settings, "SOURCE_SSH_KNOWN_HOSTS", ""))
    if key and not Credential.objects.filter(key="claugine.source.ssh").exists():
        from inventory.credentials import _public_key
        from inventory.crypto import encrypt
        Credential.objects.create(
            key="claugine.source.ssh", kind="ssh_key", scope="old management system (read-only)",
            username=target.split("@")[0] if "@" in target else "", secret_encrypted=encrypt(key),
            public=_public_key(key), attrs={"known_hosts": known}, source="manual",
            description="ssh key of the daily read of the old system's database", created_at=timezone.now())


class Migration(migrations.Migration):
    dependencies = [("inventory", "0029_native_router_template"), ("tenants", "0040_old_system_source")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
