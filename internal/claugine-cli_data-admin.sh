#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



declare -gra ADMIN_TOOLS=(
  curl
  grep
  jq
  mkpasswd
  onezone
  rsync
  sed
  sha256sum
  sort
  ssh
  tail
  tee
  xorriso
)
