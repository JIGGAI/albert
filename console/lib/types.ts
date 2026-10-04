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
  summary: Record<string, unknown> & { query?: string; hits?: number; degraded?: string[] };
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

export interface GraphNode {
  id: string;
  kind: "entity" | "memory";
  label: string;
  type: string;
  sensitivity: string | null;
  workspace_id: string | null;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  relation_type: string;
  sensitivity: string;
  valid_from: string;
  valid_until: string | null;
  source_memory_id: string | null;
}

export interface GraphSnapshot {
  nodes: GraphNode[];
  edges: GraphEdge[];
  truncated: boolean;
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
