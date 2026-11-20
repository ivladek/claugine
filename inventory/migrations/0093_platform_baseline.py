"""Owner 2026-11-18 (T25): the last baseline check of each platform (inventory/baseline.py)."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0092_templates_rrs_explicit'),
    ]

    operations = [
        migrations.AddField(
            model_name='platform',
            name='baseline',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
