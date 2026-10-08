#!/bin/bash
# VyOS image, phase 1: ISO with claugine scripts uploaded to the test zone, builder VM created
#   then follow the manual steps printed at the end
#   data: test_platform, test_vyos_* in tests/claugine_TEST.sh

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

vyos_image_build \
  platform="${test_platform}" \
  cluster="${test_vyos_cluster}" \
  vnet="${test_vyos_vnet}" \
  addr="${test_vyos_addr}" \
  gw="${test_vyos_gw}"
