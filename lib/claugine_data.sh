#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



# INDEX
#   _data_consistancy_check         check that the loaded data is consistent
#   _data_load_var                  load one YAML tree into a variable: directories and file names become keys, all files are me...
#   _data_runtime_fe_nodes_refresh  load runtime data for platform FE nodes
#   _data_runtime_platform_init     load runtime data for platform
#   data_load_provider              load user data: the inventory and secrets; call again to switch to another data set
#   data_runtime_refresh            check and refresh runtime data



# check that the loaded data is consistent
#   1. federations: every zone of a primary FE is the primary itself or one of its fe.secondaries
# return 0 - consistent
#        1 - a zone of a federation is not a platform, or a primary FE not reachable
function _data_consistancy_check() {
  local platform vip member zone_id zones known missing

  # 1. federations: every zone of a primary FE is the primary itself or one of its fe.secondaries
  for platform in $(jq -r '
    .platforms // {} | to_entries[] | .key as $s | .value | to_entries[]
    | select(.value.fe.mode? == "primary") | "platforms.\($s).\(.key)"
  ' <<< "${INV}")
  do
    vip=$(inv_value var=INV path=${platform}.fe.vip)
    known="${RUNTIME[${platform},id]:-}"
    for member in $(inv_list var=INV path=${platform}.fe.secondaries)
    do
      member="${member%.fe}"
      known+="${known:+ }${RUNTIME[${member},id]:-}"
    done  # member
    zones=$(
      $ssh ${vip} "sudo -u oneadmin onezone list --no-header -l ID 2>/dev/null" |
      tr -d '[:blank:]' |
      grep -v '^$'
    )
    missing=""
    for zone_id in ${zones}
    do
      [[ " ${known} " == *" ${zone_id} "* ]] || missing+="${missing:+ }${zone_id}"
    done  # zone_id
    if [[ -z "${zones}" || -n "${missing}" ]]
    then
      _log_error "federation of ${platform}: zones [${missing:-the FE is not reachable}] are not platforms of the inventory - define each zone as a platform and list it in fe.secondaries"
      return 1
    fi
    _log \
      platform=${platform} \
      federation_zones="[${zones//$'\n'/ }]" \
      action=federation_check \
      status=ok
  done  # platform

  return 0
}  # _data_consistancy_check



# load one YAML tree into a variable: directories and file names become keys, all files are merged into one JSON document
#   var=NAME                # one of ${INV_VARS}: CONFIG, TEMPLATES, INV, SECRETS
#   dir=PATH
# return 0 - loaded, statistics printed
#        1 - wrong variable name, no directory or nothing loaded
function _data_load_var() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local var="${var:-}"
  local dir="${dir:-}"
  local file key yq_json
  dir="${dir%/}"

  if [[ " ${INV_VARS} " != *" ${var} "* || "${var}" == *" "* ]]
  then
    _log_error "\"${var}\": must be one of ${INV_VARS}"
    return 1
  fi

  [[ "${var}" == "INV" ]] && declare -gA RUNTIME=()

  if [[ -z "${dir}" || ! -d "${dir}" ]]
  then
    _log_error "${var}: directory \"${dir}\" not found"
    return 1
  fi

  # detect yq version
  if yq --version 2>&1 | grep -qi mikefarah
  then
    yq_json="yq -o=json"  # mikefarah (Go)
  else
    yq_json="yq"          # kislyuk (Python)
  fi

  declare -g ${var}="$(
    find "${dir}" -type f -name '*.yaml' -print0 |
    sort -z |
    while IFS= read -r -d '' file
    do
      key="${file#${dir}/}"
      key="${key%.yaml}"
      ${yq_json} '. // {}' "${file}" |
      jq -c --arg key "${key}" '. as $data | {} | setpath($key | split("/"); $data)'
    done |
    jq -s 'reduce .[] as $item ({}; . * $item)'
  )"

  if [[ -z "${!var}" || "${!var}" == "{}" ]]
  then
    _log_error "${var}: nothing loaded from ${dir}"
    return 1
  fi

  echo -n "var=${var} loaded: "
  printf '%s' "${!var}" | jq '
    [.. | type] |
    {
      total_values: length,
      objects: (map(select(. == "object")) | length),
      arrays: (map(select(. == "array")) | length),
      strings: (map(select(. == "string")) | length),
      numbers: (map(select(. == "number")) | length),
      booleans: (map(select(. == "boolean")) | length),
      nulls: (map(select(. == "null")) | length)
    }
  '

  return 0
}  # _data_load_var



# load runtime data for platform FE nodes:
#   leader|follower1|follower2,id|name|ip|state
#
#   platform=NAME
# return 0 - always
function _data_runtime_fe_nodes_refresh() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local platform="${platform:-}"
  local node vip id name ip state key
  local n=1

  vip=$(inv_value var=INV path=${platform}.fe.vip)
  # roles change with the states: forget the old ones
  for key in "${!RUNTIME[@]}"
  do
    [[ "${key}" =~ ^${platform//./\\.},(leader|follower[0-9]*|solo|candidate|error), ]] && unset "RUNTIME[${key}]"
  done  # key

  while IFS=$'\t' read -r id name ip state
  do
    [[ "${state}" == "-" ]] && state=4
    node=$(inv_value var=CONFIG path=onefe.states.node.${state})
    (( state == 2 )) && { node+="${n}"; (( n++ )); }
    RUNTIME[${platform},${node},id]="${id}"
    RUNTIME[${platform},${node},name]="${name}"
    RUNTIME[${platform},${node},ip]="${ip}"
    RUNTIME[${platform},${node},state]="${state}"
    _log_std \
      node_role="${node}" \
      node_id="${id}" \
      node_name="${name}" \
      node_ip="${ip}" \
      node_state="$(inv_value var=CONFIG path=onefe.states.node.${state})"
  done < <(
    $ssh ${vip} \
      "sudo -u oneadmin onezone show ${RUNTIME[${platform},id]} -j 2>/dev/null" |
    jq -r '
      .ZONE.SERVER_POOL.SERVER // []
      | if type == "array"
        then .[]
        else .
        end
      | [
          .ID,
          .NAME,
          ( .ENDPOINT
            | sub("^https?://"; "")
            | sub("[:/].*$"; "")
          ),
          .STATE
        ]
      | @tsv
    '
  )

  return 0
}  # _data_runtime_fe_nodes_refresh



# load runtime data for platform:
#   id, name, state
#   shared
#   vntemplate,id|name
#   files_ds,id|name|clusters|hosts
#   backups_ds,id|name|clusters|hosts
#   images_ds,list
#   images_ds,<id>,name|clusters|hosts
#   vms_ds,list
#   vms_ds,<id>,name|clusters|hosts
#   images_ds_list  vms_ds_list
#
#   platform=NAME
# return 0 - collected
#        1 - the runtime data can not be collected
function _data_runtime_platform_init() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local platform="${platform:-}"
  local kind vip zone_id json shared
  local key type id name clusters hosts ds_json state endpoint shared_owner
  local -a vntemplates=()

  kind=$(inv_value var=INV path=${platform}.kind)
  vip=$(inv_value var=INV path=${platform}.fe.vip)
  if [[ -z "${vip}" ]]
  then
    _log_error "platform \"${platform}\" not found in the inventory"
    return 1
  fi

  zone_id=$(
    $ssh ${vip} "sudo -u oneadmin onezone list -f ENDPOINT~\"//${vip}:\" --no-header -l ID 2>/dev/null" |
    tr -d '[:blank:]' |
    head -n1
  )
  if [[ -z "${zone_id}" ]]
  then
    _log_error "zone \"${zone_id}\" for platform \"${platform}\" is not accessible"
    return 1
  fi

  json=$($ssh ${vip} "sudo -u oneadmin onezone show ${zone_id} -j 2>/dev/null")

  RUNTIME[${platform},id]="${zone_id}"
  RUNTIME[${platform},name]=$(jq -r '.ZONE.NAME // ""' <<< "${json}")
  RUNTIME[${platform},state]=$(jq -r '.ZONE.STATE // ""' <<< "${json}")

  _log_std zone_name="${RUNTIME[${platform},name]}" zone_state="${RUNTIME[${platform},state]}"

  shared=$(inv_value var=INV path=${platform}.platform.shared_owner)
  if [[ -n "${shared}" ]]
  then
    RUNTIME[${platform},shared]=$(
      $ssh ${vip} "sudo -u oneadmin onegroup list -f NAME=\"${shared}\" --no-header -l ID 2>/dev/null" |
      tr -d '[:blank:]'
    )
    _log_std \
      zone_name="${RUNTIME[${platform},name]}" \
      zone_state="${RUNTIME[${platform},state]}" \
      shared_owner="${shared}" \
      shared_id="${RUNTIME[${platform},shared]}"
  fi

  # VNet template: kind=shared requires exactly one, otherwise it is taken when there is exactly one
  mapfile -t vntemplates < <(
    $ssh ${vip} "sudo -u oneadmin onevntemplate list --no-header -l ID 2>/dev/null" |
    tr -d '[:blank:]' |
    grep -v '^$'
  )
  if [[ "${kind}" == "shared" ]] && (( ${#vntemplates[@]} != 1 ))
  then
    _log_error "zone \"${zone_id}\" for platform \"${platform}\" must have only one VNTemplate"
    return 1
  fi
  if (( ${#vntemplates[@]} == 1 ))
  then

    RUNTIME[${platform},vntemplate,id]="${vntemplates[0]}"
    RUNTIME[${platform},vntemplate,name]=$(
      $ssh ${vip} "sudo -u oneadmin onevntemplate show ${vntemplates[0]} -j 2>/dev/null" |
      jq -r '.VNTEMPLATE.NAME // ""'
    )
    _log_std \
      zone_name="${RUNTIME[${platform},name]}" \
      zone_state="${RUNTIME[${platform},state]}" \
      vntemplate_id="${vntemplates[0]}" \
      vntemplate_name="${RUNTIME[${platform},vntemplate,name]}"
  fi

  RUNTIME[${platform},images_ds_list]=""
  RUNTIME[${platform},vms_ds_list]=""
  while IFS=$'\t' read -r type id name clusters hosts
  do
    case ${type} in
      0)  # IMAGE: several - images_ds,<id>,...
        key="images_ds,${id}"
        RUNTIME[${platform},images_ds_list]+="${RUNTIME[${platform},images_ds_list]:+ }${id}"
        ;;
      1)  # SYSTEM: several - vms_ds,<id>,...
        key="vms_ds,${id}"
        RUNTIME[${platform},vms_ds_list]+="${RUNTIME[${platform},vms_ds_list]:+ }${id}"
        ;;
      2)  # FILE: one per platform - files_ds,...
        key="files_ds"
        RUNTIME[${platform},${key},id]="${id}"
        ;;
      3)  # BACKUP: one per platform - backups_ds,...
        key="backups_ds"
        RUNTIME[${platform},${key},id]="${id}"
        ;;
    esac

    RUNTIME[${platform},${key},name]="${name}"
    RUNTIME[${platform},${key},clusters]="${clusters}"
    RUNTIME[${platform},${key},hosts]="${hosts}"
  done < <(
    $ssh ${vip} "sudo -u oneadmin onedatastore list -j 2>/dev/null" |
    jq -r '
      .DATASTORE_POOL.DATASTORE // []
      | if type == "array"
        then .[]
        else .
        end
      | [
          .TYPE,
          .ID,
          .NAME,
          (
            [ .CLUSTERS.ID // [] 
              | if type == "array" 
              then .[]
              else .
              end
            ]
            | join(" ")
          ),
          (.TEMPLATE.BRIDGE_LIST // "")
        ]
      | @tsv
    '
  )

  return 0
}  # _data_runtime_platform_init



help_data[data_load_provider]="\
  data_load_provider         # load user data: the inventory and secrets; call again to switch to another data set
    inv=PATH                 #   inventory directory, like data/SAMPLE/inventory
    secrets=PATH             #   secrets directory
    runtime=PATH             #   directory to save collected runtime data in, created if missing"
# return 0 - loaded
#        1 - inventory or secrets not loaded, runtime directory not defined or not created
#            a zone of a federation is not a platform, or a primary FE not reachable - data not loaded
function data_load_provider() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local inv="${inv:-}"
  local secrets="${secrets:-}"
  local runtime="${runtime:-}"
  local platform

  _data_load_var var=INV dir="${inv}" || return 1
  _data_load_var var=SECRETS dir="${secrets}" || return 1

  # create first: realpath fails on a path that does not exist yet
  if [[ -z "${runtime}" ]]
  then
    _log_error "runtime directory must be specified"
    return 1
  elif ! mkdir -p "${runtime}"
  then
    _log_error "runtime directory \"${runtime}\" can not be created"
    return 1
  fi
  DIR_RUNTIME=$(realpath "${runtime}")
  echo "runtime data: ${DIR_RUNTIME}"
  ls -l "${DIR_RUNTIME}"

  echo -n "platforms: "
  jq -r '
    [.platforms // {} | to_entries[] | .key as $s | .value | keys[] | "\($s).\(.)"]
    | join(" ")
  ' <<< "${INV}"

  RUNTIME=()
  for platform in $(jq -r '
    .platforms // {} | to_entries[] | .key as $s | .value | keys[] | "platforms.\($s).\(.)"
  ' <<< "${INV}")
  do
    RUNTIME[${platform},id]=""
    data_runtime_refresh platform=${platform} fe=yes quota=no usage=no
  done  # platform

  # the data must be consistent, otherwise it is not loaded
  if ! _data_consistancy_check
  then
    _log_error "inconsistent data - not loaded"
    INV="{}"
    SECRETS="{}"
    RUNTIME=()
    return 1
  fi

  return 0
}  # data_load_provider



help_data[data_runtime_refresh]="\
  data_runtime_refresh       # check and refresh runtime data
    platform=NAME            #   platforms.<site>.<platform>, exactly one; passed empty - error;
                             #     collected the first time only, see docs/data/README.md#runtime
    [fe=yes|NO]              #   refresh FE nodes status
    [quota=yes|NO]           #   refresh actual quotas
    [usage=yes|NO]           #   get usage"
# return 0 - done
#        1 - no user data, wrong platform name, the runtime data can not be collected
function data_runtime_refresh() {
  local platform              # unset: platform= not passed - only the data check; never the caller's variable
  local arg; for arg in "$@"; do local "${arg}"; done
  local fe="${fe:-no}"

  if [[ -z "${INV:-}" || "${INV}" == "{}" ]]
  then
    _log_error "no user data loaded: data_load_provider inv=PATH secrets=PATH runtime=PATH"
    return 1
  fi

  [[ -v platform ]] || return 0   # no platform= - only the data check
  if [[ -z "${platform:-}" || ! "${platform}" =~ ^platforms\.[^.[:space:]]+\.[^.[:space:]]+$ ]]
  then
    _log_error "platform=NAME must be defined, exactly one full name: platforms.<site>.<platform>; got \"${platform}\""
    return 1
  fi

  if [[ -z "${RUNTIME[${platform},id]:-}" ]]
  then
    _data_runtime_platform_init platform="${platform}" || return 1
    fe=yes
  fi

  if [[ "${fe:-no}" == "yes" ]]
  then
    _data_runtime_fe_nodes_refresh platform="${platform}" || return 1
  fi

  if [[ "${quota:-no}" == "yes" ]]
  then
    _platform_quota platform="${platform}" || return 1
  fi

  if [[ "${usage:-no}" == "yes" ]]
  then
    _platform_usage platform="${platform}" || return 1
  fi

  return 0
}  # data_runtime_refresh
