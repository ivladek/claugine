# claugine-cli

**CL**oud **AU**tomation en**GINE**
Bash toolkit for day-0/1/2 operations across one or more OpenNebula zones on
[OpenNebula](https://opennebula.io) platforms:
- front-end (FE) maintenance
- host maintenance
- EVPN/VXLAN networking
- ACLs
- tenant quotas


The CLI is a set of shell functions. You `source` it into your shell once,
point it at a front-end, and then call the functions directly.

## Requirements

On the workstation that runs the CLI:

- **bash 4.3+**, on macOS install an actual version using `brew install bash`.
- `jq`
- `ssh` access to every host as user with passwordless `sudo`

## Layout

```
bin/claugine-cli.sh  entry point - source it, do not execute it
lib/cli/             command modules (claugine_cli_<area>.sh)
internal/cli/        script built-in data
templates/           files pushed to targets
data/cli/            YOUR platform data - SAMPLE provided
```

## Platform data

Everything specific to your installation:
- front-end IPs
- tenant quotas
- datastore IDs
Lives in `data/cli/claugine_cli_*.sh`.
Every such file is sourced at start-up, except files with `SAMPLE` in the name.

Start from the sample:

```bash
cp data/cli/claugine_cli_SAMPLE.sh data/cli/claugine_cli_mycloud.sh
nano data/cli/claugine_cli_mycloud.sh
```

| Variable | Meaning |
|---|---|
| `DC1_PROD`, `DC2_PROD`, … | front-end VIP of each zone (any names you like) |
| `IMAGES_QUOTA`, `FILES_QUOTA`, `BACKUPS_QUOTA` | per-tenant datastore quota, GB |
| `IMAGES_DEFAULT`, `FILES_DEFAULT`, `BACKUPS_DEFAULT` | quota for tenants not listed above |
| `IMAGES_DS_LIST`, `IMAGES_DS`, `FILES_DS`, `BACKUPS_DS` | datastore IDs per front-end (and per cluster for images) |
| `SHARED_ID` | ID of the shared group per front-end, skipped by quota commands |
| `DIR_BACKUPS` | local directory for FE backups |

The data directory can also live outside the repository — pass `data=PATH`
when sourcing. Real data files are excluded by `.gitignore`; only `*SAMPLE*`
files are committed.

## Quick start

```bash
source bin/claugine-cli.sh                       # loads data, modules, prints help
fe_data_refresh fe=10.71.101.30                  # discover zones and FE nodes
quota_tenant_get tenant=romashka                 # read-only command
fe_services_restart zones=0                      # asks for confirmation
```

Start-up options (all optional):

```bash
source bin/claugine-cli.sh \
  data=~/claugine-data/cli \     # default: ./data/cli
  ssh_user=admin \               # default: $USER
  ssh_port=22 \                  # default: 22
  ssh_keyf=~/.ssh/admin.key      # default: ~/.ssh/$USER.key
```

Run `claugine_cli_help` at any time to list every command with its parameters.

### Conventions

- Parameters are passed as `name=value`.
- `zones=` takes space-separated zone IDs in quotes (`zones="0 100"`) or `ALL`.
- Every command that changes something asks `yes/no` first (auto-declined
  after 60 s). Add `confirm=yes` to skip the prompt in scripts.
- Zone data is collected by `fe_data_refresh` and cached in the shell; most
  commands refresh it automatically when it is older than 5 minutes.

## Commands

### Front-end (`fe_*`)

| Command | Parameters | What it does |
|---|---|---|
| `fe_data_refresh` | `fe=IP` `[data=VAR]` | discover zones, FE nodes, roles and state; required first |
| `fe_cfg_ver_get` | `zones=` | show config and DB versions on each FE node |
| `fe_backup` | `zones=` `[full=yes\|NO]` | back up the OpenNebula DB (and configs with `full=yes`) to `DIR_BACKUPS` |
| `fe_configs_backups_list` | | list config backups on the FE nodes |
| `fe_configs_backups_cleanup` | `[confirm=yes]` | delete old config backups |
| `fe_disk_cleanup` | `zones=` `[confirm=yes]` | free disk space on FE nodes |
| `fe_os_update` | `zones=` `[confirm=yes]` | rolling OS update of FE nodes, followers first |
| `fe_services_restart` | `zones=` `[confirm=yes]` | restart OpenNebula services on FE nodes |
| `fe_fireedge_restart` | `zones=` `[confirm=yes]` | restart FireEdge only |
| `fe_fireedge_views_update` | `[src=PATH]` `[confirm=yes]` | push custom FireEdge views (default: `templates/opennebula-fireedge-views/custom`) |

### Hosts (`host_*`)

| Command | Parameters | What it does |
|---|---|---|
| `host_maintenance_on` | `zone_id=` `hosts=` `[interval=30]` `[limit=1800]` | disable hosts and wait until all VMs are moved off |
| `host_maintenance_off` | `zone_id=` `hosts=` | return hosts to service |

### EVPN / VXLAN (`evpn_*`)

| Command | Parameters | What it does |
|---|---|---|
| `evpn_vtep_nic_get` | `zones=` | show VTEP interfaces on nodes |
| `evpn_vtep_vnet_get` | `zones=` | show VTEP settings of VXLAN virtual networks |
| `evpn_vtep_vnet_set` | `zones=` `[confirm=yes]` | fix VTEP settings on virtual networks |
| `evpn_vtep_vnm_patch` | `zones=` `[confirm=yes]` | patch the VXLAN network driver for EVPN (original kept as `.original`) |
| `evpn_vtep_vnm_unpatch` | `zones=` `[confirm=yes]` | restore the original driver |

### ACL (`acl_*`)

| Command | Parameters | What it does |
|---|---|---|
| `acl_role_rights_get` | | show VM ADMIN/MANAGE/USE operation sets in `oned.conf` |
| `acl_role_rights_set` | `[confirm=yes]` | apply the operation sets defined in `internal/cli` |
| `acl_tenant_get` | `tenant=` | show ACLs of a tenant |

### Quotas (`quota_*`)

| Command | Parameters | What it does |
|---|---|---|
| `quota_tenant_get` | `tenant=` | show a tenant's quotas and usage |
| `quota_ds_set` | `tenants=LIST\|ALL` `[confirm=yes]` | apply datastore quotas from your data file |
| `quota_vcpu_conf_show` | | show vCPU-based quota configuration |
| `quota_cpu_to_vcpu_set` | `[confirm=yes]` | set each VM's CPU equal to its vCPU |

### Other

| Command | Parameters | What it does |
|---|---|---|
| `os_service_wait` | `ip=` `service=` `[interval=10]` `[limit=300]` | wait until a systemd service is active |
| `claugine_cli_help` | | list all commands |

## Adding a command

Create or extend a module in `lib/cli/claugine_cli_<area>.sh`; it is loaded
automatically. Register the help text, then define the function:

```bash
claugine_cli_commands+=( "\
  area_do_something
    zones=LIST    # zones ids
    confirm=yes   # to suppress interactive confirmation"
)
function area_do_something() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n zone=${zone_data}
  ...
  stop_without_confirmation confirm=${confirm:-no} && return 1
  ...
}
```

## License

[Apache License 2.0](LICENSE)

## Author

Vladislav Kirilin — [@ivladek](https://github.com/ivladek)
