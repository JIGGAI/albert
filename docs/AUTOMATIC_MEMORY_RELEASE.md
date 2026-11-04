# Automatic-memory release gate

Albert 0.4.0 ships automatic-memory adapters as an explicit opt-in beta. The
machine-readable source of truth is `src/albert/automatic_memory_release.json`;
`albert-automatic-memory-release-gate` validates it and emits a content-addressed
JSON receipt. General availability remains disabled until a later release
records real opt-in deployment feedback beyond synthetic and internal fixtures.

## Supported versions

| Host | Tested | Supported contract | Fidelity |
| --- | --- | --- | --- |
| OpenClaw | 2026.9.6 | `>=2026.9.6,<2027` | Full |
| Codex CLI | 0.154.0 | `>=0.154.0,<0.155.0` | No true session-end/failure hook |
| Claude Code | 2.1.312 | `>=2.1.268,<2.2` | Failure hook is best effort on affected 2.1.x builds |
| Cursor | Desktop 3.27.22; Agent CLI 2026.10.22-27dcf5f | Tested hooks-v1 surfaces | Cloud agents lack start/end hooks |

The package contains all four adapters. Transactional `albert-capture install`,
doctor, and uninstall currently target OpenClaw only. Follow each host's guide
under `docs/integrations/` for manual installation and exact reduced-fidelity behavior.

## Golden flow

Before promoting an artifact, install its wheel into a clean environment and run:

```bash
albert-automatic-memory-release-gate --output release-gate.json
pytest -q tests/test_{openclaw,codex,claude,cursor}_adapter.py
pytest -q tests/test_capture_{sdk,operations,migration,promotion,security}.py
pytest -q tests/test_memory_safety.py tests/test_public_benchmark.py
```

The host fixtures prove start recall, minimized capture, idempotent checkpoints,
outage replay, and fail-open host behavior. Server fixtures prove consolidation,
searchability, tenant isolation, linked deletion, poisoning/secret rejection,
telemetry minimization, and migration rollback. A host version outside the table
is blocked until this matrix passes against that actual host artifact.

## SLOs and recovery bounds

- Hook command budget: 2 seconds; host work continues on timeout or outage.
- Recall injection: at most 6,000 characters, cited, delimited, and explicitly non-authoritative.
- Local recovery queue: AES-GCM encrypted, `0600`, at most 1,000 items or 8 MB,
  seven days old, and 12 delivery attempts.
- Replay uses stable operation keys. Server idempotency prevents duplicate
  sessions, events, and checkpoints after Brain, network, queue, or worker recovery.
- Queue overflow/expiry drops capture rather than blocking the host. Operators
  inspect `albert-capture doctor` and the body-free capture operations endpoint.

## Security and data handling

Use one least-privilege credential per tenant/workspace and host. Credentials
must come from a secret store or private Albert-owned credential file. The SDK
rejects known secret material before durable queueing; the server independently
enforces capture policy fail closed. Hidden prompts, full transcripts, tool
bodies, absolute paths, and raw error details are excluded. Recall is untrusted
data: poisoning and instruction-like content is quarantined or rendered as
non-authoritative evidence. Telemetry contains identifiers, counts, statuses,
versions, and bounded reasons, never captured bodies. Albert Cloud and a
self-hosted Brain are separate trust and data planes; no capture crosses them
unless an operator explicitly configures that endpoint and credential.

## Migration and rollback

Back up the database and encrypted queue keys, record the wheel/image digest,
then run `alembic upgrade head`. Verify readiness, the release-gate receipt, one
new capture/checkpoint, consolidation, recall, and deletion before widening the
beta. To roll back, stop API/workers, restore the matching database backup and
artifact, or use the reviewed Alembic downgrade only when its release notes say
the data conversion is reversible. Preserve capture provenance and workspace
identity during export/restore. Reinstalling an adapter must retain its encrypted
queue and sequencing database until replay is complete.

## Deletion

Deleting source memory, an episode, expired retained capture, or a workspace
queues derivative deletion through the recorded linkage. Confirm the source,
promoted memory, embeddings, graph projection, queue jobs, and recall results are
gone in the same tenant scope. Never satisfy deletion by hiding search output
while retaining linked derivatives. Keep only body-free audit evidence required by policy.

## Known limitations

- Codex cannot truthfully emit session-end or failure events on the tested hooks.
- Claude Code failure delivery is best effort on affected 2.1.x versions.
- Cursor cloud agents lack automatic start recall and true session-end capture.
- Only OpenClaw has the transactional one-command installer in 0.4.0.
- The public benchmark is a deterministic internal baseline, not a matched third-party comparison.

## Opt-in beta

The beta cohort is limited to synthetic and internal fixture deployments and
requires explicit operator enablement. Record artifact digest, host/version,
tenant shape, queue/latency health, recovery incidents, deletion rehearsal, and
operator feedback without captured bodies. Security, isolation, deletion,
credential, poisoning, or telemetry findings block expansion. The manifest has
no unresolved fixture launch blocker, but `general_availability` is deliberately
false until real opt-in deployments complete this feedback loop.
