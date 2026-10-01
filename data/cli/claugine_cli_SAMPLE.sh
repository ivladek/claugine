#!/bin/bash
set -u

export DC1_PROD="10.71.101.30"
export DC2_PROD="10.72.101.30"

declare -grA IMAGES_QUOTA=(
  [ooo-vasilyok]=100
  [too-romashka]=1000
)

declare -grA FILES_QUOTA=()

declare -grA BACKUPS_QUOTA=(
  [ooo-vasilyok]=500
)

declare -gr DIR_BACKUPS=${HOME}/backups

declare -gr IMAGES_DEFAULT=20
declare -gr FILES_DEFAULT=1
declare -gr BACKUPS_DEFAULT=100

declare -grA IMAGES_DS_LIST=(
  [${DC1_PROD}]="101"
  [${DC2_PROD}]="101"
)
declare -grA IMAGES_DS=(
  [${DC1_PROD},0]=101
  [${DC2_PROD},0]=101
  [${DC2_PROD},100]=101
  [${DC2_PROD},101]=101
)
declare -grA FILES_DS=(
  [${DC1_PROD}]=102
  [${DC2_PROD}]=102
)
declare -grA BACKUPS_DS=(
  [${DC1_PROD}]=105
  [${DC2_PROD}]=105
)
declare -grA SHARED_ID=(
  [${DC1_PROD}]=100
  [${DC2_PROD}]=100
)
