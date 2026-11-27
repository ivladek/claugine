"""Owner 2026-11-18 (T25): a host deployment's run - steps, log, snapshot (inventory/hostdeploy.py)."""

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0094_disk_layouts'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='HostDeployment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('state', models.CharField(choices=[('running', 'running'), ('failed', 'failed'), ('done', 'deployed'), ('abandoned', 'abandoned')], default='running', max_length=16)),
                ('reset_factory', models.BooleanField(default=False)),
                ('power_confirmed', models.BooleanField(default=False)),
                ('steps', models.JSONField(blank=True, default=dict, help_text='{step: {status, at, detail}}')),
                ('log', models.TextField(blank=True)),
                ('snapshot', models.JSONField(blank=True, default=dict)),
                ('data', models.JSONField(blank=True, default=dict, help_text='what later steps need: seed, MACs, local storage, ids')),
                ('started_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('cluster', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='deployments', to='inventory.cluster')),
                ('host', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='deployments', to='inventory.host')),
                ('started_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-started_at'],
            },
        ),
    ]
