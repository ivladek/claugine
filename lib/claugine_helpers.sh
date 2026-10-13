#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



# INDEX
#   _caller         print the name of the command that called a helper: the first function without "_" prefix
#   _log            print a log line: the arguments separated by spaces
#   _log_error      print an error block
#   _log_fe         print a log line of an FE node
#   _log_std        print a log line of a platform
#   _one_object_id  print the id of an OpenNebula object found by id or name on a platform



# print the name of the command that called a helper: the first function without "_" prefix
# return 0 - always
function _caller() {
  local fn
  for fn in "${FUNCNAME[@]:1}"
  do
    [[ "${fn}" == _* ]] || { echo "${fn}"; return 0; }
  done  # fn
  echo "${FUNCNAME[-1]}"
}  # _caller



# print a log line: the arguments separated by spaces
#   -n                      # optional, the first argument: no newline - progress dots follow
#   KEY=VALUE ...           # printed as is: name=value, no quotes
# return 0 - always
function _log() {
  echo "$@"
}  # _log



# print an error block to stderr: never mixed into a value captured with $(...)
#   $* - the message
# return 0 - always
function _log_error() {
  {
    _log
    _log "!!! ERROR !!!" "$@"
    _log "!!! TRACE !!!" "$(_caller)"
  } >&2
}  # _log_error



# print a log line of an FE node
#   -n                      # optional, the first argument: no newline
#   KEY=VALUE ...
# return 0 - always
function _log_fe() {
  local nl=""

  [[ "${1:-}" == "-n" ]] && { nl="-n"; shift; }
  _log_std ${nl} \
    node_role=${node} \
    node_name=${RUNTIME[${platform},${node},name]:-} \
    node_ip=${RUNTIME[${platform},${node},ip]:-} \
    "$@"
}  # _log_fe



# print a log line of a platform
#   -n                      # optional, the first argument: no newline
#   KEY=VALUE ...
# return 0 - always
function _log_std() {
  local nl=""

  [[ "${1:-}" == "-n" ]] && { nl="-n"; shift; }
  _log ${nl} \
    platform=${platform} \
    zone_id=${RUNTIME[${platform},id]:-} \
    zone_vip=${vip:-} \
    "$@"
}  # _log_std



# print the id of an OpenNebula object found by id or name on a platform
#   platform=NAME           # collected by data_runtime_refresh
#   object=TYPE             # one of onefe.objects of CONFIG, the CLI command is one<TYPE>: cluster, image, vnet, ...
#   name=STRING             # name or id
#   usage: id=$(_one_object_id platform=P object=image name="Ubuntu 26.04") || return 1
# return 0 - the id printed
#        1 - error, printed to stderr
function _one_object_id() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local platform="${platform:-}"
  local object="${object:-}"
  local name="${name:-}"
  local filter types vip
  local -a ids=()

  vip=$(inv_value var=INV path=${platform}.fe.vip)
  types="$(inv_list var=CONFIG path=onefe.objects | tr '\n' ' ')"
  if [[ " ${types} " != *" ${object} "* || -z "${name}" || -z "${vip}" ]]
  then
    _log_error "object must be one of [${types% }], name and a loaded platform must be defined: object=\"${object}\" name=\"${name}\" platform=\"${platform}\""
    return 1
  fi

  filter="NAME"
  [[ "${name}" =~ ^[0-9]+$ ]] && filter="ID"
  mapfile -t ids < <(
    $ssh ${vip} "sudo -u oneadmin one${object} list -f ${filter}=\"${name}\" --no-header -l ID 2>/dev/null" |
    tr -d '[:blank:]' |
    grep -v '^$'
  )

  if (( ${#ids[@]} == 0 ))
  then
    _log_error \
      "${object} \"${name}\" not found in" \
      platform=${platform} \
      zone_id=${RUNTIME[${platform},id]} \
      zone_vip=${vip}
    return 1
  fi
  if (( ${#ids[@]} != 1 ))
  then
    _log_error \
      "${object} name \"${name}\" is not unique: ids=[${ids[*]}] in " \
      platform=${platform} \
      zone_id=${RUNTIME[${platform},id]} \
      zone_vip=${vip} \
      "- use the id"
    return 1
  fi

  echo "${ids[0]}"
  return 0
}  # _one_object_id



# print the id of the IMAGES or VMS datastore of a cluster on a platform
#   platform=NAME           # collected by data_runtime_refresh
#   cluster_id=ID           # cluster id, see _one_object_id
#   type=images|vms         # IMAGES or VMS datastore
# return 0 - the id printed
#        1 - error, printed to stderr
function _cluster_ds_id() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local platform="${platform:-}"
  local cluster_id="${cluster_id:-}"
  local type="${type:-}"
  local ds_id

  if [[ ! "${type}" =~ ^(images|vms)$ || ! "${cluster_id}" =~ ^[0-9]+$ || -z "${platform}" ]]
  then
    _log_error "type must be [images|vms], cluster_id a number and platform defined: type=\"${type}\" cluster_id=\"${cluster_id}\" platform=\"${platform}\""
    return 1
  fi

  # exactly one per cluster, checked by data_load_provider
  for ds_id in ${RUNTIME[${platform},${type}_ds_list]:-}
  do
    if [[ " ${RUNTIME[${platform},${type}_ds,${ds_id},clusters]:-} " == *" ${cluster_id} "* ]]
    then
      echo "${ds_id}"
      return 0
    fi
  done  # ds_id

  _log_error "no ${type^^} datastore in cluster" platform=${platform} cluster_id=${cluster_id}
  return 1
}  # _cluster_ds_id
