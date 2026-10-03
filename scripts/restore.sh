#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "usage: CONFIRM_ALBERT_RESTORE=yes $0 INPUT.dump" >&2
  exit 2
fi
if [ "${CONFIRM_ALBERT_RESTORE:-}" != "yes" ]; then
  echo "restore replaces Albert database contents; set CONFIRM_ALBERT_RESTORE=yes" >&2
  exit 2
fi
input=$1
if [ ! -f "$input" ] || [ ! -s "$input" ]; then
  echo "backup file is missing or empty: $input" >&2
  exit 2
fi

docker compose exec -T db pg_restore \
  -U albert -d albert --clean --if-exists --no-owner < "$input"
echo "restore completed from $input"

