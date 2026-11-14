import type {
  CaptureCheckpoint, CaptureEvent, CaptureEventWrite, CaptureProfile, CaptureProfileWrite,
  CaptureSession, CaptureSessionStart, Compatibility, ContextRequest, ContextResponse, Episode,
  EpisodeCreate, Memory, MemoryCreate, MemoryUpdate, SearchRequest, SearchResponse,
  SessionContextRequest, SessionContextResponse, Telemetry, UUID, Relationship,
  RelationshipCreate, WorkingMemory, WorkingMemoryCreate, WorkingMemoryFinish,
  Lock, LockAcquire, ExportBundle, ImportResult, Json,
} from "./types.js";

const RETRYABLE = new Set([408, 425, 429, 500, 502, 503, 504]);
const REQUIRED = ["/v1/memories", "/v1/episodes", "/v1/search", "/v1/context/assemble"] as const;

export class AlbertError extends Error {
  constructor(message: string, readonly code: string, readonly status?: number, readonly requestId?: string, readonly details?: unknown) { super(message); this.name = "AlbertError"; }
}
export class AlbertHttpError extends AlbertError {
  constructor(message: string, status: number, requestId?: string, details?: unknown) { super(message, `http_${status}`, status, requestId, details); this.name = "AlbertHttpError"; }
}
export class AlbertCompatibilityError extends AlbertError {
  constructor(readonly feature: string, message = `Albert server does not support ${feature}`) { super(message, "incompatible_server"); this.name = "AlbertCompatibilityError"; }
}
export class AlbertConfigurationError extends AlbertError {
  constructor(message: string) { super(message, "invalid_configuration"); this.name = "AlbertConfigurationError"; }
}

export interface AlbertClientOptions {
  baseUrl: string;
  fetch?: typeof globalThis.fetch;
  headers?: Readonly<Record<string, string>>;
  authorization?: () => string | undefined | Promise<string | undefined>;
  retries?: number;
  retryBaseMs?: number;
  timeoutMs?: number;
  telemetry?: Telemetry;
  allowInsecure?: boolean;
}
interface RequestOptions { body?: unknown; headers?: Record<string, string> | undefined; idempotent?: boolean; timeoutMs?: number; signal?: AbortSignal | undefined }

function safeOrigin(value: string, allowInsecure: boolean): string {
  let url: URL;
  try { url = new URL(value); } catch { throw new AlbertConfigurationError("baseUrl must be an absolute URL"); }
  if (url.username || url.password || url.search || url.hash) throw new AlbertConfigurationError("baseUrl cannot contain credentials, query, or fragment");
  const local = url.hostname === "localhost" || url.hostname === "127.0.0.1" || url.hostname === "[::1]";
  if (url.protocol !== "https:" && !(url.protocol === "http:" && (allowInsecure || local))) throw new AlbertConfigurationError("baseUrl must use HTTPS outside explicit local development");
  return url.toString().replace(/\/$/, "");
}
function errorDetail(value: unknown): string | undefined {
  if (typeof value === "string") return value;
  if (value && typeof value === "object" && "detail" in value) { const detail = (value as { detail?: unknown }).detail; return typeof detail === "string" ? detail : JSON.stringify(detail); }
  return undefined;
}
function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => { const timer = setTimeout(resolve, ms); signal?.addEventListener("abort", () => { clearTimeout(timer); reject(signal.reason); }, { once: true }); });
}

export class AlbertClient {
  readonly memories;
  readonly capture;
  readonly context;
  readonly profiles;
  readonly sources;
  readonly jobs;
  readonly graph;
  readonly workingMemory;
  readonly locks;
  readonly portability;
  readonly mcp;
  private readonly baseUrl: string;
  private readonly fetcher: typeof globalThis.fetch;
  private readonly defaultHeaders: Readonly<Record<string, string>>;
  private readonly authorize?: AlbertClientOptions["authorization"];
  private readonly retries: number;
  private readonly retryBaseMs: number;
  private readonly timeoutMs: number;
  private readonly telemetry: Telemetry | undefined;
  private compatibility?: Compatibility;

  constructor(options: AlbertClientOptions) {
    this.baseUrl = safeOrigin(options.baseUrl, options.allowInsecure ?? false);
    if (!options.fetch && !globalThis.fetch) throw new AlbertConfigurationError("A Fetch implementation is required");
    this.fetcher = options.fetch ?? globalThis.fetch.bind(globalThis);
    this.defaultHeaders = Object.freeze({ ...(options.headers ?? {}) });
    this.authorize = options.authorization;
    this.retries = Math.max(0, options.retries ?? 2);
    this.retryBaseMs = Math.max(1, options.retryBaseMs ?? 100);
    this.timeoutMs = Math.max(1, options.timeoutMs ?? 30_000);
    this.telemetry = options.telemetry;
    this.memories = Object.freeze({
      create: (value: MemoryCreate, signal?: AbortSignal) => this.request<Memory>("POST", "/v1/memories", { body: value, signal }),
      get: (id: UUID, signal?: AbortSignal) => this.request<Memory>("GET", `/v1/memories/${encodeURIComponent(id)}`, { signal }),
      update: (id: UUID, value: MemoryUpdate, signal?: AbortSignal) => this.request<Memory>("PATCH", `/v1/memories/${encodeURIComponent(id)}`, { body: value, signal }),
      delete: (id: UUID, signal?: AbortSignal) => this.request<void>("DELETE", `/v1/memories/${encodeURIComponent(id)}`, { signal }),
      search: (value: SearchRequest, signal?: AbortSignal) => this.request<SearchResponse>("POST", "/v1/search", { body: value, signal }),
    });
    this.context = Object.freeze({
      assemble: (value: ContextRequest, signal?: AbortSignal) => this.request<ContextResponse>("POST", "/v1/context/assemble", { body: value, signal }),
      sessionStart: (value: SessionContextRequest, signal?: AbortSignal) => this.request<SessionContextResponse>("POST", "/v1/context/session-start", { body: value, signal }),
    });
    this.profiles = Object.freeze({
      create: (value: CaptureProfileWrite, signal?: AbortSignal) => this.request<CaptureProfile>("POST", "/v1/capture/profiles", { body: value, signal }),
      get: (key: string, signal?: AbortSignal) => this.request<CaptureProfile>("GET", `/v1/capture/profiles/${encodeURIComponent(key)}`, { signal }),
    });
    this.capture = Object.freeze({
      start: (value: CaptureSessionStart, signal?: AbortSignal) => this.request<CaptureSession>("POST", "/v1/capture/sessions", { body: value, idempotent: true, signal }),
      event: (session: UUID, value: CaptureEventWrite, signal?: AbortSignal) => this.request<CaptureEvent>("POST", `/v1/capture/sessions/${encodeURIComponent(session)}/events`, { body: value, idempotent: true, signal }),
      events: (session: UUID, events: CaptureEventWrite[], signal?: AbortSignal) => this.request<{ events: CaptureEvent[] }>("POST", `/v1/capture/sessions/${encodeURIComponent(session)}/events/batch`, { body: { version: "1", events }, idempotent: true, signal }),
      checkpoint: (session: UUID, through_sequence: number, idempotency_key: string, signal?: AbortSignal) => this.request<CaptureCheckpoint>("POST", `/v1/capture/sessions/${encodeURIComponent(session)}/checkpoints`, { body: { version: "1", through_sequence, idempotency_key }, idempotent: true, signal }),
      end: (session: UUID, final_sequence: number, idempotency_key: string, signal?: AbortSignal) => this.request<CaptureSession>("POST", `/v1/capture/sessions/${encodeURIComponent(session)}/end`, { body: { version: "1", final_sequence, idempotency_key }, idempotent: true, signal }),
    });
    this.sources = Object.freeze({
      ingest: (value: EpisodeCreate, waitSeconds = 0, signal?: AbortSignal) => this.request<Episode>("POST", "/v1/episodes", { body: value, headers: waitSeconds > 0 ? { Prefer: `wait=${waitSeconds}` } : undefined, idempotent: true, signal }),
      get: (id: UUID, waitSeconds = 0, signal?: AbortSignal) => this.request<Episode>("GET", `/v1/episodes/${encodeURIComponent(id)}`, { headers: waitSeconds > 0 ? { Prefer: `wait=${waitSeconds}` } : undefined, signal }),
      delete: (id: UUID, signal?: AbortSignal) => this.request<void>("DELETE", `/v1/episodes/${encodeURIComponent(id)}`, { signal }),
    });
    this.jobs = Object.freeze({ get: this.sources.get, wait: (id: UUID, waitSeconds = 30, signal?: AbortSignal) => this.sources.get(id, waitSeconds, signal) });
    this.graph = Object.freeze({
      add: (value: RelationshipCreate, signal?: AbortSignal) => this.request<Relationship>("POST", "/v1/relationships", { body: value, signal }),
      query: (query: string, limit = 50, signal?: AbortSignal) => this.request<Relationship[]>("POST", "/v1/graph/query", { body: { query, limit }, signal }),
      subgraph: (entityId: UUID, hops = 2, limit = 50, signal?: AbortSignal) => this.request<Relationship[]>("GET", `/v1/graph/entities/${encodeURIComponent(entityId)}/subgraph?hops=${hops}&limit=${limit}`, { signal }),
    });
    this.workingMemory = Object.freeze({
      start: (value: WorkingMemoryCreate, signal?: AbortSignal) => this.request<WorkingMemory>("POST", "/v1/working-memory", { body: value, signal }),
      update: (id: UUID, progress: Record<string, Json>, expires_in_seconds?: number, signal?: AbortSignal) => this.request<WorkingMemory>("PATCH", `/v1/working-memory/${encodeURIComponent(id)}`, { body: { progress, ...(expires_in_seconds === undefined ? {} : { expires_in_seconds }) }, signal }),
      get: (id: UUID, signal?: AbortSignal) => this.request<WorkingMemory>("GET", `/v1/working-memory/${encodeURIComponent(id)}`, { signal }),
      complete: (id: UUID, value: WorkingMemoryFinish = {}, signal?: AbortSignal) => this.request<WorkingMemory>("POST", `/v1/working-memory/${encodeURIComponent(id)}/complete`, { body: value, signal }),
      fail: (id: UUID, value: WorkingMemoryFinish = {}, signal?: AbortSignal) => this.request<WorkingMemory>("POST", `/v1/working-memory/${encodeURIComponent(id)}/fail`, { body: value, signal }),
    });
    this.locks = Object.freeze({
      acquire: (value: LockAcquire, signal?: AbortSignal) => this.request<Lock>("POST", "/v1/locks/acquire", { body: value, signal }),
      renew: (id: UUID, token: string, fence: number, ttl_seconds = 900, signal?: AbortSignal) => this.request<Lock>("POST", `/v1/locks/${encodeURIComponent(id)}/renew`, { body: { token, fence, ttl_seconds }, signal }),
      release: (id: UUID, token: string, fence: number, signal?: AbortSignal) => this.request<void>("POST", `/v1/locks/${encodeURIComponent(id)}/release`, { body: { token, fence }, signal }),
    });
    this.portability = Object.freeze({
      export: (workspaceId?: UUID, signal?: AbortSignal) => this.request<ExportBundle>("GET", `/v1/export${workspaceId ? `?workspace_id=${encodeURIComponent(workspaceId)}` : ""}`, { signal }),
      import: (bundle: ExportBundle, workspace_id?: UUID, signal?: AbortSignal) => this.request<ImportResult>("POST", "/v1/import", { body: { bundle, ...(workspace_id ? { workspace_id } : {}) }, signal }),
    });
    this.mcp = Object.freeze({
      memory_remember: this.memories.create, memory_ingest: this.sources.ingest,
      memory_get: this.memories.get, memory_update: this.memories.update,
      memory_get_episode: this.sources.get, memory_search: this.memories.search,
      memory_assemble_context: this.context.assemble, memory_forget: this.memories.delete,
      memory_forget_episode: this.sources.delete, memory_query_graph: this.graph.query,
      memory_get_subgraph: this.graph.subgraph, memory_add_relationship: this.graph.add,
      working_memory_start: this.workingMemory.start, working_memory_update: this.workingMemory.update,
      working_memory_get: this.workingMemory.get, working_memory_complete: this.workingMemory.complete,
      working_memory_fail: this.workingMemory.fail, memory_lock_acquire: this.locks.acquire,
      memory_lock_renew: this.locks.renew, memory_lock_release: this.locks.release,
      memory_export: this.portability.export, memory_import: this.portability.import,
    });
  }

  async negotiate(signal?: AbortSignal): Promise<Compatibility> {
    const document = await this.request<{ paths?: Record<string, unknown> }>("GET", "/openapi.json", { signal });
    const paths = new Set(Object.keys(document.paths ?? {}));
    for (const path of REQUIRED) if (!paths.has(path)) throw new AlbertCompatibilityError(path, `Albert server is missing required REST v1 path ${path}`);
    this.compatibility = Object.freeze({ restApi: "v1" as const, capture: "1" as const, paths });
    return this.compatibility;
  }
  async requireFeature(path: string, signal?: AbortSignal): Promise<void> {
    const value = this.compatibility ?? await this.negotiate(signal);
    if (!value.paths.has(path)) throw new AlbertCompatibilityError(path);
  }

  private async request<T>(method: string, path: string, options: RequestOptions = {}): Promise<T> {
    const safeMethod = method === "GET" || method === "HEAD" || method === "OPTIONS";
    const attempts = safeMethod || options.idempotent ? this.retries + 1 : 1;
    for (let attempt = 1; attempt <= attempts; attempt++) {
      const started = Date.now();
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(new Error("Albert request timed out")), options.timeoutMs ?? this.timeoutMs);
      const abort = () => controller.abort(options.signal?.reason);
      options.signal?.addEventListener("abort", abort, { once: true });
      try {
        const authorization = await this.authorize?.();
        const headers: Record<string, string> = { Accept: "application/json", ...this.defaultHeaders, ...(options.headers ?? {}) };
        if (authorization) headers.Authorization = authorization;
        if (options.body !== undefined) headers["Content-Type"] = "application/json";
        const init: RequestInit = { method, headers, signal: controller.signal };
        if (options.body !== undefined) init.body = JSON.stringify(options.body);
        const response = await this.fetcher(`${this.baseUrl}${path}`, init);
        const requestId = response.headers.get("x-request-id") ?? undefined;
        if (response.ok) {
          this.emit({ operation: path, method, status: response.status, attempt, duration_ms: Date.now() - started });
          if (response.status === 204) return undefined as T;
          return await response.json() as T;
        }
        let payload: unknown;
        try { payload = await response.json(); } catch { payload = undefined; }
        if (attempt < attempts && RETRYABLE.has(response.status)) {
          this.emit({ operation: path, method, status: response.status, attempt, duration_ms: Date.now() - started });
          await sleep(this.backoff(response, attempt), options.signal); continue;
        }
        throw new AlbertHttpError(errorDetail(payload) ?? `Albert API returned ${response.status}`, response.status, requestId, payload);
      } catch (error) {
        if (error instanceof AlbertError) { this.emit({ operation: path, method, status: error.status, attempt, duration_ms: Date.now() - started, error: error.code }); throw error; }
        if (attempt < attempts && !options.signal?.aborted) {
          this.emit({ operation: path, method, attempt, duration_ms: Date.now() - started, error: "network_error" });
          await sleep(this.retryBaseMs * 2 ** (attempt - 1), options.signal); continue;
        }
        const message = options.signal?.aborted ? "Albert request was aborted" : error instanceof Error ? error.message : "Albert network request failed";
        throw new AlbertError(message, options.signal?.aborted ? "aborted" : "network_error");
      } finally { clearTimeout(timeout); options.signal?.removeEventListener("abort", abort); }
    }
    throw new AlbertError("Albert request failed", "network_error");
  }
  private backoff(response: Response, attempt: number): number {
    const retry = Number(response.headers.get("retry-after"));
    return Number.isFinite(retry) && retry >= 0 ? Math.min(retry * 1000, 30_000) : this.retryBaseMs * 2 ** (attempt - 1);
  }
  private emit(event: Parameters<NonNullable<Telemetry>>[0]): void { try { this.telemetry?.(Object.freeze({ ...event })); } catch { /* telemetry must never affect requests */ } }
}
