#!/bin/bash
set -u

# Put FireEdge views on the FEs of the platforms
#   run by hand or by cron; fe_fireedge_views_update to every FE node of each platform, one after another -
#   the views of config/files/<CONFIG.onefe.fireedge_views.dir>
#
#   datafile=PATH           # the job data, the only file the job loads:
#                           #   USER, HOME - must be defined in file for cron jobs
#                           #   CLAUGINE - claugine bin file
#                           #   PROVIDER - directory with data (inventory, secrets)
#                           #   FIREEDGE_VIEWS_PLATFORMS - the platforms of the views, separated by spaces;
#                           #     a platform not in the inventory is skipped
#
#   all output on the terminal, the job returns 1 if any platform failed
#
# usage:
#   fireedge-views.sh datafile=<data>/<set>/jobs-data.sh

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
if [[ -z "${FIREEDGE_VIEWS_PLATFORMS:-}" ]]
then
  echo "fireedge-views: FIREEDGE_VIEWS_PLATFORMS must be defined in ${datafile} file"
  exit 1
fi

if [[ ! -f "${CLAUGINE}" || ! -d "${PROVIDER}" ]]
then
  echo "fireedge-views: CLAUGINE and PROVIDER must be defined in ${datafile} file, no views updated"
  exit 1
fi

# claugine gets no arguments of the job: datafile= is not its parameter
set --
if ! source "${CLAUGINE}"
then
  echo "fireedge-views: claugine not loaded, no views updated"
  exit 1
fi
if ! data_load_provider \
  inv="${PROVIDER}/inventory" \
  secrets="${PROVIDER}/secrets" \
  runtime="${PROVIDER}/runtime"
then
  echo "fireedge-views: claugine data not loaded, no views updated"
  exit 1
fi

result=0
for platform in ${FIREEDGE_VIEWS_PLATFORMS}
do
  [[ " ${PLATFORMS} " != *" ${platform} "* ]] && continue
  if fe_fireedge_views_update platform="${platform}" confirm=yes
  then
    echo "fireedge-views: ${platform} completed successfully"
    # fe_fireedge_restart platform="${platform}" confirm=yes
  else
    echo "fireedge-views: ${platform} finished with errors"
    result=1
  fi
done  # platform

exit ${result}
