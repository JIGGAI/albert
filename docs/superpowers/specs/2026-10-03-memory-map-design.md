# Memory map: links, clusters, live activation, reading pane

Status: approved in conversation 2026-10-03
Scope: slice two of the Albert console, part A (the map). Part B (operations
views: agent activity, memory health, service health, retrieval quality) is a
separate spec built on the tables introduced here.

## Intent

The first Explorer showed unconnected dots and a 500-character stub of each
memory. On the first real deployment (236 memories, no LLM classifier) it
answered neither "what does this memory hold?" nor "what are agents doing with
it?". The memories do relate: measured on that deployment, keeping each
memory's three nearest neighbours above 0.78 cosine similarity connects 97 of
98 team memories into 3 clusters and 120 of 138 engineering notes into 4.

The map makes that structure visible, makes every memory readable, and shows
recall and storage as they happen. Inspiration: the "real-time memory
visualization" pattern (typed nodes, typed weighted links, pulsing activation
over SSE, a details pane, a retrieval timeline, progressive disclosure).

### Decisions already made

- 2D map with labelled clusters; the 3D view and its dependency are removed.
- Map and live activation ship together.
- No entity layer in this build. Entity links become one more link kind later.
- All four operations views follow in part B.
- The console stays operators-only.

### Success criteria

- Opening the map on a tenant with content shows clusters with names and links
  between related memories without any LLM configured.
- Any memory can be read in full, formatted, with its source, related memories
  and recall history.
- A search or store by an agent is visible on the map within about a second.
- The last hour or day can be replayed.
- Typing a question in the map's search box shows where the results sit.

## Derived data (new, rebuildable)

All of it is derived from memories, chunks and traces and can be dropped and
rebuilt with `albert-admin rebuild-links`.

### memory_links

`id, organization_id, workspace_id, source_memory_id, target_memory_id, kind,
weight, count, created_at, updated_at`; unique on
`(source_memory_id, target_memory_id, kind)`; both memory columns cascade on
delete.

| kind | meaning | direction | weight |
|---|---|---|---|
| `similar` | nearest neighbours by chunk-embedding cosine similarity, same workspace and embedding model | stored once per pair, smaller id first | best chunk-pair similarity |
| `sequence` | consecutive dated entries from the same source directory | earlier to later | 1.0 |
| `recalled` | returned together among the top results of the same search | stored once per pair | number of co-recalls |

`similar` links are written when a memory is indexed: its
`ALBERT_LINK_NEIGHBORS` (default 5) nearest memories at or above
`ALBERT_LINK_SIMILARITY_MIN` (default 0.78). A pair at or above 0.97 is a near
duplicate; the UI draws it differently, no separate kind. Re-indexing a memory
replaces its `similar` links. Because a memory's links are computed when it is
indexed, an older memory does not gain a link merely because a newer one would
now be among its nearest; `rebuild-links` recomputes the exact union.

`sequence` links apply to episode-derived memories whose source file name
contains a date (`YYYY-MM-DD`): each links to the previous dated entry in the
same directory.

### memory_recalls and memory_stats

`memory_recalls(id, memory_id, organization_id, trace_id, recalled_at, rank,
query, principal_id)` gets one row per memory per counted search.
`memory_stats(memory_id, organization_id, recall_count, last_recalled_at)` is
the running total the map sizes nodes by.

A worker housekeeping pass (interval `ALBERT_HOUSEKEEPING_SECONDS`, default 60)
reads traces newer than a stored cursor. Counted traces are `POST /v1/search`
and `POST /v1/context/assemble` with status ok or degraded; console searches are
never counted. For each, the memory hits in the `fuse` span update stats and
recalls, and every pair among the top 5 hits increments a `recalled` link.
`memory_recalls` rows older than the trace retention are pruned.

`console_state(key, value)` holds the cursor.

### Trace summary additions

Still ids only. Search and context-assembly summaries gain `memory_ids` (final
memory hits, at most 50). Memory and episode creation summaries gain
`stored_ids`, as do the worker's indexing job traces (which is where an episode's memory first exists). The SSE feed therefore carries what the map needs to pulse nodes
without a second request.

## Clusters

Computed per map snapshot from `similar` links by deterministic label
propagation (nodes in id order, ties to the smallest label, at most 20 rounds).
Groups smaller than 3 are left unclustered. A cluster's name is its two most
distinctive title terms by TF-IDF across clusters, after removing stop words,
numbers and tenant-generic words; fallback `Cluster n`.

## Console API (all `console.read`, operator scope)

- `GET /v1/console/workspaces?organization_id=` lists workspaces with memory
  counts.
- `GET /v1/console/map?organization_id=&workspace_id=&limit=` returns
  `nodes[{id, title, type, team, role, sensitivity, recalls, last_recalled_at,
  created_at, cluster}]`, `links[{source, target, kind, weight}]`,
  `clusters[{id, label, size}]`, `truncated`. Default 1,500 nodes, maximum
  3,000, newest first. Cached per tenant data version, which now includes links
  and stats. One audit event per load.
- `GET /v1/console/memories/{id}` returns the full content (up to 50,000
  characters, `truncated` flag), source uri and path, team, role, chunk count,
  `related[{id, title, type, kind, weight}]` and
  `recalls{count, last, recent[{trace_id, at, query, rank, principal_id}]}`.
  Audited per read.
- `POST /v1/console/search` `{organization_id, workspace_id?, query, limit}` runs
  hybrid search across all sensitivities for that scope and returns ranked hits
  with titles and backends. Audited; excluded from recall statistics.

**Rule change from slice one:** the map carries memory titles and the reading
pane full content. Slice one labelled graph nodes by type only. The console is
operators-only, every content read is audited, and traces still never hold
subjects or content.

## Console UI

`/map` replaces `/explorer` (which redirects). Renderer: `react-force-graph-2d`
on canvas; panels are DOM.

- **Nodes:** hexagons. Fill by memory type, with a "color by" switch for team,
  sensitivity or age. Radius grows with the log of recall count. A ring marks
  confidential and restricted memories.
- **Links:** `similar` thin, opacity by weight, doubled when a near duplicate;
  `sequence` with an arrowhead; `recalled` dashed in the graph-backend hue,
  width by count. Each kind can be switched off.
- **Clusters:** a soft hull behind each cluster with its name; names are always
  visible, node titles appear past a zoom threshold or on hover with a short
  preview.
- **Live:** an SSE subscription pulses nodes named in `memory_ids` (recall) and
  `stored_ids` (store); a stored memory not yet on the map triggers a refetch.
  A counter and a ticker of the latest events sit in the bottom bar.
- **Replay:** the bottom bar switches from live to a scrubber over the last
  hour or day; play, pause and speed; events replay as pulses; an event opens
  its trace.
- **Reading pane:** right side, resizable, default about 460 px. Title, chips
  for type, team, role and sensitivity, source path, dates; the content as
  formatted markdown (no raw HTML); related memories, each jumping to and
  centring that node; recall history linking to trace replay.
- **Search:** runs the console search; hits are ringed with rank badges and
  listed in the pane.
- **Filters:** team, role, type, link kinds, "recalled only"; tenant and
  workspace pickers.
- Trace replay's side panel uses the same map with the trace's candidates and
  hits highlighted.
- Reduced motion is honoured; empty and capped states are explained.

## Error handling

- Link computation failing never fails indexing: it is logged and recorded on
  the job trace's `link` span.
- A map over the node cap says so and keeps the newest memories.
- The reading pane shows a truncated notice past 50,000 characters.
- Stream loss falls back to showing "paused" and reconnects.

## Testing

- pytest: nearest-memory selection and replacement, sequence linking, recall
  processing (cursor, pair counting, console searches ignored), cluster
  determinism and naming, map shape, cap and cache invalidation, full-content
  detail, console search scope and audit, rebuild command.
- Playwright on the isolated harness: clusters and links render with counts,
  full content beyond 500 characters reads as formatted text, related jump,
  search highlights, a live pulse after an API search, replay loads events,
  workspace switch, empty tenant.

## Deployment

Migration `0005_memory_links`. After upgrade: `albert-admin rebuild-links` once.
New settings in `.env.example`. The 3D dependency is removed from the console.

## Out of scope

Operations views (part B), entity links, per-operator login, tenant self-serve.
