"""Lineage (parent, root, delegation depth) persisted on flow executions.

The schema projection and the query shape are covered without a database in
``backend/tests/test_flow_execution_lineage.py``. These tests are the ones that
need Postgres: the foreign key actually refusing an unknown parent, the
children lookup really returning only direct children in a stable order, and a
creation path that knows nothing about lineage really writing the defaults.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError

from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.models.flow_execution import FlowExecution
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import (
    FlowExecutionCreate,
    FlowExecutionListResponse,
)

LINEAGE_COLUMNS = ("parent_execution_id", "root_execution_id", "delegation_depth")


def _create_flow(db_session, test_user, name):
    return crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=name,
            prompt_template="Test",
            trigger_event_source="github",
            trigger_event_types=["test"],
            agent_type="codex",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )


def _create_execution(db_session, flow, **lineage):
    return crud_flow_execution.create(
        db_session,
        FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED", **lineage),
    )


def test_creation_paths_that_do_not_set_lineage_write_the_defaults(
    db_session, test_user
):
    """Nothing writes lineage yet: a plain run is a chain head, from the DB up."""
    flow = _create_flow(db_session, test_user, "Lineage defaults")
    execution = _create_execution(db_session, flow)
    db_session.flush()

    # Reload from the server so the assertion is about the stored row and the
    # column defaults, not about the Python-side attributes of a new object.
    db_session.expire(execution)

    assert execution.parent_execution_id is None
    assert execution.root_execution_id is None
    assert execution.delegation_depth == 0


def test_create_stores_the_lineage_it_is_given(db_session, test_user):
    """The writer passes the parent and root it resolved; both are persisted."""
    flow = _create_flow(db_session, test_user, "Lineage stored")
    root = _create_execution(db_session, flow)
    child = _create_execution(
        db_session,
        flow,
        parent_execution_id=root.id,
        root_execution_id=root.id,
        delegation_depth=1,
    )
    db_session.flush()
    db_session.expire(child)

    assert child.parent_execution_id == root.id
    assert child.root_execution_id == root.id
    assert child.delegation_depth == 1


def test_the_foreign_key_refuses_a_parent_that_does_not_exist(db_session, test_user):
    """A link to a row that is not there is refused where it is made."""
    flow = _create_flow(db_session, test_user, "Lineage broken link")
    execution = FlowExecution(
        id=uuid.uuid4(),
        flow_id=flow.id,
        status="PENDING",
        parent_execution_id=uuid.uuid4(),
        delegation_depth=1,
    )
    db_session.add(execution)

    with pytest.raises(IntegrityError) as error:
        db_session.flush()

    assert "fk_flow_execution_parent_execution_id" in str(error.value)
    db_session.rollback()


def test_children_lookup_returns_only_direct_children_oldest_first(
    db_session, test_user
):
    """A grandchild is not a child, and the order is the one the console shows."""
    flow = _create_flow(db_session, test_user, "Lineage children")
    root = _create_execution(db_session, flow)
    # Written first but dated later: the order follows created_at, not the
    # order the rows happened to be inserted in.
    child = _create_execution(
        db_session,
        flow,
        parent_execution_id=root.id,
        root_execution_id=root.id,
        delegation_depth=1,
    )
    child.created_at = datetime(2026, 1, 2, tzinfo=UTC)
    grandchild = _create_execution(
        db_session,
        flow,
        parent_execution_id=child.id,
        root_execution_id=root.id,
        delegation_depth=2,
    )
    grandchild.created_at = datetime(2026, 1, 3, tzinfo=UTC)
    db_session.flush()
    later = _create_execution(
        db_session,
        flow,
        parent_execution_id=root.id,
        root_execution_id=root.id,
        delegation_depth=1,
    )
    later.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    db_session.flush()

    children = crud_flow_execution.get_children(db_session, root.id)

    assert [row.id for row in children] == [later.id, child.id]


def test_children_lookup_is_deterministic_when_timestamps_tie(db_session, test_user):
    """Same-transaction children share a timestamp, so the id breaks the tie."""
    flow = _create_flow(db_session, test_user, "Lineage tie")
    root = _create_execution(db_session, flow)
    children = [
        _create_execution(
            db_session,
            flow,
            parent_execution_id=root.id,
            root_execution_id=root.id,
            delegation_depth=1,
        )
        for _ in range(3)
    ]
    same_moment = datetime(2026, 2, 1, tzinfo=UTC)
    for child in children:
        child.created_at = same_moment
    db_session.flush()

    ordered = crud_flow_execution.get_children(db_session, root.id)

    assert [row.id for row in ordered] == sorted(
        (child.id for child in children), key=str
    )


def test_children_lookup_is_empty_for_a_leaf(db_session, test_user):
    flow = _create_flow(db_session, test_user, "Lineage leaf")
    leaf = _create_execution(db_session, flow)
    db_session.flush()

    assert crud_flow_execution.get_children(db_session, leaf.id) == []


def test_lightweight_list_rows_load_the_lineage_columns(db_session, test_user):
    """The list projection must carry lineage, not lazy-load it per row."""
    flow = _create_flow(db_session, test_user, "Lineage list")
    root = _create_execution(db_session, flow)
    child = _create_execution(
        db_session,
        flow,
        parent_execution_id=root.id,
        root_execution_id=root.id,
        delegation_depth=1,
    )
    db_session.flush()

    # Drop the identity map's fully-loaded copies so the assertion below is
    # about the list projection, not about rows this test happens to hold.
    db_session.expire_all()
    rows = crud_flow_execution.get_multi(
        db_session, account_id=str(test_user.account_id), lightweight=True
    )
    row = next(candidate for candidate in rows if candidate.id == child.id)

    assert set(LINEAGE_COLUMNS) & sa_inspect(row).unloaded == set()
    listed = FlowExecutionListResponse.model_validate(row)
    assert listed.parent_execution_id == root.id
    assert listed.root_execution_id == root.id
    assert listed.delegation_depth == 1
