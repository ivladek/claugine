#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"


claugine_cli_commands+=( "\
  acl_role_rights_set        # set rights set in oned.conf for each VM right - USE MANAGE ADMIN
    confirm=yes|NO           #   to suppress interactive confirmation"
)
function acl_role_rights_set() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n zone=${zone_data}
  local zone_id node op_set

  if [[ -z "${zone[@]:-}" ]]
  then
    echo
    echo "!!! ERROR !!! ONE FE zones data ${zone_data} is not initialized"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    echo "to initialize data: fe_data_refresh [data=var name] fe=IP"
    return 1
  fi

  for zone_id in ${zone[list]}
  do
    echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} zone_state=${zone[${zone_id},state]} action=acl_role_rights_set"
    stop_without_confirmation confirm=${confirm:-no} && return 1

    for node in "${FE_NODE_ROLES[@]}"
    do
      for op_set in "${!VM_OPERATIONS[@]}"
      do
        echo "zone_id=${zone_id} node_role=${node} node_name=${zone[${zone_id},${node},name]} node_ip=${zone[${zone_id},${node},ip]} action=acl_role_rights_patch VM_${op_set}=[${VM_OPERATIONS[${op_set}]}]"
        $ssh ${zone[${zone_id},${node},ip]} "sudo sed -Ezi '
          s|VM_${op_set}_OPERATIONS[[:space:]]*=[[:space:]]*\"[^\"]*\"|VM_${op_set}_OPERATIONS = \"${VM_OPERATIONS[${op_set}]}\"|
        ' /etc/one/oned.conf"
      done  # op_set
    done  # node
  done # zone_id

  return 0
}  # acl_role_rights_set



claugine_cli_commands+=( "\
  acl_role_rights_get        # show rights set in oned.conf for each VM right - USE MANAGE ADMIN"
)
function acl_role_rights_get() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n zone=${zone_data}
  local zone_id node op_set

  if [[ -z "${zone[@]:-}" ]]
  then
    echo
    echo "!!! ERROR !!! ONE FE zones data ${zone_data} is not initialized"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    echo "to initialize data: fe_data_refresh [data=var name] fe=IP"
    return 1
  fi

  for zone_id in ${zone[list]}
  do
    echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} zone_state=${zone[${zone_id},state]} action=acl_role_rights_get"

    for node in "${FE_NODE_ROLES[@]}"
    do
      echo "zone_id=${zone_id} node_role=${node} node_name=${zone[${zone_id},${node},name]} node_ip=${zone[${zone_id},${node},ip]}"
      $ssh ${zone[${zone_id},${node},ip]} "sudo sed -n '
        /^VM_ADMIN_OPERATIONS[[:space:]]*=/p;
        /^VM_MANAGE_OPERATIONS[[:space:]]*=/p;
        /^VM_USE_OPERATIONS[[:space:]]*=/p
      ' /etc/one/oned.conf"
    done  # node
  done  # zone_id

  return 0
}  # acl_role_rights_get



claugine_cli_commands+=( "\
  acl_tenant_get             # show ACL for tenant group and group admin
    tenants=LIST             #   tenants ids or names"
)
function acl_tenant_get() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n zone=${zone_data}
  local tenants="${tenants:-ALL}"
  local -a tenants_list=()
  local zone_id
  local json tenant group_id group_name admin_id admin_name

  if [[ -z "${zone[@]:-}" ]]
  then
    echo
    echo "!!! ERROR !!! ONE FE zones data ${zone_data} is not initialized"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    echo "to initialize data: fe_data_refresh [data=var name] fe=IP"
    return 1
  fi

  if [[ "${tenants}" == ALL ]]
  then
    mapfile -t tenants_list < <(
      $ssh ${zone[0,vip]} "sudo -u oneadmin onegroup list --no-header -l ID 2>/dev/null" |
      tr -d '[:blank:]' |
      sort -n
    )
  else
    for tenant in ${tenants}
    do
      if [[ "${tenant}" =~ ^[0-9]+$ ]]
      then
        mapfile -t -O "${#tenants_list[@]}" tenants_list < <(
          $ssh ${zone[0,vip]} "sudo -u oneadmin onegroup list -f ID=\"${tenant}\" --no-header -l ID 2>/dev/null"
        )
      elif [[ "${tenant}" == tenant-* ]]
      then
        mapfile -t -O "${#tenants_list[@]}" tenants_list < <(
          $ssh ${zone[0,vip]} "sudo -u oneadmin onegroup list -f NAME=\"${tenant}\" --no-header -l ID 2>/dev/null"
        )
      else
        mapfile -t -O "${#tenants_list[@]}" tenants_list < <(
          $ssh ${zone[0,vip]} "sudo -u oneadmin onegroup list -f NAME~\"${tenant}\" --no-header -l ID 2>/dev/null"
        )
      fi
    done  # tenant
  fi

  echo "zone_id=0 zone_name=${zone[0,name]} zone_vip=${zone[0,vip]} zone_state=${zone[0,state]} action=acl_tenant_get"

  for group_id in ${tenants_list[@]}
  do
    json=$($ssh ${zone[0,vip]} "sudo -u oneadmin onegroup show ${group_id} -j 2>/dev/null")
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
      $ssh ${zone[0,vip]} "sudo -u oneadmin oneuser show ${admin_id} -j 2>/dev/null" |
      jq -r '.USER.NAME'
    )

    echo "zone_id=0 zone_name=${zone[0,name]} zone_vip=${zone[0,vip]} tenant=${tenant} group_name=${group_name} group_id=${group_id} admin_name=${admin_name} admin_id=${admin_id}"

    $ssh ${zone[0,vip]} "sudo -u oneadmin oneacl list -f USER='-1' 2>/dev/null"
    {
      $ssh ${zone[0,vip]} "sudo -u oneadmin oneacl list -f USER='@${group_id}' --no-header 2>/dev/null"
      $ssh ${zone[0,vip]} "sudo -u oneadmin oneacl list -f USER='#${admin_id}' --no-header 2>/dev/null"
    } | sort -nr
  done  # group_id

  return 0
}  # acl_tenant_get



claugine_cli_commands+=( "\
  acl_tenant_set             # delete than set new ACLs for all tenants
    tenants=LIST             #   tenants ids or names
    dry=yes|NO               #   dry run without deleteing and creating ACLs
    confirm=yes|NO           #   to suppress interactive confirmation"
)
function acl_tenant_set() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n zone=${zone_data}
  local dry="${dry:-no}"
  local tenants="${tenants:-ALL}"
  local -a tenants_list=()
  local zones zone_id acl acl_id acl_subj acl_obj acl_ops acl_zone
  local -a acls
  local json tenant group_id group_name admin_id admin_name
  local -A clusters

  if [[ -z "${zone[@]:-}" ]]
  then
    echo
    echo "!!! ERROR !!! ONE FE zones data ${zone_data} is not initialized"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    echo "to initialize data: fe_data_refresh [data=var name] fe=IP"
    return 1
  fi

  if [[ "${tenants}" == ALL ]]
  then
    mapfile -t tenants_list < <(
      $ssh ${zone[0,vip]} "sudo -u oneadmin onegroup list --no-header -l ID 2>/dev/null" |
      tr -d '[:blank:]' |
      sort -n
    )
  else
    for tenant in ${tenants}
    do
      if [[ "${tenant}" =~ ^[0-9]+$ ]]
      then
        mapfile -t -O "${#tenants_list[@]}" tenants_list < <(
          $ssh ${zone[0,vip]} "sudo -u oneadmin onegroup list -f ID=\"${tenant}\" --no-header -l ID 2>/dev/null"
        )
      elif [[ "${tenant}" == tenant-* ]]
      then
        mapfile -t -O "${#tenants_list[@]}" tenants_list < <(
          $ssh ${zone[0,vip]} "sudo -u oneadmin onegroup list -f NAME=\"${tenant}\" --no-header -l ID 2>/dev/null"
        )
      else
        mapfile -t -O "${#tenants_list[@]}" tenants_list < <(
          $ssh ${zone[0,vip]} "sudo -u oneadmin onegroup list -f NAME~\"${tenant}\" --no-header -l ID 2>/dev/null"
        )
      fi
    done  # tenant
  fi

  echo "zone_id=0 zone_name=${zone[0,name]} zone_vip=${zone[0,vip]} zone_state=${zone[0,state]} action=acl_tenant_set"
  [[ "${dry}" == "no" ]] && stop_without_confirmation confirm=${confirm:-no} && return 1

  for group_id in ${tenants_list[@]}
  do
    (( group_id < 100 || group_id == SHARED_ID[${zone[0,vip]}] )) && continue

    json=$($ssh ${zone[0,vip]} "sudo -u oneadmin onegroup show ${group_id} -j 2>/dev/null")
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
      $ssh ${zone[0,vip]} "sudo -u oneadmin oneuser show ${admin_id} -j 2>/dev/null" |
      jq -r '.USER.NAME'
    )

    zones=""
    clusters=()
    while read -r cluster_id zone_id
    do
      [[ " ${zones} " != *" ${zone_id} "* ]] && zones+=" ${zone_id}"
      clusters[${zone_id}]+=" ${cluster_id}"
    done < <(
      $ssh ${zone[0,vip]} "sudo -u oneadmin oneacl list -f USER=@${group_id} --no-header 2>/dev/null" |
      awk '$3 ~ /^-H/ {
        sub(/^%/, "", $4)
        sub(/^#/, "", $6)
        print $4, $6
      }'
    )

    echo "zone_id=0 zone_name=${zone[0,name]} zone_vip=${zone[0,vip]} tenant=${tenant} group_id=${group_id} acl=current"
    $ssh ${zone[0,vip]} "sudo -u oneadmin oneacl list -f USER=@${group_id} 2>/dev/null"
    $ssh ${zone[0,vip]} "sudo -u oneadmin oneacl list -f USER=#${admin_id} --no-header 2>/dev/null"

    echo -n "zone_id=0 zone_name=${zone[0,name]} zone_vip=${zone[0,vip]} tenant=${tenant} group_id=${group_id} acl_delete=["
    if [[ "${dry}" == "no" ]]
    then
      for acl_id in $(
        $ssh ${zone[0,vip]} "sudo -u oneadmin oneacl list -f USER=@${group_id} --no-header -l ID 2>/dev/null"
        $ssh ${zone[0,vip]} "sudo -u oneadmin oneacl list -f USER=#${admin_id} --no-header -l ID 2>/dev/null"
      )
      do
        echo -n " ${acl_id}"
        $ssh ${zone[0,vip]} "sudo -u oneadmin oneacl delete ${acl_id} &>/dev/null"
      done  # acl_id
    else
      echo -n " dry run: no ACLs will be deleted"
    fi
    echo " ]"

    # grant rights to tenant admin for users management
    acls=(
      "#${admin_id} GROUP/#${group_id} USE+MANAGE       *"
      "#${admin_id} USER/@${group_id}  USE+MANAGE+ADMIN *"
      "#${admin_id} USER/*             CREATE           *"
    )
    for zone_id in ${zones}
    do
      # grant rights to tenant for cluster and cluster resources
      acls+=( "@${group_id} ZONE/#${zone_id}           USE    #${zone_id}" )
      for cluster_id in ${clusters[${zone_id}]}
      do
        acls+=( "@${group_id} CLUSTER/#${cluster_id}   USE    #${zone_id}" )
        acls+=( "@${group_id} HOST/%${cluster_id}      MANAGE #${zone_id}" )
        acls+=( "@${group_id} DATASTORE/%${cluster_id} USE    #${zone_id}" )
      done  # cluster_id

      # grant rights to tenant for using shared and tenant resources
      acls+=( "@${group_id} IMAGE+TEMPLATE/@${SHARED_ID[${zone[${zone_id},vip]}]} USE    #${zone_id}" )
      acls+=( "@${group_id} NET+IMAGE+TEMPLATE+DOCUMENT+SECGROUP/@${group_id}     USE    #${zone_id}" )
      acls+=( "@${group_id} VM+DOCUMENT/*                                         CREATE #${zone_id}" )

      # grant rights to tenant admin for tenant resources management
      acls+=( "#${admin_id} MARKETPLACEAPP/*                                                       USE              #${zone_id}" )
      acls+=( "#${admin_id} VNTEMPLATE/#${VNTEMPLATE_ID[${zone[${zone_id},vip]}]}                  USE              #${zone_id}" )
      acls+=( "#${admin_id} VM+NET+IMAGE+TEMPLATE+DOCUMENT+SECGROUP+VROUTER+BACKUPJOB/@${group_id} USE+MANAGE       #${zone_id}" )
      acls+=( "#${admin_id} VM+NET+IMAGE+TEMPLATE+DOCUMENT+SECGROUP+VROUTER+BACKUPJOB/*            CREATE           #${zone_id}" )
    done  # zone_id

    echo -n "zone_id=0 zone_name=${zone[0,name]} zone_vip=${zone[0,vip]} tenant=${tenant} group_id=${group_id} acl_create=["
    if [[ "${dry}" == "no" ]]
    then
      echo -n "["
    else
      echo " dry run: no ACLs will be created ]"
    fi

    for acl in "${acls[@]}"
    do
      if [[ "${dry}" == "no" ]]
      then
        acl_id=$($ssh ${zone[0,vip]} "sudo -u oneadmin oneacl create \"${acl}\" 2>/dev/null")
        echo -n " ${acl_id}"
      else
        read -r acl_subj acl_obj acl_ops acl_zone <<< "${acl}"
        printf '%-6s %-64s %-17s %s\n' "${acl_subj}" "${acl_obj}" "${acl_ops}" "${acl_zone}"
      fi
    done  # acl

    if [[ "${dry}" == "no" ]]
    then
      echo " ]"
      echo "zone_id=0 zone_name=${zone[0,name]} zone_vip=${zone[0,vip]} tenant=${tenant} group_id=${group_id} acl=new"
      $ssh ${zone[0,vip]} "sudo -u oneadmin oneacl list -f USER=@${group_id}             2>/dev/null"
      $ssh ${zone[0,vip]} "sudo -u oneadmin oneacl list -f USER=#${admin_id} --no-header 2>/dev/null"
    fi
  done  # group_id
}  # acl_tenant_set
