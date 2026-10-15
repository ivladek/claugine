#!/bin/bash
# latest Ubuntu Server and VyOS Stream ISOs, customized copies in the local repos
#   data: test_platform - repos.zakroma.local_dir of its site is the repository

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

iso_get_ubuntu platform="${test_platform}" version=26.04 result=iso && echo "iso=${iso}"
iso_get_vyos platform="${test_platform}" result=iso && echo "iso=${iso}"
