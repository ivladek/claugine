#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



# INDEX
#   vyos_image_create            both phases in one run: vyos_image_vm_create then vyos_image_to_platforms
#   vyos_image_to_platforms      phase 2: create VyOS image from the image VM and push it to target platforms
#   vyos_image_vm_create         phase 1: create VyOS Image VM, includes manual operation inside VM console



help_data[vyos_image_create]="\
  vyos_image_create          # vyos_image_vm_create then vyos_image_to_platforms
    platform=NAME            #   platform to create Image VM
    cluster=ID|NAME(0)       #
    vnet=STRING              #
    [addr=ip|AUTO]           #
    [gw=IP]                  #
    [target=LIST|all]        #   platforms list to put image on, all or empty - every platform
    [wait=N(600)]            #   seconds to wait for power off of Image VM"
function vyos_image_create() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local cluster="${cluster:-0}"
  local vnet="${vnet:-}"
  local addr="${addr:-auto}"
  local gw="${gw:-}"
  local target="${target:-}"
  local vmfile="${DIR_RUNTIME}/vyos_image_create-$(date '+%s')"

  vyos_image_vm_create \
    platform="${platform:-}" \
    cluster="${cluster}" \
    vnet="${vnet}" \
    addr="${addr}" \
    gw="${gw}" \
    vmfile="${vmfile}" ||
  { rm -f "${vmfile}"; return 1; }

  vyos_image_to_platforms \
    platform="${platform}" \
    vmfile="${vmfile}" \
    target="${target}" \
    wait="${wait:-600}" \
    confirm=yes ||
  return 1

  return 0
}  # vyos_image_create




help_data[vyos_image_to_platforms]="\
  vyos_image_to_platforms    # Create VyOS image from the image VM and push it to target platforms
    platform=NAME            #   platform hosted Image VM
    vmfile=PATH              #   file with the Image VM id
    [target=LIST|all]        #   platforms list to put image on, all or empty - every platform
    [wait=N(600)]            #   seconds to wait for power off of Image VM
    confirm=yes              #   to suppress interactive confirmation"
function vyos_image_to_platforms() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local vmfile="${vmfile:-}"
  local target="${target:-}"
  local wait="${wait:-600}"
  local vm="" name cluster member errors=0
  local dir site
  local fe json vm_id vm_name ver disk_id out tmp_name tmp_id file iso_id
  local vip

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

  # the image VM: its id in vmfile
  [[ -f "${vmfile}" ]] && vm="$(< "${vmfile}")"
  json=$($ssh ${fe} "sudo -u oneadmin onevm show \"${vm}\" -j 2>/dev/null")
  if [[ ! "${vm}" =~ ^[0-9]+$ || -z "${json}" ]]
  then
    _log_error "image VM id \"${vm}\" of vmfile \"${vmfile}\" not found"
    return 1
  fi
  vm_id=$(jq -r '.VM.ID' <<< "${json}")
  vm_name=$(jq -r '.VM.NAME' <<< "${json}")
  ver="${vm_name#$(inv_value var=CONFIG path=vyos.image_name) }"
  name="${vm_name}"
  cluster=$(jq -r '[.VM.HISTORY_RECORDS.HISTORY // empty] | flatten | last | .CID // ""' <<< "${json}")

  _log_std vm_name="${vm_name}" vm_id="${vm_id}" action="wait_poweroff" limit="${wait}s"
  if ! vm_wait platform="${platform}" vm="${vm_id}" state=POWEROFF limit="${wait}"
  then
    _log_error "image VM \"${vm_name}\" [${vm_id}] not powered off in ${wait}s - when it is: vyos_image_to_platforms platform=${platform} vmfile=\"${vmfile}\""
    return 1
  fi

  _log_std vm_name="${vm_name}" vm_id="${vm_id}" vyos_version="${ver}" image="${name}"
  _stop_without_confirmation confirm=${confirm:-no} && return 1

  # 1. save the disk as a temporary image
  json=$($ssh ${fe} "sudo -u oneadmin onevm show ${vm_id} -j 2>/dev/null")
  disk_id=$(jq -r '[.VM.TEMPLATE.DISK | if type == "array" then .[] else . end | select(.TYPE != "CDROM")][0].DISK_ID // ""' <<< "${json}")
  tmp_name="${vm_name} disk $(date '+%Y%m%d%H%M%S')"
  out=$($ssh ${fe} "sudo -u oneadmin onevm disk-saveas ${vm_id} ${disk_id} \"${tmp_name}\"" 2>&1)
  tmp_id="${out##* }"
  if [[ -z "${disk_id}" || ! "${tmp_id}" =~ ^[0-9]+$ ]]
  then
    _log_error "can not save disk [${disk_id}] of VM ${vm_id}: ${out}"
    return 1
  fi
  _log vm_id="${vm_id}" disk_id="${disk_id}" action="saveas" image="${tmp_name}" image_id="${tmp_id}"
  image_wait platform="${platform}" image="${tmp_id}" || return 1

  # 2. download
  file="${dir}/vyos-${ver}.qcow2"
  image_download platform="${platform}" image="${tmp_id}" file="${file}" || return 1

  # 3. publish to the platform
  image_upload platform="${platform}" file="${file}" name="${name}" type=OS cluster="${cluster:-0}" || return 1

  # 4. publish to the target platforms: all or empty - every platform; the platform of the image VM is done
  [[ -z "${target}" || "${target,,}" == "all" ]] && target="${PLATFORMS}"
  for member in ${target}
  do
    [[ "${member}" == "${platform}" ]] && continue
    if [[ " ${PLATFORMS} " != *" ${member} "* ]]
    then
      _log_error "target platform ${member} not found in the inventory"
      errors=$((errors + 1))
      continue
    fi
    image_upload platform="${member}" file="${file}" name="${name}" type=OS || errors=$((errors + 1))
  done  # member

  # 5. cleanup: the temporary image, the image VM, then the ISO image of vyos_image_vm_create - free after the VM
  _log_std image_id="${tmp_id}" action="delete_temporary"
  $ssh ${fe} "sudo -u oneadmin oneimage delete ${tmp_id}"
  vm_delete platform="${platform}" vm="${vm_id}" confirm=yes && rm -f "${vmfile}"
  iso_id=$($ssh ${fe} "sudo -u oneadmin oneimage show \"${vm_name} ISO\" -j 2>/dev/null" | jq -r '.IMAGE.ID // ""')
  if [[ -n "${iso_id}" ]]
  then
    _log_std image="${vm_name} ISO" image_id="${iso_id}" action="delete_iso"
    $ssh ${fe} "sudo -u oneadmin oneimage delete ${iso_id}"
  fi

  _log
  if (( errors > 0 ))
  then
    _log_error "image \"${name}\" not published on ${errors} target platform(s), see above"
    return 1
  fi
  _log_std image="${name}" local_copy="${file}" target="[${target}]" action="vyos_image_to_platforms" result="done"
  return 0
}  # vyos_image_to_platforms



help_data[vyos_image_vm_create]="\
  vyos_image_vm_create       # Create VyOS Image VM, includes manual operation inside VM console
    platform=NAME            #   platform to create Image VM
    cluster=ID|NAME(0)       #
    vnet=STRING              #
    [addr=ip|AUTO]           #
    [gw=IP]                  #
    vmfile=PATH              #   file to save Image VM id"
function vyos_image_vm_create() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local cluster="${cluster:-0}"
  local vnet="${vnet:-}"
  local addr="${addr:-auto}"
  local gw="${gw:-}"
  local vmfile="${vmfile:-}"
  local fe ver iso_name iso_file vm_name vm_id vm_ip site directory vip pswd json

  if [[ -z "${vmfile}" ]] || ! touch "${vmfile}" 2>/dev/null
  then
    _log_error "vmfile must be a file that can be written: vmfile=\"${vmfile}\""
    return 1
  fi

  data_runtime_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)
  fe=${vip}

  if [[ -z "${vnet}" ]]
  then
    _log_error "vnet must be defined"
    return 1
  fi

  site="${platform#platforms.}"
  site="${site%%.*}"
  directory="$(inv_value var=INV path=resources.${site}.repos.zakroma.local_dir)"
  if [[ -z "${directory}" ]] || ! mkdir -p "${directory%/}/vyos"
  then
    _log_error "repository resources.${site}.repos.zakroma.local_dir/vyos not defined or can not be created"
    return 1
  fi
  directory="${directory%/}/vyos"

  iso_get_vyos platform="${platform}" result=iso_file || return 1
  iso_file="${iso_file##*/}"
  ver="${iso_file#vyos-}"
  ver="${ver%%-*}"
  vm_name="$(inv_value var=CONFIG path=vyos.image_name) ${ver}"
  iso_name="${vm_name} ISO"

  # the default password of the image: config/data/vms.yaml, the only place of it
  pswd=$(inv_value var=CONFIG path=vms.password)
  if [[ -z "${pswd}" ]]
  then
    _log_error "password not defined in config/data/vms.yaml"
    return 1
  fi

  if $ssh ${fe} "sudo -u oneadmin onevm show \"${vm_name}\" -j" &>/dev/null
  then
    _log_error "image VM \"${vm_name}\" already exists - terminate it first"
    return 1
  fi

  image_upload platform="${platform}" file="${directory}/${iso_file}" name="${iso_name}" type=CDROM cluster="${cluster}" || return 1

  vm_create platform="${platform}" cluster="${cluster}" \
    name="${vm_name}" \
    hostname=$(inv_value var=CONFIG path=vyos.vm.hostname) \
    cpu=$(inv_value var=CONFIG path=vyos.vm.cpu) \
    ram=$(inv_value var=CONFIG path=vyos.vm.ram) \
    image1="$(inv_value var=CONFIG path=vyos.vm.image)" \
    disk1="$(inv_value var=CONFIG path=vyos.vm.disk)" \
    image2="${iso_name}" \
    disk2=iso \
    vnet1="${vnet}" \
    addr1="${addr}" \
    gw1="${gw}" \
    autostart=no \
    user=vyos \
    pswd="${pswd}" \
    key="${SSH_PUB}" \
  || return 1

  vm_wait platform="${platform}" vm="${vm_name}" state=RUNNING || return 1

  json=$($ssh ${fe} "sudo -u oneadmin onevm show \"${vm_name}\" -j 2>/dev/null")
  vm_id=$(jq -r '.VM.ID // ""' <<< "${json}")
  vm_ip=$(jq -r '.VM.TEMPLATE.NIC | if type == "array" then .[0] else . end | .IP // ""' <<< "${json}")
  if ! echo "${vm_id}" > "${vmfile}"
  then
    _log_error "image VM \"${vm_name}\" [${vm_id}] created, but its id not saved to ${vmfile}"
    return 1
  fi
  _log_std vm_name="${vm_name}" vm_id="${vm_id}" vm_ip="${vm_ip}" vmfile="${vmfile}" action="image_vm_created"
  _log
  _log "$(inv_value var=TEMPLATES path=vyos.imagevm.content)"

  return 0
}  # vyos_image_vm_create
