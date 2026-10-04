# Knowledge-graph backends

Albert's knowledge graph has two independent choices: **where the graph is
stored** and **what builds it** from memory text. PostgreSQL with embeddings is
the baseline every install has; the rest are options on top.

## What ships today

| Piece | Setting | Status |
|---|---|---|
| Store: PostgreSQL | `ALBERT_GRAPH_STORE=postgres` (default) | Available |
| Store: FalkorDB | `ALBERT_GRAPH_STORE=falkordb` | Available |
| Store: Neo4j | `ALBERT_GRAPH_STORE=neo4j` | Available |
| Builder: regex | `ALBERT_LLM_PROVIDER=none` (default) | Available |
| Builder: LLM classifier | `ALBERT_LLM_PROVIDER=openai-compatible` | Available |
| Builder: Graphiti | not yet a setting | Planned |

`GET /v1/health/live` reports the active `graph_store` and `graph_builder`. The
console's **Backends** page shows every option, which is in use, whether it is
reachable, how much it holds, how it has been performing, and the steps to
switch. The console is read-only, so switching is always a settings change.

## Choosing a store

The store holds entities and the typed, time-bounded edges between them.

**PostgreSQL** (default). Entities and edges are tables in the database Albert
already uses.

- One database to run, back up and restore.
- Graph writes commit in the same transaction as the memory they came from, so
  the graph can never disagree with the memory store.
- Suits lookups of one or two hops. It has not been benchmarked on large
  graphs; measure before relying on it past a few hundred thousand edges.
- Choose it unless you have a specific reason below.

**FalkorDB.** A Redis-based graph database queried with Cypher.

- Built for deep multi-hop traversal, with a small memory footprint.
- The lightest database Graphiti can build on.
- Choose it when traversal depth is a core workload, or when you want Graphiti
  on a single host.
- Costs: a second service to operate and back up; graph writes are no longer
  transactional with memories.

**Neo4j.** The established graph database.

- Mature Cypher tooling, graph algorithms (community detection, centrality),
  visual exploration, clustering and commercial support.
- Choose it when you need that ecosystem or already operate Neo4j.
- Costs: the heaviest option (a JVM service, typically 1-2 GB of memory or
  more), plus the same loss of transactional writes as FalkorDB.

### Running with FalkorDB

```bash
# .env
ALBERT_GRAPH_STORE=falkordb

docker compose --profile falkordb up -d
```

The `falkordb` service only starts with that profile, so a default install runs
nothing extra. Albert keeps the whole graph in one FalkorDB graph
(`ALBERT_FALKORDB_GRAPH`, default `albert`) and filters every query by
organization and workspace. Point `ALBERT_FALKORDB_URL` at an existing server
to use one you already run.

Switching stores does not move data. Edges written to PostgreSQL stay there;
re-index memories (or re-import explicit relationships) to populate the new
store. Back up the `albert-falkordb` volume alongside the database.

All three stores run the same behavioural tests
(`tests/test_graph_contract.py`), and CI runs them against a real FalkorDB and
a real Neo4j.

### Running with Neo4j

```bash
# .env
ALBERT_GRAPH_STORE=neo4j
ALBERT_NEO4J_PASSWORD=<a strong password>

docker compose --profile neo4j up -d
```

The `neo4j` service (Neo4j 5.26 Community) only starts with that profile. Its
heap and page cache default to 512 MB and 256 MB (`ALBERT_NEO4J_HEAP`,
`ALBERT_NEO4J_PAGECACHE`); allow the host 1-2 GB for it. As with FalkorDB,
switching does not move data: re-index to fill it, and back up the
`albert-neo4j` volume.

FalkorDB and Neo4j share one implementation of the queries
(`src/albert/graph_cypher.py`); each only supplies its connection.

## Choosing a builder

The builder turns memory text into entities and edges. It is the larger
quality lever: a better store does not help a graph nothing fills.

**Regex** (default). Pattern matching for statements like "A uses B".

- No model, no cost, deterministic.
- Finds little in ordinary prose. Treat it as a placeholder: explicit
  relationships written through the API or MCP still work fully.

**LLM classifier.** Albert's own extractor, using any OpenAI-compatible model.

- Extracts typed entities and relationships from each memory as it is indexed
  and writes them to whichever store is configured.
- Choose it as the first step up from regex. See [providers](PROVIDERS.md).
- Costs: one model call per memory indexed.

**Graphiti** (planned). A temporal knowledge-graph framework.

- Detects when a new fact contradicts an old one and ends the old fact's
  validity, deduplicates entities, and keeps episode provenance.
- Requires FalkorDB or Neo4j as its store and an LLM for extraction.
- Choose it when facts that change over time are central to how agents use
  memory and the classifier's plain extraction is not enough.
- Costs: several model calls per memory, and a second database.

## How a store plugs in

Every graph read and write goes through the `GraphStore` interface in
`src/albert/graph_store.py`. The PostgreSQL implementation is
`PostgresGraphStore` in `src/albert/graph.py` and the FalkorDB one is
`FalkorDBGraphStore` and `Neo4jGraphStore` in `src/albert/graph_cypher.py`; a test fails if any
other module reaches for the graph tables directly.

A new store implements these operations:

| Operation | Purpose |
|---|---|
| `add_relationship` | Upsert both entities and create or reuse an open edge |
| `close_memory_edges` | End the validity of a memory's edges when it is edited or forgotten |
| `retarget_memory_edges` | Make extracted edges follow a memory's sensitivity and validity |
| `query` | Edges matching the words of a search query (the graph leg of hybrid search) |
| `get_entity`, `subgraph` | Entity lookup and bounded traversal |
| `export_relationships` | Explicit edges for a portable export |
| `snapshot`, `counts`, `fingerprint` | Console views and their cache key |
| `ping` | Reachability and version for the Backends page |

Rules for a store that is not PostgreSQL:

- It cannot join the request's database transaction. Each write must be
  idempotent, because the worker retries a failed indexing job.
- It must enforce organization, workspace, sensitivity and validity filters
  itself on every read. Tenant isolation is the store's responsibility.
- It returns the store-neutral types (`GraphEntity`, `StoredRelationship`,
  `RelationshipRead`), so REST and MCP contracts do not change.
- It is registered in `GRAPH_STORES` with `available=True`, added to the
  `graph_store` setting, and constructed in `get_graph_store()`.

## Order of work

1. Store interface with PostgreSQL behind it. Done.
2. An LLM for the classifier. Every builder beyond regex needs one.
3. FalkorDB store adapter. Done.
4. Neo4j store adapter. Done.
5. Graphiti as a builder, once a self-hosted model is available.
