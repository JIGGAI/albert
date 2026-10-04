import type { GraphSnapshot, Organization, Overview, TraceDetail, TraceSummary } from "./types";

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
export const getGraph = (params: {
  organization_id: string;
  workspace_id?: string;
  temporal_as_of?: string;
  limit?: number;
}) => get<GraphSnapshot>("/graph", params);
export const getOverview = () => get<Overview>("/overview");
export const listOrganizations = () => get<{ items: Organization[] }>("/organizations");
export const getMemory = (id: string) => get<Record<string, unknown>>(`/memories/${id}`);
export const getEntity = (id: string) => get<Record<string, unknown>>(`/entities/${id}`);

export function openTraceStream(onEvent: (trace: TraceSummary) => void): () => void {
  const source = new EventSource(`${base}/stream`);
  source.addEventListener("trace", (event) => {
    onEvent(JSON.parse((event as MessageEvent<string>).data) as TraceSummary);
  });
  return () => source.close();
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
