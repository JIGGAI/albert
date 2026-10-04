# Albert

Albert is an independent, multi-tenant memory service for AI agents. It exposes
the same authorization-aware memory capabilities through REST and MCP and
combines PostgreSQL full-text search, pgvector semantic retrieval, and a
temporal entity/relationship graph.

The project is under active construction. The authoritative design and delivery
plan are in [`docs/MEMORY_PLATFORM.md`](docs/MEMORY_PLATFORM.md).

Embedding and classifier choices are documented in
[`docs/PROVIDERS.md`](docs/PROVIDERS.md).

## Development

```bash
cp .env.example .env
docker compose up --build
```

After startup, create the first organization, workspace, and administrator key:

```bash
docker compose exec api albert-admin bootstrap \
  --organization "Example" --workspace "Default" --principal "Administrator"
```

The command prints the API key once. Store it securely.

REST documentation is available at `http://localhost:8080/docs`, MCP uses
streamable HTTP at `http://localhost:8081/mcp`, and the operator console (live
traces, replay, memory map) runs at `http://localhost:8082` once
`ALBERT_CONSOLE_API_KEY` is set (see [deployment](docs/DEPLOYMENT.md)). HTTP MCP clients authenticate
with their own Albert API key as a bearer credential.

For client setup, drop-in agent instructions, ordinary workflows, multi-agent
handoffs, REST recipes, and troubleshooting, see the
[`docs/integrations` handbook](docs/integrations/README.md).

## Independence

Albert is a clean, standalone system with its own infrastructure, credentials,
database, and product identity.
