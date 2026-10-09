#!/bin/bash
# user data: load it, check the federations, the runtime data of test_platform, the getters
#   read-only: changes nothing on the platforms
#   data: test_inv, test_secrets, test_runtime, test_platform, test_secondary

# claugine: load if not loaded yet - the test can be run or sourced
DIR_TESTS="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -v CLAUGINE_LOADED ]]
then
  source "${DIR_TESTS}/../bin/claugine.sh" || { return 1 2>/dev/null || exit 1; }
fi

# test data, shared by all tests in this directory
source "${DIR_TESTS}/claugine_TEST.sh" || { return 1 2>/dev/null || exit 1; }
#source "${DIR_TESTS}/claugine_TEST_SAMPLE.sh" || { return 1 2>/dev/null || exit 1; }

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

# always load: loading is what is tested; the federation check runs inside
data_load_provider inv="${test_inv}" secrets="${test_secrets}" runtime="${test_runtime}"
test_check name="data_load_provider" cmd="[[ -n \"\${INV:-}\" && \"\${INV}\" != '{}' ]]" expect=0

# runtime data of test_platform, collected by data_load_provider
for key in id name state leader,ip
do
  test_check name="RUNTIME[${test_platform},${key}]" cmd="[[ -n \"\${RUNTIME[${test_platform},${key}]:-}\" ]]" expect=0
done  # key

# federation: test_secondary is listed in fe.secondaries of test_platform, and names it as primary
test_check name="fe.secondaries of ${test_platform}" cmd="inv_list var=INV path=${test_platform}.fe.secondaries | grep -qx ${test_secondary}.fe" expect=0
test_check name="fe.primary of ${test_secondary}" cmd="[[ \"\$(inv_value var=INV path=${test_secondary}.fe.primary)\" == ${test_platform}.fe ]]" expect=0
test_check name="consistency check again" cmd="_data_consistancy_check" expect=0

# getters, one per shape of the data
test_check name="inv_value: fe.vip" cmd="[[ -n \"\$(inv_value var=INV path=${test_platform}.fe.vip)\" ]]" expect=0
test_check name="inv_list: nodes_order.direct" cmd="[[ \$(inv_list var=CONFIG path=onefe.nodes_order.direct | wc -l) -gt 0 ]]" expect=0
test_check name="inv_keys: platforms" cmd="[[ \$(inv_keys var=INV path=platforms | wc -l) -gt 0 ]]" expect=0
test_check name="inv_list_field: ubuntu.iso.files" cmd="[[ -n \"\$(inv_list_field var=CONFIG path=ubuntu.iso.files field=name)\" ]]" expect=0
test_check name="inv_list_find: ubuntu.iso.files" cmd="file=\$(inv_list_field var=CONFIG path=ubuntu.iso.files field=name | head -n1); inv_list_find var=CONFIG path=ubuntu.iso.files value=\"\${file}\" | jq -e .path >/dev/null" expect=0
test_check name="missing path prints nothing" cmd="[[ -z \"\$(inv_value var=INV path=platforms.no.such.path)\" ]]" expect=0

echo
_log tests=$(( test_pass + test_fail )) passed=${test_pass} failed=${test_fail}
(( test_fail == 0 ))
