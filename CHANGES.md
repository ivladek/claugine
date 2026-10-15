# Changelog

## [02.10.00] - 2026-10-15

### Changed
- jobs: new `quota-set.sh` (`quota_tenant_set`, dry run by default), `quota-list.sh` (`quota_tenants_report`), `quota-show.sh` (`quota_tenant_show`) - every platform of `QUOTA_PLATFORMS` of the job data or `platforms=`; SAMPLE: `state: deployed` in every `platform.yaml`, `QUOTA_PLATFORMS` in `jobs-data.sh`
- cluster kinds: `mgmt`, `shared`, `dedicated`, `service`, `portal` - checked by `_data_check_inventory` against the platform kind: `mgmt` - exactly one `mgmt` cluster; `shared` - `shared`, `dedicated`, `service`, at least one `shared`; `portal` - `portal` only; `quota_code` - required for `shared` and `dedicated`, not allowed for the others; SAMPLE: mgmt and portal clusters of their kinds, quota codes for dc2/payload1
- `clusters.yaml`: `type` renamed `kind` - the same word as `platform.kind` (SAMPLE, frcloud); a platform of `kind: shared` must have at least one cluster of `kind: shared`
- `_data_availability_get` - why a platform is unavailable, `RUNTIME[<platform>,availability]`: `unreachable` (VIP and FE nodes do not answer ssh - powered off or network), `vip_down` (a node answers, the VIP does not), `oned_down` (`onezone list` fails), `no_zone` (no zone of the VIP); shown in the load errors
- `fe_data_refresh` removed: the runtime data is collected and checked by `data_provider_load` - run it again to collect everything again; `data_platform_refresh fe=yes` - the FE nodes
- quota: `_data_check_quota` - after the quota load and `quota=reload`: every tenant has at least one qr on a shared platform; `quota_tenant_set` - the datastores of the platform not of the tenant set to -1; `_get_tenants` - an error if `onegroup list` fails; `data_provider_load quota=load` fails - everything cleared; CPU usage rounded up only with a fraction (2.0 - 2)
- clusters: a cluster of `clusters.yaml` not on the platform - undeployed, not an error: `RUNTIME[<platform>,undeployed_clusters_list]` (names), no id, no quotas, not in `qr_list`; a cluster of the platform missing in `clusters.yaml` - still an error
- platform `state` in `platform.yaml`: `planned` (to be deployed - not in `PLATFORMS`, listed in `PLATFORMS_PLANNED`, not collected, not checked; can not be in `fe.secondaries`/`fe.primary` until deployed - checked by `_data_check_inventory`), `updated` (clusters to deploy or delete), `retired` (to destroy), not set - `deployed`; `data_platform_refresh` on a planned platform - only with `deployment=yes`, no runtime data; unavailable platforms: the FE not reachable or no zone of its VIP - `RUNTIME[<platform>,available]=no`, an error - the data not loaded, except `state: retired` - not checked, the load goes on, commands on it fail; skipped by the federation check, the quota load, `acl_tenant_get`, `fe-backup` (reported), the VyOS image targets; `acl_tenant_set` fails on an unavailable zone - the ACLs untouched; `_data_check_inventory` - every platform has `fe.vip`; a tenant qr whose clusters are all undeployed - skipped (`QUOTA[...,undeployed_qrs]`, `RUNTIME[<platform>,undeployed_qr_list]`), counted by `_data_check_quota`
- `DEPLOYMENT=required` - a global set by `data_provider_load` when a platform is planned, updated or retired, or a cluster is undeployed: only deployment operations are enabled - `data_platform_refresh deployment=yes`, every other command fails; the quota data is not loaded; shown by `source bin/claugine`
- entry point `bin/claugine` (was `bin/claugine.sh`), it gives the help - the `claugine_help` command is removed: `source bin/claugine` - load once, then the command list and the data status; run directly - `bin/claugine [--help]` the command list, `bin/claugine [--help] PATTERN ...` the full help of every command with PATTERN in its name
- `_data_consistancy_check`, every violation is reported: every platform of the inventory has runtime data - its FE is reachable; every cluster has exactly one IMAGES and one VMS datastore, every such datastore belongs to a cluster; every federation OpenNebula reports - every zone is a platform (endpoint = `fe.vip`), exactly one `fe.mode: primary` lists all others in `fe.secondaries`, each of them is `fe.mode: secondary` with `fe.primary` to it
- `acl_tenant_get`, `acl_tenant_set`, `quota_tenant_get`: groups below id 100 (`oneadmin`, `users`) and the shared group are never handled as tenants - `tenants=ALL` no longer touches their ACLs and quotas
- `vm_create`: `addrN=auto` - OpenNebula leases a free IP of the VNet, no aliases
- `binaries/` - new: the prebuilt VyOS 2026.03 ISO with the claugine scripts and the VyOS image qcow2 on Google Drive, the link in `binaries/README.md`; the files are ignored by git
- `image_upload` - the new image goes to the shared group of the platform (`platform.shared_owner`), if it has one
- `acl_tenant_set`, `acl_tenant_get` - the clusters of a tenant from its `QUOTA_<SITE>_<CLUSTER>` group attributes (were the HOST ACLs of the tenant group); deleted - the ACLs of the tenant group, its role groups and the admin; `acl_tenant_get` shows them with the ACLs; a tenant without the attributes, without its role groups, or with a cluster not found in a zone - a warning, its ACLs untouched
- `_data_consistancy_check` renamed `_data_check`; `_data_check_platform` - also exactly one FILE and one BACKUP datastore, at least one IMAGES and one VMS datastore
- quota: a qr always has cpu, ram_gb, disk_gb, gpu not set - 0; datastore quotas - IMAGES always -1, the number of images not limited
- quota data: loaded for all shared platforms at once or for none - the global `QUOTA_LOADED`, `QUOTA[<platform>,loaded]` removed; `data_platform_refresh quota=reload|refresh tenants=` - limit, real, usage or usage of these tenants only; `quota_tenant_set` reloads the tenant it set
- `quota_tenant_set` - one template per tenant for every cluster of the platform: the tenant's qrs - their limits, other qrs of the platform and the clusters without `quota_code` (`RUNTIME[<platform>,noqr_clusters_list]`) - all `-1`; the group's current VM quotas no longer read (`vm_quotas` removed)
- qr - the cluster part of `QUOTA_<SITE>_<qr>`, the site code stripped; it is `quota_code` of `clusters.yaml` (`STD01`, not `DC1_STD01`); a qr without clusters or a tenant without qrs on any shared platform - an error, the quota data not loaded; no qrs on one site - the tenant has no quotas there
- `QUOTA` keys - tenant first: `[<tenant>,<platform>,...]` platform wide, `[<tenant>,<platform>,<qr>,...]` per quota record, `[<tenant>,group_id]`; quota record - `qr` in keys and code: `qrs`, `RUNTIME[<platform>,qr_list]`, `RUNTIME[<platform>,<qr>,clusters_list]`
- quota module: new `quota_tenant_set` (the group quotas from `QUOTA` limits - VM per record, datastores; VM quotas of other clusters deleted; dry run by default), `quota_tenant_show`, `quota_tenants_report` (check: ok, differ, exceeded, no_clusters); removed `quota_ds_set`, `quota_tenant_get`, `quota_vcpu_conf_show`; `quota_cpu_to_vcpu_set` kept
- data load flow: `data_provider_load` - the runtime data of every platform, `_data_consistancy_check`, then with `quota=load` the quotas of the platforms of kind `shared`; `data_platform_refresh quota=SKIP|load|refresh` - the only entry to the quota data: the platform must be kind `shared`, the quotas loaded if missing, `refresh` - the usage again
- `data_quota_load`, `data_usage_refresh` - internal: `_data_quota_load` (limit, real, then usage), `_data_usage_refresh` (usage); `_platform_shared_check` removed - replaced by `data_platform_refresh quota=load`
- `_data_consistancy_check` split: `_data_check_inventory` (no platform calls), `_data_check_platform`, `_data_check_federations`
- `fe.yaml`: `primary` and `secondaries` - platforms `platforms.<site>.<platform>`, without `.fe`
- `_platform_shared_check` - also the quota data loaded; every action on a shared platform calls it, `acl_tenant_*` for every zone of the federation, `data_usage_refresh` too; `data_quota_load` - kind only (`quota=no`)
- `kind: shared` of `platform.yaml` - the only sign of a platform with tenants: `data_quota_load`, `acl_tenant_get`, `acl_tenant_set`, `quota_ds_set`, `quota_tenant_get` stop on another kind, `data_provider_load quota=load` loads the quotas of these platforms only; helper `_platform_shared_check`
- renamed: `data_load_provider` - `data_provider_load`, `data_runtime_refresh` - `data_platform_refresh`; at start the script loads its own data only
- `data_provider_load` - `quota=load|SKIP`: the tenant quotas of every platform loaded by `data_quota_load`
- `data_platform_refresh` - the platform data only: `quota=`, `usage=` removed
- `data_quota_load` - datastore quotas: `QUOTA[<platform>,<tenant>,ds,<id>,limit|real|usage,size_gb]` for the IMAGE datastores of the records' clusters and the FILE and BACKUP datastores; defaults in `config/data/quota.yaml`
- new `data_quota_load`: the tenant quotas in `QUOTA[<platform>,<tenant>,<record>,limit|real|usage,<field>]`, `QUOTA[<platform>,<tenant>,site,<field>]`; the cluster of a record - `quota_code` of `clusters.yaml`; new `data_usage_refresh`: the usage again
- `quota_ds_set` - the datastore quotas from the group attribute `QUOTA_<SITE>` (`IMAGES_GB`, `FILES_GB`, `BACKUPS_GB`); `patch.yaml` dropped from the inventory; a tenant without `QUOTA_<SITE>` - a warning, its quotas untouched
- `acl_tenant_set` - tenant roles: the rights go to the role groups `sa-<name>` (super admin), `power-<name>`, `user-<name>`, `ro-<name>` (read only) instead of the tenant admin user; users must have `tenant-<name>` as the primary group and a role group as a secondary one, see `docs/platform/payload.md`
- `vyos_image_create` - new: `vyos_image_vm_create`, the wait for the power off (`limit=600`), `vyos_image_to_platforms` in one run
- `vm_delete platform= vm=ID|NAME limit= confirm=` - new: terminate a VM and wait until it is gone; used by `vyos_image_to_platforms`
- `vm_wait platform= vm=ID|NAME state=NAME limit=` - new: a VM state or an LCM state of an active VM by name, stops at once on a failure, `UNKNOWN` or an unexpected `DONE`; replaces the internal `_vyos_vm_wait`
- ISO: the downloaded file keeps the name from its url, the modified one is `<name>-claugine.iso` (`iso_customize dst=` mandatory); `iso_get_ubuntu` and `iso_get_vyos` return its full path in the caller variable of `result=VAR`; the repository is `repos.zakroma.local_dir/<ubuntu|vyos>` of the site of `platform=`, or `directory=` (`site=` removed); `iso_info` and `_iso_repo_dir` removed
- `image_upload` takes a local `file=` only, copied to the FE by rsync - `url=` removed; `cluster=ID|NAME` (default `0`) replaces `ds=` - the image goes to the IMAGES datastore of the cluster; `format=` and `prefix=` removed - set by `type=` for q35 VMs: CDROM - `sd`, OS and DATABLOCK - `vd` and qcow2; `image_publish` removed - use `image_upload`
- `fe_backup`: every step is checked - a failed archive, DB dump or copy is reported and the next one goes on; returns 1 if any failed, old backups are removed only after a backup without errors
- data sets: `data/<set>/` holds `inventory/`, `secrets/`, `runtime/` and the values of the scripts - `tests-data.sh` (was `tests/claugine_TEST.sh`), `jobs-data.sh`; tests take the data set from `CLAUGINE_DATA`, a job loads only its `DATA=` file
- `jobs/` - new: production scripts for cron; `fe-backup.sh` - FE backup of every platform of the site of the admin host, a Telegram report per platform; `create-vyos-image.sh` - the VyOS image by hand in one run by `vyos_image_create`, the platforms of `VYOS_IMAGE_TARGET_PLATFORMS`, `all` or empty - every platform - the image VM and the ISO image deleted, the image published; `fireedge-views.sh` - the FireEdge views to the FEs of `FIREEDGE_VIEWS_PLATFORMS`; `acl-reset.sh` - the tenant ACLs of `ACL_PLATFORMS` deleted and created again by `acl_tenant_set`, `dry=yes` by default
- output: `_log` is the only way to stdout, `_log_error` writes to stderr; every field of a log record is `name=value` or `name=[...]` - lists and the progress of waits in brackets: `platforms: [...]`, `desired_state=RUNNING status=[....] state=RUNNING`

### Draft
- `claugine_vyos` - to be reviewed by Monday, 2026-10-12: changed together with the other modules, not reviewed and not run yet; `vyos_image_vm_create addr=` defaults to `auto` - a free IP leased from the VNet, logged as `vm_ip=`; the image VM is named as the image it makes - `<image_name> <version>`, `vm.prefix` removed
- VyOS image (VyOS 1.5, Stream 2026.03): the grub of the installer is kept - standard VyOS boot (`add system image`, kernel options, console, password recovery), claugine `grub.cfg` removed; the default password only in `password` of `config/data/vms.yaml` (was `builder.password`), to the image VM context by `vm_create`; `SET_HOSTNAME` of the context (was `HOSTNAME`); `SSH_PORT` always applied; the context CD by its label `CONTEXT`, no ssh to the image VM in any phase, the installed system never boots during the build: `vyos-image-prepare` applies the image config `VYOS_INITITAL_CONFIG` to the live session (password hash of `CRYPTED_PASSWORD_BASE64` of the context), copies the config saved by VyOS to the installed disk with the scripts and powers off; `vyos_image_to_platforms` saves the disk, then deletes the image VM and the ISO image (`ip=`, `limit=`, `builder.wait`, `vyos-image-finalize` removed); `vyos-first-run.script` merged into `vyos-postconfig-bootup.script`: the context on the first boot or on every boot with `VYOS_CONFIG_REAPPLY=YES` (all ethernet interfaces and static routes replaced, no reboot), `CONTEXT_SCRIPTS_ENFORCE=YES` for the context scripts, ssh password authentication disabled only with `SSH_PUBLIC_KEY`, `FIRSTRUN_SCRIPT_DISABLE` and `CONTEXT_SCRIPTS_DISABLE` removed; image VM version parsed correctly; `vyos_image_vm_create result=VAR` returns the image VM name - the default image name of `vyos_image_to_platforms`; `vyos_image_create` - `vyos_image_vm_create`, the wait for the power off, `vyos_image_to_platforms` in one run; `vyos_image_build` renamed to `vyos_image_vm_create`, `vyos_image_finalize` to `vyos_image_to_platforms`, the builder VM is the image VM: `vyos.vm` of `config/data/vyos.yaml` (was `vyos.builder`), `VYOS_IMAGE_VM_*` of the job data (was `VYOS_BUILDER_*`), template `vyos/imagevm.yaml`

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
  - code layout: every function has its help entry (a command) or a description and its arguments (an internal function), then its return codes; all `local` declarations at the start; every `for` loop ends with `done  # <variable>`, every function with `}  # name`; functions not called by hand start with `_` (`_is_ipv4`, `_is_mac`, `_stop_without_confirmation`, `_os_service_wait`, `_data_load_var`, `_script_INIT`, `_script_MAIN`); the help entries are in `help_data`
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
  - `vyos_image_vm_create` - phase 1: ISO, image VM in the given cluster
  - `vyos_image_to_platforms` - phase 2: image created and published to the image VM's platform; to others - `image_publish`
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
