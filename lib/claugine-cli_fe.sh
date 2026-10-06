#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



claugine_cli_commands+=( "\
  fe_backup                  # backup all data required toi restore FE from scratch
    zones=LIST|ALL           #   zone ids, default ALL
    full=yes|NO              #   include configs or db only"
)
function fe_backup() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n _zone=${zone_data}
  local zones="${zones:-ALL}"
  local full="${full:-no}"
  local zone_id node
  local backup_path backup_base backup_file

  _zone_init || return 1

  backup_path="${DIR_BACKUPS}/$(_zone_vip ${_zone[master]})_$(date '+%H-%M-%S')"

  zones=$(_zones_resolve zones="${zones}") || return 1
  for zone_id in ${zones}
  do
    echo "$(_zone_label ${zone_id}) zone_state=${_zone[${zone_id},state]} action=backup"

    for node in "${FE_NODE_ROLES[@]}"
    do
      backup_base="${zone_id}-${_zone[${zone_id},name]}-${_zone[${zone_id},vip]}_${_zone[${zone_id},${node},id]}-${_zone[${zone_id},${node},name]}-${_zone[${zone_id},${node},ip]}-${_zone[${zone_id},${node},role]}"
      if [[ "${full}" == "yes" ]]
      then
        for dir in "${ONE_FE_BACKUP_DIRS[@]}"
        do
          echo "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} backup_from=${dir} backup_to=${backup_path}"
          backup_file=$(sed 's|^/||; s|/|-|g; s|\.||g' <<< "${dir}")
          backup_file="${backup_base}_${backup_file}.zip"
          $ssh ${_zone[${zone_id},${node},ip]} "sudo zip -rq /tmp/${backup_file} ${dir}"
          rsync --progress -e "$ssh" --rsync-path "sudo rsync" ${_zone[${zone_id},${node},ip]}:/tmp/${backup_file} ${backup_path}/
          $ssh ${_zone[${zone_id},${node},ip]} "sudo rm -vf /tmp/${backup_file}"
        done  # dir
      fi

      if [[ ${node} == "leader" ]]
      then
        echo "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} backup_from=onedb-zone backup_to=${backup_path}"
        backup_file="${backup_base}_onedb-zone"
        $ssh ${_zone[${zone_id},${node},ip]} "sudo onedb backup /tmp/${backup_file}.sql --force"
        $ssh ${_zone[${zone_id},${node},ip]} "sudo zip -q /tmp/${backup_file}.zip /tmp/${backup_file}.sql"
        rsync --progress -e "$ssh" --rsync-path "sudo rsync" ${_zone[${zone_id},${node},ip]}:/tmp/${backup_file}.zip ${backup_path}/
        $ssh ${_zone[${zone_id},${node},ip]} "sudo rm -vf /tmp/${backup_file}.sql"
        $ssh ${_zone[${zone_id},${node},ip]} "sudo rm -vf /tmp/${backup_file}.zip"

        if [[ "${zone_id}" == "${_zone[master]}" && "${_zone[list]}" != "${_zone[master]}" ]]
        then
          echo "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} backup_from=onedb-federated backup_to=${backup_path}"
          backup_file="${backup_base}_onedb-federated"
          $ssh ${_zone[${zone_id},${node},ip]} "sudo onedb backup /tmp/${backup_file}.sql --federated --force"
          $ssh ${_zone[${zone_id},${node},ip]} "sudo zip /tmp/${backup_file}.zip /tmp/${backup_file}.sql"
          rsync --progress -e "$ssh" --rsync-path "sudo rsync" ${_zone[${zone_id},${node},ip]}:/tmp/${backup_file}.zip ${backup_path}/
          $ssh ${_zone[${zone_id},${node},ip]} "sudo rm -vf /tmp/${backup_file}.sql"
          $ssh ${_zone[${zone_id},${node},ip]} "sudo rm -vf /tmp/${backup_file}.zip"
        fi
      fi
    done  # node
  done  # zone_id

  echo "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} backups_age=${ONE_BACKUP_DAYS}d"
  find "${DIR_BACKUPS}" \
    -mindepth 1 \
    -maxdepth 1 \
    -type d \
    -name "*_*" \
    -mtime +${ONE_BACKUP_DAYS} \
    -exec rm -rvf {} +

  du -hd0 ${DIR_BACKUPS}
  du -hd1 ${DIR_BACKUPS}/*
  echo "${backup_path}"
  ls -lah ${backup_path}

  return 0
}  # fe_backup



claugine_cli_commands+=( "\
  fe_configs_backups_cleanup # cleanup all configs backups created during upgrade
    zones=LIST|ALL           #   zone ids, default ALL
    confirm=yes|NO           #   to suppress interactive confirmation"
)
function fe_configs_backups_cleanup() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n _zone=${zone_data}
  local zones="${zones:-ALL}"
  local src="${src:-${DIR_TEMPLATE}/${DIR_FIREEDGE_VIEWS_CUSTOM}}"
  local zone_id node

  _zone_init || return 1
  zones=$(_zones_resolve zones="${zones}") || return 1

  for zone_id in ${zones}
  do
    echo "$(_zone_label ${zone_id}) zone_state=${_zone[${zone_id},state]} action=cleanup_configs_backups"
    stop_without_confirmation confirm=${confirm:-no} && return 1

    for node in "${FE_NODE_ROLES[@]}"
    do
      echo "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} action=cleanup_configs_backups dir=${HOME}/.ssh/id_rsa*"
      $ssh ${_zone[${zone_id},${node},ip]} "sudo find ${HOME}/.ssh \
        -type f \
        -name 'id_rsa*' \
        -exec rm -fv -- {} +"
      $ssh ${_zone[${zone_id},${node},ip]} "sudo find ${HOME}/.ssh \
        -type f \
        -name 'known_hosts*' \
        -exec rm -fv -- {} +"

      echo "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} action=cleanup_configs_backups dir=/etc/one.*"
      $ssh ${_zone[${zone_id},${node},ip]} "sudo rm -fv /etc/one.pre-upgrade"
      $ssh ${_zone[${zone_id},${node},ip]} "sudo find /etc \
        -maxdepth 1 \
        -type d \
        -name 'one.*' \
        -exec rm -rfv -- {} +"

      echo "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} action=cleanup_configs_backups dir=/etc/one/*.original"
      $ssh ${_zone[${zone_id},${node},ip]} "sudo find /etc/one \
        -type f \
        -name '*.original' \
        -exec rm -fv -- {} +"

      echo "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} action=cleanup_configs_backups dir=/var/lib/one/remotes.*"
      $ssh ${_zone[${zone_id},${node},ip]} "sudo rm -fv /var/lib/one/remotes.pre-upgrade"
      $ssh ${_zone[${zone_id},${node},ip]} "sudo find /var/lib/one \
        -maxdepth 1 \
        -type d \
        -name 'remotes.*' \
        -exec rm -rfv -- {} +"

      echo "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} action=cleanup_configs_backups dir=/var/lib/one/backups/config/*"
      $ssh ${_zone[${zone_id},${node},ip]} "sudo find /var/lib/one/backups/config \
        -mindepth 1 \
        -maxdepth 1 \
        -type d \
        -exec rm -rfv -- {} +"

      echo "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} action=cleanup_configs_backups dir=/etc/one/*.original"
      $ssh ${_zone[${zone_id},${node},ip]} "sudo find /var/lib/one/remotes \
        -type f \
        -name '*.dpkg-old' \
        -exec rm -fv -- {} +"

      echo "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} action=cleanup_configs_backups dir=/var/lib/one/.ssh/id_rsa*"
      $ssh ${_zone[${zone_id},${node},ip]} "sudo find /var/lib/one/.ssh \
        -type f \
        -name 'id_rsa*' \
        -exec rm -fv -- {} +"
      $ssh ${_zone[${zone_id},${node},ip]} "sudo find /var/lib/one/.ssh \
        -type f \
        -name 'known_hosts*' \
        -exec rm -fv -- {} +"

      echo "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} action=cleanup_configs_backups dir=/var/lib/one/remotes/etc/*.original"
      $ssh ${_zone[${zone_id},${node},ip]} "sudo find /var/lib/one/remotes/etc \
        -type f \
        -name '*.original' \
        -exec rm -fv -- {} +"

      echo "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} action=cleanup_configs_backups dir=/var/lib/one/*.sql"
      $ssh ${_zone[${zone_id},${node},ip]} "sudo find /var/lib/one \
        -maxdepth 1 \
        -type f \
        -name '*.sql' \
        -exec rm -fv -- {} +"
    done  # node
  done  # zone_id

  return 0
}  # fe_configs_backups_cleanup



claugine_cli_commands+=( "\
  fe_configs_backups_list    # show directories with configs backups created during upgrade
    zones=LIST|ALL           #   zone ids, default ALL"
)
function fe_configs_backups_list() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n _zone=${zone_data}
  local zones="${zones:-ALL}"
  local src="${src:-${DIR_TEMPLATE}/${DIR_FIREEDGE_VIEWS_CUSTOM}}"
  local zone_id node

  _zone_init || return 1
  zones=$(_zones_resolve zones="${zones}") || return 1

  for zone_id in ${zones}
  do
    echo "$(_zone_label ${zone_id}) zone_state=${_zone[${zone_id},state]} action=list_configs_backups"
    for node in "${FE_NODE_ROLES[@]}"
    do
      echo "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]}"
      
      for dir in "${ONE_FE_CONFIG_DIRS[@]}"
      do
        echo "${dir}"
        $ssh ${_zone[${zone_id},${node},ip]} "sudo ls -la ${dir}"
      done  # dir
    done  # node
  done  # zone_id

  return 0
}  # fe_configs_backups_list



claugine_cli_commands+=( "\
  fe_cfg_ver_get             # show current and required version for configuration and db
    zones=LIST|ALL           #   zone ids, default ALL"
)
function fe_cfg_ver_get() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n _zone=${zone_data}
  local zones="${zones:-ALL}"
  local zone_id node cfg

  _zone_init || return 1

  zones=$(_zones_resolve zones="${zones}") || return 1
  for zone_id in ${zones}
  do
    echo "$(_zone_label ${zone_id}) zone_state=${_zone[${zone_id},state]} action=cfg_ver_get"

    for node in "${FE_NODE_ROLES[@]}"
    do
      cfg=$($ssh ${_zone[${zone_id},${node},ip]} "sudo onecfg status")
      _zone[${zone_id},${node},cfg_cur]=$(awk '$1 == "Config:" {print $2}' <<< "${cfg}")
      _zone[${zone_id},${node},cfg_new]=$(awk '$1 == "OpenNebula:" {print $2}' <<< "${cfg}")
      echo "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} cfg_cur=${_zone[${zone_id},${node},cfg_cur]} cfg_new=${_zone[${zone_id},${node},cfg_new]}"

      cfg=$($ssh ${_zone[${zone_id},${node},ip]} "sudo onedb version")
      _zone[${zone_id},${node},db_zone_cur]=$(awk '$1 == "Local:" {print $2}' <<< "${cfg}")
      _zone[${zone_id},${node},db_zone_new]=$(awk '/^Required local version:/ {print $4}' <<< "${cfg}")
      echo "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} db_zone_cur=${_zone[${zone_id},${node},db_zone_cur]} db_zone_new=${_zone[${zone_id},${node},db_zone_new]}"
      _zone[${zone_id},${node},db_fed_cur]=$(awk '$1 == "Shared:" {print $2}' <<< "${cfg}")
      _zone[${zone_id},${node},db_fed_new]=$(awk '/^Required shared version:/ {print $4}' <<< "${cfg}")
      echo "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} db_fed_cur=${_zone[${zone_id},${node},db_fed_cur]} db_fed_new=${_zone[${zone_id},${node},db_fed_new]}"
    done  # node
  done  # zone_id

  return 0
}  # fe_cfg_ver_get



claugine_cli_commands+=( "\
  fe_data_refresh            # load or refresh an installation context: all zones and FE nodes
                             #   the context becomes the current one: zone_data=VAR_NAME
    data=VAR_NAME            #   context variable, default \${zone_data}; created if not declared
    fe=IP                    #   any FE of the installation: load from scratch, required for a new context"
)
function fe_data_refresh {
  local arg; for arg in "$@"; do local "${arg}"; done
  local data=${data:-${zone_data}}
  local -n _zone=${data}
  local debug="${debug:-no}"
  local json zone_id node node_id n
  local name ip state type
  local -a hosts_list
  local timestamp=$(date '+%s')

  # a new context variable is created
  declare -p "${data}" &>/dev/null || declare -gA "${data}=()"
  if [[ "$(declare -p "${data}" 2>/dev/null)" != "declare -A"* ]]
  then
    echo
    echo "!!! ERROR !!! variable \"${data}\" must be an associative array"
    echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
    return 1
  fi

  # get zones list if empty
  if [[ -n "${fe:-}" || -z "${_zone[list]:-}" ]]
  then
    if [[ -z "${fe:-}" ]]
    then
      echo
      echo "!!! ERROR !!! need to define fe to init zone data"
      echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
      return 1
    fi

    _zone=()
    _zone[list]=""
    for zone_id in $(
      $ssh ${fe} "sudo -u oneadmin onezone list --no-header -l ID 2>/dev/null" |
      tr -d '[:blank:]' |
      sort -n
    )
    do
      [[ -n "${_zone[list]}" ]] && _zone[list]+=" "
      _zone[list]+="${zone_id}"
    done  # zone_id

    if [[ -z "${_zone[list]}" ]]
    then
      echo
      echo "!!! ERROR !!! error get zone list from ONE FE ${fe}"
      echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
      return 1
    fi

    _zone[fe]="${fe}"                   # FE the context is loaded and refreshed from
    _zone[master]="${_zone[list]%% *}"  # master zone: the lowest id, 0 in OpenNebula
    _zone[installation]="${data}"
  fi
  [[ -n "${_zone[fe]:-}" ]] || _zone[fe]="${_zone[0,vip]}"  # context loaded by an older version
  [[ -n "${_zone[master]:-}" ]] || _zone[master]="${_zone[list]%% *}"
  _zone[installation]="${data}"
  export zone_data="${data}"

  # get/refresh all zones data using the FE of the context
  for zone_id in ${_zone[list]}
  do
    json=$($ssh ${_zone[fe]} "sudo -u oneadmin onezone show ${zone_id} -j 2>/dev/null")

    IFS=$'\t' read -r name ip state < <(
      jq -r '
        .ZONE
        | [
            .NAME,
            (.TEMPLATE.ENDPOINT
              | sub("^https?://"; "")
              | sub("[:/].*$"; "")),
            .STATE
          ]
        | @tsv
      ' <<< "${json}"
    )
    _zone[${zone_id},name]="${name}"
    _zone[${zone_id},vip]="${ip}"
    _zone[${zone_id},state]="${state}"
  
    mapfile -t hosts_list < <(
      $ssh ${_zone[${zone_id},vip]} "sudo -u oneadmin onehost list --no-header -l NAME 2>/dev/null" |
      tr -d '[:blank:]' |
      sort -n
    )

    echo "installation=${data} $(_zone_label ${zone_id}) zone_state=${_zone[${zone_id},state]}"

    n=1
    for node_id in {0..2}
    do
      IFS=$'\t' read -r name ip state < <(
        jq -r \
        --arg id "${node_id}" '
          .ZONE.SERVER_POOL.SERVER[]
          | select(.ID == $id)
          | [
              .NAME,
              (.ENDPOINT 
                | sub("^https?://"; "")
                | sub("[:/].*$"; "")
              ),
              .STATE
            ]
          | @tsv
        ' <<< "${json}"
      )

      [[ "${state}" == "-" ]] && state=4
      node=${ONE_FE_NODE_STATE[${state}]}
      if (( state == 2 ))
      then
        node+="${n}"
        (( n++ ))
      fi

      _zone[${zone_id},${node},id]="${node_id}"
      _zone[${zone_id},${node},name]="${name}"
      _zone[${zone_id},${node},ip]="${ip}"
      _zone[${zone_id},${node},state]="${state}"
      _zone[${zone_id},${node},role]="${node}"

      if [[ " ${hosts_list[*]} " == *" ${name} "* ]]
      then
        _zone[${zone_id},${node},type]=mixed
      else
        _zone[${zone_id},${node},type]=dedicated
      fi

      echo "zone_id=${zone_id} node_role=${node} node_id=${_zone[${zone_id},${node},id]} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} node_state=${ONE_FE_NODE_STATE[${_zone[${zone_id},${node},state]}]} node_type=${_zone[${zone_id},${node},type]}"
    done  # node_id
  done  # zone_id

  echo -n "installation=${data} fe=${_zone[fe]} master_zone=${_zone[master]} zones=[${_zone[list]}] "
  if [[ -z "${_zone[timestamp]:-}" ]]
  then
    echo "timestamp=${timestamp}"
  else
    echo -n "timestamp_old=${_zone[timestamp]} timestamp_new=${timestamp} timestamp_delta="
    n=$(( timestamp - _zone[timestamp] ))
    printf '%02d:%02d:%02d\n' "$(( n / 3600 ))" "$(( n % 3600 / 60 ))" "$(( n % 60 ))"
  fi
  _zone[timestamp]="${timestamp}"

  return 0
}  # fe_data_refresh



claugine_cli_commands+=( "\
  fe_disk_cleanup            # clean data from /var/tmp after unsuccessful image loading
    zones=LIST|ALL           #   zone ids, default ALL
    confirm=yes|NO           #   to suppress interactive confirmation"
)
function fe_disk_cleanup() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n _zone=${zone_data}
  local zones="${zones:-ALL}"
  local zone_id node

  _zone_init || return 1

  zones=$(_zones_resolve zones="${zones}") || return 1
  for zone_id in ${zones}
  do
    echo "$(_zone_label ${zone_id}) zone_state=${_zone[${zone_id},state]} action=disk_cleanup"
    stop_without_confirmation confirm=${confirm:-no} && return 1

    for node in "${FE_NODE_ROLES[@]}"
    do
      echo "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} action=disk_cleanup"
      $ssh ${_zone[${zone_id},${node},ip]} "
        sudo find /var/tmp -maxdepth 1 -type f \
        -regextype posix-extended \
        -regex '.*/[a-z0-9]{32}' \
        -mmin +120 \
        -print \
        -delete
      "
      $ssh ${_zone[${zone_id},${node},ip]} "sudo df -h"
    done  # node
  done  # zone_id

  return 0
}  # fe_disk_cleanup



claugine_cli_commands+=( "\
  fe_fireedge_restart        # restart fireedge service on FE
    zones=LIST|ALL           #   zone ids, default ALL
    confirm=yes|NO           #   to suppress interactive confirmation"
)
function fe_fireedge_restart() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n _zone=${zone_data}
  local zones="${zones:-ALL}"
  local zone_id node
  local service="opennebula-fireedge"

  _zone_init || return 1

  zones=$(_zones_resolve zones="${zones}") || return 1
  for zone_id in ${zones}
  do
    echo "$(_zone_label ${zone_id}) zone_state=${_zone[${zone_id},state]} action=fireedge_restart"
    stop_without_confirmation confirm=${confirm:-no} && return 1

    for node in "${FE_NODE_ROLES_REVERSED[@]}"
    do
      echo -n "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} service=${service} action=stop status="
      $ssh ${_zone[${zone_id},${node},ip]} "sudo systemctl stop ${service} &>/dev/null"
      $ssh ${_zone[${zone_id},${node},ip]} "sudo systemctl is-active ${service}"

      echo -n "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} service=${service} action=start status="
      $ssh ${_zone[${zone_id},${node},ip]} "sudo systemctl start ${service} &>/dev/null"
      $ssh ${_zone[${zone_id},${node},ip]} "sudo systemctl is-active ${service}"
    done  # node
  done  # zone_id

  return 0
}  # fe_fireedge_restart



claugine_cli_commands+=( "\
  fe_fireedge_views_update   # distribute updated Fireedge views to FE
    zones=LIST|ALL           #   zone ids, default ALL
    src=PATH                 #   directory with custom views
    confirm=yes|NO           #   to suppress interactive confirmation"
)
function fe_fireedge_views_update() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n _zone=${zone_data}
  local zones="${zones:-ALL}"
  local src="${src:-${DIR_TEMPLATE}/${DIR_FIREEDGE_VIEWS_CUSTOM}}"
  local zone_id node view

  _zone_init || return 1
  zones=$(_zones_resolve zones="${zones}") || return 1

  for zone_id in ${zones}
  do
    echo "$(_zone_label ${zone_id}) zone_state=${_zone[${zone_id},state]} action=fireedge_views_update"
    stop_without_confirmation confirm=${confirm:-no} && return 1
    for node in "${FE_NODE_ROLES[@]}"
    do
      for view in "${FIREEDGE_VIEWS_LIST[@]}"
      do
        echo "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} fireedge_view=${view} source=${src}/${view}"
        rsync -rtI --delete --progress -e "$ssh" --rsync-path "sudo rsync" --exclude='.*' \
          "${src}/${view}" \
          "${_zone[${zone_id},${node},ip]}:/etc/one/fireedge/sunstone/views/"
      done  # view
      $ssh ${_zone[${zone_id},${node},ip]} "sudo chown -R root:root /etc/one/fireedge/sunstone/views/"
      $ssh ${_zone[${zone_id},${node},ip]} "sudo find /etc/one/fireedge/sunstone/views -type f -exec chmod 644 {} +"
    done  # node
  done  # zone_id

  return 0
}  # fe_fireedge_views_update



claugine_cli_commands+=( "\
  fe_os_update               # install OS updates
                             # detect combined KVM/FE nodes and put them in maintenance mode before
    zones=LIST|ALL           #   zone ids, default ALL
    confirm=yes|NO           #   to suppress interactive confirmation"
)
function fe_os_update {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n _zone=${zone_data}
  local zones="${zones:-ALL}"
  local zone_id node service

  _zone_init || return 1

  zones=$(_zones_resolve zones="${zones}") || return 1
  for zone_id in ${zones}
  do
    echo "$(_zone_label ${zone_id}) zone_state=${_zone[${zone_id},state]} action=os_update"
    stop_without_confirmation confirm=${confirm:-no} && return 1
    $ssh ${_zone[${zone_id},vip]} "sudo -u oneadmin onezone list 2>/dev/null"
    $ssh ${_zone[${zone_id},vip]} "sudo -u oneadmin onezone show ${zone_id} 2>/dev/null"

    for node in "${FE_NODE_ROLES_REVERSED[@]}"
    do
      echo "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} node_state=${ONE_FE_NODE_STATE[${_zone[${zone_id},${node},state]}]} node_type=${_zone[${zone_id},${node},type]}"

      if [[ "${_zone[${zone_id},${node},type]}" == "mixed" ]]
      then
        if ! host_maintenance_on zone_id=${zone_id} hosts="${_zone[${zone_id},${node},name]}" confirm=${confirm:-no}
        then
          echo
          echo "!!! ERROR !!! ONE FE node combined with KVM can not be entered in maintenance mode"
          echo "!!! TRACE !!! ${FUNCNAME[0]}: $*"
          return 2
        fi
      fi

      for service in "${ONE_FE_SERVICES_ALL[@]}"
      do
        echo -n "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} service=${service} action=stop status="
        $ssh ${_zone[${zone_id},${node},ip]} "sudo systemctl stop ${service} &>/dev/null"
        $ssh ${_zone[${zone_id},${node},ip]} "sudo systemctl is-active ${service}"
      done  # service

      echo "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} action=install_updates"
      $ssh ${_zone[${zone_id},${node},ip]} "sudo apt-get update"
      $ssh ${_zone[${zone_id},${node},ip]} "sudo DEBIAN_FRONTEND=noninteractive apt-get -y dist-upgrade"

      echo -n "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} action=reboot status="
      $ssh ${_zone[${zone_id},${node},ip]} "sudo reboot"
      if os_service_wait ip="${_zone[${zone_id},${node},ip]}" service=opennebula progress=yes interval=10 limit=180
      then
        echo "ready"
      else
        return 1
      fi
    done  # node

    echo "$(_zone_label ${zone_id}) zone_state=${_zone[${zone_id},state]}"
    $ssh ${_zone[${zone_id},vip]} "sudo -u oneadmin onezone list 2>/dev/null"
    $ssh ${_zone[${zone_id},vip]} "sudo -u oneadmin onezone show ${zone_id} 2>/dev/null"
  done  # zone_id

  fe_data_refresh
  fe_cfg_ver_get zones=ALL

  return 0
}  # fe_os_update



claugine_cli_commands+=( "\
  fe_services_restart        # restart OpenNebula services on FE in right order
    zones=LIST|ALL           #   zone ids, default ALL
    confirm=yes|NO           #   to suppress interactive confirmation"
)
function fe_services_restart() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n _zone=${zone_data}
  local zones="${zones:-ALL}"
  local zone_id node service

  _zone_init || return 1

  zones=$(_zones_resolve zones="${zones}") || return 1
  for zone_id in ${zones}
  do
    echo "$(_zone_label ${zone_id}) zone_state=${_zone[${zone_id},state]} action=services_restart"
    stop_without_confirmation confirm=${confirm:-no} && return 1
    $ssh ${_zone[${zone_id},vip]} "sudo -u oneadmin onezone list 2>/dev/null"
    $ssh ${_zone[${zone_id},vip]} "sudo -u oneadmin onezone show ${zone_id} 2>/dev/null"

    for node in "${FE_NODE_ROLES_REVERSED[@]}"
    do
      for service in "${FE_SERVICES[@]}"
      do
        echo -n "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} service=${service} action=stop status="
        $ssh ${_zone[${zone_id},${node},ip]} "sudo systemctl stop ${service} &>/dev/null"
        $ssh ${_zone[${zone_id},${node},ip]} "sudo systemctl is-active ${service}"
      done  # service

      for service in "${FE_SERVICES[@]}"
      do
        echo -n "zone_id=${zone_id} node_role=${node} node_name=${_zone[${zone_id},${node},name]} node_ip=${_zone[${zone_id},${node},ip]} service=${service} action=start status="
        $ssh ${_zone[${zone_id},${node},ip]} "sudo systemctl start ${service} &>/dev/null"
        $ssh ${_zone[${zone_id},${node},ip]} "sudo systemctl is-active ${service}"
      done  # service
    done  # node

    echo "$(_zone_label ${zone_id}) zone_state=${_zone[${zone_id},state]}"
    $ssh ${_zone[${zone_id},vip]} "sudo -u oneadmin onezone list 2>/dev/null"
    $ssh ${_zone[${zone_id},vip]} "sudo -u oneadmin onezone show ${zone_id} 2>/dev/null"
  done  # zone_id

  fe_data_refresh

  return 0
}  # fe_services_restart
