#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



# print the id of an OpenNebula object found by id or name in a zone of the current installation
#   zone_id=N
#   object=TYPE              # one of ONE_OBJECT_TYPES, the CLI command is one<TYPE>: cluster, image, vnet, ...
#   name=STRING              # name or id
#   usage: id=$(_one_object_id zone_id=N object=image name="Ubuntu 26.04") || return N
#   errors go to stderr, the id to stdout
#   return 1 - wrong parameters or zone, 2 - not found, 3 - name is not unique
function _one_object_id() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local zone_id="${zone_id:-}"
  local object="${object:-}"
  local name="${name:-}"
  local filter
  local -a ids=()

  if [[ " ${ONE_OBJECT_TYPES[*]} " != *" ${object} "* || -z "${name}" ]]
  then
    {
      echo
      echo "!!! ERROR !!! object must be one of [${ONE_OBJECT_TYPES[*]}], name must be defined: object=\"${object}\" name=\"${name}\""
      echo "!!! TRACE !!! $(_zone_caller): $*"
    } >&2
    return 1
  fi
  _zone_check zone_id="${zone_id}" >&2 || return 1

  filter="NAME"
  [[ "${name}" =~ ^[0-9]+$ ]] && filter="ID"
  mapfile -t ids < <(
    $ssh $(_zone_vip ${zone_id}) "sudo -u oneadmin one${object} list -f ${filter}=\"${name}\" --no-header -l ID 2>/dev/null" |
    tr -d '[:blank:]' |
    grep -v '^$'
  )

  if (( ${#ids[@]} != 1 ))
  then
    {
      echo
      if (( ${#ids[@]} == 0 ))
      then
        echo "!!! ERROR !!! ${object} \"${name}\" not found in $(_zone_label ${zone_id})"
      else
        echo "!!! ERROR !!! ${object} name \"${name}\" is not unique: ids=[${ids[*]}] in $(_zone_label ${zone_id}) - use the id"
      fi
      echo "!!! TRACE !!! $(_zone_caller): $*"
    } >&2
    (( ${#ids[@]} == 0 )) && return 2
    return 3
  fi

  echo "${ids[0]}"
  return 0
}  # _one_object_id



# BLOCK  installation context and zones
#   context - one OpenNebula installation (one zone or a federation) loaded by
#   fe_data_refresh into the associative array named by ${zone_data}
#   rules: docs/DEVELOPMENT.md
#######

# name of the command that called a helper: the first function without "_" prefix
function _zone_caller() {
  local fn
  for fn in "${FUNCNAME[@]:1}"
  do
    [[ "${fn}" == _* ]] || { echo "${fn}"; return 0; }
  done
  echo "${FUNCNAME[-1]}"
}  # _zone_caller



# context name and the FE it was loaded from, for logs and prompts
function _zone_installation() {
  local -n _ctx=${zone_data}
  echo "${zone_data}(${_ctx[fe]:-${_ctx[0,vip]:-none}})"
}  # _zone_installation



# check that the context is loaded, refresh it when older than DATA_REFRESH_LIMIT
#   return 1 - not loaded or refresh failed
function _zone_init() {
  local age log

  if [[ "$(declare -p "${zone_data}" 2>/dev/null)" != "declare -A"* ]]
  then
    echo
    echo "!!! ERROR !!! installation context \"${zone_data}\" is not an associative array"
    echo "!!! TRACE !!! $(_zone_caller)"
    echo "to load an installation: fe_data_refresh [data=VAR_NAME] fe=IP"
    return 1
  fi

  local -n _ctx=${zone_data}
  if [[ -z "${_ctx[list]:-}" ]]
  then
    echo
    echo "!!! ERROR !!! installation context \"${zone_data}\" is not loaded"
    echo "!!! TRACE !!! $(_zone_caller)"
    echo "to load an installation: fe_data_refresh [data=VAR_NAME] fe=IP"
    return 1
  fi

  # contexts loaded by older versions
  [[ -n "${_ctx[master]:-}" ]] || _ctx[master]="${_ctx[list]%% *}"
  [[ -n "${_ctx[fe]:-}" ]] || _ctx[fe]="${_ctx[${_ctx[master]},vip]}"
  [[ -n "${_ctx[installation]:-}" ]] || _ctx[installation]="${zone_data}"

  age=$(( $(date '+%s') - ${_ctx[timestamp]:-0} ))
  (( age <= DATA_REFRESH_LIMIT )) && return 0

  echo "installation=$(_zone_installation) action=refresh age=${age}s"
  log="$(mktemp)"
  if ! fe_data_refresh > "${log}" 2>&1
  then
    cat "${log}"
    rm -f "${log}"
    echo
    echo "!!! ERROR !!! can not refresh installation context \"${zone_data}\""
    echo "!!! TRACE !!! $(_zone_caller)"
    return 1
  fi
  rm -f "${log}"
  return 0
}  # _zone_init



# check that the zone belongs to the current installation
#   zone_id=N
#   return 1 - unknown zone or zone without VIP
function _zone_check() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local zone_id="${zone_id:-}"
  local -n _ctx=${zone_data}

  if [[ ! "${zone_id}" =~ ^[0-9]+$ || " ${_ctx[list]:-} " != *" ${zone_id} "* || -z "${_ctx[${zone_id},vip]:-}" ]]
  then
    echo
    echo "!!! ERROR !!! zone_id=\"${zone_id}\" is not a zone of installation $(_zone_installation): zones=[${_ctx[list]:-}]"
    echo "!!! TRACE !!! $(_zone_caller): zone_id=${zone_id}"
    return 1
  fi
  return 0
}  # _zone_check



# print the checked list of zone ids, ALL - all zones of the installation
#   zones=LIST|ALL
#   usage: zones=$(_zones_resolve zones="${zones}") || return 1
#   errors go to stderr, the list to stdout
function _zones_resolve() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local zones="${zones:-ALL}"
  local zone_id
  local -n _ctx=${zone_data}

  [[ "${zones^^}" == ALL ]] && zones="${_ctx[list]:-}"
  if [[ -z "${zones// /}" ]]
  then
    echo "!!! ERROR !!! no zones in installation $(_zone_installation)" >&2
    return 1
  fi
  for zone_id in ${zones}
  do
    _zone_check zone_id="${zone_id}" >&2 || return 1
  done  # zone_id

  echo ${zones}
  return 0
}  # _zones_resolve



# print the VIP of the zone
#   $1 - zone id
function _zone_vip() {
  local -n _ctx=${zone_data}
  echo "${_ctx[${1},vip]:-}"
}  # _zone_vip



# print the log label of the zone: installation=... zone_id=N zone_name=... zone_vip=...
#   $1 - zone id
function _zone_label() {
  local -n _ctx=${zone_data}
  echo "installation=${zone_data} zone_id=${1} zone_name=${_ctx[${1},name]:-} zone_vip=${_ctx[${1},vip]:-}"
}  # _zone_label

##############
# END OF BLOCK  installation context and zones
