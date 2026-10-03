# OpenClaw integration

OpenClaw can register Albert as a local stdio server or as a protected remote
streamable-HTTP server.

## Local stdio

```bash
openclaw mcp add albert \
  --command /absolute/path/to/albert-mcp \
  --arg=--transport \
  --arg=stdio \
  --env ALBERT_API_URL=http://127.0.0.1:8080 \
  --env ALBERT_MCP_API_KEY=REPLACE_FROM_SECRET_STORE

openclaw mcp doctor albert --probe
```

Configure OpenClaw's secret-backed HTTP authorization setting to send the
caller's Albert API key as `Authorization: Bearer <key>`.

Prefer an OpenClaw-supported secret reference over a literal key when managing
the production configuration.

## Protected remote endpoint

```bash
openclaw mcp add albert \
  --url https://memory.internal.example/mcp \
  --transport streamable-http

openclaw mcp doctor albert --probe
```

Equivalent direct configuration:

```json5
{
  mcp: {
    servers: {
      albert: {
        url: "https://memory.internal.example/mcp",
        transport: "streamable-http",
        enabled: true
      }
    }
  }
}
```

Use `openclaw mcp status --verbose` for a configuration summary and
`openclaw mcp reload` after a change when the active runtime owns the
connection. The remote gateway forwards each caller's bearer key, preserving
its Albert principal, capabilities, and audit identity.

Apply [AGENTS.example.md](AGENTS.example.md) as the durable operating policy for
agents that receive these tools.

Reference: [OpenClaw's MCP connection guide](https://docs.openclaw.ai/tools/mcp).
