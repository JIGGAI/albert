import type {
  AgentActivity,
  ApiKeyRow,
  Backends,
  ConnectionLinkRow,
  ConsoleScope,
  Credentials,
  ConsoleSearchResult,
  MapSnapshot,
  MemoryDetail,
  MemoryHealth,
  Organization,
  Overview,
  RetrievalQuality,
  ServiceHealth,
  MemoryOutcome,
  SettingsWorkspace,
  SettingsWorkspaces,
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
/** A workspace picker's value as query parameters (see lib/scope.ts). */
export type WorkspaceParams = { workspace_id?: string; shared_only?: string };

export const getScope = () => get<ConsoleScope>("/scope");
export const getMap = (params: { organization_id: string; limit?: number } & WorkspaceParams) =>
  get<MapSnapshot>("/map", params);
export const getMemory = (id: string) => get<MemoryDetail>(`/memories/${id}`);

export const getAgentActivity = (params: {
  organization_id?: string;
  workspace_id?: string;
  hours: number;
}) => get<AgentActivity>("/ops/agents", params);
export const getMemoryHealth = (params: { organization_id: string } & WorkspaceParams) =>
  get<MemoryHealth>("/ops/memory", params);
export const getServiceHealth = (params: { hours: number }) =>
  get<ServiceHealth>("/ops/service", params);
export const getRetrievalQuality = (params: {
  organization_id?: string;
  workspace_id?: string;
  hours: number;
}) => get<RetrievalQuality>("/ops/retrieval", params);

export const getBackends = (params: { hours: number }) => get<Backends>("/backends", params);

// Credentials live outside /v1/console: they need keys.manage, not console.read.
const settings = "/api/albert/v1/settings";

async function send<T>(method: "POST" | "PATCH" | "DELETE", path: string, body?: unknown): Promise<T> {
  const response = await fetch(`${settings}${path}`, {
    method,
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  if (!response.ok) {
    const detail = await response.text();
    let message = detail;
    try {
      const parsed = JSON.parse(detail).detail;
      message = typeof parsed === "string" ? parsed : JSON.stringify(parsed);
    } catch {}
    throw Object.assign(new Error(message || `${response.status}`), { status: response.status });
  }
  return (response.status === 204 ? undefined : await response.json()) as T;
}

export async function getCredentials(): Promise<Credentials> {
  const response = await fetch(`${settings}/credentials`, { cache: "no-store" });
  if (!response.ok) {
    throw Object.assign(new Error(`${response.status} ${await response.text()}`), {
      status: response.status,
    });
  }
  return (await response.json()) as Credentials;
}
export const createApiKey = (body: {
  name: string;
  capabilities: string[];
  workspace_id?: string;
}) => send<ApiKeyRow & { credential: string }>("POST", "/api-keys", body);
export const revokeApiKey = (id: string) => send<void>("DELETE", `/api-keys/${id}`);
export const createConnectionLink = (body: {
  name: string;
  capabilities: string[];
  expires_in_seconds: number;
  workspace_id?: string;
}) => send<ConnectionLinkRow & { url: string }>("POST", "/connection-links", body);
export const revokeConnectionLink = (id: string) => send<void>("DELETE", `/connection-links/${id}`);
export async function listSettingsWorkspaces(): Promise<SettingsWorkspaces> {
  const response = await fetch(`${settings}/workspaces`, { cache: "no-store" });
  if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
  return (await response.json()) as SettingsWorkspaces;
}
export const createWorkspace = (name: string) =>
  send<SettingsWorkspace>("POST", "/workspaces", { name });
export const renameWorkspace = (id: string, name: string) =>
  send<SettingsWorkspace>("PATCH", `/workspaces/${id}`, { name });
export const deleteWorkspace = (id: string, memories: MemoryOutcome, to?: string) =>
  send<{ records: Record<string, number>; credentials: Record<string, number> }>(
    "DELETE",
    `/workspaces/${id}?${new URLSearchParams(to ? { memories, to } : { memories })}`,
  );

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
