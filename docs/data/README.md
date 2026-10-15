# Data

[Documentation](../README.md) › data

What claugine works with, who defines it and where it lives.

| Document | Content |
|---|---|
| this page | source data, principles, the two data trees, references, secrets, sample, load and refresh, runtime data, quota data |
| [inventory.md](inventory.md) | inventory: the fixed structure that describes your platforms, field by field |
| [config.md](config.md) | internal data of claugine: `config/data`, `config/templates`, `config/files` |
| [quota.md](quota.md) | tenant quotas: records and their storage in OpenNebula groups |

- [Source data](#source-data)
- [Loading a tree](#loading-a-tree)
- [Two trees](#two-trees)
- [Principles](#principles)
- [References](#references)
- [Secrets](#secrets)
- [Sample](#sample)
- [Load and refresh](#load-and-refresh)
- [Runtime](#runtime)
- [Quota data](#quota-data)

## Source data

The YAML trees are read into four JSON documents - shell globals, **not
exported**, so child processes (`ssh`, `rsync`, ...) never see them:

| Variable | From | Loaded by | Read with |
|---|---|---|---|
| `CONFIG` | `config/data/` | `source bin/claugine` | `inv_value var=CONFIG path=<subsystem>.<key>` |
| `TEMPLATES` | `config/templates/` | `source bin/claugine` | `inv_value var=TEMPLATES path=<group>.<name>` |
| `INV` | `inv=PATH` - an inventory directory | `data_provider_load` | `inv_value var=INV path=<path>` |
| `SECRETS` | `secrets=PATH` - a secrets directory | `data_provider_load` | `inv_value var=SECRETS path=<path>` |

All three directory parameters of `data_provider_load` are required. It also
sets `DIR_RUNTIME` (`runtime=PATH`, created if missing) - the directory where
collected runtime data may be saved, and clears `RUNTIME` and `QUOTA`. Calling
`data_provider_load` again switches to another data set:

```bash
source bin/claugine
data_provider_load inv=~/data/acme/inventory secrets=~/data/acme/secrets runtime=~/data/acme/runtime [quota=load]
```

How the data is loaded, checked and refreshed: [Load and refresh](#load-and-refresh).

The inventory is the **only source of platform data**: every command that
works on a platform names it as an inventory platform,
`platform=platforms.<site>.<platform>`.
What OpenNebula knows about it is collected into [`RUNTIME`](#runtime);
what OpenNebula does not know - site directories and URLs, time zone,
tenant quotas, the shared group name - is read from the inventory. Secrets
are read only through a reference found in a fixed inventory field.

Each document is used on its own; the same functions read any of them, one
per shape of the data. `var=` names the document, `path=` is a path inside
it, keys separated by `.` - no key contains a dot:

| Function | Reads | Prints |
|---|---|---|
| `inv_value var= path=` | a value | the value; an object or array - one-line JSON |
| `inv_list var= path=` | a list | one element per line |
| `inv_list_field var= path= field=` | a list of objects | the `field` of each object, one per line |
| `inv_keys var= path=` | an object | its keys, one per line |
| `inv_list_find var= path= [field=name] value=` | a list of objects | the object whose `field` is `value`, one-line JSON |

A missing path or an empty value: the getters print nothing and return 1, so `if x=$(inv_value ...)` tests and reads in one step.

```bash
inv_value var=INV path=platforms.dc1.payload1.fe.vip                    # 10.71.101.30
inv_list var=CONFIG path=onefe.nodes_order.direct                          # leader follower1 follower2
inv_list_field var=CONFIG path=ubuntu.iso.files field=name                # grub.cfg
inv_list_find var=CONFIG path=ubuntu.iso.files value=grub.cfg        # {"name":"grub.cfg","path":"/boot/grub/"}
inv_keys var=CONFIG path=onefe.configs.oned.vm_operations    # ADMIN MANAGE USE
```

### Loading a tree

`_data_load_var var=NAME dir=PATH` loads one YAML tree into one of the four
variables; `source bin/claugine` calls it for `CONFIG` and `TEMPLATES`,
`data_provider_load` for `INV` and `SECRETS`:

1. **checks the name** - one of `INV_VARS`: `CONFIG TEMPLATES INV SECRETS`, so
   no other shell variable can be overwritten; loading `INV` clears `RUNTIME`;
2. **finds** every `*.yaml` under the directory, sorted - the result does not
   depend on the order of the file system; any file name works, spaces too;
3. **converts** each file to JSON with `yq` - the Go or the Python version,
   detected - and puts it under its path: `resources/dc1/hosts/h1.yaml` is
   `resources.dc1.hosts.h1`; an empty file, or one with only comments, is `{}`;
4. **merges** all files into one document with `jq` (deep merge, `*`): a file
   `dc1.yaml` and a directory `dc1/` fill the same object. Define a key in
   one place only - for a key defined twice, the file later in the sort wins;
5. **stores** the document in the variable - pretty-printed, readable with
   `echo "${INV}"` - and prints statistics: the number of JSON nodes by type.

```
var=INV loaded: {
  "total_values": 1572,          all nodes, objects and arrays included
  "objects": 407,
  "arrays": 138,
  "strings": 815,
  "numbers": 212,
  "booleans": 0,
  "nulls": 0
}
```

Compare the numbers after editing YAML: a drop means data was lost - a
file `yq` could not read is skipped, with the error of `yq` on the screen.

`_data_load_var` returns 1 on a wrong name, a missing directory or an empty
result; then `source` stops with 3 and `data_provider_load` with 1. Fix the cause
and run it again.

## Two trees

| Tree | Defined by | Content | Published |
|---|---|---|---|
| `config/` | claugine developers | internal data, config file templates, files copied to targets - see [config.md](config.md) | yes, part of the tool |
| data set | the end customer (provider) | `inventory/`, `secrets/`, `runtime/`, `tests-data.sh`, `jobs-data.sh` | never, except `data/SAMPLE` |

```
claugine/
├── config/                 internal, defined by developers
│   ├── data/               internal data used by the code
│   ├── templates/          YAML templates of config files changed by the tool
│   └── files/              files copied as is to targets or into ISOs
└── data/                   a data set - optional here, can live anywhere
    ├── inventory/          the target platforms, fixed structure - inventory.md
    ├── secrets/            sensitive values, free structure
    ├── runtime/            data collected while the tool runs
    ├── tests-data.sh       values of tests/*.sh - see usage/tests.md
    ├── jobs-data.sh        values of jobs/*.sh - see usage/jobs.md
    └── SAMPLE/             published sample data set with fake values
        ├── inventory/
        ├── secrets/
        ├── runtime/
        ├── tests-data.sh
        └── jobs-data.sh
```

The data set does not have to be inside the repository: `data/` is used
during development; in production the data set lives outside the tool and is
passed to it at start-up.

## Principles

- **The inventory structure is fixed by claugine.** Directory names, file
  roles and field names are defined here and in [inventory.md](inventory.md);
  a provider can not redesign them. The provider chooses the **names**: of
  sites, hosts, networks, storages, volumes, clusters, datastores and so on.
- **Secrets have a free structure.** claugine never addresses a secret
  directly - only through a reference placed in a fixed inventory field.
- **Directories and files are keys.** `inventory/resources/dc1/hosts/h1.yaml`
  is the object `resources.dc1.hosts.h1`. A file `dc1.yaml` next to a
  directory `dc1/` adds its keys to the same object.
- **Everything is YAML**; each tree is merged into one JSON document when it
  is loaded - see [loading a tree](#loading-a-tree).
- **Config holds no inventory data.** Nothing in `config/` depends on a
  provider or a site; directories, URLs and time zones of a site are in its
  site file.
- **Read-only.** claugine never changes the inventory or the secrets. What it
  collects while running goes to `runtime/`, which will be replaced by a
  database.
- **Names are unique where OpenNebula needs it.** Objects created in
  OpenNebula (clusters, datastores, networks) carry the site in their name,
  `payload1-vms01_dc1`, so that federated zones never clash.
- **Sizes are in GiB**, the unit is part of the key: `size_gb`, `total_gb`,
  `vram_gb`.
- **Keys are snake_case**; names chosen by the provider may contain `-`.

## References

A string value that starts with one of these roots is a reference to another
object:

| Root | Resolved in | Example |
|---|---|---|
| `resources.` | `inventory/resources/` | `resources.dc1.hosts.hdc1std1kvm001` |
| `platforms.` | `inventory/platforms/` | `platforms.dc1.payload1.fe` |
| `server_profiles.` | `inventory/server_profiles/` | `server_profiles.std1` |
| `providers.` | `inventory/providers/` | `providers.acme` |
| `secrets.` | `secrets/` | `secrets.acme.payload1.one` |
| `timezones.` | `config/data/timezones.yaml` | `timezones.almaty` |

A reference always holds the full path from its root. It may point to a
single value (a BGP password) or to an object (a host, a volume, a set of
credentials); [inventory.md](inventory.md) says which, field by field.
Every reference must resolve - a broken reference is an error of the data
set.

## Secrets

`secrets/` can be organized in any way: one file or many, any nesting. The
sample uses `secrets/<provider>/<topic>.yaml` for what all platforms share
and `secrets/<provider>/<platform>/<topic>.yaml` for the rest. What claugine
requires is only the **shape of the object a reference points to**:

| Referenced from | Object | Keys |
|---|---|---|
| `platform.yaml: sa` | super admins: name → account | `<name>: {uid, description, password, key}` |
| `fe.yaml: secrets` | OpenNebula FE secrets | `service_user.oneadmin: {uid, description, password, key}`, `onedb: {db, user, password}`, `oneadmin` - password of the oneadmin user |
| `hosts/*.yaml: ipmi.user` | IPMI account | `{user, password}` |
| `networks/*.yaml: bgp.*.peer_groups.*.password` | BGP peer group password | a string |

## Sample

`data/SAMPLE` is a complete data set with fake values: provider `acme`, two
sites `dc1` and `dc2`, three platforms on each site: `mgmt` - management,
`payload1` - payload, federated across both sites, `payload2` - payload, one
zone per site. Addresses are private
or documentation ranges (`192.0.2.0/24`, `198.51.100.0/24`,
`203.0.113.0/24`); passwords and keys are placeholders. Start your own data
set from it:

```bash
cp -r data/SAMPLE ~/claugine-data
```

## Load and refresh

Three kinds of data, each loaded by its own function:

| Data | Variable | Source | Loaded | Refreshed |
|---|---|---|---|---|
| internal | `CONFIG`, `TEMPLATES` | `config/` | `source bin/claugine` | never |
| platform | `INV`, `SECRETS` | the inventory, the secrets | `data_provider_load` | `data_provider_load` again - the whole data set |
| runtime | `RUNTIME` | the platform components, from the FE | `data_platform_refresh` - the first call for the platform | FE nodes: `data_platform_refresh fe=yes`; all: `data_provider_load` again |
| quota | `QUOTA` | limit - the group template, real - the group quotas, usage - the group quota usage | `data_platform_refresh quota=load\|refresh` - the first call for the platform | usage: `data_platform_refresh quota=refresh` |

```
data_provider_load inv= secrets= runtime= [quota=load|SKIP]     once per data set
  ├─ INV, SECRETS; RUNTIME=() QUOTA=()
  ├─ PLATFORMS - all but state planned (PLATFORMS_PLANNED): not collected, not checked
  ├─ every platform: _data_runtime_platform_init, FE nodes      runtime data; FE not reachable, no zone of the VIP - unavailable
  ├─ _data_check                                     fails - everything cleared, return 1
  │    ├─ _data_check_inventory      the inventory only, no platform calls
  │    ├─ _data_check_platform       every platform: its runtime data against its inventory
  │    └─ _data_check_federations    every federation once
  ├─ planned, updated, retired platform or undeployed cluster ─► DEPLOYMENT=required  only deployment operations
  └─ quota=load (not with DEPLOYMENT=required): every platform of kind shared:
       data_platform_refresh quota=load                         quota data; fails - everything cleared, return 1

data_platform_refresh platform= [fe=yes|NO] [quota=SKIP|load|refresh]    first call of every command
  ├─ INV loaded, platform name valid
  ├─ no RUNTIME[P,id] ─► _data_runtime_platform_init, fe=yes      once per platform
  ├─ fe=yes           ─► _data_runtime_fe_nodes_refresh           FE node roles and states
  └─ quota=load|refresh: kind shared required, otherwise return 1
       no QUOTA_LOADED ─► _data_quota_load of every shared platform: limit, real
                               └─ _data_usage_refresh: usage
                          ─► _data_check_quota: every tenant has a qr on some shared platform
       loaded, quota=reload  ─► _data_quota_load tenants=: limit, real, usage of these tenants
                          ─► _data_check_quota tenants=
       loaded, quota=refresh ─► _data_usage_refresh tenants=
```

Rules:

- `data_provider_load` is called once per data set; commands never call it.
- `platform.state` of [`platform.yaml`](inventory.md#platformyaml): `planned` -
  to be deployed, the platform is not in `PLATFORMS` (it is in
  `PLATFORMS_PLANNED`), not collected, not checked, not in any `fe.secondaries`
  or `fe.primary` until it is deployed; `updated` - clusters to be deployed or
  deleted; `retired` - to be destroyed; not set - `deployed`;
- `DEPLOYMENT=required` - a global set by `data_provider_load` when a platform
  is planned, updated or retired, or a cluster of the inventory is undeployed: `data_platform_refresh` then
  fails for every command except deployment operations, which pass
  `deployment=yes`; the quota data is not loaded;
- every command starts with `data_platform_refresh`; a command on tenants,
  quotas or ACLs passes `quota=load`, or `quota=refresh` when it needs fresh
  usage - `kind: shared` in `platform.yaml` is the only sign of such a platform;
- the quota data is loaded only after the data passed the consistency check;
  without `quota=load` at the data load it is loaded by the first command
  that asks for it;
- the data is checked in one place only, `_data_check`; the
  collecting functions only collect.

What `_data_check` checks - every violation is reported, any of
them stops the load:

| Check | Rules |
|---|---|
| `_data_check_inventory` | every platform has `fe.vip`, `kind` and a valid `state`; `mgmt` - one per site, exactly one cluster, of kind `mgmt`; `shared` - `shared_owner` set, clusters of kind `shared`, `dedicated` or `service`, at least one `shared`; `portal` - every cluster of kind `portal`; `quota_code` - required for clusters of kind `shared` and `dedicated`, not allowed for the others, letters and digits only; no planned platform in `fe.secondaries` or `fe.primary` |
| `_data_check_platform` | the platform has runtime data - its FE was reachable, an unavailable one with `state: retired` is skipped; exactly one FILE and one BACKUP datastore, at least one IMAGES and one VMS datastore; every cluster has exactly one IMAGES and one VMS datastore, every IMAGES and VMS datastore belongs to a cluster; every cluster of the platform is in `clusters.yaml` - one of `clusters.yaml` not on the platform is undeployed, not an error; `kind: shared` - the shared group exists, exactly one VNet template |
| `_data_check_quota` | after the quota load and reload, not part of `_data_check`: every tenant has at least one qr on a shared platform; no qrs on one platform - no quotas there |
| `_data_check_federations` | unavailable platforms skipped; for a platform whose FE lists more than one zone: every zone is a platform of the inventory (the host of its `ENDPOINT` is the `fe.vip`); all have the same `kind`; exactly one `fe.mode: primary`, its `fe.secondaries` lists all the others; every other one is `fe.mode: secondary` with `fe.primary` the primary |

## Runtime

`RUNTIME` - the associative array of what OpenNebula knows about the
platforms, and only that: whatever the inventory defines - VIP, site, mode,
primary, secondaries - is read from `INV`, never copied here. One platform
is one zone. `data_provider_load` collects the data of every platform from
its FE; it is never written to the inventory and never refreshed on a
timer: `data_provider_load` collects it again for all platforms and checks it
again. FE node roles and states are read again by
`data_platform_refresh fe=yes`.

Keys: `P` - the platform, `platforms.<site>.<platform>`; `ROLE` - FE node
role named after its state (`states.node` of
[`config/data/onefe.yaml`](config.md#dataonefeyaml)): `leader`, `follower1`,
`follower2`, or `solo`, `candidate`, `error`; commands walk the roles of
`nodes_order` of the same file.

| Key | Value |
|---|---|
| `[P,available]` | `yes` - the FE has a zone of its VIP, the runtime data collected and checked; `no` - unavailable: the FE not reachable (powered off, network) or no zone of its VIP - no runtime data; an error, the load fails, except `state: retired` - not checked, commands on it fail; planned platforms have no keys |
| `[P,availability]` | `available`, or why not - `unreachable` (ssh to the VIP and to every FE node fails: powered off, network), `vip_down` (an FE node answers, the VIP does not), `oned_down` (ssh works, `onezone list` fails), `no_zone` (no zone has the VIP in its endpoint); see `_data_availability_get` |
| `[P,id]`, `[P,name]`, `[P,state]` | the zone of the platform - the one whose endpoint is the VIP; state - see `states.zone`; `id` set - the platform is collected |
| `[P,shared]` | `kind: shared` only: id of the group named by `shared_owner` in [`platform.yaml`](inventory.md#platformyaml) - both required, a missing one stops the load |
| `[P,vntemplate,id]`, `[P,vntemplate,name]` | `kind: shared` only: the VNet template - exactly one per platform |
| `[P,undeployed_clusters_list]` | the clusters of `clusters.yaml` not on the platform yet - claugine deploys them; names, sorted; they have no id and no quotas |
| `[P,qr_list]` | `kind: shared` only: the quota records (qr) - `quota_code` of the deployed clusters in `clusters.yaml` |
| `[P,<qr>,clusters_list]` | `kind: shared` only: the ids of the clusters of the qr, found on the platform by the cluster name, sorted |
| `[P,undeployed_qr_list]` | `kind: shared` only: the qrs whose clusters are all undeployed - a tenant record of one is skipped, no quotas |
| `[P,noqr_clusters_list]` | `kind: shared` only: the ids of the clusters without `quota_code` - not for tenants: special VMs, external gateways; sorted |
| `[P,files_ds,id]`, `[P,files_ds,name]`, `[P,files_ds,clusters]`, `[P,files_ds,hosts]` | the FILE datastore - one per platform |
| `[P,backups_ds,id]`, `[P,backups_ds,name]`, `[P,backups_ds,clusters]`, `[P,backups_ds,hosts]` | the BACKUP datastore - one per platform |
| `[P,images_ds_list]` | ids of the IMAGE datastores, space separated |
| `[P,images_ds,ID,name]`, `[P,images_ds,ID,clusters]`, `[P,images_ds,ID,hosts]` | an IMAGES datastore: name, cluster ids - never empty, checked at load, `BRIDGE_LIST` hosts |
| `[P,vms_ds_list]` | ids of the SYSTEM datastores, space separated |
| `[P,vms_ds,ID,name]`, `[P,vms_ds,ID,clusters]`, `[P,vms_ds,ID,hosts]` | a VMS datastore: name, cluster ids - never empty, checked at load, `BRIDGE_LIST` hosts |
| `[P,ROLE,id]`, `[P,ROLE,name]`, `[P,ROLE,ip]`, `[P,ROLE,state]` | FE node: id in the zone server pool, name, address, state |

Commands collect it through `data_platform_refresh`
([tool](../tool/README.md#platforms)), then read
`${RUNTIME[${platform},...]}` directly.


## Quota data

`QUOTA` - the associative array of the tenant quotas of the platforms of
kind `shared`. A tenant has quotas on platforms; the unique key is
`T,P` for the platform wide quotas and `T,P,QR` for a quota record (qr).
Records and their storage in the groups: [quota.md](quota.md). Keys: `T` -
the tenant name (the group `tenant-<name>`), `P` - the platform, `QR` - a qr: the attribute `QUOTA_<SITE>_<QR>` of the group, `<SITE>` - the site of the platform, `<QR>` - `quota_code` of `clusters.yaml`, `ID` - a datastore; a value
not set - `-`; real: `-1` - the default quota, `-2` - unlimited.

| Key | Value |
|---|---|
| `QUOTA_LOADED` | a global, not a key: `yes` - the quota data of all shared platforms loaded - all or none; the tenant names - `_get_tenants` |
| `[T,group_id]` | the group id |
| `[T,P,qrs]` | the qrs of the site of the platform, of deployed clusters; empty - no quotas there |
| `[T,P,undeployed_qrs]` | the qrs whose clusters are all undeployed - no quotas |
| `[T,P,QR,clusters]` | the cluster ids of the qr: `RUNTIME[P,QR,clusters_list]` |
| `[T,P,QR,status]`, `[T,P,QR,date_created]`, `[T,P,QR,date_review]` | the state of the qr |
| `[T,P,QR,limit,FIELD]` | `QUOTA_<SITE>_<QR>` of the group template: `cpu`, `ram_gb`, `gpu`, `disk_gb` |
| `[T,P,QR,real,FIELD]` | the VM quota of the group whose `CLUSTER_IDS` are exactly the clusters of the qr |
| `[T,P,QR,usage,FIELD]` | its usage |
| `[T,P,site,FIELD]` | `QUOTA_<SITE>` of the group template, field names in lower case |
| `[T,P,ds_list]` | the datastores of the tenant: the IMAGE datastores of its qrs' clusters, FILE, BACKUP |
| `[T,P,ds,ID,type]` | `images`, `files`, `backups` |
| `[T,P,ds,ID,limit,size_gb]` | `IMAGES_GB`, `FILES_GB`, `BACKUPS_GB` of `QUOTA_<SITE>`; not set - `defaults` of [`config/data/quota.yaml`](config.md) |
| `[T,P,ds,ID,real,size_gb]`, `[T,P,ds,ID,usage,size_gb]` | the datastore quota of the group and its usage |

Commands get it through `data_platform_refresh quota=load|refresh`, then
read `${QUOTA[${platform},...]}` directly; the tenant list -
`_get_tenants platform= [tenants=]`.
