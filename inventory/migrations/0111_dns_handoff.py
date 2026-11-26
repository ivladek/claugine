from django.db import migrations, models


def move_legacy(apps, schema_editor):
    """commit-367: a handoff kept the earlier way (DnsSettings.uncertain with why "handoff", c8dd0a7..23b2cdf) moves
    to its own field, with its node and time - a redeploy's uncertain mark can then no longer wipe it."""
    DnsSettings = apps.get_model("inventory", "DnsSettings")
    for st in DnsSettings.objects.all():
        u = st.uncertain or {}
        if u.get("why") == "handoff" and not st.handoff:
            st.handoff = {"node": u.get("node"), "from": u.get("from"), "at": u.get("at") or ""}
            st.uncertain = dict(u.get("uncertain") or {})  # commit-369: a redeploy's own mark carried back with it
            st.save(update_fields=["handoff", "uncertain"])


def move_back(apps, schema_editor):
    """commit-368: back to 0110 - a pending handoff returns to where 0110's code reads it (uncertain, why "handoff")
    before the field goes, so the warning stays."""
    DnsSettings = apps.get_model("inventory", "DnsSettings")
    for st in DnsSettings.objects.all():
        h = st.handoff or {}
        u = st.uncertain or {}
        # commit-370: already moved back for this handoff (a downgrade retried after its data step committed) - left
        # as it is, so the carried mark is never wrapped twice
        if h and u.get("why") == "handoff" and u.get("node") == h.get("node") and u.get("at") == (h.get("at") or ""):
            continue
        if h:
            st.uncertain = {
                "node": h.get("node"),
                "why": "handoff",
                "at": h.get("at") or "",
                "from": h.get("from"),
                # commit-369: a redeploy's own mark set at the same time rides along and comes back on upgrade
                **({"uncertain": dict(st.uncertain)} if st.uncertain else {}),
            }
            st.save(update_fields=["uncertain"])


class Migration(migrations.Migration):
    """T34 DNS, commit-362: a primary handoff kept apart from the uncertain mark; commit-367: a pending one moved."""

    dependencies = [
        ("inventory", "0110_core_service_recovery"),
    ]

    operations = [
        migrations.AddField(
            model_name="dnssettings",
            name="handoff",
            field=models.JSONField(blank=True, default=dict, help_text="{node, from, at}"),
        ),
        migrations.RunPython(move_legacy, move_back),
    ]
