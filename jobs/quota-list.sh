#!/bin/bash
set -u

# List the quotas of the tenants on the cloud platforms - quota_tenants_report: a row per quota record and per
#   datastore, limit/real/usage and a check - ok, differ, exceeded; the usage read again
#   run by hand or by cron, read only
#
#   datafile=PATH           # the job data, the only file the job loads:
#                           #   USER, HOME - must be defined in file for cron jobs
#                           #   CLAUGINE - claugine bin file
#                           #   PROVIDER - directory with data (inventory, secrets)
#                           #   QUOTA_PLATFORMS - the platforms of kind shared, separated by spaces
#   tenants=LIST            # optional: tenants names or group ids, default - all tenants
#   platforms=LIST          # optional: instead of QUOTA_PLATFORMS
#
#   all output on the terminal, the job returns 1 if any platform failed
#
# usage:
#   quota-list.sh datafile=<data>/<set>/jobs-data.sh
#   quota-list.sh datafile=<data>/<set>/jobs-data.sh tenants="tenant-a 105"

# job data: the file of the argument datafile=; the arguments of the job
datafile=""
tenants=""
platforms=""
for arg in "$@"
do
  case "${arg}" in
    datafile=*|tenants=*|platforms=*) declare "${arg}" ;;
  esac
done  # arg

if [[ ! -f "${datafile}" ]]
then
  echo "!!! ERROR !!! datafile \"${datafile}\" is not exists" >&2
  exit 1
fi
source "${datafile}" || exit 1

# the arguments and the data of the job
platforms="${platforms:-${QUOTA_PLATFORMS:-}}"
if [[ -z "${platforms}" ]]
then
  echo "quota-list: QUOTA_PLATFORMS must be defined in ${datafile} file, or platforms= given"
  exit 1
fi

if [[ ! -f "${CLAUGINE:-}" || ! -d "${PROVIDER:-}" ]]
then
  echo "quota-list: CLAUGINE and PROVIDER must be defined in ${datafile} file"
  exit 1
fi

# claugine gets no arguments of the job: datafile= is not its parameter
set --
if ! source "${CLAUGINE}"
then
  echo "quota-list: claugine not loaded"
  exit 1
fi
if ! data_provider_load \
  inv="${PROVIDER}/inventory" \
  secrets="${PROVIDER}/secrets" \
  runtime="${PROVIDER}/runtime"
then
  echo "quota-list: claugine data not loaded"
  exit 1
fi

# every platform of the job, one after another: kind shared - checked by the command
result=0
for platform in ${platforms}
do
  if [[ " ${PLATFORMS} " != *" ${platform} "* ]]
  then
    echo "quota-list: ${platform} not found in the inventory"
    result=1
    continue
  fi
  if quota_tenants_report platform="${platform}" tenants="${tenants}"
  then
    echo "quota-list: ${platform} completed successfully"
  else
    echo "quota-list: ${platform} finished with errors"
    result=1
  fi
done  # platform

exit ${result}
