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
output_dir=$(dirname "$output")
output_name=$(basename "$output")
mkdir -p "$output_dir"
temporary=$(mktemp "$output_dir/.${output_name}.tmp.XXXXXX")
trap 'rm -f "$temporary"' EXIT HUP INT TERM
docker compose exec -T db pg_dump -U albert -d albert --format=custom > "$temporary"
test -s "$temporary"
docker compose exec -T db pg_restore --list < "$temporary" > /dev/null
mv "$temporary" "$output"
trap - EXIT HUP INT TERM
echo "backup written to $output"
