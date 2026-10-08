#!/bin/bash
set -u
#
export SCRIPT_NAME="CLoud AUtomation enGINE"
export SCRIPT_AUTHOR="Vladislav Kirilin, [@]ivladek@me.com"
export SCRIPT_VER="02.00.00"
export SCRIPT_DATE="2026-10-08"

# BLOCK  global variables
#######

export DIR_SCRIPT            # script root directory
export DIR_LIB               # modules
export DIR_CONFIG            # confiuration data
export DIR_TEMPLATES         # templates
export DIR_FILES             # files to copy

export SSH_USER              # user for ssh
export SSH_KEYF              # file with ssh public key

# variable to import data
declare -gr INV_VARS="CONFIG TEMPLATES INV SECRETS"
declare -gA RUNTIME=()       # collected runtime data
declare -g DIR_RUNTIME=""    # directory to export runtime data

export ssh                   # ssh command including all required keys
export -a claugine_help=()   # help

##############
# END OF BLOCK  global variables



# set the directory structure, load the modules and the internal data, check the tools
#   ssh_user=NAME (${USER})
#   ssh_port=N    (22)
#   ssh_keyf=FILE (${HOME}/.ssh/${USER}.key)
# return 0 - loaded
#        1 - a required tool not found
#            internal data not loaded
function _script_INIT() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local module var dir cmd

  DIR_SCRIPT=$(
    dirname -- "$(
      readlink -f -- "${BASH_SOURCE[0]}"
    )"
  )
  DIR_SCRIPT="${DIR_SCRIPT%/bin}"
  echo "script directory: ${DIR_SCRIPT}"

  DIR_LIB="${DIR_SCRIPT}/lib"
  DIR_CONFIG="${DIR_SCRIPT}/config/data"
  DIR_TEMPLATES="${DIR_SCRIPT}/config/templates"
  DIR_FILES="${DIR_SCRIPT}/config/files"

  SSH_USER="${ssh_user:-${USER}}"
  SSH_PORT="${ssh_port:-22}"
  SSH_KEYF="${ssh_keyf:-${HOME}/.ssh/${USER}.key}"
  ssh="ssh -p ${SSH_PORT} -l ${SSH_USER} -i ${SSH_KEYF}"
  echo "to connect to all hosts: ${ssh}"

  # modules only define functions and help texts - nothing is read while loading
  echo "modules: load from ${DIR_LIB}"
  for module in "${DIR_LIB}"/claugine_*.sh
  do
    source "${module}"
  done  # module

  for var in CONFIG TEMPLATES
  do
    dir="DIR_${var}"
    echo "internal data: load data from ${!dir} to ${var}"
    _data_load_var var=${var} dir="${!dir}" || return 1
  done  # var
  echo "internal data: files in directory ${DIR_FILES}"
  ls -l "${DIR_FILES}"

  echo -n "required tools: ["
  for cmd in $(inv_list var=CONFIG path=admin.tools)
  do
    if ! command -v "${cmd}" &>/dev/null
    then
      echo " - ]"
      _log_error "required tool \"${cmd}\" not found"
      return 1
    fi
    echo -n " ${cmd}"
  done  # cmd
  echo " ]"

  echo "now you need load data: data_load_provider inv=PATH secrets=PATH runtime=PATH"
}  # _script_INIT



# entry point: refuse a direct run, load once, print the help
#   arguments of _script_INIT
# return 0 - loaded
#        1 - run directly, already loaded or not loaded
function _script_MAIN() {
  local arg; for arg in "$@"; do local "${arg}"; done

  if [[ "${BASH_SOURCE[0]}" == "$0" ]]
  then # script executed directly
    echo "!!! ERROR !!! don't run the script directly - load by source"
    exit 1
  fi

  # script already loaded
  if declare -F claugine_help &>/dev/null
  then
    echo "!!! WARNING !!! script is already loaded"
    return 1
  fi

  # script loaded by source command
  if _script_INIT "$@"
  then
    claugine_help
    return 0
  fi
}  # _script_MAIN



echo
echo "script: ${SCRIPT_NAME}"
echo "author: ${SCRIPT_AUTHOR}"
echo "version: ${SCRIPT_VER} #${SCRIPT_DATE}"

_script_MAIN "$@"
