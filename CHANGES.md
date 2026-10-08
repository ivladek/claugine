# Changelog

## [02.00.00] - 2026-10-06

Major version, the project is renamed to **claugine**: new layout and file names, all data in YAML loaded into one
JSON documents, the inventory as the only source of platform data - every command names its target platform, new modules for VMs, networks, images and ISOs.
Data files and scripts written for 01.x need the changes listed below.

### Upgrading from 01.x
- install `yq` (Go or Python version) next to `jq`
- the CLI no longer needs a data file: 01.x variables map to

  | 01.x variable | 02.00.00 |
  |---|---|
  | `DC1`, `DC2`, ... - FE VIPs | `fe.yaml` of each platform in the inventory: `vip` |
  | `IMAGES_DS_LIST[vip]`, `IMAGES_DS[vip,cluster]` | not needed: claugine finds the IMAGE datastores of each platform and cluster |
  | `VNTEMPLATE_ID`, `FILES_DS`, `BACKUPS_DS`, `SHARED_ID` | not needed: claugine finds them |
  | `IMAGES_QUOTA`, `FILES_QUOTA`, `BACKUPS_QUOTA`, `*_DEFAULT` | `patch.yaml` of the cloud platform in the inventory: `ds_quotas.tenants.<tenant>.*`, `ds_quotas.default.*` - see [docs/data/inventory.md](docs/data/inventory.md#patchyaml) |
  | `DIR_BACKUPS` | site file in the inventory: `repos.backups.local_dir` |
  | `DIR_*_REPO`, `URL_*_REPO[vip]` | site file in the inventory: `repos.zakroma.local_dir`/`url_base` + `/ubuntu`, `/vyos` |
  | `TIMEZONE_DEFAULT` | site file in the inventory: `timezone` |
- load: `source bin/claugine.sh`, then `data_load_provider inv=PATH secrets=PATH runtime=PATH` - the inventory describes every platform
- scripts: no `fe_data_refresh fe=IP` first; every command takes `platform=` as its first parameter instead of `zone_id=`/`zones=`: exactly one full name `platform=platforms.<site>.<platform>`, no lists

### Added
- data structure, see [docs/data](docs/data/README.md)
  - `config/` - data of the tool: `data/` (YAML), `templates/` (YAML templates of config files), `files/` (was `templates/`)
  - `config/data` by subsystem: `admin`, `cli`, `onefe`, `onehost`, `repos`, `timezones`, `ubuntu`, `vyos`; `config/templates` by group: `ubuntu/`, `ssh/`, `onehost/`, `onefe/`, `one_objects/`
  - data set: `inventory/` with a fixed structure - providers, server profiles, resources and platforms per site; `secrets/` - free structure, used only through references; `runtime/`
  - `data/SAMPLE` - published sample data set with fake values
  - four source documents loaded at start-up, shell globals, not exported: `INV` - inventory, `SECRETS`, `CONFIG` - `config/data`, `TEMPLATES` - `config/templates`
  - return codes: `0` - success, `1` - any error; other codes only where a caller checks them as an answer: `vnet_ar_ip_exists`, `vnet_ip_leased` - `4`, `vnet_ar_mac_get` - `3`
  - modules: functions in alphabetical order, `# INDEX` of the functions at the top of each module
  - code layout: every function has its help entry (a command) or a description and its arguments (an internal function), then its return codes; all `local` declarations at the start; every `for` loop ends with `done  # <variable>`, every function with `}  # name`; functions not called by hand start with `_` (`_is_ipv4`, `_is_mac`, `_stop_without_confirmation`, `_os_service_wait`, `_data_load_var`, `_script_INIT`, `_script_MAIN`); the help array is `claugine_help`
  - `claugine_inventory` module: getters per shape of the data, after `_archive/lib/lib_inventory.sh` - `inv_value`, `inv_list`, `inv_list_field`, `inv_keys`, `inv_list_find`; each document is used on its own: `var=CONFIG|TEMPLATES|INV|SECRETS path=A.B`
  - modules only define functions and help texts, nothing is read while loading: `source` loads the modules, then `CONFIG` and `TEMPLATES`; a default from internal data is shown in the help as its key, `limit=N(<CONFIG.onefe.timeouts.image_wait>)` - its value: `inv_value var=CONFIG path=onefe.timeouts.image_wait`
  - `_data_load_var var= dir=` - loads a YAML tree only into one of `INV_VARS`, prints statistics of the loaded document; the variables hold pretty-printed JSON
  - `source bin/claugine.sh` loads only internal data (`CONFIG`, `TEMPLATES`); `data_load_provider` loads user data (`INV`, `SECRETS`, `DIR_RUNTIME`) and clears `RUNTIME`, again - switches to another data set; every command first checks that user data is loaded
- `claugine_image` module
  - `image_wait` - wait until an image is READY
  - `image_archive` - free an image name: delete the image if no VM uses it, rename it to `NAME (YYYY-MM-DD)` otherwise
  - `image_upload` - upload a local file or let the FE download it from a URL; default datastore - the IMAGE datastore of the platform with the lowest id, the built-in `default` only if it is the only one
  - `image_download` - copy an image file from the FE datastore
  - `image_publish` - upload a local image file to a platform; with `repo=NAME` the FE downloads it from `repos.zakroma.url_base/NAME` of its site
- `claugine_iso` module
  - `iso_download` - download with resume and sha256 check
  - `iso_customize` - add files from `config/files/<os>/` to an ISO, keep it bootable
  - `iso_get_ubuntu` - latest Ubuntu Server ISO, boots straight into autoinstall
  - `iso_get_vyos` - latest VyOS Stream ISO with the claugine scripts
  - result of `iso_get_*` in the global associative array `iso_info`
- `claugine_vyos` module, see [docs/network/vyos-image.md](docs/network/vyos-image.md)
  - `vyos_image_build` - phase 1: ISO, builder VM in the given cluster
  - `vyos_image_finalize` - phase 2: image created and published to the builder's platform; to others - `image_publish`
- `claugine_vm` module
  - `vm_create` - cluster, multiple disks (including ISO), NICs with aliases, boot order, user, password, ssh key
- `claugine_vnet` module
  - `vnet_ar_ip_create`, `vnet_ar_ip_exists`, `vnet_ar_mac_create`, `vnet_ar_mac_get`, `vnet_ip_leased`
- `_is_mac`, `_is_ipv4` in `claugine_common`
- templates: `ubuntu/` (autoinstall grub.cfg), `vyos/` (image scripts)
- `tests/` - test scripts, test data in `tests/claugine_TEST.sh`, sample in `tests/claugine_TEST_SAMPLE.sh`
  - read-only and dry-run tests with a pass/fail summary: `data-load.sh`, `platform-name.sh`, `read-only.sh`, `acl-tenant-dry.sh`
  - test data: `test_secondary`, `test_platform_local`, `test_tenant`; `test_platform` is the primary of a federation
- `docs/` - documentation in five sections: `platform/`, `data/`, `tool/`, `network/`, `usage/`
- `claugine_helpers` module, split from `claugine_common`
  - `data_runtime_refresh [platform=NAME] [fe=yes]` - the first call of every command: checks that `INV` is not empty and that `platform=` is exactly one full name, collects the runtime data of the platform the first time it is touched; `fe=yes` - FE node states again
  - `RUNTIME` holds only what OpenNebula knows: zone, shared group, VNet template, FILE/BACKUP datastores, IMAGE/SYSTEM datastores with their clusters, FE nodes; the VIP, site, mode and primary are read from the inventory
  - helpers: `_one_object_id`; a value of the site of a platform is read with `inv_value var=INV path=resources.<site>.<key>`, the site taken from `platforms.<site>.<platform>`
  - logging: every log line and error block is printed by `_log`, `_log_std` (platform header `platform= zone_id= zone_vip=`), `_log_fe` (plus `node_role= node_name= node_ip=`) and `_log_error` (one `!!! ERROR !!!` / `!!! TRACE !!! <command>` form), all in `claugine_helpers.sh`
  - `_one_object_id` - id of an OpenNebula object by name or id; types in `config/data/onefe.yaml`

### Changed
- **platform addressing, see [docs/tool](docs/tool/README.md#platforms)**
  - no context and no current platform: every command takes `platform=` first - exactly one platform, the full name `platforms.<site>.<platform>`; no platform lists - several platforms, several calls; the data is trusted - a command checks only that `INV` is loaded, an unknown platform is reported when it is touched first; `zone_id=`, `zones=`, `fe_data_refresh fe=`, `data=VAR_NAME`, `zone_data` are gone
  - `RUNTIME` - runtime data of the platforms touched in the shell: zone, FE nodes, datastores, VNet template, shared group, primary; collected the first time a command touches a platform, not refreshed on a timer; FE node states are read again at the start of every command that walks the FE nodes
  - `fe_data_refresh platform=NAME` - collects the runtime data again
  - federation-wide objects (tenant groups, ACLs) are changed on the primary platform: `acl_tenant_get`, `acl_tenant_set` given a secondary follow `fe.primary`
  - `fe.yaml: secondaries` of the primary platform lists the FEs of its secondaries; `acl_tenant_set` sets ACLs for the zones of the primary and its secondaries; a command given a secondary switches to `fe.primary`
  - `data_load_provider` runs `_data_consistancy_check`; step 1 checks every federation: each zone of a primary FE must be the primary or one of its `fe.secondaries`; otherwise, or when a primary FE is not reachable, the data is not loaded (`INV`, `SECRETS`, `RUNTIME` cleared)
  - `fe_backup` - each platform to `repos.backups.local_dir` of its site; the federated DB on the primary platform
  - unknown platforms are rejected with the list of the inventory platforms
  - log lines start with `platform=... zone_id=... zone_name=... vip=...`; the confirmation prompt shows the platform
- layout: `lib/cli/`, `internal/cli/`, `data/cli/` replaced by `lib/`, `config/`, `data/`
- project renamed from claugine-cli to **claugine**: entry point `bin/claugine.sh`, files `claugine_<area>.sh` (were `claugine_cli_<area>.sh`), help `claugine_help`, command list `claugine_help`
- internal data: shell constants replaced by `config/data/*.yaml`, read through `CONFIG`; platform data files `data/claugine_<name>.sh` dropped: the CLI reads what it needs from OpenNebula
- start-up checks `jq` and `yq` first, then the tools in `config/data/admin.yaml`
- `iso_get_*`, `vyos_image_*`, `image_publish repo=` - local repository and its URL from `repos.zakroma` of the site in the inventory
- claugine also finds the IMAGE datastores of each cluster; `quota_ds_set` sets the images quota on them
- `vm_create` - `tz=` default is the `timezone` of the platform's site in the inventory
- every function, internal helpers included, takes `key=value` parameters with the same argument loop - no positional parameters
- nameref variables start with `_`: `local -n _list=${name}`
- `acl_tenant_set` - dry run by default, ACL table printed with fixed-width columns
- `host_maintenance_on`, `host_maintenance_off` - hosts by name or id through `_one_object_id`: an unknown or not unique host name stops the command; `hosts` is required
- `QUOTA.md` moved to `docs/data/quota.md`
- `README.md` - a short project brief; usage, command tables and conventions moved to `docs/`
- `bin/hpe-sum-install.sh` - sample SUM host name and repository URL

### Fixed
- `fe_fireedge_views_update` - path to the custom FireEdge views
- `fe_backup full=no` - failed with "unbound variable"
- `evpn_vtep_vnet_get`, `evpn_vtep_vnet_set` - failed with "unbound variable" when the VNet template id was not in the data file; `vntemplate_type` showed the type of the last VNet instead of the template
- `acl_tenant_set`, `quota_ds_set` - missing platform data is reported before any change instead of failing in the middle


## [01.10.10] - 2026-10-02

### Added
- `acl_tenant_set`

### Changed
- `acl_tenant_get`


## [01.10.00] - 2026-10-01

- First public release.
