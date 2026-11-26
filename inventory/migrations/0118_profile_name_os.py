"""Owner 2026-11-23 16:01 (Profiles updates U5-U7): the profile's key is shown as its Name (editable) and its name as
its Description; an OS - Base OS or VyOS - chosen when it is made (Q5): VyOS for the profiles of the VyOS service types
(bgp-rr, cloud-gw, portal-gw), Base OS for every other."""

from django.db import migrations, models

VYOS_STYPES = ("bgp-rr", "cloud-gw", "portal-gw")


def os_from_type(apps, schema_editor):
    ServiceProfile = apps.get_model("inventory", "ServiceProfile")
    ServiceProfile.objects.filter(stype__in=VYOS_STYPES).update(os="vyos")


class Migration(migrations.Migration):
    dependencies = [("inventory", "0117_rr_ix")]

    operations = [
        migrations.AlterModelOptions(name="serviceprofile", options={"ordering": ["key"]}),
        migrations.AlterField(
            model_name="serviceprofile",
            name="key",
            field=models.SlugField(max_length=40, unique=True, verbose_name="name"),
        ),
        migrations.AlterField(
            model_name="serviceprofile",
            name="name",
            field=models.CharField(blank=True, max_length=100, verbose_name="description"),
        ),
        migrations.AddField(
            model_name="serviceprofile",
            name="os",
            field=models.CharField(
                choices=[("base-os", "Base OS"), ("vyos", "VyOS")], default="base-os", max_length=8, verbose_name="OS"
            ),
        ),
        migrations.RunPython(os_from_type, migrations.RunPython.noop),
    ]
