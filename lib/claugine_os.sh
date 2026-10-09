#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



# INDEX
#   _os_service_wait  wait until a service is active on a host



# wait until a service is active on a host
#   ip=IP
#   service=NAME
#   progress=YES|no         # print dots while waiting
#   interval=N(10)          # seconds between checks
#   limit=N                 # seconds, default is <CONFIG.onefe.timeouts.data_refresh>
# return 0 - active
#        1 - not active in time
function _os_service_wait() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local service="${service:-ssh}"
  local progress="${progress:-yes}"
  local interval="${interval:-10}"
  local limit="${limit:-$(inv_value var=CONFIG path=onefe.timeouts.data_refresh)}"
  local start=$(date '+%s')
  local now

  [[ "${progress}" == "yes" ]] && _log -n .
  until $ssh ${ip} "sudo systemctl is-active --quiet ${service}" &>/dev/null
  do
    now=$(date '+%s')
    if (( now - start > limit ))
    then
      [[ "${progress}" == "yes" ]] && _log "] result=timeout limit=${limit}s"
      _log_error "service ${service} on ${ip} not active after ${limit}s"
      $ssh ${ip} "sudo systemctl status ${service}"
      $ssh ${ip} "sudo df -h"
      $ssh ${ip} "uptime"
      return 1
    fi
    sleep ${interval}

    [[ "${progress}" == "yes" ]] && _log -n .
  done  # until

  return 0
}  # _os_service_wait
