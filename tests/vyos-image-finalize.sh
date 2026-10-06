#!/bin/bash
# VyOS image, phase 2: run after vyos-image-prepare has powered the builder VM off
#   image "VyOS Router <version>" created and replicated to all zone FEs
#   usage: vyos-image-finalize.sh vyos-builder-<version>
#   data: test_fe, test_zone_id in tests/claugine-cli_TEST.sh

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

vyos_image_finalize \
  zone_id="${test_zone_id}" \
  vm="${1:?usage: ${BASH_SOURCE[0]##*/} vyos-builder-<version>}"
