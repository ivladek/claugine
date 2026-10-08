# Tests

[Documentation](../README.md) › [usage](README.md) › tests

`tests/` holds two kinds of scripts.

**Checks** - read-only or dry-run, safe on any platform. Each check runs one
command in a subshell and compares its return code; the script ends with a
summary line `tests=N passed=N failed=N` and returns `1` if any check failed.

| Script | Checks | Test data |
|---|---|---|
| `data-load.sh` | `data_load_provider`; `RUNTIME` of `test_platform` (zone id, name, state, leader IP); `fe.secondaries` of the primary and `fe.primary` of the secondary; `_data_consistancy_check`; the `inv_*` getters, a missing path gives an empty value | `test_inv`, `test_secrets`, `test_runtime`, `test_platform`, `test_secondary` |
| `platform-name.sh` | `data_runtime_refresh`: no `platform=` - `0`; a full name - `0`; empty, list, short or unknown name - `1`; a command without `platform=` - `1` | `test_platform` |
| `read-only.sh` | `fe_cfg_ver_get`, `fe_configs_backups_list`, `acl_role_rights_get`, `quota_vcpu_conf_show`, `evpn_vtep_*_get`; with `test_tenant`: `quota_tenant_get`, `acl_tenant_get` on the primary and from the secondary | `test_platform`, `test_secondary`, `test_tenant` |
| `acl-tenant-dry.sh` | `acl_tenant_set dry=yes`: from the secondary it switches to the primary and builds one ZONE ACL per federation zone at most; on `test_platform_local` - one zone at most. Skipped if `test_tenant` is empty | `test_secondary`, `test_platform_local`, `test_tenant` |

**Scenarios** - run real commands and change the platform: run them on a test zone.

| Script | Scenario | Test data |
|---|---|---|
| `get-iso.sh` | latest Ubuntu Server and VyOS Stream ISOs, customized copies in the local repositories | none - `repos.zakroma` of the site of the loaded platform |
| `create-vm.sh` | Ubuntu VM in a cluster: one disk, one NIC, user with password and ssh key | `test_platform`, `test_vm_*` |
| `vyos-image-build.sh` | VyOS image, phase 1: ISO uploaded, builder VM created; then the manual steps it prints | `test_platform`, `test_vyos_*` |
| `vyos-image-finalize.sh VM` | VyOS image, phase 2: image created and published to the builder's platform | `test_platform` |

## Test data

Test values are kept apart from the scripts:

- `tests/claugine_TEST_SAMPLE.sh` - published sample with placeholder values;
- `tests/claugine_TEST.sh` - your values, ignored by git.

```bash
cp tests/claugine_TEST_SAMPLE.sh tests/claugine_TEST.sh
nano tests/claugine_TEST.sh
```

Every script sources `claugine_TEST.sh`; the line that sources the sample
instead stays commented in each script.

Common values:

| Variable | Meaning |
|---|---|
| `test_inv`, `test_secrets`, `test_runtime` | the data set loaded with `data_load_provider` |
| `test_platform` | platform of the test objects; for the federation checks - the primary of a federation |
| `test_secondary` | a secondary of the federation of `test_platform` (listed in its `fe.secondaries`) |
| `test_platform_local` | a platform without a federation |
| `test_tenant` | an existing tenant, used only by read-only and dry-run checks; empty - those checks are skipped |

The sample values match `data/SAMPLE`: `platforms.dc1.payload1` is the primary,
`platforms.dc2.payload1` its secondary, `platforms.dc1.payload2` a local platform,
`romashka` a tenant.

## Running

A script loads claugine if it is not loaded yet, so it can be run or
sourced. If no user data is loaded, it calls `data_load_provider` with `test_inv`,
`test_secrets` and `test_runtime` of the test data:

```bash
bash tests/data-load.sh                          # checks: summary line, exit code 0|1
bash tests/get-iso.sh                            # run in a new shell
source tests/create-vm.sh                        # or in the current shell, claugine stays loaded
bash tests/vyos-image-finalize.sh vyos-builder-2026.03
```
