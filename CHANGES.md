# Changelog

## [02.00.00] - 2026-10-06

Major version: new layout and file names, one installation per context with
zones addressed by id, new modules for VMs, networks, images and ISOs.
Data files and scripts written for 01.x need the changes listed below.

### Upgrading from 01.x
- move your data files: `data/cli/claugine_cli_<name>.sh` → `data/claugine-cli_<name>.sh`; with `data=PATH` at start-up, the files in that directory are renamed the same way
- compare your data file with `data/claugine-cli_SAMPLE.sh` and add the new variables: `DIR_UBUNTU_REPO`, `URL_UBUNTU_REPO`, `DIR_VYOS_REPO`, `URL_VYOS_REPO`, `DIR_IMAGES`
- scripts: `fe_data_refresh fe=IP` first, as before; commands that used to work on all zones now also take `zones=LIST|ALL` (default `ALL`, same behaviour)

### Added
- `claugine-cli_image` module
  - `image_wait` - wait until an image is READY
  - `image_archive` - free an image name: delete the image if no VM uses it, rename it to `NAME (YYYY-MM-DD)` otherwise
  - `image_upload` - upload a local file or let the FE download it from a URL
  - `image_download` - copy an image file from the FE datastore
  - `image_publish` - upload a local image file to several zones, by URL from a repository per zone VIP when defined
- `claugine-cli_iso` module
  - `iso_download` - download with resume and sha256 check
  - `iso_customize` - add files from `templates/<os>/` to an ISO, keep it bootable
  - `iso_get_ubuntu` - latest Ubuntu Server ISO, boots straight into autoinstall
  - `iso_get_vyos` - latest VyOS Stream ISO with the claugine scripts
  - result of `iso_get_*` in the global associative array `iso_info`
- `claugine-cli_vyos` module, see [docs/VYOS.md](docs/VYOS.md)
  - `vyos_image_build` - phase 1: ISO, builder VM in the given cluster
  - `vyos_image_finalize` - phase 2: image created and published to the zones
- `claugine-cli_vm` module
  - `vm_create` - cluster, multiple disks (including ISO), NICs with aliases, boot order, user, password, ssh key
- `claugine-cli_vnet` module
  - `vnet_ar_ip_create`, `vnet_ar_ip_exists`, `vnet_ar_mac_create`, `vnet_ar_mac_get`, `vnet_ip_leased`
- `is_mac`, `is_ipv4` in `claugine-cli_common`
- templates: `ubuntu/` (autoinstall grub.cfg), `vyos/` (image scripts)
- `tests/` - test scripts, test data in `tests/claugine-cli_TEST.sh`, sample in `tests/claugine-cli_TEST_SAMPLE.sh`
- `docs/` - documentation: `QUOTA.md`, `VYOS.md`, `DEVELOPMENT.md`
- `claugine-cli_one-helpers` module, split from `claugine-cli_common`
  - installation context helpers: `_zone_init`, `_zone_check`, `_zones_resolve`, `_zone_vip`, `_zone_label`
  - `_one_object_id` - id of an OpenNebula object by name or id; `ONE_OBJECT_TYPES` in internal data

### Changed
- **zone addressing, see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md#installation-and-zones)**
  - commands work only inside the installation loaded by `fe_data_refresh`; another installation - load it, then run the commands
  - `fe_data_refresh` - creates the `data=` variable if needed, always makes it current, stores `fe`, `master`, `installation`, refreshes through the FE it was loaded from
  - every command checks the context and refreshes it when older than `DATA_REFRESH_LIMIT`; unknown zone ids are rejected
  - new `zones=LIST|ALL` (default `ALL`, same behaviour) - `fe_configs_backups_list`, `fe_configs_backups_cleanup`, `fe_fireedge_views_update`, `acl_role_rights_get`, `acl_role_rights_set`, `quota_cpu_to_vcpu_set`, `quota_ds_set`, `quota_tenant_get`
  - `acl_tenant_get`, `acl_tenant_set`, `fe_backup` - master zone from the context instead of zone `0`
  - log lines start with `installation=... zone_id=... zone_name=... zone_vip=...`; the confirmation prompt shows the installation
- layout: `lib/cli/`, `internal/cli/`, `data/cli/` replaced by `lib/`, `internal/`, `data/`
- file names: `claugine_cli_<area>.sh` renamed to `claugine-cli_<area>.sh`
- internal data split into one file per subsystem: `claugine-cli_data-{admin,common,one,ubuntu,vyos}.sh`
- start-up checks that the tools in `ADMIN_TOOLS` are installed
- nameref variables start with `_`: `local -n _zone=${zone_data}`
- `acl_tenant_set` - dry run by default, ACL table printed with fixed-width columns
- `host_maintenance_on`, `host_maintenance_off` - hosts by name or id through `_one_object_id`: an unknown or not unique host name stops the command; `hosts` is required
- `QUOTA.md` moved to `docs/QUOTA.md`
- `README.md` - layout, conventions and command tables updated
- `bin/hpe-sum-install.sh` - sample SUM host name and repository URL

### Fixed
- `fe_fireedge_views_update` - path to the custom FireEdge views
- `fe_backup full=no` - failed with "unbound variable"


## [01.10.10] - 2026-10-02

### Added
- `acl_tenant_set`

### Changed
- `acl_tenant_get`


## [01.10.00] - 2026-10-01

- First public release.
