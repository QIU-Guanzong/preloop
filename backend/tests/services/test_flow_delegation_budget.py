"""What a delegation tree may cost, and what happens when it cannot (#631).

Two properties are worth more than the rest and every test here serves one
of them: a child the tree cannot afford never gets a row, and a child that
is already running is never killed to pay for a later one.
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from preloop.a2a.delegation import REFUSAL_REASONS, validate_delegation_task
from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.models.flow_execution import FlowExecution
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services import flow_delegation_budget as budget
from preloop.services.flow_delegation_call import delegate_flow
from preloop.services.flow_trigger_service import FlowTriggerService

pytestmark = pytest.mark.asyncio


# --- fixtures --------------------------------------------------------------


def _flow(db_session, *, name, account_id, tools=("run_flow",), callable_flows=None):
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=name,
            prompt_template="t",
            agent_type="codex",
            agent_config={},
            allowed_mcp_tools=[{"tool_name": tool} for tool in tools],
        ),
        account_id=account_id,
    )
    flow.callable_flows = callable_flows
    db_session.flush()
    db_session.refresh(flow)
    return flow


def _execution(
    db_session,
    flow,
    *,
    parent=None,
    depth=0,
    root=None,
    status="RUNNING",
    cost=None,
    ceiling=None,
):
    """One execution row, optionally a delegated child with a ceiling."""
    details = None
    if ceiling is not None:
        details = {
            "source": "flow_delegation",
            budget.DELEGATION_DETAILS_KEY: {budget.COST_CEILING_KEY: ceiling},
        }
    execution = crud_flow_execution.create(
        db_session,
        obj_in=FlowExecutionCreate(
            flow_id=flow.id,
            status=status,
            parent_execution_id=parent.id if parent is not None else None,
            root_execution_id=root.id if root is not None else None,
            delegation_depth=depth,
            trigger_event_details=details,
        ),
    )
    if cost is not None:
        execution.estimated_cost = Decimal(str(cost))
    db_session.flush()
    return execution


def _count_executions(db_session):
    return db_session.query(FlowExecution.id).count()


def _patch_dispatch():
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
    return _flow(db_session, name="Child Flow", account_id=test_user.account_id)


@pytest.fixture
def parent_flow(db_session, test_user, child_flow):
    return _flow(
        db_session,
        name="Parent Flow",
        account_id=test_user.account_id,
        callable_flows=[{"flow": "Child Flow"}],
    )


@pytest.fixture
def parent_execution(db_session, parent_flow):
    return _execution(db_session, parent_flow)


@pytest.fixture
def ceilings(monkeypatch):
    """Set the two instance ceilings for one test, in USD."""

    def _set(*, tree, per_child):
        monkeypatch.setattr(
            budget.settings, "flow_delegation_max_tree_usd", tree, raising=False
        )
        monkeypatch.setattr(
            budget.settings,
            "flow_delegation_default_child_usd",
            per_child,
            raising=False,
        )

    return _set


async def _delegate(db_session, parent_execution, account_id, **kwargs):
    nats_patch, dispatch_patch = _patch_dispatch()
    with nats_patch, dispatch_patch:
        return await delegate_flow(
            db_session,
            account_id=str(account_id),
            parent_execution_id=parent_execution.id,
            reference="Child Flow",
            **kwargs,
        )


def _child_ceiling_of(db_session, record):
    child = crud_flow_execution.get(
        db_session, id=record["metadata"]["preloop.ai/executionId"]
    )
    delegation = child.trigger_event_details[budget.DELEGATION_DETAILS_KEY]
    return child, delegation[budget.COST_CEILING_KEY]


def _refusal(record):
    assert record["status"]["state"] == "TASK_STATE_REJECTED"
    validate_delegation_task(record)
    return (
        record["metadata"]["preloop.ai/refusalReason"],
        record["status"]["message"]["parts"][0]["text"],
    )


# --- the ceiling one child is admitted under -------------------------------


def test_the_entry_ceiling_clamps_a_larger_argument():
    """The operator's per child cap wins over the agent's ask."""
    assert budget.resolve_child_ceiling(requested=10.0, entry_ceiling=1.5) == 1.5


def test_an_argument_below_the_entry_ceiling_is_honoured():
    assert budget.resolve_child_ceiling(requested=0.5, entry_ceiling=1.5) == 0.5


def test_an_unnamed_ceiling_falls_back_to_the_entry_then_the_default(monkeypatch):
    monkeypatch.setattr(
        budget.settings, "flow_delegation_default_child_usd", 2.0, raising=False
    )
    assert budget.resolve_child_ceiling(requested=None, entry_ceiling=3.0) == 3.0
    assert budget.resolve_child_ceiling(requested=None, entry_ceiling=None) == 2.0


def test_a_nonsense_ceiling_is_read_as_no_ceiling(monkeypatch):
    """Zero, negative, non numeric and non finite are not budgets."""
    monkeypatch.setattr(
        budget.settings, "flow_delegation_default_child_usd", 2.0, raising=False
    )
    for asked in (0, -1, "free", None, float("nan"), float("inf"), float("-inf")):
        assert budget.resolve_child_ceiling(requested=asked, entry_ceiling=None) == 2.0


def test_positive_usd_rejects_nan_and_infinity():
    """NaN is not a ceiling: recording it would poison every later comparison."""
    assert budget._positive(float("nan")) is None
    assert budget._positive(float("inf")) is None
    assert budget._positive(float("-inf")) is None
    assert budget._positive(1.5) == 1.5
    nan_row = SimpleNamespace(
        trigger_event_details={
            budget.DELEGATION_DETAILS_KEY: {budget.COST_CEILING_KEY: float("nan")}
        }
    )
    assert budget.recorded_ceiling(nan_row) is None


def test_zero_means_no_default_child_ceiling(monkeypatch):
    monkeypatch.setattr(
        budget.settings, "flow_delegation_default_child_usd", 0, raising=False
    )
    assert budget.resolve_child_ceiling(requested=None, entry_ceiling=None) is None


async def test_the_entry_ceiling_is_the_value_stored_on_the_child(
    db_session, test_user, parent_flow, child_flow, parent_execution, ceilings
):
    """Acceptance: the clamp is asserted on the child, not on the argument."""
    ceilings(tree=50.0, per_child=2.0)
    parent_flow.callable_flows = [{"flow": "Child Flow", "max_usd_per_child": 1.5}]
    db_session.flush()

    record = await _delegate(
        db_session, parent_execution, test_user.account_id, max_cost_usd=10.0
    )

    validate_delegation_task(record)
    child, ceiling = _child_ceiling_of(db_session, record)
    assert ceiling == 1.5
    assert child.parent_execution_id == parent_execution.id


async def test_a_child_nobody_named_a_ceiling_for_takes_the_default(
    db_session, test_user, parent_flow, child_flow, parent_execution, ceilings
):
    ceilings(tree=50.0, per_child=2.0)
    record = await _delegate(db_session, parent_execution, test_user.account_id)
    _, ceiling = _child_ceiling_of(db_session, record)
    assert ceiling == 2.0


# --- refusing before the child exists --------------------------------------


async def test_a_child_the_tree_cannot_afford_is_refused_before_a_row_exists(
    db_session, test_user, parent_flow, child_flow, parent_execution, ceilings
):
    """Acceptance: no execution row, and the reason names what is left."""
    ceilings(tree=5.0, per_child=2.0)
    before = _count_executions(db_session)

    record = await _delegate(
        db_session, parent_execution, test_user.account_id, max_cost_usd=9.0
    )

    reason, message = _refusal(record)
    assert reason == "budget_exceeded"
    assert reason in REFUSAL_REASONS
    assert "5.00 USD left" in message
    assert "9.00 USD" in message
    assert _count_executions(db_session) == before


async def test_spend_from_finished_children_is_counted_against_the_tree(
    db_session, test_user, parent_flow, child_flow, parent_execution, ceilings
):
    """Acceptance: two finished children, and a third refused because of them."""
    ceilings(tree=5.0, per_child=2.0)
    _execution(
        db_session,
        child_flow,
        parent=parent_execution,
        root=parent_execution,
        depth=1,
        status="SUCCEEDED",
        cost="2.25",
        ceiling=2.0,
    )
    _execution(
        db_session,
        child_flow,
        parent=parent_execution,
        root=parent_execution,
        depth=1,
        status="SUCCEEDED",
        cost="2.00",
        ceiling=2.0,
    )
    before = _count_executions(db_session)

    record = await _delegate(
        db_session, parent_execution, test_user.account_id, max_cost_usd=2.0
    )

    reason, message = _refusal(record)
    assert reason == "budget_exceeded"
    # 5.00 allowance minus 4.25 spent by the two that finished.
    assert "0.75 USD left" in message
    assert _count_executions(db_session) == before


async def test_a_child_that_fits_in_what_is_left_still_starts(
    db_session, test_user, parent_flow, child_flow, parent_execution, ceilings
):
    """The ceiling refuses the next child, it does not close the tree."""
    ceilings(tree=5.0, per_child=2.0)
    _execution(
        db_session,
        child_flow,
        parent=parent_execution,
        root=parent_execution,
        depth=1,
        status="SUCCEEDED",
        cost="4.50",
        ceiling=2.0,
    )

    record = await _delegate(
        db_session, parent_execution, test_user.account_id, max_cost_usd=0.5
    )

    validate_delegation_task(record)
    assert record["status"]["state"] in {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}
    _, ceiling = _child_ceiling_of(db_session, record)
    assert ceiling == 0.5


async def test_a_running_child_is_counted_at_its_ceiling_not_its_spend(
    db_session, test_user, parent_flow, child_flow, parent_execution, ceilings
):
    """Work that has not been paid for yet must not fund a second fan out."""
    ceilings(tree=5.0, per_child=2.0)
    for _ in range(2):
        _execution(
            db_session,
            child_flow,
            parent=parent_execution,
            root=parent_execution,
            depth=1,
            status="RUNNING",
            cost="0.01",
            ceiling=2.0,
        )

    record = await _delegate(
        db_session, parent_execution, test_user.account_id, max_cost_usd=2.0
    )

    reason, message = _refusal(record)
    assert reason == "budget_exceeded"
    assert "1.00 USD left" in message


async def test_a_finished_child_releases_what_it_did_not_spend(
    db_session, test_user, parent_flow, child_flow, parent_execution, ceilings
):
    """A reservation is not a charge: a cheap child gives the rest back."""
    ceilings(tree=5.0, per_child=2.0)
    for _ in range(2):
        _execution(
            db_session,
            child_flow,
            parent=parent_execution,
            root=parent_execution,
            depth=1,
            status="SUCCEEDED",
            cost="0.10",
            ceiling=2.0,
        )

    record = await _delegate(
        db_session, parent_execution, test_user.account_id, max_cost_usd=2.0
    )

    validate_delegation_task(record)
    assert "preloop.ai/refusalReason" not in record["metadata"]


# --- nothing running is killed to make room --------------------------------


async def test_children_already_running_are_untouched_by_a_refusal(
    db_session, test_user, parent_flow, child_flow, parent_execution, ceilings
):
    """Acceptance: a ceiling reached later does not kill what is running."""
    ceilings(tree=4.0, per_child=2.0)
    running = [
        _execution(
            db_session,
            child_flow,
            parent=parent_execution,
            root=parent_execution,
            depth=1,
            status="RUNNING",
            ceiling=2.0,
        )
        for _ in range(2)
    ]

    record = await _delegate(
        db_session, parent_execution, test_user.account_id, max_cost_usd=2.0
    )
    assert _refusal(record)[0] == "budget_exceeded"

    for child in running:
        db_session.refresh(child)
        assert child.status == "RUNNING"
        assert child.stop_requested_at is None
        assert child.error_message is None

    # And they go on to finish, which is the point of refusing early.
    for child in running:
        child.status = "SUCCEEDED"
        child.estimated_cost = Decimal("1.00")
    db_session.flush()
    for child in running:
        db_session.refresh(child)
        assert child.status == "SUCCEEDED"


async def test_a_budget_refusal_does_not_look_like_a_failure_of_the_parent(
    db_session, test_user, parent_flow, child_flow, parent_execution, ceilings
):
    """Acceptance: the parent is still running and carries no error."""
    ceilings(tree=1.0, per_child=2.0)

    record = await _delegate(
        db_session, parent_execution, test_user.account_id, max_cost_usd=5.0
    )

    assert _refusal(record)[0] == "budget_exceeded"
    db_session.refresh(parent_execution)
    assert parent_execution.status == "RUNNING"
    assert parent_execution.error_message is None
    assert parent_execution.stop_requested_at is None


async def test_the_budget_refusal_is_audited_with_its_own_reason_code(
    db_session, test_user, parent_flow, child_flow, parent_execution, ceilings
):
    """Acceptance: one audit row, reason code distinct from the other rules."""
    ceilings(tree=1.0, per_child=2.0)
    correlation_id = str(uuid.uuid4())

    with patch(
        "preloop.services.dynamic_mcp_server._log_tool_execution_async"
    ) as audit:
        await _delegate(
            db_session,
            parent_execution,
            test_user.account_id,
            max_cost_usd=5.0,
            correlation_id=correlation_id,
        )

    assert audit.call_count == 1
    kwargs = audit.call_args.kwargs
    assert kwargs["status"] == "refused:budget_exceeded"
    assert kwargs["tool_name"] == "run_flow"
    assert kwargs["execution_id"] == str(parent_execution.id)
    assert kwargs["correlation_id"] == correlation_id
    assert kwargs["tool_args"]["refusal_reason"] == "budget_exceeded"
    assert "1.00 USD left" in kwargs["tool_args"]["refusal_message"]


# --- a ceiling covers a subtree --------------------------------------------


async def test_a_grandchild_must_fit_inside_its_parents_ceiling(
    db_session, test_user, parent_flow, child_flow, ceilings
):
    """A cap cannot be avoided by delegating one level deeper."""
    ceilings(tree=50.0, per_child=2.0)
    grandchild_flow = _flow(
        db_session, name="Grandchild Flow", account_id=test_user.account_id
    )
    child_flow.callable_flows = [{"flow": grandchild_flow.name}]
    db_session.flush()

    root = _execution(db_session, parent_flow)
    child = _execution(
        db_session, child_flow, parent=root, root=root, depth=1, ceiling=1.0
    )
    before = _count_executions(db_session)

    nats_patch, dispatch_patch = _patch_dispatch()
    with nats_patch, dispatch_patch:
        record = await delegate_flow(
            db_session,
            account_id=str(test_user.account_id),
            parent_execution_id=child.id,
            reference="Grandchild Flow",
            max_cost_usd=5.0,
        )

    reason, message = _refusal(record)
    assert reason == "budget_exceeded"
    assert "1.00 USD left" in message
    assert _count_executions(db_session) == before


async def test_a_grandchild_that_fits_inside_that_ceiling_starts(
    db_session, test_user, parent_flow, child_flow, ceilings
):
    ceilings(tree=50.0, per_child=2.0)
    _flow(db_session, name="Grandchild Flow", account_id=test_user.account_id)
    child_flow.callable_flows = [{"flow": "Grandchild Flow"}]
    db_session.flush()

    root = _execution(db_session, parent_flow)
    child = _execution(
        db_session, child_flow, parent=root, root=root, depth=1, ceiling=4.0
    )

    nats_patch, dispatch_patch = _patch_dispatch()
    with nats_patch, dispatch_patch:
        record = await delegate_flow(
            db_session,
            account_id=str(test_user.account_id),
            parent_execution_id=child.id,
            reference="Grandchild Flow",
            max_cost_usd=3.0,
        )

    validate_delegation_task(record)
    _, ceiling = _child_ceiling_of(db_session, record)
    assert ceiling == 3.0


async def test_a_grandchild_that_fits_its_parent_but_not_the_tree_is_refused(
    db_session, test_user, parent_flow, child_flow, ceilings
):
    """Every allowance above the call has to hold, not just the nearest."""
    ceilings(tree=6.0, per_child=2.0)
    _flow(db_session, name="Grandchild Flow", account_id=test_user.account_id)
    child_flow.callable_flows = [{"flow": "Grandchild Flow"}]
    db_session.flush()

    root = _execution(db_session, parent_flow)
    child = _execution(
        db_session, child_flow, parent=root, root=root, depth=1, ceiling=5.0
    )
    # A sibling of that child eats most of the tree allowance.
    _execution(
        db_session,
        child_flow,
        parent=root,
        root=root,
        depth=1,
        status="SUCCEEDED",
        cost="4.00",
        ceiling=5.0,
    )

    nats_patch, dispatch_patch = _patch_dispatch()
    with nats_patch, dispatch_patch:
        record = await delegate_flow(
            db_session,
            account_id=str(test_user.account_id),
            parent_execution_id=child.id,
            reference="Grandchild Flow",
            max_cost_usd=4.0,
        )

    reason, _message = _refusal(record)
    assert reason == "budget_exceeded"


# --- what the instance ceilings mean ---------------------------------------


async def test_zero_removes_the_instance_tree_ceiling(
    db_session, test_user, parent_flow, child_flow, parent_execution, ceilings
):
    ceilings(tree=0, per_child=2.0)
    record = await _delegate(
        db_session, parent_execution, test_user.account_id, max_cost_usd=1000.0
    )
    validate_delegation_task(record)
    _, ceiling = _child_ceiling_of(db_session, record)
    assert ceiling == 1000.0


async def test_an_unbounded_child_inside_a_bounded_tree_is_refused(
    db_session, test_user, parent_flow, child_flow, parent_execution, ceilings
):
    """A ceiling of "none" cannot be honoured inside an allowance."""
    ceilings(tree=5.0, per_child=0)
    before = _count_executions(db_session)

    record = await _delegate(db_session, parent_execution, test_user.account_id)

    reason, message = _refusal(record)
    assert reason == "budget_exceeded"
    assert "max_cost_usd" in message
    assert _count_executions(db_session) == before


async def test_the_parents_own_spend_counts_against_its_tree(
    db_session, test_user, parent_flow, child_flow, parent_execution, ceilings
):
    """A tree ceiling covers the run that started it, not only its children."""
    ceilings(tree=5.0, per_child=2.0)
    parent_execution.estimated_cost = Decimal("4.50")
    db_session.flush()

    record = await _delegate(
        db_session, parent_execution, test_user.account_id, max_cost_usd=2.0
    )

    reason, message = _refusal(record)
    assert reason == "budget_exceeded"
    assert "0.50 USD left" in message


# --- the order the rules run in --------------------------------------------


async def test_a_cycle_is_refused_before_the_budget_is_consulted(
    db_session, test_user, parent_flow, ceilings
):
    """Money is the last rule: a call that breaks two rules names the first."""
    ceilings(tree=0.01, per_child=2.0)
    parent_flow.callable_flows = [{"flow": "Parent Flow", "allow_self": True}]
    db_session.flush()
    execution = _execution(db_session, parent_flow)

    nats_patch, dispatch_patch = _patch_dispatch()
    with nats_patch, dispatch_patch:
        record = await delegate_flow(
            db_session,
            account_id=str(test_user.account_id),
            parent_execution_id=execution.id,
            reference="Parent Flow",
            max_cost_usd=100.0,
        )

    assert _refusal(record)[0] == "cycle_detected"


# --- attribution: one fan out is one batch ---------------------------------


async def test_siblings_of_one_fan_out_share_a_batch_id(
    db_session, test_user, parent_flow, child_flow, parent_execution, ceilings
):
    ceilings(tree=50.0, per_child=2.0)
    records = [
        await _delegate(
            db_session, parent_execution, test_user.account_id, max_cost_usd=1.0
        )
        for _ in range(3)
    ]

    batches = set()
    for record in records:
        child = crud_flow_execution.get(
            db_session, id=record["metadata"]["preloop.ai/executionId"]
        )
        batches.add(str(child.batch_id))
    assert len(batches) == 1
    assert batches == {str(budget.fanout_batch_id(parent_execution.id))}


async def test_two_parents_fan_out_into_two_batches(
    db_session, test_user, parent_flow, child_flow, ceilings
):
    ceilings(tree=50.0, per_child=2.0)
    first = _execution(db_session, parent_flow)
    second = _execution(db_session, parent_flow)
    assert budget.fanout_batch_id(first.id) != budget.fanout_batch_id(second.id)

    records = [
        await _delegate(db_session, first, test_user.account_id),
        await _delegate(db_session, second, test_user.account_id),
    ]
    batches = {
        str(
            crud_flow_execution.get(
                db_session, id=record["metadata"]["preloop.ai/executionId"]
            ).batch_id
        )
        for record in records
    }
    assert len(batches) == 2


async def test_the_batch_rollup_endpoint_reports_the_fan_out_total(
    client, db_session, test_user, parent_flow, child_flow, parent_execution, ceilings
):
    """Acceptance: the existing endpoint sums the children, no new query."""
    ceilings(tree=50.0, per_child=2.0)
    costs = ["0.25", "1.50"]
    children = []
    for _ in costs:
        record = await _delegate(
            db_session, parent_execution, test_user.account_id, max_cost_usd=2.0
        )
        children.append(
            crud_flow_execution.get(
                db_session, id=record["metadata"]["preloop.ai/executionId"]
            )
        )
    for child, cost in zip(children, costs, strict=False):
        child.status = "SUCCEEDED"
        child.estimated_cost = Decimal(cost)
        child.total_tokens = 100
        child.tool_calls_count = 2
    db_session.flush()

    batch_id = str(budget.fanout_batch_id(parent_execution.id))
    response = client.get(f"/api/v1/flows/batches/{batch_id}/executions")

    assert response.status_code == 200, response.text
    rollup = response.json()["rollup"]
    assert rollup["total"] == 2
    assert rollup["total_estimated_cost"] == pytest.approx(1.75)
    assert rollup["total_estimated_cost"] == pytest.approx(
        sum(float(child.estimated_cost) for child in children)
    )
    assert rollup["total_tokens"] == 200
    assert rollup["total_tool_calls"] == 4
    assert rollup["completed"] == 2


# --- reading the tree ------------------------------------------------------


async def test_the_tree_is_read_by_root_execution_id(
    db_session, test_user, parent_flow, child_flow, parent_execution
):
    """Descendants at any depth come back from one indexed query."""
    child = _execution(
        db_session, child_flow, parent=parent_execution, root=parent_execution, depth=1
    )
    grandchild = _execution(
        db_session, child_flow, parent=child, root=parent_execution, depth=2
    )

    rows = crud_flow_execution.get_by_root(
        db_session,
        root_execution_id=parent_execution.id,
        account_id=test_user.account_id,
    )

    assert {str(row.id) for row in rows} == {str(child.id), str(grandchild.id)}


async def test_the_tree_read_does_not_cross_accounts(
    db_session, test_user, parent_flow, child_flow, parent_execution
):
    _execution(
        db_session, child_flow, parent=parent_execution, root=parent_execution, depth=1
    )
    rows = crud_flow_execution.get_by_root(
        db_session,
        root_execution_id=parent_execution.id,
        account_id=uuid.uuid4(),
    )
    assert rows == []


async def test_a_tree_snapshot_holds_an_advisory_lock(
    db_session, test_user, parent_execution
):
    """Concurrent admissions serialize on the root, not on a best-effort read."""
    budget.tree_budgets(
        db_session,
        parent_execution=parent_execution,
        account_id=test_user.account_id,
    )
    held = db_session.execute(
        text(
            "SELECT COUNT(*) FROM pg_locks "
            "WHERE locktype = 'advisory' AND granted AND pid = pg_backend_pid()"
        )
    ).scalar()
    assert held >= 1
