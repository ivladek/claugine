#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



function claugine_cli_help() {
  local cmd

  echo
  echo "CLaud AUtomation enGINE functions"

  for cmd in "${claugine_cli_commands[@]}"
  do
    echo "${cmd}"
  done
}  # claugine_cli_help



function stop_without_confirmation() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local confirm="${confirm:-no}"
  local answer="no"

  [[ "${confirm}" == "yes" ]] && return 1

  read -r -t 60 -p "!!! ATTENTION !!! Approve critial operation  [yes/no or it will automatically declined in 1 minute]: " answer
  [[ "${answer}" == "yes" ]] && return 1

  echo "!!! OPERATION CANCELED !!!"
  return 0
}  # stop_without_confirmation
