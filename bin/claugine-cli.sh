#!/bin/bash
set -u
#
export SCRIPT_NAME="CLoud AUtomation enGINE CLI"
export SCRIPT_AUTHOR="Vladislav Kirilin, [@]ivladek@me.com"
export SCRIPT_VER="01.10.10"
export SCRIPT_DATE="2026-10-02"
#



# BLOCK  module variables
#######

export DIR_SCRIPT DIR_LIB DIR_INTERNAL DIR_TEMPLATE
export DIR_DATA
export SSH_USER SSH_KEYF ssh

export -a claugine_cli_commands=()  # cli help
export -A claugine_zone=()          # default var for zone data
export    zone_data=claugine_zone   # pointer to current zone data used by each command

##############
# END OF BLOCK  variables



# set directories structure
#   data=PATH
#   ssh_user=NAME (${USER})
#   ssh_port=N    (22)
#   ssh_keyf=FILE (${HOME}/.ssh/${USER}.key)
function script_INIT {
  local arg; for arg in "$@"; do local "${arg}"; done
  local module

  DIR_SCRIPT=$(                              # script root directory
    dirname -- "$(
      readlink -f -- "${BASH_SOURCE[0]}"
    )"
  )
  DIR_SCRIPT="${DIR_SCRIPT%/bin}"

  DIR_LIB="${DIR_SCRIPT}/lib/cli"            # script modules
  DIR_INTERNAL="${DIR_SCRIPT}/internal/cli"  # internal script data
  DIR_TEMPLATE="${DIR_SCRIPT}/templates"     # templates

  DIR_DATA="${data:-}"
  [[ -z "${DIR_DATA}" ]] && DIR_DATA="${DIR_SCRIPT}/data/cli"
  DIR_DATA=$(realpath "${DIR_DATA}" 2>/dev/null)
  if [[ -z "${DIR_DATA}" ]]
  then
    echo "!!! ERROR !!! data directory \"${DIR_DATA}\" not exists"
    return 1
  fi

  SSH_USER="${ssh_user:-${USER}}"
  SSH_PORT="${ssh_port:-22}"
  SSH_KEYF="${ssh_keyf:-${HOME}/.ssh/${USER}.key}"
  ssh="ssh -p ${SSH_PORT} -l ${SSH_USER} -i ${SSH_KEYF}"

  echo "data: load from ${DIR_INTERNAL}"
  for module in "${DIR_INTERNAL}"/claugine_cli_*.sh
  do
    source "${module}"
  done

  echo "data: load from ${DIR_DATA}"
  for module in "${DIR_DATA}"/claugine_cli_*.sh
  do
    [[ "${module}" == *SAMPLE* ]] && continue
    source "${module}"
  done

  echo "modules: load from ${DIR_LIB}"
  for module in "${DIR_LIB}"/claugine_cli_*.sh
  do
    source "${module}"
  done

  echo "script directory: ${DIR_SCRIPT}"
  echo "data directory: ${DIR_DATA}"
  echo "ssh commnad: ${ssh}"
}  # script_INIT



# MAIN
function script_MAIN {
  if [[ "${BASH_SOURCE[0]}" == "$0" ]]
  then # script executed directly
    echo "!!! ERROR !!! don't run the script directly - load by source"
    exit 1
  fi

  # script already loaded
  if declare -F claugine_cli_help &>/dev/null
  then
    echo "!!! WARNING !!! script is already loaded"
    return 1
  fi

  # script loaded by source command
  if script_INIT "$@"
  then
    claugine_cli_help
    return 0
  fi
}



echo
echo "script: ${SCRIPT_NAME}"
echo "author: ${SCRIPT_AUTHOR}"
echo "version: ${SCRIPT_VER} #${SCRIPT_DATE}"

script_MAIN "$@"
