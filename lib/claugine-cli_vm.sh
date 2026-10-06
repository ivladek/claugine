#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"


claugine_cli_commands+=( "\
  vm_create                  # create Service VM
    zone_id=N                #   zone of the current installation
    cluster=ID|NAME          #   cluster for the VM, its images and vnets must be in it
    name=STRING              #
    hostname=STRING          #
    cpu=N                    #
    ram=N                    # GB
    image1=STRING            # name or id, image1/disk1 must be defined
    disk1=N                  # size in GB or iso for ISO
    ...N {2..9}              # imageN/diskN
    vnet1=STRING             # name or id, vnet1/addr1 must be defined
    addr1=LIST               # first is address, others aliases
    mtu1=N                   #
    gw1=IP                   # only one default gateway
    routes1=STRING           # 10.10.11.0/24 via 10.10.20.21, 10.10.12.0/24 via 10.10.20.22
    metric1=N                # 
    dns1=IP                  #
    ...N {2..9}              # vnetN/addrN[/mtuN/gwN/routesN/metricN/dnsN]
    boot=STRING              # boot devices list, like - disk0,disk2,nic0
    autostart=YES|no         # create scheduled action to RESUME each hour
    user=STRING              #
    pswd=STRING              # hashed or clear text password
    key=STRING               # public key
    tz=TIMEZONE              # default is ${TIMEZONE_DEFAULT}"
)
function vm_create() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n _zone=${zone_data}
  local param
  local cluster_id vm_id image_id vnet_id n
  local image disk
  local vnet mtu gw routes metric dns
  local addrs addr addr_type nic_type nic_name_type
  local template_file template_data

  _zone_init || return 1
  _zone_check zone_id="${zone_id:-}" || return 1

  for param in "${VM_REQUIRED_PARAMETERS[@]}"
  do
    if [[ -z "${!param:-}" ]]
    then
      echo
      echo "!!! ERROR !!! VM required parameter \"${param}\" not defined"
      echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
      return 2
    fi
  done  # param

  if [[ ! "${pswd}" =~ ^\$6\$(rounds=[0-9]+\$)?[./0-9A-Za-z]{1,16}\$[./0-9A-Za-z]{86}$ ]]
  then
    pswd=$(mkpasswd -m sha-512 -R 100000 "${pswd}")
  fi
  pswd=$(base64 -w 0 <<< "${pswd}")

  cluster_id=$(_one_object_id zone_id="${zone_id}" object=cluster name="${cluster}") || return 3

  template_data="
    NAME = \"${name}\"
    SCHED_REQUIREMENTS = \"HYPERVISOR=kvm & CLUSTER_ID=${cluster_id}\"
    FEATURES = [
      MIGRATE_AUTO_CONVERGE = \"10,5\",
      MIGRATE_COMPRESSED = \"YES\"
    ]
    OS = ["$'\n'

  [[ -n "${boot:-}" ]] && template_data+="      BOOT = \"${boot}\","$'\n'

  template_data+="
      ARCH = \"x86_64\",
      FIRMWARE = \"UEFI\",
      MACHINE = \"q35\"
    ]
    CPU = \"${cpu}\"
    VCPU = \"${cpu}\"
    CPU_MODEL = [
      MODEL = \"host-passthrough\"
    ]
    MEMORY = \"$(( ram * 1024 ))\"
    MEMORY_RESIZE_MODE = \"BALLOONING\"
    VIDEO = [
      TYPE = \"virtio\"
    ]
    GRAPHICS = [
      LISTEN = \"0.0.0.0\",
      TYPE = \"VNC\"
    ]
    CONTEXT = [
      SET_HOSTNAME = \"${hostname}\",
      USERNAME = \"${user}\",
      CRYPTED_PASSWORD_BASE64 = \"${pswd}\",
      NETWORK = \"yes\",
      SSH_PUBLIC_KEY = \"${key}\",
      TIMEZONE = \"${tz:-${TIMEZONE_DEFAULT}}\"
    ]"$'\n'

  [[ "${autostart:-}" != no ]] && template_data+="
    SCHED_ACTION = [
      TYPE     = \"VM\",
      ACTION   = \"resume\",
      TIME     = \"+1000\",
      REPEAT   = \"3\",
      DAYS     = \"1\",
      END_TYPE = \"0\"
  ]"$'\n'

  n=1
  image="image${n}"
  until [[ -z "${!image:-}" ]]
  do
    disk="disk${n}"

    image_id=$(_one_object_id zone_id="${zone_id}" object=image name="${!image}") || return 4

    template_data+="
      DISK = [
        IMAGE_ID = \"${image_id}\","$'\n'

    if [[ "${!disk:-}" == "iso" ]]
    then
      template_data+="        DISK_TYPE = \"CDROM\""$'\n'
    elif [[ "${!disk:-}" =~ ^[0-9]+$ ]]
    then
      template_data+="        SIZE = \"$(( ${!disk} * 1024 ))\""$'\n'
    else
      echo
      echo "!!! ERROR !!! Image \"${!image}\" [${image_id}] wrong size \"${!disk:-}\""
      echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
      return 5
    fi

    template_data+="      ]"$'\n'

    (( n++ ))
    image="image${n}"
  done  # until

  n=1
  vnet="vnet${n}"
  until [[ -z "${!vnet:-}" ]]
  do
    mtu="mtu${n}"
    gw="gw${n}"
    routes="routes${n}"
    metric="metric${n}"
    dns="dns${n}"

    vnet_id=$(_one_object_id zone_id="${zone_id}" object=vnet name="${!vnet}") || return 6

    addrs="addr${n}"
    addr_type=ip
    nic_type=""
    nic_name_type="NAME"
    for addr in ${!addrs}
    do
      template_data+=$'\n'"NIC${nic_type} = ["$'\n'

      if [[ "${addr}" == "mac" ]]
      then
        addr=$(vnet_ar_mac_create zone_id=${zone_id} vnet=${vnet_id}) || return 7
        addr_type=mac
      elif vnet_ip_leased zone_id=${zone_id} vnet=${vnet_id} ip="${addr}"
      then
        echo
        echo "!!! ERROR !!! IP Address ${addr} in VNet \"${!vnet}\" [${vnet_id}] has been already leased"
        echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
        return 6
      else
        vnet_ar_ip_create zone_id=${zone_id} vnet=${vnet_id} ip="${addr}" || return 8
        if [[ -z "${nic_type}" ]]
        then
          [[ -n "${!gw:-}" ]] && template_data+="  GATEWAY = \"${!gw}\","$'\n'
          [[ -n "${!routes:-}" ]] && template_data+="  ROUTES = \"${!routes}\","$'\n'
          [[ -n "${!metric:-}" ]] && template_data+="  METRIC = \"${!metric}\","$'\n'
          [[ -n "${!dns:-}" ]] && template_data+="  DNS = \"${!dns}\","$'\n'
        fi
      fi

      [[ -n "${!mtu:-}" ]] && template_data+="  MTU = \"${!mtu}\","$'\n'

      template_data+="  ${nic_name_type} = \"VMNIC${n}\","$'\n'
      template_data+="  NETWORK_ID = \"${vnet_id}\","$'\n'
      template_data+="  ${addr_type^^} = \"${addr}\""$'\n'
      template_data+="]"$'\n'

      nic_type="_ALIAS"
      nic_name_type="PARENT"
    done  # addr

    (( n++ ))
    vnet="vnet${n}"
  done  # until

  template_file=$($ssh ${_zone[${zone_id},vip]} "sudo -u oneadmin mktemp")
  $ssh ${_zone[${zone_id},vip]} "sudo -u oneadmin tee ${template_file} &>/dev/null" <<< "${template_data}"
  vm_id=$($ssh ${_zone[${zone_id},vip]} "sudo -u oneadmin onevm create ${template_file}")
  vm_id="${vm_id##* }"
  $ssh ${_zone[${zone_id},vip]} "sudo rm -f ${template_file}"

  echo "$(_zone_label ${zone_id}) zone_state=${_zone[${zone_id},state]} vm_name=${name} vm_id=${vm_id} vm_addr=${addr1} vm_hostname=${hostname}"
  if [[ -z "${vm_id}" ]]
  then
    echo "!!! ERROR !!! VM was not created"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    echo "${template_data}"
    return 9
  fi

  return 0
}  # vm_create
