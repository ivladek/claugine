#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



# INDEX
#   vnet_ar_ip_create   create Address Range for 1 IP address
#   vnet_ar_ip_exists   check that Address Range for IP Address exists
#   vnet_ar_mac_create  returns free MAC address in Address Range
#   vnet_ar_mac_get     return latest free MAC address available  in Address Range
#   vnet_ip_leased      check that address has been already leased



help_data[vnet_ar_ip_create]="\
  vnet_ar_ip_create          # create Address Range for 1 IP address
    platform=NAME            #   platforms.<site>.<platform>
    vnet=ID                  #   name or id
    ip=IP                    #   ip address to check"
# return 0 - an address range for the IP exists or was created
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            wrong IP
#            wrong VNet
#            onevnet addar failed
function vnet_ar_ip_create() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local vnet="${vnet:-}"
  local ip="${ip:-}"
  local result vip

  data_runtime_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)

  vnet_ar_ip_exists platform="${platform}" vnet="${vnet:-}" ip="${ip:-}" && return 0
  result=$?
  (( result != 4 )) && return ${result}

  $ssh ${vip} "sudo -u oneadmin onevnet addar ${vnet} -i ${ip} -s 1"
  return $?
}  # vnet_ar_ip_create



help_data[vnet_ar_ip_exists]="\
  vnet_ar_ip_exists          # check that Address Range for IP Address exists
    platform=NAME            #   platforms.<site>.<platform>
    vnet=ID                  #   name or id
    ip=IP                    #   ip address to check"
# return 0 - an address range contains the IP
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            wrong IP
#            wrong VNet
#        4 - no address range contains the IP
function vnet_ar_ip_exists() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local vnet="${vnet:-}"
  local ip="${ip:-}"
  local json vip

  data_runtime_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)

  if ! _is_ipv4 ip="${ip}"
  then
    _log_error "wrong IP address \"${ip}\""
    return 1
  fi

  json=$($ssh ${vip} "sudo -u oneadmin onevnet show ${vnet} -j 2>/dev/null")
  if [[ -z "${json}" ]]
  then
    _log_error "wrong VNet id|name \"${vnet}\""
    return 1
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



help_data[vnet_ar_mac_create]="\
  vnet_ar_mac_create         # returns free MAC address in Address Range
    platform=NAME            #   platforms.<site>.<platform>
    vnet=ID                  #   name or id"
# return 0 - prints a free MAC address, an address range is added if needed
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            wrong VNet
#            no free MAC address
function vnet_ar_mac_create() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local vnet="${vnet:-}"
  local result vip

  data_runtime_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)

  vnet_ar_mac_get platform="${platform}" vnet="${vnet:-}"
  result=$?
  if (( result == 3 ))
  then
    $ssh ${vip} "sudo -u oneadmin onevnet addar ${vnet} -s 1"
    vnet_ar_mac_get platform="${platform}" vnet="${vnet:-}"
    result=$?
  fi

  return ${result}
}  # vnet_ar_mac_create



help_data[vnet_ar_mac_get]="\
  vnet_ar_mac_get            # return latest free MAC address available  in Address Range
    platform=NAME            #   platforms.<site>.<platform>
    vnet=ID                  #   name or id"
# return 0 - prints the last free MAC address of the address ranges
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            wrong VNet
#        3 - no free MAC address
function vnet_ar_mac_get() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local vnet="${vnet:-}"
  local json mac vip

  data_runtime_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)

  json=$($ssh ${vip} "sudo -u oneadmin onevnet show ${vnet} -j 2>/dev/null")
  if [[ -z "${json}" ]]
  then
    _log_error "wrong VNet id|name \"${vnet}\""
    return 1
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



help_data[vnet_ip_leased]="\
  vnet_ip_leased             # check that address has been already leased
    platform=NAME            #   platforms.<site>.<platform>
    vnet=ID                  #   name or id
    ip=IP                    #   ip address to check"
# return 0 - the IP is leased
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            wrong IP
#            wrong VNet
#        4 - the IP is not leased
function vnet_ip_leased() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local vnet="${vnet:-}"
  local ip="${ip:-}"
  local json vip

  data_runtime_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)

  if ! _is_ipv4 ip="${ip}"
  then
    _log_error "wrong IP address \"${ip}\""
    return 1
  fi

  json=$($ssh ${vip} "sudo -u oneadmin onevnet show ${vnet} -j 2>/dev/null")
  if [[ -z "${json}" ]]
  then
    _log_error "wrong VNet id|name \"${vnet}\""
    return 1
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
