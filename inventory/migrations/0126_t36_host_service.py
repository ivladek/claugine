"""T36 B4a (owner 2026-11-25 19:22, rules v2 B1-B4): each host service profile gets its host slot (kvm-mgmt,
kvm-simple-fe, kvm-simple, kvm-shared, kvm-shared-gpu as 0122 named them) and each host in a cluster the host service
it is made from (inventory/hostservice.py, the same rules: a mgmt platform's hosts kvm-mgmt; a simple platform's first
three by name kvm-simple-fe, the rest kvm-simple; the others kvm-shared, or kvm-shared-gpu with GPUs). A host in no
cluster gets none (it is chosen when it is deployed). Reversible: the fields go."""

from django.db import migrations, models

SLOTS = {
    "kvm-mgmt": "mgmt",
    "kvm-simple-fe": "simple-fe",
    "kvm-simple": "simple",
    "kvm-shared": "shared",
    "kvm-shared-gpu": "shared-gpu",
}


def forward(apps, schema_editor):
    SP = apps.get_model("inventory", "ServiceProfile")
    Host = apps.get_model("inventory", "Host")
    Platform = apps.get_model("inventory", "Platform")
    by_slot = {}
    for key, slot in SLOTS.items():
        p = SP.objects.filter(key=key, stype="host").first()
        if p is None and slot == "shared":  # renamed since 0122: the type's own profile (made before T36)
            p = next(
                (
                    x
                    for x in SP.objects.filter(stype="host").order_by("pk")
                    if not (x.legacy or {}).get("t36", {}).get("made")
                ),
                None,
            )
        if p is not None and not p.host_slot:
            p.host_slot = slot
            p.save(update_fields=["host_slot"])
            by_slot[slot] = p
    done = set()
    for pl in Platform.objects.order_by("pk"):
        hosts = list(Host.objects.filter(clusters__platform=pl).distinct().order_by("name"))
        for i, h in enumerate(hosts):
            if h.pk in done:
                continue
            if pl.kind == "mgmt":
                slot = "mgmt"
            elif pl.kind == "simple":
                slot = "simple-fe" if i < 3 else "simple"
            else:
                gpus = bool(h.profile_id and h.profile.gpu_count)
                slot = "shared-gpu" if gpus else "shared"
            p = by_slot.get(slot)
            if p is not None:
                Host.objects.filter(pk=h.pk).update(service=p)
                done.add(h.pk)
    print(
        f"\n  inventory 0126: host slots {', '.join(f'{p.key}={s}' for s, p in by_slot.items()) or 'none'}; "
        f"{len(done)} hosts given their host service"
    )


class Migration(migrations.Migration):
    dependencies = [("inventory", "0125_t36_dns_slot")]

    operations = [
        migrations.AddField(
            model_name="serviceprofile",
            name="host_slot",
            field=models.CharField(
                blank=True,
                choices=[
                    ("mgmt", "a mgmt platform's hosts"),
                    ("simple-fe", "a simple platform's hosts 1-3 (with the front end)"),
                    ("simple", "a simple platform's hosts 4-6"),
                    ("shared", "a shared or external platform's hosts"),
                    ("shared-gpu", "a shared or external platform's hosts with GPUs"),
                ],
                max_length=12,
                verbose_name="host slot",
            ),
        ),
        migrations.AddField(
            model_name="host",
            name="service",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name="host_machines",
                to="inventory.serviceprofile",
            ),
        ),
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
