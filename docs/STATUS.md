# Implementation status

## Ready now

Albert 0.3 is ready for an isolated self-hosted deployment and integration
testing through REST and MCP.

Implemented and tested:

- Versioned FastAPI REST service and generated OpenAPI schema
- Official MCP Python SDK with stdio, SSE, and streamable HTTP transports
- Multi-tenant organizations, workspaces, principals, API keys, and capabilities
- Peppered one-way API-key and lock-token storage
- Canonical episodes and explicit durable memories
- Asynchronous job worker with retry and stale-job recovery
- Deterministic classification and optional OpenAI-compatible classification
- Dependency-free development embeddings
- Local sentence-transformer and OpenAI-compatible semantic embedding options
- Deployment-time provider probes, vector-dimension validation, and reindex tooling
- PostgreSQL full-text retrieval over a stored, GIN-indexed tsvector
- Chunked embeddings with pgvector storage, cosine retrieval, HNSW index, and
  iterative scans so small tenants are not starved by post-filtering
- Reciprocal-rank fusion across lexical, vector, and graph results
- Typed temporal entities and relationships with provenance
- Natural-language graph lookup and bounded subgraph traversal
- Stale extracted-edge invalidation when memories are edited and re-enriched
- Sensitivity enforcement across memories, episodes, vector results, and graph edges
- Bounded context assembly with citations
- Configurable minimum vector similarity with raw backend scores
- Working memory, expiration, completion/failure, and progress state
- Exclusive lease locks with secret tokens and monotonic fencing values
- Audit events without raw memory bodies
- Credential-pattern screening on every stored surface, including metadata,
  working memory, graph descriptions, and import bundles
- Scrubbing deletion for memories and episodes, including derived records
- Per-principal episode idempotency
- Alembic migration and drift check
- Docker image and Docker Compose deployment
- Readiness/liveness endpoints
- Backup and guarded restore scripts
- Portable memory/explicit-graph export and transactional import
- Per-caller bearer identity forwarding for HTTP MCP, verified end to end
  through the MCP session manager
- Flight recorder: every request and worker job traced with retrieval
  candidates and fusion math, off-thread writes, sampling and retention
- Live trace feed over server-sent events
- Operator console (Next.js): Live feed, trace Replay, and a 2D memory Map with
  similarity/sequence/recalled-together links, named clusters, live recall and
  store pulses, replay, operator search and a full-content reading pane;
  Playwright-covered
- Derived memory links and recall statistics, rebuildable with
  `albert-admin rebuild-links`
- Workspace, principal, API-key listing, issuance, and revocation commands
- Unit/integration tests for retrieval, graph behavior, isolation, sensitivity,
  ingestion, deletion, and locks
- Integration handbook covering Codex, Claude Desktop/Code, Cursor, OpenClaw,
  direct REST clients, agent instructions, workflows, handoffs, and diagnostics

## Deployment posture

This release is suitable for:

- Local installations
- Private internal networks behind TLS
- Development and evaluation environments
- Controlled agent integrations using scoped API keys

It has not yet received an external security audit and should not be marketed as
a compliance-certified public SaaS platform.

## Deliberate implementation choices

- PostgreSQL is canonical storage.
- PostgreSQL/pgvector provides vector indexing.
- Graph storage sits behind a `GraphStore` interface (`ALBERT_GRAPH_STORE`).
  PostgreSQL is the only store shipped; FalkorDB, Neo4j and a Graphiti builder
  are planned adapters that will not change REST or MCP contracts. See
  [graph backends](GRAPH_BACKENDS.md).
- The default hashing embedder avoids downloads but is lexical rather than
  semantic. Operators must select `sentence-transformers` or an OpenAI-compatible
  embedding endpoint for semantic retrieval.
- Model classification is optional; deterministic behavior remains available
  when no external model is configured.

## Work required before untrusted public SaaS operation

- Independent application-security review and penetration test
- OIDC/OAuth human identity flows
- PostgreSQL row-level security as defense in depth
- Distributed rate limiting, quotas, and abuse controls
- Customer lifecycle, billing, and administrative UI
- External tamper-evident audit retention
- Formal data-retention, legal deletion, and compliance controls
- Signed release images, SBOM publication, and supply-chain policy
- Load testing against the intended tenant and corpus scale

These items do not prevent self-hosted REST/MCP use; they define the boundary
between a deployable open system and a managed public service.
