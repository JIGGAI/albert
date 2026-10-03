# Cursor integration

Cursor reads MCP definitions from `.cursor/mcp.json` for one project or
`~/.cursor/mcp.json` for all projects.

## Local stdio

```json
{
  "mcpServers": {
    "albert": {
      "command": "/absolute/path/to/albert-mcp",
      "args": ["--transport", "stdio"],
      "env": {
        "ALBERT_API_URL": "http://127.0.0.1:8080",
        "ALBERT_MCP_API_KEY": "REPLACE_FROM_SECRET_STORE"
      }
    }
  }
}
```

## Protected remote endpoint

```json
{
  "mcpServers": {
    "albert": {
      "url": "https://memory.internal.example/mcp",
      "headers": {
        "Authorization": "Bearer REPLACE_FROM_SECRET_STORE"
      }
    }
  }
}
```

Do not commit a project-level file containing a credential. Prefer Cursor's
secret-backed environment substitution when available; the gateway forwards
the bearer key so each client keeps its own Albert principal and audit identity.

After saving the configuration, open Cursor's MCP settings and verify that the
Albert tools appear. Add the contents of
[AGENTS.example.md](AGENTS.example.md) to the project's agent rules, adapted to
the rule-file format used by that Cursor version.

Reference: [Cursor's MCP documentation](https://docs.cursor.com/context/model-context-protocol).
