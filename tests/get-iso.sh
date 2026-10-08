#!/bin/bash
# latest Ubuntu Server and VyOS Stream ISOs, customized copies in the local repos
#   data: repos.zakroma of the site of the loaded platform, or pass site= / directory=

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

iso_get_ubuntu version=26.04 && echo "ubuntu: ${iso_info[version]} ${iso_info[iso]}"
iso_get_vyos && echo "vyos: ${iso_info[version]} ${iso_info[iso]}"
