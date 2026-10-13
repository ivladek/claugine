#!/bin/bash
# VyOS image, phase 1: ISO with claugine scripts uploaded to the test zone, image VM created
#   then follow the manual steps printed at the end
#   data: test_platform, test_vyos_* in tests-data.sh of the data set

# claugine: load if not loaded yet - the test can be run or sourced
DIR_TESTS="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if ! declare -F data_load_provider &>/dev/null
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
[[ "${INV:-}" != "{}" && -n "${INV:-}" ]] || data_load_provider inv="${test_inv}" secrets="${test_secrets}" runtime="${test_runtime}" || { return 1 2>/dev/null || exit 1; }

vyos_image_vm_create \
  platform="${test_platform}" \
  cluster="${test_vyos_cluster}" \
  vnet="${test_vyos_vnet}" \
  addr="${test_vyos_addr}" \
  gw="${test_vyos_gw}" \
  vmfile="${DIR_RUNTIME}/${test_platform}-vyos-image-vm.id"
