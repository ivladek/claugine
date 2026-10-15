#!/bin/bash
set -u

# Show the quotas of one tenant on the cloud platforms - quota_tenant_show: per quota record and per datastore,
#   limit/real/usage, the usage read again; the site values of the tenant
#   run by hand, read only
#
#   datafile=PATH           # the job data, the only file the job loads:
#                           #   USER, HOME - must be defined in file for cron jobs
#                           #   CLAUGINE - claugine bin file
#                           #   PROVIDER - directory with data (inventory, secrets)
#                           #   QUOTA_PLATFORMS - the platforms of kind shared, separated by spaces
#   tenant=NAME             # the tenant: name, tenant-<name> or group id
#   platforms=LIST          # optional: instead of QUOTA_PLATFORMS
#
#   all output on the terminal, the job returns 1 if any platform failed
#
# usage:
#   quota-show.sh datafile=<data>/<set>/jobs-data.sh tenant=tenant-a

# job data: the file of the argument datafile=; the arguments of the job
datafile=""
tenant=""
platforms=""
for arg in "$@"
do
  case "${arg}" in
    datafile=*|tenant=*|platforms=*) declare "${arg}" ;;
  esac
done  # arg

if [[ ! -f "${datafile}" ]]
then
  echo "!!! ERROR !!! datafile \"${datafile}\" is not exists" >&2
  exit 1
fi
source "${datafile}" || exit 1

# the arguments and the data of the job
if [[ -z "${tenant}" ]]
then
  echo "quota-show: tenant must be given: tenant=NAME"
  exit 1
fi
platforms="${platforms:-${QUOTA_PLATFORMS:-}}"
if [[ -z "${platforms}" ]]
then
  echo "quota-show: QUOTA_PLATFORMS must be defined in ${datafile} file, or platforms= given"
  exit 1
fi

if [[ ! -f "${CLAUGINE:-}" || ! -d "${PROVIDER:-}" ]]
then
  echo "quota-show: CLAUGINE and PROVIDER must be defined in ${datafile} file"
  exit 1
fi

# claugine gets no arguments of the job: datafile= is not its parameter
set --
if ! source "${CLAUGINE}"
then
  echo "quota-show: claugine not loaded"
  exit 1
fi
if ! data_provider_load \
  inv="${PROVIDER}/inventory" \
  secrets="${PROVIDER}/secrets" \
  runtime="${PROVIDER}/runtime"
then
  echo "quota-show: claugine data not loaded"
  exit 1
fi

# every platform of the job, one after another: kind shared - checked by the command
result=0
for platform in ${platforms}
do
  if [[ " ${PLATFORMS} " != *" ${platform} "* ]]
  then
    echo "quota-show: ${platform} not found in the inventory"
    result=1
    continue
  fi
  if quota_tenant_show platform="${platform}" tenant="${tenant}"
  then
    echo "quota-show: ${platform} completed successfully"
  else
    echo "quota-show: ${platform} finished with errors"
    result=1
  fi
done  # platform

exit ${result}
