from django.db import migrations, models


class Migration(migrations.Migration):
    """owner 2026-11-03 (u11): the requester may pick a base IX subnet for an extra public prefix; blank = auto."""

    dependencies = [("tenants", "0083_l2_internal")]

    operations = [
        migrations.AddField(
            model_name="networkrequest",
            name="pool",
            field=models.CharField(blank=True, default="", max_length=64,
                                   help_text="inet/corpnet: the base IX subnet the requester chose; blank = auto"),
        ),
    ]
