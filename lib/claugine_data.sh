#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



# INDEX
#   _data_check                     check that the loaded data is consistent: the three checks below
#   _data_check_federations         check every federation: its zones against the inventory
#   _data_check_inventory           check the inventory: kinds, clusters, shared_owner - no platform calls
#   _data_check_platform            check the runtime data of the platform against its inventory
#   _data_check_quota               check the quota data: every tenant has at least one qr on a shared platform
#   _data_availability_get          print why the platform is unavailable: unreachable, vip_down, oned_down, no_zone
#   _data_load_var                  load one YAML tree into a variable: directories and file names become keys, all files are me...
#   _data_quota_load                load the quotas of the tenants of the platform: limit, real, then usage
#   _data_runtime_fe_nodes_refresh  load runtime data for platform FE nodes
#   _data_runtime_platform_init     load runtime data for platform
#   _data_usage_refresh             set or refresh the usage of the quotas
#   data_platform_refresh           check, collect and refresh the runtime and quota data of the platform
#   data_provider_load              load user data: the inventory and secrets; call again to switch to another data set



# check that the loaded data is consistent - the only place where the data is checked; every violation is reported
#   _data_check_inventory    the inventory: kinds, clusters, shared_owner - no platform calls
#   _data_check_platform     every platform: its runtime data against its inventory
#   _data_check_federations  every federation: its zones against the inventory
# return 0 - consistent
#        1 - not, the violations printed
function _data_check() {
  local platform result=0

  _data_check_inventory || result=1
  for platform in ${PLATFORMS}
  do
    _data_check_platform platform="${platform}" || result=1
  done  # platform
  _data_check_federations || result=1

  return ${result}
}  # _data_check



# check the inventory, no platform calls:
#   every platform has kind in platform.yaml:
#     mgmt    one per site, exactly one cluster, of kind mgmt
#     shared  several per site, shared_owner in platform.yaml, clusters of kind shared, dedicated or service,
#             at least one of kind shared
#     portal  several per site, every cluster of kind portal
#   every cluster has kind in clusters.yaml:
#     mgmt       the core services to run the platform itself and all other platforms of the site
#     shared     several tenants run their VMs on the same hosts
#     dedicated  the cluster of one tenant
#     service    no tenant VMs: services of the platform - EGW, L2 endpoints
#     portal     a cluster of a platform managed by an external portal
#   quota_code - letters and digits only: required for kind shared and dedicated - binds the cluster to the
#     tenant quota records; not allowed for the other kinds
# return 0 - consistent
#        1 - not, the violations printed
function _data_check_inventory() {
  local platform kind site name cluster_kind shared_clusters quota_code state result=0
  local mgmt_sites=""
  local -A cluster_kinds=([mgmt]="mgmt" [shared]="shared dedicated service" [portal]="portal")

  for platform in ${PLATFORMS}
  do
    kind=$(inv_value var=INV path=${platform}.platform.kind)
    case "${kind}" in
      mgmt)
        site="${platform#platforms.}"
        site="${site%%.*}"
        mgmt_sites+=" ${site}"
        if (( $(inv_keys var=INV path=${platform}.clusters | wc -l) != 1 ))
        then
          _log_error "platform ${platform}: kind mgmt must have exactly one cluster in clusters.yaml, of kind mgmt"
          result=1
        fi
        ;;
      shared)
        if ! inv_value var=INV path=${platform}.platform.shared_owner >/dev/null
        then
          _log_error "platform ${platform}: kind shared requires shared_owner in platform.yaml"
          result=1
        fi
        ;;
      portal)
        ;;
      *)
        _log_error "platform ${platform}: kind \"${kind}\" in platform.yaml - must be mgmt, shared or portal"
        result=1
        ;;
    esac

    # every platform has fe.vip
    if ! inv_value var=INV path=${platform}.fe.vip >/dev/null
    then
      _log_error "platform ${platform}: no fe.vip in fe.yaml"
      result=1
    fi

    # state: deployed (not set), updated, retired; planned platforms are not in PLATFORMS
    state=$(inv_value var=INV path=${platform}.platform.state) || state="deployed"
    if [[ ! "${state}" =~ ^(deployed|updated|retired)$ ]]
    then
      _log_error "platform ${platform}: state \"${state}\" in platform.yaml - must be planned, deployed, updated or retired"
      result=1
    fi

    # a planned platform joins a federation when deployed: not in fe.secondaries, not fe.primary
    for name in $(inv_list var=INV path=${platform}.fe.secondaries) $(inv_value var=INV path=${platform}.fe.primary)
    do
      if [[ " ${PLATFORMS_PLANNED} " == *" ${name} "* ]]
      then
        _log_error "platform ${platform}: ${name} in fe.secondaries or fe.primary is planned - add it when it is deployed"
        result=1
      fi
    done  # name

    # the clusters: kind allowed by the kind of the platform; quota_code - shared and dedicated only, letters and digits
    shared_clusters=0
    for name in $(inv_keys var=INV path=${platform}.clusters)
    do
      cluster_kind=$(inv_value var=INV path=${platform}.clusters.${name}.kind) || cluster_kind=""
      if [[ " ${cluster_kinds[${kind}]:-} " != *" ${cluster_kind} "* || -z "${cluster_kind}" ]]
      then
        _log_error \
          "platform ${platform}: cluster ${name}: kind \"${cluster_kind}\" in clusters.yaml -" \
          "a platform of kind ${kind} allows [${cluster_kinds[${kind}]:-}]"
        result=1
      fi
      [[ "${cluster_kind}" == "shared" ]] && (( shared_clusters++ ))

      if quota_code=$(inv_value var=INV path=${platform}.clusters.${name}.quota_code)
      then
        if [[ ! "${quota_code}" =~ ^[A-Za-z0-9]+$ ]]
        then
          _log_error "platform ${platform}: cluster ${name}: quota_code \"${quota_code}\" - letters and digits only"
          result=1
        fi
        if [[ "${cluster_kind}" != "shared" && "${cluster_kind}" != "dedicated" ]]
        then
          _log_error "platform ${platform}: cluster ${name} of kind ${cluster_kind}: quota_code not allowed - shared and dedicated only"
          result=1
        fi
      elif [[ "${cluster_kind}" == "shared" || "${cluster_kind}" == "dedicated" ]]
      then
        _log_error "platform ${platform}: cluster ${name} of kind ${cluster_kind} has no quota_code in clusters.yaml"
        result=1
      fi
    done  # name

    # kind shared: at least one cluster of kind shared - for the tenants
    if [[ "${kind}" == "shared" ]] && (( shared_clusters == 0 ))
    then
      _log_error "platform ${platform}: kind shared requires at least one cluster of kind shared in clusters.yaml"
      result=1
    fi
  done  # platform

  # kind mgmt: one platform per site
  for site in $(tr ' ' '\n' <<< "${mgmt_sites}" | grep -v '^$' | sort | uniq -d)
  do
    _log_error "site ${site}: more than one platform of kind mgmt"
    result=1
  done  # site

  (( result == 0 )) && _log platforms="[${PLATFORMS}]" action=inventory_check status=ok
  return ${result}
}  # _data_check_inventory



# check the runtime data of the platform against its inventory:
#   the platform has runtime data: its FE was reachable
#   datastores: exactly one FILE and one BACKUP datastore, at least one IMAGES and one VMS datastore;
#     every cluster has exactly one IMAGES and exactly one VMS datastore,
#     every IMAGES and VMS datastore belongs to a cluster
#   clusters: every cluster of the platform is in clusters.yaml, by name; a cluster of clusters.yaml
#     not on the platform - undeployed, not an error: RUNTIME[<platform>,undeployed_clusters_list]
#   kind shared: the group of shared_owner exists, exactly one VNet template
#   platform=NAME
# return 0 - consistent
#        1 - not, the violations printed
function _data_check_platform() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local platform="${platform:-}"
  local vip type ds_id cluster_id clusters cluster_list ids name inv_clusters one_clusters failed result=0
  local -a list
  local -A cluster_ds
  local -A ds_name=([images_ds]=IMAGES [vms_ds]=VMS)

  # unavailable: an error, but state retired - to be destroyed, may be down already
  if [[ "${RUNTIME[${platform},available]:-}" != "yes" ]]
  then
    if [[ "$(inv_value var=INV path=${platform}.platform.state)" == "retired" ]]
    then
      _log platform=${platform} state=retired action=platform_check status=unavailable
      return 0
    fi
    _log_error "platform ${platform}: unavailable - ${RUNTIME[${platform},availability]:-unknown}"
    return 1
  fi
  vip=$(inv_value var=INV path=${platform}.fe.vip)

  # datastores: exactly one FILE and one BACKUP datastore, at least one IMAGES and one VMS datastore
  failed=0
  for type in 2:FILE 3:BACKUP
  do
    ids=$(
      $ssh ${vip} "sudo -u oneadmin onedatastore list -j 2>/dev/null" |
      jq -r --arg t "${type%%:*}" '[.DATASTORE_POOL.DATASTORE // empty] | flatten | map(select(.TYPE == $t) | .ID) | join(" ")'
    )
    read -ra list <<< "${ids}"
    if (( ${#list[@]} != 1 ))
    then
      _log_error "platform ${platform}: ${type#*:} datastores [${ids}] - exactly one is required"
      failed=1
    fi
  done  # type
  for type in images_ds vms_ds
  do
    if [[ -z "${RUNTIME[${platform},${type}_list]}" ]]
    then
      _log_error "platform ${platform}: no ${ds_name[${type}]} datastore"
      failed=1
    fi
  done  # type

  # datastores: exactly one IMAGES and one VMS datastore per cluster, no datastore without a cluster
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
    _log platform=${platform} clusters="[${cluster_list//$'\n'/ }]" action=datastores_check status=ok
  else
    result=1
  fi

  # every cluster of the platform is in the inventory; undeployed clusters of the inventory - not an error
  failed=0
  inv_clusters=$(inv_keys var=INV path=${platform}.clusters | sort)
  one_clusters=$($ssh ${vip} "sudo -u oneadmin onecluster list --no-header -l NAME -s NAME=30 2>/dev/null" | tr -d '[:blank:]' | grep -v '^$' | sort)
  for name in $(comm -13 <(echo "${inv_clusters}") <(echo "${one_clusters}"))
  do
    _log_error "platform ${platform}: cluster ${name} of the platform missing in clusters.yaml"
    failed=1
  done  # name
  if (( failed == 0 ))
  then
    _log \
      platform=${platform} \
      clusters="[${one_clusters//$'\n'/ }]" \
      undeployed="[${RUNTIME[${platform},undeployed_clusters_list]:-}]" \
      action=clusters_check \
      status=ok
  else
    result=1
  fi

  # kind shared: the shared group of shared_owner exists, exactly one VNet template
  if [[ "$(inv_value var=INV path=${platform}.platform.kind)" == "shared" ]]
  then
    failed=0
    if [[ ! "${RUNTIME[${platform},shared]:-}" =~ ^[0-9]+$ ]]
    then
      _log_error "platform ${platform}: shared group \"$(inv_value var=INV path=${platform}.platform.shared_owner)\" not found"
      failed=1
    fi
    if [[ -z "${RUNTIME[${platform},vntemplate,id]:-}" ]]
    then
      _log_error "platform ${platform}: kind shared requires exactly one VNet template"
      failed=1
    fi
    if (( failed == 0 ))
    then
      _log \
        platform=${platform} \
        shared_id="${RUNTIME[${platform},shared]}" \
        vntemplate_id="${RUNTIME[${platform},vntemplate,id]}" \
        action=shared_check \
        status=ok
    else
      result=1
    fi
  fi

  return ${result}
}  # _data_check_platform



# check the quota data loaded by _data_quota_load: every tenant has at least one qr on a shared platform;
#   a tenant without qrs on one platform - no quotas there, not an error;
#   called by data_platform_refresh after loading and reloading
#   tenants=LIST             # optional: only these tenants - names or group ids; empty - all
# return 0 - consistent
#        1 - not, the violations printed; a group list can not be read
function _data_check_quota() {
  local tenants=""
  local arg; for arg in "$@"; do local "${arg}"; done
  local platform member tenant found list all="" result=0

  # the tenants of every shared platform: _get_tenants
  for platform in ${PLATFORMS}
  do
    [[ "$(inv_value var=INV path=${platform}.platform.kind)" == "shared" ]] || continue
    [[ "${RUNTIME[${platform},available]:-}" == "yes" ]] || continue   # unavailable - its tenants not loaded
    list=$(_get_tenants platform="${platform}" tenants="${tenants:-ALL}") || return 1
    all+=" ${list//$'\n'/ }"
  done  # platform

  for tenant in $(tr ' ' '\n' <<< "${all}" | grep -v '^$' | sort -u)
  do
    found=""
    for member in ${PLATFORMS}
    do
      # qrs of undeployed clusters count: not checked
      [[ -n "${QUOTA[${tenant},${member},qrs]:-}${QUOTA[${tenant},${member},undeployed_qrs]:-}" ]] && { found=yes; break; }
    done  # member
    if [[ -z "${found}" ]]
    then
      _log_error "tenant ${tenant}, group ${QUOTA[${tenant},group_id]:-}: no quota records on any shared platform"
      result=1
    fi
  done  # tenant

  return ${result}
}  # _data_check_quota



# check every federation - a platform whose FE lists more than one zone, each federation checked once:
#   every zone is a platform of the inventory: the host of its ENDPOINT is the fe.vip of the platform;
#   every one has the same kind of platform.yaml;
#   exactly one of them is fe.mode primary, its fe.secondaries lists exactly all the others;
#   every other one is fe.mode secondary, its fe.primary is the primary
#   platforms without runtime data are skipped - reported by _data_check_platform
# return 0 - consistent
#        1 - not, the violations printed
function _data_check_federations() {
  local platform vip member zone_id host checked members primaries primary expected actual mode ref failed result=0
  local -a list zone_ids

  checked=" "
  for platform in ${PLATFORMS}
  do
    [[ "${RUNTIME[${platform},available]:-}" == "yes" ]] || continue     # unavailable
    [[ "${checked}" == *" ${platform} "* ]] && continue      # a member of a federation checked already
    vip=$(inv_value var=INV path=${platform}.fe.vip)

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

    # every member has the same kind of platform.yaml
    actual=$(
      for member in ${members}
      do
        inv_value var=INV path=${member}.platform.kind || printf '%s' "-"   # not set - "-"
        echo
      done | sort -u | xargs
    )
    if [[ "${actual}" == *" "* ]]
    then
      _log_error \
        "federation of [${members}]:" \
        "every platform must have the same kind in platform.yaml, found [${actual}]"
      failed=1
    fi

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
      actual=$(inv_list var=INV path=${primary}.fe.secondaries | sort | xargs)
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
        if [[ "${mode}" != "secondary" || "${ref}" != "${primary}" ]]
        then
          _log_error \
            "federation of [${members}]:" \
            "${member} must have fe.mode secondary and fe.primary ${primary}," \
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
}  # _data_check_federations



# load one YAML tree into a variable: directories and file names become keys, all files are merged into one JSON document
#   var=NAME                # one of ${INV_VARS}: CONFIG, TEMPLATES, INV, SECRETS
#   dir=PATH
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



# print why the platform has no zone of its VIP - the probes in this order:
#   unreachable  ssh to the VIP and to every FE node fails: powered off or a network problem
#   vip_down     ssh to the VIP fails, an FE node answers: the VIP is not up (keepalived)
#   oned_down    ssh to the VIP works, onezone list fails: OpenNebula is not running
#   no_zone      onezone list works, no zone has the VIP in its endpoint: the inventory or the federation is wrong
#   platform=NAME
# return 0 - always
function _data_availability_get() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local platform="${platform:-}"
  local vip node probe="-o ConnectTimeout=5 -o BatchMode=yes"

  vip=$(inv_value var=INV path=${platform}.fe.vip)

  if ! $ssh ${probe} ${vip} "true" &>/dev/null
  then
    for node in $(inv_list var=INV path=${platform}.fe.nodes)
    do
      if $ssh ${probe} ${node} "true" &>/dev/null
      then
        echo "vip_down"
        return 0
      fi
    done  # node
    echo "unreachable"
    return 0
  fi

  if ! $ssh ${probe} ${vip} "sudo -u oneadmin onezone list --no-header -l ID" &>/dev/null
  then
    echo "oned_down"
    return 0
  fi

  echo "no_zone"
  return 0
}  # _data_availability_get



# load runtime data for platform:
#   id, name, state
#   availability            available; not - see _data_availability_get, the platform has no other keys
#   shared
#   vntemplate,id|name
#   files_ds,id|name|clusters|hosts
#   backups_ds,id|name|clusters|hosts
#   images_ds,list
#   images_ds,<id>,name|clusters|hosts
#   vms_ds,list
#   vms_ds,<id>,name|clusters|hosts
#   images_ds_list  vms_ds_list
#   undeployed_clusters_list  the clusters of clusters.yaml not on the platform yet - claugine deploys them, names sorted
#   qr_list                 kind=shared only, the qrs of the deployed clusters
#   undeployed_qr_list      kind=shared only, the qrs whose clusters are all undeployed
#   <qr>,clusters_list      kind=shared only, sorted ids
#   noqr_clusters_list      kind=shared only: the clusters without quota_code - kind service, sorted ids
#
#   platform=NAME
function _data_runtime_platform_init() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local platform="${platform:-}"
  local kind vip zone_id json shared
  local key type id name clusters hosts ds_json state endpoint shared_owner qr
  local -a vntemplates=()
  local -A one_cluster_ids=()

  if ! vip=$(inv_value var=INV path=${platform}.fe.vip)
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
    # unavailable: why - RUNTIME[<platform>,availability]; the caller decides
    RUNTIME[${platform},availability]="$(_data_availability_get platform="${platform}")"
    _log_std platform="${platform}" vip="${vip}" zone=none availability="${RUNTIME[${platform},availability]}"
    return 1
  fi
  RUNTIME[${platform},availability]="available"

  json=$($ssh ${vip} "sudo -u oneadmin onezone show ${zone_id} -j 2>/dev/null")

  RUNTIME[${platform},id]="${zone_id}"
  RUNTIME[${platform},name]=$(jq -r '.ZONE.NAME // ""' <<< "${json}")
  RUNTIME[${platform},state]=$(jq -r '.ZONE.STATE // ""' <<< "${json}")

  _log_std zone_name="${RUNTIME[${platform},name]}" zone_state="${RUNTIME[${platform},state]}"

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
    RUNTIME[${platform},${key},clusters]="$(tr ' ' '\n' <<< "${clusters}" | sort -n | xargs)"   # sorted ids
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

  # the clusters of clusters.yaml: deployed - found on the platform by name; undeployed - not yet, claugine deploys them
  while read -r id name
  do
    [[ -n "${id}" ]] && one_cluster_ids[${name}]="${id}"
  done < <($ssh ${vip} "sudo -u oneadmin onecluster list --no-header -l ID,NAME -s NAME=30 2>/dev/null")
  RUNTIME[${platform},undeployed_clusters_list]=""
  for name in $(inv_keys var=INV path=${platform}.clusters | sort)
  do
    [[ -n "${one_cluster_ids[${name}]:-}" ]] && continue
    RUNTIME[${platform},undeployed_clusters_list]+="${RUNTIME[${platform},undeployed_clusters_list]:+ }${name}"
  done  # name
  if [[ -n "${RUNTIME[${platform},undeployed_clusters_list]}" ]]
  then
    _log_std \
      zone_name="${RUNTIME[${platform},name]}" \
      undeployed_clusters="[${RUNTIME[${platform},undeployed_clusters_list]}]"
  fi

  kind=$(inv_value var=INV path=${platform}.platform.kind)
  if [[ "${kind}" == "shared" ]]
  then
    shared=$(inv_value var=INV path=${platform}.platform.shared_owner)
    RUNTIME[${platform},shared]=$(
      $ssh ${vip} "sudo -u oneadmin onegroup list -f NAME=\"${shared}\" --no-header -l ID 2>/dev/null" |
      tr -d '[:blank:]'
    )
    _log_std \
      zone_name="${RUNTIME[${platform},name]}" \
      zone_state="${RUNTIME[${platform},state]}" \
      shared_owner="${shared}" \
      shared_id="${RUNTIME[${platform},shared]}"

    mapfile -t vntemplates < <(
      $ssh ${vip} "sudo -u oneadmin onevntemplate list --no-header -l ID 2>/dev/null" |
      tr -d '[:blank:]' |
      grep -v '^$'
    )

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

    RUNTIME[${platform},qr_list]=""
    RUNTIME[${platform},noqr_clusters_list]=""
    RUNTIME[${platform},undeployed_qr_list]=""
    for name in $(inv_keys var=INV path=${platform}.clusters)
    do
      # undeployed - no id, no quotas; its qr - undeployed if no deployed cluster has it, see below
      if [[ -z "${one_cluster_ids[${name}]:-}" ]]
      then
        qr="$(inv_value var=INV path=${platform}.clusters.${name}.quota_code)" &&
          RUNTIME[${platform},undeployed_qr_list]+=" ${qr^^}"
        continue
      fi
      # a cluster without quota_code - kind service: no tenant VMs, services of the platform - EGW, L2 endpoints
      if ! qr="$(inv_value var=INV path=${platform}.clusters.${name}.quota_code)"
      then
        RUNTIME[${platform},noqr_clusters_list]+="${RUNTIME[${platform},noqr_clusters_list]:+ }${one_cluster_ids[${name}]}"
        continue
      fi
      qr="${qr^^}"
      if [[ " ${RUNTIME[${platform},qr_list]} " != *" ${qr} "* ]]
      then
        RUNTIME[${platform},qr_list]+="${RUNTIME[${platform},qr_list]:+ }${qr}"
        RUNTIME[${platform},${qr},clusters_list]=""
      fi
      RUNTIME[${platform},${qr},clusters_list]+="${RUNTIME[${platform},${qr},clusters_list]:+ }${one_cluster_ids[${name}]}"
    done  # name

    for qr in ${RUNTIME[${platform},qr_list]}
    do
      # sorted ids: CLUSTER_IDS of the VM quota of the qr
      RUNTIME[${platform},${qr},clusters_list]="$(tr ' ' '\n' <<< "${RUNTIME[${platform},${qr},clusters_list]}" | sort -n | xargs)"
      _log_std \
        zone_name="${RUNTIME[${platform},name]}" \
        qr="${qr}" \
        clusters="[${RUNTIME[${platform},${qr},clusters_list]}]"
    done  # qr
    RUNTIME[${platform},noqr_clusters_list]="$(tr ' ' '\n' <<< "${RUNTIME[${platform},noqr_clusters_list]}" | sort -n | xargs)"
    RUNTIME[${platform},undeployed_qr_list]="$(
      for qr in ${RUNTIME[${platform},undeployed_qr_list]}
      do
        [[ " ${RUNTIME[${platform},qr_list]} " == *" ${qr} "* ]] || echo "${qr}"
      done | sort -u | xargs
    )"
    [[ -n "${RUNTIME[${platform},undeployed_qr_list]}" ]] &&
      _log_std zone_name="${RUNTIME[${platform},name]}" undeployed_qrs="[${RUNTIME[${platform},undeployed_qr_list]}]"
    _log_std \
      zone_name="${RUNTIME[${platform},name]}" \
      noqr_clusters="[${RUNTIME[${platform},noqr_clusters_list]}]"
  fi

  return 0
}  # _data_runtime_platform_init



# QUOTA keys - a tenant has quotas on platforms; the unique key: <tenant>,<platform> - platform wide quotas,
#   <tenant>,<platform>,<qr> - a quota record (qr): QUOTA_<SITE>_<qr> of the group, <SITE> - the site of the platform,
#     <qr> - quota_code of clusters.yaml:
#   QUOTA_LOADED                                   global: yes - the quota data of all shared platforms loaded; the tenants - _get_tenants
#   QUOTA[<tenant>,group_id]                       the id of the group tenant-<tenant>
#   QUOTA[<tenant>,<platform>,qrs]                 the quota records of the site of the platform, of deployed clusters
#   QUOTA[<tenant>,<platform>,undeployed_qrs]      the quota records whose clusters are all undeployed - no quotas
#   QUOTA[<tenant>,<platform>,<qr>,clusters]       the ids of the clusters of the qr: RUNTIME[<platform>,<qr>,clusters_list]
#   QUOTA[<tenant>,<platform>,<qr>,status|date_created|date_review]
#   QUOTA[<tenant>,<platform>,<qr>,limit,<field>]  QUOTA_<SITE>_<QR> of the group template: cpu ram_gb gpu disk_gb
#   QUOTA[<tenant>,<platform>,<qr>,real,<field>]   the VM quota of the group with CLUSTER_IDS - exactly the clusters of the qr
#   QUOTA[<tenant>,<platform>,<qr>,usage,<field>]  the usage of that VM quota
#   QUOTA[<tenant>,<platform>,site,<field>]        QUOTA_<SITE> of the group template, field names in lower case
#   QUOTA[<tenant>,<platform>,ds_list]             the datastores of the tenant: IMAGE of the clusters of its qrs, FILE, BACKUP
#   QUOTA[<tenant>,<platform>,ds,<id>,type]        images files backups
#   QUOTA[<tenant>,<platform>,ds,<id>,limit,size_gb]  QUOTA_<SITE>: IMAGES_GB FILES_GB BACKUPS_GB, not set - quota.defaults of CONFIG;
#                                                    images default - images_gb per qr on the IMAGE datastore of its clusters
#   QUOTA[<tenant>,<platform>,ds,<id>,real,size_gb]   the datastore quota of the group: SIZE
#   QUOTA[<tenant>,<platform>,ds,<id>,usage,size_gb]  the datastore quota of the group: SIZE_USED
#   a value not set - "-"; real: -1 - the default quota, -2 - unlimited

# load the quotas of the tenants of the platform: limit and real, then usage by _data_usage_refresh - see the QUOTA keys above;
#   called by data_platform_refresh quota=load|refresh|reload, the platform checked by it: kind shared, runtime data
#   platform=NAME
#   tenants=LIST             # optional: only these tenants loaded again - after quota_tenant_set; empty - all, the platform
# return 0 - loaded
#        1 - a group can not be read, or a qr without clusters - the quota data not loaded:
#            data_platform_refresh clears QUOTA_LOADED, its next quota= call loads all again
function _data_quota_load() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local platform="${platform:-}"
  local tenants="${tenants:-}"
  local vip site json key id tenant qr clusters field value list
  local cpu ram_gb gpu disk_gb status date_created date_review
  local real_cpu real_ram_gb real_gpu real_disk_gb
  local ds_id ds_type cluster_id real_size_gb
  local -a tenants_list=() qrs=() deployed_qrs=() undeployed_qrs=()
  local -A ds_limits=()

  vip=$(inv_value var=INV path=${platform}.fe.vip)
  site="${platform#platforms.}"
  site="${site%%.*}"
  site="${site^^}"

  # the tenants: _get_tenants - all or tenants=
  list=$(_get_tenants platform="${platform}" tenants="${tenants:-ALL}") || return 1
  read -ra tenants_list <<< "${list//$'\n'/ }"

  for tenant in "${tenants_list[@]}"
  do
    json="$($ssh ${vip} "sudo -u oneadmin onegroup show tenant-${tenant} -j 2>/dev/null")"
    if [[ -z "${json}" ]]
    then
      _log_error "platform ${platform}: group tenant-${tenant} can not be read"
      return 1
    fi
    id="$(jq -r '.GROUP.ID' <<< "${json}")"

    mapfile -t qrs < <(
      jq -r --arg s "${site}" '
        (.GROUP.TEMPLATE // {})
        | keys_unsorted[]
        | select(test("^QUOTA_" + $s + "_[A-Z0-9]+$"))
        | ltrimstr("QUOTA_" + $s + "_")
      ' <<< "${json}"
    )
    # each qr must have its cluster(s) in the inventory - quota_code; all of them undeployed - the qr skipped, no quotas
    deployed_qrs=()
    undeployed_qrs=()
    for qr in "${qrs[@]}"
    do
      if [[ -n "${RUNTIME[${platform},${qr},clusters_list]:-}" ]]
      then
        deployed_qrs+=( "${qr}" )
      elif [[ " ${RUNTIME[${platform},undeployed_qr_list]} " == *" ${qr} "* ]]
      then
        undeployed_qrs+=( "${qr}" )
      else
        _log_error "platform ${platform}: tenant ${tenant}, group ${id}: quota record QUOTA_${site}_${qr} - no cluster with quota_code ${qr}"
        return 1
      fi
    done  # qr
    qrs=( "${deployed_qrs[@]}" )

    # drop all quotas for tenant add load them again
    for key in "${!QUOTA[@]}"
    do
      [[ "${key}" == "${tenant},${platform},"* ]] && unset "QUOTA[${key}]"
    done  # key

    QUOTA[${tenant},group_id]="${id}"
    QUOTA[${tenant},${platform},qrs]="${qrs[*]}"
    QUOTA[${tenant},${platform},undeployed_qrs]="${undeployed_qrs[*]}"
    QUOTA[${tenant},${platform},ds_list]=""
    (( ${#undeployed_qrs[@]} == 0 )) ||
      _log_std tenant="${tenant}" group_id="${id}" undeployed_qrs="[${undeployed_qrs[*]}]" notice="the clusters are undeployed - no quotas"
    (( ${#qrs[@]} == 0 )) && continue
    _log_std tenant="${tenant}" group_id="${id}" qrs="[${qrs[*]}]"

    # site wide values: QUOTA_<SITE>
    while IFS=$'\t' read -r field value
    do
      [[ -n "${field}" ]] && QUOTA[${tenant},${platform},site,${field}]="${value}"
    done < <(
      jq -r --arg q "QUOTA_${site}" '
        (.GROUP.TEMPLATE[$q] // {})
        | objects
        | to_entries[]
        | [(.key | ascii_downcase), (.value | tostring)]
        | @tsv
      ' <<< "${json}"
    )

    for qr in "${qrs[@]}"
    do
      QUOTA[${tenant},${platform},${qr},clusters]="${RUNTIME[${platform},${qr},clusters_list]}"

      # limit: QUOTA_<SITE>_<QR> = [ CPU, RAM_GB, GPU, DISK_GB, STATUS, DATE_CREATED, DATE_REVIEW ]
      IFS=$'\t' read -r cpu ram_gb gpu disk_gb status date_created date_review < <(
        jq -r --arg q "QUOTA_${site}_${qr}" '
          .GROUP.TEMPLATE[$q]
          | [ .CPU, .RAM_GB, (.GPU // "0"), .DISK_GB, .STATUS, .DATE_CREATED, .DATE_REVIEW ]
          | map(. // "-")
          | @tsv
        ' <<< "${json}"
      )

      # real: the VM quota of the group whose CLUSTER_IDS are exactly the clusters of the record
      IFS=$'\t' read -r real_cpu real_ram_gb real_gpu real_disk_gb < <(
        jq -r --arg c "${QUOTA[${tenant},${platform},${qr},clusters]}" '
          ($c | split(" ") | map(select(length > 0)) | sort) as $want
          | [.GROUP.VM_QUOTA.VM // empty]
          | flatten
          | map(select($want != [] and ((.CLUSTER_IDS // "") | split(",") | map(gsub("\\s"; "")) | map(select(length > 0)) | sort) == $want))
          | (.[0] // {})
          | [ .CPU, .MEMORY, .PCI_DEV, .SYSTEM_DISK_SIZE ]
          | map(. // "-")
          | @tsv
        ' <<< "${json}"
      )
      # MB -> GB
      [[ "${real_ram_gb}" =~ ^[0-9]+$ ]] && (( real_ram_gb /= 1024 ))
      [[ "${real_disk_gb}" =~ ^[0-9]+$ ]] && (( real_disk_gb /= 1024 ))

      QUOTA[${tenant},${platform},${qr},status]="${status}"
      QUOTA[${tenant},${platform},${qr},date_created]="${date_created}"
      QUOTA[${tenant},${platform},${qr},date_review]="${date_review}"

      for field in cpu ram_gb gpu disk_gb
      do
        QUOTA[${tenant},${platform},${qr},limit,${field}]="${!field}"
        key="real_${field}"
        QUOTA[${tenant},${platform},${qr},real,${field}]="${!key}"
      done  # field

      # limit/real
      _log_std \
        tenant="${tenant}" \
        qr="${qr}" \
        clusters="[${QUOTA[${tenant},${platform},${qr},clusters]}]" \
        status="${status}" \
        date_created="${date_created}" \
        date_review="${date_review}" \
        cpu="${cpu}/${real_cpu}" \
        ram_gb="${ram_gb}/${real_ram_gb}" \
        gpu="${gpu}/${real_gpu}" \
        disk_gb="${disk_gb}/${real_disk_gb}"
    done  # qr

    # datastores: 
    #   IMAGE - per record on the IMAGE datastore of its clusters
    #   FILE and BACKUP - one per platform
    ds_limits=()
    for qr in "${qrs[@]}"
    do
      for ds_id in ${RUNTIME[${platform},images_ds_list]}
      do
        for cluster_id in ${QUOTA[${tenant},${platform},${qr},clusters]}
        do
          [[ " ${RUNTIME[${platform},images_ds,${ds_id},clusters]} " == *" ${cluster_id} "* ]] || continue
          ds_limits[${ds_id}]=$(( ${ds_limits[${ds_id}]:-0} + $(inv_value var=CONFIG path=quota.defaults.images_gb) ))
          QUOTA[${tenant},${platform},ds,${ds_id},type]="images"
          break
        done  # cluster_id
      done  # ds_id
    done  # qr
    for ds_id in "${!ds_limits[@]}"
    do
      # QUOTA_<SITE> IMAGES_GB - the quota of each IMAGE datastore of the tenant
      QUOTA[${tenant},${platform},ds,${ds_id},limit,size_gb]="${QUOTA[${tenant},${platform},site,images_gb]:-${ds_limits[${ds_id}]}}"
    done  # ds_id
    for ds_type in files backups
    do
      ds_id="${RUNTIME[${platform},${ds_type}_ds,id]}"
      QUOTA[${tenant},${platform},ds,${ds_id},type]="${ds_type}"
      QUOTA[${tenant},${platform},ds,${ds_id},limit,size_gb]="${QUOTA[${tenant},${platform},site,${ds_type}_gb]:-$(inv_value var=CONFIG path=quota.defaults.${ds_type}_gb)}"
    done  # ds_type

    for ds_id in $(printf '%s\n' "${!ds_limits[@]}" | sort -n) ${RUNTIME[${platform},files_ds,id]} ${RUNTIME[${platform},backups_ds,id]}
    do
      QUOTA[${tenant},${platform},ds_list]+="${QUOTA[${tenant},${platform},ds_list]:+ }${ds_id}"
      real_size_gb=$(
        jq -r --arg id "${ds_id}" '
          [.GROUP.DATASTORE_QUOTA.DATASTORE // empty]
          | flatten
          | map(select(.ID == $id))
          | (.[0].SIZE // "-")
        ' <<< "${json}"
      )
      # MB -> GB
      [[ "${real_size_gb}" =~ ^[0-9]+$ ]] && (( real_size_gb /= 1024 ))
      QUOTA[${tenant},${platform},ds,${ds_id},real,size_gb]="${real_size_gb}"

      # limit/real
      _log_std \
        tenant="${tenant}" \
        ds_id="${ds_id}" \
        type="${QUOTA[${tenant},${platform},ds,${ds_id},type]}" \
        size_gb="${QUOTA[${tenant},${platform},ds,${ds_id},limit,size_gb]}/${real_size_gb}"
    done  # ds_id
  done  # tenant

  _data_usage_refresh platform="${platform}" tenants="${tenants}" || return 1
  return 0
}  # _data_quota_load



# set or refresh the usage of the quotas loaded by _data_quota_load: QUOTA[...,usage,...];
#   called by _data_quota_load and data_platform_refresh quota=refresh
#   platform=NAME
#   tenants=LIST             # optional: only these tenants; empty - all
# return 0 - always; a group that can not be read - a warning, its usage not changed
function _data_usage_refresh() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local platform="${platform:-}"
  local tenants="${tenants:-}"
  local vip json tenant qr clusters field key ds_id used_size_gb
  local used_cpu used_ram_gb used_gpu used_disk_gb

  vip=$(inv_value var=INV path=${platform}.fe.vip)

  for tenant in $(_get_tenants platform="${platform}" tenants="${tenants:-ALL}")
  do
    [[ -n "${QUOTA[${tenant},${platform},qrs]}" ]] || continue
    json="$($ssh ${vip} "sudo -u oneadmin onegroup show ${QUOTA[${tenant},group_id]} -j 2>/dev/null")"
    if [[ -z "${json}" ]]
    then
      _log_std tenant="${tenant}" group_id="${QUOTA[${tenant},group_id]}" warning="group can not be read - usage not refreshed"
      continue
    fi

    for qr in ${QUOTA[${tenant},${platform},qrs]}
    do
      clusters="${QUOTA[${tenant},${platform},${qr},clusters]}"
      IFS=$'\t' read -r used_cpu used_ram_gb used_gpu used_disk_gb < <(
        jq -r --arg c "${clusters}" '
          ($c | split(" ") | map(select(length > 0)) | sort) as $want
          | [.GROUP.VM_QUOTA.VM // empty]
          | flatten
          | map(select($want != [] and ((.CLUSTER_IDS // "") | split(",") | map(gsub("\\s"; "")) | map(select(length > 0)) | sort) == $want))
          | (.[0] // {})
          | [ .CPU_USED, .MEMORY_USED, .PCI_DEV_USED, .SYSTEM_DISK_SIZE_USED ]
          | map(. // "-")
          | @tsv
        ' <<< "${json}"
      )
      # MB -> GB
      [[ "${used_ram_gb}" =~ ^[0-9]+$ ]] && (( used_ram_gb /= 1024 ))
      [[ "${used_disk_gb}" =~ ^[0-9]+$ ]] && (( used_disk_gb /= 1024 ))
      # CPU used rounded up: 1.1 - 2, 2.0 - 2
      if [[ "${used_cpu}" =~ ^([0-9]+)\.([0-9]+)$ ]]
      then
        used_cpu=$(( 10#${BASH_REMATCH[1]} ))
        [[ "${BASH_REMATCH[2]}" =~ [1-9] ]] && (( used_cpu++ ))
      fi

      for field in cpu ram_gb gpu disk_gb
      do
        key="used_${field}"
        QUOTA[${tenant},${platform},${qr},usage,${field}]="${!key}"
      done  # field

      _log_std \
        tenant="${tenant}" \
        qr="${qr}" \
        clusters="[${clusters}]" \
        cpu_used="${used_cpu}" \
        ram_gb_used="${used_ram_gb}" \
        gpu_used="${used_gpu}" \
        disk_gb_used="${used_disk_gb}"
    done  # qr

    for ds_id in ${QUOTA[${tenant},${platform},ds_list]}
    do
      used_size_gb=$(
        jq -r --arg id "${ds_id}" '
          [.GROUP.DATASTORE_QUOTA.DATASTORE // empty]
          | flatten
          | map(select(.ID == $id))
          | (.[0].SIZE_USED // "-")
        ' <<< "${json}"
      )
      [[ "${used_size_gb}" =~ ^[0-9]+$ ]] && (( used_size_gb /= 1024 ))
      QUOTA[${tenant},${platform},ds,${ds_id},usage,size_gb]="${used_size_gb}"
      _log_std tenant="${tenant}" ds_id="${ds_id}" type="${QUOTA[${tenant},${platform},ds,${ds_id},type]}" size_gb_used="${used_size_gb}"
    done  # ds_id
  done  # tenant

  return 0
}  # _data_usage_refresh



help_data[data_provider_load]="\
  data_provider_load         # load user data: the inventory and secrets; call again to switch to another data set
    inv=PATH                 #   inventory directory, like data/SAMPLE/inventory
    secrets=PATH             #   secrets directory
    runtime=PATH             #   directory to save collected runtime data in, created if missing
    [quota=load|SKIP]        #   tenant quotas of the platforms with kind shared, loaded after the consistency check,
                             #     see data_platform_refresh"
function data_provider_load() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local inv="${inv:-}"
  local secrets="${secrets:-}"
  local runtime="${runtime:-}"
  local quota="${quota:-skip}"
  local platform kind state

  if [[ "${quota}" != "load" && "${quota}" != "skip" ]]
  then
    _log_error "quota=\"${quota}\": must be load or skip"
    return 1
  fi

  if [[ -z "${runtime}" ]]
  then
    _log_error "runtime directory must be specified"
    return 1
  elif ! mkdir -p "${runtime}"
  then
    _log_error "runtime directory \"${runtime}\" can not be created"
    return 1
  fi

  _data_load_var var=INV dir="${inv}" || return 1
  _data_load_var var=SECRETS dir="${secrets}" || return 1

  DIR_RUNTIME=$(realpath "${runtime}")
  _log "runtime data: ${DIR_RUNTIME} $(ls -l "${DIR_RUNTIME}")"

  # PLATFORMS - every platform but planned ones; PLATFORMS_PLANNED - platform.state planned: to be deployed,
  #   no runtime data, not checked
  export PLATFORMS=""
  export PLATFORMS_PLANNED=""
  for platform in $(
    jq -r '
      [.platforms // {} | to_entries[] | .key as $s | .value | keys[] | "platforms.\($s).\(.)"]
      | join(" ")
    ' <<< "${INV}"
  )
  do
    if [[ "$(inv_value var=INV path=${platform}.platform.state)" == "planned" ]]
    then
      PLATFORMS_PLANNED+="${PLATFORMS_PLANNED:+ }${platform}"
    else
      PLATFORMS+="${PLATFORMS:+ }${platform}"
    fi
  done  # platform
  _log "platforms: [${PLATFORMS}] planned: [${PLATFORMS_PLANNED}]"

  RUNTIME=()
  QUOTA=()
  QUOTA_LOADED=""
  DEPLOYMENT=""
  # every platform: available - its runtime data; unavailable - the FE is not reachable or has no zone of its VIP:
  #   no runtime data, not checked, commands on it fail
  for platform in ${PLATFORMS}
  do
    RUNTIME[${platform},id]=""
    RUNTIME[${platform},available]="no"
    if _data_runtime_platform_init platform=${platform}
    then
      RUNTIME[${platform},available]="yes"
      _data_runtime_fe_nodes_refresh platform=${platform}
    else
      RUNTIME[${platform},id]=""
      _log_std platform="${platform}" available=no availability="${RUNTIME[${platform},availability]:-}" warning="unavailable"
    fi
  done  # platform

  # the data must be consistent, otherwise it is not loaded
  if ! _data_check
  then
    _log_error "inconsistent data - not loaded"
    INV="{}"
    SECRETS="{}"
    RUNTIME=()
    QUOTA=()
    QUOTA_LOADED=""
    DEPLOYMENT=""
    PLATFORMS=""
    PLATFORMS_PLANNED=""
    return 1
  fi

  # DEPLOYMENT=required - only deployment operations, see data_platform_refresh deployment=:
  #   a planned platform, a platform of state updated or retired, an undeployed cluster
  [[ -n "${PLATFORMS_PLANNED}" ]] && DEPLOYMENT="required" && _log_std planned="[${PLATFORMS_PLANNED}]" deployment=required
  for platform in ${PLATFORMS}
  do
    state=$(inv_value var=INV path=${platform}.platform.state) || state="deployed"
    if [[ "${state}" != "deployed" || -n "${RUNTIME[${platform},undeployed_clusters_list]:-}" ]]
    then
      DEPLOYMENT="required"
      _log_std \
        platform="${platform}" \
        state="${state}" \
        undeployed_clusters="[${RUNTIME[${platform},undeployed_clusters_list]:-}]" \
        deployment=required
    fi
  done  # platform
  [[ "${DEPLOYMENT}" == "required" ]] && _log "deployment required: only deployment operations are enabled"

  if [[ "${quota}" == "load" ]]
  then
    if [[ "${DEPLOYMENT}" == "required" ]]
    then
      _log_std quota=skip reason="deployment required"
    else
      for platform in ${PLATFORMS}
      do
        kind=$(inv_value var=INV path=${platform}.platform.kind)
        [[ "${kind}" == "shared" && "${RUNTIME[${platform},available]}" == "yes" ]] || continue
        if ! data_platform_refresh platform=${platform} quota=load
        then
          _log_error "quota data can not be loaded - data not loaded"
          INV="{}"
          SECRETS="{}"
          RUNTIME=()
          QUOTA=()
          QUOTA_LOADED=""
          DEPLOYMENT=""
          PLATFORMS=""
          PLATFORMS_PLANNED=""
          return 1
        fi
        break
      done  # platform
    fi
  fi

  return 0
}  # data_provider_load



help_data[data_platform_refresh]="\
  data_platform_refresh      # check, collect and refresh the runtime and quota data of the platform
    platform=NAME            #   platforms.<site>.<platform>, exactly one; passed empty - error;
                             #     runtime data collected the first time only, see docs/data/README.md#runtime
    [fe=yes|NO]              #   refresh FE nodes status
    [quota=SKIP|load|refresh|reload]  # tenant quotas - the platform must be kind shared; not loaded (QUOTA_LOADED) -
                             #     limit, real and usage of all tenants of all shared platforms; loaded: load - nothing, refresh - usage,
                             #     reload - limit, real and usage again: after quota_tenant_set
    [tenants=LIST]           #   refresh, reload: only these tenants - names or group ids; empty - all
    [deployment=yes|NO]      #   a deployment operation: the only kind allowed when DEPLOYMENT=required, see data_provider_load;
                             #     a planned platform - allowed with deployment=yes only, no runtime data, return 0"
function data_platform_refresh() {
  local platform="" fe=no quota=skip tenants="" deployment=no
  local arg; for arg in "$@"; do local "${arg}"; done
  local kind member

  if [[ -z "${INV:-}" || "${INV}" == "{}" ]]
  then
    _log_error "no user data loaded: data_provider_load inv=PATH secrets=PATH runtime=PATH"
    return 1
  fi

  if [[ "${DEPLOYMENT}" == "required" && "${deployment}" != "yes" ]]
  then
    _log_error "deployment required: planned, updated or retired platforms or undeployed clusters - only deployment operations are enabled"
    return 1
  fi

  if [[ -z "${platform}" || ! "${platform}" =~ ^platforms\.[^.[:space:]]+\.[^.[:space:]]+$ ]]
  then
    _log_error "platform must be exactly one full name: platforms.<site>.<platform>; got \"${platform}\""
    return 1
  fi

  # planned: to be deployed - nothing to collect
  if [[ " ${PLATFORMS_PLANNED} " == *" ${platform} "* ]]
  then
    [[ "${deployment}" == "yes" ]] && return 0
    _log_error "platform \"${platform}\": planned - only deployment operations"
    return 1
  fi
  if [[ " ${PLATFORMS} " != *" ${platform} "* ]]
  then
    _log_error "platform \"${platform}\" not found in the inventory"
    return 1
  fi

  if [[ -z "${RUNTIME[${platform},id]:-}" ]]
  then
    if ! _data_runtime_platform_init platform="${platform}"
    then
      RUNTIME[${platform},id]=""
      _log_error "platform \"${platform}\": unavailable - ${RUNTIME[${platform},availability]:-unknown}"
      return 1
    fi
    RUNTIME[${platform},available]="yes"
    fe=yes
  fi

  if [[ "${fe:-no}" == "yes" ]]
  then
    _data_runtime_fe_nodes_refresh platform="${platform}" || return 1
  fi

  case "${quota}" in
    skip) ;;
    load|refresh|reload)
      kind=$(inv_value var=INV path=${platform}.platform.kind)
      if [[ "${kind}" != "shared" ]]
      then
        _log_error "platform \"${platform}\": kind \"${kind}\" - no tenants, kind shared required"
        return 1
      fi
      if [[ -z "${QUOTA_LOADED}" ]]
      then
        # not loaded: all platforms of kind shared at once - the quota data is loaded for all or for none
        QUOTA=()
        for member in ${PLATFORMS}
        do
          [[ "$(inv_value var=INV path=${member}.platform.kind)" == "shared" ]] || continue
          [[ "${RUNTIME[${member},available]:-}" == "yes" ]] || continue   # unavailable - its quotas not loaded
          if ! _data_quota_load platform="${member}"
          then
            QUOTA=()
            return 1
          fi
        done  # member
        if ! _data_check_quota
        then
          QUOTA=()
          return 1
        fi
        QUOTA_LOADED="yes"
      elif [[ "${quota}" == "reload" ]]
      then
        # limit, real, usage of the tenants on the platform again; an error - all loaded again next time
        _data_quota_load platform="${platform}" tenants="${tenants}" || { QUOTA_LOADED=""; return 1; }
        _data_check_quota tenants="${tenants}" || { QUOTA_LOADED=""; return 1; }
      elif [[ "${quota}" == "refresh" ]]
      then
        _data_usage_refresh platform="${platform}" tenants="${tenants}" || return 1
      fi
    ;;
    *)
      _log_error "quota=\"${quota}\": must be skip, load, refresh or reload"
      return 1
    ;;
  esac

  return 0
}  # data_platform_refresh
