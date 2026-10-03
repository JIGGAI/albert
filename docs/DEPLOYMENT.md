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

Do not reuse the database password as the API-key pepper. Start the database,
migration, API, and worker first:

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

The API key is printed once. Store it in a password manager or secret manager.
To give the MCP gateway that identity, set `ALBERT_MCP_API_KEY` in `.env` and
start the gateway:

```bash
docker compose up -d mcp
```

Endpoints:

- REST API: `http://127.0.0.1:8080`
- OpenAPI UI: `http://127.0.0.1:8080/docs`
- MCP streamable HTTP: `http://127.0.0.1:8081/mcp`

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

The Compose ports are intended for loopback development. Before exposing Albert:

- Bind container ports to loopback or an internal network.
- Put REST and MCP behind a TLS reverse proxy.
- Limit request body size at the proxy as well as the application.
- Apply network rate limiting and connection limits.
- Do not expose PostgreSQL publicly.
- Store `.env` outside source control with mode `0600`.

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
