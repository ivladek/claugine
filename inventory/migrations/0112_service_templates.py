"""Owner 2026-11-22 16:59 / 17:08 / 17:46 (T34): service profiles use templates for their installation and every config
file; a template of a service has its own key (role), a kind (config, steps, script) and the scope "service"."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("inventory", "0111_dns_handoff")]

    operations = [
        migrations.AlterField(
            model_name="configtemplate", name="role", field=models.CharField(max_length=40, unique=True)
        ),
        migrations.AddField(
            model_name="configtemplate",
            name="kind",
            field=models.CharField(
                choices=[
                    ("network", "network function or host service configuration"),
                    ("config", "config file of a service"),
                    ("steps", "installation, steps"),
                    ("script", "installation, script"),
                ],
                default="network",
                max_length=10,
                verbose_name="kind",
            ),
        ),
        migrations.AlterField(
            model_name="configtemplate",
            name="os",
            field=models.CharField(
                choices=[("vyos", "VyOS"), ("frr", "FRR"), ("ubuntu", "Ubuntu")],
                default="vyos",
                help_text="What the configuration is for: VyOS routers or FRR on KVM hosts.",
                max_length=16,
                verbose_name="target",
            ),
        ),
        migrations.AddField(
            model_name="serviceprofile",
            name="install_template",
            field=models.CharField(blank=True, max_length=40, verbose_name="installation"),
        ),
        migrations.AddField(model_name="serviceprofile", name="configs", field=models.JSONField(blank=True, default=list)),
        migrations.AddField(model_name="serviceprofile", name="nics", field=models.JSONField(blank=True, default=list)),
    ]
