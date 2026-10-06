#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



claugine_cli_commands+=( "\
  vyos_image_build           # phase 1: latest VyOS Stream ISO with claugine scripts, builder VM
    zone_id=N                #   zone of the builder VM
    cluster=ID|NAME          #   cluster of the builder VM
    vnet=STRING              #   builder VM network, name or id
    addr=IP                  #   builder VM address, must be reachable by ssh from this host
    gw=IP                    #   optional
    dns=IP                   #   optional
    url=URL                  #   optional: ISO url, default - latest from ${VYOS_STREAM_PAGE}
                             #   ISO is kept in \${DIR_VYOS_REPO}, the FE downloads it from \${URL_VYOS_REPO[zone VIP]} if defined
    empty_image=STRING       #   default: \"${VYOS_EMPTY_IMAGE}\"
    ds=ID                    #   default: first of IMAGES_DS_LIST for the zone VIP"
)
function vyos_image_build() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n _zone=${zone_data}
  local cluster="${cluster:-}"
  local vnet="${vnet:-}"
  local addr="${addr:-}"
  local url="${url:-}"
  local empty_image="${empty_image:-${VYOS_EMPTY_IMAGE}}"
  local ds="${ds:-}"
  local fe ver iso_name iso_url vm_name

  _zone_init || return 1
  _zone_check zone_id="${zone_id:-}" || return 1
  fe=$(_zone_vip ${zone_id})

  if [[ -z "${cluster}" || -z "${vnet}" || -z "${addr}" || ! -f "${SSH_KEYF}.pub" ]]
  then
    echo
    echo "!!! ERROR !!! cluster, vnet and addr must be defined, public key ${SSH_KEYF}.pub must exist"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 2
  fi

  # latest VyOS Stream ISO with the claugine scripts
  iso_get_vyos url="${url}" directory="${DIR_VYOS_REPO}" || return 3
  ver="${iso_info[version]}"
  iso_name="${VYOS_IMAGE_NAME} ${ver} ISO"
  vm_name="${VYOS_BUILDER_PREFIX}${ver}"
  echo "$(_zone_label ${zone_id}) vyos_version=${ver} iso=${iso_info[iso]} vm_name=${vm_name}"

  if $ssh ${fe} "sudo -u oneadmin onevm show \"${vm_name}\" -j" &>/dev/null
  then
    echo
    echo "!!! ERROR !!! builder VM \"${vm_name}\" already exists - terminate it first"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 4
  fi

  # upload ISO: the FE downloads it from the local repo if defined, rsync otherwise
  iso_url="${URL_VYOS_REPO[${fe}]:-}"
  [[ -n "${iso_url}" ]] && iso_url="${iso_url%/}/${iso_info[file]}"
  image_upload zone_id="${zone_id}" file="${iso_info[iso]}" url="${iso_url}" name="${iso_name}" type=CDROM prefix=sd ds="${ds}" || return 5

  # builder VM: empty disk + ISO, boot from ISO
  vm_create zone_id="${zone_id}" cluster="${cluster}" \
    name="${vm_name}" hostname=vyos-builder \
    cpu=1 ram=1 \
    image1="${empty_image}" disk1="${VYOS_BUILDER_DISK}" \
    image2="${iso_name}" disk2=iso \
    boot="disk1,disk0" \
    vnet1="${vnet}" addr1="${addr}" gw1="${gw:-}" dns1="${dns:-}" \
    user=vyos pswd="${VYOS_DEFAULT_PASSWORD}" key="$(< "${SSH_KEYF}.pub")" \
    autostart=no \
    || return 6

  cat <<EOF

next - manual steps on the VM console (FireEdge VNC):
  1. login: vyos / vyos
  2. install image   (default answers, "no" to reboot)
  3. sudo bash /usr/lib/live/mount/medium/claugine/vyos-image-prepare
     the VM powers off when done
  4. on this host: vyos_image_finalize zone_id=${zone_id} vm=${vm_name}
EOF

  return 0
}  # vyos_image_build



claugine_cli_commands+=( "\
  vyos_image_finalize        # phase 2: VyOS image from the builder VM, published to the zones
    zone_id=N                #   zone of the builder VM
    vm=STRING                #   builder VM name or id
    ip=IP                    #   optional: builder VM address, default - first NIC
    name=STRING              #   optional: image name, default - \"${VYOS_IMAGE_NAME} <version>\"
    zones=LIST|ALL           #   zones to publish the image to, default ALL
                             #     another installation: switch the context, then image_publish
    ds=ID                    #   optional: default - first of IMAGES_DS_LIST for each zone VIP
    limit=N(${VYOS_WAIT_LIMIT})       #   seconds to wait for boot and power off
    confirm=yes              #   to suppress interactive confirmation"
)
function vyos_image_finalize() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n _zone=${zone_data}
  local zones="${zones:-ALL}"
  local vm="${vm:-}"
  local ip="${ip:-}"
  local name="${name:-}"
  local ds="${ds:-}"
  local limit="${limit:-${VYOS_WAIT_LIMIT}}"
  local dir="${DIR_VYOS_REPO}"
  local fe json vm_id vm_name ver disk_id out tmp_name tmp_id format file start
  local vyos_ssh

  _zone_init || return 1
  _zone_check zone_id="${zone_id:-}" || return 1
  zones=$(_zones_resolve zones="${zones}") || return 1
  fe=$(_zone_vip ${zone_id})

  json=$($ssh ${fe} "sudo -u oneadmin onevm show \"${vm}\" -j 2>/dev/null")
  if [[ -z "${vm}" || -z "${json}" ]]
  then
    echo
    echo "!!! ERROR !!! builder VM \"${vm}\" not found"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 2
  fi
  vm_id=$(jq -r '.VM.ID' <<< "${json}")
  vm_name=$(jq -r '.VM.NAME' <<< "${json}")
  ver="${vm_name#${VYOS_BUILDER_PREFIX}}"
  [[ -z "${ip}" ]] && ip=$(jq -r '.VM.TEMPLATE.NIC | if type == "array" then .[0] else . end | .IP // empty' <<< "${json}")
  name="${name:-${VYOS_IMAGE_NAME} ${ver}}"

  if [[ "$(jq -r '.VM.STATE' <<< "${json}")" != 8 || -z "${ip}" ]]
  then
    echo
    echo "!!! ERROR !!! VM \"${vm_name}\" [${vm_id}] must be powered off (vyos-image-prepare does it) and have an IP: ip=\"${ip}\""
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 3
  fi

  echo "$(_zone_label ${zone_id}) vm_name=${vm_name} vm_id=${vm_id} vm_ip=${ip} vyos_version=${ver} image=\"${name}\" zones=[${zones}]"
  echo "existing images named \"${name}\" in the zones: deleted if not used by VMs, renamed to \"${name} (YYYY-MM-DD)\" otherwise"
  stop_without_confirmation confirm=${confirm:-no} && return 1

  # 1. detach the ISO
  for disk_id in $(jq -r '.VM.TEMPLATE.DISK | if type == "array" then .[] else . end | select(.TYPE == "CDROM") | .DISK_ID' <<< "${json}")
  do
    echo "vm_id=${vm_id} disk_id=${disk_id} action=detach_iso"
    $ssh ${fe} "sudo -u oneadmin onevm disk-detach ${vm_id} ${disk_id}" || return 4
  done  # disk_id
  start=$(date '+%s')
  until [[ -z "$($ssh ${fe} "sudo -u oneadmin onevm show ${vm_id} -j 2>/dev/null" |
                jq -r '.VM.TEMPLATE.DISK | if type == "array" then .[] else . end | select(.TYPE == "CDROM") | .DISK_ID')" ]]
  do
    if (( $(date '+%s') - start > 120 ))
    then
      echo
      echo "!!! ERROR !!! ISO is still attached to VM ${vm_id} after 120s"
      echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
      return 4
    fi
    sleep 5
  done  # until
  _vyos_vm_wait zone_id="${zone_id}" vm_id="${vm_id}" state=8 limit=60 || return 4

  # 2. boot: first run applies the context, creates the flag and reboots
  echo "vm_id=${vm_id} action=boot"
  $ssh ${fe} "sudo -u oneadmin onevm resume ${vm_id}" || return 5

  # 3. wait for ssh after the first run reboot: flag older than the boot time
  vyos_ssh="ssh -i ${SSH_KEYF} -l vyos -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR ${ip}"
  echo -n "vm_id=${vm_id} vm_ip=${ip} action=wait_first_run status=."
  start=$(date '+%s')
  until ${vyos_ssh} 'f=/opt/vyatta/etc/init.flag; [[ -f $f ]] && (( $(stat -c %Y $f) < $(date -d "$(uptime -s)" +%s) ))' &>/dev/null
  do
    if (( $(date '+%s') - start > limit ))
    then
      echo " timeout ${limit}s"
      echo
      echo "!!! ERROR !!! no ssh to vyos@${ip} after the first run - check the VM console"
      echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
      return 6
    fi
    sleep 10
    echo -n "."
  done
  echo " ready"

  # 4. reset the builder settings and power off, detached from ssh
  echo "vm_id=${vm_id} action=finalize script=/config/scripts/vyos-image-finalize"
  ${vyos_ssh} "setsid nohup bash /opt/vyatta/etc/config/scripts/vyos-image-finalize > /tmp/vyos-image-finalize.log 2>&1 < /dev/null &" || return 7
  _vyos_vm_wait zone_id="${zone_id}" vm_id="${vm_id}" state=8 limit="${limit}" || return 7

  # 5. save the disk as a temporary image
  json=$($ssh ${fe} "sudo -u oneadmin onevm show ${vm_id} -j 2>/dev/null")
  disk_id=$(jq -r '[.VM.TEMPLATE.DISK | if type == "array" then .[] else . end | select(.TYPE != "CDROM")][0].DISK_ID // empty' <<< "${json}")
  tmp_name="${VYOS_BUILDER_PREFIX}${ver} disk $(date '+%Y%m%d%H%M%S')"
  out=$($ssh ${fe} "sudo -u oneadmin onevm disk-saveas ${vm_id} ${disk_id} \"${tmp_name}\"" 2>&1)
  tmp_id="${out##* }"
  if [[ -z "${disk_id}" || ! "${tmp_id}" =~ ^[0-9]+$ ]]
  then
    echo
    echo "!!! ERROR !!! can not save disk [${disk_id}] of VM ${vm_id}: ${out}"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 8
  fi
  echo "vm_id=${vm_id} disk_id=${disk_id} action=saveas image=\"${tmp_name}\" image_id=${tmp_id}"
  image_wait zone_id="${zone_id}" image="${tmp_id}" || return 8

  # 6. download
  format=$($ssh ${fe} "sudo -u oneadmin oneimage show ${tmp_id} -j" | jq -r '.IMAGE.FORMAT // "qcow2"')
  file="${dir}/vyos-${ver}.${format}"
  image_download zone_id="${zone_id}" image="${tmp_id}" file="${file}" || return 9

  # 7. publish to the zones, the builder zone included
  image_publish zones="${zones}" file="${file}" repo=URL_VYOS_REPO name="${name}" type=OS prefix=vd format="${format}" ds="${ds}" || return 10

  # 8. cleanup
  echo "$(_zone_label ${zone_id}) image_id=${tmp_id} action=delete_temporary"
  $ssh ${fe} "sudo -u oneadmin oneimage delete ${tmp_id}"

  echo
  echo "done: image \"${name}\" installation=${zone_data} zones=[${zones}], local copy ${file}"
  echo "builder VM \"${vm_name}\" [${vm_id}] is powered off - terminate it when not needed"
  return 0
}  # vyos_image_finalize



# wait for a VM state, internal
#   zone_id=N vm_id=N state=N limit=N
function _vyos_vm_wait() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local start=$(date '+%s')
  local fe=$(_zone_vip ${zone_id})
  local current

  echo -n "vm_id=${vm_id} action=wait_state state=${state} status=."
  while :
  do
    current=$($ssh ${fe} "sudo -u oneadmin onevm show ${vm_id} -j 2>/dev/null" | jq -r '.VM.STATE // empty')
    if [[ "${current}" == "${state}" ]]
    then
      echo " ok"
      return 0
    fi
    if (( $(date '+%s') - start > limit ))
    then
      echo " timeout ${limit}s state=${current}"
      return 1
    fi
    sleep 5
    echo -n "."
  done
}  # _vyos_vm_wait
