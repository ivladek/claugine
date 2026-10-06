#!/bin/bash
# latest Ubuntu Server and VyOS Stream ISOs, customized copies in the local repos
#   data: ${DIR_UBUNTU_REPO}, ${DIR_VYOS_REPO} from data/claugine-cli_*.sh, no test data needed

# claugine-cli: load if not loaded yet - the test can be run or sourced
DIR_TESTS="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if ! declare -F claugine_cli_help &>/dev/null
then
  source "${DIR_TESTS}/../bin/claugine-cli.sh" || { return 1 2>/dev/null || exit 1; }
fi

# test data, shared by all tests in this directory
source "${DIR_TESTS}/claugine-cli_TEST.sh" || { return 1 2>/dev/null || exit 1; }
#source "${DIR_TESTS}/claugine-cli_TEST_SAMPLE.sh" || { return 1 2>/dev/null || exit 1; }

iso_get_ubuntu version=26.04 && echo "ubuntu: ${iso_info[version]} ${iso_info[iso]}"
iso_get_vyos && echo "vyos: ${iso_info[version]} ${iso_info[iso]}"
