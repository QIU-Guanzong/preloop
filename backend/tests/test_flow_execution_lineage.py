"""Lineage (parent, root, delegation depth) on flow executions.

The columns land before the writer that fills them, so these tests pin the
contract that writer will rely on: the three fields are on both execution
response shapes with the null, null and 0 defaults; creation stores a lineage
the caller passes and derives nothing on its own; and the children lookup is a
direct-children query with an order that cannot drift. The Postgres-level
behaviour - the foreign key, the migration and the real ordering - is in
``backend/tests/models``.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

from sqlalchemy.orm import Session

from preloop.models.crud.flow_execution import CRUDFlowExecution
from preloop.models.models.flow_execution import FlowExecution
from preloop.models.schemas.flow_execution import (
    FlowExecutionCreate,
    FlowExecutionListResponse,
    FlowExecutionResponse,
)

LINEAGE_FIELDS = {"parent_execution_id", "root_execution_id", "delegation_depth"}


def _execution(flow_id: uuid.UUID, **kwargs) -> FlowExecution:
    """An unpersisted execution row, as an API projection would receive one."""
    return FlowExecution(
        flow_id=flow_id,
        status="PENDING",
        start_time=datetime.now(UTC),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        **kwargs,
    )


# --------------------------------------------------------------------------
# Response schema
# --------------------------------------------------------------------------


def test_lineage_is_exposed_on_both_response_shapes():
    """The console reads the chain from a list row and from the detail page."""
    for schema in (FlowExecutionListResponse, FlowExecutionResponse):
        assert LINEAGE_FIELDS <= set(schema.model_fields)


def test_the_schema_defaults_a_row_without_lineage_to_null_null_zero():
    """A creation that knows nothing about lineage still reads as a chain head."""
    created = FlowExecutionCreate(flow_id=uuid.uuid4())

    assert created.parent_execution_id is None
    assert created.root_execution_id is None
    assert created.delegation_depth == 0


def test_a_row_without_lineage_projects_as_null_null_zero():
    """Existing rows must not fail or invent a link once the fields are exposed.

    ``delegation_depth`` is NOT NULL DEFAULT 0 in the database, so a row read
    back always carries the 0 even though the ids stay null.
    """
    execution = _execution(uuid.uuid4(), id=uuid.uuid4(), delegation_depth=0)

    listed = FlowExecutionListResponse.model_validate(execution)

    assert listed.parent_execution_id is None
    assert listed.root_execution_id is None
    assert listed.delegation_depth == 0


def test_a_recorded_lineage_projects_through_the_list_row():
    """Assigning the fields later is a projection change, not a second lookup."""
    root_id = uuid.uuid4()
    parent_id = uuid.uuid4()
    execution = _execution(
        uuid.uuid4(),
        id=uuid.uuid4(),
        parent_execution_id=parent_id,
        root_execution_id=root_id,
        delegation_depth=2,
    )

    listed = FlowExecutionListResponse.model_validate(execution)

    assert listed.parent_execution_id == parent_id
    assert listed.root_execution_id == root_id
    assert listed.delegation_depth == 2


# --------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------


def test_create_takes_no_lineage_argument_from_its_caller():
    """Nothing derives lineage: the values are inputs, not a computed chain."""
    assert set(inspect.signature(CRUDFlowExecution.create).parameters) == {
        "self",
        "db",
        "obj_in",
    }


def test_create_writes_the_defaults_when_no_lineage_is_given():
    """A webhook, matrix cell or scheduled run is a chain head by default."""
    db = MagicMock(spec=Session)

    execution = CRUDFlowExecution().create(
        db, FlowExecutionCreate(flow_id=uuid.uuid4(), status="PENDING")
    )

    assert execution.parent_execution_id is None
    assert execution.root_execution_id is None
    assert execution.delegation_depth == 0
    db.add.assert_called_once_with(execution)


def test_create_stores_a_callers_lineage_verbatim():
    """The run_flow writer passes the parent and root it resolved; nothing rewrites them."""
    db = MagicMock(spec=Session)
    parent_id = uuid.uuid4()
    root_id = uuid.uuid4()

    execution = CRUDFlowExecution().create(
        db,
        FlowExecutionCreate(
            flow_id=uuid.uuid4(),
            status="PENDING",
            parent_execution_id=parent_id,
            root_execution_id=root_id,
            delegation_depth=3,
        ),
    )

    assert execution.parent_execution_id == parent_id
    assert execution.root_execution_id == root_id
    assert execution.delegation_depth == 3
    db.add.assert_called_once_with(execution)


# --------------------------------------------------------------------------
# Children lookup
# --------------------------------------------------------------------------


def _recording_query() -> MagicMock:
    """A session query whose builder methods all return itself."""
    query = MagicMock()
    for method in ("filter", "order_by", "offset", "limit"):
        getattr(query, method).return_value = query
    return query


def test_get_children_matches_the_parent_column_only():
    """A chain is walked by parent; a grandchild must not show up as a child."""
    db = MagicMock(spec=Session)
    query = _recording_query()
    db.query.return_value = query
    child = _execution(uuid.uuid4(), id=uuid.uuid4())
    query.all.return_value = [child]
    parent_id = uuid.uuid4()

    children = CRUDFlowExecution().get_children(db, parent_id)

    db.query.assert_called_once_with(FlowExecution)
    assert children == [child]
    criterion = query.filter.call_args.args[0]
    assert str(criterion) == str(FlowExecution.parent_execution_id == parent_id)


def test_get_children_orders_oldest_first_then_by_id():
    """Ordering is pinned here because paging over an unstable order loses rows."""
    db = MagicMock(spec=Session)
    query = _recording_query()
    db.query.return_value = query

    CRUDFlowExecution().get_children(db, uuid.uuid4())

    assert [str(clause) for clause in query.order_by.call_args.args] == [
        "flow_execution.created_at ASC",
        "flow_execution.id ASC",
    ]
    # No page was asked for, so every child comes back.
    query.limit.assert_not_called()


def test_get_children_pages_when_asked():
    db = MagicMock(spec=Session)
    query = _recording_query()
    db.query.return_value = query

    CRUDFlowExecution().get_children(db, uuid.uuid4(), skip=10, limit=5)

    query.offset.assert_called_once_with(10)
    query.limit.assert_called_once_with(5)


def test_get_children_is_empty_for_a_leaf():
    """A leaf has no children; that is an empty list, not an error."""
    db = MagicMock(spec=Session)
    query = _recording_query()
    db.query.return_value = query
    query.all.return_value = []

    assert CRUDFlowExecution().get_children(db, uuid.uuid4()) == []
