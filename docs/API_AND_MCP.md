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
- `GET /v1/console/memories/{id}` and `/entities/{id}`: audited detail reads
  with content truncated to 500 characters

Every request and worker job is recorded as a trace with ordered spans
(`auth`, `resolve_scope`, `lexical`, `vector`, `graph`, `fuse`; jobs:
`classify`, `chunk`, `embed`, `write_edges`). Retrieval spans carry up to 50
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
