"""Owner 2026-11-23 16:31 (Q6): "rr-ix is the same for any network with extenal routers"; 16:32 (Q9): "lets compine it
to one rr-ix" - the VyOS templates rr-inet and rr-corpnet become one, rr-ix, and so do their service profiles.

Templates: rr-inet is renamed rr-ix (rr-corpnet when there is no rr-inet); every site service made with rr-corpnet is
made with rr-ix, and rr-corpnet is deleted. Its body was the same as rr-inet's when the portal wrote them (both RR_IX);
a body edited by hand is named in the output, as the earlier template migrations do.

Profiles (commit-429): the bgp-rr profile of rr-inet becomes rr-ix ("Route reflector, any network with external
routers"). Another IX route reflector profile is merged into it only when its deployment settings - vCPU, RAM, disk,
VMs, NICs - are the same; otherwise it stays, made with rr-ix, and is named in the output. What every profile was, the
merged ones whole with their services, and rr-corpnet's body and services are recorded in the surviving profile's
legacy["0117"], so the reverse puts them back as they were (commit-431: a key held since by another profile gets
0116's "-vyos")."""

from django.db import migrations

DESCRIPTION = "Route reflector, any network with external routers"
TEMPLATE_NAME = "Route reflector — an exchange with external routers"
SETTINGS = ("vcpu", "ram_gb", "disk_gb", "vms", "nics")
KEPT = ("key", "name", "install_template", "services", "own_lines", "checks", "configs", "notes") + SETTINGS


MAX_KEY = 40  # ServiceProfile.key


def _free(ServiceProfile, key, pk=None):
    """key, or - when another profile holds it - key-vyos, key-vyos2, ... within the field's 40 characters
    (commit-433)."""
    taken = ServiceProfile.objects.exclude(pk=pk)
    if not taken.filter(key=key).exists():
        return key
    n = 1
    while True:
        suffix = "-vyos" if n == 1 else f"-vyos{n}"
        cand = key[: MAX_KEY - len(suffix)] + suffix
        if not taken.filter(key=cand).exists():
            return cand
        n += 1


def _corp(svc, Network):
    """commit-432: a route reflector on the corporate exchange - its network (params, as 0116 read it), else NIC 1's."""
    net = Network.objects.filter(pk=(svc.params or {}).get("network") or 0).first()
    if net is None and svc.nic1_network_id:
        net = Network.objects.filter(pk=svc.nic1_network_id).first()
    return bool(net and net.name.startswith("corpnet"))


def forwards(apps, schema_editor):
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    CoreService = apps.get_model("inventory", "CoreService")
    ServiceProfile = apps.get_model("inventory", "ServiceProfile")
    record = {"templates": {"deleted": []}, "merged": [], "kept": []}
    inet = ConfigTemplate.objects.filter(role="rr-inet").first()
    corp = ConfigTemplate.objects.filter(role="rr-corpnet").first()
    ix = ConfigTemplate.objects.filter(role="rr-ix").first()
    if ix is None:
        ix = inet or corp
        if ix is not None:
            record["templates"]["renamed"] = {"pk": ix.pk, "role": ix.role, "name": ix.name}
            ix.role, ix.name = "rr-ix", TEMPLATE_NAME
            ix.save(update_fields=["role", "name"])
    if ix is not None:  # commit-432: the shared template, renamed or there before - the reverse finds it by its id
        record["templates"]["shared"] = ix.pk
        # commit-436: the services on it at the upgrade keep it on the reverse
        record["templates"]["on_shared"] = list(CoreService.objects.filter(template=ix).values_list("pk", flat=True))
    for old in (inet, corp):
        if old is None or ix is None or old.pk == ix.pk:
            continue
        if old.body.strip() != ix.body.strip():
            print(
                f"\n  inventory 0117: {old.role} differed from rr-ix (edited by hand); rr-ix kept, {old.role} deleted"
            )
        record["templates"]["deleted"].append(
            {
                "role": old.role,
                "name": old.name,
                "kind": old.kind,
                "os": old.os,
                "os_version": old.os_version,
                "description": old.description,
                "body": old.body,
                "services": list(CoreService.objects.filter(template=old).values_list("pk", flat=True)),
            }
        )
        CoreService.objects.filter(template=old).update(template=ix)
        old.delete()
    rr = [p for p in ServiceProfile.objects.filter(stype="bgp-rr").order_by("pk") if p.install_template != "rr-vxlan"]
    keep = next((p for p in rr if p.install_template in ("rr-inet", "rr-ix")), rr[0] if rr else None)
    if keep is None:
        return
    record["self"] = {f: getattr(keep, f) for f in KEPT}
    record["on_keep"] = list(CoreService.objects.filter(profile=keep).values_list("pk", flat=True))  # commit-436
    for p in rr:
        if p.pk == keep.pk:
            continue
        mine = {f: getattr(p, f) for f in KEPT}
        mine["legacy"] = p.legacy or {}  # commit-434: put back as it was (0116 may have reused an operator's profile)
        if all(getattr(p, f) == getattr(keep, f) for f in SETTINGS):
            mine["site_services"] = list(CoreService.objects.filter(profile=p).values_list("pk", flat=True))
            record["merged"].append(mine)
            CoreService.objects.filter(profile=p).update(profile=keep)
            p.delete()
        else:  # commit-429: its own settings stay; made with rr-ix from now on
            print(
                f"\n  inventory 0117: profile {p.key} kept (its size, VMs or NICs differ from rr-ix); made with rr-ix"
            )
            record["kept"].append({"pk": p.pk, "install_template": p.install_template})
            p.install_template = "rr-ix"
            p.save(update_fields=["install_template"])
    keep.key, keep.name, keep.install_template = _free(ServiceProfile, "rr-ix", keep.pk), DESCRIPTION, "rr-ix"
    keep.legacy = {**(keep.legacy or {}), "0117": record}
    keep.save(update_fields=["key", "name", "install_template", "legacy"])


def backwards(apps, schema_editor):
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    CoreService = apps.get_model("inventory", "CoreService")
    Network = apps.get_model("inventory", "Network")
    ServiceProfile = apps.get_model("inventory", "ServiceProfile")
    keep = next((p for p in ServiceProfile.objects.filter(stype="bgp-rr") if "0117" in (p.legacy or {})), None)
    record = (keep.legacy or {}).get("0117") if keep else None
    if record is None:
        return
    t = record.get("templates") or {}
    ix = ConfigTemplate.objects.filter(role="rr-ix").first()
    if ix is not None and t.get("renamed"):
        ix.role, ix.name = t["renamed"]["role"], t["renamed"]["name"]
        ix.save(update_fields=["role", "name"])
    deleted = t.get("deleted") or []
    if isinstance(deleted, dict):  # recorded by the first form of this migration (cc9250e)
        deleted = [deleted]
    corp = None
    for d in deleted:  # each template merged into rr-ix, with the services it had
        if ConfigTemplate.objects.filter(role=d["role"]).exists():
            continue
        made = ConfigTemplate.objects.create(
            role=d["role"],
            name=d["name"],
            kind=d["kind"],
            os=d["os"],
            os_version=d["os_version"],
            description=d["description"],
            body=d["body"],
        )
        CoreService.objects.filter(pk__in=d.get("services") or []).update(template=made)
        if made.role == "rr-corpnet":
            corp = made
    for k in record.get("kept") or []:
        ServiceProfile.objects.filter(pk=k["pk"]).update(install_template=k["install_template"])
    corp_profile = None
    for m in record.get("merged") or []:
        fields = {f: m[f] for f in KEPT if f in m}
        fields["key"] = _free(ServiceProfile, fields["key"])
        p = ServiceProfile.objects.create(stype="bgp-rr", legacy=m.get("legacy") or {}, **fields)  # commit-434
        CoreService.objects.filter(pk__in=m.get("site_services") or [], profile=keep).update(profile=p)
        if p.install_template == "rr-corpnet":
            corp_profile = p
    if corp_profile is None:  # a profile kept (its own settings) and made with rr-corpnet again
        corp_profile = (
            ServiceProfile.objects.filter(stype="bgp-rr", install_template="rr-corpnet").order_by("pk").first()
        )
    # commit-432: the route reflectors added since the upgrade - on the corporate exchange, rr-corpnet's template and
    # profile, as before rr-ix (0116's rule)
    corp = corp or ConfigTemplate.objects.filter(role="rr-corpnet").first()
    shared = t.get("shared") or (t.get("renamed") or {}).get("pk") or 0  # commit-432: renamed back or not
    renamed = ConfigTemplate.objects.filter(pk=shared).first()
    # commit-436/438: only links made since the upgrade, each link by its own history - a template recorded at the
    # upgrade keeps (or got back) its own, and so does a profile
    tpl_known = set(t.get("on_shared") or [])
    for d in deleted:
        tpl_known |= set(d.get("services") or [])
    prof_known = set(record.get("on_keep") or [])
    for m in record.get("merged") or []:
        prof_known |= set(m.get("site_services") or [])
    for svc in CoreService.objects.filter(stype="bgp-rr").select_related("template"):
        corp_net = _corp(svc, Network)
        fields = []
        if (
            corp_net
            and svc.pk not in tpl_known
            and corp is not None
            and renamed is not None
            and svc.template_id == renamed.pk
            and renamed.pk != corp.pk
        ):
            svc.template = corp
            fields.append("template")
        if svc.pk not in prof_known and svc.profile_id == keep.pk:
            # commit-440: a newer profile link follows the template the service has now (0116: the template first) - a
            # profile made with that template (the survivor's own record counts as it will be restored); the network
            # decides only when the template names neither
            role = svc.template.role if svc.template_id else ""
            target = None
            if role in ("rr-inet", "rr-corpnet"):
                if (record.get("self") or {}).get("install_template") == role:
                    target = keep
                elif role == "rr-corpnet":  # commit-441: the standard one the reverse chose (recreated first)
                    target = corp_profile
                else:
                    target = (
                        ServiceProfile.objects.filter(stype="bgp-rr", install_template=role)
                        .exclude(pk=keep.pk)
                        .order_by("pk")
                        .first()
                    )
            elif corp_net:
                target = corp_profile
            if target is not None and target.pk != keep.pk:
                svc.profile = target
                fields.append("profile")
        if fields:
            svc.save(update_fields=fields)
    me = record.get("self") or {}
    for f in KEPT:
        if f in me:
            setattr(keep, f, me[f])
    keep.key = _free(ServiceProfile, keep.key, keep.pk)  # commit-431: a key taken since gets "-vyos"
    legacy = dict(keep.legacy or {})
    legacy.pop("0117", None)
    keep.legacy = legacy
    keep.save()


class Migration(migrations.Migration):
    dependencies = [("inventory", "0116_profile_vms_vyos")]

    operations = [migrations.RunPython(forwards, backwards)]
