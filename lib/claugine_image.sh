#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



# INDEX
#   image_archive   free the image name NAME for a new image
#   image_download  copy an image file from the FE datastore to a local file
#   image_upload    upload a local file as a new image, an existing image with the same name is deleted or archived
#   image_wait      wait until the image is READY



help_data[image_archive]="\
  image_archive              # free the image name NAME for a new image:
                             #   not used by VMs - the image is deleted
                             #   used by VMs     - renamed to \"NAME (YYYY-MM-DD)\", date of its registration
    platform=NAME            #   platforms.<site>.<platform>
    name=STRING              #   image name
    limit=N                  #   seconds to wait for the deletion, default is <CONFIG.onefe.timeouts.image_wait>"
function image_archive() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local fe
  local name="${name:-}"
  local limit="${limit:-$(inv_value var=CONFIG path=onefe.timeouts.image_wait)}"
  local start=$(date '+%s')
  local json id regtime vms new_name vip

  data_runtime_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)
  fe=${vip}

  json=$($ssh ${fe} "sudo -u oneadmin oneimage show \"${name}\" -j 2>/dev/null")
  [[ -z "${json}" ]] && return 0  # nothing to archive

  read -r id regtime vms < <(jq -r '"\(.IMAGE.ID) \(.IMAGE.REGTIME) \(.IMAGE.RUNNING_VMS // 0)"' <<< "${json}")

  # not used - delete, wait until it is gone: the name is busy while the image exists
  if (( vms == 0 ))
  then
    _log_std -n image_id="${id}" image="${name}" action="delete" status="[."

    if ! $ssh ${fe} "sudo -u oneadmin oneimage delete ${id}" &>/dev/null
    then
      _log "] result=failed"
      _log_error "can not delete image \"${name}\" [${id}] on ${fe}"
      return 1
    fi

    while $ssh ${fe} "sudo -u oneadmin oneimage show ${id} -j" &>/dev/null
    do
      if (( $(date '+%s') - start > limit ))
      then
        _log "] result=timeout limit=${limit}s"
        _log_error "image \"${name}\" [${id}] on ${fe} is not deleted in ${limit}s"
        return 1
      fi
      sleep 5
      _log -n "."
    done

    _log "] result=deleted"
    return 0
  fi

  # used by VMs - rename
  new_name="${name} ($(date -d "@${regtime}" '+%Y-%m-%d'))"
  if $ssh ${fe} "sudo -u oneadmin oneimage show \"${new_name}\" -j" &>/dev/null
  then
    new_name="${name} ($(date -d "@${regtime}" '+%Y-%m-%d %H-%M'))"
  fi

  _log_std image_id="${id}" image="${name}" running_vms="${vms}" action="archive" new_name="${new_name}"
  if ! $ssh ${fe} "sudo -u oneadmin oneimage rename ${id} \"${new_name}\""
  then
    _log_error "can not rename image \"${name}\" [${id}] on ${fe}"
    return 1
  fi

  return 0
}  # image_archive



help_data[image_download]="\
  image_download             # copy an image file from the FE datastore to a local file
    platform=NAME            #   platforms.<site>.<platform>
    image=STRING             #   name or id
    file=PATH                #   local file"
function image_download() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local fe
  local image="${image:-}"
  local file="${file:-}"
  local json source vip

  data_runtime_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)
  fe=${vip}

  json=$($ssh ${fe} "sudo -u oneadmin oneimage show \"${image}\" -j 2>/dev/null")
  source=$(jq -r '.IMAGE.SOURCE // ""' <<< "${json}" 2>/dev/null)
  if [[ "${source}" != /* || -z "${file}" ]]
  then
    _log_error "image \"${image}\" on ${fe} not found or its source is not a file: \"${source}\""
    return 1
  fi

  mkdir -p "$(dirname "${file}")"
  _log_std image="${image}" action="download" source="${source}" target="${file}"
  if ! rsync --progress -e "$ssh" --rsync-path "sudo rsync" "${fe}:${source}" "${file}"
  then
    _log_error "can not download ${fe}:${source}"
    return 1
  fi

  return 0
}  # image_download



help_data[image_upload]="\
  image_upload               # upload a local file as a new image, an existing image with the same name is deleted or archived
    platform=NAME            #   platforms.<site>.<platform>
    cluster=ID|NAME(0)       #   cluster: the image goes to its IMAGES datastore
    file=PATH                #   local file, copied to the FE by rsync
    name=STRING              #   image name
    type=OS|CDROM|DATABLOCK  #
                             #   the group of the image: the shared group of the platform (platform.shared_owner),
                             #     if the platform has one"
function image_upload() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local fe vip
  local file="${file:-}"
  local name="${name:-}"
  local type="${type:-OS}"
  local cluster="${cluster:-0}"
  local remote out id opts result cluster_id ds_id

  data_runtime_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)
  fe=${vip}

  if [[ ! -f "${file}" || -z "${name}" ]]
  then
    _log_error "file must exist and name must be defined (file=\"${file}\" name=\"${name}\")"
    return 1
  fi

  # the IMAGES datastore of the cluster
  cluster_id=$(_one_object_id platform="${platform}" object=cluster name="${cluster}") || return 1
  ds_id=$(_cluster_ds_id platform="${platform}" cluster_id="${cluster_id}" type=images) || return 1

  image_archive platform="${platform}" name="${name}" || return 1

  remote="/var/tmp/claugine_${file##*/}"
  _log_std image="${name}" action="copy" source="${file}" target="${remote}"
  if ! rsync --progress -e "$ssh" --rsync-path "sudo rsync" "${file}" "${fe}:${remote}"
  then
    _log_error "can not copy ${file} to ${fe}:${remote}"
    return 1
  fi
  $ssh ${fe} "sudo chown oneadmin: ${remote}"

  # VMs are q35: CDROM - prefix sd; OS, DATABLOCK - prefix vd, qcow2
  if [[ "${type}" == "CDROM" ]]
  then
    opts="--type ${type} --datastore ${ds_id} --prefix sd"
  else
    opts="--type ${type} --datastore ${ds_id} --prefix vd --format qcow2"
  fi
  out=$($ssh ${fe} "sudo -u oneadmin oneimage create --name \"${name}\" --path \"${remote}\" ${opts}" 2>&1)
  id="${out##* }"
  if [[ ! "${id}" =~ ^[0-9]+$ ]]
  then
    _log_error "can not create image \"${name}\" on ${fe}: ${out}"
    $ssh ${fe} "sudo rm -f ${remote}"
    return 1
  fi
  _log_std image="${name}" image_id="${id}" type="${type}" cluster_id="${cluster_id}" ds="${ds_id}" action="create"

  image_wait platform="${platform}" image="${id}"
  result=$?
  $ssh ${fe} "sudo rm -f ${remote}"
  (( result != 0 )) && return 1

  # the shared group of the platform owns the image: shared with all tenants by their ACLs
  if [[ -n "${RUNTIME[${platform},shared]:-}" ]]
  then
    _log_std image="${name}" image_id="${id}" group_id="${RUNTIME[${platform},shared]}" action="chgrp_shared"
    if ! $ssh ${fe} "sudo -u oneadmin oneimage chgrp ${id} ${RUNTIME[${platform},shared]}"
    then
      _log_error "can not set the shared group [${RUNTIME[${platform},shared]}] to image \"${name}\" [${id}]"
      return 1
    fi
  fi

  return 0
}  # image_upload



help_data[image_wait]="\
  image_wait                 # wait until the image is READY
    platform=NAME            #   platforms.<site>.<platform>
    image=STRING             #   name or id
    limit=N                  #   seconds, default is <CONFIG.onefe.timeouts.image_wait>"
function image_wait() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local fe
  local image="${image:-}"
  local limit="${limit:-$(inv_value var=CONFIG path=onefe.timeouts.image_wait)}"
  local start=$(date '+%s')
  local state vip

  data_runtime_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)
  fe=${vip}

  _log_std -n image="${image}" action="wait_ready" status="[."
  while :
  do
    state=$(
      $ssh ${fe} "sudo -u oneadmin oneimage show \"${image}\" -j 2>/dev/null" |
      jq -r '.IMAGE.STATE // ""' 2>/dev/null
    )

    case "${state}" in
      1)
        _log "] state=$(inv_value var=CONFIG path=onefe.states.image.1)"
        return 0
        ;;
      5)
        _log "] state=$(inv_value var=CONFIG path=onefe.states.image.5)"
        $ssh ${fe} "sudo -u oneadmin oneimage show \"${image}\" 2>/dev/null" | grep -iE '^ *(error|message)'
        return 1
        ;;
      "")
        _log "] state=not_found"
        return 1
        ;;
    esac

    if (( $(date '+%s') - start > limit ))
    then
      _log "] state=$(inv_value var=CONFIG path=onefe.states.image.${state:-none}) result=timeout limit=${limit}s"
      return 1
    fi

    sleep 5
    _log -n "."
  done
}  # image_wait
