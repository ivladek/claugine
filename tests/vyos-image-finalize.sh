#!/bin/bash
# VyOS image, phase 2: run after vyos-image-prepare has powered the builder VM off
#   image "VyOS Router <version>" created and replicated to all zone FEs
#   usage: vyos-image-finalize.sh vyos-builder-<version>
#   data: test_platform in tests/claugine_TEST.sh

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

vyos_image_finalize \
  platform="${test_platform}" \
  vm="${1:?usage: ${BASH_SOURCE[0]##*/} vyos-builder-<version>}"
