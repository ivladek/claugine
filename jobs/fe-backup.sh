#!/bin/bash
set -u

# Back up the FEs of every platform of the site of this admin host
#   run by cron; fe_backup full=yes of each platform, the site - by the second octet of eth0 and SITES;
#   each platform is reported to Telegram, the output of claugine to the log files of the site
#
#   datafile=PATH           # the job data, the only file the job loads:
#                           #   USER, HOME - must be defined in file for cron jobs
#                           #   CLAUGINE - claugine bin file
#                           #   PROVIDER - directory with data (inventory, secrets)
#                           #   LOGS - directory of the job logs
#                           #   SITES - used by this script, you can implement your own logic
#                           #   REPORT_URL, REPORT_CHAT - data for report to Telegram
#
#   the job returns 1 if any backup failed
#
# usage:
#   fe-backup.sh datafile=<data>/<set>/jobs-data.sh
#   /etc/cron.d/claugine - one line per admin host, the site in the log names:
#     # backup every day at 05:25
#     25 05 * * *  <user>  /bin/bash <jobs>/fe-backup.sh datafile=<data>/<set>/jobs-data.sh

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

log=/dev/stdout
err=/dev/stderr

# report one line to the log and to Telegram
function report() {
  echo "$*" >> "${log}"
  curl -s -o /dev/null --max-time 10 \
    -X POST "${REPORT_URL}" \
    -d chat_id="${REPORT_CHAT}" \
    -d text="$*, log=${log}, err=${err}" ||
  echo "report to telegram failed" >> "${err}"
}  # report

# detect site of this host based on octet 2
ip=$(ip -4 -o addr show dev eth0 | awk '{print $4}')
ip=${ip%%/*}
oct2=$(cut -d. -f2 <<< "${ip}")
if [[ -z "${oct2}" || -z "${SITES[${oct2}]:-}" ]]
then
  report "fe-backup: site not detected: hostname=$(hostname) ip=${ip}"
  exit 1
fi
site=${SITES[${oct2}]}

# the log files of the site: new on every run
for file in "${LOGS}/fe-backup-${site}.log" "${LOGS}/fe-backup-${site}.err"
do
  if ! : 2>/dev/null > "${file}"
  then
    report "fe-backup: can not create log file ${file}, no backups made"
    exit 1
  fi
done  # file
log="${LOGS}/fe-backup-${site}.log"
err="${LOGS}/fe-backup-${site}.err"

if [[ ! -f "${CLAUGINE}" || ! -d "${PROVIDER}" ]]
then
  report "fe-backup: CLAUGINE and PROVIDER must be defined in ${datafile} file, no backups made, log=${log}, err=${err}"
  exit 1
fi

# claugine gets no arguments of the job: datafile= is not its parameter
set --
if ! source "${CLAUGINE}" 1>> "${log}" 2>>"${err}"
then
  report "fe-backup: claugine not loaded, no backups made"
  exit 1
fi
if ! data_provider_load \
  inv="${PROVIDER}/inventory" \
  secrets="${PROVIDER}/secrets" \
  runtime="${PROVIDER}/runtime" 1>> "${log}" 2>>"${err}"
then
  report "fe-backup: claugine data not loaded, no backups made"
  exit 1
fi

result=0
for platform in ${PLATFORMS}
do
  [[ "${platform}" == platforms.${site}.* ]] || continue
  if [[ "${RUNTIME[${platform},available]:-}" != "yes" ]]
  then
    report "fe-backup: ${platform} unavailable - skipped"
    result=1
    continue
  fi
  if fe_backup platform="${platform}" full=yes 1>> "${log}" 2>>"${err}"
  then
    report "fe-backup: ${platform} completed successfully"
  else
    report "fe-backup: ${platform} finished with errors"
    result=1
  fi
done  # platform

exit ${result}
