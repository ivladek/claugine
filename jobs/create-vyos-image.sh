#!/bin/bash
set -u

# Create a new VyOS image on OpenNebula and put it on the target platforms
#   run by hand; one manual step inside - install VyOS on the image VM on its VNC console
#   vyos_image_create: the image VM, the wait for its power off, the image on the platforms
#
#   datafile=PATH           # the job data, the only file the job loads:
#                           #   USER, HOME - must be defined in file for cron jobs
#                           #   CLAUGINE - claugine bin file
#                           #   PROVIDER - directory with data (inventory, secrets)
#                           #   VYOS_IMAGE_VM_PLATFORM, VYOS_IMAGE_VM_CLUSTER, VYOS_IMAGE_VM_VNET - the image VM
#                           #   VYOS_IMAGE_TARGET_PLATFORMS - the platforms of the image, separated by spaces,
#                           #     all or empty - every platform
#
#   all output on the terminal, the job returns 1 if the image is not created or not put on any platform
#
# usage:
#   create-vyos-image.sh datafile=<data>/<set>/jobs-data.sh

# job data: the file of the argument datafile=; the arguments of the job
datafile=""
for arg in "$@"
do
  case "${arg}" in
    datafile=*) declare "${arg}" ;;
  esac
done  # arg

if [[ ! -f "${datafile}" ]]
then
  echo "!!! ERROR !!! datafile \"${datafile}\" is not exists" >&2
  exit 1
fi
source "${datafile}" || exit 1

# the arguments and the data of the job
platform="${VYOS_IMAGE_VM_PLATFORM:-}"
if [[ -z "${platform}" ]]
then
  echo "create-vyos-image: VYOS_IMAGE_VM_PLATFORM must be defined in ${datafile} file"
  exit 1
fi

if [[ ! -f "${CLAUGINE}" || ! -d "${PROVIDER}" ]]
then
  echo "create-vyos-image: CLAUGINE and PROVIDER must be defined in ${datafile} file, no image created"
  exit 1
fi

# claugine gets no arguments of the job: datafile= is not its parameter
set --
if ! source "${CLAUGINE}"
then
  echo "create-vyos-image: claugine not loaded, no image created"
  exit 1
fi
if ! data_load_provider \
  inv="${PROVIDER}/inventory" \
  secrets="${PROVIDER}/secrets" \
  runtime="${PROVIDER}/runtime"
then
  echo "create-vyos-image: claugine data not loaded, no image created"
  exit 1
fi

result=0
if vyos_image_create \
  platform="${platform}" \
  cluster="${VYOS_IMAGE_VM_CLUSTER:-0}" \
  vnet="${VYOS_IMAGE_VM_VNET:-}" \
  addr=auto \
  target="${VYOS_IMAGE_TARGET_PLATFORMS:-}"
then
  echo "create-vyos-image: image on ${platform} and [${VYOS_IMAGE_TARGET_PLATFORMS:-all}] completed successfully"
else
  echo "create-vyos-image: image on ${platform} finished with errors"
  result=1
fi

exit ${result}
