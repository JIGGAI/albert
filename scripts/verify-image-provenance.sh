#!/bin/sh
# Validate the machine-readable evidence consumed by guarded deployment.
set -eu

if [ "$#" -ne 3 ]; then
  echo "usage: $0 PROVENANCE_JSON EXPECTED_REVISION EXPECTED_TREE" >&2
  exit 2
fi

provenance=$1
expected_revision=$2
expected_tree=$3

jq -e \
  --arg revision "$expected_revision" \
  --arg tree "$expected_tree" '
    .schema == 1 and
    .image == "ghcr.io/jiggai/albert" and
    (.mode == "promoted" or .mode == "built") and
    .final_revision == $revision and
    .tree == $tree and
    (.digest | test("^sha256:[0-9a-f]{64}$")) and
    (.ci_run | test("^https://github.com/JIGGAI/albert/actions/runs/[0-9]+$")) and
    (if .mode == "promoted" then
       (.candidate | test("^ghcr.io/jiggai/albert:candidate-tree-[0-9a-f]{40}$"))
     else true end)
  ' "$provenance" >/dev/null
