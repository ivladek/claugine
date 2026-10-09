# Tool

How claugine is built: the loader, the modules, the helpers, and the
rules for existing and new code in `bin/`, `lib/`, `config/` and `tests/`.
The data the tool works with is described in [data](../data/README.md).

- [Design](#design)
- [Files](#files)
- [Platforms](#platforms)
- [OpenNebula objects](#opennebula-objects)
- [Commands](#commands)
- [Output and errors](#output-and-errors)
- [Variables](#variables)
- [Tests](#tests)

## Design

claugine is a set of Bash functions loaded into the admin's shell. It keeps
no daemon and no state on disk. The **inventory is the only source of
platform data**: every command names its target as an inventory platform,
`platform=platforms.<site>.<platform>`. There is no context and no current
platform.

Three steps:

1. `source bin/claugine` - **internal data only**: `_script_INIT` sets the
   directories (`DIR_SCRIPT`, `DIR_LIB`, `DIR_CONFIG` - `config/data`,
   `DIR_FILES` - `config/files`, `DIR_TEMPLATES` - `config/templates`), loads
   the modules, then `CONFIG` and `TEMPLATES`, checks the tools of
   `config.admin.tools`; `_script_MAIN` then prints the commands. A direct
   run (`bin/claugine [--help] [PATTERN ...]`) only prints help: it reads the
   modules for their `help_data`, nothing else; A module only defines
   functions and help texts and reads nothing while loading: a default from
   internal data is shown in the help as its key, `limit=N(<CONFIG.onefe.timeouts.image_wait>)`, the value: `inv_value var=CONFIG path=onefe.timeouts.image_wait`;
2. `data_load_provider inv=PATH secrets=PATH runtime=PATH` - **user data**: `INV`
   and `SECRETS`, `DIR_RUNTIME` - where collected runtime data may be saved.
   It collects the runtime data of every platform into `RUNTIME` and runs
   `_data_consistancy_check`; inconsistent data is not loaded - `INV`,
   `SECRETS` and `RUNTIME` are cleared. To switch to another data set, call
   it again;
3. commands - each starts with `data_runtime_refresh`: `INV` is loaded, the
   platform name is right, then works with the platform named by `platform=`.

The four documents are shell globals, **not exported**, see
[data](../data/README.md#source-data). The runtime data of every platform
is collected from its FE into `RUNTIME` by `data_load_provider`; commands
reuse it. Only the FE node states are read again, at the start of every
command that walks the FE nodes (`fe=yes`). How a YAML tree becomes a document:
[loading a tree](../data/README.md#loading-a-tree).

```
 config/data, config/templates ─► source bin/claugine ─► CONFIG, TEMPLATES ───────┐
 inventory, secrets             ─► data_load_provider     ─► INV, SECRETS            ├─► commands
                                   ├─ every platform     ─► RUNTIME (from its FE)    │
                                   └─ _data_consistancy_check: federations           │
 platform=platforms.<site>.<platform> ─► data_runtime_refresh [fe=yes] ─────────────┘
```

| Module | Area |
|---|---|
| `claugine_data.sh` | user data and runtime data: `data_load_provider`, `data_runtime_refresh`, `_data_load_var`, `_data_consistancy_check`, `_data_runtime_platform_init`, `_data_runtime_fe_nodes_refresh` |
| `claugine_inventory.sh` | getters of the loaded documents: `inv_value`, `inv_list`, `inv_list_field`, `inv_keys`, `inv_list_find` |
| `claugine_common.sh` | help, input checks (`_is_ipv4`, `_is_mac`), confirmation |
| `claugine_helpers.sh` | `_caller`, the log helpers `_log*`, `_one_object_id` |
| `claugine_platform.sh` | an earlier copy of the runtime functions (`platform_runtime_refresh`, `_platform_load`, `_platform_nodes`), not used |
| `claugine_fe.sh` | front-end: runtime refresh of a platform, backups, updates, services, FireEdge |
| `claugine_host.sh` | host maintenance |
| `claugine_vm.sh`, `claugine_vnet.sh` | service VMs, address ranges |
| `claugine_image.sh`, `claugine_iso.sh`, `claugine_vyos.sh` | images, ISOs, VyOS image |
| `claugine_evpn.sh` | EVPN/VXLAN settings |
| `claugine_acl.sh`, `claugine_quota.sh` | tenant ACLs, VM operation sets, quotas |
| `claugine_os.sh` | OS services |

## Files

| Path | Content |
|---|---|
| `bin/claugine` | loader of the internal data and modules (`source`), help (direct run), see [Design](#design) |
| `lib/claugine_<area>.sh` | one module per area |
| `config/data/<subsystem>.yaml` | internal data of the tool: `config.<subsystem>.*`, see [internal data](../data/config.md) |
| `config/templates/<name>.yaml` | YAML templates of config files |
| `config/files/<area>/` | files copied as is to targets or into ISOs |
| `data/` | a data set during development: `inventory/`, `secrets/`, `runtime/`; only `data/SAMPLE` is published |
| `tests/` | test scripts, run or sourced; test data `claugine_TEST.sh` |
| `docs/` | documentation |

## Platforms

**Platform** - an OpenNebula installation of one site in the inventory,
`platforms.<site>.<platform>`, one zone; management or payload, see
[platform](../platform/README.md#platforms). A federation is several
platforms: the primary and its secondaries. A secondary names its primary
(`fe.primary`), the primary lists its secondaries (`fe.secondaries`);
`data_load_provider` checks that every zone of a federation is one of
them.

Why: commands used to work inside a *context* - a platform loaded by
`fe_data_refresh fe=IP` - with zones addressed by id and the context
refreshed on a timer. Now the inventory names every target: no context,
no zone ids in parameters, no periodic refresh.

- Every command takes the target platform as its first parameter:

  | Parameter | Use |
  |---|---|
  | `platform=NAME` | exactly one platform, the full name: `platform=platforms.dc1.payload1` |

  There are no platform lists: to work on several platforms, call the
  command for each.
- **Federation-wide objects** - users, groups, ACLs - are changed on the
  primary platform: a command for them that gets a secondary platform
  switches to its `fe.primary` and says so, then works with the primary as
  its platform. `acl_tenant_set` builds the ACLs of every zone of the
  federation from the primary and its `fe.secondaries`. Per-zone objects (images, vnets, VMs,
  hosts, clusters, group quotas) are handled on the platform given.
- **RUNTIME** - the associative array of runtime data, see
  [data](../data/README.md#runtime). `data_load_provider` collects it for
  every platform: zone id, name and state, FE nodes, IMAGE/SYSTEM/FILE/BACKUP
  datastores, VNet template, shared group - only what the inventory does not
  define; the VIP, site, mode and primary are read from `INV`. It is not
  refreshed on a timer; `fe_data_refresh platform=NAME` collects it again
  for one platform, `data_load_provider` for all.
- **`data_runtime_refresh`** - the first call of every command:

  ```bash
  data_runtime_refresh [platform="${platform:-}"] [fe=yes] || return 1
  ```

  - it checks that `INV` is not empty - the only data check; without
    `platform=` it does only that;
  - `platform=` - exactly one full name `platforms.<site>.<platform>`: empty
    (the command got no `platform=`), a list or a short name is an error.
    The command does not declare `platform` itself, it passes
    `platform="${platform:-}"`. The runtime data of the platform is
    collected if it is missing - after `fe_data_refresh`, or for a platform
    whose FE was not reachable at load; a name not in the inventory is
    reported then;
  - `fe=yes` - reads the FE node roles and states again: commands that
    walk the FE nodes;
  - `quota=yes`, `usage=yes` - reserved: the functions behind them are not
    written yet.

  A command that needs fresh node states later - after a restart - calls it
  again with `fe=yes`.
- **The data is trusted.** `source` gives correct internal data, `data_load_provider`
  correct user data; a command checks only that `INV` is not empty, through
  `data_runtime_refresh`. No command checks inventory, config or runtime fields
  before it runs.
- Helpers in `lib/claugine_helpers.sh` and `lib/claugine_inventory.sh`;
  on error they print the message and the command returns `1`:

  | Helper | Does |
  |---|---|
  | `_one_object_id platform=P object=TYPE name=NAME` | id of an OpenNebula object, see [OpenNebula objects](#opennebula-objects) |
  | `_caller` | name of the command that called a helper, for `!!! TRACE !!!` |
  | `_log`, `_log_std`, `_log_fe`, `_log_error` | log lines and error blocks, see [output and errors](#output-and-errors) |

  `_data_runtime_platform_init` and `_data_runtime_fe_nodes_refresh`
  collect the runtime data for `data_runtime_refresh`; commands never call
  them directly. After it, read
  `${RUNTIME[${platform},id|vntemplate,id|...]}` directly, and the
  inventory for the rest: `vip=$(inv_value var=INV path=${platform}.fe.vip)`.
- What OpenNebula knows comes from `RUNTIME`; what it does not - site
  directories and URLs, time zone, tenant quotas - from the inventory of the
  platform. `config/` never holds a value of a provider or a site.

Skeleton of a command:

```bash
help_data[area_do_something]="\
  area_do_something          # one line: what it does
    platform=NAME            #   platforms.<site>.<platform>
    name=STRING              #   ...
    confirm=yes              #   to suppress interactive confirmation"
# return 0 - done
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh, or not confirmed
#        2 - ...
function area_do_something() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local name="${name:-}"
  local node vip

  data_runtime_refresh platform="${platform:-}" fe=yes || return 1   # fe=yes - only if the command walks the FE nodes
  vip=$(inv_value var=INV path=${platform}.fe.vip)
  _log_std zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" name="${name}" action="do_something"
  _stop_without_confirmation confirm=${confirm:-no} && return 1
  for node in $(inv_list var=CONFIG path=onefe.nodes_order.direct)
  do
    _log_fe action="do_something"
    $ssh ${RUNTIME[${platform},${node},ip]} "..."
  done  # node
  return 0
}  # area_do_something
```

Log lines are printed by the log helpers, see [output and errors](#output-and-errors).

`platform` is not declared in the command: `data_runtime_refresh` stops the
command when it is missing, so after it `${platform}` is always set. A
command without a platform starts with `data_runtime_refresh || return 1`.

## OpenNebula objects

Commands accept objects by name or id (`image1="Ubuntu 26.04"` or
`image1=42`). To turn either into an id, use `_one_object_id` from
`lib/claugine_helpers.sh` - never a hand-written `one<object> list -f`:

```bash
cluster_id=$(_one_object_id platform="${platform}" object=cluster name="${cluster}") || return 1
```

| Parameter | Meaning |
|---|---|
| `platform=NAME` | inventory platform, loaded |
| `object=TYPE` | one of `config.onefe.objects`; the CLI command is `one<TYPE>`: `cluster`, `datastore`, `group`, `host`, `image`, `marketapp`, `secgroup`, `template`, `user`, `vm`, `vnet`, `vntemplate`, `zone` |
| `name=STRING` | name, or id if it is a number |

It prints the id; errors go to stderr. Return: `1` - wrong parameters, not
found, or the name is not unique (OpenNebula allows equal names for
different owners) - the message lists the ids to use instead.

Commands that only pass the object on to `one<object> show NAME|ID`, which
accepts both, do not need it. Tenant lookups (`acl_*`, `quota_*`) keep their
own search: they accept partial names and return several groups.

## Commands

- Name: `<area>_<object>_<verb>` or `<area>_<verb>`, area = module name
  (`fe_`, `host_`, `vm_`, `vnet_`, `image_`, `iso_`, `vyos_`, ...).
- Parameters are `key=value` only - in commands and in internal `_` helpers
  alike, never positional (`$1`, `shift`). Every function that takes
  parameters starts with
  `local arg; for arg in "$@"; do local "${arg}"; done`, then every parameter
  gets a default: `local name="${name:-}"`. All code runs under `set -u`.
- Layout of every function, in this order:
  1. a command - its help entry `help_data[name]="..."`, keyed by the
     command name: what it does and its arguments; the whole entry is what
     `bin/claugine PATTERN` prints for a matching command; an internal function - a comment: what it does, then its
     arguments, one per line;
  2. `# return 0 - ...` and one line per further code, with what is printed;
  3. `function name() {`, the argument loop, then **all** `local`
     declarations - nothing is declared further down: `local vip` at the
     top, `vip=$(...)` where it is needed;
  4. every `for` loop ends with `done  # <variable>`, the function with
     `}  # name`.

  In a module the functions are in alphabetical order - the help prints in
  the same order - and the module starts with `# INDEX`: every function
  with its one-line description.

  Help notation: `yes|NO` - the upper-case value is the default, `N(300)` -
  the default number. A default from internal data is shown as its key:
  `limit=N(<CONFIG.onefe.timeouts.image_wait>)`.
- Changes ask for confirmation with `_stop_without_confirmation`;
  `confirm=yes` skips it. Commands that change many objects offer `dry=YES|no`.
- A function not meant to be called by hand starts with `_` and has no help
  entry (`_vyos_vm_wait`, `_is_ipv4`, `_stop_without_confirmation`,
  `_data_load_var`). Commands and the `inv_*` getters have help entries.
- Return: `0` success, `1` - any error (and declined confirmation); never
  `exit` in a module. A different code only where a caller checks it as an
  answer, not an error: `vnet_ar_ip_exists` and `vnet_ip_leased` - `4`,
  `vnet_ar_mac_get` - `3`. The `# return` block lists the reasons of `1`,
  one per line.
- A result that is more than a status goes into a global associative array
  named after the module (`iso_info`), reset at the start of the call.
  Commands do not print results meant for `$(...)` capture together with logs.

## Output and errors

Every line is printed by one of the log helpers of `lib/claugine_helpers.sh`
- no hand-written `echo` of a log line. They take `key=value` arguments and
print them as is, separated by spaces: `name=value`, no quotes, no checks.
`-n` as the first argument leaves the line open for progress dots.

| Helper | Prints | Uses of the caller |
|---|---|---|
| `_log KEY=VALUE ...` | the arguments | - |
| `_log_std KEY=VALUE ...` | `platform= zone_id= zone_vip=`, then the arguments | `${platform}`, `${vip}` |
| `_log_fe KEY=VALUE ...` | the `_log_std` header, `node_role= node_name= node_ip=`, then the arguments | `${platform}`, `${vip}`, `${node}` |
| `_log_error "message"` | a blank line, `!!! ERROR !!! message`, `!!! TRACE !!! <command>` | - |

```bash
_log_std zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" action="backup"
_log_fe \
  backup_from="${dir}" \
  backup_to="${backup_path}"
_log_std -n image="${image}" action="wait_ready" status="."     # dots follow: echo -n "."
platform=${other} vip=${other_vip} _log_std action="acl_tenant_set"     # a line of another platform
_log dir="${dir_backups}" action="cleanup"                     # no platform
_log_error "backups directory \"${dir_backups}\" can not be created"
return 1
```

```
platform=platforms.dc1.payload1 zone_id=0 zone_vip=10.71.101.30 zone_state=enabled action=backup
platform=platforms.dc1.payload1 zone_id=0 zone_vip=10.71.101.30 node_role=leader node_name=fe1 node_ip=10.71.101.31 backup_from=onedb-zone backup_to=...

!!! ERROR !!! backups directory "/srv/backups" can not be created
!!! TRACE !!! fe_backup
```

- `zone_state=` is given on the first line of a command, not in every line.
- A helper whose output is captured with `$(...)` sends its errors to stderr:
  `_log_error "..." >&2`.
- Never print passwords or keys.

## Variables

| Kind | Where | How |
|---|---|---|
| internal data | `config/data/<subsystem>.yaml` | read with `inv_value var=CONFIG path=<subsystem>.<key>` - never copied into shell constants |
| inventory | data set `inventory/`, `INV` | read with `inv_* var=INV path=<...>` |
| secrets | data set `secrets/`, `SECRETS` | read with `inv_value var=SECRETS path=<...>`, only through a reference read from the inventory |
| internal data, templates | `config/data`, `config/templates`; `CONFIG`, `TEMPLATES` | `inv_* var=CONFIG path=<...>`, `inv_value var=TEMPLATES path=<group>.<name>.content` |
| secrets | data set `secrets/` | never read by the CLI |
| runtime data | `RUNTIME`, filled on the first touch of a platform, cleared by `data_load_provider` | after `data_runtime_refresh`: `${RUNTIME[${platform},...]}` |
| results | global associative array named after the module (`iso_info`) | reset at the start of the call |
| test data | `tests/claugine_TEST.sh`, sample `tests/claugine_TEST_SAMPLE.sh` | plain `name=value`, `test_` prefix |
| function locals | | `local`; namerefs start with `_`: `local -n _list=${name}` |

- New internal data: a key in the right `config/data` file and a line in
  [internal data](../data/config.md). New inventory data: only a field the
  [inventory](../data/inventory.md) structure defines, the same field with
  fake values in `data/SAMPLE`.
- Data is read with the getter of its shape: a value - `inv_value`, a
  list - `inv_list` in a `for` loop or `mapfile -t`, a field of each object
  in a list - `inv_list_field`, one object of a list -
  `inv_list_find`, the keys of an object - `inv_keys`.
- Real data never goes into published files: only `data/SAMPLE` and
  `tests/*SAMPLE*` are published (see `.gitignore`).

## Tests

- One script per scenario in `tests/`, data only from `tests/claugine_TEST.sh`;
  user data - `data_load_provider` with `test_inv`, `test_secrets`, `test_runtime` of the test data, if nothing is loaded yet.
- Each script loads the CLI if it is not loaded and works both run and sourced.
- The line to switch to the sample data stays commented in every script.
