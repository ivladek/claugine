"""Owner 2026-11-17: no …240/30 in the templates — the route reflectors are listed one by one (ix.rrs).

    egw, cgw              network-group CLOUD-RR / CORPNET-RR (ix.rr_net, the /30) → address-group with each route
                          reflector; the BGP rules (GW-to-IX 11, IX-to-GW 12 and 13) refer to the address-group
    rr-inet, rr-corpnet   the same for {{ label }}-RR; no `bgp listen range` for the route reflectors (the other ones
                          are explicit neighbors)
    rr-vxlan              no `bgp listen range` for the route reflectors; the underlay group (CLOUD-IX) loses the
                          route reflectors' /30, which its own network already holds

A stored template is changed when it has the blocks as the portal wrote them; otherwise it is named in the output and
left for Infrastructure → Templates. Backward: the old blocks again."""

from django.db import migrations


def _group(label, var="ix.rr_net"):
    old = (
        f'set firewall group network-group {label}-RR description "BGP Route Reflectors"\n'
        f"set firewall group network-group {label}-RR network {{{{ {var} }}}}\n"
    )
    new = (
        f'set firewall group address-group {label}-RR description "BGP Route Reflectors"\n'
        f"{{% for rr in ix.rrs %}}set firewall group address-group {label}-RR address {{{{ rr }}}}\n{{% endfor %}}"
    )
    return old, new


def _rules(label):
    return [
        (
            f"set firewall ipv4 name GW-to-IX rule 11 destination group network-group {label}-RR",
            f"set firewall ipv4 name GW-to-IX rule 11 destination group address-group {label}-RR",
        ),
        (
            f"set firewall ipv4 name IX-to-GW rule 12 source group network-group {label}-RR",
            f"set firewall ipv4 name IX-to-GW rule 12 source group address-group {label}-RR",
        ),
        (
            f"set firewall ipv4 name IX-to-GW rule 13 source group network-group {label}-RR",
            f"set firewall ipv4 name IX-to-GW rule 13 source group address-group {label}-RR",
        ),
    ]


LISTEN_IX = (
    "{% endfor %}{% for n in rr_hosts %}set protocols bgp listen range {{ n }} peer-group {{ pg }}-rr\n"
    "{% endfor %}{% for rr in other_rrs %}",
    "{% endfor %}{% for rr in other_rrs %}",
)
LISTEN_VXLAN = (
    "{% endfor %}{% for n in rr_hosts %}set protocols bgp listen range {{ n }} peer-group cloud-rr\n"
    "{% endfor %}{% for rr in other_rrs %}",
    "{% endfor %}{% for rr in other_rrs %}",
)
ZONES_VXLAN = (
    "{% endfor %}{% for n in rr_hosts %}set firewall group network-group {{ label }}-IX network {{ n }}\n"
    "{% endfor %}set firewall ipv4 name GW-to-IX default-action drop",
    "{% endfor %}set firewall ipv4 name GW-to-IX default-action drop",
)

CHANGES = {
    "egw": [_group("CLOUD"), *_rules("CLOUD")],
    "cgw": [_group("CORPNET"), *_rules("CORPNET")],
    "rr-inet": [_group("{{ label }}"), LISTEN_IX],
    "rr-corpnet": [_group("{{ label }}"), LISTEN_IX],
    "rr-vxlan": [ZONES_VXLAN, LISTEN_VXLAN],
}


def _run(apps, back=False):
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for role, pairs in CHANGES.items():
        for t in ConfigTemplate.objects.filter(role=role):
            body = t.body
            todo = [(n, o) if back else (o, n) for o, n in pairs]
            if all(new in body and old not in body for old, new in todo):
                continue
            if not all(old in body for old, _new in todo):
                if not back:
                    print(
                        f"  inventory 0092: {role} template NOT changed (edited): list the route reflectors one by one "
                        "in Infrastructure → Templates"
                    )
                continue
            for old, new in todo:
                body = body.replace(old, new, 1)
            t.body = body
            t.save(update_fields=["body"])
            if not back:
                print(f"  inventory 0092: {role} template — route reflectors listed one by one, no /30")


def forward(apps, schema_editor):
    _run(apps)


def backward(apps, schema_editor):
    _run(apps, back=True)


class Migration(migrations.Migration):
    dependencies = [("inventory", "0091_rrs_explicit")]
    operations = [migrations.RunPython(forward, backward)]
