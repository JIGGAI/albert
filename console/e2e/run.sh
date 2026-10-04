#!/bin/sh
# Start a SQLite-backed Albert API and worker, bootstrap an operator key, run Playwright.
set -eu
root=$(cd "$(dirname "$0")/../.." && pwd)
db=$(mktemp -t albert-e2e.XXXXXX)
rm -f "$db"
export ALBERT_DATABASE_URL="sqlite:///$db.sqlite3"
export ALBERT_API_KEY_PEPPER=e2e-pepper-that-is-longer-than-thirty-two-characters
export ALBERT_EMBEDDING_PROVIDER=hashing ALBERT_EMBEDDING_DIMENSIONS=64
export ALBERT_WORKER_POLL_SECONDS=0.2
cd "$root"
.venv/bin/alembic upgrade head >/dev/null
key=$(.venv/bin/albert-admin bootstrap --organization E2E --workspace Default | tail -1)
# A second tenant with nothing in it, for the empty-state spec.
.venv/bin/albert-admin bootstrap --organization "Empty Org" --workspace Default >/dev/null
# Dedicated ports so the suite never collides with, or silently reuses, an Albert
# deployment already running on this machine (8080/8082 are its defaults).
API_PORT="${ALBERT_E2E_API_PORT:-18080}"
export ALBERT_E2E_CONSOLE_PORT="${ALBERT_E2E_CONSOLE_PORT:-18082}"
export ALBERT_E2E_KEY="$key" ALBERT_CONSOLE_API_KEY="$key" ALBERT_API_URL="http://127.0.0.1:$API_PORT"
.venv/bin/uvicorn albert.api:app --port "$API_PORT" --log-level warning &
api=$!
.venv/bin/albert-worker &
worker=$!
trap 'kill $api $worker 2>/dev/null || true; wait $api $worker 2>/dev/null || true; rm -f "$db.sqlite3"' EXIT INT TERM
for _ in $(seq 1 40); do
  if curl -fs "$ALBERT_API_URL/v1/health/ready" >/dev/null 2>&1; then break; fi
  sleep 0.25
done
if ! curl -fs "$ALBERT_API_URL/v1/health/ready" >/dev/null 2>&1; then
  echo "e2e API did not start on $ALBERT_API_URL" >&2
  exit 1
fi
cd console && npx playwright test "$@"
