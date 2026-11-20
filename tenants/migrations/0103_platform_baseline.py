"""Owner 2026-11-18 (T25): the platform baseline in Settings → Platforms - base OS Ubuntu 26.04, OpenNebula 7.4,
FRR 10.6 ("thiese data will use for any decisions")."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0102_prefix_own_network'),
    ]

    operations = [
        migrations.AddField(
            model_name='portalsettings',
            name='baseline_frr',
            field=models.CharField(default='10.6', max_length=8, verbose_name='FRR version'),
        ),
        migrations.AddField(
            model_name='portalsettings',
            name='baseline_one',
            field=models.CharField(default='7.4', max_length=8, verbose_name='OpenNebula version'),
        ),
        migrations.AddField(
            model_name='portalsettings',
            name='baseline_os',
            field=models.CharField(default='26.04', max_length=8, verbose_name='base OS (Ubuntu)'),
        ),
    ]
