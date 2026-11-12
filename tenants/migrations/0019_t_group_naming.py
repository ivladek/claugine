from django.core.validators import RegexValidator
from django.db import migrations, models

MAX = 14


def shorten(apps, schema_editor):
    """Organization short names longer than 14 characters are regenerated (none is used by a group yet)."""
    from tenants.naming import compact, _free
    Organization = apps.get_model("tenants", "Organization")
    taken = {o.short_name for o in Organization.objects.exclude(short_name__isnull=True) if len(o.short_name) <= MAX}
    for org in Organization.objects.exclude(short_name__isnull=True):
        if len(org.short_name) > MAX:
            new = _free(compact(org.name, MAX) or org.short_name[:MAX], taken, MAX)
            taken.add(new)
            org.short_name = new
            org.save(update_fields=["short_name"])


class Migration(migrations.Migration):
    dependencies = [("tenants", "0018_rm_approval_switch")]
    operations = [
        migrations.RunPython(shorten, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="organization", name="short_name",
            field=models.CharField(blank=True, help_text="Used in tenant IDs: _t_<short name>_<tenant>. Fixed once a tenant group exists.",
                                   max_length=14, null=True, unique=True, verbose_name="short name",
                                   validators=[RegexValidator("^[a-z][a-z0-9]{2,13}$", "3-14 characters: lowercase latin letters and digits, starting with a letter.")]),
        ),
    ]
