from django.db import migrations, models

ALIASES = (("nic-data1", "nic-data2"), ("nic-storage1", "nic-storage2"))


def forward(apps, schema_editor):
    """The bonds become an assignment of aliases: bond0 → nic-data1/2, bond1 → nic-storage1/2."""
    P = apps.get_model("inventory", "ServerProfile")
    for p in P.objects.all():
        n = dict(p.nics or {})
        bonds = n.get("bonds") or []
        if not bonds or n.get("assign"):
            continue
        assign = {}
        for b, names in zip(bonds[:2], ALIASES):
            for port, alias in zip(b.get("links") or [], names):
                assign[alias] = port
        n["assign"], n["fabrics"] = assign, 2 if len(bonds) > 1 else 1
        n.pop("bonds", None)
        p.nics = n
        p.save(update_fields=["nics"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0040_no_edge_choice")]
    operations = [
        migrations.AddField("serverprofile", "verified_at",
                            models.DateTimeField(blank=True, null=True, help_text="verified: only the volumes can change")),
        migrations.AddField("serverprofile", "verified_by", models.CharField(blank=True, max_length=150)),
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
