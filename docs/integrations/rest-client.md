# REST client integration

The REST API is the preferred interface for services, custom orchestrators, and
bulk workflows. Interactive OpenAPI documentation is served at `/docs`.

## Authentication and health

Set the endpoint and key in the calling process without printing either secret:

```bash
export ALBERT_API_URL=http://127.0.0.1:8080
export ALBERT_API_KEY='value-from-secret-store'

curl --fail-with-body -sS "$ALBERT_API_URL/v1/health/ready"
```

Authenticated requests use:

```http
Authorization: Bearer alb_<prefix>_<secret>
Content-Type: application/json
```

## Store and retrieve

Create a durable memory:

```bash
curl --fail-with-body -sS -X POST \
  -H "Authorization: Bearer $ALBERT_API_KEY" \
  -H 'Content-Type: application/json' \
  --data '{
    "subject": "Deployment choice",
    "content": "Albert uses PostgreSQL for canonical records.",
    "memory_type": "decision",
    "project_ref": "albert",
    "sensitivity": "internal"
  }' \
  "$ALBERT_API_URL/v1/memories"
```

Ingest a source episode for asynchronous enrichment:

```bash
curl --fail-with-body -sS -X POST \
  -H "Authorization: Bearer $ALBERT_API_KEY" \
  -H 'Content-Type: application/json' \
  --data '{
    "content": "Verified deployment or conversation content",
    "source_type": "agent",
    "source_uri": "run:example-123",
    "project_ref": "albert"
  }' \
  "$ALBERT_API_URL/v1/episodes"
```

Run hybrid search:

```bash
curl --fail-with-body -sS -X POST \
  -H "Authorization: Bearer $ALBERT_API_KEY" \
  -H 'Content-Type: application/json' \
  --data '{
    "query": "Where are canonical records stored?",
    "project_ref": "albert",
    "limit": 10,
    "include_graph": true
  }' \
  "$ALBERT_API_URL/v1/search"
```

Build a bounded context block with citations:

```bash
curl --fail-with-body -sS -X POST \
  -H "Authorization: Bearer $ALBERT_API_KEY" \
  -H 'Content-Type: application/json' \
  --data '{
    "query": "Albert storage architecture",
    "project_ref": "albert",
    "limit": 10,
    "max_characters": 12000
  }' \
  "$ALBERT_API_URL/v1/context/assemble"
```

Inspect `degraded` in search and context responses. A successful HTTP status
does not mean every retrieval backend participated.

## Working memory and locks

Start task state and atomically request its resource lock:

```bash
curl --fail-with-body -sS -X POST \
  -H "Authorization: Bearer $ALBERT_API_KEY" \
  -H 'Content-Type: application/json' \
  --data '{
    "task_id": "agent-20261002-update-docs",
    "description": "Update integration documentation",
    "resource_type": "repository",
    "resource_ref": "JIGGAI/albert",
    "expires_in_seconds": 7200,
    "acquire_lock": true
  }' \
  "$ALBERT_API_URL/v1/working-memory"
```

When `acquire_lock` succeeds, the working-memory response includes a `lock`
object with its secret token and monotonic fence. Retain both only in ephemeral
task state for renewal or explicit release. A client may still create working
memory first and call `POST /v1/locks/acquire` separately when that better fits
its workflow.

Read state with `GET /v1/working-memory/{id}` and update progress with
`PATCH /v1/working-memory/{id}`. Finish with
`POST /v1/working-memory/{id}/complete`, or record failure with
`POST /v1/working-memory/{id}/fail`. Completing or failing a working record
releases associated active locks. All four lifecycle operations are also
available through MCP.

## Graph operations

- `POST /v1/relationships` creates an explicit typed relationship.
- `POST /v1/graph/query` searches currently valid relationships.
- `GET /v1/graph/entities/{entity_id}/subgraph?hops=2&limit=50` traverses a
  bounded authorized subgraph.

## Error handling

| Status | Meaning | Client behavior |
|---|---|---|
| `401` | Missing, malformed, expired, or revoked key | Refresh the secret; do not retry blindly |
| `403` | Missing capability or unauthorized sensitivity | Use a properly scoped principal |
| `404` | Missing or deliberately hidden resource | Verify id and tenant scope |
| `409` | Lock conflict or stale lease state | Stop mutation, back off, and reconcile ownership |
| `422` | Invalid request | Correct the payload |
| `503` | Dependency unavailable | Retry with bounded exponential backoff |

Use idempotent retry rules carefully: a timeout after a create request may have
occurred after the server committed it.
