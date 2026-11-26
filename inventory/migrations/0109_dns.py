"""T34 B5 (owner 2026-11-21 13:38-13:50): Infrastructure → DNS - the name servers' settings, the zones and the
records added to them (the automatic records are made from the portal's records, never stored)."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0108_onefe_type"),
    ]

    operations = [
        migrations.CreateModel(
            name="DnsZone",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=253, unique=True, verbose_name="zone")),
                (
                    "kind",
                    models.CharField(
                        choices=[("direct", "direct"), ("reverse", "reverse")],
                        default="direct",
                        max_length=8,
                        verbose_name="kind",
                    ),
                ),
                (
                    "source",
                    models.CharField(blank=True, help_text='"domain", "network:<id>" or "" (added)', max_length=40),
                ),
                ("notes", models.CharField(blank=True, max_length=300)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["kind", "name"]},
        ),
        migrations.CreateModel(
            name="DnsRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "name",
                    models.CharField(
                        help_text="Relative to the zone; @ for the zone itself.", max_length=253, verbose_name="name"
                    ),
                ),
                (
                    "rtype",
                    models.CharField(
                        choices=[
                            ("A", "A"),
                            ("AAAA", "AAAA"),
                            ("CNAME", "CNAME"),
                            ("MX", "MX"),
                            ("TXT", "TXT"),
                            ("SRV", "SRV"),
                            ("PTR", "PTR"),
                            ("NS", "NS"),
                            ("CAA", "CAA"),
                        ],
                        max_length=8,
                        verbose_name="type",
                    ),
                ),
                ("value", models.CharField(max_length=1000, verbose_name="value")),
                ("ttl", models.PositiveIntegerField(blank=True, null=True, verbose_name="TTL")),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "zone",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="records", to="inventory.dnszone"
                    ),
                ),
            ],
            options={"ordering": ["zone", "name", "rtype", "pk"]},
        ),
        migrations.CreateModel(
            name="DnsSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "forwarders",
                    models.JSONField(blank=True, default=list, help_text="addresses the servers forward to"),
                ),
                (
                    "forward_zones",
                    models.JSONField(blank=True, default=list, help_text='[{"name": "corp.example", "servers": [..]}]'),
                ),
                (
                    "sites",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text='{"<site>": {"internal": [prefixes], "public": [prefixes]}}: the views',
                    ),
                ),
                (
                    "internal_extra",
                    models.JSONField(blank=True, default=list, help_text="more internal prefixes (all sites)"),
                ),
                (
                    "notify_extra",
                    models.JSONField(blank=True, default=list, help_text="more servers that may transfer the zones"),
                ),
                (
                    "soa",
                    models.JSONField(
                        blank=True, default=dict, help_text="ttl, refresh, retry, expire, minimum (seconds)"
                    ),
                ),
                ("serial", models.PositiveBigIntegerField(default=0)),
                ("applied_at", models.DateTimeField(blank=True, null=True)),
                (
                    "applied",
                    models.JSONField(blank=True, default=dict, help_text="{zone: sha256 of the text applied}"),
                ),
                (
                    "last_check",
                    models.JSONField(blank=True, default=dict, help_text="the last Check: at, servers, zones"),
                ),
                (
                    "published",
                    models.JSONField(
                        blank=True, default=dict, help_text="{zone: zone file text} with the serial in it"
                    ),
                ),
                ("claim", models.JSONField(blank=True, default=dict)),
                ("reserved", models.PositiveBigIntegerField(default=0)),
                ("candidate", models.JSONField(blank=True, default=dict, help_text="{node, serial, texts}")),
                ("uncertain", models.JSONField(blank=True, default=dict, help_text="{node, why, at}")),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "primary",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="inventory.coreservicenode",
                        verbose_name="primary",
                    ),
                ),
            ],
        ),
    ]
