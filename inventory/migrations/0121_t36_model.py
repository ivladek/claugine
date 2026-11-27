"""T36 (owner 2026-11-24/25, rules v2): one set of states for the infrastructure (planned → deployed ⇄ non-compliant →
retired), OpenNebula per platform, the cluster kind simple, Profiles → OS (OsProfile), functions (HostService: OS,
requires, version, VyOS template), each logged service's function, and services made of roles (ServiceProfile)."""

import django.db.models.deletion
from django.db import migrations, models

STATES = [("planned", "planned"), ("deployed", "deployed"), ("non-compliant", "non-compliant"), ("retired", "retired")]


class Migration(migrations.Migration):
    dependencies = [("inventory", "0120_vyos_logging")]

    operations = [
        migrations.AlterField(
            model_name="host", name="state", field=models.CharField(choices=STATES, default="deployed", max_length=16)
        ),
        migrations.AlterField(
            model_name="cluster", name="state", field=models.CharField(choices=STATES, default="deployed", max_length=16)
        ),
        migrations.AlterField(
            model_name="storage", name="state", field=models.CharField(choices=STATES, default="deployed", max_length=16)
        ),
        migrations.AlterField(
            model_name="coreservice",
            name="state",
            field=models.CharField(choices=STATES, default="deployed", max_length=16, verbose_name="state"),
        ),
        migrations.AlterField(
            model_name="network",
            name="state",
            field=models.CharField(choices=STATES, default="deployed", max_length=16, verbose_name="state"),
        ),
        migrations.AddField(
            model_name="platform",
            name="one_version",
            field=models.CharField(blank=True, default="", max_length=8, verbose_name="OpenNebula version"),
        ),
        migrations.AddField(
            model_name="platform",
            name="one_ee",
            field=models.BooleanField(default=False, verbose_name="OpenNebula Enterprise Edition"),
        ),
        migrations.AddField(
            model_name="platform",
            name="one_login",
            field=models.CharField(blank=True, default="", max_length=200, verbose_name="OpenNebula repository login"),
        ),
        migrations.AddField(
            model_name="hostservice", name="os", field=models.CharField(default="base-os", max_length=8, verbose_name="OS")
        ),
        migrations.AddField(
            model_name="site",
            name="layout",
            field=models.CharField(
                blank=True, choices=[("simple", "simple"), ("dedicated", "dedicated")], max_length=10, verbose_name="layout"
            ),
        ),
        migrations.AddField(
            model_name="hostservice", name="legacy", field=models.JSONField(blank=True, default=dict, editable=False)
        ),
        migrations.AddField(
            model_name="hostservice",
            name="requires",
            field=models.JSONField(blank=True, default=list, help_text="keys of the functions it needs"),
        ),
        migrations.AddField(
            model_name="hostservice",
            name="version",
            field=models.CharField(blank=True, max_length=32, verbose_name="version"),
        ),
        migrations.AddField(
            model_name="hostservice",
            name="vyos_template",
            field=models.CharField(blank=True, max_length=40, verbose_name="VyOS template"),
        ),
        migrations.AlterField(
            model_name="hostservice",
            name="group",
            field=models.CharField(
                choices=[
                    ("platform", "Platform"),
                    ("jump", "Jump host"),
                    ("system", "System"),
                    ("function", "Function"),
                    ("part", "Part of a function"),
                ],
                default="platform",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="loggedservice",
            name="function",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="logging_rules",
                to="inventory.hostservice",
            ),
        ),
        migrations.CreateModel(
            name="OsProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.SlugField(max_length=16, unique=True)),
                ("distro", models.CharField(max_length=40, verbose_name="distro")),
                ("version", models.CharField(max_length=32, verbose_name="version")),
                ("min_vcpu", models.PositiveSmallIntegerField(default=1, verbose_name="min vCPU")),
                ("min_ram_gb", models.PositiveSmallIntegerField(default=1, verbose_name="min RAM, GB")),
                ("min_disk_gb", models.PositiveSmallIntegerField(default=2, verbose_name="min disk, GB")),
                ("media_url", models.URLField(blank=True, max_length=500, verbose_name="installation media")),
                (
                    "logging",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="os_profiles",
                        to="inventory.loggedservice",
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["key"]},
        ),
        migrations.AddField(
            model_name="serviceprofile",
            name="usage",
            field=models.CharField(
                choices=[("site", "Site → Services"), ("project", "project automation")],
                default="site",
                max_length=8,
                verbose_name="used in",
            ),
        ),
        migrations.AddField(model_name="serviceprofile", name="roles", field=models.JSONField(blank=True, default=list)),
        migrations.AddField(
            model_name="serviceprofile",
            name="placement",
            field=models.CharField(blank=True, max_length=200, verbose_name="placed on"),
        ),
    ]
