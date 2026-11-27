"""Owner 2026-11-26 12:09 ("any refence use only this id"; scope "B5 now, rest as a job"): the references B5 touches
are ids - a service profile role's functions (roles[].functions) and a function's requires, both HostService ids
instead of keys. A key with no row stays out and is named. Reversible (the keys come back from the ids)."""

from django.db import migrations, models


def _convert(apps, to_id):
    SP = apps.get_model("inventory", "ServiceProfile")
    HS = apps.get_model("inventory", "HostService")
    by_key = dict(HS.objects.values_list("key", "pk"))
    by_id = {v: k for k, v in by_key.items()}
    m = by_key if to_id else by_id
    gone = []

    def conv(xs, where):
        out = []
        for x in xs or []:
            if (isinstance(x, int) and to_id) or (isinstance(x, str) and not to_id):  # already done
                out.append(x)
            elif x in m:
                out.append(m[x])
            else:
                gone.append(f"{where}: {x}")
        return out

    for p in SP.objects.all():
        roles = [
            {**r, "functions": conv(r.get("functions"), f"service {p.key}")} if isinstance(r, dict) else r
            for r in p.roles or []
        ]
        if roles != (p.roles or []):
            p.roles = roles
            p.save(update_fields=["roles"])
    for h in HS.objects.all():
        req = conv(h.requires, f"function {h.key} requires")
        if req != (h.requires or []):
            h.requires = req
            h.save(update_fields=["requires"])
    if gone:
        print("\n  inventory 0131: no such function, left out - " + "; ".join(gone))


def forward(apps, schema_editor):
    _convert(apps, True)


def backward(apps, schema_editor):
    _convert(apps, False)


class Migration(migrations.Migration):
    dependencies = [("inventory", "0130_t36_graylog_split")]

    operations = [
        migrations.AlterField(
            model_name="hostservice",
            name="requires",
            field=models.JSONField(blank=True, default=list, help_text="ids of the functions it needs"),
        ),
        migrations.RunPython(forward, backward),
    ]
