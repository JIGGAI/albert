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
each result, and a 3D explorer of entities, memories and edges with a time
slider. It sees every tenant, so it is for operators, not end users.

Issue it a key on a dedicated service principal, then start it:

```bash
docker compose exec api albert-admin create-principal \
  --organization "Example" --name "Console" --principal-type service
docker compose exec api albert-admin create-key \
  --principal-id <console principal id> --name console \
  --capabilities console.read,memory.read
# put the printed key in .env as ALBERT_CONSOLE_API_KEY, then:
docker compose up -d console
```

Open `http://127.0.0.1:8082`. The key stays inside the console container; the
browser only ever talks to the console. Like the API and MCP, the console binds
to loopback by default; put it behind the same TLS reverse proxy before exposing
it, and treat anyone who can reach it as an operator.

Traces hold ids, scores, timings and the search query text, never memory
subjects or content. `ALBERT_TRACE_SAMPLE_RATE` (default `1.0`) is the dial
for busy installs; error and degraded traces are always kept.
`ALBERT_TRACE_RETENTION_DAYS` (default `14`) bounds the table; the worker prunes
nightly.

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

```bash
./scripts/backup.sh backups/albert-before-upgrade.dump
git pull --ff-only
docker compose build
docker compose run --rm migrate
docker compose up -d api worker mcp
curl --fail http://127.0.0.1:8080/v1/health/ready
```

Review release notes and migrations before every upgrade. Test restores on a
separate environment periodically.

## Identity administration

Use `albert-admin create-workspace`, `create-principal`, `list-principals`,
`create-key`, `list-keys`, and `revoke-key` inside the API container. Prefer
workspace-scoped principals for ordinary agents and organization-scoped
principals only for administration, cross-workspace retrieval, and imports.

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
