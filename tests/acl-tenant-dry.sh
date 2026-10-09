#!/bin/bash
# acl_tenant_set dry run: from the secondary - switched to the primary, ACLs for every zone of the federation;
#   on a platform without a federation - its own zone only
#   dry run: changes nothing on the platforms
#   data: test_platform, test_secondary, test_platform_local, test_tenant

# claugine: load if not loaded yet - the test can be run or sourced
DIR_TESTS="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if ! declare -F data_load_provider &>/dev/null
then
  source "${DIR_TESTS}/../bin/claugine" || { return 1 2>/dev/null || exit 1; }
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

if [[ -z "${test_tenant:-}" ]]
then
  _log test="acl_tenant_set" result=skipped reason="set test_tenant in the test data"
  return 0 2>/dev/null || exit 0
fi

zones=$(( 1 + $(inv_list var=INV path=${test_platform}.fe.secondaries | wc -l) ))
test_check name="from the secondary: switched to the primary" cmd="acl_tenant_set platform=${test_secondary} tenants=${test_tenant} dry=yes | grep -q 'action=switch_to_primary'" expect=0
test_check name="from the secondary: dry run, nothing deleted" cmd="acl_tenant_set platform=${test_secondary} tenants=${test_tenant} dry=yes | grep -q 'acl_delete=\[ dry run: no ACLs will be deleted \]'" expect=0
test_check name="from the secondary: ZONE ACLs for at most ${zones} zones" cmd="n=\$(acl_tenant_set platform=${test_secondary} tenants=${test_tenant} dry=yes | grep -c ' ZONE/#'); (( n >= 1 && n <= ${zones} ))" expect=0
test_check name="no federation: one zone" cmd="n=\$(acl_tenant_set platform=${test_platform_local} tenants=${test_tenant} dry=yes | grep -c ' ZONE/#'); (( n <= 1 ))" expect=0

echo
_log tests=$(( test_pass + test_fail )) passed=${test_pass} failed=${test_fail}
(( test_fail == 0 ))
