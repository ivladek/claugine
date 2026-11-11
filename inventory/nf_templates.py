"""Built-in configuration templates, taken from the configurations of the running network services
(VyOS 2026.03, FRR). Addresses, names, ASNs, ports, users and secrets are variables; secrets are
never part of a template: they come from Infrastructure → Credentials when a configuration is built.

What the first boot already sets from the OpenNebula context (vyos-postconfig-bootup.script) is left out:
addresses, MTU and IPv6 options of the NICs, the default route, host name, time zone, the login user and its
key or password, the vyos user, ssh keep-alive/host validation/password login, commit revisions, system options
and syslog. Quoting: double quotes around descriptions, single quotes around passwords, no other quotes.
"""

COMMON_SYSTEM = """set system time-zone {{ time_zone }}
{% for n in ntp %}set service ntp server {{ n }}
{% endfor %}"""  # the time zone from the site settings (correction 09), also given in the context at the first boot

SSH = """delete service ssh port
{% for a in ssh_listen %}set service ssh listen-address {{ a }}
{% endfor %}set service ssh port {{ ssh_port }}"""

# Management VRF (all VyOS routers of the provider, not the tenant's igw): eth0 on the site's cloud-vyos network in
# VRF MGMT; ssh listens on every address of the VRF (no listen-address), on the site's VyOS management port, and
# only the management hosts reach it.
MGMT_BASE = """set firewall group network-group MGMT description "Management hosts"
{% for n in mgmt_hosts %}set firewall group network-group MGMT network {{ n }}
{% endfor %}set interfaces ethernet eth0 description "MGMT: {{ mgmt.net_name }}"
set interfaces ethernet eth0 vrf MGMT
delete service ssh port
set service ssh port {{ ssh_port }}
set service ssh vrf MGMT
{% if ntp %}set service ntp vrf MGMT
{% endif %}delete protocols static route 0.0.0.0/0
{% if mgmt.gw %}set vrf name MGMT protocols static route 0.0.0.0/0 next-hop {{ mgmt.gw }}
{% endif %}set vrf name MGMT table 100
{% if syslog %}set system syslog remote {{ syslog.host }} facility all level info
set system syslog remote {{ syslog.host }} port {{ syslog.port }}
set system syslog remote {{ syslog.host }} protocol {{ syslog.proto }}
set system syslog remote {{ syslog.host }} vrf MGMT
{% endif %}set system syslog marker disable
"""

# Every VyOS router: zone-based firewall, the router itself is the local zone GW and management is the zone MGMT
# (eth0 in VRF MGMT: VyOS 2026.03 matches the interface of a VRF only with the VRF a member too). Ping and ssh from
# the management hosts only; the same lines on every router (correction 2026-10-27: "all vyos must zone based
# firewall and unified access to mgmt interface").
MGMT_FW = """set firewall group port-group SSHPORT description "SSH port (management)"
set firewall group port-group SSHPORT port {{ ssh_port }}
{% if ntp %}set firewall group address-group NTP description "NTP servers (time)"
{% for n in ntp %}set firewall group address-group NTP address {{ n }}
{% endfor %}{% endif %}set firewall ipv4 name GW-to-MGMT default-action drop
set firewall ipv4 name GW-to-MGMT description "GW->MGMT: ping and replies to the management hosts, time, logs"
set firewall ipv4 name GW-to-MGMT rule 1 action accept
set firewall ipv4 name GW-to-MGMT rule 1 destination group network-group MGMT
set firewall ipv4 name GW-to-MGMT rule 1 protocol icmp
set firewall ipv4 name GW-to-MGMT rule 2 action accept
set firewall ipv4 name GW-to-MGMT rule 2 destination group network-group MGMT
set firewall ipv4 name GW-to-MGMT rule 2 state established
set firewall ipv4 name GW-to-MGMT rule 3 action accept
set firewall ipv4 name GW-to-MGMT rule 3 destination group network-group MGMT
set firewall ipv4 name GW-to-MGMT rule 3 state related
{% if ntp %}set firewall ipv4 name GW-to-MGMT rule 20 action accept
set firewall ipv4 name GW-to-MGMT rule 20 destination group address-group NTP
set firewall ipv4 name GW-to-MGMT rule 20 destination port 123
set firewall ipv4 name GW-to-MGMT rule 20 protocol udp
{% endif %}{% if syslog %}set firewall ipv4 name GW-to-MGMT rule 30 action accept
set firewall ipv4 name GW-to-MGMT rule 30 destination address {{ syslog.host }}
set firewall ipv4 name GW-to-MGMT rule 30 destination port {{ syslog.port }}
set firewall ipv4 name GW-to-MGMT rule 30 protocol {{ syslog.proto }}
{% endif %}set firewall ipv4 name MGMT-to-GW default-action drop
set firewall ipv4 name MGMT-to-GW description "MGMT->GW: ping and ssh from the management hosts, replies"
set firewall ipv4 name MGMT-to-GW rule 1 action accept
set firewall ipv4 name MGMT-to-GW rule 1 protocol icmp
set firewall ipv4 name MGMT-to-GW rule 1 source group network-group MGMT
set firewall ipv4 name MGMT-to-GW rule 2 action accept
set firewall ipv4 name MGMT-to-GW rule 2 state established
set firewall ipv4 name MGMT-to-GW rule 3 action accept
set firewall ipv4 name MGMT-to-GW rule 3 state related
set firewall ipv4 name MGMT-to-GW rule 10 action accept
set firewall ipv4 name MGMT-to-GW rule 10 destination group port-group SSHPORT
set firewall ipv4 name MGMT-to-GW rule 10 protocol tcp
set firewall ipv4 name MGMT-to-GW rule 10 source group network-group MGMT
set firewall zone GW from MGMT firewall name MGMT-to-GW
set firewall zone MGMT from GW firewall name GW-to-MGMT
set firewall zone GW local-zone
set firewall zone MGMT member interface eth0
set firewall zone MGMT member vrf MGMT
"""
MGMT_ZONE = MGMT_FW + MGMT_BASE

# before 2026-10-27 (routers without zones: the input filter let ssh in from the management hosts only); kept for the
# old L2 body and the migration
MGMT_FILTER = """set firewall ipv4 input filter rule 10 action accept
set firewall ipv4 input filter rule 10 destination port {{ ssh_port }}
set firewall ipv4 input filter rule 10 protocol tcp
set firewall ipv4 input filter rule 10 source group network-group MGMT
set firewall ipv4 input filter rule 11 action drop
set firewall ipv4 input filter rule 11 destination port {{ ssh_port }}
set firewall ipv4 input filter rule 11 protocol tcp
""" + MGMT_BASE

# eth1 (the VXLAN underlay): BGP, ping, established/related, only with the underlay and the BGP peers
RR_VXLAN_ZONES = """set firewall group network-group {{ label }}-IX description "VXLAN underlay network"
set firewall group network-group {{ label }}-IX network {{ ix.cidr }}
{% for n in int_hosts %}set firewall group network-group {{ label }}-IX network {{ n }}
{% endfor %}{% for n in rr_hosts %}set firewall group network-group {{ label }}-IX network {{ n }}
{% endfor %}set firewall ipv4 name GW-to-IX default-action drop
set firewall ipv4 name GW-to-IX description "GW->IX: BGP"
set firewall ipv4 name GW-to-IX rule 1 action accept
set firewall ipv4 name GW-to-IX rule 1 destination group network-group {{ label }}-IX
set firewall ipv4 name GW-to-IX rule 1 protocol icmp
set firewall ipv4 name GW-to-IX rule 11 action accept
set firewall ipv4 name GW-to-IX rule 11 destination group network-group {{ label }}-IX
set firewall ipv4 name GW-to-IX rule 11 destination port 179
set firewall ipv4 name GW-to-IX rule 11 protocol tcp
set firewall ipv4 name GW-to-IX rule 12 action accept
set firewall ipv4 name GW-to-IX rule 12 destination group network-group {{ label }}-IX
set firewall ipv4 name GW-to-IX rule 12 state established
set firewall ipv4 name GW-to-IX rule 13 action accept
set firewall ipv4 name GW-to-IX rule 13 destination group network-group {{ label }}-IX
set firewall ipv4 name GW-to-IX rule 13 state related
set firewall ipv4 name IX-to-GW default-action drop
set firewall ipv4 name IX-to-GW description "IX->GW: BGP"
set firewall ipv4 name IX-to-GW rule 1 action accept
set firewall ipv4 name IX-to-GW rule 1 protocol icmp
set firewall ipv4 name IX-to-GW rule 1 source group network-group {{ label }}-IX
set firewall ipv4 name IX-to-GW rule 11 action accept
set firewall ipv4 name IX-to-GW rule 11 destination port 179
set firewall ipv4 name IX-to-GW rule 11 protocol tcp
set firewall ipv4 name IX-to-GW rule 11 source group network-group {{ label }}-IX
set firewall ipv4 name IX-to-GW rule 12 action accept
set firewall ipv4 name IX-to-GW rule 12 source group network-group {{ label }}-IX
set firewall ipv4 name IX-to-GW rule 12 state established
set firewall ipv4 name IX-to-GW rule 13 action accept
set firewall ipv4 name IX-to-GW rule 13 source group network-group {{ label }}-IX
set firewall ipv4 name IX-to-GW rule 13 state related
set firewall zone GW from IX firewall name IX-to-GW
set firewall zone IX from GW firewall name GW-to-IX
set firewall zone IX member interface eth1
"""

# One side of an L2 channel: eth1 (s2s) carries only WireGuard with the other side, the WireGuard interface only VXLAN
# with the other side's tunnel address; the bridged frames (eth2 <-> vxlan) are not routed, the zones do not see them.
L2_ZONES = """set firewall group address-group S2S-PEER description "the other side on the s2s network"
set firewall group address-group S2S-PEER address {{ s2s.remote }}
set firewall group address-group TUN-PEER description "the other side in the WireGuard tunnel"
set firewall group address-group TUN-PEER address {{ wg.remote }}
set firewall ipv4 name GW-to-S2S default-action drop
set firewall ipv4 name GW-to-S2S description "GW->S2S: WireGuard to the other side"
set firewall ipv4 name GW-to-S2S rule 1 action accept
set firewall ipv4 name GW-to-S2S rule 1 destination group address-group S2S-PEER
set firewall ipv4 name GW-to-S2S rule 1 protocol icmp
set firewall ipv4 name GW-to-S2S rule 11 action accept
set firewall ipv4 name GW-to-S2S rule 11 destination group address-group S2S-PEER
set firewall ipv4 name GW-to-S2S rule 11 destination port {{ wg.port }}
set firewall ipv4 name GW-to-S2S rule 11 protocol udp
set firewall ipv4 name GW-to-S2S rule 12 action accept
set firewall ipv4 name GW-to-S2S rule 12 state established
set firewall ipv4 name GW-to-S2S rule 13 action accept
set firewall ipv4 name GW-to-S2S rule 13 state related
set firewall ipv4 name S2S-to-GW default-action drop
set firewall ipv4 name S2S-to-GW description "S2S->GW: WireGuard from the other side"
set firewall ipv4 name S2S-to-GW rule 1 action accept
set firewall ipv4 name S2S-to-GW rule 1 protocol icmp
set firewall ipv4 name S2S-to-GW rule 1 source group address-group S2S-PEER
set firewall ipv4 name S2S-to-GW rule 11 action accept
set firewall ipv4 name S2S-to-GW rule 11 destination port {{ wg.port }}
set firewall ipv4 name S2S-to-GW rule 11 protocol udp
set firewall ipv4 name S2S-to-GW rule 11 source group address-group S2S-PEER
set firewall ipv4 name S2S-to-GW rule 12 action accept
set firewall ipv4 name S2S-to-GW rule 12 source group address-group S2S-PEER
set firewall ipv4 name S2S-to-GW rule 12 state established
set firewall ipv4 name S2S-to-GW rule 13 action accept
set firewall ipv4 name S2S-to-GW rule 13 source group address-group S2S-PEER
set firewall ipv4 name S2S-to-GW rule 13 state related
set firewall ipv4 name GW-to-TUN default-action drop
set firewall ipv4 name GW-to-TUN description "GW->TUN: VXLAN to the other side"
set firewall ipv4 name GW-to-TUN rule 1 action accept
set firewall ipv4 name GW-to-TUN rule 1 destination group address-group TUN-PEER
set firewall ipv4 name GW-to-TUN rule 1 protocol icmp
set firewall ipv4 name GW-to-TUN rule 11 action accept
set firewall ipv4 name GW-to-TUN rule 11 destination group address-group TUN-PEER
set firewall ipv4 name GW-to-TUN rule 11 destination port 4789
set firewall ipv4 name GW-to-TUN rule 11 protocol udp
set firewall ipv4 name TUN-to-GW default-action drop
set firewall ipv4 name TUN-to-GW description "TUN->GW: VXLAN from the other side"
set firewall ipv4 name TUN-to-GW rule 1 action accept
set firewall ipv4 name TUN-to-GW rule 1 protocol icmp
set firewall ipv4 name TUN-to-GW rule 1 source group address-group TUN-PEER
set firewall ipv4 name TUN-to-GW rule 11 action accept
set firewall ipv4 name TUN-to-GW rule 11 destination port 4789
set firewall ipv4 name TUN-to-GW rule 11 protocol udp
set firewall ipv4 name TUN-to-GW rule 11 source group address-group TUN-PEER
set firewall zone GW from S2S firewall name S2S-to-GW
set firewall zone GW from TUN firewall name TUN-to-GW
set firewall zone S2S from GW firewall name GW-to-S2S
set firewall zone S2S member interface eth1
set firewall zone TUN from GW firewall name GW-to-TUN
set firewall zone TUN member interface wg{{ vni }}
"""

# Internet edge of a tenant: BGP to the route reflectors of the internet exchange, firewall zones IX / CLIENT / GW.
EGW = """set firewall group network-group CLIENT description "public subnets"
{% for n in client_networks %}set firewall group network-group CLIENT network {{ n }}
{% endfor %}set firewall group network-group CLOUD-IX description "Cloud Interexchange network"
set firewall group network-group CLOUD-IX network {{ ix.cidr }}
set firewall group network-group CLOUD-RR description "BGP Route Reflectors"
set firewall group network-group CLOUD-RR network {{ ix.rr_net }}
set firewall group network-group MARTIANS description "Internet restricted addresses"
{% for n in martians %}set firewall group network-group MARTIANS network {{ n }}
{% endfor %}set firewall ipv4 name CLIENT-to-GW default-action drop
set firewall ipv4 name CLIENT-to-GW description "CLIENT->GW: ping only"
set firewall ipv4 name CLIENT-to-GW rule 1 action accept
set firewall ipv4 name CLIENT-to-GW rule 1 protocol icmp
set firewall ipv4 name CLIENT-to-GW rule 1 source group network-group CLIENT
set firewall ipv4 name CLIENT-to-IX default-action drop
set firewall ipv4 name CLIENT-to-IX description "CLIENT->IX: normalize"
set firewall ipv4 name CLIENT-to-IX rule 1 action accept
set firewall ipv4 name CLIENT-to-IX rule 1 destination group network-group !MARTIANS
set firewall ipv4 name CLIENT-to-IX rule 1 source group network-group CLIENT
set firewall ipv4 name GW-to-CLIENT default-action drop
set firewall ipv4 name GW-to-CLIENT description "GW->CLIENT: ping only"
set firewall ipv4 name GW-to-CLIENT rule 1 action accept
set firewall ipv4 name GW-to-CLIENT rule 1 destination group network-group CLIENT
set firewall ipv4 name GW-to-CLIENT rule 1 protocol icmp
set firewall ipv4 name GW-to-IX default-action drop
set firewall ipv4 name GW-to-IX description "GW->IX: BGP"
set firewall ipv4 name GW-to-IX rule 1 action accept
set firewall ipv4 name GW-to-IX rule 1 destination group network-group CLOUD-IX
set firewall ipv4 name GW-to-IX rule 1 protocol icmp
set firewall ipv4 name GW-to-IX rule 11 action accept
set firewall ipv4 name GW-to-IX rule 11 destination group network-group CLOUD-RR
set firewall ipv4 name GW-to-IX rule 11 destination port 179
set firewall ipv4 name GW-to-IX rule 11 protocol tcp
set firewall ipv4 name IX-to-CLIENT default-action drop
set firewall ipv4 name IX-to-CLIENT description "IX->CLIENT: normalize"
set firewall ipv4 name IX-to-CLIENT rule 1 action accept
set firewall ipv4 name IX-to-CLIENT rule 1 destination group network-group CLIENT
set firewall ipv4 name IX-to-CLIENT rule 1 source group network-group !MARTIANS
set firewall ipv4 name IX-to-GW default-action drop
set firewall ipv4 name IX-to-GW description "IX->GW: BGP"
set firewall ipv4 name IX-to-GW rule 1 action accept
set firewall ipv4 name IX-to-GW rule 1 protocol icmp
set firewall ipv4 name IX-to-GW rule 1 source group network-group CLOUD-IX
set firewall ipv4 name IX-to-GW rule 12 action accept
set firewall ipv4 name IX-to-GW rule 12 source group network-group CLOUD-RR
set firewall ipv4 name IX-to-GW rule 12 state established
set firewall ipv4 name IX-to-GW rule 13 action accept
set firewall ipv4 name IX-to-GW rule 13 source group network-group CLOUD-RR
set firewall ipv4 name IX-to-GW rule 13 state related
set firewall zone CLIENT from GW firewall name GW-to-CLIENT
set firewall zone CLIENT from IX firewall name IX-to-CLIENT
set firewall zone CLIENT member interface eth2
set firewall zone GW from CLIENT firewall name CLIENT-to-GW
set firewall zone GW from IX firewall name IX-to-GW
set firewall zone IX from CLIENT firewall name CLIENT-to-IX
set firewall zone IX from GW firewall name GW-to-IX
set firewall zone IX member interface eth1
set interfaces ethernet eth1 description "{{ ix.name }}"
set interfaces ethernet eth2 description "{{ gw.net_name }}"
delete protocols static route 0.0.0.0/0
{% for n in routed_networks %}set protocols static route {{ n }} next-hop {{ igw_ip }}
{% endfor %}set protocols bgp address-family ipv4-unicast redistribute connected
set protocols bgp address-family ipv4-unicast redistribute static
{% for rr in ix.rrs %}set protocols bgp neighbor {{ rr }} peer-group cloud-int
{% endfor %}set protocols bgp parameters router-id {{ ix.ip }}
set protocols bgp peer-group cloud-int address-family ipv4-unicast soft-reconfiguration inbound
set protocols bgp peer-group cloud-int password '{{ ix.bgp_password }}'
set protocols bgp peer-group cloud-int remote-as internal
set protocols bgp peer-group cloud-int update-source {{ ix.ip }}
set protocols bgp system-as {{ ix.asn }}
set protocols bgp timers holdtime 15
set protocols bgp timers keepalive 3
{% if mbps %}set qos interface eth1 egress LIMIT-OUT
set qos interface eth2 egress LIMIT-OUT
set qos policy shaper LIMIT-OUT bandwidth {{ mbps }}mbit
set qos policy shaper LIMIT-OUT default bandwidth 100%
set qos policy shaper LIMIT-OUT default queue-type fq-codel
{% endif %}""" + MGMT_ZONE + "\n" + COMMON_SYSTEM + "\n"

# Correction 17 (2026-10-29): the egw's bandwidth is a shaper (fq-codel) on the egress of both sides — eth1 (IX: the
# tenant's upload) and eth2 (the tenant's main network: its download); the old ingress limiter and rate-control gave
# unpredictable drops. What the old block put on a router is deleted when the egw is configured (EGW_OBSOLETE).
EGW_QOS_OLD = """{% if mbps %}set qos interface eth1 egress LIMIT-OUT
set qos interface eth1 ingress LIMIT-IN
set qos policy limiter LIMIT-IN default bandwidth {{ mbps }}mbit
set qos policy rate-control LIMIT-OUT bandwidth {{ mbps }}mbit
{% endif %}"""
EGW_QOS = """{% if mbps %}set qos interface eth1 egress LIMIT-OUT
set qos interface eth2 egress LIMIT-OUT
set qos policy shaper LIMIT-OUT bandwidth {{ mbps }}mbit
set qos policy shaper LIMIT-OUT default bandwidth 100%
set qos policy shaper LIMIT-OUT default queue-type fq-codel
{% endif %}"""
EGW_OBSOLETE = ("qos interface eth1 ingress", "qos policy limiter LIMIT-IN", "qos policy rate-control LIMIT-OUT")

# Corporate network edge of a tenant: like the egw, on the corporate exchange; private addresses are allowed.
CGW = """set firewall group network-group CLIENT description "corporate subnets of the tenant"
{% for n in client_networks %}set firewall group network-group CLIENT network {{ n }}
{% endfor %}set firewall group network-group CORPNET-IX description "CorpNet Interexchange network"
set firewall group network-group CORPNET-IX network {{ ix.cidr }}
set firewall group network-group CORPNET-RR description "BGP Route Reflectors"
set firewall group network-group CORPNET-RR network {{ ix.rr_net }}
set firewall ipv4 name CLIENT-to-GW default-action drop
set firewall ipv4 name CLIENT-to-GW description "CLIENT->GW: ping only"
set firewall ipv4 name CLIENT-to-GW rule 1 action accept
set firewall ipv4 name CLIENT-to-GW rule 1 protocol icmp
set firewall ipv4 name CLIENT-to-IX default-action drop
set firewall ipv4 name CLIENT-to-IX description "CLIENT->IX: from the tenant subnets"
set firewall ipv4 name CLIENT-to-IX rule 1 action accept
set firewall ipv4 name CLIENT-to-IX rule 1 source group network-group CLIENT
set firewall ipv4 name GW-to-CLIENT default-action drop
set firewall ipv4 name GW-to-CLIENT description "GW->CLIENT: ping only"
set firewall ipv4 name GW-to-CLIENT rule 1 action accept
set firewall ipv4 name GW-to-CLIENT rule 1 protocol icmp
set firewall ipv4 name GW-to-IX default-action drop
set firewall ipv4 name GW-to-IX description "GW->IX: BGP"
set firewall ipv4 name GW-to-IX rule 1 action accept
set firewall ipv4 name GW-to-IX rule 1 destination group network-group CORPNET-IX
set firewall ipv4 name GW-to-IX rule 1 protocol icmp
set firewall ipv4 name GW-to-IX rule 11 action accept
set firewall ipv4 name GW-to-IX rule 11 destination group network-group CORPNET-RR
set firewall ipv4 name GW-to-IX rule 11 destination port 179
set firewall ipv4 name GW-to-IX rule 11 protocol tcp
set firewall ipv4 name IX-to-CLIENT default-action drop
set firewall ipv4 name IX-to-CLIENT description "IX->CLIENT: to the tenant subnets"
set firewall ipv4 name IX-to-CLIENT rule 1 action accept
set firewall ipv4 name IX-to-CLIENT rule 1 destination group network-group CLIENT
set firewall ipv4 name IX-to-GW default-action drop
set firewall ipv4 name IX-to-GW description "IX->GW: BGP"
set firewall ipv4 name IX-to-GW rule 1 action accept
set firewall ipv4 name IX-to-GW rule 1 protocol icmp
set firewall ipv4 name IX-to-GW rule 1 source group network-group CORPNET-IX
set firewall ipv4 name IX-to-GW rule 12 action accept
set firewall ipv4 name IX-to-GW rule 12 source group network-group CORPNET-RR
set firewall ipv4 name IX-to-GW rule 12 state established
set firewall ipv4 name IX-to-GW rule 13 action accept
set firewall ipv4 name IX-to-GW rule 13 source group network-group CORPNET-RR
set firewall ipv4 name IX-to-GW rule 13 state related
set firewall zone CLIENT from GW firewall name GW-to-CLIENT
set firewall zone CLIENT from IX firewall name IX-to-CLIENT
{% for n in corp_networks %}set firewall zone CLIENT member interface {{ n.iface }}
{% endfor %}set firewall zone GW from CLIENT firewall name CLIENT-to-GW
set firewall zone GW from IX firewall name IX-to-GW
set firewall zone IX from CLIENT firewall name CLIENT-to-IX
set firewall zone IX from GW firewall name GW-to-IX
set firewall zone IX member interface eth1
set interfaces ethernet eth1 description "{{ ix.name }}"
{% for n in corp_networks %}set interfaces ethernet {{ n.iface }} address {{ n.gw }}/{{ n.prefix_len }}
set interfaces ethernet {{ n.iface }} description "{{ n.net_name }}"
{% endfor %}delete protocols static route 0.0.0.0/0
set protocols bgp address-family ipv4-unicast redistribute connected
set protocols bgp address-family ipv4-unicast redistribute static
{% for rr in ix.rrs %}set protocols bgp neighbor {{ rr }} peer-group corpnet-int
{% endfor %}set protocols bgp parameters router-id {{ ix.ip }}
set protocols bgp peer-group corpnet-int address-family ipv4-unicast soft-reconfiguration inbound
set protocols bgp peer-group corpnet-int password '{{ ix.bgp_password }}'
set protocols bgp peer-group corpnet-int remote-as internal
set protocols bgp peer-group corpnet-int update-source {{ ix.ip }}
set protocols bgp system-as {{ ix.asn }}
set protocols bgp timers holdtime 15
set protocols bgp timers keepalive 3
""" + MGMT_ZONE + "\n" + COMMON_SYSTEM + "\n"

# Tenant gateway: EXT (main public network to the egw), INT (private networks, SNAT), PUB (extra public networks).
IGW = """set firewall group network-group INT description "INTernal networks - SNATted, not routed in Internet"
{% for n in int_networks %}set firewall group network-group INT network {{ n.cidr }}
{% endfor %}set firewall group network-group PUB description "PUBlic networks - delegated, Internet routed"
{% for n in pub_networks %}set firewall group network-group PUB network {{ n.cidr }}
{% endfor %}set firewall group network-group SSHFROM description "SSH: allowed IP"
{% for n in igw_ssh_from %}set firewall group network-group SSHFROM network {{ n }}
{% endfor %}set firewall group port-group SSHPORT description "GW SSH port"
set firewall group port-group SSHPORT port {{ ssh_port }}
set firewall ipv4 name EXT-to-GW default-action drop
set firewall ipv4 name EXT-to-GW description "EXT->GW: SSH access only from allowed hosts"
set firewall ipv4 name EXT-to-GW rule 1 action accept
set firewall ipv4 name EXT-to-GW rule 1 protocol icmp
set firewall ipv4 name EXT-to-GW rule 10 action accept
set firewall ipv4 name EXT-to-GW rule 10 destination address {{ ext.ip }}
set firewall ipv4 name EXT-to-GW rule 10 destination group port-group SSHPORT
set firewall ipv4 name EXT-to-GW rule 10 protocol tcp
set firewall ipv4 name EXT-to-GW rule 10 source group network-group SSHFROM
set firewall ipv4 name GW-to-EXT default-action drop
set firewall ipv4 name GW-to-EXT description "GW->EXT: any"
set firewall ipv4 name GW-to-EXT rule 1 action accept
set firewall ipv4 name GW-to-EXT rule 1 protocol icmp
set firewall ipv4 name GW-to-EXT rule 11 action accept
set firewall ipv4 name GW-to-EXT rule 11 destination group network-group SSHFROM
set firewall ipv4 name GW-to-EXT rule 11 state established
set firewall ipv4 name GW-to-EXT rule 12 action accept
set firewall ipv4 name GW-to-EXT rule 12 destination group network-group SSHFROM
set firewall ipv4 name GW-to-EXT rule 12 state related
set firewall ipv4 name GW-to-EXT rule 13 action drop
set firewall ipv4 name GW-to-EXT rule 13 destination group network-group SSHFROM
set firewall ipv4 name GW-to-EXT rule 13 state invalid
{% if int_networks %}set firewall ipv4 name EXT-to-INT default-action accept
set firewall ipv4 name EXT-to-INT description "EXT->INT: any"
set firewall ipv4 name GW-to-INT default-action accept
set firewall ipv4 name GW-to-INT description "GW->INT: any"
set firewall ipv4 name INT-to-EXT default-action accept
set firewall ipv4 name INT-to-EXT description "INT->EXT: any"
set firewall ipv4 name INT-to-GW default-action accept
set firewall ipv4 name INT-to-GW description "INT->GW: any"
{% endif %}{% if pub_networks %}set firewall ipv4 name EXT-to-PUB default-action accept
set firewall ipv4 name EXT-to-PUB description "EXT->PUB: any"
set firewall ipv4 name GW-to-PUB default-action drop
set firewall ipv4 name GW-to-PUB description "GW->PUB: deny"
set firewall ipv4 name GW-to-PUB rule 1 action accept
set firewall ipv4 name GW-to-PUB rule 1 protocol icmp
set firewall ipv4 name PUB-to-EXT default-action accept
set firewall ipv4 name PUB-to-EXT description "PUB->EXT: any"
set firewall ipv4 name PUB-to-GW default-action drop
set firewall ipv4 name PUB-to-GW description "PUB->GW: deny"
set firewall ipv4 name PUB-to-GW rule 1 action accept
set firewall ipv4 name PUB-to-GW rule 1 protocol icmp
{% endif %}{% if int_networks and pub_networks %}set firewall ipv4 name INT-to-PUB default-action accept
set firewall ipv4 name INT-to-PUB description "INT->PUB: any"
set firewall ipv4 name PUB-to-INT default-action accept
set firewall ipv4 name PUB-to-INT description "PUB->INT: any"
set firewall ipv4 name PUB-to-INT rule 11 action accept
set firewall ipv4 name PUB-to-INT rule 11 destination group network-group INT
set firewall ipv4 name PUB-to-INT rule 11 source group network-group PUB
set firewall ipv4 name PUB-to-INT rule 11 state established
set firewall ipv4 name PUB-to-INT rule 12 action accept
set firewall ipv4 name PUB-to-INT rule 12 destination group network-group INT
set firewall ipv4 name PUB-to-INT rule 12 source group network-group PUB
set firewall ipv4 name PUB-to-INT rule 12 state related
set firewall ipv4 name PUB-to-INT rule 13 action drop
set firewall ipv4 name PUB-to-INT rule 13 destination group network-group INT
set firewall ipv4 name PUB-to-INT rule 13 source group network-group PUB
set firewall ipv4 name PUB-to-INT rule 13 state invalid
{% endif %}set firewall zone EXT from GW firewall name GW-to-EXT
{% if int_networks %}set firewall zone EXT from INT firewall name INT-to-EXT
{% endif %}{% if pub_networks %}set firewall zone EXT from PUB firewall name PUB-to-EXT
{% endif %}set firewall zone EXT member interface eth0
set firewall zone GW from EXT firewall name EXT-to-GW
{% if int_networks %}set firewall zone GW from INT firewall name INT-to-GW
{% endif %}{% if pub_networks %}set firewall zone GW from PUB firewall name PUB-to-GW
{% endif %}set firewall zone GW local-zone
{% if int_networks %}set firewall zone INT from EXT firewall name EXT-to-INT
set firewall zone INT from GW firewall name GW-to-INT
{% if pub_networks %}set firewall zone INT from PUB firewall name PUB-to-INT
{% endif %}{% for n in int_networks %}set firewall zone INT member interface {{ n.iface }}
{% endfor %}{% endif %}{% if pub_networks %}set firewall zone PUB from EXT firewall name EXT-to-PUB
set firewall zone PUB from GW firewall name GW-to-PUB
{% if int_networks %}set firewall zone PUB from INT firewall name INT-to-PUB
{% endif %}{% for n in pub_networks %}set firewall zone PUB member interface {{ n.iface }}
{% endfor %}{% endif %}set interfaces ethernet eth0 description "EXT: {{ ext.net_name }}"
{% for n in lan_networks %}set interfaces ethernet {{ n.iface }} address {{ n.gw }}/{{ n.prefix_len }}
set interfaces ethernet {{ n.iface }} description "{{ n.zone }}: {{ n.net_name }}"
set interfaces ethernet {{ n.iface }} ipv6 address no-default-link-local
set interfaces ethernet {{ n.iface }} ipv6 disable-forwarding
{% endfor %}{% if int_networks %}set nat source rule 10 description "INT->INET"
set nat source rule 10 destination group network-group !PUB
set nat source rule 10 outbound-interface name eth0
set nat source rule 10 source group network-group INT
set nat source rule 10 translation address masquerade
{% endif %}{% if service_user %}set system login user {{ service_user.name }} authentication public-keys {{ service_user.name }} key {{ service_user.key }}
set system login user {{ service_user.name }} authentication public-keys {{ service_user.name }} type {{ service_user.type }}
{% endif %}{% for k in igw_keys %}set system login user {{ igw_user }} authentication public-keys {{ k.name }} key {{ k.key }}
set system login user {{ igw_user }} authentication public-keys {{ k.name }} type {{ k.type }}
{% endfor %}{% if not igw_keys %}delete service ssh disable-password-authentication
{% endif %}set system time-zone {{ time_zone }}
set system syslog marker disable
""" + SSH + "\n"

# One side of an L2 channel: a bridge of eth1 and a VXLAN tunnel to the other side over the s2s network.
L2 = """set interfaces bridge br{{ vni }} ipv6 address no-default-link-local
set interfaces bridge br{{ vni }} ipv6 disable-forwarding
set interfaces bridge br{{ vni }} member interface eth2
set interfaces bridge br{{ vni }} member interface vxlan{{ vni }}
set interfaces bridge br{{ vni }} mtu {{ mtu }}
set interfaces bridge br{{ vni }} stp
set interfaces ethernet eth1 description "{{ s2s.name }}"
set interfaces ethernet eth2 description "{{ side_label }}: {{ attached }}"
set interfaces vxlan vxlan{{ vni }} ipv6 address no-default-link-local
set interfaces vxlan vxlan{{ vni }} ipv6 disable-forwarding
set interfaces vxlan vxlan{{ vni }} mtu {{ vxlan_mtu }}
set interfaces vxlan vxlan{{ vni }} port 4789
set interfaces vxlan vxlan{{ vni }} remote {{ wg.remote }}
set interfaces vxlan vxlan{{ vni }} source-address {{ wg.local }}
set interfaces vxlan vxlan{{ vni }} vni {{ vni }}
set interfaces wireguard wg{{ vni }} address {{ wg.local }}/30
set interfaces wireguard wg{{ vni }} description "L2 tunnel to {{ wg.peer }}"
set interfaces wireguard wg{{ vni }} mtu {{ wg.mtu }}
set interfaces wireguard wg{{ vni }} port {{ wg.port }}
set interfaces wireguard wg{{ vni }} private-key '{{ wg.private_key }}'
set interfaces wireguard wg{{ vni }} peer {{ wg.peer }} address {{ s2s.remote }}
set interfaces wireguard wg{{ vni }} peer {{ wg.peer }} allowed-ips {{ wg.remote }}/32
set interfaces wireguard wg{{ vni }} peer {{ wg.peer }} persistent-keepalive 25
set interfaces wireguard wg{{ vni }} peer {{ wg.peer }} port {{ wg.port }}
set interfaces wireguard wg{{ vni }} peer {{ wg.peer }} preshared-key '{{ wg.preshared_key }}'
set interfaces wireguard wg{{ vni }} peer {{ wg.peer }} public-key '{{ wg.peer_public_key }}'
""" + L2_ZONES + MGMT_ZONE + "\n" + COMMON_SYSTEM + "\n"

L2_PLAIN = """set interfaces bridge br{{ vni }} ipv6 address no-default-link-local
set interfaces bridge br{{ vni }} ipv6 disable-forwarding
set interfaces bridge br{{ vni }} member interface eth2
set interfaces bridge br{{ vni }} member interface vxlan{{ vni }}
set interfaces bridge br{{ vni }} mtu {{ mtu }}
set interfaces bridge br{{ vni }} stp
set interfaces ethernet eth1 description "{{ s2s.name }}"
set interfaces ethernet eth2 description "{{ side_label }}: {{ attached }}"
set interfaces vxlan vxlan{{ vni }} ipv6 address no-default-link-local
set interfaces vxlan vxlan{{ vni }} ipv6 disable-forwarding
set interfaces vxlan vxlan{{ vni }} mtu {{ vxlan_mtu }}
set interfaces vxlan vxlan{{ vni }} port 4789
set interfaces vxlan vxlan{{ vni }} remote {{ s2s.remote }}
set interfaces vxlan vxlan{{ vni }} source-address {{ s2s.ip }}
set interfaces vxlan vxlan{{ vni }} vni {{ vni }}
""" + MGMT_FILTER + "\n" + COMMON_SYSTEM + "\n"  # the body before the WireGuard tunnel (2026-10-25), for the migration

# L2 channel in one VM on an edge cluster of the tenant's platform: the external VLAN (eth1) and the tenant network
# (eth2) in one bridge.
L2BR = """set interfaces bridge br{{ vni }} ipv6 address no-default-link-local
set interfaces bridge br{{ vni }} ipv6 disable-forwarding
set interfaces bridge br{{ vni }} member interface eth1
set interfaces bridge br{{ vni }} member interface eth2
set interfaces bridge br{{ vni }} mtu {{ mtu }}
set interfaces bridge br{{ vni }} stp
set interfaces ethernet eth1 description "EXT: {{ vlan }}"
set interfaces ethernet eth2 description "TENANT: {{ attached }}"
""" + MGMT_ZONE + "\n" + COMMON_SYSTEM + "\n"

# Route reflector of an exchange network (internet or corporate): eBGP to the uplinks, iBGP to the gateways.
RR_IX = """set firewall group network-group {{ label }}-EXT description "eBGP peers"
{% for n in ext_hosts %}set firewall group network-group {{ label }}-EXT network {{ n }}
{% endfor %}set firewall group network-group {{ label }}-IX description "{{ title }} Interexchange network"
set firewall group network-group {{ label }}-IX network {{ ix.cidr }}
set firewall group network-group {{ label }}-RR description "BGP Route Reflectors"
set firewall group network-group {{ label }}-RR network {{ ix.rr_net }}
set firewall ipv4 name GW-to-IX default-action drop
set firewall ipv4 name GW-to-IX description "GW->IX: BGP"
set firewall ipv4 name GW-to-IX rule 1 action accept
set firewall ipv4 name GW-to-IX rule 1 destination group network-group {{ label }}-IX
set firewall ipv4 name GW-to-IX rule 1 protocol icmp
set firewall ipv4 name GW-to-IX rule 11 action accept
set firewall ipv4 name GW-to-IX rule 11 destination group network-group {{ label }}-IX
set firewall ipv4 name GW-to-IX rule 11 destination port 179
set firewall ipv4 name GW-to-IX rule 11 protocol tcp
set firewall ipv4 name GW-to-IX rule 12 action accept
set firewall ipv4 name GW-to-IX rule 12 destination group network-group {{ label }}-IX
set firewall ipv4 name GW-to-IX rule 12 state established
set firewall ipv4 name GW-to-IX rule 13 action accept
set firewall ipv4 name GW-to-IX rule 13 destination group network-group {{ label }}-IX
set firewall ipv4 name GW-to-IX rule 13 state related
set firewall ipv4 name IX-to-GW default-action drop
set firewall ipv4 name IX-to-GW description "IX->GW: BGP"
set firewall ipv4 name IX-to-GW rule 1 action accept
set firewall ipv4 name IX-to-GW rule 1 protocol icmp
set firewall ipv4 name IX-to-GW rule 1 source group network-group {{ label }}-IX
set firewall ipv4 name IX-to-GW rule 11 action accept
set firewall ipv4 name IX-to-GW rule 11 destination port 179
set firewall ipv4 name IX-to-GW rule 11 protocol tcp
set firewall ipv4 name IX-to-GW rule 11 source group network-group {{ label }}-IX
set firewall ipv4 name IX-to-GW rule 12 action accept
set firewall ipv4 name IX-to-GW rule 12 source group network-group {{ label }}-IX
set firewall ipv4 name IX-to-GW rule 12 state established
set firewall ipv4 name IX-to-GW rule 13 action accept
set firewall ipv4 name IX-to-GW rule 13 source group network-group {{ label }}-IX
set firewall ipv4 name IX-to-GW rule 13 state related
set firewall zone GW from IX firewall name IX-to-GW
set firewall zone IX from GW firewall name GW-to-IX
set firewall zone IX member interface eth1
set interfaces ethernet eth1 description "{{ ix.name }}"
set policy prefix-list all rule 10 action permit
set policy prefix-list all rule 10 description "All IPv4 address space"
set policy prefix-list all rule 10 le 32
set policy prefix-list all rule 10 prefix 0.0.0.0/0
set policy route-map {{ pg }}-ext rule 10 action permit
set policy route-map {{ pg }}-ext rule 10 match ip address prefix-list all
set protocols bgp address-family ipv4-unicast
set protocols bgp listen limit 200
{% for n in ext_hosts %}set protocols bgp listen range {{ n }} peer-group {{ pg }}-ext
{% endfor %}{% for n in int_hosts %}set protocols bgp listen range {{ n }} peer-group {{ pg }}-int
{% endfor %}{% for n in rr_hosts %}set protocols bgp listen range {{ n }} peer-group {{ pg }}-rr
{% endfor %}{% for rr in other_rrs %}set protocols bgp neighbor {{ rr }} peer-group {{ pg }}-rr
{% endfor %}set protocols bgp parameters router-id {{ ix.ip }}
set protocols bgp peer-group {{ pg }}-ext address-family ipv4-unicast soft-reconfiguration inbound
set protocols bgp peer-group {{ pg }}-ext password '{{ passwords.ext }}'
set protocols bgp peer-group {{ pg }}-ext remote-as {{ remote_asn }}
set protocols bgp peer-group {{ pg }}-ext update-source {{ ix.ip }}
set protocols bgp peer-group {{ pg }}-int address-family ipv4-unicast route-map export {{ pg }}-ext
set protocols bgp peer-group {{ pg }}-int address-family ipv4-unicast route-reflector-client
set protocols bgp peer-group {{ pg }}-int address-family ipv4-unicast soft-reconfiguration inbound
set protocols bgp peer-group {{ pg }}-int password '{{ passwords.int }}'
set protocols bgp peer-group {{ pg }}-int remote-as internal
set protocols bgp peer-group {{ pg }}-int update-source {{ ix.ip }}
set protocols bgp peer-group {{ pg }}-rr address-family ipv4-unicast soft-reconfiguration inbound
set protocols bgp peer-group {{ pg }}-rr password '{{ passwords.rrs }}'
set protocols bgp peer-group {{ pg }}-rr remote-as internal
set protocols bgp peer-group {{ pg }}-rr update-source {{ ix.ip }}
set protocols bgp system-as {{ ix.asn }}
set protocols bgp timers holdtime 15
set protocols bgp timers keepalive 3
""" + MGMT_ZONE + "\n" + COMMON_SYSTEM + "\n"

# Route reflector of the VXLAN/EVPN underlay of the KVM hosts.
RR_VXLAN = """set interfaces ethernet eth1 description "{{ ix.name }}"
set protocols bgp address-family l2vpn-evpn advertise-all-vni
set protocols bgp listen limit 200
{% for n in int_hosts %}set protocols bgp listen range {{ n }} peer-group cloud-int
{% endfor %}{% for n in rr_hosts %}set protocols bgp listen range {{ n }} peer-group cloud-rr
{% endfor %}{% for rr in other_rrs %}set protocols bgp neighbor {{ rr }} capability extended-nexthop
set protocols bgp neighbor {{ rr }} peer-group cloud-rr
{% endfor %}set protocols bgp parameters router-id {{ ix.ip }}
set protocols bgp peer-group cloud-int address-family ipv4-unicast route-reflector-client
set protocols bgp peer-group cloud-int address-family l2vpn-evpn route-reflector-client
set protocols bgp peer-group cloud-int capability dynamic
{% if passwords.int %}set protocols bgp peer-group cloud-int password '{{ passwords.int }}'
{% endif %}set protocols bgp peer-group cloud-int remote-as internal
set protocols bgp peer-group cloud-int update-source {{ ix.ip }}
set protocols bgp peer-group cloud-rr address-family ipv4-unicast route-reflector-client
set protocols bgp peer-group cloud-rr address-family l2vpn-evpn route-reflector-client
{% if passwords.rrs %}set protocols bgp peer-group cloud-rr password '{{ passwords.rrs }}'
{% endif %}set protocols bgp peer-group cloud-rr remote-as internal
set protocols bgp peer-group cloud-rr update-source {{ ix.ip }}
set protocols bgp system-as {{ ix.asn }}
set protocols bgp timers holdtime 15
set protocols bgp timers keepalive 3
""" + RR_VXLAN_ZONES + MGMT_ZONE + "\n" + COMMON_SYSTEM + "\n"

HOST_FRR = """!
hostname {{ host_name }}
!
log file /var/log/frr/bgpd.log informational
!
router bgp {{ ix.asn }}
bgp router-id {{ ix.ip }}
  no bgp default ipv4-unicast
  timers bgp 3 15
!
  neighbor fabric peer-group
  neighbor fabric remote-as {{ ix.asn }}
  neighbor fabric password {{ passwords.int }}
  neighbor fabric capability extended-nexthop
  neighbor fabric update-source {{ ix.ip }}
!
{% for rr in ix.rrs %}neighbor {{ rr }} peer-group fabric
{% endfor %}!
  address-family ipv4 unicast
    neighbor fabric activate
  exit-address-family
!
  address-family l2vpn evpn
    neighbor fabric activate
    advertise-all-vni
  exit-address-family
!
exit
!
"""


# The site igw (core service cloud-gw): the tenant igw with the management part — eth0 in VRF MGMT, zone MGMT, ssh there
# only, NTP/syslog through it, the default route to the egw (correction 12: its own template, cloud-igw)
CLOUD_IGW_MGMT = """set firewall group network-group MGMT description "Management hosts"
{% for n in mgmt_hosts %}set firewall group network-group MGMT network {{ n }}
{% endfor %}""" + MGMT_FW + """set interfaces ethernet eth0 description "MGMT: {{ mgmt.net_name }}"
set interfaces ethernet eth0 vrf MGMT
delete protocols static route 0.0.0.0/0
{% if ext.gateway %}set protocols static route 0.0.0.0/0 next-hop {{ ext.gateway }} distance 100
{% endif %}delete service ssh port
set service ssh port {{ ssh_port }}
set service ssh vrf MGMT
{% if ntp %}set service ntp vrf MGMT
{% endif %}{% if mgmt.gw %}set vrf name MGMT protocols static route 0.0.0.0/0 next-hop {{ mgmt.gw }}
{% endif %}set vrf name MGMT table 100
{% if syslog %}set system syslog remote {{ syslog.host }} facility all level info
set system syslog remote {{ syslog.host }} port {{ syslog.port }}
set system syslog remote {{ syslog.host }} protocol {{ syslog.proto }}
set system syslog remote {{ syslog.host }} vrf MGMT
{% endif %}set system syslog marker disable
{% for n in ntp %}set service ntp server {{ n }}
{% endfor %}"""
CLOUD_IGW = IGW.replace("{% if not igw_keys %}delete service ssh disable-password-authentication\n{% endif %}", "") + CLOUD_IGW_MGMT
# (the site igw keeps ssh key-only: the password-login line is the tenant igw's, correction 14)

DESCRIPTIONS = {'egw': "Provider VM in front of a tenant's main public network. eth1 the internet exchange (zone IX): iBGP to the exchange's route reflectors, connected and static routes redistributed (the tenant's routed networks via its igw). eth2 the tenant's public subnets (zone CLIENT): to and from the internet with martians dropped; the router answers them ping only. Bandwidth limited to the tenant's Mbps on eth1. Management: eth0 in VRF MGMT, zone MGMT — ping and ssh only from the management hosts (ssh nowhere else); time (NTP) and logs (syslog) go out through it. Time zone from the site settings. NIC addresses, MTU, host name (= VM name) and the login user come from the OpenNebula context at the first boot (the image's first-boot script).", 'igw': "The tenant's own router, under the tenant's full responsibility: EXT the main public network (ssh only from the hosts the tenant allows, with the tenant's keys), INT private networks behind SNAT, PUB extra public networks; no management NIC, no logs to the provider. The site gateway (core service cloud-igw) is built from it with the management part (eth0 in VRF MGMT, zone MGMT, ssh there only — none on EXT) and its own differences.", 'cgw': "Provider VM in front of a tenant's corporate network. eth1 the corporate exchange (zone IX): iBGP to its route reflectors. The tenant's corporate subnets (zone CLIENT): only to and from the exchange; the router answers them ping only. Management: eth0 in VRF MGMT, zone MGMT — ping and ssh only from the management hosts (ssh nowhere else); time (NTP) and logs (syslog) go out through it. Time zone from the site settings. NIC addresses, MTU, host name (= VM name) and the login user come from the OpenNebula context at the first boot (the image's first-boot script).", 'l2-a': "L2 channel, tenant side: bridges the tenant network (eth2) into a VXLAN tunnel to the external side, carried in WireGuard over the s2s network (eth1); keys and the pre-shared key made at random per channel and kept nowhere else. Zones: S2S (eth1) — WireGuard with the other side only; TUN (the WireGuard interface) — VXLAN with the other side only; the bridged frames are not filtered. Management: eth0 in VRF MGMT, zone MGMT — ping and ssh only from the management hosts (ssh nowhere else); time (NTP) and logs (syslog) go out through it. Time zone from the site settings. NIC addresses, MTU, host name (= VM name) and the login user come from the OpenNebula context at the first boot (the image's first-boot script).", 'l2-b': "L2 channel, external side: bridges the external VLAN (eth2) into the same VXLAN-in-WireGuard tunnel over the s2s network (eth1); it runs on the site's management platform. Zones: S2S (eth1) — WireGuard with the other side only; TUN (the WireGuard interface) — VXLAN with the other side only; the bridged frames are not filtered. Management: eth0 in VRF MGMT, zone MGMT — ping and ssh only from the management hosts (ssh nowhere else); time (NTP) and logs (syslog) go out through it. Time zone from the site settings. NIC addresses, MTU, host name (= VM name) and the login user come from the OpenNebula context at the first boot (the image's first-boot script).", 'l2-br': "L2 channel in one VM on the edge cluster of the tenant's platform: bridges the external VLAN (eth1) and the tenant network (eth2); no addresses on them, nothing reaches the router there. Management: eth0 in VRF MGMT, zone MGMT — ping and ssh only from the management hosts (ssh nowhere else); time (NTP) and logs (syslog) go out through it. Time zone from the site settings. NIC addresses, MTU, host name (= VM name) and the login user come from the OpenNebula context at the first boot (the image's first-boot script).", 'rr-inet': "Route reflector of the internet exchange (eth1, zone IX: BGP and ping with the exchange network only): eBGP to the uplinks, iBGP to the tenants' egw (route reflector clients, the uplinks' routes exported to them) and to the other route reflectors. Management: eth0 in VRF MGMT, zone MGMT — ping and ssh only from the management hosts (ssh nowhere else); time (NTP) and logs (syslog) go out through it. Time zone from the site settings. NIC addresses, MTU, host name (= VM name) and the login user come from the OpenNebula context at the first boot (the image's first-boot script).", 'rr-corpnet': "Route reflector of the corporate exchange (eth1, zone IX: BGP and ping with the exchange network only): eBGP to the corporate uplinks, iBGP to the tenants' cgw (route reflector clients) and to the other route reflectors. Management: eth0 in VRF MGMT, zone MGMT — ping and ssh only from the management hosts (ssh nowhere else); time (NTP) and logs (syslog) go out through it. Time zone from the site settings. NIC addresses, MTU, host name (= VM name) and the login user come from the OpenNebula context at the first boot (the image's first-boot script).", 'rr-vxlan': "Route reflector of the VXLAN/EVPN underlay (eth1, zone IX: BGP and ping with the underlay and its BGP peers only): iBGP (ipv4 and l2vpn-evpn) with the KVM hosts' FRR (route reflector clients) and with the other route reflectors. Management: eth0 in VRF MGMT, zone MGMT — ping and ssh only from the management hosts (ssh nowhere else); time (NTP) and logs (syslog) go out through it. Time zone from the site settings. NIC addresses, MTU, host name (= VM name) and the login user come from the OpenNebula context at the first boot (the image's first-boot script)."}

CLOUD_IGW_DESCRIPTION = ("The site's igw (core service cloud-gw): the tenant igw with the management part — eth0 in VRF MGMT, "
                         "zone MGMT, ssh only there (none on EXT), NTP and logs through it; EXT on core-services (default "
                         "route to the egw), INT on core-cloud (masqueraded). The service's config addon (destination NAT, "
                         "further addresses, routes) is added to it.")

BUILTIN = [
    ("egw", "egw — internet edge", "vyos", "2026.03", EGW, DESCRIPTIONS["egw"]),
    ("igw", "igw — tenant gateway", "vyos", "2026.03", IGW, DESCRIPTIONS["igw"]),
    ("cgw", "cgw — corporate edge", "vyos", "2026.03", CGW, DESCRIPTIONS["cgw"]),
    ("l2-a", "L2 channel — tenant side", "vyos", "2026.03", L2, DESCRIPTIONS["l2-a"]),
    ("l2-b", "L2 channel — external side", "vyos", "2026.03", L2, DESCRIPTIONS["l2-b"]),
    ("l2-br", "L2 channel — VLAN-to-network bridge", "vyos", "2026.03", L2BR, DESCRIPTIONS["l2-br"]),
    ("rr-inet", "Route reflector — internet exchange", "vyos", "2026.03", RR_IX, DESCRIPTIONS["rr-inet"]),
    ("rr-corpnet", "Route reflector — corporate exchange", "vyos", "2026.03", RR_IX, DESCRIPTIONS["rr-corpnet"]),
    ("rr-vxlan", "Route reflector — VXLAN/EVPN", "vyos", "2026.03", RR_VXLAN, DESCRIPTIONS["rr-vxlan"]),
    ("host-frr", "KVM host — FRR for VXLAN/EVPN", "frr", "10.6", HOST_FRR, ""),
]
