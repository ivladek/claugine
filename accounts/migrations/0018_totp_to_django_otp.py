"""Two-factor keys move to django-otp's TOTPDevice (owner 2026-11-11: the same tools as claugine-web). Nobody enrols
again: the key, whether it is set up, and the last accepted step move as they are."""

import base64

from django.db import migrations


def forward(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    TOTPDevice = apps.get_model("otp_totp", "TOTPDevice")
    for u in User.objects.exclude(totp_secret=""):
        secret = u.totp_secret.strip().replace(" ", "").upper()
        try:
            key = base64.b32decode(secret + "=" * (-len(secret) % 8)).hex()
        except ValueError:
            continue
        TOTPDevice.objects.create(
            user_id=u.pk, name="authenticator", key=key, confirmed=u.totp_enabled, tolerance=1, last_t=u.totp_last_counter or -1
        )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0017_sa_change_op_required"), ("otp_totp", "0003_add_timestamps")]

    operations = [
        migrations.RunPython(forward, migrations.RunPython.noop),
        migrations.RemoveField(model_name="user", name="totp_secret"),
        migrations.RemoveField(model_name="user", name="totp_last_counter"),
    ]
