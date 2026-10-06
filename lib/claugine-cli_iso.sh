#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



claugine_cli_commands+=( "\
  iso_download               # download a file, resume a partial download, verify sha256
    url=URL                  #
    file=PATH                #   local file
    sha256=HASH              #   optional
    overwrite=yes|NO         #   download again even if the file exists"
)
function iso_download() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local sha256="${sha256:-}"
  local overwrite="${overwrite:-no}"

  if [[ -z "${url:-}" || -z "${file:-}" || ! -d "$(dirname "${file}")" ]]
  then
    echo
    echo "!!! ERROR !!! url and file must be defined, directory of the file must exist"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 1
  fi

  [[ "${overwrite,,}" == "yes" ]] && rm -f "${file}" "${file}.part"

  if [[ -f "${file}" ]]
  then
    if [[ -z "${sha256}" ]] || sha256sum -c --status <<< "${sha256}  ${file}"
    then
      echo "file=${file} action=download status=cached"
      return 0
    fi
    echo "file=${file} action=download status=checksum_mismatch"
    rm -f "${file}"
  fi

  echo "file=${file} action=download url=${url}"
  if ! curl \
    --fail \
    --location \
    --progress-bar \
    --continue-at - \
    --output "${file}.part" \
    "${url}"
  then
    rm -f "${file}" "${file}.part"
    echo
    echo "!!! ERROR !!! can not download ${url}"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 2
  fi

  if [[ -n "${sha256}" ]] && ! sha256sum -c --status <<< "${sha256} ${file}.part"
  then
    rm -f "${file}.part"
    echo
    echo "!!! ERROR !!! sha256 of ${url} does not match ${sha256}"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 3
  fi

  mv "${file}.part" "${file}"
  [[ -n "${sha256}" ]] && echo "file=${file} sha256=ok"
  return 0
}  # iso_download



claugine_cli_commands+=( "\
  iso_customize              # copy an ISO with added files, keep it bootable
    src=PATH                 #   original ISO
    dst=PATH                 #   customized ISO, overwritten
    os=NAME                  #   ubuntu|vyos|...: files from internal data
                             #     \${DIR_TEMPLATE}/\${DIR_<OS>_ISO_FILES}/\${<OS>_ISO_FILES[@]}
                             #     placed to \${<OS>_ISO_FILES_PATH[file]}"
)
function iso_customize() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local src="${src:-}"
  local dst="${dst:-}"
  local os="${os:-}"
  local file path dir dir_var
  local -a maps=()

  if [[ ! -f "${src}" || -z "${dst}" || -z "${os}" ]]
  then
    echo
    echo "!!! ERROR !!! original ISO \"${src}\" not found, or dst \"${dst}\" or os \"${os}\" not defined"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 1
  fi

  dir_var="DIR_${os^^}_ISO_FILES"
  if ! declare -p "${os^^}_ISO_FILES" "${os^^}_ISO_FILES_PATH" "${dir_var}" &>/dev/null
  then
    echo
    echo "!!! ERROR !!! ${dir_var}, ${os^^}_ISO_FILES and ${os^^}_ISO_FILES_PATH must be defined in internal data"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 2
  fi
  local -n _iso_files="${os^^}_ISO_FILES"
  local -n _iso_paths="${os^^}_ISO_FILES_PATH"
  dir="${DIR_TEMPLATE}/${!dir_var}"

  for file in "${_iso_files[@]}"
  do
    path="${_iso_paths[${file}]:-}"
    if [[ ! -f "${dir}/${file}" || -z "${path}" ]]
    then
      echo
      echo "!!! ERROR !!! ${dir}/${file} not found or ${os^^}_ISO_FILES_PATH[${file}] not defined"
      echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
      return 3
    fi
    maps+=( -map "${dir}/${file}" "${path%/}/${file}" )
    echo "iso=${dst} action=add file=${dir}/${file} target=${path%/}/${file}"
  done  # file

  echo "iso=${dst} action=customize source=${src}"
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
    echo
    echo "!!! ERROR !!! can not build ${dst}"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 4
  fi

  return 0
}  # iso_customize



claugine_cli_commands+=( "\
  iso_get_ubuntu             # latest Ubuntu Server ISO customized with os=ubuntu files
    version=YY.MM            #   default ${UBUNTU_VERSION}
    directory=PATH           #   default \${DIR_UBUNTU_REPO}, must be available thru http too
    overwrite=yes|NO         #   download again
                             #   result: global associative array iso_info - version, original_iso, iso, file"
)
function iso_get_ubuntu() {
  local arg; for arg in "$@"; do local "${arg}"; done
  declare -gA iso_info=()
  local version="${version:-${UBUNTU_VERSION}}"
  local directory="${directory:-${DIR_UBUNTU_REPO:-}}"
  local overwrite="${overwrite:-no}"
  local ver url sha256 original_iso custom_iso

  if [[ ! "${version}" =~ ^[0-9]{2}\.[0-9]{2}$ || ! -d "${directory}" ]]
  then
    echo
    echo "!!! ERROR !!! version must be like ${UBUNTU_VERSION} and directory \"${directory}\" must exist"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 1
  fi
  directory="${directory%/}"

  # latest point release: 26.04, 26.04.1, ...
  ver="$(
    curl -fsSL "${UBUNTU_BASE_URL}/" |
    grep -oE "href=\"${version//./\\.}(\.[0-9]+)?/\"" |
    sed -E 's/^href="([^/]+)\/"$/\1/' |
    sort -V |
    tail -n 1
  )"
  if [[ -z "${ver}" ]]
  then
    echo
    echo "!!! ERROR !!! Ubuntu ${version} not found on ${UBUNTU_BASE_URL}"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 2
  fi

  url="${UBUNTU_BASE_URL}/${ver}/ubuntu-${ver}-live-server-amd64.iso"
  sha256="$(
    curl -fsSL "${UBUNTU_BASE_URL}/${ver}/SHA256SUMS" |
    awk -v f="ubuntu-${ver}-live-server-amd64.iso" '$2 == "*"f || $2 == f {print $1}'
  )"
  original_iso="${directory}/ubuntu-${ver}-live-server-amd64.iso"
  custom_iso="${directory}/ubuntu-server-${version}-autoinstall.iso"
  echo "ubuntu_version=${ver} url=${url} sha256=${sha256:-unknown}"

  iso_download url="${url}" file="${original_iso}" sha256="${sha256}" overwrite="${overwrite}" || return 3
  iso_customize src="${original_iso}" dst="${custom_iso}" os=ubuntu || return 4

  iso_info=( [version]="${ver}" [original_iso]="${original_iso}" [iso]="${custom_iso}" [file]="${custom_iso##*/}" )
  return 0
}  # iso_get_ubuntu



claugine_cli_commands+=( "\
  iso_get_vyos               # latest VyOS Stream ISO customized with os=vyos files
    url=URL                  #   optional: ISO url, default - latest from ${VYOS_STREAM_PAGE}
    directory=PATH           #   default \${DIR_VYOS_REPO}, must be available thru http too
    overwrite=yes|NO         #   download again
                             #   result: global associative array iso_info - version, original_iso, iso, file"
)
function iso_get_vyos() {
  local arg; for arg in "$@"; do local "${arg}"; done
  declare -gA iso_info=()
  local url="${url:-}"
  local directory="${directory:-${DIR_VYOS_REPO:-}}"
  local overwrite="${overwrite:-no}"
  local file ver original_iso custom_iso

  if [[ ! -d "${directory}" ]]
  then
    echo
    echo "!!! ERROR !!! directory \"${directory}\" must exist"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 1
  fi
  directory="${directory%/}"

  if [[ -z "${url}" ]]
  then
    url="$(
      curl -fsSL "${VYOS_STREAM_PAGE}" |
      grep -oE "https://[^\"' ]+-generic-amd64\.iso" |
      head -1
    )"
  fi
  file="${url##*/}"
  ver="${file#vyos-}"
  ver="${ver%-generic-amd64.iso}"
  if [[ -z "${url}" || "${ver}" == "${file}" ]]
  then
    echo
    echo "!!! ERROR !!! can not find the VyOS Stream ISO url on ${VYOS_STREAM_PAGE}: \"${url}\""
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 2
  fi

  original_iso="${directory}/${file}"
  custom_iso="${directory}/vyos-${ver}-generic-amd64-autoinstall.iso"
  echo "vyos_version=${ver} url=${url}"

  iso_download url="${url}" file="${original_iso}" overwrite="${overwrite}" || return 3
  iso_customize src="${original_iso}" dst="${custom_iso}" os=vyos || return 4

  iso_info=( [version]="${ver}" [original_iso]="${original_iso}" [iso]="${custom_iso}" [file]="${custom_iso##*/}" )
  return 0
}  # iso_get_vyos
