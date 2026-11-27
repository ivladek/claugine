"""T34 S3b (owner 2026-11-23): Q24 "vm counts specied in each service profile" - ServiceProfile.vms; Q23 "A: VyOS
profiles" - a type may have several profiles, the VyOS services get theirs (each naming its VyOS template); every
site service is linked to the profile it is made from (CoreService.profile)."""

from django.db import migrations, models

ORIGIN = "0116"
VMS = {"ns": 2, "logs": 1, "onefe": 3, "admin": 3, "backup": 1}  # the owner's examples: 3 per RR, 3 admin, 1 backup
VYOS = [  # key, name, type, its VyOS template, VMs, NICs
    ("rr-inet", "Route reflector, IX", "bgp-rr", "rr-inet", 3, ["core-vyos", "service"]),
    ("rr-corpnet", "Route reflector, IX corpnet", "bgp-rr", "rr-corpnet", 3, ["core-vyos", "service"]),
    ("rr-vxlan", "Route reflector, EVPN", "bgp-rr", "rr-vxlan", 3, ["core-vyos", "service"]),
    ("cloud-gw", "Cloud egw / igw", "cloud-gw", "igw", 2, []),
    ("portal-gw", "Portal gateways", "portal-gw", "egw", 2, []),
]


def rr_profile(svc):
    """The route reflector profile of a bgp-rr service: by its template, else by its network as core_types picks it."""
    role = svc.template.role if svc.template_id else ""
    if role in ("rr-inet", "rr-corpnet", "rr-vxlan"):
        return role
    Network = svc.__class__._meta.apps.get_model("inventory", "Network")
    net = Network.objects.filter(pk=(svc.params or {}).get("network") or 0).first()
    if net is None:
        return "rr-inet"
    return "rr-vxlan" if net.kind == "evpn" else ("rr-corpnet" if net.name.startswith("corpnet") else "rr-inet")


def forwards(apps, schema_editor):
    ServiceProfile = apps.get_model("inventory", "ServiceProfile")
    CoreService = apps.get_model("inventory", "CoreService")
    for stype, n in VMS.items():
        ServiceProfile.objects.filter(stype=stype, vms=None).update(vms=n)
    seeded = {}  # commit-409: a seeded key held by a profile of another type or template keeps it; ours gets -vyos
    for key, name, stype, tpl, vms, nics in VYOS:
        k = key
        while True:
            have = ServiceProfile.objects.filter(key=k).first()
            if have is None or (have.stype == stype and have.install_template == tpl):
                break
            k += "-vyos"
        if have is None:
            have = ServiceProfile.objects.create(
                key=k,
                name=name,
                stype=stype,
                services=[],
                vcpu=2,
                ram_gb=4,
                vms=vms,
                install_template=tpl,
                nics=[{"network": x} for x in nics],
                legacy={"origin": ORIGIN},
            )
        seeded[key] = have
    by_type = {}
    for p in ServiceProfile.objects.order_by("pk"):
        by_type.setdefault(p.stype, p)
    for svc in CoreService.objects.filter(profile=None).select_related("template"):
        if svc.stype == "bgp-rr":
            prof = seeded.get(rr_profile(svc))
        elif svc.stype in ("cloud-gw", "portal-gw"):
            prof = seeded.get(svc.stype)
        else:
            prof = by_type.get(svc.stype)  # a base-OS type: its one profile
        if prof is not None and prof.stype == svc.stype:
            svc.profile = prof
            svc.save(update_fields=["profile"])


def backwards(apps, schema_editor):
    """commit-410: 0115's state has one profile per type - stop before any change when the profiles 0116 did not make
    would break it."""
    from django.core.exceptions import ImproperlyConfigured

    ServiceProfile = apps.get_model("inventory", "ServiceProfile")
    CoreService = apps.get_model("inventory", "CoreService")
    types = {}
    for p in ServiceProfile.objects.order_by("key"):  # in Python: exclude() on a JSON key drops rows without it
        if (p.legacy or {}).get("origin") != ORIGIN:
            types.setdefault(p.stype, []).append(p.key)
    two = {t: keys for t, keys in types.items() if len(keys) > 1}
    if two:
        raise ImproperlyConfigured(
            "0116 cannot be reversed: before it a type had one service profile; delete or merge these first: "
            + "; ".join(f"{t}: {', '.join(keys)}" for t, keys in sorted(two.items()))
        )
    CoreService.objects.update(profile=None)
    ServiceProfile.objects.filter(legacy__origin=ORIGIN).delete()


class Migration(migrations.Migration):
    dependencies = [("inventory", "0115_profile_nic_rows")]

    operations = [
        migrations.AddField(
            model_name="serviceprofile",
            name="vms",
            field=models.PositiveSmallIntegerField(blank=True, null=True, verbose_name="VMs"),
        ),
        migrations.AlterField(
            model_name="serviceprofile",
            name="stype",
            field=models.CharField(help_text="a core service type, or host", max_length=16, verbose_name="for"),
        ),
        migrations.AddField(
            model_name="coreservice",
            name="profile",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.PROTECT,
                related_name="site_services",
                to="inventory.serviceprofile",
            ),
        ),
        migrations.RunPython(forwards, backwards),
    ]
