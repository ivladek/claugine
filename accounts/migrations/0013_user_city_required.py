"""Owner 2026-10-29 20:24: every user has a city; a missing one is Astana."""
from django.db import migrations, models


def fill(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    User.objects.filter(models.Q(city="") | models.Q(city__isnull=True)).update(city="astana")


class Migration(migrations.Migration):
    dependencies = [("accounts", "0012_corrections18")]

    operations = [
        migrations.RunPython(fill, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="user", name="city",
            field=models.CharField(choices=__import__("accounts.kz", fromlist=["CITIES"]).CITIES, default="astana",
                                   max_length=32, verbose_name="city"),
        ),
    ]
