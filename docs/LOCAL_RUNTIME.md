# One-command local runtime

`albert-local` runs Albert's API, worker, and HTTP MCP data plane on one host
without Docker. Basic mode uses SQLite and the deterministic hashing embedder;
it is intended for local evaluation and development, not production clustering
or multi-host workloads.

## Install and start

Albert requires Python 3.12 or newer on 64-bit Linux or macOS (x86-64 or
Arm64). Install the package, then run:

```bash
albert-local start
albert-local status
albert-local smoke
```

The first `start` securely bootstraps the runtime under
`$XDG_DATA_HOME/albert` (or `~/.local/share/albert`), creates a private
administrator credential, and starts the API and MCP endpoints on loopback.
The credential and API-key pepper are mode `0600`; they are never printed by
the lifecycle commands. The runtime root and logs are mode `0700`/`0600`.

Use explicit installation when selecting ports or semantic mode:

```bash
albert-local install --api-port 8080 --mcp-port 8081
albert-local install --mode semantic  # requires albert-memory[semantic]
albert-local start
```

Semantic prerequisites are checked before state is created. Both modes bind
only to `127.0.0.1`; use the regular production deployment instead of exposing
this single-node runtime to a network.

## Lifecycle

```bash
albert-local stop
albert-local backup ~/backups/albert-local.zip
albert-local restore ~/backups/albert-local.zip
albert-local upgrade
albert-local rollback
```

Backups are consistent mode-`0600` archives and include the database,
configuration, API credential, and pepper. Protect them as secrets. `upgrade`
creates a local rollback point before adopting the currently installed package
and optional mode. Stop the runtime before restore, upgrade, or rollback. A
package downgrade must be installed separately before `rollback` when release
compatibility requires the older executable as well as the restored state.
To recover onto a clean host, run `albert-local install`, stop it if running,
then restore the archive.

Uninstall preserves data by default:

```bash
albert-local uninstall
albert-local install  # re-enable preserved state
```

`albert-local uninstall --remove-data` permanently removes only a validated,
owned Albert local root. Back up first if the state may be needed again.

## Limits

- One process supervisor and one SQLite database; no clustering or HA.
- Loopback-only endpoints and one locally bootstrapped tenant/workspace.
- Basic hashing recall is deterministic but lower quality than semantic mode.
- Advanced deployments retain their existing PostgreSQL, graph, model, and
  provider choices; `albert-local` does not replace those configurations.
