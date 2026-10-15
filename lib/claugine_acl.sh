#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



# INDEX
#   acl_role_rights_get  show rights set in oned.conf for each VM right - USE MANAGE ADMIN
#   acl_role_rights_set  set rights set in oned.conf for each VM right - USE MANAGE ADMIN
#   acl_tenant_get       show the quota records and the ACLs of the tenants
#   acl_tenant_set       delete than set new ACLs for all tenants by their quota records



help_data[acl_role_rights_get]="\
  acl_role_rights_get        # show rights set in oned.conf for each VM right - USE MANAGE ADMIN
    platform=NAME            #   platforms.<site>.<platform>"
function acl_role_rights_get() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local node op_set vip

  data_platform_refresh platform="${platform:-}" fe=yes || return 1
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
function acl_role_rights_set() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local node op_set vip

  data_platform_refresh platform="${platform:-}" fe=yes || return 1
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
  acl_tenant_get             # show the quota records and the ACLs of the tenants
    platform=NAME            #   platforms.<site>.<platform>: any platform of the federation,
                             #     the ACLs are read on its primary platform
    tenants=LIST             #   tenants names or group ids
                             #   the quota records of every zone with their clusters - QUOTA, see data_platform_refresh quota=;
                             #   the ACLs of the tenant group, its role groups sa-, power-, user-, ro-<name>, its admin"
function acl_tenant_get() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local primary vip json group_id tenant role subject admin_id member qr
  local -a subjects=()

  # switch to the primary FE if the corresponding platform is a secondary
  if primary=$(inv_value var=INV path=${platform:-}.fe.primary)
  then
    _log platform="${platform}" primary="${primary}" action="switch_to_primary"
    platform="${primary}"
  fi
  data_platform_refresh platform="${platform:-}" quota=load || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)

  _log_std \
    zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" \
    action="acl_tenant_get"

  for tenant in $(_get_tenants platform="${platform}" tenants="${tenants:-ALL}")
  do
    _log
    group_id=""
    for member in ${platform} $(inv_list var=INV path=${platform}.fe.secondaries)
    do
      if [[ "${RUNTIME[${member},available]:-}" != "yes" ]]
      then
        _log_std tenant="${tenant}" platform="${member}" warning="unavailable - not shown"
        continue
      fi
      [[ -n "${QUOTA[${tenant},group_id]:-}" ]] || continue
      group_id="${QUOTA[${tenant},group_id]}"
      for qr in ${QUOTA[${tenant},${member},qrs]}
      do
        _log_std \
          tenant="${tenant}" \
          zone_id="${RUNTIME[${member},id]}" \
          qr="${qr}" \
          clusters="[${QUOTA[${tenant},${member},${qr},clusters]}]"
      done  # qr
    done  # member

    json=$($ssh ${vip} "sudo -u oneadmin onegroup show ${group_id} -j 2>/dev/null")
    admin_id=$(jq -r '.GROUP.ADMINS.ID | if type == "array" then .[0] else . end // ""' <<< "${json}")
    subjects=( "@${group_id}" )
    for role in sa power user ro
    do
      subjects+=( "@$($ssh ${vip} "sudo -u oneadmin onegroup show ${role}-${tenant} -j 2>/dev/null" | jq -r '.GROUP.ID // ""')" )
    done  # role
    [[ -n "${admin_id}" ]] && subjects+=( "#${admin_id}" )

    _log_std tenant="${tenant}" group_id="${group_id}" admin_id="${admin_id}" acl_users="[${subjects[*]}]"
    $ssh ${vip} "sudo -u oneadmin oneacl list 2>/dev/null" | head -1   # the header
    for subject in "${subjects[@]}"
    do
      [[ "${subject}" == "@" ]] && continue   # a role group not found
      $ssh ${vip} "sudo -u oneadmin oneacl list -f USER='${subject}' --no-header 2>/dev/null"
    done | sort -nr  # subject
  done  # tenant

  return 0
}  # acl_tenant_get



help_data[acl_tenant_set]="\
  acl_tenant_set             # delete than set new ACLs for all tenants
    platform=NAME            #   platforms.<site>.<platform>: any platform of the federation,
                             #     the ACLs are set on its primary platform for every zone of the federation
    tenants=LIST             #   tenants names or group ids
    dry=YES|no               #   dry run without deleteing and creating ACLs
    confirm=yes|NO           #   to suppress interactive confirmation
                             #   the ACLs: of the tenant group and its role groups sa-, power-, user-, ro-<name>,
                             #     for the clusters of its quota records in every zone - QUOTA, see data_platform_refresh quota=;
                             #     deleted: the ACLs of the tenant group, its role groups and its admin;
                             #     a tenant without quota records - a warning, its ACLs untouched"
function acl_tenant_set() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local primary vip json member zone_id qr cluster_id found
  local dry="${dry:-yes}"
  local group_id tenant admin_id sa_id power_id user_id ro_id shared role subject
  local acl acl_id acl_subj acl_obj acl_ops acl_zone
  local -a acls=()

  # switch to the primary FE if the corresponding platform is a secondary
  if primary=$(inv_value var=INV path=${platform:-}.fe.primary)
  then
    _log platform="${platform}" primary="${primary}" action="switch_to_primary"
    platform="${primary}"
  fi
  data_platform_refresh platform="${platform:-}" quota=load || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)
  shared="${RUNTIME[${platform},shared]:-}"

  _log_std action=acl_tenant_set

  for tenant in $(_get_tenants platform="${platform}" tenants="${tenants:-ALL}")
  do
    group_id=""
    found=""
    for member in ${platform} $(inv_list var=INV path=${platform}.fe.secondaries)
    do
      if [[ "${RUNTIME[${member},available]:-}" != "yes" ]]
      then
        # the ACLs of every zone are set at once: without this zone the tenant would lose its ACLs there
        _log_error "platform ${member}: unavailable - ACLs of tenant ${tenant} not changed"
        return 1
      fi
      [[ -n "${QUOTA[${tenant},group_id]:-}" ]] || continue
      group_id="${QUOTA[${tenant},group_id]}"
      [[ -n "${QUOTA[${tenant},${member},qrs]}" ]] && found=yes
      _log_std tenant="${tenant}" group_id="${group_id}" quotas="[${QUOTA[${tenant},${member},qrs]}]"
    done  # member
    if [[ -z "${found}" ]]
    then
      _log_std tenant="${tenant}" group_id="${group_id}" warning="no quota records - ACLs not changed"
      continue
    fi

    # the admin of the tenant group, the role groups
    json=$($ssh ${vip} "sudo -u oneadmin onegroup show ${group_id} -j 2>/dev/null")
    admin_id=$(jq -r '.GROUP.ADMINS.ID | if type == "array" then .[0] else . end // ""' <<< "${json}")
    for role in sa power user ro
    do
      printf -v "${role}_id" '%s' "$(
        $ssh ${vip} "sudo -u oneadmin onegroup show ${role}-${tenant} -j 2>/dev/null" | jq -r '.GROUP.ID // ""'
      )"
    done  # role
    _log_std tenant="${tenant}" group_id="${group_id}" admin_id="${admin_id}" \
      sa_id="${sa_id}" power_id="${power_id}" user_id="${user_id}" ro_id="${ro_id}"
    if [[ -z "${sa_id}" || -z "${power_id}" || -z "${user_id}" || -z "${ro_id}" ]]
    then
      _log_std tenant="${tenant}" group_id="${group_id}" warning="role groups not all found - ACLs not changed"
      continue
    fi

    # the ACLs of the tenant
    acls=(
      # SA: manage the tenant group and its role groups, the users of the tenant, create users
      "@${sa_id} GROUP/#${group_id} USE+MANAGE *"
      "@${sa_id} GROUP/#${sa_id} USE+MANAGE *"
      "@${sa_id} GROUP/#${power_id} USE+MANAGE *"
      "@${sa_id} GROUP/#${user_id} USE+MANAGE *"
      "@${sa_id} GROUP/#${ro_id} USE+MANAGE *"
      "@${sa_id} USER/@${group_id} USE+MANAGE+ADMIN *"
      "@${sa_id} USER/* CREATE *"
    )
    # every zone of the federation: the clusters of the quota records of the tenant in the zone - QUOTA;
    #   a record without clusters, a zone without clusters - no ACLs
    for member in ${platform} $(inv_list var=INV path=${platform}.fe.secondaries)
    do
      [[ "${RUNTIME[${member},available]:-}" == "yes" ]] || continue   # unavailable - checked above
      zone_id="${RUNTIME[${member},id]}"
      found=""
      for qr in ${QUOTA[${tenant},${member},qrs]:-}
      do
        for cluster_id in ${QUOTA[${tenant},${member},${qr},clusters]}
        do
          [[ -z "${found}" ]] && acls+=( "@${group_id} ZONE/#${zone_id} USE #${zone_id}" )
          found=yes
          acls+=(
            # all roles
            "@${group_id} CLUSTER/#${cluster_id} USE #${zone_id}"
            "@${group_id} DATASTORE/%${cluster_id} USE #${zone_id}"
            # hosts: SA, Power, User - manage; RO - use
            "@${sa_id} HOST/%${cluster_id} MANAGE #${zone_id}"
            "@${power_id} HOST/%${cluster_id} MANAGE #${zone_id}"
            "@${user_id} HOST/%${cluster_id} MANAGE #${zone_id}"
            "@${ro_id} HOST/%${cluster_id} USE #${zone_id}"
          )
        done  # cluster_id
      done  # qr
      [[ -n "${found}" ]] || continue   # the tenant has no cluster in this zone

      [[ -n "${shared}" ]] && acls+=( "@${group_id} IMAGE+TEMPLATE/@${shared} USE #${zone_id}" )
      acls+=(
        # Super Admins
        "@${sa_id} MARKETPLACEAPP/* USE #${zone_id}"
        "@${sa_id} VNTEMPLATE/#${RUNTIME[${member},vntemplate,id]} USE #${zone_id}"
        "@${sa_id} VM+NET+IMAGE+TEMPLATE+DOCUMENT+SECGROUP+VROUTER+BACKUPJOB/@${group_id} USE+MANAGE #${zone_id}"
        "@${sa_id} VM+NET+IMAGE+TEMPLATE+DOCUMENT+SECGROUP+VROUTER+BACKUPJOB/* CREATE #${zone_id}"
        # Power Users
        "@${power_id} MARKETPLACEAPP/* USE #${zone_id}"
        "@${power_id} VNTEMPLATE/#${RUNTIME[${member},vntemplate,id]} USE #${zone_id}"
        "@${power_id} VM+NET+IMAGE+TEMPLATE+DOCUMENT+SECGROUP+VROUTER+BACKUPJOB/@${group_id} USE+MANAGE #${zone_id}"
        "@${power_id} VM+NET+IMAGE+TEMPLATE+DOCUMENT+SECGROUP+VROUTER+BACKUPJOB/* CREATE #${zone_id}"
        # Users
        "@${user_id} NET+IMAGE+TEMPLATE+DOCUMENT+SECGROUP/@${group_id} USE #${zone_id}"
        "@${user_id} VM+TEMPLATE+DOCUMENT/* CREATE #${zone_id}"
        # Read Only
        "@${ro_id} MARKETPLACEAPP/* USE #${zone_id}"
        "@${ro_id} VNTEMPLATE/#${RUNTIME[${member},vntemplate,id]} USE #${zone_id}"
        "@${ro_id} VM+NET+IMAGE+TEMPLATE+DOCUMENT+SECGROUP+VROUTER+BACKUPJOB/@${group_id} USE #${zone_id}"
      )
    done  # member

    [[ "${dry}" == "no" ]] && _stop_without_confirmation confirm=${confirm:-no} && return 1

    # delete: the ACLs of the tenant group, its role groups and its admin
    if [[ "${dry}" == "no" ]]
    then
      _log_std -n tenant="${tenant}" group_id="${group_id}" acl_delete="["
      for subject in "@${group_id}" "@${sa_id}" "@${power_id}" "@${user_id}" "@${ro_id}" ${admin_id:+"#${admin_id}"}
      do
        for acl_id in $($ssh ${vip} "sudo -u oneadmin oneacl list -f USER='${subject}' --no-header -l ID 2>/dev/null")
        do
          _log -n " ${acl_id}"
          $ssh ${vip} "sudo -u oneadmin oneacl delete ${acl_id} &>/dev/null"
        done  # acl_id
      done  # subject
      _log " ]"
      _log_std -n tenant="${tenant}" group_id="${group_id}" acl_create="["
    else
      _log_std tenant="${tenant}" group_id="${group_id}" acl_delete="[ dry run: no ACLs will be deleted ]"
      _log_std tenant="${tenant}" group_id="${group_id}" acl_create="[ dry run: no ACLs will be created ]"
    fi

    for acl in "${acls[@]}"
    do
      if [[ "${dry}" == "no" ]]
      then
        acl_id=$($ssh ${vip} "sudo -u oneadmin oneacl create \"${acl}\" 2>/dev/null")
        acl_id="${acl_id##* }"
        _log -n " ${acl_id:-failed}"
      else
        read -r acl_subj acl_obj acl_ops acl_zone <<< "${acl}"
        _log "$(printf '%-6s %-64s %-17s %s' "${acl_subj}" "${acl_obj}" "${acl_ops}" "${acl_zone}")"
      fi
    done  # acl

    if [[ "${dry}" == "no" ]]
    then
      _log " ]"
      _log_std tenant="${tenant}" group_id="${group_id}" acl="new"
      for subject in "@${group_id}" "@${sa_id}" "@${power_id}" "@${user_id}" "@${ro_id}"
      do
        $ssh ${vip} "sudo -u oneadmin oneacl list -f USER='${subject}' --no-header 2>/dev/null"
      done | sort -nr  # subject
    fi
  done  # group_id

  return 0
}  # acl_tenant_set
