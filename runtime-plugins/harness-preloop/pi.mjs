import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { Bridge, loadConfig } from "./bridge.mjs";

/** Pi extension; MCP discovery completes before the first model turn. */
export default async function preloop(pi) {
  const config = loadConfig("pi");
  const bridge = new Bridge(config);
  const provider = config.preloop?.model;
  if (provider && !provider.models?.length)
    throw new Error("No authorized Preloop models");
  if (provider)
    pi.registerProvider("preloop", {
      ...provider,
      models: provider.models.map((model) => ({
        name: model.id,
        reasoning: false,
        input: ["text"],
        contextWindow: 128000,
        maxTokens: 16384,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        ...model,
      })),
    });
  const clients = [];
  let mcpUnavailable = false;
  try {
    for (const [serverName, server] of Object.entries(
      config.mcpServers || {},
    )) {
      const client = new Client({ name: "preloop-pi", version: "0.1.0" });
      clients.push(client);
      await client.connect(
        new StreamableHTTPClientTransport(new URL(server.url), {
          requestInit: { headers: server.headers || {} },
        }),
      );
      let cursor;
      do {
        const page = await client.listTools(cursor ? { cursor } : undefined);
        for (const tool of page.tools) {
          pi.registerTool({
            name: `mcp__${serverName}__${tool.name}`,
            label: tool.name,
            description: tool.description || tool.name,
            parameters: tool.inputSchema,
            async execute(_id, args, signal) {
              const result = await client.callTool(
                { name: tool.name, arguments: args },
                undefined,
                {
                  signal,
                  timeout:
                    config.preloop?.control?.approval_timeout_ms || 600000,
                },
              );
              if (result.isError)
                throw new Error(
                  result.content
                    ?.filter((c) => c.type === "text")
                    .map((c) => c.text)
                    .join("\n") || "MCP tool failed",
                );
              return { content: result.content, details: {} };
            },
          });
        }
        cursor = page.nextCursor;
      } while (cursor);
    }
  } catch {
    await Promise.allSettled(clients.map((client) => client.close()));
    // Pi continues after extension load failures, so keep the native gate loaded.
    mcpUnavailable = true;
  }
  pi.on("session_start", async (_event, ctx) => {
    bridge.sessions.clear();
    bridge.sessions.set(ctx.sessionManager.getSessionId(), {
      send: async (text) =>
        pi.sendUserMessage(text, {
          deliverAs: ctx.isIdle() ? "followUp" : "steer",
        }),
      stop: async () => ctx.abort(),
    });
    if (provider?.models?.length) {
      const model = ctx.modelRegistry.find("preloop", provider.models[0].id);
      if (!model || !(await pi.setModel(model)))
        throw new Error("Preloop model is unavailable");
    }
    await bridge.lifecycle("session_start", ctx.sessionManager.getSessionId());
    bridge.start();
  });
  pi.on("before_provider_headers", (event, ctx) => {
    event.headers["X-Preloop-Session-Id"] = ctx.sessionManager.getSessionId();
  });
  pi.on("tool_call", async (event, ctx) => {
    if (mcpUnavailable)
      return {
        block: true,
        reason:
          "Preloop MCP is unavailable. Restore connectivity and restart Pi.",
      };
    const decision = await bridge.check(
      event.toolName,
      event.input,
      ctx.sessionManager.getSessionId(),
      ctx.signal,
    );
    if (decision.decision !== "allow")
      return { block: true, reason: decision.reason || "Denied by Preloop" };
  });
  pi.on("agent_end", async (_event, ctx) => {
    await bridge.lifecycle("response", ctx.sessionManager.getSessionId());
  });
  pi.on("session_compact", async (_event, ctx) => {
    await bridge.lifecycle("compaction", ctx.sessionManager.getSessionId());
  });
  pi.on("session_shutdown", async (_event, ctx) => {
    await bridge.lifecycle("session_end", ctx.sessionManager.getSessionId());
    bridge.stop();
    await Promise.allSettled(clients.map((client) => client.close()));
  });
}
