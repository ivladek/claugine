# Payload platform

[Platform](README.md) › payload platform

The platform tenants see. A site can have several payload platforms - for
example with different host types or different exchange network connections
([site layout](README.md#platforms)). A payload platform has one zone per
site, and its zones form one OpenNebula federation: the FE of any site
manages the resources of all sites while the inter-site link is up, and every
site keeps working on its own when it is down.

## Layout

- **FE** - three VMs on the management platform of the same site, one VIP; one such trio per payload platform.
- **Clusters** - one or more clusters of identical hosts, N+1; more clusters
  are added without changes to the design.
- **Host OS addresses** - host management, storage, VXLAN transport.
- **Networks** - tenant networks are VXLAN overlays on the EVPN fabric
  ([EVPN fabric](../network/evpn.md)); transit segments are VLANs.

## Workloads

| workload | owner | details |
|---|---|---|
| tenant VMs | tenants | inside their resource pools |
| tenant internal gateways | tenants | OpenNebula Virtual Router, VyOS or the tenant's own appliance |
| EGW - tenant external gateways | provider | one VyOS VM per tenant: tenant landscape ↔ exchange network, see [IX to the Internet](../network/ix-internet.md), [IX to a corporate network](../network/ix-corpnet.md) |
| L2-over-L3 second legs | provider | the tenant-facing leg of L2-over-L3 channels, see [L2-over-L3 channels](../network/l2-over-l3.md) |

## Tenants

| object | OpenNebula | claugine |
|---|---|---|
| tenant | group `tenant-<name>` | `acl_tenant_*`, `quota_*` |
| tenant admin | user `admin-<name>`, admin of the group | `acl_tenant_set` |
| users | users of the group, local or LDAP | |
| quota | group quotas per zone: CPU, RAM, disks, datastores | [tenant quotas](../data/quota.md) |
| access | ACLs: the tenant's zones and clusters, its own resources, the shared images and templates, the VNet template | `acl_tenant_set` |
| shared resources | group named by `shared_owner` in `platform.yaml` of the platform - owner of images and templates offered to all tenants | found by claugine |

Isolation:

- **management** - each tenant is a separate group; members manage only the
  resources of their tenant and cannot share them with other tenants;
- **network** - every tenant subnet is its own VXLAN; the whole landscape is
  closed by the tenant's EGW;
- **storage** - no direct access to datastores; read access to the shared
  images; tenants manage only their own disks;
- **compute** - hypervisor isolation; dedicated hosts on request.

## IaaS service

| part | what the tenant gets |
|---|---|
| compute | resource pool: vCPU, RAM GiB (quota) |
| storage | disk GiB including snapshots and images (quota) |
| gateway | tenant internal gateway in the resource pool |
| connection | to the Internet or a corporate network at a fixed rate, through the EGW |
| subnet | main public network (/31 minimum) and additional subnets |
| backup | backups of the VMs the tenant selects |

Tenants build their internal landscape themselves: overlay subnets of any
number, addressing and topology, usually a star around the internal gateway.

## claugine

```bash
fe_cfg_ver_get platform="<site1>.<payload> <site2>.<payload>"   # every zone of the federation
```

All command groups apply; tenant commands need the shared group and the VNet
template of each zone, found by claugine on the FE; tenant ACLs and groups
are changed on the primary platform of the federation.
