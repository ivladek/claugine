#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



# INDEX
#   acl_role_rights_get  show rights set in oned.conf for each VM right - USE MANAGE ADMIN
#   acl_role_rights_set  set rights set in oned.conf for each VM right - USE MANAGE ADMIN
#   acl_tenant_get       show ACL for tenant group and group admin
#   acl_tenant_set       delete than set new ACLs for all tenants



help_data[acl_role_rights_get]="\
  acl_role_rights_get        # show rights set in oned.conf for each VM right - USE MANAGE ADMIN
    platform=NAME            #   platforms.<site>.<platform>"
# return 0 - done
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
function acl_role_rights_get() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local node op_set vip

  data_runtime_refresh platform="${platform:-}" fe=yes || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)

  _log_std \
    zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" \
    action="acl_role_rights_get"

  for node in $(inv_list var=CONFIG path=onefe.nodes_order.direct)
  do
    _log_fe
    $ssh ${RUNTIME[${platform},${node},ip]} "sudo sed -n '
      /^VM_ADMIN_OPERATIONS[[:space:]]*=/p;
      /^VM_MANAGE_OPERATIONS[[:space:]]*=/p;
      /^VM_USE_OPERATIONS[[:space:]]*=/p
    ' /etc/one/oned.conf"
  done  # node

  return 0
}  # acl_role_rights_get



help_data[acl_role_rights_set]="\
  acl_role_rights_set        # set rights set in oned.conf for each VM right - USE MANAGE ADMIN
    platform=NAME            #   platforms.<site>.<platform>
    confirm=yes|NO           #   to suppress interactive confirmation"
# return 0 - done
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
function acl_role_rights_set() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local node op_set vip

  data_runtime_refresh platform="${platform:-}" fe=yes || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)

  _log_std \
    zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" \
    action="acl_role_rights_set"
  _stop_without_confirmation confirm=${confirm:-no} && return 1

  for node in $(inv_list var=CONFIG path=onefe.nodes_order.direct)
  do
    for op_set in $(inv_keys var=CONFIG path=onefe.configs.oned.vm_operations)
    do
      _log_fe \
        action=acl_role_rights_patch \
        VM_${op_set}="[$(inv_value var=CONFIG path=onefe.configs.oned.vm_operations.${op_set})]"
      $ssh ${RUNTIME[${platform},${node},ip]} "sudo sed -Ezi '
        s|VM_${op_set}_OPERATIONS[[:space:]]*=[[:space:]]*\"[^\"]*\"|VM_${op_set}_OPERATIONS = \"$(inv_value var=CONFIG path=onefe.configs.oned.vm_operations.${op_set})\"|
      ' /etc/one/oned.conf"
    done  # op_set
  done  # node

  return 0
}  # acl_role_rights_set



help_data[acl_tenant_get]="\
  acl_tenant_get             # show ACL for tenant group and group admin
    platform=NAME            #   platforms.<site>.<platform>: any platform of the federation,
                             #     the ACLs are read on its primary platform
    tenants=LIST             #   tenants ids or names"
# return 0 - done
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
function acl_tenant_get() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local primary
  local tenants="${tenants:-ALL}"
  local -a tenants_list=()
  local json tenant group_id group_name admin_id admin_name vip

  # switch to the primary FE if the corresponding platform is a secondary
  primary=$(inv_value var=INV path=${platform:-}.fe.primary)
  if [[ -n "${primary}" ]]
  then
    _log platform="${platform}" primary="${primary%.fe}" action="switch_to_primary"
    platform="${primary%.fe}"
  fi
  data_runtime_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)

  if [[ "${tenants}" == ALL ]]
  then
    mapfile -t tenants_list < <(
      $ssh ${vip} "sudo -u oneadmin onegroup list --no-header -l ID 2>/dev/null" |
      tr -d '[:blank:]' |
      sort -n
    )
  else
    for tenant in ${tenants}
    do
      if [[ "${tenant}" =~ ^[0-9]+$ ]]
      then
        mapfile -t -O "${#tenants_list[@]}" tenants_list < <(
          $ssh ${vip} "sudo -u oneadmin onegroup list -f ID=\"${tenant}\" --no-header -l ID 2>/dev/null"
        )
      elif [[ "${tenant}" == tenant-* ]]
      then
        mapfile -t -O "${#tenants_list[@]}" tenants_list < <(
          $ssh ${vip} "sudo -u oneadmin onegroup list -f NAME=\"${tenant}\" --no-header -l ID 2>/dev/null"
        )
      else
        mapfile -t -O "${#tenants_list[@]}" tenants_list < <(
          $ssh ${vip} "sudo -u oneadmin onegroup list -f NAME~\"${tenant}\" --no-header -l ID 2>/dev/null"
        )
      fi
    done  # tenant
  fi

  _log_std \
    zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" \
    action="acl_tenant_get"

  for group_id in ${tenants_list[@]}
  do
    (( group_id < 100 )) || [[ "${group_id}" == "${RUNTIME[${platform},shared]:-}" ]] && continue
    json=$($ssh ${vip} "sudo -u oneadmin onegroup show ${group_id} -j 2>/dev/null")
    group_name=$(jq -r '.GROUP.NAME' <<< "${json}")
    tenant="${group_name#*-}"
    admin_id=$(jq -r '
      .GROUP.ADMINS.ID
      | if type == "array"
        then .[0]
        else .
        end
    ' <<< "${json}")
    admin_name=$(
      $ssh ${vip} "sudo -u oneadmin oneuser show ${admin_id} -j 2>/dev/null" |
      jq -r '.USER.NAME'
    )

    _log_std \
      tenant="${tenant}" \
      group_name="${group_name}" \
      group_id="${group_id}" \
      admin_name="${admin_name}" \
      admin_id="${admin_id}"

    $ssh ${vip} "sudo -u oneadmin oneacl list -f USER='-1' 2>/dev/null"
    {
      $ssh ${vip} "sudo -u oneadmin oneacl list -f USER='@${group_id}' --no-header 2>/dev/null"
      $ssh ${vip} "sudo -u oneadmin oneacl list -f USER='#${admin_id}' --no-header 2>/dev/null"
    } | sort -nr
  done  # group_id

  return 0
}  # acl_tenant_get



help_data[acl_tenant_set]="\
  acl_tenant_set             # delete than set new ACLs for all tenants
    platform=NAME            #   platforms.<site>.<platform>: any platform of the federation,
                             #     the ACLs are set on its primary platform for every zone of the federation
    tenants=LIST             #   tenants ids or names
    dry=YES|no               #   dry run without deleteing and creating ACLs
    confirm=yes|NO           #   to suppress interactive confirmation"
# return 0 - done
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            not confirmed
function acl_tenant_set() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local primary member
  local dry="${dry:-yes}"
  local tenants="${tenants:-ALL}"
  local -a tenants_list=()
  local zones zone_id cluster_id acl acl_id acl_subj acl_obj acl_ops acl_zone
  local -a acls
  local json tenant group_id group_name admin_id admin_name vip
  local -A clusters

  # switch to the primary FE if the corresponding platform is a secondary
  primary=$(inv_value var=INV path=${platform:-}.fe.primary)
  if [[ -n "${primary}" ]]
  then
    _log platform="${platform}" primary="${primary%.fe}" action="switch_to_primary"
    platform="${primary%.fe}"
  fi
  data_runtime_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)

  if [[ "${tenants}" == ALL ]]
  then
    mapfile -t tenants_list < <(
      $ssh ${vip} "sudo -u oneadmin onegroup list --no-header -l ID 2>/dev/null" |
      tr -d '[:blank:]' |
      sort -n
    )
  else
    for tenant in ${tenants}
    do
      if [[ "${tenant}" =~ ^[0-9]+$ ]]
      then
        mapfile -t -O "${#tenants_list[@]}" tenants_list < <(
          $ssh ${vip} "sudo -u oneadmin onegroup list -f ID=\"${tenant}\" --no-header -l ID 2>/dev/null"
        )
      elif [[ "${tenant}" == tenant-* ]]
      then
        mapfile -t -O "${#tenants_list[@]}" tenants_list < <(
          $ssh ${vip} "sudo -u oneadmin onegroup list -f NAME=\"${tenant}\" --no-header -l ID 2>/dev/null"
        )
      else
        mapfile -t -O "${#tenants_list[@]}" tenants_list < <(
          $ssh ${vip} "sudo -u oneadmin onegroup list -f NAME~\"${tenant}\" --no-header -l ID 2>/dev/null"
        )
      fi
    done  # tenant
  fi

  _log_std \
    zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" \
    secondaries="[$(inv_list var=INV path=${platform}.fe.secondaries | xargs)]" \
    action="acl_tenant_set"

  for group_id in ${tenants_list[@]}
  do
    (( group_id < 100 )) || [[ "${group_id}" == "${RUNTIME[${platform},shared]:-}" ]] && continue
    [[ "${dry}" == "no" ]] && _stop_without_confirmation confirm=${confirm:-no} && return 1

    json=$($ssh ${vip} "sudo -u oneadmin onegroup show ${group_id} -j 2>/dev/null")
    group_name=$(jq -r '.GROUP.NAME' <<< "${json}")
    tenant="${group_name#*-}"
    admin_id=$(jq -r '
      .GROUP.ADMINS.ID
      | if type == "array"
        then .[0]
        else .
        end
    ' <<< "${json}")
    admin_name=$(
      $ssh ${vip} "sudo -u oneadmin oneuser show ${admin_id} -j 2>/dev/null" |
      jq -r '.USER.NAME'
    )

    zones=""
    clusters=()
    while read -r cluster_id zone_id
    do
      [[ " ${zones} " != *" ${zone_id} "* ]] && zones+="${zones:+ }${zone_id}"
      clusters[${zone_id}]+="${clusters[${zone_id}]:+ }${cluster_id}"
    done < <(
      $ssh ${vip} "sudo -u oneadmin oneacl list -f USER=@${group_id} --no-header 2>/dev/null" |
      awk '$3 ~ /^-H/ {
        sub(/^%/, "", $4)
        sub(/^#/, "", $6)
        print $4, $6
      }'
    )

    _log_std tenant="${tenant}" group_id="${group_id}" acl="current"
    $ssh ${vip} "sudo -u oneadmin oneacl list -f USER=@${group_id} 2>/dev/null"
    $ssh ${vip} "sudo -u oneadmin oneacl list -f USER=#${admin_id} --no-header 2>/dev/null"

    if [[ "${dry}" == "no" ]]
    then
      _log_std -n tenant="${tenant}" group_id="${group_id}" acl_delete="["
      for acl_id in $(
        $ssh ${vip} "sudo -u oneadmin oneacl list -f USER=@${group_id} --no-header -l ID 2>/dev/null"
        $ssh ${vip} "sudo -u oneadmin oneacl list -f USER=#${admin_id} --no-header -l ID 2>/dev/null"
      )
      do
        _log -n " ${acl_id}"
        $ssh ${vip} "sudo -u oneadmin oneacl delete ${acl_id} &>/dev/null"
      done  # acl_id
      _log " ]"
    else
      _log_std tenant="${tenant}" group_id="${group_id}" acl_delete="[ dry run: no ACLs will be deleted ]"
    fi

    # grant rights to tenant admin for users management
    acls=(
      "#${admin_id} GROUP/#${group_id} USE+MANAGE       *"
      "#${admin_id} USER/@${group_id}  USE+MANAGE+ADMIN *"
      "#${admin_id} USER/*             CREATE           *"
    )
    # every zone of the tenant: the primary and its fe.secondaries - all of them platforms, checked by data_load_provider
    for member in ${platform} $(inv_list var=INV path=${platform}.fe.secondaries)
    do
      member="${member%.fe}"
      zone_id="${RUNTIME[${member},id]}"
      [[ " ${zones} " == *" ${zone_id} "* ]] || continue   # the tenant has no cluster in this zone
      # grant rights to tenant for cluster and cluster resources
      acls+=( "@${group_id} ZONE/#${zone_id}           USE    #${zone_id}" )
      for cluster_id in ${clusters[${zone_id}]}
      do
        acls+=( "@${group_id} CLUSTER/#${cluster_id}   USE    #${zone_id}" )
        acls+=( "@${group_id} HOST/%${cluster_id}      MANAGE #${zone_id}" )
        acls+=( "@${group_id} DATASTORE/%${cluster_id} USE    #${zone_id}" )
      done  # cluster_id

      # grant rights to tenant for using shared and tenant resources
      [[ -n "${RUNTIME[${platform},shared]:-}" ]] && acls+=( "@${group_id} IMAGE+TEMPLATE/@${RUNTIME[${platform},shared]} USE    #${zone_id}" )
      acls+=( "@${group_id} NET+IMAGE+TEMPLATE+DOCUMENT+SECGROUP/@${group_id}     USE    #${zone_id}" )
      acls+=( "@${group_id} VM+DOCUMENT/*                                         CREATE #${zone_id}" )

      # grant rights to tenant admin for tenant resources management
      acls+=( "#${admin_id} MARKETPLACEAPP/*                                                       USE              #${zone_id}" )
      acls+=( "#${admin_id} VNTEMPLATE/#${RUNTIME[${member},vntemplate,id]}                  USE              #${zone_id}" )
      acls+=( "#${admin_id} VM+NET+IMAGE+TEMPLATE+DOCUMENT+SECGROUP+VROUTER+BACKUPJOB/@${group_id} USE+MANAGE       #${zone_id}" )
      acls+=( "#${admin_id} VM+NET+IMAGE+TEMPLATE+DOCUMENT+SECGROUP+VROUTER+BACKUPJOB/*            CREATE           #${zone_id}" )
    done  # member

    if [[ "${dry}" == "no" ]]
    then
      _log_std -n tenant="${tenant}" group_id="${group_id}" acl_create="["
    else
      _log_std tenant="${tenant}" group_id="${group_id}" acl_create="[ dry run: no ACLs will be created ]"
    fi

    for acl in "${acls[@]}"
    do
      if [[ "${dry}" == "no" ]]
      then
        acl_id=$($ssh ${vip} "sudo -u oneadmin oneacl create \"${acl}\" 2>/dev/null")
        _log -n " ${acl_id}"
      else
        read -r acl_subj acl_obj acl_ops acl_zone <<< "${acl}"
        _log "$(printf '%-6s %-64s %-17s %s\n' "${acl_subj}" "${acl_obj}" "${acl_ops}" "${acl_zone}")"
      fi
    done  # acl

    if [[ "${dry}" == "no" ]]
    then
      _log " ]"
      _log_std tenant="${tenant}" group_id="${group_id}" acl="new"
      $ssh ${vip} "sudo -u oneadmin oneacl list -f USER=@${group_id}             2>/dev/null"
      $ssh ${vip} "sudo -u oneadmin oneacl list -f USER=#${admin_id} --no-header 2>/dev/null"
    fi
  done  # group_id
}  # acl_tenant_set
