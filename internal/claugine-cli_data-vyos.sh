#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



declare -gr VYOS_STREAM_PAGE="https://vyos.net/get/stream/"
declare -gr VYOS_IMAGE_NAME="VyOS Router"
declare -gr VYOS_BUILDER_PREFIX="vyos-builder-"
declare -gr VYOS_BUILDER_DISK=2
declare -gr VYOS_EMPTY_IMAGE="Empty disk"
declare -gr VYOS_DEFAULT_PASSWORD="ChangeMeN0W*2026"
declare -gr VYOS_WAIT_LIMIT=900

declare -gra VYOS_ISO_FILES=(
  grub.cfg                       # grub.cfg for the installed system, vyos-image-prepare writes it to the target disk
  vyos-image-prepare             # executed manually after boot from installation ISO
  vyos-image-finalize            # executed manually after first boot after installation from ISO
  vyos-postconfig-bootup.script  # VyOS standard script executed automatically during each boot
  vyos-first-run.script          # executed automatically only on first boot
)

declare -grA VYOS_ISO_FILES_PATH=(
  [grub.cfg]="/claugine/"
  [vyos-image-prepare]="/claugine/"
  [vyos-image-finalize]="/claugine/"
  [vyos-postconfig-bootup.script]="/claugine/"
  [vyos-first-run.script]="/claugine/"
)

declare -gr DIR_VYOS_ISO_FILES="vyos"
