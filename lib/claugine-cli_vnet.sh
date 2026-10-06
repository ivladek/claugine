#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



claugine_cli_commands+=( "\
  vnet_ar_ip_create          # create Address Range for 1 IP address
    zone_id=N                #   zone of the current installation
    vnet=ID                  #   name or id
    ip=IP                    #   ip address to check"
)
function vnet_ar_ip_create() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n _zone=${zone_data}
  local vnet="${vnet:-}"
  local ip="${ip:-}"
  local result

  _zone_init || return 1
  _zone_check zone_id="${zone_id:-}" || return 1

  vnet_ar_ip_exists zone_id="${zone_id}" vnet="${vnet:-}" ip="${ip:-}" && return 0
  result=$?
  (( result != 4 )) && return ${result}

  $ssh ${_zone[${zone_id},vip]} "sudo -u oneadmin onevnet addar ${vnet} -i ${ip} -s 1"
  return $?
}  # vnet_ar_ip_create



claugine_cli_commands+=( "\
  vnet_ar_ip_exists             # check that Address Range for IP Address exists
    zone_id=N                #   zone of the current installation
    vnet=ID                  #   name or id
    ip=IP                    #   ip address to check"
)
function vnet_ar_ip_exists() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n _zone=${zone_data}
  local vnet="${vnet:-}"
  local ip="${ip:-}"
  local json

  _zone_init || return 1
  _zone_check zone_id="${zone_id:-}" || return 1

  if ! is_ipv4 ip="${ip}"
  then
    echo
    echo "!!! ERROR !!! wrong IP address \"${ip}\""
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 2
  fi

  json=$($ssh ${_zone[${zone_id},vip]} "sudo -u oneadmin onevnet show ${vnet} -j 2>/dev/null")
  if [[ -z "${json}" ]]
  then
    echo
    echo "!!! ERROR !!! wrong VNet id|name \"${vnet}\""
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 3
  fi

  jq -e --arg ip "${ip}" '
    def as_array:
      if type == "array" then .
      elif type == "object" then [.]
      else []
      end;
    def ip4num:
      split(".") | map(tonumber)
      | reduce .[] as $octet (0; . * 256 + $octet);

    ($ip | ip4num) as $target
    | any(
        (.VNET.AR_POOL | as_array)[] | (.AR | as_array)[]
        | select((.IP | type) == "string");
        (.IP | ip4num) as $start
        | (.SIZE | tonumber) as $size
        | $target >= $start and $target < $start + $size
      )
  ' <<< "${json}" &>/dev/null \
  && return 0  # IP is inside an existing AR
  return 4     # no AR contains the IP
}  # vnet_ar_ip_exists



claugine_cli_commands+=( "\
  vnet_ar_mac_create         # returns free MAC address in Address Range
    zone_id=N                #   zone of the current installation
    vnet=ID                  #   name or id"
)
function vnet_ar_mac_create() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n _zone=${zone_data}
  local vnet="${vnet:-}"
  local result

  _zone_init || return 1
  _zone_check zone_id="${zone_id:-}" || return 1

  vnet_ar_mac_get zone_id="${zone_id}" vnet="${vnet:-}"
  result=$?
  if (( result == 3 ))
  then
    $ssh ${_zone[${zone_id},vip]} "sudo -u oneadmin onevnet addar ${vnet} -s 1"
    vnet_ar_mac_get zone_id="${zone_id}" vnet="${vnet:-}"
    result=$?
  fi

  return ${result}
}  # vnet_ar_mac_create



claugine_cli_commands+=( "\
  vnet_ar_mac_get            # return latest free MAC address available  in Address Range
    zone_id=N                #   zone of the current installation
    vnet=ID                  #   name or id"
)
function vnet_ar_mac_get() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n _zone=${zone_data}
  local vnet="${vnet:-}"
  local json mac

  _zone_init || return 1
  _zone_check zone_id="${zone_id:-}" || return 1

  json=$($ssh ${_zone[${zone_id},vip]} "sudo -u oneadmin onevnet show ${vnet} -j 2>/dev/null")
  if [[ -z "${json}" ]]
  then
    echo
    echo "!!! ERROR !!! wrong VNet id|name \"${vnet}\""
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 2
  fi

  # get the ETHER AR with the highest AR_ID that still has a free MAC
  # and inside it the highest MAC that is not leased (or on hold)
  mac=$(jq -r '
    def as_array:
      if type == "array" then .
      elif type == "object" then [.]
      else []
      end;
    def mac2num:
      ascii_downcase | split(":")
      | reduce .[] as $h (0;
          . * 256 + ($h | explode | map(if . >= 97 then . - 87 else . - 48 end) | .[0] * 16 + .[1]));
    def num2mac:
      . as $n
      | [range(5; -1; -1) as $i | ($n / pow(256; $i) | floor) % 256]
      | map(. as $b | "0123456789abcdef"[($b / 16 | floor):($b / 16 | floor) + 1]
                   + "0123456789abcdef"[($b % 16):($b % 16) + 1])
      | join(":");

    [ (.VNET.AR_POOL | as_array)[] | (.AR | as_array)[]
      | select(.TYPE == "ETHER" and (.MAC | type) == "string") ]
    | sort_by(.AR_ID | tonumber) | reverse
    | first(
        .[]
        | (.MAC | mac2num) as $start
        | (.SIZE | tonumber) as $size
        | [ (.LEASES | as_array)[] | (.LEASE | as_array)[] | .MAC | mac2num ] as $leased
        | first(range($size - 1; -1; -1) | $start + . | select(IN($leased[]) | not))
        | num2mac
      ) // ""
  ' <<< "${json}" 2>/dev/null)
  echo -n "${mac}"

  [[ -z "${mac}" ]] && return 3
  return 0
}  # vnet_ar_mac_get



claugine_cli_commands+=( "\
  vnet_ip_leased             # check that address has been already leased
    zone_id=N                #   zone of the current installation
    vnet=ID                  #   name or id
    ip=IP                    #   ip address to check"
)
function vnet_ip_leased() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n _zone=${zone_data}
  local vnet="${vnet:-}"
  local ip="${ip:-}"
  local json

  _zone_init || return 1
  _zone_check zone_id="${zone_id:-}" || return 1

  if ! is_ipv4 ip="${ip}"
  then
    echo
    echo "!!! ERROR !!! wrong IP address \"${ip}\""
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 2
  fi

  json=$($ssh ${_zone[${zone_id},vip]} "sudo -u oneadmin onevnet show ${vnet} -j 2>/dev/null")
  if [[ -z "${json}" ]]
  then
    echo
    echo "!!! ERROR !!! wrong VNet id|name \"${vnet}\""
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 3
  fi

  jq -e --arg ip "${ip}" '
    def as_array:
      if type == "array" then .
      elif type == "object" then [.]
      else []
      end;

    any(
      (.VNET.AR_POOL | as_array)[] | (.AR | as_array)[]
      | (.LEASES | as_array)[] | (.LEASE | as_array)[];
      .IP == $ip
    )
  ' <<< "${json}" &>/dev/null \
  && return 0  # IP is leased
  return 4     # IP is not leased
}  # vnet_ip_leased
