"""The get_execution tool as an agent meets it: catalog, gating, guards (#632).

The scope rules that decide one read live in flow_execution_read and are
tested against a database there. What is tested here is the surface: that the
catalog and the callable describe one tool, that a flow which has not enabled
it neither sees it nor gets served it, and that the tool refuses to read
anything at all outside a flow execution.
"""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP
from fastmcp.tools import Tool

from preloop.api.endpoints.tools import BUILTIN_TOOLS
from preloop.services.dynamic_fastmcp import DynamicFastMCP
from preloop.services.dynamic_mcp_server import UserContext
from preloop.services.initialize_mcp import initialize_mcp_with_tools
from preloop.tools.builtin_defs import GET_EXECUTION_TOOL

pytestmark = pytest.mark.asyncio

TOOL_NAME = "get_execution"


@pytest.fixture
def mcp_server():
    """Registrations only: no server, no provider connection."""
    return initialize_mcp_with_tools()


def _catalog_entry():
    entries = [entry for entry in BUILTIN_TOOLS if entry["name"] == TOOL_NAME]
    assert len(entries) == 1
    return entries[0]


def _flow_context(*, allowed, execution_id="flow-exec-1"):
    return UserContext(
        user_id="00000000-0000-0000-0000-000000000001",
        account_id="00000000-0000-0000-0000-000000000002",
        username="flow",
        has_tracker=True,
        enabled_default_tools=[],
        enabled_proxied_tools=[],
        tracker_types=["github"],
        flow_execution_id=execution_id,
        allowed_flow_tools=list(allowed),
    )


# --- the catalog and the callable describe one tool ------------------------


async def test_the_catalog_entry_and_the_callable_cannot_drift(mcp_server):
    """One definition (builtin_defs) reaches both the REST list and MCP."""
    entry = _catalog_entry()
    tool = await mcp_server.get_tool(TOOL_NAME)
    assert tool is not None
    assert tool.description == entry["description"]
    assert tool.parameters == entry["schema"]
    assert tool.parameters["additionalProperties"] is False

    parameters = {
        key: parameter
        for key, parameter in inspect.signature(tool.fn).parameters.items()
        if key != "ctx"
    }
    assert set(parameters) == set(entry["schema"]["properties"])
    required = {
        key
        for key, parameter in parameters.items()
        if parameter.default is inspect.Parameter.empty
    }
    assert required == set(entry["schema"]["required"]) == {"execution_id"}


async def test_the_result_payload_is_opt_in():
    """Polling a child should not drag its result into context every turn."""
    tool = GET_EXECUTION_TOOL
    include = tool["schema"]["properties"]["include_result"]
    assert include["type"] == "boolean"
    assert "include_result" not in tool["schema"]["required"]


async def test_the_tool_is_off_unless_a_flow_selects_it():
    """Reading execution rows is for flows that delegate, not for every flow."""
    assert _catalog_entry()["default_enabled"] is False
    assert GET_EXECUTION_TOOL["default_enabled"] is False
    assert _catalog_entry()["requires_tracker"] is False


async def test_the_description_tells_the_agent_what_a_refusal_looks_like():
    """A refusal is a record with a reason, not prose to parse."""
    description = _catalog_entry()["description"]
    assert "execution_not_found" in description
    assert "TASK_STATE_REJECTED" in description
    assert "preloop.ai/truncated" in description


# --- the flow allow-list decides whether the tool exists at all ------------


def _list_tools_patches(offered):
    db = MagicMock()
    db.close = MagicMock()
    return (
        patch(
            "preloop.services.dynamic_fastmcp.get_db",
            side_effect=lambda: iter([db]),
        ),
        patch(
            "preloop.services.mcp_tool_discovery._get_proxied_tools_sync",
            return_value=[],
        ),
        patch(
            "preloop.services.dynamic_fastmcp.crud_tool_configuration."
            "get_multi_by_account",
            return_value=[],
        ),
        patch(
            "preloop.models.crud.crud_account.get", return_value=MagicMock(meta_data={})
        ),
        patch.object(FastMCP, "list_tools", new=AsyncMock(return_value=offered)),
    )


async def test_a_flow_that_did_not_enable_the_tool_is_not_offered_it():
    """The allow-list is the offer: an unselected tool is not in the list."""
    mcp = DynamicFastMCP("test-mcp")
    mcp._user_context_provider = lambda: _flow_context(allowed=["get_issue"])
    offered = [
        Tool(name=TOOL_NAME, description="Read an execution", parameters={}),
        Tool(name="get_issue", description="Get issue", parameters={}),
    ]

    patches = _list_tools_patches(offered)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = await mcp.list_tools()

    assert {tool.name for tool in result} == {"get_issue"}


async def test_a_flow_that_enabled_the_tool_is_offered_it_despite_default_off():
    """Selecting the tool on the flow is the opt in the default expects."""
    mcp = DynamicFastMCP("test-mcp")
    mcp._user_context_provider = lambda: _flow_context(allowed=[TOOL_NAME, "get_issue"])
    offered = [
        Tool(name=TOOL_NAME, description="Read an execution", parameters={}),
        Tool(name="get_issue", description="Get issue", parameters={}),
    ]

    patches = _list_tools_patches(offered)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = await mcp.list_tools()

    assert {tool.name for tool in result} == {TOOL_NAME, "get_issue"}


def _call_tool_patches(available_names, *, policy=("allow", None, None)):
    db = MagicMock()
    db.close = MagicMock()
    available = [
        SimpleNamespace(name=name, description="", parameters={})
        for name in available_names
    ]
    return (
        patch(
            "preloop.services.dynamic_fastmcp.get_db",
            side_effect=lambda: iter([db]),
        ),
        patch(
            "preloop.services.dynamic_fastmcp.kill_switch_service.tools_halted",
            return_value=False,
        ),
        patch(
            "preloop.services.dynamic_fastmcp.crud_tool_configuration."
            "get_multi_by_account",
            return_value=[],
        ),
        patch(
            "preloop.services.policy_evaluator.evaluate_policy_async",
            new=AsyncMock(return_value=policy),
        ),
        available,
    )


async def test_calling_the_tool_without_enabling_it_is_refused_at_the_boundary():
    """Naming a tool the flow does not have is denied before dispatch."""
    mcp = DynamicFastMCP("test-mcp")
    mcp._user_context_provider = lambda: _flow_context(allowed=["get_issue"])
    db_patch, halt_patch, config_patch, policy_patch, available = _call_tool_patches(
        ["get_issue"]
    )

    with (
        db_patch,
        halt_patch,
        config_patch,
        policy_patch,
        patch.object(mcp, "list_tools", new=AsyncMock(return_value=available)),
        patch.object(
            mcp.__class__.__bases__[0],
            "call_tool",
            new=AsyncMock(),
            create=True,
        ) as dispatch,
    ):
        result = await mcp.call_tool(TOOL_NAME, {"execution_id": "abc"})

    assert "Access denied" in result.content[0].text
    dispatch.assert_not_called()


async def test_a_policy_deny_outranks_a_flow_that_enabled_the_tool():
    """The flow permits the read; a CEL deny still stops the call."""
    mcp = DynamicFastMCP("test-mcp")
    mcp._user_context_provider = lambda: _flow_context(allowed=[TOOL_NAME])
    db_patch, halt_patch, config_patch, policy_patch, available = _call_tool_patches(
        [TOOL_NAME], policy=("deny", None, "execution reads are off this week")
    )

    with (
        db_patch,
        halt_patch,
        config_patch,
        policy_patch,
        patch.object(mcp, "list_tools", new=AsyncMock(return_value=available)),
        patch.object(
            mcp.__class__.__bases__[0],
            "call_tool",
            new=AsyncMock(),
            create=True,
        ) as dispatch,
    ):
        result = await mcp.call_tool(TOOL_NAME, {"execution_id": "abc"})

    assert "Access denied" in result.content[0].text
    assert "execution reads are off this week" in result.content[0].text
    dispatch.assert_not_called()


# --- the tool function's own guards ---------------------------------------


async def test_without_a_user_context_nothing_is_read(mcp_server, monkeypatch):
    """No identity, no read, and no approval request either."""
    tool = await mcp_server.get_tool(TOOL_NAME)
    read = MagicMock()
    approval = AsyncMock()
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp_http.get_current_user_context",
        lambda: None,
    )
    monkeypatch.setattr("preloop.services.initialize_mcp.require_approval", approval)
    monkeypatch.setattr("preloop.services.flow_execution_read.read_execution", read)

    result = await tool.fn(execution_id="abc")

    assert result.startswith("Error")
    approval.assert_not_awaited()
    read.assert_not_called()


async def test_a_session_that_is_not_a_flow_execution_cannot_read(
    mcp_server, monkeypatch
):
    """A chat session has no execution of its own, so it has no descendants."""
    tool = await mcp_server.get_tool(TOOL_NAME)
    read = MagicMock()
    approval = AsyncMock()
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp_http.get_current_user_context",
        lambda: SimpleNamespace(
            account_id="acct", user_id="user", flow_execution_id=None
        ),
    )
    monkeypatch.setattr("preloop.services.initialize_mcp.require_approval", approval)
    monkeypatch.setattr("preloop.services.flow_execution_read.read_execution", read)

    result = await tool.fn(execution_id="abc")

    assert result.startswith("Error")
    assert "flow execution" in result
    approval.assert_not_awaited()
    read.assert_not_called()


async def test_an_approval_denial_stops_the_read(mcp_server, monkeypatch):
    """A workflow that declines the call is final: nothing is read."""
    tool = await mcp_server.get_tool(TOOL_NAME)
    read = MagicMock()
    approval = AsyncMock(return_value=(False, "Denied by configured policy"))
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp_http.get_current_user_context",
        lambda: SimpleNamespace(
            account_id="acct",
            user_id="user",
            flow_execution_id="exec-1",
            runtime_session_id=None,
            api_key_id=None,
            api_key_name=None,
        ),
    )
    monkeypatch.setattr("preloop.services.initialize_mcp.require_approval", approval)
    monkeypatch.setattr("preloop.services.flow_execution_read.read_execution", read)

    result = await tool.fn(execution_id="abc")

    assert result == "Denied by configured policy"
    read.assert_not_called()


async def test_the_calling_execution_comes_from_the_session_not_the_arguments(
    mcp_server, monkeypatch
):
    """An agent chooses what to read, never who is reading."""
    tool = await mcp_server.get_tool(TOOL_NAME)
    record = {"id": "child", "status": {"state": "TASK_STATE_WORKING"}}
    read = MagicMock(return_value=record)
    session = MagicMock()
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp_http.get_current_user_context",
        lambda: SimpleNamespace(
            account_id="acct",
            user_id="user",
            flow_execution_id="caller-exec",
            runtime_session_id="rs-1",
            api_key_id="key-1",
            api_key_name="runtime token",
        ),
    )
    monkeypatch.setattr(
        "preloop.services.initialize_mcp.require_approval",
        AsyncMock(return_value=(True, None)),
    )
    monkeypatch.setattr("preloop.services.flow_execution_read.read_execution", read)
    monkeypatch.setattr(
        "preloop.models.db.session.get_db_session", lambda: iter([session])
    )

    result = await tool.fn(execution_id="child-exec", include_result=True)

    assert json.loads(result) == record
    kwargs = read.call_args.kwargs
    assert kwargs["caller_execution_id"] == "caller-exec"
    assert kwargs["reference"] == "child-exec"
    assert kwargs["include_result"] is True
    assert kwargs["account_id"] == "acct"
    session.close.assert_called_once()


async def test_a_caller_without_an_execution_row_is_told_so(mcp_server, monkeypatch):
    """The service refuses to judge; the tool says that in one line."""
    from preloop.services.flow_delegation_call import DelegationUnavailableError

    tool = await mcp_server.get_tool(TOOL_NAME)
    session = MagicMock()
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp_http.get_current_user_context",
        lambda: SimpleNamespace(
            account_id="acct",
            user_id="user",
            flow_execution_id="caller-exec",
            runtime_session_id=None,
            api_key_id=None,
            api_key_name=None,
        ),
    )
    monkeypatch.setattr(
        "preloop.services.initialize_mcp.require_approval",
        AsyncMock(return_value=(True, None)),
    )
    monkeypatch.setattr(
        "preloop.services.flow_execution_read.read_execution",
        MagicMock(side_effect=DelegationUnavailableError("not an execution")),
    )
    monkeypatch.setattr(
        "preloop.models.db.session.get_db_session", lambda: iter([session])
    )

    result = await tool.fn(execution_id="child-exec")

    assert result == "Error: not an execution"
    session.close.assert_called_once()
