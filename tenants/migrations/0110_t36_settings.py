"""T36 (owner 2026-11-24): the default size of the machines' logs (Profiles → OS) and the public SSH/RDP ports of the
admin VMs (Settings → Security, N3: 55274 and 55275)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tenants", "0109_baseline_vyos_repo_private")]

    operations = [
        migrations.AddField(
            model_name="portalsettings",
            name="log_size_mb",
            field=models.PositiveIntegerField(default=200, verbose_name="machines' logs kept, MB"),
        ),
        migrations.AddField(
            model_name="portalsettings",
            name="public_ssh_port",
            field=models.PositiveIntegerField(default=55274, verbose_name="public SSH port"),
        ),
        migrations.AddField(
            model_name="portalsettings",
            name="public_rdp_port",
            field=models.PositiveIntegerField(default=55275, verbose_name="public RDP port"),
        ),
    ]
