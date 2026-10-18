import type {
  AgentActivity,
  Backends,
  ConsoleSearchResult,
  MapSnapshot,
  MemoryDetail,
  MemoryHealth,
  Organization,
  Overview,
  RetrievalQuality,
  ServiceHealth,
  TraceDetail,
  TraceSummary,
  Workspace,
} from "./types";

const base = "/api/albert/v1/console";

type Params = Record<string, string | number | undefined>;

function query(params?: Params): string {
  if (!params) return "";
  const entries = Object.entries(params)
    .filter(([, value]) => value !== undefined && value !== "")
    .map(([key, value]) => [key, String(value)]);
  return entries.length ? `?${new URLSearchParams(entries).toString()}` : "";
}

async function get<T>(path: string, params?: Params): Promise<T> {
  const response = await fetch(`${base}${path}${query(params)}`, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`${response.status} ${await response.text()}`);
  }
  return (await response.json()) as T;
}

export const listTraces = (params: Params) =>
  get<{ items: TraceSummary[]; next_cursor: string | null }>("/traces", params);
export const getTrace = (id: string) => get<TraceDetail>(`/traces/${id}`);
export const listWorkspaces = (organization_id: string) =>
  get<{ items: Workspace[]; unscoped_memories: number }>("/workspaces", { organization_id });
export const getMap = (params: { organization_id: string; workspace_id?: string; limit?: number }) =>
  get<MapSnapshot>("/map", params);
export const getMemory = (id: string) => get<MemoryDetail>(`/memories/${id}`);

export const getAgentActivity = (params: { organization_id?: string; hours: number }) =>
  get<AgentActivity>("/ops/agents", params);
export const getMemoryHealth = (params: { organization_id: string; workspace_id?: string }) =>
  get<MemoryHealth>("/ops/memory", params);
export const getServiceHealth = (params: { hours: number }) =>
  get<ServiceHealth>("/ops/service", params);
export const getRetrievalQuality = (params: { organization_id?: string; hours: number }) =>
  get<RetrievalQuality>("/ops/retrieval", params);

export const getBackends = (params: { hours: number }) => get<Backends>("/backends", params);

export async function searchMap(body: {
  organization_id: string;
  workspace_id?: string;
  query: string;
  limit?: number;
  include_graph?: boolean;
}): Promise<ConsoleSearchResult> {
  const response = await fetch(`${base}/search`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(`${response.status} ${await response.text()}`);
  }
  return (await response.json()) as ConsoleSearchResult;
}
export const getOverview = () => get<Overview>("/overview");
export const listOrganizations = () => get<{ items: Organization[] }>("/organizations");

export function openTraceStream(
  onEvent: (trace: TraceSummary) => void,
  onState?: (open: boolean) => void,
): () => void {
  const maxReconnects = 3;
  let source: EventSource | null = null;
  let retry: ReturnType<typeof setTimeout> | null = null;
  let reconnects = 0;
  let closed = false;

  const connect = () => {
    source = new EventSource(`${base}/stream`);
    source.addEventListener("trace", (event) => {
      onEvent(JSON.parse((event as MessageEvent<string>).data) as TraceSummary);
    });
    source.onopen = () => {
      reconnects = 0;
      onState?.(true);
    };
    source.onerror = () => {
      source?.close();
      source = null;
      onState?.(false);
      if (closed || reconnects >= maxReconnects) return;
      const delay = 250 * 2 ** reconnects;
      reconnects += 1;
      retry = setTimeout(connect, delay);
    };
  };

  connect();
  return () => {
    closed = true;
    if (retry) clearTimeout(retry);
    source?.close();
  };
}

export function shortId(id: string | null): string {
  return id ? id.slice(0, 8) : "";
}

export function relativeTime(iso: string, now = Date.now()): string {
  const seconds = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}
