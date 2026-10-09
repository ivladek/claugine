#!/bin/bash
set -u

# test data for tests/*.sh - sample, published
#   copy to claugine_TEST.sh and set your values
#   not loaded by bin/claugine, each test sources it

# user data loaded by the tests with data_load_provider, if nothing is loaded yet
test_inv="${DIR_TESTS}/../data/SAMPLE/inventory"
test_secrets="${DIR_TESTS}/../data/SAMPLE/secrets"
test_runtime="${DIR_TESTS}/../data/SAMPLE/runtime"

# common: inventory platforms of the tests
test_platform=platforms.dc1.payload1        # the platform of the test objects; primary of a federation
test_secondary=platforms.dc2.payload1       # a secondary of the federation of test_platform
test_platform_local=platforms.dc1.payload2  # a platform without a federation
test_tenant=romashka                        # a tenant: read-only and dry-run tests
# create-vm.sh
test_vm_cluster=0
test_vm_name=cloud-test
test_vm_hostname=test-dc1
test_vm_cpu=2
test_vm_ram=4
test_vm_image="Ubuntu 26.04"
test_vm_disk=10
test_vm_vnet=core_dc1
test_vm_addr=10.71.101.222
test_vm_gw=10.71.101.254
test_vm_user=claugine-user
test_vm_pswd="ChangeMe*2026"
test_vm_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE admin@example.com"

# vyos-image-build.sh: builder VM
test_vyos_cluster=0
test_vyos_vnet=core_dc1
test_vyos_addr=10.71.101.223
test_vyos_gw=10.71.101.254
