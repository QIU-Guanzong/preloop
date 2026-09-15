"""Execution lineage: creation-time values and the direct-children lookup.

Lineage is written by later work (the ``run_flow`` child, continuations,
retries); what lands here is the storage contract that work relies on. These
tests pin the three properties a tree built on top of it needs: a row
remembers the lineage it was created with, an unset lineage reads back as no
parent/no root/depth 0, and the children lookup returns direct children only,
in a stable order.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from preloop.models.crud import crud_ai_model, crud_flow, crud_flow_execution
from preloop.models.models import FlowExecution
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate


def _make_flow(db_session, account, name="Lineage Flow"):
    """A flow owned by ``account``, with the model row FlowCreate requires."""
    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": f"Lineage Model {uuid.uuid4()}",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
            "api_key": "provider-secret",
        },
        account_id=account.id,
    )
    return crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=f"{name} {uuid.uuid4()}",
            prompt_template="Test",
            trigger_event_source="manual",
            trigger_event_types=["test"],
            ai_model_id=ai_model.id,
            agent_type="codex",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=account.id,
        ),
        account_id=account.id,
    )


def _create_execution(db_session, flow, **lineage):
    execution = crud_flow_execution.create(
        db_session,
        FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED", **lineage),
    )
    db_session.flush()
    return execution


def test_create_stores_parent_root_and_depth(db_session, create_account):
    """A child row round-trips the lineage it was created with."""
    account = create_account()
    flow = _make_flow(db_session, account)
    root = _create_execution(db_session, flow)
    child = _create_execution(
        db_session,
        flow,
        parent_execution_id=root.id,
        root_execution_id=root.id,
        delegation_depth=1,
    )

    stored = db_session.query(FlowExecution).filter(FlowExecution.id == child.id).one()

    assert stored.parent_execution_id == root.id
    assert stored.root_execution_id == root.id
    assert stored.delegation_depth == 1


def test_create_defaults_lineage_for_a_trigger_started_run(db_session, create_account):
    """A trigger-started execution is not a child of anything."""
    account = create_account()
    flow = _make_flow(db_session, account)

    execution = _create_execution(db_session, flow)

    assert execution.parent_execution_id is None
    assert execution.root_execution_id is None
    assert execution.delegation_depth == 0


def test_creation_refuses_a_parent_that_does_not_exist(db_session, create_account):
    """The self-referencing FK is what keeps a tree from dangling."""
    account = create_account()
    flow = _make_flow(db_session, account)

    with pytest.raises(IntegrityError):
        _create_execution(
            db_session,
            flow,
            parent_execution_id=uuid.uuid4(),
            root_execution_id=uuid.uuid4(),
            delegation_depth=1,
        )
    db_session.rollback()


def test_list_children_returns_only_direct_children(db_session, create_account):
    """One call returns one level, and a leaf returns an empty list."""
    account = create_account()
    flow = _make_flow(db_session, account)
    root = _create_execution(db_session, flow)
    first = _create_execution(
        db_session,
        flow,
        parent_execution_id=root.id,
        root_execution_id=root.id,
        delegation_depth=1,
    )
    second = _create_execution(
        db_session,
        flow,
        parent_execution_id=root.id,
        root_execution_id=root.id,
        delegation_depth=1,
    )
    grandchild = _create_execution(
        db_session,
        flow,
        parent_execution_id=first.id,
        root_execution_id=root.id,
        delegation_depth=2,
    )

    root_children = crud_flow_execution.list_children(db_session, root.id)

    assert {row.id for row in root_children} == {first.id, second.id}
    assert grandchild.id not in {row.id for row in root_children}
    assert [
        row.id for row in crud_flow_execution.list_children(db_session, first.id)
    ] == [grandchild.id]
    assert crud_flow_execution.list_children(db_session, grandchild.id) == []


def test_list_children_is_ordered_oldest_first(db_session, create_account):
    """Order comes from created_at, not from the order rows were inserted."""
    account = create_account()
    flow = _make_flow(db_session, account)
    root = _create_execution(db_session, flow)
    newer = _create_execution(
        db_session,
        flow,
        parent_execution_id=root.id,
        root_execution_id=root.id,
        delegation_depth=1,
    )
    older = _create_execution(
        db_session,
        flow,
        parent_execution_id=root.id,
        root_execution_id=root.id,
        delegation_depth=1,
    )
    now = datetime.now(timezone.utc)
    newer.created_at = now
    older.created_at = now - timedelta(minutes=5)
    db_session.flush()

    rows = crud_flow_execution.list_children(db_session, root.id)

    assert [row.id for row in rows] == [older.id, newer.id]


def test_list_children_is_deterministic_when_timestamps_collide(
    db_session, create_account
):
    """Equal created_at values must not shuffle rows between two reads."""
    account = create_account()
    flow = _make_flow(db_session, account)
    root = _create_execution(db_session, flow)
    children = [
        _create_execution(
            db_session,
            flow,
            parent_execution_id=root.id,
            root_execution_id=root.id,
            delegation_depth=1,
        )
        for _ in range(4)
    ]
    shared_created_at = datetime.now(timezone.utc)
    for child in children:
        child.created_at = shared_created_at
    db_session.flush()

    first_read = crud_flow_execution.list_children(db_session, root.id)
    second_read = crud_flow_execution.list_children(db_session, root.id)

    assert [row.id for row in first_read] == [row.id for row in second_read]
    assert [row.id for row in first_read] == sorted(row.id for row in children)


def test_list_children_can_be_scoped_to_an_account(db_session, create_account):
    """The lookup never crosses an account boundary when one is given."""
    account = create_account()
    other_account = create_account()
    flow = _make_flow(db_session, account)
    foreign_flow = _make_flow(db_session, other_account)
    root = _create_execution(db_session, flow)
    child = _create_execution(
        db_session,
        flow,
        parent_execution_id=root.id,
        root_execution_id=root.id,
        delegation_depth=1,
    )
    foreign_child = _create_execution(
        db_session,
        foreign_flow,
        parent_execution_id=root.id,
        root_execution_id=root.id,
        delegation_depth=1,
    )

    scoped = crud_flow_execution.list_children(
        db_session, root.id, account_id=account.id
    )

    assert [row.id for row in scoped] == [child.id]
    assert foreign_child.id not in {row.id for row in scoped}
