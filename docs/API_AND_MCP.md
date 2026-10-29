# REST and MCP integration

## Authentication

REST requests use an Albert API key as a bearer credential:

```http
Authorization: Bearer alb_<prefix>_<secret>
```

Keys are independently revocable and capability-scoped. The server stores a
peppered HMAC digest, never the plaintext key.

## Organizations, workspaces and shared memories

An **organization** is a tenant: nothing is ever read or written across one.
A **workspace** partitions an organization. Every key belongs to a principal
that is either organization-wide or scoped to one workspace.

A record with no workspace is **organization-wide**: it is shared by every
workspace in the organization.

| Key | Reads | Writes by default | Writes organization-wide |
| --- | --- | --- | --- |
| organization-wide | every workspace and the shared records; `workspace_id` narrows a read to that workspace plus the shared records | organization-wide, or the `workspace_id` it names | yes |
| workspace-scoped | its workspace plus the shared records | its workspace | only with `memory.write.organization`, by sending `"scope": "organization"` |

The same rule covers memories, episodes, graph entities and relationships,
search, context assembly and export. Changing or deleting a shared record from
a workspace-scoped key also needs `memory.write.organization`; without it the
key can read shared records but not alter them. `POST /v1/memories`,
`POST /v1/episodes` and `POST /v1/relationships` accept
`"scope": "organization"`; sending it together with `workspace_id` is a 422.

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

An episode is stored durably as soon as it is accepted, but it is only
searchable once the worker has indexed it (classified, chunked, embedded and
written to the graph). Every episode response says where it is in that process:

```json
"indexing": {
  "state": "retrying",
  "searchable": false,
  "attempt": 2,
  "max_attempts": 5,
  "last_error": "TimeoutError: model call exceeded 30s",
  "next_attempt_at": "2026-10-20T02:14:09Z",
  "memory_id": null
}
```

| `state` | Meaning |
|---|---|
| `queued` | Waiting for a worker. |
| `running` | A worker is indexing it now. |
| `retrying` | An attempt failed; `last_error` says why and `next_attempt_at` when the next one runs. |
| `complete` | Searchable. `memory_id` is the memory it became. |
| `failed` | Every attempt failed; `last_error` holds the final error. |

`last_error` is the first line of the worker's error, at most 300 characters.
While an episode is not yet searchable, responses carry `Retry-After` (seconds
until it is worth asking again) and `Location` (the episode's URL).

To avoid polling, send `Prefer: wait=N` ([RFC 7240](https://www.rfc-editor.org/rfc/rfc7240))
on `POST /v1/episodes` or `GET /v1/episodes/{id}`. Albert holds the response
until the episode is `complete` or `failed`, or `N` seconds pass, whichever is
first, and answers `Preference-Applied: wait=N`. `N` is capped by
`ALBERT_MAX_INDEX_WAIT_SECONDS` (default 30). The status code does not change
with the wait: `201` still means the episode was created, `200` that it already
existed; read `indexing.state` for whether it is searchable. With the default
hashing embeddings and no model, indexing takes well under a second; with an
LLM classifier or Graphiti, allow for several model calls.

`DELETE /v1/memories/{id}` scrubs the memory's subject, content, metadata and
chunks and closes its extracted relationships. `DELETE /v1/episodes/{id}` does
the same for the episode and every memory derived from it. Both need
`memory.delete`. With the Graphiti builder, both also queue a worker job that
removes Graphiti's own copy: the episode, the facts it created, entities only
it mentioned, and the summaries of entities it shared with other episodes.
The job retries until Graphiti's store is reachable.

## Console API

Operator endpoints under `/v1/console`, gated by `console.read` (implied by
`admin`). A `console.read` key sees only its own organization, and only its own
workspace plus the shared organization-wide memories when its principal is
workspace-scoped; a request naming another organization or workspace gets 404.
Every trace records the workspace it ran in: the key's for a request, the
record's for a job, none for organization-wide work. A workspace-scoped key
sees only its own workspace's traces; job-queue counts are kept per
organization and never carry memory content. Only an `admin` key
crosses tenants. `console.read` can be issued from Settings and by connection
links, which is why it never reaches further than the key that issued it.

- `GET /v1/console/scope`: what this key can see: `admin`, `organization`
  `{id, name}`, `workspace` (`{id, name}` or null) and `capabilities`
- `GET /v1/console/traces` with filters `kind`, `status`, `organization_id`,
  `workspace_id`, `principal_id`, `name`, `since`, `until`, `q`, keyset
  `cursor`, `limit`. Each trace carries `workspace_id`
- `GET /v1/console/traces/{id}` including `spans`
- `GET /v1/console/stream`: server-sent events, one `event: trace` per recorded
  trace; reconnect with `Last-Event-ID`
- `GET /v1/console/graph?organization_id=…`: capped graph snapshot (entities,
  memories by type only, edges) at an optional `temporal_as_of`
- `GET /v1/console/overview`: traces per minute, job counts, writer drops
- `GET /v1/console/workspaces?organization_id=…`: a tenant's workspaces with
  active-memory counts
- `GET /v1/console/map?organization_id=…&workspace_id=…&shared_only=…&limit=…`:
  the memory map. A workspace's map includes the shared memories;
  `shared_only=true` draws just those. `nodes` (id, title, type, team, role, sensitivity, recalls,
  last_recalled_at, created_at, cluster), `links` (source, target, kind,
  weight), `clusters` (id, label, size) and `truncated`. Newest memories first;
  default `ALBERT_MAP_NODE_LIMIT`, maximum 3,000. One audit event per load.
- `GET /v1/console/memories/{id}`: audited full read for the reading pane.
  Content up to 50,000 characters with a `truncated` flag, source, team, role,
  chunk count, `related` memories with link kind and weight, and `recalls`
  (count, last, the ten most recent with query, rank and trace id)
- `POST /v1/console/search` `{organization_id, workspace_id?, query, limit,
  include_graph?}`: operator search across every sensitivity in that scope.
  Returns ranked hits with titles and backends (memories, plus relationships
  when `include_graph` is true), the `path` the request took (each retrieval
  step with its timing, status and candidate count), the total `duration_ms`
  and the `trace_id` for the full replay. Audited, and never counted as a
  recall. The console's Retrieval tab uses it for "Try a search"
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

- `GET /v1/console/ops/agents?organization_id=…&workspace_id=…&hours=…`: per principal,
  searches, searches that found nothing, average hits, stores, failures and
  last seen; plus recalls and stores per hour (per day past 48 hours)
- `GET /v1/console/ops/memory?organization_id=…&workspace_id=…&shared_only=…`:
  totals by type, sensitivity and workspace (shared memories are listed as
  "Organization-wide (shared)"); never recalled, unlinked, unindexed and
  near-duplicate counts; memories added per day; most recalled and
  near-duplicate pairs with titles (audited)
- `GET /v1/console/ops/retrieval?organization_id=…&workspace_id=…&hours=…`: zero-hit and
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

`memory.read`, `memory.write`, `memory.write.organization`, `memory.delete`,
`memory.export`, `memory.import`, `graph.query`, `graph.write`,
`working_memory.read`, `working_memory.write`, `locks.acquire`,
`capture.write`, `capture.system`, `console.read`, `keys.manage`,
`memory.confidential`, `memory.restricted`, and `admin`. Reading working memory
needs `working_memory.read` or `working_memory.write`.
`memory.write.organization` matters only on a workspace-scoped key: it lets the
key write, change and delete the shared organization-wide records.

## Credentials and workspaces

Routes under `/v1/settings` need `keys.manage`, which `console.read` never
implies. A credential is never issued wider than its issuer: a
workspace-scoped manager sees, issues and revokes only its own workspace's
credentials, cannot grant
`memory.write.organization`, and cannot create, rename or delete workspaces. Neither
`admin` nor `keys.manage` can be granted.

- `GET /v1/settings/credentials`: keys and connection links, each with its
  `workspace_id` and `workspace_name` (null for organization-wide), the
  `grantable_capabilities`, and the scope this key issues into
- `POST /v1/settings/api-keys` `{name, capabilities, workspace_id?}` and
  `POST /v1/settings/connection-links` `{name, capabilities,
  expires_in_seconds, workspace_id?}`: omit `workspace_id` to issue into the
  manager's own scope; a redeemed link's key keeps the link's workspace
- `DELETE /v1/settings/api-keys/{id}`, `DELETE /v1/settings/connection-links/{id}`
- `GET /v1/settings/workspaces`: workspaces with active-memory counts, the
  `credentials` (active keys and unused links) deleting each would revoke,
  `shared_memories`, and `can_manage`
- `POST /v1/settings/workspaces` `{name}` and `PATCH /v1/settings/workspaces/{id}`
  `{name}`: organization-wide managers only; a duplicate name is a 409
- `DELETE /v1/settings/workspaces/{id}?memories=organization|workspace|delete&to={id}`:
  organization-wide managers only. `memories` is required and says what happens
  to everything the workspace holds:
  - `organization`: memories, episodes and their graph become organization-wide,
    shared by every workspace.
  - `workspace`: they move into the workspace named by `to`. An entity that
    already exists there absorbs the moving one's edges, and an identical
    episode from the same owner is merged into the one already there.
  - `delete`: they are forgotten as `DELETE /v1/memories/{id}` would (scrubbed,
    rows kept for audit identity) and the workspace's graph is removed. This
    also needs `memory.delete`.

  In every case the workspace's API keys and connection links are deleted and
  its principals deactivated (kept, so audit events still name them), and its
  working memory and locks follow the memories or are removed with them. Traces
  and audit events keep the old workspace id as history. The response counts
  what was moved or removed; one `workspace.delete` audit event records the
  choice.

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

- `memory_remember` (`shared=True` stores it organization-wide)
- `memory_ingest` (`wait_seconds` returns once the episode is searchable;
  `shared=True` as above)
- `memory_get`
- `memory_update`
- `memory_get_episode` (includes `indexing`; also takes `wait_seconds`)
- `memory_forget_episode`
- `memory_search`
- `memory_assemble_context`
- `memory_forget`
- `memory_query_graph`
- `memory_get_subgraph`
- `memory_add_relationship` (`shared=True` as above)
- `working_memory_start`
- `working_memory_update`
- `working_memory_get`
- `working_memory_complete` (also writes the task-record episode; `record=False` skips it)
- `working_memory_fail` (same, with status `failed`)
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
