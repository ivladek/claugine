"""Owner 2026-11-17: the route reflectors of a network are exactly three addresses, …241, …242 and …243; nothing holds …240.

The default range `rrs` was …240 + 4 (…240/30), so choosing it in a network's BGP dialog listed …240 as a route reflector
too: the egw template then asked for a BGP neighbor nobody answers at, and a route reflector's template took …240 as its
own address. On every network whose `rrs` range is that default:

    range rrs            …240 + 4  → …241 + 3
    peer group rrs       …240/30 (or the four addresses) → …241, …242, …243, from the range
    route reflectors     …240 dropped (the list is …241, …242, …243)

A network whose range or lists are something else is left as it is and named in the output. Data only; rolling back keeps
the new values (the older code works with them)."""

import ipaddress

from django.db import migrations


def forward(apps, schema_editor):
    Network = apps.get_model("inventory", "Network")
    NetworkRange = apps.get_model("inventory", "NetworkRange")
    PeerGroup = apps.get_model("inventory", "PeerGroup")
    for n in Network.objects.all().order_by("site", "name"):
        r = NetworkRange.objects.filter(network=n, name="rrs").first()
        if r is None:
            continue
        try:
            net = ipaddress.ip_network(f"{n.subnet}/{n.prefix}", strict=True)
        except ValueError:
            continue
        if net.num_addresses < 244:
            continue
        base = net.network_address + 240
        old = [str(base + i) for i in range(4)]
        new = old[1:]
        label = f"{n.name}_{n.site}"
        if (r.start, r.length) == (str(base), 4):
            r.start, r.length = new[0], 3
            r.save(update_fields=["start", "length"])
            print(f"  inventory 0091: {label}: range rrs {old[0]} + 4 → {new[0]} + 3")
        elif (r.start, r.length) != (new[0], 3):
            print(f"  inventory 0091: {label}: range rrs is {r.start} + {r.length}: left as it is")
            continue
        g = PeerGroup.objects.filter(network=n, name="rrs").first()
        if g is not None:
            hosts = [str(x) for x in (g.hosts or [])]
            if hosts in ([f"{old[0]}/30"], old, new, []) or (not hosts and g.range_id == r.pk):
                if hosts != new or g.range_id != r.pk:
                    g.hosts, g.range = new, r
                    g.save(update_fields=["hosts", "range"])
                    print(
                        f"  inventory 0091: {label}: peer group rrs {', '.join(hosts) or '(the range)'} → {', '.join(new)}"
                    )
            else:
                print(f"  inventory 0091: {label}: peer group rrs is {', '.join(hosts)}: left as it is")
        rrs = [str(x) for x in (n.rrs or [])]
        if rrs and set(rrs) <= set(old) and rrs != new:
            n.rrs = new
            n.save(update_fields=["rrs"])
            print(f"  inventory 0091: {label}: route reflectors {', '.join(rrs)} → {', '.join(new)}")
        elif old[0] in rrs:
            n.rrs = [x for x in rrs if x != old[0]]
            n.save(update_fields=["rrs"])
            print(f"  inventory 0091: {label}: route reflector {old[0]} dropped")


class Migration(migrations.Migration):
    dependencies = [("inventory", "0090_platform_onegate_proxy")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
