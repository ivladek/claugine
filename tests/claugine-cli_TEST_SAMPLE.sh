#!/bin/bash
set -u

# test data for tests/*.sh - sample, published
#   copy to claugine-cli_TEST.sh and set your values
#   not loaded by claugine-cli.sh, each test sources it

# common: FE to load the zones data from, zone for test objects
test_fe=10.71.101.30
test_zone_id=0

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
