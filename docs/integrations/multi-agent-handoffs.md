# Multi-agent coordination and handoffs

Albert separates durable knowledge from mutable coordination:

- durable memories record facts, decisions, procedures, lessons, and warnings;
- working memory records current task state and expires; and
- locks serialize mutation of a named shared resource.

## Identity model

Give each independently trusted automation its own Albert principal or key when
possible. This improves revocation, capability scoping, and audit attribution.
Do not share a human administrator key.

The current HTTP MCP gateway collapses all callers behind its single
`ALBERT_MCP_API_KEY`. For distinct audit identity, use separate gateway
instances and keys, or connect each agent through local stdio with its own key.

## Starting work

Use a globally recognizable task id such as:

```text
<agent>-<UTC timestamp>-<short task name>
```

Start working memory with the intended resource. Acquire the lock separately
and associate `working_memory_id` when the caller needs the returned token for
renewal. Keep the token and fence in ephemeral process state only.

## Progress payload

A useful `working_memory_update` payload is small and machine-readable:

```json
{
  "phase": "verification",
  "completed": ["updated docs", "checked links"],
  "next": ["run tests", "commit"],
  "risks": [],
  "commit": null
}
```

Do not place credentials, raw private content, or lock tokens in progress.

## Handoff protocol

The outgoing agent should:

1. update progress with completed work, exact next action, verification, and
   known risk;
2. avoid completing the working record if the task continues;
3. release the lock if the successor cannot inherit the same ephemeral token;
   and
4. send the successor the working-memory id, never the credential or lock token
   through durable memory.

The incoming agent should:

1. search durable memory for project context;
2. use the outgoing agent's handoff summary and working-memory id;
3. verify current external state;
4. acquire a fresh lock before mutation; and
5. continue updating the same working record when ownership permits.

Albert 0.1 does not expose a read/list endpoint for working-memory records, so
the handoff summary remains necessary even when an id is supplied.

## Fencing behavior

Each reacquisition advances the resource's fence. A stale agent may still hold
an old secret token in memory, so downstream systems guarded by Albert locks
should record and reject writes with an older fence. The token proves lease
possession to Albert; the monotonic fence protects external writers from stale
owners.

## Completion and failure

Use `working_memory_complete` through MCP when the outcome is finished. REST
clients can use `/complete` or `/fail`. Both terminal transitions release
associated active locks. Consolidation notes should summarize outcome and
verification; durable lessons belong in normal memories.
