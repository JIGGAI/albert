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
| `console` | 8082 | Operator console (Next.js). Holds the operator key server-side and proxies only `/v1/console` reads and the operator search. |
| `db` | internal | PostgreSQL 17 with pgvector. |
| `falkordb`, `neo4j` | internal | Optional graph stores, started only with their Compose profile. |

## Tenancy and access

- An **organization** is a tenant. A **workspace** is a partition inside it.
- A **principal** (agent, service or person) belongs to one organization and is
  either scoped to one workspace or organization-wide.
- An **API key** belongs to a principal and carries **capabilities**:
  `memory.read`, `memory.write`, `memory.delete`, `memory.export`,
  `memory.import`, `graph.query`, `graph.write`, `working_memory.read`,
  `working_memory.write`, `locks.acquire`, `console.read`,
  `memory.confidential`, `memory.restricted`, `admin` (which implies the rest).
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
| `episodes` | Source documents and transcripts as ingested, idempotent per principal |
| `memories` | The durable memory items: subject, content, type, sensitivity, validity window |
| `memory_chunks` | Chunk text and its embedding (pgvector, HNSW index) |
| `entities`, `relationships` | The knowledge graph in the default store: typed entities, typed edges with a validity window and the memory they came from |
| `memory_links` | Derived links between memories: `similar`, `sequence`, `recalled` |
| `memory_recalls`, `memory_stats` | Which searches returned which memory, and running recall counts |
| `working_memories`, `resource_locks` | In-progress task state and exclusive leases with fencing tokens |
| `jobs` | The indexing queue |
| `traces` | The flight recorder: one row per request or job, spans embedded |
| `audit_events` | Who did what, without memory bodies |
| `console_state` | Cursors for derived data |

Derived tables (`memory_links`, `memory_recalls`, `memory_stats`) can be dropped
and rebuilt; `albert-admin rebuild-links` recomputes the links.

## Write path

1. `POST /v1/memories` or `POST /v1/episodes` (`memory_remember`,
   `memory_ingest`). The API checks capabilities, resolves the workspace,
   screens for credentials, writes the row, queues an indexing job and returns.
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

1. `auth` and `resolve_scope`: the tenant, the workspace and the sensitivities
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
subjects or content. A background thread writes traces off the request path,
so recording cannot slow or fail a request. Traces are sampled
(`ALBERT_TRACE_SAMPLE_RATE`; errors and degraded requests are always kept),
kept for `ALBERT_TRACE_RETENTION_DAYS`, and streamed live over server-sent
events. The console's Live, Replay, Operations and Backends screens are all
computed from them.

## Console

Read-only by design. It can show memory titles and, in the reading pane, full
content across every sensitivity, so treat anyone who can reach it as an
operator; each map load, memory read and operator search is audited. It cannot
change memory or configuration.

| Screen | Shows |
|---|---|
| Live | Every request and job as it happens |
| Replay | One trace step by step, with candidates and fusion math |
| Map | Memories, their links and named clusters; live pulses; search; reading pane |
| Operations | Agent activity, memory health, retrieval quality, service health |
| Backends | Graph stores and builders: in use, reachable, working, how to switch |

## Settings

Prefix `ALBERT_`. Secrets and connection strings are omitted here; see
`.env.example`.

| Setting | Default | Controls |
|---|---|---|
| `EMBEDDING_PROVIDER` | `hashing` | `hashing`, `sentence-transformers` or `openai-compatible` |
| `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS` | `BAAI/bge-small-en-v1.5`, 384 | The embedding model and its vector size |
| `LLM_PROVIDER`, `CLASSIFICATION_MODEL` | `none` | The graph builder: regex, or an OpenAI-compatible model |
| `GRAPH_STORE` | `postgres` | `postgres`, `falkordb` or `neo4j` |
| `MIN_VECTOR_SIMILARITY` | 0.2 | Floor below which a vector match is not returned |
| `CHUNK_CHARACTERS`, `CHUNK_OVERLAP_CHARACTERS`, `MAX_CHUNKS_PER_MEMORY` | 1500, 200, 200 | Chunking |
| `LINK_NEIGHBORS`, `LINK_SIMILARITY_MIN` | 5, 0.78 | How many `similar` links a memory gets and how alike two memories must be |
| `HOUSEKEEPING_SECONDS` | 60 | How often recall statistics catch up with traces |
| `MAP_NODE_LIMIT` | 1500 | Newest memories drawn on one map (maximum 3000) |
| `TRACE_SAMPLE_RATE`, `TRACE_RETENTION_DAYS`, `TRACE_QUEUE_SIZE` | 1.0, 14, 1000 | The flight recorder |
| `WORKER_POLL_SECONDS` | 2.0 | Worker idle poll |
| `LOCK_DEFAULT_TTL_SECONDS` | 900 | Default lease length |
| `MAX_SEARCH_LIMIT` | 100 | Largest `limit` a search accepts |

## Testing

- `pytest`: 166 tests, on SQLite by default, covering isolation, sensitivity,
  retrieval, links, recall processing, clustering, the console API and the
  recorder.
- One graph store contract suite runs against PostgreSQL, FalkorDB and Neo4j;
  CI starts real FalkorDB and Neo4j servers for it.
- A PostgreSQL smoke script covers pgvector retrieval, graph paths, links and
  recall processing against a migrated database.
- Playwright runs the console end to end against an isolated API and worker.
