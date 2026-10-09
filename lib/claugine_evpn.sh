#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



# INDEX
#   evpn_vtep_nic_get      show vtep type (dev or local_ip) for each VM nic
#   evpn_vtep_vnet_get     show vtep type (dev or local_ip) for each VNet and VNTemplate
#   evpn_vtep_vnet_set     set vtep to local_ip for each VNet and VNTemplate
#   evpn_vtep_vnm_patch    patch /var/lib/one/remotes/vnm/vxlan/vxlan.rb
#   evpn_vtep_vnm_unpatch  recover original /var/lib/one/remotes/vnm/vxlan/vxlan.rb



help_data[evpn_vtep_nic_get]="\
  evpn_vtep_nic_get          # show vtep type (dev or local_ip) for each VM nic
    platform=NAME            #   platforms.<site>.<platform>"
# return 0 - no NIC with VXLAN_TEP=dev
#        1 - a NIC with VXLAN_TEP=dev found
#            no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
function evpn_vtep_nic_get() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local node
  local vm_id json name nic vtep vip
  local result=0

  data_runtime_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)
  _log_std zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" action="evpn_vtep_nic_get"
  for vm_id in $(
    $ssh ${vip} "sudo -u oneadmin onevm list --no-header -l ID 2>/dev/null" |
    tr -d '[:blank:]' |
    sort -n
  )
  do
    json=$($ssh ${vip} "sudo -u oneadmin onevm show ${vm_id} -j 2>/dev/null")
    name=$(jq -r '.VM.NAME' <<< "${json}")

    for nic in $(
      jq -r '
          .VM.TEMPLATE.NIC
          | if type == "array"
            then .[]
            else .
            end
          | select(type == "object")
          | .NIC_ID
      ' <<< "${json}"
    )
    do
      vtep=$(jq -r --arg nic "${nic}" '
        .VM.TEMPLATE.NIC
        | if type == "array"
          then .[]
          else .
          end
        | select(type == "object")
        | select((.NIC_ID | tostring) == $nic)
        | .VXLAN_TEP // ""
      ' <<< "${json}")
      [[ "${vtep}" == "dev" ]] && result=1
      _log_std vm="${name}" vm_id="${vm_id}" nic="${nic}" vtep="${vtep}"
    done  # nic
  done  # vm_id

  return ${result}
}  # evpn_vtep_nic_get



help_data[evpn_vtep_vnet_get]="\
  evpn_vtep_vnet_get         # show vtep type (dev or local_ip) for each VNet and VNTemplate
    platform=NAME            #   platforms.<site>.<platform>"
# return 0 - no VXLAN network or VNet template with VXLAN_TEP=dev
#        1 - VXLAN_TEP=dev found
#            no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
function evpn_vtep_vnet_get() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local node
  local vnet_id json name type vtep vip
  local result=0

  data_runtime_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)
  _log_std zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" action="evpn_vtep_vnet_get"

  for vnet_id in $(
    $ssh ${vip} "sudo -u oneadmin onevnet list -f BRIDGE~onebr --no-header -l ID 2>/dev/null" |
    tr -d '[:blank:]' |
    sort -n
  )
  do
    json=$($ssh ${vip} "sudo -u oneadmin onevnet show ${vnet_id} -j 2>/dev/null")
    name=$(jq -r '.VNET.NAME' <<< "${json}")
    type=$(jq -r '.VNET.VN_MAD // ""' <<< "${json}")
    vtep=$(jq -r '.VNET.TEMPLATE.VXLAN_TEP // ""' <<< "${json}")
    [[ "${type}" != "vxlan" ]] && continue
    [[ "${vtep}" == "dev" ]] && result=1
    _log_std vnet_id="${vnet_id}" vnet_name="${name}" vnet_type="${type}" vnet_vtep="${vtep}"
  done  # vnet_id

  if [[ -z "${RUNTIME[${platform},vntemplate,id]:-}" ]]
  then
    _log_std vntemplate="skipped" reason="not exactly one VNet template in the zone"
    return ${result}
  fi
  json=$($ssh ${vip} "sudo -u oneadmin onevntemplate show ${RUNTIME[${platform},vntemplate,id]} -j 2>/dev/null")
  name=$(jq -r '.VNTEMPLATE.NAME' <<< "${json}")
  type=$(jq -r '.VNTEMPLATE.TEMPLATE.VN_MAD // ""' <<< "${json}")
  vtep=$(jq -r '.VNTEMPLATE.TEMPLATE.VXLAN_TEP // ""' <<< "${json}")
  [[ "${vtep}" == "dev" ]] && result=1
  _log_std \
    vntemplate_id="${RUNTIME[${platform},vntemplate,id]}" \
    vntemplate_name="${name}" \
    vntemplate_type="${type}" \
    vntemplate_vtep="${vtep}"

  return ${result}
}  # evpn_vtep_vnet_get



help_data[evpn_vtep_vnet_set]="\
  evpn_vtep_vnet_set         # set vtep to local_ip for each VNet and VNTemplate
    platform=NAME            #   platforms.<site>.<platform>
    confirm=yes|NO           #   to suppress interactive confirmation"
# return 0 - done
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            not confirmed
function evpn_vtep_vnet_set() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local node
  local vnet_id json name type vtep vip

  data_runtime_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)
  _log_std zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" action="evpn_vtep_vnet_set"
  _stop_without_confirmation confirm=${confirm:-no} && return 1

  for vnet_id in $(
    $ssh ${vip} "sudo -u oneadmin onevnet list -f BRIDGE~onebr --no-header -l ID 2>/dev/null" |
    tr -d '[:blank:]' |
    sort -n
  )
  do
    json=$($ssh ${vip} "sudo -u oneadmin onevnet show ${vnet_id} -j 2>/dev/null")
    name=$(jq -r '.VNET.NAME' <<< "${json}")
    type=$(jq -r '.VNET.VN_MAD // ""' <<< "${json}")
    vtep=$(jq -r '.VNET.TEMPLATE.VXLAN_TEP // ""' <<< "${json}")
    [[ "${type}" != "vxlan" ]] && continue
    _log_std vnet_id="${vnet_id}" vnet_name="${name}" vnet_type="${type}" vtep_old="${vtep}" vtep_new="local_ip"
    $ssh ${vip} "sudo -u oneadmin onevnet update ${vnet_id} --append 2>/dev/null" <<< "VXLAN_TEP = \"local_ip\""
  done  # vnet_id

  if [[ -z "${RUNTIME[${platform},vntemplate,id]:-}" ]]
  then
    _log_std vntemplate="skipped" reason="not exactly one VNet template in the zone"
    return 0
  fi
  json=$($ssh ${vip} "sudo -u oneadmin onevntemplate show ${RUNTIME[${platform},vntemplate,id]} -j 2>/dev/null")
  name=$(jq -r '.VNTEMPLATE.NAME' <<< "${json}")
  type=$(jq -r '.VNTEMPLATE.TEMPLATE.VN_MAD // ""' <<< "${json}")
  vtep=$(jq -r '.VNTEMPLATE.TEMPLATE.VXLAN_TEP // ""' <<< "${json}")
  _log_std \
    vntemplate_id="${RUNTIME[${platform},vntemplate,id]}" \
    vntemplate_name="${name}" \
    vntemplate_type="${type}" \
    vtep_old="${vtep}" \
    vtep_new="local_ip"
  $ssh ${vip} "sudo -u oneadmin onevntemplate update ${RUNTIME[${platform},vntemplate,id]} --append 2>/dev/null" <<< "VXLAN_TEP = \"local_ip\""

  return 0
}  # evpn_vtep_vnet_set



help_data[evpn_vtep_vnm_patch]="\
  evpn_vtep_vnm_patch        # patch /var/lib/one/remotes/vnm/vxlan/vxlan.rb
                             # to change vtep assignment logic
                             # each bridge created on KVM host during VM start
                             # will have vtep=local_ip inspite of VM NIC vtep settings
                             # inspite of VM NIC vtep settings
    platform=NAME            #   platforms.<site>.<platform>
    confirm=yes|NO           #   to suppress interactive confirmation"
# return 0 - done
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            not confirmed
function evpn_vtep_vnm_patch() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local node file vip

  data_runtime_refresh platform="${platform:-}" fe=yes || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)
  _log_std zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" action="vnm_evpn_vtep_vnm_patch"
  _stop_without_confirmation confirm=${confirm:-no} && return 1

  _log_std \
    zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" \
    action="vnm_evpn_vtep_vnm_patch_prechecks"
  if evpn_vtep_vnet_get platform=${platform}
  then
    _log_std \
      zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" \
      action="vnm_evpn_vtep_vnm_patch_prechecks" \
      status="ok"
  else
    _log_std \
      zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" \
      action="vnm_evpn_vtep_vnm_patch_prechecks" \
      status="failed"
    return 1
  fi

  for node in $(inv_list var=CONFIG path=onefe.nodes_order.direct)
  do
    _log_fe -n action="vnm_evpn_vtep_vnm_patch" status=
    if ! $ssh ${RUNTIME[${platform},${node},ip]} "sudo test -e /var/lib/one/remotes/vnm/vxlan/vxlan.rb.original"
    then
      echo required
      $ssh ${RUNTIME[${platform},${node},ip]} "
        sudo cp -a \
          /var/lib/one/remotes/vnm/vxlan/vxlan.rb \
          /var/lib/one/remotes/vnm/vxlan/vxlan.rb.original
        sudo sed -i \
          \"s/vxlan_tep = conf_attribute(@nic, :vxlan_tep, 'dev')/vxlan_tep = @nic[:conf][:vxlan_tep] || 'dev'/\" \
          /var/lib/one/remotes/vnm/vxlan/vxlan.rb
        sudo sed -i -E \
          's/^([[:space:]]*:vxlan_tep:[[:space:]]*).*/\1local_ip/' \
          /var/lib/one/remotes/etc/vnm/OpenNebulaNetwork.conf
      "
    else
      echo exists
    fi

    for file in \
      /var/lib/one/remotes/vnm/vxlan/vxlan.rb.original \
      /var/lib/one/remotes/vnm/vxlan/vxlan.rb \
      /var/lib/one/remotes/etc/vnm/OpenNebulaNetwork.conf
    do
      $ssh ${RUNTIME[${platform},${node},ip]} "
        echo ${file}
        sudo grep 'vxlan_tep' ${file}
      "
    done  # file
  done  # node

  $ssh ${vip} "sudo -u oneadmin onehost sync --force"

  return 0
}  # evpn_vtep_vnm_patch



help_data[evpn_vtep_vnm_unpatch]="\
  evpn_vtep_vnm_unpatch      # recover original /var/lib/one/remotes/vnm/vxlan/vxlan.rb
                             # to return default vtep assignment logic
                             # each bridge created on KVM host during VM start
                             # will have vtep type (local_ip or dev) based of VM NIC settings
    platform=NAME            #   platforms.<site>.<platform>
    confirm=yes|NO           #   to suppress interactive confirmation"
# return 0 - done
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            not confirmed
function evpn_vtep_vnm_unpatch() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local node file vip

  data_runtime_refresh platform="${platform:-}" fe=yes || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)
  _log_std zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" action="evpn_vtep_vnm_unpatch"
  _stop_without_confirmation confirm=${confirm:-no} && return 1

  _log_std -n \
    zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" \
    action="vnm_evpn_vtep_vnm_unpatch_prechecks" \
    status=
  if evpn_vtep_vnet_get platform=${platform} && evpn_vtep_nic_get platform=${platform}
  then
    echo ok
  else
    echo failed
    return 1
  fi

  for node in $(inv_list var=CONFIG path=onefe.nodes_order.direct)
  do
    _log_fe -n action="vnm_evpn_vtep_vnm_unpatch" status=
    if $ssh ${RUNTIME[${platform},${node},ip]} "sudo test -e /var/lib/one/remotes/vnm/vxlan/vxlan.rb.original"
    then
      echo required
      $ssh ${RUNTIME[${platform},${node},ip]} "
        sudo rm /var/lib/one/remotes/vnm/vxlan/vxlan.rb
        sudo mv \
          /var/lib/one/remotes/vnm/vxlan/vxlan.rb.original \
          /var/lib/one/remotes/vnm/vxlan/vxlan.rb
        sudo chown oneadmin:oneadmin /var/lib/one/remotes/vnm/vxlan/vxlan.rb
        sudo chmod 640 /var/lib/one/remotes/vnm/vxlan/vxlan.rb
      "
    else
      echo exists
    fi

    for file in \
      /var/lib/one/remotes/vnm/vxlan/vxlan.rb \
      /var/lib/one/remotes/etc/vnm/OpenNebulaNetwork.conf
    do
      $ssh ${RUNTIME[${platform},${node},ip]} "
        echo ${file}
        sudo grep 'vxlan_tep' ${file}
      "
    done  # file
  done  # node

  $ssh ${vip} "sudo -u oneadmin onehost sync --force"

  return 0
}  # evpn_vtep_vnm_unpatch
