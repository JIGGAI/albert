# Implementation status

## Ready now

Albert 0.4 is ready for an isolated self-hosted deployment and integration
testing through REST and MCP. [Architecture](ARCHITECTURE.md) describes how it
is built.

Implemented and tested:

- Versioned FastAPI REST service and generated OpenAPI schema
- Official MCP Python SDK with stdio, SSE, and streamable HTTP transports
- Multi-tenant organizations, workspaces, principals, API keys, and capabilities
- Peppered one-way API-key and lock-token storage
- Canonical episodes and explicit durable memories
- Asynchronous job worker with retry and stale-job recovery
- Deterministic classification and optional OpenAI-compatible classification,
  with model output validated, bounded and never able to fail indexing
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
- Operations views in the console: agent activity, memory health, retrieval
  quality and service health
- Backends view in the console: graph stores and builders in use, reachable
  and working, with the steps to switch
- Pluggable graph store: PostgreSQL, FalkorDB and Neo4j behind one interface,
  verified by one contract test suite run against real servers in CI
- Derived memory links and recall statistics, rebuildable with
  `albert-admin rebuild-links`
- Workspace, principal, API-key listing, issuance, and revocation commands
- 166 unit and integration tests for retrieval, graph behavior, isolation,
  sensitivity, ingestion, deletion, locks, links, recall statistics, the
  recorder and the console API; Playwright coverage of every console screen
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
  PostgreSQL (default), FalkorDB and Neo4j stores ship. A Graphiti builder is
  not built; it is on hold until a self-hosted model is available. See
  [graph backends](GRAPH_BACKENDS.md).
- The default hashing embedder avoids downloads but is lexical rather than
  semantic. Operators must select `sentence-transformers` or an OpenAI-compatible
  embedding endpoint for semantic retrieval.
- Model classification is optional; deterministic behavior remains available
  when no external model is configured.

## Known gaps

- The regex graph builder finds little in ordinary prose; a useful graph needs
  the LLM classifier.
- Switching graph stores does not migrate existing edges; memories must be
  re-indexed.
- FalkorDB and Neo4j writes are not transactional with the memory they came
  from.
- The console has no login of its own; access is controlled by the network it
  is published on.
- Neo4j is exercised in CI, not yet in a long-running deployment.

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
