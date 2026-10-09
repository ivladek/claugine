#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



# INDEX
#   vyos_image_build     phase 1: latest VyOS Stream ISO with claugine scripts, builder VM
#   vyos_image_finalize  phase 2: VyOS image from the builder VM, published to its platform



help_data[vyos_image_build]="\
  vyos_image_build           # phase 1: latest VyOS Stream ISO with claugine scripts, builder VM
    platform=NAME            #   platforms.<site>.<platform> of the builder VM
    cluster=ID|NAME(0)       #   cluster of the builder VM
    vnet=STRING              #   builder VM network, name or id
    addr=IP|AUTO             #   builder VM address, default - a free IP leased from the VNet;
                             #     must be reachable by ssh from this host
    empty_image=STRING       #   default: \"<CONFIG.vyos.builder.image>\"
    ds=ID                    #   default: the default IMAGE datastore of the platform, see image_upload"
# return 0 - builder VM created and powered off
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            not confirmed
#            vnet or the public key missing, no repository
#            iso_get_vyos failed
#            builder VM already exists
#            ISO upload failed
#            VM not created
#            waiting for the VM failed
function vyos_image_build() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local cluster="${cluster:-0}"
  local vnet="${vnet:-}"
  local addr="${addr:-auto}"
  local empty_image="${empty_image:-$(inv_value var=CONFIG path=vyos.builder.image)}"
  local ds="${ds:-}"
  local fe ver iso_name iso_url iso_file vm_name vm_ip site directory repo_url vip content

  data_runtime_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)
  fe=${vip}

  if [[ -z "${vnet}" ]]
  then
    _log_error "vnet must be defined"
    return 1
  fi

  # local repository and its URL: repos.zakroma of the zone's site
  site="${platform#platforms.}"
  site="${site%%.*}"
  directory="$(inv_value var=INV path=resources.${site}.repos.zakroma.local_dir)"
  if [[ -z "${directory}" ]] || ! mkdir -p "${directory%/}/vyos"
  then
    _log_error "repository resources.${site}.repos.zakroma.local_dir/vyos not defined or can not be created"
    return 1
  fi
  directory="${directory%/}/vyos"
  repo_url=$(inv_value var=INV path=resources.${site}.repos.zakroma.url_base)
  [[ -n "${repo_url}" ]] && repo_url="${repo_url%/}/vyos"

  # latest VyOS Stream ISO: its url gives the names - vyos-<version>-generic-amd64.iso, the modified one -claugine.iso
  if [[ -z "${url}" ]]
  then
    url="$(
      curl -fsSL "$(inv_value var=CONFIG path=vyos.url)" |
      grep -oE "https://[^\"' ]+-generic-amd64\.iso" |
      head -1
    )"
  fi
  iso_file="${url##*/}"
  iso_file="${iso_file%.iso}-claugine.iso"
  ver="${url##*/vyos-}"
  ver="${ver%-generic-amd64.iso}"
  iso_get_vyos url="${url}" directory="${directory}" || return 1
  iso_name="$(inv_value var=CONFIG path=vyos.image_name) ${ver} ISO"
  vm_name="$(inv_value var=CONFIG path=vyos.builder.prefix)${ver}"
  _log_std vyos_version="${ver}" iso="${directory}/${iso_file}" vm_name="${vm_name}"

  if $ssh ${fe} "sudo -u oneadmin onevm show \"${vm_name}\" -j" &>/dev/null
  then
    _log_error "builder VM \"${vm_name}\" already exists - terminate it first"
    return 1
  fi

  # upload ISO: the FE downloads it from the local repo if its URL is defined, rsync otherwise
  iso_url=""
  [[ -n "${repo_url}" ]] && iso_url="${repo_url%/}/${iso_file}"
  image_upload platform="${platform}" file="${directory}/${iso_file}" url="${iso_url}" name="${iso_name}" type=CDROM prefix=sd ds="${ds}" || return 1

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

  # the address of the builder VM: as given, or leased from the VNet with addr=auto
  vm_ip=$(
    $ssh ${fe} "sudo -u oneadmin onevm show \"${vm_name}\" -j 2>/dev/null" |
    jq -r '.VM.TEMPLATE.NIC | if type == "array" then .[0] else . end | .IP // ""'
  )
  _log_std vm_name="${vm_name}" vm_ip="${vm_ip}" action="builder_created"

  _log
  # the text of config/templates, its ${variables} filled in from this function
  content=$(inv_value var=TEMPLATES path=vyos.builder.content)
  _log "$(eval "echo \"${content//\"/\\\"}\"")"

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
    limit=N                  #   seconds to wait for boot and power off, default is <CONFIG.vyos.builder.wait>
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
  local dir site
  local fe json vm_id vm_name ver disk_id out tmp_name tmp_id format file start
  local vyos_ssh vip content

  data_runtime_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)
  fe=${vip}
  site="${platform#platforms.}"
  site="${site%%.*}"
  dir="$(inv_value var=INV path=resources.${site}.repos.zakroma.local_dir)"
  if [[ -z "${dir}" ]] || ! mkdir -p "${dir%/}/vyos"
  then
    _log_error "repository resources.${site}.repos.zakroma.local_dir/vyos not defined or can not be created"
    return 1
  fi
  dir="${dir%/}/vyos"

  json=$($ssh ${fe} "sudo -u oneadmin onevm show \"${vm}\" -j 2>/dev/null")
  if [[ -z "${vm}" || -z "${json}" ]]
  then
    _log_error "builder VM \"${vm}\" not found"
    return 1
  fi
  vm_id=$(jq -r '.VM.ID' <<< "${json}")
  vm_name=$(jq -r '.VM.NAME' <<< "${json}")
  ver="${vm_name#$(inv_value var=CONFIG path=vyos.builder.prefix)}"
  [[ -z "${ip}" ]] && ip=$(jq -r '.VM.TEMPLATE.NIC | if type == "array" then .[0] else . end | .IP // ""' <<< "${json}")
  name="${name:-$(inv_value var=CONFIG path=vyos.image_name) ${ver}}"

  if [[ "$(jq -r '.VM.STATE' <<< "${json}")" != 8 || -z "${ip}" ]]
  then
    _log_error "VM \"${vm_name}\" [${vm_id}] must be powered off (vyos-image-prepare does it) and have an IP: ip=\"${ip}\""
    return 1
  fi

  _log_std vm_name="${vm_name}" vm_id="${vm_id}" vm_ip="${ip}" vyos_version="${ver}" image="${name}"
  content=$(inv_value var=TEMPLATES path=vyos.finalize_confirm.content)
  _log "$(eval "echo \"${content//\"/\\\"}\"")"
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
  vm_wait platform="${platform}" vm="${vm_id}" state=POWEROFF limit=60 || return 1

  # 2. boot: first run applies the context, creates the flag and reboots
  _log vm_id="${vm_id}" action="boot"
  $ssh ${fe} "sudo -u oneadmin onevm resume ${vm_id}" || return 1

  # 3. wait for ssh after the first run reboot: flag older than the boot time
  vyos_ssh="ssh -i ${SSH_KEYF} -l vyos -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR ${ip}"
  _log -n vm_id="${vm_id}" vm_ip="${ip}" action="wait_first_run" status="[."
  start=$(date '+%s')
  until ${vyos_ssh} 'f=/opt/vyatta/etc/init.flag; [[ -f $f ]] && (( $(stat -c %Y $f) < $(date -d "$(uptime -s)" +%s) ))' &>/dev/null
  do
    if (( $(date '+%s') - start > limit ))
    then
      _log "] result=timeout limit=${limit}s"
      _log_error "no ssh to vyos@${ip} after the first run - check the VM console"
      return 1
    fi
    sleep 10
    _log -n "."
  done
  _log "] result=ready"

  # 4. reset the builder settings and power off, detached from ssh
  _log vm_id="${vm_id}" action="finalize" script="/config/scripts/vyos-image-finalize"
  ${vyos_ssh} "setsid nohup bash /opt/vyatta/etc/config/scripts/vyos-image-finalize > /tmp/vyos-image-finalize.log 2>&1 < /dev/null &" || return 1
  vm_wait platform="${platform}" vm="${vm_id}" state=POWEROFF limit="${limit}" || return 1

  # 5. save the disk as a temporary image
  json=$($ssh ${fe} "sudo -u oneadmin onevm show ${vm_id} -j 2>/dev/null")
  disk_id=$(jq -r '[.VM.TEMPLATE.DISK | if type == "array" then .[] else . end | select(.TYPE != "CDROM")][0].DISK_ID // ""' <<< "${json}")
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

  _log
  _log_std image="${name}" local_copy="${file}" action="vyos_image_finalize" result="done"
  content=$(inv_value var=TEMPLATES path=vyos.finalize_done.content)
  _log "$(eval "echo \"${content//\"/\\\"}\"")"
  return 0
}  # vyos_image_finalize
