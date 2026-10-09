#!/bin/bash
# every read-only command on test_platform; the tenant commands also from test_secondary
#   read-only: changes nothing on the platforms
#   data: test_platform, test_secondary, test_tenant

# claugine: load if not loaded yet - the test can be run or sourced
DIR_TESTS="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -v CLAUGINE_LOADED ]]
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

test_check name="fe_cfg_ver_get" cmd="fe_cfg_ver_get platform=${test_platform}" expect=0
test_check name="fe_configs_backups_list" cmd="fe_configs_backups_list platform=${test_platform}" expect=0
test_check name="acl_role_rights_get" cmd="acl_role_rights_get platform=${test_platform}" expect=0
test_check name="quota_vcpu_conf_show" cmd="quota_vcpu_conf_show platform=${test_platform}" expect=0
# evpn: 1 - VXLAN_TEP=dev found, an answer, not an error
test_check name="evpn_vtep_nic_get" cmd="evpn_vtep_nic_get platform=${test_platform}" expect=any
test_check name="evpn_vtep_vnet_get" cmd="evpn_vtep_vnet_get platform=${test_platform}" expect=any

if [[ -n "${test_tenant:-}" ]]
then
  test_check name="quota_tenant_get" cmd="quota_tenant_get platform=${test_platform} tenant=${test_tenant}" expect=0
  test_check name="acl_tenant_get on the primary" cmd="acl_tenant_get platform=${test_platform} tenants=${test_tenant}" expect=0
  test_check name="acl_tenant_get from the secondary" cmd="acl_tenant_get platform=${test_secondary} tenants=${test_tenant}" expect=0
else
  _log test="tenant commands" result=skipped reason="set test_tenant in the test data"
fi

echo
_log tests=$(( test_pass + test_fail )) passed=${test_pass} failed=${test_fail}
(( test_fail == 0 ))
