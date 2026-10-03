# Claude Desktop and Claude Code integration

## Claude Desktop with local stdio

Add Albert to `claude_desktop_config.json`:

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

Typical configuration locations are:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Use a dedicated, least-privilege Albert key. If the key must be present in the
JSON file, restrict file permissions and never commit or paste the file into a
conversation. Restart Claude Desktop after changing the file.

## Claude Code

Connect to a protected streamable-HTTP gateway:

```bash
claude mcp add --transport http --scope user \
  albert https://memory.internal.example/mcp
claude mcp list
```

Configure Claude Code's supported authorization-header or secret mechanism to
send the agent's Albert API key as `Authorization: Bearer <key>`.

For local stdio, register the command through JSON:

```bash
claude mcp add-json --scope user albert \
  '{"type":"stdio","command":"/absolute/path/to/albert-mcp","args":["--transport","stdio"],"env":{"ALBERT_API_URL":"http://127.0.0.1:8080","ALBERT_MCP_API_KEY":"REPLACE_FROM_SECRET_STORE"}}'
claude mcp get albert
```

Use `/mcp` inside Claude Code to inspect connection status. Each remote caller's
bearer key is forwarded to Albert for capability checks and audit attribution.

Put the operating rules from [AGENTS.example.md](AGENTS.example.md) into the
instruction file used by the project so tool use is consistent.

Reference: [Anthropic's Claude Code MCP documentation](https://docs.anthropic.com/en/docs/claude-code/mcp).
