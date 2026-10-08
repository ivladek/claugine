#!/bin/bash
# platform=: exactly one full name of the inventory; anything else is rejected before any FE is touched
#   read-only: changes nothing on the platforms
#   data: test_platform

# claugine: load if not loaded yet - the test can be run or sourced
DIR_TESTS="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if ! declare -F claugine_help &>/dev/null
then
  source "${DIR_TESTS}/../bin/claugine.sh" || { return 1 2>/dev/null || exit 1; }
fi

# test data, shared by all tests in this directory
source "${DIR_TESTS}/claugine_TEST.sh" || { return 1 2>/dev/null || exit 1; }
#source "${DIR_TESTS}/claugine_TEST_SAMPLE.sh" || { return 1 2>/dev/null || exit 1; }

# user data: load if not loaded yet - test_inv, test_secrets, test_runtime of the test data
[[ "${INV:-}" != "{}" && -n "${INV:-}" ]] || data_load_provider inv="${test_inv}" secrets="${test_secrets}" runtime="${test_runtime}" || { return 1 2>/dev/null || exit 1; }

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

test_check name="no platform= - only the data check" cmd="data_runtime_refresh" expect=0
test_check name="the platform of the test" cmd="data_runtime_refresh platform=${test_platform}" expect=0
test_check name="platform= empty" cmd="data_runtime_refresh platform=" expect=1
test_check name="a list" cmd="data_runtime_refresh platform=\"${test_platform} ${test_platform}\"" expect=1
test_check name="a short name" cmd="data_runtime_refresh platform=${test_platform#platforms.}" expect=1
test_check name="not in the inventory" cmd="data_runtime_refresh platform=platforms.nosite.noplatform" expect=1
test_check name="a command without platform=" cmd="fe_cfg_ver_get" expect=1

echo
_log tests=$(( test_pass + test_fail )) passed=${test_pass} failed=${test_fail}
(( test_fail == 0 ))
