#!/bin/bash
set -u

# One-time: write the tenant quota attributes from a tenants CSV to the OpenNebula groups
#   temporary, not part of claugine - no claugine modules or data used
#   run on an admin station where the OpenNebula CLI already points to the FE of zone 0
#   (the federation primary: groups are federation-wide, one update covers zk1 - zone 0 and at1 - zone 100)
#
#   csv=PATH                # the tenants CSV: Тенант, Site, Clusters, CPU, RAM, GPU, Storage, Images, Files,
#                           #   Backups, Mbps, Inet pfx, Corp pfx, L2 VLAN, Subnet, Start, ...
#                           #   fields in double quotes may hold commas, no quotes inside a field
#   apply=NO|yes            # no - print the attributes of every group, nothing changed; yes - onegroup update
#
#   group tenant-<Тенант>: the whole group template is replaced - FIREEDGE first, then the quota attributes
#     FIREEDGE               - the FireEdge views of the group: cloud, group admin - groupadmin
#     quota attributes as in docs/data/quota.md:
#     QUOTA_<SITE>_<CLUSTER> - per row: CPU, RAM_GB, GPU, DISK_GB (Storage), STATUS (ok), DATE_CREATED (Start)
#     QUOTA_<SITE>           - per tenant and site: IMAGES_GB, FILES_GB, BACKUPS_GB - sums of its rows;
#                              connectivity - the same in every row of the site, or the tenant is skipped:
#                              Corp pfx         - CONNECTION_TYPE corpnet, CORPNET_PREFIXES, CORPNET_SUBNETS
#                              Mbps or Inet pfx - CONNECTION_TYPE inet, INET_MBPS, INET_MAIN_PREFIX, INET_MAIN_SUBNET,
#                                                 the next subnets - INET_PUBLIC_PREFIXES, INET_PUBLIC_SUBNETS
#                              otherwise        - CONNECTION_TYPE none
#                              L2 VLAN          - L2_VLANS
#     Subnet: addresses like 1.2.3.0/29, separated by spaces, written as in the CSV; other text (none, ...) is ignored;
#       SUBNETS_FIX below replaces the Subnet of a tenant on a site
#     empty values are omitted, End and Статус are not written
#
# usage:
#   once-tenants-quota.sh csv=<data>/<set>/runtime/tenants.csv              # dry run
#   once-tenants-quota.sh csv=<data>/<set>/runtime/tenants.csv apply=yes

csv=""
apply="no"
for arg in "$@"
do
  case "${arg}" in
    csv=*|apply=*) declare "${arg}" ;;
  esac
done  # arg

if [[ ! -f "${csv}" ]]
then
  echo "!!! ERROR !!! csv \"${csv}\" is not exists" >&2
  exit 1
fi
apply="${apply,,}"
if [[ ! "${apply}" =~ ^(yes|no)$ ]]
then
  echo "once-tenants-quota: apply must be yes or no: apply=\"${apply}\""
  exit 1
fi
if ! command -v onegroup &>/dev/null
then
  echo "once-tenants-quota: onegroup not found"
  exit 1
fi

# corrections of the CSV: the Subnet of a tenant on a site - [<Тенант>|<Site>]="subnets separated by spaces"
declare -A SUBNETS_FIX=(
  ["corp-suyndik|at1"]="10.61.104.128/28"
)


# split one CSV line into the array fields: "a, b",c,,"d" -> [a, b] [c] [] [d]
function csv_split() {
  local line="$1"
  fields=()
  while :
  do
    if [[ "${line}" =~ ^\"([^\"]*)\"(,|$) ]] || [[ "${line}" =~ ^([^,]*)(,|$) ]]
    then
      fields+=( "${BASH_REMATCH[1]}" )
      line="${line:${#BASH_REMATCH[0]}}"
      [[ -z "${BASH_REMATCH[2]}" ]] && break
    else
      return 1
    fi
  done  # field
  return 0
}  # csv_split

# one attribute line of a vector, nothing for an empty value
function attr() {
  [[ -n "$2" ]] && printf '  %s = "%s",\n' "$1" "$2"
  return 0
}  # attr

# a vector attribute from its attribute lines: the last comma removed
function vector() {
  local body="${2%,$'\n'}"
  printf '%s = [\n%s\n]\n' "$1" "${body%,}"
}  # vector


# ---- read the CSV
declare -A col=()           # column name -> index
declare -a tenants=()       # tenants in the order of the CSV
declare -A sites=()         # tenant -> its sites, in order
declare -A sum_images=() sum_files=() sum_backups=() con=() con_error=() clusters=()
fields=()
n=0
while IFS= read -r line || [[ -n "${line}" ]]
do
  line="${line%$'\r'}"
  (( n++ == 0 )) && line="${line#$'\xEF\xBB\xBF'}"
  if ! csv_split "${line}"
  then
    echo "!!! ERROR !!! line ${n}: can not be parsed" >&2
    exit 1
  fi
  if (( n == 1 ))
  then
    for i in "${!fields[@]}"; do [[ -n "${fields[i]}" ]] && col[${fields[i]}]=${i}; done
    for c in "Тенант" "Site" "Clusters" "CPU" "RAM" "GPU" "Storage" "Images" "Files" "Backups" \
             "Mbps" "Inet pfx" "Corp pfx" "L2 VLAN" "Subnet" "Start"
    do
      if [[ -z "${col[${c}]:-}" ]]
      then
        echo "!!! ERROR !!! column \"${c}\" not found in ${csv}" >&2
        exit 1
      fi
    done  # c
    continue
  fi

  tenant="${fields[${col[Тенант]}]:-}"
  [[ -z "${tenant}" ]] && continue
  site="${fields[${col[Site]}]:-}"
  key="${tenant}|${site}"
  [[ " ${tenants[*]} " == *" ${tenant} "* ]] || tenants+=( "${tenant}" )
  [[ " ${sites[${tenant}]:-} " == *" ${site} "* ]] || sites[${tenant}]+="${sites[${tenant}]:+ }${site}"

  # sums of the site
  for c in Images Files Backups
  do
    value="${fields[${col[${c}]}]:-}"
    value="${value%%.*}"
    [[ -z "${value}" ]] && continue
    case "${c}" in
      Images)  sum_images[${key}]=$(( ${sum_images[${key}]:-0} + value )) ;;
      Files)   sum_files[${key}]=$(( ${sum_files[${key}]:-0} + value )) ;;
      Backups) sum_backups[${key}]=$(( ${sum_backups[${key}]:-0} + value )) ;;
    esac
  done  # c

  # connectivity of the site: the same in every row that has it
  value="${fields[${col[Mbps]}]:-}|${fields[${col[Inet pfx]}]:-}|${fields[${col[Corp pfx]}]:-}|${fields[${col[L2 VLAN]}]:-}|${fields[${col[Subnet]}]:-}"
  if [[ "${value}" != "||||" ]]
  then
    [[ -n "${con[${key}]:-}" && "${con[${key}]}" != "${value}" ]] && con_error[${key}]=1
    con[${key}]="${value}"
  fi

  # the cluster of the row
  cluster="${fields[${col[Clusters]}]:-}"
  clusters[${key}]+="$(vector "QUOTA_${site^^}_${cluster^^}" "$(
    attr CPU          "${fields[${col[CPU]}]:-}"
    attr RAM_GB       "${fields[${col[RAM]}]:-}"
    attr GPU          "${fields[${col[GPU]}]:-}"
    attr DISK_GB      "${fields[${col[Storage]}]:-}"
    attr STATUS       "ok"
    attr DATE_CREATED "${fields[${col[Start]}]:-}"
  )")"$'\n'
done < "${csv}"


# ---- the template of each tenant, then onegroup update
result=0
skipped=0
tmp=$(mktemp)
for tenant in "${tenants[@]}"
do
  # the FireEdge views of the group first
  tpl="$(vector FIREEDGE "$(
    attr DEFAULT_VIEW             "cloud"
    attr GROUP_ADMIN_DEFAULT_VIEW "groupadmin"
    attr GROUP_ADMIN_VIEWS        "groupadmin"
    attr VIEWS                    "cloud"
  )")"$'\n'
  error=""
  for site in ${sites[${tenant}]}
  do
    key="${tenant}|${site}"
    if [[ -n "${con_error[${key}]:-}" ]]
    then
      error="site ${site}: different connectivity in its rows"
      break
    fi
    IFS='|' read -r mbps inet_pfx corp_pfx vlans subnet <<< "${con[${key}]:-||||}"
    subnet="${SUBNETS_FIX[${key}]:-${subnet}}"   # a correction of the CSV

    nets=()
    for net in ${subnet}
    do
      [[ "${net}" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]] || continue
      nets+=( "${net}" )   # as in the CSV
    done  # net

    lines="$(
      attr IMAGES_GB  "${sum_images[${key}]:-}"
      attr FILES_GB   "${sum_files[${key}]:-}"
      attr BACKUPS_GB "${sum_backups[${key}]:-}"
    )"$'\n'
    if [[ -n "${corp_pfx}" ]]
    then
      addrs=""
      for net in "${nets[@]}"; do addrs+="${addrs:+ }${net%/*}"; done
      lines+="$(
        attr CONNECTION_TYPE  "corpnet"
        attr CORPNET_PREFIXES "${corp_pfx}"
        attr CORPNET_SUBNETS  "${addrs}"
      )"$'\n'
    elif [[ -n "${mbps}" || -n "${inet_pfx}" ]]
    then
      main="${nets[0]:-}"
      prefixes=""
      addrs=""
      for net in "${nets[@]:1}"; do prefixes+="${prefixes:+ }${net#*/}"; addrs+="${addrs:+ }${net%/*}"; done
      if [[ -n "${main}" && -n "${inet_pfx}" && "${main#*/}" != "${inet_pfx}" ]]
      then
        echo "warning: tenant ${tenant} site ${site}: Inet pfx ${inet_pfx}, the first subnet /${main#*/}" >&2
      fi
      lines+="$(
        attr CONNECTION_TYPE      "inet"
        attr INET_MBPS            "${mbps}"
        attr INET_MAIN_PREFIX     "${inet_pfx}"
        attr INET_MAIN_SUBNET     "${main%/*}"
        attr INET_PUBLIC_PREFIXES "${prefixes}"
        attr INET_PUBLIC_SUBNETS  "${addrs}"
      )"$'\n'
    else
      lines+="$(attr CONNECTION_TYPE "none")"$'\n'
    fi
    lines+="$(attr L2_VLANS "${vlans}")"
    tpl+="$(vector "QUOTA_${site^^}" "$(grep -v '^$' <<< "${lines}")")"$'\n'
    tpl+="${clusters[${key}]}"
  done  # site

  group="tenant-${tenant}"
  echo "----------------------------------------"
  if [[ -n "${error}" ]]
  then
    echo "!!! ERROR !!! group=${group}: ${error} - skipped"
    skipped=$(( skipped + 1 ))
    result=1
    continue
  fi
  echo "group=${group} apply=${apply}"
  printf '%s' "${tpl}"

  if ! onegroup show "${group}" &>/dev/null
  then
    echo "!!! ERROR !!! group ${group} not found"
    result=1
    continue
  fi
  [[ "${apply}" == "yes" ]] || continue
  printf '%s' "${tpl}" > "${tmp}"
  # the whole template replaced: no --append
  if onegroup update "${group}" "${tmp}"
  then
    echo "group=${group} result=updated"
  else
    echo "!!! ERROR !!! group ${group} not updated"
    result=1
  fi
done  # tenant
rm -f "${tmp}"

echo "----------------------------------------"
echo "tenants=${#tenants[@]} skipped=${skipped} apply=${apply}"
exit ${result}
