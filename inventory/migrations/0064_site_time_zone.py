"""Correction 09 (rewritten 2026-10-27 19:25): the site's time zone is an IANA one, checked on input — every site's
former value becomes one (almaty → Asia/Almaty; empty → Asia/Almaty); every VyOS template sets the time zone from the
site settings ("set system time-zone {{ time_zone }}"). The IPMI keeps taking its UTC offset from it. The jump host between the sites is cleared (every host is reached
from either site since 2026-10-27)."""
from django.db import migrations, models

LEGACY = {"almaty": "Asia/Almaty", "astana": "Asia/Almaty", "aqtobe": "Asia/Aqtobe", "atyrau": "Asia/Atyrau"}
LINE = "set system time-zone {{ time_zone }}\n"
OLD_COMMON = '{% for n in ntp %}set service ntp server {{ n }}\n{% endfor %}'
DESCRIPTIONS = {'egw': "Provider VM in front of a tenant's main public network. eth1 the internet exchange (zone IX): iBGP to the exchange's route reflectors, connected and static routes redistributed (the tenant's routed networks via its igw). eth2 the tenant's public subnets (zone CLIENT): to and from the internet with martians dropped; the router answers them ping only. Bandwidth limited to the tenant's Mbps on eth1. Management: eth0 in VRF MGMT, zone MGMT — ping and ssh only from the management hosts (ssh nowhere else); time (NTP) and logs (syslog) go out through it. Time zone from the site settings. NIC addresses, MTU, host name (= VM name) and the login user come from the OpenNebula context at the first boot (the image's first-boot script).", 'igw': "The tenant's own router, under the tenant's full responsibility: EXT the main public network (ssh only from the hosts the tenant allows, with the tenant's keys), INT private networks behind SNAT, PUB extra public networks; no management NIC, no logs to the provider. The site gateway (core service cloud-igw) is built from it with the management part (eth0 in VRF MGMT, zone MGMT, ssh there only — none on EXT) and its own differences.", 'cgw': "Provider VM in front of a tenant's corporate network. eth1 the corporate exchange (zone IX): iBGP to its route reflectors. The tenant's corporate subnets (zone CLIENT): only to and from the exchange; the router answers them ping only. Management: eth0 in VRF MGMT, zone MGMT — ping and ssh only from the management hosts (ssh nowhere else); time (NTP) and logs (syslog) go out through it. Time zone from the site settings. NIC addresses, MTU, host name (= VM name) and the login user come from the OpenNebula context at the first boot (the image's first-boot script).", 'l2-a': "L2 channel, tenant side: bridges the tenant network (eth2) into a VXLAN tunnel to the external side, carried in WireGuard over the s2s network (eth1); keys and the pre-shared key made at random per channel and kept nowhere else. Zones: S2S (eth1) — WireGuard with the other side only; TUN (the WireGuard interface) — VXLAN with the other side only; the bridged frames are not filtered. Management: eth0 in VRF MGMT, zone MGMT — ping and ssh only from the management hosts (ssh nowhere else); time (NTP) and logs (syslog) go out through it. Time zone from the site settings. NIC addresses, MTU, host name (= VM name) and the login user come from the OpenNebula context at the first boot (the image's first-boot script).", 'l2-b': "L2 channel, external side: bridges the external VLAN (eth2) into the same VXLAN-in-WireGuard tunnel over the s2s network (eth1); it runs on the site's management platform. Zones: S2S (eth1) — WireGuard with the other side only; TUN (the WireGuard interface) — VXLAN with the other side only; the bridged frames are not filtered. Management: eth0 in VRF MGMT, zone MGMT — ping and ssh only from the management hosts (ssh nowhere else); time (NTP) and logs (syslog) go out through it. Time zone from the site settings. NIC addresses, MTU, host name (= VM name) and the login user come from the OpenNebula context at the first boot (the image's first-boot script).", 'l2-br': "L2 channel in one VM on the edge cluster of the tenant's platform: bridges the external VLAN (eth1) and the tenant network (eth2); no addresses on them, nothing reaches the router there. Management: eth0 in VRF MGMT, zone MGMT — ping and ssh only from the management hosts (ssh nowhere else); time (NTP) and logs (syslog) go out through it. Time zone from the site settings. NIC addresses, MTU, host name (= VM name) and the login user come from the OpenNebula context at the first boot (the image's first-boot script).", 'rr-inet': "Route reflector of the internet exchange (eth1, zone IX: BGP and ping with the exchange network only): eBGP to the uplinks, iBGP to the tenants' egw (route reflector clients, the uplinks' routes exported to them) and to the other route reflectors. Management: eth0 in VRF MGMT, zone MGMT — ping and ssh only from the management hosts (ssh nowhere else); time (NTP) and logs (syslog) go out through it. Time zone from the site settings. NIC addresses, MTU, host name (= VM name) and the login user come from the OpenNebula context at the first boot (the image's first-boot script).", 'rr-corpnet': "Route reflector of the corporate exchange (eth1, zone IX: BGP and ping with the exchange network only): eBGP to the corporate uplinks, iBGP to the tenants' cgw (route reflector clients) and to the other route reflectors. Management: eth0 in VRF MGMT, zone MGMT — ping and ssh only from the management hosts (ssh nowhere else); time (NTP) and logs (syslog) go out through it. Time zone from the site settings. NIC addresses, MTU, host name (= VM name) and the login user come from the OpenNebula context at the first boot (the image's first-boot script).", 'rr-vxlan': "Route reflector of the VXLAN/EVPN underlay (eth1, zone IX: BGP and ping with the underlay and its BGP peers only): iBGP (ipv4 and l2vpn-evpn) with the KVM hosts' FRR (route reflector clients) and with the other route reflectors. Management: eth0 in VRF MGMT, zone MGMT — ping and ssh only from the management hosts (ssh nowhere else); time (NTP) and logs (syslog) go out through it. Time zone from the site settings. NIC addresses, MTU, host name (= VM name) and the login user come from the OpenNebula context at the first boot (the image's first-boot script)."}
ROLES = ("egw", "igw", "cgw", "l2-a", "l2-b", "l2-br", "rr-inet", "rr-corpnet", "rr-vxlan")


def forward(apps, schema_editor):
    import zoneinfo
    zones = zoneinfo.available_timezones()
    Site = apps.get_model("inventory", "Site")
    for s in Site.objects.all():
        v = (s.timezone or "").strip()
        new = v if v in zones else LEGACY.get(v.lower(), "Asia/Almaty")
        if new != s.timezone or s.jump:
            s.timezone = new
            s.jump = {}  # no jump host between the sites any more (2026-10-27)
            s.save(update_fields=["timezone", "jump"])
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    for t in ConfigTemplate.objects.filter(role__in=ROLES):
        if "system time-zone" in t.body:
            continue
        if OLD_COMMON in t.body:  # the common part every VyOS template ends with (but the tenant igw)
            t.body = t.body.replace(OLD_COMMON, LINE + OLD_COMMON, 1)
        else:  # the tenant igw: at its end
            t.body = t.body.rstrip("\n") + "\n" + LINE
        t.save(update_fields=["body"])
    for role, desc in DESCRIPTIONS.items():
        ConfigTemplate.objects.filter(role=role).update(description=desc)


class Migration(migrations.Migration):
    dependencies = [("inventory", "0063_mgmt_time_and_logs")]
    operations = [
        migrations.AlterField(model_name="site", name="timezone",
                              field=models.CharField(blank=True, help_text="IANA time zone, e.g. Asia/Almaty", max_length=60)),
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
