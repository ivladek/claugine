#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



# INDEX
#   _iso_repo_dir   print the local repository directory of an OS, created if missing
#   iso_customize   copy an ISO with added files, keep it bootable
#   iso_download    download a file, resume a partial download, verify sha256
#   iso_get_ubuntu  latest Ubuntu Server ISO customized with os=ubuntu files
#   iso_get_vyos    latest VyOS Stream ISO customized with os=vyos files



# print the local repository directory of an OS, created if missing
#   os=NAME                 # ubuntu, vyos, ...: subdirectory of repos.zakroma.local_dir
#   site=NAME | platform=NAME  # the site, or the site of a platform: platforms.<site>.<platform>
#   directory=PATH          # optional: use this directory instead
# return 0 - the directory printed
#        1 - no site or no repository
#            the error goes to stderr
function _iso_repo_dir() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local os="${os:-}"
  local site="${site:-}"
  local platform="${platform:-}"
  local directory="${directory:-}"
  local base

  if [[ -z "${directory}" ]]
  then
    if [[ -z "${site}" && -n "${platform}" ]]
    then
      site="${platform#platforms.}"
      site="${site%%.*}"
    fi
    if [[ -z "${site}" ]]
    then
      _log_error "no repository for ${os}: pass site=, platform= or directory=" >&2
      return 1
    fi
    base=$(inv_value var=INV path=resources.${site}.repos.zakroma.local_dir)
    directory="${base%/}/${os}"
  fi
  directory="${directory/#\~/${HOME}}"
  if ! mkdir -p "${directory}"
  then
    _log_error "repository directory \"${directory}\" can not be created" >&2
    return 1
  fi
  echo "${directory%/}"
}  # _iso_repo_dir



claugine_help+=( "\
  iso_customize              # copy an ISO with added files, keep it bootable
    src=PATH                 #   original ISO
    dst=PATH                 #   customized ISO, overwritten
    os=NAME                  #   ubuntu|vyos|...: files from internal data
                             #     \${DIR_FILES}/<CONFIG.OS.iso.dir>/<name>
                             #     placed to <path>/<name> for each of config.OS.iso.files"
)
# return 0 - built
#        1 - original ISO not found
#            dst or os not defined
#            iso.dir or iso.files not defined in internal data
#            a file of iso.files not found
#            ISO can not be built
function iso_customize() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local src="${src:-}"
  local dst="${dst:-}"
  local os="${os:-}"
  local file path dir
  local -a maps=() files=()

  if [[ ! -f "${src}" || -z "${dst}" || -z "${os}" ]]
  then
    _log_error "original ISO \"${src}\" not found, or dst \"${dst}\" or os \"${os}\" not defined"
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
    path="$(inv_list_find var=CONFIG path=${os}.iso.files field=name value="${file}" | jq -r '.path // empty')"
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



claugine_help+=( "\
  iso_download               # download a file, resume a partial download, verify sha256
    url=URL                  #
    file=PATH                #   local file
    sha256=HASH              #   optional
    overwrite=yes|NO         #   download again even if the file exists"
)
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
  [[ -n "${sha256}" ]] && echo "file=${file} sha256=ok"
  return 0
}  # iso_download



claugine_help+=( "\
  iso_get_ubuntu             # latest Ubuntu Server ISO customized with os=ubuntu files
    version=YY.MM            #   default <CONFIG.ubuntu.version>
    site=NAME                #   site whose repos.zakroma.local_dir/ubuntu is the repository
    platform=NAME            #   instead of site: the site of this platform, platforms.<site>.<platform>
    directory=PATH           #   instead of site: the repository directory
    overwrite=yes|NO         #   download again
                             #   result: global associative array iso_info - version, original_iso, iso, file"
)
# return 0 - done; iso_info: version, original_iso, iso, file
#        1 - no user data, no repository, wrong version or directory
#            the version not found on the download site
#            iso_download failed
#            iso_customize failed
function iso_get_ubuntu() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local version="${version:-$(inv_value var=CONFIG path=ubuntu.version)}"
  local directory ver url sha256 original_iso custom_iso
  local overwrite="${overwrite:-no}"
  declare -gA iso_info=()

  data_runtime_refresh || return 1
  directory=$(_iso_repo_dir os=ubuntu site="${site:-}" platform="${platform:-}" directory="${directory:-}") || return 1

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
  sha256="$(
    curl -fsSL "$(inv_value var=CONFIG path=ubuntu.url)/${ver}/SHA256SUMS" |
    awk -v f="ubuntu-${ver}-live-server-amd64.iso" '$2 == "*"f || $2 == f {print $1}'
  )"
  original_iso="${directory}/ubuntu-${ver}-live-server-amd64.iso"
  custom_iso="${directory}/ubuntu-server-${version}-autoinstall.iso"
  echo "ubuntu_version=${ver} url=${url} sha256=${sha256:-unknown}"

  iso_download url="${url}" file="${original_iso}" sha256="${sha256}" overwrite="${overwrite}" || return 1
  iso_customize src="${original_iso}" dst="${custom_iso}" os=ubuntu || return 1

  iso_info=( [version]="${ver}" [original_iso]="${original_iso}" [iso]="${custom_iso}" [file]="${custom_iso##*/}" )
  return 0
}  # iso_get_ubuntu



claugine_help+=( "\
  iso_get_vyos               # latest VyOS Stream ISO customized with os=vyos files
    url=URL                  #   optional: ISO url, default - latest from <CONFIG.vyos.url>
    site=NAME                #   site whose repos.zakroma.local_dir/vyos is the repository
    platform=NAME            #   instead of site: the site of this platform, platforms.<site>.<platform>
    directory=PATH           #   instead of site: the repository directory
    overwrite=yes|NO         #   download again
                             #   result: global associative array iso_info - version, original_iso, iso, file"
)
# return 0 - done; iso_info: version, original_iso, iso, file
#        1 - no user data, no repository or directory
#            ISO url not found on the download site
#            iso_download failed
#            iso_customize failed
function iso_get_vyos() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local url="${url:-}"
  local directory file ver original_iso custom_iso
  local overwrite="${overwrite:-no}"
  declare -gA iso_info=()

  data_runtime_refresh || return 1
  directory=$(_iso_repo_dir os=vyos site="${site:-}" platform="${platform:-}" directory="${directory:-}") || return 1

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

  original_iso="${directory}/${file}"
  custom_iso="${directory}/vyos-${ver}-generic-amd64-autoinstall.iso"
  echo "vyos_version=${ver} url=${url}"

  iso_download url="${url}" file="${original_iso}" overwrite="${overwrite}" || return 1
  iso_customize src="${original_iso}" dst="${custom_iso}" os=vyos || return 1

  iso_info=( [version]="${ver}" [original_iso]="${original_iso}" [iso]="${custom_iso}" [file]="${custom_iso##*/}" )
  return 0
}  # iso_get_vyos
