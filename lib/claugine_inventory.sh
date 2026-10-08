#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



# INDEX
#   inv_keys        the keys of an object, one per line
#   inv_list        the items of a list, one per line
#   inv_list_field  one field of each object in a list, one per line
#   inv_list_find   the object in a list whose field is value, one-line JSON
#   inv_value       a value of a loaded document; an object or a list - one-line JSON
#
# arguments of the getters:
#   var=NAME                 # the document: CONFIG, TEMPLATES, INV, SECRETS
#   path=A.B                 # keys separated by "."
#   missing - nothing; an object or a list printed as a value - one-line JSON



claugine_help+=( "\
  inv_keys                   # the keys of an object, one per line
    var=NAME                 #   CONFIG, TEMPLATES, INV, SECRETS
    path=A.B                 #   keys separated by ".""
)
# return 0 - always; missing path - nothing printed
function inv_keys() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n inv_data=${var}

  jq -rc \
    --arg path "${path}" '
      getpath($path / ".") // empty
      | objects
      | keys_unsorted[]
    ' <<< "${inv_data:-}"
}  # inv_keys



claugine_help+=( "\
  inv_list                   # the items of a list, one per line
    var=NAME                 #   CONFIG, TEMPLATES, INV, SECRETS
    path=A.B                 #   keys separated by ".""
)
# return 0 - always; missing path - nothing printed
function inv_list() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n inv_data=${var}

  jq -rc \
    --arg path "${path}" '
      getpath($path / ".")[]?
    ' <<< "${inv_data:-}"
}  # inv_list



claugine_help+=( "\
  inv_list_field             # one field of each object in a list, one per line
    var=NAME                 #   CONFIG, TEMPLATES, INV, SECRETS
    path=A.B                 #   keys separated by "."
    field=NAME               #   field of the objects; inv_list_find - default: name"
)
# return 0 - always; missing path - nothing printed
function inv_list_field() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n inv_data=${var}

  jq -rc \
    --arg path "${path}" \
    --arg field "${field}" '
      getpath($path / ".")[]? | .[$field] // empty
    ' <<< "${inv_data:-}"
}  # inv_list_field



claugine_help+=( "\
  inv_list_find              # the object in a list whose field is value, one-line JSON
    var=NAME                 #   CONFIG, TEMPLATES, INV, SECRETS
    path=A.B                 #   keys separated by "."
    field=NAME               #   field of the objects; inv_list_find - default: name
    value=STRING             #   value of the field"
)
# return 0 - always; missing path - nothing printed
function inv_list_find() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n inv_data=${var}
  local field="${field:-name}"

  jq -rc \
    --arg path "${path}" \
    --arg field "${field}" \
    --arg value "${value}" '
      getpath($path / ".")[]? | select((.[$field] | tostring) == $value)
    ' <<< "${inv_data:-}"
}  # inv_list_find



claugine_help+=( "\
  inv_value                  # a value of a loaded document; an object or a list - one-line JSON
    var=NAME                 #   CONFIG, TEMPLATES, INV, SECRETS
    path=A.B                 #   keys separated by ".""
)
# return 0 - always; missing path - nothing printed
function inv_value() {
  local arg; for arg in "$@"; do local "${arg}"; done
  local -n inv_data=${var}

  jq -rc \
    --arg path "${path}" '
      getpath($path / ".") // empty
    ' <<< "${inv_data:-}"
}  # inv_value
