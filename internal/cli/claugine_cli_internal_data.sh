#!/bin/bash
set -u

#
# ZONE_DATA: {
#   [list] = STRING
#   [timestamp] = UNIX EPOCH - refresh timestamp
#   [${zone},name] = STRING
#   [${zone},vip] = IP
#   [${zone},state] = N
#   [${zone},${role},id] = N
#   [${zone},${role},name] = STRING
#   [${zone},${role},ip] = IP
#   [${zone},${role},state] = N
#   [${zone},${role},type] = [types]
#   [${zone},${role},cfg_cur] = STRING
#   [${zone},${role},cfg_new] = STRING
#   [${zone},${role},db_zone_cur] = STRING
#   [${zone},${role},db_zone_new] = STRING
#   [${zone},${role},db_fed_cur] = STRING
#   [${zone},${role},db_fed_new] = STRING
#     [roles] = leader | follower1 | follower2
#     [types] = dedicated | mixed
#
# LIST:
#   space separatd list
#   ALL


declare -gr FIREEDGE_VIEWS_CUSTOM="opennebula-fireedge-views/custom"

declare -gr FIREEDGE_VIEWS_LIST=(
  user
  groupadmin
)

declare -gra FE_SERVICES=(
  opennebula
  opennebula-fireedge
  opennebula-flow
  opennebula-gate
)

declare -gra ONE_FE_SERVICES_ALL=(
  ${FE_SERVICES[@]}
  mysql
)

declare -grA VM_OPERATIONS=(
  [ADMIN]="delete, deploy, hold, migrate, recover, release, resched, retry, stop, suspend, vmgroup, undeploy"
  [MANAGE]="backup, disk-attach, disk-resize, disk-saveas, disk-snapshot, exec, nic-attach, pci-attach, poweroff, reboot, rename, resize, restore, resume, sched-action, sg-attach, snapshot, terminate, update, updateconf"
  [USE]=""
)

declare -gra FE_NODE_ROLES=(
  leader
  follower1
  follower2
)

declare -gra FE_NODE_ROLES_REVERSED=(
  follower2
  follower1
  leader
)

declare -gr DATA_REFRESH_LIMIT=300
declare -gr HOST_DISABLE_TIMEOUT=180
declare -gr HOST_ENABLE_TIMEOUT=180
declare -gr HOST_FLUSH_TIMEOUT=1800


declare -gra ONE_FE_CONFIG_DIRS=(
  ${HOME}
  ${HOME}/.ssh
  ${HOME}/.one
  /etc/one
  /etc/one/fireedge/sunstone/views
  /var/lib/one
  /var/lib/one/.ssh
  /var/lib/one/.one
  /var/lib/one/backups/config
  /var/lib/one/remotes/vnm/vxlan
  /var/lib/one/remotes/etc/vnm
)

declare -gra ONE_FE_BACKUP_DIRS=(
  /etc/one
  /var/lib/one/.ssh
  /var/lib/one/.one
  /var/lib/one/remotes
)

declare -gr ONE_BACKUP_DAYS=10

declare -rga ONE_HOST_STATE=(
  [0]=INIT	                # Host initialization/monitoring starting
  [1]=MONITORING_MONITORED	# Host is being monitored
  [2]=MONITORED	            # Normal operational state
  [3]=ERROR	                # Monitoring/host error
  [4]=DISABLED	            # Host manually disabled
  [5]=MONITORING_ERROR	    # Monitoring an errored host
  [6]=MONITORING_INIT	      # Monitoring during initialization
  [7]=MONITORING_DISABLED	  # Monitoring a disabled host
  [8]=OFFLINE	              # Host marked offline
)
