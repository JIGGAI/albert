#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "usage: $0 OUTPUT.dump" >&2
  exit 2
fi

output=$1
case "$output" in
  ""|"/"|"."|"..")
    echo "refusing unsafe output path" >&2
    exit 2
    ;;
esac

umask 077
mkdir -p "$(dirname "$output")"
docker compose exec -T db pg_dump -U albert -d albert --format=custom > "$output"
test -s "$output"
echo "backup written to $output"

