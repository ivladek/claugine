#!/bin/bash
# test VM: Ubuntu, one disk, one NIC, user with password and ssh key
#   data: test_vm_* in tests/claugine-cli_TEST.sh

# claugine-cli: load if not loaded yet - the test can be run or sourced
DIR_TESTS="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if ! declare -F claugine_cli_help &>/dev/null
then
  source "${DIR_TESTS}/../bin/claugine-cli.sh" || { return 1 2>/dev/null || exit 1; }
fi

# test data, shared by all tests in this directory
source "${DIR_TESTS}/claugine-cli_TEST.sh" || { return 1 2>/dev/null || exit 1; }
#source "${DIR_TESTS}/claugine-cli_TEST_SAMPLE.sh" || { return 1 2>/dev/null || exit 1; }

fe_data_refresh fe="${test_fe}"

vm_create \
  zone_id="${test_zone_id}" \
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
