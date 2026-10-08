# Internal data

[Documentation](../README.md) › [data](README.md) › internal data

`config/` holds the data of claugine itself. It is defined by the developers,
published with the tool and the same for every installation; a provider never
edits it. Platform-specific values belong to the [inventory](inventory.md).

```
config/
├── data/          internal data used by the code
├── templates/     YAML templates of config files the tool writes or changes
└── files/         files copied as is to targets or added to ISOs
```

- [data](#data)
- [templates](#templates)
- [files](#files)
- [Rules](#rules)

## data

`config/data/<name>.yaml` - constants, lists and defaults, one file per
subsystem; loaded into `INV` as `config.<name>.*`.

| File | Subsystem |
|---|---|
| [`admin.yaml`](#dataadminyaml) | admin workstation and jump hosts |
| [`cli.yaml`](#datacliyaml) | data of the CLI commands |
| [`onefe.yaml`](#dataonefeyaml) | OpenNebula front-ends |
| [`onehost.yaml`](#dataonehostyaml) | OpenNebula KVM hosts |
| [`repos.yaml`](#datareposyaml) | third-party APT repositories |
| [`timezones.yaml`](#datatimezonesyaml) | time zones |
| [`ubuntu.yaml`](#dataubuntuyaml) | Ubuntu Server ISO |
| [`vyos.yaml`](#datavyosyaml) | VyOS image |

Keys never contain a dot: a path splits on `.`.

### `data/admin.yaml`

| Key | Meaning |
|---|---|
| `tools` | commands that must be installed, checked at start-up; `jq` and `yq` first - the data is loaded with them |
| `packages` | packages of an admin workstation / jump host: command-line tools, network diagnostics, desktop, browsers |

### `data/cli.yaml`

| Key | Meaning |
|---|---|
| `vm_parameters` | parameters `vm_create` can not run without |

### `data/onefe.yaml`

| Key | Meaning |
|---|---|
| `repos` | names of `repos.yaml` entries added to an FE node |
| `packages` | packages of an FE node |
| `services` | FE services, restarted in this order by `fe_services_restart` |
| `services_all` | services stopped before an FE OS update: `services` and the database |
| `nodes_order.direct`, `nodes_order.reversed` | FE node roles in the order commands walk them: leader first, or followers first for updates and restarts |
| `config_dirs` | directories shown by `fe_configs_backups_list`; `~` - home of the ssh user on the FE node |
| `backup.dirs` | directories saved by `fe_backup full=yes` |
| `backup.days` | local FE backups older than this are deleted |
| `fireedge_views` | custom FireEdge views: `dir` in `config/files/`, `list` of views pushed by `fe_fireedge_views_update` |
| `configs.oned.vm_operations` | `ADMIN`, `MANAGE`, `USE` - VM operation sets written to `oned.conf` by `acl_role_rights_set` |
| `timeouts` | seconds: `data_refresh` - not used: runtime data is not refreshed on a timer; `host_disable`, `host_enable`, `host_flush` - host maintenance; `image_wait` - an image becoming READY |
| `objects` | object types `_one_object_id` can look up; the CLI command is `one<type>` |
| `states.zone`, `states.node`, `states.host`, `states.image` | OpenNebula state codes → names; `states.node` names also become the FE node roles |

### `data/onehost.yaml`

| Key | Meaning |
|---|---|
| `repos` | names of `repos.yaml` entries added to a KVM host |
| `packages` | packages of a KVM host |

### `data/repos.yaml`

Third-party APT repositories: `<name>: {key, source}` - the URL of the
signing key and the `sources.list` line. A source may contain
`$(lsb_release -cs)` / `$(lsb_release -rs)`, expanded on the target.
`onefe.yaml` and `onehost.yaml` list which of them a node gets.
Repositories of a site (`zakroma`, `host_data`, ...) are not here - they are
in the site file of the [inventory](inventory.md#site-file).

### `data/timezones.yaml`

Time zones, referenced by sites as `timezones.<name>`:

| Key | Meaning |
|---|---|
| `utc` | offset, hours |
| `linux` | tz database name, `Asia/Almaty` |
| `windows` | Windows time zone name |
| `ipmi` | time zone index of the IPMI firmware |

### `data/ubuntu.yaml`

Ubuntu Server ISO for `iso_get_ubuntu`: `url` - the release server,
`version`, and `iso` - the files added to the ISO (`dir` in `config/files/`,
`files` - list of `{name, path}`: `files/<dir>/<name>` is placed at
`<path>/<name>` on the ISO).

### `data/vyos.yaml`

VyOS image, see [VyOS image](../network/vyos-image.md):

| Key | Meaning |
|---|---|
| `url` | page with the latest VyOS Stream ISO link |
| `image_name` | image name prefix, the version is appended |
| `builder.cpu`, `builder.ram` | builder VM size, RAM in GiB |
| `builder.image` | image of the builder VM disk, resized to `builder.disk` |
| `builder.disk` | builder VM disk, GiB - the size of the image |
| `builder.prefix` | builder VM name prefix |
| `builder.password` | password of user `vyos` in the image |
| `builder.wait` | seconds to wait for a boot or a power off |
| `iso` | files added to the ISO - as for Ubuntu |

## templates

`config/templates/<group>/<name>.yaml` - one config file or config fragment
the tool writes to a target or passes to OpenNebula.

```yaml
file: /etc/sysctl.d/99-disable-ipv6.conf    # optional: where the content goes on the target
content: |                                  # the text, with ${variables}
  net.ipv6.conf.all.disable_ipv6 = 1
```

| Key | Meaning |
|---|---|
| `file` | optional: target path; without it the content is used by the code - an OpenNebula template, an SQL script, a fragment of another template |
| `content` | the text; `${name}` is replaced by the value the code passes |

| Group | Templates |
|---|---|
| `ubuntu/` - OS of every node | `ipv6_disable`, `snapd_disable`, `upgrade_and_reboot`, `repo_auth`, `repo_firefox` |
| `ssh/` | `config_default`, `config_service` |
| `onehost/` - KVM hosts | `apparmor`, `ufw`, `frr` |
| `onefe/` - FE: database and OpenNebula | `mysql_binlog`, `mysql_onedb_create`, `oned_mysql`, `oned_raft`, `api_endpoints`, `opennebula-service`, `nodejs` |
| `one_objects/` - OpenNebula objects | `datastore_{vms,images,files,backups}`, `network_{vlan,bridge,template}`, `vm_context`, `vm_context_disk`, `vm_context_nic`, `vm_context_nic_alias` - the last four assembled into one VM template |

## files

`config/files/<area>/` - files used as they are:

| Directory | Content |
|---|---|
| `ubuntu/` | `grub.cfg` of the autoinstall ISO |
| `vyos/` | scripts and `grub.cfg` added to the VyOS ISO, see [VyOS image](../network/vyos-image.md#files) |
| `opennebula-fireedge-views/` | FireEdge views: `original/` - as shipped by OpenNebula, `custom/` - pushed to the FEs |

## Rules

- Nothing here depends on a provider or a site: no addresses, names or
  passwords. A value that differs between installations belongs to the
  inventory; a secret belongs to the secrets.
- Sizes in GiB, times in seconds; the unit is in the key or the comment.
- A new constant goes into the file of its subsystem, and into this page.
