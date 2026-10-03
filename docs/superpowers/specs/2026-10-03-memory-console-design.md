# Memory console: flight recorder, live feed, 3D explorer

Status: design, awaiting review
Date: 2026-10-03
Scope: slice one of the Albert memory console

## Intent

Operators need to see how Albert's memory behaves, not infer it from logs: what
every request did step by step, why a memory ranked where it did, how the
entity graph grows and expires over time, and what the worker is doing right
now. Today Albert records a coarse audit event per operation and nothing a
person could browse.

Slice one delivers three things on real data: a **flight recorder** that
captures every request and job as a trace, a **live feed** of those traces,
and a **3D explorer** that renders the memory graph and lights up any trace's
retrieval path through it. Operations dashboards, tenant self-serve, and
per-operator login are later slices built on the same recorder.

### Decisions already made

- Audience: operators only, authenticated with an operator API key.
- Home: the Albert repository, shipped as a compose service with every install.
- Order: recorder, then live feed, then explorer, all on real traces.
- Stack: Next.js and React with three.js (`react-force-graph-3d`).

### Success criteria

- Every `/v1/*` request and every worker job leaves a trace with ordered spans
  and, for retrieval, the ranked candidates per backend and the fusion math.
- An operator can open any trace within seconds of it happening, replay its
  steps, and see its candidates highlighted in the 3D graph.
- Recording adds no more than 5 % latency to `/v1/search` on the SQLite test
  stack, enforced by a benchmark test in CI.
- Traces never contain memory content.

## Architecture

```text
API process (per replica)              Worker process
  request --> handlers --> response       job --> enrich_*
     |  Recorder (contextvar) spans          |  Recorder spans
     v                                        v
  TraceWriter queue  -------> traces table <-------  TraceWriter queue
                                  |  pg_notify('albert_traces')
                                  v
                    GET /v1/console/stream  (SSE, one LISTEN per replica)
                    GET /v1/console/traces, /traces/{id}, /graph, /overview
                                  ^
                                  |  bearer: operator key held server-side
                    albert-console (Next.js, separate container, :8082)
                      Live  |  Replay  |  Explorer
```

## Component 1: recorder

### Data model

One new table, `traces`:

| column | type | notes |
|---|---|---|
| id | uuid | |
| kind | string | `request` or `job` |
| name | string | `POST /v1/search`, `enrich_episode`, ... |
| organization_id | uuid, nullable | null for unauthenticated requests |
| principal_id | uuid, nullable | |
| status | string | `ok`, `error`, `degraded` |
| http_status | int, nullable | |
| started_at | timestamptz | indexed with organization_id |
| duration_ms | float | |
| summary | json | bounded; see below |
| spans | json | ordered list; see below |

Spans are a JSON array on the trace row rather than a child table. A trace is
written once and read whole; one insert per request is the cheapest write path
and the replay UI always needs every span.

Each span: `name`, `seq`, `started_offset_ms`, `duration_ms`, `status`,
`detail`. Span names in this slice: `auth`, `resolve_scope`, `lexical`,
`vector`, `graph`, `fuse`, `respond` for requests; `claim`, `classify`,
`chunk`, `embed`, `write_edges`, `complete` for jobs.

`detail` is capped at 8 KB per span and holds ids and numbers only:

- retrieval spans: `[{"id": "<memory or relationship id>", "kind": "memory"|"relationship", "score": 0.73, "rank": 1}]`, at most 50 entries, plus `candidates_total` and the applied filters (workspace, sensitivities, memory types, temporal_as_of).
- `fuse`: per final hit, `{"id", "rank", "rrf": {"lexical": 0.016, "vector": 0.0}, "backends": [...]}`.
- `vector`: additionally `embedding_model`, `min_similarity`, `iterative_scan` (bool).
- job spans: counts (chunks written, edges written/closed), `embedding_model`, classifier provider, truncation flag.

`summary` holds: request query text (searches, graph queries and context
assembly only; capped at 500 characters), hit count, degraded reasons, and the
ids of resources created, read, updated or deleted. **No memory subject or
content is ever stored in a trace.** The console fetches content on demand
through the authorized API when an operator clicks a node.

### Capture

`albert/recorder.py` provides:

- `Recorder`: collects spans, computes offsets, enforces caps, builds the row.
- `current_recorder()` via a `ContextVar`. FastAPI runs sync handlers in a
  threadpool with the request context copied, so code in `search.py` and
  `services.py` can call `span("vector")` without parameters being threaded
  through.
- `@contextmanager span(name, **detail)`: records duration and status;
  exceptions mark the span `error` and re-raise.
- When no recorder is active (unit tests, scripts) spans are no-ops.

A Starlette middleware creates the recorder per request, fills `auth` and
status from the response, and hands the finished trace to the writer in a
`finally`, so error responses are recorded. The worker does the same around
`process_job`.

Instrumentation points: `authenticate` (`auth`), `resolve_workspace`
(`resolve_scope`), the three retrieval functions and the fusion loop in
`search.py`, `index_memory_chunks` (`chunk`, `embed`), `classify` call sites
(`classify`), relationship writes in `enrich_*` (`write_edges`).

### Writer

`TraceWriter` is a bounded in-process queue (default 1,000) drained by one
background thread that inserts in batches on its own session, with
`pg_notify('albert_traces', <trace id>)` in the same transaction on
PostgreSQL. The request thread only enqueues. If the queue is full the trace
is dropped and a counter incremented; recording never blocks or fails a
request. On shutdown the writer flushes.

Settings: `ALBERT_TRACE_SAMPLE_RATE` (default 1.0; `error` and `degraded`
traces are always kept), `ALBERT_TRACE_RETENTION_DAYS` (default 14; worker
housekeeping deletes older rows nightly), `ALBERT_TRACE_QUEUE_SIZE`.

### Benchmark

`tests/test_recorder_overhead.py` runs `/v1/search` N times with recording on
and off against the SQLite stack and asserts the median overhead is under 5 %
and under 2 ms absolute. It runs in CI with the rest of the suite.

## Component 2: live feed and console API

All endpoints require `console.read`; `admin` implies it. They return traces
across all organizations because the audience is operators.

- `GET /v1/console/stream`: SSE. Each event is one `traces` row without
  `spans`. On PostgreSQL each API replica holds one `LISTEN albert_traces`
  connection and fans out in memory; on SQLite it polls every second.
  Supports `Last-Event-ID` for reconnect.
- `GET /v1/console/traces`: filters `kind`, `status`, `organization_id`,
  `principal_id`, `name`, `since`, `until`, `q` (matches `name` and
  `summary.query`); keyset pagination on `(started_at, id)`.
- `GET /v1/console/traces/{id}`: the full row including spans.
- `GET /v1/console/graph`: nodes and edges for the explorer. Nodes are
  entities, memories (active, id + type + sensitivity + workspace, no content)
  and the `source_memory_id` links between memories and relationships.
  Parameters: `organization_id` (required), `workspace_id`, `temporal_as_of`,
  `limit` (default 2,000 nodes, max 5,000). Cached per parameter set for 30 s.
- `GET /v1/console/overview`: traces per minute for the last hour, degraded
  and error rates, jobs by status, oldest pending job age, worker last-seen,
  trace table size and writer drop counter.
- `GET /v1/console/memories/{id}` and `/entities/{id}`: operator reads for the
  explorer's detail panel, returning subject, type, sensitivity, scope and the
  first 500 characters of content. Audited like any read.

## Component 3: console app

`console/` holds a Next.js app, built into the `albert-console` image and run
as a compose service on `127.0.0.1:8082`. The Next.js server holds
`ALBERT_CONSOLE_API_KEY` and proxies `/api/*` to `ALBERT_API_URL`; the browser
never sees the key. Access to the console is governed by the same rule as the
API and MCP: loopback or a TLS reverse proxy.

### Screens

**Live.** Virtualized feed from the SSE stream, newest first, with kind, name,
status, duration, backends used (from `summary`), organization and principal.
Filter bar mirrors the list endpoint. Clicking a row opens Replay.

**Replay.** Left: the span waterfall with offsets and durations; a step
scrubber highlights one span at a time and shows its `detail`. For retrieval
traces the `fuse` step renders a table of final hits with each backend's rank
and RRF contribution and the candidates that were found by one backend but
not the others. Right: the Explorer, focused on this trace.

**Explorer.** `react-force-graph-3d`. Node color by kind (entity, memory,
relationship), shape or ring by sensitivity. A time slider sets
`temporal_as_of` and refetches, so edges appear and expire as it moves. With a
trace selected, lexical, vector and graph candidates are tinted in three
colors, final hits pulse, and the camera frames them. Hover shows the node's
display name or memory type; click opens a detail panel that fetches through
the proxy. Organization and workspace pickers come from the overview.

### Design direction

Dark, instrument-panel aesthetic; dense but legible; monospace for ids and
numbers; motion only where it carries meaning (new feed rows, trace path
pulse). The frontend-design skill is invoked when the UI is built.

## Error handling

- Recorder failures are logged at warning and dropped; they never propagate.
- Writer queue overflow increments `traces_dropped`, exposed in `overview`.
- SSE clients reconnect with `Last-Event-ID`; the server backfills from the
  table so a dropped connection loses nothing.
- The graph endpoint refuses `limit` above the cap and returns `truncated:
  true` when the cap was hit, which the explorer shows.
- Console proxy returns the API's status codes unchanged; a 401 from the API
  renders a "console key invalid" screen rather than a blank page.

## Testing

- Recorder: span ordering, offsets, caps, no-op without a recorder, error
  spans on exceptions, sampling keeps errors.
- API: a `/v1/search` produces one trace whose `lexical`, `vector`, `graph`
  and `fuse` spans contain the ids of the hits returned; a failed request
  produces an `error` trace; traces never include content strings.
- Writer: batching, overflow drop counter, flush on shutdown.
- Stream: SQLite poll path delivers a trace created after subscription;
  `Last-Event-ID` backfills.
- Console API: capability enforced; graph cap and cache; overview numbers.
- Overhead benchmark as above.
- Console: Playwright smoke against the SQLite dev stack covering the three
  screens and a trace-to-explorer highlight. CI builds the console image.

## Deployment

- Migration `0004_traces` adds the table and indexes.
- Compose adds `albert-console` depending on `api`, with
  `ALBERT_CONSOLE_API_KEY` and `ALBERT_API_URL`; `.env.example` gains
  `ALBERT_CONSOLE_API_KEY`, `ALBERT_CONSOLE_BIND`, `ALBERT_TRACE_SAMPLE_RATE`,
  `ALBERT_TRACE_RETENTION_DAYS`.
- `albert-admin create-key --capabilities console.read` documented in
  DEPLOYMENT.md for issuing the console's key to a dedicated principal.
- Dockerfile for the console is multi-stage Node; the API image is unchanged.

## Performance budget

- Request path: one enqueue, microseconds; insert happens off-thread.
- Storage: roughly 3 to 8 KB per retrieval trace at full sampling; 14-day
  retention. The sample rate is the dial for busy installs.
- Console reads: trace list and overview are indexed; graph snapshot is capped
  and cached 30 s. The console only reads, so a read replica is a deployment
  option, not a code change.

## Out of scope for slice one

Ops dashboards with charts and alerting, tenant self-serve views, per-operator
login, OpenTelemetry export, retrieval-quality evaluation harness. Each builds
on the recorder without changing it.
