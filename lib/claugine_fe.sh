#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



# INDEX
#   fe_backup                   backup all data required toi restore FE from scratch
#   fe_cfg_ver_get              show current and required version for configuration and db
#   fe_configs_backups_cleanup  cleanup all configs backups created during upgrade
#   fe_configs_backups_list     show directories with configs backups created during upgrade
#   fe_data_refresh             collect the runtime data of a platform again: zone, FE nodes, datastores
#   fe_disk_cleanup             clean data from /var/tmp after unsuccessful image loading
#   fe_fireedge_restart         restart fireedge service on FE
#   fe_fireedge_views_update    distribute updated Fireedge views to FE
#   fe_os_update                install OS updates
#   fe_services_restart         restart OpenNebula services on FE in right order



help_data[fe_backup]="\
  fe_backup                  # backup all data required toi restore FE from scratch
    platform=NAME            #   platforms.<site>.<platform>
    full=yes|NO              #   include configs or db only
                             #   to repos.backups.local_dir of the site of each platform: <vip>_<time>/;
                             #   the federated DB is saved on the primary platform of a federation"
# return 0 - done
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            backups directory can not be created
function fe_backup() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local full="${full:-no}"
  local node dir
  local backup_path backup_base backup_file
  local dir_backups vip site

  data_runtime_refresh platform="${platform:-}" fe=yes || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)

  # backup directory of the site of the platform
  site="${platform#platforms.}"
  site="${site%%.*}"
  dir_backups=$(inv_value var=INV path=resources.${site}.repos.backups.local_dir)
  if ! mkdir -p "${dir_backups}"
  then
    _log_error "backups directory \"${dir_backups}\" can not be created"
    return 1
  fi
  backup_path="${dir_backups}/${vip}_$(date '+%H-%M-%S')"

  _log_std \
    zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" \
    action="backup" \
    backup_to="${backup_path}"

  for node in $(inv_list var=CONFIG path=onefe.nodes_order.direct)
  do
    backup_base="${RUNTIME[${platform},id]}-${RUNTIME[${platform},name]}-${vip}_${RUNTIME[${platform},${node},id]}-${RUNTIME[${platform},${node},name]}-${RUNTIME[${platform},${node},ip]}-${node}"
    if [[ "${full}" == "yes" ]]
    then
      for dir in $(inv_list var=CONFIG path=onefe.backup.dirs)
      do
        _log_fe backup_from="${dir}" backup_to="${backup_path}"
        backup_file=$(sed 's|^/||; s|/|-|g; s|\.||g' <<< "${dir}")
        backup_file="${backup_base}_${backup_file}.zip"
        $ssh ${RUNTIME[${platform},${node},ip]} "sudo zip -rq /tmp/${backup_file} ${dir}"
        rsync --progress -e "$ssh" --rsync-path "sudo rsync" ${RUNTIME[${platform},${node},ip]}:/tmp/${backup_file} ${backup_path}/
        $ssh ${RUNTIME[${platform},${node},ip]} "sudo rm -vf /tmp/${backup_file}"
      done  # dir
    fi

    if [[ ${node} == "leader" ]]
    then
      _log_fe backup_from="onedb-zone" backup_to="${backup_path}"
      backup_file="${backup_base}_onedb-zone"
      $ssh ${RUNTIME[${platform},${node},ip]} "sudo onedb backup /tmp/${backup_file}.sql --force"
      $ssh ${RUNTIME[${platform},${node},ip]} "sudo zip -q /tmp/${backup_file}.zip /tmp/${backup_file}.sql"
      rsync --progress -e "$ssh" --rsync-path "sudo rsync" ${RUNTIME[${platform},${node},ip]}:/tmp/${backup_file}.zip ${backup_path}/
      $ssh ${RUNTIME[${platform},${node},ip]} "sudo rm -vf /tmp/${backup_file}.sql"
      $ssh ${RUNTIME[${platform},${node},ip]} "sudo rm -vf /tmp/${backup_file}.zip"

      if [[ "$(inv_value var=INV path=${platform}.fe.mode)" == "primary" ]]
      then
        _log_fe backup_from="onedb-federated" backup_to="${backup_path}"
        backup_file="${backup_base}_onedb-federated"
        $ssh ${RUNTIME[${platform},${node},ip]} "sudo onedb backup /tmp/${backup_file}.sql --federated --force"
        $ssh ${RUNTIME[${platform},${node},ip]} "sudo zip /tmp/${backup_file}.zip /tmp/${backup_file}.sql"
        rsync --progress -e "$ssh" --rsync-path "sudo rsync" ${RUNTIME[${platform},${node},ip]}:/tmp/${backup_file}.zip ${backup_path}/
        $ssh ${RUNTIME[${platform},${node},ip]} "sudo rm -vf /tmp/${backup_file}.sql"
        $ssh ${RUNTIME[${platform},${node},ip]} "sudo rm -vf /tmp/${backup_file}.zip"
      fi
    fi
  done  # node

  _log dir="${dir_backups}" action="cleanup" backups_age="$(inv_value var=CONFIG path=onefe.backup.days)d"
  find "${dir_backups}" \
    -mindepth 1 \
    -maxdepth 1 \
    -type d \
    -name "*_*" \
    -mtime +$(inv_value var=CONFIG path=onefe.backup.days) \
    -exec rm -rvf {} +
  du -hd0 ${dir_backups}
  du -hd1 ${dir_backups}/*

  _log_std backup="${backup_path}"
  ls -lah ${backup_path}

  return 0
}  # fe_backup



help_data[fe_cfg_ver_get]="\
  fe_cfg_ver_get             # show current and required version for configuration and db
    platform=NAME            #   platforms.<site>.<platform>"
# return 0 - done
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
function fe_cfg_ver_get() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local node cfg cur new vip

  data_runtime_refresh platform="${platform:-}" fe=yes || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)
  _log_std zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" action="cfg_ver_get"

  for node in $(inv_list var=CONFIG path=onefe.nodes_order.direct)
  do
    cfg=$($ssh ${RUNTIME[${platform},${node},ip]} "sudo onecfg status")
    cur=$(awk '$1 == "Config:" {print $2}' <<< "${cfg}")
    new=$(awk '$1 == "OpenNebula:" {print $2}' <<< "${cfg}")
    _log_fe cfg_cur="${cur}" cfg_new="${new}"

    cfg=$($ssh ${RUNTIME[${platform},${node},ip]} "sudo onedb version")
    cur=$(awk '$1 == "Local:" {print $2}' <<< "${cfg}")
    new=$(awk '/^Required local version:/ {print $4}' <<< "${cfg}")
    _log_fe db_zone_cur="${cur}" db_zone_new="${new}"
    cur=$(awk '$1 == "Shared:" {print $2}' <<< "${cfg}")
    new=$(awk '/^Required shared version:/ {print $4}' <<< "${cfg}")
    _log_fe db_fed_cur="${cur}" db_fed_new="${new}"
  done  # node

  return 0
}  # fe_cfg_ver_get



help_data[fe_configs_backups_cleanup]="\
  fe_configs_backups_cleanup # cleanup all configs backups created during upgrade
    platform=NAME            #   platforms.<site>.<platform>
    confirm=yes|NO           #   to suppress interactive confirmation"
# return 0 - done
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            not confirmed
function fe_configs_backups_cleanup() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local src="${src:-${DIR_FILES}/$(inv_value var=CONFIG path=onefe.fireedge_views.dir)}"
  local node vip

  data_runtime_refresh platform="${platform:-}" fe=yes || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)

  _log_std zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" action="cleanup_configs_backups"
  _stop_without_confirmation confirm=${confirm:-no} && return 1

  for node in $(inv_list var=CONFIG path=onefe.nodes_order.direct)
  do
    _log_fe action="cleanup_configs_backups" dir="${HOME}/.ssh/id_rsa*"
    $ssh ${RUNTIME[${platform},${node},ip]} "sudo find ${HOME}/.ssh \
      -type f \
      -name 'id_rsa*' \
      -exec rm -fv -- {} +"
    $ssh ${RUNTIME[${platform},${node},ip]} "sudo find ${HOME}/.ssh \
      -type f \
      -name 'known_hosts*' \
      -exec rm -fv -- {} +"

    _log_fe action="cleanup_configs_backups" dir="/etc/one.*"
    $ssh ${RUNTIME[${platform},${node},ip]} "sudo rm -fv /etc/one.pre-upgrade"
    $ssh ${RUNTIME[${platform},${node},ip]} "sudo find /etc \
      -maxdepth 1 \
      -type d \
      -name 'one.*' \
      -exec rm -rfv -- {} +"

    _log_fe action="cleanup_configs_backups" dir="/etc/one/*.original"
    $ssh ${RUNTIME[${platform},${node},ip]} "sudo find /etc/one \
      -type f \
      -name '*.original' \
      -exec rm -fv -- {} +"

    _log_fe action="cleanup_configs_backups" dir="/var/lib/one/remotes.*"
    $ssh ${RUNTIME[${platform},${node},ip]} "sudo rm -fv /var/lib/one/remotes.pre-upgrade"
    $ssh ${RUNTIME[${platform},${node},ip]} "sudo find /var/lib/one \
      -maxdepth 1 \
      -type d \
      -name 'remotes.*' \
      -exec rm -rfv -- {} +"

    _log_fe action="cleanup_configs_backups" dir="/var/lib/one/backups/config/*"
    $ssh ${RUNTIME[${platform},${node},ip]} "sudo find /var/lib/one/backups/config \
      -mindepth 1 \
      -maxdepth 1 \
      -type d \
      -exec rm -rfv -- {} +"

    _log_fe action="cleanup_configs_backups" dir="/etc/one/*.original"
    $ssh ${RUNTIME[${platform},${node},ip]} "sudo find /var/lib/one/remotes \
      -type f \
      -name '*.dpkg-old' \
      -exec rm -fv -- {} +"

    _log_fe action="cleanup_configs_backups" dir="/var/lib/one/.ssh/id_rsa*"
    $ssh ${RUNTIME[${platform},${node},ip]} "sudo find /var/lib/one/.ssh \
      -type f \
      -name 'id_rsa*' \
      -exec rm -fv -- {} +"
    $ssh ${RUNTIME[${platform},${node},ip]} "sudo find /var/lib/one/.ssh \
      -type f \
      -name 'known_hosts*' \
      -exec rm -fv -- {} +"

    _log_fe action="cleanup_configs_backups" dir="/var/lib/one/remotes/etc/*.original"
    $ssh ${RUNTIME[${platform},${node},ip]} "sudo find /var/lib/one/remotes/etc \
      -type f \
      -name '*.original' \
      -exec rm -fv -- {} +"

    _log_fe action="cleanup_configs_backups" dir="/var/lib/one/*.sql"
    $ssh ${RUNTIME[${platform},${node},ip]} "sudo find /var/lib/one \
      -maxdepth 1 \
      -type f \
      -name '*.sql' \
      -exec rm -fv -- {} +"
  done  # node

  return 0
}  # fe_configs_backups_cleanup



help_data[fe_configs_backups_list]="\
  fe_configs_backups_list    # show directories with configs backups created during upgrade
    platform=NAME            #   platforms.<site>.<platform>"
# return 0 - done
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
function fe_configs_backups_list() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local src="${src:-${DIR_FILES}/$(inv_value var=CONFIG path=onefe.fireedge_views.dir)}"
  local node vip

  data_runtime_refresh platform="${platform:-}" fe=yes || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)

  _log_std zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" action="list_configs_backups"
  for node in $(inv_list var=CONFIG path=onefe.nodes_order.direct)
  do
    _log_fe
    
    for dir in $(inv_list var=CONFIG path=onefe.config_dirs)
    do
      echo "${dir}"
      $ssh ${RUNTIME[${platform},${node},ip]} "sudo ls -la ${dir}"
    done  # dir
  done  # node

  return 0
}  # fe_configs_backups_list



help_data[fe_data_refresh]="\
  fe_data_refresh            # collect the runtime data of a platform again: zone, FE nodes, datastores,
                             # VNet template, shared group; every command collects it the first time
                             # it touches a platform, this command only refreshes it
    platform=NAME            #   platforms.<site>.<platform>"
# return 0 - done
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
function fe_data_refresh() {
  local arg; for arg in "$@"; do local "${arg}"; done

  # forget what was collected: data_runtime_refresh collects it again
  unset "RUNTIME[${platform:-},zone_id]"
  data_runtime_refresh platform="${platform:-}" || return 1

  return 0
}  # fe_data_refresh



help_data[fe_disk_cleanup]="\
  fe_disk_cleanup            # clean data from /var/tmp after unsuccessful image loading
    platform=NAME            #   platforms.<site>.<platform>
    confirm=yes|NO           #   to suppress interactive confirmation"
# return 0 - done
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            not confirmed
function fe_disk_cleanup() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local node vip

  data_runtime_refresh platform="${platform:-}" fe=yes || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)
  _log_std zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" action="disk_cleanup"
  _stop_without_confirmation confirm=${confirm:-no} && return 1

  for node in $(inv_list var=CONFIG path=onefe.nodes_order.direct)
  do
    _log_fe action="disk_cleanup"
    $ssh ${RUNTIME[${platform},${node},ip]} "
      sudo find /var/tmp -maxdepth 1 -type f \
      -regextype posix-extended \
      -regex '.*/[a-z0-9]{32}' \
      -mmin +120 \
      -print \
      -delete
    "
    $ssh ${RUNTIME[${platform},${node},ip]} "sudo df -h"
  done  # node

  return 0
}  # fe_disk_cleanup



help_data[fe_fireedge_restart]="\
  fe_fireedge_restart        # restart fireedge service on FE
    platform=NAME            #   platforms.<site>.<platform>
    confirm=yes|NO           #   to suppress interactive confirmation"
# return 0 - done
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            not confirmed
function fe_fireedge_restart() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local node vip
  local service="opennebula-fireedge"

  data_runtime_refresh platform="${platform:-}" fe=yes || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)
  _log_std zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" action="fireedge_restart"
  _stop_without_confirmation confirm=${confirm:-no} && return 1

  for node in $(inv_list var=CONFIG path=onefe.nodes_order.reversed)
  do
    _log_fe -n service="${service}" action="stop" status=
    $ssh ${RUNTIME[${platform},${node},ip]} "sudo systemctl stop ${service} &>/dev/null"
    $ssh ${RUNTIME[${platform},${node},ip]} "sudo systemctl is-active ${service}"

    _log_fe -n service="${service}" action="start" status=
    $ssh ${RUNTIME[${platform},${node},ip]} "sudo systemctl start ${service} &>/dev/null"
    $ssh ${RUNTIME[${platform},${node},ip]} "sudo systemctl is-active ${service}"
  done  # node

  return 0
}  # fe_fireedge_restart



help_data[fe_fireedge_views_update]="\
  fe_fireedge_views_update   # distribute updated Fireedge views to FE
    platform=NAME            #   platforms.<site>.<platform>
    src=PATH                 #   directory with custom views
    confirm=yes|NO           #   to suppress interactive confirmation"
# return 0 - done
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            not confirmed
function fe_fireedge_views_update() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local src="${src:-${DIR_FILES}/$(inv_value var=CONFIG path=onefe.fireedge_views.dir)}"
  local node view vip

  data_runtime_refresh platform="${platform:-}" fe=yes || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)

  _log_std zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" action="fireedge_views_update"
  _stop_without_confirmation confirm=${confirm:-no} && return 1
  for node in $(inv_list var=CONFIG path=onefe.nodes_order.direct)
  do
    for view in $(inv_list var=CONFIG path=onefe.fireedge_views.list)
    do
      _log_fe fireedge_view="${view}" source="${src}/${view}"
      rsync -rtI --delete --progress -e "$ssh" --rsync-path "sudo rsync" --exclude='.*' \
        "${src}/${view}" \
        "${RUNTIME[${platform},${node},ip]}:/etc/one/fireedge/sunstone/views/"
    done  # view
    $ssh ${RUNTIME[${platform},${node},ip]} "sudo chown -R root:root /etc/one/fireedge/sunstone/views/"
    $ssh ${RUNTIME[${platform},${node},ip]} "sudo find /etc/one/fireedge/sunstone/views -type f -exec chmod 644 {} +"
  done  # node

  return 0
}  # fe_fireedge_views_update



help_data[fe_os_update]="\
  fe_os_update               # install OS updates
                             # detect combined KVM/FE nodes and put them in maintenance mode before
    platform=NAME            #   platforms.<site>.<platform>
    confirm=yes|NO           #   to suppress interactive confirmation"
# return 0 - done
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh, not confirmed
#            a node not ready after reboot
#            an FE node that is also a KVM host can not enter maintenance
function fe_os_update() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local node service type hosts vip

  data_runtime_refresh platform="${platform:-}" fe=yes || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)
  _log_std zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" action="os_update"
  hosts=" $($ssh ${vip} "sudo -u oneadmin onehost list --no-header -l NAME 2>/dev/null" | tr -d '[:blank:]' | xargs) "  # KVM hosts of the zone
  _stop_without_confirmation confirm=${confirm:-no} && return 1
  $ssh ${vip} "sudo -u oneadmin onezone list 2>/dev/null"
  $ssh ${vip} "sudo -u oneadmin onezone show ${RUNTIME[${platform},id]} 2>/dev/null"

  for node in $(inv_list var=CONFIG path=onefe.nodes_order.reversed)
  do
    type=dedicated
    [[ "${hosts}" == *" ${RUNTIME[${platform},${node},name]} "* ]] && type=mixed   # FE node that is also a KVM host
    _log_fe node_state="$(inv_value var=CONFIG path=onefe.states.node.${RUNTIME[${platform},${node},state]})" node_type="${type}"

    if [[ "${type}" == "mixed" ]]
    then
      if ! host_maintenance_on platform=${platform} hosts="${RUNTIME[${platform},${node},name]}" confirm=${confirm:-no}
      then
        _log_error "ONE FE node combined with KVM can not be entered in maintenance mode"
        return 1
      fi
    fi

    for service in $(inv_list var=CONFIG path=onefe.services_all)
    do
      _log_fe -n service="${service}" action="stop" status=
      $ssh ${RUNTIME[${platform},${node},ip]} "sudo systemctl stop ${service} &>/dev/null"
      $ssh ${RUNTIME[${platform},${node},ip]} "sudo systemctl is-active ${service}"
    done  # service

    _log_fe action="install_updates"
    $ssh ${RUNTIME[${platform},${node},ip]} "sudo apt-get update"
    $ssh ${RUNTIME[${platform},${node},ip]} "sudo DEBIAN_FRONTEND=noninteractive apt-get -y dist-upgrade"

    _log_fe -n action="reboot" status=
    $ssh ${RUNTIME[${platform},${node},ip]} "sudo reboot"
    if _os_service_wait ip="${RUNTIME[${platform},${node},ip]}" service=opennebula progress=yes interval=10 limit=180
    then
      echo "ready"
    else
      return 1
    fi
  done  # node

  _log_std zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})"
  $ssh ${vip} "sudo -u oneadmin onezone list 2>/dev/null"
  $ssh ${vip} "sudo -u oneadmin onezone show ${RUNTIME[${platform},id]} 2>/dev/null"

  data_runtime_refresh platform="${platform:-}" fe=yes
  fe_cfg_ver_get platform="${platform}"

  return 0
}  # fe_os_update



help_data[fe_services_restart]="\
  fe_services_restart        # restart OpenNebula services on FE in right order
    platform=NAME            #   platforms.<site>.<platform>
    confirm=yes|NO           #   to suppress interactive confirmation"
# return 0 - done
#        1 - no user data, wrong or unknown platform, FE not reachable - data_runtime_refresh
#            not confirmed
function fe_services_restart() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local node service vip

  data_runtime_refresh platform="${platform:-}" fe=yes || return 1
  vip=$(inv_value var=INV path=${platform}.fe.vip)
  _log_std zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})" action="services_restart"
  _stop_without_confirmation confirm=${confirm:-no} && return 1
  $ssh ${vip} "sudo -u oneadmin onezone list 2>/dev/null"
  $ssh ${vip} "sudo -u oneadmin onezone show ${RUNTIME[${platform},id]} 2>/dev/null"

  for node in $(inv_list var=CONFIG path=onefe.nodes_order.reversed)
  do
    for service in $(inv_list var=CONFIG path=onefe.services)
    do
      _log_fe -n service="${service}" action="stop" status=
      $ssh ${RUNTIME[${platform},${node},ip]} "sudo systemctl stop ${service} &>/dev/null"
      $ssh ${RUNTIME[${platform},${node},ip]} "sudo systemctl is-active ${service}"
    done  # service

    for service in $(inv_list var=CONFIG path=onefe.services)
    do
      _log_fe -n service="${service}" action="start" status=
      $ssh ${RUNTIME[${platform},${node},ip]} "sudo systemctl start ${service} &>/dev/null"
      $ssh ${RUNTIME[${platform},${node},ip]} "sudo systemctl is-active ${service}"
    done  # service
  done  # node

  _log_std zone_state="$(inv_value var=CONFIG path=onefe.states.zone.${RUNTIME[${platform},state]})"
  $ssh ${vip} "sudo -u oneadmin onezone list 2>/dev/null"
  $ssh ${vip} "sudo -u oneadmin onezone show ${RUNTIME[${platform},id]} 2>/dev/null"

  data_runtime_refresh platform="${platform:-}" fe=yes

  return 0
}  # fe_services_restart
