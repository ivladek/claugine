#!/bin/bash
# jobs/fe-backup.sh with stand-ins: claugine, ip and curl are faked in a temporary directory
#   offline: no FE, no Telegram, nothing changed on the platforms
#   data: test_jobs_data - jobs-data.sh of the job; its SITES gives the site, the rest is replaced by the stand-ins

# claugine: load if not loaded yet - the test can be run or sourced
DIR_TESTS="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if ! declare -F data_provider_load &>/dev/null
then
  source "${DIR_TESTS}/../bin/claugine" || { return 1 2>/dev/null || exit 1; }
fi

# test data: tests-data.sh of the data set CLAUGINE_DATA, e.g. CLAUGINE_DATA=data/SAMPLE
if [[ ! -f "${CLAUGINE_DATA:-}/tests-data.sh" ]]
then
  _log_error "CLAUGINE_DATA must be a data set directory with tests-data.sh: CLAUGINE_DATA=\"${CLAUGINE_DATA:-}\""
  return 1 2>/dev/null || exit 1
fi
DIR_DATA="$(cd -- "${CLAUGINE_DATA}" && pwd)"
source "${DIR_DATA}/tests-data.sh" || { return 1 2>/dev/null || exit 1; }

if [[ ! -f "${test_jobs_data:-}" ]]
then
  _log_error "test_jobs_data must be a jobs-data.sh file: test_jobs_data=\"${test_jobs_data:-}\""
  return 1 2>/dev/null || exit 1
fi

# a site of the job data: the first octet of its SITES
test_job_octet=$( (source "${test_jobs_data}" >/dev/null 2>&1; printf '%s\n' "${!SITES[@]}" | sort -n | head -n1) )
test_job_site=$( (source "${test_jobs_data}" >/dev/null 2>&1; echo "${SITES[${test_job_octet}]:-}") )
if [[ -z "${test_job_site}" ]]
then
  _log_error "no SITES in the job data: test_jobs_data=\"${test_jobs_data}\""
  return 1 2>/dev/null || exit 1
fi

# sandbox: the job data with the stand-ins, fake claugine, ip and curl
test_job_dir=$(mktemp -d)
mkdir -p "${test_job_dir}/bin" "${test_job_dir}/claugine/bin" "${test_job_dir}/provider" "${test_job_dir}/logs"
cat > "${test_job_dir}/jobs-data.sh" <<DATA
source "${test_jobs_data}"
CLAUGINE="${test_job_dir}/claugine/bin/claugine"
PROVIDER="${test_job_dir}/provider"
LOGS="${test_job_dir}/logs"
REPORT_URL="https://telegram.invalid/sendMessage"
DATA
# claugine: LOADFAIL=1 - data not loaded; BACKUPFAIL=NAME - fe_backup of platforms.<site>.NAME fails
cat > "${test_job_dir}/claugine/bin/claugine" <<'FAKE'
echo "fake claugine loaded"
function data_provider_load() {
  [[ "${LOADFAIL:-}" == "1" ]] && { echo "fake: data not loaded" >&2; return 1; }
  PLATFORMS="platforms.${TEST_SITE}.p1 platforms.${TEST_SITE}.p2 platforms.${TEST_SITE}.p3 platforms.other.p1"
}
function fe_backup() {
  local arg; for arg in "$@"; do local "${arg}"; done
  echo "fake: backup ${platform}"
  [[ "${platform}" == "platforms.${TEST_SITE}.${BACKUPFAIL:-none}" ]] && { echo "fake: backup failed" >&2; return 1; }
  return 0
}
FAKE
printf '#!/bin/bash\necho "2: eth0    inet ${TEST_IP}/24 brd x"\n' > "${test_job_dir}/bin/ip"
printf '#!/bin/bash\nfor a in "$@"; do [[ "$a" == text=* ]] && echo "${a#text=}" >> "%s/telegram"; done\nexit 0\n' "${test_job_dir}" > "${test_job_dir}/bin/curl"
chmod +x "${test_job_dir}/bin/"*

# run the job as cron does - an empty environment - with the stand-ins
#   NAME=VALUE ...           # extra environment: TEST_IP, LOADFAIL, BACKUPFAIL
#   DATA=...                 # the job argument, default - the sandbox job data
# return - the return code of the job
function test_job_run() {
  local data="DATA=${test_job_dir}/jobs-data.sh"
  local -a envs=()
  local arg
  for arg in "$@"
  do
    [[ "${arg}" == DATA=* ]] && { data="${arg}"; continue; }
    envs+=("${arg}")
  done  # arg
  rm -f "${test_job_dir}/telegram"
  env -i PATH="${test_job_dir}/bin:/usr/bin:/bin" HOME="${test_job_dir}" USER=test \
    TEST_SITE="${test_job_site}" TEST_IP="10.${test_job_octet}.1.5" "${envs[@]}" \
    /bin/bash "${DIR_TESTS}/../jobs/fe-backup.sh" "${data}" >"${test_job_dir}/stdout" 2>"${test_job_dir}/stderr"
}  # test_job_run

# run one check in a subshell - its output is shown - and count the result
#   name=TEXT                # what is checked
#   cmd=COMMAND              # the command, as one string
#   expect=0|1|any           # the expected return code
# return 0 - always
test_pass=0
test_fail=0
function test_check() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local rc

  ( eval "${cmd}" )
  rc=$?
  if [[ "${expect}" == "any" || "${rc}" == "${expect}" ]]
  then
    (( test_pass++ ))
    _log test="${name}" rc=${rc} result=ok
  else
    (( test_fail++ ))
    _log test="${name}" rc=${rc} expected=${expect} result=FAIL
  fi
}  # test_check

log="${test_job_dir}/logs/fe-backup-${test_job_site}.log"
err="${test_job_dir}/logs/fe-backup-${test_job_site}.err"
tg="${test_job_dir}/telegram"
_log jobs_data="${test_jobs_data}" site="${test_job_site}" octet="${test_job_octet}" sandbox="${test_job_dir}"

test_check name="no DATA= - stops before anything" expect=0 \
  cmd="! test_job_run DATA= && grep -q 'DATA= must be' '${test_job_dir}/stderr'"
test_check name="unknown site - reported" expect=0 \
  cmd="! test_job_run TEST_IP=10.254.1.5 && grep -q 'site not detected' '${tg}'"
test_check name="claugine not found - reported, no backups" expect=0 \
  cmd="mv '${test_job_dir}/claugine' '${test_job_dir}/claugine.off'; ! test_job_run; rc=\$?; mv '${test_job_dir}/claugine.off' '${test_job_dir}/claugine'; (( rc == 0 )) && grep -q 'CLAUGINE and PROVIDER must be defined' '${tg}'"
test_check name="data not loaded - reported, no backups" expect=0 \
  cmd="! test_job_run LOADFAIL=1 && grep -q 'data not loaded' '${tg}' && ! grep -q 'fake: backup' '${log}'"
test_check name="all backups ok - rc 0, one report per platform of the site" expect=0 \
  cmd="test_job_run && (( \$(grep -c 'completed successfully' '${tg}') == 3 )) && ! grep -q 'platforms.other' '${tg}'"
test_check name="claugine output in the log, not on stdout" expect=0 \
  cmd="test_job_run && grep -q 'fake claugine loaded' '${log}' && [[ ! -s '${test_job_dir}/stdout' ]]"
test_check name="one backup fails - rc 1, the others still run" expect=0 \
  cmd="! test_job_run BACKUPFAIL=p2 && grep -q 'p2 finished with errors' '${tg}' && grep -q 'p3 completed successfully' '${tg}' && grep -q 'fake: backup failed' '${err}'"
test_check name="logs are new on every run" expect=0 \
  cmd="test_job_run BACKUPFAIL=p2; test_job_run && [[ ! -s '${err}' ]]"

rm -rf "${test_job_dir}"
echo
_log tests=$(( test_pass + test_fail )) passed=${test_pass} failed=${test_fail}
(( test_fail == 0 ))
