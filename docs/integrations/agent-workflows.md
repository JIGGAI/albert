# Standard agent workflows

These recipes keep retrieval useful without turning every interaction into
unbounded logging.

## Read-only question

1. Call `memory_search` with the question, `project_ref`, and a small limit.
2. Verify recalled claims against current authoritative state when relevant.
3. Answer the question. Do not create working memory or write a memory solely
   because a search occurred.

Use `memory_assemble_context` instead when the model needs a bounded text block
and structured citations.

## Shared-resource mutation

1. Search for prior decisions, warnings, and procedures.
2. Start working memory with the resource type and reference.
3. Acquire a lock associated with the working-memory id.
4. Perform the work, renewing the lease before expiry.
5. Update progress at handoff-worthy milestones, not after every command.
6. Store only durable discoveries.
7. Verify the result and complete working memory. Completion releases its locks.

If lock acquisition returns a conflict, stop before mutation. A lease is a
coordination boundary, not an advisory message to ignore.

## Incident diagnosis

1. Search for the service, symptom, exact error, and recent procedures.
2. Start working memory for a long incident so status survives handoffs.
3. Inspect current health and logs before proposing a cause.
4. Store a `lesson` only after the root cause is verified.
5. Store a `procedure` when the recovery is repeatable.
6. Store a `warning` when a tempting action is unsafe.

Never paste secret-bearing logs or complete dumps into memory.

## Ingestion versus explicit memory

Use `memory_remember` when the agent already knows the exact durable statement
that should be retrieved later. Use `memory_ingest` when a canonical source
episode should be preserved and asynchronously classified, embedded, and linked
to the graph.

An accepted episode can remain temporarily absent from semantic or graph
results while the worker processes its enrichment job. Lexical and explicit
memory workflows do not require pretending that enrichment is synchronous.

## Corrections and deletion

Use REST `PATCH /v1/memories/{id}` to correct an existing memory when identity
and provenance should be preserved. Use `memory_forget` or REST `DELETE` when a
memory must be removed. Deletion also invalidates derived relationships tied to
that memory.

Do not add a contradictory replacement and leave a known-bad memory active
unless history explicitly requires both facts with temporal validity.

## Scope convention

Choose a stable vocabulary and reuse it:

- `project_ref`: repository, customer project, or long-lived application
- `task_ref`: issue, ticket, or bounded task
- `run_ref`: one execution or automation run (REST only in the current MCP tool
  signatures)
- `workspace_id`: hard tenant partition selected by the credential or REST body

Narrow scope improves relevance and reduces accidental cross-project recall.
