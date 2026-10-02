#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



claugine_cli_commands+=( "\
  quota_cpu_to_vcpu_set      # set CPU to VCPU for each VM where CPU != VCPU
    confirm=yes|NO           #   to suppress interactive confirmation"
)
function quota_cpu_to_vcpu_set() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n zone=${zone_data}
  local zone_id node
  local vm_id json name cpu vcpu

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
    echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} zone_state=${zone[${zone_id},state]} action=quota_cpu_to_vcpu_set"
    stop_without_confirmation confirm=${confirm:-no} && return 1

    for vm_id in $(
      $ssh ${zone[${zone_id},vip]} "sudo -u oneadmin onevm list --no-header -l ID 2>/dev/null" |
      tr -d '[:blank:]' |
      sort -n
    )
    do
      json=$($ssh ${zone[${zone_id},vip]} "sudo -u oneadmin onevm show ${vm_id} -j 2>/dev/null")
      name=$(jq -r '.VM.NAME' <<< "${json}")
      cpu=$(jq -r '.VM.TEMPLATE.CPU // ""' <<< "${json}")
      vcpu=$(jq -r '.VM.TEMPLATE.VCPU // ""' <<< "${json}")

      if [[ -z "${vcpu}" ]]
      then
        echo -n "!"
        vcpu=1
      fi
      [[ "${vcpu}" == "${cpu}" ]] && continue

      echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} vm=${name} vm_id=${vm_id} vcpu=${vcpu} cpu=${cpu}>${vcpu}"
      $ssh ${zone[${zone_id},vip]} "sudo -u oneadmin onevm resize ${vm_id} --cpu ${vcpu}"
    done  # vm_id
  done  # zone_id

  return 0
}  # quota_cpu_to_vcpu_set



claugine_cli_commands+=( "\
  quota_ds_set               # set quotas for IMAGES, FILES and BACKUPS datastores
    tenants=LIST             #   tenants ids or names
    confirm=yes|NO           #   to suppress interactive confirmation"
)
function quota_ds_set() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n zone=${zone_data}
  local tenants="${tenants:-ALL}"
  local zone_id
  local -a tenants_list=()
  local -A quotas=()
  local -a clusters=()
  local tenant json
  local group_id group_name
  local images_gib files_gib backups_gib backups_n images
  local cluster_id ds_id quota_ds quota

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
    echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} zone_state=${zone[${zone_id},state]} action=quota_ds_set"

    if (( ${#tenants_list[@]} == 0 ))
    then
      if [[ "${tenants}" == ALL ]]
      then
        mapfile -t tenants_list < <(
          $ssh ${zone[${zone_id},vip]} "sudo -u oneadmin onegroup list --no-header -l ID 2>/dev/null" |
          tr -d '[:blank:]' |
          sort -n
        )
      else
        for tenant in ${tenants}
        do
          if [[ "${tenant}" =~ ^[0-9]+$ ]]
          then
            mapfile -t -O "${#tenants_list[@]}" tenants_list < <(
              $ssh ${zone[${zone_id},vip]} "sudo -u oneadmin onegroup list -f ID=\"${tenant}\" --no-header -l ID 2>/dev/null"
            )
          elif [[ "${tenant}" == tenant-* ]]
          then
            mapfile -t -O "${#tenants_list[@]}" tenants_list < <(
              $ssh ${zone[${zone_id},vip]} "sudo -u oneadmin onegroup list -f NAME=\"${tenant}\" --no-header -l ID 2>/dev/null"
            )
          else
            mapfile -t -O "${#tenants_list[@]}" tenants_list < <(
              $ssh ${zone[${zone_id},vip]} "sudo -u oneadmin onegroup list -f NAME~\"${tenant}\" --no-header -l ID 2>/dev/null"
            )
          fi
        done  # tenant
      fi
    fi

    for group_id in ${tenants_list[@]}
    do
      (( group_id < 100 || group_id == SHARED_ID[${zone[${zone_id},vip]}] )) && continue

      json=$($ssh ${zone[${zone_id},vip]} "sudo -u oneadmin onegroup show ${group_id} -j 2>/dev/null")
      group_name=$(jq -r '.GROUP.NAME' <<< "${json}")
      tenant="${group_name#*-}"

      echo -n "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} tenant=${tenant} group_id=${group_id} quota_defined="
      if jq -e '
        .GROUP.VM_QUOTA.VM
        | arrays
        | any(.[]; .CPU != null)
      ' <<< "${json}" >/dev/null
      then
        echo yes
      else
        echo no
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
      echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} tenant=${tenant} group_id=${group_id} clusters=[${clusters[*]}]"

      images_gib="${IMAGES_QUOTA[${tenant}]:-${IMAGES_DEFAULT}}"
      files_gib="${FILES_QUOTA[${tenant}]:-${FILES_DEFAULT}}"
      backups_gib="${BACKUPS_QUOTA[${tenant}]:-${BACKUPS_DEFAULT}}"

      quota_ds='[]'
      for ds_id in ${IMAGES_DS_LIST[${zone[${zone_id},vip]}]}
      do
        quota_ds=$(jq --arg ds "${ds_id}" '
          . + [{ID: $ds, IMAGES: "0", SIZE: "0"}]
        ' <<< "$quota_ds")
      done  # ds_id

      images=""
      for cluster_id in "${clusters[@]}"
      do
        [[ " ${images} " == *" ${IMAGES_DS[${zone[${zone_id},vip]},${cluster_id}]} "* ]] && continue
        [[ -n "${images}" ]] && images+=" "
        images+="${IMAGES_DS[${zone[${zone_id},vip]},${cluster_id}]}"
        echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} tenant=${tenant} group_id=${group_id} images_ds=${IMAGES_DS[${zone[${zone_id},vip]},${cluster_id}]} images_gib=${images_gib} images_n=-1"
        quota_ds=$(jq \
          --arg images_id "${IMAGES_DS[${zone[${zone_id},vip]},${cluster_id}]}" \
          --arg images_size "$((images_gib * 1024))" '
          map(
            if .ID == $images_id
            then . + {IMAGES: "-1", SIZE: $images_size}
            else .
            end
          )
        ' <<< "$quota_ds")
      done  # cluster_id

      (( backups_gib == 0 )) && backups_n=0 || backups_n="-1"
      quota_ds=$(jq \
        --arg files_id "${FILES_DS[${zone[${zone_id},vip]}]}" \
        --arg backups_id "${BACKUPS_DS[${zone[${zone_id},vip]}]}" \
        --arg files_size "$((files_gib * 1024))" \
        --arg backups_size "$((backups_gib * 1024))" \
        --arg backups_n "${backups_n}" '
          . + [{ID: $files_id, IMAGES: "-1", SIZE: $files_size}]
          | . + [{ID: $backups_id, IMAGES: $backups_n, SIZE: $backups_size}]
      ' <<< "$quota_ds")
      echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} tenant=${tenant} group_id=${group_id} files_ds=${FILES_DS[${zone[${zone_id},vip]}]} files_gib=${files_gib} files_n=-1"
      echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} tenant=${tenant} group_id=${group_id} backups_ds=${BACKUPS_DS[${zone[${zone_id},vip]}]} backups_gib=${backups_gib} backups_n=${backups_n}"
  
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

      $ssh ${zone[${zone_id},vip]} "tee /var/tmp/one-tenant-quota-new &>/dev/null" <<< "${quota}"

      echo -n "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} tenant=${tenant} quota_set="
      if $ssh ${zone[${zone_id},vip]} "sudo -u oneadmin onegroup quota ${group_id} /var/tmp/one-tenant-quota-new"
      then
        echo ok
      else
        echo error
        $ssh ${zone[${zone_id},vip]} "sudo cat /var/tmp/one-tenant-quota-new"
      fi

      $ssh ${zone[${zone_id},vip]} "sudo rm -f /var/tmp/one-tenant-quota-new"
    done  # group_id
  done  # zone_id
}  # quota_ds_set



claugine_cli_commands+=( "\
  quota_tenant_get           # show quota and usage for tenant
    tenant=STRING            #"
)
function quota_tenant_get() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n zone=${zone_data}
  local tenant="${tenant:-}"
  local zone_id
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
  local vms vms_used

  if [[ -z "${zone[@]:-}" ]]
  then
    echo
    echo "!!! ERROR !!! ONE FE zones data ${zone_data} is not initialized"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    echo "to initialize data: fe_data_refresh [data=var name] fe=IP"
    return 1
  fi

  group_name="tenant-${tenant}"
  for zone_id in ${zone[list]}
  do
    echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} zone_state=${zone[${zone_id},state]} action=quota_tenant_get"

    json=$($ssh ${zone[${zone_id},vip]} "sudo -u oneadmin onegroup show ${group_name} -j 2>/dev/null")
    group_id=$(jq -r '.GROUP.ID // ""' <<< "${json}")

    echo -n "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} tenant=${tenant} group_name=${group_name} group_id=${group_id} quota_defined="
    if jq -e '
      .GROUP.VM_QUOTA.VM
      | arrays
      | any(.[]; .CPU != null)
    ' <<< "${json}" >/dev/null
    then
      echo yes
    else
      echo no
      continue
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
      $ssh ${zone[${zone_id},vip]} "sudo -u oneadmin oneuser show ${admin_id} -j 2>/dev/null" |
      jq -r '.USER.NAME // ""'
    )
    quotas[${zone[${zone_id},vip]},${group_id},admin,id]="${admin_id}"
    quotas[${zone[${zone_id},vip]},${group_id},admin,name]="${admin_name}"
    echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} tenant=${tenant} group_name=${group_name} group_id=${group_id} admin_name=${admin_name} admin_id=${admin_id}"

    id_list=""
    while IFS=$'\t' read -r id size size_used images images_used
    do
      [[ " ${id_list} " != *" ${id} "* ]] && id_list+=" ${id}"

      (( size > 0 )) && (( size /= 1024 ))
      (( size_used > 0 )) && (( size_used /= 1024 ))
      quotas[${zone[${zone_id},vip]},${group_id},ds,${id},size]="${size}"
      quotas[${zone[${zone_id},vip]},${group_id},ds,${id},images]="${images}"
      quotas[${zone[${zone_id},vip]},${group_id},ds,${id},size_used]="${size_used}"
      quotas[${zone[${zone_id},vip]},${group_id},ds,${id},images_used]="${images_used}"
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

    quotas[${zone[${zone_id},vip]},${group_id},ds,list]=$(
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
      [[ " ${id_list} " != *" ${id} "* ]] && id_list+=" ${id}"

      [[ ${cpu_used} = *.* ]] && cpu_used=$(( ${cpu_used%.*} + 1 ))
      [[ ${run_cpu_used} = *.* ]] && run_cpu_used=$(( ${run_cpu_used%.*} + 1 ))

      (( ram > 0 )) && (( ram /= 1024 ))
      (( run_ram > 0 )) && (( run_ram /= 1024 ))
      (( ram_used > 0 )) && (( ram_used /= 1024 ))
      (( run_ram_used > 0 )) && (( run_ram_used /= 1024 ))

      (( disk_size > 0 )) && (( disk_size /= 1024 ))
      (( disk_size_used > 0 )) && (( disk_size_used /= 1024 ))

      quotas[${zone[${zone_id},vip]},${group_id},cl,${id},vms]="${vms}"
      quotas[${zone[${zone_id},vip]},${group_id},cl,${id},run_vms]="${run_vms}"
      quotas[${zone[${zone_id},vip]},${group_id},cl,${id},vms_used]="${vms_used}"
      quotas[${zone[${zone_id},vip]},${group_id},cl,${id},run_vms_used]="${run_vms_used}"

      quotas[${zone[${zone_id},vip]},${group_id},cl,${id},cpu]="${cpu}"
      quotas[${zone[${zone_id},vip]},${group_id},cl,${id},run_cpu]="${run_cpu}"
      quotas[${zone[${zone_id},vip]},${group_id},cl,${id},cpu_used]="${cpu_used}"
      quotas[${zone[${zone_id},vip]},${group_id},cl,${id},run_cpu_used]="${run_cpu_used}"

      quotas[${zone[${zone_id},vip]},${group_id},cl,${id},ram]="${ram}"
      quotas[${zone[${zone_id},vip]},${group_id},cl,${id},run_ram]="${run_ram}"
      quotas[${zone[${zone_id},vip]},${group_id},cl,${id},ram_used]="${ram_used}"
      quotas[${zone[${zone_id},vip]},${group_id},cl,${id},run_ram_used]="${run_ram_used}"

      quotas[${zone[${zone_id},vip]},${group_id},cl,${id},gpu]="${pci_dev}"
      quotas[${zone[${zone_id},vip]},${group_id},cl,${id},run_gpu]="${run_pci_dev}"
      quotas[${zone[${zone_id},vip]},${group_id},cl,${id},gpu_used]="${pci_dev_used}"
      quotas[${zone[${zone_id},vip]},${group_id},cl,${id},run_gpu_used]="${run_pci_dev_used}"

      quotas[${zone[${zone_id},vip]},${group_id},cl,${id},disk_size]="${disk_size}"
      quotas[${zone[${zone_id},vip]},${group_id},cl,${id},disk_size_used]="${disk_size_used}"
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

    quotas[${zone[${zone_id},vip]},${group_id},cl,list]=$(
      printf '%s\n' ${id_list} |
      sort -n |
      xargs
    )

    echo
    printf "%-8s %10s %10s %10s %10s %14s %14s %14s %10s %10s\n" \
      "CLUSTER" "VMS" "VMS/RUN" "CPU" "CPU/RUN" "RAM GiB" "RAM/RUN GiB" "DISK GiB" "GPU" "GPU/RUN"
    printf "%-8s %10s %10s %10s %10s %14s %14s %14s %10s %10s\n" \
      "-------" "----------" "----------" "----------" "----------" "--------------" "--------------" "--------------" "----------" "----------"
    for id in ${quotas[${zone[${zone_id},vip]},${group_id},cl,list]}
    do
      printf "%-8s %10s %10s %10s %10s %14s %14s %14s %10s %10s\n" \
        "${id}" \
        "${quotas[${zone[${zone_id},vip]},${group_id},cl,${id},vms]}/${quotas[${zone[${zone_id},vip]},${group_id},cl,${id},vms_used]}" \
        "${quotas[${zone[${zone_id},vip]},${group_id},cl,${id},run_vms]}/${quotas[${zone[${zone_id},vip]},${group_id},cl,${id},run_vms_used]}" \
        "${quotas[${zone[${zone_id},vip]},${group_id},cl,${id},cpu]}/${quotas[${zone[${zone_id},vip]},${group_id},cl,${id},cpu_used]}" \
        "${quotas[${zone[${zone_id},vip]},${group_id},cl,${id},run_cpu]}/${quotas[${zone[${zone_id},vip]},${group_id},cl,${id},run_cpu_used]}" \
        "${quotas[${zone[${zone_id},vip]},${group_id},cl,${id},ram]}/${quotas[${zone[${zone_id},vip]},${group_id},cl,${id},ram_used]}" \
        "${quotas[${zone[${zone_id},vip]},${group_id},cl,${id},run_ram]}/${quotas[${zone[${zone_id},vip]},${group_id},cl,${id},run_ram_used]}" \
        "${quotas[${zone[${zone_id},vip]},${group_id},cl,${id},disk_size]}/${quotas[${zone[${zone_id},vip]},${group_id},cl,${id},disk_size_used]}" \
        "${quotas[${zone[${zone_id},vip]},${group_id},cl,${id},gpu]}/${quotas[${zone[${zone_id},vip]},${group_id},cl,${id},gpu_used]}" \
        "${quotas[${zone[${zone_id},vip]},${group_id},cl,${id},run_gpu]}/${quotas[${zone[${zone_id},vip]},${group_id},cl,${id},run_gpu_used]}"
    done  # id

    echo
    printf "%-10s %14s %14s\n" \
      "DATASTORE" "SIZE GiB" "IMAGES"
    printf "%-10s %14s %14s\n" \
      "---------" "--------------" "--------------"
    for id in ${quotas[${zone[${zone_id},vip]},${group_id},ds,list]}
    do
      printf "%-10s %14s %14s\n" \
        "${id}" \
        "${quotas[${zone[${zone_id},vip]},${group_id},ds,${id},size]}/${quotas[${zone[${zone_id},vip]},${group_id},ds,${id},size_used]}" \
        "${quotas[${zone[${zone_id},vip]},${group_id},ds,${id},images]}/${quotas[${zone[${zone_id},vip]},${group_id},ds,${id},images_used]}"
    done  # id
  done  # vip
}  # quota_tenant_get



claugine_cli_commands+=( "\
  quota_vcpu_conf_show       # show VCPU configuration in oned.conf"
)
function quota_vcpu_conf_show() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n zone=${zone_data}
  local zones="${zones:-ALL}"
  local zone_id node

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
    echo "zone_id=${zone_id} zone_name=${zone[${zone_id},name]} zone_vip=${zone[${zone_id},vip]} zone_state=${zone[${zone_id},state]} action=quota_vcpu_conf_show"
    for node in "${FE_NODE_ROLES[@]}"
    do
      echo "zone_id=${zone_id} node_role=${node} node_name=${zone[${zone_id},${node},name]} node_ip=${zone[${zone_id},${node},ip]}"
      $ssh ${zone[${zone_id},${node},ip]} "sudo grep -E '^.*QUOTA_VM_ATTRIBUTE.*VCPU' /etc/one/oned.conf"
    done  # node
  done  # zone_id

  return 0
}  # quota_vcpu_conf_show
