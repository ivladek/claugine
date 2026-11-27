"""owner 2026-11-03 (u11): L2 channel kinds — external (the existing ones) and internal (two project networks on two
sites). Adds the kind flag and the internal second endpoint (site_b, tenant_network_b)."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tenants", "0082_project_admins_backfill")]

    operations = [
        migrations.AddField("l2channel", "kind", models.CharField(
            max_length=10, default="external",
            choices=[("external", "external"), ("internal", "internal")])),
        migrations.AddField("l2channel", "site_b", models.CharField(max_length=16, blank=True, default="")),
        migrations.AddField("l2channel", "tenant_network_b", models.CharField(max_length=128, blank=True, default="")),
    ]
