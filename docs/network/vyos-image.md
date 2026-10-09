# VyOS image for OpenNebula

[Platform](../platform/README.md) › [Network](README.md) › VyOS image

This document describes how claugine builds a VyOS Stream router image
for OpenNebula and how VMs created from that image configure themselves.

- [Result](#result)
- [How it works](#how-it-works)
- [Before you start](#before-you-start)
- [Files](#files)
- [Phase 1: `vyos_image_build`](#phase-1-vyos_image_build)
- [Manual steps on the console](#manual-steps-on-the-console)
- [Phase 2: `vyos_image_finalize`](#phase-2-vyos_image_finalize)
- [VMs created from the image](#vms-created-from-the-image)
- [Publishing to another platform](#publishing-to-another-platform)
- [Existing images with the same name](#existing-images-with-the-same-name)
- [Cleanup](#cleanup)
- [Troubleshooting](#troubleshooting)

## Result

- An OS image `VyOS Router <version>` (qcow2) in the images datastore of every
  zone of the current platform (or of the zones you choose).
- A local copy of the image, `vyos-<version>.qcow2`, in the local repository.
- A VM created from the image applies the OpenNebula context on its first boot:
  addresses, gateway, routes, MTU, host name, user, password, ssh key, time zone.

## How it works

VyOS publishes no cloud image for OpenNebula, and `install image` is
interactive. The build is split into two automated phases with three commands
on the VM console in between.

```mermaid
flowchart TD
  A["vyos_image_build<br/>(admin host)"] -->|ISO + builder VM| B["VM console<br/>live session from the ISO"]
  B -->|"install image<br/>vyos-image-prepare<br/>VM powers off"| C["vyos_image_finalize<br/>(admin host)"]
  C -->|"first boot: context applied, reboot"| D["vyos-image-finalize<br/>(on the VM): reset, power off"]
  D --> E["disk saved, downloaded,<br/>published to the zones"]
```

| Step | Where | Who | What happens |
|---|---|---|---|
| 1 | admin host | `vyos_image_build` | download the latest VyOS Stream ISO, add the claugine files, upload the ISO, create the builder VM |
| 2 | VM console | you | `install image`, then `vyos-image-prepare`: grub and scripts written to the installed disk, VM powers off |
| 3 | admin host | `vyos_image_finalize` | detach the ISO, boot the installed system, wait for the first run |
| 4 | builder VM | `vyos-image-finalize` | reset the configuration to the image defaults, re-enable the first run, power off |
| 5 | admin host | `vyos_image_finalize` | save the disk as an image, download it, publish it to the zones |

## Before you start

**Workstation** - the tools from `config/data/admin.yaml` (checked at start-up), in
particular `curl`, `xorriso`, `rsync`, `jq`, `ssh`.

**Data set** - the site of the builder zone in the inventory:
`repos.zakroma.local_dir` - the local repository, the ISOs and the image go
to its `vyos/` subdirectory (created if missing); `repos.zakroma.url_base` -
the URL the FEs reach it by: the FE downloads files from `<url_base>/vyos`
instead of an rsync copy from the workstation.

**Internal data** (`config/data/vyos.yaml`) - defaults, change
only if needed:

| Key | Default | Meaning |
|---|---|---|
| `url` | `https://vyos.net/get/stream/` | page with the latest Stream ISO link |
| `image_name` | `VyOS Router` | image name prefix, the version is appended |
| `builder.cpu`, `builder.ram` | `1`, `1` | builder VM size, RAM in GiB |
| `builder.prefix` | `vyos-builder-` | builder VM name prefix |
| `builder.disk` | `2` | builder VM disk, GiB - the size of the image |
| `builder.image` | `Empty disk` | image used for the builder VM disk, resized to `builder.disk` |
| `builder.password` | | password of user `vyos` in the image |
| `builder.wait` | `900` | seconds to wait for a boot or a power off |
| `iso.dir`, `iso.files` | | files added to the ISO: directory in `config/files/`, list of `name` + `path`, see [Files](#files) |

**OpenNebula**

- the builder platform in the inventory: `platform=platforms.<site>.<platform>`;
- an image named `builder.image` of `config/data/vyos.yaml` on the zone FE;
- a virtual network for the builder VM with a free address;
- that address must be reachable by ssh from the workstation;
- ssh key pair `${SSH_KEYF}` / `${SSH_KEYF}.pub` on the workstation - the
  public key is put into the builder VM context and used in phase 2.

## Files

All files live in `config/files/vyos/`. `iso_get_vyos` adds them to the ISO under
`/claugine/`; in the live session the ISO is mounted at
`/usr/lib/live/mount/medium`.

| File | Runs | Purpose |
|---|---|---|
| `grub.cfg` | - | boot menu of the installed system: one entry, `net.ifnames=0`; `${VYOS_VER}` is replaced by the installed version |
| `vyos-image-prepare` | manually, live session | writes `grub.cfg` and the scripts below to the installed disk, powers off |
| `vyos-postconfig-bootup.script` | every boot (VyOS standard hook) | mounts the context CD, runs the first run script once, runs context scripts, creates the flag `/opt/vyatta/etc/init.flag`, reboots after the first run |
| `vyos-first-run.script` | first boot only | applies the context: interfaces, routes, host name, user, password, ssh key, time zone; renamed to `*.disabled` afterwards |
| `vyos-image-finalize` | by `vyos_image_finalize` over ssh | resets the builder configuration, re-enables the first run, removes host keys, machine-id and history, powers off |

On the installed system the scripts are in `/config/scripts/`
(`/opt/vyatta/etc/config/scripts/`).

## Phase 1: `vyos_image_build`

```bash
source bin/claugine
vyos_image_build platform=platforms.dc1.mgmt cluster=default vnet=core_dc1 addr=10.71.101.223 gw=10.71.101.254
```

| Parameter | Meaning |
|---|---|
| `platform=NAME` | platform where the builder VM runs, `platforms.<site>.<platform>` |
| `cluster=ID\|NAME` | cluster of the builder VM; `builder.image` of `config/data/vyos.yaml`, the ISO datastore and `vnet` must belong to it |
| `vnet=STRING` | builder VM network, name or id |
| `addr=IP` | builder VM address, reachable by ssh from the workstation |
| `gw=IP`, `dns=IP` | optional |
| `url=URL` | optional: ISO URL, default - the latest from `url` of `config/data/vyos.yaml` |
| `empty_image=STRING` | optional: default `builder.image` of `config/data/vyos.yaml` |
| `ds=ID` | optional: default - the default IMAGE datastore of the zone: the lowest id, the built-in `default` (id 1) only if it is the only one |

What it does:

1. `iso_get_vyos` finds the latest Stream ISO, downloads it to the local repository
   (cached, partial downloads resumed) and builds
   `vyos-<version>-generic-amd64-autoinstall.iso` with the files from
   `config/files/vyos/` in `/claugine/`. The ISO stays bootable.
2. Stops if the builder VM `vyos-builder-<version>` already exists.
3. Uploads the ISO as CDROM image `VyOS Router <version> ISO` (prefix `sd`).
   An existing image with this name is handled as described in
   [Existing images](#existing-images-with-the-same-name).
4. Creates the builder VM `vyos-builder-<version>` in `cluster`: 1 CPU, 1 GB RAM,
   disk 0 - `builder.image` resized to `builder.disk` GiB (`config/data/vyos.yaml`), disk 1 -
   the ISO, boot order ISO first, NIC with `addr`, user `vyos` with the
   default password and your public key in the context, no hourly autostart.
5. Prints the manual steps.

## Manual steps on the console

Open the VM console in FireEdge (VNC):

1. Log in as `vyos` / `vyos` (live session defaults).
2. Run `install image` and accept the defaults; answer **no** to the reboot
   question.
3. Run
   ```
   sudo bash /usr/lib/live/mount/medium/claugine/vyos-image-prepare
   ```
   It finds the installed system (partition labeled `persistence`), saves the
   original `grub.cfg` as `grub.cfg.original`, writes the claugine `grub.cfg`,
   copies the scripts to `/config/scripts/` and powers the VM off after 10
   seconds.

## Phase 2: `vyos_image_finalize`

Run it when the builder VM is in state POWEROFF:

```bash
vyos_image_finalize platform=platforms.dc1.mgmt vm=vyos-builder-2026.03
```

| Parameter | Meaning |
|---|---|
| `platform=NAME` | platform of the builder VM |
| `vm=STRING` | builder VM name or id |
| `ip=IP` | optional: builder VM address, default - the first NIC |
| `name=STRING` | optional: image name, default `VyOS Router <version>` |
| `ds=ID` | optional: default - the default IMAGE datastore of the platform |
| `limit=N` | optional: seconds for a boot or a power off, default `builder.wait` of `config/data/vyos.yaml` |
| `confirm=yes` | skip the confirmation |

What it does:

1. Detaches the ISO and waits until it is gone. From now on the only CD-ROM
   is the context CD, which `vyos-postconfig-bootup.script` mounts as `/dev/sr0`.
2. Boots the installed system. On this first boot the first run script applies
   the context (address, your ssh key), creates the flag and reboots.
3. Waits until ssh as `vyos` works **and** the flag is older than the current
   boot - that is, the first run reboot is over.
4. Starts `vyos-image-finalize` on the VM, detached from the ssh session
   (the session drops when the interfaces are removed). The script:
   - deletes `interfaces ethernet`, `protocols static`, `service` and the
     ssh keys of user `vyos`;
   - sets the default password, host name `vyos-router`, ssh on port 22,
     `ctrl-alt-delete ignore`, `reboot-on-panic`;
   - restores `vyos-first-run.script` and removes the flag, so the next VM
     created from the image runs its first run;
   - removes ssh host keys, the config archive, `machine-id` and shell
     history;
   - powers off. Log while it runs: `/tmp/vyos-image-finalize.log`.
5. Saves the VM disk as a temporary image and waits until it is READY.
6. Downloads it to `<directory>/vyos-<version>.qcow2`.
7. Publishes it with `image_publish` to every zone in `zones` as OS image
   `VyOS Router <version>`: by URL from `repos.zakroma.url_base/vyos` of each zone's site, by rsync otherwise.
8. Deletes the temporary image.

## VMs created from the image

On the first boot `vyos-first-run.script` reads the OpenNebula context:

| Context variable | Default | Effect |
|---|---|---|
| `ETHn_MAC` | | the interface is configured only if present |
| `ETHn_IP`, `ETHn_MASK` | mask `255.255.255.0` | interface address |
| `ETHn_GATEWAY` | | default route via this gateway |
| `ETHn_ROUTES` | | `NET via GW, NET via GW` - static routes |
| `ETHn_METRIC` | `100` | distance of the routes of this interface |
| `ETHn_MTU` | `1500` | MTU; IPv6 link-local and forwarding disabled |
| `HOSTNAME` | `vyos-router` | host name |
| `USERNAME` | `vyos` | another name creates that user and disables `vyos` |
| `CRYPTED_PASSWORD_BASE64` | default password | base64 of the password hash |
| `SSH_PUBLIC_KEY` | | key of the user; ssh password login is disabled when set |
| `SSH_PORT` | `22` | ssh port |
| `TIMEZONE` | `Asia/Almaty` | time zone |

`vyos-postconfig-bootup.script` also understands:

| Context variable | Effect |
|---|---|
| `FIRSTRUN_SCRIPT_DISABLE=yes` | skip the first run, keep the image defaults |
| `CONTEXT_SCRIPTS_DISABLE=yes` | never run `*.sh` from the context CD |
| `CONTEXT_SCRIPTS_ENFORCE=yes` | run `*.sh` from the context CD on every boot, not only the first |

After the first run the VM reboots once; the flag file
`/opt/vyatta/etc/init.flag` holds the initialization date.

> The default password is published with the scripts. Always set
> `SSH_PUBLIC_KEY` or `CRYPTED_PASSWORD_BASE64` in the VM template.

`vm_create` fills these variables from its parameters (`vnetN`, `addrN`,
`gwN`, `routesN`, `metricN`, `mtuN`, `hostname`, `user`, `pswd`, `key`, `tz`).

## Publishing to another platform

To put the same image into other platforms, publish the local copy - no
second builder VM is needed:

```bash
for p in platforms.dc2.payload1 platforms.dc1.payload2
do
  image_publish platform=${p} \
    file=/srv/claugine/repo/zakroma/vyos/vyos-2026.03.qcow2 name="VyOS Router 2026.03" \
    repo=vyos type=OS format=qcow2                  # the FE downloads from its site's repository
done
```

`vyos_image_finalize` publishes to the builder's platform only and prints
this command for the others.

## Existing images with the same name

`image_upload` frees the name before it creates a new image (`image_archive`):

- the image is used by no VM - it is deleted, and the upload waits until it
  is gone;
- the image is used by VMs - it is renamed to `NAME (YYYY-MM-DD)`, the date of
  its registration (`NAME (YYYY-MM-DD HH-MM)` if that name is taken too).

VM templates that refer to the image by ID lose it when it is deleted;
templates that refer to it by name get the new image.

## Cleanup

`vyos_image_finalize` leaves for you to decide:

- the builder VM `vyos-builder-<version>`, powered off - terminate it;
- the ISO image `VyOS Router <version> ISO` on the zone FE;
- in the local repository: the original ISO, the customized ISO and
  `vyos-<version>.qcow2`.

## Troubleshooting

Both commands return `1` on any error; the message names the step. The
messages and what to do:

| Step | Message | What to do |
|---|---|---|
| build | platform not in the inventory, FE not reachable | check the inventory and the VIP |
| build | `cluster`, `vnet` or `addr` not set, `${SSH_KEYF}.pub` missing | pass them; create the key |
| build | ISO not found, not downloaded or not customized | check the download site and `config/data/vyos.yaml` |
| build | builder VM already exists | terminate it, or finish it with phase 2 |
| build | ISO upload failed, VM creation failed | cluster, image or network not found or not unique, address leased |
| finalize | builder VM not found | check `vm=` |
| finalize | VM not powered off, or no IP | finish the manual steps first |
| finalize | ISO not detached, VM did not start | check the VM in OpenNebula |
| finalize | no ssh after the first run | check the VM console: context, address, key |
| finalize | `vyos-image-finalize` did not power the VM off | see `/tmp/vyos-image-finalize.log` on the VM |
| finalize | disk not saved, image download failed | check the datastore and the local repository |
| finalize | publishing failed | the image is in the local repository - rerun `image_publish` |

Test scripts: `tests/vyos-image-build.sh` and
`tests/vyos-image-finalize.sh vyos-builder-<version>`, data in
`tests/claugine_TEST.sh`.
