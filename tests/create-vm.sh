#!/bin/bash
# test VM: Ubuntu, one disk, one NIC, user with password and ssh key
#   data: test_vm_* in tests-data.sh of the data set

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

# user data: load if not loaded yet - test_inv, test_secrets, test_runtime of the test data
[[ "${INV:-}" != "{}" && -n "${INV:-}" ]] || data_provider_load inv="${test_inv}" secrets="${test_secrets}" runtime="${test_runtime}" || { return 1 2>/dev/null || exit 1; }

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
