#!/bin/bash
set -u
echo "module=${BASH_SOURCE[0]} <<--loaded-- from=${BASH_SOURCE[1]}"



declare -gr TIMEZONE_DEFAULT="Asia/Almaty"
declare -gr IMAGE_WAIT_LIMIT=1800
