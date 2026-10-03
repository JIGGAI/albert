# Albert

Albert is an independent, multi-tenant memory service for AI agents. It exposes
the same authorization-aware memory capabilities through REST and MCP and
combines PostgreSQL full-text search, pgvector semantic retrieval, and a
temporal entity/relationship graph.

The project is under active construction. The authoritative design and delivery
plan are in [`docs/MEMORY_PLATFORM.md`](docs/MEMORY_PLATFORM.md).

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

REST documentation is available at `http://localhost:8080/docs` and MCP uses
streamable HTTP at `http://localhost:8081/mcp`.

## Independence

Albert is a clean, standalone system. It does not access, depend on, synchronize
with, or deploy to TenHost.

