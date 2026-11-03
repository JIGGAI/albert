# OpenClaw integration

Albert's supported OpenClaw path is the native lifecycle adapter. It provides
bounded cited recall and minimized capture without a core OpenClaw patch. Manual
MCP and REST connections are custom-client options and do not provide equivalent
lifecycle capture.

## Requirements and package verification

- OpenClaw `>=2026.9.6,<2027.0.0`.
- A released `albert-memory` wheel whose exact URL, SHA-256 digest, GitHub OIDC
  attestation subject, version, compatibility range, and source revision match
  the signed Cloud manifest v2.
- An HTTPS Albert Brain endpoint or a private Albert Cloud one-time link.

Install an exact version from the release channel selected by your operator. Do
not use an unversioned VCS URL or a floating branch. Before production use,
verify the downloaded wheel against its published digest and provenance:

```bash
python -m pip download --no-deps --only-binary=:all: albert-memory==0.4.0
python -m pip hash albert_memory-0.4.0-py3-none-any.whl
gh attestation verify albert_memory-0.4.0-py3-none-any.whl --repo JIGGAI/albert
python -m pip install --require-hashes -r operator-approved-albert-lock.txt
```

The release provenance names the full rollback source commit. The package
contract check rejects a wheel missing the connector, adapter, plugin runtime,
plugin manifest, ownership marker, or compatibility declaration.

## Connect from Albert Cloud

Copy the private, single-use connection link from Cloud. Avoid shell history and
process arguments by allowing the command to prompt:

```bash
albert-capture connect
albert-capture doctor --smoke
```

For managed automation, provide the link over private standard input or an
already-open private file descriptor:

```bash
secret-provider read-once albert-link | albert-capture connect --link-stdin
albert-capture connect --link-fd 3 3<operator-owned-private-link-file
```

`connect` validates the signed manifest, package provenance, host compatibility,
one-time redemption identity, Brain health, runtime plugin state, live capture,
and fresh recall. It commits only after every check passes. Before commit, a
failure compensates the newly issued credential and restores the prior
Albert-owned files and complete OpenClaw configuration.

## Direct self-hosted installation

Cloud links are preferred because they bind identity, revocation, package, and
rollback metadata. A self-hosted operator can connect directly:

```bash
albert-capture install --api-url https://brain.example.com
albert-capture doctor --smoke
```

The token is read by hidden prompt. Albert stores it in an owned `0600` file and
configures OpenClaw with a non-secret wrapper path. The token is never placed in
arguments, process environment, stdout, or logs.

## Operate the connection

### Diagnose

Run `albert-capture doctor` after host or Brain maintenance. Add `--smoke` when
an operator explicitly wants one live capture-and-recall proof. Doctor validates
the host range, OpenClaw configuration, runtime plugin, Brain readiness, and
credential without printing secret values.

### Upgrade

1. Download and verify the exact new wheel and its attestation.
2. Record the currently installed version and provenance rollback source.
3. Install the exact new version from the operator-approved hash lock.
4. Run `albert-capture connect` with a newly issued Cloud link. For direct
   self-hosting, rerun `install` and enter the replacement credential privately.
5. Run `albert-capture doctor --smoke`.

Reconnection is transactional and preserves the encrypted queue key and queued
captures. If validation fails, reinstall the exact previous wheel from its
recorded digest and source, then rerun doctor.

### Rotate or reconnect

Issue a new single-agent Cloud link, run `albert-capture connect`, and verify with
doctor. Successful commit replaces only Albert's credential and owned plugin
entry. Revoke the old credential in Cloud after proof succeeds. If redemption
fails before commit, Cloud compensation revokes the newly issued credential.

### Revoke

Revoke the OpenClaw agent credential in Cloud or the Brain administration API,
then run `albert-capture uninstall` on the host. Revocation stops remote access;
uninstall removes the local owned plugin entry and files. Perform both steps for
a retired or compromised host.

### Roll back

Use the `rollback_source` and exact wheel digest from the prior release
provenance. Install that wheel by hash, reconnect with a fresh one-time link, and
run doctor. Never reactivate an old or revoked credential. If the new release
changed server requirements, complete the server rollback first.

### Uninstall

```bash
albert-capture uninstall
```

Uninstall requires Albert's ownership marker and removes only Albert-owned
OpenClaw configuration and `~/.openclaw/albert`. It preserves unrelated plugins
and settings. Revoke the corresponding credential separately.

## Migration from manual MCP

1. Leave the manual connection in place while installing and verifying the
   native adapter.
2. Run `albert-capture doctor --smoke` and confirm cited recall in a fresh
   session.
3. Disable the manual MCP entry, reload OpenClaw, and verify the native adapter
   again.
4. Remove the manual entry and revoke its distinct credential after an operator
   observation window.

Rollback is the reverse: disable or uninstall the native adapter, restore the
previous custom-client configuration from its operator backup, and issue a new
least-privilege credential. Do not reuse the native adapter credential.

## Reduced-fidelity behavior

Albert outages do not block an OpenClaw turn. Allowed capture events enter a
bounded encrypted queue for idempotent replay. Recall may be absent or stale
until connectivity returns. The adapter never substitutes uncited memory, and
agents must treat recalled text as non-authoritative context. Queue saturation,
repeated replay failure, or failed doctor checks require operator attention.

Capture is limited to current user goals, bounded final assistant outcomes or
failure summaries, lifecycle counts, and reasons. Hidden prompts, full
transcripts, tool bodies, absolute paths, hook message arrays, and known
credential material are not sent to Albert.

## Compatibility evidence

The released wheel includes the versioned
`com.jiggai.albert.host-target` OpenClaw descriptor. The installer validates its
package provenance, exact tested host release, supported range, full hook
fidelity, owned install root, private credential transport, reload and uninstall
commands, probes, and upgrade/rotation/revoke/rollback behavior before changing
host state. Unknown schemas, versions, capabilities, target IDs, unsafe paths,
foreign ownership, or contradictory hook declarations fail closed. Other host
targets remain disabled until their own conformance tickets pass.

| OpenClaw version | Evidence | Status |
| --- | --- | --- |
| 2026.9.6 | Typed lifecycle fixture and native manifest validation | Tested |
| `>=2026.9.6,<2027.0.0` | Package and manifest v2 compatibility gates | Supported contract |
| Outside that range | Installer rejection | Unsupported |

The lifecycle fixture covers retry idempotency, ordering, cited start recall,
pre-compaction and end checkpoints, completion/failure minimization, outage
continuity, and secret non-retention.

## Advanced: manual MCP and REST clients

Use manual MCP or REST only for custom clients that intentionally do not use the
native lifecycle adapter. They provide tool calls, not automatic session recall
and capture. Current OpenClaw configuration has no documented portable secret
reference for these custom entries; do not place a durable credential in an
OpenClaw configuration file. Use an operator-controlled wrapper or credential
broker that injects a short-lived, least-privilege credential at process start.

For remote MCP, use a public HTTPS endpoint and configure the broker to send the
caller's credential as bearer authorization. For stdio, configure an absolute
path to an operator-owned wrapper that launches `albert-mcp --transport stdio`.
Keep this custom credential distinct from the native adapter credential and
reload the client after changes.

Apply [AGENTS.example.md](AGENTS.example.md) as operating policy and use the
[sanitized troubleshooting guide](troubleshooting.md) for failures.
