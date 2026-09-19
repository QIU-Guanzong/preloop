import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import WebSocket from "ws";

export function loadConfig(runtime, path = process.env.PRELOOP_HARNESS_CONFIG) {
  return JSON.parse(
    readFileSync(
      path ||
        (runtime === "pi"
          ? join(
              process.env.PI_CODING_AGENT_DIR || join(homedir(), ".pi/agent"),
              "preloop.json",
            )
          : join(
              process.env.DSH_HOME || join(homedir(), ".dsh"),
              "preloop.json",
            )),
      "utf8",
    ),
  );
}

export function canonicalToolName(name) {
  return (
    {
      bash: "Bash",
      shell: "Bash",
      read: "Read",
      write: "Write",
      edit: "Edit",
      grep: "Grep",
      find: "Glob",
      glob: "Glob",
      ls: "LS",
    }[name] || name
  );
}

/** Shared authenticated transport. Only a positive server decision permits a tool. */
export class Bridge {
  constructor(document, { fetchImpl = fetch } = {}) {
    this.config = document.preloop?.control || {};
    this.fetch = fetchImpl;
    this.sessions = new Map();
    this.commands = new Map();
    this.stopped = false;
    this.attempts = 0;
  }

  async check(name, input, sessionId, signal) {
    if (
      this.config.native_tool_approvals !== "on" ||
      name.startsWith("mcp__preloop__") ||
      name.startsWith("mcp__preloop-mcp__")
    ) {
      return { decision: "allow" };
    }
    try {
      const url = new URL(this.config.control_ws_url);
      url.protocol = url.protocol === "wss:" ? "https:" : "http:";
      url.pathname = url.pathname.replace(
        /\/control\/ws$/,
        "/permission-check",
      );
      const timeout = AbortSignal.timeout(
        this.config.approval_timeout_ms || 600_000,
      );
      const response = await this.fetch(url.toString(), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${this.config.bearer_token}`,
        },
        signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
        body: JSON.stringify({
          source: this.config.runtime,
          tool_name: canonicalToolName(name),
          tool_input: {
            ...input,
            ...(input?.path && !input.file_path
              ? { file_path: input.path }
              : {}),
          },
          session_id: sessionId,
          cwd: process.cwd(),
          evaluation_phase: "permission_request",
        }),
      });
      if (!response.ok) throw new Error("Permission service unavailable");
      const result = await response.json();
      if (!["allow", "deny"].includes(result.decision))
        throw new Error("Invalid permission decision");
      return result;
    } catch {
      return {
        decision: "deny",
        reason: "Preloop permission check failed or was cancelled",
      };
    }
  }

  async lifecycle(eventType, sessionId, metadata = {}) {
    if (!this.config.managed_agent_id || !this.config.bearer_token) return;
    try {
      const url = new URL(this.config.control_ws_url);
      url.protocol = url.protocol === "wss:" ? "https:" : "http:";
      url.pathname = url.pathname.replace(
        /\/agents\/control\/ws$/,
        "/usage/ingest",
      );
      await this.fetch(url.toString(), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${this.config.bearer_token}`,
        },
        signal: AbortSignal.timeout(3000),
        body: JSON.stringify({
          source: this.config.runtime,
          agent_id: this.config.managed_agent_id,
          records: [
            {
              external_id: randomUUID(),
              conversation_id: sessionId,
              timestamp: new Date().toISOString(),
              event_type: eventType,
              metadata,
            },
          ],
        }),
      });
    } catch {
      /* Observability must not block the harness or weaken the approval gate. */
    }
  }

  start() {
    if (
      this.stopped ||
      this.config.enabled === false ||
      !this.config.control_ws_url ||
      !this.config.bearer_token
    )
      return;
    const socket = new WebSocket(this.config.control_ws_url, {
      headers: { Authorization: `Bearer ${this.config.bearer_token}` },
    });
    this.socket = socket;
    socket.on("open", () => {
      this.attempts = 0;
      this.presence();
      this.heartbeat = setInterval(
        () => this.emit("status", "heartbeat", { status: "online" }),
        30_000,
      );
      this.heartbeat.unref();
    });
    socket.on("message", async (data) => {
      let command;
      try {
        command = JSON.parse(String(data));
      } catch {
        return;
      }
      if (command.type !== "command") return;
      try {
        const result = await this.dispatch(command);
        this.emit(
          "status",
          "command_result",
          { command_id: command.message_id, status: "completed", result },
          command.message_id,
        );
      } catch (error) {
        this.emit(
          "status",
          "command_error",
          {
            command_id: command.message_id,
            status: "failed",
            error: error.message,
          },
          command.message_id,
        );
      }
    });
    socket.on("error", () => {}); // close owns reconnect; never log bearer URLs.
    socket.on("close", (code) => {
      clearInterval(this.heartbeat);
      if (code === 4000) this.stopped = true;
      if (!this.stopped) {
        this.reconnect = setTimeout(
          () => this.start(),
          Math.min(30_000, 2000 * 2 ** this.attempts++),
        );
        this.reconnect.unref();
      }
    });
  }

  presence() {
    const active =
      this.sessions.size > 0 && this.config.remote_control_enabled !== false;
    this.emit("presence", "capabilities", {
      status: "online",
      protocol: "preloop.agent_control.v1",
      runtime: this.config.runtime,
      runtime_principal_id: this.config.runtime_principal_id,
      runtime_principal_name: this.config.runtime_principal_name,
      capabilities: {
        new_session: false,
        existing_session: active,
        text: active,
        voice: false,
        interrupt: active,
        tool_approval: this.config.native_tool_approvals === "on",
      },
    });
  }

  emit(type, name, payload, messageId = randomUUID()) {
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(
        JSON.stringify({ type, name, message_id: messageId, payload }),
      );
    }
  }

  async dispatch(command) {
    const id = command.message_id;
    if (id && this.commands.has(id)) return this.commands.get(id);
    const pending = this.execute(command);
    if (id) {
      this.commands.set(id, pending);
      while (this.commands.size > 512)
        this.commands.delete(this.commands.keys().next().value);
    }
    return pending;
  }

  async execute(command) {
    if (!["send_message", "stop", "interrupt"].includes(command.name))
      throw new Error("Unsupported control command");
    if (this.config.remote_control_enabled === false)
      throw new Error("Remote control disabled");
    const payload = command.payload || {};
    if (
      payload.start_new_session ||
      payload.spawn_worktree ||
      (payload.input_mode && payload.input_mode !== "text")
    ) {
      throw new Error("Only text control of active sessions is supported");
    }
    const targets = [
      payload.session_reference,
      payload.session_source_id,
      payload.target_session_id,
      payload.runtime_session_id,
    ].filter((value) => typeof value === "string" && value.trim());
    const target = targets.find((value) => this.sessions.has(value));
    const session = targets.length
      ? this.sessions.get(target)
      : this.sessions.size === 1
        ? this.sessions.values().next().value
        : undefined;
    if (!session)
      throw new Error("Target session is not active or is ambiguous");
    if (command.name !== "send_message") {
      await session.stop();
      return "interrupted";
    }
    const text = payload.text || payload.message;
    if (typeof text !== "string" || !text.trim())
      throw new Error("A message is required");
    if (payload.interrupt) await session.stop();
    await session.send(text);
    return "queued";
  }

  stop() {
    this.stopped = true;
    clearInterval(this.heartbeat);
    clearTimeout(this.reconnect);
    this.socket?.close();
  }
}
