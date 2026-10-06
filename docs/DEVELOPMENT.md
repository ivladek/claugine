# Development conventions

Rules for existing and new code in `lib/`, `internal/`, `data/` and `tests/`.

- [Installation and zones](#installation-and-zones)
- [OpenNebula objects](#opennebula-objects)
- [Commands](#commands)
- [Output and errors](#output-and-errors)
- [Variables and data](#variables-and-data)
- [Files](#files)
- [Tests](#tests)

## Installation and zones

**Installation** - one OpenNebula installation: a single zone or a federation
of zones. **Context** - the installation currently loaded into the shell.

Why: a target used to be addressed in three ways - `zone_id`, `zones` and
FE IPs (`fe=`, `fes=`) - with the zone data checked in a block copied into
every function, unknown zone ids running `ssh ""`, and the master zone
hard-coded as `0`. Since 02.00.00 there is one model: one installation per
context, zone ids inside it, shared helpers for every check.

- `fe_data_refresh fe=IP` loads an installation into the associative array
  named by `zone_data` (default `claugine_zone`) and makes it the current
  context. `data=VAR_NAME` loads it into another variable, created if needed.
  This is the only command that takes a front-end IP as input.
- Besides the zones, the context keeps `[fe]` - the FE it is loaded and
  refreshed from, `[master]` - the master zone id, `[installation]` - its
  name, `[timestamp]` - the last refresh.
- Every other command works **only inside the current context**.
- To work on another installation, switch the context first:
  ```bash
  fe_data_refresh data=one_dc1 fe=10.71.101.30   # load dc1, context = one_dc1
  fe_data_refresh data=one_dc2 fe=10.72.101.30   # load dc2, context = one_dc2
  zone_data=one_dc1                              # back to dc1 without reloading
  ```
- A target inside the installation is always addressed by zone id:

  | Parameter | Use | Default |
  |---|---|---|
  | `zone_id=N` | the command works on one zone: VM, vnet, host, image | none - required |
  | `zones=LIST\|ALL` | the command works on several zones: maintenance, reports, replication | `ALL` |
  | - | federation-wide objects: users, groups, ACLs, quotas | the master zone |

  A command never takes `fe=`/`fes=`. It takes `zone_id` and `zones`
  together only when they mean different things, like
  `vyos_image_finalize`: the zone of the builder VM and the zones to publish to.
- Commands check the context and zones only through the helpers in
  `lib/claugine-cli_one-helpers.sh`; on error they print the message and the
  command returns `1`:

  | Helper | Does |
  |---|---|
  | `_zone_init` | context loaded? refreshes it when older than `DATA_REFRESH_LIMIT` |
  | `_zone_check zone_id=N` | error if the zone is not in the context or has no VIP |
  | `_zones_resolve zones=LIST\|ALL` | expands `ALL`, checks every id, prints the list: `zones=$(_zones_resolve zones="${zones}") \|\| return 1` |
  | `_zone_vip N` | prints the VIP of zone N |
  | `_zone_label N` | prints `installation=... zone_id=N zone_name=... zone_vip=...` for log lines |
  | `_zone_installation` | prints `<context>(<fe>)` - used by the confirmation prompt |
  | `_zone_caller` | name of the command that called a helper, for `!!! TRACE !!!` |

  After the check, `${_zone[${zone_id},...]}` may be read directly.
- The master zone is `${_zone[master]}` - never a literal `0` in commands.
- Platform data that differs per front-end is keyed by **zone VIP**
  (`IMAGES_DS_LIST[vip]`, `SHARED_ID[vip]`, `URL_*_REPO[vip]`, ...). Zone ids
  repeat in every installation (each has a zone 0), VIPs are unique, so one
  data file serves all installations. Commands read it through the VIP of the
  zone: `${IMAGES_DS_LIST[$(_zone_vip ${zone_id})]}`.

- Per-zone objects (images, vnets, VMs, hosts, clusters) are looked up in the
  zone they are used in; federation-wide objects (users, groups, ACLs) in the
  master zone. Group quotas and usage are per zone, so `quota_*` commands
  take `zones=`.

Skeleton of a command:

```bash
claugine_cli_commands+=( "\
  area_do_something          # one line: what it does
    zone_id=N                #   target zone
    name=STRING              #   ...
    confirm=yes              #   to suppress interactive confirmation"
)
function area_do_something() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n _zone=${zone_data}
  local name="${name:-}"
  local fe

  _zone_init || return 1
  _zone_check zone_id="${zone_id:-}" || return 1
  fe=$(_zone_vip ${zone_id})

  echo "$(_zone_label ${zone_id}) name=${name} action=do_something"
  stop_without_confirmation confirm=${confirm:-no} && return 1
  ...
  return 0
}  # area_do_something
```

## OpenNebula objects

Commands accept objects by name or id (`image1="Ubuntu 26.04"` or
`image1=42`). To turn either into an id, use `_one_object_id` from
`lib/claugine-cli_one-helpers.sh` - never a hand-written `one<object> list -f`:

```bash
cluster_id=$(_one_object_id zone_id="${zone_id}" object=cluster name="${cluster}") || return 3
```

| Parameter | Meaning |
|---|---|
| `zone_id=N` | zone of the current installation, checked |
| `object=TYPE` | one of `ONE_OBJECT_TYPES` (`internal/claugine-cli_data-one.sh`); the CLI command is `one<TYPE>`: `cluster`, `datastore`, `group`, `host`, `image`, `marketapp`, `secgroup`, `template`, `user`, `vm`, `vnet`, `vntemplate`, `zone` |
| `name=STRING` | name, or id if it is a number |

It prints the id; errors go to stderr. Return: `1` - wrong parameters or
zone, `2` - not found, `3` - the name is not unique (OpenNebula allows equal
names for different owners) - the message lists the ids to use instead.

Commands that only pass the object on to `one<object> show NAME|ID`, which
accepts both, do not need it. Tenant lookups (`acl_*`, `quota_*`) keep their
own search: they accept partial names and return several groups.

## Commands

- Name: `<area>_<object>_<verb>` or `<area>_<verb>`, area = module name
  (`fe_`, `host_`, `vm_`, `vnet_`, `image_`, `iso_`, `vyos_`, ...).
- Parameters are `key=value` only:
  `local arg; for arg in "$@"; do local "${arg}"; done`, then every parameter
  gets a default: `local name="${name:-}"`. All code runs under `set -u`.
- Help: register with `claugine_cli_commands+=( "..." )` right before the
  function. Notation: `yes|NO` - the upper-case value is the default,
  `N(300)` - the default number.
- Changes ask for confirmation with `stop_without_confirmation`;
  `confirm=yes` skips it. Commands that change many objects offer `dry=YES|no`.
- Internal functions start with `_`, have no help entry and are not called by
  hand (`_vyos_vm_wait`).
- Return: `0` success, `1` - context or zone error (and declined
  confirmation), `2..N` - one code per further failure point, listed in the
  topic document; never `exit` in a module.
- A result that is more than a status goes into a global associative array
  named after the module (`iso_info`), reset at the start of the call.
  Commands do not print results meant for `$(...)` capture together with logs.

## Output and errors

- Progress: one line per action, `key=value` pairs, starting with the zone
  label and ending with `action=...` / `status=...`.
- Error, then return:
  ```bash
  echo
  echo "!!! ERROR !!! what is wrong, with values"
  echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
  return N
  ```
- Never print passwords or keys.

## Variables and data

| Kind | Where | Declaration |
|---|---|---|
| built-in constants | `internal/claugine-cli_data-<subsystem>.sh` | `declare -gr NAME`, `declare -gra`, `declare -grA` (string keys need `-A`) |
| platform data (yours) | `data/claugine-cli_<name>.sh`, sample `data/claugine-cli_SAMPLE.sh` | same |
| test data (yours) | `tests/claugine-cli_TEST.sh`, sample `tests/claugine-cli_TEST_SAMPLE.sh` | plain `name=value`, `test_` prefix |
| function locals | | `local`; namerefs start with `_`: `local -n _zone=${zone_data}` |

- Real data never goes into published files: only `*SAMPLE*` files of
  `data/` and `tests/` are published (see `.gitignore`).
- New per-front-end data: an associative array keyed by VIP, added to the
  sample file with placeholder addresses.

## Files

| Path | Content |
|---|---|
| `bin/claugine-cli.sh` | loader: `internal/`, `data/` (except `*SAMPLE*`, `*TEST*`), `lib/`, tool check |
| `lib/claugine-cli_<area>.sh` | one module per area |
| `lib/claugine-cli_common.sh` | help, input checks (`is_ipv4`, `is_mac`), confirmation |
| `lib/claugine-cli_one-helpers.sh` | installation context, zones, `_one_object_id` |
| `internal/claugine-cli_data-<subsystem>.sh` | built-in data per subsystem |
| `templates/<area>/` | files copied to targets or into ISOs |
| `tests/` | test scripts, run or sourced |
| `docs/` | topic documents and this file |

## Tests

- One script per scenario in `tests/`, data only from `tests/claugine-cli_TEST.sh`.
- Each script loads the CLI if it is not loaded and works both run and sourced.
- The line to switch to the sample data stays commented in every script.
