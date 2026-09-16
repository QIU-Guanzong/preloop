"""Parking a parent on its children and resuming it when they finish (#633).

The park handshake is three durable steps across three processes, so every
test here drives the steps the way the platform does: ``wait_for_children``
writes the request, ``confirm_park`` is the orchestrator releasing the
container, and the resume is claimed by whatever notices the last child. What
is asserted is the row and the resumed turn, not the internals.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as _Row
from unittest.mock import AsyncMock, patch

import pytest

from tests.bound_session import bound_session_factory

from preloop.a2a.delegation import validate_delegation_task
from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.models.flow_execution import FlowExecution
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services import flow_child_wait
from preloop.services.approval_park import consumed_seconds_from_details
from preloop.services.flow_child_wait import (
    ChildOutcome,
    build_child_resume_details,
    children_prompt_block,
    is_terminal_child,
    resume_parent_if_ready,
    sweep_child_parks,
    wait_for_children,
)
from preloop.services.flow_delegation_call import (
    record_refusal_on_parent,
    refusal_record,
)

pytestmark = pytest.mark.asyncio


# --- harness ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _module_sessions(db_session, monkeypatch):
    """Every session the module opens is the test transaction.

    The module is written against short lived sessions it opens itself; the
    test suite runs inside one rolled back transaction. Binding the factory
    keeps the durable steps honest (real UPDATEs, real conditional claims)
    without leaking rows. The stand-in still looks like a Session so
    GitLab's INIT_TEST_DATA lifespan seed (next(get_db_session()).query)
    does not fail setup.
    """
    monkeypatch.setattr(
        "preloop.models.db.session.get_session_factory",
        lambda: bound_session_factory(db_session),
    )


@pytest.fixture(autouse=True)
def _no_dispatch():
    """A resume is created and linked, but nothing is actually started."""
    with (
        patch(
            "preloop.services.flow_execution_dispatcher.dispatch_execute",
            new_callable=AsyncMock,
        ),
        patch(
            "preloop.services.flow_execution_dispatcher.flow_execution_worker_enabled",
            return_value=True,
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def pinned_routing():
    """Model pinning is asserted on the call, not re-tested here.

    A resume must keep the model and harness of the run it continues (a
    native session cannot be restored into another harness), which needs a
    routing record the controller writes and an account-owned model row.
    That contract has its own tests; these ones assert that the pin is asked
    for with the parked execution as the source.
    """
    with patch(
        "preloop.services.model_routing.prepare_execution_routing",
        side_effect=lambda db, flow, details, **kwargs: details,
    ) as pin:
        yield pin


def _flow(db_session, account_id, name):
    return crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=name,
            prompt_template="do the thing",
            agent_type="codex",
            agent_config={},
            allowed_mcp_tools=[{"tool_name": "run_flow"}],
        ),
        account_id=account_id,
    )


def _execution(db_session, flow, *, parent=None, status="RUNNING", details=None, **kw):
    execution = crud_flow_execution.create(
        db_session,
        obj_in=FlowExecutionCreate(
            flow_id=flow.id,
            status=status,
            trigger_event_details=details,
            parent_execution_id=parent.id if parent is not None else None,
            root_execution_id=parent.id if parent is not None else None,
            delegation_depth=1 if parent is not None else 0,
            **kw,
        ),
    )
    db_session.flush()
    return execution


def _child(db_session, flow, parent, *, status="RUNNING", label=None, **kw):
    return _execution(
        db_session,
        flow,
        parent=parent,
        status=status,
        details={
            "source": "flow_delegation",
            "payload": {},
            "delegation": {
                "parent_execution_id": str(parent.id),
                "label": label,
                "depth": 1,
            },
        },
        **kw,
    )


def _finish(db_session, child, status, **columns):
    for key, value in columns.items():
        setattr(child, key, value)
    child.status = status
    db_session.flush()
    return child


def _park(db_session, parent, *, compute_seconds=120, expires_in=3600):
    """Run the two steps before the claim: request, then confirm."""
    wait_id = uuid.uuid4()
    crud_flow_execution.request_park(
        db_session,
        execution_id=parent.id,
        approval_request_id=wait_id,
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        kind="children",
    )
    crud_flow_execution.confirm_park(
        db_session,
        execution_id=parent.id,
        compute_seconds=compute_seconds,
        kind="children",
    )
    db_session.refresh(parent)
    return wait_id


def _resume_of(db_session, parent):
    db_session.refresh(parent)
    if parent.resume_execution_id is None:
        return None
    return crud_flow_execution.get(db_session, id=str(parent.resume_execution_id))


@pytest.fixture
def parent_flow(db_session, test_user):
    return _flow(db_session, test_user.account_id, "Parent Flow")


@pytest.fixture
def child_flow(db_session, test_user):
    return _flow(db_session, test_user.account_id, "Child Flow")


@pytest.fixture
def parent(db_session, parent_flow):
    return _execution(db_session, parent_flow, details={"payload": {"topic": "audit"}})


# --- the park --------------------------------------------------------------


async def test_a_parent_with_a_slow_child_parks_and_keeps_its_compute_seconds(
    db_session, test_user, parent, child_flow
):
    """Criterion 1: parked, no runtime, and the column excludes the waiting."""
    _child(db_session, child_flow, parent, status="RUNNING")

    answer = json.loads(
        await wait_for_children(
            account_id=test_user.account_id,
            parent_execution_id=parent.id,
            wait_seconds=0,
        )
    )

    assert answer["status"] == "parked_for_children"
    assert len(answer["waiting_for"]) == 1
    db_session.refresh(parent)
    assert parent.park_kind == "children"
    assert parent.park_request_id is not None

    # The orchestrator releasing the container is the second durable step.
    crud_flow_execution.confirm_park(
        db_session,
        execution_id=parent.id,
        compute_seconds=120,
        kind="children",
    )
    db_session.refresh(parent)
    assert parent.status == "WAITING_FOR_CHILDREN"
    assert parent.parked_compute_seconds == 120
    assert parent.agent_session_reference is None
    assert parent.end_time is None

    # Hours pass while parked. Nothing adds them to the column, so the run
    # that resumes this one is charged only the agent seconds actually spent.
    parent.parked_at = datetime.now(UTC) - timedelta(hours=6)
    db_session.flush()
    db_session.refresh(parent)
    assert parent.parked_compute_seconds == 120
    details = build_child_resume_details(parent, [])
    assert consumed_seconds_from_details(details) == 120


async def test_a_fast_child_inside_the_window_never_parks_the_parent(
    db_session, test_user, parent, child_flow
):
    """Criterion 6: nothing to park for, so no park request is written."""
    _child(db_session, child_flow, parent, status="SUCCEEDED", estimated_cost=0.25)

    answer = json.loads(
        await wait_for_children(
            account_id=test_user.account_id,
            parent_execution_id=parent.id,
            wait_seconds=5,
        )
    )

    assert answer["status"] == "children_finished"
    assert len(answer["children"]) == 1
    db_session.refresh(parent)
    assert parent.park_request_id is None
    assert parent.status == "RUNNING"


async def test_a_parent_that_started_nothing_has_nothing_to_wait_for(
    db_session, test_user, parent
):
    answer = json.loads(
        await wait_for_children(
            account_id=test_user.account_id,
            parent_execution_id=parent.id,
            wait_seconds=0,
        )
    )
    assert answer["status"] == "no_children"
    db_session.refresh(parent)
    assert parent.park_request_id is None


# --- the resume ------------------------------------------------------------


async def test_two_children_finishing_at_once_resume_the_parent_exactly_once(
    db_session, test_user, parent, child_flow
):
    """Criterion 2: the conditional claim is the whole idempotency story."""
    first = _child(db_session, child_flow, parent, label="one")
    second = _child(db_session, child_flow, parent, label="two")
    _park(db_session, parent)
    _finish(db_session, first, "SUCCEEDED")
    _finish(db_session, second, "SUCCEEDED")

    results = await asyncio.gather(
        resume_parent_if_ready(parent.id),
        resume_parent_if_ready(parent.id),
    )

    resumed = [item for item in results if item is not None]
    assert len(resumed) == 1
    db_session.refresh(parent)
    assert parent.status == "RESUMING"
    assert str(parent.resume_execution_id) == resumed[0].resume_execution_id
    started = (
        db_session.query(FlowExecution)
        .filter(
            FlowExecution.flow_id == parent.flow_id, FlowExecution.status == "PENDING"
        )
        .count()
    )
    assert started == 1


async def test_the_resumed_prompt_has_one_row_per_child_including_refused_and_failed(
    db_session, test_user, parent, parent_flow, child_flow
):
    """Criterion 3: a failed child, a refused call and a completed one."""
    done = _child(db_session, child_flow, parent, label="completed one")
    failed = _child(db_session, child_flow, parent, label="broken one")
    _finish(
        db_session,
        done,
        "SUCCEEDED",
        estimated_cost=1.5,
        result={"summary": "found nothing"},
    )
    _finish(db_session, failed, "FAILED", estimated_cost=0.5)
    record_refusal_on_parent(
        db_session,
        parent_execution_id=parent.id,
        record=refusal_record(
            reason="fanout_exceeded",
            message="too many children",
            flow_id=child_flow.id,
            flow_name=child_flow.name,
            depth=1,
            parent_execution_id=parent.id,
            root_execution_id=parent.id,
            attempt_id=str(uuid.uuid4()),
        ),
        label="refused one",
    )
    _park(db_session, parent)

    resumed = await resume_parent_if_ready(parent.id)

    assert resumed is not None
    resume = _resume_of(db_session, parent)
    prompt = resume.trigger_event_details["_children_prompt"]
    rows = [
        line
        for line in prompt.splitlines()
        if line.startswith("| ") and not line.startswith(("| execution", "| ---"))
    ]
    assert len(rows) == 3  # one per run_flow call, refusal included
    assert str(done.id) in prompt and "SUCCEEDED" in prompt
    assert str(failed.id) in prompt and "FAILED" in prompt
    assert "REFUSED (fanout_exceeded)" in prompt
    assert (
        "completed one" in prompt and "broken one" in prompt and "refused one" in prompt
    )
    assert "$1.5000" in prompt
    assert f"/flows/executions/{done.id}/result" in prompt

    children = resume.trigger_event_details["payload"]["children"]
    assert len(children) == 3
    for record in children.values():
        validate_delegation_task(record)
    assert children[str(done.id)]["artifacts"][0]["parts"][0]["data"] == {
        "summary": "found nothing"
    }
    assert resume.trigger_event_details["_resume"]["execution_id"] == str(parent.id)
    assert resume.trigger_event_details["payload"]["topic"] == "audit"


async def test_a_child_still_running_at_the_deadline_expires_and_the_parent_resumes(
    db_session, test_user, parent, child_flow
):
    """Criterion 4: an expired record per unfinished child, resumed anyway."""
    done = _child(db_session, child_flow, parent, label="quick")
    slow = _child(db_session, child_flow, parent, label="slow")
    _finish(db_session, done, "SUCCEEDED")
    _park(db_session, parent, expires_in=3600)
    parent.park_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.flush()

    resumed = await resume_parent_if_ready(parent.id)

    assert resumed is not None
    assert resumed.expired_child_ids == (str(slow.id),)
    resume = _resume_of(db_session, parent)
    record = resume.trigger_event_details["payload"]["children"][str(slow.id)]
    validate_delegation_task(record)
    assert "deadline passed" in record["status"]["message"]["parts"][0]["text"]
    assert "expired" in resume.trigger_event_details["_children_prompt"]
    db_session.refresh(slow)
    assert slow.status == "RUNNING"  # not stopped: that is #689


async def test_a_parent_is_not_resumed_while_a_child_is_still_running(
    db_session, test_user, parent, child_flow
):
    done = _child(db_session, child_flow, parent)
    _child(db_session, child_flow, parent)
    _finish(db_session, done, "SUCCEEDED")
    _park(db_session, parent)

    assert await resume_parent_if_ready(parent.id) is None
    db_session.refresh(parent)
    assert parent.status == "WAITING_FOR_CHILDREN"
    assert parent.resume_execution_id is None


async def test_a_stopped_or_refused_child_releases_the_parent_too(
    db_session, test_user, parent, child_flow
):
    """The release condition is terminal, not successful."""
    stopped = _child(db_session, child_flow, parent)
    _finish(db_session, stopped, "STOPPED")
    _park(db_session, parent)

    assert await resume_parent_if_ready(parent.id) is not None
    assert is_terminal_child("STOPPED")
    assert is_terminal_child("FAILED")
    assert is_terminal_child("REFUSED")
    assert not is_terminal_child("RUNNING")
    assert not is_terminal_child("WAITING_FOR_HUMAN")
    assert not is_terminal_child("A_STATUS_NOBODY_ADDED")


# --- the sweep -------------------------------------------------------------


async def test_the_sweep_resumes_a_parent_whose_resume_never_landed(
    db_session, test_user, parent, child_flow
):
    """Criterion 5: the crash between confirm and claim, recovered."""
    child = _child(db_session, child_flow, parent)
    _park(db_session, parent)
    _finish(db_session, child, "SUCCEEDED")
    # The process that should have claimed the park died here.

    counts = await sweep_child_parks()

    assert counts["resumed"] == 1
    resume = _resume_of(db_session, parent)
    assert resume is not None
    assert resume.status == "PENDING"


async def test_the_sweep_reclaims_a_claim_whose_resume_was_never_created(
    db_session, test_user, parent, child_flow
):
    """A crash after the claim leaves RESUMING with no child; the lease ends it."""
    child = _child(db_session, child_flow, parent)
    wait_id = _park(db_session, parent)
    _finish(db_session, child, "SUCCEEDED")
    assert crud_flow_execution.claim_parked_children_for_resume(
        db_session, execution_id=parent.id, wait_id=wait_id
    )
    parent.orchestrator_heartbeat_at = datetime.now(UTC) - timedelta(hours=2)
    db_session.flush()

    counts = await sweep_child_parks()

    assert counts["reclaimed"] == 1
    assert counts["resumed"] == 1
    assert _resume_of(db_session, parent) is not None


async def test_the_sweep_expires_a_parent_whose_deadline_passed(
    db_session, test_user, parent, child_flow
):
    slow = _child(db_session, child_flow, parent)
    _park(db_session, parent)
    parent.park_expires_at = datetime.now(UTC) - timedelta(minutes=5)
    db_session.flush()

    counts = await sweep_child_parks()

    assert counts["resumed"] == 1
    assert counts["expired"] == 1
    resume = _resume_of(db_session, parent)
    assert str(slow.id) in resume.trigger_event_details["payload"]["children"]


async def test_the_resume_keeps_the_place_of_a_parent_that_is_itself_a_child(
    db_session, test_user, parent_flow, child_flow
):
    """A parked parent's resume inherits its lineage, so the tree survives."""
    grandparent = _execution(db_session, parent_flow)
    middle = _child(db_session, parent_flow, grandparent)
    child = _child(db_session, child_flow, middle)
    _finish(db_session, child, "SUCCEEDED")
    _park(db_session, middle)

    assert await resume_parent_if_ready(middle.id) is not None

    resume = _resume_of(db_session, middle)
    assert str(resume.parent_execution_id) == str(grandparent.id)
    assert resume.delegation_depth == 1


async def test_the_resume_pins_the_model_and_harness_of_the_run_it_continues(
    db_session, test_user, parent, child_flow, pinned_routing
):
    child = _child(db_session, child_flow, parent)
    _finish(db_session, child, "SUCCEEDED")
    _park(db_session, parent)

    assert await resume_parent_if_ready(parent.id) is not None

    kwargs = pinned_routing.call_args.kwargs
    assert kwargs["pin_kind"] == "continuation"
    assert str(kwargs["source_execution"].id) == str(parent.id)


# --- the prompt block, on its own ------------------------------------------


def test_the_prompt_block_names_the_limitation_it_cannot_hide():
    """Results arrive as the next turn; the block has to say so."""
    block = children_prompt_block(
        [
            ChildOutcome(
                record={
                    "id": "e1",
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "metadata": {
                        "preloop.ai/kind": "delegation_task",
                        "preloop.ai/executionId": "e1",
                        "preloop.ai/flowId": "f1",
                        "preloop.ai/flowName": "Child Flow",
                        "preloop.ai/depth": 1,
                        "preloop.ai/status": "SUCCEEDED",
                    },
                },
                label="one",
            )
        ]
    )
    assert "as this turn" in block
    assert "not as the return value" in block
    assert "| e1 | Child Flow | one | SUCCEEDED |" in block
    assert "do not start these flows again" in block.lower()


def test_an_empty_wait_still_produces_a_readable_block():
    block = children_prompt_block([])
    assert "0 run_flow call(s)" in block


def test_the_in_process_window_and_deadline_come_from_configuration(monkeypatch):
    monkeypatch.setattr(flow_child_wait.settings, "flow_delegation_wait_seconds", 7)
    monkeypatch.setattr(
        flow_child_wait.settings, "flow_delegation_child_wait_seconds", 30
    )
    assert flow_child_wait.in_process_wait_seconds() == 7
    # A deadline shorter than a minute is a park that expires before the
    # orchestrator has finished releasing the container.
    assert flow_child_wait.child_wait_deadline_seconds() == 60


# --- the orchestrator side of the same handshake ---------------------------


class TestTheOrchestratorConfirmsTheRightPark:
    """One handshake, two kinds: the row says which status to write."""

    def _orchestrator(self):
        from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

        orchestrator = object.__new__(FlowExecutionOrchestrator)
        orchestrator.db = None
        orchestrator.execution_log = _Row(id=uuid.uuid4())
        orchestrator.flow = _Row(id=uuid.uuid4())
        orchestrator.tool_calls_count = 1
        orchestrator.total_tokens = 10
        orchestrator.estimated_cost = 0.1
        orchestrator._update_execution_log = AsyncMock()
        orchestrator._sync_runtime_session = lambda **kwargs: None
        orchestrator._notify_terminal = AsyncMock()
        orchestrator._start_queued_followup = AsyncMock()
        return orchestrator

    @pytest.mark.asyncio
    async def test_a_children_park_confirms_waiting_for_children(self, monkeypatch):
        from preloop.services import flow_orchestrator as module

        confirmed = {}
        monkeypatch.setattr(
            module.crud_flow_execution,
            "confirm_park",
            lambda db, **kwargs: confirmed.update(kwargs),
        )
        orchestrator = self._orchestrator()

        await orchestrator._finalize_park(
            agent_result={
                "park": {
                    "kind": "children",
                    "approval_request_id": str(uuid.uuid4()),
                    "compute_seconds": 140,
                },
                "actions_taken": [],
            },
            output_summary=None,
            merged_result=None,
        )

        kwargs = orchestrator._update_execution_log.await_args.kwargs
        assert "status" not in kwargs
        assert "end_time" not in kwargs
        assert confirmed["kind"] == "children"
        assert confirmed["compute_seconds"] == 140
        orchestrator._notify_terminal.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_park_without_a_kind_is_still_a_human_park(self, monkeypatch):
        from preloop.services import flow_orchestrator as module

        confirmed = {}
        monkeypatch.setattr(
            module.crud_flow_execution,
            "confirm_park",
            lambda db, **kwargs: confirmed.update(kwargs),
        )
        orchestrator = self._orchestrator()

        await orchestrator._finalize_park(
            agent_result={"park": {"compute_seconds": 5}},
            output_summary=None,
            merged_result=None,
        )

        assert "status" not in orchestrator._update_execution_log.await_args.kwargs
        assert confirmed["kind"] == "human"

    def test_waiting_for_children_is_not_a_terminal_status(self):
        from preloop.services.flow_orchestrator import (
            PARKED_STATUSES,
            TERMINAL_EXECUTION_STATUSES,
        )

        assert "WAITING_FOR_CHILDREN" in PARKED_STATUSES
        assert "WAITING_FOR_HUMAN" in PARKED_STATUSES
        assert "WAITING_FOR_CHILDREN" not in TERMINAL_EXECUTION_STATUSES

    def test_the_park_kind_decides_the_status_and_typos_do_not_invent_one(self):
        assert crud_flow_execution.parked_status_for_kind("children") == (
            "WAITING_FOR_CHILDREN"
        )
        assert (
            crud_flow_execution.parked_status_for_kind("human") == "WAITING_FOR_HUMAN"
        )
        assert crud_flow_execution.parked_status_for_kind(None) == "WAITING_FOR_HUMAN"
        assert crud_flow_execution.parked_status_for_kind("childrn") == (
            "WAITING_FOR_HUMAN"
        )
