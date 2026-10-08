# Usage

[Documentation](../README.md) › usage

For platform administrators: how to install and start claugine, load
platforms and run commands. Command reference: [commands.md](commands.md).
Included tests: [tests.md](tests.md).

- [Requirements](#requirements)
- [Install](#install)
- [Data set](#data-set)
- [Start](#start)
- [Platforms](#platforms)
- [Confirmation](#confirmation)
- [Examples](#examples)

## Requirements

On the admin workstation:

- **bash 4.3+**; on macOS install a current version with `brew install bash`;
- **jq** and **yq** - the data is loaded with them; either yq works: the Go
  version (`brew install yq`) or the Python one (`pip install yq`);
- the tools of `config/data/admin.yaml`, checked at start-up: `curl grep jq
  mkpasswd onezone rsync sed sha256sum sort ssh tail tee xorriso yq`;
- `ssh` access to every FE node as a user with passwordless `sudo`.

## Install

```bash
git clone https://github.com/ivladek/claugine.git ~/code/claugine
```

## Data set

User data - the inventory and the secrets - is loaded after claugine with
`data_load_provider`; the inventory is the only source of platform data - see
[data](../data/README.md). Every command finds its FE by the
`vip` in `fe.yaml` of the platform, and reads from the inventory what
OpenNebula does not know:

| What | Inventory | Commands |
|---|---|---|
| FE of the platform | `fe.yaml`: `vip`, `mode`, `primary` | all |
| backups directory | `repos.backups.local_dir` of the platform's site | `fe_backup` |
| local repository of images and ISOs | `repos.zakroma.local_dir` of the site: `<local_dir>/ubuntu`, `<local_dir>/vyos`, ... | `iso_get_*`, `vyos_image_*` |
| URL of that repository for the FEs | `repos.zakroma.url_base` of the platform's site | `image_publish repo=`, `vyos_image_*` |
| time zone of VMs | `timezone` of the platform's site | `vm_create` without `tz=` |
| tenant datastore quotas | `patch.yaml` of the platform | `quota_ds_set` |
| shared group | `shared_owner` in `platform.yaml` | tenant commands |

Start your data set from the sample:

```bash
cp -r ~/code/claugine/data/SAMPLE ~/claugine-data   # keep real data outside the repository
```

## Start

claugine is a set of shell functions: `source` it into your shell, do not
execute it. Then load your data:

```bash
source ~/code/claugine/bin/claugine.sh                 # internal data and modules
data_load_provider inv=~/claugine-data/inventory \
  secrets=~/claugine-data/secrets runtime=~/claugine-data/runtime
```

`data_load_provider` collects the runtime data of every platform from its FE
and checks the data: every zone of a federation must be a platform of the
inventory (the primary or one of its `fe.secondaries`). Inconsistent data,
or a primary FE that can not be reached, is not loaded. Calling it again
switches to another data set. Without user data, commands stop with a
message.

Start-up options of `source bin/claugine.sh`, all optional:

| Option | Default |
|---|---|
| `ssh_user=NAME` | `${USER}` |
| `ssh_port=N` | `22` |
| `ssh_keyf=FILE` | `~/.ssh/${USER}.key` |

All three parameters of `data_load_provider` are required; the runtime directory is
created if missing.

The loader prints what it loaded into `CONFIG` and `TEMPLATES`, the modules,
the tool check and the command list; `data_load_provider` - statistics of `INV` and
`SECRETS` (see [loading a tree](../data/README.md#loading-a-tree)), the
runtime directory and the platforms of the inventory:
`dc1.mgmt dc1.payload1 ...`. `claugine_help` prints the
command list again at any time.

## Platforms

Parameters are always `name=value`. Every command names its target as an
inventory platform - the first parameter, `platform=`:

```bash
fe_cfg_ver_get platform=platforms.dc1.payload1      # exactly one platform, the full name
for p in platforms.dc1.payload1 platforms.dc2.payload1; do fe_services_restart platform=${p}; done   # several - one by one
```

- `platform=NAME` - exactly one platform, the full name
  `platforms.<site>.<platform>`; a list, a short name or an empty value is
  rejected;
- federation-wide objects (users, groups, ACLs) are changed on the primary
  platform: give any platform of the federation, the command goes to the
  primary and says so.

There is no context to load and no platform to switch to. `data_load_provider`
collects what OpenNebula knows about every platform - zone, FE nodes,
datastores, VNet template, shared group - and keeps it for the shell
session; FE node states are read again at the start of every FE operation.
`data_runtime_refresh` does it for a command - and can be called directly:

```bash
data_runtime_refresh platform=platforms.dc1.payload1 fe=yes
```

To collect everything of one platform again:

```bash
fe_data_refresh platform=platforms.dc1.payload1
```

The platforms of the inventory are printed by `data_load_provider`; an unknown
platform is rejected when a command touches it.

## Confirmation

Every command that changes something prints the platform and the planned
action and asks `yes/no` (declined automatically after 60 s). In scripts add
`confirm=yes`. Commands that change many objects at once (`acl_tenant_set`)
run dry by default: `dry=no` to apply.

## Examples

Read-only:

```bash
fe_cfg_ver_get platform=platforms.dc1.payload1          # config and DB versions on every FE node
quota_tenant_get platform=platforms.dc1.payload1 tenant=romashka # a tenant's quotas and usage
evpn_vtep_vnet_get platform=platforms.dc1.payload1               # VTEP settings of VXLAN networks
acl_tenant_get platform=platforms.dc2.payload1 tenants=romashka  # read on the primary, dc1.payload1
```

Front-end maintenance:

```bash
fe_backup platform=platforms.dc1.payload1 full=yes      # to repos.backups.local_dir of the site
fe_os_update platform=platforms.dc2.payload1                     # rolling: followers first, then the leader
fe_services_restart platform=platforms.dc1.payload2
```

Host maintenance:

```bash
host_maintenance_on  platform=platforms.dc1.payload1 hosts="kvm01 kvm02"   # disable, wait until the VMs move off
host_maintenance_off platform=platforms.dc1.payload1 hosts="kvm01 kvm02"
```

Tenants:

```bash
acl_tenant_set platform=platforms.dc1.payload1 tenants=romashka          # dry run: shows the ACLs it would set
acl_tenant_set platform=platforms.dc1.payload1 tenants=romashka dry=no
quota_ds_set platform=platforms.dc1.payload1 tenants=romashka            # datastore quotas from patch.yaml
```

Images:

```bash
iso_get_ubuntu site=dc1                                 # latest Ubuntu Server ISO, autoinstall
image_publish platform=platforms.dc1.payload1 file="${iso_info[iso]}" name="Ubuntu 26.04 ISO" \
  type=CDROM prefix=sd repo=ubuntu                      # the FE downloads from repos.zakroma.url_base/ubuntu
vyos_image_build platform=platforms.dc1.mgmt cluster=0 vnet=core addr=10.71.101.223   # see network/vyos-image.md
```

Service VM:

```bash
vm_create platform=platforms.dc1.mgmt cluster=0 name=svc1 hostname=svc1 cpu=2 ram=4 \
  image1="Ubuntu 26.04" disk1=10 vnet1=core addr1=10.71.101.50 gw1=10.71.101.254 \
  user=admin pswd='...' key="ssh-ed25519 ..."
```
