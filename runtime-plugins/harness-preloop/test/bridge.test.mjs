import assert from "node:assert/strict";
import { test } from "node:test";
import { Bridge, canonicalToolName } from "../bridge.mjs";

const config = {
  preloop: {
    control: {
      runtime: "pi",
      bearer_token: "test-token",
      runtime_principal_id: "pi-test",
      control_ws_url: "wss://example.com/api/v1/agents/control/ws",
      native_tool_approvals: "on",
    },
  },
};

test("native tools use the existing central policy names", () => {
  assert.equal(canonicalToolName("bash"), "Bash");
  assert.equal(canonicalToolName("write"), "Write");
  assert.equal(canonicalToolName("unknown"), "unknown");
});

test("approval failure and unknown decisions deny execution", async () => {
  for (const fetchImpl of [
    async () => {
      throw Error("offline");
    },
    async () => ({ ok: true, json: async () => ({ decision: "maybe" }) }),
    async () => ({ ok: false }),
  ]) {
    const bridge = new Bridge(config, { fetchImpl });
    const decision = await bridge.check(
      "bash",
      { command: "echo hi" },
      "session-a",
    );
    assert.equal(decision.decision, "deny");
  }
});

test("approval carries real session identity and honors explicit denial", async () => {
  const bridge = new Bridge(config, {
    fetchImpl: async (url, init) => {
      assert.equal(url, "https://example.com/api/v1/agents/permission-check");
      assert.equal(init.headers.Authorization, "Bearer test-token");
      assert.deepEqual(JSON.parse(init.body), {
        source: "pi",
        tool_name: "Bash",
        tool_input: { command: "echo hi" },
        session_id: "session-a",
        cwd: process.cwd(),
        evaluation_phase: "permission_request",
      });
      return {
        ok: true,
        json: async () => ({ decision: "deny", reason: "Policy" }),
      };
    },
  });
  assert.equal(
    (await bridge.check("bash", { command: "echo hi" }, "session-a")).reason,
    "Policy",
  );
});

test("replayed commands share the same outcome and cannot target another session", async () => {
  const bridge = new Bridge(config);
  let calls = 0;
  bridge.sessions.set("one", {
    send: async () => {
      calls++;
    },
    stop: async () => {},
  });
  const cmd = {
    type: "command",
    name: "send_message",
    message_id: "id",
    payload: { text: "continue", target_session_id: "one" },
  };
  await Promise.all([bridge.dispatch(cmd), bridge.dispatch(cmd)]);
  assert.equal(calls, 1);
  await assert.rejects(
    bridge.dispatch({
      ...cmd,
      message_id: "other",
      payload: { text: "hi", target_session_id: "two" },
    }),
    /session/,
  );
});

test("ambiguous and unsupported commands fail instead of choosing a session", async () => {
  const bridge = new Bridge(config);
  bridge.sessions.set("one", {});
  bridge.sessions.set("two", {});
  await assert.rejects(
    bridge.dispatch({ name: "send_message", payload: { text: "hi" } }),
    /session/,
  );
  await assert.rejects(bridge.dispatch({ name: "new_session" }), /Unsupported/);
});

test("websocket authenticates, advertises capabilities, and acknowledges native-session commands", async () => {
  const { WebSocketServer } = await import("ws");
  const { once } = await import("node:events");
  const server = new WebSocketServer({ port: 0, host: "127.0.0.1" });
  await once(server, "listening");
  const bridge = new Bridge({
    preloop: {
      control: {
        ...config.preloop.control,
        control_ws_url: `ws://127.0.0.1:${server.address().port}/api/v1/agents/control/ws`,
      },
    },
  });
  let sent = 0,
    stopped = 0;
  bridge.sessions.set("native-session", {
    send: async (text) => {
      assert.equal(text, "continue");
      sent++;
    },
    stop: async () => {
      stopped++;
    },
  });
  const connected = once(server, "connection");
  bridge.start();
  const [socket, request] = await connected;
  try {
    assert.equal(request.headers.authorization, "Bearer test-token");
    const presence = JSON.parse(String((await once(socket, "message"))[0]));
    assert.equal(presence.payload.capabilities.new_session, false);
    assert.equal(presence.payload.capabilities.text, true);
    const command = {
      type: "command",
      name: "send_message",
      message_id: "remote-1",
      payload: {
        text: "continue",
        target_session_id: "preloop-uuid",
        session_source_id: "native-session",
      },
    };
    for (let i = 0; i < 2; i++) {
      const reply = once(socket, "message");
      socket.send(JSON.stringify(command));
      const result = JSON.parse(String((await reply)[0]));
      assert.equal(result.name, "command_result");
      assert.equal(result.payload.command_id, "remote-1");
    }
    assert.equal(sent, 1);
    const reply = once(socket, "message");
    socket.send(
      JSON.stringify({ ...command, name: "stop", message_id: "remote-2" }),
    );
    await reply;
    assert.equal(stopped, 1);
  } finally {
    bridge.stop();
    socket.terminate();
    await new Promise((resolve) => server.close(resolve));
  }
});

test("new-session requests cannot be redirected into an active conversation", async () => {
  const bridge = new Bridge(config);
  let called = false;
  bridge.sessions.set("one", {
    send: async () => {
      called = true;
    },
  });
  await assert.rejects(
    bridge.dispatch({
      name: "send_message",
      payload: { text: "new task", start_new_session: true },
    }),
    /active sessions/,
  );
  assert.equal(called, false);
});
