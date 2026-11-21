import { spawn } from "node:child_process";
import { dirname, resolve } from "node:path";
import { ALBERT_TOOL_NAMES, createAlbertTools, createCorpusSupplement, createNativeRuntime, promptSupplement } from "./native.js";

const MAX_OUTPUT_BYTES = 20_000;
const MAX_SUMMARY_CHARACTERS = 4_000;
const DEFAULT_CREDENTIALS = "~/.openclaw/albert/credentials.env";

function common(event, ctx, hook) {
  return {
    hook,
    session_id: event.sessionId ?? ctx.sessionId,
    session_key: event.sessionKey ?? ctx.sessionKey,
    agent_id: ctx.agentId,
    run_id: event.runId ?? ctx.runId,
    project_ref: Array.isArray(ctx.activeProjectKeys) ? ctx.activeProjectKeys[0] : undefined,
  };
}

function assistantOutcome(messages) {
  if (!Array.isArray(messages)) return undefined;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (!message || message.role !== "assistant") continue;
    if (typeof message.content === "string") {
      return message.content.slice(0, MAX_SUMMARY_CHARACTERS);
    }
    if (!Array.isArray(message.content)) continue;
    const text = message.content
      .filter((part) => part?.type === "text" && typeof part.text === "string")
      .map((part) => part.text)
      .join("\n")
      .slice(0, MAX_SUMMARY_CHARACTERS);
    if (text) return text;
  }
  return undefined;
}

function compactionId(event) {
  return `${event.sessionFile ?? "session"}:${event.messageCount}:${event.compactingCount ?? 0}:${event.tokenCount ?? 0}`;
}

function clampInteger(value, fallback, minimum, maximum) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.min(maximum, Math.max(minimum, Math.round(number)));
}

// ---------------------------------------------------------------------------
// Native transport (default): recall and capture from inside the gateway process.
// Recall is one bounded HTTP call before the prompt; capture runs in per-session
// lanes that no hook waits for, so the Brain's latency never reaches the user.
// ---------------------------------------------------------------------------
function registerNativeTransport(api, config) {
  const credentialsFile =
    typeof config.credentialsFile === "string" && config.credentialsFile
      ? config.credentialsFile
      : typeof config.command === "string" && config.command.includes("/")
        ? resolve(dirname(dirname(config.command)), "credentials.env")
        : DEFAULT_CREDENTIALS;
  const recallTimeoutMs = clampInteger(config.recallTimeoutMs, 1500, 100, 4500);
  const recallMaxItems = clampInteger(config.recallMaxItems, 8, 1, 50);
  const recallMaxCharacters = clampInteger(config.recallMaxCharacters, 6000, 500, 24_000);
  const captureTimeoutMs = clampInteger(config.captureTimeoutMs, 1500, 100, 5000);
  const outageCooldownMs = clampInteger(config.outageCooldownMs, 30_000, 1000, 600_000);

  let runtime = null;
  let lastFailure = 0;
  const ensureRuntime = () => {
    if (runtime) return runtime;
    if (Date.now() - lastFailure < 30_000) return null;
    try {
      runtime = createNativeRuntime({
        credentialsFile,
        recallTimeoutMs,
        recallMaxItems,
        recallMaxCharacters,
        captureTimeoutMs,
        outageCooldownMs,
        logger: api.logger,
      });
      api.logger.info?.(`albert-memory: native transport ready (${runtime.credentials.apiUrl})`);
    } catch (error) {
      lastFailure = Date.now();
      api.logger.warn?.(
        `albert-memory: native transport unavailable: ${error instanceof Error ? error.message : "unknown error"}`,
      );
    }
    return runtime;
  };
  ensureRuntime();

  const flushTimer = setInterval(() => {
    const current = ensureRuntime();
    if (current) current.client.flush().catch(() => {});
  }, 30_000);
  flushTimer.unref?.();

  // Agent-facing tools (albert_search / albert_remember / albert_get), the memory-core corpus
  // supplement (memory_search also returns Albert hits) and a short prompt supplement. Each is
  // optional and skipped on hosts that lack the API.
  if (config.registerTools !== false && typeof api.registerTool === "function") {
    api.registerTool(
      { contextVersion: 2, create: (ctx) => createAlbertTools(ensureRuntime, { agentId: ctx?.agentId }) },
      { names: [...ALBERT_TOOL_NAMES] },
    );
  }
  if (config.corpusSupplement !== false && typeof api.registerMemoryCorpusSupplement === "function") {
    api.registerMemoryCorpusSupplement(createCorpusSupplement(ensureRuntime));
  }
  if (config.promptSupplement !== false && typeof api.registerMemoryPromptSupplement === "function") {
    api.registerMemoryPromptSupplement(promptSupplement);
  }

  api.on("session_start", (event, ctx) => {
    const current = ensureRuntime();
    if (!current) return;
    current.adapter.sessionStart(common(event, ctx, "session_start"));
  }, { timeoutMs: 1000 });

  api.on(
    "before_prompt_build",
    async (event, ctx) => {
      const current = ensureRuntime();
      if (!current) return undefined;
      // `currentUserMessage` is the admitted request; older hosts and some run kinds
      // only populate `prompt`. Either is the user's text, never a hidden prompt.
      const userMessage =
        typeof event.currentUserMessage === "string" && event.currentUserMessage
          ? event.currentUserMessage
          : typeof event.prompt === "string"
            ? event.prompt
            : "";
      api.logger.debug?.(
        `albert-memory: before_prompt_build trigger=${ctx.trigger ?? "?"} keys=${Object.keys(event).join(",")} message=${userMessage.length}ch`,
      );
      const started = Date.now();
      const context = await current.adapter.beforePromptBuild({
        ...common(event, ctx, "before_prompt_build"),
        current_user_message: userMessage,
        message_id: event.currentUserMessageId,
      });
      api.logger.debug?.(`albert-memory: recall ${Date.now() - started}ms context=${context ? context.length : 0}ch`);
      if (typeof context === "string" && context) {
        ctx.hookInvocation?.assertActive();
        // Appended, not prepended: the recall block changes every turn, and putting it in
        // front of the (large, static) system prompt invalidates the provider's prompt cache.
        return { appendContext: context };
      }
      return undefined;
    },
    { timeoutMs: recallTimeoutMs + 1000 },
  );

  api.on("before_compaction", (event, ctx) => {
    const current = ensureRuntime();
    if (!current) return;
    current.adapter.beforeCompaction({
      ...common(event, ctx, "before_compaction"),
      compaction_id: compactionId(event),
      message_count: event.messageCount,
      token_count: event.tokenCount,
    });
  }, { timeoutMs: 1000 });

  api.on("agent_end", (event, ctx) => {
    const current = ensureRuntime();
    if (!current) return;
    current.adapter.agentEnd({
      ...common(event, ctx, "agent_end"),
      success: event.success,
      outcome: event.success ? assistantOutcome(event.messages) : undefined,
    });
  }, { timeoutMs: 1000 });

  api.on("session_end", (event, ctx) => {
    const current = ensureRuntime();
    if (!current) return;
    current.adapter.sessionEnd({ ...common(event, ctx, "session_end"), reason: event.reason });
  }, { timeoutMs: 1000 });

  api.on("gateway_stop", async () => {
    clearInterval(flushTimer);
    if (runtime) await runtime.adapter.drain(1500);
  }, { timeoutMs: 1800 });
}

// ---------------------------------------------------------------------------
// Adapter transport (legacy): one Python process per hook, serialised globally.
// Kept for rollback (`transport: "adapter"`) and for hosts without the native path.
// ---------------------------------------------------------------------------
function invoke(command, timeoutMs, payload) {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(command, [], { stdio: ["pipe", "pipe", "ignore"], env: process.env });
    let output = "";
    let outputBytes = 0;
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error("adapter timeout"));
    }, timeoutMs);
    child.stdout.on("data", (chunk) => {
      outputBytes += chunk.length;
      if (outputBytes > MAX_OUTPUT_BYTES) {
        child.kill("SIGKILL");
        reject(new Error("adapter output exceeded bound"));
        return;
      }
      output += chunk.toString("utf8");
    });
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (code !== 0) {
        reject(new Error(`adapter exited ${String(code)}`));
        return;
      }
      try {
        resolvePromise(JSON.parse(output));
      } catch {
        reject(new Error("adapter returned invalid JSON"));
      }
    });
    child.stdin.end(JSON.stringify(payload));
  });
}

function registerAdapterTransport(api, config) {
  const command = config.command || "albert-openclaw-adapter";
  const timeoutMs = config.timeoutMs || 1800;
  let lane = Promise.resolve();
  const submit = (payload) => {
    const current = lane.then(() => invoke(command, timeoutMs, payload));
    lane = current.catch((error) => {
      api.logger.warn?.(`albert-memory: ${error instanceof Error ? error.message : "failed"}`);
    });
    return current;
  };

  api.on("session_start", async (event, ctx) => {
    await submit(common(event, ctx, "session_start"));
  });

  api.on("before_prompt_build", async (event, ctx) => {
    const result = await submit({
      ...common(event, ctx, "before_prompt_build"),
      current_user_message: event.currentUserMessage ?? "",
      message_id: event.currentUserMessageId,
    });
    if (typeof result?.context === "string" && result.context) {
      ctx.hookInvocation?.assertActive();
      return { prependContext: result.context };
    }
    return undefined;
  }, { timeoutMs: 5000 });

  api.on("before_compaction", async (event, ctx) => {
    await submit({
      ...common(event, ctx, "before_compaction"),
      compaction_id: compactionId(event),
      message_count: event.messageCount,
      token_count: event.tokenCount,
    });
  }, { timeoutMs: 5000 });

  api.on("agent_end", async (event, ctx) => {
    await submit({
      ...common(event, ctx, "agent_end"),
      success: event.success,
      outcome: event.success ? assistantOutcome(event.messages) : undefined,
    });
  }, { timeoutMs: 5000 });

  api.on("session_end", async (event, ctx) => {
    await submit({
      ...common(event, ctx, "session_end"),
      reason: event.reason,
    });
  }, { timeoutMs: 1800 });

  api.on("gateway_stop", async () => {
    await lane;
  }, { timeoutMs: 1800 });
}

export default {
  id: "albert-memory",
  name: "Albert Memory",
  description: "Bounded cited recall and minimized OpenClaw lifecycle capture.",
  register(api) {
    const config = api.pluginConfig ?? {};
    if (config.transport === "adapter") {
      registerAdapterTransport(api, config);
      return;
    }
    registerNativeTransport(api, config);
  },
};
