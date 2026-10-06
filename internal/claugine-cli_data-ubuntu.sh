#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



declare -gr UBUNTU_BASE_URL="https://releases.ubuntu.com"
declare -gr UBUNTU_VERSION="26.04"

declare -gra UBUNTU_ISO_FILES=(
  grub.cfg
)

declare -grA UBUNTU_ISO_FILES_PATH=(
  [grub.cfg]="/boot/grub/"
)

declare -gr DIR_UBUNTU_ISO_FILES="ubuntu"
