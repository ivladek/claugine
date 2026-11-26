"""T36 commit-507 (owner 2026-11-25 18:10): each name server profile's slot - ns-primary the primary site's 1st DNS
server, ns-secondary its 2nd, ns-site a non-primary site's. Reversible: the field goes."""

from django.db import migrations, models

SLOTS = {"ns-primary": 1, "ns-secondary": 2, "ns-site": 0}


def forward(apps, schema_editor):
    SP = apps.get_model("inventory", "ServiceProfile")
    for key, slot in SLOTS.items():
        SP.objects.filter(key=key, stype="ns").update(dns_slot=slot)


class Migration(migrations.Migration):
    dependencies = [("inventory", "0124_t36_primary_site")]

    operations = [
        migrations.AddField(
            model_name="serviceprofile",
            name="dns_slot",
            field=models.PositiveSmallIntegerField(
                blank=True,
                choices=[
                    (1, "the primary site's 1st DNS server"),
                    (2, "the primary site's 2nd DNS server"),
                    (0, "a non-primary site's DNS server"),
                ],
                null=True,
                verbose_name="DNS slot",
            ),
        ),
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
