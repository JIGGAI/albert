import { spawn } from "node:child_process";

const MAX_OUTPUT_BYTES = 20_000;
const MAX_SUMMARY_CHARACTERS = 4_000;

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

function invoke(command, timeoutMs, payload) {
  return new Promise((resolve, reject) => {
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
        resolve(JSON.parse(output));
      } catch {
        reject(new Error("adapter returned invalid JSON"));
      }
    });
    child.stdin.end(JSON.stringify(payload));
  });
}

export default {
  id: "albert-memory",
  name: "Albert Memory",
  description: "Bounded cited recall and minimized OpenClaw lifecycle capture.",
  register(api) {
    const command = api.pluginConfig?.command || "albert-openclaw-adapter";
    const timeoutMs = api.pluginConfig?.timeoutMs || 1800;
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
        compaction_id: `${event.sessionFile ?? "session"}:${event.messageCount}:${event.compactingCount ?? 0}:${event.tokenCount ?? 0}`,
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
  },
};
