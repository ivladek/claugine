"""Keys of the people who work on the infrastructure (Infrastructure → Secrets → Keys); OS user keys of an
OpenNebula front end that stayed a user (no oneadmin password was kept for it) move into its OpenNebula secret;
the jump hosts' account set is dropped (the jump hosts are in the sites)."""
from django.db import migrations
from django.utils import timezone

KEYS = ()  # no personal keys in the product (2026-10-30); add people's keys in Secrets → Keys
SU = ".service_user.oneadmin"


def forward(apps, schema_editor):
    Secret = apps.get_model("inventory", "Secret")
    now = timezone.now()
    for line in KEYS:
        t, b64, comment = line.split(" ", 2)
        if not Secret.objects.filter(kind="key", public_key__startswith=f"{t} {b64}").exists():
            Secret.objects.get_or_create(kind="key", name=comment, defaults={
                "key_type": t, "comment": comment, "public_key": line, "created_at": now,
                "purpose": "a person's key"})
    for u in Secret.objects.filter(kind="user", name__endswith=SU):
        fe = u.name[: -len(SU)]
        one, _c = Secret.objects.get_or_create(kind="opennebula", name=fe, defaults={
            "db_name": "onedb", "db_user": "onedbuser", "created_at": now})
        one.username = "oneadmin"
        if u.private_key_encrypted and not one.os_private_key_encrypted:
            one.os_private_key_encrypted, one.public_key = u.private_key_encrypted, u.public_key
        if u.password_encrypted and not one.os_password_encrypted:
            one.os_password_encrypted = u.password_encrypted
        one.save()
        u.delete()
    Secret.objects.filter(kind="user", name__startswith="cloud.admins.jumps").delete()


class Migration(migrations.Migration):
    dependencies = [("inventory", "0033_credentials_to_secrets")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
