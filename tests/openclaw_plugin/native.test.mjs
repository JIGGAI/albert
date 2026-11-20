// Tests for the native OpenClaw transport (src/albert/openclaw_plugin/native.js).
// Run: node --test tests/openclaw_plugin
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdtempSync, readFileSync, writeFileSync, chmodSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, beforeEach, test } from "node:test";

import {
  EncryptedQueue,
  NativeCaptureClient,
  NativeOpenClawAdapter,
  loadCredentials,
  operationKey,
  screenText,
} from "../../src/albert/openclaw_plugin/native.js";
import plugin from "../../src/albert/openclaw_plugin/index.js";

const QUEUE_KEY = Buffer.alloc(32, 7).toString("base64url");
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ---- a controllable fake Brain ------------------------------------------------
function fakeBrain() {
  const state = {
    sessions: new Map(), // id -> { lastSequence, events: [], ended, checkpoints: [] }
    starts: [],
    recalls: [],
    inFlight: 0,
    maxInFlight: 0,
    delayMs: 0,
    hang: false,
    down: false,
    nextId: 1,
  };
  const server = createServer(async (request, response) => {
    state.inFlight += 1;
    state.maxInFlight = Math.max(state.maxInFlight, state.inFlight);
    const done = (status, body) => {
      state.inFlight -= 1;
      response.writeHead(status, { "content-type": "application/json" });
      response.end(JSON.stringify(body));
    };
    if (state.hang) return; // never answers; client timeout must fire
    if (state.delayMs) await sleep(state.delayMs);
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : {};
    if (request.headers.authorization !== "Bearer unit-token") return done(401, { detail: "unauthorized" });
    const url = request.url;
    if (url === "/v1/capture/sessions") {
      const id = `srv-${state.nextId++}`;
      state.sessions.set(id, { lastSequence: 0, events: [], ended: false, checkpoints: [], body });
      state.starts.push(body);
      return done(201, { id });
    }
    let match = url.match(/^\/v1\/capture\/sessions\/([^/]+)\/events$/);
    if (match) {
      const session = state.sessions.get(match[1]);
      if (!session) return done(404, { detail: "no session" });
      const expected = session.lastSequence + 1;
      if (body.sequence !== expected) {
        return done(409, { detail: { error: "out_of_order_event", expected_sequence: expected } });
      }
      session.lastSequence = body.sequence;
      session.events.push(body);
      return done(201, { id: `evt-${state.nextId++}` });
    }
    match = url.match(/^\/v1\/capture\/sessions\/([^/]+)\/checkpoints$/);
    if (match) {
      state.sessions.get(match[1])?.checkpoints.push(body);
      return done(201, { id: `ckpt-${state.nextId++}` });
    }
    match = url.match(/^\/v1\/capture\/sessions\/([^/]+)\/end$/);
    if (match) {
      const session = state.sessions.get(match[1]);
      if (session) session.ended = true;
      return done(200, { id: match[1] });
    }
    if (url === "/v1/context/session-start") {
      state.recalls.push(body);
      return done(200, {
        context: `remembered: ${body.query}`,
        citations: [{ id: "mem-1", source_uri: "albert://mem-1" }],
      });
    }
    return done(404, { detail: "unknown" });
  });
  return { state, server };
}

let brain;
let baseUrl;
before(async () => {
  brain = fakeBrain();
  await new Promise((resolve) => brain.server.listen(0, "127.0.0.1", resolve));
  baseUrl = `http://127.0.0.1:${brain.server.address().port}`;
});
after(() => brain.server.close());
beforeEach(() => {
  brain.state.sessions.clear();
  brain.state.starts.length = 0;
  brain.state.recalls.length = 0;
  brain.state.maxInFlight = 0;
  brain.state.delayMs = 0;
  brain.state.hang = false;
});

function makeRuntime(options = {}) {
  const dir = mkdtempSync(join(tmpdir(), "albert-native-"));
  const time = { now: Date.now() };
  const queue = new EncryptedQueue(join(dir, "capture.db.native"), QUEUE_KEY, "openclaw:test", {
    clock: () => time.now / 1000,
  });
  const client = new NativeCaptureClient({
    now: () => time.now,
    apiUrl: options.apiUrl ?? baseUrl,
    token: "unit-token",
    queue,
    workspaceId: "ws-1",
    captureTimeoutMs: options.captureTimeoutMs ?? 400,
    recallTimeoutMs: options.recallTimeoutMs ?? 300,
    outageCooldownMs: options.outageCooldownMs ?? 2000,
    logger: { warn() {}, info() {} },
  });
  return { dir, time, queue, client, adapter: new NativeOpenClawAdapter(client, { logger: { warn() {} } }) };
}

function envelope(hook, extra = {}) {
  return {
    hook,
    session_id: "physical-1",
    session_key: "agent:main:s1",
    agent_id: "main",
    run_id: "run-1",
    project_ref: "jiggai/albert",
    ...extra,
  };
}

async function settled(adapter) {
  await Promise.allSettled([...adapter.lanes.values()]);
  await Promise.allSettled([...adapter.lanes.values()]);
}

test("screening redacts and reports the same credential classes as the Python adapter", () => {
  const result = screenText("token: abcdefgh12345678 and AKIAABCDEFGHIJKLMNOP");
  assert.deepEqual(result.reasons, ["assigned_credential", "aws_access_key"]);
  assert.match(result.content, /\[REDACTED:assigned_credential\]/);
  assert.equal(screenText("just a normal sentence").reasons.length, 0);
  assert.equal(operationKey("start", "s", "i"), operationKey("start", "s", "i"));
  assert.notEqual(operationKey("start", "s", "i"), operationKey("start", "s", "j"));
});

test("credential file must be a private regular file", () => {
  const dir = mkdtempSync(join(tmpdir(), "albert-cred-"));
  const file = join(dir, "credentials.env");
  writeFileSync(file, JSON.stringify({ ALBERT_CAPTURE_TOKEN: "t", ALBERT_CAPTURE_QUEUE_KEY: QUEUE_KEY, ALBERT_API_URL: "http://brain/" }));
  chmodSync(file, 0o644);
  assert.throws(() => loadCredentials(file), /permissions/);
  chmodSync(file, 0o600);
  const loaded = loadCredentials(file);
  assert.equal(loaded.apiUrl, "http://brain");
  assert.equal(loaded.scope, "openclaw:default");
});

test("before_prompt_build returns cited context and does not wait for capture", async () => {
  const { adapter } = makeRuntime();
  brain.state.delayMs = 150; // every Brain call takes 150 ms
  const started = Date.now();
  const context = await adapter.beforePromptBuild(envelope("before_prompt_build", {
    current_user_message: "what do we know about the MT-07?",
    message_id: "m1",
  }));
  const elapsed = Date.now() - started;
  assert.match(context, /ALBERT REFERENCE CONTEXT/);
  assert.match(context, /remembered: what do we know about the MT-07\?/);
  assert.match(context, /albert:\/\/mem-1/);
  // one recall round trip, not recall + start + event
  assert.ok(elapsed < 320, `prompt hook took ${elapsed} ms`);
  await settled(adapter);
  const [session] = [...brain.state.sessions.values()];
  assert.equal(session.events.length, 1);
  assert.equal(session.events[0].event_class, "user_goal");
  assert.equal(session.events[0].sequence, 1);
  assert.equal(brain.state.starts[0].workspace_id, "ws-1");
});

test("capture hooks return immediately and arrive in order per session", async () => {
  const { adapter } = makeRuntime();
  brain.state.delayMs = 100;
  const started = Date.now();
  adapter.sessionStart(envelope("session_start"));
  adapter.agentEnd(envelope("agent_end", { success: true, outcome: "PING-OK" }));
  adapter.sessionEnd(envelope("session_end", { reason: "closed" }));
  assert.ok(Date.now() - started < 30, "capture hooks must not block");
  await settled(adapter);
  const [session] = [...brain.state.sessions.values()];
  assert.deepEqual(session.events.map((event) => event.event_class), [
    "session_started",
    "assistant_outcome",
    "session_ended",
  ]);
  assert.deepEqual(session.events.map((event) => event.sequence), [1, 2, 3]);
  assert.equal(session.checkpoints.length, 1);
  assert.equal(session.checkpoints[0].through_sequence, 3);
  assert.equal(session.ended, true);
});

test("sessions run in parallel, not through one global lane", async () => {
  const { adapter } = makeRuntime();
  brain.state.delayMs = 120;
  for (let index = 0; index < 4; index += 1) {
    adapter.sessionStart(envelope("session_start", { session_id: `p-${index}`, session_key: `agent:main:${index}` }));
  }
  await settled(adapter);
  assert.ok(brain.state.maxInFlight >= 3, `max in flight was ${brain.state.maxInFlight}`);
  assert.equal(brain.state.sessions.size, 4);
});

test("secrets in the user message are neither recalled nor captured", async () => {
  const { adapter } = makeRuntime();
  const context = await adapter.beforePromptBuild(envelope("before_prompt_build", {
    current_user_message: "my password: hunter2hunter2 please remember",
    message_id: "m2",
  }));
  assert.equal(context, undefined);
  await settled(adapter);
  assert.equal(brain.state.recalls.length, 0);
  assert.equal(brain.state.sessions.size, 0);
});

test("a hung Brain costs one bounded recall, then the breaker skips recall", async () => {
  const { adapter, client } = makeRuntime({ recallTimeoutMs: 200, outageCooldownMs: 5000 });
  brain.state.hang = true;
  const timings = [];
  for (let index = 0; index < 3; index += 1) {
    const started = Date.now();
    const context = await adapter.beforePromptBuild(envelope("before_prompt_build", {
      current_user_message: `question ${index}`,
      message_id: `h-${index}`,
    }));
    timings.push(Date.now() - started);
    assert.match(context, /UNAVAILABLE/);
  }
  assert.ok(timings[0] >= 180 && timings[0] < 600, `first recall ${timings[0]} ms`);
  assert.ok(timings[2] < 50, `breaker should short-circuit, took ${timings[2]} ms`);
  assert.equal(client.recallOpen(), true);
  brain.state.hang = false;
  await settled(adapter);
});

test("captures survive an outage in an encrypted queue and replay in order", async () => {
  const { adapter, queue, client, dir, time } = makeRuntime({ apiUrl: "http://127.0.0.1:9" }); // refused
  adapter.sessionStart(envelope("session_start", { session_id: "q-1", session_key: "agent:main:q" }));
  adapter.agentEnd(envelope("agent_end", { session_id: "q-1", session_key: "agent:main:q", success: true, outcome: "secret-free outcome text" }));
  await settled(adapter);
  assert.ok(queue.stats().queued >= 2, `queued ${queue.stats().queued}`);
  const onDisk = readFileSync(join(dir, "capture.db.native"));
  assert.ok(!onDisk.includes("secret-free outcome text"), "queue must be encrypted at rest");
  assert.ok(!onDisk.includes("unit-token"));
  // While the Brain is unreachable, further captures skip the network entirely.
  assert.equal(client.captureOpen(), true);
  // Brain returns: point the client at it, let the back-off and retry timers lapse, flush.
  client.apiUrl = baseUrl;
  time.now += 60_000;
  const delivered = await client.flush();
  assert.ok(delivered >= 2, `delivered ${delivered}`);
  const [session] = [...brain.state.sessions.values()];
  assert.deepEqual(session.events.map((event) => event.event_class), ["session_started", "assistant_outcome"]);
  assert.equal(queue.stats().queued, 0);
});

test("a restarted gateway resynchronises with the server's sequence cursor", async () => {
  const { adapter } = makeRuntime();
  adapter.sessionStart(envelope("session_start", { session_id: "r-1", session_key: "agent:main:r" }));
  adapter.agentEnd(envelope("agent_end", { session_id: "r-1", session_key: "agent:main:r", success: true, outcome: "first" }));
  await settled(adapter);
  // simulate a new process: fresh adapter, same queue (session mapping persisted)
  const fresh = new NativeOpenClawAdapter(adapter.client, { logger: { warn() {} } });
  fresh.agentEnd(envelope("agent_end", { session_id: "r-1", session_key: "agent:main:r", run_id: "run-2", success: true, outcome: "second" }));
  await settled(fresh);
  const [session] = [...brain.state.sessions.values()];
  assert.deepEqual(session.events.map((event) => event.sequence), [1, 2, 3]);
  assert.equal(session.events[2].content, "second");
});

test("plugin registers native hooks and falls back cleanly without credentials", async () => {
  const handlers = new Map();
  const api = {
    pluginConfig: { credentialsFile: "/nonexistent/credentials.env" },
    logger: { warn() {}, info() {} },
    on(name, handler) { handlers.set(name, handler); },
  };
  plugin.register(api);
  assert.deepEqual([...handlers.keys()].sort(), [
    "agent_end", "before_compaction", "before_prompt_build", "gateway_stop", "session_end", "session_start",
  ]);
  // No runtime: hooks are no-ops rather than errors.
  assert.equal(await handlers.get("before_prompt_build")({ currentUserMessage: "hi" }, { sessionKey: "k", agentId: "a" }), undefined);
  await handlers.get("gateway_stop")();
});

test("plugin with real credentials appends recall context from the native transport", async () => {
  const dir = mkdtempSync(join(tmpdir(), "albert-plugin-"));
  const credentials = join(dir, "credentials.env");
  writeFileSync(credentials, JSON.stringify({
    ALBERT_API_URL: baseUrl,
    ALBERT_CAPTURE_TOKEN: "unit-token",
    ALBERT_CAPTURE_QUEUE_KEY: QUEUE_KEY,
    ALBERT_CAPTURE_QUEUE_PATH: join(dir, "capture.db"),
    ALBERT_WORKSPACE_ID: "ws-9",
  }), { mode: 0o600 });
  const handlers = new Map();
  const api = { pluginConfig: { credentialsFile: credentials, recallTimeoutMs: 500 }, logger: { warn() {}, info() {} }, on(name, handler) { handlers.set(name, handler); } };
  plugin.register(api);
  const ctx = { sessionId: "sid", sessionKey: "agent:main:plugin", agentId: "main", runId: "run-9", hookInvocation: { assertActive() {} } };
  const result = await handlers.get("before_prompt_build")({ currentUserMessage: "plugin question", currentUserMessageId: "pm1" }, ctx);
  assert.match(result.appendContext, /remembered: plugin question/);
  assert.equal(result.prependContext, undefined);
  await handlers.get("agent_end")({ success: true, messages: [{ role: "assistant", content: "done" }] }, ctx);
  await handlers.get("gateway_stop")();
  await sleep(50);
  const [session] = [...brain.state.sessions.values()];
  assert.ok(session.events.some((event) => event.event_class === "assistant_outcome" && event.content === "done"));
  assert.ok(existsSync(join(dir, "capture.db.native")));
});
