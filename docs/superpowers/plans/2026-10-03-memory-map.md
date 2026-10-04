# Memory Map Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the console Explorer into a 2D memory map with stored links, named clusters, live recall/store pulses, replay, search, and a full-content reading pane.

**Architecture:** The worker writes `similar` and `sequence` links when it indexes a memory; a housekeeping pass turns counted search traces into recall rows, per-memory stats and `recalled` links. The console API serves a cached, clustered map snapshot, full memory detail and an operator search. The console replaces the three.js Explorer with a canvas map (`react-force-graph-2d`), a markdown reading pane and a live/replay bar fed by the existing SSE trace stream.

**Tech Stack:** Python 3.12, SQLAlchemy 2, Alembic, pgvector; Next.js 15, React 19, `react-force-graph-2d`, `react-markdown`, Playwright.

**Spec:** `docs/superpowers/specs/2026-10-03-memory-map-design.md`

## Global Constraints

- Traces and their summaries hold ids, scores, timings and query text only. `memory_ids` and `stored_ids` are ids.
- Link computation never fails indexing; failures are logged and recorded on the job's `link` span.
- Console searches (`/v1/console/*`) never count toward recall statistics.
- Settings (prefix `ALBERT_`): `LINK_NEIGHBORS` 5, `LINK_SIMILARITY_MIN` 0.78, `HOUSEKEEPING_SECONDS` 60, `MAP_NODE_LIMIT` 1500 (hard cap 3000).
- Near duplicate threshold 0.97 is a UI constant, not a link kind.
- Map titles: memory `subject` with leading `#` and whitespace stripped; fallback first non-empty content line; at most 120 characters.
- Reading pane content cap 50,000 characters with a `truncated` flag.
- All REST handlers stay sync `def`. `ruff` clean at 100 columns. Console `eslint` and `tsc --noEmit` clean.
- e2e harness ports stay 18080/18082; housekeeping interval 1 s in the harness.

## Review Focus

1. A memory re-indexed after an edit must lose its old `similar` links and gain current ones, never accumulate both — Task 2 `test_reindex_replaces_similar_links`.
2. Deleting a memory must remove its links, recalls and stats (cascade) so the map never references a scrubbed memory — Task 2 `test_deleting_a_memory_removes_its_links`.
3. A console search must not inflate recall counts — Task 3 `test_console_searches_are_not_counted`.
4. Processing the same trace twice (worker restart) must not double count — Task 3 `test_recall_processing_is_idempotent_across_runs`.
5. A workspace-scoped map must not include links to memories in another workspace — Task 5 `test_map_is_scoped_to_workspace`.

---

### Task 1: Tables, settings, migration 0005

**Files:** modify `src/albert/models.py`, `src/albert/config.py`, `.env.example`; create `migrations/versions/0005_memory_links.py`; test `tests/test_links_model.py`.

**Interfaces — Produces:** models `MemoryLink(id, organization_id, workspace_id, source_memory_id, target_memory_id, kind, weight, count, created_at, updated_at)` unique `(source_memory_id, target_memory_id, kind)`; `MemoryRecall(id, memory_id, organization_id, trace_id, recalled_at, rank, query, principal_id)`; `MemoryStat(memory_id pk, organization_id, recall_count, last_recalled_at)`; `ConsoleState(key pk, value JSON)`. Settings `link_neighbors`, `link_similarity_min`, `housekeeping_seconds`, `map_node_limit`.

- [ ] Write `test_link_tables_round_trip_and_cascade` (insert two memories, a link, a recall, a stat; delete one memory with the ORM `session.delete`; link/recall/stat rows are gone) and `test_link_settings_defaults`.
- [ ] Run: FAIL on missing models.
- [ ] Implement models, settings, migration (FKs `ondelete=CASCADE`), `.env.example` entries.
- [ ] Run tests; run `alembic upgrade head`, `downgrade 0004`, `upgrade head` on a scratch SQLite file.
- [ ] Commit `feat(links): link, recall and stats tables with migration 0005`.

### Task 2: Similarity and sequence links at indexing; rebuild command

**Files:** create `src/albert/links.py`; modify `src/albert/services.py` (call from `enrich_memory`, `enrich_episode`), `src/albert/admin.py` (`rebuild-links`), `tests/test_tracing.py` (job span order gains `link`); test `tests/test_links.py`.

**Interfaces — Produces:**
- `nearest_memories(session, memory, *, k, minimum) -> list[tuple[UUID, float]]` best chunk-pair cosine per other active memory in the same organization, workspace and embedding model, sorted descending. PostgreSQL uses `<=>`; other dialects compute cosine in Python.
- `refresh_similar_links(session, memory) -> int` deletes every `similar` link touching the memory, inserts its nearest as canonical pairs (smaller id first), returns count.
- `link_sequence(session, memory) -> int` for a memory whose episode `source_uri` file name contains `YYYY-MM-DD`: links the previous dated memory in the same directory (by episode `occurred_at`) to it with kind `sequence`.
- `refresh_links_for_memory(session, memory) -> dict` runs both inside `span("link")`, swallowing and logging errors, returning `{"similar": n, "sequence": n}`.
- `rebuild_links(session, organization_id=None) -> dict` clears `similar` and `sequence` links in scope and recomputes for every active memory.
- CLI `albert-admin rebuild-links [--organization NAME]`.

- [ ] Tests: `test_similar_memories_are_linked_and_unrelated_are_not`, `test_reindex_replaces_similar_links`, `test_links_stay_inside_a_workspace`, `test_dated_entries_form_a_sequence`, `test_deleting_a_memory_removes_its_links`, `test_link_failure_does_not_fail_indexing` (monkeypatch `nearest_memories` to raise; job completes; `link` span status error), `test_rebuild_links_command`.
- [ ] Run: FAIL (`albert.links` missing).
- [ ] Implement; update the job span assertion to `["classify", "chunk", "embed", "write_edges", "link"]`.
- [ ] Run full suite + ruff. Commit `feat(links): similarity and sequence links written at indexing`.

### Task 3: Recall processing from traces

**Files:** modify `src/albert/api.py` (`memory_ids`, `stored_ids` notes), `src/albert/links.py` (`process_recall_traces`), `src/albert/worker.py` (housekeeping call, interval setting, prune); test `tests/test_recalls.py`.

**Interfaces — Produces:** trace `summary.memory_ids: list[str]` on search/context; `summary.stored_ids` on `POST /v1/memories` and `POST /v1/episodes`; `process_recall_traces(session, *, batch=500) -> int` counting only names `POST /v1/search` and `POST /v1/context/assemble`, statuses `ok`/`degraded`, cursor in `ConsoleState["recall_cursor"]` as `"<written_at iso>|<trace id>"`, pairs among the top 5 memory hits; `COUNTED_TRACE_NAMES`.

- [ ] Tests: `test_search_trace_summary_lists_memory_ids`, `test_store_trace_summary_lists_stored_ids`, `test_recall_processing_counts_hits_and_pairs`, `test_recall_processing_is_idempotent_across_runs`, `test_console_searches_are_not_counted` (insert a trace named `POST /v1/console/search` with hits; nothing counted), `test_old_recalls_are_pruned_with_traces`.
- [ ] Run: FAIL. Implement. Run suite. Commit `feat(links): recall statistics and recalled-together links from traces`.

### Task 4: Clusters and names

**Files:** create `src/albert/clusters.py`; test `tests/test_clusters.py`.

**Interfaces — Produces:** `cluster_nodes(node_ids: list[str], links: list[tuple[str, str, float]], *, min_size=3) -> dict[str, int]` (node id to cluster index, clusters numbered by descending size then smallest member id; unclustered nodes absent); `name_clusters(titles: dict[str, str], assignment: dict[str, int]) -> dict[int, str]`.

- [ ] Tests: two dense groups joined by one weak link become two clusters; result identical across runs and input order; groups under 3 are unclustered; names use distinctive terms (`"Brand Voice"`-like) and never a stop word or a bare number; fallback `Cluster 1`.
- [ ] Run: FAIL. Implement. Commit `feat(console): deterministic clustering and cluster names`.

### Task 5: Console map API

**Files:** create `src/albert/console_map.py` (router); modify `src/albert/api.py` (include), `src/albert/console.py` (detail endpoint moves here); test `tests/test_console_map.py`.

**Interfaces — Produces:** endpoints exactly as in the spec: `GET /v1/console/workspaces`, `GET /v1/console/map`, `GET /v1/console/memories/{id}` (full), `POST /v1/console/search`. `clean_title(subject, content) -> str`.

- [ ] Tests: `test_workspaces_lists_names_and_counts`, `test_map_returns_titled_nodes_links_and_clusters`, `test_map_is_scoped_to_workspace`, `test_map_cap_and_truncated_flag`, `test_map_cache_follows_new_links`, `test_memory_detail_returns_full_content_related_and_recalls`, `test_console_search_ranks_hits_and_is_audited`, `test_map_endpoints_require_console_capability`.
- [ ] Run: FAIL. Implement. Update the slice-one detail test (content no longer 500). Commit `feat(console): map snapshot, full memory detail and operator search`.

### Task 6: Map view

**Files:** console: remove `components/Graph3D.tsx`, `components/ExplorerPanel.tsx`, `components/TimeSlider.tsx`, `components/DetailPanel.tsx`, `lib/highlight.ts` usage in Explorer; create `app/map/page.tsx`, `components/MapView.tsx`, `components/MapPanel.tsx`, `components/MapToolbar.tsx`, `lib/map.ts`; modify `app/explorer/page.tsx` (redirect), `components/Nav.tsx`, `app/traces/[id]/page.tsx`, `lib/api.ts`, `lib/types.ts`, `package.json` (drop `react-force-graph-3d`, `three`, `@types/three`; add `react-force-graph-2d`, `react-markdown`); e2e `map.spec.ts` replaces `explorer.spec.ts` and `empty.spec.ts` is updated.

**Interfaces — Produces:** `MapPanel({ trace? })` owning tenant/workspace selection, snapshot loading, filters and selection; `MapView({ snapshot, filters, colorBy, selectedId, highlight, pulses, searchHits, onSelect })`; test ids `map-canvas`, `map-node-count`, `map-link-count`, `map-cluster-count`, `map-empty`, `map-legend`.

- [ ] e2e (seed two topical groups of memories through the API): map shows nodes, links > 0, clusters ≥ 1 with a non-fallback name; workspace picker present; empty tenant message; link-kind toggle changes the link count.
- [ ] Run: FAIL. Implement. Lint, tsc, e2e. Commit `feat(console): 2D memory map with clusters and typed links`.

### Task 7: Reading pane

**Files:** create `components/ReadingPane.tsx`; e2e `reading.spec.ts`.

**Interfaces:** `ReadingPane({ memoryId, onNavigate, onClose })`; test ids `reading-pane`, `reading-content`, `reading-related`, `reading-recalls`. Selecting a node is also possible by `?memory=<id>` on `/map`, which the e2e uses instead of clicking canvas pixels.

- [ ] e2e: a 2,000-character markdown memory is shown in full, its heading rendered as a heading element, related list non-empty and clicking one changes `?memory=`.
- [ ] Run: FAIL. Implement. Commit `feat(console): reading pane with full content, related and recall history`.

### Task 8: Live pulses, replay, search

**Files:** create `components/ReplayBar.tsx`, `components/MapSearch.tsx`; modify `MapPanel`, `MapView`; e2e `live.spec.ts` addition and `mapsearch.spec.ts`.

**Interfaces:** test ids `map-pulse-count`, `map-live-state`, `replay-event-count`, `map-search-input`, `map-search-hit-count`.

- [ ] e2e: with the map open, an API search makes `map-pulse-count` increase; switching to replay shows `replay-event-count` > 0; a map search shows hit count > 0 and opens the top hit in the reading pane.
- [ ] Run: FAIL. Implement. Commit `feat(console): live recall pulses, replay and search on the map`.

### Task 9: Docs and packaging

- [ ] `docs/API_AND_MCP.md` (new endpoints, summary fields), `docs/DEPLOYMENT.md` (rebuild-links after upgrade, settings), `docs/STATUS.md`, `docs/SECURITY.md` (title/content rule change), `.env.example`, `e2e/run.sh` (`ALBERT_HOUSEKEEPING_SECONDS=1`).
- [ ] Full verification: ruff, pytest, eslint, tsc, `next build`, all e2e, migration round trip. Commit `docs: memory map`.
