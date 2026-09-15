"""The run_flow tool as an agent meets it: catalog, gating, error paths (#630).

The rules that decide one delegation live in flow_delegation_call and are
tested against a database there. What is tested here is everything around
them: that the catalog and the callable describe the same tool, that a flow
which did not select run_flow is neither offered it nor served it, that a
policy deny outranks an allowlist that permits the call, and that the tool
refuses to do anything at all outside a flow execution.
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
from preloop.tools.builtin_defs import RUN_FLOW_TOOL

pytestmark = pytest.mark.asyncio

TOOL_NAME = "run_flow"


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
    assert required == set(entry["schema"]["required"]) == {"flow"}


async def test_the_tool_is_off_unless_a_flow_selects_it():
    """Delegation spends budget, so no agent gets it by default (#128)."""
    assert _catalog_entry()["default_enabled"] is False
    assert RUN_FLOW_TOOL["default_enabled"] is False
    assert _catalog_entry()["requires_tracker"] is False


async def test_the_description_names_the_refusal_reasons_an_agent_will_see():
    """A refusal is a record with a reason, not prose to parse."""
    description = _catalog_entry()["description"]
    for reason in (
        "flow_not_found",
        "flow_not_callable",
        "tool_not_allowed",
        "depth_exceeded",
        "cycle_detected",
        "fanout_exceeded",
        "budget_exceeded",
    ):
        assert reason in description


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


async def test_a_flow_that_did_not_select_run_flow_is_not_offered_it():
    """The allow-list is the offer: an unselected tool is not in the list."""
    mcp = DynamicFastMCP("test-mcp")
    mcp._user_context_provider = lambda: _flow_context(allowed=["get_issue"])
    offered = [
        Tool(name=TOOL_NAME, description="Run a flow", parameters={}),
        Tool(name="get_issue", description="Get issue", parameters={}),
    ]

    patches = _list_tools_patches(offered)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = await mcp.list_tools()

    assert {tool.name for tool in result} == {"get_issue"}


async def test_a_flow_that_selected_run_flow_is_offered_it_despite_default_off():
    """Selecting the tool on the flow is the opt in the default expects."""
    mcp = DynamicFastMCP("test-mcp")
    mcp._user_context_provider = lambda: _flow_context(allowed=[TOOL_NAME, "get_issue"])
    offered = [
        Tool(name=TOOL_NAME, description="Run a flow", parameters={}),
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


async def test_calling_run_flow_without_selecting_it_is_refused_at_the_boundary():
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
        result = await mcp.call_tool(TOOL_NAME, {"flow": "Child Flow"})

    assert "Access denied" in result.content[0].text
    dispatch.assert_not_called()


async def test_a_policy_deny_outranks_a_flow_that_selected_the_tool():
    """The allowlist permits delegation; a CEL deny still stops the call."""
    mcp = DynamicFastMCP("test-mcp")
    mcp._user_context_provider = lambda: _flow_context(allowed=[TOOL_NAME])
    db_patch, halt_patch, config_patch, policy_patch, available = _call_tool_patches(
        [TOOL_NAME], policy=("deny", None, "delegation is off this week")
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
        result = await mcp.call_tool(TOOL_NAME, {"flow": "Child Flow"})

    assert "Access denied" in result.content[0].text
    assert "delegation is off this week" in result.content[0].text
    dispatch.assert_not_called()


async def test_a_permitted_call_reaches_dispatch():
    """The same setup without the deny does reach the tool function."""
    mcp = DynamicFastMCP("test-mcp")
    mcp._user_context_provider = lambda: _flow_context(allowed=[TOOL_NAME])
    db_patch, halt_patch, config_patch, policy_patch, available = _call_tool_patches(
        [TOOL_NAME]
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
            new=AsyncMock(return_value=MagicMock(content=[MagicMock(text="{}")])),
            create=True,
        ) as dispatch,
    ):
        await mcp.call_tool(TOOL_NAME, {"flow": "Child Flow"})

    dispatch.assert_called_once()


# --- the tool function's own guards ---------------------------------------


async def test_without_a_user_context_nothing_is_delegated(mcp_server, monkeypatch):
    """No identity, no delegation, and no approval request either."""
    tool = await mcp_server.get_tool(TOOL_NAME)
    delegate = AsyncMock()
    approval = AsyncMock()
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp_http.get_current_user_context",
        lambda: None,
    )
    monkeypatch.setattr("preloop.services.initialize_mcp.require_approval", approval)
    monkeypatch.setattr("preloop.services.flow_delegation_call.delegate_flow", delegate)

    result = await tool.fn(flow="Child Flow")

    assert result.startswith("Error")
    approval.assert_not_awaited()
    delegate.assert_not_awaited()


async def test_a_session_that_is_not_a_flow_execution_cannot_delegate(
    mcp_server, monkeypatch
):
    """A chat session has no parent execution, so there is nothing to nest."""
    tool = await mcp_server.get_tool(TOOL_NAME)
    delegate = AsyncMock()
    approval = AsyncMock()
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp_http.get_current_user_context",
        lambda: SimpleNamespace(
            account_id="acct", user_id="user", flow_execution_id=None
        ),
    )
    monkeypatch.setattr("preloop.services.initialize_mcp.require_approval", approval)
    monkeypatch.setattr("preloop.services.flow_delegation_call.delegate_flow", delegate)

    result = await tool.fn(flow="Child Flow")

    assert result.startswith("Error")
    assert "flow execution" in result
    approval.assert_not_awaited()
    delegate.assert_not_awaited()


async def test_an_approval_denial_stops_the_delegation(mcp_server, monkeypatch):
    """A workflow that declines the call is final: no child is created."""
    tool = await mcp_server.get_tool(TOOL_NAME)
    delegate = AsyncMock()
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
    monkeypatch.setattr("preloop.services.flow_delegation_call.delegate_flow", delegate)

    result = await tool.fn(flow="Child Flow")

    assert result == "Denied by configured policy"
    delegate.assert_not_awaited()
    assert approval.await_args.kwargs["tool_name"] == TOOL_NAME
    assert approval.await_args.kwargs["tool_source"] == "builtin"


async def test_the_record_is_returned_as_json(mcp_server, monkeypatch):
    """The agent reads one JSON record, refusal or not."""
    tool = await mcp_server.get_tool(TOOL_NAME)
    record = {"id": "task-1", "status": {"state": "TASK_STATE_SUBMITTED"}}
    delegate = AsyncMock(return_value=record)
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp_http.get_current_user_context",
        lambda: SimpleNamespace(
            account_id="acct",
            user_id="user",
            flow_execution_id="exec-1",
            runtime_session_id="session-1",
            api_key_id="key-1",
            api_key_name="runtime",
        ),
    )
    monkeypatch.setattr(
        "preloop.services.initialize_mcp.require_approval",
        AsyncMock(return_value=(True, None)),
    )
    monkeypatch.setattr("preloop.services.flow_delegation_call.delegate_flow", delegate)
    monkeypatch.setattr(
        "preloop.models.db.session.get_db_session", lambda: iter([MagicMock()])
    )

    result = await tool.fn(flow="Child Flow", payload={"k": "v"}, label="one")

    assert json.loads(result) == record
    kwargs = delegate.await_args.kwargs
    assert kwargs["reference"] == "Child Flow"
    assert kwargs["parent_execution_id"] == "exec-1"
    assert kwargs["payload"] == {"k": "v"}
    assert kwargs["label"] == "one"


async def test_a_halted_account_answers_in_the_halts_own_words(mcp_server, monkeypatch):
    """The kill switch refuses a delegated start like any other start."""
    from preloop.services.kill_switch import FlowHaltActiveError

    tool = await mcp_server.get_tool(TOOL_NAME)
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
    monkeypatch.setattr(
        "preloop.services.initialize_mcp.require_approval",
        AsyncMock(return_value=(True, None)),
    )
    monkeypatch.setattr(
        "preloop.services.flow_delegation_call.delegate_flow",
        AsyncMock(side_effect=FlowHaltActiveError("account halted by kill switch")),
    )
    monkeypatch.setattr(
        "preloop.models.db.session.get_db_session", lambda: iter([MagicMock()])
    )

    result = await tool.fn(flow="Child Flow")

    assert result == "Error: account halted by kill switch"


# --- waiting for children (#633) ------------------------------------------


def _flow_identity():
    return SimpleNamespace(
        account_id="acct",
        user_id="user",
        flow_execution_id="exec-1",
        runtime_session_id=None,
        api_key_id=None,
        api_key_name=None,
    )


async def test_wait_asks_for_the_children_of_this_execution(mcp_server, monkeypatch):
    """The wait is about the caller's whole fan out, not this one child."""
    tool = await mcp_server.get_tool(TOOL_NAME)
    record = {
        "id": "task-1",
        "status": {"state": "TASK_STATE_SUBMITTED"},
        "metadata": {"preloop.ai/executionId": "child-1"},
    }
    parked = json.dumps({"status": "parked_for_children"})
    wait = AsyncMock(return_value=parked)
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp_http.get_current_user_context",
        _flow_identity,
    )
    monkeypatch.setattr(
        "preloop.services.initialize_mcp.require_approval",
        AsyncMock(return_value=(True, None)),
    )
    monkeypatch.setattr(
        "preloop.services.flow_delegation_call.delegate_flow",
        AsyncMock(return_value=record),
    )
    monkeypatch.setattr("preloop.services.flow_child_wait.wait_for_children", wait)
    monkeypatch.setattr(
        "preloop.models.db.session.get_db_session", lambda: iter([MagicMock()])
    )

    result = await tool.fn(flow="Child Flow", wait=True)

    assert result == parked
    assert wait.await_args.kwargs["parent_execution_id"] == "exec-1"
    assert wait.await_args.kwargs["account_id"] == "acct"


async def test_a_refused_call_with_wait_still_waits_for_siblings(
    mcp_server, monkeypatch
):
    """The last fan-out call can be the refused one; siblings still need the wait."""
    tool = await mcp_server.get_tool(TOOL_NAME)
    record = {
        "id": "attempt-1",
        "status": {"state": "TASK_STATE_REJECTED"},
        "metadata": {"preloop.ai/refusalReason": "fanout_exceeded"},
    }
    parked = json.dumps({"status": "parked_for_children"})
    wait = AsyncMock(return_value=parked)
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp_http.get_current_user_context",
        _flow_identity,
    )
    monkeypatch.setattr(
        "preloop.services.initialize_mcp.require_approval",
        AsyncMock(return_value=(True, None)),
    )
    monkeypatch.setattr(
        "preloop.services.flow_delegation_call.delegate_flow",
        AsyncMock(return_value=record),
    )
    monkeypatch.setattr("preloop.services.flow_child_wait.wait_for_children", wait)
    monkeypatch.setattr(
        "preloop.models.db.session.get_db_session", lambda: iter([MagicMock()])
    )

    result = await tool.fn(flow="Child Flow", wait=True)

    assert result == parked
    wait.assert_awaited_once()
    assert wait.await_args.kwargs["parent_execution_id"] == "exec-1"


async def test_the_default_call_still_never_waits(mcp_server, monkeypatch):
    """Asynchronous by default: the old behaviour is the unchanged one."""
    tool = await mcp_server.get_tool(TOOL_NAME)
    wait = AsyncMock()
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp_http.get_current_user_context",
        _flow_identity,
    )
    monkeypatch.setattr(
        "preloop.services.initialize_mcp.require_approval",
        AsyncMock(return_value=(True, None)),
    )
    monkeypatch.setattr(
        "preloop.services.flow_delegation_call.delegate_flow",
        AsyncMock(return_value={"id": "task-1"}),
    )
    monkeypatch.setattr("preloop.services.flow_child_wait.wait_for_children", wait)
    monkeypatch.setattr(
        "preloop.models.db.session.get_db_session", lambda: iter([MagicMock()])
    )

    await tool.fn(flow="Child Flow")

    wait.assert_not_awaited()


async def test_the_wait_is_documented_where_an_agent_will_read_it():
    """The closed schema carries the argument and its honest limitation."""
    entry = _catalog_entry()
    assert entry["schema"]["properties"]["wait"]["type"] == "boolean"
    assert "next turn" in entry["schema"]["properties"]["wait"]["description"]
    assert "parked" in entry["description"]
