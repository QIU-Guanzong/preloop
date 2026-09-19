import { randomUUID } from "node:crypto";
import { Bridge, loadConfig } from "./bridge.mjs";

export const name = "preloop";
export const inject = ["tools", "agents"];

/** Cordis plugin. Retain host policy and enforce remote denials monotonically. */
export function apply(ctx, options = {}) {
  const config = loadConfig("deepseek", options.configPath);
  const provider = config.preloop?.model;
  // Native saved selections override Cordis composition defaults. Enforce the
  // managed route at request time without changing the user's native settings.
  if (provider)
    ctx.on(
      "agent/request",
      async (_payload, next) => {
        if (!provider.models?.length)
          throw new Error("No authorized Preloop models");
        const request = await next();
        const selected =
          provider.models.find((model) => model.id === request.model) ||
          provider.models[0];
        return {
          ...request,
          provider: "preloop",
          model: selected.id,
          ...(selected.maxTokens ? { maxTokens: selected.maxTokens } : {}),
        };
      },
      { prepend: true },
    );
  if (config.preloop?.model?.apiKey)
    process.env.PRELOOP_DSH_MODEL_TOKEN = config.preloop.model.apiKey;
  const bridge = new Bridge(config);
  const denials = new Map();
  ctx.tools.guard((exec) => denials.get(exec.token));
  ctx.on("tools/pre-execute", async (exec, next) => {
    const local = await next();
    if (local.kind === "deny" || local.kind === "cancel") return local;
    const decision = await bridge.check(
      exec.name,
      exec.arguments,
      exec.agent?.id,
      exec.signal,
    );
    if (decision.decision !== "allow") {
      const reason = decision.reason || "Denied by Preloop";
      denials.set(exec.token, reason);
      return { kind: "deny", reason };
    }
    return local;
  });
  ctx.on("tools/result", (exec) => {
    denials.delete(exec.token);
  });
  ctx.on("agent/created", ({ agent }) => {
    bridge.sessions.set(agent.id, {
      send: async (text) =>
        agent.steer({
          id: randomUUID(),
          role: "user",
          content: [{ type: "text", text }],
          source: { kind: "user" },
        }),
      stop: async () => agent.cancel({ kind: "user" }),
    });
    void bridge.lifecycle("session_start", agent.id);
    bridge.presence();
  });
  ctx.on("agent/disposed", ({ agent }) => {
    void bridge.lifecycle("session_end", agent.id);
    bridge.sessions.delete(agent.id);
    bridge.presence();
  });
  ctx.on("agent/status", ({ agent, status }) => {
    if (status === "idle") void bridge.lifecycle("response", agent.id);
  });
  ctx.on("dispose", () => {
    bridge.stop();
    denials.clear();
  });
  bridge.start();
}
