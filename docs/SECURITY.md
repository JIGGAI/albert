# Security model

## Trust boundaries

Albert treats imported content, model output, MCP clients, API clients, and
optional model providers as separate trust boundaries. Authentication does not
make supplied memory content trustworthy.

## Tenant isolation

- Every canonical and derived row carries an organization identifier.
- Workspace-scoped credentials cannot select another workspace. They read
  their own workspace plus the organization-wide records every workspace
  shares, and change shared records only with `memory.write.organization`.
- Retrieval applies organization and workspace constraints inside every
  lexical, vector, and graph query, in every graph store.
- The console API follows the same boundary: a `console.read` key sees only
  its own organization, and only its own workspace (plus the shared memories)
  when it is workspace-scoped. Only `admin` crosses tenants. `console.read` can
  be issued from Settings and by connection links, so it is never allowed to
  reach further than the key that issued it.
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
  browser never receives it. Its proxy forwards an exact allowlist:
  `/v1/console/*` reads, the operator search, and the Settings routes for
  credentials and workspaces.
- The console proxy answers only to the hosts in `ALBERT_CONSOLE_ALLOWED_HOSTS`
  (loopback by default), which defeats DNS rebinding, and accepts a write only
  when its `Origin` is the console's own, which defeats cross-site requests.
- `keys.manage` issues credentials and is never implied by `console.read`. An
  issued credential is never wider than its issuer: a workspace-scoped manager
  issues, sees and revokes only its own workspace's credentials and cannot
  grant `memory.write.organization`. `admin` and `keys.manage` cannot be
  granted at all.
- A connection link is the only secret for an unauthenticated redeem: only its
  digest is stored, redemption is one conditional update so a link mints at
  most one key, and the key can never carry more than the link granted.
- The console map shows memory titles and its reading pane full content, across
  every sensitivity. Each map load, memory read and operator search writes an
  audit event. Traces, recall rows and link rows still hold ids, scores and
  query text only, never subjects or content.
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
- A graph builder with a store of its own is forgotten too. Under Graphiti,
  deleting a memory or episode, retention expiry and workspace deletion queue a
  retried job that removes Graphiti's episode, the facts it created and the
  entities only it mentioned, and clears the summaries of entities it shared,
  since those may repeat it. A build that finishes after its memory was
  forgotten is removed the same way.
- Traces record ids, scores, timings and search query text, never memory
  subjects or content, so the recorder is not a second copy of the corpus.
  Each trace records its workspace, and a workspace-scoped console key sees
  only its own workspace's traces.
- When the classifier raises a memory's sensitivity, the episode it came from
  is raised with it, so the source text is never easier to read than the
  memory.
- Automatic capture is filtered by a server-side profile before anything is
  stored: unlisted event classes are refused, metadata outside the profile's
  allowlist is dropped, raw bodies are refused unless the profile allows them,
  and credential-shaped values reject the event or are redacted. Repository
  remotes are kept only as a hash and worktree paths are cut to their last
  folder. Refusals are audited with a reason and no body. Captured material
  expires after the profile's retention period.
- Imported instructions are data; retrieval does not grant them authority.

## Locks

Resource locks are leases with secret tokens, expirations, and monotonic fencing
values. Releasing a lock preserves its row so reacquisition increments the fence.
Downstream writers must reject stale fencing values when locks guard external
resources.

## Known security work before public SaaS operation

- A console login. The proxy already refuses other host names and cross-origin
  writes, so a web page in the operator's own browser cannot drive it, but
  anyone who can reach the console is an operator; publish it only to
  operators.
- External security review and threat-model review
- PostgreSQL row-level security
- OIDC/OAuth for humans
- Distributed rate limiting and abuse controls
- Configurable retention and legal deletion workflows
- Signed container images and an SBOM
- Tamper-evident external audit retention
- Formal content scanning and customer-managed encryption keys
