# Data

[Documentation](../README.md) › data

What claugine works with, who defines it and where it lives.

| Document | Content |
|---|---|
| this page | source data, principles, the two data trees, references, secrets, sample, runtime data |
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
- [Runtime](#runtime)

## Source data

The YAML trees are read into four JSON documents - shell globals, **not
exported**, so child processes (`ssh`, `rsync`, ...) never see them:

| Variable | From | Loaded by | Read with |
|---|---|---|---|
| `CONFIG` | `config/data/` | `source bin/claugine` | `inv_value var=CONFIG path=<subsystem>.<key>` |
| `TEMPLATES` | `config/templates/` | `source bin/claugine` | `inv_value var=TEMPLATES path=<group>.<name>` |
| `INV` | `inv=PATH` - an inventory directory | `data_load_provider` | `inv_value var=INV path=<path>` |
| `SECRETS` | `secrets=PATH` - a secrets directory | `data_load_provider` | `inv_value var=SECRETS path=<path>` |

All three parameters of `data_load_provider` are required. It also sets `DIR_RUNTIME`
(`runtime=PATH`, created if missing) - the directory where collected runtime
data may be saved, and clears `RUNTIME`. Calling `data_load_provider` again switches
to another data set:

```bash
source bin/claugine
data_load_provider inv=~/data/acme/inventory secrets=~/data/acme/secrets runtime=~/data/acme/runtime
```

`data_load_provider` then collects the runtime data of every platform and
runs `_data_consistancy_check`, step by step; every violation is reported.
Every platform of the inventory must have runtime data: a platform whose FE
can not be reached is an error. Step 1 checks the datastores: every
cluster has exactly one IMAGES and exactly one VMS datastore, every IMAGES
and VMS datastore belongs to a cluster; one datastore may serve several
clusters. Step 2 checks every federation OpenNebula reports - a platform
whose FE lists more than one zone; each federation once, from its first
member:

- every zone is a platform of the inventory: the host of its `ENDPOINT` is
  the `fe.vip` of the platform;
- exactly one of them has `fe.mode: primary`, and its `fe.secondaries` lists
  exactly all the others;
- every other one has `fe.mode: secondary` and `fe.primary` pointing to the
  primary.

Any violation stops the load: `INV`, `SECRETS`,
`RUNTIME` and `PLATFORMS` are cleared, and commands refuse to run until the
inventory or the platform is fixed and the data loaded again.

The inventory is the **only source of platform data**: every command names
its target as an inventory platform, `platform=platforms.<site>.<platform>`.
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

A missing path: `inv_value` prints an empty value, the other getters print nothing.

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
`data_load_provider` for `INV` and `SECRETS`:

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
result; then `source` stops with 3 and `data_load_provider` with 1. Fix the cause
and run it again.

## Two trees

| Tree | Defined by | Content | Published |
|---|---|---|---|
| `config/` | claugine developers | internal data, config file templates, files copied to targets - see [config.md](config.md) | yes, part of the tool |
| data set | the end customer (provider) | `inventory/`, `secrets/`, `runtime/` | never, except `data/SAMPLE` |

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
    └── SAMPLE/             published sample data set with fake values
        ├── inventory/
        ├── secrets/
        └── runtime/
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

## Runtime

`RUNTIME` - the associative array of what OpenNebula knows about the
platforms, and only that: whatever the inventory defines - VIP, site, mode,
primary, secondaries - is read from `INV`, never copied here. One platform
is one zone. `data_load_provider` collects the data of every platform from
its FE; it is never written to the inventory and never refreshed on a
timer: `fe_data_refresh platform=NAME` collects it again for one platform,
`data_load_provider` for all. FE node roles and states are read again by
`data_runtime_refresh fe=yes`.

Keys: `P` - the platform, `platforms.<site>.<platform>`; `ROLE` - FE node
role named after its state (`states.node` of
[`config/data/onefe.yaml`](config.md#dataonefeyaml)): `leader`, `follower1`,
`follower2`, or `solo`, `candidate`, `error`; commands walk the roles of
`nodes_order` of the same file.

| Key | Value |
|---|---|
| `[P,id]`, `[P,name]`, `[P,state]` | the zone of the platform - the one whose endpoint is the VIP; state - see `states.zone`; `id` set - the platform is collected |
| `[P,shared]` | id of the group named by `shared_owner` in [`platform.yaml`](inventory.md#platformyaml); absent if the platform has none |
| `[P,vntemplate,id]`, `[P,vntemplate,name]` | the VNet template - one per platform; required with `kind: shared`, otherwise set when there is exactly one |
| `[P,files_ds,id]`, `[P,files_ds,name]`, `[P,files_ds,clusters]`, `[P,files_ds,hosts]` | the FILE datastore - one per platform |
| `[P,backups_ds,id]`, `[P,backups_ds,name]`, `[P,backups_ds,clusters]`, `[P,backups_ds,hosts]` | the BACKUP datastore - one per platform |
| `[P,images_ds_list]` | ids of the IMAGE datastores, space separated |
| `[P,images_ds,ID,name]`, `[P,images_ds,ID,clusters]`, `[P,images_ds,ID,hosts]` | an IMAGES datastore: name, cluster ids - never empty, checked at load, `BRIDGE_LIST` hosts |
| `[P,vms_ds_list]` | ids of the SYSTEM datastores, space separated |
| `[P,vms_ds,ID,name]`, `[P,vms_ds,ID,clusters]`, `[P,vms_ds,ID,hosts]` | a VMS datastore: name, cluster ids - never empty, checked at load, `BRIDGE_LIST` hosts |
| `[P,ROLE,id]`, `[P,ROLE,name]`, `[P,ROLE,ip]`, `[P,ROLE,state]` | FE node: id in the zone server pool, name, address, state |

Commands collect it through `data_runtime_refresh`
([tool](../tool/README.md#platforms)), then read
`${RUNTIME[${platform},...]}` directly.
