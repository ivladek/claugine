"""commit-177: the baseline revision."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0105_internal_domain'),
    ]

    operations = [
        migrations.AddField(
            model_name='portalsettings',
            name='baseline_rev',
            field=models.PositiveIntegerField(default=1, verbose_name='baseline revision'),
        ),
    ]
