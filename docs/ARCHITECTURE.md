# Architecture

How Albert is built today. For the original design brief this grew from, see
[the design record](MEMORY_PLATFORM.md); where the two differ, this document
and the code are right.

## Processes

| Process | Port | What it does |
|---|---|---|
| `api` | 8080 | REST API (FastAPI). Authenticates, scopes, stores, searches, audits. Also serves the operator API under `/v1/console`. |
| `mcp` | 8081 | MCP gateway. Exposes 22 tools and forwards each caller's bearer key to the REST API, so MCP and REST share one set of rules. |
| `worker` | none | Runs indexing jobs and a housekeeping pass. |
| `console` | 8082 | Operator console (Next.js). Holds the operator key server-side and proxies only an exact allowlist: `/v1/console` reads, the operator search, and the Settings routes for credentials and workspaces. |
| `db` | internal | PostgreSQL 17 with pgvector. |
| `falkordb`, `neo4j` | internal | Optional graph stores, started only with their Compose profile. |

## Tenancy and access

- An **organization** is a tenant. A **workspace** is a partition inside it.
- A **principal** (agent, service or person) belongs to one organization and is
  either scoped to one workspace or organization-wide.
- A record with no workspace is **organization-wide** and shared by every
  workspace. A workspace-scoped key reads its own workspace plus the shared
  records, never a sibling workspace; it writes into its workspace, and writes
  or changes shared records only with `memory.write.organization` and an
  explicit `"scope": "organization"`. The rule lives in `albert/scope.py` and
  is applied by every read path and every graph store.
- An **API key** belongs to a principal and carries **capabilities**:
  `memory.read`, `memory.write`, `memory.write.organization`, `memory.delete`,
  `memory.export`, `memory.import`, `graph.query`, `graph.write`,
  `working_memory.read`, `working_memory.write`, `locks.acquire`,
  `capture.write`, `capture.system`, `console.read`, `keys.manage`,
  `memory.confidential`, `memory.restricted`, `admin` (which implies the rest).
- `keys.manage` issues credentials, never wider than the issuer: a
  workspace-scoped manager issues, sees and revokes only its own workspace's
  credentials and cannot grant `memory.write.organization`. Nothing can grant
  `admin` or `keys.manage`.
- Every memory, episode and edge has a **sensitivity**: `public`, `internal`,
  `confidential` or `restricted`. A key sees `confidential` or `restricted`
  material only with `memory.confidential` or `memory.restricted`. The filter is applied in every
  retrieval backend, not after the fact.
- Keys are stored as a peppered HMAC digest. Text that looks like a credential
  is refused on every stored surface.

## Data model

All in PostgreSQL unless a different graph store is configured.

| Table | Holds |
|---|---|
| `organizations`, `workspaces`, `principals`, `api_keys` | Tenancy and identity |
| `connection_links` | One-time links that mint a scoped key when redeemed; only a digest of the token is stored |
| `episodes` | Source documents and transcripts as ingested, idempotent per principal |
| `memories` | The durable memory items: subject, content, type, sensitivity, validity window |
| `memory_chunks` | Chunk text and its embedding (pgvector, HNSW index) |
| `entities`, `relationships` | The knowledge graph in the default store: typed entities, typed edges with a validity window and the memory they came from |
| `memory_links` | Derived links between memories: `similar`, `sequence`, `recalled` |
| `memory_recalls`, `memory_stats` | Which searches returned which memory, and running recall counts |
| `working_memories`, `resource_locks` | In-progress task state and exclusive leases with fencing tokens |
| `capture_profiles`, `capture_sessions`, `capture_events`, `capture_checkpoints` | Automatic capture: versioned profiles, sessions, the events accepted from them (each linked to its episode) and checkpoints |
| `jobs` | The indexing queue |
| `traces` | The flight recorder: one row per request or job, spans embedded, with the organization and workspace it ran in |
| `audit_events` | Who did what, without memory bodies |
| `console_state` | Cursors for derived data |

Derived tables (`memory_links`, `memory_recalls`, `memory_stats`) can be dropped
and rebuilt; `albert-admin rebuild-links` recomputes the links.

## Write path

1. `POST /v1/memories` or `POST /v1/episodes` (`memory_remember`,
   `memory_ingest`), or an accepted `/v1/capture` event. The API checks
   capabilities, resolves where the record lands (the key's workspace, or
   organization-wide when asked and allowed), screens for credentials, writes
   the row, queues an indexing job and returns. Capture events first pass their
   profile: allowed event classes, metadata minimization, size bounds, secret
   rejection or redaction.
2. The worker claims the job and runs these spans:
   - `classify`: memory type, sensitivity, entities and relationships. The
     regex extractor needs no model; with `ALBERT_LLM_PROVIDER=openai-compatible`
     a model does it. A label outside the vocabulary falls back to a safe
     default, so a model's wording cannot fail indexing. Sensitivity can only
     be raised by classification, never lowered.
   - `chunk`: paragraph-aware chunks of `ALBERT_CHUNK_CHARACTERS` with overlap.
   - `embed`: one vector per chunk.
   - `write_edges`: extracted edges go to the graph store. Edges a previous
     version of the memory produced are closed, not deleted, so the graph can
     be queried as of an earlier time.
   - `link`: `similar` links to the nearest memories in the same workspace and
     a `sequence` link to the previous dated entry. A failure here is recorded
     and never fails the job.
3. Editing a memory's text drops its chunks and `similar` links immediately and
   re-queues it. Forgetting a memory scrubs its text and removes or closes
   everything derived from it.

## Read path

`POST /v1/search` and `POST /v1/context/assemble` run these spans:

1. `auth` and `resolve_scope`: the tenant, the workspace (plus the
   organization-wide memories every workspace shares) and the sensitivities
   this key may see.
2. `lexical`: PostgreSQL full-text search over a stored, GIN-indexed `tsvector`.
3. `vector`: cosine similarity over chunk embeddings of the current embedding
   model, with a minimum similarity floor and pgvector iterative scans so a
   small tenant in a large table is not starved.
4. `graph`: edges whose entities or relation match the query, from the
   configured graph store, valid at the requested time.
5. `fuse`: reciprocal-rank fusion (k = 60) across the three rankings. Each hit
   reports the backends that found it.

If the vector or graph backend fails, the search still returns what the others
found and names the degraded backend. Context assembly adds citations and
trims to a character budget.

## Learning from use

The worker's housekeeping pass (every `ALBERT_HOUSEKEEPING_SECONDS`) reads
search traces newer than a cursor and, for each, records which memories were
returned, increments their recall counts and strengthens `recalled` links
between the top five. The cursor row is locked so two workers cannot count the
same trace. Operator searches from the console are never counted.

## Knowledge graph

Every graph read and write goes through the `GraphStore` interface
(`src/albert/graph_store.py`). Three stores implement it and pass one shared
contract test suite: PostgreSQL (default, transactional with the memory),
FalkorDB and Neo4j (shared Cypher implementation, idempotent writes). See
[graph backends](GRAPH_BACKENDS.md).

## Flight recorder

Each request and job runs inside a recorder that collects ordered spans with
timings and bounded detail: candidate ids, scores and ranks, never memory
subjects or content. Every trace records the organization and workspace it ran
in: the key's for a request, the processed record's for a job, none for
organization-wide work. A background thread writes traces off the request path,
so recording cannot slow or fail a request. Traces are sampled
(`ALBERT_TRACE_SAMPLE_RATE`; errors and degraded requests are always kept),
kept for `ALBERT_TRACE_RETENTION_DAYS`, and streamed live over server-sent
events. The console's Live, Replay, Operations and Backends screens are all
computed from them.

## Console

It never changes memory or configuration. It can show memory titles and, in
the reading pane, full content across every sensitivity, so treat anyone who
can reach it as an operator; each map load, memory read and operator search is
audited. With `keys.manage` on its key, Settings also issues and revokes
credentials and creates, renames and deletes workspaces; deleting one shares,
moves or forgets the memories it held, as the operator chooses.

What it shows follows its key: a `console.read` key sees only its own
organization, and only its own workspace plus the shared memories when the key
is workspace-scoped; only an `admin` key sees every tenant.
`GET /v1/console/scope` tells the UI which, so it offers only pickers that
change something.

| Screen | Shows |
|---|---|
| Live | Every request and job as it happens |
| Replay | One trace step by step, with candidates and fusion math |
| Map | Memories, their links and named clusters; live pulses; search; reading pane |
| Operations | Agent activity, memory health, retrieval quality, service health |
| Backends | Graph stores and builders: in use, reachable, working, how to switch |
| Workspaces | Create, rename and delete workspaces, with what happens to their memories |
| Settings | Scoped API keys and one-time connection links |

## Settings

Prefix `ALBERT_`. Secrets and connection strings are omitted here; see
`.env.example`.

| Setting | Default | Controls |
|---|---|---|
| `EMBEDDING_PROVIDER` | `hashing` | `hashing`, `sentence-transformers` or `openai-compatible` |
| `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS` | `BAAI/bge-small-en-v1.5`, 384 | The embedding model and its vector size |
| `LLM_PROVIDER`, `CLASSIFICATION_MODEL` | `none` | The graph builder: regex, or an OpenAI-compatible model |
| `GRAPH_STORE` | `postgres` | `postgres`, `falkordb` or `neo4j` |
| `GRAPH_BUILDER` | unset | `regex`, `llm-classifier` or `graphiti`; unset picks regex without a model, the LLM classifier with one |
| `EMBEDDING_REQUEST_DIMENSIONS` | false | Send the dimension count to an OpenAI-compatible embedding endpoint |
| `PUBLIC_BASE_URL`, `PUBLIC_MCP_URL` | `http://localhost:8080`, `http://localhost:8081/mcp` | Addresses advertised by connection links; set them to what agents actually reach |
| `MIN_VECTOR_SIMILARITY` | 0.2 | Floor below which a vector match is not returned |
| `CHUNK_CHARACTERS`, `CHUNK_OVERLAP_CHARACTERS`, `MAX_CHUNKS_PER_MEMORY` | 1500, 200, 200 | Chunking |
| `LINK_NEIGHBORS`, `LINK_SIMILARITY_MIN` | 5, 0.78 | How many `similar` links a memory gets and how alike two memories must be |
| `HOUSEKEEPING_SECONDS` | 60 | How often recall statistics catch up with traces |
| `MAP_NODE_LIMIT` | 1500 | Newest memories drawn on one map (maximum 3000) |
| `TRACE_SAMPLE_RATE`, `TRACE_RETENTION_DAYS`, `TRACE_QUEUE_SIZE` | 1.0, 14, 1000 | The flight recorder |
| `WORKER_POLL_SECONDS` | 2.0 | Worker idle poll |
| `LOCK_DEFAULT_TTL_SECONDS` | 900 | Default lease length |
| `MAX_SEARCH_LIMIT` | 100 | Largest `limit` a search accepts |
| `MAX_INDEX_WAIT_SECONDS` | 30 | Longest `Prefer: wait=N` an episode write or read may hold a worker thread |
| `CONSOLE_GRAPH_CACHE_SECONDS` | 30 | How long a console graph or map snapshot is reused while nothing changes |

## Testing

- `pytest`, on SQLite by default, covering tenant and workspace isolation,
  shared memories, sensitivity, retrieval, links, recall processing,
  clustering, credentials, automatic capture, the console API and the recorder.
- One graph store contract suite runs against PostgreSQL, FalkorDB and Neo4j;
  CI starts real FalkorDB and Neo4j servers for it.
- A PostgreSQL smoke script covers pgvector retrieval, graph paths, links and
  recall processing against a migrated database.
- Playwright runs the console end to end against an isolated API and worker.
