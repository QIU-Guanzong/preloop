"""Opt-in worker test: build the two harness Dockerfiles before running."""

import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import pytest

from preloop.agents.runner_launch import build_runner_launch

pytestmark = pytest.mark.skipif(
    os.getenv("PRELOOP_TEST_HARNESS_DOCKER") != "1",
    reason="Set PRELOOP_TEST_HARNESS_DOCKER=1 with both test worker images built",
)


@pytest.mark.parametrize("kind", ["pi", "deepseek"])
@pytest.mark.parametrize("transport", ["hosted", "private"])
@pytest.mark.asyncio
async def test_real_worker_bootstrap(
    kind: str, transport: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A private launch drops root, writes in a fresh volume, and gates a tool."""
    approvals: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler protocol
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/api/v1/agents/permission-check":
                approvals.append(body)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"decision":"allow"}')
                return
            result = next(
                (message for message in body["messages"] if message["role"] == "tool"),
                None,
            )
            if result:
                results.append(result)
                delta = {
                    "role": "assistant",
                    "content": "worker-smoke-complete\nFLOW_EXECUTION_SUCCESS",
                }
            else:
                command = (
                    'test "$(id -u)" = 10000 && '
                    'printf \'{"status":"success"}\' > /workspace/result.json && '
                    "echo worker-permissions-ok"
                )
                delta = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-worker",
                            "type": "function",
                            "function": {
                                "name": "bash",
                                "arguments": json.dumps(
                                    {
                                        "command": command,
                                        "description": "Check worker permissions",
                                    }
                                ),
                            },
                        }
                    ],
                }
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for value, finish in [
                (delta, None),
                ({}, "stop" if result else "tool_calls"),
            ]:
                chunk = {
                    "id": "worker",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "test",
                    "choices": [{"index": 0, "delta": value, "finish_reason": finish}],
                }
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")

    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        api = f"http://host.docker.internal:{server.server_port}"
        monkeypatch.setenv("PRELOOP_DISABLE_TELEMETRY", "true")
        launch = await build_runner_launch(
            {
                "agent_type": kind,
                "prompt": "Check workspace permissions. " * 20000,
                "agent_config": {"native_tool_approvals": True},
                "model_gateway_enabled": True,
                "model_gateway_model_alias": "test",
                "model_gateway_token": "test-model",
                "account_api_token": "test-api",
            }
        )
        env = {**launch["env"], "PRELOOP_URL": api}
        script = launch["script"]
        if transport == "private":
            # Exercise the actual CLI wrapper: it pipes the script into bash,
            # unlike the hosted Docker path's bash -c invocation.
            source = (
                Path(__file__).parents[3] / "cli/internal/cmd/runner_launch.go"
            ).read_text()
            script = source.split("const runnerBootstrap = `", 1)[1].split("`", 1)[0]
            env["PRELOOP_RUNNER_SCRIPT"] = launch["script"]
        run = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--user",
                "0:0",
                "--entrypoint",
                "/bin/bash",
                "--add-host",
                "host.docker.internal:host-gateway",
                "--mount",
                "type=volume,destination=/workspace",
                *[arg for key in env for arg in ("--env", key)],
                f"preloop-harness-{kind}:test",
                "-c",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=90,
            env={**os.environ, **env},
        )
        assert run.returncode == 0, run.stdout + run.stderr
        if transport == "private":
            assert "PRELOOP_RUNNER_RESULT_V1 " in run.stdout
        assert "worker-smoke-complete" in run.stdout
        lines = run.stdout.splitlines()
        assert "PRELOOP_AGENT_EXEC_START" in lines
        assert "FLOW_EXECUTION_SUCCESS" in lines
        assert lines.index("PRELOOP_AGENT_EXEC_START") < lines.index(
            "FLOW_EXECUTION_SUCCESS"
        )
        assert len(approvals) == 1
        assert approvals[0]["source"] == kind
        assert approvals[0]["tool_name"] == "Bash"
        assert len(results) == 1
        assert "worker-permissions-ok" in json.dumps(results[0])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
