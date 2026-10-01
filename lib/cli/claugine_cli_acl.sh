#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



claugine_cli_commands+=( "\
  acl_role_rights_set  # set rights set in oned.conf for each VM right - USE MANAGE ADMIN
    confirm=yes        #   to suppress interactive confirmation"
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
    echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} zone_state=${zone[${zone_id},state]}"
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
  acl_role_rights_get  # show rights set in oned.conf for each VM right - USE MANAGE ADMIN
  acl_role_rights_get"
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
    echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} zone_state=${zone[${zone_id},state]}"

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
  acl_tenant_get    # show ACL for tenant group and group admin
  acl_tenant_get
    tenant=STRING"
)
function acl_tenant_get() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n zone=${zone_data}
  local zone_id=0
  local tenant="${tenant:-}"
  local json group_id group_name admin_id admin_name

  if [[ -z "${zone[@]:-}" ]]
  then
    echo
    echo "!!! ERROR !!! ONE FE zones data ${zone_data} is not initialized"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    echo "to initialize data: fe_data_refresh [data=var name] fe=IP"
    return 1
  fi

  echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} zone_state=${zone[${zone_id},state]}"

  group_name="tenant-${tenant}"
  json=$($ssh ${zone[${zone_id},vip]} "sudo -u oneadmin onegroup show \"${group_name}\" -j 2>/dev/null")
  [[ -z "${json}" ]] && return 1

  group_id=$(jq -r '.GROUP.ID' <<< "${json}")
  admin_id=$(
    jq -r '
      .GROUP.ADMINS.ID
      | if type == "array"
        then .[0]
        else .
        end // ""
    ' <<< "${json}"
  )
  admin_name=$(
    $ssh ${zone[${zone_id},vip]} "sudo -u oneadmin oneuser show ${admin_id} -j 2>/dev/null" |
    jq -r '.USER.NAME // ""'
  )

  echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} tenant=${tenant} group_name=${group_name} group_id=${group_id} admin_name=${admin_name} admin_id=${admin_id}"

  $ssh ${zone[${zone_id},vip]} "sudo -u oneadmin oneacl list -f USER='-1' 2>/dev/null"
  {
    $ssh ${zone[${zone_id},vip]} "sudo -u oneadmin oneacl list -f USER='@${group_id}' --no-header 2>/dev/null"
    $ssh ${zone[${zone_id},vip]} "sudo -u oneadmin oneacl list -f USER='#${admin_id}' --no-header 2>/dev/null"
   } | sort -nr

  return 0
}  # acl_tenant_get
