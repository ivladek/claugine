# Inventory

[Documentation](../README.md) › [data](README.md) › inventory

The inventory describes the target platforms: the hardware of each site and
the OpenNebula platforms built on it. Its **structure is fixed by
claugine**; the provider fills it in and chooses the names. Examples below
come from the sample data set `data/SAMPLE`.

- [Layout](#layout)
- [providers](#providers)
- [server_profiles](#server_profiles)
- [resources](#resources)
  - [site file](#site-file)
  - [hosts](#hosts)
  - [networks](#networks)
  - [storages](#storages)
- [platforms](#platforms)
  - [platform.yaml](#platformyaml)
  - [fe.yaml](#feyaml)
  - [clusters.yaml](#clustersyaml)
  - [datastores.yaml](#datastoresyaml)
  - [networks.yaml](#networksyaml)
  - [services](#services)
  - [patch.yaml](#patchyaml)
- [Checks](#checks)

## Layout

```
inventory/
├── providers/
│   └── <provider>.yaml              owner of the platforms
├── server_profiles/
│   └── <profile>.yaml               hardware configuration of a server model, shared by all sites
├── resources/                       hardware, per site
│   ├── <site>.yaml                  site: location, time, NTP, repositories
│   └── <site>/
│       ├── hosts/<host>.yaml        physical servers - KVM hosts
│       ├── networks/<network>.yaml  L2 segments with IP addressing, routing, BGP
│       └── storages/
│           ├── <storage>.yaml       storage systems and their volumes
│           └── local/<host>.yaml    local storage of one host
└── platforms/                       OpenNebula platforms, per site
    └── <site>/
        └── <platform>/              in the sample: mgmt, payload1, payload2
            ├── platform.yaml        provider, super admins
            ├── fe.yaml              OpenNebula front-end
            ├── clusters.yaml        clusters: hosts and networks
            ├── datastores.yaml      datastores on volumes
            ├── networks.yaml        virtual networks on L2 segments
            ├── services/            services running on the platform
            └── patch.yaml           temporary data, until the database
```

| Part | Scope | Content |
|---|---|---|
| `providers` | inventory | service owners; one per inventory in this version, several later |
| `server_profiles` | inventory | server models used by hosts of any site |
| `resources` | per site | what physically exists: hosts, L2 networks, storage |
| `platforms` | per site | what is built on the resources: OpenNebula installations |

A resource belongs to a site; a platform uses the resources of its site. A
platform federated across sites has one directory per site; the secondary
sites point to the primary one (`fe.yaml: primary`), the primary lists them
(`fe.yaml: secondaries`).

Platforms of a site - the provider names them; the sample has:

| Platform | Kind | Purpose |
|---|---|---|
| `mgmt` | management | core services of the site: FE nodes of payload platforms, route reflectors, backup servers, DNS, jump hosts; FE on the host OS; one per site |
| `payload1` | payload | tenant workloads; federated across sites: `primary` on dc1, `secondary` on dc2 |
| `payload2` | payload | another tenant platform, for example one operated through a third-party portal; one independent zone per site |

Services run on the management platform only.

## providers

`providers/<provider>.yaml` - the organization that builds and runs the
platforms. The file name is the provider id, used as the first level of the
sample secrets tree.

```yaml
name: ACME Cloud
id: acme
country: XX
legal_entity: ACME Cloud Ltd
website: https://www.example.com
zone:
  public: example.com          # public DNS zone
  internal: example.net        # internal DNS zone
```

## server_profiles

`server_profiles/<profile>.yaml` - one server model. Hosts refer to it with
`server_profile:`.

| Key | Meaning |
|---|---|
| `chassis.vendor`, `chassis.model` | server model |
| `ipmi` | IPMI flavour: `hpe`, ... |
| `CPU` | `vendor`, `model`, `sockets`, `cores` (per socket), `freq` (MHz) |
| `RAM.total_gb` | installed memory |
| `RAM.dimm` | `size_gb`, `freq` (MHz), `slots`, `modules` |
| `boot` | boot disk: `bus`, `controller`, `dev`, `size_gb` |
| `boot.volumes.<name>` | OS volumes: `size_gb` (`-1` - the rest of the disk), `fs`, `path` |
| `ssd` | optional, data disks: `bus`, `size_gb` (each), `count` |
| `nics.hardware.<name>` | network adapters: `vendor`, `model`, `ports`, `media`, `speed` (Gbit/s), `bus`, `slot`, `mtu`, `links` - port names |
| `nics.bonds.<name>` | bonds: `links`, `mtu` |
| `gpus` | optional: `vendor`, `model`, `hw` (`vendor`, `device` - PCI ids, `vram_gb`, `cores`), `quantity`, `usage` (`vfio`) |

## resources

### site file

`resources/<site>.yaml` - the site itself.

| Key | Meaning |
|---|---|
| `location` | `country`, `city`, `dc` - number of the data center in the city |
| `timezone` | reference to `config/data/timezones.yaml`: `timezones.<name>` |
| `ntp_servers` | list of addresses |
| `repos.<name>` | repositories of the site: `url_base` - as hosts and FEs reach it, `local_dir` - its directory on the admin workstation, an absolute path |

Repositories claugine uses:

| Repository | Content | Used by |
|---|---|---|
| `repos.zakroma` | images and ISOs: `<local_dir>/ubuntu`, `<local_dir>/vyos`, ... - created if missing; FEs download from `<url_base>/<name>` | `iso_get_*`, `vyos_image_*`, `image_publish repo=` |
| `repos.host_data` | files for host initialization | |
| `repos.backups` | FE backups, `local_dir` only | `fe_backup` |

### hosts

`resources/<site>/hosts/<host>.yaml` - one physical server; the file name is
the host name.

```yaml
server_profile: server_profiles.std1
ip: 10.71.100.11
ipmi:
  host: 10.71.200.11
  user: secrets.acme.ipmi.users.standard
```

| Key | Meaning |
|---|---|
| `server_profile` | reference to the server model |
| `ip` | management address of the host OS |
| `ipmi.host` | IPMI address |
| `ipmi.user` | reference to the IPMI account in secrets: `{user, password}` |

### networks

`resources/<site>/networks/<network>.yaml` - one L2 segment of the site and
its IP addressing. Platforms create virtual networks on top of it.

```yaml
interface: bond0          # host interface the VLAN is built on
bridge: vxlan             # optional: name of the host bridge
vlan: 101
mtu: 1500
subnet: 10.71.101.0
prefix: 24
gw: 10.71.101.254         # optional
routes:                   # optional
  - network: 10.71.10.0/24
    gw: 10.71.101.254
    metric: 50
dns:                      # optional
  - 10.71.101.11
ranges:                   # optional: named address ranges, used by platform networks
  temp:
    start: 10.71.101.170
    len: 30
```

| Key | Meaning |
|---|---|
| `interface` | bond or NIC of the host that carries the segment |
| `bridge` | optional: host bridge name, if not derived from the network |
| `vlan`, `mtu` | VLAN id, MTU |
| `subnet`, `prefix`, `gw` | IPv4 network, prefix length, gateway |
| `routes` | static routes: `network` (CIDR), `gw`, `metric` |
| `dns` | DNS servers |
| `ranges.<name>` | address range: `start`, `len` - number of addresses |
| `bgp` | optional, for EVPN and exchange networks - below |

`bgp` - BGP settings of the segment (EVPN fabric, IX):

| Key | Meaning |
|---|---|
| `local_asn`, `remote_asn` | AS numbers |
| `subnets` | optional: public or corporate subnets announced through the segment |
| `ibgp.peer_groups.<name>` | iBGP peer group: `password` - reference to a string in secrets, `hosts` - addresses or CIDR blocks of the peers |
| `ebgp.peer_groups.<name>` | eBGP peer group, same keys |
| `rrs` | addresses of the route reflectors |

### storages

`resources/<site>/storages/<storage>.yaml` - a storage system and the volumes
it presents; `storages/local/<host>.yaml` - local storage of one host.

```yaml
type: nfs
parameters: nfs4  _netdev,rw,vers=4.2,noatime,nconnect=16  0 0
targets:
  - 10.71.111.240
volumes:
  payload1-vms-1:
    dev: /payload1_vms_1
    size_gb: 102400
    clients:
      - 10.71.111.0/24
```

| Key | Meaning |
|---|---|
| `type` | `nfs`, `iscsi`, `local` |
| `parameters` | fstab fields after the mount point: file system, options, dump, pass |
| `targets` | addresses of the storage system (NFS server, iSCSI portals); not for `local` |
| `volumes.<name>.dev` | export path, block device or local device |
| `volumes.<name>.size_gb` | size |
| `volumes.<name>.clients` | who may mount it: subnets for NFS, initiator IQNs for iSCSI |
| `volumes.<name>.mount` | `local` only: mount point on the host |

## platforms

`platforms/<site>/<platform>/` - one OpenNebula platform on one site. For a
platform federated across sites this is the zone of that site.

### platform.yaml

```yaml
provider: providers.acme
sa: secrets.acme.admins.sa
shared_owner: shared
```

| Key | Meaning |
|---|---|
| `provider` | reference to the provider that owns the platform |
| `sa` | reference to the super admins in secrets: `<name>: {uid, description, password, key}`; added to every system of the platform to manage it |
| `shared_owner` | optional, platforms with tenants: name of the group that owns the images and templates shared with every tenant; `fe_data_refresh` finds its id |

### fe.yaml

The OpenNebula front-end.

```yaml
name: payload1_dc2
mode: secondary
primary: platforms.dc1.payload1.fe
type: vm
nodes:
  - sdc2p1one1
  - sdc2p1one2
  - sdc2p1one3
nic: eth0
vip: 10.72.101.30
hostname: sdc2p1one
secrets: secrets.acme.payload1.one
empty_bridges: cleanup
```

| Key | Values | Meaning |
|---|---|---|
| `name` | | zone name in OpenNebula |
| `mode` | `single` - 1 node, no federation; `local` - 3 nodes, no federation; `primary` - 3 nodes, federation, primary site; `secondary` - 3 nodes, federation, secondary site | |
| `primary` | reference | `secondary` only: the FE of the primary site |
| `secondaries` | list of references | `primary` only: the FEs of the secondary sites; `acl_tenant_set` sets ACLs for the zones of all of them. `data_load_provider` checks that every zone of the federation is the primary or one of them - otherwise the data is not loaded |
| `type` | `vm` - FE in dedicated VMs; `host` - FE on the host OS | |
| `nodes` | | FE node names; for `type: host` - host names of `resources/<site>/hosts` |
| `nic` | | interface the VIP is configured on for the Raft cluster |
| `vip` | | VIP of the FE - the address claugine connects to |
| `hostname` | | host name of the VIP |
| `secrets` | reference | FE secrets: `service_user.oneadmin {uid, description, password, key}`, `onedb {db, user, password}`, `oneadmin` |
| `empty_bridges` | `keep` - keep empty bridges on KVM hosts; `cleanup` - delete a bridge when no VM uses it | |

### clusters.yaml

```yaml
shared-std01_dc1:
  type: shared
  hosts:
    - resources.dc1.hosts.hdc1std1kvm011
  network:
    - resources.dc1.networks.one-core
    - resources.dc1.networks.data-nfs
    - resources.dc1.networks.payload1-vxlan
```

| Key | Meaning |
|---|---|
| `<name>` | cluster name in OpenNebula, with the site suffix |
| `type` | `shared` |
| `hosts` | references to the hosts of the cluster |
| `network` | references to the L2 segments the hosts of the cluster are connected to |

### datastores.yaml

```yaml
payload1-images01_dc1:
  type: images
  clusters:
    - shared-std01_dc1
  volume: resources.dc1.storages.nas2.volumes.payload1-images-1
```

| Key | Meaning |
|---|---|
| `<name>` | datastore name in OpenNebula, with the site suffix |
| `type` | `vms` - SYSTEM, `images` - IMAGE, `files` - FILE, `restic` - BACKUP with restic |
| `clusters` | optional: clusters of this platform the datastore is attached to |
| `volume` | reference to the storage volume |
| `path` | optional: directory inside the volume |
| `service` | `restic` only: reference to the backup server, `platforms.<site>.mgmt.services.<name>` |

### networks.yaml

Virtual networks of the platform, each on an L2 segment of the site.

```yaml
payload1-ix_dc1:
  type: vlan
  clusters:
    - shared-std01_dc1
  network: resources.dc1.networks.payload1-ix
  ranges:
    - egw
```

| Key | Meaning |
|---|---|
| `<name>` | virtual network name in OpenNebula, with the site suffix |
| `type` | `vlan` - 802.1Q on the segment interface, `bridge` - the existing host bridge |
| `clusters` | optional: clusters of this platform the network is added to |
| `network` | reference to the L2 segment |
| `ranges` | optional: names of the segment's `ranges` used as address ranges |

### services

`platforms/<site>/mgmt/services/` - services running on the management
platform: backup servers, DNS, jump hosts, VMs for other services. Payload
platforms do not run services. The structure of a service file is not fixed
yet; datastores already refer to backup servers here.

### patch.yaml

Temporary data that lets the CLI work before the tenant database exists;
dropped once it does. Payload platforms only; in the sample - `payload1` on dc1.

```yaml
ds_quotas:              # datastore quotas, GiB: quota_ds_set
  default:
    images: 20
    files: 1
    backups: 0
  tenants:              # tenant name: only the values that differ from default
    romashka:
      images: 1000
```

## Checks

A valid inventory satisfies:

- every reference resolves, references into `secrets/` included;
- every host has a `server_profile` that exists;
- every `ranges` name used by a platform network exists in its segment;
- every cluster named in `datastores.yaml` and `networks.yaml` is defined in
  `clusters.yaml` of the same platform;
- a `secondary` FE has `primary`, the other modes do not;
- for `type: host`, every FE node is a host of the site;
- object names created in OpenNebula end with `_<site>`.
