#!/bin/bash
set -u

# Set the group quotas of the tenants on the cloud platforms from their quota records - quota_tenant_set
#   run by hand; one template per tenant and platform: the qrs of the tenant - their limits, the other clusters
#   of the platform - -1, the datastores of the tenant; after a set the quota data of the tenant is read again
#
#   datafile=PATH           # the job data, the only file the job loads:
#                           #   USER, HOME - must be defined in file for cron jobs
#                           #   CLAUGINE - claugine bin file
#                           #   PROVIDER - directory with data (inventory, secrets)
#                           #   QUOTA_PLATFORMS - the platforms of kind shared, separated by spaces
#   dry=YES|no              # yes - print the quota templates, nothing changed; no - set the quotas
#   tenants=LIST            # optional: tenants names or group ids, default - all tenants
#   platforms=LIST          # optional: instead of QUOTA_PLATFORMS
#
#   all output on the terminal, the job returns 1 if any platform failed
#
# usage:
#   quota-set.sh datafile=<data>/<set>/jobs-data.sh                     # dry run
#   quota-set.sh datafile=<data>/<set>/jobs-data.sh dry=no              # set all tenants
#   quota-set.sh datafile=<data>/<set>/jobs-data.sh dry=no tenants="tenant-a 105"

# job data: the file of the argument datafile=; the arguments of the job
datafile=""
dry="yes"
tenants="ALL"
platforms=""
for arg in "$@"
do
  case "${arg}" in
    datafile=*|dry=*|tenants=*|platforms=*) declare "${arg}" ;;
  esac
done  # arg

if [[ ! -f "${datafile}" ]]
then
  echo "!!! ERROR !!! datafile \"${datafile}\" is not exists" >&2
  exit 1
fi
source "${datafile}" || exit 1

# the arguments and the data of the job
dry="${dry,,}"
if [[ ! "${dry}" =~ ^(yes|no)$ ]]
then
  echo "quota-set: dry must be yes or no: dry=\"${dry}\""
  exit 1
fi
platforms="${platforms:-${QUOTA_PLATFORMS:-}}"
if [[ -z "${platforms}" ]]
then
  echo "quota-set: QUOTA_PLATFORMS must be defined in ${datafile} file, or platforms= given"
  exit 1
fi

if [[ ! -f "${CLAUGINE:-}" || ! -d "${PROVIDER:-}" ]]
then
  echo "quota-set: CLAUGINE and PROVIDER must be defined in ${datafile} file, no quotas changed"
  exit 1
fi

# claugine gets no arguments of the job: datafile= is not its parameter
set --
if ! source "${CLAUGINE}"
then
  echo "quota-set: claugine not loaded, no quotas changed"
  exit 1
fi
if ! data_provider_load \
  inv="${PROVIDER}/inventory" \
  secrets="${PROVIDER}/secrets" \
  runtime="${PROVIDER}/runtime"
then
  echo "quota-set: claugine data not loaded, no quotas changed"
  exit 1
fi

# every platform of the job, one after another: kind shared - checked by the command
result=0
for platform in ${platforms}
do
  if [[ " ${PLATFORMS} " != *" ${platform} "* ]]
  then
    echo "quota-set: ${platform} not found in the inventory"
    result=1
    continue
  fi
  if quota_tenant_set platform="${platform}" tenants="${tenants}" dry="${dry}" confirm=yes
  then
    echo "quota-set: ${platform} completed successfully, dry=${dry}"
  else
    echo "quota-set: ${platform} finished with errors, dry=${dry}"
    result=1
  fi
done  # platform

exit ${result}
