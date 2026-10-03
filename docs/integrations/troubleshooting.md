# Integration troubleshooting

## Establish the failing layer

Check the REST service first:

```bash
curl --fail-with-body -sS http://127.0.0.1:8080/v1/health/live
curl --fail-with-body -sS http://127.0.0.1:8080/v1/health/ready
docker compose ps
```

Then inspect only the relevant service logs:

```bash
docker compose logs --tail=200 api
docker compose logs --tail=200 mcp
docker compose logs --tail=200 worker
docker compose logs --tail=200 db
```

The default MCP URL is `http://127.0.0.1:8081/mcp`.

## Client sees no Albert tools

- Confirm the MCP server name and transport.
- For stdio, use an absolute executable path and confirm the client process can
  see `ALBERT_API_URL` and `ALBERT_MCP_API_KEY`.
- For HTTP, confirm the URL ends in `/mcp`, is reachable, and sends the caller's
  Albert key as its bearer credential.
- Restart the client or reload its MCP catalog after configuration changes.
- Codex: run `codex mcp list`.
- Claude Code: run `claude mcp list` and inspect `/mcp`.
- OpenClaw: run `openclaw mcp doctor albert --probe`.

## MCP starts and immediately exits

`ALBERT_MCP_API_KEY` is required for stdio. With stdio, protocol messages use stdout, so
diagnostics or wrappers must not print banners to stdout. Ensure the command
runs in an environment where the installed `albert-mcp` version matches the
deployed API.

## Authentication or authorization errors

- `401`: the Albert key is absent, malformed, expired, or revoked.
- `403`: the principal lacks a required capability or requested sensitivity.
- `404` on a known id can intentionally hide a resource outside the caller's
  organization, workspace, or sensitivity access.

For HTTP MCP, the connecting client sends its own Albert API key. For stdio,
the local MCP process reads `ALBERT_MCP_API_KEY` from its environment.

## Search returns weak matches

- Add `project_ref` or `task_ref` to narrow the corpus.
- Check the response's `degraded` list.
- The default hashing embedder is dependency-free but lexical, not a true
  semantic model. Configure `sentence-transformers` or an OpenAI-compatible
  embedding provider for semantic retrieval.
- Confirm the worker processed newly ingested episodes.
- Use subject terms and entity names that are present in the corpus when testing
  lexical and graph retrieval.

## Newly ingested episode is not in vector or graph results

Episode enrichment is asynchronous. Check the worker and the episode's
`enrichment_status` through `GET /v1/episodes/{id}`. Investigate
`enrichment_error` rather than repeatedly ingesting the same content.

## Lock conflict or stale lease

- Treat `409` as an active coordination conflict and stop mutation.
- Verify the resource type and reference match exactly across agents.
- Renew before expiration with the current token and fence.
- Never retry renewal with an older fence after another owner has reacquired the
  resource.
- Completing or failing associated working memory releases its active locks.

## HTTP MCP is reachable by unintended users

The gateway rejects requests without a bearer key and binds to loopback by
default. If it is unexpectedly reachable, restore the loopback/firewall policy,
confirm TLS termination, rotate any exposed keys, and review audit events.

## Deeper checks

Use `/docs` on the REST service to compare the deployed OpenAPI contract with
the request being sent. See [Deployment](../DEPLOYMENT.md),
[Security](../SECURITY.md), and [REST and MCP integration](../API_AND_MCP.md) for
the service-level configuration and boundaries.
