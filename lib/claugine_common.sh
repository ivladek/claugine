#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



# INDEX
#   _is_ipv4                    check that ip is a valid IP v4 address
#   _is_mac                     check that mac is a valid MAC address: xx:xx:xx:xx:xx:xx
#   _stop_without_confirmation  ask to confirm a critical operation; no answer in 1 minute - declined



# check that ip is a valid IP v4 address
#   ip=IP
# return 0 - valid
#        1 - not valid
function _is_ipv4() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local octet
  [[ "${ip:-}" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
  for octet in "${BASH_REMATCH[@]:1}"
  do
    (( 10#${octet} <= 255 )) || return 1
  done  # octet
  return 0
}  # _is_ipv4



# check that mac is a valid MAC address: xx:xx:xx:xx:xx:xx
#   mac=MAC
# return 0 - valid
#        1 - not valid
function _is_mac() {
  local arg; for arg in "$@"; do local "${arg}"; done
  [[ "${mac:-}" =~ ^([[:xdigit:]]{2}:){5}[[:xdigit:]]{2}$ ]] || return 1
  return 0
}  # _is_mac



# ask to confirm a critical operation; no answer in 1 minute - declined
#   confirm=yes|NO          # yes - do not ask
# return 0 - declined: the caller stops
#        1 - confirmed
function _stop_without_confirmation() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local confirm="${confirm:-no}"
  local answer="no"

  [[ "${confirm}" == "yes" ]] && return 1

  read -r -t 60 -p "!!! ATTENTION !!! platform=${platform:-} Approve critial operation  [yes/no or it will automatically declined in 1 minute]: " answer
  [[ "${answer}" == "yes" ]] && return 1

  _log "!!! OPERATION CANCELED !!!"
  return 0
}  # _stop_without_confirmation
