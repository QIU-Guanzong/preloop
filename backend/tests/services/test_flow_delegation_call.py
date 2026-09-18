"""Call time delegation: the rules run_flow enforces before a child exists (#630).

Every refusal here is a rule that, if it failed open, would let an agent spend
an account's budget from inside a model turn, so each test asserts both the
refusal reason and that no execution row was created.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from preloop.a2a.delegation import (
    REFUSAL_REASONS,
    validate_delegation_task,
)
from preloop.models.crud import crud_account, crud_flow, crud_flow_execution
from preloop.models.models.flow_execution import FlowExecution
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.flow_delegation_call import (
    DelegationRefusedError,
    DelegationUnavailableError,
    ancestor_flow_ids,
    clamp_timeout,
    delegate_flow,
    evaluate_delegation,
    resolve_delegation_target,
)
from preloop.services.flow_trigger_service import FlowTriggerService
from preloop.services.kill_switch import (
    FlowHaltActiveError,
    invalidate_kill_switch_cache,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clear_kill_switch_cache():
    invalidate_kill_switch_cache()
    yield
    invalidate_kill_switch_cache()


def _flow(
    db_session,
    *,
    name,
    account_id,
    tools=("run_flow",),
    callable_flows=None,
    is_enabled=True,
):
    """Create a flow that may (or may not) delegate."""
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=name,
            prompt_template="t",
            agent_type="codex",
            agent_config={},
            allowed_mcp_tools=[{"tool_name": name} for name in tools],
        ),
        account_id=account_id,
    )
    flow.callable_flows = callable_flows
    flow.is_enabled = is_enabled
    db_session.flush()
    db_session.refresh(flow)
    return flow


def _execution(db_session, flow, *, parent=None, depth=0, root=None, status="RUNNING"):
    """Create one execution row, optionally as a child of ``parent``."""
    execution = crud_flow_execution.create(
        db_session,
        obj_in=FlowExecutionCreate(
            flow_id=flow.id,
            status=status,
            parent_execution_id=parent.id if parent is not None else None,
            root_execution_id=root.id if root is not None else None,
            delegation_depth=depth,
        ),
    )
    db_session.flush()
    return execution


def _other_account(db_session):
    """A second account, so account scoping can be asserted and not assumed."""
    return crud_account.create(
        db_session,
        obj_in={
            "organization_name": f"other-{uuid.uuid4().hex[:8]}",
            "is_active": True,
        },
    )


def _count_executions(db_session):
    return db_session.query(FlowExecution.id).count()


def _patch_dispatch():
    """Patch NATS and dispatch so a created child never actually runs."""
    return (
        patch(
            "preloop.services.flow_trigger_service.get_nats_client",
            new_callable=AsyncMock,
        ),
        patch.object(
            FlowTriggerService, "_start_flow_execution", new_callable=AsyncMock
        ),
    )


@pytest.fixture
def child_flow(db_session, test_user):
    """The delegation target: a plain flow in the calling account."""
    return _flow(db_session, name="Child Flow", account_id=test_user.account_id)


@pytest.fixture
def parent_flow(db_session, test_user, child_flow):
    """A flow with the tool enabled and the child on its allowlist."""
    return _flow(
        db_session,
        name="Parent Flow",
        account_id=test_user.account_id,
        callable_flows=[{"flow": "Child Flow"}],
    )


@pytest.fixture
def parent_execution(db_session, parent_flow):
    return _execution(db_session, parent_flow)


# --- the happy path --------------------------------------------------------


async def test_a_permitted_call_creates_exactly_one_child_with_lineage(
    db_session, test_user, parent_flow, child_flow, parent_execution
):
    """One child, carrying parent id, root id and depth 1."""
    before = _count_executions(db_session)
    nats_patch, dispatch_patch = _patch_dispatch()
    with nats_patch, dispatch_patch:
        record = await delegate_flow(
            db_session,
            account_id=str(test_user.account_id),
            parent_execution_id=parent_execution.id,
            reference="Child Flow",
            payload={"message": "hello"},
            label="unit one",
        )

    assert _count_executions(db_session) == before + 1
    child = crud_flow_execution.get(
        db_session, id=record["metadata"]["preloop.ai/executionId"]
    )
    assert str(child.flow_id) == str(child_flow.id)
    assert str(child.parent_execution_id) == str(parent_execution.id)
    assert str(child.root_execution_id) == str(parent_execution.id)
    assert child.delegation_depth == 1
    assert child.trigger_event_details["payload"] == {"message": "hello"}
    assert child.trigger_event_details["delegation"]["label"] == "unit one"
    assert child.trigger_event_details["source"] == "flow_delegation"


async def test_the_returned_record_is_a_valid_task_in_a_startable_state(
    db_session, test_user, parent_flow, child_flow, parent_execution
):
    """The record validates against the #625 task schema for what it fills."""
    nats_patch, dispatch_patch = _patch_dispatch()
    with nats_patch, dispatch_patch:
        record = await delegate_flow(
            db_session,
            account_id=str(test_user.account_id),
            parent_execution_id=parent_execution.id,
            reference="Child Flow",
        )

    validate_delegation_task(record)
    assert record["status"]["state"] in {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}
    assert record["contextId"] == str(parent_execution.id)
    assert record["metadata"]["preloop.ai/depth"] == 1
    assert record["metadata"]["preloop.ai/flowId"] == str(child_flow.id)
    assert "artifacts" not in record


async def test_a_grandchild_keeps_the_root_of_the_tree(
    db_session, test_user, parent_flow, child_flow, parent_execution
):
    """root_execution_id points at the first execution, not at the caller."""
    middle_flow = _flow(
        db_session,
        name="Middle Flow",
        account_id=test_user.account_id,
        callable_flows=[{"flow": "Child Flow"}],
    )
    middle = _execution(
        db_session, middle_flow, parent=parent_execution, depth=1, root=parent_execution
    )

    nats_patch, dispatch_patch = _patch_dispatch()
    with nats_patch, dispatch_patch:
        record = await delegate_flow(
            db_session,
            account_id=str(test_user.account_id),
            parent_execution_id=middle.id,
            reference="Child Flow",
        )

    child = crud_flow_execution.get(
        db_session, id=record["metadata"]["preloop.ai/executionId"]
    )
    assert str(child.parent_execution_id) == str(middle.id)
    assert str(child.root_execution_id) == str(parent_execution.id)
    assert child.delegation_depth == 2
    assert record["contextId"] == str(parent_execution.id)


# --- refusals, one test per reason ----------------------------------------


async def _refuse(db_session, account_id, execution, reference, **kwargs):
    """Delegate and assert nothing was created; return the refusal record."""
    before = _count_executions(db_session)
    nats_patch, dispatch_patch = _patch_dispatch()
    with nats_patch, dispatch_patch:
        record = await delegate_flow(
            db_session,
            account_id=str(account_id),
            parent_execution_id=execution.id,
            reference=reference,
            **kwargs,
        )
    assert _count_executions(db_session) == before
    assert record["status"]["state"] == "TASK_STATE_REJECTED"
    validate_delegation_task(record)
    return record


async def test_a_target_absent_from_callable_flows_is_refused(
    db_session, test_user, parent_flow, parent_execution
):
    """flow_not_callable: the flow exists, the allowlist does not name it."""
    _flow(db_session, name="Unlisted Flow", account_id=test_user.account_id)
    record = await _refuse(
        db_session, test_user.account_id, parent_execution, "Unlisted Flow"
    )
    assert record["metadata"]["preloop.ai/refusalReason"] == "flow_not_callable"
    assert "Unlisted Flow" in record["status"]["message"]["parts"][0]["text"]


async def test_an_unknown_reference_is_refused(
    db_session, test_user, parent_flow, parent_execution
):
    """flow_not_found: nothing in this account answers to that name."""
    record = await _refuse(
        db_session, test_user.account_id, parent_execution, "No Such Flow"
    )
    assert record["metadata"]["preloop.ai/refusalReason"] == "flow_not_found"


async def test_an_unknown_reference_names_the_flows_the_caller_may_call(
    db_session, test_user, parent_flow, parent_execution
):
    """Measured on 2026-09-18 (issue #647): an orchestrator called a lens
    by the result schema the lens emits rather than by its flow name, read
    "does not name a flow in this account" as proof the lens was
    uncallable, and reviewed nothing. The refusal now carries the
    allowlist, so the next call can be the right one."""
    record = await _refuse(
        db_session, test_user.account_id, parent_execution, "preloop.review.x/v1"
    )
    text = record["status"]["message"]["parts"][0]["text"]
    assert record["metadata"]["preloop.ai/refusalReason"] == "flow_not_found"
    assert "callable flows here: 'Child Flow'" in text


async def test_an_unlisted_target_names_the_flows_the_caller_may_call(
    db_session, test_user, parent_flow, parent_execution
):
    """The same hint on the other refusal: the flow exists, it is just not
    one this caller may start."""
    _flow(db_session, name="Unlisted Flow", account_id=test_user.account_id)
    record = await _refuse(
        db_session, test_user.account_id, parent_execution, "Unlisted Flow"
    )
    text = record["status"]["message"]["parts"][0]["text"]
    assert record["metadata"]["preloop.ai/refusalReason"] == "flow_not_callable"
    assert "callable flows here: 'Child Flow'" in text


async def test_a_caller_with_no_allowlist_says_so_rather_than_listing_nothing(
    db_session, test_user
):
    """An empty allowlist is a configuration answer, not an empty list."""
    caller = _flow(db_session, name="Lonely Flow", account_id=test_user.account_id)
    execution = _execution(db_session, caller)
    record = await _refuse(
        db_session, test_user.account_id, execution, "Anything At All"
    )
    text = record["status"]["message"]["parts"][0]["text"]
    assert "this flow has no callable flows configured" in text


async def test_the_hint_stops_at_ten_names_and_counts_the_rest(db_session, test_user):
    """A portfolio orchestrator may call many lenses; the refusal stays a
    sentence rather than becoming a catalogue."""
    from preloop.services.flow_delegation_call import callable_names_hint

    caller = _flow(
        db_session,
        name="Wide Flow",
        account_id=test_user.account_id,
        callable_flows=[{"flow": f"Lens {index}"} for index in range(13)],
    )
    hint = callable_names_hint(caller)
    assert "'Lens 0'" in hint
    assert "'Lens 9'" in hint
    assert "'Lens 10'" not in hint
    assert hint.endswith("and 3 more")


async def test_a_flow_in_another_account_is_refused_as_not_found(
    db_session, test_user, parent_flow, parent_execution
):
    """The account is the resolution scope: a neighbour's flow is invisible."""
    other = _other_account(db_session)
    _flow(db_session, name="Foreign Flow", account_id=other.id)

    record = await _refuse(
        db_session, test_user.account_id, parent_execution, "Foreign Flow"
    )
    assert record["metadata"]["preloop.ai/refusalReason"] == "flow_not_found"


async def test_a_disabled_target_is_refused(
    db_session, test_user, parent_flow, child_flow, parent_execution
):
    """flow_not_callable: an allowlisted flow that is switched off."""
    child_flow.is_enabled = False
    db_session.flush()
    record = await _refuse(
        db_session, test_user.account_id, parent_execution, "Child Flow"
    )
    assert record["metadata"]["preloop.ai/refusalReason"] == "flow_not_callable"


async def test_the_tool_absent_from_the_flows_tool_allowlist_is_refused(
    db_session, test_user, child_flow
):
    """tool_not_allowed: selecting the target is not selecting the tool."""
    caller = _flow(
        db_session,
        name="No Tool Flow",
        account_id=test_user.account_id,
        tools=("get_issue",),
        callable_flows=[{"flow": "Child Flow"}],
    )
    execution = _execution(db_session, caller)
    record = await _refuse(db_session, test_user.account_id, execution, "Child Flow")
    assert record["metadata"]["preloop.ai/refusalReason"] == "tool_not_allowed"


async def test_a_flow_calling_itself_is_refused(db_session, test_user):
    """cycle_detected: the caller's own flow is the first ancestor."""
    caller = _flow(
        db_session,
        name="Self Flow",
        account_id=test_user.account_id,
        callable_flows=[{"flow": "Self Flow", "allow_self": True}],
    )
    execution = _execution(db_session, caller)
    record = await _refuse(db_session, test_user.account_id, execution, "Self Flow")
    assert record["metadata"]["preloop.ai/refusalReason"] == "cycle_detected"
    assert "making this call" in record["status"]["message"]["parts"][0]["text"]


async def test_a_cycle_through_a_second_flow_is_refused(
    db_session, test_user, parent_flow, parent_execution
):
    """cycle_detected: A -> B -> A is caught by the ancestor walk."""
    second = _flow(
        db_session,
        name="Second Flow",
        account_id=test_user.account_id,
        callable_flows=[{"flow": "Parent Flow"}],
    )
    child = _execution(
        db_session, second, parent=parent_execution, depth=1, root=parent_execution
    )
    record = await _refuse(db_session, test_user.account_id, child, "Parent Flow")
    assert record["metadata"]["preloop.ai/refusalReason"] == "cycle_detected"
    assert "above this execution" in record["status"]["message"]["parts"][0]["text"]


async def test_a_call_above_the_maximum_depth_is_refused(
    db_session, test_user, parent_flow, child_flow, parent_execution, monkeypatch
):
    """depth_exceeded, with the limit named in the message."""
    from preloop.config import settings

    monkeypatch.setattr(settings, "flow_delegation_max_depth", 1)
    deep_flow = _flow(
        db_session,
        name="Deep Flow",
        account_id=test_user.account_id,
        callable_flows=[{"flow": "Child Flow"}],
    )
    deep = _execution(
        db_session, deep_flow, parent=parent_execution, depth=1, root=parent_execution
    )

    record = await _refuse(db_session, test_user.account_id, deep, "Child Flow")
    assert record["metadata"]["preloop.ai/refusalReason"] == "depth_exceeded"
    assert (
        "maximum delegation depth is 1"
        in (record["status"]["message"]["parts"][0]["text"])
    )


async def test_the_child_count_cap_refuses_the_next_child(
    db_session, test_user, parent_flow, child_flow, parent_execution, monkeypatch
):
    """fanout_exceeded, with the cap named in the message."""
    from preloop.config import settings

    monkeypatch.setattr(settings, "flow_delegation_max_children", 1)
    _execution(
        db_session,
        child_flow,
        parent=parent_execution,
        depth=1,
        root=parent_execution,
    )

    record = await _refuse(
        db_session, test_user.account_id, parent_execution, "Child Flow"
    )
    assert record["metadata"]["preloop.ai/refusalReason"] == "fanout_exceeded"
    assert (
        "maximum number of direct children is 1"
        in (record["status"]["message"]["parts"][0]["text"])
    )


async def test_the_per_entry_ceiling_refuses_the_next_child_of_that_flow(
    db_session, test_user, child_flow
):
    """fanout_exceeded: the allowlist entry's own max_children (#627)."""
    caller = _flow(
        db_session,
        name="Capped Flow",
        account_id=test_user.account_id,
        callable_flows=[{"flow": "Child Flow", "max_children": 1}],
    )
    execution = _execution(db_session, caller)
    _execution(db_session, child_flow, parent=execution, depth=1, root=execution)

    record = await _refuse(db_session, test_user.account_id, execution, "Child Flow")
    assert record["metadata"]["preloop.ai/refusalReason"] == "fanout_exceeded"
    assert "caps it at 1" in record["status"]["message"]["parts"][0]["text"]


async def test_every_refusal_reason_this_issue_implements_is_from_the_frozen_list(
    db_session, test_user, parent_flow, child_flow, parent_execution
):
    """No reason is invented here: #625 froze the vocabulary."""
    implemented = {
        "tool_not_allowed",
        "flow_not_found",
        "flow_not_callable",
        "depth_exceeded",
        "cycle_detected",
        "fanout_exceeded",
    }
    assert implemented <= set(REFUSAL_REASONS)


# --- audit -----------------------------------------------------------------


async def test_one_audit_row_per_refusal_carries_the_reason_code(
    db_session, test_user, parent_flow, parent_execution
):
    """A refusal is auditable: one row, the reason code, the correlation id."""
    correlation_id = str(uuid.uuid4())
    with patch(
        "preloop.services.dynamic_mcp_server._log_tool_execution_async"
    ) as logged:
        await delegate_flow(
            db_session,
            account_id=str(test_user.account_id),
            parent_execution_id=parent_execution.id,
            reference="No Such Flow",
            correlation_id=correlation_id,
        )

    logged.assert_called_once()
    kwargs = logged.call_args.kwargs
    assert kwargs["tool_name"] == "run_flow"
    assert kwargs["status"] == "refused:flow_not_found"
    assert kwargs["tool_args"]["refusal_reason"] == "flow_not_found"
    assert kwargs["correlation_id"] == correlation_id
    assert kwargs["execution_id"] == str(parent_execution.id)


async def test_each_refusal_reason_lands_its_own_audit_row(
    db_session, test_user, parent_flow, child_flow, parent_execution, monkeypatch
):
    """Six refused calls, six rows, six distinct reason codes."""
    from preloop.config import settings

    _flow(db_session, name="Unlisted Flow", account_id=test_user.account_id)
    no_tool_flow = _flow(
        db_session,
        name="Toolless Flow",
        account_id=test_user.account_id,
        tools=("get_issue",),
        callable_flows=[{"flow": "Child Flow"}],
    )
    self_flow = _flow(
        db_session,
        name="Looping Flow",
        account_id=test_user.account_id,
        callable_flows=[{"flow": "Looping Flow", "allow_self": True}],
    )
    capped_flow = _flow(
        db_session,
        name="Full Flow",
        account_id=test_user.account_id,
        callable_flows=[{"flow": "Child Flow", "max_children": 1}],
    )
    capped = _execution(db_session, capped_flow)
    _execution(db_session, child_flow, parent=capped, depth=1, root=capped)
    deep_flow = _flow(
        db_session,
        name="Too Deep Flow",
        account_id=test_user.account_id,
        callable_flows=[{"flow": "Child Flow"}],
    )
    deep = _execution(
        db_session, deep_flow, parent=parent_execution, depth=1, root=parent_execution
    )
    monkeypatch.setattr(settings, "flow_delegation_max_depth", 1)

    calls = [
        (_execution(db_session, no_tool_flow), "Child Flow", "tool_not_allowed"),
        (parent_execution, "No Such Flow", "flow_not_found"),
        (parent_execution, "Unlisted Flow", "flow_not_callable"),
        (deep, "Child Flow", "depth_exceeded"),
        (_execution(db_session, self_flow), "Looping Flow", "cycle_detected"),
        (capped, "Child Flow", "fanout_exceeded"),
    ]

    with patch(
        "preloop.services.dynamic_mcp_server._log_tool_execution_async"
    ) as logged:
        for execution, reference, expected in calls:
            record = await delegate_flow(
                db_session,
                account_id=str(test_user.account_id),
                parent_execution_id=execution.id,
                reference=reference,
            )
            assert record["metadata"]["preloop.ai/refusalReason"] == expected

    assert logged.call_count == len(calls)
    audited = [
        call.kwargs["tool_args"]["refusal_reason"] for call in logged.call_args_list
    ]
    assert audited == [expected for _, _, expected in calls]
    assert len(set(audited)) == len(calls)


async def test_a_permitted_call_writes_no_refusal_row(
    db_session, test_user, parent_flow, child_flow, parent_execution
):
    """Only refusals are audited here; executions audit themselves."""
    nats_patch, dispatch_patch = _patch_dispatch()
    with (
        nats_patch,
        dispatch_patch,
        patch(
            "preloop.services.dynamic_mcp_server._log_tool_execution_async"
        ) as logged,
    ):
        await delegate_flow(
            db_session,
            account_id=str(test_user.account_id),
            parent_execution_id=parent_execution.id,
            reference="Child Flow",
        )
    logged.assert_not_called()


# --- the kill switch and callers that are not executions -------------------


async def test_the_account_kill_switch_refuses_a_delegated_start(
    db_session, test_user, parent_flow, child_flow, parent_execution
):
    """The existing halt error, raised by the existing check, no child row."""
    from preloop.models.crud import crud_account_halt
    from preloop.models.models.account_halt import HALT_SCOPE_FLOWS

    crud_account_halt.set_scopes(
        db_session,
        account_id=test_user.account_id,
        scopes=[HALT_SCOPE_FLOWS],
        active=True,
        user_id=test_user.id,
        reason="delegation drill",
    )
    invalidate_kill_switch_cache(test_user.account_id)

    before = _count_executions(db_session)
    nats_patch, dispatch_patch = _patch_dispatch()
    with nats_patch, dispatch_patch:
        with pytest.raises(FlowHaltActiveError):
            await delegate_flow(
                db_session,
                account_id=str(test_user.account_id),
                parent_execution_id=parent_execution.id,
                reference="Child Flow",
            )
    assert _count_executions(db_session) == before


async def test_a_caller_that_is_not_a_flow_execution_cannot_delegate(
    db_session, test_user
):
    """No parent, no delegation: this is not a refusal, it is no caller."""
    with pytest.raises(DelegationUnavailableError):
        await delegate_flow(
            db_session,
            account_id=str(test_user.account_id),
            parent_execution_id=uuid.uuid4(),
            reference="Child Flow",
        )


async def test_an_execution_of_another_account_cannot_be_used_as_the_parent(
    db_session, test_user, parent_execution
):
    """The parent execution is read inside the calling account only."""
    other = _other_account(db_session)
    with pytest.raises(DelegationUnavailableError):
        await delegate_flow(
            db_session,
            account_id=str(other.id),
            parent_execution_id=parent_execution.id,
            reference="Child Flow",
        )


# --- helpers ---------------------------------------------------------------


async def test_resolution_never_leaves_the_account(db_session, test_user, child_flow):
    """The same reference resolves in one account and not in the other."""
    other = _other_account(db_session)
    assert (
        resolve_delegation_target(
            db_session, reference="Child Flow", account_id=test_user.account_id
        ).id
        == child_flow.id
    )
    assert (
        resolve_delegation_target(
            db_session, reference="Child Flow", account_id=other.id
        )
        is None
    )


async def test_the_ancestor_walk_lists_the_chain_from_caller_to_root(
    db_session, test_user, parent_flow, parent_execution
):
    """The cycle guard walks parent_execution_id, not a log."""
    middle_flow = _flow(db_session, name="Middle Flow", account_id=test_user.account_id)
    middle = _execution(
        db_session, middle_flow, parent=parent_execution, depth=1, root=parent_execution
    )
    assert ancestor_flow_ids(db_session, middle) == [
        str(middle_flow.id),
        str(parent_flow.id),
    ]


async def test_a_requested_timeout_is_clamped_to_the_parents_remaining_window(
    db_session, test_user, parent_flow, parent_execution
):
    """A child cannot be given more time than the caller has left."""
    parent_flow.timeout_seconds = 120
    db_session.flush()
    db_session.refresh(parent_execution)
    assert clamp_timeout(None, parent_execution=parent_execution) is None
    assert clamp_timeout(0, parent_execution=parent_execution) is None
    clamped = clamp_timeout(9999, parent_execution=parent_execution)
    assert clamped is not None and clamped <= 120
    assert clamp_timeout(30, parent_execution=parent_execution) == 30


async def test_evaluate_delegation_raises_rather_than_returning_a_reason(
    db_session, test_user, parent_flow, parent_execution
):
    """The rule engine is usable on its own; delegate_flow wraps it."""
    with pytest.raises(DelegationRefusedError) as refusal:
        evaluate_delegation(
            db_session,
            parent_execution=parent_execution,
            parent_flow=parent_flow,
            reference="No Such Flow",
        )
    assert refusal.value.reason == "flow_not_found"


async def test_a_refusal_is_kept_on_the_callers_timeline(
    db_session, test_user, parent_flow, parent_execution
):
    """A refused call creates no row, so the parent's log is its only trace.

    A parent that parks on its children (#633) reports one row per call it
    made, refusals included, and reads them back from here.
    """
    from preloop.models.crud import crud_flow_execution_log
    from preloop.services.flow_delegation_call import DELEGATION_REFUSAL_LOG_TYPE

    record = await delegate_flow(
        db_session,
        account_id=str(test_user.account_id),
        parent_execution_id=parent_execution.id,
        reference="No Such Flow",
        label="the one that never ran",
    )

    assert record["metadata"]["preloop.ai/refusalReason"] == "flow_not_found"
    rows = crud_flow_execution_log.list_by_type(
        db_session,
        execution_id=parent_execution.id,
        log_type=DELEGATION_REFUSAL_LOG_TYPE,
    )
    assert len(rows) == 1
    assert rows[0].metadata_["label"] == "the one that never ran"
    assert rows[0].metadata_["task"]["metadata"]["preloop.ai/refusalReason"] == (
        "flow_not_found"
    )
    validate_delegation_task(rows[0].metadata_["task"])
