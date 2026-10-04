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

export interface MapSearchHit {
  id: string;
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
