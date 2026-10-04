export type TraceStatus = "ok" | "error" | "degraded";

export interface TraceSummary {
  id: string;
  kind: "request" | "job";
  name: string;
  organization_id: string | null;
  principal_id: string | null;
  status: TraceStatus;
  http_status: number | null;
  started_at: string;
  duration_ms: number;
  summary: Record<string, unknown> & {
    query?: string;
    hits?: number;
    degraded?: string[];
    memory_ids?: string[];
    stored_ids?: string[];
  };
}

export interface Span {
  name: string;
  seq: number;
  started_offset_ms: number;
  duration_ms: number;
  status: "ok" | "error";
  detail: Record<string, unknown>;
}

export interface TraceDetail extends TraceSummary {
  spans: Span[];
}

export interface Candidate {
  id: string;
  kind: "memory" | "relationship";
  score: number;
  rank: number;
}

export interface FusedHit {
  id: string;
  kind: "memory" | "relationship";
  rank: number;
  rrf: Record<string, number>;
  backends: string[];
}

export type LinkKind = "similar" | "sequence" | "recalled";

export interface MapNode {
  id: string;
  title: string;
  type: string;
  team: string | null;
  role: string | null;
  sensitivity: string;
  workspace_id: string | null;
  recalls: number;
  last_recalled_at: string | null;
  created_at: string;
  cluster: number | null;
}

export interface MapLink {
  source: string;
  target: string;
  kind: LinkKind;
  weight: number;
}

export interface MapCluster {
  id: number;
  label: string;
  size: number;
}

export interface MapSnapshot {
  nodes: MapNode[];
  links: MapLink[];
  clusters: MapCluster[];
  truncated: boolean;
}

export interface Workspace {
  id: string;
  name: string;
  memories: number;
}

export interface RelatedMemory {
  id: string;
  title: string;
  type: string;
  kind: LinkKind;
  weight: number;
  direction: "next" | "previous" | null;
}

export interface RecallEvent {
  trace_id: string;
  at: string;
  query: string;
  rank: number;
  principal_id: string | null;
}

export interface MemoryDetail {
  id: string;
  title: string;
  subject: string;
  content: string;
  truncated: boolean;
  memory_type: string;
  sensitivity: string;
  workspace_id: string | null;
  organization_id: string;
  created_at: string;
  valid_from: string | null;
  valid_until: string | null;
  embedding_model: string | null;
  chunk_count: number;
  team: string | null;
  role: string | null;
  source_uri: string | null;
  path: string | null;
  related: RelatedMemory[];
  recalls: { count: number; last: string | null; recent: RecallEvent[] };
}

export interface SearchStep {
  name: "lexical" | "vector" | "graph" | "fuse";
  status: "ok" | "error";
  started_offset_ms: number;
  duration_ms: number;
  candidates: number;
  error: string | null;
}

export interface ConsoleSearchResult {
  hits: MapSearchHit[];
  degraded: string[];
  path: SearchStep[];
  duration_ms: number;
  trace_id: string | null;
}

export interface MapSearchHit {
  id: string;
  kind: "memory" | "relationship";
  memory_id: string | null;
  rank: number;
  title: string;
  type: string;
  sensitivity: string;
  score: number;
  backends: string[];
}

export interface Organization {
  id: string;
  name: string;
  memories: number;
  entities: number;
  relationships: number;
}

export interface Overview {
  traces_per_minute: { minute: string; count: number; errors: number; degraded: number }[];
  jobs: Record<string, number>;
  oldest_pending_seconds: number | null;
  trace_count: number;
  traces_dropped: number;
  worker_last_seen: string | null;
}

export interface AgentActivity {
  window_hours: number;
  bucket_hours: number;
  principals: {
    id: string;
    name: string;
    type: string | null;
    organization_id: string | null;
    requests: number;
    searches: number;
    stores: number;
    errors: number;
    zero_hit_searches: number;
    avg_hits: number;
    last_seen: string | null;
  }[];
  series: { bucket: string; recalls: number; stores: number; errors: number }[];
  truncated: boolean;
}

export interface CountLabel {
  label: string;
  count: number;
}

export interface MemoryHealth {
  total: number;
  by_type: CountLabel[];
  by_sensitivity: CountLabel[];
  by_workspace: { id: string | null; name: string; count: number }[];
  never_recalled: number;
  unindexed: number;
  unlinked: number;
  near_duplicate_pairs: number;
  added: { day: string; count: number }[];
  top_recalled: { id: string; title: string; recalls: number; last: string | null }[];
  near_duplicates: {
    a: { id: string; title: string };
    b: { id: string; title: string };
    similarity: number;
  }[];
}

export interface ServiceHealth {
  window_hours: number;
  endpoints: {
    kind: "request" | "job";
    name: string;
    count: number;
    errors: number;
    degraded: number;
    p50_ms: number | null;
    p95_ms: number | null;
  }[];
  totals: { requests: number; errors: number; error_rate: number; job_runs: number };
  jobs: {
    by_status: Record<string, number>;
    oldest_pending_seconds: number | null;
    recent_failures: {
      id: string;
      job_type: string;
      status: string;
      attempts: number;
      error: string | null;
      at: string | null;
    }[];
  };
  traces: { stored: number; dropped: number; retention_days: number; sample_rate: number };
  worker_last_seen: string | null;
  database_bytes: number | null;
  config: {
    embedding_provider: string;
    embedding_model: string;
    classifier: string;
    classification_model: string | null;
    graph_store: string;
  };
  truncated: boolean;
}

export interface RetrievalQuality {
  window_hours: number;
  searches: number;
  errors: number;
  zero_hit: number;
  zero_hit_rate: number;
  degraded: number;
  degraded_rate: number;
  avg_hits: number;
  p50_ms: number | null;
  p95_ms: number | null;
  backends: {
    name: "lexical" | "vector" | "graph";
    hit_share: number;
    solo_share: number;
    p50_ms: number | null;
    p95_ms: number | null;
    failures: number;
  }[];
  zero_hit_queries: { query: string; count: number; last: string | null }[];
  slowest: { trace_id: string; query: string; duration_ms: number; hits: number; at: string | null }[];
  truncated: boolean;
}

export interface BackendOption {
  id: string;
  name: string;
  summary: string;
  choose_when: string;
  available: boolean;
  active: boolean;
  enable: string[];
}

export interface StoreBackend extends BackendOption {
  status: {
    reachable: boolean;
    version: string | null;
    latency_ms: number | null;
    error: string | null;
  } | null;
  counts: { entities: number; edges: number } | null;
}

export interface Backends {
  stores: StoreBackend[];
  builders: (BackendOption & { model: string | null })[];
  vectors: {
    provider: string;
    model: string;
    dimensions: number;
    chunks: number;
    memories_embedded: number;
    memories_total: number;
  };
  activity: {
    window_hours: number;
    bucket_hours: number;
    indexing_jobs: number;
    indexing_failures: number;
    extraction_failures: number;
    edges_written: number;
    edges_closed: number;
    write_failures: number;
    write_p50_ms: number | null;
    write_p95_ms: number | null;
    classify_p50_ms: number | null;
    classify_p95_ms: number | null;
    graph_queries: number;
    graph_candidates: number;
    graph_failures: number;
    graph_query_p50_ms: number | null;
    graph_query_p95_ms: number | null;
    series: { bucket: string; edges_written: number }[];
    truncated: boolean;
  };
}
