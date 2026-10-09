#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



# INDEX
#   _vyos_vm_wait        wait for a VM state
#   vyos_image_build     phase 1: latest VyOS Stream ISO with claugine scripts, builder VM
#   vyos_image_finalize  phase 2: VyOS image from the builder VM, published to its platform



# wait for a VM state
#   platform=NAME vm_id=N state=N limit=N
# return 0 - the VM is in the state
#        1 - timeout
function _vyos_vm_wait() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local start=$(date '+%s')
  local fe; fe=$(inv_value var=INV path=${platform}.fe.vip)
  local current

  _log -n vm_id="${vm_id}" action="wait_state" state="${state}" status="."
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



help_data[vyos_image_build]="\
  vyos_image_build           # phase 1: latest VyOS Stream ISO with claugine scripts, builder VM
    platform=NAME            #   platforms.<site>.<platform> of the builder VM
    cluster=ID|NAME          #   cluster of the builder VM
    vnet=STRING              #   builder VM network, name or id
    addr=IP                  #   builder VM address, must be reachable by ssh from this host
    gw=IP                    #   optional
    dns=IP                   #   optional
    url=URL                  #   optional: ISO url, default - latest from <CONFIG.vyos.url>
                             #   ISO is kept in repos.zakroma.local_dir/vyos of the platform's site; the FE downloads it
                             #     from repos.zakroma.url_base/vyos if defined, rsync from this host otherwise
    empty_image=STRING       #   default: \"<CONFIG.vyos.builder.image>\"
    ds=ID                    #   default: the default IMAGE datastore of the platform, see image_upload"
# return 0 - builder VM created and powered off
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            not confirmed
#            cluster, vnet, addr or the public key missing, no repository
#            iso_get_vyos failed
#            builder VM already exists
#            ISO upload failed
#            VM not created
#            waiting for the VM failed
function vyos_image_build() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local cluster="${cluster:-}"
  local vnet="${vnet:-}"
  local addr="${addr:-}"
  local url="${url:-}"
  local empty_image="${empty_image:-$(inv_value var=CONFIG path=vyos.builder.image)}"
  local ds="${ds:-}"
  local fe ver iso_name iso_url vm_name site directory repo_url vip

  data_runtime_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)
  fe=${vip}

  if [[ -z "${cluster}" || -z "${vnet}" || -z "${addr}" || ! -f "${SSH_KEYF}.pub" ]]
  then
    _log_error "cluster, vnet and addr must be defined, public key ${SSH_KEYF}.pub must exist"
    return 1
  fi

  # local repository and its URL: repos.zakroma of the zone's site
  site="${platform#platforms.}"
  site="${site%%.*}"
  directory=$(_iso_repo_dir os=vyos site="${site}") || return 1
  repo_url=$(inv_value var=INV path=resources.${site}.repos.zakroma.url_base)
  [[ -n "${repo_url}" ]] && repo_url="${repo_url%/}/vyos"

  # latest VyOS Stream ISO with the claugine scripts
  iso_get_vyos url="${url}" directory="${directory}" || return 1
  ver="${iso_info[version]}"
  iso_name="$(inv_value var=CONFIG path=vyos.image_name) ${ver} ISO"
  vm_name="$(inv_value var=CONFIG path=vyos.builder.prefix)${ver}"
  _log_std vyos_version="${ver}" iso="${iso_info[iso]}" vm_name="${vm_name}"

  if $ssh ${fe} "sudo -u oneadmin onevm show \"${vm_name}\" -j" &>/dev/null
  then
    _log_error "builder VM \"${vm_name}\" already exists - terminate it first"
    return 1
  fi

  # upload ISO: the FE downloads it from the local repo if its URL is defined, rsync otherwise
  iso_url=""
  [[ -n "${repo_url}" ]] && iso_url="${repo_url%/}/${iso_info[file]}"
  image_upload platform="${platform}" file="${iso_info[iso]}" url="${iso_url}" name="${iso_name}" type=CDROM prefix=sd ds="${ds}" || return 1

  # builder VM: empty disk + ISO, boot from ISO
  vm_create platform="${platform}" cluster="${cluster}" \
    name="${vm_name}" hostname=vyos-builder \
    cpu="$(inv_value var=CONFIG path=vyos.builder.cpu)" ram="$(inv_value var=CONFIG path=vyos.builder.ram)" \
    image1="${empty_image}" disk1="$(inv_value var=CONFIG path=vyos.builder.disk)" \
    image2="${iso_name}" disk2=iso \
    boot="disk1,disk0" \
    vnet1="${vnet}" addr1="${addr}" gw1="${gw:-}" dns1="${dns:-}" \
    user=vyos pswd="$(inv_value var=CONFIG path=vyos.builder.password)" key="$(< "${SSH_KEYF}.pub")" \
    autostart=no \
    || return 1

  cat <<EOF

next - manual steps on the VM console (FireEdge VNC):
  1. login: vyos / vyos
  2. install image   (default answers, "no" to reboot)
  3. sudo bash /usr/lib/live/mount/medium/claugine/vyos-image-prepare
     the VM powers off when done
  4. on this host: vyos_image_finalize platform=${platform} vm=${vm_name}
EOF

  return 0
}  # vyos_image_build



help_data[vyos_image_finalize]="\
  vyos_image_finalize        # phase 2: VyOS image from the builder VM, published to its platform
    platform=NAME            #   platforms.<site>.<platform> of the builder VM
    vm=STRING                #   builder VM name or id
    ip=IP                    #   optional: builder VM address, default - first NIC
    name=STRING              #   optional: image name, default - \"<CONFIG.vyos.image_name> <version>\"
    ds=ID                    #   optional: default - the default IMAGE datastore of the platform, see image_upload
                             #   the image is kept in repos.zakroma.local_dir/vyos of the platform's site;
                             #     to other platforms: image_publish platform=... repo=vyos, one by one
    limit=N(<CONFIG.vyos.builder.wait>)       #   seconds to wait for boot and power off
    confirm=yes              #   to suppress interactive confirmation"
# return 0 - image created and published
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            not confirmed
#            no repository, builder VM not found
#            VM not powered off or has no IP
#            ISO not detached
#            VM not started
#            no ssh after the first run
#            VM not powered off after the run
#            disk not saved as image
#            image download failed
#            image_publish failed
function vyos_image_finalize() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local vm="${vm:-}"
  local ip="${ip:-}"
  local name="${name:-}"
  local ds="${ds:-}"
  local limit="${limit:-$(inv_value var=CONFIG path=vyos.builder.wait)}"
  local dir
  local fe json vm_id vm_name ver disk_id out tmp_name tmp_id format file start
  local vyos_ssh vip

  data_runtime_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)
  fe=${vip}
  dir=$(_iso_repo_dir os=vyos platform="${platform}") || return 1

  json=$($ssh ${fe} "sudo -u oneadmin onevm show \"${vm}\" -j 2>/dev/null")
  if [[ -z "${vm}" || -z "${json}" ]]
  then
    _log_error "builder VM \"${vm}\" not found"
    return 1
  fi
  vm_id=$(jq -r '.VM.ID' <<< "${json}")
  vm_name=$(jq -r '.VM.NAME' <<< "${json}")
  ver="${vm_name#$(inv_value var=CONFIG path=vyos.builder.prefix)}"
  [[ -z "${ip}" ]] && ip=$(jq -r '.VM.TEMPLATE.NIC | if type == "array" then .[0] else . end | .IP // empty' <<< "${json}")
  name="${name:-$(inv_value var=CONFIG path=vyos.image_name) ${ver}}"

  if [[ "$(jq -r '.VM.STATE' <<< "${json}")" != 8 || -z "${ip}" ]]
  then
    _log_error "VM \"${vm_name}\" [${vm_id}] must be powered off (vyos-image-prepare does it) and have an IP: ip=\"${ip}\""
    return 1
  fi

  _log_std vm_name="${vm_name}" vm_id="${vm_id}" vm_ip="${ip}" vyos_version="${ver}" image="${name}"
  echo "existing images named \"${name}\" on the platform: deleted if not used by VMs, renamed to \"${name} (YYYY-MM-DD)\" otherwise"
  _stop_without_confirmation confirm=${confirm:-no} && return 1

  # 1. detach the ISO
  for disk_id in $(jq -r '.VM.TEMPLATE.DISK | if type == "array" then .[] else . end | select(.TYPE == "CDROM") | .DISK_ID' <<< "${json}")
  do
    _log vm_id="${vm_id}" disk_id="${disk_id}" action="detach_iso"
    $ssh ${fe} "sudo -u oneadmin onevm disk-detach ${vm_id} ${disk_id}" || return 1
  done  # disk_id
  start=$(date '+%s')
  until [[ -z "$($ssh ${fe} "sudo -u oneadmin onevm show ${vm_id} -j 2>/dev/null" |
                jq -r '.VM.TEMPLATE.DISK | if type == "array" then .[] else . end | select(.TYPE == "CDROM") | .DISK_ID')" ]]
  do
    if (( $(date '+%s') - start > 120 ))
    then
      _log_error "ISO is still attached to VM ${vm_id} after 120s"
      return 1
    fi
    sleep 5
  done  # until
  _vyos_vm_wait platform="${platform}" vm_id="${vm_id}" state=8 limit=60 || return 1

  # 2. boot: first run applies the context, creates the flag and reboots
  _log vm_id="${vm_id}" action="boot"
  $ssh ${fe} "sudo -u oneadmin onevm resume ${vm_id}" || return 1

  # 3. wait for ssh after the first run reboot: flag older than the boot time
  vyos_ssh="ssh -i ${SSH_KEYF} -l vyos -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR ${ip}"
  _log -n vm_id="${vm_id}" vm_ip="${ip}" action="wait_first_run" status="."
  start=$(date '+%s')
  until ${vyos_ssh} 'f=/opt/vyatta/etc/init.flag; [[ -f $f ]] && (( $(stat -c %Y $f) < $(date -d "$(uptime -s)" +%s) ))' &>/dev/null
  do
    if (( $(date '+%s') - start > limit ))
    then
      echo " timeout ${limit}s"
      _log_error "no ssh to vyos@${ip} after the first run - check the VM console"
      return 1
    fi
    sleep 10
    echo -n "."
  done
  echo " ready"

  # 4. reset the builder settings and power off, detached from ssh
  _log vm_id="${vm_id}" action="finalize" script="/config/scripts/vyos-image-finalize"
  ${vyos_ssh} "setsid nohup bash /opt/vyatta/etc/config/scripts/vyos-image-finalize > /tmp/vyos-image-finalize.log 2>&1 < /dev/null &" || return 1
  _vyos_vm_wait platform="${platform}" vm_id="${vm_id}" state=8 limit="${limit}" || return 1

  # 5. save the disk as a temporary image
  json=$($ssh ${fe} "sudo -u oneadmin onevm show ${vm_id} -j 2>/dev/null")
  disk_id=$(jq -r '[.VM.TEMPLATE.DISK | if type == "array" then .[] else . end | select(.TYPE != "CDROM")][0].DISK_ID // empty' <<< "${json}")
  tmp_name="$(inv_value var=CONFIG path=vyos.builder.prefix)${ver} disk $(date '+%Y%m%d%H%M%S')"
  out=$($ssh ${fe} "sudo -u oneadmin onevm disk-saveas ${vm_id} ${disk_id} \"${tmp_name}\"" 2>&1)
  tmp_id="${out##* }"
  if [[ -z "${disk_id}" || ! "${tmp_id}" =~ ^[0-9]+$ ]]
  then
    _log_error "can not save disk [${disk_id}] of VM ${vm_id}: ${out}"
    return 1
  fi
  _log vm_id="${vm_id}" disk_id="${disk_id}" action="saveas" image="${tmp_name}" image_id="${tmp_id}"
  image_wait platform="${platform}" image="${tmp_id}" || return 1

  # 6. download
  format=$($ssh ${fe} "sudo -u oneadmin oneimage show ${tmp_id} -j" | jq -r '.IMAGE.FORMAT // "qcow2"')
  file="${dir}/vyos-${ver}.${format}"
  image_download platform="${platform}" image="${tmp_id}" file="${file}" || return 1

  # 7. publish to the platform
  image_publish platform="${platform}" file="${file}" repo=vyos name="${name}" type=OS prefix=vd format="${format}" ds="${ds}" || return 1

  # 8. cleanup
  _log_std image_id="${tmp_id}" action="delete_temporary"
  $ssh ${fe} "sudo -u oneadmin oneimage delete ${tmp_id}"

  echo
  echo "done: image \"${name}\" ${platform}, local copy ${file}"
  echo "to other platforms: image_publish platform=platforms.<site>.<platform> file=\"${file}\" repo=vyos name=\"${name}\" format=${format}"
  echo "builder VM \"${vm_name}\" [${vm_id}] is powered off - terminate it when not needed"
  return 0
}  # vyos_image_finalize
