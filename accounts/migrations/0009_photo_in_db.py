"""Photos move into the database (the portal is restored from a database backup alone)."""
from django.db import migrations, models


def copy_photos(apps, schema_editor):
    from django.core.files.storage import default_storage
    from accounts.models import photo_bytes
    User = apps.get_model("accounts", "User")
    for u in User.objects.exclude(photo="").exclude(photo__isnull=True):
        try:
            with default_storage.open(u.photo.name, "rb") as f:
                data, ctype = photo_bytes(f)
        except Exception:  # noqa: BLE001 - a missing or broken file leaves no photo
            continue
        User.objects.filter(pk=u.pk).update(photo_data=data, photo_type=ctype)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0008_tasks")]

    operations = [
        migrations.AddField("user", "photo_data", models.BinaryField(blank=True, default=b"", editable=False,
                                                                       verbose_name="photo")),
        migrations.AddField("user", "photo_type", models.CharField(blank=True, editable=False, max_length=32)),
        migrations.RunPython(copy_photos, migrations.RunPython.noop),
        migrations.RemoveField("user", "photo"),
    ]
