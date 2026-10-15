#!/bin/bash
set -u

# Reset ACLs of the tenants of the cloud platforms: deleted and created again
#   run by hand; acl_tenant_set on the primary platform of each federation -
#   a secondary platform is replaced by its primary, every federation is reset once, for all its zones
#
#   datafile=PATH           # the job data, the only file the job loads:
#                           #   USER, HOME - must be defined in file for cron jobs
#                           #   CLAUGINE - claugine bin file
#                           #   PROVIDER - directory with data (inventory, secrets)
#                           #   ACL_PLATFORMS - the platforms of the ACLs, separated by spaces
#   dry=YES|no              # yes - show the current and the new ACLs, nothing changed; no - reset the ACLs
#   tenants=LIST            # optional: tenants ids or names, default - all tenants
#
#   all output on the terminal, the job returns 1 if any federation failed
#
# usage:
#   acl-reset.sh datafile=<data>/<set>/jobs-data.sh                  # dry run
#   acl-reset.sh datafile=<data>/<set>/jobs-data.sh dry=no           # reset all tenants
#   acl-reset.sh datafile=<data>/<set>/jobs-data.sh dry=no tenants="tenant-a 105"

# job data: the file of the argument datafile=; the arguments of the job
datafile=""
dry="yes"
tenants="ALL"
for arg in "$@"
do
  case "${arg}" in
    datafile=*|dry=*|tenants=*) declare "${arg}" ;;
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
  echo "acl-reset: dry must be yes or no: dry=\"${dry}\""
  exit 1
fi

if [[ -z "${ACL_PLATFORMS:-}" ]]
then
  echo "acl-reset: ACL_PLATFORMS must be defined in ${datafile} file"
  exit 1
fi

if [[ ! -f "${CLAUGINE}" || ! -d "${PROVIDER}" ]]
then
  echo "acl-reset: CLAUGINE and PROVIDER must be defined in ${datafile} file, no ACLs changed"
  exit 1
fi

# claugine gets no arguments of the job: datafile= is not its parameter
set --
if ! source "${CLAUGINE}"
then
  echo "acl-reset: claugine not loaded, no ACLs changed"
  exit 1
fi
if ! data_provider_load \
  inv="${PROVIDER}/inventory" \
  secrets="${PROVIDER}/secrets" \
  runtime="${PROVIDER}/runtime"
then
  echo "acl-reset: claugine data not loaded, no ACLs changed"
  exit 1
fi

# the primary platform of each federation, once
result=0
primaries=""
for platform in ${ACL_PLATFORMS}
do
  if [[ " ${PLATFORMS} " != *" ${platform} "* ]]
  then
    echo "acl-reset: ${platform} not found in the inventory"
    result=1
    continue
  fi
  primary=$(inv_value var=INV path=${platform}.fe.primary)
  primary="${primary:-${platform}}"
  [[ " ${primaries} " == *" ${primary} "* ]] || primaries+="${primaries:+ }${primary}"
done  # platform

for platform in ${primaries}
do
  if acl_tenant_set platform="${platform}" tenants="${tenants}" dry="${dry}" confirm=yes
  then
    echo "acl-reset: ${platform} and its secondaries completed successfully, dry=${dry}"
  else
    echo "acl-reset: ${platform} and its secondaries finished with errors, dry=${dry}"
    result=1
  fi
done  # platform

exit ${result}
