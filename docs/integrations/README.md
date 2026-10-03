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

## Important deployment boundary

Albert's current streamable-HTTP MCP process uses one server-side
`ALBERT_MCP_API_KEY` for all callers. It does not authenticate individual MCP
callers. Therefore:

- use stdio for a single local client; or
- expose `/mcp` only inside a private, authenticated network boundary; and
- run separate gateways with separate Albert principals when caller-level
  isolation is required.

Do not place the current `/mcp` endpoint directly on the public internet. A
public multi-user service needs OAuth or per-request identity forwarding before
the MCP gateway can enforce caller-specific authorization.
