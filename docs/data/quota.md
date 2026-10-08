# Tenant quotas

[Documentation](../README.md) › [data](README.md) › tenant quotas

This document defines how tenant quotas are described and how they are stored
as attributes of the tenant's OpenNebula group.

The datastore sizes applied by `quota_ds_set` come from the temporary
`patch.yaml` of the payload platform (`ds_quotas`), see
[inventory](inventory.md#patchyaml).

## Conventions

- A **tenant** is an OpenNebula group named `tenant-<name>`.
- Each tenant has exactly one tenant admin user, named `admin-<name>`.
- The tenant's quota is stored in the group template as a set of attributes.
- The master list of all tenant quotas is kept in a CSV file,
  or exported to CSV from a database.

## Quota records

A tenant has one or more records: **one per cluster on each site** where it has
resources. The combination of `tenant` + `site` + `cluster` is unique across the
whole table.

| Column | Key | Level | Description |
|---|---|---|---|
| `tenant` | ✔ | – | tenant name (`<name>` from `tenant-<name>`) |
| `site` | ✔ | – | site code where the resources are located |
| `cluster` | ✔ | – | cluster name where the resources are located |
| `status` | | cluster | status of the record, see [Status](#status) |
| `cpu` | | cluster | CPU cores allocated |
| `ram_gb` | | cluster | RAM, GB |
| `gpu` | | cluster | GPUs allocated |
| `disk_gb` | | cluster | VM disk space, GB |
| `images_gb` | | site (sum) | image datastore space, GB |
| `files_gb` | | site (sum) | file datastore space, GB |
| `backups_gb` | | site (sum) | backup datastore space, GB |
| `connection_type` | | site (one record) | `inet`, `corpnet` or `none`, see [Connection types](#connection-types) |
| `igw_type` | | site (one record) | internet gateway type (`inet` only) |
| `inet_mbps` | | site (one record) | internet bandwidth, Mbps (`inet` only) |
| `inet_main_prefix` | | site (one record) | length of the main public prefix (`inet` only) |
| `inet_public_prefixes` | | site (one record) | lengths of additional public prefixes, space-separated (`inet` only) |
| `l2_vlans` | | site (one record) | VLANs used to bring in external L2 channels, space-separated |
| `l2_vnets` | | site (one record) | tenant VNets the external L2 channels connect to, space-separated |
| `corpnet_prefixes` | | site (one record) | corporate network prefixes (`corpnet` only) |
| `date_created` | | cluster | first day the resources are available to the tenant |
| `date_review` | | cluster | date of the next status review; required when `status` is `paused` or `suspended` |

**Level** shows where the value is stored in the group:

- **cluster** - stored per record, in `QUOTA_<SITE>_<CLUSTER>`;
- **site (sum)** - set per record; the values of all records on the same site are
  added up and stored once, in `QUOTA_<SITE>`;
- **site (one record)** - connectivity. Only **one record per site** may contain
  connectivity fields; they are left empty in the tenant's other records on that
  site. Stored once, in `QUOTA_<SITE>`.

### Status

| Value | Account | VMs | Connectivity | Resources |
|---|---|---|---|---|
| `ok` | active | running | working | kept |
| `paused` | blocked | running | blocked | kept |
| `suspended` | blocked | powered off | blocked | kept |
| `archived` | deleted | deleted | – | one full backup of every VM is kept |

The status is set per record, so a tenant can be paused on one cluster and active
on another. When the status is `paused` or `suspended`, `date_review` must be set.

**Status lifecycle**

```
ok ──► paused ──(2 weeks)──► suspended ──(1 month)──► archived
▲                   │                        │
└───────────────────┴────────────────────────┘
```

| From | Maximum time | Back to `ok` | When the time runs out |
|---|---|---|---|
| `paused` | 2 weeks | any time during the 2 weeks | `ok` or `suspended` |
| `suspended` | 1 month | any time during the month | `ok` or `archived` |

`date_review` is the date the current status ends: the day the record was paused
plus 2 weeks, or the day it was suspended plus 1 month. Until that date the record
can be returned to `ok` at any time. On that date it must move to one of the next
statuses. `archived` is final: the resources are deleted and cannot return to `ok`.

### Connection types

| Field | `inet` | `corpnet` | `none` |
|---|:---:|:---:|:---:|
| `igw_type` | ✔ | | |
| `inet_mbps` | ✔ | | |
| `inet_main_prefix` | ✔ | | |
| `inet_public_prefixes` | ✔ | | |
| `l2_vlans`, `l2_vnets` | ✔ | | ✔ |
| `corpnet_prefixes` | | ✔ | |

- **`inet`** - connection to the provider's internet exchange.
- **`corpnet`** - routed connection to the corporate network. No internet gateway is
  allowed, internet access is controlled by the corporate network, so the
  `igw_*`, `inet_*` and `l2_*` fields do not apply.
- **`none`** - no connectivity is managed by the provider. Only external L2
  channels (`l2_*`) can be attached.

**Internet gateway (`igw_type`)**

| Value | Created by | Managed by |
|---|---|---|
| `vyos` | provider | tenant (VyOS router) |
| `onevr` | provider | tenant (OpenNebula virtual router) |
| `custom` | tenant | tenant |

**Main public prefix (`inet_main_prefix`)**

- cannot be changed after the tenant is provisioned;
- the first address is always assigned to the EGW;
- the second address is always assigned to the IGW.

Additional public prefixes (`inet_public_prefixes`) are routed through the IGW.

**External L2 channels (`l2_*`)** connect an external network to tenant VNets at
layer 2. The provider does not manage addressing in these networks.

## Example

Example records, one row each, with the same columns as the CSV file. Empty cells mean
the field is not used. `vasilyok` has two clusters on `dc1`: only the `ca1` record
carries the connectivity for that site.

| `tenant` | `site` | `cluster` | `status` | `cpu` | `ram_gb` | `gpu` | `disk_gb` | `images_gb` | `files_gb` | `backups_gb` | `connection_type` | `igw_type` | `inet_mbps` | `inet_main_prefix` | `inet_public_prefixes` | `l2_vlans` | `l2_vnets` | `corpnet_prefixes` | `date_created` | `date_review` |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|---:|---:|---|---|---|---|---|---|
| vasilyok | dc1 | ca1 | ok | 10 | 50 |  | 500 | 20 | 1 | 1000 | inet | vyos | 10 | 31 | 29 28 | 205 | 55 |  | 2026-01-15 |  |
| vasilyok | dc1 | gi1 | ok | 20 | 100 | 2 | 1000 | 30 | 1 | 500 |  |  |  |  |  |  |  |  | 2026-02-01 |  |
| vasilyok | dc2 | gi1 | paused | 50 | 500 | 4 | 2500 | 200 | 1 | 5000 | inet | custom | 100 | 29 |  |  |  |  | 2026-03-01 | 2026-10-09 |
| romashka | dc1 | ci1 | ok | 10 | 20 |  | 100 | 100 | 1 | 500 | corpnet |  |  |  |  |  |  |  | 2026-02-10 |  |

## Storage in the OpenNebula group

Each record is split into two vector attributes in the group template:

- **`QUOTA_<SITE>`** - one per site: the summed datastore space (`IMAGES_GB`,
  `FILES_GB`, `BACKUPS_GB`) of all the tenant's records on that site, plus the
  site's connectivity.
- **`QUOTA_<SITE>_<CLUSTER>`** - one per record: `CPU`, `RAM_GB`, `GPU`,
  `DISK_GB`, `STATUS`, `DATE_CREATED` and `DATE_REVIEW` (required when `STATUS`
  is `paused` or `suspended`).

Rules:

- attribute and field names are upper case;
- all values are strings;
- fields that do not apply, or are empty, are omitted.

### `tenant-vasilyok`

`QUOTA_DC1` holds the sum of the `ca1` and `gi1` records (20 + 30 images,
1 + 1 files, 1000 + 500 backups) and the connectivity from the `ca1` record.

```
QUOTA_DC1 = [
  IMAGES_GB = "50",
  FILES_GB = "2",
  BACKUPS_GB = "1500",
  CONNECTION_TYPE = "inet",
  IGW_TYPE = "vyos",
  INET_MBPS = "10",
  INET_MAIN_PREFIX = "31",
  INET_PUBLIC_PREFIXES = "29 28",
  L2_VLANS = "205",
  L2_VNETS = "55"
]
QUOTA_DC1_CA1 = [
  CPU = "10",
  RAM_GB = "50",
  DISK_GB = "500",
  STATUS = "ok",
  DATE_CREATED = "2026-01-15"
]
QUOTA_DC1_GI1 = [
  CPU = "20",
  RAM_GB = "100",
  GPU = "2",
  DISK_GB = "1000",
  STATUS = "ok",
  DATE_CREATED = "2026-02-01"
]
QUOTA_DC2 = [
  IMAGES_GB = "200",
  FILES_GB = "1",
  BACKUPS_GB = "5000",
  CONNECTION_TYPE = "inet",
  IGW_TYPE = "custom",
  INET_MBPS = "100",
  INET_MAIN_PREFIX = "29"
]
QUOTA_DC2_GI1 = [
  CPU = "50",
  RAM_GB = "500",
  GPU = "4",
  DISK_GB = "2500",
  STATUS = "paused",
  DATE_CREATED = "2026-03-01",
  DATE_REVIEW = "2026-10-09"
]
```

### `tenant-romashka`

```
QUOTA_DC1 = [
  IMAGES_GB = "100",
  FILES_GB = "1",
  BACKUPS_GB = "500",
  CONNECTION_TYPE = "corpnet"
]
QUOTA_DC1_CI1 = [
  CPU = "10",
  RAM_GB = "20",
  DISK_GB = "100",
  STATUS = "ok",
  DATE_CREATED = "2026-02-10"
]
```
