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



# check that the loaded data is consistent; every violation is reported
#   every platform of the inventory has runtime data: its FE was reachable
#   1. datastores: every cluster has exactly one IMAGES and exactly one VMS datastore,
#      every IMAGES and VMS datastore belongs to a cluster
#   2. federations - a platform whose FE lists more than one zone, each federation checked once:
#      every zone is a platform of the inventory: the host of its ENDPOINT is the fe.vip of the platform;
#      exactly one of them is fe.mode primary, its fe.secondaries lists exactly all the others;
#      every other one is fe.mode secondary, its fe.primary is the primary
# return 0 - consistent
#        1 - a platform without runtime data: its FE not reachable
#            a cluster without or with more than one IMAGES or VMS datastore
#            an IMAGES or VMS datastore without a cluster
#            clusters of a platform can not be read
#            a zone of a federation is not a platform
#            not exactly one primary, fe.secondaries or fe.primary of a federation wrong
function _data_consistancy_check() {
  local platform vip member zone_id host checked members primaries primary expected actual mode ref
  local type ds_id cluster_id clusters cluster_list ids failed result=0
  local -a list zone_ids
  local -A cluster_ds
  local -A ds_name=([images_ds]=IMAGES [vms_ds]=VMS)

  checked=" "
  for platform in ${PLATFORMS}
  do
    # 0. Each platform in inventory must have runtime data
    if [[ -z "${RUNTIME[${platform},id]:-}" ]]
    then
      _log_error "platform ${platform}: no runtime data - the FE is not reachable"
      result=1
      continue
    fi
    vip=$(inv_value var=INV path=${platform}.fe.vip)

    # 1. datastores: exactly one IMAGES and one VMS datastore per cluster, no datastore without a cluster
    failed=0
    cluster_list=$(
      $ssh ${vip} "sudo -u oneadmin onecluster list --no-header -l ID 2>/dev/null" |
      tr -d '[:blank:]' |
      grep -v '^$' |
      sort -n
    )
    if [[ -z "${cluster_list}" ]]
    then
      _log_error "platform ${platform}: clusters can not be read"
      failed=1
    else
      for type in images_ds vms_ds
      do
        cluster_ds=()
        for ds_id in ${RUNTIME[${platform},${type}_list]:-}
        do
          clusters="${RUNTIME[${platform},${type},${ds_id},clusters]:-}"
          if [[ -z "${clusters}" ]]
          then
            _log_error "platform ${platform}: ${ds_name[${type}]} datastore ${ds_id} belongs to no cluster"
            failed=1
            continue
          fi
          for cluster_id in ${clusters}
          do
            cluster_ds[${cluster_id}]+="${cluster_ds[${cluster_id}]:+ }${ds_id}"
          done  # cluster_id
        done  # ds_id

        for cluster_id in ${cluster_list}
        do
          ids="${cluster_ds[${cluster_id}]:-}"
          read -ra list <<< "${ids}"
          if (( ${#list[@]} != 1 ))
          then
            _log_error \
              "platform ${platform}:" \
              "cluster ${cluster_id} has ${ds_name[${type}]} datastores [${ids}] -" \
              "exactly one is required"
            failed=1
          fi
        done  # cluster_id
      done  # type
    fi
    if (( failed == 0 ))
    then
      _log \
        platform=${platform} \
        clusters="[${cluster_list//$'\n'/ }]" \
        action=datastores_check \
        status=ok
    else
      result=1
    fi

    # 2. federation of the platform: found in OpenNebula, checked once - from its first member
    [[ "${checked}" == *" ${platform} "* ]] && continue   # a member of a federation checked already

    mapfile -t zone_ids < <(
      $ssh ${vip} "sudo -u oneadmin onezone list --no-header -l ID 2>/dev/null" |
      tr -d '[:blank:]' |
      grep -v '^$' |
      sort -n
    )
    (( ${#zone_ids[@]} > 1 )) || continue
    failed=0

    # every zone is a platform: the host of its ENDPOINT is the fe.vip of the platform
    members=""
    primaries=""
    for zone_id in "${zone_ids[@]}"
    do
      host=$(
        $ssh ${vip} "sudo -u oneadmin onezone show ${zone_id} -j 2>/dev/null" |
        jq -r '.ZONE.TEMPLATE.ENDPOINT // "" | sub("^[a-z]+://"; "") | sub(":.*$"; "")'
      )
      member=$(jq -r --arg host "${host}" '
        [.platforms // {} | to_entries[] | .key as $s | .value | to_entries[]
         | select(.value.fe.vip? == $host) | "platforms.\($s).\(.key)"][0] // ""
      ' <<< "${INV}")
      if [[ -z "${host}" || -z "${member}" ]]
      then
        _log_error \
          "federation of zones [${zone_ids[*]}]:" \
          "zone ${zone_id} with endpoint ${host:-unknown} is not a platform of the inventory -" \
          "define it as a platform"
        failed=1
        continue
      fi
      members+="${members:+ }${member}"
      [[ "$(inv_value var=INV path=${member}.fe.mode)" == "primary" ]] && primaries+="${primaries:+ }${member}"
    done  # zone_id
    checked+="${members} "

    # exactly one primary, its fe.secondaries lists exactly all the others
    read -ra list <<< "${primaries}"
    if (( ${#list[@]} != 1 ))
    then
      _log_error \
        "federation of [${members}]:" \
        "exactly one platform must have fe.mode primary, found [${primaries}]"
      failed=1
    else
      primary="${primaries}"
      expected=$(tr ' ' '\n' <<< "${members}" | grep -vxF "${primary}" | sort | xargs)
      actual=$(inv_list var=INV path=${primary}.fe.secondaries | sed 's/\.fe$//' | sort | xargs)
      if [[ "${actual}" != "${expected}" ]]
      then
        _log_error \
          "federation of [${members}]:" \
          "fe.secondaries of the primary ${primary} must be [${expected}], found [${actual}]"
        failed=1
      fi

      # every other one is fe.mode secondary, its fe.primary is the primary
      for member in ${expected}
      do
        mode=$(inv_value var=INV path=${member}.fe.mode)
        ref=$(inv_value var=INV path=${member}.fe.primary)
        if [[ "${mode}" != "secondary" || "${ref}" != "${primary}.fe" ]]
        then
          _log_error \
            "federation of [${members}]:" \
            "${member} must have fe.mode secondary and fe.primary ${primary}.fe," \
            "found fe.mode=${mode} fe.primary=${ref}"
          failed=1
        fi
      done  # member
    fi

    if (( failed == 0 ))
    then
      _log \
        platform=${primary} \
        federation_zones="[${zone_ids[*]}]" \
        secondaries="[${expected}]" \
        action=federation_check \
        status=ok
    else
      result=1
    fi
  done  # platform

  return ${result}
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

  _log "var=${var} loaded: $(printf '%s' "${!var}" | jq '
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
  ')"

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
      1)  # VM: several - vms_ds,<id>,...
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
  _log "runtime data: ${DIR_RUNTIME} $(ls -l "${DIR_RUNTIME}")"

  export PLATFORMS=$(
    jq -r '
      [.platforms // {} | to_entries[] | .key as $s | .value | keys[] | "platforms.\($s).\(.)"]
      | join(" ")
    ' <<< "${INV}"
  )
  _log "platforms: [${PLATFORMS}]"

  RUNTIME=()
  for platform in ${PLATFORMS}
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
    PLATFORMS=""
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
