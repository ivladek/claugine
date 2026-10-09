#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



# INDEX
#   vm_create  create Service VM
#   vm_wait    wait until the VM is in a state



help_data[vm_create]="\
  vm_create                  # create Service VM
    platform=NAME            #   platforms.<site>.<platform>
    cluster=ID|NAME          #   cluster for the VM, its images and vnets must be in it
    name=STRING              #
    hostname=STRING          #
    cpu=N                    #
    ram=N                    # GB
    image1=STRING            # name or id, image1/disk1 must be defined
    disk1=N                  # size in GB or iso for ISO
    ...N {2..9}              # imageN/diskN
    vnet1=STRING             # name or id, vnet1/addr1 must be defined
    addr1=LIST               # IP [IP ...] - the first is the address, the others aliases
                             #   auto - a free IP leased by OpenNebula from the VNet, no aliases
                             #   mac - a MAC address only, no IP, no aliases
    mtu1=N                   #
    gw1=IP                   # with an IP or auto, not with mac
    routes1=STRING           # with an IP or auto: 10.10.11.0/24 via 10.10.20.21, 10.10.12.0/24 via 10.10.20.22
    metric1=N                # with an IP or auto
    dns1=IP                  # with an IP or auto
    ...N {2..9}              # vnetN/addrN[/mtuN/gwN/routesN/metricN/dnsN]
    boot=STRING              # boot devices list, like - disk0,disk2,nic0
    autostart=YES|no         # create scheduled action to RESUME each hour
    user=STRING              #
    pswd=STRING              # hashed or clear text password
    key=STRING               # public key
    tz=TIMEZONE              # default - the timezone of the zone's site in the inventory"
# return 0 - VM created
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            not confirmed
#            a required parameter or the time zone not defined
#            cluster not found
#            image not found
#            wrong disk size
#            VNet not found or IP already leased
#            wrong address
#            address range can not be created
#            VM not created
function vm_create() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local tz="${tz:-}"
  local param
  local cluster_id vm_id image_id vnet_id n
  local image disk
  local vnet mtu gw routes metric dns
  local addrs addr addr_type nic_type nic_name_type
  local template_file template_data vip site

  data_runtime_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)

  # time zone: tz= or the site's timezone (a reference to config/data/timezones.yaml)
  if [[ -z "${tz}" ]]
  then
    site="${platform#platforms.}"
    site="${site%%.*}"
    tz=$(inv_value var=INV path=resources.${site}.timezone)
    tz=$(inv_value var=CONFIG path=${tz}.linux)
  fi
  if [[ -z "${tz}" ]]
  then
    _log_error "time zone of the site not found in config/data/timezones.yaml - pass tz="
    return 1
  fi

  for param in $(inv_list var=CONFIG path=cli.vm_parameters)
  do
    if [[ -z "${!param:-}" ]]
    then
      _log_error "VM required parameter \"${param}\" not defined"
      return 1
    fi
  done  # param

  if [[ ! "${pswd}" =~ ^\$6\$(rounds=[0-9]+\$)?[./0-9A-Za-z]{1,16}\$[./0-9A-Za-z]{86}$ ]]
  then
    pswd=$(mkpasswd -m sha-512 -R 100000 "${pswd}")
  fi
  pswd=$(base64 -w 0 <<< "${pswd}")

  cluster_id=$(_one_object_id platform="${platform}" object=cluster name="${cluster}") || return 1

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
      TIMEZONE = \"${tz}\"
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

    image_id=$(_one_object_id platform="${platform}" object=image name="${!image}") || return 1

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
      _log_error "Image \"${!image}\" [${image_id}] wrong size \"${!disk:-}\""
      return 1
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

    vnet_id=$(_one_object_id platform="${platform}" object=vnet name="${!vnet}") || return 1

    addrs="addr${n}"
    addr_type=ip
    nic_type=""
    nic_name_type="NAME"
    for addr in ${!addrs}
    do
      template_data+=$'\n'"NIC${nic_type} = ["$'\n'

      if [[ "${addr}" == "mac" ]]
      then
        addr=$(vnet_ar_mac_create platform=${platform} vnet=${vnet_id}) || return 1
        addr_type=mac
      else
        if [[ "${addr}" != "auto" ]]
        then
          if vnet_ip_leased platform=${platform} vnet=${vnet_id} ip="${addr}"
          then
            _log_error "IP Address ${addr} in VNet \"${!vnet}\" [${vnet_id}] has been already leased"
            return 1
          else
            vnet_ar_ip_create platform=${platform} vnet=${vnet_id} ip="${addr}" || return 1
          fi
        fi
        if [[ -z "${nic_type}" ]]
        then
          [[ -n "${!gw:-}" ]] && template_data+="  GATEWAY = \"${!gw}\","$'\n'
          [[ -n "${!routes:-}" ]] && template_data+="  ROUTES = \"${!routes}\","$'\n'
          [[ -n "${!metric:-}" ]] && template_data+="  METRIC = \"${!metric}\","$'\n'
          [[ -n "${!dns:-}" ]] && template_data+="  DNS = \"${!dns}\","$'\n'
        fi
      fi

      [[ -n "${!mtu:-}" ]] && template_data+="  MTU = \"${!mtu}\","$'\n'
      [[ "${addr}" != "auto" ]] && template_data+="  ${addr_type^^} = \"${addr}\"",$'\n'
      template_data+="  ${nic_name_type} = \"VMNIC${n}\","$'\n'
      template_data+="  NETWORK_ID = \"${vnet_id}\""$'\n'
      template_data+="]"$'\n'

      nic_type="_ALIAS"
      nic_name_type="PARENT"
    done  # addr

    (( n++ ))
    vnet="vnet${n}"
  done  # until

  template_file=$($ssh ${vip} "sudo -u oneadmin mktemp")
  $ssh ${vip} "sudo -u oneadmin tee ${template_file} &>/dev/null" <<< "${template_data}"
  vm_id=$($ssh ${vip} "sudo -u oneadmin onevm create ${template_file}")
  vm_id="${vm_id##* }"
  $ssh ${vip} "sudo rm -f ${template_file}"

  _log_std \
    zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" \
    vm_name="${name}" \
    vm_id="${vm_id}" \
    vm_addr="${addr1}" \
    vm_hostname="${hostname}"
  if [[ -z "${vm_id}" ]]
  then
    _log_error "VM was not created"
    _log "${template_data}"
    return 1
  fi

  return 0
}  # vm_create




help_data[vm_wait]="\
  vm_wait                    # wait until the VM is in a state
    platform=NAME            #   platforms.<site>.<platform>
    vm=ID|NAME               #   name or id
    state=NAME(RUNNING)      #   a VM state: POWEROFF, STOPPED, UNDEPLOYED, DONE, ...
                             #     or an LCM state of an ACTIVE VM: RUNNING, BOOT, ...
                             #     names: <CONFIG.onefe.states.vm>, <CONFIG.onefe.states.vm_lcm>
    limit=N                  #   seconds, default is <CONFIG.onefe.timeouts.vm_wait>"
# return 0 - the VM is in the state
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh, unknown state name
#            VM not found, VM in a failure state, VM DONE while waiting for another state
#            timeout
function vm_wait() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local vm="${vm:-}"
  local state="${state:-RUNNING}"
  local limit="${limit:-$(inv_value var=CONFIG path=onefe.timeouts.vm_wait)}"
  local start=$(date '+%s')
  local json vm_state lcm_state current vip

  data_runtime_refresh platform="${platform:-}" || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)

  state="${state^^}"
  if ! jq -e --arg s "${state}" '[.onefe.states.vm[], .onefe.states.vm_lcm[]] | index($s)' <<< "${CONFIG}" &>/dev/null
  then
    _log_error "unknown VM state \"${state}\" - see CONFIG.onefe.states.vm and CONFIG.onefe.states.vm_lcm"
    return 1
  fi

  _log_std -n vm="${vm}" action="wait_state" desired_state="${state}" status="[."
  while :
  do
    json=$($ssh ${vip} "sudo -u oneadmin onevm show \"${vm}\" -j 2>/dev/null")
    vm_state=$(jq -r '.VM.STATE // ""' <<< "${json}" 2>/dev/null)
    lcm_state=$(jq -r '.VM.LCM_STATE // ""' <<< "${json}" 2>/dev/null)
    if [[ -z "${vm_state}" ]]
    then
      _log "] state=not_found"
      return 1
    fi

    # the name of the current state: the LCM state of an ACTIVE VM, the VM state otherwise
    if [[ "${vm_state}" == "3" ]]   # 3 - ACTIVE: the LCM state tells what it does
    then
      current=$(inv_value var=CONFIG path=onefe.states.vm_lcm.${lcm_state})
    else
      current=$(inv_value var=CONFIG path=onefe.states.vm.${vm_state})
    fi
    current="${current:-${vm_state}/${lcm_state}}"

    if [[ "${current}" == "${state}" || ( "${state}" == "ACTIVE" && "${vm_state}" == "3" ) ]]   # 3 - ACTIVE, any LCM state
    then
      _log "] state=${current}"
      return 0
    fi

    if [[ "${current}" == *FAILURE || "${current}" == "UNKNOWN" || "${current}" == "DONE" ]]
    then
      _log "] state=${current}"
      _log_error "VM ${vm} is ${current}, not ${state}"
      return 1
    fi

    if (( $(date '+%s') - start > limit ))
    then
      _log "] state=${current} result=timeout limit=${limit}s"
      return 1
    fi

    sleep 5
    _log -n "."
  done
}  # vm_wait
