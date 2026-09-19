"""Launch contracts for Pi and DeepSeek Harness."""

import json
import subprocess
from unittest.mock import AsyncMock, patch

import pytest

from preloop.agents.factory import create_agent_executor, SUPPORTED_AGENT_TYPES
from preloop.agents.harness import DeepSeekAgent, PiAgent
from preloop.utils.execve_limits import PROMPT_FILE_PATH


@pytest.mark.parametrize("kind,cls", [("pi", PiAgent), ("deepseek", DeepSeekAgent)])
@pytest.mark.asyncio
async def test_gateway_launch_and_identity(kind: str, cls: type) -> None:
    agent = create_agent_executor(kind, {})
    assert kind in SUPPORTED_AGENT_TYPES
    assert isinstance(agent, cls)
    context = {
        "prompt": "untrusted $(touch /tmp/should-not-exist)\n" * 20000,
        "model_gateway_enabled": True,
        "model_gateway_model_alias": "provider/test-model",
        "model_gateway_url": "https://example.com/openai/v1",
        "model_gateway_token": "test-gateway-token",
        "account_api_token": "test-mcp-token",
        "allowed_mcp_servers": ["preloop-mcp"],
    }
    env = await agent._prepare_environment(context)
    assert json.loads(env["PRELOOP_HARNESS_MODEL"])["models"] == [
        {"id": "provider/test-model"}
    ]
    assert env["PRELOOP_MODEL_TOKEN"] == "test-gateway-token"
    mcp = json.loads(env["MCP_CONFIG_JSON"])
    assert (
        mcp["mcpServers"]["preloop-mcp"]["headers"]["Authorization"]
        == "Bearer test-mcp-token"
    )
    script = agent._build_harness_script(context)
    assert "$(touch" not in script
    assert "test-gateway-token" not in script
    assert f"< {PROMPT_FILE_PATH}" in script
    assert 'exit "$PRELOOP_HARNESS_EXIT"' in script
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    with patch(
        "preloop.agents.container.ContainerAgentExecutor.start", new_callable=AsyncMock
    ) as start:
        start.return_value = "worker-id"
        assert await agent.start(context) == "worker-id"
        launch = start.call_args.args[0]
        assert launch["_container_command"] == ["/bin/bash"]
        assert launch["_agent_env"]["PRELOOP_HARNESS"] == kind


@pytest.mark.parametrize("cls", [PiAgent, DeepSeekAgent])
@pytest.mark.asyncio
async def test_missing_model_or_credential_fails_before_launch(cls: type) -> None:
    agent = cls({})
    with pytest.raises(ValueError, match="No model"):
        await agent._prepare_environment({})
    with pytest.raises(ValueError, match="credential"):
        await agent._prepare_environment({"model_identifier": "test-model"})


@pytest.mark.asyncio
async def test_custom_provider_and_native_approvals() -> None:
    env = await DeepSeekAgent({})._prepare_environment(
        {
            "model_identifier": "custom-model",
            "model_provider": "custom",
            "model_endpoint": "https://example.com/v1",
            "model_api_key": "test-key",
            "agent_config": {"native_tool_approvals": True},
            "model_parameters": {"max_output_tokens": 512},
        }
    )
    assert env["PRELOOP_NATIVE_APPROVALS"] == "on"
    assert json.loads(env["PRELOOP_HARNESS_MODEL"])["models"][0]["maxTokens"] == 512
    assert (
        json.loads(env["PRELOOP_HARNESS_MODEL"])["baseUrl"] == "https://example.com/v1"
    )


@pytest.mark.parametrize("kind", ["pi", "deepseek"])
@pytest.mark.asyncio
async def test_private_runner_uses_same_bootstrap(kind: str) -> None:
    from preloop.agents.runner_launch import (
        build_runner_launch,
        validate_runner_completion,
    )

    launch = await build_runner_launch(
        {
            "agent_type": kind,
            "prompt": "Write a result",
            "model_gateway_enabled": True,
            "model_gateway_model_alias": "provider/model",
            "model_gateway_token": "test-token",
            "account_api_token": "test-mcp",
            "allowed_mcp_servers": ["preloop-mcp"],
        }
    )
    assert launch["env"]["PRELOOP_HARNESS"] == kind
    assert (
        json.loads(launch["env"]["PRELOOP_HARNESS_MODEL"])["baseUrl"]
        == "${PRELOOP_URL}/openai/v1"
    )
    assert "bootstrap.mjs" in launch["script"]
    status, _, _ = validate_runner_completion(
        {
            "status": "SUCCEEDED",
            "exit_code": 0,
            "completion_protocol": "docker_v1",
            "launch_version": 1,
            "result": {"status": "success"},
        },
        leased_job={"launch_version": 1, "agent_type": kind},
    )
    assert status == "SUCCEEDED"
