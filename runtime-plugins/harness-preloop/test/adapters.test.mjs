import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import piPlugin from "../pi.mjs";
import { apply as deepseekPlugin } from "../deepseek.mjs";

function configuration(runtime) {
  const path = join(
    mkdtempSync(join(tmpdir(), "preloop-plugin-")),
    "preloop.json",
  );
  writeFileSync(
    path,
    JSON.stringify({
      preloop: {
        control: {
          enabled: false,
          runtime,
          native_tool_approvals: "on",
          bearer_token: "test",
          control_ws_url: "ws://127.0.0.1:1/api/v1/agents/control/ws",
        },
      },
    }),
  );
  return path;
}

test("Pi returns a native blocking result on failed approval, and stamps native session headers", async () => {
  const previous = process.env.PRELOOP_HARNESS_CONFIG;
  process.env.PRELOOP_HARNESS_CONFIG = configuration("pi");
  const hooks = new Map();
  try {
    await piPlugin({ on: (name, handler) => hooks.set(name, handler) });
    const ctx = { sessionManager: { getSessionId: () => "native-pi-session" } };
    const result = await hooks.get("tool_call")(
      { toolName: "bash", input: { command: "echo hi" } },
      ctx,
    );
    assert.equal(result.block, true);
    const request = { headers: {} };
    hooks.get("before_provider_headers")(request, ctx);
    assert.equal(request.headers["X-Preloop-Session-Id"], "native-pi-session");
  } finally {
    if (previous === undefined) delete process.env.PRELOOP_HARNESS_CONFIG;
    else process.env.PRELOOP_HARNESS_CONFIG = previous;
  }
});

test("DeepSeek retains local denials and enforces remote denials with a monotonic guard", async () => {
  const hooks = new Map();
  let guard;
  deepseekPlugin(
    {
      tools: {
        guard: (fn) => {
          guard = fn;
        },
      },
      on: (name, handler) => hooks.set(name, handler),
    },
    { configPath: configuration("deepseek") },
  );
  const exec = {
    token: Symbol(),
    name: "bash",
    arguments: { command: "echo hi" },
    agent: { id: "dsh-session" },
  };
  const gate = hooks.get("tools/pre-execute");
  const local = { kind: "deny", reason: "Host policy" };
  assert.equal(await gate(exec, async () => local), local);
  const denied = await gate(exec, async () => ({ kind: "allow" }));
  assert.equal(denied.kind, "deny");
  assert.equal(guard(exec), denied.reason);
  hooks.get("tools/result")(exec);
  assert.equal(guard(exec), undefined);
  hooks.get("dispose")();
});

test("DeepSeek forces managed routing over native selections and blocks empty catalogs", async () => {
  const path = configuration("deepseek");
  const hooks = new Map();
  const model = { apiKey: "test-token", models: [{ id: "managed-model" }] };
  const config = { preloop: { control: { enabled: false }, model } };
  for (const models of [[{ id: "managed-model" }], []]) {
    config.preloop.model.models = models;
    writeFileSync(path, JSON.stringify(config));
    deepseekPlugin(
      {
        tools: { guard: () => {} },
        on: (name, handler) => hooks.set(name, handler),
      },
      { configPath: path },
    );
    const request = () =>
      hooks.get("agent/request")({}, async () => ({
        provider: "native",
        model: "native-model",
        maxTokens: 100,
      }));
    if (models.length)
      assert.deepEqual(await request(), {
        provider: "preloop",
        model: "managed-model",
        maxTokens: 100,
      });
    else await assert.rejects(request(), /No authorized/);
    hooks.get("dispose")();
  }
});

test("Pi retains its native gate when MCP initialization fails", async () => {
  const previous = process.env.PRELOOP_HARNESS_CONFIG;
  const path = configuration("pi");
  writeFileSync(
    path,
    JSON.stringify({
      mcpServers: { preloop: { url: "http://127.0.0.1:1/mcp/v1" } },
      preloop: { control: { enabled: false, native_tool_approvals: "on" } },
    }),
  );
  process.env.PRELOOP_HARNESS_CONFIG = path;
  const hooks = new Map();
  try {
    await piPlugin({ on: (name, handler) => hooks.set(name, handler) });
    const result = await hooks.get("tool_call")(
      { toolName: "bash", input: { command: "echo should-not-run" } },
      { sessionManager: { getSessionId: () => "native-pi-session" } },
    );
    assert.equal(result.block, true);
    assert.match(result.reason, /MCP.*unavailable/);
  } finally {
    if (previous === undefined) delete process.env.PRELOOP_HARNESS_CONFIG;
    else process.env.PRELOOP_HARNESS_CONFIG = previous;
  }
});
