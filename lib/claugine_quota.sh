#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



# INDEX
#   _quota_group_set        set a quota template to a group on the platform
#   _quota_tenant_template  print the quota template of the tenant from its QUOTA limits
#   quota_cpu_to_vcpu_set   set CPU to VCPU for each VM where CPU != VCPU
#   quota_tenant_set        set the quotas of the tenants from QUOTA: every cluster of the platform, -1 - not of the tenant
#   quota_tenant_show       show the quotas of a tenant: limit, real, usage
#   quota_tenants_report    a table of the quotas of the tenants: limit, real, usage, check



# print the quota template for tenant
#   platform=NAME            # QUOTA loaded
#   tenant=NAME
# return 0 - always
function _quota_tenant_template() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local platform="${platform:-}"
  local tenant="${tenant:-}"
  local qr clusters cpu ram_gb gpu disk_gb ds_id cluster_id
  local reset=$(inv_value var=TEMPLATES path=one.quota_vm_reset.content)   # all limits -1

  for qr in ${RUNTIME[${platform},qr_list]}
  do
    clusters="${RUNTIME[${platform},${qr},clusters_list]}"
    if [[ " ${QUOTA[${tenant},${platform},qrs]} " != *" ${qr} "* ]]
    then
      echo "VM = [ CLUSTER_IDS = \"${clusters// /,}\", ${reset} ]"
      continue
    fi
    cpu="${QUOTA[${tenant},${platform},${qr},limit,cpu]}"
    ram_gb="${QUOTA[${tenant},${platform},${qr},limit,ram_gb]}"
    gpu="${QUOTA[${tenant},${platform},${qr},limit,gpu]}"
    disk_gb="${QUOTA[${tenant},${platform},${qr},limit,disk_gb]}"
    echo "VM = [
      CLUSTER_IDS = \"${clusters// /,}\",
      VMS = \"$(( cpu * 2 ))\", RUNNING_VMS = \"${cpu}\",
      CPU = \"$(( cpu * 2 ))\", RUNNING_CPU = \"${cpu}\",
      MEMORY = \"$(( ram_gb * 2 * 1024 ))\", RUNNING_MEMORY = \"$(( ram_gb * 1024 ))\",
      SYSTEM_DISK_SIZE = \"$(( disk_gb * 1024 ))\",
      PCI_DEV = \"${gpu}\", RUNNING_PCI_DEV = \"${gpu}\",
      PCI_NIC = \"0\", RUNNING_PCI_NIC = \"0\"
    ]"
  done  # qr

  for cluster_id in ${RUNTIME[${platform},noqr_clusters_list]}
  do
    echo "VM = [ CLUSTER_IDS = \"${cluster_id}\", ${reset} ]"
  done  # cluster_id

  for ds_id in ${QUOTA[${tenant},${platform},ds_list]}
  do
    echo "DATASTORE = [
      ID = \"${ds_id}\",
      IMAGES = \"-1\",
      SIZE = \"$(( QUOTA[${tenant},${platform},ds,${ds_id},limit,size_gb] * 1024 ))\"
    ]"
  done  # ds_id

  # the other datastores of the platform: IMAGE of other qrs, FILE and BACKUP of a tenant without qrs here - -1
  for ds_id in ${RUNTIME[${platform},images_ds_list]} ${RUNTIME[${platform},files_ds,id]} ${RUNTIME[${platform},backups_ds,id]}
  do
    [[ " ${QUOTA[${tenant},${platform},ds_list]} " == *" ${ds_id} "* ]] && continue
    echo "DATASTORE = [ ID = \"${ds_id}\", IMAGES = \"-1\", SIZE = \"-1\" ]"
  done  # ds_id

  return 0
}  # _quota_tenant_template



# set a quota template to a group on the platform: onegroup quota, the existing quotas not in the template are kept
#   platform=NAME
#   group_id=ID
#   template=STRING
# return 0 - set
#        1 - error, the template printed
function _quota_group_set() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local vip file="/tmp/claugine-group-quota-${group_id:-}"
  vip=$(inv_value var=INV path=${platform}.fe.vip)

  $ssh ${vip} "tee ${file} &>/dev/null" <<< "${template:-}"
  $ssh ${vip} "chmod 644 ${file}"

  if ! $ssh ${vip} "sudo -u oneadmin onegroup quota ${group_id} ${file} >/dev/null"
  then
    _log_error "group ${group_id}: quota can not be set" "${template:-}"
    $ssh ${vip} "rm -f ${file}"
    return 1
  fi
  $ssh ${vip} "rm -f ${file}"
  return 0
}  # _quota_group_set



help_data[quota_cpu_to_vcpu_set]="\
  quota_cpu_to_vcpu_set      # set CPU to VCPU for each VM where CPU != VCPU
    platform=NAME            #   platforms.<site>.<platform>
    confirm=yes|NO           #   to suppress interactive confirmation"
function quota_cpu_to_vcpu_set() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local node
  local vm_id json name cpu vcpu vip

  data_platform_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)

  _log_std zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" action="quota_cpu_to_vcpu_set"
  _stop_without_confirmation confirm=${confirm:-no} && return 1

  for vm_id in $(
    $ssh ${vip} "sudo -u oneadmin onevm list --no-header -l ID 2>/dev/null" |
    tr -d '[:blank:]' |
    sort -n
  )
  do
    json=$($ssh ${vip} "sudo -u oneadmin onevm show ${vm_id} -j 2>/dev/null")
    name=$(jq -r '.VM.NAME' <<< "${json}")
    cpu=$(jq -r '.VM.TEMPLATE.CPU // ""' <<< "${json}")
    vcpu=$(jq -r '.VM.TEMPLATE.VCPU // ""' <<< "${json}")

    if [[ -z "${vcpu}" ]]
    then
      _log -n "!"
      vcpu=1
    fi
    [[ "${vcpu}" == "${cpu}" ]] && continue

    _log_std vm="${name}" vm_id="${vm_id}" vcpu="${vcpu}" cpu="${cpu}>${vcpu}"
    $ssh ${vip} "sudo -u oneadmin onevm resize ${vm_id} --cpu ${vcpu}"
  done  # vm_id

  return 0
}  # quota_cpu_to_vcpu_set



help_data[quota_tenant_set]="\
  quota_tenant_set           # set the quotas of the tenants on the platform from QUOTA, see data_platform_refresh quota=
    platform=NAME            #   platforms.<site>.<platform>, kind shared; the quotas are set in its zone
    tenants=LIST             #   tenants names or group ids
    dry=YES|no               #   dry run: print the quota templates only
    confirm=yes|NO           #   to suppress interactive confirmation
                             #   one template per tenant, see _quota_tenant_template: VM - every cluster of the platform,
                             #     the qrs of the tenant - its limits, other qrs and the clusters without quota_code - all -1;
                             #   DATASTORE - per datastore of the tenant, the other datastores of the platform - -1"
function quota_tenant_set() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local dry="${dry:-yes}"
  local vip tenant group_id template errors=0

  data_platform_refresh platform="${platform:-}" quota=load || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)

  _log_std action=quota_tenant_set dry="${dry}"
  [[ "${dry}" == "no" ]] && _stop_without_confirmation confirm=${confirm:-no} && return 1

  for tenant in $(_get_tenants platform="${platform}" tenants="${tenants:-ALL}")
  do
    group_id="${QUOTA[${tenant},group_id]}"

    template=$(_quota_tenant_template platform="${platform}" tenant="${tenant}")

    _log_std tenant="${tenant}" group_id="${group_id}" qrs="[${QUOTA[${tenant},${platform},qrs]}]"
    if [[ "${dry}" != "no" ]]
    then
      _log "${template}"
      continue
    fi

    if _quota_group_set platform="${platform}" group_id="${group_id}" template="${template}"
    then
      _log_std tenant="${tenant}" group_id="${group_id}" quota_set=ok
      # the real quotas of the tenant changed: its quota data loaded again
      data_platform_refresh platform="${platform}" quota=reload tenants="${tenant}" || (( errors++ ))
    else
      (( errors++ ))
    fi
  done  # tenant

  (( errors == 0 )) || return 1
  return 0
}  # quota_tenant_set



help_data[quota_tenant_show]="\
  quota_tenant_show          # show the quotas of a tenant: limit, real, usage - the usage of the tenant read again
    platform=NAME            #   platforms.<site>.<platform>, kind shared
    tenant=NAME              #   tenant name or group id"
function quota_tenant_show() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local name qr ds_id field

  data_platform_refresh platform="${platform:-}" quota=load || return 1
  name=$(_get_tenants platform="${platform}" tenants="${tenant:-}" | head -1)
  if [[ -z "${tenant:-}" || -z "${name}" ]]
  then
    _log_error "platform ${platform}: tenant \"${tenant:-}\" not found"
    return 1
  fi
  data_platform_refresh platform="${platform}" quota=refresh tenants="${name}" || return 1   # usage of the tenant only

  _log_std tenant="${name}" group_id="${QUOTA[${name},group_id]}" qrs="[${QUOTA[${name},${platform},qrs]}]"
  _log "site:$(
    for field in $(printf '%s\n' "${!QUOTA[@]}" | grep "^${name},${platform},site," | sed "s/^${name},${platform},site,//" | sort)
    do
      printf ' %s=%s' "${field}" "${QUOTA[${name},${platform},site,${field}]}"
    done  # field
  )"

  printf '%-16s %-12s %-10s %-10s %-10s %-11s %-11s %-11s %-11s\n' \
    RECORD CLUSTERS STATUS CREATED REVIEW "CPU l/r/u" "RAM_GB" "GPU" "DISK_GB"
  for qr in ${QUOTA[${name},${platform},qrs]}
  do
    printf '%-16s %-12s %-10s %-10s %-10s' \
      "${qr}" "$(tr ' ' ',' <<< "${QUOTA[${name},${platform},${qr},clusters]}")" \
      "${QUOTA[${name},${platform},${qr},status]}" \
      "${QUOTA[${name},${platform},${qr},date_created]}" \
      "${QUOTA[${name},${platform},${qr},date_review]}"
    for field in cpu ram_gb gpu disk_gb
    do
      printf ' %-11s' "${QUOTA[${name},${platform},${qr},limit,${field}]}/${QUOTA[${name},${platform},${qr},real,${field}]}/${QUOTA[${name},${platform},${qr},usage,${field}]}"
    done  # field
    printf '\n'
  done  # qr

  printf '%-16s %-8s %s\n' DATASTORE TYPE "SIZE_GB l/r/u"
  for ds_id in ${QUOTA[${name},${platform},ds_list]}
  do
    printf '%-16s %-8s %s\n' "${ds_id}" "${QUOTA[${name},${platform},ds,${ds_id},type]}" \
      "${QUOTA[${name},${platform},ds,${ds_id},limit,size_gb]}/${QUOTA[${name},${platform},ds,${ds_id},real,size_gb]}/${QUOTA[${name},${platform},ds,${ds_id},usage,size_gb]}"
  done  # ds_id

  return 0
}  # quota_tenant_show



help_data[quota_tenants_report]="\
  quota_tenants_report       # a table of the quotas of the tenants: a row per record and per datastore - their usage read again
    platform=NAME            #   platforms.<site>.<platform>, kind shared
    tenants=LIST             #   tenants names or group ids
                             #   CHECK: ok; differ - real is not the quota of the limit (see _quota_tenant_template);
                             #     exceeded - usage over the real quota; no_clusters - a record without clusters"
function quota_tenants_report() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local tenant qr ds_id field limit real usage expected check

  data_platform_refresh platform="${platform:-}" quota=refresh tenants="${tenants:-}" || return 1   # usage of these tenants

  printf '%-16s %-16s %-12s %-10s %-11s %-11s %-11s %-11s %s\n' \
    TENANT RECORD CLUSTERS STATUS "CPU l/r/u" "RAM_GB" "GPU" "DISK_GB" CHECK
  for tenant in $(_get_tenants platform="${platform}" tenants="${tenants:-ALL}")
  do
    for qr in ${QUOTA[${tenant},${platform},qrs]:-}
    do
      check="ok"
      [[ -n "${QUOTA[${tenant},${platform},${qr},clusters]}" ]] || check="no_clusters"
      printf '%-16s %-16s %-12s %-10s' \
        "${tenant}" "${qr}" "$(tr ' ' ',' <<< "${QUOTA[${tenant},${platform},${qr},clusters]}")" \
        "${QUOTA[${tenant},${platform},${qr},status]}"
      for field in cpu ram_gb gpu disk_gb
      do
        limit="${QUOTA[${tenant},${platform},${qr},limit,${field}]}"
        real="${QUOTA[${tenant},${platform},${qr},real,${field}]}"
        usage="${QUOTA[${tenant},${platform},${qr},usage,${field}]}"
        printf ' %-11s' "${limit}/${real}/${usage}"
        [[ "${check}" == "no_clusters" ]] && continue
        [[ "${limit}" =~ ^[0-9]+$ ]] || limit=0
        # the real quota of the limit: cpu and ram x 2, gpu and disk x 1
        case "${field}" in
          cpu|ram_gb) expected=$(( limit * 2 )) ;;
          *)          expected=${limit} ;;
        esac
        [[ "${real}" == "${expected}" ]] || [[ "${check}" == "exceeded" ]] || check="differ"
        [[ "${usage}" =~ ^[0-9]+$ && "${real}" =~ ^[0-9]+$ ]] && (( usage > real )) && check="exceeded"
      done  # field
      printf ' %s\n' "${check}"
    done  # qr
  done  # tenant

  printf '\n%-16s %-16s %-8s %-20s %s\n' TENANT DATASTORE TYPE "SIZE_GB l/r/u" CHECK
  for tenant in $(_get_tenants platform="${platform}" tenants="${tenants:-ALL}")
  do
    for ds_id in ${QUOTA[${tenant},${platform},ds_list]}
    do
      limit="${QUOTA[${tenant},${platform},ds,${ds_id},limit,size_gb]}"
      real="${QUOTA[${tenant},${platform},ds,${ds_id},real,size_gb]}"
      usage="${QUOTA[${tenant},${platform},ds,${ds_id},usage,size_gb]}"
      check="ok"
      [[ "${real}" == "${limit}" ]] || check="differ"
      [[ "${usage}" =~ ^[0-9]+$ && "${real}" =~ ^[0-9]+$ ]] && (( usage > real )) && check="exceeded"
      printf '%-16s %-16s %-8s %-20s %s\n' "${tenant}" "${ds_id}" "${QUOTA[${tenant},${platform},ds,${ds_id},type]}" "${limit}/${real}/${usage}" "${check}"
    done  # ds_id
  done  # tenant

  return 0
}  # quota_tenants_report
