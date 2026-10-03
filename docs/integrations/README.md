# Integration handbook

This handbook explains how to connect ordinary agent clients to Albert and how
to use memory safely in day-to-day work.

## Start here

1. Deploy Albert by following [Deployment](../DEPLOYMENT.md).
2. Choose an integration:
   - [Codex](codex.md)
   - [Claude Desktop and Claude Code](claude-desktop.md)
   - [Cursor](cursor.md)
   - [OpenClaw](openclaw.md)
   - [Direct REST clients](rest-client.md)
3. Copy [the example agent instructions](AGENTS.example.md) into the agent's
   project instructions and tailor the scope names.
4. Adopt the [standard workflows](agent-workflows.md). For concurrent agents,
   also read [multi-agent handoffs](multi-agent-handoffs.md).
5. Keep [troubleshooting](troubleshooting.md) available to operators.

## Choosing MCP or REST

| Use case | Interface |
|---|---|
| Interactive coding or assistant client | MCP |
| Application or service integration | REST |
| Bulk ingestion or custom orchestration | REST |
| Agent that already supports MCP tools | MCP |

Both interfaces reach the same FastAPI service and authorization rules. The MCP
server is a thin REST client and has no separate database.

## Organizations and workspaces

An organization is the hard tenant boundary. A workspace is an optional team,
client, or project partition inside that organization. Workspace-scoped keys
are confined automatically; organization-scoped keys can select one workspace
per request or omit it for cross-workspace operations. Records whose
`workspace_id` is `null` are organization-wide. Prefer workspace-scoped keys
for ordinary agents and reserve organization-wide identities for trusted
administration and portability operations.
Workspace-filtered retrieval returns only that workspace; organization-wide
records are returned when an organization-scoped caller omits the workspace.

## Important deployment boundary

Streamable HTTP requires an Albert API key in the caller's bearer header and
forwards that identity to REST. Give independently trusted agents separate keys
so capability checks, audit attribution, and revocation remain distinct.

The bearer scheme is appropriate for controlled self-hosting behind TLS. A
public human-facing service still needs OAuth/OIDC, rate limits, abuse controls,
and the additional protections listed in [Security](../SECURITY.md).
