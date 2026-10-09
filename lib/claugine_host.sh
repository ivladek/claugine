#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



# INDEX
#   host_maintenance_off  enable hosts
#   host_maintenance_on   disable hosts than evacuate vms



help_data[host_maintenance_off]="\
  host_maintenance_off       # enable hosts
    platform=NAME            #   platforms.<site>.<platform>
    hosts=LIST               #   host names or ids
    confirm=yes|NO           # to suppress interactive confirmation"
# return 0 - done
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            not confirmed
#            hosts not defined or not found
#            host unreachable
#            wrong host state
function host_maintenance_off() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local interval="${interval:-30}"
  local limit="${limit:-$(inv_value var=CONFIG path=onefe.timeouts.host_flush)}"
  local start now
  local host json id name state vms cluster_id cluster_name vip

  data_runtime_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)

  if [[ -z "${hosts:-}" ]]
  then
    _log_error "hosts must be defined"
    return 1
  fi

  for host in ${hosts}
  do
    id=$(_one_object_id platform="${platform}" object=host name="${host}") || return 1
    json=$($ssh ${vip} "sudo -u oneadmin onehost show ${id} -j 2>/dev/null")

    name=$(jq -r '.HOST.NAME' <<< "${json}")
    state=$(jq -r '.HOST.STATE' <<< "${json}")
    vms=$(jq -r '.HOST.HOST_SHARE.RUNNING_VMS' <<< "${json}")
    cluster_id=$(jq -r '.HOST.CLUSTER_ID' <<< "${json}")
    cluster_name=$(jq -r '.HOST.CLUSTER' <<< "${json}")

    _log_std \
      host_id="${id}" \
      host_name="${name}" \
      host_state="$(inv_value var=CONFIG path=onefe.states.host.${state})" \
      vms="${vms}" \
      cluster_id="${cluster_id}" \
      cluster_name="${cluster_name}"
    _stop_without_confirmation confirm=${confirm:-no} && return 1

    if ! $ssh ${name} "uptime" &>/dev/null
    then
      _log_error "host ${host} unreachable"
      return 1
    fi

    (( state == 2 )) && continue  # MONITORED
    if (( state == 4 || state == 8 ))  # DISABLED or OFFLINE
    then
      _log_std host_id="${id}" host_name="${name}" action="enable"
      $ssh ${vip} "sudo -u oneadmin onehost enable ${id} &>/dev/null"
      start=$(date '+%s')
      until (( state == 2 ))
      do
        now=$(date '+%s')
        (( now - start > $(inv_value var=CONFIG path=onefe.timeouts.host_enable) )) && break
        sleep 5

        state=$(
          $ssh ${vip} "sudo -u oneadmin onehost show ${id} -j 2>/dev/null" |
          jq -r '.HOST.STATE'
        )
      done  # until
    fi

    (( state == 2 )) && continue  # MONITORED

    _log_error "wrong host state $(inv_value var=CONFIG path=onefe.states.host.${state})"
    return 1
  done  # host

  return 0
}  # host_maintenance_off



help_data[host_maintenance_on]="\
  host_maintenance_on        # disable hosts than evacuate vms
    platform=NAME            #   platforms.<site>.<platform>
    hosts=LIST               #   host names or ids
    interval=N(30)           #
    limit=N(<CONFIG.onefe.timeouts.host_flush>) #
    confirm=yes|NO           #   to suppress interactive confirmation"
# return 0 - done
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            not confirmed
#            hosts not defined or not found
#            host unreachable
#            wrong host state
#            VMs can not be moved off the host
function host_maintenance_on() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local interval="${interval:-30}"
  local limit="${limit:-$(inv_value var=CONFIG path=onefe.timeouts.host_flush)}"
  local start now
  local host json id name state vms cluster_id cluster_name vip
  local ids=""

  data_runtime_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)

  if [[ -z "${hosts:-}" ]]
  then
    _log_error "hosts must be defined"
    return 1
  fi

  for host in ${hosts}  # disable hosts
  do
    id=$(_one_object_id platform="${platform}" object=host name="${host}") || return 1
    ids+="${ids:+ }${id}"
    json=$($ssh ${vip} "sudo -u oneadmin onehost show ${id} -j 2>/dev/null")

    name=$(jq -r '.HOST.NAME' <<< "${json}")
    state=$(jq -r '.HOST.STATE' <<< "${json}")
    vms=$(jq -r '.HOST.HOST_SHARE.RUNNING_VMS' <<< "${json}")
    cluster_id=$(jq -r '.HOST.CLUSTER_ID' <<< "${json}")
    cluster_name=$(jq -r '.HOST.CLUSTER' <<< "${json}")

    _log_std \
      host_id="${id}" \
      host_name="${name}" \
      host_state="$(inv_value var=CONFIG path=onefe.states.host.${state})" \
      vms="${vms}" \
      cluster_id="${cluster_id}" \
      cluster_name="${cluster_name}"
    _stop_without_confirmation confirm=${confirm:-no} && return 1

    if ! $ssh ${name} "uptime" &>/dev/null
    then
      _log_error "host ${host} unreachable"
      return 1
    fi

    if (( state == 2 ))  # MONITORED
    then
      _log_std host_id="${id}" host_name="${name}" action="disable"
      $ssh ${vip} "sudo -u oneadmin onehost disable ${id} &>/dev/null"
      start=$(date '+%s')
      until (( state == 4 ))
      do
        now=$(date '+%s')
        (( now - start > $(inv_value var=CONFIG path=onefe.timeouts.host_disable) )) && break
        sleep 5

        state=$(
          $ssh ${vip} "sudo -u oneadmin onehost show ${id} -j 2>/dev/null" |
          jq -r '.HOST.STATE'
        )
      done  # until
    fi

    (( state == 4 )) && continue  # DISABLED

    _log_error "wrong host state $(inv_value var=CONFIG path=onefe.states.host.${state})"
    return 1
  done  # host

  for id in ${ids}  # flush hosts
  do
    json=$($ssh ${vip} "sudo -u oneadmin onehost show ${id} -j 2>/dev/null")
    name=$(jq -r '.HOST.NAME' <<< "${json}")
    vms=$(jq -r '.HOST.HOST_SHARE.RUNNING_VMS' <<< "${json}")

    _log_std host_id="${id}" host_name="${name}" action="flush"
    $ssh ${vip} "sudo -u oneadmin onehost flush ${id}"
    start=$(date '+%s')

    _log_std -n host_id="${id}" host_name="${name}" vms="[${vms}"
    until (( vms == 0 ))
    do
      now=$(date '+%s')
      if (( now - start > limit ))
      then
        echo "]"
        _log_error "host ${name} is in state $(inv_value var=CONFIG path=onefe.states.host.${state}) and can not evacutate vms after ${limit}s"
        return 1
      fi
      sleep ${interval}

      echo -n .
      vms=$(
        $ssh ${vip} "sudo -u oneadmin onehost show ${id} -j 2>/dev/null" |
        jq -r '.HOST.HOST_SHARE.RUNNING_VMS'
      )
      echo -n ${vms}
    done  # until
    echo "]"
  done  # id

  return 0
}  # host_maintenance_on
