# REST and MCP integration

## Authentication

REST requests use an Albert API key as a bearer credential:

```http
Authorization: Bearer alb_<prefix>_<secret>
```

Keys are independently revocable and capability-scoped. The server stores a
peppered HMAC digest, never the plaintext key.

## REST example

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $ALBERT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "subject": "Deployment choice",
    "content": "Albert uses PostgreSQL for canonical records.",
    "memory_type": "decision",
    "project_ref": "albert"
  }' \
  http://127.0.0.1:8080/v1/memories
```

Hybrid search:

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $ALBERT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"Where are canonical records stored?","limit":10}' \
  http://127.0.0.1:8080/v1/search
```

Search responses identify the contributing backends and disclose degradation.
Context assembly returns a bounded text block plus structured citations.

## MCP

The deployed MCP endpoint is `/mcp` using streamable HTTP. Configure a compatible
client with the URL exposed by your TLS reverse proxy. The gateway authenticates
to Albert using `ALBERT_MCP_API_KEY`; use a dedicated principal rather than a
human administrator key.

For local stdio operation:

```bash
ALBERT_API_URL=http://127.0.0.1:8080 \
ALBERT_MCP_API_KEY="$ALBERT_API_KEY" \
albert-mcp --transport stdio
```

Tools include:

- `memory_remember`
- `memory_ingest`
- `memory_get`
- `memory_search`
- `memory_assemble_context`
- `memory_forget`
- `memory_query_graph`
- `memory_get_subgraph`
- `working_memory_start`
- `working_memory_update`
- `working_memory_complete`
- `memory_lock_acquire`
- `memory_lock_renew`
- `memory_lock_release`

The MCP implementation is a client of the REST service; it has no independent
database or authorization rules.

