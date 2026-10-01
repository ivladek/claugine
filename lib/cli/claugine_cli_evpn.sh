#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



claugine_cli_commands+=( "\
  evpn_vtep_nic_get
    zones=ID LIST    # ALL for all nodes in each zone"
)
function evpn_vtep_nic_get() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n zone=${zone_data}
  local zones="${zones:-ALL}"
  local zone_id node
  local vm_id json name nic vtep
  local result=0

  if [[ -z "${zone[@]:-}" ]]
  then
    echo
    echo "!!! ERROR !!! ONE FE zones data ${zone_data} is not initialized"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    echo "to initialize data: fe_data_refresh [data=var name] fe=IP"
    return 1
  fi

  [[ "${zones^^}" == ALL ]] && zones="${zone[list]}"
  for zone_id in ${zones}
  do
    echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} zone_state=${zone[${zone_id},state]}"
    for vm_id in $(
      $ssh ${zone[${zone_id},vip]} "sudo -u oneadmin onevm list --no-header -l ID 2>/dev/null" |
      tr -d '[:blank:]' |
      sort -n
    )
    do
      json=$($ssh ${zone[${zone_id},vip]} "sudo -u oneadmin onevm show ${vm_id} -j 2>/dev/null")
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
        echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} vm=${name} vm_id=${vm_id} nic=${nic} vtep=${vtep}"
      done  # nic
    done  # vm_id
  done  # zone_id

  return ${result}
}  # evpn_vtep_nic_get



claugine_cli_commands+=( "\
  evpn_vtep_vnet_get
    zones=ID LIST     # ALL for all nodes in each zone"
)
function evpn_vtep_vnet_get() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n zone=${zone_data}
  local zones="${zones:-ALL}"
  local zone_id node
  local vnet_id json name type vtep
  local result=0

  if [[ -z "${zone[@]:-}" ]]
  then
    echo
    echo "!!! ERROR !!! ONE FE zones data ${zone_data} is not initialized"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    echo "to initialize data: fe_data_refresh [data=var name] fe=IP"
    return 1
  fi

  [[ "${zones^^}" == ALL ]] && zones="${zone[list]}"
  for zone_id in ${zones}
  do
    echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} zone_state=${zone[${zone_id},state]}"

    for vnet_id in $(
      $ssh ${zone[${zone_id},vip]} "sudo -u oneadmin onevnet list -f BRIDGE~onebr --no-header -l ID 2>/dev/null" |
      tr -d '[:blank:]' |
      sort -n
    )
    do
      json=$($ssh ${zone[${zone_id},vip]} "sudo -u oneadmin onevnet show ${vnet_id} -j 2>/dev/null")
      name=$(jq -r '.VNET.NAME' <<< "${json}")
      type=$(jq -r '.VNET.VN_MAD // ""' <<< "${json}")
      vtep=$(jq -r '.VNET.TEMPLATE.VXLAN_TEP // ""' <<< "${json}")
      [[ "${type}" != "vxlan" ]] && continue
      [[ "${vtep}" == "dev" ]] && result=1
      echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} vnet_id=${vnet_id} vnet_name=${name} vnet_type=${type} vnet_vtep=${vtep}"
    done  # vnet_id

    for vnet_id in $(
      $ssh ${zone[${zone_id},vip]} "sudo -u oneadmin onevntemplate list --no-header -l ID 2>/dev/null" |
      tr -d '[:blank:]' |
      sort -n
      )
    do
      json=$($ssh ${zone[${zone_id},vip]} "sudo -u oneadmin onevntemplate show ${vnet_id} -j 2>/dev/null")
      name=$(jq -r '.VNTEMPLATE.NAME' <<< "${json}")
      type=$(jq -r '.VNTEMPLATE.TEMPLATE.VXLAN_MODE // ""' <<< "${json}")
      vtep=$(jq -r '.VNTEMPLATE.TEMPLATE.VXLAN_TEP // ""' <<< "${json}")
      [[ "${type}" != "evpn" ]] && continue
      [[ "${vtep}" == "dev" ]] && result=1
      echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} vntemplate_id=${vnet_id} vntemplate_name=${name} vntemplate_type=${type} vntemplate_vtep=${vtep}"
    done  # vnet_id
  done  # zone_id

  return ${result}
}  # evpn_vtep_vnet_get



claugine_cli_commands+=( "\
  evpn_vtep_vnet_set
    zones=ID LIST     # ALL for all nodes in each zone
    confirm=yes       # to suppress interactive confirmation"
)
function evpn_vtep_vnet_set() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n zone=${zone_data}
  local zones="${zones:-ALL}"
  local zone_id node
  local vnet_id json name type vtep

  if [[ -z "${zone[@]:-}" ]]
  then
    echo
    echo "!!! ERROR !!! ONE FE zones data ${zone_data} is not initialized"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    echo "to initialize data: fe_data_refresh [data=var name] fe=IP"
    return 1
  fi

  [[ "${zones^^}" == ALL ]] && zones="${zone[list]}"
  for zone_id in ${zones}
  do
    echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} zone_state=${zone[${zone_id},state]}"
    stop_without_confirmation confirm=${confirm:-no} && return 1

    for vnet_id in $(
      $ssh ${zone[${zone_id},vip]} "sudo -u oneadmin onevnet list -f BRIDGE~onebr --no-header -l ID 2>/dev/null" |
      tr -d '[:blank:]' |
      sort -n
    )
    do
      json=$($ssh ${zone[${zone_id},vip]} "sudo -u oneadmin onevnet show ${vnet_id} -j 2>/dev/null")
      name=$(jq -r '.VNET.NAME' <<< "${json}")
      type=$(jq -r '.VNET.VN_MAD // ""' <<< "${json}")
      vtep=$(jq -r '.VNET.TEMPLATE.VXLAN_TEP // ""' <<< "${json}")
      [[ "${type}" != "vxlan" ]] && continue
      echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} vnet_id=${vnet_id} vnet_name=${name} vnet_type=${type} vtep_old=${vtep} vtep_new=local_ip"
      $ssh ${zone[${zone_id},vip]} "sudo -u oneadmin onevnet update ${vnet_id} --append 2>/dev/null" <<< "VXLAN_TEP = \"local_ip\""
    done  # vnet_id

    for vnet_id in $(
      $ssh ${zone[${zone_id},vip]} "sudo -u oneadmin onevntemplate list --no-header -l ID 2>/dev/null" |
      tr -d '[:blank:]' |
      sort -n
      )
    do
      json=$($ssh ${zone[${zone_id},vip]} "sudo -u oneadmin onevntemplate show ${vnet_id} -j 2>/dev/null")
      name=$(jq -r '.VNTEMPLATE.NAME' <<< "${json}")
      type=$(jq -r '.VNTEMPLATE.TEMPLATE.VXLAN_MODE // ""' <<< "${json}")
      vtep=$(jq -r '.VNTEMPLATE.TEMPLATE.VXLAN_TEP // ""' <<< "${json}")
      [[ "${type}" != "evpn" ]] && continue
      echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} vntemplate_id=${vnet_id} vntemplate_name=${name} vntemplate_type=${type} vtep_old=${vtep} vtep_new=local_ip"
      $ssh ${zone[${zone_id},vip]} "sudo -u oneadmin onevntemplate update ${vnet_id} --append 2>/dev/null" <<< "VXLAN_TEP = \"local_ip\""
    done  # vnet_id
  done  # zone_id

  return 0
}  # evpn_vtep_vnet_set



claugine_cli_commands+=( "\
  evpn_vtep_vnm_patch
    zones=ID LIST      # ALL for all nodes in each zone
    confirm=yes        # to suppress interactive confirmation"
)
function evpn_vtep_vnm_patch() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n zone=${zone_data}
  local zones="${zones:-ALL}"
  local zone_id node file

  if [[ -z "${zone[@]:-}" ]]
  then
    echo
    echo "!!! ERROR !!! ONE FE zones data ${zone_data} is not initialized"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    echo "to initialize data: fe_data_refresh [data=var name] fe=IP"
    return 1
  fi

  [[ "${zones^^}" == ALL ]] && zones="${zone[list]}"
  for zone_id in ${zones}
  do
    echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} zone_state=${zone[${zone_id},state]}"
    stop_without_confirmation confirm=${confirm:-no} && return 1

    echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} zone_state=${zone[${zone_id},state]} action=vnm_evpn_vtep_vnm_patch_prechecks"
    if evpn_vtep_vnet_get zones=${zone_id}
    then
      echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} zone_state=${zone[${zone_id},state]} action=vnm_evpn_vtep_vnm_patch_prechecks status=ok"
    else
      echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} zone_state=${zone[${zone_id},state]} action=vnm_evpn_vtep_vnm_patch_prechecks status=failed"
      return 1
    fi

    for node in "${FE_NODE_ROLES[@]}"
    do
      echo -n "zone_id=${zone_id} node_role=${node} node_name=${zone[${zone_id},${node},name]} node_ip=${zone[${zone_id},${node},ip]} action=vnm_evpn_vtep_vnm_patch status="
      if ! $ssh ${zone[${zone_id},${node},ip]} "sudo test -e /var/lib/one/remotes/vnm/vxlan/vxlan.rb.original"
      then
        echo required
        $ssh ${zone[${zone_id},${node},ip]} "
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
        $ssh ${zone[${zone_id},${node},ip]} "
          echo ${file}
          sudo grep 'vxlan_tep' ${file}
        "
      done  # file
    done  # node

    $ssh ${zone[${zone_id},vip]} "sudo -u oneadmin onehost sync --force"
  done  # zone_id

  return 0
}  # evpn_vtep_vnm_patch



claugine_cli_commands+=( "\
  evpn_vtep_vnm_unpatch
    zones=LIST           # zones ids
    confirm=yes          # to suppress interactive confirmation"
)
function evpn_vtep_vnm_unpatch() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n zone=${zone_data}
  local zones="${zones:-ALL}"
  local zone_id node file

  if [[ -z "${zone[@]:-}" ]]
  then
    echo
    echo "!!! ERROR !!! ONE FE zones data ${zone_data} is not initialized"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    echo "to initialize data: fe_data_refresh [data=var name] fe=IP"
    return 1
  fi

  [[ "${zones^^}" == ALL ]] && zones="${zone[list]}"
  for zone_id in ${zones}
  do
    echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} zone_state=${zone[${zone_id},state]} action=vnm_evpn_vtep_vnm_unpatch_prechecks"
    stop_without_confirmation confirm=${confirm:-no} && return 1

    if evpn_vtep_vnet_get zones=${zone_id} && evpn_vtep_nic_get zones=${zone_id}
    then
      echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} zone_state=${zone[${zone_id},state]} action=vnm_evpn_vtep_vnm_unpatch_prechecks status=ok"
    else
      echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} zone_state=${zone[${zone_id},state]} action=vnm_evpn_vtep_vnm_unpatch_prechecks status=failed"
      return 1
    fi

    for node in "${FE_NODE_ROLES[@]}"
    do
      echo -n "zone_id=${zone_id} node_role=${node} node_name=${zone[${zone_id},${node},name]} node_ip=${zone[${zone_id},${node},ip]} action=vnm_evpn_vtep_vnm_unpatch status="
      if $ssh ${zone[${zone_id},${node},ip]} "sudo test -e /var/lib/one/remotes/vnm/vxlan/vxlan.rb.original"
      then
        echo required
        $ssh ${zone[${zone_id},${node},ip]} "
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
        $ssh ${zone[${zone_id},${node},ip]} "
          echo ${file}
          sudo grep 'vxlan_tep' ${file}
        "
      done  # file
    done  # node

    $ssh ${zone[${zone_id},vip]} "sudo -u oneadmin onehost sync --force"
  done  # zone_id

  return 0
}  # evpn_vtep_vnm_unpatch
