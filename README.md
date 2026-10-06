# claugine-cli

**CL**oud **AU**tomation en**GINE**
Bash toolkit for day-0/1/2 operations on [OpenNebula](https://opennebula.io)
installations - one zone or a federation of zones:
- front-end (FE) maintenance
- host maintenance
- EVPN/VXLAN networking
- ACLs and tenant quotas
- VMs, virtual networks, images
- Ubuntu and VyOS images from the latest ISOs

The CLI is a set of shell functions. You `source` it into your shell once,
load an installation by one of its front-ends, and then call the functions
directly.

## Requirements

On the workstation that runs the CLI:

- **bash 4.3+**, on macOS install an actual version using `brew install bash`
- the tools listed in `ADMIN_TOOLS` (`internal/claugine-cli_data-admin.sh`),
  checked at start-up: `curl grep jq mkpasswd rsync sed sha256sum sort ssh tail tee xorriso`, ...
- `ssh` access to every FE node as a user with passwordless `sudo`

## Layout

```
bin/claugine-cli.sh   entry point - source it, do not execute it
lib/                  command modules: claugine-cli_<area>.sh
internal/             built-in data: claugine-cli_data-<subsystem>.sh
data/                 YOUR platform data - claugine-cli_SAMPLE.sh provided
templates/            files pushed to targets or added to ISOs
tests/                test scripts, YOUR test data - claugine-cli_TEST_SAMPLE.sh provided
docs/                 documentation
```

## Platform data

Everything specific to your platforms - front-end VIPs, datastores, quotas,
local repositories - lives in `data/claugine-cli_*.sh`. Every such file is
sourced at start-up, except files with `SAMPLE` or `TEST` in the name.

Start from the sample:

```bash
cp data/claugine-cli_SAMPLE.sh data/claugine-cli_mycloud.sh
nano data/claugine-cli_mycloud.sh
```

| Variable | Meaning |
|---|---|
| `DC1`, `DC2`, … | front-end VIP of each zone (any names you like) |
| `IMAGES_QUOTA`, `FILES_QUOTA`, `BACKUPS_QUOTA` | per-tenant datastore quota, GB |
| `IMAGES_DEFAULT`, `FILES_DEFAULT`, `BACKUPS_DEFAULT` | quota for tenants not listed above |
| `IMAGES_DS_LIST`, `IMAGES_DS`, `FILES_DS`, `BACKUPS_DS` | datastore IDs per zone VIP (and per cluster for images) |
| `SHARED_ID`, `VNTEMPLATE_ID` | shared group and VNet template per zone VIP |
| `DIR_BACKUPS`, `DIR_IMAGES` | local directories for FE backups and images |
| `DIR_UBUNTU_REPO`, `URL_UBUNTU_REPO` | local Ubuntu ISO repository and its URL per zone VIP |
| `DIR_VYOS_REPO`, `URL_VYOS_REPO` | local VyOS repository and its URL per zone VIP |

Per-zone data is keyed by the zone VIP: zone ids repeat in every
installation, VIPs do not, so one data file serves all your installations.

The data directory can also live outside the repository - pass `data=PATH`
when sourcing. Real data files are excluded by `.gitignore`; only `*SAMPLE*`
files are published.

## Quick start

```bash
source bin/claugine-cli.sh                       # loads data, modules, prints help
fe_data_refresh fe=10.71.101.30                  # load the installation of this FE
quota_tenant_get tenant=romashka                 # read-only, all zones
fe_services_restart zones=0                      # asks for confirmation
```

Start-up options (all optional):

```bash
source bin/claugine-cli.sh \
  data=~/claugine-data \         # default: ./data
  ssh_user=admin \               # default: $USER
  ssh_port=22 \                  # default: 22
  ssh_keyf=~/.ssh/admin.key      # default: ~/.ssh/$USER.key
```

Run `claugine_cli_help` at any time to list every command with its parameters.

## Conventions

- Parameters are passed as `name=value`.
- **Installation context.** `fe_data_refresh fe=IP` loads one installation
  (all its zones and FE nodes) and makes it current. Every command works only
  inside the current installation. To work on another one, load it:
  ```bash
  fe_data_refresh data=one_dc1 fe=10.71.101.30
  fe_data_refresh data=one_dc3 fe=10.73.101.30   # current: one_dc3
  zone_data=one_dc1                              # current: one_dc1, no reload
  ```
  The context is refreshed automatically when it is older than 5 minutes.
- **Zones.** `zone_id=N` - one zone; `zones=LIST|ALL` - several zones in
  quotes (`zones="0 100"`), default `ALL`. Federation-wide objects (ACLs of
  tenants) go to the master zone. Unknown zone ids are rejected.
- Every command that changes something shows the installation and asks
  `yes/no` first (auto-declined after 60 s). Add `confirm=yes` to skip the
  prompt in scripts.

## Commands

### Front-end (`fe_*`)

| Command | Parameters | What it does |
|---|---|---|
| `fe_data_refresh` | `fe=IP` `[data=VAR]` | load or refresh an installation: zones, FE nodes, roles and state; required first |
| `fe_cfg_ver_get` | `[zones=]` | show config and DB versions on each FE node |
| `fe_backup` | `[zones=]` `[full=yes\|NO]` | back up the OpenNebula DB (and configs with `full=yes`) to `DIR_BACKUPS` |
| `fe_configs_backups_list` | `[zones=]` | list config backups on the FE nodes |
| `fe_configs_backups_cleanup` | `[zones=]` `[confirm=yes]` | delete old config backups |
| `fe_disk_cleanup` | `[zones=]` `[confirm=yes]` | free disk space on FE nodes |
| `fe_os_update` | `[zones=]` `[confirm=yes]` | rolling OS update of FE nodes, followers first |
| `fe_services_restart` | `[zones=]` `[confirm=yes]` | restart OpenNebula services on FE nodes |
| `fe_fireedge_restart` | `[zones=]` `[confirm=yes]` | restart FireEdge only |
| `fe_fireedge_views_update` | `[zones=]` `[src=PATH]` `[confirm=yes]` | push custom FireEdge views (default: `templates/opennebula-fireedge-views/custom`) |

### Hosts (`host_*`)

| Command | Parameters | What it does |
|---|---|---|
| `host_maintenance_on` | `zone_id=` `hosts=` `[interval=30]` `[limit=1800]` | disable hosts (names or ids) and wait until all VMs are moved off |
| `host_maintenance_off` | `zone_id=` `hosts=` | return hosts to service |

### VMs and virtual networks (`vm_*`, `vnet_*`)

| Command | Parameters | What it does |
|---|---|---|
| `vm_create` | `zone_id=` `cluster=` `name=` `cpu=` `ram=` `image1=` `disk1=` `vnet1=` `addr1=` ... | create a service VM in a cluster: several disks (ISO too), NICs with aliases, routes, boot order, user, password, ssh key |
| `vnet_ar_ip_create` | `zone_id=` `vnet=` `ip=` | address range for one IP, if missing |
| `vnet_ar_ip_exists` | `zone_id=` `vnet=` `ip=` | check the address range for an IP |
| `vnet_ar_mac_create` | `zone_id=` `vnet=` | address range for one MAC, returns the MAC |
| `vnet_ar_mac_get` | `zone_id=` `vnet=` | latest free MAC of the address ranges |
| `vnet_ip_leased` | `zone_id=` `vnet=` `ip=` | check that an IP is leased |

### Images and ISOs (`image_*`, `iso_*`, `vyos_*`)

| Command | Parameters | What it does |
|---|---|---|
| `image_upload` | `zone_id=` `file=\|url=` `name=` `type=` `[ds=]` | new image from a local file or a URL; an existing image with the name is deleted if unused, renamed otherwise |
| `image_publish` | `[zones=]` `file=` `name=` `[repo=VAR]` | `image_upload` to several zones |
| `image_download` | `zone_id=` `image=` `file=` | copy an image file from the datastore |
| `image_archive` | `zone_id=` `name=` | free an image name: delete or rename |
| `image_wait` | `zone_id=` `image=` | wait until an image is READY |
| `iso_get_ubuntu` | `[version=]` | latest Ubuntu Server ISO, boots straight into autoinstall |
| `iso_get_vyos` | `[url=]` | latest VyOS Stream ISO with the claugine scripts |
| `iso_download`, `iso_customize` | | building blocks of `iso_get_*` |
| `vyos_image_build` | `zone_id=` `cluster=` `vnet=` `addr=` | VyOS image, phase 1: ISO and builder VM, see [docs/VYOS.md](docs/VYOS.md) |
| `vyos_image_finalize` | `zone_id=` `vm=` `[zones=]` | VyOS image, phase 2: image created and published to the zones |

### EVPN / VXLAN (`evpn_*`)

| Command | Parameters | What it does |
|---|---|---|
| `evpn_vtep_nic_get` | `[zones=]` | show VTEP interfaces on nodes |
| `evpn_vtep_vnet_get` | `[zones=]` | show VTEP settings of VXLAN virtual networks |
| `evpn_vtep_vnet_set` | `[zones=]` `[confirm=yes]` | fix VTEP settings on virtual networks |
| `evpn_vtep_vnm_patch` | `[zones=]` `[confirm=yes]` | patch the VXLAN network driver for EVPN (original kept as `.original`) |
| `evpn_vtep_vnm_unpatch` | `[zones=]` `[confirm=yes]` | restore the original driver |

### ACL (`acl_*`)

| Command | Parameters | What it does |
|---|---|---|
| `acl_role_rights_get` | `[zones=]` | show VM ADMIN/MANAGE/USE operation sets in `oned.conf` |
| `acl_role_rights_set` | `[zones=]` `[confirm=yes]` | apply the operation sets defined in `internal/` |
| `acl_tenant_get` | `[tenants=]` | show ACLs of tenants |
| `acl_tenant_set` | `[tenants=]` `[dry=YES\|no]` `[confirm=yes]` | replace ACLs of tenants; dry run by default |

### Quotas (`quota_*`)

Quota records and fields: [docs/QUOTA.md](docs/QUOTA.md).

| Command | Parameters | What it does |
|---|---|---|
| `quota_tenant_get` | `[zones=]` `tenant=` | show a tenant's quotas and usage |
| `quota_ds_set` | `[zones=]` `[tenants=]` `[confirm=yes]` | apply datastore quotas from your data file |
| `quota_vcpu_conf_show` | `[zones=]` | show vCPU-based quota configuration |
| `quota_cpu_to_vcpu_set` | `[zones=]` `[confirm=yes]` | set each VM's CPU equal to its vCPU |

### Other

| Command | Parameters | What it does |
|---|---|---|
| `os_service_wait` | `ip=` `service=` `[interval=10]` `[limit=300]` | wait until a systemd service is active |
| `claugine_cli_help` | | list all commands |

## Adding a command

Create or extend a module `lib/claugine-cli_<area>.sh`; it is loaded
automatically. Conventions and a command skeleton:
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Documentation

Detailed documents are in [docs/](docs/README.md):

- [Tenant quotas](docs/QUOTA.md)
- [VyOS router image](docs/VYOS.md)
- [Development conventions](docs/DEVELOPMENT.md)

Release history: [CHANGES.md](CHANGES.md).

## License

[Apache License 2.0](LICENSE)

## Author

Vladislav Kirilin — [@ivladek](https://github.com/ivladek)
