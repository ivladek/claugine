#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



# INDEX
#   iso_customize   copy an ISO with added files, keep it bootable
#   iso_download    download a file, resume a partial download, verify sha256
#   iso_get_ubuntu  latest Ubuntu Server ISO customized with os=ubuntu files
#   iso_get_vyos    latest VyOS Stream ISO customized with os=vyos files



help_data[iso_customize]="\
  iso_customize              # copy an ISO with added files, keep it bootable
    src=PATH                 #   original ISO
    dst=PATH                 #   optional: customized ISO, overwritten; default - <src without .iso>-claugine.iso
    os=NAME                  #   ubuntu|vyos|..."
# return 0 - built
#        1 - original ISO not found
#            os not defined
#            iso.dir or iso.files not defined in internal data
#            a file of iso.files not found
#            ISO can not be built
function iso_customize() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local src="${src:-}"
  local dst="${dst:-${src%.iso}-claugine.iso}"
  local os="${os:-}"
  local file path dir
  local -a maps=() files=()

  if [[ ! -f "${src}" || -z "${os}" ]]
  then
    _log_error "original ISO \"${src}\" not found or os \"${os}\" not defined"
    return 1
  fi

  dir="$(inv_value var=CONFIG path=${os}.iso.dir)"
  mapfile -t files < <(inv_list_field var=CONFIG path=${os}.iso.files field=name)
  if [[ -z "${dir}" ]] || (( ${#files[@]} == 0 ))
  then
    _log_error "config.${os}.iso.dir and config.${os}.iso.files must be defined in internal data"
    return 1
  fi
  dir="${DIR_FILES}/${dir}"

  for file in "${files[@]}"
  do
    path="$(inv_list_find var=CONFIG path=${os}.iso.files field=name value="${file}" | jq -r '.path // ""')"
    if [[ ! -f "${dir}/${file}" || -z "${path}" ]]
    then
      _log_error "${dir}/${file} not found or path not defined in config ${os}.iso.files"
      return 1
    fi
    maps+=( -map "${dir}/${file}" "${path%/}/${file}" )
    _log iso="${dst}" action="add" file="${dir}/${file}" target="${path%/}/${file}"
  done  # file

  _log iso="${dst}" action="customize" source="${src}"
  rm -f "${dst}"
  if ! xorriso \
    -indev  "${src}" \
    -outdev "${dst}" \
    -uid 0 -gid 0 \
    "${maps[@]}" \
    -boot_image any replay \
    -compliance no_emul_toc \
    -padding included
  then
    _log_error "can not build ${dst}"
    return 1
  fi

  return 0
}  # iso_customize



help_data[iso_download]="\
  iso_download               # download a file, resume a partial download, verify sha256
    url=URL                  #
    file=PATH                #   local file
    sha256=HASH              #   optional
    overwrite=yes|NO         #   download again even if the file exists"
# return 0 - downloaded, sha256 matches
#        1 - url or file not defined
#            directory missing
#            download failed
#            sha256 does not match
function iso_download() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local sha256="${sha256:-}"
  local overwrite="${overwrite:-no}"

  if [[ -z "${url:-}" || -z "${file:-}" || ! -d "$(dirname "${file}")" ]]
  then
    _log_error "url and file must be defined, directory of the file must exist"
    return 1
  fi

  [[ "${overwrite,,}" == "yes" ]] && rm -f "${file}" "${file}.part"

  if [[ -f "${file}" ]]
  then
    if [[ -z "${sha256}" ]] || sha256sum -c --status <<< "${sha256}  ${file}"
    then
      _log file="${file}" action="download" status="cached"
      return 0
    fi
    _log file="${file}" action="download" status="checksum_mismatch"
    rm -f "${file}"
  fi

  _log file="${file}" action="download" url="${url}"
  if ! curl \
    --fail \
    --location \
    --progress-bar \
    --continue-at - \
    --output "${file}.part" \
    "${url}"
  then
    rm -f "${file}" "${file}.part"
    _log_error "can not download ${url}"
    return 1
  fi

  if [[ -n "${sha256}" ]] && ! sha256sum -c --status <<< "${sha256} ${file}.part"
  then
    rm -f "${file}.part"
    _log_error "sha256 of ${url} does not match ${sha256}"
    return 1
  fi

  mv "${file}.part" "${file}"
  [[ -n "${sha256}" ]] && _log file="${file}" sha256="ok"
  return 0
}  # iso_download



help_data[iso_get_ubuntu]="\
  iso_get_ubuntu             # latest Ubuntu Server ISO customized with os=ubuntu files
    platform=NAME            #   instead of site: the site of this platform, platforms.<site>.<platform>
    version=YY.MM            #   default <CONFIG.ubuntu.version>
    directory=PATH           #   instead of site: the repository directory
    overwrite=yes|NO         #   download again
                             #   the ISO: <name of the downloaded file>-claugine.iso in the repository"
# return 0 - done
#        1 - no user data, no repository, wrong version or directory
#            the version not found on the download site
#            iso_download failed
#            iso_customize failed
function iso_get_ubuntu() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local version="${version:-$(inv_value var=CONFIG path=ubuntu.version)}"
  local site
  local platform="${platform:-}"
  local directory="${directory:-}"
  local file ver url sha256 original_iso
  local overwrite="${overwrite:-no}"

  data_runtime_refresh || return 1
  if [[ -z "${directory:-}" ]]
  then
    site="${platform#platforms.}"
    site="${site%%.*}"
    directory=$(inv_value var=INV path=resources.${site:-none}.repos.zakroma.local_dir)
    [[ -n "${directory}" ]] && directory="${directory%/}/ubuntu"
  fi
  mkdir -p "${directory}" 2>/dev/null

  if [[ ! "${version}" =~ ^[0-9]{2}\.[0-9]{2}$ || ! -d "${directory}" ]]
  then
    _log_error "version must be like $(inv_value var=CONFIG path=ubuntu.version) and directory \"${directory}\" must exist"
    return 1
  fi
  directory="${directory%/}"

  # latest point release: 26.04, 26.04.1, ...
  ver="$(
    curl -fsSL "$(inv_value var=CONFIG path=ubuntu.url)/" |
    grep -oE "href=\"${version//./\\.}(\.[0-9]+)?/\"" |
    sed -E 's/^href="([^/]+)\/"$/\1/' |
    sort -V |
    tail -n 1
  )"
  if [[ -z "${ver}" ]]
  then
    _log_error "Ubuntu ${version} not found on $(inv_value var=CONFIG path=ubuntu.url)"
    return 1
  fi

  url="$(inv_value var=CONFIG path=ubuntu.url)/${ver}/ubuntu-${ver}-live-server-amd64.iso"
  file="${url##*/}"
  sha256="$(
    curl -fsSL "$(inv_value var=CONFIG path=ubuntu.url)/${ver}/SHA256SUMS" |
    awk -v f="${file}" '$2 == "*"f || $2 == f {print $1}'
  )"
  # the file name from the url; iso_customize makes <name>-claugine.iso next to it
  original_iso="${directory}/${file}"
  _log ubuntu_version="${ver}" url="${url}" sha256="${sha256:-unknown}"

  iso_download url="${url}" file="${original_iso}" sha256="${sha256}" overwrite="${overwrite}" || return 1
  iso_customize src="${original_iso}" os=ubuntu || return 1

  return 0
}  # iso_get_ubuntu



help_data[iso_get_vyos]="\
  iso_get_vyos               # latest VyOS Stream ISO customized with os=vyos files
    url=URL                  #   optional: ISO url, default - latest from <CONFIG.vyos.url>
    platform=NAME            #   instead of site: the site of this platform, platforms.<site>.<platform>
    directory=PATH           #   instead of site: the repository directory
    overwrite=yes|NO         #   download again
                             #   the ISO: <name of the downloaded file>-claugine.iso in the repository"
# return 0 - done
#        1 - no user data, no repository or directory
#            ISO url not found on the download site
#            iso_download failed
#            iso_customize failed
function iso_get_vyos() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local url="${url:-}"
  local site
  local platform="${platform:-}"
  local directory="${directory:-}"
  local file ver original_iso
  local overwrite="${overwrite:-no}"

  data_runtime_refresh || return 1
  if [[ -z "${directory:-}" ]]
  then
    site="${platform#platforms.}"
    site="${site%%.*}"
    directory=$(inv_value var=INV path=resources.${site:-none}.repos.zakroma.local_dir)
    [[ -n "${directory}" ]] && directory="${directory%/}/ubuntu"
  fi
  mkdir -p "${directory}" 2>/dev/null

  if [[ ! -d "${directory}" ]]
  then
    _log_error "directory \"${directory}\" must exist"
    return 1
  fi
  directory="${directory%/}"

  if [[ -z "${url}" ]]
  then
    url="$(
      curl -fsSL "$(inv_value var=CONFIG path=vyos.url)" |
      grep -oE "https://[^\"' ]+-generic-amd64\.iso" |
      head -1
    )"
  fi
  file="${url##*/}"
  ver="${file#vyos-}"
  ver="${ver%-generic-amd64.iso}"
  if [[ -z "${url}" || "${ver}" == "${file}" ]]
  then
    _log_error "can not find the VyOS Stream ISO url on $(inv_value var=CONFIG path=vyos.url): \"${url}\""
    return 1
  fi

  # the file name from the url; iso_customize makes <name>-claugine.iso next to it
  original_iso="${directory}/${file}"
  _log vyos_version="${ver}" url="${url}"

  iso_download url="${url}" file="${original_iso}" overwrite="${overwrite}" || return 1
  iso_customize src="${original_iso}" os=vyos || return 1

  return 0
}  # iso_get_vyos
