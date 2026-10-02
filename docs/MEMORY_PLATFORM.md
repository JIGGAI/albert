# Albert: Independent Agent Memory Platform

Status: architecture and implementation brief  
Repository: `JIGGAI/albert`  
Last updated: 2026-10-02

## 1. Executive summary

Albert will be a standalone, deployable memory service for AI agents and
applications. Clients will be able to store, classify, relate, search, and
retrieve durable memories through versioned REST APIs and Model Context
Protocol (MCP) tools.

Albert is intended to combine three retrieval modes:

1. Lexical retrieval for exact words, identifiers, error messages, and names.
2. Vector retrieval for semantic similarity when wording differs.
3. Temporal knowledge-graph retrieval for entities, relationships, provenance,
   and facts that change over time.

The combined result is a hybrid memory-RAG platform, not merely a vector
database with an MCP wrapper.

## 2. Independence boundary

Albert must be built and operated completely independently.

- Do not access, call, modify, deploy to, synchronize with, or depend on any
  TenHost system.
- Do not copy TenHost source code, configuration, databases, credentials,
  memories, or proprietary implementation details.
- Do not deploy Albert on an existing TenHost host.
- Use synthetic fixtures and independently created test data.
- Give Albert its own repository, infrastructure, credentials, databases,
  monitoring, backups, documentation, and product identity.
- Any future interoperability must be a separately authorized project using a
  documented public contract. It is not part of the current scope.

Public specifications and appropriately licensed open-source projects may be
used as references or dependencies, subject to license review and attribution.

## 3. Goals

Albert should:

- Work for individual users, teams, self-hosters, and multi-tenant services.
- Offer the same core behavior through REST, MCP, and future SDKs.
- Preserve the original source and provenance of every derived memory.
- Support explicit memories and automatic ingestion from approved sources.
- Classify memory type, sensitivity, scope, entities, and relationships.
- Combine keyword, vector, graph, metadata, and temporal retrieval.
- Enforce authorization before retrieval, not after results are assembled.
- Support working memory, handoffs, progress, and exclusive resource locks.
- Be inspectable, exportable, deletable, auditable, and rebuildable.
- Degrade honestly when an optional retrieval backend is unavailable.
- Run locally through Docker Compose and scale to a production deployment.

## 4. Non-goals for the initial release

- Training a foundation model.
- Creating a general-purpose graph database or vector database.
- Silently saving every conversation by default.
- Storing passwords, tokens, private keys, or other secrets as memories.
- Depending on a single model vendor, graph engine, or embedding provider.
- Requiring a knowledge graph for a minimal installation.
- Building autonomous agent orchestration before the memory service is sound.

## 5. Product model

Albert separates durable source records from derived indexes:

```text
Approved sources
    |
    v
Immutable episodes and explicit memories  <-- canonical records
    |
    +--> lexical document/index
    +--> chunks and embeddings
    +--> classified memory items
    +--> entities and temporal relationships
    +--> summaries and consolidations
              |
              v
       hybrid retrieval and context assembly
              |
              v
        REST, MCP, and SDK clients
```

PostgreSQL is the authoritative transactional store. Vector indexes and graph
representations are derived and must be reconstructable from canonical records.
Large original artifacts may live in S3-compatible object storage while their
identity, hash, metadata, and access policy remain in PostgreSQL.

## 6. Proposed technology stack

### Service layer

- Python 3.12+
- FastAPI and Pydantic
- SQLAlchemy 2 and Alembic migrations
- Separate API and worker processes using shared application services
- OpenAPI generated from the REST implementation

Python is preferred because the proposed graph integration and the JIGGA
reference implementation are Python-native. MCP must remain a transport adapter,
not a second implementation of business rules.

### Primary database

- PostgreSQL for canonical memories, tenants, identities, authorization,
  processing state, working memory, locks, audit records, and outbox events.
- PostgreSQL full-text search for lexical retrieval.
- `pgvector` for embeddings and nearest-neighbor indexes.

Using PostgreSQL for both metadata and vectors keeps the first production
version operationally simple and preserves transactional consistency. A
dedicated vector engine can be added later behind an interface if measured scale
requires it.

### Knowledge graph

- Graphiti as the temporal knowledge-graph framework.
- FalkorDB or Neo4j as the initial graph database.
- Do not make Kuzu the default: Graphiti currently marks its Kuzu driver as
  deprecated because the upstream Kuzu project is no longer maintained.

The graph backend must be replaceable. PostgreSQL remains the authoritative
source for episodes and processing state even when graph-derived entities and
edges are materialized elsewhere.

### Asynchronous processing

Initial options:

- PostgreSQL-backed job queue for the simplest deployment, or
- Redis plus a worker framework when throughput requires it.

Jobs include chunking, classification, embedding, entity resolution, graph
extraction, deduplication, consolidation, deletion propagation, and index
rebuilding. The API should not wait for expensive model calls.

### Deployment

- Docker images for API, MCP gateway, and worker.
- Docker Compose for a complete single-host installation.
- Optional Kubernetes manifests or Helm chart after the deployment contract is
  stable.
- Object storage may use MinIO locally and an S3-compatible provider in hosted
  environments.

## 7. Major components

```text
Clients
|-- MCP: Codex, Claude, Cursor, OpenClaw, and compatible agents
|-- REST: applications, automations, scripts, and web products
`-- SDKs: Python and TypeScript
              |
              v
API gateway and authentication
              |
              v
Albert application services
|-- memory ingestion and CRUD
|-- authorization and scope resolution
|-- classification and policy enforcement
|-- hybrid search and context assembly
|-- entity and graph queries
|-- working memory and locks
|-- audit, export, and deletion
`-- health, administration, and usage controls
       |              |                 |
       v              v                 v
PostgreSQL         pgvector       Graphiti graph DB
FTS + metadata     embeddings     entities + temporal edges
```

All interfaces must call the same application services. REST, MCP, CLI, and
SDK behavior must not drift.

## 8. Tenancy, identity, and scopes

Albert must be multi-tenant from its first schema even if the first deployment
has only one user.

Suggested hierarchy:

```text
organization
`-- workspace
    |-- project
    |   `-- task
    |       `-- run
    `-- agents and users
```

Every canonical memory and every derived record must carry an organization ID.
Optional workspace, project, task, run, user, and agent identifiers narrow its
scope. Database queries must include tenant predicates by construction. Where
practical, PostgreSQL row-level security should provide defense in depth.

Principal types:

- Human user
- Service account
- Agent
- Administrative operator

Authorization should combine roles with explicit capabilities such as:

- `memory.read`
- `memory.write`
- `memory.delete`
- `memory.export`
- `graph.query`
- `working_memory.write`
- `locks.acquire`
- `admin.manage`

API keys must be stored as one-way hashes, shown only once, independently
revocable, optionally expiring, and scoped to capabilities and workspaces.
OIDC/OAuth can be added for human-facing hosted installations.

## 9. Core data concepts

### Episode

An immutable unit of source material: a message, transcript, note, task result,
document, configuration observation, or imported event. It includes source,
time, content hash, provenance, owner, scope, sensitivity, and ingestion status.

### Memory item

A durable statement or procedure derived from an episode or written explicitly.
Suggested types include:

- fact
- preference
- procedure
- lesson
- warning
- project state
- task result
- decision
- relationship
- recurring pattern
- reminder
- summary

Every derived item points back to its episode or explicit source.

### Chunk

A retrievable text segment derived from an episode or memory item. Chunking
policy must be versioned so indexes can be reproduced after the algorithm
changes.

### Embedding

A vector associated with a versioned text representation, model, dimensions,
normalization policy, and creation time. Changing models creates a new embedding
version; vectors from different spaces must never be compared directly.

### Entity and relationship

An entity is a typed real-world or operational concept such as a person,
organization, project, tool, server, product, or policy. A relationship is a
typed edge with provenance, confidence, `valid_from`, and `valid_until`.

### Working memory

Mutable task coordination state that is deliberately separate from durable
knowledge. It contains task state, progress, resource references, ownership,
expiration, and optional locks. Completion may propose selected durable
memories; it must not promote everything automatically.

### Audit event

An append-only record of reads, writes, administrative operations, policy
decisions, degraded searches, exports, and deletions. Audit payloads must be
redacted and must never contain credentials.

## 10. Ingestion and classification pipeline

```text
receive source
  -> authenticate and authorize
  -> validate size and media type
  -> detect or reject secrets
  -> persist canonical episode
  -> make explicit text lexically searchable
  -> enqueue enrichment
  -> chunk and classify
  -> determine scope and sensitivity
  -> create embeddings
  -> extract and resolve entities
  -> propose temporal relationships
  -> run deterministic verifiers
  -> apply approval policy
  -> update graph and derived indexes
  -> emit audit and outbox events
```

Classification should produce typed JSON with a versioned schema. Model output
is untrusted input and must pass deterministic validation. Sensitive facts,
preferences, and relationships may require human approval before becoming
durable or broadly visible.

Explicit memories should be available for lexical retrieval immediately. The
API must expose enrichment status so callers know whether vector and graph
indexing has completed.

## 11. Hybrid retrieval

For each search:

1. Authenticate the principal.
2. Resolve allowed tenant, workspace, project, task, agent, and sensitivity
   scopes.
3. Run lexical retrieval for precise terms.
4. Run vector retrieval for semantic similarity.
5. Retrieve temporally valid graph facts and related entities.
6. Apply metadata, time, sensitivity, and policy filters inside each retrieval
   path.
7. Fuse ranked lists using reciprocal-rank fusion (RRF).
8. Apply recency, confidence, provenance, and diversity adjustments.
9. Optionally rerank a small authorized shortlist.
10. Return results with citations, source IDs, retrieval signals, and enough
    explanation to audit why they were selected.

The system must never retrieve globally and filter unauthorized results only at
the end. This risks leaks through scores, timing, logs, rerankers, and model
prompts.

RRF is the initial fusion strategy because BM25, cosine similarity, and graph
scores are not directly comparable. Retrieval evaluation—not intuition—should
drive later weighting or learned ranking.

## 12. REST API outline

The initial versioned interface should include:

```text
POST   /v1/memories
GET    /v1/memories/{memory_id}
PATCH  /v1/memories/{memory_id}
DELETE /v1/memories/{memory_id}

POST   /v1/episodes
GET    /v1/episodes/{episode_id}
GET    /v1/episodes/{episode_id}/status

POST   /v1/search
POST   /v1/context/assemble

GET    /v1/entities/{entity_id}
POST   /v1/graph/query
GET    /v1/graph/subgraph

POST   /v1/working-memory
PATCH  /v1/working-memory/{working_id}
POST   /v1/working-memory/{working_id}/complete
POST   /v1/working-memory/{working_id}/fail

POST   /v1/locks/acquire
POST   /v1/locks/{lock_id}/renew
POST   /v1/locks/{lock_id}/release

GET    /v1/export
POST   /v1/import
GET    /v1/health/live
GET    /v1/health/ready
```

The exact paths are provisional. An OpenAPI contract and generated compatibility
tests should stabilize them before the first public release.

## 13. MCP tool outline

The MCP server should expose a small, stable tool surface:

- `memory_remember`
- `memory_get`
- `memory_search`
- `memory_forget`
- `memory_ingest`
- `memory_enrichment_status`
- `memory_query_graph`
- `memory_get_subgraph`
- `memory_assemble_context`
- `working_memory_start`
- `working_memory_update`
- `working_memory_complete`
- `working_memory_fail`
- `memory_lock_acquire`
- `memory_lock_renew`
- `memory_lock_release`

MCP tool handlers should be thin clients of Albert's REST or application service
layer. They must not contain a separate storage or authorization implementation.

## 14. Working memory and locking

Working memory is required for safe collaboration by multiple agents.

- Records have owners, status, progress, resource type/reference, expiration,
  and heartbeat timestamps.
- Locks are explicit leases with fencing tokens, not permanent boolean flags.
- Acquisition and renewal are transactional.
- Expired leases cannot be renewed by an old owner after a new owner has
  acquired the resource.
- Completing a task releases its locks transactionally.
- Durable lessons are proposed separately during completion.

PostgreSQL transactions and advisory locks may support implementation, but the
public abstraction must be a lease with an opaque lock ID and monotonic fencing
token.

## 15. Security requirements

- TLS for all non-loopback connections.
- One-way hashing for API keys and refresh tokens.
- Encryption at rest using infrastructure-managed keys where available.
- Tenant and scope enforcement in every storage adapter.
- Deny-by-default capabilities.
- Request size, rate, and concurrency limits.
- Secret and credential detection before durable ingestion.
- Prompt-injection labeling and isolation for untrusted imported content.
- No raw memory content in routine logs, traces, metrics, or exception reports.
- Tamper-evident or externally retained audit logs for hosted deployments.
- Export and deletion workflows, including derived-index deletion.
- Retention policies and legal-hold support as later production features.
- Dependency scanning, signed images, migration tests, and reproducible builds.

Memory poisoning is a core threat. Confidence, provenance, source trust, and
approval status must be stored explicitly and used during retrieval.

## 16. Reliability and operations

- Readiness must check PostgreSQL and required migrations; optional backends
  report degraded state separately.
- Search may fall back to lexical retrieval if vector or graph systems fail,
  but every response must disclose degradation.
- Use an outbox pattern for graph, embedding, webhook, and audit propagation.
- Jobs must be idempotent and safe to retry.
- Back up canonical PostgreSQL data and object storage independently.
- Graph and vector indexes should be rebuildable and tested as such.
- Provide documented restore drills, not just backup jobs.
- Instrument request latency, retrieval latency by backend, job age, failures,
  index lag, model cost, authorization denials, and degraded searches.
- Support zero-downtime database migrations only after the initial schema and
  deployment model stabilize.

## 17. Suggested repository structure

```text
albert/
|-- apps/
|   |-- api/
|   |-- mcp/
|   `-- worker/
|-- albert/
|   |-- auth/
|   |-- memory/
|   |-- retrieval/
|   |-- graph/
|   |-- embeddings/
|   |-- working_memory/
|   |-- audit/
|   `-- common/
|-- migrations/
|-- deploy/
|   |-- compose/
|   `-- kubernetes/
|-- docs/
|-- tests/
|   |-- unit/
|   |-- integration/
|   |-- contract/
|   `-- retrieval_eval/
|-- pyproject.toml
|-- Dockerfile
|-- compose.yaml
`-- README.md
```

This is a starting layout, not a constraint. The first scaffold should keep the
API, MCP adapter, worker, and domain services clearly separated without creating
premature microservices.

## 18. Delivery plan

### Phase 0: contracts and evaluation corpus

- Finalize terminology and threat model.
- Define tenant and authorization invariants.
- Write OpenAPI and MCP schemas.
- Create a synthetic retrieval corpus and expected-query benchmark.
- Record architecture decisions as ADRs.

### Phase 1: deployable lexical/vector MVP

- FastAPI service and PostgreSQL migrations.
- Organizations, workspaces, principals, API keys, and capabilities.
- Canonical episodes and explicit memories.
- PostgreSQL full-text and pgvector retrieval.
- Hybrid RRF search with citations.
- MCP server calling the same application services.
- Worker, outbox, health checks, Docker Compose, tests, and backups.

### Phase 2: classification and operational memory

- Versioned classification schemas and deterministic verifiers.
- Secret screening, sensitivity, confidence, and approval queue.
- Deduplication and contradiction candidates.
- Working-memory state and leased locks.
- Import/export and deletion propagation.

### Phase 3: temporal knowledge graph

- Graphiti adapter with FalkorDB or Neo4j.
- Entity resolution, typed ontology, temporal edges, and provenance.
- Graph query and subgraph APIs/MCP tools.
- Graph results integrated into RRF retrieval.
- Rebuild and reconciliation tooling.

### Phase 4: production platform

- OIDC, administrative UI, quotas, billing hooks, and webhooks.
- Horizontal workers and high-availability deployment.
- Policy management, retention controls, and stronger audit retention.
- Python and TypeScript SDKs.
- Retrieval-quality dashboards and controlled ranking experiments.

## 19. Initial acceptance criteria

The first usable release should demonstrate that:

1. Two organizations cannot retrieve or infer each other's records.
2. REST and MCP return equivalent results for equivalent authorized requests.
3. Exact identifiers are found lexically and paraphrased facts are found
   semantically.
4. Every result includes canonical provenance.
5. An unavailable vector backend produces explicit degraded output while
   lexical search continues.
6. Deleting a memory removes it from lexical and vector retrieval and queues
   deletion from every derived backend.
7. Rebuilding all derived indexes from canonical records produces equivalent
   retrieval behavior.
8. Locks expire safely, use fencing tokens, and prevent stale writers.
9. Logs and traces contain no API keys or raw memory bodies.
10. A clean machine can start the complete system from documented Docker
    Compose instructions and pass health and contract tests.

Graph-specific acceptance criteria will be added before Phase 3 and must cover
temporal invalidation, entity resolution, provenance, and authorization.

## 20. Prior research and conclusions

### Public Raccoon repositories

The public repositories under `github.com/raccoonaihq` were reviewed. They
primarily contain browser-automation API specifications, generated SDKs, an MCP
wrapper, examples, evaluation projects, and upstream forks. Their public API is
associated with `raccoonai.tech` and exposes automation sessions and tasks.
The public repositories did not expose a reusable hosted memory backend.

Conclusion: do not base Albert on those repositories or assume that similarly
named products share an implementation.

### JIGGA memory architecture

JIGGA's public memory documents provide a useful independent design:

- Canonical raw and structured memory remain inspectable.
- Keyword, vector, and graph layers are derived and replaceable.
- Scope and sensitivity rules apply uniformly.
- Rankings from different retrieval modes use RRF.
- Model-produced graph facts pass deterministic verifier gates.

The current JIGGA core implements file-based canonical memory, SQLite FTS5,
scoped search, a provider seam, and multi-backend routing. The separate
`JIGGAI/memory-vector-local` pack implements local vector storage and search.
True semantic behavior requires its optional sentence-transformer embedder; its
dependency-free feature-hashing default is lexical rather than semantic.

The documented JIGGA Graphiti adapter and graph extraction pipeline are designs,
not currently present as production implementations in the core repository.
Albert can adopt the concepts but must implement, integrate, test, and operate
its own server-oriented equivalents.

### Architecture classification

Albert should be described as a multi-tenant hybrid agent-memory platform with:

- relational canonical storage,
- lexical and vector retrieval,
- a temporal knowledge graph,
- provenance-aware RAG context assembly, and
- working-memory coordination.

"Knowledge graph" should be used only when Albert has actual typed entities,
edges, traversal, provenance, and temporal semantics—not merely hierarchical
metadata in relational tables.

## 21. Key open decisions

1. FalkorDB versus Neo4j for the first graph deployment.
2. Local embedding default and hosted-provider interface.
3. PostgreSQL-backed jobs versus Redis for the first release.
4. Initial ontology ownership and extension mechanism.
5. Chunking policies for conversations, documents, and structured events.
6. Reranker policy, including whether hosted deployments enable one by default.
7. Approval rules for inferred facts, preferences, and relationships.
8. Single-host resource target and supported scale envelope.
9. License and contribution model for Albert itself.
10. Hosted-service requirements versus self-hosted-only initial delivery.

These should be resolved through short architecture decision records and
retrieval/security tests rather than undocumented implementation choices.

## 22. Immediate next steps

1. Approve this brief as the initial product boundary.
2. Choose Albert's license and initial deployment target.
3. Write ADRs for PostgreSQL/pgvector, graph engine, and job queue.
4. Create the synthetic evaluation corpus and authorization test matrix.
5. Scaffold the Phase 1 service, migrations, MCP adapter, worker, and Docker
   Compose environment.
6. Implement contract tests before adding model-assisted classification.

This document is the working source of truth until superseded by committed ADRs,
API contracts, and implementation documentation.
