import assert from "node:assert/strict";
import test from "node:test";
import { AlbertClient, AlbertCompatibilityError, AlbertConfigurationError, AlbertHttpError } from "../dist/esm/index.js";
import { BrowserAlbertClient } from "../dist/esm/browser.js";
import { ServerAlbertClient } from "../dist/esm/server.js";

const json = (value, status = 200, headers = {}) => new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json", ...headers } });

test("server credentials remain isolated per client", async () => {
  const observed = [];
  const fetch = async (url, init) => { observed.push([url, init.headers.Authorization]); return json({ hits: [], degraded: [] }); };
  const first = new ServerAlbertClient({ baseUrl: "https://one.example", apiKey: "one-secret", fetch });
  const second = new ServerAlbertClient({ baseUrl: "https://two.example", apiKey: "two-secret", fetch });
  await Promise.all([first.memories.search({ query: "one" }), second.memories.search({ query: "two" })]);
  assert.deepEqual(observed, [["https://one.example/v1/search", "Bearer one-secret"], ["https://two.example/v1/search", "Bearer two-secret"]]);
});

test("non-idempotent writes are not retried and return structured errors", async () => {
  let calls = 0;
  const client = new AlbertClient({ baseUrl: "http://127.0.0.1:8080", retries: 3, fetch: async () => { calls++; return json({ detail: "unavailable" }, 503, { "x-request-id": "req-1" }); } });
  await assert.rejects(client.memories.create({ content: "fact" }), (error) => error instanceof AlbertHttpError && error.status === 503 && error.requestId === "req-1");
  assert.equal(calls, 1);
});

test("idempotent capture is retried with the identical body", async () => {
  const bodies = [];
  const client = new AlbertClient({ baseUrl: "http://localhost:8080", retries: 1, retryBaseMs: 1, fetch: async (_url, init) => { bodies.push(init.body); return bodies.length === 1 ? json({ detail: "retry" }, 503, { "retry-after": "0" }) : json({ id: "session" }, 201); } });
  const result = await client.capture.start({ client_session_id: "client", idempotency_key: "stable" });
  assert.equal(result.id, "session");
  assert.equal(bodies.length, 2); assert.equal(bodies[0], bodies[1]);
});

test("compatibility negotiation fails with a typed actionable error", async () => {
  const client = new AlbertClient({ baseUrl: "https://brain.example", fetch: async () => json({ paths: { "/v1/memories": {} } }) });
  await assert.rejects(client.negotiate(), (error) => error instanceof AlbertCompatibilityError && error.feature === "/v1/episodes");
});

test("browser client rejects server keys and unsafe remote origins", () => {
  assert.throws(() => new BrowserAlbertClient({ baseUrl: "https://brain.example", getAccessToken: () => "token", apiKey: "secret" }), AlbertConfigurationError);
  assert.throws(() => new AlbertClient({ baseUrl: "http://brain.example" }), AlbertConfigurationError);
  assert.throws(() => new AlbertClient({ baseUrl: "https://secret@brain.example" }), AlbertConfigurationError);
});

test("telemetry excludes credentials, bodies, and query strings", async () => {
  const events = [];
  const client = new ServerAlbertClient({ baseUrl: "https://brain.example", apiKey: "never-log", telemetry: (event) => events.push(event), fetch: async () => json({ hits: [], degraded: [] }) });
  await client.memories.search({ query: "private question" });
  const encoded = JSON.stringify(events);
  assert.equal(encoded.includes("never-log"), false); assert.equal(encoded.includes("private question"), false);
  assert.deepEqual(events[0].operation, "/v1/search");
});

test("declares all 22 public MCP-equivalent operations", () => {
  const client = new AlbertClient({ baseUrl: "http://localhost:8080" });
  assert.deepEqual(Object.keys(client.mcp).sort(), [
    "memory_add_relationship", "memory_assemble_context", "memory_export", "memory_forget",
    "memory_forget_episode", "memory_get", "memory_get_episode", "memory_get_subgraph",
    "memory_import", "memory_ingest", "memory_lock_acquire", "memory_lock_release",
    "memory_lock_renew", "memory_query_graph", "memory_remember", "memory_search",
    "memory_update", "working_memory_complete", "working_memory_fail", "working_memory_get",
    "working_memory_start", "working_memory_update",
  ]);
});
