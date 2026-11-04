# Deployment guide

## Requirements

- Docker Engine with the Compose plugin
- A Linux host with at least 4 GB RAM for the default stack
- A DNS name and TLS reverse proxy for any non-loopback deployment
- Random database and API-key-pepper secrets

## First deployment

```bash
git clone https://github.com/JIGGAI/albert.git
cd albert
cp .env.example .env
```

Replace these values in `.env`:

```dotenv
ALBERT_POSTGRES_PASSWORD=<random database password>
ALBERT_API_KEY_PEPPER=<at least 32 random characters>
```

Select the embedding and optional model-classification mode described in
[Embedding and classification providers](PROVIDERS.md) before the first start.
Albert's immutable initial migration uses 384 embedding dimensions. Keep
`ALBERT_EMBEDDING_DIMENSIONS=384` unless you add and test a deliberate schema
migration for another dimension.

Do not reuse the database password as the API-key pepper. Start the database,
migration, provider validation, API, and worker first:

```bash
docker compose up -d --build db migrate api worker
docker compose ps
curl --fail http://127.0.0.1:8080/v1/health/ready
```

Create the first organization, workspace, principal, and administrator key:

```bash
docker compose exec api albert-admin bootstrap \
  --organization "Example" \
  --workspace "Default" \
  --principal "Administrator"
```

The API key is printed once. Store it in a password manager or secret manager,
then start the HTTP MCP gateway:

```bash
docker compose up -d mcp
```

Each HTTP MCP client sends its own Albert API key in the `Authorization: Bearer`
header. `ALBERT_MCP_API_KEY` is used only when launching a local stdio MCP
process, where there is no HTTP request carrying caller identity.

Endpoints:

- REST API: `http://127.0.0.1:8080`
- OpenAPI UI: `http://127.0.0.1:8080/docs`
- MCP streamable HTTP: `http://127.0.0.1:8081/mcp`

## Memory console

The console is an operator UI shipped with Albert: a live feed of every request
and worker job, a step-by-step replay of any trace with the fusion math behind
each result, and a memory map: every memory as a node, linked to the ones it
resembles, follows or is recalled with, gathered into named clusters, pulsing
as agents recall and store. Selecting a memory opens it in full. An Operations
page reports who is using memory, what it holds, how well retrieval is working
and how the service itself is doing. It sees what its key can see: give the
console principal no workspace to see the whole organization, a workspace to
limit it to that workspace and the organization-wide memories it shares, or
give the key `admin` if one console should see every tenant on the instance.
The side menu always says which.

Issue it a key on a dedicated service principal, then start it:

```bash
docker compose exec api albert-admin create-principal \
  --organization "Example" --name "Console" --principal-type service
docker compose exec api albert-admin create-key \
  --principal-id <console principal id> --name console \
  --capabilities console.read,memory.read,keys.manage
# put the printed key in .env as ALBERT_CONSOLE_API_KEY, then:
docker compose up -d console
```

`keys.manage` powers the console's **Settings** page. It creates, renames and
deletes workspaces (deleting asks whether the workspace's memories become
organization-wide, move to another workspace, or are deleted too; the last
needs `memory.delete` on the console key), and issues and revokes agent
credentials two ways, each held to a workspace you pick or to the whole
organization:

- **One-time connection link.** Pick capabilities and an expiry; the link is
  shown once and mints one key, capped at those capabilities, the first time an
  agent (or Albert Cloud's *Connect external Albert*) redeems it. Used,
  revoked and expired links answer 410.
- **Least-privilege API key.** Pick capabilities; the key is shown once.

Every issued credential gets its own principal so it can be audited and revoked
alone. Neither path can grant `admin` or `keys.manage`, and nothing is issued
wider than the console's own key: a workspace-scoped console issues only into
its workspace, cannot grant `memory.write.organization`, and cannot create
workspaces. Leave `keys.manage` off the console key to keep the console
read-only; Settings then explains how to enable it.

Links and redeemed connections advertise `ALBERT_PUBLIC_BASE_URL` (REST, and
the host the link points at) and `ALBERT_PUBLIC_MCP_URL`. Set both to the
addresses agents actually reach before handing a link to anything that is not
on this machine.

The key is optional when Compose parses the file, so the first
`docker compose up` works before any key exists. After changing it, recreate the
service so it picks up the new value:

```bash
docker compose up -d --force-recreate console
```

Open `http://127.0.0.1:8082`. The key stays inside the console container; the
browser only ever talks to the console. Like the API and MCP, the console binds
to loopback by default; put it behind the same TLS reverse proxy before exposing
it, and treat anyone who can reach it as an operator.

The console answers only to the host names in `ALBERT_CONSOLE_ALLOWED_HOSTS`
(loopback by default), so a page on another site cannot reach it by pointing
its own DNS name at your machine. When you publish it under a name, add that
name, for example
`ALBERT_CONSOLE_ALLOWED_HOSTS=albert-console.internal.example.com`; requests
under any other name get `421`. Settings changes must also come from a console
page itself: a write whose `Origin` is missing or names another site gets `403`.

After upgrading an install that already holds memories, build the map's links
once (new memories are linked as they are indexed):

```bash
docker compose exec api albert-admin rebuild-links
```

`ALBERT_LINK_NEIGHBORS` (5) and `ALBERT_LINK_SIMILARITY_MIN` (0.78) control how
many `similar` links a memory gets and how alike two memories must be; rerun
`rebuild-links` after changing them. `ALBERT_HOUSEKEEPING_SECONDS` (60) is how
often recall counts catch up with traces, and `ALBERT_MAP_NODE_LIMIT` (1500) is
how many of the newest memories one map draws.

Traces hold ids, scores, timings and the search query text, never memory
subjects or content. `ALBERT_TRACE_SAMPLE_RATE` (default `1.0`) is the dial
for busy installs; error and degraded traces are always kept.
`ALBERT_TRACE_RETENTION_DAYS` (default `14`) bounds the table; the worker's
housekeeping pass prunes older traces about once a minute.

## Upgrading within 0.4

Migrations `0006`–`0013` run with the usual `migrate` step; none needs a manual
backfill. Behaviour to know about:

- Workspace-scoped keys now also read the organization-wide memories (those
  with no workspace), which every workspace shares. To keep something private
  to one workspace, store it there. Writing or changing shared memories from a
  workspace key needs `memory.write.organization`.
- A `console.read` key now sees only its own organization (and workspace).
  Give a console key `admin` if it must see every tenant.
- Migration `0013` records each trace's workspace and backfills request traces
  from their principal; older job traces stay organization-wide.
- The console now answers only to the host names in
  `ALBERT_CONSOLE_ALLOWED_HOSTS` (loopback by default). If you open it under a
  DNS name, add that name to `.env` before upgrading, or it answers `421`.

## Upgrading to 0.4

1. Back up, then pull and rebuild: `docker compose up -d --build`. Migration
   `0005` adds the link, recall and statistics tables.
2. Build links for the memories you already have:
   `docker compose exec api albert-admin rebuild-links`.
3. Optional: to build the knowledge graph with a model, set
   `ALBERT_LLM_PROVIDER`, `ALBERT_OPENAI_API_KEY` and
   `ALBERT_CLASSIFICATION_MODEL`, restart `api` and `worker`, then re-index:
   `docker compose exec api albert-admin reindex-memories --all`
   (add `--workspace NAME` to limit it). Each memory is one model call.
4. Optional: to move the graph to FalkorDB or Neo4j, follow
   [graph backends](GRAPH_BACKENDS.md). Existing edges are not migrated.

The console's Explorer is replaced by Map; `/explorer` redirects.

## Upgrading to 0.3

Migration `0003` moves embeddings from `memories` to a `memory_chunks` table and
clears every memory's `embedding_model`. After `alembic upgrade head`, queue the
re-embedding once:

```bash
docker compose run --rm api albert-admin reindex-memories
```

Lexical and graph retrieval work immediately; vector results return as the
worker processes the queue.

## Semantic embeddings

The dependency-free `hashing` provider is suitable for development and exact
vocabulary overlap, but it is not a semantic model. Production semantic search
should use one of:

1. An OpenAI-compatible embedding service:

   ```dotenv
   ALBERT_EMBEDDING_PROVIDER=openai-compatible
   ALBERT_EMBEDDING_MODEL=<model-name>
   ALBERT_EMBEDDING_DIMENSIONS=384
   ALBERT_OPENAI_BASE_URL=https://provider.example/v1
   ALBERT_OPENAI_API_KEY=<secret>
   ```

2. A custom image that installs Albert's `semantic` extra and selects
   `sentence-transformers`:

   ```dockerfile
   FROM ghcr.io/jiggai/albert:VERSION
   USER root
   RUN pip install 'albert-memory[semantic]'
   USER albert
   ```

   ```dotenv
   ALBERT_EMBEDDING_PROVIDER=sentence-transformers
   ALBERT_EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
   ALBERT_EMBEDDING_DIMENSIONS=384
   ```

The configured dimensions are part of the database schema. Changing dimensions
requires a migration and complete re-embedding. Changing models without changing
dimensions still requires clearing and rebuilding embeddings because unrelated
vector spaces must never be compared.

## Model-assisted classification

Albert uses deterministic classification and relationship patterns by default.
For richer entity and relationship extraction, configure an OpenAI-compatible
chat-completions service:

```dotenv
ALBERT_LLM_PROVIDER=openai-compatible
ALBERT_CLASSIFICATION_MODEL=<model-name>
ALBERT_OPENAI_BASE_URL=https://provider.example/v1
ALBERT_OPENAI_API_KEY=<secret>
```

Provider output is parsed into a constrained schema before it reaches storage.
Never configure an untrusted endpoint for confidential memories.

## TLS and network exposure

Compose binds REST and MCP to `127.0.0.1` by default. Before exposing Albert:

- Bind container ports to loopback or an internal network.
- Put REST and MCP behind a TLS reverse proxy.
- Limit request body size at the proxy as well as the application.
- Apply network rate limiting and connection limits.
- Do not expose PostgreSQL publicly.
- Store `.env` outside source control with mode `0600`.
- Set `ALBERT_API_BIND` or `ALBERT_MCP_BIND` only when the reverse proxy or
  private network requires a non-loopback listener.

## Upgrades

CI builds standard and semantic images from the same pull-request merge tree
after the backend, migration, graph-store, console, and browser gates pass. A
main release publishes both lanes from one checked-out revision:

- `ghcr.io/jiggai/albert@sha256:...` is the standard runtime;
- `ghcr.io/jiggai/albert-semantic@sha256:...` includes the pinned
  `semantic` extra without inheriting from an older Albert image;
- both BuildKit outputs carry SBOM and provenance attestations; and
- `release-bundle.json` binds both immutable digests to the source revision,
  Git tree, compatibility manifest, model identity, embedding dimensions, and
  artifact-test result.

The bundle uses the versioned `albert-release-bundle/v1` contract and is itself
signed with a GitHub artifact attestation. Guarded deployment must verify it
before promotion:

```bash
scripts/release-bundle.py verify release-bundle.json \
  --revision <40-character-Albert-revision> \
  --tree <40-character-Albert-tree>
```

Verification fails closed if an image or evidence reference is missing, a
reference is tag-based instead of digest-pinned, revisions differ, or the two
compatibility manifests differ. JIGGAI/albert-cloud must consume each
`images.<lane>.reference` independently; record both rollback digests so one
lane can roll back without changing the other. Continue to require a database
backup, reviewed migrations, and post-deploy readiness checks.

```bash
./scripts/backup.sh backups/albert-before-upgrade.dump
git pull --ff-only
docker compose build
docker compose run --rm migrate
docker compose up -d api worker mcp console
curl --fail http://127.0.0.1:8080/v1/health/ready
```

Review release notes and migrations before every upgrade. Test restores on a
separate environment periodically.

### Compatibility manifest (v1)

Every image publishes what it supports, so automation can decide upgrades and
rollbacks without inspecting Alembic or the image layout:

```bash
docker run --rm --entrypoint albert-admin ghcr.io/jiggai/albert@sha256:... compat --json
```

The command needs no environment variables and no database. It prints one line
of compact JSON (`--json` is accepted for symmetry; JSON is the only format),
described by `contracts/compatibility-v1.schema.json`:

```json
{"compatibility_manifest":"1","package":"albert-memory","package_version":"0.4.0","contracts":{"rest_api":["v1"],"capture":["1"],"export_format":["albert-export-v1"]},"migrations":{"heads":["0015"],"known_revisions":["0001","0002","0003","0004","0005","0006","0007","0008","0009","0010","0011","0012","0013","0014","0015"]}}
```

- `contracts.rest_api`, `contracts.capture`, `contracts.export_format`: the REST
  API, capture contract, and `/v1/export` bundle format versions this image
  serves.
- `migrations.heads`: the Alembic head revision(s), sorted.
- `migrations.known_revisions`: every revision the image ships, base to head.

**Rollback rule:** a database migrated by image T can run image O if and only if
every revision in T's `migrations.heads` appears in O's
`migrations.known_revisions`. Otherwise restore the pre-upgrade backup instead
of rolling back.

If the image cannot read its migrations it exits non-zero, prints nothing on
stdout, and writes
`{"compatibility_manifest":"1","error":{"code":"migrations_unavailable","message":"..."}}`
on stderr; never treat that as a partial manifest. The image locates migrations
through `/app/alembic.ini` (its `script_location`, resolved relative to the ini
file); `ALBERT_MIGRATIONS_DIR` overrides this for custom layouts.

Stability: within manifest version `1`, existing fields are never renamed,
removed, or retyped. New fields (for example additional contract entries) may
be added and the schema extended to match, so consumers should ignore keys they
do not recognise. Any incompatible change ships as `compatibility_manifest`
`"2"` with a new schema.

## Identity administration

Use `albert-admin create-workspace`, `create-principal`, `list-principals`,
`create-key`, `list-keys`, and `revoke-key` inside the API container, or the
console's Workspaces and Settings pages for workspaces and per-agent credentials. Prefer
workspace-scoped principals for ordinary agents: they read their workspace and
the shared organization-wide memories. Keep organization-scoped principals for
administration, cross-workspace retrieval and imports, and grant
`memory.write.organization` only to the agents that should publish knowledge
every workspace sees.

### Admin CLI contract (v1)

Automation should pass `--json` to `create-key`, `create-principal`,
`create-workspace`, `list-principals`, `list-keys`, and `revoke-key`, and run
`albert-admin contract` to discover the contract version, package version, and
the commands that accept `--json`. In JSON mode each command prints exactly one
compact JSON object and a newline on stdout, always carrying
`"admin_contract": "1"`. The shape of every output is defined in
[`contracts/admin-v1.schema.json`](../contracts/admin-v1.schema.json) (JSON
Schema draft 2020-12, one `$defs` entry per command plus `error`). Ids are UUID
strings and timestamps are ISO-8601 UTC or `null`. `create-key` returns the
credential once; `list-keys` never returns hashes or credentials.

```bash
docker compose exec api albert-admin list-principals --organization "Acme" --json
```

On failure a JSON-mode command writes
`{"admin_contract":"1","error":{"code":"...","message":"..."}}` to stderr and
exits with code 2. Codes: `organization_not_found`, `workspace_not_found`,
`principal_not_found`, `api_key_not_found`, `unknown_capabilities`,
`invalid_principal_type`, and `invalid_id` (a malformed UUID).

Within v1 changes are additive only: new fields, commands, or error codes may
appear, so consumers must ignore what they do not know. Removing or retyping a
field requires a new contract version. The tab-separated text output is for
people and is not a contract; parse `--json` instead.

## Backups and restores

Create a database backup:

```bash
./scripts/backup.sh backups/albert-$(date +%F).dump
```

Restore only into a stopped or isolated Albert deployment:

```bash
docker compose stop api worker mcp
CONFIRM_ALBERT_RESTORE=yes ./scripts/restore.sh backups/albert-2026-10-02.dump
docker compose up -d api worker mcp
```

The restore script replaces database contents. Back up external object storage
separately when that optional backend is introduced.
