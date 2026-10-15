# Jobs

[Documentation](../README.md) › [usage](README.md) › jobs

`jobs/` holds production scripts built on claugine, run by cron on an admin
host. A job holds no values: the only file it loads is its argument
`DATA=<path>/jobs-data.sh`, which names the claugine loader and the data set the job loads
with `data_provider_load`.

| Script | What it does | Result |
|---|---|---|
| `fe-backup.sh` | `fe_backup full=yes` of every platform of the site of the admin host; the site - by the second octet of its `eth0` address, `SITES` | a Telegram message per platform; `1` if the site is unknown, a log file can not be created, `CLAUGINE` or `PROVIDER` not found, claugine or the data is not loaded, or any backup failed |
| `create-vyos-image.sh` | by hand, the job data as `datafile=<data>/<set>/jobs-data.sh`, one run: `vyos_image_create` on `VYOS_IMAGE_VM_PLATFORM` - while it waits up to 10 minutes, `install image` and `vyos-image-prepare` on the VM console; the image on the image VM platform and on the platforms of `VYOS_IMAGE_TARGET_PLATFORMS` (`all` or empty - every platform), the image VM and the ISO image deleted. See [VyOS image](../network/vyos-image.md) | all output on the terminal; `1` if any step failed |
| `fireedge-views.sh` | by hand or by cron, the job data as `datafile=<data>/<set>/jobs-data.sh`: `fe_fireedge_views_update confirm=yes` of every platform of `FIREEDGE_VIEWS_PLATFORMS`, one after another - the views of `config/files/<onefe.fireedge_views.dir>` to every FE node | all output on the terminal; `1` if `FIREEDGE_VIEWS_PLATFORMS`, `CLAUGINE` or `PROVIDER` is missing, claugine or the data is not loaded, a platform is not in the inventory, or its update failed |
| `quota-set.sh` | by hand, the job data as `datafile=<data>/<set>/jobs-data.sh`, `dry=YES\|no`, optional `tenants=LIST`, `platforms=LIST`: `quota_tenant_set confirm=yes` on every platform of `QUOTA_PLATFORMS` (or `platforms=`), one after another - the quotas are per zone; `dry=yes` (default) prints the quota templates, `dry=no` sets them | all output on the terminal; `1` if `QUOTA_PLATFORMS`, `CLAUGINE` or `PROVIDER` is missing, `dry` is wrong, claugine or the data is not loaded, a platform is not in the inventory, or a platform failed |
| `quota-list.sh` | by hand or by cron, read only, the job data as `datafile=<data>/<set>/jobs-data.sh`, optional `tenants=LIST`, `platforms=LIST`: `quota_tenants_report` of every platform of `QUOTA_PLATFORMS` - a row per quota record and per datastore, limit/real/usage, check `ok`, `differ`, `exceeded` | all output on the terminal; `1` as `quota-set.sh` |
| `quota-show.sh` | by hand, read only, the job data as `datafile=<data>/<set>/jobs-data.sh`, `tenant=NAME`, optional `platforms=LIST`: `quota_tenant_show` of the tenant on every platform of `QUOTA_PLATFORMS` | all output on the terminal; `1` if `tenant=` is missing, or as `quota-set.sh` |
| `acl-reset.sh` | by hand, the job data as `datafile=<data>/<set>/jobs-data.sh`, `dry=YES\|no`, optional `tenants=LIST`: `acl_tenant_set confirm=yes` on the primary platform of each federation of `ACL_PLATFORMS` - a secondary is replaced by its primary, every federation once; `dry=yes` (default) shows the current and the new ACLs, `dry=no` deletes and creates them | all output on the terminal; `1` if `ACL_PLATFORMS`, `CLAUGINE` or `PROVIDER` is missing, `dry` is wrong, claugine or the data is not loaded, a platform is not in the inventory, or a federation failed |

## Job data

- `data/SAMPLE/jobs-data.sh` - published sample with placeholder values;
- `data/<set>/jobs-data.sh` - your values, ignored by git.

| Variable | Meaning |
|---|---|
| `USER`, `HOME` | the user of the job - cron sets no `USER`; claugine connects as this user |
| `CLAUGINE` | the claugine loader, `<repo>/bin/claugine` |
| `PROVIDER` | the data set loaded by the job: its `inventory/`, `secrets/`, `runtime/` |
| `LOGS` | directory of the job logs `fe-backup-<site>.log` and `.err`, named in the reports |
| `SITES` | site of the admin host by the second octet of its `eth0` address: `[71]=dc1` |
| `VYOS_IMAGE_VM_PLATFORM`, `VYOS_IMAGE_VM_CLUSTER`, `VYOS_IMAGE_VM_VNET`, `VYOS_IMAGE_VM_ADDR`, `VYOS_IMAGE_VM_GW` | `create-vyos-image.sh`: the image VM - platform, cluster (`0`), VNet, address (`auto`), gateway |
| `VYOS_IMAGE_TARGET_PLATFORMS` | `create-vyos-image.sh`: the platforms of the image, separated by spaces, `all` or empty - every platform; passed as `target=` to `vyos_image_create` |
| `FIREEDGE_VIEWS_PLATFORMS` | `fireedge-views.sh`: the platforms of the FireEdge views, separated by spaces |
| `ACL_PLATFORMS` | `acl-reset.sh`: the platforms of the ACLs, separated by spaces; a secondary is replaced by its primary |
| `QUOTA_PLATFORMS` | `quota-set.sh`, `quota-list.sh`, `quota-show.sh`: the platforms of kind `shared`, separated by spaces - every zone of a federation, the quotas are per zone; `platforms=` of the job replaces it |
| `REPORT_URL`, `REPORT_CHAT` | `fe-backup.sh`: Telegram - the bot `sendMessage` url with its token, the chat id |

## Logs and reports

A job writes its own logs, new on every run - the cron line has no redirects:

| File | Content |
|---|---|
| `LOGS/fe-backup-<site>.log` | the report lines, the output of claugine, `data_provider_load` and `fe_backup` |
| `LOGS/fe-backup-<site>.err` | their errors; empty after a run without errors |

Each report is one line in the log and one Telegram message; the message ends
with the paths of both log files. Until the site is known, the log files are
not there yet: a report goes to the stdout of the job, which cron mails or drops.

| Report | When | Return |
|---|---|---|
| `site not detected: hostname= ip=` | the second octet of `eth0` is not in `SITES` | `1`, no backups |
| `can not create log file ...` | `LOGS` missing or not writable | `1`, no backups |
| `CLAUGINE and PROVIDER must be defined in ...` | the loader or the data set directory of the job data not found | `1`, no backups |
| `claugine not loaded` / `claugine data not loaded` | `source` of the loader or `data_provider_load` failed - see `.err` | `1`, no backups |
| `<platform> completed successfully` | `fe_backup` of the platform returned `0` | |
| `<platform> finished with errors` | `fe_backup` returned `1` - see `.err`; the other platforms still run | `1` at the end |

## Cron

One file per admin host, `/etc/cron.d/claugine`: the job data as the argument
`DATA=`, no redirects - the job writes its own logs. The site is in the log
names, so the logs of all hosts can share one directory.

```
# FE backup every day at 05:25
25 05 * * *  <user>  /bin/bash <jobs>/fe-backup.sh DATA=<data>/<set>/jobs-data.sh
```

A file in `/etc/cron.d/` is owned by root, mode `644`, and ends with a newline.
Run a job by hand as cron does - an empty environment:

```bash
env -i /bin/bash <jobs>/fe-backup.sh DATA=<data>/<set>/jobs-data.sh
```

Test without an FE or Telegram: `tests/fe-backup-job.sh` runs the job with
stand-ins, see [tests](tests.md).
