"""Owner 2026-11-18 (T25): the hosts' DNS domain and the OpenNebula repository for host deployments."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0103_platform_baseline'),
    ]

    operations = [
        migrations.AddField(
            model_name='portalsettings',
            name='hosts_domain',
            field=models.CharField(blank=True, default='', max_length=100, verbose_name="hosts' domain"),
        ),
        migrations.AddField(
            model_name='portalsettings',
            name='one_repo',
            field=models.URLField(default='https://downloads.opennebula.io/repo', verbose_name='OpenNebula repository'),
        ),
        migrations.AddField(
            model_name='portalsettings',
            name='one_repo_login',
            field=models.CharField(blank=True, default='', max_length=200, verbose_name='OpenNebula repository login'),
        ),
    ]
