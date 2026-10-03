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
export ALBERT_E2E_KEY="$key" ALBERT_CONSOLE_API_KEY="$key" ALBERT_API_URL=http://127.0.0.1:8080
.venv/bin/uvicorn albert.api:app --port 8080 --log-level warning &
api=$!
.venv/bin/albert-worker &
worker=$!
trap 'kill $api $worker 2>/dev/null || true; rm -f "$db.sqlite3"' EXIT INT TERM
for _ in $(seq 1 40); do
  if curl -fs http://127.0.0.1:8080/v1/health/ready >/dev/null 2>&1; then break; fi
  sleep 0.25
done
cd console && npx playwright test "$@"
