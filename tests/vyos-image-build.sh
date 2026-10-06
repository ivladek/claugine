#!/bin/bash
# VyOS image, phase 1: ISO with claugine scripts uploaded to the test zone, builder VM created
#   then follow the manual steps printed at the end
#   data: test_fe, test_zone_id, test_vyos_* in tests/claugine-cli_TEST.sh

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

vyos_image_build \
  zone_id="${test_zone_id}" \
  cluster="${test_vyos_cluster}" \
  vnet="${test_vyos_vnet}" \
  addr="${test_vyos_addr}" \
  gw="${test_vyos_gw}"
