#!/bin/bash
set -u

# data for jobs/*.sh - sample, published
#   copy to data/<set>/jobs-data.sh and set your values
#   the only file a job loads - the cron line passes it as the argument DATA=<this file>

# user of the job, as cron sets no USER: claugine connects as this user
USER="${USER:-claugine}"
HOME="${HOME:-/home/${USER}}"

# claugine loader, the data set to load with data_provider_load (inventory, secrets, runtime), logs of the jobs (see the cron lines)
CLAUGINE="${HOME}/claugine/bin/claugine"
PROVIDER="${HOME}/claugine/data/SAMPLE"
LOGS="${HOME}/backups"

# site of the admin host by the second octet of its eth0 address
declare -A SITES=(
  [71]=dc1
  [72]=dc2
)

# create-vyos-image.sh: the image VM, the test platform of the image
VYOS_IMAGE_VM_PLATFORM=platforms.dc1.mgmt
VYOS_IMAGE_VM_CLUSTER=0
VYOS_IMAGE_VM_VNET=core_dc1
VYOS_IMAGE_VM_ADDR=auto                      # or a fixed IP
VYOS_IMAGE_VM_GW=10.71.101.254
VYOS_IMAGE_TARGET_PLATFORMS=platforms.dc1.payload1   # the platforms of the image, separated by spaces, all or empty - every platform

# fireedge-views.sh: the platforms of the FireEdge views, separated by spaces
FIREEDGE_VIEWS_PLATFORMS="platforms.dc1.payload1 platforms.dc2.payload1"

# acl-reset.sh: the platforms of the ACLs, separated by spaces - a secondary is replaced by its primary
ACL_PLATFORMS="platforms.dc1.payload1"

# quota-set.sh, quota-list.sh, quota-show.sh: the platforms of kind shared, separated by spaces - the quotas are per zone
QUOTA_PLATFORMS="platforms.dc1.payload1 platforms.dc2.payload1"

# fe-backup.sh: Telegram report of each backup - bot sendMessage url with its token, and the chat id
REPORT_URL="https://api.telegram.org/bot<BOT_TOKEN>/sendMessage"
REPORT_CHAT="-1000000000000"
