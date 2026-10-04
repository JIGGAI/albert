# REST and MCP integration

## Authentication

REST requests use an Albert API key as a bearer credential:

```http
Authorization: Bearer alb_<prefix>_<secret>
```

Keys are independently revocable and capability-scoped. The server stores a
peppered HMAC digest, never the plaintext key.

## REST example

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $ALBERT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "subject": "Deployment choice",
    "content": "Albert uses PostgreSQL for canonical records.",
    "memory_type": "decision",
    "project_ref": "albert"
  }' \
  http://127.0.0.1:8080/v1/memories
```

Hybrid search:

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $ALBERT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"Where are canonical records stored?","limit":10}' \
  http://127.0.0.1:8080/v1/search
```

Search responses identify the contributing backends and disclose degradation.
A vector hit also reports `metadata.chunk_index`, the chunk of the memory that
matched best. Context assembly returns a bounded text block plus structured
citations.

## Episodes and deletion

`POST /v1/episodes` is idempotent per principal: re-posting the same content
and `source_uri` from the same API key returns the existing episode with
status `200` instead of `201`. Another principal posting identical text gets its
own episode; nothing about other principals' episodes is disclosed.

`DELETE /v1/memories/{id}` scrubs the memory's subject, content, metadata and
chunks and closes its extracted relationships. `DELETE /v1/episodes/{id}` does
the same for the episode and every memory derived from it. Both need
`memory.delete`.

## Console API

Operator endpoints under `/v1/console`, gated by `console.read` (implied by
`admin`). They cross tenants on purpose.

- `GET /v1/console/traces` with filters `kind`, `status`, `organization_id`,
  `principal_id`, `name`, `since`, `until`, `q`, keyset `cursor`, `limit`
- `GET /v1/console/traces/{id}` including `spans`
- `GET /v1/console/stream`: server-sent events, one `event: trace` per recorded
  trace; reconnect with `Last-Event-ID`
- `GET /v1/console/graph?organization_id=…`: capped graph snapshot (entities,
  memories by type only, edges) at an optional `temporal_as_of`
- `GET /v1/console/overview`: traces per minute, job counts, writer drops
- `GET /v1/console/workspaces?organization_id=…`: a tenant's workspaces with
  active-memory counts
- `GET /v1/console/map?organization_id=…&workspace_id=…&limit=…`: the memory
  map. `nodes` (id, title, type, team, role, sensitivity, recalls,
  last_recalled_at, created_at, cluster), `links` (source, target, kind,
  weight), `clusters` (id, label, size) and `truncated`. Newest memories first;
  default `ALBERT_MAP_NODE_LIMIT`, maximum 3,000. One audit event per load.
- `GET /v1/console/memories/{id}`: audited full read for the reading pane.
  Content up to 50,000 characters with a `truncated` flag, source, team, role,
  chunk count, `related` memories with link kind and weight, and `recalls`
  (count, last, the ten most recent with query, rank and trace id)
- `POST /v1/console/search` `{organization_id, workspace_id?, query, limit}`:
  operator search across every sensitivity in that scope, returning ranked hits
  with titles and backends. Audited, and never counted as a recall
- `GET /v1/console/entities/{id}`: audited detail read, description truncated
  to 500 characters

### Backends

- `GET /v1/console/backends?hours=…`: every graph store and builder with its
  description, whether it ships in this build and whether it is in use; for
  the active store a live reachability check, version and entity and edge
  counts; the embedding setup; and activity in the window (memories indexed,
  relationships written and superseded, failures, extraction and graph-write
  latency, graph lookups made by searches). Connection settings are never
  returned.

### Operations views

Computed on request from traces, jobs and the link and recall tables. `hours`
is the window (1 to 720, default 24); at most the newest 20,000 traces in it are
read (3,000 for retrieval, which reads spans), and `truncated` says when that
cap was hit.

- `GET /v1/console/ops/agents?organization_id=…&hours=…`: per principal,
  searches, searches that found nothing, average hits, stores, failures and
  last seen; plus recalls and stores per hour (per day past 48 hours)
- `GET /v1/console/ops/memory?organization_id=…&workspace_id=…`: totals by
  type, sensitivity and workspace; never recalled, unlinked, unindexed and
  near-duplicate counts; memories added per day; most recalled and
  near-duplicate pairs with titles (audited)
- `GET /v1/console/ops/retrieval?organization_id=…&hours=…`: zero-hit and
  degraded rates, hits per search, latency percentiles, each backend's share of
  final hits and the share only it found, searches that found nothing, slowest
  searches
- `GET /v1/console/ops/service?hours=…`: count, failures and latency
  percentiles per endpoint and job type, the job queue and recent job failures
  (error type only), trace storage, worker last run, database size and the
  active providers

### Memory links

Links between memories are derived data, rebuildable with
`albert-admin rebuild-links`:

| kind | meaning | written |
|---|---|---|
| `similar` | nearest neighbours by chunk-embedding cosine similarity in the same workspace | when a memory is indexed (`ALBERT_LINK_NEIGHBORS`, `ALBERT_LINK_SIMILARITY_MIN`) |
| `sequence` | the previous dated entry (`YYYY-MM-DD` in the file name) from the same source directory | when an episode is indexed |
| `recalled` | returned together among the top five results of one search; weight is the count | by the worker, from traces |

The worker turns `POST /v1/search` and `POST /v1/context/assemble` traces into
per-memory recall counts every `ALBERT_HOUSEKEEPING_SECONDS`. Clusters are
computed per map snapshot from `similar` links and named from the most
distinctive words in their members' titles.

Every request and worker job is recorded as a trace with ordered spans
(`auth`, `resolve_scope`, `lexical`, `vector`, `graph`, `fuse`; jobs:
`classify`, `chunk`, `embed`, `write_edges`, `link`). Search and context
summaries list the final `memory_ids`; memory creation and indexing-job
summaries list `stored_ids`. Ids only, so the live map can pulse the right
nodes straight from the event stream. Retrieval spans carry up to 50
candidates as `{id, kind, score, rank}` and `fuse` carries each final hit's
per-backend reciprocal-rank contribution.

## Capabilities

`memory.read`, `memory.write`, `memory.delete`, `memory.export`,
`memory.import`, `graph.query`, `graph.write`, `working_memory.read`,
`working_memory.write`, `locks.acquire`, `console.read`, `memory.confidential`,
`memory.restricted`, and `admin`. Reading working memory needs
`working_memory.read` or `working_memory.write`.

## Portability

`GET /v1/export` returns authorized active memories, workspace names, and
explicit graph relationships in the versioned `albert-export-v1` format. Derived
relationships and embeddings are intentionally omitted and rebuilt by the
destination worker.

`POST /v1/import` accepts that bundle transactionally and is idempotent: a
memory already imported from the same source organization and `ref` is reused
rather than duplicated, and the result reports it under `memories_skipped`. An
optional `workspace_id` flattens the bundle into one destination workspace; an
organization-scoped importer can omit it to recreate or match workspaces by
name. Export and import require `memory.export` and `memory.import`
respectively.

## MCP

The deployed MCP endpoint is `/mcp` using streamable HTTP. Configure a compatible
client with the URL exposed by your TLS reverse proxy and send that client's
Albert API key as the HTTP bearer credential. The gateway forwards the key to
REST, preserving caller capability scope, revocation, and audit identity.

For local stdio operation:

```bash
ALBERT_API_URL=http://127.0.0.1:8080 \
ALBERT_MCP_API_KEY="$ALBERT_API_KEY" \
albert-mcp --transport stdio
```

Tools include:

- `memory_remember`
- `memory_ingest`
- `memory_get`
- `memory_update`
- `memory_get_episode`
- `memory_forget_episode`
- `memory_search`
- `memory_assemble_context`
- `memory_forget`
- `memory_query_graph`
- `memory_get_subgraph`
- `memory_add_relationship`
- `working_memory_start`
- `working_memory_update`
- `working_memory_get`
- `working_memory_complete`
- `working_memory_fail`
- `memory_lock_acquire`
- `memory_lock_renew`
- `memory_lock_release`
- `memory_export`
- `memory_import`

The MCP implementation is a client of the REST service; it has no independent
database or authorization rules.

## Workflow guides

The [integration handbook](integrations/README.md) contains ready-to-use setup
and operating guidance:

- [example `AGENTS.md`](integrations/AGENTS.example.md)
- [Codex](integrations/codex.md)
- [Claude Desktop and Claude Code](integrations/claude-desktop.md)
- [Cursor](integrations/cursor.md)
- [OpenClaw](integrations/openclaw.md)
- [direct REST clients](integrations/rest-client.md)
- [standard agent workflows](integrations/agent-workflows.md)
- [multi-agent handoffs](integrations/multi-agent-handoffs.md)
- [troubleshooting](integrations/troubleshooting.md)

HTTP MCP rejects anonymous requests. Keep API keys in the client's secret store,
use one principal per independently trusted caller, and require TLS for every
non-loopback connection.
