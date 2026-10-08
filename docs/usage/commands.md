# Commands

[Documentation](../README.md) › [usage](README.md) › commands

Every command with its main parameters. `claugine_help` prints the full
list with all parameters and defaults. `platform=` is always the first
parameter: `platform=NAME` - exactly one platform, the full name
`platforms.<site>.<platform>`. To work on several platforms, call the
command for each.

## Data

| Command | Parameters | What it does |
|---|---|---|
| `data_load_provider` | `inv=PATH` `secrets=PATH` `runtime=PATH` | load user data: the inventory and secrets, collect the runtime data of every platform, check every federation - inconsistent data is not loaded; again - switch to another data set |
| `data_runtime_refresh` | `[platform=NAME]` `[fe=yes]` | check that user data is loaded and the platform name; collect the runtime data of the platform if missing, `fe=yes` - read the FE nodes again; every command calls it first |
| `inv_value` | `var=` `path=` | a value of a loaded document: `var=CONFIG\|TEMPLATES\|INV\|SECRETS` |
| `inv_list` | `var=` `path=` | the items of a list, one per line |
| `inv_keys` | `var=` `path=` | the keys of an object, one per line |
| `inv_list_field` | `var=` `path=` `field=` | one field of each object in a list |
| `inv_list_find` | `var=` `path=` `[field=name]` `value=` | the object in a list whose field is value |

## Front-end (`fe_*`)

| Command | Parameters | What it does |
|---|---|---|
| `fe_data_refresh` | `platform=NAME` | collect the runtime data of the platform again: zone, FE nodes, datastores, VNet template, shared group; `data_load_provider` collects it for every platform |
| `fe_cfg_ver_get` | `platform=NAME` | show config and DB versions on each FE node |
| `fe_backup` | `platform=NAME` `[full=yes\|NO]` | back up the OpenNebula DB (and configs with `full=yes`) to `repos.backups.local_dir` of the site |
| `fe_configs_backups_list` | `platform=NAME` | list config backups on the FE nodes |
| `fe_configs_backups_cleanup` | `platform=NAME` `[confirm=yes]` | delete old config backups |
| `fe_disk_cleanup` | `platform=NAME` `[confirm=yes]` | free disk space on FE nodes |
| `fe_os_update` | `platform=NAME` `[confirm=yes]` | rolling OS update of FE nodes, followers first |
| `fe_services_restart` | `platform=NAME` `[confirm=yes]` | restart OpenNebula services on FE nodes |
| `fe_fireedge_restart` | `platform=NAME` `[confirm=yes]` | restart FireEdge only |
| `fe_fireedge_views_update` | `platform=NAME` `[src=PATH]` `[confirm=yes]` | push custom FireEdge views (default: `config/files/opennebula-fireedge-views/custom`) |

## Hosts (`host_*`)

| Command | Parameters | What it does |
|---|---|---|
| `host_maintenance_on` | `platform=NAME` `hosts=` `[interval=30]` `[limit=1800]` | disable hosts (names or ids) and wait until all VMs are moved off |
| `host_maintenance_off` | `platform=NAME` `hosts=` | return hosts to service |

## VMs and virtual networks (`vm_*`, `vnet_*`)

| Command | Parameters | What it does |
|---|---|---|
| `vm_create` | `platform=NAME` `cluster=` `name=` `cpu=` `ram=` `image1=` `disk1=` `vnet1=` `addr1=` ... | create a service VM in a cluster: several disks (ISO too), NICs with aliases, routes, boot order, user, password, ssh key |
| `vnet_ar_ip_create` | `platform=NAME` `vnet=` `ip=` | address range for one IP, if missing |
| `vnet_ar_ip_exists` | `platform=NAME` `vnet=` `ip=` | check the address range for an IP |
| `vnet_ar_mac_create` | `platform=NAME` `vnet=` | address range for one MAC, returns the MAC |
| `vnet_ar_mac_get` | `platform=NAME` `vnet=` | latest free MAC of the address ranges |
| `vnet_ip_leased` | `platform=NAME` `vnet=` `ip=` | check that an IP is leased |

## Images and ISOs (`image_*`, `iso_*`, `vyos_*`)

| Command | Parameters | What it does |
|---|---|---|
| `image_upload` | `platform=NAME` `file=\|url=` `name=` `type=` `[ds=]` | new image from a local file or a URL; an existing image with the name is deleted if unused, renamed otherwise |
| `image_publish` | `platform=NAME` `file=` `name=` `[repo=NAME]` | `image_upload` to the platform; with `repo` the FE downloads the file from `repos.zakroma.url_base/NAME` of its site |
| `image_download` | `platform=NAME` `image=` `file=` | copy an image file from the datastore |
| `image_archive` | `platform=NAME` `name=` | free an image name: delete or rename |
| `image_wait` | `platform=NAME` `image=` | wait until an image is READY |
| `iso_get_ubuntu` | `site=\|platform=\|directory=` `[version=]` | latest Ubuntu Server ISO, boots straight into autoinstall |
| `iso_get_vyos` | `site=\|platform=\|directory=` `[url=]` | latest VyOS Stream ISO with the claugine scripts |
| `iso_download`, `iso_customize` | | building blocks of `iso_get_*` |
| `vyos_image_build` | `platform=NAME` `cluster=` `vnet=` `addr=` | VyOS image, phase 1: ISO and builder VM, see [VyOS image](../network/vyos-image.md) |
| `vyos_image_finalize` | `platform=NAME` `vm=` | VyOS image, phase 2: image created and published to the builder's platform; to others - `image_publish` |

## EVPN / VXLAN (`evpn_*`)

| Command | Parameters | What it does |
|---|---|---|
| `evpn_vtep_nic_get` | `platform=NAME` | show VTEP interfaces on nodes |
| `evpn_vtep_vnet_get` | `platform=NAME` | show VTEP settings of VXLAN virtual networks |
| `evpn_vtep_vnet_set` | `platform=NAME` `[confirm=yes]` | fix VTEP settings on virtual networks |
| `evpn_vtep_vnm_patch` | `platform=NAME` `[confirm=yes]` | patch the VXLAN network driver for EVPN (original kept as `.original`) |
| `evpn_vtep_vnm_unpatch` | `platform=NAME` `[confirm=yes]` | restore the original driver |

## ACL (`acl_*`)

| Command | Parameters | What it does |
|---|---|---|
| `acl_role_rights_get` | `platform=NAME` | show VM ADMIN/MANAGE/USE operation sets in `oned.conf` |
| `acl_role_rights_set` | `platform=NAME` `[confirm=yes]` | apply the operation sets of `configs.oned.vm_operations` in `config/data/onefe.yaml` |
| `acl_tenant_get` | `platform=NAME` `[tenants=]` | show ACLs of tenants, read on the primary platform of the federation |
| `acl_tenant_set` | `platform=NAME` `[tenants=]` `[dry=YES\|no]` `[confirm=yes]` | replace ACLs of tenants for every zone of the federation - the primary and its `fe.secondaries` - on the primary platform; dry run by default |

## Quotas (`quota_*`)

Quota records and fields: [tenant quotas](../data/quota.md).

| Command | Parameters | What it does |
|---|---|---|
| `quota_tenant_get` | `platform=NAME` `tenant=` | show a tenant's quotas and usage |
| `quota_ds_set` | `platform=NAME` `[tenants=]` `[confirm=yes]` | apply datastore quotas from `patch.yaml` of the zone's inventory platform; needs a data set |
| `quota_vcpu_conf_show` | `platform=NAME` | show vCPU-based quota configuration |
| `quota_cpu_to_vcpu_set` | `platform=NAME` `[confirm=yes]` | set each VM's CPU equal to its vCPU |

## Other

| Command | Parameters | What it does |
|---|---|---|
| `claugine_help` | | list all commands |
