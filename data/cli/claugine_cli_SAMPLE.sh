#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"

export DC1="10.71.101.30"
export DC2="10.72.101.30"

declare -grA SHARED_ID=(
  [${DC1}]=100
  [${DC2}]=100
)

declare -grA VNTEMPLATE_ID=(
  [${DC1}]=0
  [${DC2}]=0
)

declare -gr DIR_BACKUPS=${HOME}/backups

declare -grA IMAGES_QUOTA=(
  [ooo-vasilyok]=100
  [too-romashka]=1000
)

declare -grA FILES_QUOTA=()

declare -grA BACKUPS_QUOTA=(
  [ooo-vasilyok]=500
)

declare -gr IMAGES_DEFAULT=20
declare -gr FILES_DEFAULT=1
declare -gr BACKUPS_DEFAULT=100

declare -grA IMAGES_DS_LIST=(
  [${DC1}]="101"
  [${DC2}]="101"
)

declare -grA IMAGES_DS=(
  [${DC1},0]=101
  [${DC2},0]=101
  [${DC2},100]=101
  [${DC2},101]=101
)

declare -grA FILES_DS=(
  [${DC1}]=102
  [${DC2}]=102
)

declare -grA BACKUPS_DS=(
  [${DC1}]=105
  [${DC2}]=105
)
