#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



# INDEX
#   quota_cpu_to_vcpu_set  set CPU to VCPU for each VM where CPU != VCPU
#   quota_ds_set           set quotas for IMAGES, FILES and BACKUPS datastores
#   quota_tenant_get       show quota and usage for tenant
#   quota_vcpu_conf_show   show VCPU configuration in oned.conf



help_data[quota_cpu_to_vcpu_set]="\
  quota_cpu_to_vcpu_set      # set CPU to VCPU for each VM where CPU != VCPU
    platform=NAME            #   platforms.<site>.<platform>
    confirm=yes|NO           #   to suppress interactive confirmation"
# return 0 - done
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            not confirmed
function quota_cpu_to_vcpu_set() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local node
  local vm_id json name cpu vcpu vip

  data_runtime_refresh platform="${platform:-}" || return 1
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



help_data[quota_ds_set]="\
  quota_ds_set               # set quotas for IMAGES, FILES and BACKUPS datastores
    platform=NAME            #   platforms.<site>.<platform>
    tenants=LIST             #   tenants ids or names
    confirm=yes|NO           #   to suppress interactive confirmation
                             #   quotas, GiB: patch.yaml ds_quotas of the inventory platform with the zone VIP"
# return 0 - done
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            not confirmed
function quota_ds_set() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local tenants="${tenants:-ALL}"
  local -a tenants_list=()
  local -A quotas=()
  local -a clusters=()
  local tenant json
  local group_id group_name
  local images_gib files_gib backups_gib backups_n images
  local cluster_id ds_id ds_clusters quota_ds quota q vip

  data_runtime_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)
  _log_std zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" action="quota_ds_set"

  if (( ${#tenants_list[@]} == 0 ))
  then
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
  fi

  for group_id in ${tenants_list[@]}
  do
    (( group_id < 100 )) || [[ "${group_id}" == "${RUNTIME[${platform},shared]:-}" ]] && continue

    json=$($ssh ${vip} "sudo -u oneadmin onegroup show ${group_id} -j 2>/dev/null")
    group_name=$(jq -r '.GROUP.NAME' <<< "${json}")
    tenant="${group_name#*-}"

    _log_std -n tenant="${tenant}" group_id="${group_id}" quota_defined=
    if jq -e '
      .GROUP.VM_QUOTA.VM
      | arrays
      | any(.[]; .CPU != null)
    ' <<< "${json}" >/dev/null
    then
      _log yes
    else
      _log no
      continue
    fi

    mapfile -t clusters < <(jq -r '
      [
        .GROUP.VM_QUOTA.VM[]
        | .CLUSTER_IDS // empty
        | split(",")[]
        | gsub("\\s"; "")
        | select(length > 0)
      ]
      | unique[]
    ' <<< "$json")
    _log_std tenant="${tenant}" group_id="${group_id}" clusters="[${clusters[*]}]"

    q="${platform}.patch.ds_quotas"
    images_gib="$(inv_value var=INV path=${q}.tenants.${tenant}.images)"
    files_gib="$(inv_value var=INV path=${q}.tenants.${tenant}.files)"
    backups_gib="$(inv_value var=INV path=${q}.tenants.${tenant}.backups)"
    images_gib="${images_gib:-$(inv_value var=INV path=${q}.default.images)}"
    files_gib="${files_gib:-$(inv_value var=INV path=${q}.default.files)}"
    backups_gib="${backups_gib:-$(inv_value var=INV path=${q}.default.backups)}"

    quota_ds='[]'
    for ds_id in ${RUNTIME[${platform},images_ds_list]}
    do
      quota_ds=$(jq --arg ds "${ds_id}" '
        . + [{ID: $ds, IMAGES: "0", SIZE: "0"}]
      ' <<< "$quota_ds")
    done  # ds_id

    images=""
    for cluster_id in "${clusters[@]}"
    do
      for ds_id in ${RUNTIME[${platform},images_ds_list]}
      do
        # IMAGE datastores of the cluster: assigned to it, or to all clusters
        ds_clusters=" ${RUNTIME[${platform},images_ds,${ds_id},clusters]} "
        [[ "${ds_clusters}" == "  " || "${ds_clusters}" == *" ${cluster_id} "* ]] || continue
        [[ " ${images} " == *" ${ds_id} "* ]] && continue
        images+="${images:+ }${ds_id}"
        _log_std tenant="${tenant}" group_id="${group_id}" images_ds="${ds_id}" images_gib="${images_gib}" images_n="-1"
        quota_ds=$(jq \
          --arg images_id "${ds_id}" \
          --arg images_size "$((images_gib * 1024))" '
          map(
            if .ID == $images_id
            then . + {IMAGES: "-1", SIZE: $images_size}
            else .
            end
          )
        ' <<< "$quota_ds")
      done  # ds_id
    done  # cluster_id

    (( backups_gib == 0 )) && backups_n=0 || backups_n="-1"
    quota_ds=$(jq \
      --arg files_id "${RUNTIME[${platform},files_ds,id]}" \
      --arg backups_id "${RUNTIME[${platform},backups_ds,id]}" \
      --arg files_size "$((files_gib * 1024))" \
      --arg backups_size "$((backups_gib * 1024))" \
      --arg backups_n "${backups_n}" '
        . + [{ID: $files_id, IMAGES: "-1", SIZE: $files_size}]
        | . + [{ID: $backups_id, IMAGES: $backups_n, SIZE: $backups_size}]
    ' <<< "$quota_ds")
    _log_std tenant="${tenant}" group_id="${group_id}" files_ds="${RUNTIME[${platform},files_ds,id]}" files_gib="${files_gib}" files_n="-1"
    _log_std \
      tenant="${tenant}" \
      group_id="${group_id}" \
      backups_ds="${RUNTIME[${platform},backups_ds,id]}" \
      backups_gib="${backups_gib}" \
      backups_n="${backups_n}"

    quota=$(
      jq -r '
        .GROUP.VM_QUOTA.VM
        | map(
          select(has("CLUSTER_IDS"))
          | with_entries(
            select(
              (.key | endswith("USED") | not)
              and (.key | contains("VCPU") | not)
            )
          )
        )
        | .[]
        | "VM = [\n" +
          (
            to_entries
            | map(
                "  \(.key) = \"\(.value)\""
              )
            | join(",\n")
          ) +
          "\n]"
      '  <<< "$json"

      jq -r '
        .[]
        | "DATASTORE = [\n" +
          (
            to_entries
            | map(
                "  \(.key) = \"\(.value)\""
              )
            | join(",\n")
          ) +
          "\n]"
      ' <<< "${quota_ds}"
    )

    $ssh ${vip} "tee /var/tmp/one-tenant-quota-new &>/dev/null" <<< "${quota}"

    _log_std -n tenant="${tenant}" quota_set=
    if $ssh ${vip} "sudo -u oneadmin onegroup quota ${group_id} /var/tmp/one-tenant-quota-new"
    then
      _log ok
    else
      _log error
      $ssh ${vip} "sudo cat /var/tmp/one-tenant-quota-new"
    fi

    $ssh ${vip} "sudo rm -f /var/tmp/one-tenant-quota-new"
  done  # group_id
}  # quota_ds_set



help_data[quota_tenant_get]="\
  quota_tenant_get           # show quota and usage for tenant
    platform=NAME            #   platforms.<site>.<platform>
    tenant=STRING            #"
# return 0 - done
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
function quota_tenant_get() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local tenant="${tenant:-}"
  local json
  local group_id group_name admin_id admin_name
  local -A quotas=()
  local id id_list
  local size size_used images images_used
  local cpu cpu_used ram ram_used
  local pci_dev pci_dev_used pci_nic pci_nic_used
  local run_cpu run_cpu_used run_ram run_ram_used
  local run_pci_dev run_pci_dev_used run_pci_nic run_pci_nic_used
  local run_vcpu run_vcpu_used
  local run_vms run_vms_used
  local disk_size disk_size_used
  local vcpu vcpu_used
  local vms vms_used vip

  data_runtime_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)

  group_name="tenant-${tenant}"
  _log_std zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" action="quota_tenant_get"

  json=$($ssh ${vip} "sudo -u oneadmin onegroup show ${group_name} -j 2>/dev/null")
  group_id=$(jq -r '.GROUP.ID // ""' <<< "${json}")

  _log_std -n tenant="${tenant}" group_name="${group_name}" group_id="${group_id}" quota_defined=
  if jq -e '
    .GROUP.VM_QUOTA.VM
    | arrays
    | any(.[]; .CPU != null)
  ' <<< "${json}" >/dev/null
  then
    _log yes
  else
    _log no
    return 0
  fi

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
    $ssh ${vip} "sudo -u oneadmin oneuser show ${admin_id} -j 2>/dev/null" |
    jq -r '.USER.NAME // ""'
  )
  quotas[${vip},${group_id},admin,id]="${admin_id}"
  quotas[${vip},${group_id},admin,name]="${admin_name}"
  _log_std tenant="${tenant}" group_name="${group_name}" group_id="${group_id}" admin_name="${admin_name}" admin_id="${admin_id}"

  id_list=""
  while IFS=$'\t' read -r id size size_used images images_used
  do
    [[ " ${id_list} " != *" ${id} "* ]] && id_list+="${id_list:+ }${id}"

    (( size > 0 )) && (( size /= 1024 ))
    (( size_used > 0 )) && (( size_used /= 1024 ))
    quotas[${vip},${group_id},ds,${id},size]="${size}"
    quotas[${vip},${group_id},ds,${id},images]="${images}"
    quotas[${vip},${group_id},ds,${id},size_used]="${size_used}"
    quotas[${vip},${group_id},ds,${id},images_used]="${images_used}"
  done < <(jq -r '
    .GROUP.DATASTORE_QUOTA.DATASTORE
    | if type == "array" then .[] else . end
    | [
        .ID,
        .SIZE,
        .SIZE_USED,
        .IMAGES,
        .IMAGES_USED
      ]
    | map(. // 0)
    | @tsv
  ' <<< "${json}")

  quotas[${vip},${group_id},ds,list]=$(
    printf '%s\n' ${id_list} |
    sort -n |
    xargs
  )

  id_list=""
  while IFS=$'\t' read -r \
    id \
    cpu cpu_used ram ram_used \
    pci_dev pci_dev_used pci_nic pci_nic_used \
    run_cpu run_cpu_used run_ram run_ram_used \
    run_pci_dev run_pci_dev_used run_pci_nic run_pci_nic_used \
    run_vcpu run_vcpu_used \
    run_vms run_vms_used \
    disk_size disk_size_used \
    vcpu vcpu_used \
    vms vms_used
  do
    [[ " ${id_list} " != *" ${id} "* ]] && id_list+="${id_list:+ }${id}"

    [[ ${cpu_used} = *.* ]] && cpu_used=$(( ${cpu_used%.*} + 1 ))
    [[ ${run_cpu_used} = *.* ]] && run_cpu_used=$(( ${run_cpu_used%.*} + 1 ))

    (( ram > 0 )) && (( ram /= 1024 ))
    (( run_ram > 0 )) && (( run_ram /= 1024 ))
    (( ram_used > 0 )) && (( ram_used /= 1024 ))
    (( run_ram_used > 0 )) && (( run_ram_used /= 1024 ))

    (( disk_size > 0 )) && (( disk_size /= 1024 ))
    (( disk_size_used > 0 )) && (( disk_size_used /= 1024 ))

    quotas[${vip},${group_id},cl,${id},vms]="${vms}"
    quotas[${vip},${group_id},cl,${id},run_vms]="${run_vms}"
    quotas[${vip},${group_id},cl,${id},vms_used]="${vms_used}"
    quotas[${vip},${group_id},cl,${id},run_vms_used]="${run_vms_used}"

    quotas[${vip},${group_id},cl,${id},cpu]="${cpu}"
    quotas[${vip},${group_id},cl,${id},run_cpu]="${run_cpu}"
    quotas[${vip},${group_id},cl,${id},cpu_used]="${cpu_used}"
    quotas[${vip},${group_id},cl,${id},run_cpu_used]="${run_cpu_used}"

    quotas[${vip},${group_id},cl,${id},ram]="${ram}"
    quotas[${vip},${group_id},cl,${id},run_ram]="${run_ram}"
    quotas[${vip},${group_id},cl,${id},ram_used]="${ram_used}"
    quotas[${vip},${group_id},cl,${id},run_ram_used]="${run_ram_used}"

    quotas[${vip},${group_id},cl,${id},gpu]="${pci_dev}"
    quotas[${vip},${group_id},cl,${id},run_gpu]="${run_pci_dev}"
    quotas[${vip},${group_id},cl,${id},gpu_used]="${pci_dev_used}"
    quotas[${vip},${group_id},cl,${id},run_gpu_used]="${run_pci_dev_used}"

    quotas[${vip},${group_id},cl,${id},disk_size]="${disk_size}"
    quotas[${vip},${group_id},cl,${id},disk_size_used]="${disk_size_used}"
  done < <(jq -r '
    .GROUP.VM_QUOTA.VM
    | if type == "array" then .[] else . end
    | [
        (.CLUSTER_IDS // "-"),
        .CPU,
        .CPU_USED,
        .MEMORY,
        .MEMORY_USED,
        .PCI_DEV,
        .PCI_DEV_USED,
        .PCI_NIC,
        .PCI_NIC_USED,
        .RUNNING_CPU,
        .RUNNING_CPU_USED,
        .RUNNING_MEMORY,
        .RUNNING_MEMORY_USED,
        .RUNNING_PCI_DEV,
        .RUNNING_PCI_DEV_USED,
        .RUNNING_PCI_NIC,
        .RUNNING_PCI_NIC_USED,
        .RUNNING_VCPU,
        .RUNNING_VCPU_USED,
        .RUNNING_VMS,
        .RUNNING_VMS_USED,
        .SYSTEM_DISK_SIZE,
        .SYSTEM_DISK_SIZE_USED,
        .VCPU,
        .VCPU_USED,
        .VMS,
        .VMS_USED
      ]
    | map(. // 0)
    | @tsv
  ' <<< "${json}")

  quotas[${vip},${group_id},cl,list]=$(
    printf '%s\n' ${id_list} |
    sort -n |
    xargs
  )

  _log
  _log "$(printf "%-8s %10s %10s %10s %10s %14s %14s %14s %10s %10s\n" \
    "CLUSTER" "VMS" "VMS/RUN" "CPU" "CPU/RUN" "RAM GiB" "RAM/RUN GiB" "DISK GiB" "GPU" "GPU/RUN")"
  _log "$(printf "%-8s %10s %10s %10s %10s %14s %14s %14s %10s %10s\n" \
    "-------" "----------" "----------" "----------" "----------" "--------------" "--------------" "--------------" "----------" "----------")"
  for id in ${quotas[${vip},${group_id},cl,list]}
  do
    _log "$(printf "%-8s %10s %10s %10s %10s %14s %14s %14s %10s %10s\n" \
      "${id}" \
      "${quotas[${vip},${group_id},cl,${id},vms]}/${quotas[${vip},${group_id},cl,${id},vms_used]}" \
      "${quotas[${vip},${group_id},cl,${id},run_vms]}/${quotas[${vip},${group_id},cl,${id},run_vms_used]}" \
      "${quotas[${vip},${group_id},cl,${id},cpu]}/${quotas[${vip},${group_id},cl,${id},cpu_used]}" \
      "${quotas[${vip},${group_id},cl,${id},run_cpu]}/${quotas[${vip},${group_id},cl,${id},run_cpu_used]}" \
      "${quotas[${vip},${group_id},cl,${id},ram]}/${quotas[${vip},${group_id},cl,${id},ram_used]}" \
      "${quotas[${vip},${group_id},cl,${id},run_ram]}/${quotas[${vip},${group_id},cl,${id},run_ram_used]}" \
      "${quotas[${vip},${group_id},cl,${id},disk_size]}/${quotas[${vip},${group_id},cl,${id},disk_size_used]}" \
      "${quotas[${vip},${group_id},cl,${id},gpu]}/${quotas[${vip},${group_id},cl,${id},gpu_used]}" \
      "${quotas[${vip},${group_id},cl,${id},run_gpu]}/${quotas[${vip},${group_id},cl,${id},run_gpu_used]}"
    )"
  done  # id

  _log
  _log "$(printf "%-10s %14s %14s\n" \
    "DATASTORE" "SIZE GiB" "IMAGES")"
  _log "$(printf "%-10s %14s %14s\n" \
    "---------" "--------------" "--------------")"
  for id in ${quotas[${vip},${group_id},ds,list]}
  do
    _log "$(printf "%-10s %14s %14s\n" \
      "${id}" \
      "${quotas[${vip},${group_id},ds,${id},size]}/${quotas[${vip},${group_id},ds,${id},size_used]}" \
      "${quotas[${vip},${group_id},ds,${id},images]}/${quotas[${vip},${group_id},ds,${id},images_used]}"
    )"
  done  # id
}  # quota_tenant_get



help_data[quota_vcpu_conf_show]="\
  quota_vcpu_conf_show       # show VCPU configuration in oned.conf
    platform=NAME            #   platforms.<site>.<platform>"
# return 0 - done
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
function quota_vcpu_conf_show() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local node vip

  data_runtime_refresh platform="${platform:-}" fe=yes || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)
  _log_std zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" action="quota_vcpu_conf_show"
  for node in $(inv_list var=CONFIG path=onefe.nodes_order.direct)
  do
    _log_fe
    $ssh ${RUNTIME[${platform},${node},ip]} "sudo grep -E '^.*QUOTA_VM_ATTRIBUTE.*VCPU' /etc/one/oned.conf"
  done  # node

  return 0
}  # quota_vcpu_conf_show
