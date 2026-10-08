#!/bin/bash
# test VM: Ubuntu, one disk, one NIC, user with password and ssh key
#   data: test_vm_* in tests/claugine_TEST.sh

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

vm_create \
  platform="${test_platform}" \
  cluster="${test_vm_cluster}" \
  name="${test_vm_name}" \
  hostname="${test_vm_hostname}" \
  cpu="${test_vm_cpu}" \
  ram="${test_vm_ram}" \
  image1="${test_vm_image}" \
  disk1="${test_vm_disk}" \
  vnet1="${test_vm_vnet}" \
  addr1="${test_vm_addr}" \
  gw1="${test_vm_gw}" \
  user="${test_vm_user}" \
  pswd="${test_vm_pswd}" \
  key="${test_vm_key}"
