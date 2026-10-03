# Codex integration

Codex can connect to Albert over local stdio or streamable HTTP. Prefer stdio
when Codex and Albert run on the same trusted host.

## Local stdio

Add this to `~/.codex/config.toml`:

```toml
[mcp_servers.albert]
command = "albert-mcp"
args = ["--transport", "stdio"]
env = { ALBERT_API_URL = "http://127.0.0.1:8080", ALBERT_MCP_API_KEY = "REPLACE_FROM_SECRET_STORE" }
```

Use the absolute path to `albert-mcp` if it is not on the environment inherited
by Codex. Protect the config file if a key is placed in it; an environment or
secret-injection mechanism is preferable.

## Streamable HTTP

For an internal, protected endpoint:

```bash
codex mcp add albert --url https://memory.internal.example/mcp
codex mcp list
```

Equivalent configuration:

```toml
[mcp_servers.albert]
url = "https://memory.internal.example/mcp"
```

The current Albert HTTP gateway authenticates upstream with its own
`ALBERT_MCP_API_KEY`; it does not identify the Codex caller. Keep the URL on a
private network or behind a trusted access layer.

## Project instructions

Copy [AGENTS.example.md](AGENTS.example.md) to the project root as `AGENTS.md`,
then adjust project and resource naming. The MCP connection makes tools
available; the instructions tell Codex when and why to use them.

Restart the client or begin a new session after configuration changes, then ask
Codex to list Albert's tools and run a narrowly scoped `memory_search`.

Reference: [OpenAI's MCP documentation](https://developers.openai.com/learn/docs-mcp).
