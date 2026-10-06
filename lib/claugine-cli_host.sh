#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



claugine_cli_commands+=( "\
  host_maintenance_off       # enable hosts
    zone_id=N                #   zone of the current installation
    hosts=LIST               #   host names or ids
    confirm=yes|NO           # to suppress interactive confirmation"
)
function host_maintenance_off() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n _zone=${zone_data}
  local interval="${interval:-30}"
  local limit="${limit:-${HOST_FLUSH_TIMEOUT}}"
  local start now
  local host json id name state vms cluster_id cluster_name

  _zone_init || return 1
  _zone_check zone_id="${zone_id:-}" || return 1

  if [[ -z "${hosts:-}" ]]
  then
    echo
    echo "!!! ERROR !!! hosts must be defined"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 2
  fi

  for host in ${hosts}
  do
    id=$(_one_object_id zone_id="${zone_id}" object=host name="${host}") || return 2
    json=$($ssh ${_zone[${zone_id},vip]} "sudo -u oneadmin onehost show ${id} -j 2>/dev/null")

    name=$(jq -r '.HOST.NAME' <<< "${json}")
    state=$(jq -r '.HOST.STATE' <<< "${json}")
    vms=$(jq -r '.HOST.HOST_SHARE.RUNNING_VMS' <<< "${json}")
    cluster_id=$(jq -r '.HOST.CLUSTER_ID' <<< "${json}")
    cluster_name=$(jq -r '.HOST.CLUSTER' <<< "${json}")

    echo "$(_zone_label ${zone_id}) host_id=${id} host_name=${name} host_state=${ONE_HOST_STATE[${state}]} vms=${vms} cluster_id=${cluster_id} cluster_name=${cluster_name}"
    stop_without_confirmation confirm=${confirm:-no} && return 1

    if ! $ssh ${name} "uptime" &>/dev/null
    then
      echo
      echo "!!! ERROR !!! host ${host} unreachable"
      echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
      return 3
    fi

    (( state == 2 )) && continue  # MONITORED
    if (( state == 4 || state == 8 ))  # DISABLED or OFFLINE
    then
      echo "$(_zone_label ${zone_id}) host_id=${id} host_name=${name} action=enable"
      $ssh ${_zone[${zone_id},vip]} "sudo -u oneadmin onehost enable ${id} &>/dev/null"
      start=$(date '+%s')
      until (( state == 2 ))
      do
        now=$(date '+%s')
        (( now - start > HOST_ENABLE_TIMEOUT )) && break
        sleep 5

        state=$(
          $ssh ${_zone[${zone_id},vip]} "sudo -u oneadmin onehost show ${id} -j 2>/dev/null" |
          jq -r '.HOST.STATE'
        )
      done  # until
    fi

    (( state == 2 )) && continue  # MONITORED

    echo
    echo "!!! ERROR !!! wrong host state ${ONE_HOST_STATE[${state}]}"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 4
  done  # host

  return 0
}  # host_maintenance_off



claugine_cli_commands+=( "\
  host_maintenance_on        # disable hosts than evacuate vms
    zone_id=N                #   zone of the current installation
    hosts=LIST               #   host names or ids
    interval=N(30)           #
    limit=N(${HOST_FLUSH_TIMEOUT})            #
    confirm=yes|NO           #   to suppress interactive confirmation"
)
function host_maintenance_on() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n _zone=${zone_data}
  local interval="${interval:-30}"
  local limit="${limit:-${HOST_FLUSH_TIMEOUT}}"
  local start now
  local host json id name state vms cluster_id cluster_name
  local ids=""

  _zone_init || return 1
  _zone_check zone_id="${zone_id:-}" || return 1

  if [[ -z "${hosts:-}" ]]
  then
    echo
    echo "!!! ERROR !!! hosts must be defined"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 2
  fi

  for host in ${hosts}  # disable hosts
  do
    id=$(_one_object_id zone_id="${zone_id}" object=host name="${host}") || return 2
    ids+=" ${id}"
    json=$($ssh ${_zone[${zone_id},vip]} "sudo -u oneadmin onehost show ${id} -j 2>/dev/null")

    name=$(jq -r '.HOST.NAME' <<< "${json}")
    state=$(jq -r '.HOST.STATE' <<< "${json}")
    vms=$(jq -r '.HOST.HOST_SHARE.RUNNING_VMS' <<< "${json}")
    cluster_id=$(jq -r '.HOST.CLUSTER_ID' <<< "${json}")
    cluster_name=$(jq -r '.HOST.CLUSTER' <<< "${json}")

    echo "$(_zone_label ${zone_id}) host_id=${id} host_name=${name} host_state=${ONE_HOST_STATE[${state}]} vms=${vms} cluster_id=${cluster_id} cluster_name=${cluster_name}"
    stop_without_confirmation confirm=${confirm:-no} && return 1

    if ! $ssh ${name} "uptime" &>/dev/null
    then
      echo
      echo "!!! ERROR !!! host ${host} unreachable"
      echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
      return 3
    fi

    if (( state == 2 ))  # MONITORED
    then
      echo "$(_zone_label ${zone_id}) host_id=${id} host_name=${name} action=disable"
      $ssh ${_zone[${zone_id},vip]} "sudo -u oneadmin onehost disable ${id} &>/dev/null"
      start=$(date '+%s')
      until (( state == 4 ))
      do
        now=$(date '+%s')
        (( now - start > HOST_DISABLE_TIMEOUT )) && break
        sleep 5

        state=$(
          $ssh ${_zone[${zone_id},vip]} "sudo -u oneadmin onehost show ${id} -j 2>/dev/null" |
          jq -r '.HOST.STATE'
        )
      done  # until
    fi

    (( state == 4 )) && continue  # DISABLED

    echo
    echo "!!! ERROR !!! wrong host state ${ONE_HOST_STATE[${state}]}"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 4
  done  # host

  for id in ${ids}  # flush hosts
  do
    json=$($ssh ${_zone[${zone_id},vip]} "sudo -u oneadmin onehost show ${id} -j 2>/dev/null")
    name=$(jq -r '.HOST.NAME' <<< "${json}")
    vms=$(jq -r '.HOST.HOST_SHARE.RUNNING_VMS' <<< "${json}")

    echo "$(_zone_label ${zone_id}) host_id=${id} host_name=${name} action=flush"
    $ssh ${_zone[${zone_id},vip]} "sudo -u oneadmin onehost flush ${id}"
    start=$(date '+%s')

    echo -n "$(_zone_label ${zone_id}) host_id=${id} host_name=${name} vms=[${vms}"
    until (( vms == 0 ))
    do
      now=$(date '+%s')
      if (( now - start > limit ))
      then
        echo "]"
        echo
        echo "!!! ERROR !!! host ${name} is in state ${ONE_HOST_STATE[${state}]} and can not evacutate vms after ${limit}s"
        return 5
      fi
      sleep ${interval}

      echo -n .
      vms=$(
        $ssh ${_zone[${zone_id},vip]} "sudo -u oneadmin onehost show ${id} -j 2>/dev/null" |
        jq -r '.HOST.HOST_SHARE.RUNNING_VMS'
      )
      echo -n ${vms}
    done  # until
    echo "]"
  done  # id

  return 0
}  # host_maintenance_on
