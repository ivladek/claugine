#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



claugine_cli_commands+=( "\
  image_wait                 # wait until the image is READY
    zone_id=N                #   zone of the current installation
    image=STRING             #   name or id
    limit=N(${IMAGE_WAIT_LIMIT})       #   seconds"
)
function image_wait() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local fe
  local image="${image:-}"
  local limit="${limit:-${IMAGE_WAIT_LIMIT}}"
  local start=$(date '+%s')
  local state

  _zone_init || return 1
  _zone_check zone_id="${zone_id:-}" || return 1
  fe=$(_zone_vip ${zone_id})

  echo -n "$(_zone_label ${zone_id}) image=\"${image}\" action=wait_ready status=."
  while :
  do
    state=$(
      $ssh ${fe} "sudo -u oneadmin oneimage show \"${image}\" -j 2>/dev/null" |
      jq -r '.IMAGE.STATE // empty' 2>/dev/null
    )

    case "${state}" in
      1)
        echo " ${ONE_IMAGE_STATE[1]}"
        return 0
        ;;
      5)
        echo " ${ONE_IMAGE_STATE[5]}"
        $ssh ${fe} "sudo -u oneadmin oneimage show \"${image}\" 2>/dev/null" | grep -iE '^ *(error|message)'
        return 1
        ;;
      "")
        echo " not found"
        return 1
        ;;
    esac

    if (( $(date '+%s') - start > limit ))
    then
      echo " timeout ${limit}s state=${ONE_IMAGE_STATE[${state}]:-${state}}"
      return 1
    fi

    sleep 5
    echo -n "."
  done
}  # image_wait



claugine_cli_commands+=( "\
  image_archive              # free the image name NAME for a new image:
                             #   not used by VMs - the image is deleted
                             #   used by VMs     - renamed to \"NAME (YYYY-MM-DD)\", date of its registration
    zone_id=N                #   zone of the current installation
    name=STRING              #   image name
    limit=N(${IMAGE_WAIT_LIMIT})       #   seconds to wait for the deletion"
)
function image_archive() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local fe
  local name="${name:-}"
  local limit="${limit:-${IMAGE_WAIT_LIMIT}}"
  local start=$(date '+%s')
  local json id regtime vms new_name

  _zone_init || return 1
  _zone_check zone_id="${zone_id:-}" || return 1
  fe=$(_zone_vip ${zone_id})

  json=$($ssh ${fe} "sudo -u oneadmin oneimage show \"${name}\" -j 2>/dev/null")
  [[ -z "${json}" ]] && return 0  # nothing to archive

  read -r id regtime vms < <(jq -r '"\(.IMAGE.ID) \(.IMAGE.REGTIME) \(.IMAGE.RUNNING_VMS // 0)"' <<< "${json}")

  # not used - delete, wait until it is gone: the name is busy while the image exists
  if (( vms == 0 ))
  then
    echo -n "$(_zone_label ${zone_id}) image_id=${id} image=\"${name}\" action=delete status=."

    if ! $ssh ${fe} "sudo -u oneadmin oneimage delete ${id}" &>/dev/null
    then
      echo " failed"
      echo
      echo "!!! ERROR !!! can not delete image \"${name}\" [${id}] on ${fe}"
      echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
      return 2
    fi

    while $ssh ${fe} "sudo -u oneadmin oneimage show ${id} -j" &>/dev/null
    do
      if (( $(date '+%s') - start > limit ))
      then
        echo " timeout ${limit}s"
        echo
        echo "!!! ERROR !!! image \"${name}\" [${id}] on ${fe} is not deleted in ${limit}s"
        echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
        return 3
      fi
      sleep 5
      echo -n "."
    done

    echo " deleted"
    return 0
  fi

  # used by VMs - rename
  new_name="${name} ($(date -d "@${regtime}" '+%Y-%m-%d'))"
  if $ssh ${fe} "sudo -u oneadmin oneimage show \"${new_name}\" -j" &>/dev/null
  then
    new_name="${name} ($(date -d "@${regtime}" '+%Y-%m-%d %H-%M'))"
  fi

  echo "$(_zone_label ${zone_id}) image_id=${id} image=\"${name}\" running_vms=${vms} action=archive new_name=\"${new_name}\""
  if ! $ssh ${fe} "sudo -u oneadmin oneimage rename ${id} \"${new_name}\""
  then
    echo
    echo "!!! ERROR !!! can not rename image \"${name}\" [${id}] on ${fe}"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 4
  fi

  return 0
}  # image_archive



claugine_cli_commands+=( "\
  image_upload               # upload a local file as a new image, an existing image with the same name is deleted or archived
    zone_id=N                #   zone of the current installation
    file=PATH                #   local file, copied to the FE by rsync
    url=URL                  #   instead of file: the FE downloads the image from this url
    name=STRING              #   image name
    type=OS|CDROM|DATABLOCK  #
    ds=ID                    #   default: first of IMAGES_DS_LIST for the zone VIP
    prefix=vd|sd             #   default: vd, use sd for CDROM on q35
    format=qcow2|raw         #   optional"
)
function image_upload() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local fe
  local file="${file:-}"
  local url="${url:-}"
  local name="${name:-}"
  local type="${type:-OS}"
  local ds="${ds:-}"
  local prefix="${prefix:-vd}"
  local format="${format:-}"
  local remote="" path out id opts result

  _zone_init || return 1
  _zone_check zone_id="${zone_id:-}" || return 1
  fe=$(_zone_vip ${zone_id})

  [[ -n "${ds}" ]] || ds="${IMAGES_DS_LIST[${fe}]:-}"
  ds="${ds%% *}"

  if [[ ( -z "${url}" && ! -f "${file}" ) || -z "${name}" || -z "${ds}" ]]
  then
    echo
    echo "!!! ERROR !!! url or existing file must be defined, name and ds too (file=\"${file}\" ds=\"${ds}\")"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 2
  fi

  image_archive zone_id="${zone_id}" name="${name}" || return 3

  if [[ -n "${url}" ]]
  then
    path="${url}"
    echo "$(_zone_label ${zone_id}) image=\"${name}\" action=download_by_fe url=${url}"
  else
    remote="/var/tmp/claugine_${file##*/}"
    path="${remote}"
    echo "$(_zone_label ${zone_id}) image=\"${name}\" action=copy source=${file} target=${remote}"
    if ! rsync --progress -e "$ssh" --rsync-path "sudo rsync" "${file}" "${fe}:${remote}"
    then
      echo
      echo "!!! ERROR !!! can not copy ${file} to ${fe}:${remote}"
      echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
      return 4
    fi
    $ssh ${fe} "sudo chown oneadmin: ${remote}"
  fi

  opts="--type ${type} --datastore ${ds} --prefix ${prefix}"
  [[ -n "${format}" ]] && opts+=" --format ${format}"
  out=$($ssh ${fe} "sudo -u oneadmin oneimage create --name \"${name}\" --path \"${path}\" ${opts}" 2>&1)
  id="${out##* }"
  if [[ ! "${id}" =~ ^[0-9]+$ ]]
  then
    echo
    echo "!!! ERROR !!! can not create image \"${name}\" on ${fe}: ${out}"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    [[ -n "${remote}" ]] && $ssh ${fe} "sudo rm -f ${remote}"
    return 5
  fi
  echo "$(_zone_label ${zone_id}) image=\"${name}\" image_id=${id} type=${type} ds=${ds} action=create"

  image_wait zone_id="${zone_id}" image="${id}"
  result=$?
  [[ -n "${remote}" ]] && $ssh ${fe} "sudo rm -f ${remote}"

  (( result != 0 )) && return 6
  return 0
}  # image_upload



claugine_cli_commands+=( "\
  image_download             # copy an image file from the FE datastore to a local file
    zone_id=N                #   zone of the current installation
    image=STRING             #   name or id
    file=PATH                #   local file"
)
function image_download() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local fe
  local image="${image:-}"
  local file="${file:-}"
  local json source

  _zone_init || return 1
  _zone_check zone_id="${zone_id:-}" || return 1
  fe=$(_zone_vip ${zone_id})

  json=$($ssh ${fe} "sudo -u oneadmin oneimage show \"${image}\" -j 2>/dev/null")
  source=$(jq -r '.IMAGE.SOURCE // empty' <<< "${json}" 2>/dev/null)
  if [[ "${source}" != /* || -z "${file}" ]]
  then
    echo
    echo "!!! ERROR !!! image \"${image}\" on ${fe} not found or its source is not a file: \"${source}\""
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 2
  fi

  mkdir -p "$(dirname "${file}")"
  echo "$(_zone_label ${zone_id}) image=\"${image}\" action=download source=${source} target=${file}"
  if ! rsync --progress -e "$ssh" --rsync-path "sudo rsync" "${fe}:${source}" "${file}"
  then
    echo
    echo "!!! ERROR !!! can not download ${fe}:${source}"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 3
  fi

  return 0
}  # image_download



claugine_cli_commands+=( "\
  image_publish              # upload a local image file to several zones: image_upload in each zone
    zones=LIST|ALL           #   zone ids, default ALL
    file=PATH                #   local file
    repo=VAR_NAME            #   optional: associative array [zone VIP]=base URL of the directory of file,
                             #     the FE downloads file from there instead of a copy by rsync, like URL_VYOS_REPO
    name=STRING              #   image name
    type=OS|CDROM|DATABLOCK  #
    ds=ID                    #   default: first of IMAGES_DS_LIST for each zone VIP
    prefix=vd|sd             #   default: vd
    format=qcow2|raw         #   optional"
)
function image_publish() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local zones="${zones:-ALL}"
  local file="${file:-}"
  local repo="${repo:-}"
  local name="${name:-}"
  local zone_id fe url

  _zone_init || return 1
  zones=$(_zones_resolve zones="${zones}") || return 1

  if [[ ! -f "${file}" || -z "${name}" ]] || { [[ -n "${repo}" ]] && ! declare -p "${repo}" &>/dev/null; }
  then
    echo
    echo "!!! ERROR !!! file \"${file}\" must exist, name must be defined, repo \"${repo}\" must be declared if defined"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 2
  fi

  [[ -n "${repo}" ]] && local -n _repo=${repo}
  for zone_id in ${zones}
  do
    fe=$(_zone_vip ${zone_id})
    url=""
    if [[ -n "${repo}" ]]
    then
      url="${_repo[${fe}]:-}"
      [[ -n "${url}" ]] && url="${url%/}/${file##*/}"
    fi
    image_upload zone_id="${zone_id}" file="${file}" url="${url}" name="${name}" \
      type="${type:-OS}" ds="${ds:-}" prefix="${prefix:-vd}" format="${format:-}" \
      || return 3
  done  # zone_id

  return 0
}  # image_publish
