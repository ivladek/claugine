#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



claugine_cli_commands+=( "\
  os_service_wait
    ip=IP
    service=NAME
    progress=YES|no
    interval=N(10)
    limit=N(${DATA_REFRESH_LIMIT})"
)
function os_service_wait() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local service="${service:-ssh}"
  local progress="${progress:-yes}"
  local interval="${interval:-10}"
  local limit="${limit:-${DATA_REFRESH_LIMIT}}"
  local start=$(date '+%s')
  local now

  [[ "${progress}" == "yes" ]] && echo -n .
  until $ssh ${ip} "sudo systemctl is-active --quiet ${service}" &>/dev/null
  do
    now=$(date '+%s')
    if (( now - start > limit ))
    then
      echo x
      echo
      echo "!!! ERROR !!! service ${service} on ${ip} not active after ${limit}s"
      $ssh ${ip} "sudo systemctl status ${service}"
      $ssh ${ip} "sudo df -h"
      $ssh ${ip} "uptime"
      return 1
    fi
    sleep ${interval}

    [[ "${progress}" == "yes" ]] && echo -n .
  done  # until

  return 0
}  # os_service_wait
