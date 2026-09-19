import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// Container-only bootstrap. Secrets enter via environment, never shell interpolation.
const runtime = process.env.PRELOOP_HARNESS;
const root =
  runtime === "pi" ? process.env.PI_CODING_AGENT_DIR : process.env.DSH_HOME;
mkdirSync(root, { recursive: true });
const model = JSON.parse(process.env.PRELOOP_HARNESS_MODEL);
model.apiKey = process.env.PRELOOP_MODEL_TOKEN;
model.baseUrl = model.baseUrl.replace(
  "${PRELOOP_URL}",
  process.env.PRELOOP_URL || "",
);
const apiURL = process.env.PRELOOP_URL || process.env.PRELOOP_API_URL || "";
const config = {
  mcpServers: JSON.parse(process.env.MCP_CONFIG_JSON || '{"mcpServers":{}}')
    .mcpServers,
  preloop: {
    model,
    control: {
      enabled: false,
      runtime,
      bearer_token: process.env.PRELOOP_API_TOKEN,
      control_ws_url:
        apiURL.replace(/^http/, "ws") + "/api/v1/agents/control/ws",
      native_tool_approvals:
        process.env.PRELOOP_NATIVE_APPROVALS === "on" ? "on" : "off",
    },
  },
};
if (process.env.PRELOOP_MCP_URL) {
  for (const [name, server] of Object.entries(config.mcpServers || {})) {
    if (["preloop", "preloop-mcp"].includes(name))
      server.url = process.env.PRELOOP_MCP_URL;
  }
}
writeFileSync(join(root, "preloop.json"), JSON.stringify(config), {
  mode: 0o600,
});
if (runtime === "deepseek") {
  // JSON is valid YAML; !!js isn't needed because the provider resolves this env key.
  const patch = [
    // Workers already run inside an isolated, unprivileged container. Nested
    // OS sandboxing is unavailable under normal Docker/Kubernetes seccomp.
    // CLI onboarding never writes this override to a user's local runtime.
    { id: "sandbox-policy", config: { mode: "danger-full-access" } },
    { id: "permission", config: { defaultPreset: "danger-full-access" } },
    { id: "headless-startup", disabled: true },
    {
      id: "llm-pi-ai",
      config: {
        providers: {
          preloop: {
            api: model.api,
            baseURL: model.baseUrl,
            apiKeyEnv: "PRELOOP_MODEL_TOKEN",
            models: model.models,
          },
        },
      },
    },
    {
      id: "agent-default-model",
      config: { provider: "preloop", model: model.models[0].id },
    },
    {
      insert: [
        {
          id: "preloop-headless-startup",
          name: join(dirname(fileURLToPath(import.meta.url)), "stdin.mjs"),
        },
        {
          id: "preloop-control",
          name: join(dirname(fileURLToPath(import.meta.url)), "deepseek.mjs"),
        },
        ...Object.entries(config.mcpServers || {}).map(([name, server]) => ({
          id: `preloop-mcp-${name}`,
          name: "@deepseek-ai/dsh-mcp-client",
          config: {
            serverName: name,
            transport: "streamable-http",
            url: server.url,
            headers: server.headers,
            toolCallTimeoutMs: 600000,
            failOnStartupError: true,
          },
        })),
      ],
    },
  ];
  writeFileSync(join(root, "cordis.patch.yml"), JSON.stringify(patch), {
    mode: 0o600,
  });
}
