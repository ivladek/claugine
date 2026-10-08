# Management platform

[Platform](README.md) › management platform

One per site. Runs everything the site needs to work - and nothing of the
tenants.

## Layout

- **Hosts** - three hosts in one cluster.
- **FE** - three FE nodes installed directly on the host OS of these hosts;
  one VIP. The FE is standalone: management platforms of different sites are
  not federated.
- **Networks** - VLAN based (802.1Q); a Linux bridge per OpenNebula network
  on every host. Host OS addresses: host management and storage networks.
- **Tenants** - none. There is no shared group and no EVPN fabric; tenant
  commands of claugine stop with a clear message here, which is the
  normal state.

## Workloads

| workload | VMs | details |
|---|---|---|
| DNS | two per site, synchronised between sites | name resolution for all platform components |
| LDAP | two per site, one name space for all sites | authentication of all platform users |
| SMTP | one per site | notifications and reports |
| admin consoles | two | the only entry points for administrators |
| payload FE | three per payload platform | FE nodes of each payload platform zone of the site, see [payload platform](payload.md) |
| route reflectors | three, VyOS | EVPN fabric and exchange networks, see [EVPN fabric](../network/evpn.md), [IX to the Internet](../network/ix-internet.md) |
| L2-over-L3 first legs | one per channel | the client-facing leg of an L2-over-L3 channel, see [L2-over-L3 channels](../network/l2-over-l3.md) |

## claugine

```bash
fe_cfg_ver_get platform=<site>.mgmt                    # any command: platform=<site>.<platform>
```

- FE and host maintenance: `fe_*`, `host_maintenance_*`;
- service VMs: `vm_create` with `cluster=`, `vnet_*`;
- images: `iso_get_ubuntu`, `vyos_image_build` / `vyos_image_finalize` - the
  VyOS image for the route reflectors.
