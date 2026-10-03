# Security model

## Trust boundaries

Albert treats imported content, model output, MCP clients, API clients, and
optional model providers as separate trust boundaries. Authentication does not
make supplied memory content trustworthy.

## Tenant isolation

- Every canonical and derived row carries an organization identifier.
- Workspace-scoped credentials cannot select another workspace.
- Retrieval applies organization and workspace constraints inside every
  lexical, vector, and graph query.
- Episode deduplication is scoped to the calling principal, so re-posting text
  cannot be used to learn whether, or at what sensitivity, another principal
  holds it.
- Cross-tenant access is covered by automated tests.
- A future hosted deployment should add PostgreSQL row-level security as defense
  in depth after connection-pool tenant context is formally designed.

## Credentials

- API keys contain a lookup prefix and random secret.
- Only a peppered HMAC-SHA256 digest is stored.
- Keys are displayed once, scoped to capabilities, revocable, and optionally
  expiring.
- Streamable HTTP MCP requires a caller bearer key and forwards that same
  identity to REST; stdio uses its process-local `ALBERT_MCP_API_KEY`.
- Lock tokens are stored using the same one-way construction.
- The console's operator key is held by the console server process only; the
  browser never receives it. Console detail reads are audited like any read.
- Secrets belong in environment-backed secret management, never memory.

## Memory poisoning and model output

- Every memory records provenance, confidence, sensitivity, owner, and scope.
- Model-produced classifications are constrained and parsed before storage.
- Relationship extraction retains the source memory and episode.
- The built-in credential detector rejects common secret formats on every
  stored surface: memory and episode bodies, metadata, working-memory
  descriptions, progress and consolidation notes, relationship names and
  descriptions, and import bundles. It is defense in depth, not a complete
  data-loss-prevention system.
- Classification may raise a memory's sensitivity but never lower it; the
  caller-supplied level is retained in `metadata.classification`.
- Deletion scrubs content rather than only flagging rows, so a forgotten memory
  or episode is not recoverable through any read path.
- Traces record ids, scores, timings and search query text, never memory
  subjects or content, so the recorder is not a second copy of the corpus.
- Imported instructions are data; retrieval does not grant them authority.

## Locks

Resource locks are leases with secret tokens, expirations, and monotonic fencing
values. Releasing a lock preserves its row so reacquisition increments the fence.
Downstream writers must reject stale fencing values when locks guard external
resources.

## Known security work before public SaaS operation

- External security review and threat-model review
- PostgreSQL row-level security
- OIDC/OAuth for humans
- Distributed rate limiting and abuse controls
- Configurable retention and legal deletion workflows
- Signed container images and an SBOM
- Tamper-evident external audit retention
- Formal content scanning and customer-managed encryption keys
