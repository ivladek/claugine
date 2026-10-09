#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



# INDEX
#   image_archive   free the image name NAME for a new image
#   image_download  copy an image file from the FE datastore to a local file
#   image_publish   upload a local image file to a platform: image_upload, from the site repository if repo=
#   image_upload    upload a local file as a new image, an existing image with the same name is deleted or archived
#   image_wait      wait until the image is READY



help_data[image_archive]="\
  image_archive              # free the image name NAME for a new image:
                             #   not used by VMs - the image is deleted
                             #   used by VMs     - renamed to \"NAME (YYYY-MM-DD)\", date of its registration
    platform=NAME            #   platforms.<site>.<platform>
    name=STRING              #   image name
    limit=N                  #   seconds to wait for the deletion, default is <CONFIG.onefe.timeouts.image_wait>"
# return 0 - no image with the name, or it was deleted or renamed
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            can not delete the image
#            image not deleted in time
#            can not rename the image
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
# return 0 - downloaded
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            image not found or its source is not a file
#            download failed
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



help_data[image_publish]="\
  image_publish              # upload a local image file to a platform: image_upload, from the site repository if repo=
    platform=NAME            #   platforms.<site>.<platform>
    file=PATH                #   local file
    repo=NAME                #   optional: the file is also in repos.zakroma/NAME of the platform's site - the FE downloads it
                             #     from repos.zakroma.url_base/NAME instead of a copy by rsync, like repo=vyos
    name=STRING              #   image name
    type=OS|CDROM|DATABLOCK  #
    ds=ID                    #   default: the default IMAGE datastore of the platform, see image_upload
    prefix=vd|sd             #   default: vd
    format=qcow2|raw         #   optional"
# return 0 - published
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            file not found or name not defined
#            image_upload failed
function image_publish() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local file="${file:-}"
  local repo="${repo:-}"
  local name="${name:-}"
  local url=""
  local site

  data_runtime_refresh platform="${platform:-}" || return 1

  if [[ ! -f "${file}" || -z "${name}" ]]
  then
    _log_error "file \"${file}\" must exist, name must be defined"
    return 1
  fi

  # repository URL of the platform's site: the FE downloads the file from it
  if [[ -n "${repo}" ]]
  then
    site="${platform#platforms.}"
    site="${site%%.*}"
    url=$(inv_value var=INV path=resources.${site}.repos.zakroma.url_base)
    url="${url%/}/${repo}/${file##*/}"
  fi

  image_upload platform="${platform}" file="${file}" url="${url}" name="${name}" \
    type="${type:-OS}" ds="${ds:-}" prefix="${prefix:-vd}" format="${format:-}" \
    || return 1

  return 0
}  # image_publish



help_data[image_upload]="\
  image_upload               # upload a local file as a new image, an existing image with the same name is deleted or archived
    platform=NAME            #   platforms.<site>.<platform>
    file=PATH                #   local file, copied to the FE by rsync
    url=URL                  #   instead of file: the FE downloads the image from this url
    name=STRING              #   image name
    type=OS|CDROM|DATABLOCK  #
    ds=ID                    #   default: the IMAGE datastore of the zone with the lowest id,
                             #     the built-in \"default\" (id 1) only if it is the only one
    prefix=vd|sd             #   default: vd, use sd for CDROM on q35
    format=qcow2|raw         #   optional"
# return 0 - uploaded and READY
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            not confirmed
#            url or file, name, ds not defined
#            existing image with the name can not be archived
#            file can not be copied to the FE
#            image can not be created
#            image not READY - image_wait
function image_upload() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local fe vip
  local file="${file:-}"
  local url="${url:-}"
  local name="${name:-}"
  local type="${type:-OS}"
  local ds="${ds:-}"
  local prefix="${prefix:-vd}"
  local format="${format:-}"
  local remote="" path out id opts result

  data_runtime_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)
  fe=${vip}

  if [[ -z "${ds}" ]]
  then
    ds=$(tr ' ' '\n' <<< "${RUNTIME[${platform},images_ds_list]:-}" | grep -vx 1 | sort -n | head -n1)
    [[ -n "${ds}" ]] || ds=$(tr ' ' '\n' <<< "${RUNTIME[${platform},images_ds_list]:-}" | head -n1)
    [[ -n "${ds}" ]] && _log_std images_ds="${ds}" default="yes"
  fi

  if [[ ( -z "${url}" && ! -f "${file}" ) || -z "${name}" || -z "${ds}" ]]
  then
    _log_error "url or existing file must be defined, name and ds too (file=\"${file}\" ds=\"${ds}\")"
    return 1
  fi

  image_archive platform="${platform}" name="${name}" || return 1

  if [[ -n "${url}" ]]
  then
    path="${url}"
    _log_std image="${name}" action="download_by_fe" url="${url}"
  else
    remote="/var/tmp/claugine_${file##*/}"
    path="${remote}"
    _log_std image="${name}" action="copy" source="${file}" target="${remote}"
    if ! rsync --progress -e "$ssh" --rsync-path "sudo rsync" "${file}" "${fe}:${remote}"
    then
      _log_error "can not copy ${file} to ${fe}:${remote}"
      return 1
    fi
    $ssh ${fe} "sudo chown oneadmin: ${remote}"
  fi

  opts="--type ${type} --datastore ${ds} --prefix ${prefix}"
  [[ -n "${format}" ]] && opts+=" --format ${format}"
  out=$($ssh ${fe} "sudo -u oneadmin oneimage create --name \"${name}\" --path \"${path}\" ${opts}" 2>&1)
  id="${out##* }"
  if [[ ! "${id}" =~ ^[0-9]+$ ]]
  then
    _log_error "can not create image \"${name}\" on ${fe}: ${out}"
    [[ -n "${remote}" ]] && $ssh ${fe} "sudo rm -f ${remote}"
    return 1
  fi
  _log_std image="${name}" image_id="${id}" type="${type}" ds="${ds}" action="create"

  image_wait platform="${platform}" image="${id}"
  result=$?
  [[ -n "${remote}" ]] && $ssh ${fe} "sudo rm -f ${remote}"

  (( result != 0 )) && return 1
  return 0
}  # image_upload



help_data[image_wait]="\
  image_wait                 # wait until the image is READY
    platform=NAME            #   platforms.<site>.<platform>
    image=STRING             #   name or id
    limit=N                  #   seconds, default is <CONFIG.onefe.timeouts.image_wait>"
# return 0 - the image is READY
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh, image in ERROR
#            timeout
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
